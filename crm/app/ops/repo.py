"""運維（ops）資料層 —— 失敗任務、重試參數、手動重跑、告警、審計。

★ 這裡只碰 **CRM DB**（`crm_outbound_task` / `crm_ai_inbound_queue` / `crm_alert` /
  `crm_ops_config` / `crm_audit_log`）。發送嘗試紀錄在 Connector 庫 → 走內網 API。
★ RBAC 由 router 負責（`require_ops`），這裡不做權限判斷。
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2.extras

from app.core import db
from app.core import schema
from app.internal import repo as internal_repo

# 任務狀態機的合法值（`crm_outbound_task.status` 的 CHECK）
TASK_STATUSES = ("pending", "processing", "sent", "failed", "uncertain")
# 運維頁預設只看這兩種（真正需要人處理的）
DEFAULT_FAIL_STATUSES = ("failed", "uncertain")

# ops 身分在審計頁看得到的操作類型（admin 不受限）
# 運維端「預設看得到」的類型 —— **唯一真相已移到 `router.AUDIT_GROUPS`**
# （2026-10-20 重新設計：按「你會問什麼」分組，而不是 4 個孤零零的原始類型）。
# 這裡保留這個名字只為相容舊自檢；新程式請用 `AUDIT_GROUPS` 的聯集。
OPS_AUDIT_TYPES = ("task_retry", "ops_config_change", "alert_ack", "alert_notify")


# 可寫入的運維參數白名單 —— **唯一真相在 schema.OPS_CONFIG_DEFAULTS**
CONFIG_KEYS = frozenset(schema.OPS_CONFIG_DEFAULTS.keys())

SEVERITIES = ("info", "warning", "critical")
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


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


def _rows(cur) -> list[dict]:
    """逐列轉 dict 並容錯 —— 單列壞掉不該讓整個清單 500。"""
    out: list[dict] = []
    for r in cur.fetchall():
        try:
            out.append(dict(r))
        except Exception:  # noqa: BLE001
            continue
    return out


def severity_rank(s: str) -> int:
    return _SEVERITY_RANK.get(str(s or "").lower(), 1)


# ================================================================ 概覽

def task_counts(business_id: int = 0) -> dict:
    """各狀態任務數（未來 pending/processing 一眼看出積壓）。"""
    sql = "SELECT status, count(*) AS n FROM crm_outbound_task"
    args: list = []
    if business_id:
        sql += " WHERE business_id = %s"
        args.append(int(business_id))
    sql += " GROUP BY status"
    out = {s: 0 for s in TASK_STATUSES}
    with ro() as cur:
        cur.execute(sql, tuple(args) or None)
        for r in _rows(cur):
            if r.get("status") in out:
                out[r["status"]] = int(r["n"])
    return out


def ai_queue_counts() -> dict:
    out = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    with ro() as cur:
        cur.execute("SELECT status, count(*) AS n FROM crm_ai_inbound_queue GROUP BY status")
        for r in _rows(cur):
            if r.get("status") in out:
                out[r["status"]] = int(r["n"])
    return out


def alert_counts(only_open: bool = True) -> dict:
    sql = "SELECT severity, count(*) AS n FROM crm_alert"
    if only_open:
        sql += " WHERE acknowledged = false"
    sql += " GROUP BY severity"
    out = {s: 0 for s in SEVERITIES}
    with ro() as cur:
        cur.execute(sql)
        for r in _rows(cur):
            key = str(r.get("severity") or "warning").lower()
            out[key] = out.get(key, 0) + int(r["n"])
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return out


def throughput_24h() -> dict:
    """近 24h 的發送產出（sent / failed），運維頁的「今天怎麼樣」。"""
    with ro() as cur:
        cur.execute(
            "SELECT"
            "  count(*) FILTER (WHERE status='sent') AS sent,"
            "  count(*) FILTER (WHERE status='failed') AS failed,"
            "  count(*) FILTER (WHERE status='uncertain') AS uncertain"
            " FROM crm_outbound_task WHERE COALESCE(finished_at, created_at) > now() - interval '24 hours'"
        )
        row = _rows(cur)
        return dict(row[0]) if row else {"sent": 0, "failed": 0, "uncertain": 0}


def dashboard(business_id: int = 0) -> dict:
    return {
        "tasks": task_counts(business_id),
        "ai_queue": ai_queue_counts(),
        "alerts": alert_counts(only_open=True),
        "last24h": throughput_24h(),
    }


# ================================================================ 失敗任務

def _task_sql(*, statuses, business_id: int = 0, platform: str = "",
              keyword: str = "", count: bool = False) -> tuple[str, list]:
    base = ("SELECT {cols} FROM crm_outbound_task t"
            " LEFT JOIN crm_business b ON b.id = t.business_id"
            " LEFT JOIN crm_business_platform_account a ON a.id = t.business_platform_account_id")
    cols = ("count(*) AS n" if count else
            "t.id, t.business_id, b.business_name, t.source, t.platform,"
            " t.business_platform_account_id, a.display_name AS account_name,"
            " a.platform_business_account_id AS account_native_id,"
            " t.end_user_sender_id, t.message_content, t.status, t.is_temp_error,"
            " t.retry_count, t.max_retry, t.backoff_seconds, t.fail_reason,"
            " t.processing_at, t.created_at, t.next_retry_at, t.finished_at,"
            " COALESCE(t.finished_at, t.processing_at, t.created_at) AS sort_at")
    where: list[str] = []
    args: list = []
    sts = [s for s in (statuses or DEFAULT_FAIL_STATUSES) if s in TASK_STATUSES]
    if sts:
        where.append("t.status = ANY(%s)")
        args.append(sts)
    if business_id:
        where.append("t.business_id = %s")
        args.append(int(business_id))
    if platform:
        where.append("t.platform = %s")
        args.append(platform)
    if keyword:
        where.append("(t.end_user_sender_id ILIKE %s OR COALESCE(t.fail_reason,'') ILIKE %s"
                     " OR COALESCE(b.business_name,'') ILIKE %s)")
        kw = f"%{keyword}%"
        args += [kw, kw, kw]
    sql = base.format(cols=cols)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, args


def list_tasks(*, statuses=None, business_id: int = 0, platform: str = "",
               keyword: str = "", limit: int = 50, offset: int = 0) -> dict:
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    sql, args = _task_sql(statuses=statuses, business_id=business_id, platform=platform,
                          keyword=keyword)
    csql, cargs = _task_sql(statuses=statuses, business_id=business_id, platform=platform,
                            keyword=keyword, count=True)
    with ro() as cur:
        cur.execute(csql, tuple(cargs) or None)
        total = int((_rows(cur) or [{"n": 0}])[0]["n"])
        cur.execute(sql + " ORDER BY sort_at DESC NULLS LAST, t.id DESC LIMIT %s OFFSET %s",
                    tuple(args) + (lim, off))
        items = _rows(cur)
    for it in items:
        for k in ("created_at", "processing_at", "next_retry_at", "finished_at", "sort_at"):
            v = it.get(k)
            if v is not None and hasattr(v, "isoformat"):
                it[k] = v.isoformat()
    return {"items": items, "total": total, "limit": lim, "offset": off}


def get_task(task_id: int) -> dict | None:
    with ro() as cur:
        cur.execute(
            "SELECT t.*, b.business_name, a.display_name AS account_name,"
            "       a.platform_business_account_id AS account_native_id"
            "  FROM crm_outbound_task t"
            "  LEFT JOIN crm_business b ON b.id = t.business_id"
            "  LEFT JOIN crm_business_platform_account a ON a.id = t.business_platform_account_id"
            " WHERE t.id = %s", (int(task_id),))
        rows = _rows(cur)
    if not rows:
        return None
    it = rows[0]
    for k in ("created_at", "processing_at", "next_retry_at", "finished_at"):
        v = it.get(k)
        if v is not None and hasattr(v, "isoformat"):
            it[k] = v.isoformat()
    return it


def retry_task(task_id: int, *, reset_retry_count: bool = True) -> dict | None:
    """★ 手動重跑：把 `failed` / `uncertain` 打回 `pending` 讓 Connector 重新認領。

    `uncertain` 也可以重跑 —— 那正是這個狀態存在的意義（可能已發出，需要人判斷）。
    `sent` / `processing` 不准重跑：前者已完成，後者可能正在發（重跑 = 重複發送）。
    回傳新狀態；任務不存在或狀態不允許 → None / {'error': ...}
    """
    with tx() as cur:
        cur.execute("SELECT status FROM crm_outbound_task WHERE id=%s FOR UPDATE",
                    (int(task_id),))
        rows = _rows(cur)
        if not rows:
            return None
        cur_status = rows[0]["status"]
        if cur_status not in ("failed", "uncertain"):
            return {"error": f"目前狀態為 {cur_status}，只有 failed / uncertain 可以重跑"}
        cur.execute(
            "UPDATE crm_outbound_task SET status='pending',"
            " retry_count = CASE WHEN %s THEN 0 ELSE COALESCE(retry_count,0) END,"
            " next_retry_at = NULL, processing_at = NULL, finished_at = NULL,"
            " fail_reason = NULL, is_temp_error = NULL"
            " WHERE id = %s RETURNING id, status",
            (bool(reset_retry_count), int(task_id)),
        )
        out = dict(cur.fetchone())
    return out


def retry_tasks(task_ids: list[int]) -> dict:
    """批次重跑 —— 逐筆嘗試，回報成功/失敗清單（不因一筆失敗就整批放棄）。"""
    ok, bad = [], []
    for tid in (task_ids or [])[:200]:
        try:
            res = retry_task(int(tid))
            if res and res.get("status") == "pending":
                ok.append(int(tid))
            else:
                bad.append({"id": int(tid), "reason": (res or {}).get("error", "任務不存在")})
        except Exception as e:  # noqa: BLE001
            bad.append({"id": int(tid), "reason": str(e)[:200]})
    return {"retried": ok, "skipped": bad}


# ================================================================ 運維參數

def ops_config() -> tuple[dict, dict]:
    """(扁平, 巢狀) —— 直接沿用內網層的實作，避免兩套預設值打架。"""
    return internal_repo.ops_config()


def config_type_of(key: str) -> str:
    """依 `OPS_CONFIG_DEFAULTS` 的預設值型別推斷合法型別（bool 要在 int 之前判）。"""
    d = schema.OPS_CONFIG_DEFAULTS.get(key)
    if isinstance(d, bool):
        return "bool"
    if isinstance(d, int):
        return "int"
    if isinstance(d, float):
        return "float"
    if isinstance(d, list):
        return "list"
    return "text"


def coerce_config(key: str, value):
    """把 UI 傳來的值轉成型別；不合法回 `(None, 錯誤訊息)`。"""
    t = config_type_of(key)
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on"), None
            return bool(value), None
        if t == "int":
            return int(value), None
        if t == "float":
            return float(value), None
        if t == "list":
            if isinstance(value, list):
                return list(value), None
            s = str(value or "").strip()
            return ([x.strip() for x in s.replace(";", ",").split(",") if x.strip()]
                    if s else []), None
        return str(value or ""), None
    except (TypeError, ValueError):
        return None, f"{key} 型別不正確（預期 {t}）"


def set_config(key: str, value) -> tuple[bool, str | None]:
    if key not in CONFIG_KEYS:
        return False, f"不認識的運維參數：{key}"
    val, err = coerce_config(key, value)
    if err:
        return False, err
    internal_repo.set_ops_config(key, val)
    return True, None


# ================================================================ 告警

def list_alerts(*, only_open: bool = True, severity: str = "", limit: int = 100,
                offset: int = 0) -> dict:
    where: list[str] = []
    args: list = []
    if only_open:
        where.append("acknowledged = false")
    if severity:
        where.append("severity = %s")
        args.append(severity)
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit or 100), 500))
    with ro() as cur:
        cur.execute(f"SELECT count(*) AS n FROM crm_alert{tail}", tuple(args) or None)
        total = int((_rows(cur) or [{"n": 0}])[0]["n"])
        cur.execute(
            "SELECT id, alert_type, severity, title, detail, first_seen_at, last_seen_at,"
            "       count, acknowledged, acknowledged_by, acknowledged_at, notified_at"
            f" FROM crm_alert{tail}"
            " ORDER BY acknowledged ASC, last_seen_at DESC LIMIT %s OFFSET %s",
            tuple(args) + (lim, max(0, int(offset or 0))),
        )
        items = _rows(cur)
    for it in items:
        for k in ("first_seen_at", "last_seen_at", "acknowledged_at", "notified_at"):
            v = it.get(k)
            if v is not None and hasattr(v, "isoformat"):
                it[k] = v.isoformat()
    return {"items": items, "total": total, "limit": lim}


def ack_alert(alert_id: int, staff_id: int | None) -> bool:
    with tx() as cur:
        cur.execute(
            "UPDATE crm_alert SET acknowledged = true, acknowledged_by = %s, acknowledged_at = now()"
            " WHERE id = %s AND acknowledged = false",
            (staff_id, int(alert_id)),
        )
        return cur.rowcount > 0


def ack_all_alerts(staff_id: int | None) -> int:
    with tx() as cur:
        cur.execute(
            "UPDATE crm_alert SET acknowledged = true, acknowledged_by = %s, acknowledged_at = now()"
            " WHERE acknowledged = false",
            (staff_id,),
        )
        return cur.rowcount


def open_alerts_for_email(min_severity: str = "warning", limit: int = 50) -> list[dict]:
    """待通知的未確認告警（`notified_at IS NULL`），且嚴重度達門檻。"""
    rank = severity_rank(min_severity)
    with ro() as cur:
        cur.execute(
            "SELECT id, alert_type, severity, title, detail, first_seen_at, last_seen_at, count"
            "  FROM crm_alert WHERE acknowledged = false AND notified_at IS NULL"
            " ORDER BY last_seen_at DESC LIMIT %s", (int(limit),))
        items = _rows(cur)
    out = []
    for it in items:
        if severity_rank(it.get("severity")) >= rank:
            out.append(it)
    return out


def mark_alerts_notified(ids: list[int]) -> int:
    if not ids:
        return 0
    with tx() as cur:
        cur.execute("UPDATE crm_alert SET notified_at = now() WHERE id = ANY(%s)",
                    ([int(i) for i in ids],))
        return cur.rowcount


def recent_failed_count(window_minutes: int, business_id: int = 0) -> int:
    """近 `window_minutes` 分鐘內失敗的任務數（大量失敗告警用）。

    ⚠️ `make_interval(mins => …)` 的參數是 **int** —— 送 float 會直接
      `function make_interval(mins => numeric) does not exist`（「秒」那個參數才是 double）。
    """
    sql = ("SELECT count(*) AS n FROM crm_outbound_task"
           " WHERE status = 'failed'"
           "   AND COALESCE(finished_at, created_at) > now() - make_interval(mins => %s)")
    args: list = [max(1, int(window_minutes))]
    if business_id:
        sql += " AND business_id = %s"
        args.append(int(business_id))
    with ro() as cur:
        cur.execute(sql, tuple(args))
        return int((_rows(cur) or [{"n": 0}])[0]["n"])


# ================================================================ 審計

def audit_type_counts(*, business_id: int = 0, time_from=None, time_to=None,
                      types=None) -> dict[str, int]:
    """當前時間範圍內「每個類型各有幾筆」。給前端 chip 顯示數字用。

    ⚠️ 用 `GROUP BY operate_type` 一次查完，不要每個類型各查一次（27 次往返）。
    """
    where: list[str] = []
    args: list = []
    if business_id:
        where.append("business_id = %s")
        args.append(int(business_id))
    if time_from:
        where.append("operate_at >= %s::timestamptz")
        args.append(time_from)
    if time_to:
        where.append("operate_at < %s::timestamptz")
        args.append(time_to)
    if types:
        where.append("operate_type = ANY(%s)")
        args.append(list(types))
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    with ro() as cur:
        cur.execute("SELECT operate_type, count(*) AS n FROM crm_audit_log"
                    f"{tail} GROUP BY operate_type", tuple(args) or None)
        return {str(r["operate_type"]): int(r["n"]) for r in _rows(cur)}


def list_audit(*, limit: int = 50, offset: int = 0, types=None,
               business_id: int = 0, time_from=None, time_to=None,
               q: str = "", only_failed: bool = False) -> dict:
    """審計列表（篩選 + 分頁）。回 `{items, total, limit, offset, has_more}`。

    ★ 重新設計（2026-10-20 小jo）：
      · `time_from` / `time_to`：時間區間（ISO 字串，含頭不含尾）
      · `types`：多選類型（空 = 不限）
      · `q`：模糊搜尋 —— 操作人 / 商家 id / 有效身分 / 失敗原因 / payload 全文
        （payload 用 `::text ILIKE`，能搜到「#536」「122116181475…」這種具體 id）
      · `only_failed`：只看失敗（`fail_reason IS NOT NULL`）
    ⚠️ 一律 `ORDER BY id DESC`（等於時間新→舊，且 id 是主鍵，比 operate_at 更穩、
      分頁也不會因為同時寫入而重複／漏行 —— operate_at 可能相同）。
    """
    where: list[str] = []
    args: list = []
    if types:
        where.append("operate_type = ANY(%s)")
        args.append(list(types))
    if business_id:
        where.append("business_id = %s")
        args.append(int(business_id))
    if time_from:
        where.append("operate_at >= %s::timestamptz")
        args.append(time_from)
    if time_to:
        where.append("operate_at < %s::timestamptz")
        args.append(time_to)
    if only_failed:
        # ★ 三種「失敗」都要算進來：
        #   ① 新記錄的 `fail_reason`；② 登入失敗（本來就是一種結果，不必再帶原因）；
        #   ③ 舊資料把錯誤塞在 payload（`errors` 非空 / `sent=false`）——
        #      不加這條，「只看失敗」對加欄位之前的歷史資料會永遠是 0 筆。
        where.append(
            "(fail_reason IS NOT NULL OR operate_type = 'login_failed'"
            " OR (payload ? 'errors' AND jsonb_array_length("
            "      CASE WHEN jsonb_typeof(payload->'errors') = 'array'"
            "           THEN payload->'errors' ELSE '[]'::jsonb END) > 0)"
            " OR payload->>'sent' = 'false')")
    if q:
        like = "%" + str(q).strip() + "%"
        where.append(
            "(COALESCE(staff_name, '') ILIKE %s OR COALESCE(staff_role, '') ILIKE %s"
            " OR COALESCE(fail_reason, '') ILIKE %s OR COALESCE(payload::text, '') ILIKE %s"
            " OR CAST(COALESCE(business_id, 0) AS text) LIKE %s"
            " OR COALESCE(operate_type, '') ILIKE %s)")
        args.extend([like] * 6)
    tail = (" WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    with ro() as cur:
        cur.execute(f"SELECT count(*) AS n FROM crm_audit_log{tail}", tuple(args) or None)
        total = int((_rows(cur) or [{"n": 0}])[0]["n"])
        cur.execute(
            "SELECT id, business_id, staff_id, staff_name, staff_role, staff_roles,"
            "       operate_type, platform, business_platform_account_id,"
            "       end_user_sender_id, payload, fail_reason, operate_at"
            f" FROM crm_audit_log{tail} ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(args) + (lim, off),
        )
        items = _rows(cur)
    for it in items:
        v = it.get("operate_at")
        if v is not None and hasattr(v, "isoformat"):
            it["operate_at"] = v.isoformat()
    return {"items": items, "total": total, "limit": lim, "offset": off,
            "has_more": off + len(items) < total}
