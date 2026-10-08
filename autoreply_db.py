"""AI 自動回復的資料層：資料表建立 + FAQ / 業務表格 / 開關 / 日誌讀寫。

資料庫固定為 `platform_forwarder`（見 .env 的 DATABASE_URL），不碰其他庫。

資料表：
  autoreply_faq             FAQ 問答對（可個別停啟用）
  autoreply_business_table  業務表格定義（欄位順序）
  autoreply_business_row    業務表格資料列（欄位名 → 值）
  autoreply_setting         開關（全域 / 渠道 / 渠道+場景）
  autoreply_log             每次自動回復的處置紀錄（含未命中）
另有既有的 forward_log / igfb_dm / igfb_comment 三張表加自動回復欄位。
"""
import asyncio
import json
import logging
from urllib.parse import urlsplit, urlunsplit

import psycopg2

import config

log = logging.getLogger("wechat-forwarder")

# ---------- 資料表定義（create_db.py 也呼叫這裡，保持單一真相） ----------

_DDL = """
CREATE TABLE IF NOT EXISTS autoreply_faq (
    id          BIGSERIAL PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    keywords    TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    hit_count   BIGINT NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_business_table (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    columns     JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_business_row (
    id          BIGSERIAL PRIMARY KEY,
    table_id    BIGINT NOT NULL REFERENCES autoreply_business_table(id) ON DELETE CASCADE,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_setting (
    scope_key   TEXT PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_log (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel        VARCHAR(20) NOT NULL,
    reply_scope    VARCHAR(20) NOT NULL,
    tier           VARCHAR(20) NOT NULL,
    detail         TEXT,
    reason         TEXT,
    question       TEXT,
    answer         TEXT,
    conversation_key TEXT,
    message_id     TEXT,
    post_id        TEXT,
    comment_id     TEXT,
    faq_id         BIGINT,
    table_id       BIGINT,
    matched_rows   JSONB,
    score          DOUBLE PRECISION,
    rewrote        BOOLEAN NOT NULL DEFAULT false,
    rewritten_query TEXT,
    history_used   INTEGER NOT NULL DEFAULT 0,
    send_status    VARCHAR(20) NOT NULL DEFAULT 'not_sent',
    -- 一則訊息只處理一次的認領鍵（見下方唯一索引）
    claim_key      TEXT
);

CREATE INDEX IF NOT EXISTS idx_autoreply_log_created ON autoreply_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_channel ON autoreply_log(channel, reply_scope);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_tier ON autoreply_log(tier);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_rewrote ON autoreply_log(rewrote);
CREATE INDEX IF NOT EXISTS idx_autoreply_business_row_table ON autoreply_business_row(table_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_autoreply_faq_enabled ON autoreply_faq(enabled, sort_order);

-- 「一則訊息只處理一次」的原子認領鍵。
-- Meta / 微信 都可能對同一個事件重複投遞，加上聊天模式會併發處理，
-- 用唯一索引 + INSERT ... ON CONFLICT (claim_key) DO NOTHING 保證只有一個處理者能認領成功。
-- 註：必須是「非部分」唯一索引，ON CONFLICT (claim_key) 才認得；
-- PostgreSQL 的唯一索引允許多個 NULL，所以未認領的列不受影響。
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_log_claim
    ON autoreply_log(claim_key);
"""

# 既有表的自動回復欄位（逐條 ALTER，可重複執行）
_COLUMN_MIGRATIONS = (
    ("forward_log", "auto_reply_state", "VARCHAR(20)"),
    ("forward_log", "auto_reply_tier", "VARCHAR(20)"),
    ("forward_log", "auto_reply_detail", "TEXT"),
    ("igfb_dm", "auto_reply_state", "VARCHAR(20)"),
    ("igfb_dm", "auto_reply_tier", "VARCHAR(20)"),
    ("igfb_dm", "auto_reply_detail", "TEXT"),
    ("igfb_comment", "auto_reply_state", "VARCHAR(20)"),
    ("igfb_comment", "auto_reply_tier", "VARCHAR(20)"),
    ("igfb_comment", "auto_reply_detail", "TEXT"),
    # 業務表格 query 改寫的留痕
    ("autoreply_log", "rewritten_query", "TEXT"),
    ("autoreply_log", "history_used", "INTEGER NOT NULL DEFAULT 0"),
    ("autoreply_log", "claim_key", "TEXT"),
)

# 自動回復狀態值
STATE_MISS = "miss"          # 自動回復未命中 → 待人工處理
STATE_HIT = "hit"            # 自動回復已送出
STATE_FAILED = "failed"      # 有命中但發送失敗
STATE_SKIPPED = "skipped"    # 未觸發（開關關閉 / 非文字訊息 / 冷卻中）

