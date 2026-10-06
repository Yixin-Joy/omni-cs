"""把目標系統的回覆，透過 WhatsApp Cloud API 發送給對應的用戶聊天窗口。"""
import logging
from pathlib import Path

import httpx

import config

log = logging.getLogger("wechat-forwarder")

GRAPH_BASE = "https://graph.facebook.com"

# WhatsApp Cloud API 允許的媒體 MIME 白名單（其他一律被 400 拒絕）
SUPPORTED_MIMES = {
    "audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg", "audio/opus",
    "application/vnd.ms-powerpoint", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel", "application/pdf", "text/plain",
    "image/jpeg", "image/png", "video/3gpp", "video/mp4",
}

# 常見但不在白名單的類型 → 對應到白名單裡語義最接近的
MIME_FALLBACK = {
    ".csv": "text/plain",
    ".txt": "text/plain",
    ".json": "text/plain",
    ".xml": "text/plain",
    ".md": "text/plain",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
}


def normalize_mime(mime_type: str, filename: str = "") -> str:
    """把不在 WhatsApp 白名單的 MIME 轉成白名單內的等效類型。

    例：text/csv → text/plain（CSV 本質就是純文字），否則上傳必被 400 拒絕。
    真的無法對應時原樣回傳，讓 Meta 報錯（不亂猜，避免送出毀損檔案）。
    """
    mime = (mime_type or "").lower().split(";")[0].strip()
    if mime in SUPPORTED_MIMES:
        return mime
    ext = Path(filename or "").suffix.lower()
    if ext in MIME_FALLBACK:
        return MIME_FALLBACK[ext]
    if mime.startswith("text/"):
        return "text/plain"
    return mime


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"}


def _messages_url() -> str:
    return f"{GRAPH_BASE}/{config.GRAPH_API_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/messages"


async def send_text(to: str, body: str) -> dict:
    """發送文字消息，回傳 {"status": int, "body": str}。"""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_messages_url(), headers=_auth_headers(), json=payload)
        return {"status": resp.status_code, "body": resp.text}


async def upload_media(file_path: str, mime_type: str, filename: str = "") -> str:
    """上傳媒體到 WhatsApp，回傳 media_id。

    filename 用於 MIME 兜底判斷（副檔名），預設取本地檔名的副檔名。
    """
    send_mime = normalize_mime(mime_type, filename or file_path)
    if send_mime != (mime_type or "").lower().split(";")[0].strip():
        log.warning("MIME %s 不在 WhatsApp 白名單，改用 %s 上傳", mime_type, send_mime)

    url = f"{GRAPH_BASE}/{config.GRAPH_API_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/media"
    data = {"messaging_product": "whatsapp", "type": send_mime}
    async with httpx.AsyncClient(timeout=30) as client:
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f, send_mime)}
            resp = await client.post(url, headers=_auth_headers(), files=files, data=data)
        if resp.status_code != 200:
            raise RuntimeError(f"上傳媒體失敗 ({resp.status_code}): {resp.text[:300]}")
        media_id = resp.json().get("id", "")
        if not media_id:
            raise RuntimeError(f"上傳媒體失敗: {resp.text[:300]}")
        return media_id


async def send_document(to: str, media_id: str, filename: str, caption: str = "") -> dict:
    """發送文件消息，可帶 caption，回傳 {"status": int, "body": str}。"""
    document = {"id": media_id, "filename": filename}
    if caption:
        document["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": document,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_messages_url(), headers=_auth_headers(), json=payload)
        return {"status": resp.status_code, "body": resp.text}


async def send_image(to: str, media_id: str, caption: str = "") -> dict:
    """發送圖片消息，可帶 caption，回傳 {"status": int, "body": str}。"""
    image = {"id": media_id}
    if caption:
        image["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_messages_url(), headers=_auth_headers(), json=payload)
        return {"status": resp.status_code, "body": resp.text}
