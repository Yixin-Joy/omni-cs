"""Facebook / Instagram 私訊與貼文留言：Graph API 讀取/發送 + webhook 事件解析。

路徑對照：
  IG 私訊         → Instagram 登入路徑（IGAA token + graph.instagram.com，打 /me）
  IG 貼文 / 留言  → FB 登入路徑（page token + graph.facebook.com + IG business id）
  FB 私訊/貼文/留言 → page token + graph.facebook.com + page id

所有函式都只「讀」或「依上層指令發送」，不自行落地 DB；DB 寫入由 igfb_db 負責。
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

import config

log = logging.getLogger("wechat-forwarder")

FB_BASE = "https://graph.facebook.com"


def ig_base() -> str:
    return config.ig_graph_host()


# ---------- 時間 ----------

def to_dt(value) -> datetime | None:
    """把 epoch 秒 / ISO 字串統一轉成 tz-aware datetime，無法解析回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        except (ValueError, OSError):
            return None


def iso(dt: datetime | None) -> str:
    return dt.isoformat().replace("+00:00", "Z") if dt else ""


# ---------- 低層 HTTP ----------

async def _get(url: str, token: str, params: dict | None = None, timeout: float = 25):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        return r.status_code, _as_json(r)


async def _post(url: str, token: str, payload: dict, timeout: float = 25):
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        return r.status_code, r.text


def _as_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _ver() -> str:
    return config.META_GRAPH_VERSION or "v21.0"


# ---------- 訊息正規化（FB Messenger 與 IG 私訊結構共通） ----------

def _from_parts(frm: dict) -> tuple[str, str]:
    """取出 sender 的 (id, 顯示名)。FB 用 name、IG 用 username，互為 fallback。"""
    if not frm:
        return "", ""
    fid = str(frm.get("id") or "")
    name = frm.get("name") or frm.get("username") or ""
    return fid, name


def _norm_message(msg: dict) -> dict:
    """正規化 message object → (message_type, text, attachments)。

    相容兩種來源：webhook 事件為 {text, attachments}；Graph API 回傳為
    {message: "文字"}（message 可能是字串）。attachments 統一為 [{type, url}]。
    """
    # Graph API 回傳：文字在最外層 message 欄位（可能是純字串）
    if "text" not in msg and ("message" in msg or "attachments" in msg or "sticker_id" in msg):
        raw = msg.get("message")
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, dict):
            text = str(raw.get("text") or "")
        else:
            text = str(raw or "")
    else:
        text = str(msg.get("text") or "")

    raw_atts = msg.get("attachments") or []
    attachments: list[dict] = []
    for a in raw_atts:
        if not isinstance(a, dict):
            continue
        a_type = a.get("type") or "file"
        payload = a.get("payload") or {}
        url = payload.get("url") or a.get("url") or ""
        attachments.append({"type": a_type, "url": url})

    sticker_id = msg.get("sticker_id")
    if sticker_id:
        attachments.append({"type": "sticker", "url": "", "sticker_id": str(sticker_id)})

    if attachments:
        t = attachments[0]["type"]
        mtype = t if t in ("image", "audio", "video", "file", "sticker") else "file"
        return {"message_type": mtype, "text": text, "attachments": attachments}
    return {"message_type": "text", "text": text, "attachments": []}


# ---------- FB「留言轉私訊」系統通知 ----------
#
# 使用者在粉專貼文留言後，FB 會建立一個 comment-to-DM 會話：把留言原文複製進
# Messenger，並附上這段系統說明（內容隨語言而異）。對客服工作台來說這是純噪音——
# 它會被同步成「未讀私訊」、把真實對話的預覽蓋掉，還可能被當成客戶問題丟給自動回復。
# 這種會話無法從 participants 分辨（PSID 跟一般私訊相同），只能靠文字特徵識別。
# 每組都是高度特異的片語，命中任一組即視為系統通知。

_SYS_NOTICE_PHRASES = (
    # 這則對話不是對方開的，是 FB 因留言而建立的
    "创建这个聊天是因为",
    "創建這個聊天是因為",
    "creating this chat because",
    "created this chat because",
    # 留言事件本身
    "评论了你的帖子",
    "評論了你的貼文",
    "commented on your post",
    # 七天後自動消失
    "天后消失",
    "天後消失",
    "disappear after 7 days",
)


def is_system_dm_notice(text: str) -> bool:
    """判斷是否為 FB「留言轉私訊」的系統通知（不是真人訊息）。"""
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    return any(p.lower() in low for p in _SYS_NOTICE_PHRASES)


# ---------- FB 私訊（Messenger） ----------

