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
                     filename: str = "", credential: dict | None = None) -> "_base.SendResult":
    return _base.SendResult(ok=False, error="IG/FB 附件發送尚未支援（請用文字或連結）")


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
