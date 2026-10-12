"""自動回復的編排層：開關判斷 → 呼叫引擎 → 送出 → 標記 / 記日誌。

由 webhook 收到客戶訊息後呼叫 `handle_inbound()`（非阻塞、失敗只記 log）。
它負責：
  * 每個渠道＋場景（私訊 / 貼文留言）各自一把開關；
  * 命中 FAQ / 業務表格 → 實際發送給客戶，並在訊息與日誌標「自動回復-<FAQ命中/表格命中>」；
  * 三層都沒命中 → 不發送，把該則訊息標成 `miss`（工作台顯示「自動回復未命中，待處理」）；
  * 冷卻與每日上限防護，避免同一會話被自動回復洗版。
"""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from app.core import config
from app.core import db
from app.channels import igfb_db
from app.autoreply import autoreply_db
from app.llm import reply_engine
from app.channels import wechat
from app.llm.deepseek_client import DeepSeekError  # noqa: F401  (供呼叫端捕捉)

log = logging.getLogger("wechat-forwarder")

# 標記狀態（與 autoreply_db 的常數一致）
STATE_MISS = "miss"
STATE_HIT = "hit"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 開關 ----------

def is_enabled(channel: str, scope: str) -> bool:
    return autoreply_db.effective_scope_mode(channel, scope)


# ---------- 送出 ----------

async def _send(channel: str, scope: str, text: str, *, to: str = "",
                comment_id: str = "") -> dict:
    """實際送訊息。回傳 {ok, status, body, error}。"""
    if scope == "comment":
        from app.channels import igfb
        result = await igfb.reply_comment(channel, comment_id, text)
        return {"ok": result.get("status") == 200, "status": result.get("status"),
                "body": result.get("body") or ""}

    if channel == "whatsapp":
        from app.channels import whatsapp_send
        result = await whatsapp_send.send_text(to, text)
    elif channel in ("ig", "fb"):
        from app.channels import igfb
        result = await igfb.send_dm(channel, to, "text", text, "", "")
    elif channel == "wechat":
        from app.channels import wechat_send
        result = await asyncio.to_thread(wechat_send.send_text, to, text)
    else:
        raise ValueError(f"不支援的渠道：{channel}")

    status = result.get("status")
    return {"ok": status == 200, "status": status, "body": result.get("body") or ""}


# WhatsApp 圖片訊息的 caption 上限是 1024 字元；留點餘裕，超過就改走「文字一條 + 圖一條」。
CAPTION_MAX = 900


def public_asset_url(rel_path: str) -> str:
    """`kb_asset.rel_path` → **Meta 抓得到的絕對網址**。

    為什麼 IG / FB 一定要絕對網址：它們的 attachment 只收 `payload.url`，
    由 Meta 的伺服器**自己去下載**那個檔案 —— 所以那個網址必須是外網可達、
    不需登入的（私有路徑、localhost 一律不行）。

    能拼出絕對網址的前提（兩個都要成立，否則回空字串）：
      ① 圖在**公開圖片目錄**（`media/kb_assets/`，掛在 `/media`）——
         舊圖存在 `media/kb/<uuid>/assets/` 底下，`media_url_of_rel()` 會回空；
      ② 設了 `PUBLIC_BASE_URL`（ngrok 隧道）—— 沒設就只有相對路徑，Meta 抓不到。

    回空字串 = 這張圖發不出去，呼叫端要**記下原因**而不是靜靜略過。
    """
    try:
        from app.rag import rag_config
        rel = rag_config.media_url_of_rel(rel_path or "")
    except Exception:
        rel = ""
    if not rel:
        return ""
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")  # 模組頂層已 import config
    if not base:
        return ""
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    return base + "/" + rel.lstrip("/")


