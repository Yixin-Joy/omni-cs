"""微信客服消息發送（從舊 `app/channels/wechat_send.py` 移植）。

限制（平台硬限制，程式繞不過）：客服消息接口需**認證公眾號**，未認證調用回 48001；
認證後還需來源 IP 白名單、且用戶 48 小時內互動過（否則 45015）。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.channels.base import SendResult
from app.core import config

log = logging.getLogger("connector")

API_BASE = "https://api.weixin.qq.com/cgi-bin"

# 這些 errcode 代表「稍後可能成功」→ 可重試（temp_error）
_TEMP_CODES = {-1, 45009}
# 這些代表「已發出但我們不知道結果」→ uncertain
_UNCERTAIN_CODES: set[int] = set()

_token_cache: dict = {"token": "", "expires_at": 0.0}


def _get_access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]
    resp = httpx.get(
        f"{API_BASE}/token",
        params={"grant_type": "client_credential",
                "appid": config.WECHAT_APPID, "secret": config.WECHAT_APPSECRET},
        timeout=15,
    )
    data = resp.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"取得微信 access_token 失敗: {data}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 7200) - 300
    return token


def _classify(status: int, errcode: int | None, body: str) -> SendResult:
    if status == 200 and errcode == 0:
        return SendResult(ok=True, platform_msg_id=None)
    if errcode in _UNCERTAIN_CODES:
        return SendResult(ok=False, error=f"微信回應無法判定: {body[:200]}", uncertain=True)
    if errcode in _TEMP_CODES or status >= 500:
        return SendResult(ok=False, error=f"微信暫時性錯誤（errcode={errcode}）: {body[:200]}",
                          temp_error=True)
    return SendResult(ok=False, error=f"微信拒絕（errcode={errcode}）: {body[:200]}")


def _custom_send(payload: dict) -> SendResult:
    token = _get_access_token()
    resp = httpx.post(f"{API_BASE}/message/custom/send",
                      params={"access_token": token}, json=payload, timeout=15)
    try:
        errcode = resp.json().get("errcode", -1)
    except Exception:
        errcode = -1
    return _classify(resp.status_code, errcode, resp.text)


def _do_send_text(openid: str, content: str) -> SendResult:
    return _custom_send({"touser": openid, "msgtype": "text", "text": {"content": content}})


def _do_send_image(openid: str, local_path: str) -> SendResult:
    token = _get_access_token()
    with open(local_path, "rb") as f:
        resp = httpx.post(
            f"{API_BASE}/media/upload",
            params={"access_token": token, "type": "image"},
            files={"media": (local_path.rsplit("/", 1)[-1], f, "image/jpeg")},
            timeout=30,
        )
    if resp.status_code != 200:
        return _classify(resp.status_code, None, resp.text)
    try:
        media_id = resp.json().get("media_id", "")
        errcode = resp.json().get("errcode", 0)
    except Exception:
        media_id, errcode = "", -1
    if not media_id:
        return _classify(200, errcode, resp.text)
    return _custom_send({"touser": openid, "msgtype": "image", "image": {"media_id": media_id}})


async def send_text(target_id: str, text: str) -> SendResult:
    try:
        return await asyncio.to_thread(_do_send_text, target_id, text)
    except httpx.TimeoutException:
        # 送出階段逾時 → 可能已發出
        return SendResult(ok=False, error="微信傳送逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return SendResult(ok=False, error=f"微信傳送失敗: {e}", temp_error=True)


async def send_asset(target_id: str, media_path: str, caption: str = "",
                     filename: str = "") -> SendResult:
    # 微信客服消息不支援圖片 caption（文字要另發一條）
    try:
        return await asyncio.to_thread(_do_send_image, target_id, media_path)
    except httpx.TimeoutException:
        return SendResult(ok=False, error="微信傳圖逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return SendResult(ok=False, error=f"微信傳圖失敗: {e}", temp_error=True)