async def fb_conversations(limit: int = 50) -> list[dict]:
    """回傳粉專的 Messenger 會話清單（含 participants / updated_time / snippet）。"""
    if not config.META_PAGE_ID or not config.META_PAGE_ACCESS_TOKEN:
        return []
    url = f"{FB_BASE}/{_ver()}/{config.META_PAGE_ID}/conversations"
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN,
                              params={"fields": "participants,updated_time,snippet", "limit": limit})
    if status != 200:
        log.warning("fb_conversations 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def fb_thread_messages(thread_id: str, limit: int = 100) -> list[dict]:
    """回傳某 Messenger 會話的訊息清單。"""
    url = f"{FB_BASE}/{_ver()}/{thread_id}/messages"
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN,
                              params={"fields": "message,from,created_time,attachments{type,payload},sticker",
                                      "limit": limit})
    if status != 200:
        log.warning("fb_thread_messages 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def fb_send_text(psid: str, text: str) -> dict:
    url = f"{FB_BASE}/{_ver()}/{config.META_PAGE_ID}/messages"
    payload = {"recipient": {"id": psid}, "message": {"text": text}}
    status, body = await _post(url, config.META_PAGE_ACCESS_TOKEN, payload)
    return {"status": status, "body": body}


async def fb_send_attachment(psid: str, mime_type: str, url: str) -> dict:
    atype = "image" if mime_type.startswith("image/") else "file"
    payload = {
        "recipient": {"id": psid},
        "message": {"attachment": {"type": atype, "payload": {"url": url, "is_reusable": True}}},
    }
    status, body = await _post(f"{FB_BASE}/{_ver()}/{config.META_PAGE_ID}/messages",
                               config.META_PAGE_ACCESS_TOKEN, payload)
    return {"status": status, "body": body}


# ---------- FB 貼文 / 留言 ----------

async def fb_posts(limit: int = 10, after: str = "") -> dict:
    url = f"{FB_BASE}/{_ver()}/{config.META_PAGE_ID}/feed"
    # 不可加 type：該欄位已棄用，一加整個 /feed 就回 400（deprecate_post_aggregated_fields_for_attachement）。
    # full_picture 取貼文主圖；attachments 當備援。
    params = {"fields": "id,message,created_time,permalink_url,full_picture,attachments{media{image{src}}}",
              "limit": limit}
    if after:
        params["after"] = after
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN, params=params)
    if status != 200:
        log.warning("fb_posts 失敗 %s: %s", status, data)
        return {"data": [], "after": ""}
    posts = data.get("data", [])
    for p in posts:
        if not (p.get("full_picture") or p.get("media_url")):
            # 備援：從 attachments 取第一張圖
            for a in (p.get("attachments") or {}).get("data", []) or []:
                src = ((a.get("media") or {}).get("image") or {}).get("src")
                if src:
                    p["media_url"] = src
                    break
    return {"data": posts, "after": _next_cursor(data)}


async def fb_comments(post_id: str, limit: int = 100) -> list[dict]:
    url = f"{FB_BASE}/{_ver()}/{post_id}/comments"
    fields = "id,message,from{name,id},created_time,like_count,comments{id,message,from{name,id},created_time,like_count}"
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN, params={"fields": fields, "limit": limit})
    if status != 200:
        log.warning("fb_comments 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def fb_reply_comment(comment_id: str, text: str) -> dict:
    url = f"{FB_BASE}/{_ver()}/{comment_id}/comments"
    status, body = await _post(url, config.META_PAGE_ACCESS_TOKEN, {"message": text})
    return {"status": status, "body": body}


# ---------- IG 私訊（Instagram 登入路徑） ----------

async def ig_conversations(limit: int = 50) -> list[dict]:
    if not config.META_IG_ACCESS_TOKEN:
        return []
    url = f"{ig_base()}/{_ver()}/me/conversations"
    status, data = await _get(url, config.META_IG_ACCESS_TOKEN,
                              params={"fields": "participants,updated_time,snippet", "limit": limit})
    if status != 200:
        log.warning("ig_conversations 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def ig_thread_messages(thread_id: str, limit: int = 100) -> list[dict]:
    url = f"{ig_base()}/{_ver()}/{thread_id}/messages"
    status, data = await _get(url, config.META_IG_ACCESS_TOKEN,
                              params={"fields": "message,from,created_time,attachments{type,payload}", "limit": limit})
    if status != 200:
        log.warning("ig_thread_messages 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def ig_send_text(recipient_id: str, text: str) -> dict:
    url = f"{ig_base()}/{_ver()}/me/messages"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    status, body = await _post(url, config.META_IG_ACCESS_TOKEN, payload)
    return {"status": status, "body": body}


async def ig_send_attachment(recipient_id: str, mime_type: str, url: str) -> dict:
    atype = "image" if mime_type.startswith("image/") else "file"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"attachment": {"type": atype, "payload": {"url": url}}},
    }
    status, body = await _post(f"{ig_base()}/{_ver()}/me/messages", config.META_IG_ACCESS_TOKEN, payload)
    return {"status": status, "body": body}


async def ig_user_name(igsid: str) -> str:
    """取 IG 使用者的顯示名稱（username）。

    IG 私訊的 webhook 事件不帶使用者名稱，要另外查這個端點補上。
    """
    if not igsid or not config.META_IG_ACCESS_TOKEN:
        return ""
    url = f"{ig_base()}/{_ver()}/{igsid}"
    status, data = await _get(url, config.META_IG_ACCESS_TOKEN, params={"fields": "username,name"})
    if status != 200:
        return ""
    return str(data.get("username") or data.get("name") or "")


# ---------- IG 貼文 / 留言（FB 登入路徑） ----------

async def ig_posts(limit: int = 10, after: str = "") -> dict:
    url = f"{FB_BASE}/{_ver()}/{config.META_IG_USER_ID}/media"
    params = {"fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count",
              "limit": limit}
    if after:
        params["after"] = after
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN, params=params)
    if status != 200:
        log.warning("ig_posts 失敗 %s: %s", status, data)
        return {"data": [], "after": ""}
    return {"data": data.get("data", []), "after": _next_cursor(data)}


