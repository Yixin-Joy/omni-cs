"""RAG 知識庫的資料層：資料表建立 + 知識庫 / 文件 / 分塊 / 參數讀寫。

資料庫固定為 `platform_forwarder`（見 .env 的 DATABASE_URL），不碰其他庫。
設計依據見根目錄 `RAG_DESIGN.md`。

資料表：
  kb_collection  知識庫（分組），可整組啟停
  kb_document    文件（解析 / 分塊 / 向量化的狀態機）
  kb_chunk       知識塊（含 embedding REAL[]；第一版不使用 pgvector）
  kb_job         入庫工作佇列（服務重啟可續跑）
  kb_param       參數覆寫（★ 檢索參數=全域 collection_id IS NULL；分塊參數按知識庫）

為什麼是 `REAL[]` 而不是 pgvector：本機 PostgreSQL 18.6 沒有 `vector` 擴充
（`pg_available_extensions` 查無），而 Windows 上要編 pgvector 得裝 MSVC 工具鏈。
第一版資料量（萬級塊）用「`REAL[]` 落地 + 啟動載入記憶體算 cosine」完全夠，
單次檢索 < 20ms、1 萬塊約 40MB 記憶體。等真的上到十萬塊再遷 pgvector。
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

-- collection_id IS NULL = 全域：
--     · 檢索參數（top_k / min_score / …）一律存這裡，所有知識庫共用一組
--     · 未分組文件（collection_id IS NULL）的分塊參數也存這裡
-- 分塊參數（chunk_*）才按知識庫分開存（2026-10-08 定案）
CREATE TABLE IF NOT EXISTS kb_param (
    collection_id BIGINT REFERENCES kb_collection(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    value         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc       ON kb_chunk(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_embed     ON kb_chunk(embedding_status, enabled);
CREATE INDEX IF NOT EXISTS idx_kb_document_coll   ON kb_document(collection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_document_status ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_job_status      ON kb_job(status, id);
"""

# 舊庫升級：kb_param 原本是 `collection_id BIGINT NOT NULL` + PK(collection_id, key)，
# 沒辦法存「全域」那批（NULL）。這裡把它改成可空 + NULLS NOT DISTINCT 唯一約束 ——
# 少了 NULLS NOT DISTINCT，PG 會把每一筆 NULL 當成互不相等，同一組參數能插無限多筆。
# 兩個步驟都可重複執行（DROP NOT NULL 已是 nullable 也不會錯；DO 區塊先檢查約束在不在）。
# ⚠️ 順序不能顛倒：`ALTER COLUMN ... DROP NOT NULL` 在 PK 欄位上會直接報
#    `column "collection_id" is in a primary key`，所以必須先拆掉舊 PK。
_MIGRATE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'kb_param_pkey' AND conrelid = 'kb_param'::regclass) THEN
        ALTER TABLE kb_param DROP CONSTRAINT kb_param_pkey;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'kb_param_scope_key' AND conrelid = 'kb_param'::regclass) THEN
        ALTER TABLE kb_param ADD CONSTRAINT kb_param_scope_key
            UNIQUE NULLS NOT DISTINCT (collection_id, key);
    END IF;
END $$;
ALTER TABLE kb_param ALTER COLUMN collection_id DROP NOT NULL;
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
            cur.execute(_MIGRATE)
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


# ======================================================================
# 知識庫（kb_collection）
# ======================================================================

def list_collections(enabled_only: bool = False) -> list[dict]:
    """列出知識庫，附文件數 / 塊數統計（概覽頁用）。"""
    where = "WHERE c.enabled" if enabled_only else ""
    return query(f"""
        SELECT c.*,
               COALESCE(d.doc_count, 0)   AS doc_count,
               COALESCE(d.ready_count, 0) AS ready_count,
               COALESCE(d.chunk_count, 0) AS chunk_count
        FROM kb_collection c
        LEFT JOIN (
            SELECT collection_id,
                   count(*)                                                   AS doc_count,
                   count(*) FILTER (WHERE status = 'ready')                    AS ready_count,
                   COALESCE(sum(chunk_count), 0)                              AS chunk_count
            FROM kb_document GROUP BY collection_id
        ) d ON d.collection_id = c.id
        {where}
        ORDER BY c.sort_order, c.id
    """)


def get_collection(cid: int) -> dict | None:
    rows = query("SELECT * FROM kb_collection WHERE id=%s", (cid,))
    return rows[0] if rows else None


