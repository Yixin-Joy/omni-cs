"""AI 入站佇列消費者 —— 單一序列（設計案 §5）。

Connector 收到客戶訊息後**只做兩件事**：落地本機 DB、非同步打
`POST /internal/api/ai/inbound` 把來訊快照寫進 `crm_ai_inbound_queue`。
AI 決策由這裡在背景下跑：

    pending ──認領──▶ processing ──跑四層引擎──▶ done
                          │                       └─ 命中：建 `crm_outbound_task`
                          │                          （Connector 認領 → 真的送出）
                          └─ 崩掉：逾時回收 → pending（還有額度）／ failed

為什麼是**單一序列**：同一個客戶的訊息必須按到達順序處理，「冷卻中不再回」
與「今日已達上限」這兩個防護才有意義（並行會讓兩則訊息同時通過判斷）。

冪等有兩道鎖，所以「認領後 worker 掛掉 → 重啟重跑同一筆」是安全的：
  1. 隊列表 `UNIQUE(message_id)` —— 同一則訊息不會被入隊兩次；
  2. `autoreply_log.claim_key` 唯一索引 —— 真的處理過就不會再回一次。

失敗只記 log，絕不讓一筆壞資料弄死整個迴圈。
"""
from __future__ import annotations

import asyncio
import logging

from app.autoreply import autoreply
from app.internal import repo

log = logging.getLogger("crm")

# 從快照裡找文字：不同渠道的原始欄位名不一（webhook 正規化後統一叫 text）
_TEXT_KEYS = ("text", "caption", "body")


def _inbound_text(content: dict) -> str:
    """取來訊文字。非文字訊息（純圖 / 語音 / 貼圖）回空字串 → 引擎層直接交人工。"""
    for k in _TEXT_KEYS:
        v = content.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


async def _process_one(row: dict, flat: dict) -> str:
    """跑一筆。回傳處理結果分類（hit/miss/skipped/failed/retry/done）。"""
    qid = int(row["id"])
    platform = str(row["platform"] or "")
    # ⚠️ jsonb 若曾被寫成純量（舊系統踩過），這裡一律防守成 {}
    content = row.get("content")
    if not isinstance(content, dict):
        content = {}
    scope = str(content.get("scope") or "dm").strip() or "dm"

    try:
        res = await autoreply.handle_inbound(
            business_id=int(row["business_id"]),
            channel=platform,
            scope=scope,
            text=_inbound_text(content),
            platform_account_id=int(row["business_platform_account_id"] or 0),
            to=str(row["end_user_sender_id"] or ""),
            message_id=str(row["message_id"] or ""),
            comment_id=str(content.get("comment_id") or ""),
            post_id=str(content.get("post_id") or ""),
            post_config_id=int(content.get("post_config_id") or 0),
            conversation_key=str(content.get("conversation_key") or ""),
            sender_name=str(content.get("sender_name") or ""),
        )
    except Exception as e:  # noqa: BLE001 —— 一筆失敗不能中斷整條佇列
        # handle_inbound 內部已吞掉引擎 / 業務例外；走到這裡通常是更底層的故障
        log.warning("AI 佇列處理失敗 id=%s（%s/%s）：%s", qid, platform, scope, e)
        retry = int(row.get("retry_count") or 0)
        max_retry = int(flat.get("ai.queue_max_retry") or 2)
        if retry <= max_retry:
            await asyncio.to_thread(repo.requeue_ai_inbound, qid, str(e)[:300])
            return "retry"
        await asyncio.to_thread(repo.finish_ai_inbound, qid, "failed", str(e)[:300])
        return "failed"

    state = str(res.get("state") or "")
    reason = str(res.get("reason") or res.get("detail") or "")
    # 引擎「命中但建任務失敗」→ 這筆要留 failed 給運維看；其餘（含未命中）都算處理完成
    status = "failed" if state == autoreply.STATE_FAILED else "done"
    await asyncio.to_thread(repo.finish_ai_inbound, qid, status, reason)

    if state == autoreply.STATE_HIT:
        log.info("AI 佇列命中 id=%s（%s/%s，%s，task=%s）",
                 qid, platform, scope, res.get("label") or res.get("tier"), res.get("task_id"))
    elif state == autoreply.STATE_MISS:
        log.info("AI 佇列未命中 id=%s（%s/%s）：%s", qid, platform, scope, reason)
    return state or status


async def process_once(limit: int = 0) -> dict:
    """跑一輪：認領一批 → 逐筆處理（串行，保證順序）。回傳統計供日誌／自檢。"""
    flat, _ = await asyncio.to_thread(repo.ops_config)
    n = int(limit) if limit else int(flat.get("ai.claim_batch") or 5)
    rows = await asyncio.to_thread(repo.claim_ai_inbound, n)

    stats = {"claimed": len(rows), "hit": 0, "miss": 0, "skipped": 0,
             "failed": 0, "retry": 0, "done": 0}
    for row in rows:
        outcome = await _process_one(row, flat)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def reap_stuck() -> dict:
    """回收卡住的認領項（worker 崩掉時）。由 sweep 迴圈低頻呼叫。"""
    flat, _ = repo.ops_config()
    res = repo.reap_stuck_ai_inbound(
        int(flat.get("ai.queue_stuck_timeout") or 600),
        int(flat.get("ai.queue_max_retry") or 2),
    )
    if res["requeued"] or res["failed"]:
        log.warning("AI 佇列回收卡住項：重排 %s 筆、標失敗 %s 筆",
                    len(res["requeued"]), len(res["failed"]))
    return res