async def ig_comments(media_id: str, limit: int = 100) -> list[dict]:
    url = f"{FB_BASE}/{_ver()}/{media_id}/comments"
    fields = "id,text,timestamp,like_count,from{username,id},replies{id,text,timestamp,like_count,from{username,id}}"
    status, data = await _get(url, config.META_PAGE_ACCESS_TOKEN, params={"fields": fields, "limit": limit})
    if status != 200:
        log.warning("ig_comments 失敗 %s: %s", status, data)
        return []
    return data.get("data", [])


async def ig_reply_comment(comment_id: str, text: str) -> dict:
    url = f"{FB_BASE}/{_ver()}/{comment_id}/replies"
    status, body = await _post(url, config.META_PAGE_ACCESS_TOKEN, {"message": text})
    return {"status": status, "body": body}


def _next_cursor(data: dict) -> str:
    try:
        return data.get("paging", {}).get("cursors", {}).get("after", "") or ""
    except Exception:
        return ""


# ---------- 媒體下載 ----------

async def download_media(url: str, save_path: str) -> None:
    """下載 FB/IG 的媒體（簽名 CDN url，無需 auth），存到 save_path。"""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(resp.content)


def media_ext(url: str, mime_type: str = "") -> str:
    """從 url/mime 猜副檔名，猜不到給 .bin。"""
    if mime_type and "/" in mime_type:
        ext = mime_type.split("/")[-1].split(";")[0].strip()
        if ext and len(ext) <= 5:
            return ext
    path = url.split("?")[0]
    suffix = Path(path).suffix.lstrip(".")
    return suffix if suffix and len(suffix) <= 5 else "bin"


# ---------- webhook 事件解析 ----------

def extract_events(body: dict) -> list[dict]:
    """把 Meta webhook POST 拆成正規化事件清單。

    只處理 page / instagram（whatsapp 由 main.py 另走既有流程）。
    回傳每個事件帶 type：
      fb_dm / ig_dm    私訊
      fb_comment / ig_comment  留言
    其餘（post 新增、已讀回執、貼文本身等）忽略。
    """
    events: list[dict] = []
    obj = body.get("object", "")
    if obj not in ("page", "instagram"):
        return events

    for entry in body.get("entry", []):
        entry_id = str(entry.get("id") or "")

        # 1) 私訊（FB Messenger 用 messaging，IG 登入路徑也用 messaging）
        for m in entry.get("messaging", []) or []:
            ev = _parse_dm(obj, m, entry.get("time"))
            if ev:
                events.append(ev)

        # 2) 留言（page.feed 與 instagram.comments）
        for change in entry.get("changes", []) or []:
            field = change.get("field", "")
            value = change.get("value", {}) or {}
            if obj == "page" and field == "feed" and value.get("item") == "comment":
                ev = _parse_fb_comment(value, entry_id)
            elif obj == "instagram" and field == "comments":
                ev = _parse_ig_comment(value, entry_id)
            else:
                continue
            if ev:
                events.append(ev)

    return events


def self_id(platform: str) -> str:
    """我方在這個平台的身分 id（FB = 粉專 id；IG = IG 專業帳號 id）。"""
    return config.META_PAGE_ID if platform == "fb" else config.META_IG_USER_ID


def is_self(platform: str, from_id: str) -> bool:
    """這個 id 是不是我方（用來避免自動回復回覆到自己發出的訊息／留言）。"""
    sid = self_id(platform)
    return bool(sid) and bool(from_id) and str(from_id) == str(sid)