async def _send_assets(channel: str, scope: str, assets: list, *, to: str = "",
                       caption: str = "") -> dict:
    """圖片隨文字一起送出（知識庫「圖文結合輸出」的輸出端）。

    只有**私訊**才發圖 —— 貼文留言是公開場合，塞圖會很吵，也不好收回。

    各渠道怎麼送：

    | 渠道 | 方式 | 能不能帶文字 |
    |---|---|---|
    | WhatsApp | 上傳拿 media_id → `send_image(caption=)` | ✅ caption |
    | 微信 | 上傳臨時素材 → `send_image` | ❌ 客服訊息沒有 caption 欄位 |
    | **IG / FB** | `send_dm(..., "file", file_url, mime)` → Meta 自己抓 URL | ❌ attachment 只有 url |

    回傳 `{sent, failed, skipped, captioned, reason}`：

    * ★ `captioned=True` = **文字已經隨圖片一起送出去了**（WhatsApp 的 caption），
      呼叫端**不可以再單獨發一條純文字** —— 否則客戶會收到「一條純文字 + 一條一模一樣帶圖的」
      （實測就是這個症狀）。微信／IG／FB 的圖片不帶文字 → 回 False，由呼叫端補發文字。
    * ⚠️ **caption 只貼第一張成功送出的圖**：多張圖時若每張都貼同一段說明，
      客戶端會看到同一段話重複出現。
    * `reason` 是給人看的「為什麼沒送成」（例：沒設 `PUBLIC_BASE_URL`、
      圖不在公開目錄、Meta 回 400）—— 以前這裡是靜默 `skipped`，
      使用者只看到「IG 沒收到圖」卻查不出原因。
    * 圖送失敗不算回覆失敗 —— 文字另外發，圖只是加分項。
    """
    # body/status 供呼叫端寫進 forward_log.target_response（「這條到底送了什麼」的證據）
    out = {"sent": 0, "failed": 0, "skipped": 0, "captioned": False,
           "status": None, "body": "", "reason": ""}
    if scope != "dm" or not assets:
        out["skipped"] = len(assets or [])
        return out

    from app.core import config
    cap_left = (caption or "")[:CAPTION_MAX]
    for a in assets:
        rel = a.get("rel_path") or ""
        path = os.path.join(config.MEDIA_DIR, rel)
        if not rel or not os.path.exists(path):
            out["failed"] += 1
            out["reason"] = out["reason"] or "找不到圖檔"
            continue
        mime = a.get("mime") or "image/png"
        try:
            if channel == "whatsapp":
                from app.channels import whatsapp_send
                mid = await whatsapp_send.upload_media(path, mime, a.get("filename") or "")
                if not mid:
                    out["failed"] += 1
                    out["reason"] = out["reason"] or "WhatsApp 上傳素材失敗"
                    continue
                r = await whatsapp_send.send_image(to, mid, caption=cap_left)
                ok = (r or {}).get("status") == 200
                if ok and cap_left.strip():
                    out["captioned"] = True    # 文字已經跟著這張圖出去了
                    cap_left = ""              # 後面的圖不再重複同一段說明
            elif channel == "wechat":
                from app.channels import wechat_send
                r = await asyncio.to_thread(wechat_send.send_image, to, path)
                ok = (r or {}).get("status") == 200
                # ⚠️ 微信圖片不帶文字 → captioned 保持 False，由呼叫端補發文字
            elif channel in ("ig", "fb"):
                # IG / FB：把**公開絕對網址**交給 Meta，由它自己來抓圖。
                url = public_asset_url(rel)
                if not url:
                    out["skipped"] += 1
                    out["reason"] = out["reason"] or (
                        "圖片不在公開目錄，或沒設定 PUBLIC_BASE_URL"
                        "（IG／FB 的圖必須是外網可達的網址，Meta 會自己來下載）")
                    continue
                from app.channels import igfb
                # message_type="file" + mime=image/* → igfb 內部會挑 ig_send_attachment
                r = await igfb.send_dm(channel, to, "file", "", url, mime)
                ok = (r or {}).get("status") == 200
            else:
                out["skipped"] += 1
                out["reason"] = out["reason"] or f"這個渠道（{channel}）不支援自動發圖"
                continue
            out["sent" if ok else "failed"] += 1
            if not ok:
                out["reason"] = out["reason"] or f"送出失敗（HTTP {(r or {}).get('status')}）"
            if out["status"] is None:
                out["status"] = (r or {}).get("status")
                out["body"] = (r or {}).get("body") or ""
        except Exception as e:
            log.warning("自動回復附圖送出失敗（%s）：%s", a.get("id"), e)
            out["failed"] += 1
            out["reason"] = out["reason"] or f"{type(e).__name__}: {e}"
    return out


# ---------- 標記 ----------

