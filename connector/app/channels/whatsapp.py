"""WhatsApp Cloud API 發送（從舊 `app/channels/whatsapp_send.py` 移植）。

WhatsApp 是**唯一支援 caption 的渠道** —— 圖文可一條送出（設計案 §12 / 記憶檔結論）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from app.channels.base import SendResult, is_image
from app.core import config

log = logging.getLogger("connector")

GRAPH_BASE = "https://graph.facebook.com"

SUPPORTED_MIMES = {
    "audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg", "audio/opus",
    "application/vnd.ms-powerpoint", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel", "application/pdf", "text/plain",
    "image/jpeg", "image/png", "video/3gpp", "video/mp4",
}

MIME_FALLBACK = {
    ".csv": "text/plain", ".txt": "text/plain", ".json": "text/plain",
    ".xml": "text/plain", ".md": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4",
}


def normalize_mime(mime_type: str, filename: str = "") -> str:
    """把不在白名單的 MIME 轉成白名單內等效類型（例：text/csv → text/plain），否則上傳必被 400。"""
    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime in SUPPORTED_MIMES:
        return mime
    ext = Path(filename or "").suffix.lower()
    if ext in MIME_FALLBACK:
        return MIME_FALLBACK[ext]
    if mime.startswith("text/"):
        return "text/plain"
    return mime


def _auth_headers(credential: dict) -> dict:
    token = (credential or {}).get("access_token") or config.WHATSAPP_ACCESS_TOKEN
    return {"Authorization": f"Bearer {token}"}


def _messages_url(credential: dict) -> str:
    phone_id = (credential or {}).get("phone_number_id") or config.WHATSAPP_PHONE_NUMBER_ID
    return f"{GRAPH_BASE}/{config.GRAPH_API_VERSION}/{phone_id}/messages"


def _classify(status: int, body: str) -> SendResult:
    if 200 <= status < 300:
        try:
            import json as _json
            mid = (_json.loads(body).get("messages") or [{}])[0].get("id")
        except Exception:
            mid = None
        return SendResult(ok=True, platform_msg_id=mid)
    if status == 429 or status >= 500:
        return SendResult(ok=False, error=f"WhatsApp 暫時性錯誤（{status}）: {body[:200]}",
                          temp_error=True)
    return SendResult(ok=False, error=f"WhatsApp 拒絕（{status}）: {body[:200]}")


async def _post(url: str, credential: dict, **kw) -> tuple[int, str]:
    async with httpx.AsyncClient(timeout=kw.pop("timeout", 20)) as client:
        resp = await client.post(url, headers=_auth_headers(credential), **kw)
        return resp.status_code, resp.text


async def send_text(target_id: str, text: str, credential: dict | None = None) -> SendResult:
    cred = credential or {}
    payload = {"messaging_product": "whatsapp", "to": target_id, "type": "text",
               "text": {"body": text}}
    try:
        status, body = await _post(_messages_url(cred), cred, json=payload)
    except httpx.TimeoutException:
        return SendResult(ok=False, error="WhatsApp 傳送逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return SendResult(ok=False, error=f"WhatsApp 連線失敗: {e}", temp_error=True)
    return _classify(status, body)


async def _upload_media(file_path: str, mime_type: str, filename: str,
                        credential: dict) -> str:
    cred = credential or {}
    send_mime = normalize_mime(mime_type, filename or file_path)
    url = f"{GRAPH_BASE}/{config.GRAPH_API_VERSION}/{(cred.get('phone_number_id') or config.WHATSAPP_PHONE_NUMBER_ID)}/media"
    data = {"messaging_product": "whatsapp", "type": send_mime}
    async with httpx.AsyncClient(timeout=40) as client:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(filename or file_path), f, send_mime)}
            resp = await client.post(url, headers=_auth_headers(cred), files=files, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"上傳媒體失敗 ({resp.status_code}): {resp.text[:200]}")
        media_id = resp.json().get("id", "")
        if not media_id:
            raise RuntimeError(f"上傳媒體失敗: {resp.text[:200]}")
        return media_id


async def send_asset(target_id: str, media_path: str, caption: str = "",
                     filename: str = "", credential: dict | None = None) -> SendResult:
    cred = credential or {}
    name = filename or os.path.basename(media_path)
    try:
        media_id = await _upload_media(media_path, "", name, cred)
    except httpx.TimeoutException:
        return SendResult(ok=False, error="WhatsApp 上傳媒體逾時", temp_error=True)
    except Exception as e:  # noqa: BLE001
        return SendResult(ok=False, error=f"WhatsApp 上傳媒體失敗: {e}", temp_error=True)

    if is_image(name):
        node = {"id": media_id}
        if caption:
            node["caption"] = caption
        payload = {"messaging_product": "whatsapp", "to": target_id,
                   "type": "image", "image": node}
    else:
        node = {"id": media_id, "filename": name}
        if caption:
            node["caption"] = caption
        payload = {"messaging_product": "whatsapp", "to": target_id,
                   "type": "document", "document": node}

    try:
        status, body = await _post(_messages_url(cred), cred, json=payload)
    except httpx.TimeoutException:
        return SendResult(ok=False, error="WhatsApp 傳送媒體逾時", uncertain=True)
    except Exception as e:  # noqa: BLE001
        return SendResult(ok=False, error=f"WhatsApp 傳送媒體失敗: {e}", temp_error=True)
    return _classify(status, body)
