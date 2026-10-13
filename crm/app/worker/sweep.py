"""逾時清理 —— 設計案 §11.4 的三種情境。

`processing` 超過 `outbound.processing_timeout` 秒還沒收到回調時，依**發送紀錄**判斷：

| 情境 | 處理 |
|---|---|
| 沒有 attempt 紀錄 | 確定沒發出 → 退回 `pending` 重新排隊 |
| 部分子項成功 | 可能已發出 → `uncertain`（等人工確認） |
| 全部子項成功但沒回調 | 其實成功了 → `sent` |
| **取不到紀錄**（Connector 掛/逾時） | 保守 → `uncertain`（**寧可人工確認，也不冒重複發送的風險**） |
"""
from __future__ import annotations

import logging

from app.internal import connector_client
from app.internal import repo

log = logging.getLogger("crm")


def expected_sub_count(message_content) -> int:
    """這筆任務預期會發幾個子項：文字 1 個 + 每個附件 1 個。"""
    mc = message_content or {}
    if not isinstance(mc, dict):
        return 0
    n = 0
    if str(mc.get("text") or "").strip():
        n += 1
    assets = mc.get("assets") or []
    if isinstance(assets, list):
        n += len(assets)
    return n


def decide(attempts: list[dict] | None, expected: int) -> tuple[str, str | None]:
    """回傳 (decision, reason)；decision ∈ {'pending','uncertain','sent'}。"""
    if attempts is None:
        return "uncertain", "逾時且無法取得發送紀錄（Connector 未回應）"
    if not attempts:
        return "pending", None
    sent = {a.get("sub_seq") for a in attempts if a.get("status") == "sent"}
    failed = {a.get("sub_seq") for a in attempts if a.get("status") == "failed"}
    if expected and len(sent) >= expected and not failed:
        return "sent", "逾時但發送紀錄顯示全部成功（補記為已發送）"
    return "uncertain", (
        f"逾時且發送紀錄不完整（成功 {len(sent)}/{expected or '?'}、失敗 {len(failed)}）"
    )


def sweep_once(limit: int = 100) -> dict:
    """跑一輪逾時清理。回傳統計（供日誌與自檢斷言）。"""
    flat, _ = repo.ops_config()
    timeout = int(flat.get("outbound.processing_timeout") or 300)

    rows = repo.find_timed_out(timeout, limit)
    stats = {"checked": len(rows), "requeued": 0, "uncertain": 0, "sent": 0}

    for r in rows:
        attempts = connector_client.fetch_attempts(r["id"])
        decision, reason = decide(attempts, expected_sub_count(r["message_content"]))

        if decision == "pending":
            if repo.requeue_processing(r["id"]):
                stats["requeued"] += 1
        elif decision == "sent":
            if repo.mark_sent_recovered(r["id"], reason or "逾時補記"):
                stats["sent"] += 1
        else:
            if repo.mark_uncertain(r["id"], reason or "逾時"):
                stats["uncertain"] += 1

    if rows:
        log.info("逾時清理：%s", stats)
    return stats


def sweep_sessions_once() -> int:
    n = repo.purge_expired_sessions()
    if n:
        log.info("清理過期 session：%s 筆", n)
    return n
