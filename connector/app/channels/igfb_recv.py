"""Facebook / Instagram webhook 事件解析（接收側）。

從舊單體 `app/channels/igfb.py` 搬入「webhook 事件解析」這一段。
Graph API 的歷史同步 / 發送（fb_conversations / fb_send_text …）不搬 ——
那些屬於舊單體 igfb_db 的職責，不在 Connector 收訊範圍。

平台原生 ID（多租戶反查）：IG/FB webhook 的 `entry.id`（FB = page id；IG = ig user id）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core import config


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


def _from_parts(frm: dict) -> tuple[str, str]:
    """取出 sender 的 (id, 顯示名)。FB 用 name、IG 用 username，互為 fallback。"""
    if not frm:
        return "", ""
    fid = str(frm.get("id") or "")
    name = frm.get("name") or frm.get("username") or ""
    return fid, name


def _norm_message(msg: dict) -> dict:
    """正規化 message object → (message_type, text, attachments)。"""
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


_SYS_NOTICE_PHRASES = (
    "创建这个聊天是因为",
    "創建這個聊天是因為",
    "creating this chat because",
    "created this chat because",
    "评论了你的帖子",
    "評論了你的貼文",
    "commented on your post",
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


def self_id(platform: str) -> str:
    """我方在這個平台的身分 id（FB = 粉專 id；IG = IG 專業帳號 id）。"""
    return config.META_PAGE_ID if platform == "fb" else config.META_IG_USER_ID


def is_self(platform: str, from_id: str) -> bool:
    """這個 id 是不是我方（用來避免自動回復回覆到自己發出的訊息／留言）。"""
    sid = self_id(platform)
    return bool(sid) and bool(from_id) and str(from_id) == str(sid)


def _dt_or_now(value) -> datetime:
    return to_dt(value) or datetime.now(timezone.utc)


def _parse_dm(obj: str, m: dict, entry_time) -> dict | None:
    msg = m.get("message") or {}
    if not msg:
        return None
    sender = m.get("sender") or {}
    sender_id, sender_name = _from_parts(sender)
    norm = _norm_message(msg)
    if not sender_id:
        return None
    platform = "fb" if obj == "page" else "ig"
    if msg.get("is_echo") or is_self(platform, sender_id):
        return None
    if is_system_dm_notice(norm["text"]):
        return None
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


def _parse_fb_comment(value: dict, entry_id: str) -> dict | None:
    if value.get("verb") not in ("add", "edited"):
        return None
    comment_id = str(value.get("comment_id") or "")
    if not comment_id:
        return None
    frm = value.get("from") or {}
    fid, fname = _from_parts(frm)
    post_id = str(value.get("post_id") or "")
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


def extract_events(body: dict) -> list[dict]:
    """把 Meta webhook POST 拆成正規化事件清單。

    只處理 page / instagram（whatsapp 由 webhook 層另走既有流程）。
    回傳每個事件帶 type：
      fb_dm / ig_dm       私訊
      fb_comment / ig_comment  留言
    其餘（post 新增、已讀回執、貼文本身等）忽略。

    每個事件都帶 `account_native_id`（平台原生 ID，取自 entry.id）——
    這是 CRM 多租戶反查商家的對齊鍵。
    """
    events: list[dict] = []
    obj = body.get("object", "")
    if obj not in ("page", "instagram"):
        return events

    for entry in body.get("entry", []):
        entry_id = str(entry.get("id") or "")

        for m in entry.get("messaging", []) or []:
            ev = _parse_dm(obj, m, entry.get("time"))
            if ev:
                ev["account_native_id"] = entry_id
                events.append(ev)

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
                ev["account_native_id"] = entry_id
                events.append(ev)

    return events