# 層級值
TIER_FAQ = "faq"
TIER_TABLE = "table"
TIER_RAG = "rag"
TIER_MISS = "miss"
TIER_ERROR = "error"
# 處理中的佔位層級：認領列在被後續 update_claim 改寫前用它，不會出現在紀錄列表
TIER_CLAIM = "claim"

TIER_LABELS = {TIER_FAQ: "FAQ 命中", TIER_TABLE: "表格命中", TIER_RAG: "知識庫命中"}

# 紀錄頁 / 統計要認得的層級（其他值一律歸到 error）
KNOWN_TIERS = (TIER_FAQ, TIER_TABLE, TIER_RAG, TIER_MISS, TIER_ERROR)


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier or "", tier or "")


# ---------- 連線 ----------

def _connect(dbname: str | None = None):
    url = config.DATABASE_URL
    if dbname:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))
    return psycopg2.connect(url)


def _current_dbname() -> str:
    return urlsplit(config.DATABASE_URL).path.lstrip("/") or "platform_forwarder"


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 沒有參數時不要讓 psycopg2 介入：否則 SQL 裡的 % 會被當成佔位符
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> int:
    """執行 INSERT/UPDATE/DELETE，回傳受影響列數。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def execute_returning(sql: str, params: tuple = ()):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def executemany(sql: str, seq: list[tuple]) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, seq)
        conn.commit()
    finally:
        conn.close()


# ---------- 建表 / 遷移 ----------

def init_schema() -> None:
    """建立自動回復相關資料表並補上既有表的新欄位。可重複執行。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 先補欄位再跑 DDL：DDL 裡有依賴新欄位的索引（例如 claim_key），
            # 對既有資料庫而言欄位得先存在。
            for table, column, coltype in _COLUMN_MIGRATIONS:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
            cur.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


async def init_schema_async() -> None:
    """非同步包裝（服務啟動時用），失敗只記 log 不擋啟動。"""
    try:
        await asyncio.to_thread(init_schema)
        log.info("自動回復資料表就緒（%s）", _current_dbname())
    except Exception as e:
        log.warning("自動回復資料表初始化失敗: %s", e)


# ---------- 開關 ----------

KEY_GLOBAL = "global"


def scope_key(channel: str, scope: str) -> str:
    return f"{channel}:{scope}"


def get_setting(key: str) -> bool | None:
    """回傳該鍵的設定值；沒設定過回 None（可與「設為 false」區分）。"""
    rows = query("SELECT enabled FROM autoreply_setting WHERE scope_key=%s", (key,))
    return bool(rows[0]["enabled"]) if rows else None


def set_setting(key: str, enabled: bool) -> None:
    execute("""
        INSERT INTO autoreply_setting (scope_key, enabled, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (scope_key) DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=now()
    """, (key, bool(enabled)))


def all_settings() -> dict[str, bool]:
    return {r["scope_key"]: bool(r["enabled"])
            for r in query("SELECT scope_key, enabled FROM autoreply_setting")}


def effective_scope_mode(channel: str, scope: str) -> bool:
    """某渠道＋場景是否開啟自動回復。

    判斷優先序（越前面越優先）：渠道+場景 > 渠道 > 全域 > 預設關閉。
    """
    values = all_settings()
    for key in (scope_key(channel, scope), channel, KEY_GLOBAL):
        if key in values:
            return values[key]
    return False


def global_enabled() -> bool:
    return bool(get_setting(KEY_GLOBAL))


def set_global(enabled: bool) -> None:
    set_setting(KEY_GLOBAL, enabled)


def scope_overrides() -> dict[str, bool]:
    """只回傳渠道／渠道+場景層級的覆寫（不含全域）。"""
    return {k: v for k, v in all_settings().items() if k != KEY_GLOBAL}


# ---------- FAQ ----------

def list_faqs(enabled_only: bool = False, q: str = "") -> list[dict]:
    """列出 FAQ。`q` 有值時對 question / answer / keywords 做內文檢索（ILIKE）。

    ⚠️ 一定要參數化。`query(sql, params)` 即使傳空 tuple，psycopg2 仍會做 `%` 解析，
    SQL 裡若出現字面 `%` 會噴 `IndexError: tuple index out of range`
    （這個專案踩過）。所以用 `ILIKE %s` + 值裡自己加 `%`，不要寫字面 `LIKE '%x%'`。
    """
    sql = """
        SELECT id, question, answer, keywords, enabled, hit_count, sort_order,
               created_at, updated_at
        FROM autoreply_faq
    """
    where, params = [], []
    if enabled_only:
        where.append("enabled = true")
    key = (q or "").strip()
    if key:
        where.append("(question ILIKE %s OR answer ILIKE %s OR COALESCE(keywords,'') ILIKE %s)")
        needle = f"%{key}%"
        params += [needle, needle, needle]
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, id"
    return query(sql, tuple(params))