def _strip_images(text: str) -> str:
    """把回覆文字裡的 markdown 圖片標記拿掉（客戶端不認 markdown）。

    圖不是丟掉 —— 它由 `_send_assets()` 以真正的圖片訊息送出。
    """
    try:
        from app.rag import kb_chunk
        return kb_chunk.strip_md_images(text or "")
    except Exception:
        import re as _re
        return _re.sub(r"!\[[^\]\n]*\]\([^)\s]*\)", "", text or "").strip()


def assets_inline_caption(channel: str, scope: str, assets: list, text: str) -> bool:
    """這次回覆能不能「文字塞進圖片 caption」變成一條（＝客戶不會收到重複的兩條）。

    ★ 小jo 回報：舊版固定「先發一條純文字 → 再發一張 caption 又是同一段文字的圖」，
    客戶端就看到「一條不帶圖的 + 一條一模一樣帶圖的」。能帶 caption 的渠道就一條搞定。

    · WhatsApp：`send_image(to, media_id, caption)` 支援（caption 上限 1024 字元）→ True。
    · 微信：客服消息的圖片**沒有 caption 欄位** → 只能文字一條、圖一條（平台限制）。
    · IG／FB：attachment 的 payload **只有 url、沒有 caption** → 同樣只能兩條
      （圖會發，但文字要另外發；見 `_send_assets` 的 ig/fb 分支）。
    · 貼文留言（公開場合）不發圖 → False。
    """
    return bool(assets and scope == "dm" and channel == "whatsapp"
                and (text or "").strip() and len(text) <= CAPTION_MAX)


def _assets_caption(assets: list) -> str:
    """純圖片回覆時的文字說明（用題注／描述，沒有就用一句通用話）。"""
    caps = []
    for a in assets or []:
        c = (a or {}).get("caption") or ""
        if c.strip():
            caps.append(c.strip())
    if not caps:
        return "請參考下圖。"
    return f"請參考下圖（{'、'.join(caps[:3])}）。"


def _mark(channel: str, scope: str, state: str, tier: str = "", detail: str = "",
          *, message_id: str = "", comment_id: str = "", conversation_key: str = "") -> None:
    """把自動回復狀態寫進原始訊息列（供工作台顯示標記）。"""
    try:
        if scope == "comment":
            autoreply_db.mark_comment(channel, comment_id, state, tier, detail)
        elif channel in ("ig", "fb"):
            autoreply_db.mark_dm(channel, message_id, state, tier, detail)
        else:
            # 微信 / WhatsApp 的訊息記在 forward_log，以 sender_id 對應 conversation
            if message_id:
                autoreply_db.mark_forward_log([message_id], channel, state, tier, detail)
            elif conversation_key:
                _mark_latest_forward_log(channel, conversation_key, state, tier, detail)
    except Exception as e:
        log.warning("寫入自動回復標記失敗 %s/%s: %s", channel, scope, e)


def _mark_latest_forward_log(channel: str, conversation_key: str, state: str,
                            tier: str, detail: str) -> None:
    """以會話為單位標記最後一則客戶來訊（webhook 未帶 message_id 時的退路）。"""
    rows = autoreply_db.query("""
        SELECT message_id FROM forward_log
        WHERE platform=%s AND direction='inbound'
          AND (message_id=%s OR sender_id=%s OR conversation_id=%s)
        ORDER BY created_at DESC LIMIT 1
    """, (channel, conversation_key, conversation_key,
          wechat.openid_to_conversation_id(conversation_key)))
    if rows and rows[0]["message_id"]:
        autoreply_db.mark_forward_log([rows[0]["message_id"]], channel, state, tier, detail)


def _log(record: dict, *, claim_key: str = "") -> None:
    """寫入日誌。若有認領列就改寫它（一則訊息一列），否則插新列。

    批次補做（backfill）的訊息可能沒有認領列（例如人工直接觸發），
    改寫 0 列時要退回插新列，否則這筆紀錄會憑空消失。
    """
    try:
        if claim_key and autoreply_db.update_claim(claim_key, record) > 0:
            return
        autoreply_db.insert_log(record)
    except Exception as e:
        log.warning("寫入自動回復日誌失敗: %s", e)


