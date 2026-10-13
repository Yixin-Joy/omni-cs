"""回調重試佇列 —— Connector 把結果回報 CRM 失敗時，落本地排隊重試（設計案 §6.3 / §11.2）。

退避：1s → 5s → 30s → 2min → 5min（上限 5 次）。
"""
from __future__ import annotations

import json
import logging

from app.core import db
from app.worker import client

log = logging.getLogger("connector")

BACKOFF = (1, 5, 30, 120, 300)


def enqueue(task_id: int, status: str, fail_reason: str | None = None,
            is_temp_error: bool | None = None) -> None:
    result = {"status": status, "fail_reason": fail_reason, "is_temp_error": is_temp_error}
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connector_callback_queue (crm_task_id, result)"
                " VALUES (%s, %s::jsonb)"
                " ON CONFLICT (crm_task_id) DO UPDATE SET result = EXCLUDED.result,"
                "  status = 'pending', retry_count = 0, next_retry_at = now()",
                (int(task_id), json.dumps(result, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def _due(limit: int) -> list[dict]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, crm_task_id, result, retry_count, max_retry"
                "  FROM connector_callback_queue"
                " WHERE status = 'pending' AND next_retry_at <= now()"
                " ORDER BY next_retry_at LIMIT %s",
                (int(limit),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "crm_task_id": r[1], "result": r[2] or {},
             "retry_count": r[3] or 0, "max_retry": r[4] or 5} for r in rows]


def _mark(row_id: int, status: str, retry_count: int, next_retry_at_sql: str | None = None) -> None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if next_retry_at_sql:
                cur.execute(
                    f"UPDATE connector_callback_queue SET retry_count = %s,"
                    f" next_retry_at = {next_retry_at_sql} WHERE id = %s",
                    (int(retry_count), int(row_id)),
                )
            else:
                cur.execute(
                    "UPDATE connector_callback_queue SET status = %s, retry_count = %s WHERE id = %s",
                    (status, int(retry_count), int(row_id)),
                )
        conn.commit()
    finally:
        conn.close()


async def flush(limit: int = 50) -> dict:
    """把該重試的回調再送一次。回傳統計。"""
    rows = _due(limit)
    stats = {"due": len(rows), "done": 0, "retry": 0, "gave_up": 0}
    for r in rows:
        res = r["result"] or {}
        try:
            await client.update_task_status(
                r["crm_task_id"], res.get("status") or "failed",
                res.get("fail_reason"), res.get("is_temp_error"),
            )
            _mark(r["id"], "done", r["retry_count"])
            stats["done"] += 1
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("回調重試失敗 task=%s: %s", r["crm_task_id"], e)

        rc = r["retry_count"] + 1
        if rc >= r["max_retry"]:
            _mark(r["id"], "failed", rc)
            stats["gave_up"] += 1
        else:
            secs = BACKOFF[min(rc - 1, len(BACKOFF) - 1)]
            _mark(r["id"], "pending", rc, f"now() + make_interval(secs => {float(secs)})")
            stats["retry"] += 1
    return stats
