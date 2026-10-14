"""Facebook / Instagram 發送側（私訊回覆 + 貼文留言回覆）。

從舊單體 `app/channels/igfb.py` 搬入**發送**這一段（收訊在 `igfb_recv.py`）。
與舊版的差異：**憑證改由呼叫端傳入**（多租戶，真相在 CRM 的
`crm_business_platform_account.credential`，經帳號映射快取到 Connector），
不再讀 `config` / `.env`。

平台憑證欄位（`account_map.CREDENTIAL_KEYS['fb' | 'ig']`）：

- `fb`：`page_id`（粉專 id）、`page_access_token`（EAA…）
- `ig`：`ig_user_id`、`ig_access_token`（IGAA…，私訊用）、
        `page_access_token`（EAA…，留言回覆走 graph.facebook.com 用）

⚠️ IG 的**私訊**走 `graph.instagram.com` + IGAA token；
   IG 的**留言回覆**走 `graph.facebook.com` + 粉專 token（與舊單體一致）。
"""
from __future__ import annotations

import logging

import httpx

from app.channels import base as _base

log = logging.getLogger("connector")

FB_BASE = "https://graph.facebook.com"
IG_BASE = "https://graph.instagram.com"


def _ver() -> str:
    from app.core import config
    return getattr(config, "GRAPH_API_VERSION", "") or "v21.0"


async def _post(url: str, token: str, payload: dict, timeout: float = 25):
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        return r.status_code, r.text