def create_faq(question: str, answer: str, keywords: str = "", enabled: bool = True) -> dict:
    row = execute_returning("""
        INSERT INTO autoreply_faq (question, answer, keywords, enabled, sort_order)
        VALUES (%s, %s, %s, %s, COALESCE((SELECT max(sort_order)+1 FROM autoreply_faq), 0))
        RETURNING id, question, answer, keywords, enabled, hit_count, sort_order
    """, (question, answer, keywords, enabled))
    keys = ("id", "question", "answer", "keywords", "enabled", "hit_count", "sort_order")
    return dict(zip(keys, row))


def update_faq(faq_id: int, fields: dict) -> bool:
    allowed = ("question", "answer", "keywords", "enabled", "sort_order")
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=%s")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(faq_id)
    return execute(f"UPDATE autoreply_faq SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def delete_faq(faq_id: int) -> bool:
    return execute("DELETE FROM autoreply_faq WHERE id=%s", (faq_id,)) > 0


def set_faq_enabled(faq_id: int, enabled: bool) -> bool:
    return update_faq(faq_id, {"enabled": bool(enabled)})


def bump_faq_hit(faq_id: int) -> None:
    execute("UPDATE autoreply_faq SET hit_count = hit_count + 1 WHERE id=%s", (faq_id,))


def replace_faqs(items: list[dict], mode: str = "replace") -> dict:
    """匯入 FAQ。mode: replace = 先清空再寫入；append = 直接附加。"""
    written = 0
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if mode == "replace":
                cur.execute("DELETE FROM autoreply_faq")
            for i, it in enumerate(items):
                cur.execute("""
                    INSERT INTO autoreply_faq (question, answer, keywords, enabled, sort_order)
                    VALUES (%s, %s, %s, %s, %s)
                """, (it["question"], it["answer"], it.get("keywords", ""),
                      bool(it.get("enabled", True)), i))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "mode": mode}


# ---------- 業務表格 ----------

def list_tables(enabled_only: bool = False) -> list[dict]:
    sql = """
        SELECT id, name, description, columns, enabled, sort_order, created_at, updated_at
        FROM autoreply_business_table
    """
    if enabled_only:
        sql += " WHERE enabled = true"
    sql += " ORDER BY sort_order, id"
    return query(sql)


def create_table(name: str, description: str = "", columns: list | None = None,
                 enabled: bool = True) -> dict:
    import json
    row = execute_returning("""
        INSERT INTO autoreply_business_table (name, description, columns, enabled, sort_order)
        VALUES (%s, %s, %s::jsonb, %s,
                COALESCE((SELECT max(sort_order)+1 FROM autoreply_business_table), 0))
        RETURNING id, name, description, columns, enabled, sort_order
    """, (name, description, json.dumps(columns or [], ensure_ascii=False), enabled))
    keys = ("id", "name", "description", "columns", "enabled", "sort_order")
    return dict(zip(keys, row))


def update_table(table_id: int, fields: dict) -> bool:
    import json
    allowed = ("name", "description", "enabled", "sort_order")
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=%s")
            params.append(v)
    if "columns" in fields:
        sets.append("columns=%s::jsonb")
        params.append(json.dumps(fields["columns"] or [], ensure_ascii=False))
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(table_id)
    return execute(f"UPDATE autoreply_business_table SET {', '.join(sets)} WHERE id=%s",
                   tuple(params)) > 0


def delete_table(table_id: int) -> bool:
    return execute("DELETE FROM autoreply_business_table WHERE id=%s", (table_id,)) > 0


def list_rows(table_id: int | None = None, enabled_only: bool = False) -> list[dict]:
    sql = """
        SELECT id, table_id, data, enabled, sort_order
        FROM autoreply_business_row
    """
    where, params = [], []
    if table_id is not None:
        where.append("table_id=%s")
        params.append(table_id)
    if enabled_only:
        where.append("enabled=true")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY table_id, sort_order, id"
    return query(sql, tuple(params))