def _claim_key(channel: str, scope: str, message_id: str, comment_id: str,
               conversation_key: str, text: str) -> str:
    """一則訊息的唯一識別：優先用平台給的訊息 id / 留言 id。

    兩者都沒有時（例如某些微信訊息），退回「渠道＋場景＋會話＋文字雜湊」，
    這樣同一句話在同一會話被重複投遞仍然只會處理一次。
    """
    ident = (comment_id or "").strip() or (message_id or "").strip()
    if not ident:
        digest = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
        ident = f"{conversation_key}:{digest}"
    return f"{channel}:{scope}:{ident}"


# ---------- 人工確認的引擎回覆（工作台右鍵「由自動回復引擎給出回復」） ----------

def mark_manual_reply(channel: str, scope: str, tier: str, *, comment_id: str = "",
                      conversation_key: str = "") -> None:
    """客服確認送出引擎產生的回覆後，把該則標成「人工確認的自動回復-<層>命中」。

    與真正的自動回復區分：state 一樣是 hit，但 detail 以「人工確認」開頭，
    工作台據此顯示「人工確認的…」；log 的 reason 記 manual，方便和自動流程分開統計。
    """
    if tier not in (reply_engine.TIER_FAQ, reply_engine.TIER_TABLE, reply_engine.TIER_RAG):
        return
    label = {reply_engine.TIER_FAQ: "自動回復-FAQ命中",
             reply_engine.TIER_TABLE: "自動回復-表格命中",
             reply_engine.TIER_RAG: "自動回復-知識庫命中"}[tier]
    detail = f"人工確認的{label}"

    message_id = ""
    if scope != "comment" and channel in ("ig", "fb"):
        # 先查出要標哪一則（各回覆端點送出後會把 inbound 全部標成已處理，所以要在此之前標）
        try:
            row = autoreply_db.latest_unanswered_dm(channel, conversation_key)
            message_id = (row or {}).get("message_id") or ""
        except Exception as e:
            log.warning("查未回覆私訊失敗 %s/%s: %s", channel, conversation_key, e)

    _mark(channel, scope, STATE_HIT, tier=tier, detail=detail,
          message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)

    _log({"channel": channel, "reply_scope": scope, "tier": tier, "reason": "manual",
          "detail": detail, "conversation_key": conversation_key, "message_id": message_id,
          "comment_id": comment_id, "send_status": "sent"})
    log.info("人工確認的引擎回覆已送出（%s/%s，%s）", channel, scope, label)


def log_manual_miss(channel: str, scope: str, *, text: str = "", detail: str = "",
                    message_id: str = "", comment_id: str = "", post_id: str = "",
                    conversation_key: str = "", matched_rows: list | None = None) -> None:
    """人工按了「由自動回復引擎給出回復」但引擎沒命中 → 也留一筆紀錄。

    為什麼要記：命中的會由 mark_manual_reply 記成「人工確認的…命中」，
    但未命中在原本設計裡完全不留痕跡 → 紀錄頁看不出「這一則被人工丟給引擎試過、答不出來」，
    也就無從判斷該補哪條 FAQ / 表格資料。

    與自動流程的未命中區分：
      * reason 記 `manual_no_match`（自動流程是 no_match / llm_error / …）
      * claim_key 加 `:manual` 後綴 → 同一則人工只留第一筆（連點不會灌爆），
        也不會和自動流程為同一則寫的那筆互相覆蓋（那是不同的兩次事件）。

    只寫 log，**不動**訊息上的 auto_reply_state —— 人工右鍵失敗不該影響未讀紅點與
    auto_miss_count 的統計。
    """
    ck = _claim_key(channel, scope, message_id, comment_id, conversation_key, text) + ":manual"
    note = (detail or "").strip()
    record = {
        "claim_key": ck,
        "channel": channel, "reply_scope": scope,
        "tier": reply_engine.TIER_MISS, "reason": "manual_no_match",
        "detail": ("人工選擇由引擎回覆，但未命中：" + note) if note else "人工選擇由引擎回覆，但未命中",
        "question": text, "conversation_key": conversation_key,
        "message_id": message_id, "post_id": post_id, "comment_id": comment_id,
        "send_status": "not_sent",
        "matched_rows": matched_rows,
    }
    try:
        # claim_message 走 ON CONFLICT (claim_key) DO NOTHING → 回 False 表示這次已經記過
        if not autoreply_db.claim_message(ck, record):
            return
        log.info("人工未命中已記錄（%s/%s）", channel, scope)
    except Exception as e:
        log.warning("寫入人工未命中紀錄失敗: %s", e)