def _parse_dm(obj: str, m: dict, entry_time) -> dict | None:
    msg = m.get("message") or {}
    if not msg:
        return None
    sender = m.get("sender") or {}
    sender_id, sender_name = _from_parts(sender)
    norm = _norm_message(msg)
    # 私訊事件都是 inbound（客戶來訊）；echo / 我方發送的回執不算
    if not sender_id:
        return None
    # 我方送出的訊息會以 echo 回拋；sender 就是我方 id。若當成客戶來訊，
    # 自動回復會回覆自己，形成無限循環 —— 這裡必須擋掉。
    platform = "fb" if obj == "page" else "ig"
    if msg.get("is_echo") or is_self(platform, sender_id):
        return None
    # FB「留言轉私訊」的系統通知：不是真人訊息，別當私訊落地
    # （否則會變成未讀、蓋掉真實對話的預覽）
    if is_system_dm_notice(norm["text"]):
        return None
    # 時間來源（依優先序）：message.timestamp > entry.time > 現在。
    # IG/FB 私訊的 webhook 常不帶 timestamp，若不兜底會存成 null，導致列表無法排序 / 未讀算不出。
    ts = m.get("timestamp") or msg.get("timestamp") or entry_time
    created_at = to_dt(ts)
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    return {
        "type": "fb_dm" if obj == "page" else "ig_dm",
        "platform": platform,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_id": str(msg.get("mid") or msg.get("id") or ""),
        "message_type": norm["message_type"],
        "text": norm["text"],
        "attachments": norm["attachments"],
        "created_at": created_at,
        "direction": "inbound",
    }


def _dt_or_now(value) -> datetime:
    """解析時間欄位；缺失時退回「現在」。

    IG 的 webhook 留言事件不帶任何時間欄位，若不兜底會存成 null，
    導致留言列表無法排序、時間顯示為空。
    """
    return to_dt(value) or datetime.now(timezone.utc)


def _parse_fb_comment(value: dict, entry_id: str) -> dict | None:
    if value.get("verb") not in ("add", "edited"):
        return None
    comment_id = str(value.get("comment_id") or "")
    if not comment_id:
        return None
    frm = value.get("from") or {}
    fid, fname = _from_parts(frm)
    post_id = str(value.get("post_id") or "")
    # FB webhook 對「頂層留言」也會帶 parent_id = post_id；這不算回覆，要正規化為 None，
    # 否則會被誤判成子留言，導致不計入新留言、也追溯不到討論串根。
    parent_id = str(value.get("parent_id") or "") or None
    if parent_id and post_id and parent_id == post_id:
        parent_id = None
    return {
        "type": "fb_comment",
        "platform": "fb",
        "post_id": post_id,
        "comment_id": comment_id,
        "parent_id": parent_id,
        "from_id": fid,
        "from_name": fname or "用戶",
        "text": str(value.get("message") or ""),
        "like_count": int(value.get("like_count") or 0),
        "created_at": _dt_or_now(value.get("created_time")),
    }


def _parse_ig_comment(value: dict, entry_id: str) -> dict | None:
    comment_id = str(value.get("id") or value.get("comment_id") or "")
    if not comment_id:
        return None
    media_id = value.get("media", {})
    if isinstance(media_id, dict):
        media_id = media_id.get("id", "")
    return {
        "type": "ig_comment",
        "platform": "ig",
        "post_id": str(media_id or ""),
        "comment_id": comment_id,
        "parent_id": str(value.get("parent_id") or "") or None,
        "from_id": str(value.get("from", {}).get("id") or "") if isinstance(value.get("from"), dict) else "",
        "from_name": str(value.get("username") or value.get("from", {}).get("username") or "用戶"),
        "text": str(value.get("text") or ""),
        "like_count": int(value.get("like_count") or 0),
        "created_at": _dt_or_now(value.get("created_time") or value.get("timestamp")),
    }


# ---------- 回覆分派（給 console 用） ----------

async def send_dm(platform: str, recipient_id: str, message_type: str, text: str, file_url: str, mime: str) -> dict:
    """發送私訊回覆。message_type: text / file（file 的 mime 決定 image 或 file）。"""
    if platform == "ig":
        if message_type == "file" and file_url:
            return await ig_send_attachment(recipient_id, mime, file_url)
        return await ig_send_text(recipient_id, text)
    # fb
    if message_type == "file" and file_url:
        return await fb_send_attachment(recipient_id, mime, file_url)
    return await fb_send_text(recipient_id, text)


async def reply_comment(platform: str, comment_id: str, text: str) -> dict:
    """回覆貼文留言。IG 需要 instagram_manage_comments 權限。"""
    if platform == "ig":
        return await ig_reply_comment(comment_id, text)
    return await fb_reply_comment(comment_id, text)
