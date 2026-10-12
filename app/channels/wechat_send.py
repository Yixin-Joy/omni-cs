"""把客服工作台的回覆，透過微信客服消息接口發送給對應的公眾號用戶。

限制：客服消息接口（custom/send）需要**認證公眾號**才有權限。
      未認證訂閱號調用會回 errcode 48001（api unauthorized），文字 / 圖片都發不出去；
      這是平台硬限制，程式無法繞過。
      認證後還需：來源 IP 在白名單（否則 40164）、且用戶 48 小時內互動過。
"""
import logging
import time
from pathlib import Path

import httpx

from app.core import config

log = logging.getLogger("wechat-forwarder")

API_BASE = "https://api.weixin.qq.com/cgi-bin"

_token_cache: dict = {"token": "", "expires_at": 0.0}


def _get_access_token() -> str:
    """取得並快取微信 access_token（有效 7200 秒，提前 5 分鐘過期）。"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    resp = httpx.get(
        f"{API_BASE}/token",
        params={
            "grant_type": "client_credential",
            "appid": config.WECHAT_APPID,
            "secret": config.WECHAT_APPSECRET,
        },
        timeout=15,
    )
    data = resp.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"取得微信 access_token 失敗: {data}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 7200) - 300
    return token


def _custom_send(payload: dict) -> dict:
    """POST 到 custom/send，把 errcode 翻譯成 HTTP 語義方便上層判斷。

    errcode==0 視為成功（status=200），否則視為失敗（status=502）。
    """
    token = _get_access_token()
    resp = httpx.post(
        f"{API_BASE}/message/custom/send",
        params={"access_token": token},
        json=payload,
        timeout=15,
    )
    body = resp.text
    status = resp.status_code
    if status == 200:
        try:
            errcode = resp.json().get("errcode", -1)
        except Exception:
            errcode = -1
        status = 200 if errcode == 0 else 502
    return {"status": status, "body": body}


def send_text(openid: str, content: str) -> dict:
    """發送文字客服消息，回傳 {"status": int, "body": str}。"""
    return _custom_send({
        "touser": openid,
        "msgtype": "text",
        "text": {"content": content},
    })


def send_image(openid: str, local_path: str) -> dict:
    """上傳圖片到微信臨時素材並作為客服消息發送，回傳 {"status": int, "body": str}。"""
    token = _get_access_token()
    upload_url = f"{API_BASE}/media/upload"
    with open(local_path, "rb") as f:
        resp = httpx.post(
            upload_url,
            params={"access_token": token, "type": "image"},
            files={"media": (Path(local_path).name, f, "image/jpeg")},
            timeout=30,
        )
    body = resp.text
    if resp.status_code != 200:
        return {"status": resp.status_code, "body": body}
    try:
        media_id = resp.json().get("media_id", "")
    except Exception:
        media_id = ""
    if not media_id:
        return {"status": 502, "body": body}

    return _custom_send({
        "touser": openid,
        "msgtype": "image",
        "image": {"media_id": media_id},
    })