async def _get_json(url: str, token: str, params: dict | None = None,
                    timeout: float = 20) -> tuple[int, dict]:
    """唯讀 GET，回 `(status, json)`（解析失敗回 `{}`）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            try:
                return r.status_code, r.json()
            except Exception:  # noqa: BLE001
                return r.status_code, {}
    except Exception as e:  # noqa: BLE001 —— 網路層失敗當 0，呼叫端據此跳過
        log.warning("Graph GET 失敗 %s: %s", url, e)
        return 0, {}


def _classify(status: int, body: str) -> "_base.SendResult":
    """把 Graph API 回應轉成 SendResult（沿用其他通道的分類語義）。"""
    if status == 200:
        mid = ""
        try:
            import json
            d = json.loads(body or "{}")
            mid = str(d.get("message_id") or d.get("id") or "")
        except Exception:  # noqa: BLE001
            mid = ""
        return _base.SendResult(ok=True, platform_msg_id=mid)
    # 5xx / 429 = 臨時錯誤（可退避重試）；4xx = 永久錯誤
    if status >= 500 or status == 429:
        return _base.SendResult(ok=False, error=f"Meta {status}: {body[:200]}", temp_error=True)
    return _base.SendResult(ok=False, error=f"Meta {status}: {body[:200]}")


# ---------------------------------------------------------------- 私訊

def _fb_page_id(cred: dict) -> str:
    return cred.get("page_id") or ""


async def _send_text(platform: str, target_id: str, text: str, credential: dict) -> "_base.SendResult":
    cred = credential or {}
    if platform == "fb":
        page = _fb_page_id(cred)
        tok = cred.get("page_access_token") or ""
        if not page or not tok:
            return _base.SendResult(ok=False, error="FB 憑證缺少 page_id / page_access_token")
        url = f"{FB_BASE}/{_ver()}/{page}/messages"
        payload = {"recipient": {"id": target_id}, "message": {"text": text}}
    else:
        tok = cred.get("ig_access_token") or ""
        if not tok:
            return _base.SendResult(ok=False, error="IG 憑證缺少 ig_access_token")
        url = f"{IG_BASE}/{_ver()}/me/messages"
        payload = {"recipient": {"id": target_id}, "message": {"text": text}}
    try:
        status, body = await _post(url, tok, payload)
    except httpx.TimeoutException:
        return _base.SendResult(ok=False, error="Meta 傳送逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return _base.SendResult(ok=False, error=f"Meta 連線失敗: {e}", temp_error=True)
    return _classify(status, body)


async def _send_attachment(platform: str, target_id: str, url: str,
                           mime: str, credential: dict) -> "_base.SendResult":
    cred = credential or {}
    atype = "image" if (mime or "").startswith("image/") else "file"
    if platform == "fb":
        page = _fb_page_id(cred)
        tok = cred.get("page_access_token") or ""
        if not page or not tok:
            return _base.SendResult(ok=False, error="FB 憑證缺少 page_id / page_access_token")
        api = f"{FB_BASE}/{_ver()}/{page}/messages"
        att = {"type": atype, "payload": {"url": url, "is_reusable": True}}
    else:
        tok = cred.get("ig_access_token") or ""
        if not tok:
            return _base.SendResult(ok=False, error="IG 憑證缺少 ig_access_token")
        api = f"{IG_BASE}/{_ver()}/me/messages"
        att = {"type": atype, "payload": {"url": url}}
    payload = {"recipient": {"id": target_id}, "message": {"attachment": att}}
    try:
        status, body = await _post(api, tok, payload)
    except httpx.TimeoutException:
        return _base.SendResult(ok=False, error="Meta 傳送逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return _base.SendResult(ok=False, error=f"Meta 連線失敗: {e}", temp_error=True)
    return _classify(status, body)


# ---------------------------------------------------------------- 公開介面（給 base 分派）

async def send_text(platform: str, target_id: str, text: str,
                    credential: dict | None = None) -> "_base.SendResult":
    return await _send_text(platform, target_id, text, credential or {})


async def send_asset(target_id: str, media_path: str, caption: str = "",
                     filename: str = "", credential: dict | None = None,
                     platform: str = "fb") -> "_base.SendResult":
    """私訊送圖片（知識庫「圖文結合輸出」的輸出端）。

    ★ IG／FB 的 attachment **只收公開 URL**（`payload.url`），由 Meta 自己去下載，
      不能上傳本地檔案 → 這裡把 `media_path`（在 MEDIA_DIR 底下）換算成
      `PUBLIC_BASE_URL + /files/<相對路徑>` 再交給 Graph API。

    ⚠️ 這裡原本是 `return SendResult(ok=False, error="尚未支援")` ——
       於是「AI 回覆帶圖」在 IG／FB 上**永遠失敗**（CRM 記錄寫「附圖 N」，
       客戶端卻只收到文字）。修好 `_asset_items` 的上傳後，這支必須同時可用。

    ⚠️ `caption`：IG／FB 的圖片訊息沒有 caption 欄位 → 忽略（呼叫端會另發一條文字）。
    """
    from pathlib import Path as _P

    from app.core import config
    from app import media as _media

    try:
        rel = _P(str(media_path)).resolve().relative_to(_P(config.MEDIA_DIR).resolve())
    except Exception:  # noqa: BLE001 —— 不在 MEDIA_DIR 底下就拼不出公開網址
        return _base.SendResult(
            ok=False, error=f"圖片不在媒體目錄底下，無法產生公開網址：{media_path}")

    url = _media.public_url(str(rel).replace("\\", "/"))
    if not url:
        return _base.SendResult(
            ok=False,
            error="沒設定 PUBLIC_BASE_URL，IG／FB 抓不到圖片"
                  "（Meta 會自己來下載，網址必須外網可達）")

    mime = _guess_mime(filename or str(rel))
    return await _send_attachment(platform if platform in ("ig", "fb") else "fb",
                                  target_id, url, mime, credential or {})


def _guess_mime(name: str) -> str:
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }.get(ext, "image/png")


async def reply_comment(platform: str, comment_id: str, text: str,
                        credential: dict | None = None, media_url: str = "") -> "_base.SendResult":
    """回覆貼文留言。

    - `fb`：`POST {FB_BASE}/{ver}/{comment_id}/comments`（粉專 token）
    - `ig`：`POST {FB_BASE}/{ver}/{comment_id}/replies`（粉專 token；與舊單體一致）
    """
    cred = credential or {}
    tok = cred.get("page_access_token") or cred.get("ig_access_token") or ""
    if not tok:
        return _base.SendResult(ok=False, error=f"{platform} 憑證缺少留言回覆用的 token")
    node = "comments" if platform == "fb" else "replies"
    # 帶圖留言：先發圖（message 帶 attachment url 不支援），這裡只發文字（與舊單體一致）
    payload: dict = {"message": text}
    url = f"{FB_BASE}/{_ver()}/{comment_id}/{node}"
    try:
        status, body = await _post(url, tok, payload)
    except httpx.TimeoutException:
        return _base.SendResult(ok=False, error="Meta 留言回覆逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return _base.SendResult(ok=False, error=f"Meta 連線失敗: {e}", temp_error=True)
    return _classify(status, body)


# ================================================================
# 只讀拉取（歷史補拉用）—— 憑證由呼叫端傳入（多租戶）
#
# ★ 為什麼需要：公網隧道中斷時 webhook 推不進來 → 訊息沒入庫 → 工作台看不到。
#   「重新整理」按鈕靠這些函式主動把最近的會話/訊息/貼文/留言拉回來補齊。
# ================================================================

def _fb_creds(cred: dict) -> tuple[str, str]:
    return cred.get("page_id") or "", cred.get("page_access_token") or ""


def _ig_creds(cred: dict) -> tuple[str, str]:
    """IG 私訊：`me` + IGAA token（graph.instagram.com）。"""
    return cred.get("ig_user_id") or "", cred.get("ig_access_token") or ""


def _next_cursor(data: dict) -> str:
    try:
        return ((data or {}).get("paging") or {}).get("cursors", {}).get("after", "") or ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- FB 私訊

async def fb_conversations(page_id: str, token: str, limit: int = 25) -> list[dict]:
    """粉專的 Messenger 會話清單（participants / updated_time / snippet）。"""
    if not page_id or not token:
        return []
    url = f"{FB_BASE}/{_ver()}/{page_id}/conversations"
    status, data = await _get_json(url, token,
                                   {"fields": "participants,updated_time,snippet", "limit": limit})
    if status != 200:
        log.warning("fb_conversations 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


async def fb_thread_messages(thread_id: str, token: str, limit: int = 30) -> list[dict]:
    """某 Messenger 會話的訊息（**回傳順序＝API 給的，通常是新→舊**）。"""
    if not thread_id or not token:
        return []
    url = f"{FB_BASE}/{_ver()}/{thread_id}/messages"
    status, data = await _get_json(
        url, token,
        {"fields": "message,from,created_time,attachments{type,payload},sticker", "limit": limit})
    if status != 200:
        log.warning("fb_thread_messages 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


# ---------------------------------------------------------------- IG 私訊

async def ig_conversations(token: str, limit: int = 25) -> list[dict]:
    """IG 專業帳號的會話清單（Instagram 登入路徑：graph.instagram.com/me）。"""
    if not token:
        return []
    url = f"{IG_BASE}/{_ver()}/me/conversations"
    status, data = await _get_json(url, token,
                                   {"fields": "participants,updated_time,snippet", "limit": limit})
    if status != 200:
        log.warning("ig_conversations 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


async def ig_thread_messages(thread_id: str, token: str, limit: int = 30) -> list[dict]:
    if not thread_id or not token:
        return []
    url = f"{IG_BASE}/{_ver()}/{thread_id}/messages"
    status, data = await _get_json(
        url, token,
        {"fields": "message,from,created_time,attachments{type,payload}", "limit": limit})
    if status != 200:
        log.warning("ig_thread_messages 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


# ---------------------------------------------------------------- 貼文 / 留言

async def fb_posts(page_id: str, token: str, limit: int = 10) -> list[dict]:
    """粉專貼文（不可加 `type` 參數 —— 已棄用，一加整個 /feed 回 400）。"""
    if not page_id or not token:
        return []
    url = f"{FB_BASE}/{_ver()}/{page_id}/feed"
    status, data = await _get_json(
        url, token,
        {"fields": "id,message,created_time,permalink_url,full_picture,"
                   "attachments{media{image{src}}}", "limit": limit})
    if status != 200:
        log.warning("fb_posts 失敗 %s: %s", status, data)
        return []
    posts = data.get("data") or []
    for p in posts:
        if not (p.get("full_picture") or p.get("media_url")):
            for a in (p.get("attachments") or {}).get("data") or []:
                src = ((a.get("media") or {}).get("image") or {}).get("src")
                if src:
                    p["media_url"] = src
                    break
    return posts


async def fb_comments(post_id: str, token: str, limit: int = 50) -> list[dict]:
    if not post_id or not token:
        return []
    url = f"{FB_BASE}/{_ver()}/{post_id}/comments"
    fields = ("id,message,from{name,id},created_time,like_count,"
              "comments{id,message,from{name,id},created_time,like_count}")
    status, data = await _get_json(url, token, {"fields": fields, "limit": limit})
    if status != 200:
        log.warning("fb_comments 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


async def ig_posts(ig_user_id: str, page_token: str, limit: int = 10) -> list[dict]:
    """IG 貼文：走 **FB 登入路徑**（graph.facebook.com + 粉專 token + IG business id）。"""
    if not ig_user_id or not page_token:
        return []
    url = f"{FB_BASE}/{_ver()}/{ig_user_id}/media"
    status, data = await _get_json(
        url, page_token,
        {"fields": "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count",
         "limit": limit})
    if status != 200:
        log.warning("ig_posts 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []


async def ig_comments(media_id: str, page_token: str, limit: int = 50) -> list[dict]:
    if not media_id or not page_token:
        return []
    url = f"{FB_BASE}/{_ver()}/{media_id}/comments"
    fields = ("id,text,timestamp,like_count,from{username,id},"
              "replies{id,text,timestamp,like_count,from{username,id}}")
    status, data = await _get_json(url, page_token, {"fields": fields, "limit": limit})
    if status != 200:
        log.warning("ig_comments 失敗 %s: %s", status, data)
        return []
    return data.get("data") or []
