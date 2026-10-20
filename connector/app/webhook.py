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
from app.channels import igfb          # 新貼文落地要讀單篇貼文（get_post）
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


def _candidate_app_secrets(body: dict) -> list[tuple[str, str]]:
    """這個 payload 可能由**哪些 App** 送來？回 `[(說明, secret)]`，系統級放最後。

    ★★ 為什麼要這樣（2026-10-20 小jo「以後每個商戶都可能對應一個獨立的 meta app」）：
      簽章 = `HMAC-SHA256(App Secret, raw body)`。一商戶一 App 之後，
      **用系統 secret 去驗別家 App 的簽章一定失敗**（403），
      所有那個商戶的 webhook 會全部被擋掉 —— 這種壞法很隱蔽：
      Meta 那邊看起來「有推」，我們這邊只看到 signature invalid。

      做法：payload 裡 `entry[].id` **就是** page_id / ig_user_id（原生對齊鍵），
      拿它去帳號映射找該帳號的 `credential.meta_app_secret`（CRM 已按商戶解析好）。
      找到就用它驗；找不到才回退系統級。
    ⚠️ 解析 **未驗簽** 的 JSON 只為了「挑要拿哪把鑰匙」，不用它的任何業務內容
       —— 真正處理的 events 是驗簽通過之後才取的。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(desc: str, secret: str) -> None:
        s = (secret or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append((desc, s))

    for ent in (body.get("entry") or []):
        native = str((ent or {}).get("id") or "").strip()
        if not native:
            continue
        for plat in ("fb", "ig", "page", "instagram"):
            acc = account_map.lookup("ig" if plat in ("ig", "instagram") else "fb", native)
            if not acc:
                continue
            cred = acc.get("credential") or {}
            if isinstance(cred, str):        # jsonb 有時回字串
                try:
                    import json as _json
                    cred = _json.loads(cred)
                except Exception:  # noqa: BLE001
                    cred = {}
            add(f"{plat}:{native}", (cred or {}).get("meta_app_secret") or "")

    add("system", config.META_APP_SECRET or "")
    return out


def _verify_igfb_signature(raw: bytes, headers, body: dict | None = None) -> None:
    """驗 IG/FB webhook 簽章。`body` 用來挑「這家商戶的 App Secret」（多租戶）。

    相容性：
      · 關閉驗簽（`META_VERIFY_SIGNATURE=false`）→ 直接放過（本機開發）；
      · 只有系統 secret（舊部署）→ 行為與以前完全一樣；
      · 有商戶 secret → 先用商戶的驗，不對再試系統的（一商戶一 App 與混合部署都能跑）。
    """
    if not config.META_VERIFY_SIGNATURE:
        return
    cands = _candidate_app_secrets(body or {}) if body else []
    if not cands:
        cands = [("system", config.META_APP_SECRET or "")]
    if not any(s for _d, s in cands):
        return                              # 一把 secret 都沒設 → 無從驗，放過（舊行為）
    sig = headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256="):
        raise ValueError("缺少 X-Hub-Signature-256 簽章")
    got = sig[len("sha256="):]
    tried: list[str] = []
    for desc, secret in cands:
        want = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(got, want):
            if desc != "system":
                log.info("webhook 簽章以商戶 App Secret 驗證通過（%s）", desc)
            return
        tried.append(desc)
    raise ValueError("webhook 簽章不匹配（已試：" + "、".join(tried) + "）")


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
            _verify_igfb_signature(raw, request.headers, body)
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


async def _bg_sync_dm_names(platform: str, account: dict | None) -> None:
    """背景補私訊/留言的顯示名稱（失敗只記 log，不影響收訊）。

    從舊單體 `app/channels/igfb_db.py::_bg_sync_dm_names()` 搬過來。
    """
    try:
        got = await igfb_store.sync_dm_names(platform, account)
        if got.get("updated") or got.get("comments"):
            log.info("補名完成 %s：私訊 %s 筆 / 留言 %s 筆",
                     platform, got.get("updated"), got.get("comments"))
    except Exception as e:  # noqa: BLE001
        log.warning("補名失敗 %s: %s", platform, e)


async def _bg_sync_post(platform: str, account: dict | None, ev: dict) -> None:
    """新貼文落地：先用 Graph 讀完整欄位（圖／permalink），失敗才退回 webhook 的資料。

    ★ 為什麼不能只寫 webhook payload（2026-10-20 小jo「新貼文發佈檢測不到」）：
      ① `feed` webhook 對自家發文只有 `post_id / message / created_time`；
      ② 舊版 `extract_events()` 把非 comment 的 feed 事件全 `continue` 掉了，
         所以新貼文只能等 6 小時一次的縮圖續命迴圈 → 體感就是「永遠檢測不到」。
    """
    post_id = str(ev.get("post_id") or "")
    if not post_id:
        return
    cred = (account or {}).get("credential") or {}
    if isinstance(cred, str):
        try:
            import json as _json
            cred = _json.loads(cred)
        except Exception:  # noqa: BLE001
            cred = {}
    post: dict = {}
    try:
        post = await igfb.get_post(platform, post_id, cred)
    except Exception as e:  # noqa: BLE001
        log.warning("新貼文讀取失敗 %s/%s: %s", platform, post_id, e)
    if not post:
        # 退回 webhook 欄位：至少讓它出現在列表（圖之後由續命迴圈補）
        post = {"id": post_id, "message": ev.get("message") or "",
                "created_time": ev.get("created_at")}
        log.info("新貼文 %s/%s 用 webhook 欄位入庫（Graph 讀取失敗）", platform, post_id)
    else:
        post.setdefault("id", post_id)
    try:
        await asyncio.to_thread(igfb_store.upsert_post, platform, account, post)
        log.info("新貼文已落地 %s/%s", platform, post_id)
    except Exception as e:  # noqa: BLE001
        log.warning("新貼文寫入失敗 %s/%s: %s", platform, post_id, e)


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

        # ★ 補名字：**Meta 的 webhook 不帶使用者名稱**（FB 私訊的 sender 只有 id；
        #   FB 留言的 from 連 name 都不回）。沒有這一步，新會話在工作台上永遠是空的
        #   —— 舊單體有 `sync_dm_names()` 在收訊後背景補，這段沒搬過來就是回歸。
        #   背景執行（`create_task`）：補名要打 Graph API，不能卡住 webhook 回應。
        if not (ev.get("sender_name") or "").strip():
            asyncio.create_task(_bg_sync_dm_names(platform, account))

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
    elif t == "fb_post":
        # 粉專自己發了新貼文 → 立刻寫進 `igfb_post`（工作台「貼文留言」才看得到）。
        # ★ 背景執行：要再打一次 Graph 取圖／permalink，不能卡住 webhook 回應。
        #   不需要通知 CRM（沒有要 AI 回覆的東西）。
        asyncio.create_task(_bg_sync_post(platform, account, ev))