def replace_rows(table_id: int, rows: list[dict]) -> int:
    """整表覆寫資料列（匯入用），回傳寫入列數。"""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM autoreply_business_row WHERE table_id=%s", (table_id,))
            for i, data in enumerate(rows):
                cur.execute("""
                    INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
                    VALUES (%s, %s::jsonb, true, %s)
                """, (table_id, json.dumps(data, ensure_ascii=False), i))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def append_rows(table_id: int, rows: list[dict]) -> int:
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(max(sort_order)+1, 0) FROM autoreply_business_row WHERE table_id=%s",
                        (table_id,))
            start = cur.fetchone()[0]
            for i, data in enumerate(rows):
                cur.execute("""
                    INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
                    VALUES (%s, %s::jsonb, true, %s)
                """, (table_id, json.dumps(data, ensure_ascii=False), start + i))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def create_row(table_id: int, data: dict) -> dict:
    import json
    row = execute_returning("""
        INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
        VALUES (%s, %s::jsonb, true,
                COALESCE((SELECT max(sort_order)+1 FROM autoreply_business_row WHERE table_id=%s), 0))
        RETURNING id, table_id, data, enabled, sort_order
    """, (table_id, json.dumps(data, ensure_ascii=False), table_id))
    keys = ("id", "table_id", "data", "enabled", "sort_order")
    return dict(zip(keys, row))


def update_row(row_id: int, data: dict | None = None, enabled: bool | None = None) -> bool:
    import json
    sets, params = [], []
    if data is not None:
        sets.append("data=%s::jsonb")
        params.append(json.dumps(data, ensure_ascii=False))
    if enabled is not None:
        sets.append("enabled=%s")
        params.append(bool(enabled))
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(row_id)
    return execute(f"UPDATE autoreply_business_row SET {', '.join(sets)} WHERE id=%s",
                   tuple(params)) > 0


def delete_row(row_id: int) -> bool:
    return execute("DELETE FROM autoreply_business_row WHERE id=%s", (row_id,)) > 0


def delete_rows_bulk(row_ids: list[int]) -> int:
    if not row_ids:
        return 0
    return execute("DELETE FROM autoreply_business_row WHERE id = ANY(%s)", (list(row_ids),))


# ---------- 日誌 / 認領 ----------

# 日誌與認領共用一份欄位清單，避免兩邊走鐘
_LOG_COLUMNS = (
    "claim_key", "channel", "reply_scope", "tier", "detail", "reason", "question",
    "answer", "conversation_key", "message_id", "post_id", "comment_id",
    "faq_id", "table_id", "matched_rows", "score", "rewrote", "rewritten_query",
    "history_used", "send_status",
)

_LOG_SQL = f"""
    INSERT INTO autoreply_log ({', '.join(_LOG_COLUMNS)})
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
"""


def _log_params(record: dict) -> tuple:
    matched = record.get("matched_rows")
    return (
        record.get("claim_key"),
        record.get("channel"), record.get("reply_scope"), record.get("tier"),
        record.get("detail"), record.get("reason"), record.get("question"),
        record.get("answer"), record.get("conversation_key"), record.get("message_id"),
        record.get("post_id"), record.get("comment_id"), record.get("faq_id"),
        record.get("table_id"),
        json.dumps(matched, ensure_ascii=False) if matched is not None else None,
        record.get("score"), bool(record.get("rewrote")),
        record.get("rewritten_query") or None,
        int(record.get("history_used") or 0),
        record.get("send_status", "not_sent"),
    )


def insert_log(record: dict) -> None:
    execute(_LOG_SQL, _log_params(record))


def claim_message(claim_key: str, record: dict) -> bool:
    """原子認領一則訊息：回傳 True 表示由我處理，False 表示別人已處理過。

    靠 `idx_autoreply_log_claim` 唯一索引 + ON CONFLICT DO NOTHING 保證只有一個
    呼叫者能成功插入。這解決的是「同一則訊息被重複投遞 / 併發處理」造成的重複回覆
    —— 冷卻檢查是「先查再寫」、中間有窗口，擋不住同時進來的兩份。
    """
    if not claim_key:
        return True
    conn = _connect()
    try:
        with conn.cursor() as cur:
            params = list(_log_params(record))
            params[0] = claim_key
            cur.execute(_LOG_SQL + " ON CONFLICT (claim_key) DO NOTHING", tuple(params))
            inserted = cur.rowcount > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def update_claim(claim_key: str, record: dict) -> int:
    """認領列已存在，後續用它記下最終結果（不再插新列）。"""
    if not claim_key:
        insert_log(record)
        return 0
    matched = record.get("matched_rows")
    return execute("""
        UPDATE autoreply_log SET
            tier=%s, detail=%s, reason=%s, question=%s, answer=%s,
            conversation_key=%s, message_id=%s, post_id=%s, comment_id=%s,
            faq_id=%s, table_id=%s, matched_rows=%s::jsonb, score=%s, rewrote=%s,
            rewritten_query=%s, history_used=%s, send_status=%s
        WHERE claim_key=%s
    """, (
        record.get("tier"), record.get("detail"), record.get("reason"),
        record.get("question"), record.get("answer"), record.get("conversation_key"),
        record.get("message_id"), record.get("post_id"), record.get("comment_id"),
        record.get("faq_id"), record.get("table_id"),
        json.dumps(matched, ensure_ascii=False) if matched is not None else None,
        record.get("score"), bool(record.get("rewrote")),
        record.get("rewritten_query") or None, int(record.get("history_used") or 0),
        record.get("send_status", "not_sent"), claim_key,
    ))


