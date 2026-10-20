"""內網 API 的資料存取層 —— 全部走 CRM DB（唯一寫入者）。

包含：原子認領（含每商家公平限流）、狀態回調（含臨時錯誤自動重排）、
運維參數、帳號映射、AI 入站入隊、告警 upsert、逾時清理候選查詢。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg2.extras

from app.core import db
from app.core import schema


@contextmanager
def tx():
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def ro():
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


# ================================================================ 運維參數

def ops_config() -> tuple[dict, dict]:
    """回傳 (扁平, 巢狀)。巢狀依 `a.b` 的點號分組（設計案 §4.1）。"""
    flat = dict(schema.OPS_CONFIG_DEFAULTS)
    with ro() as cur:
        cur.execute("SELECT config_key, config_value FROM crm_ops_config")
        for r in cur.fetchall():
            flat[r["config_key"]] = r["config_value"]

    nested: dict = {}
    for k, v in flat.items():
        head, _, tail = k.partition(".")
        if tail:
            nested.setdefault(head, {})[tail] = v
        else:
            nested[k] = v
    return flat, nested


def ops_get(key: str, default=None):
    flat, _ = ops_config()
    return flat.get(key, default)


def set_ops_config(key: str, value) -> None:
    with tx() as cur:
        cur.execute(
            "INSERT INTO crm_ops_config (config_key, config_value, updated_at)"
            " VALUES (%s, %s::jsonb, now())"
            " ON CONFLICT (config_key) DO UPDATE SET config_value = EXCLUDED.config_value,"
            " updated_at = now()",
            (key, json.dumps(value)),
        )


# ================================================================ 帳號映射

def account_map(since=None) -> list[dict]:
    """對齊鍵 = (platform, platform_business_account_id)。真相在 CRM（MIGRATION_PLAN §1）。"""
    sql = (
        "SELECT a.platform, a.platform_business_account_id, a.business_id,"
        "       a.id AS bp_account_id, a.display_name, a.credential,"
        "       (COALESCE(a.enabled, true) AND COALESCE(b.enabled, true)) AS enabled,"
        "       COALESCE(a.updated_at, a.created_at) AS updated_at"
        "  FROM crm_business_platform_account a"
        "  JOIN crm_business b ON b.id = a.business_id"
    )
    params: list = []
    if since:
        sql += " WHERE COALESCE(a.updated_at, a.created_at) > %s"
        params.append(since)
    sql += " ORDER BY a.id"
    with ro() as cur:
        cur.execute(sql, params or None)
        out = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("updated_at") is not None:
                d["updated_at"] = d["updated_at"].isoformat()
            out.append(d)
        return out


# ================================================================ 任務認領

# ★ 原子認領：`FOR UPDATE SKIP LOCKED` 保證多份 worker 不重派（已實測，見 MIGRATION_PLAN §9.4）。
#   每商家公平限流用視窗函式在第二個 CTE 做 —— 注意 FOR UPDATE 不可與視窗函式同層，
#   所以先在 `locked`（無視窗函式）鎖住候選列，再於 `picked` 分組取前 K 筆。
_CLAIM_SQL = """
WITH locked AS (
    SELECT t.id, t.business_id, t.created_at
      FROM crm_outbound_task t
     WHERE t.status = 'pending'
       AND (t.next_retry_at IS NULL OR t.next_retry_at <= now())
     ORDER BY t.created_at, t.id
     LIMIT %(scan)s
     FOR UPDATE SKIP LOCKED
),
picked AS (
    SELECT id FROM (
        SELECT id, row_number() OVER (PARTITION BY business_id ORDER BY created_at, id) AS rn
          FROM locked
    ) s
    WHERE s.rn <= %(per_biz)s
    LIMIT %(limit)s
)
UPDATE crm_outbound_task t
   SET status = 'processing', processing_at = now()
  FROM picked p
 WHERE t.id = p.id
RETURNING t.id, t.business_id, t.platform, t.business_platform_account_id,
          t.end_user_sender_id, t.message_content, t.retry_count, t.max_retry,
          t.backoff_seconds, t.source
