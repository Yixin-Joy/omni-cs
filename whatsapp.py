"""WhatsApp（Meta Cloud API）webhook 的解析與格式轉換。

把 Meta 推來的消息 JSON 轉成目標 webhook 需要的 JSON（跟微信端同一格式）。
"""
import time
from datetime import datetime, timezone

from wechat import openid_to_conversation_id  # 復用：wa_id → 穩定 conversationId


def verify(expect_token: str, hub_verify_token: str) -> bool:
    """驗證 Meta webhook 的 verify_token。"""
    return bool(expect_token) and hub_verify_token == expect_token


def extract_messages(body: dict) -> list[dict]:
    """從 Meta webhook JSON 提取 messages，每條附帶該用戶的 profile name。

    回傳 [{"message": {...}, "profile_name": "..."}, ...]
    """
    out = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            contacts = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }
            for m in value.get("messages", []):
                out.append({
                    "message": m,
                    "profile_name": contacts.get(m.get("from", ""), ""),
                })
    return out


def _ts(msg: dict) -> int:
    try:
        return int(msg.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def _updated_at(ts: int) -> str:
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _name(profile_name: str, ts: int) -> str:
    if profile_name:
        return profile_name
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("Visitor at %H:%M on %b %d")


def _media_of(msg: dict) -> tuple[str, str, str] | None:
    """回傳 (media_id, mime_type, filename)，非媒體消息回傳 None。"""
    msg_type = msg.get("type", "")
    if msg_type == "image":
        d = msg.get("image", {})
        return d.get("id", ""), d.get("mime_type", "image/jpeg"), f"{d.get('id', 'img')}.jpg"
    if msg_type == "audio":
        d = msg.get("audio", {})
        mime = d.get("mime_type", "audio/ogg")
        ext = mime.split("/")[-1].split(";")[0] or "ogg"
        return d.get("id", ""), mime, f"{d.get('id', 'audio')}.{ext}"
    if msg_type == "video":
        d = msg.get("video", {})
        mime = d.get("mime_type", "video/mp4")
        ext = mime.split("/")[-1] or "mp4"
        return d.get("id", ""), mime, f"{d.get('id', 'video')}.{ext}"
    if msg_type == "document":
        d = msg.get("document", {})
        filename = d.get("filename") or f"{d.get('id', 'doc')}.bin"
        return d.get("id", ""), d.get("mime_type", "application/octet-stream"), filename
    if msg_type == "sticker":
        d = msg.get("sticker", {})
        return d.get("id", ""), d.get("mime_type", "image/webp"), f"{d.get('id', 'sticker')}.webp"
    return None


def convert(msg: dict, cfg: dict, profile_name: str = "") -> dict | None:
    """把單條 WhatsApp message 轉成目標 JSON。不支援的類型回傳 None。

    cfg 需包含 channel、channel_identity_id。
    """
    msg_type = msg.get("type", "")
    from_user = msg.get("from", "")
    ts = _ts(msg)

    payload = {
        "contactEmail": "",
        "id": int(time.time() * 1000),  # WhatsApp 的 id 是字串，目標要數字，改用毫秒時間戳
        "conversationId": openid_to_conversation_id(from_user),
        "channel": cfg["channel"],
        "webClientSender": {
            "webClientUUID": from_user,
            "name": _name(profile_name, ts),
        },
        "messageType": "",
        "uploadedFiles": [],
        "updatedAt": _updated_at(ts),
        "timestamp": ts or int(time.time()),
        "channelIdentityId": cfg["channel_identity_id"],
    }

    if msg_type == "text":
        payload["messageType"] = "text"
        payload["messageContent"] = msg.get("text", {}).get("body", "")

    else:
        media = _media_of(msg)
        if media is None:
            return None
        media_id, mime_type, filename = media
        payload["messageType"] = "file"
        payload["uploadedFiles"] = [{
            "fileId": media_id,
            "filename": filename,
            "url": "",  # 統一由 main._resolve_whatsapp_files 下載後回填
            "mimeType": mime_type,
            "fileSize": 0,
        }]
        # 圖片消息可帶 caption（圖文結合），存進 messageContent 避免文字遺失
        if msg_type == "image":
            caption = (msg.get("image") or {}).get("caption", "") or ""
            if caption:
                payload["messageContent"] = caption

    return payload