def is_claimed(claim_key: str) -> bool:
    """這則訊息是否已被認領過（除錯用）。"""
    if not claim_key:
        return False
    rows = query("SELECT 1 FROM autoreply_log WHERE claim_key=%s LIMIT 1", (claim_key,))
    return bool(rows)


def recent_reply_count(conversation_key: str, seconds: int) -> int:
    """該會話在近 N 秒內已成功送出的自動回復數（冷卻判斷用）。"""
    if not conversation_key or seconds <= 0:
        return 0
    rows = query("""
        SELECT count(*) AS n FROM autoreply_log
        WHERE conversation_key=%s AND send_status='sent'
          AND created_at > now() - (%s || ' seconds')::interval
    """, (conversation_key, int(seconds)))
    return rows[0]["n"] if rows else 0


def today_reply_count(conversation_key: str) -> int:
    """該會話今天的自動回復送出數（每日上限判斷用）。"""
    if not conversation_key:
        return 0
    rows = query("""
        SELECT count(*) AS n FROM autoreply_log
        WHERE conversation_key=%s AND send_status='sent'
          AND created_at >= date_trunc('day', now())
    """, (conversation_key,))
    return rows[0]["n"] if rows else 0


def list_logs(channel: str = "", scope: str = "", tier: str = "", limit: int = 100,
              offset: int = 0) -> dict:
    where, params = ["tier <> %s"], [TIER_CLAIM]
    if channel:
        where.append("channel=%s")
        params.append(channel)
    if scope:
        where.append("reply_scope=%s")
        params.append(scope)
    if tier:
        where.append("tier=%s")
        params.append(tier)
    clause = " WHERE " + " AND ".join(where)
    # has_matched_rows：這筆有沒有檢索明細可以展開。列表不撈明細本體，但 UI 要能
    # 判斷「這列值不值得點」，所以在 SQL 端算一個布林值（很便宜，且 JSONB 有函式可用）。
    rows = query(f"""
        SELECT id, created_at, channel, reply_scope, tier, detail, reason, question,
               answer, conversation_key, message_id, post_id, comment_id,
               faq_id, table_id, score, rewrote, rewritten_query, history_used, send_status,
               (matched_rows IS NOT NULL
                AND jsonb_typeof(matched_rows) = 'array'
                AND jsonb_array_length(matched_rows) > 0) AS has_matched_rows
        FROM autoreply_log{clause}
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
    """, tuple(params + [max(1, min(limit, 500)), max(0, offset)]))
    total = query(f"SELECT count(*) AS n FROM autoreply_log{clause}", tuple(params))
    return {"logs": rows, "total": total[0]["n"] if total else 0}


def get_log(log_id: int) -> dict | None:
    """單筆紀錄的完整內容（含 `matched_rows` 召回明細）。

    ⚠️ 為什麼列表不帶這個欄位：`matched_rows` 每筆可能是幾十個候選塊的 JSON，
    列表動輒上百筆，一起 SELECT 會讓回應肥到沒必要。**列表輕、詳情重。**
    """
    rows = query("""
        SELECT id, created_at, channel, reply_scope, tier, detail, reason, question,
               answer, conversation_key, message_id, post_id, comment_id,
               faq_id, table_id, matched_rows, score, rewrote, rewritten_query,
               history_used, send_status
        FROM autoreply_log WHERE id=%s
    """, (log_id,))
    return rows[0] if rows else None