"""


def claim_tasks(limit: int = 50, per_business_limit: int = 10) -> list[dict]:
    limit = max(1, min(int(limit or 50), 500))
    per_biz = max(1, int(per_business_limit or 10))
    scan = max(limit * per_biz, limit)
    scan = min(scan, 2000)

    with tx() as cur:
        cur.execute(_CLAIM_SQL, {"scan": scan, "per_biz": per_biz, "limit": limit})
        rows = [dict(r) for r in cur.fetchall()]
        bp_ids = [r["business_platform_account_id"] for r in rows
                  if r["business_platform_account_id"]]
        native = {}
        if bp_ids:
            cur.execute(
                "SELECT id, platform_business_account_id FROM crm_business_platform_account"
                " WHERE id = ANY(%s)",
                (bp_ids,),
            )
            native = {r["id"]: r["platform_business_account_id"] for r in cur.fetchall()}

    out = []
    for r in rows:
        out.append({
            "task_id": r["id"],
            "business_id": r["business_id"],
            "platform": r["platform"],
            "bp_account_id": r["business_platform_account_id"],
            "platform_business_account_id": native.get(r["business_platform_account_id"], ""),
            "end_user_sender_id": r["end_user_sender_id"],
            "message_content": r["message_content"],
            "retry_count": r["retry_count"],
            "max_retry": r["max_retry"],
            "backoff_seconds": r["backoff_seconds"],
            "source": r["source"],
        })
    return out


def get_task(task_id: int) -> dict | None:
    with ro() as cur:
        cur.execute("SELECT * FROM crm_outbound_task WHERE id = %s", (task_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def _sync_log_send_status(cur, task_id: int, status: str) -> int:
    """把**自動回復紀錄**的 `send_status` 跟任務終態對齊；回受影響列數。

    ★ 為什麼需要（2026-10-14 小jo：「記錄中明明已經發出了為麼顯示未送出」）：
      `autoreply_log.send_status` 只在**建立紀錄那一刻**寫一次
      （`not_sent` / `queued` / `failed`），任務真的送成功、`crm_outbound_task`
      變成 `sent` 之後，**沒有任何地方回頭更新它** → 記錄頁永遠停在「未送出」。

    ⚠️ 連帶影響（比顯示更嚴重）：`recent_reply_count()` / `today_reply_count()`
      都用 `send_status='sent'` 判斷，所以**冷卻與每日上限一直是失效的**
      （永遠算 0 則 → AI 可以無視 3 秒冷卻、無視每日 30 則上限連發）。
      回寫補上之後這兩個防護才真的生效。

    ⚠️ 用 `outbound_task_id` 關聯（有 `idx_autoreply_log_task`）；
      人工回覆的任務沒有對應紀錄 → rowcount=0，無害。
    """
    cur.execute(
        "UPDATE autoreply_log SET send_status=%s WHERE outbound_task_id=%s",
        (status, int(task_id)),
    )
    return cur.rowcount


def update_task_status(task_id: int, status: str, fail_reason: str | None = None,
                       is_temp_error: bool | None = None) -> dict | None:
    """Connector 回調任務狀態（設計案 §4.1 / §11）。

    - `sent`      → 終態
    - `uncertain` → 可能已發出，終態（等人工確認）
    - `failed`    → 臨時錯誤且還有重試額度 ⇒ **退回 pending 並排下次重試**；否則終態
    """
    if status not in ("sent", "failed", "uncertain"):
        return {"error": "invalid status"}

    with tx() as cur:
        cur.execute(
            "SELECT id, status, retry_count, max_retry, backoff_seconds"
            "  FROM crm_outbound_task WHERE id = %s FOR UPDATE",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        if status == "sent":
            cur.execute(
                "UPDATE crm_outbound_task SET status='sent', finished_at=now(),"
                " fail_reason=NULL, is_temp_error=NULL, next_retry_at=NULL WHERE id=%s",
                (task_id,),
            )
            # ★ 回寫紀錄（否則記錄頁永遠「未送出」、冷卻／上限永不生效）
            _sync_log_send_status(cur, task_id, "sent")
            return {"task_id": task_id, "status": "sent", "retried": False}

        if status == "uncertain":
            cur.execute(
                "UPDATE crm_outbound_task SET status='uncertain', finished_at=now(),"
                " fail_reason=%s, is_temp_error=true WHERE id=%s",
                (fail_reason, task_id),
            )
            _sync_log_send_status(cur, task_id, "uncertain")
            return {"task_id": task_id, "status": "uncertain", "retried": False}

        # status == 'failed'
        rc = int(row["retry_count"] or 0)
        max_retry = int(row["max_retry"] or 0)
        if is_temp_error and rc < max_retry:
            nrc = rc + 1
            backoff = int(row["backoff_seconds"] or 1) * (2 ** (nrc - 1))
            cur.execute(
                "UPDATE crm_outbound_task SET status='pending', retry_count=%s,"
                " next_retry_at = now() + make_interval(secs => %s),"
                " fail_reason=%s, is_temp_error=true, processing_at=NULL WHERE id=%s",
                (nrc, float(backoff), fail_reason, task_id),
            )
            return {"task_id": task_id, "status": "pending", "retried": True,
                    "retry_count": nrc, "backoff_seconds": backoff}

        cur.execute(
            "UPDATE crm_outbound_task SET status='failed', finished_at=now(),"
            " fail_reason=%s, is_temp_error=%s WHERE id=%s",
            (fail_reason, bool(is_temp_error), task_id),
        )
        # ★ 重試耗盡 → 紀錄標「送出失敗」（不再停在「傳送中」誤導人）
        _sync_log_send_status(cur, task_id, "failed")
        return {"task_id": task_id, "status": "failed", "retried": False}


def find_timed_out(processing_timeout: int, limit: int = 100) -> list[dict]:
    """processing 超時（processing_at 早於 now - timeout）的任務。"""
    with ro() as cur:
        cur.execute(
            "SELECT id, business_id, platform, business_platform_account_id,"
            "       end_user_sender_id, message_content, retry_count, max_retry,"
            "       backoff_seconds, processing_at"
            "  FROM crm_outbound_task"
            " WHERE status = 'processing'"
            "   AND processing_at IS NOT NULL"
            "   AND processing_at < now() - make_interval(secs => %s)"
            " ORDER BY processing_at LIMIT %s",
            (float(processing_timeout), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]


def requeue_processing(task_id: int) -> bool:
    """逾時 + 確定沒發出 → 退回 pending（重排）。"""
    with tx() as cur:
        cur.execute(
            "UPDATE crm_outbound_task SET status='pending', processing_at=NULL"
            " WHERE id=%s AND status='processing'",
            (task_id,),
        )
        return cur.rowcount > 0


def mark_uncertain(task_id: int, reason: str) -> bool:
    with tx() as cur:
        cur.execute(
            "UPDATE crm_outbound_task SET status='uncertain', finished_at=now(),"
            " fail_reason=%s, is_temp_error=true"
            " WHERE id=%s AND status='processing'",
            (reason, task_id),
        )
        ok = cur.rowcount > 0
        if ok:
            _sync_log_send_status(cur, task_id, "uncertain")
        return ok


def mark_sent_recovered(task_id: int, reason: str) -> bool:
    with tx() as cur:
        cur.execute(
            "UPDATE crm_outbound_task SET status='sent', finished_at=now(),"
            " fail_reason=%s, is_temp_error=NULL, next_retry_at=NULL"
            " WHERE id=%s AND status='processing'",
            (reason, task_id),
        )
        ok = cur.rowcount > 0
        # ★ 逾時補記成功也要回寫，否則紀錄頁還是「未送出」
        if ok:
            _sync_log_send_status(cur, task_id, "sent")
        return ok


# ================================================================ AI 入站佇列

def enqueue_ai_inbound(message_id: str, platform: str,
                       platform_business_account_id: str, end_user_sender_id: str,
                       content: dict) -> dict:
    """把來訊快照寫入 `crm_ai_inbound_queue`（冪等：message_id 唯一）。"""
    with tx() as cur:
        cur.execute(
            "SELECT a.id AS bp_account_id, a.business_id, a.platform, b.enabled AS biz_enabled"
            "  FROM crm_business_platform_account a"
            "  JOIN crm_business b ON b.id = a.business_id"
            " WHERE a.platform = %s AND a.platform_business_account_id = %s",
            (platform, platform_business_account_id),
        )
        acc = cur.fetchone()
        if not acc:
            return {"queued": False, "reason": "unregistered_account"}

        cur.execute(
            "INSERT INTO crm_ai_inbound_queue"
            " (message_id, business_id, platform, business_platform_account_id,"
            "  end_user_sender_id, content)"
            " VALUES (%s, %s, %s, %s, %s, %s::jsonb)"
            " ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id, acc["business_id"], acc["platform"], acc["bp_account_id"],
             end_user_sender_id, json.dumps(content, ensure_ascii=False)),
        )
        row = cur.fetchone()
        return {
            "queued": bool(row),
            "id": row["id"] if row else None,
            "business_id": acc["business_id"],
            "bp_account_id": acc["bp_account_id"],
            "duplicate": row is None,
        }


# ================================================================ AI 入站佇列（消費端）

def claim_ai_inbound(limit: int = 1) -> list[dict]:
    """★ 原子認領 `pending` → `processing`（單一序列的取件口）。

    佇列表本身就是佇列（不用 Redis）；`FOR UPDATE SKIP LOCKED` 保證同一筆只會被
    一個 worker 拿到。`retry_count` 在此遞增 —— **認領即記一次嘗試**，所以逾時回收
    時可以直接用它判斷還剩幾次機會。
    """
    n = max(1, int(limit))
    with tx() as cur:
        cur.execute(
            "UPDATE crm_ai_inbound_queue SET status='processing', claimed_at=now(),"
            " retry_count = COALESCE(retry_count, 0) + 1"
            " WHERE id IN (SELECT id FROM crm_ai_inbound_queue"
            "               WHERE status='pending' ORDER BY id LIMIT %s"
            "               FOR UPDATE SKIP LOCKED)"
            " RETURNING id, message_id, business_id, platform,"
            "           business_platform_account_id, end_user_sender_id, content, retry_count",
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]


def finish_ai_inbound(queue_id: int, status: str, reason: str = "") -> bool:
    """結束一筆佇列項（`done` / `failed`）。"""
    if status not in ("done", "failed"):
        raise ValueError("status 只能是 done / failed")
    with tx() as cur:
        cur.execute(
            "UPDATE crm_ai_inbound_queue SET status=%s, fail_reason=%s, processed_at=now()"
            " WHERE id=%s AND status='processing'",
            (status, (reason or None), int(queue_id)),
        )
        return cur.rowcount > 0


def requeue_ai_inbound(queue_id: int, reason: str = "") -> bool:
    """把認領後失敗的項退回 `pending`（下次輪詢重試）。"""
    with tx() as cur:
        cur.execute(
            "UPDATE crm_ai_inbound_queue SET status='pending', claimed_at=NULL, fail_reason=%s"
            " WHERE id=%s AND status='processing'",
            ((reason or None), int(queue_id)),
        )
        return cur.rowcount > 0


def reap_stuck_ai_inbound(timeout: int, max_retry: int) -> dict:
    """回收卡住的 `processing`（worker 崩掉／被砍，來不及回寫狀態）。

    還剩重試額度 → 退回 `pending`；否則標 `failed`（不讓它無限重跑）。
    ⚠️ 舊資料可能沒有 `claimed_at`，所以用 `COALESCE(claimed_at, created_at)` 判斷。
    """
    with tx() as cur:
        cur.execute(
            "UPDATE crm_ai_inbound_queue SET status='pending', claimed_at=NULL,"
            " fail_reason='處理逾時，重新排隊'"
            " WHERE status='processing' AND COALESCE(retry_count, 0) < %s"
            "   AND COALESCE(claimed_at, created_at) < now() - make_interval(secs => %s)"
            " RETURNING id",
            (int(max_retry), int(timeout)),
        )
        requeued = [r["id"] for r in cur.fetchall()]
        cur.execute(
            "UPDATE crm_ai_inbound_queue SET status='failed', processed_at=now(),"
            " fail_reason='處理逾時且已超過重試上限'"
            " WHERE status='processing' AND COALESCE(retry_count, 0) >= %s"
            "   AND COALESCE(claimed_at, created_at) < now() - make_interval(secs => %s)"
            " RETURNING id",
            (int(max_retry), int(timeout)),
        )
        failed = [r["id"] for r in cur.fetchall()]
    return {"requeued": requeued, "failed": failed}


def ai_queue_stats() -> dict:
    """各狀態筆數（供運維頁與自檢斷言）。"""
    with ro() as cur:
        cur.execute("SELECT status, count(*) AS n FROM crm_ai_inbound_queue GROUP BY status")
        return {r["status"]: int(r["n"]) for r in cur.fetchall()}


# ================================================================ 外送任務

def enqueue_outbound_task(*, business_id: int, platform: str, bp_account_id: int,
                          end_user_sender_id: str, message_content: dict,
                          staff_id: int | None = None, source: str = "ai",
                          max_retry: int | None = None,
                          backoff_seconds: int | None = None) -> dict:
    """建立一筆外送任務（CRM → `crm_outbound_task` → Connector 認領送出）。

    `source`：'ai'（引擎命中）或 'manual'（客服人工回覆）。
    退避與重試次數預設讀運維參數（`outbound.max_retry` / `outbound.backoff_seconds`）。
    """
    cfg, _ = ops_config()
    mr = int(max_retry if max_retry is not None else cfg.get("outbound.max_retry", 3))
    bs = int(backoff_seconds if backoff_seconds is not None else cfg.get("outbound.backoff_seconds", 1))
    with tx() as cur:
        cur.execute(
            "INSERT INTO crm_outbound_task"
            " (business_id, staff_id, source, platform, business_platform_account_id,"
            "  end_user_sender_id, message_content, status, max_retry, backoff_seconds)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'pending', %s, %s) RETURNING id",
            (int(business_id), staff_id, source, platform, int(bp_account_id),
             end_user_sender_id, json.dumps(message_content, ensure_ascii=False), mr, bs),
        )
        row = cur.fetchone()
    return {"task_id": row["id"]}


# ================================================================ 告警

def upsert_alert(alert_type: str, severity: str, title: str,
                 detail: dict | None = None, dedupe_key: str | None = None,
                 increment: int = 1) -> int:
    """未確認的同型別同 dedupe_key 告警合併成一條（累計次數、更新最後時間）。"""
    key = dedupe_key or title
    payload = dict(detail or {})
    payload["dedupe_key"] = key
    with tx() as cur:
        cur.execute(
            "SELECT id FROM crm_alert"
            " WHERE alert_type = %s AND acknowledged = false"
            "   AND COALESCE(detail->>'dedupe_key', '') = %s"
            " ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (alert_type, key),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE crm_alert SET last_seen_at = now(), count = count + %s,"
                " severity = %s, title = %s, detail = %s::jsonb WHERE id = %s",
                (int(increment), severity, title, json.dumps(payload, ensure_ascii=False),
                 row["id"]),
            )
            return row["id"]
        cur.execute(
            "INSERT INTO crm_alert (alert_type, severity, title, detail, count)"
            " VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id",
            (alert_type, severity, title, json.dumps(payload, ensure_ascii=False),
             int(increment)),
        )
        return cur.fetchone()["id"]


# ================================================================ Session 清理

def purge_expired_sessions() -> int:
    with tx() as cur:
        cur.execute("DELETE FROM crm_session WHERE expires_at < now()")
        return cur.rowcount
