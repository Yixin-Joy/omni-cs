"""自動回復的編排層（★ CRM 版）：開關判斷 → 呼叫引擎 → **建任務** → 標記 / 記日誌。

由 AI 入站佇列 worker 消費後呼叫 `handle_inbound()`（非阻塞、失敗只記 log）。

與舊版（platform_forwarder）的關鍵差異：
  * **不再直接發訊息** —— 命中後把回覆寫進 `crm_outbound_task`（`source='ai'`），
    由 Connector 認領送出。所以「送出成功」在這裡等價於「已排隊（queued）」。
  * 圖片與文字的組合（WhatsApp 一條 caption / 微信兩條）由 **Connector** 的
    `build_sub_items()` 決定 —— 這裡只把 `{text, assets}` 塞進任務內容。
  * 訊息狀態標記、對話歷史 → 一律走 Connector 內網 API，不再直連消息表。
  * 日誌欄位改為 CRM 版（`business_id`/`platform`/`scope`/`outbound_task_id`…）。
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from app.core import config
from app.autoreply import autoreply_db
from app.llm import reply_engine
from app.llm.deepseek_client import DeepSeekError  # noqa: F401  (供呼叫端捕捉)

log = logging.getLogger("crm")

# 標記狀態（與 autoreply_db 的常數一致）
STATE_MISS = "miss"
STATE_HIT = "hit"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- 開關 ----------

def is_enabled(business_id: int, channel: str, scope: str,
               platform_account_id: int = 0) -> bool:
    """**兩層 AND**（設計案 §9.1）：

      * 第一層：商家 × 平台 × 場景（`business_ai_switch`）
      * 第二層：平台帳號（`crm_business_platform_account.ai_auto_reply_enabled`）

    `platform_account_id` 是 CRM 的 `crm_business_platform_account.id`
    （不是平台原生帳號 ID）。沒給（0）＝呼叫端沒有帳號上下文（命中測試、人工觸發）
    → 只判斷第一層。

    ⚠️ 別把「沒給帳號」當成「開啟」到處用：正式來訊一定帶著 bp_account_id
    （由 `crm_ai_inbound_queue.business_platform_account_id` 帶進來）。
    """
    if not autoreply_db.effective_scope_mode(business_id, channel, scope):
        return False
    if platform_account_id:
        return autoreply_db.platform_account_enabled(int(platform_account_id))
    return True


# ---------- 送出（= 建任務，由 Connector 真的發） ----------

# WhatsApp 圖片訊息的 caption 上限是 1024 字元；留點餘裕（Connector 端會再判斷）。
CAPTION_MAX = 900


def _asset_items(assets: list) -> list[dict]:
    """把 kb_asset 清單收斂成任務內容裡的 assets（Connector `build_sub_items` 認這個格式）。"""
    out: list[dict] = []
    for a in assets or []:
        if not a or not a.get("rel_path"):
            continue
        out.append({"rel_path": a.get("rel_path"), "mime": a.get("mime") or "image/png",
                    "caption": a.get("caption") or "", "filename": a.get("filename") or ""})
    return out


def enqueue_reply(*, business_id: int, platform: str, platform_account_id: int, to: str,
                  scope: str, text: str, assets: list | None = None, comment_id: str = "",
                  source: str = "ai", staff_id: int | None = None) -> dict:
    """建立外送任務（命中後不再直發）。回傳 `{task_id}`。"""
    from app.internal import repo
    content: dict = {"text": text or ""}
    items = _asset_items(assets or [])
    if items:
        content["assets"] = items
    if scope == "comment" and comment_id:
        content["comment_id"] = comment_id
    return repo.enqueue_outbound_task(
        business_id=business_id, platform=platform, bp_account_id=platform_account_id,
        end_user_sender_id=to, message_content=content, source=source, staff_id=staff_id)


def assets_inline_caption(channel: str, scope: str, assets: list, text: str) -> bool:
    """這次回覆能不能「文字塞進圖片 caption」變成一條（＝客戶不會收到重複的兩條）。

    · WhatsApp：能 → True（實際由 Connector 執行）。
    · 微信 / IG / FB：不能（平台限制）→ False。
    · 貼文留言（公開場合）不發圖 → False。

    僅用於**日誌說明**（這次是不是帶圖、文字是否隨圖）；真正的分派在 Connector。
    """
    return bool(assets and scope == "dm" and channel == "whatsapp"
                and (text or "").strip() and len(text) <= CAPTION_MAX)


def _strip_images(text: str) -> str:
    """把回覆文字裡的 markdown 圖片標記拿掉（客戶端不認 markdown）。

    圖不是丟掉 —— 它由任務的 `assets` 以真正的圖片訊息送出。
    """
    try:
        from app.rag import kb_chunk
        return kb_chunk.strip_md_images(text or "")
    except Exception:
        import re as _re
        return _re.sub(r"!\[[^\]\n]*\]\([^)\s]*\)", "", text or "").strip()


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


# ---------- 標記（走 Connector 內網 API） ----------

def _mark(channel: str, scope: str, state: str, tier: str = "", detail: str = "",
          *, message_id: str = "", comment_id: str = "", conversation_key: str = "") -> None:
    """把自動回復狀態寫進原始訊息列（供工作台顯示標記）。"""
    try:
        if scope == "comment":
            autoreply_db.mark_comment(channel, comment_id, state, tier, detail)
        elif channel in ("ig", "fb"):
            autoreply_db.mark_dm(channel, message_id, state, tier, detail)
        else:
            # 微信 / WhatsApp 的訊息記在 forward_log，用 message_id 定位。
            # 沒有 message_id 時以 conversation_id 兜底。
            if message_id:
                autoreply_db.mark_forward_log([message_id], channel, state, tier, detail)
            elif conversation_key:
                autoreply_db.mark_forward_log_handled(channel, conversation_key)
    except Exception as e:
        log.warning("寫入自動回復標記失敗 %s/%s: %s", channel, scope, e)


def _log(record: dict, *, claim_key: str = "") -> None:
    """寫入日誌。若有認領列就改寫它（一則訊息一列），否則插新列。

    批次補做 / 人工觸發的訊息可能沒有認領列，改寫 0 列時要退回插新列，
    否則這筆紀錄會憑空消失。
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

    兩者都沒有時，退回「渠道＋場景＋會話＋文字雜湊」，同一句話重複投遞仍只處理一次。
    """
    ident = (comment_id or "").strip() or (message_id or "").strip()
    if not ident:
        digest = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
        ident = f"{conversation_key}:{digest}"
    return f"{channel}:{scope}:{ident}"


def _base_record(*, business_id: int, channel: str, scope: str, platform_account_id: int,
                 end_user_sender_id: str) -> dict:
    """日誌的共同欄位。"""
    return {"business_id": int(business_id), "platform": channel, "scope": scope,
            "business_platform_account_id": platform_account_id or None,
            "end_user_sender_id": end_user_sender_id or None}


# ---------- 人工確認的引擎回覆（工作台右鍵「由自動回復引擎給出回復」） ----------

def mark_manual_reply(business_id: int, channel: str, scope: str, tier: str, *,
                      platform_account_id: int = 0, comment_id: str = "",
                      conversation_key: str = "", end_user_sender_id: str = "") -> None:
    """客服確認送出引擎產生的回覆後，把該則標成「人工確認的自動回復-<層>命中」。"""
    if tier not in (reply_engine.TIER_FAQ, reply_engine.TIER_TABLE, reply_engine.TIER_RAG):
        return
    label = {reply_engine.TIER_FAQ: "自動回復-FAQ命中",
             reply_engine.TIER_TABLE: "自動回復-表格命中",
             reply_engine.TIER_RAG: "自動回復-知識庫命中"}[tier]
    detail = f"人工確認的{label}"

    _mark(channel, scope, STATE_HIT, tier=tier, detail=detail,
          message_id="", comment_id=comment_id, conversation_key=conversation_key)

    _log({**_base_record(business_id=business_id, channel=channel, scope=scope,
                         platform_account_id=platform_account_id,
                         end_user_sender_id=end_user_sender_id or conversation_key),
          "tier": tier, "detail": detail, "send_status": "sent"})
    log.info("人工確認的引擎回覆已送出（%s/%s，%s）", channel, scope, label)


def log_manual_miss(business_id: int, channel: str, scope: str, *, platform_account_id: int = 0,
                    text: str = "", detail: str = "", message_id: str = "", comment_id: str = "",
                    post_id: str = "", conversation_key: str = "",
                    end_user_sender_id: str = "", matched_rows: list | None = None) -> None:
    """人工按了「由自動回復引擎給出回復」但引擎沒命中 → 也留一筆紀錄。"""
    ck = _claim_key(channel, scope, message_id, comment_id, conversation_key, text) + ":manual"
    note = (detail or "").strip()
    record = {
        **_base_record(business_id=business_id, channel=channel, scope=scope,
                       platform_account_id=platform_account_id,
                       end_user_sender_id=end_user_sender_id or conversation_key),
        "claim_key": ck,
        "tier": reply_engine.TIER_MISS,
        "detail": ("人工選擇由引擎回覆，但未命中：" + note) if note else "人工選擇由引擎回覆，但未命中",
        "question": text, "message_id": message_id, "send_status": "not_sent",
        "matched_rows": matched_rows,
    }
    try:
        if not autoreply_db.claim_message(ck, record):
            return
        log.info("人工未命中已記錄（%s/%s）", channel, scope)
    except Exception as e:
        log.warning("寫入人工未命中紀錄失敗: %s", e)


def _usage(answer) -> dict:
    """把這次決策的用量欄位抽出來，讓每筆 log 都能回答「為什麼這麼慢」。"""
    return {"elapsed_ms": getattr(answer, "elapsed_ms", 0),
            "llm_calls": getattr(answer, "llm_calls", 0),
            "llm_seconds": getattr(answer, "llm_seconds", 0.0)}


# ---------- 防護 ----------

def _guard(business_id: int, end_user_sender_id: str, ignore_cooldown: bool = False) -> str:
    """回傳擋下的原因；空字串表示放行。

    ignore_cooldown：批次補做時用（人工剛開啟開關不該被冷卻擋掉）；每日上限仍保留。
    """
    if not end_user_sender_id:
        return ""
    cooldown = config.AUTOREPLY_COOLDOWN_SECONDS
    if (not ignore_cooldown and cooldown > 0
            and autoreply_db.recent_reply_count(business_id, end_user_sender_id, cooldown) > 0):
        return f"冷卻中（{cooldown} 秒內已自動回復過）"
    daily = config.AUTOREPLY_MAX_PER_DAY
    if daily > 0 and autoreply_db.today_reply_count(business_id, end_user_sender_id) >= daily:
        return f"已達今日自動回復上限（{daily} 則）"
    return ""


# ---------- 主流程 ----------

async def handle_inbound(
    *,
    business_id: int,
    channel: str,
    scope: str,
    text: str,
    platform_account_id: int = 0,
    to: str = "",
    message_id: str = "",
    comment_id: str = "",
    post_id: str = "",
    post_config_id: int = 0,
    conversation_key: str = "",
    sender_name: str = "",
    reprocess: bool = False,
    ignore_cooldown: bool = False,
) -> dict:
    """處理一則客戶來訊的自動回復。任何失敗都只記 log，不往上拋。

    ★ CRM 版：命中後**建任務**（`crm_outbound_task`），不再直發。

    `post_config_id`：貼文留言場景要用的「貼文覆寫集」（`post_ai_config.id`）；
    0 = 沒配置過 → 與私訊相同，用商家基準集（設計案 §9.2）。

    reprocess：批次補做既有未處理訊息時用（那些訊息已被認領過，需跳過認領）。
    ignore_cooldown：同上（每日上限仍保留）。
    """
    conversation_key = conversation_key or to or comment_id
    end_user = to or conversation_key
    question = (text or "").strip()
    base = _base_record(business_id=business_id, channel=channel, scope=scope,
                        platform_account_id=platform_account_id, end_user_sender_id=end_user)

    # 1) 開關（兩層 AND：商家×平台×場景 + 平台帳號）
    if not is_enabled(business_id, channel, scope, platform_account_id):
        log.debug("自動回復未開啟，略過 %s/%s（biz=%s，acc=%s）",
                  channel, scope, business_id, platform_account_id)
        return {"state": STATE_SKIPPED, "reason": "disabled"}

    # 2) 原子認領：同一則訊息只處理一次。
    claim_key = _claim_key(channel, scope, message_id, comment_id, conversation_key, question)
    if reprocess:
        claimed = True
    else:
        claimed = await asyncio.to_thread(autoreply_db.claim_message, claim_key, {
            **base, "claim_key": claim_key, "tier": autoreply_db.TIER_CLAIM,
            "question": question, "message_id": message_id, "send_status": "pending",
        })
    if not claimed:
        log.info("這則訊息已處理過，略過重複投遞（%s/%s msg=%s）",
                 channel, scope, (message_id or comment_id or claim_key)[-24:])
        return {"state": STATE_SKIPPED, "reason": "duplicate"}

    # 3) 只處理文字訊息（圖片 / 語音 / 貼圖等留給人工）
    if not question:
        _mark(channel, scope, STATE_SKIPPED, detail="非文字訊息，交由人工處理",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({**base, "tier": reply_engine.TIER_MISS, "detail": "非文字訊息，未觸發自動回復",
              "message_id": message_id, "send_status": "not_sent"}, claim_key=claim_key)
        return {"state": STATE_SKIPPED, "reason": "empty_text"}

    # 4) 防護（冷卻 / 每日上限）
    blocked = await asyncio.to_thread(_guard, business_id, end_user, ignore_cooldown)
    if blocked:
        _mark(channel, scope, STATE_SKIPPED, detail=f"未觸發自動回復：{blocked}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({**base, "tier": reply_engine.TIER_MISS, "detail": blocked, "question": question,
              "message_id": message_id, "send_status": "not_sent"}, claim_key=claim_key)
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
                platform_account_id=platform_account_id,
            )
        except Exception as e:
            log.debug("取對話上下文失敗 %s/%s: %s", channel, scope, e)
            history = []

    # 貼文內容（留言場景的額外背景）——階段四接 Connector 貼文端點，先留空
    post_context = ""

    # 6) 這次要用哪一組啟用集（設計案 §9.2）：私訊 / 未配置過的貼文 = 商家基準集；
    #    貼文留言且配置過 → 該貼文的覆寫集。空集代表該層不跑。
    scope_set = None
    try:
        scope_set = await asyncio.to_thread(
            autoreply_db.resolve_scope_set, business_id,
            scope=scope, post_config_id=post_config_id)
    except Exception as e:
        log.warning("取啟用集失敗（改用商家基準集）%s/%s: %s", channel, scope, e)

    try:
        answer = await reply_engine.answer(question, business_id=business_id, channel=channel,
                                           scope=scope, platform_account_id=platform_account_id,
                                           post_context=post_context, history=history,
                                           scope_set=scope_set)
    except Exception as e:
        log.warning("自動回復引擎失敗 %s/%s: %s", channel, scope, e)
        _mark(channel, scope, STATE_MISS, detail=f"引擎錯誤：{e}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({**base, "tier": reply_engine.TIER_MISS, "detail": str(e), "question": question,
              "message_id": message_id, "send_status": "not_sent"}, claim_key=claim_key)
        return {"state": STATE_MISS, "reason": "engine_error"}

    # 6) 未命中 → 降級人工，標記待處理
    if not answer.hit:
        _mark(channel, scope, STATE_MISS, tier=answer.tier,
              detail=answer.detail or "FAQ、業務表格與知識庫皆未命中，待人工處理",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({**base, "tier": reply_engine.TIER_MISS, "detail": answer.detail,
              "question": question, "message_id": message_id, "score": answer.score,
              "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
              "history_used": answer.history_used, "send_status": "not_sent",
              "matched_rows": answer.matched_rows, **_usage(answer)}, claim_key=claim_key)
        log.info("自動回復未命中（%s/%s）：%s", channel, scope, answer.reason)
        return {"state": STATE_MISS, "reason": answer.reason, "detail": answer.detail}

    # 7) 命中 → 建任務（發送交給 Connector）
    # ⚠️ 客戶端不渲染 markdown，`![題注](/media/x.png)` 要剝掉；圖改由任務的 assets 真的發。
    assets = [a for a in (getattr(answer, "assets", None) or []) if a and a.get("rel_path")]
    send_text = _strip_images(answer.text)
    if not send_text.strip() and assets:
        send_text = _assets_caption(assets)   # 純圖片回覆：文字用題注當說明
    label = answer.label                       # 「自動回復-FAQ命中」/「自動回復-表格命中」
    with_assets = assets_inline_caption(channel, scope, assets, send_text)

    task_id = None
    try:
        task = await asyncio.to_thread(
            enqueue_reply,
            business_id=business_id, platform=channel, platform_account_id=platform_account_id,
            to=to, scope=scope, text=send_text, assets=assets, comment_id=comment_id,
            source="ai")
        task_id = task.get("task_id")
    except Exception as e:
        log.warning("自動回復建任務失敗 %s/%s: %s", channel, scope, e)
        _mark(channel, scope, STATE_FAILED, tier=answer.tier,
              detail=f"{label}（建任務失敗，待人工處理）：{str(e)[:160]}",
              message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)
        _log({**base, "tier": answer.tier, "detail": f"{label}（建任務失敗）",
              "question": question, "answer": answer.text, "score": answer.score,
              "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
              "history_used": answer.history_used, "send_status": "failed",
              "matched_rows": answer.matched_rows, **_usage(answer)}, claim_key=claim_key)
        return {"state": STATE_FAILED, "tier": answer.tier, "label": label, "error": str(e)[:300]}

    # 已排隊（真實送出結果由 Connector 回調更新任務狀態）
    if answer.faq_id:
        try:
            await asyncio.to_thread(autoreply_db.bump_faq_hit, answer.faq_id)
        except Exception:
            pass

    n_asset = len(assets) if assets else 0
    asset_note = ""
    if n_asset:
        asset_note = f"附圖 {n_asset}" + ("（文字隨圖）" if with_assets else "")
    reply_detail = (label + ("（改寫後命中）" if answer.rewrote else "") +
                    (f"（{asset_note}）" if asset_note else ""))
    _mark(channel, scope, STATE_HIT, tier=answer.tier, detail=reply_detail,
          message_id=message_id, comment_id=comment_id, conversation_key=conversation_key)

    _log({**base, "tier": answer.tier, "detail": reply_detail, "question": question,
          "answer": answer.text, "message_id": message_id, "score": answer.score,
          "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
          "history_used": answer.history_used, "matched_rows": answer.matched_rows,
          "outbound_task_id": task_id, "send_status": "queued", **_usage(answer)},
         claim_key=claim_key)
    log.info("自動回復已排隊（%s/%s，%s%s%s，task=%s）：%s", channel, scope, label,
             "，改寫後命中" if answer.rewrote else "",
             ("，" + asset_note) if asset_note else "", task_id, answer.text[:60])
    return {"state": STATE_HIT, "tier": answer.tier, "label": label, "answer": answer.text,
            "rewrote": answer.rewrote, "rewritten_query": answer.rewritten_query,
            "assets": answer.assets, "task_id": task_id}


# ---------- 給背景流程用的非阻塞入口 ----------

def spawn_inbound(**kwargs) -> None:
    """在背景跑自動回復，不阻塞呼叫端。"""
    async def runner():
        try:
            await handle_inbound(**kwargs)
        except Exception as e:
            log.warning("自動回復處理失敗（背景）: %s", e)

    try:
        asyncio.create_task(runner())
    except RuntimeError:
        log.debug("沒有事件迴圈，略過自動回復")