def log_summary() -> dict:
    """各渠道＋場景的自動回復統計（給 UI 顯示）。

    rewritten = 第二層「改寫 query 後才命中」的次數（本來會降級人工，被救回來的）。
    """
    rows = query("""
        SELECT channel, reply_scope, tier,
               count(*) AS n,
               count(*) FILTER (WHERE tier='table' AND rewrote) AS rewritten
        FROM autoreply_log WHERE tier <> %s
        GROUP BY channel, reply_scope, tier
    """, (TIER_CLAIM,))
    out: dict[str, dict] = {}
    for r in rows:
        key = scope_key(r["channel"], r["reply_scope"])
        bucket = out.setdefault(key, {"channel": r["channel"], "scope": r["reply_scope"],
                                      "faq": 0, "table": 0, "rag": 0, "miss": 0, "error": 0,
                                      "rewritten": 0, "total": 0})
        tier = r["tier"] if r["tier"] in KNOWN_TIERS else "error"
        bucket[tier] += r["n"]
        bucket["rewritten"] += r["rewritten"] or 0
        bucket["total"] += r["n"]
    return out


# ---------- 工作台「人工確認的引擎回覆」要用的查詢 ----------

def latest_unanswered_dm(channel: str, conversation_key: str) -> dict | None:
    """某 IG/FB 私訊會話裡，最新一則「還沒被回覆」的客戶訊息。"""
    rows = query("""
        SELECT message_id, text FROM igfb_dm
        WHERE platform=%s AND conversation_key=%s
          AND direction='inbound' AND is_handled=false
        ORDER BY COALESCE(created_at, inserted_at) DESC LIMIT 1
    """, (channel, conversation_key))
    return rows[0] if rows else None


def latest_unanswered_forward_log(channel: str, conversation_key: str) -> dict | None:
    """微信／WhatsApp：某會話裡最新一則未處理的客戶來訊（文字在 forwarded_payload）。"""
    rows = query("""
        SELECT message_id, raw_content, forwarded_payload FROM forward_log
        WHERE platform=%s AND direction='inbound' AND is_handled=false
          AND (sender_id=%s OR conversation_id=%s)
        ORDER BY created_at DESC LIMIT 1
    """, (channel, conversation_key, conversation_key))
    return rows[0] if rows else None


def latest_inbound_dm(channel: str, conversation_key: str) -> dict | None:
    """該 IG/FB 會話最新一則客戶訊息（不管有沒有處理過）。

    給「客服剛回完這則，要不要把它存進 FAQ」判斷用：需要知道它是不是
    `auto_reply_state='miss'`（自動回復未命中），所以不能只挑未處理的。
    """
    rows = query("""
        SELECT message_id, text, coalesce(auto_reply_state, '') AS auto_reply_state
        FROM igfb_dm
        WHERE platform=%s AND conversation_key=%s AND direction='inbound'
        ORDER BY COALESCE(created_at, inserted_at) DESC LIMIT 1
    """, (channel, conversation_key))
    return rows[0] if rows else None


def latest_inbound_forward_log(channel: str, conversation_key: str) -> dict | None:
    """WhatsApp：該會話最新一則客戶訊息（同樣不看 is_handled）。"""
    rows = query("""
        SELECT message_id,
               coalesce(forwarded_payload->>'messageContent', '') AS text,
               coalesce(auto_reply_state, '') AS auto_reply_state
        FROM forward_log
        WHERE platform=%s AND direction='inbound'
          AND (sender_id=%s OR conversation_id=%s)
        ORDER BY created_at DESC LIMIT 1
    """, (channel, conversation_key, conversation_key))
    return rows[0] if rows else None


def miss_counts() -> dict:
    """尚未人工處理的自動回復未命中數（給導覽紅點／標記用）。"""
    out: dict[str, int] = {}
    for r in query("""
        SELECT 'whatsapp:dm' AS k, count(*) AS n FROM forward_log
          WHERE platform='whatsapp' AND direction='inbound'
            AND auto_reply_state=%s AND is_handled=false
        UNION ALL
        SELECT 'wechat:dm', count(*) FROM forward_log
          WHERE platform='wechat' AND direction='inbound'
            AND auto_reply_state=%s AND is_handled=false
        UNION ALL
        SELECT platform || ':dm', count(*) FROM igfb_dm
          WHERE direction='inbound' AND auto_reply_state=%s AND is_handled=false
          GROUP BY platform
        UNION ALL
        SELECT platform || ':comment', count(*) FROM igfb_comment
          WHERE parent_id IS NULL AND auto_reply_state=%s AND is_handled=false
          GROUP BY platform
    """, (STATE_MISS, STATE_MISS, STATE_MISS, STATE_MISS)):
        out[r["k"]] = r["n"]
    return out


# ---------- 訊息的自動回復標記 ----------

