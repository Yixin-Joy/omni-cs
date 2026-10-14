"""Connector 的 webhook 收訊入口 —— 從舊單體 `app/main.py` 搬入（階段三）。

對外只暴露三個端點：
  GET  /wechat           微信接入驗證（回傳 echostr）
  POST /wechat           接收微信消息（XML）
  GET  /whatsapp/webhook WhatsApp / IG / FB webhook 驗證（回傳 challenge）
  POST /whatsapp/webhook 接收 Meta 消息（JSON，WhatsApp + IG/FB 共用）
  GET  /webhook          （別名，等同 /whatsapp/webhook 的 GET）
  POST /webhook          （別名，等同 /whatsapp/webhook 的 POST）

收到訊息後的流程（★ 與舊單體不同：不再本機跑引擎，改非同步通知 CRM）：
  1. 正規化（wechat_recv / whatsapp_recv / igfb_recv）
  2. 平台原生 ID 反查（account_map.lookup）→ 認出商家
  3. 落地本機 `forward_log`（補 platform_business_account_id / business_id / bp_account_id）
  4. 非同步 `notify_ai_inbound` 打 CRM `/internal/api/ai/inbound`
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request, Response

from app.core import config
from app.channels import wechat_recv
from app.channels import whatsapp_recv
from app.channels import igfb_recv
from app.channels import igfb_store
from app import account_map
from app import forwarder
from app.worker import client as crm_client

log = logging.getLogger("connector")

router = APIRouter(tags=["webhook"])


# ---------------------------------------------------------------- 落地

def _land_local(platform: str, payload: dict, raw_content: str,
                account: dict | None = None) -> None:
    """把一則客戶來訊落地到本機 `forward_log`（Connector 是這個庫的唯一寫入者）。

    `account` 是 account_map.lookup 的結果（可能為 None，代表未註冊）。
    落地一律成功（即使未註冊也存，供人工排查），是否通知 CRM 由呼叫端決定。
    """
    from app.core import db as _db  # 延後 import 避免循環
    sender = payload.get("webClientSender") or {}
    record = {
        "platform": platform,
        "message_id": str(payload.get("id", "")),
        "sender_id": sender.get("webClientUUID", ""),
        "conversation_id": payload.get("conversationId", ""),
        "message_type": payload.get("messageType", ""),
        "raw_content": raw_content,
        "forwarded_payload": payload,
        "forward_status": "local",
        "message_time": payload.get("updatedAt"),
        "direction": "inbound",
    }
    # ★ 多租戶對齊鍵（Connector DB 已加這三個欄位）
    if account:
        record["platform_business_account_id"] = account.get("platform_business_account_id")
        record["business_id"] = account.get("business_id")
        record["bp_account_id"] = account.get("bp_account_id")

    conn = _db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO forward_log (
                    platform, message_id, sender_id, conversation_id, message_type,
                    raw_content, forwarded_payload, forward_status, message_time, direction,
                    platform_business_account_id, business_id, bp_account_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.get("platform"), record.get("message_id"), record.get("sender_id"),
                    record.get("conversation_id"), record.get("message_type"),
                    record.get("raw_content"),
                    json.dumps(record.get("forwarded_payload"), ensure_ascii=False),
                    record.get("forward_status"), record.get("message_time"),
                    record.get("direction"), record.get("platform_business_account_id"),
                    record.get("business_id"), record.get("bp_account_id"),
                ),
            )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("寫入轉發日誌失敗: %s", e)
    finally:
        conn.close()


# ---------------------------------------------------------------- 通知 CRM

def _notify_inbound(platform: str, account: dict, message_id: str, sender_id: str,
                    text: str, content_extra: dict) -> None:
    """非同步通知 CRM 入隊（不回阻擋 webhook 回應）。

    ★ 平台原生 ID 反查（account_map.lookup）已由呼叫端完成；這裡只負責組 payload。
    """
    if not account:
        log.info("未註冊帳號 %s，不通知 CRM", platform)
        return

    payload = {
        "message_id": message_id,
        "platform": platform,
        "platform_business_account_id": account["platform_business_account_id"],
        "end_user_sender_id": sender_id,
        "scope": content_extra.get("scope", "dm"),
        "content": {
            "text": text,
            **content_extra,
        },
        "post_id": content_extra.get("post_id"),
    }

    async def _run():
        try:
            await crm_client.notify_ai_inbound(payload)
        except Exception as e:  # noqa: BLE001
            log.warning("通知 CRM ai/inbound 失敗（%s %s）: %s", platform, message_id, e)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        log.debug("沒有事件迴圈，略過通知 CRM")


# ---------------------------------------------------------------- 微信

def _wechat_credential(native_id: str | None) -> tuple[dict, dict | None]:
    """解析微信帳號的業務憑證（aes_key / appid / appsecret）。

    多租戶：有 native_id → 查 account_map 拿 credential；
    單商家過渡：native_id 為空或查不到 → fallback config。

    回傳 (credential_dict, account_or_None)。
    """
    if native_id:
        acc = account_map.lookup("wechat", native_id)
        if acc:
            return (acc.get("credential") or {}, acc)
    return ({}, None)


def _wechat_aes_key(cred: dict) -> str:
    return cred.get("encoding_aes_key") or config.WECHAT_ENCODING_AES_KEY


@router.get("/wechat")
async def wechat_verify(request: Request):
    params = request.query_params
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    echostr = params.get("echostr", "")

    msg_signature = params.get("msg_signature", "")
    if msg_signature:
        if wechat_recv.check_msg_signature(config.WECHAT_TOKEN, timestamp, nonce, echostr, msg_signature):
            try:
                plain = wechat_recv.decrypt(echostr, config.WECHAT_ENCODING_AES_KEY)
            except Exception as e:
                log.error("微信驗證 echostr 解密失敗: %s", e)
                return Response(content="decrypt failed", status_code=403)
            return Response(content=plain, media_type="text/plain")
        return Response(content="signature mismatch", status_code=403)

    signature = params.get("signature", "")
    if wechat_recv.check_signature(config.WECHAT_TOKEN, timestamp, nonce, signature):
        return Response(content=echostr, media_type="text/plain")
    return Response(content="signature mismatch", status_code=403)


@router.post("/wechat")
@router.post("/wechat/{native_id}")
async def wechat_receive(request: Request, native_id: str | None = None):
    body = await request.body()
    xml_text = body.decode("utf-8")

    # 先解析出 ToUserName（AppID）→ 才能反查商家拿 aes_key 解密。
    # 但加密模式下外層只有 Encrypt，必須先解開才知道 ToUserName。
    # 解法：路徑帶 native_id 時直接用；否則先嘗試用 config 的 aes_key（單商家），
    #       解開後再用 ToUserName 反查。
    cred, account = _wechat_credential(native_id)
    aes_key = _wechat_aes_key(cred)

    if "<Encrypt>" in xml_text:
        outer = wechat_recv.parse_xml(xml_text)
        try:
            plain = wechat_recv.decrypt(outer.get("Encrypt", ""), aes_key)
        except Exception as e:
            log.error("微信消息解密失敗: %s", e)
            return Response(content="success", media_type="text/plain")
        msg = wechat_recv.parse_xml(plain)
    else:
        msg = wechat_recv.parse_xml(xml_text)

    log.info("收到微信消息 MsgType=%s FromUser=%s", msg.get("MsgType"), msg.get("FromUserName"))

    # 若路徑沒帶 native_id，用解出來的 ToUserName 反查（覆蓋 account）
    if native_id is None:
        native_id = msg.get("ToUserName", "")
        cred, account = _wechat_credential(native_id)

    payload = wechat_recv.convert(msg, {
        "channel": config.CHANNEL,
        "channel_identity_id": config.CHANNEL_IDENTITY_ID,
    })
    if payload is None:
        log.info("忽略不支援的消息類型: %s", msg.get("MsgType"))
        return Response(content="success", media_type="text/plain")

    try:
        await _resolve_wechat_files(msg, payload, account)
    except Exception as e:
        log.warning("微信文件處理失敗（帶空 url 繼續）: %s", e)

    _land_local("wechat", payload, json.dumps(msg, ensure_ascii=False), account)

    if msg.get("MsgType") == "text":
        sender = (payload.get("webClientSender") or {}).get("webClientUUID") or msg.get("FromUserName", "")
        _notify_inbound(
            "wechat", account,
            message_id=str(payload.get("id", "")),
            sender_id=sender,
            text=payload.get("messageContent", ""),
            content_extra={"scope": "dm", "conversation_key": sender},
        )

    return Response(content="success", media_type="text/plain")


# ---------------------------------------------------------------- Meta（WhatsApp / IG / FB）

@router.get("/whatsapp/webhook")
@router.get("/webhook")
async def meta_verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and (
        whatsapp_recv.verify(config.WHATSAPP_VERIFY_TOKEN, token)
        or whatsapp_recv.verify(config.META_VERIFY_TOKEN, token)
    ):
        return Response(content=challenge, media_type="text/plain")
    return Response(content="verification failed", status_code=403)


def _verify_igfb_signature(raw: bytes, headers) -> None:
    if not config.META_VERIFY_SIGNATURE or not config.META_APP_SECRET:
        return
    sig = headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256="):
        raise ValueError("缺少 X-Hub-Signature-256 簽章")
    expected = hmac.new(config.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig[len("sha256="):], expected):
        raise ValueError("webhook 簽章不匹配")


@router.post("/whatsapp/webhook")
@router.post("/webhook")
@router.post("/webhook/{native_id}")
async def meta_receive(request: Request, native_id: str | None = None):
    raw = await request.body()
    body = json.loads(raw.decode("utf-8"))
    obj = body.get("object", "")

    # IG / FB（私訊 + 貼文留言）
    if obj in ("page", "instagram"):
        try:
            _verify_igfb_signature(raw, request.headers)
        except Exception as e:
            log.warning("IG/FB webhook 簽章驗證失敗: %s", e)
            return Response(content="signature invalid", status_code=403)
        events = igfb_recv.extract_events(body)
        for ev in events:
            await _handle_igfb_event(ev)
        log.info("IG/FB webhook object=%s events=%d", obj, len(events))
        return Response(content="EVENT_RECEIVED", media_type="text/plain")

    # WhatsApp
    items = whatsapp_recv.extract_messages(body)
    for item in items:
        m = item["message"]
        log.info("收到 WhatsApp 消息 type=%s from=%s", m.get("type"), m.get("from"))

        payload = whatsapp_recv.convert(m, {
            "channel": config.WHATSAPP_CHANNEL,
            "channel_identity_id": config.WHATSAPP_CHANNEL_IDENTITY_ID,
        }, item["profile_name"])
        if payload is None:
            log.info("忽略 WhatsApp 消息類型: %s", m.get("type"))
            continue

        # ★ 平台原生 ID：優先路徑參數，否則 WhatsApp webhook 的 metadata.phone_number_id
        wa_native = native_id or item.get("phone_number_id", "")
        account = account_map.lookup("whatsapp", wa_native) if wa_native else None

        try:
            await _resolve_whatsapp_files(payload, account)
        except Exception as e:
            log.warning("WhatsApp 文件處理失敗（帶空 url 繼續）: %s", e)

        _land_local("whatsapp", payload, json.dumps(m, ensure_ascii=False), account)

        sender = (payload.get("webClientSender") or {}).get("webClientUUID") or m.get("from", "")
        _notify_inbound(
            "whatsapp", account,
            message_id=str(payload.get("id", "")),
            sender_id=sender,
            text=payload.get("messageContent", ""),
            content_extra={"scope": "dm", "conversation_key": sender,
                           "sender_name": item.get("profile_name") or ""},
        )

    return Response(content="EVENT_RECEIVED", media_type="text/plain")


async def _resolve_whatsapp_files(payload: dict, account: dict | None = None):
    cred = (account or {}).get("credential") or {}
    access_token = cred.get("access_token") or config.WHATSAPP_ACCESS_TOKEN
    for f in payload.get("uploadedFiles", []):
        if f.get("url"):
            continue
        media_id = f.get("fileId", "")
        if not media_id or not config.PUBLIC_BASE_URL:
            continue
        filename = f.get("filename", f"{media_id}.bin")
        save_path = Path(config.MEDIA_DIR) / filename
        if not save_path.exists():
            await forwarder.download_whatsapp_media(
                access_token, media_id, str(save_path), config.GRAPH_API_VERSION
            )
        f["url"] = f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{filename}"
        f["fileSize"] = save_path.stat().st_size


async def _handle_igfb_event(ev: dict) -> None:
    """落地一則 IG/FB webhook 事件，並非同步通知 CRM。

    ★★ 落地表＝**`igfb_dm`（私訊）/ `igfb_comment`（留言）**，不是 `forward_log`
      —— 工作台讀 IG/FB 就是讀這兩張表（`internal/router.py` 的
      `_conv_dm_igfb_sql` / `_conv_comment_sql` / `/posts`）。走 `igfb_store` 統一處理。
    """
    t = ev["type"]
    platform = ev["platform"]
    native_id = ev.get("account_native_id", "")
    account = account_map.lookup(platform, native_id) if native_id else None
    me = igfb_store.self_id(platform, account)

    if t in ("fb_dm", "ig_dm"):
        try:
            atts = await igfb_store.download_attachments(ev.get("attachments") or [])
        except Exception as e:  # noqa: BLE001
            log.warning("IG/FB 私訊附件處理失敗（帶原 url 繼續）: %s", e)
            atts = ev.get("attachments") or []
        await asyncio.to_thread(igfb_store.land_dm, platform, ev, account, atts)

        _notify_inbound(
            platform, account,
            message_id=ev["message_id"],
            sender_id=ev["sender_id"],
            text=ev["text"],
            content_extra={"scope": "dm", "conversation_key": ev["sender_id"],
                           "sender_name": ev.get("sender_name") or ""},
        )
    elif t in ("fb_comment", "ig_comment"):
        await asyncio.to_thread(igfb_store.land_comment, platform, ev, account)

        # 我方自己發的留言不觸發自動回復（避免回覆自己，形成循環）
        if not (me and str(ev.get("from_id") or "") == str(me)):
            _notify_inbound(
                platform, account,
                message_id=ev["comment_id"],
                sender_id=ev["from_id"],
                text=ev["text"],
                content_extra={"scope": "comment", "comment_id": ev["comment_id"],
                               "post_id": ev["post_id"],
                               "conversation_key": ev["comment_id"],
                               "sender_name": ev.get("from_name") or ""},
            )