def _usage(answer) -> dict:
    """把這次決策的用量欄位抽出來，讓每筆 log 都能回答「為什麼這麼慢」。

    一次知識庫回覆可能呼叫模型 8 次（實測 48.9 秒），光看 detail 看不出來；
    紀錄頁有這三欄，就不用再靠猜。
    """
    return {"elapsed_ms": getattr(answer, "elapsed_ms", 0),
            "llm_calls": getattr(answer, "llm_calls", 0),
            "llm_seconds": getattr(answer, "llm_seconds", 0.0)}


# ---------- 防護 ----------

def _guard(conversation_key: str, ignore_cooldown: bool = False) -> str:
    """回傳擋下的原因；空字串表示放行。

    ignore_cooldown：批次補做（開啟自動回復後補處理既有未處理訊息）時用，
    人工剛按下開關就不該被「30 秒內已回過」擋掉；每日上限仍然保留。
    """
    if not conversation_key:
        return ""
    cooldown = config.AUTOREPLY_COOLDOWN_SECONDS
    if not ignore_cooldown and cooldown > 0 and autoreply_db.recent_reply_count(conversation_key, cooldown) > 0:
        return f"冷卻中（{cooldown} 秒內已自動回復過）"
    daily = config.AUTOREPLY_MAX_PER_DAY
    if daily > 0 and autoreply_db.today_reply_count(conversation_key) >= daily:
        return f"已達今日自動回復上限（{daily} 則）"
    return ""


# ---------- 主流程 ----------