def mark_forward_log(message_ids: list[str], platform: str, state: str,
                     tier: str = "", detail: str = "") -> int:
    if not message_ids:
        return 0
    return execute("""
        UPDATE forward_log SET auto_reply_state=%s, auto_reply_tier=%s, auto_reply_detail=%s
        WHERE platform=%s AND message_id = ANY(%s)
    """, (state, tier or None, detail or None, platform, list(message_ids)))


def mark_dm(platform: str, message_id: str, state: str, tier: str = "", detail: str = "") -> int:
    if not message_id:
        return 0
    return execute("""
        UPDATE igfb_dm SET auto_reply_state=%s, auto_reply_tier=%s, auto_reply_detail=%s
        WHERE platform=%s AND message_id=%s
    """, (state, tier or None, detail or None, platform, message_id))


def mark_comment(platform: str, comment_id: str, state: str, tier: str = "",
                 detail: str = "") -> int:
    if not comment_id:
        return 0
    return execute("""
        UPDATE igfb_comment SET auto_reply_state=%s, auto_reply_tier=%s, auto_reply_detail=%s
        WHERE platform=%s AND comment_id=%s
    """, (state, tier or None, detail or None, platform, comment_id))


def mark_forward_log_handled(platform: str, conversation_id: str) -> int:
    """自動回復送出成功後，把該會話的客戶來訊標為已處理（消除紅點）。"""
    return execute("""
        UPDATE forward_log SET is_handled=true
        WHERE platform=%s AND conversation_id=%s AND direction='inbound'
    """, (platform, conversation_id))


# ---------- 對話上下文（業務表格 query 改寫用） ----------
#
# 一律回傳「這次要處理的那則訊息之前」的最近 N 則訊息，由舊到新。
# 排除當前訊息用 id，不用文字比對 —— 客戶可能連續兩句講一模一樣的話。

def _trim_before(items: list[dict], exclude_id: str = "", exclude_text: str = "") -> list[dict]:
    """把當前這則訊息從歷史裡去掉（優先用 id，其次用文字）。"""
    out = list(items)
    if exclude_id:
        out = [x for x in out if str(x.get("id") or "") != str(exclude_id)]
    elif exclude_text:
        t = exclude_text.strip()
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("direction") == "inbound" and (out[i].get("text") or "").strip() == t:
                out.pop(i)
                break
    return [x for x in out if (x.get("text") or "").strip()]


def dm_history_forward_log(platform: str, sender_id: str, limit: int,
                           exclude_text: str = "") -> list[dict]:
    """微信 / WhatsApp 私訊：取最近 limit 則訊息（客戶與我方交替），由舊到新。"""
    if not sender_id or limit <= 0:
        return []
    conv_id = __import__("wechat").openid_to_conversation_id(sender_id)
    rows = query("""
        SELECT id, message_id, direction, forwarded_payload, raw_content
        FROM forward_log
        WHERE platform=%s AND direction IN ('inbound','outbound')
          AND (sender_id=%s OR conversation_id=%s)
        ORDER BY created_at DESC, id DESC
        LIMIT %s
    """, (platform, sender_id, conv_id, limit + 1))
    rows.reverse()

    out = []
    for r in rows:
        if r["direction"] == "outbound":
            # 我方回覆的內容記在 raw_content（回覆請求的 JSON）
            try:
                rc = json.loads(r.get("raw_content") or "{}")
            except Exception:
                rc = {}
            # raw_content 不保證是物件（純數字會解析成 int），一律收斂成 dict
            text = rc.get("messageContent", "") if isinstance(rc, dict) else ""
            if not text:
                text = (r.get("forwarded_payload") or {}).get("messageContent", "")
        else:
            text = (r.get("forwarded_payload") or {}).get("messageContent", "")
        out.append({"id": str(r.get("message_id") or r["id"]),
                    "direction": r["direction"], "text": (text or "").strip()})

    # 這則訊息（若已落地，通常是最後一筆 inbound）要先拿掉
    return _trim_before(out, exclude_text=exclude_text)[-limit:]


def dm_by_message_id(platform: str, conversation_key: str, message_id: str) -> dict | None:
    """用 message_id 取某一則私訊（工作台在聊天框右鍵指定某一則時用）。"""
    if not conversation_key or not message_id:
        return None
    rows = query("""
        SELECT id, message_id, direction, text
        FROM igfb_dm
        WHERE platform=%s AND conversation_key=%s AND message_id=%s
        ORDER BY id DESC LIMIT 1
    """, (platform, conversation_key, message_id))
    return rows[0] if rows else None


