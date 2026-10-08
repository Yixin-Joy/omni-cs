"""RAG 知識庫的資料層：資料表建立 + 知識庫 / 文件 / 分塊 / 參數讀寫。

資料庫固定為 `platform_forwarder`（見 .env 的 DATABASE_URL），不碰其他庫。
設計依據見根目錄 `RAG_DESIGN.md`。

資料表：
  kb_collection  知識庫（分組），可整組啟停
  kb_document    文件（解析 / 分塊 / 向量化的狀態機）
  kb_chunk       知識塊（含 embedding REAL[]；第一版不使用 pgvector）
  kb_job         入庫工作佇列（服務重啟可續跑）
  kb_param       參數覆寫（★ 按知識庫存，PK=(collection_id, key)）

為什麼是 `REAL[]` 而不是 pgvector：本機 PostgreSQL 18.6 沒有 `vector` 擴充
（`pg_available_extensions` 查無），而 Windows 上要編 pgvector 得裝 MSVC 工具鏈。
第一版資料量（萬級塊）用「`REAL[]` 落地 + 啟動載入記憶體算 cosine」完全夠，
單次檢索 < 20ms、1 萬塊約 40MB 記憶體。等真的上到十萬塊再遷 pgvector。
"""
import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import psycopg2

import config

log = logging.getLogger("wechat-forwarder")

# ---------- 資料表定義（create_db.py 也呼叫這裡，保持單一真相） ----------

_DDL = """
CREATE TABLE IF NOT EXISTS kb_collection (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_document (
    id            BIGSERIAL PRIMARY KEY,
    collection_id BIGINT REFERENCES kb_collection(id) ON DELETE SET NULL,
    title         TEXT,
    filename      TEXT,
    mime          TEXT,
    size_bytes    BIGINT,
    sha256        TEXT,
    source_path   TEXT,                       -- media/kb/<uuid>/<原始檔>
    doc_type      VARCHAR(10),                -- pdf|docx|xlsx|md|txt|csv|html
    version       INTEGER NOT NULL DEFAULT 1,
    superseded_by BIGINT,                     -- 同檔重傳時指向新版本
    status        VARCHAR(12) NOT NULL DEFAULT 'draft',
                  -- draft|parsing|chunking|embedding|ready|failed|disabled
    progress      SMALLINT NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    char_count    INTEGER NOT NULL DEFAULT 0,
    parse_meta    JSONB,                      -- 頁數 / 工作表 / 標題階層
    parse_params  JSONB,                      -- ★ 本次分塊用的參數快照（重跑可重現）
    error         TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_chunk (
    id               BIGSERIAL PRIMARY KEY,
    doc_id           BIGINT NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    ordinal          INTEGER NOT NULL,        -- 文件內序號（0 起）
    text             TEXT NOT NULL,           -- 已含 prefix 的最終文字（客服可編輯）
    prefix           TEXT,                    -- 「產品手冊 › 第3章 藍牙耳機 › 價格」
    char_count       INTEGER,
    page_no          INTEGER,                 -- PDF 頁碼
    heading_path     TEXT,                    -- 標題階層（md/docx）
    sheet            TEXT,                    -- Excel 工作表名
    row_from         INTEGER,                 -- 表格列範圍（含）
    row_to           INTEGER,                 -- 表格列範圍（含）
    embedding        REAL[],                  -- 語義向量（第一版不用 pgvector）
    embedding_model  TEXT,
    embedding_dim    INTEGER,
    embedding_status VARCHAR(8) NOT NULL DEFAULT 'pending',   -- pending|done|failed
    edited           BOOLEAN NOT NULL DEFAULT false,          -- ★ 客服手改過
    enabled          BOOLEAN NOT NULL DEFAULT true,           -- ★ 可停用單塊
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_job (
    id          BIGSERIAL PRIMARY KEY,
    doc_id      BIGINT REFERENCES kb_document(id) ON DELETE CASCADE,
    stage       VARCHAR(12),                  -- parse|chunk|embed
    status      VARCHAR(10) NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_param (
    collection_id BIGINT NOT NULL REFERENCES kb_collection(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    value         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, key)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc       ON kb_chunk(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_embed     ON kb_chunk(embedding_status, enabled);
CREATE INDEX IF NOT EXISTS idx_kb_document_coll   ON kb_document(collection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_document_status ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_job_status      ON kb_job(status, id);
"""

# 文件狀態值
STATUS_DRAFT = "draft"
STATUS_PARSING = "parsing"
STATUS_CHUNKING = "chunking"
STATUS_EMBEDDING = "embedding"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

# 分塊向量化狀態
EMB_PENDING = "pending"
EMB_DONE = "done"
EMB_FAILED = "failed"

# 工作佇列狀態
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


# ---------- 連線 ----------

def _connect(dbname: str | None = None):
    url = config.DATABASE_URL
    if dbname:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))
    return psycopg2.connect(url)


def _current_dbname() -> str:
    return urlsplit(config.DATABASE_URL).path.lstrip("/") or "platform_forwarder"


# ---------- 查詢輔助 ----------

def query(sql: str, params: tuple = ()) -> list[dict]:
    """執行 SELECT，回傳 list[dict]。"""
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
    """執行 INSERT ... RETURNING，回傳第一列（tuple）。"""
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
    """建立 RAG 知識庫相關資料表。可重複執行。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    finally:
        conn.close()


async def init_schema_async() -> None:
    """非同步包裝（服務啟動時用），失敗只記 log 不擋啟動。"""
    try:
        await asyncio.to_thread(init_schema)
        log.info("RAG 知識庫資料表就緒（%s）", _current_dbname())
    except Exception as e:
        log.warning("RAG 知識庫資料表初始化失敗: %s", e)
