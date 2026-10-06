"""微信公眾號 / WhatsApp / IG / FB 消息 → 客服工作台的中轉服務。

收到訊息後一律**只落地本機資料庫**（`forward_log` / `igfb_dm`），
由客服工作台（`/console`）統一查看與回覆；不轉發任何外部系統。

端點：
  GET  /wechat           微信接入驗證（回傳 echostr）
  POST /wechat           接收微信消息（XML），落地本機 DB
  GET  /whatsapp/webhook WhatsApp / IG / FB webhook 驗證（回傳 challenge）
  POST /whatsapp/webhook 接收 Meta 消息（JSON），落地本機 DB
  GET  /files/*          語音 / 視頻 / 圖片下載後的靜態檔案
  GET  /console         客服工作台 UI（查看 / 回覆 / 未讀紅點 / 檔案傳輸）
  GET/POST /console/*   客服工作台後台接口（見 console.py）
"""
import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

import config
import db
import forwarder
import igfb
import igfb_console
import igfb_db
import autoreply
import autoreply_console
import autoreply_db
import token_refresh
import wechat
import whatsapp
import console

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("wechat-forwarder")

# httpx 在 INFO 級別會記錄完整請求 URL（含 query string 裡的 appsecret 等敏感值），
# 一律壓到 WARNING，避免把 token/secret 洩漏進 log。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# jieba 首次載入詞典時會吐 DEBUG 訊息，壓掉以免污染 log
logging.getLogger("jieba").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 自動回復資料表（含既有表的新欄位），可重複執行
    await autoreply_db.init_schema_async()
    # IG 登入權杖自動續期（60 天效期，每天續一次）
    task = asyncio.create_task(token_refresh.ig_token_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="wechat-webhook-forwarder", lifespan=lifespan)

Path(config.MEDIA_DIR).mkdir(exist_ok=True)
app.mount("/files", StaticFiles(directory=config.MEDIA_DIR), name="files")

# 客服工作台 UI + 後台接口（掛在 /console 之下）
app.include_router(console.router)

# IG/FB 收件匣接口（掛在 /console/igfb 之下）
app.include_router(igfb_console.router)

# AI 自動回復設定接口（掛在 /console/autoreply 之下）
app.include_router(autoreply_console.router)


def _cfg() -> dict:
    return {
        "channel": config.CHANNEL,
        "channel_identity_id": config.CHANNEL_IDENTITY_ID,
    }


async def _land_local(platform: str, payload: dict, raw_content: str) -> None:
    """把一則客戶來訊落地到本機 `forward_log`（客服工作台讀的就是這張表）。

    本服務已不再轉發任何外部系統，落地即完成，因此 `forward_status` 一律記 `local`。
    """
    await db.insert_forward_log(db.build_record(
        platform=platform,
        payload=payload,
        raw_content=raw_content,
        forward_status="local",
    ))


@app.get("/wechat")
async def verify(request: Request):
    params = request.query_params
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    echostr = params.get("echostr", "")

    # 安全/兼容模式：用 msg_signature + 加密 echostr
    msg_signature = params.get("msg_signature", "")
    if msg_signature:
        if wechat.check_msg_signature(config.WECHAT_TOKEN, timestamp, nonce, echostr, msg_signature):
            try:
                plain = wechat.decrypt(echostr, config.WECHAT_ENCODING_AES_KEY)
            except Exception as e:
                log.error("微信驗證 echostr 解密失敗（EncodingAESKey 與後台可能不一致）: %s", e)
                return Response(content="decrypt failed", status_code=403)
            return Response(content=plain, media_type="text/plain")
        return Response(content="signature mismatch", status_code=403)

    # 明文模式：signature + 明文 echostr
    signature = params.get("signature", "")
    if wechat.check_signature(config.WECHAT_TOKEN, timestamp, nonce, signature):
        return Response(content=echostr, media_type="text/plain")
    return Response(content="signature mismatch", status_code=403)


@app.post("/wechat")
async def receive(request: Request):
    body = await request.body()
    xml_text = body.decode("utf-8")

    # 安全/兼容模式：外層是加密 XML，先解出明文再解析
    if "<Encrypt>" in xml_text:
        outer = wechat.parse_xml(xml_text)
        try:
            plain = wechat.decrypt(outer.get("Encrypt", ""), config.WECHAT_ENCODING_AES_KEY)
        except Exception as e:
            log.error("微信消息解密失敗（EncodingAESKey 與後台可能不一致）: %s", e)
            return Response(content="success", media_type="text/plain")
        msg = wechat.parse_xml(plain)
    else:
        msg = wechat.parse_xml(xml_text)

    log.info("收到微信消息 MsgType=%s FromUser=%s", msg.get("MsgType"), msg.get("FromUserName"))

    payload = wechat.convert(msg, _cfg())
    if payload is None:
        log.info("忽略不支援的消息類型: %s", msg.get("MsgType"))
        return Response(content="success", media_type="text/plain")

    # 語音/視頻等需要下載 media 再回填 url；失敗則帶空 url 繼續轉發
    try:
        await _resolve_files(msg, payload)
    except Exception as e:
        log.warning("文件處理失敗（將帶空 url 轉發）: %s", e)

    # 只落地本機 DB，供客服工作台查看 / 回覆（不轉發任何外部系統）
    await _land_local("wechat", payload, json.dumps(msg, ensure_ascii=False))

    # 無論成敗都回 success，避免微信一直重試
    # （註：微信未認證訂閱號沒有客服消息權限，自動回復會送出失敗並標記待人工）
    if msg.get("MsgType") == "text":
        sender = (payload.get("webClientSender") or {}).get("webClientUUID") or msg.get("FromUserName", "")
        autoreply.spawn_inbound(
            channel="wechat", scope="dm", text=payload.get("messageContent", ""),
            to=sender, message_id=str(payload.get("id", "")),
            conversation_key=sender,
        )

    return Response(content="success", media_type="text/plain")