def dm_history_igfb(platform: str, conversation_key: str, limit: int,
                    exclude_id: str = "", exclude_text: str = "") -> list[dict]:
    """IG / FB 私訊：取對話歷史（由舊到新）。

    exclude_id 有值時，**以那一則為界、只取它之前**的最近 limit 則
    （工作台在聊天框右鍵某一則 → 上下文就是「這一條往上數的幾條」）。
    取不到錨點（訊息未落地／id 為空）就退回舊行為：取最新 limit 則，
    再用 exclude_text 把當前那句剔掉，避免它自己被算進上下文。

    排序鍵維持與原本一致的 COALESCE(created_at, inserted_at), id —— 不改變
    「哪幾則算最近」的既有語意。
    """
    if not conversation_key or limit <= 0:
        return []

    rows: list[dict] = []
    anchored = False          # 錨點有找到 → 就算查不到更早的訊息也不該退回「最新幾則」
    if exclude_id:
        anchor = query("""
            SELECT COALESCE(created_at, inserted_at) AS t, id
            FROM igfb_dm
            WHERE platform=%s AND conversation_key=%s AND message_id=%s
            ORDER BY id DESC LIMIT 1
        """, (platform, conversation_key, exclude_id))
        if anchor:
            anchored = True
            rows = query("""
                SELECT id, message_id, direction, text
                FROM igfb_dm
                WHERE platform=%s AND conversation_key=%s
                  AND (COALESCE(created_at, inserted_at), id) < (%s, %s)
                ORDER BY COALESCE(created_at, inserted_at) DESC, id DESC
                LIMIT %s
            """, (platform, conversation_key, anchor[0]["t"], anchor[0]["id"], limit))

    if not anchored:
        rows = query("""
            SELECT id, message_id, direction, text
            FROM igfb_dm
            WHERE platform=%s AND conversation_key=%s
            ORDER BY COALESCE(created_at, inserted_at) DESC, id DESC
            LIMIT %s
        """, (platform, conversation_key, limit + 1))

    rows.reverse()
    items = [{"id": str(r.get("message_id") or r["id"]),
              "direction": r["direction"], "text": (r["text"] or "").strip()} for r in rows]
    return _trim_before(items, exclude_id=exclude_id, exclude_text=exclude_text)[-limit:]


def _thread_root(platform: str, comment_id: str) -> str:
    """往上追溯討論串的頂層留言 id。"""
    root = comment_id
    for _ in range(10):
        rows = query("SELECT parent_id FROM igfb_comment WHERE platform=%s AND comment_id=%s",
                     (platform, root))
        if not rows or not rows[0].get("parent_id"):
            break
        root = rows[0]["parent_id"]
    return root


def comment_thread_history(platform: str, comment_id: str, limit: int) -> list[dict]:
    """貼文留言：取該討論串中、這則留言之前的最近 limit 則（客戶與我方交替）。

    IG/FB 的留言時間戳可能缺失或不可靠，因此改用 id 排序（等於實際插入順序）。
    """
    if not comment_id or limit <= 0:
        return []
    root = _thread_root(platform, comment_id)
    self_id = __import__("igfb").self_id(platform)

    rows = query("""
        SELECT id, comment_id, from_id, text
        FROM igfb_comment
        WHERE platform=%s AND (comment_id=%s OR parent_id=%s)
        ORDER BY id ASC
    """, (platform, root, root))

    items = []
    for r in rows:
        is_self = bool(self_id) and str(r.get("from_id") or "") == str(self_id)
        items.append({"id": str(r["comment_id"]),
                      "direction": "outbound" if is_self else "inbound",
                      "text": (r.get("text") or "").strip()})
    return _trim_before(items, exclude_id=comment_id)[-limit:]


def get_conversation_history(channel: str, scope: str, *, sender_id: str = "",
                             comment_id: str = "", message_id: str = "",
                             current_text: str = "", limit: int = 5) -> list[dict]:
    """依渠道＋場景取得「當前訊息之前」的最近對話。取不到回空清單（改寫退化為單句改寫）。"""
    try:
        if scope == "comment":
            return comment_thread_history(channel, comment_id, limit)
        if channel in ("ig", "fb"):
            # exclude_id：以某一則為界（工作台右鍵那一則）；
            # exclude_text：沒指定錨點時，至少別把當前那句算進它自己的上下文
            return dm_history_igfb(channel, sender_id, limit, exclude_id=message_id,
                                   exclude_text=current_text)
        return dm_history_forward_log(channel, sender_id, limit, exclude_text=current_text)
    except Exception as e:  # 取歷史失敗不該影響自動回復
        log.warning("取對話上下文失敗 %s/%s: %s", channel, scope, e)
        return []