def create_collection(name: str, description: str = "") -> dict:
    row = execute_returning(
        "INSERT INTO kb_collection(name, description) VALUES (%s, %s) RETURNING id",
        (name.strip(), description.strip()),
    )
    return get_collection(row[0]) or {}


def update_collection(cid: int, fields: dict) -> bool:
    allowed = {"name", "description", "sort_order", "enabled"}
    sets, params = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k}=%s")
            params.append(fields[k])
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(cid)
    return execute(f"UPDATE kb_collection SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def delete_collection(cid: int) -> bool:
    """刪知識庫。文件不會被連帶刪除（FK 是 SET NULL），只是變成「未分組」。"""
    return execute("DELETE FROM kb_collection WHERE id=%s", (cid,)) > 0


# ======================================================================
# 參數（kb_param）—— DB 覆寫 .env 的預設值，唯一鍵 (collection_id, key)
#
# ★ 2026-10-08 定案：參數分「兩層」，分派規則寫在 `_param_target()`，
#   set / load 兩邊共用同一套，避免又出現「寫 A 讀 B」這種不一致。
#
#     **只有分塊參數是按知識庫，其餘（檢索參數、提示詞）一律全域。**
#
#     檢索參數 group=retrieval（top_k / min_score / max_per_doc / max_context_chars /
#         rerank_enabled / rerank_top_n / query_rewrite）→ **全域**（collection_id IS NULL）。
#         所有知識庫共用一組；以前「誰的 doc_id 小就聽誰」的洞一併消失。
#     提示詞 group=prompt（rag_answer_prompt / rewrite_prompt）→ **全域**（同上）。
#     分塊參數 group=chunk（chunk_strategy / chunk_size / chunk_min / chunk_max /
#         chunk_overlap / chunk_prefix）→ **按知識庫**，在分塊配置界面單獨設。
#         未分組文件（cid=None）落在全域那批。
# ======================================================================

def _param_scope(name: str) -> str:
    """參數的歸屬：'global'（檢索／提示詞，全庫共用）/ 'collection'（分塊，按知識庫）。"""
    import rag_config

    spec = rag_config.PARAM_SPECS.get(name) or {}
    return "collection" if spec.get("group") == "chunk" else "global"


def _param_target(name: str, cid: int | None) -> int | None:
    """這個參數該落在哪一行：檢索參數恆為 None（全域），分塊參數為該知識庫。"""
    return None if _param_scope(name) == "global" else cid


def get_param_overrides(cid: int | None = None, *, scope: str | None = None) -> dict[str, str]:
    """取覆寫值。`scope='global'` → 讀全域那批；否則讀「這個知識庫」那批。"""
    if scope == "global" or (scope is None and not cid):
        rows = query("SELECT key, value FROM kb_param WHERE collection_id IS NULL")
    else:
        rows = query("SELECT key, value FROM kb_param WHERE collection_id=%s", (cid,))
    return {r["key"]: r["value"] for r in rows}


def get_global_param_overrides() -> dict[str, str]:
    """全域那批覆寫（檢索參數 + 未分組文件的分塊預設）。"""
    return get_param_overrides(None, scope="global")


def set_params(cid: int | None, values: dict) -> int:
    """寫入/覆寫參數。值為 None / 空字串 = 刪除覆寫（回到 .env 預設）。

    ⚠️ 檢索參數一律寫到**全域**，所以 `set_params(某個庫, {"top_k": 5})` 改的是
    所有知識庫共用的那個值（這是刻意的：檢索參數不分庫）。
    """
    import rag_config

    n = 0
    for key, raw in (values or {}).items():
        if key not in rag_config.PARAM_SPECS:
            continue
        target = _param_target(key, cid)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            n += execute("DELETE FROM kb_param "
                         "WHERE collection_id IS NOT DISTINCT FROM %s AND key=%s",
                         (target, key))
            continue
        val = ("true" if raw else "false") if isinstance(raw, bool) else str(raw)
        execute("""
            INSERT INTO kb_param(collection_id, key, value) VALUES (%s, %s, %s)
            ON CONFLICT ON CONSTRAINT kb_param_scope_key
            DO UPDATE SET value=EXCLUDED.value, updated_at=now()
        """, (target, key, val))
        n += 1
    return n


def load_params(cid: int | None = None) -> dict:
    """★ 執行時取值的**唯一入口**：DB 有覆寫用 DB，否則用 .env / 內建預設。

    檢索參數讀全域那批；分塊參數讀該知識庫（cid=None → 讀全域，即未分組文件的預設）。
    回傳已轉型別並夾過範圍的 dict（key → value）。
    """
    import rag_config

    glob = get_global_param_overrides()
    own = get_param_overrides(cid) if cid else {}
    out: dict = {}
    for name, spec in rag_config.PARAM_SPECS.items():
        src = glob if (_param_scope(name) == "global" or not cid) else own
        raw = src.get(name)
        if raw in (None, ""):
            raw = spec["default"]
        out[name] = rag_config.coerce(name, raw)
    return out


def effective_param(cid: int | None, name: str):
    """單一參數的執行時值（檢索路徑用；5 秒快取見 kb_search.params_for）。"""
    import rag_config

    spec = rag_config.PARAM_SPECS.get(name)
    if not spec:
        return None
    if _param_scope(name) == "global" or not cid:
        raw = get_global_param_overrides().get(name)
    else:
        raw = get_param_overrides(cid).get(name)
    if raw in (None, ""):
        raw = spec["default"]
    return rag_config.coerce(name, raw)


# ======================================================================
# 文件（kb_document）
# ======================================================================

_DOC_COLS = """id, collection_id, title, filename, mime, size_bytes, sha256, source_path,
               doc_type, version, superseded_by, status, progress, chunk_count, char_count,
               parse_meta, parse_params, error, enabled, created_at, updated_at"""


def create_document(*, filename: str, title: str = "", mime: str = "", size_bytes: int = 0,
                    sha256: str = "", source_path: str = "", doc_type: str = "",
                    collection_id: int | None = None) -> dict:
    row = execute_returning("""
        INSERT INTO kb_document(collection_id, title, filename, mime, size_bytes, sha256,
                                source_path, doc_type, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft')
        RETURNING id
    """, (collection_id, title or filename, filename, mime, size_bytes, sha256,
          source_path, doc_type))
    return get_document(row[0]) or {}


def get_document(did: int) -> dict | None:
    rows = query(f"SELECT {_DOC_COLS} FROM kb_document WHERE id=%s", (did,))
    return rows[0] if rows else None


def list_documents(collection_id: int | None = None, status: str = "",
                   limit: int = 500, ungrouped: bool = False) -> list[dict]:
    """列文件。ungrouped=True 只列沒有歸屬知識庫的（collection_id IS NULL）。

    ⚠ collection_id 不能用 0 代表「未分組」—— DB 存的是 NULL，
       `collection_id=0` 會一條都查不到。
    """
    where, params = [], []
    if ungrouped:
        where.append("d.collection_id IS NULL")
    elif collection_id is not None:
        where.append("d.collection_id=%s")
        params.append(collection_id)
    if status:
        where.append("d.status=%s")
        params.append(status)
    sql = f"""
        SELECT {', '.join('d.' + c.strip() for c in _DOC_COLS.split(','))},
               c.name AS collection_name
        FROM kb_document d
        LEFT JOIN kb_collection c ON c.id = d.collection_id
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY d.created_at DESC
        LIMIT %s
    """
    params.append(limit)
    return query(sql, tuple(params))


def update_document(did: int, fields: dict) -> bool:
    allowed = {"collection_id", "title", "status", "progress", "chunk_count", "char_count",
               "parse_meta", "parse_params", "error", "enabled", "doc_type", "mime",
               "size_bytes", "sha256", "source_path", "version", "superseded_by"}
    sets, params = [], []
    for k in allowed:
        if k in fields:
            v = fields[k]
            if k in ("parse_meta", "parse_params") and v is not None:
                sets.append(f"{k}=%s::jsonb")
                params.append(json.dumps(v, ensure_ascii=False))
            else:
                sets.append(f"{k}=%s")
                params.append(v)
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(did)
    return execute(f"UPDATE kb_document SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def delete_document(did: int) -> bool:
    """刪文件（連帶刪 kb_chunk，FK 有 ON DELETE CASCADE）。"""
    return execute("DELETE FROM kb_document WHERE id=%s", (did,)) > 0


def find_document_by_sha(sha256: str, collection_id: int | None = None) -> dict | None:
    """同檔重複上傳偵測（同一知識庫內比對）。"""
    if not sha256:
        return None
    if collection_id is None:
        rows = query(f"SELECT {_DOC_COLS} FROM kb_document WHERE sha256=%s "
                     "ORDER BY id DESC LIMIT 1", (sha256,))
    else:
        rows = query(f"SELECT {_DOC_COLS} FROM kb_document WHERE sha256=%s "
                     "AND collection_id=%s ORDER BY id DESC LIMIT 1", (sha256, collection_id))
    return rows[0] if rows else None


# ======================================================================
# 知識塊（kb_chunk）
# ======================================================================

_CHUNK_COLS = """id, doc_id, ordinal, text, prefix, char_count, page_no, heading_path,
                 sheet, row_from, row_to, embedding_model, embedding_dim,
                 embedding_status, edited, enabled, created_at"""


def replace_chunks(doc_id: int, chunks: list) -> int:
    """整批換掉一份文件的分塊（重新分塊時用）。

    ⚠️ 會刪掉舊塊 —— 手改過的內容要由呼叫端先確認（`edited=true`）。
    向量另外由 embedding 階段寫入。
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_chunk WHERE doc_id=%s", (doc_id,))
            for i, c in enumerate(chunks):
                d = c.as_dict() if hasattr(c, "as_dict") else dict(c)
                cur.execute("""
                    INSERT INTO kb_chunk(doc_id, ordinal, text, prefix, char_count, page_no,
                                         heading_path, sheet, row_from, row_to, embedding_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """, (doc_id, i, d.get("text") or "", d.get("prefix") or "",
                      int(d.get("char_count") or len(d.get("text") or "")),
                      d.get("page_no"), d.get("heading_path") or "", d.get("sheet"),
                      d.get("row_from"), d.get("row_to")))
        conn.commit()
        return len(chunks)
    finally:
        conn.close()


def list_chunks(doc_id: int, enabled_only: bool = False, limit: int = 2000) -> list[dict]:
    where = "AND enabled" if enabled_only else ""
    return query(f"SELECT {_CHUNK_COLS} FROM kb_chunk WHERE doc_id=%s {where} "
                 "ORDER BY ordinal LIMIT %s", (doc_id, limit))


def get_chunk(chunk_id: int) -> dict | None:
    rows = query(f"SELECT {_CHUNK_COLS} FROM kb_chunk WHERE id=%s", (chunk_id,))
    return rows[0] if rows else None


def update_chunk(chunk_id: int, text: str | None = None, enabled: bool | None = None,
                 prefix: str | None = None) -> bool:
    """客服手改分塊內容 / 停用。改過文字就標 `edited` 並把向量標回 pending。"""
    sets, params = [], []
    if text is not None:
        new_text = text.strip()
        if not new_text:
            return False
        sets += ["text=%s", "char_count=%s", "embedding_status='pending'", "edited=true"]
        params += [new_text, len(new_text)]
    if prefix is not None:
        sets.append("prefix=%s")
        params.append(prefix.strip())
    if enabled is not None:
        sets.append("enabled=%s")
        params.append(bool(enabled))
    if not sets:
        return False
    params.append(chunk_id)
    return execute(f"UPDATE kb_chunk SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def delete_chunk(chunk_id: int) -> bool:
    return execute("DELETE FROM kb_chunk WHERE id=%s", (chunk_id,)) > 0


def insert_chunk(doc_id: int, ordinal: int, text: str, *, prefix: str = "",
                 page_no: int | None = None, heading_path: str = "",
                 sheet: str | None = None, row_from: int | None = None,
                 row_to: int | None = None, enabled: bool = True) -> int:
    row = execute_returning("""
        INSERT INTO kb_chunk(doc_id, ordinal, text, prefix, char_count, page_no,
                             heading_path, sheet, row_from, row_to, enabled, embedding_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') RETURNING id
    """, (doc_id, ordinal, text, prefix, len(text), page_no, heading_path or "",
          sheet, row_from, row_to, enabled))
    return row[0]


def renumber_chunks(doc_id: int) -> None:
    """重排 ordinal（拆分 / 合併 / 刪除後呼叫）。"""
    execute("""
        UPDATE kb_chunk c SET ordinal = t.rn
        FROM (SELECT id, row_number() OVER (ORDER BY ordinal, id) - 1 AS rn
              FROM kb_chunk WHERE doc_id=%s) t
        WHERE c.id = t.id
    """, (doc_id,))


def shift_ordinals(doc_id: int, after_ordinal: int) -> None:
    """把 `ordinal > after_ordinal` 的塊全部 +1，騰出一個位置給新插入的塊。

    必須先做這一步再 insert，否則新塊會和後一塊同 ordinal；
    而 `renumber_chunks` 是按 (ordinal, id) 排序，舊塊 id 較小會排到新塊前面 → 順序錯亂。
    """
    execute("UPDATE kb_chunk SET ordinal = ordinal + 1 WHERE doc_id=%s AND ordinal > %s",
            (doc_id, after_ordinal))


def pending_chunks(doc_id: int | None = None, limit: int = 5000) -> list[dict]:
    """還沒向量化（或標回 pending）的塊。"""
    if doc_id:
        return query(f"SELECT {_CHUNK_COLS} FROM kb_chunk WHERE doc_id=%s "
                     "AND enabled AND embedding_status <> 'done' ORDER BY ordinal LIMIT %s",
                     (doc_id, limit))
    return query(f"SELECT {_CHUNK_COLS} FROM kb_chunk WHERE enabled "
                 "AND embedding_status <> 'done' ORDER BY doc_id, ordinal LIMIT %s", (limit,))


def save_embeddings(items: list[tuple[int, list[float]]], model: str, dim: int) -> int:
    """批次寫入向量。items = [(chunk_id, vector)]。"""
    if not items:
        return 0
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for cid, vec in items:
                cur.execute("""
                    UPDATE kb_chunk
                       SET embedding=%s, embedding_model=%s, embedding_dim=%s,
                           embedding_status='done'
                     WHERE id=%s
                """, ([float(x) for x in vec], model, dim, cid))
        conn.commit()
        return len(items)
    finally:
        conn.close()


def mark_embedding_failed(chunk_ids: list[int]) -> int:
    if not chunk_ids:
        return 0
    return execute("UPDATE kb_chunk SET embedding_status='failed' WHERE id = ANY(%s)",
                   (list(chunk_ids),))


def chunk_stats(doc_id: int | None = None) -> dict:
    where = "WHERE doc_id=%s" if doc_id else ""
    params = (doc_id,) if doc_id else ()
    rows = query(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE enabled)                        AS enabled,
               count(*) FILTER (WHERE embedding_status='done')         AS done,
               count(*) FILTER (WHERE embedding_status='pending')      AS pending,
               count(*) FILTER (WHERE embedding_status='failed')       AS failed,
               count(*) FILTER (WHERE edited)                          AS edited,
               COALESCE(sum(char_count), 0)                            AS chars
        FROM kb_chunk {where}
    """, params)
    return rows[0] if rows else {}


def load_ready_chunks(collection_id: int | None = None) -> list[dict]:
    """載入可檢索的塊（含向量），供混合檢索在記憶體算 cosine。

    只取 status='ready' 且啟用、embedding 完成的塊；知識庫也要是啟用的。
    """
    where = ["d.status='ready'", "d.enabled", "c.embedding_status='done'", "c.enabled"]
    params: list = []
    if collection_id is not None:
        where.append("d.collection_id=%s")
        params.append(collection_id)
    sql = f"""
        SELECT c.id, c.doc_id, c.ordinal, c.text, c.prefix, c.char_count, c.page_no,
               c.heading_path, c.sheet, c.row_from, c.row_to, c.embedding,
               d.title AS doc_title, d.filename AS doc_filename, d.doc_type,
               d.collection_id
        FROM kb_chunk c
        JOIN kb_document d  ON d.id = c.doc_id
        LEFT JOIN kb_collection cc ON cc.id = d.collection_id
        WHERE {' AND '.join(where)}
          AND (cc.id IS NULL OR cc.enabled)
        ORDER BY c.doc_id, c.ordinal
    """
    return query(sql, tuple(params))


# ======================================================================
# 工作佇列（kb_job）
# ======================================================================

def create_job(doc_id: int, stage: str) -> int:
    row = execute_returning(
        "INSERT INTO kb_job(doc_id, stage, status, started_at) VALUES (%s, %s, 'running', now()) "
        "RETURNING id", (doc_id, stage))
    return row[0]


def finish_job(job_id: int, ok: bool, error: str = "") -> None:
    execute("""
        UPDATE kb_job SET status=%s, last_error=%s, finished_at=now(),
                          attempts=attempts+1
        WHERE id=%s
    """, ("done" if ok else "failed", (error or "")[:2000], job_id))


def list_running_jobs() -> list[dict]:
    """服務重啟後要續跑的工作（把 running 當成中斷 → 交還呼叫端重跑）。"""
    return query("SELECT * FROM kb_job WHERE status='running' ORDER BY id")


def reset_stuck_jobs() -> int:
    """啟動時把上次沒跑完的 running 工作標成 failed（避免卡住 UI 進度條）。"""
    return execute("UPDATE kb_job SET status='failed', last_error='服務重啟中斷', "
                   "finished_at=now() WHERE status='running'")


def list_jobs(doc_id: int, limit: int = 20) -> list[dict]:
    return query("SELECT * FROM kb_job WHERE doc_id=%s ORDER BY id DESC LIMIT %s",
                 (doc_id, limit))