async def handle_inbound(
    *,
    channel: str,
    scope: str,
    text: str,
    to: str = "",
    message_id: str = "",
    comment_id: str = "",
    post_id: str = "",
    conversation_key: str = "",
    sender_name: str = "",
    reprocess: bool = False,
    ignore_cooldown: bool = False,
) -> dict:
    """處理一則客戶來訊的自動回復。任何失敗都只記 log，不往上拋。

    reprocess：批次補做既有未處理訊息時用 —— 這些訊息在原本的 webhook 流程
      已經被「認領」過（去重表裡有紀錄），不跳過認領就會被當成重複投遞而略過。
    ignore_cooldown：同上（人工剛開啟開關，不該被冷卻擋掉）；每日上限仍保留。
    """
    conversation_key = conversation_key or to or comment_id
    question = (text or "").strip()

    # 1) 開關
    if not is_enabled(channel, scope):
        log.debug("自動回復未開啟，略過 %s/%s", channel, scope)
        return {"state": STATE_SKIPPED, "reason": "disabled"}

    # 2) 原子認領：同一則訊息只處理一次。
    #    Meta / 微信 都會對同一個事件重複投遞，兩份幾乎同時進來，
    #    「冷卻檢查」是先生後寫、擋不住併發，靠唯一索引才是真的只處理一次。
    claim_key = _claim_key(channel, scope, message_id, comment_id, conversation_key, question)
    if reprocess:
        claimed = True
    else:
        claimed = await asyncio.to_thread(autoreply_db.claim_message, claim_key, {
            "claim_key": claim_key, "channel": channel, "reply_scope": scope,
            "tier": autoreply_db.TIER_CLAIM, "reason": "processing",
            "question": question, "conversation_key": conversation_key,
            "message_id": message_id, "post_id": post_id, "comment_id": comment_id,
            "send_status": "pending",
        })
    if not claimed:
        log.info("這則訊息已處理過，略過重複投遞（%s/%s msg=%s）",
                 channel, scope, (message_id or comment_id or claim_key)[-24:])
        return {"state": STATE_SKIPPED, "reason": "duplicate"}

    # 3) 只處理文字訊息（圖片 / 語音 / 貼圖等留給人工）
    if not question:
        _mark(channel, scope, STATE_SKIPPED, detail="非文字訊息，交由人工處理",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({"channel": channel, "reply_scope": scope, "tier": reply_engine.TIER_MISS,
              "reason": "empty_text", "detail": "非文字訊息，未觸發自動回復",
              "conversation_key": conversation_key, "message_id": message_id,
              "post_id": post_id, "comment_id": comment_id, "send_status": "not_sent"},
             claim_key=claim_key)
        return {"state": STATE_SKIPPED, "reason": "empty_text"}

    # 4) 防護（冷卻 / 每日上限）
    blocked = await asyncio.to_thread(_guard, conversation_key, ignore_cooldown)
    if blocked:
        # 標記出來，客服才知道這則為什麼沒被自動回覆
        _mark(channel, scope, STATE_SKIPPED, detail=f"未觸發自動回復：{blocked}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({"channel": channel, "reply_scope": scope, "tier": reply_engine.TIER_MISS,
              "reason": "throttled", "detail": blocked, "question": question,
              "conversation_key": conversation_key, "message_id": message_id,
              "post_id": post_id, "comment_id": comment_id, "send_status": "not_sent"},
             claim_key=claim_key)
        return {"state": STATE_SKIPPED, "reason": "throttled", "detail": blocked}

    # 5) 取對話上下文（業務表格第一次查不到時，改寫 query 要用）
    limit = config.TABLE_REWRITE_HISTORY_TURNS
    history: list[dict] = []
    if limit > 0:
        try:
            history = await asyncio.to_thread(
                autoreply_db.get_conversation_history,
                channel, scope,
                sender_id=to, comment_id=comment_id, message_id=message_id,
                current_text=question, limit=limit,
            )
        except Exception as e:
            log.debug("取對話上下文失敗 %s/%s: %s", channel, scope, e)
            history = []

    # 貼文內容（留言場景的額外背景）
    post_context = ""
    if scope == "comment" and post_id:
        try:
            post = await asyncio.to_thread(igfb_db.get_post, channel, post_id)
            if post and post.get("name"):
                post_context = f"【被留言的貼文內容】\n{str(post['name'])[:500]}"
        except Exception as e:
            log.debug("取貼文內容失敗 %s/%s: %s", channel, post_id, e)

    try:
        answer = await reply_engine.answer(question, channel=channel, scope=scope,
                                           post_context=post_context, history=history)
    except Exception as e:
        log.warning("自動回復引擎失敗 %s/%s: %s", channel, scope, e)
        _mark(channel, scope, STATE_MISS, detail=f"引擎錯誤：{e}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({"channel": channel, "reply_scope": scope, "tier": reply_engine.TIER_MISS,
              "reason": "engine_error", "detail": str(e), "question": question,
              "conversation_key": conversation_key, "message_id": message_id,
              "post_id": post_id, "comment_id": comment_id, "send_status": "not_sent"},
             claim_key=claim_key)
        return {"state": STATE_MISS, "reason": "engine_error"}

    # 6) 未命中 → 降級人工，標記待處理
    if not answer.hit:
        _mark(channel, scope, STATE_MISS, tier=answer.tier,
              detail=answer.detail or "FAQ、業務表格與知識庫皆未命中，待人工處理",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({"channel": channel, "reply_scope": scope, "tier": reply_engine.TIER_MISS,
              "reason": answer.reason, "detail": answer.detail, "question": question,
              "conversation_key": conversation_key, "message_id": message_id,
              "post_id": post_id, "comment_id": comment_id, "score": answer.score,
              "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
              "history_used": answer.history_used, "send_status": "not_sent",
              # ★ 未命中也要留召回明細：這才是調門檻／top-k 最需要的線索
              "matched_rows": answer.matched_rows,
              **_usage(answer)},
             claim_key=claim_key)
        log.info("自動回復未命中（%s/%s）：%s", channel, scope, answer.reason)
        return {"state": STATE_MISS, "reason": answer.reason, "detail": answer.detail}

    # 7) 命中 → 送出
    # ⚠️ 客戶端（WhatsApp／微信）不會渲染 markdown，`![題注](/media/x.png)` 直接送出去
    #    會變成一行看不懂的網址字串。所以文字要**剝掉圖片標記**，圖改由附圖通道真的發。
    #    紀錄裡仍存完整 markdown（工作台會渲染出圖）。
    assets = [a for a in (getattr(answer, "assets", None) or []) if a and a.get("rel_path")]
    send_text = _strip_images(answer.text)
    if not send_text.strip() and assets:
        send_text = _assets_caption(assets)   # 純圖片回覆：文字用題注當說明

    # ★ 送出策略（小jo 2026-10-12 回報：客戶收到「一條不帶圖的 + 一條一模一樣帶圖的」）
    #   舊版固定「文字一條 → 圖一張（caption 又是同一段文字）」→ 內容重複了兩次。
    #   現在：**能用 caption 帶文字的渠道就把文字塞進圖片一起發，只發一條**；
    #   不能帶 caption 的（微信 `send_image` 沒有 caption）才文字一條、圖一條 —— 但圖不帶文字，
    #   客戶端不會看到重複的一段話，而且**順序維持「先說明、後圖片」**讀起來才順。
    asset_note = ""

    async def _push_assets(cap: str) -> dict:
        """發這次命中的圖（私訊才發）。`cap` 給非空 = 文字塞進第一張圖的 caption。"""
        nonlocal asset_note
        try:
            st = await _send_assets(channel, scope, assets, to=to, caption=cap)
        except Exception as e:
            log.warning("自動回復附圖流程失敗（不影響文字回覆）：%s", e)
            return {"sent": 0, "failed": 0, "skipped": 0, "captioned": False}
        # asset_note 只存「內容」，使用處自己包括號（避免「（附圖 （附圖 1/1））」疊字）
        if st["sent"] or st["failed"]:
            asset_note = f"附圖 {st['sent']}/{st['sent'] + st['failed']}"
            log.info("自動回復附圖：送出 %d、失敗 %d%s", st["sent"], st["failed"],
                     "，文字已隨圖送出" if st.get("captioned") else "")
        elif st.get("reason"):
            # ★ 沒送成也要留原因：以前是靜默 skipped，使用者只看到「IG 沒收到圖」
            #   卻完全查不出為什麼（小jo 2026-10-12 就是這樣問的）。
            log.warning("自動回復沒有附圖（%s）：%s", channel, st["reason"])
            asset_note = "無附圖：" + st["reason"]
        return st

    async def _push_text() -> dict:
        try:
            return await _send(channel, scope, send_text, to=to, comment_id=comment_id)
        except Exception as e:
            log.warning("自動回復送出失敗 %s/%s: %s", channel, scope, e)
            return {"ok": False, "status": None, "body": str(e)}

    # WhatsApp 的圖片能帶 caption → 文字隨圖一條送出（其他渠道沒有這個能力）
    if assets_inline_caption(channel, scope, assets, send_text):
        st = await _push_assets(send_text)
        if st.get("captioned"):
            # 文字已經跟圖一起送出去了 → 這就是完整的一條回覆，不再發第二條
            result = {"ok": True, "status": st.get("status"), "body": st.get("body") or ""}
        else:
            result = await _push_text()
            await _push_assets("")     # caption 沒成 → 圖也不再帶文字，免得又重複一段
    else:
        result = await _push_text()
        if result.get("ok") and assets and scope == "dm":
            await _push_assets("")     # 微信等：圖不帶 caption（文字已經單獨發過）

    label = answer.label  # 「自動回復-FAQ命中」/「自動回復-表格命中」

    if not result["ok"]:
        # 有命中但發送失敗：仍標記出來，方便人工接手
        _mark(channel, scope, STATE_FAILED, tier=answer.tier,
              detail=f"{label}（發送失敗，待人工處理）：{str(result.get('body'))[:160]}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({"channel": channel, "reply_scope": scope, "tier": answer.tier,
              "detail": f"{label}（發送失敗）", "reason": "send_failed",
              "question": question, "answer": answer.text, "faq_id": answer.faq_id,
              "conversation_key": conversation_key, "message_id": message_id,
              "post_id": post_id, "comment_id": comment_id, "score": answer.score,
              "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
              "history_used": answer.history_used, "send_status": "failed",
              "matched_rows": answer.matched_rows, **_usage(answer)},
             claim_key=claim_key)
        return {"state": STATE_FAILED, "tier": answer.tier, "label": label,
                "error": str(result.get("body"))[:300]}

    # 送出成功
    if answer.faq_id:
        try:
            await asyncio.to_thread(autoreply_db.bump_faq_hit, answer.faq_id)
        except Exception:
            pass

    # 標記要能看出「是不是帶對話改寫 query 後才命中」，以及「有沒有連圖一起發」
    reply_detail = (label + ("（改寫後命中）" if answer.rewrote else "") +
                    (f"（{asset_note}）" if asset_note else ""))
    _mark(channel, scope, STATE_HIT, tier=answer.tier, detail=reply_detail,
          message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)

    # 我方回覆要落地，工作台才看得到即時回覆
    try:
        if scope == "comment":
            new_id = ""
            try:
                # ⚠️ 不要在這裡寫 `import json`：一旦函式內出現 import json，
                # json 就會被當成 handle_inbound 的「區域變數」（作用域是整個函式），
                # 走 else（whatsapp / wechat）分支時那段 import 根本沒執行 →
                # json.dumps() 直接拋 UnboundLocalError，變成「訊息送出去了、
                # 但沒落地」，web 客服平台看不到這則自動回覆。用模組頂層的 json 即可。
                new_id = str(json.loads(result.get("body") or "{}").get("id") or "")
            except Exception:
                new_id = ""
            await asyncio.to_thread(igfb_db.add_own_reply, channel, comment_id, new_id, answer.text)
        elif channel in ("ig", "fb"):
            await igfb_db.ingest_dm(
                platform=channel, conversation_key=to, thread_id=None, sender_id=to,
                sender_name="", direction="outbound", message_type="text", text=answer.text,
                attachments=[], message_id=_own_message_id(result), created_at=None,
            )
            await asyncio.to_thread(igfb_db.mark_handled, channel, to)
        else:
            conversation_id = wechat.openid_to_conversation_id(to)
            reply_payload = {"to": to, "messageType": "text",
                             "messageContent": answer.text, "autoReply": label}
            await db.insert_forward_log({
                "platform": channel,
                "message_id": "",
                "sender_id": to,
                "conversation_id": conversation_id,
                "message_type": "text",
                # raw_content 必須與其他寫入端一致＝「回覆請求的 JSON 字串」。
                # 從前這裡直接寫 answer.text（純文字），當答案剛好是純數字（例如 "1280"）
                # 時 json.loads 會解析成 int，讀取端 .get() 直接爆 → 整個會話列表 500。
                "raw_content": json.dumps(reply_payload, ensure_ascii=False),
                "forwarded_payload": reply_payload,
                "forward_status": "success",
                "target_http_status": result.get("status"),
                "target_response": str(result.get("body"))[:1000],
                "error_message": None,
                "message_time": None,
                "direction": "outbound",
            })
            await asyncio.to_thread(autoreply_db.mark_forward_log_handled, channel, conversation_id)
    except Exception as e:
        log.warning("自動回復落地失敗 %s/%s: %s", channel, scope, e)

    _log({"channel": channel, "reply_scope": scope, "tier": answer.tier,
          "detail": reply_detail, "reason": "sent", "question": question,
          "answer": answer.text,
          "faq_id": answer.faq_id, "conversation_key": conversation_key,
          "message_id": message_id, "post_id": post_id, "comment_id": comment_id,
          "score": answer.score, "rewrote": answer.rewrote,
          "rewritten_query": answer.rewritten_query, "history_used": answer.history_used,
          "matched_rows": answer.matched_rows,
          "send_status": "sent", **_usage(answer)}, claim_key=claim_key)
    log.info("自動回復已送出（%s/%s，%s%s%s）：%s", channel, scope, label,
             "，改寫後命中" if answer.rewrote else "",
             ("，" + asset_note) if asset_note else "", answer.text[:60])
    return {"state": STATE_HIT, "tier": answer.tier, "label": label, "answer": answer.text,
            "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
            "assets": answer.assets}


def _own_message_id(result: dict) -> str:
    """從發送結果撈出我方訊息 id（撈不到就給時間戳替代值）。"""
    import json
    import time
    try:
        data = json.loads(result.get("body") or "{}")
        return str(data.get("message_id") or data.get("id") or "") or f"auto_{int(time.time() * 1000)}"
    except Exception:
        return f"auto_{int(time.time() * 1000)}"


# ---------- 給 webhook 用的非阻塞入口 ----------

def spawn_inbound(**kwargs) -> None:
    """在背景跑自動回復，不阻塞 webhook 回應。

    Meta / 微信 都要求秒級回應；LLM 呼叫動輒數秒，必須丟到背景。
    """
    async def runner():
        try:
            await handle_inbound(**kwargs)
        except Exception as e:
            log.warning("自動回復處理失敗（背景）: %s", e)

    try:
        asyncio.create_task(runner())
    except RuntimeError:
        # 沒有事件迴圈（例如同步呼叫）時直接放棄，不影響主流程
        log.debug("沒有事件迴圈，略過自動回復")
