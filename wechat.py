"""微信消息的簽名驗證、XML 解析、以及轉成目標 webhook 需要的 JSON 格式。"""
import base64
import hashlib
import struct
import time
import uuid
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

try:
    from Crypto.Cipher import AES
    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False


def check_signature(token: str, timestamp: str, nonce: str, signature: str) -> bool:
    """微信接入驗證（明文模式）：token/timestamp/nonce 字典序拼接後 sha1 比對。"""
    tmp = "".join(sorted([token, timestamp, nonce]))
    return hashlib.sha1(tmp.encode("utf-8")).hexdigest() == signature


def check_msg_signature(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    """安全模式的簽名驗證：sha1(sort([token, timestamp, nonce, encrypt]))。"""
    tmp = "".join(sorted([token, timestamp, nonce, encrypt]))
    return hashlib.sha1(tmp.encode("utf-8")).hexdigest() == signature


def decrypt(encrypted: str, encoding_aes_key: str) -> str:
    """解密微信密文（安全/兼容模式），回傳明文內容。

    明文結構：16 bytes 隨機串 + 4 bytes 長度 + 消息 + receive_id(AppID)。
    """
    if not _HAS_CRYPTO:
        raise RuntimeError("安全模式需要 pycryptodome：pip install pycryptodome")
    key = base64.b64decode(encoding_aes_key + "=")
    aes = AES.new(key, AES.MODE_CBC, iv=key[:16])
    raw = aes.decrypt(base64.b64decode(encrypted))
    pad = raw[-1]
    raw = raw[:-pad]
    msg_len = struct.unpack(">I", raw[16:20])[0]
    return raw[20:20 + msg_len].decode("utf-8")


def parse_xml(xml_text: str) -> dict:
    """把微信推來的 XML 解析成 dict，CDATA 內容會自動去標籤。"""
    root = ET.fromstring(xml_text)
    return {child.tag: (child.text or "") for child in root}


def _from_create_time(msg: dict) -> tuple[str, int]:
    """用微信消息的 CreateTime 生成 updatedAt 和 timestamp，缺省則用當下時間。"""
    try:
        create_time = int(msg.get("CreateTime", 0) or 0)
    except (TypeError, ValueError):
        create_time = 0
    if create_time:
        updated_at = (
            datetime.fromtimestamp(create_time, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return updated_at, create_time
    now = datetime.now(timezone.utc)
    return now.isoformat().replace("+00:00", "Z"), int(time.time())


def _visitor_name(create_time: int) -> str:
    """範例：'Visitor at 09:23 on Sep 21'。"""
    if create_time:
        dt = datetime.fromtimestamp(create_time, tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.strftime("Visitor at %H:%M on %b %d")


def openid_to_conversation_id(openid: str) -> str:
    """同一個用戶固定映射到同一個 conversationId（uuid5）。"""
    if not openid:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wechat:{openid}"))


def _extract_message_id(msg: dict) -> int:
    try:
        return int(msg.get("MsgId"))
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def convert(msg: dict, cfg: dict) -> dict | None:
    """把微信消息 dict 轉成正規化 payload。不支援的類型回傳 None。

    cfg 需包含 channel、channel_identity_id 兩個鍵。
    """
    msg_type = msg.get("MsgType", "")
    from_user = msg.get("FromUserName", "")
    updated_at, ts = _from_create_time(msg)

    payload = {
        "contactEmail": "",
        "id": _extract_message_id(msg),
        "conversationId": openid_to_conversation_id(from_user),
        "channel": cfg["channel"],
        "webClientSender": {
            "webClientUUID": from_user,
            "name": _visitor_name(ts),
        },
        "messageType": "",
        "uploadedFiles": [],
        "updatedAt": updated_at,
        "timestamp": ts,
        "channelIdentityId": cfg["channel_identity_id"],
    }

    if msg_type == "text":
        payload["messageType"] = "text"
        payload["messageContent"] = msg.get("Content", "")

    elif msg_type == "image":
        payload["messageType"] = "file"
        payload["uploadedFiles"] = [{
            "fileId": msg.get("MediaId", ""),
            "filename": f"{msg.get('MediaId', 'image')}.jpg",
            "url": msg.get("PicUrl", ""),
            "mimeType": "image/jpeg",
            "fileSize": 0,  # 微信不回傳大小；用 PicUrl 時無從得知
        }]

    elif msg_type in ("voice", "video", "shortvideo"):
        payload["messageType"] = "file"
        payload["uploadedFiles"] = [{
            "fileId": msg.get("MediaId", ""),
            "filename": _media_filename(msg_type, msg),
            "url": "",  # 由 main._resolve_files 下載後回填
            "mimeType": _media_mime(msg_type, msg),
            "fileSize": 0,
        }]

    else:
        return None

    return payload


def _media_filename(msg_type: str, msg: dict) -> str:
    mid = msg.get("MediaId", "media")
    ext = {
        "voice": msg.get("Format", "amr"),
        "video": "mp4",
        "shortvideo": "mp4",
    }.get(msg_type, "bin")
    return f"{mid}.{ext}"


def _media_mime(msg_type: str, msg: dict) -> str:
    if msg_type == "voice":
        fmt = msg.get("Format", "").lower()
        return {"amr": "audio/amr", "speex": "audio/speex", "mp3": "audio/mpeg"}.get(fmt, "audio/amr")
    return "video/mp4"