async def _resolve_files(msg: dict, payload: dict):
    for f in payload.get("uploadedFiles", []):
        if f.get("url"):
            continue
        media_id = msg.get("MediaId", "")
        if not media_id or not config.PUBLIC_BASE_URL:
            continue
        filename = f.get("filename", f"{media_id}.bin")
        save_path = Path(config.MEDIA_DIR) / filename
        if not save_path.exists():
            await forwarder.download_media(
                config.WECHAT_APPID, config.WECHAT_APPSECRET, media_id, str(save_path)
            )
        f["url"] = f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{filename}"
        f["fileSize"] = save_path.stat().st_size


@app.get("/whatsapp/webhook")
@app.get("/webhook")
async def whatsapp_verify(request: Request):
    params = request.query_params
    mode = params.get("hub.mode", "")
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    # WhatsApp 與 IG/FB 共用同一組 Meta webhook（驗證 token 兩者皆認）
    if mode == "subscribe" and (
        whatsapp.verify(config.WHATSAPP_VERIFY_TOKEN, token)
        or whatsapp.verify(config.META_VERIFY_TOKEN, token)
    ):
        return Response(content=challenge, media_type="text/plain")
    return Response(content="verification failed", status_code=403)


def _verify_igfb_signature(raw: bytes, headers) -> None:
    """選用：驗證 IG/FB webhook 的 X-Hub-Signature-256。未開啟或無 secret 時直接放行。"""
    if not config.META_VERIFY_SIGNATURE or not config.META_APP_SECRET:
        return
    sig = headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256="):
        raise ValueError("缺少 X-Hub-Signature-256 簽章")
    expected = hmac.new(config.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig[len("sha256="):], expected):
        raise ValueError("webhook 簽章不匹配")


@app.post("/whatsapp/webhook")
@app.post("/webhook")
async def whatsapp_receive(request: Request):
    raw = await request.body()
    body = json.loads(raw.decode("utf-8"))
    obj = body.get("object", "")

    # IG / FB（私訊 + 貼文留言）：落地本機 DB，供客服工作台查看 / 回覆
    if obj in ("page", "instagram"):
        try:
            _verify_igfb_signature(raw, request.headers)
        except Exception as e:
            log.warning("IG/FB webhook 簽章驗證失敗: %s", e)
            return Response(content="signature invalid", status_code=403)
        events = igfb.extract_events(body)
        await igfb_db.handle_webhook_events(events)
        log.info("IG/FB webhook object=%s events=%d", obj, len(events))
        return Response(content="EVENT_RECEIVED", media_type="text/plain")

    items = whatsapp.extract_messages(body)
    cfg = {
        "channel": config.WHATSAPP_CHANNEL,
        "channel_identity_id": config.WHATSAPP_CHANNEL_IDENTITY_ID,
    }

    for item in items:
        m = item["message"]
        log.info("收到 WhatsApp 消息 type=%s from=%s", m.get("type"), m.get("from"))

        payload = whatsapp.convert(m, cfg, item["profile_name"])
        if payload is None:
            log.info("忽略 WhatsApp 消息類型: %s", m.get("type"))
            continue

        try:
            await _resolve_whatsapp_files(payload)
        except Exception as e:
            log.warning("WhatsApp 文件處理失敗（帶空 url 轉發）: %s", e)

        # 只落地本機 DB，供客服工作台查看 / 回覆（不轉發任何外部系統）
        await _land_local("whatsapp", payload, json.dumps(m, ensure_ascii=False))

        # AI 自動回復（只在已開啟該渠道＋場景時動作；非文字訊息由 autoreply 自行略過）
        sender = (payload.get("webClientSender") or {}).get("webClientUUID") or m.get("from", "")
        autoreply.spawn_inbound(
            channel="whatsapp", scope="dm", text=payload.get("messageContent", ""),
            to=sender, message_id=str(payload.get("id", "")),
            conversation_key=sender, sender_name=item.get("profile_name") or "",
        )

    # Meta 要求快速回 200
    return Response(content="EVENT_RECEIVED", media_type="text/plain")


async def _resolve_whatsapp_files(payload: dict):
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
                config.WHATSAPP_ACCESS_TOKEN, media_id, str(save_path), config.GRAPH_API_VERSION
            )
        f["url"] = f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{filename}"
        f["fileSize"] = save_path.stat().st_size


# 註：原 `POST /whatsapp/reply`（接收目標系統 9033 的回覆並轉發給客戶）已移除。
#     回覆客戶一律走客服工作台 `POST /console/reply`（見 console.py）。
