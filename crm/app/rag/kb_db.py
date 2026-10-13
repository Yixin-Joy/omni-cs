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

from app.core import config

log = logging.getLogger("wechat-forwarder")

# ---------- 資料表定義（create_db.py 也呼叫這裡，保持單一真相） ----------

# ---------- （已作廢）舊 DDL ----------
# CRM 側的表一律由 `app.core.schema` 建立（crm_db 單一真相）。
# 以下舊 DDL 僅留存供比對「刪除行為 / 欄位語義」，**不執行**。
_LEGACY_DDL_UNUSED = """
-- ★ 2026-10-12：知識庫分兩型（kind）
--     'doc'   文檔知識庫：放文件（PDF/Word/…），切塊後可檢索
--     'image' 圖片知識庫：放圖片（單獨上傳 + 從文檔抽出來的），用「描述」檢索
--   文檔知識庫建立時會**自動建立**一個同名「<名>_圖片庫」（kind='image'，
--   image_of 指向該文檔庫）—— 從文件抽出來的圖一律落在那裡，且**啟停用連動**。
CREATE TABLE IF NOT EXISTS kb_collection (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    kind        VARCHAR(8) NOT NULL DEFAULT 'doc',   -- doc | image
    image_of    BIGINT,                              -- kind='image' 時：所屬文檔知識庫 id
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

-- ★ 圖片資產（2026-10-12 新增）—— 知識庫的「圖」可以是兩條來源：
--     ① 從文件抽出來的（doc_id 有值、page_no 有值）→ **一定會綁到知識塊**
--     ② 使用者在知識庫頁單獨上傳的（doc_id IS NULL）→ 只掛在知識庫底下
--   caption 就是它的「描述」，預設由人工填；有題注的文件會先把題注填進來。
--   caption_source 記來源：doc(題注) / manual(人工) / ai(按鈕生成)
--     ⚠️ manual 的不會被 AI 覆蓋（跟 kb_chunk.edited 同一個保護思路）。
CREATE TABLE IF NOT EXISTS kb_asset (
    id               BIGSERIAL PRIMARY KEY,
    collection_id    BIGINT REFERENCES kb_collection(id) ON DELETE SET NULL,
    doc_id           BIGINT REFERENCES kb_document(id) ON DELETE CASCADE,
    page_no          INTEGER,                 -- PDF 頁碼（單獨上傳的圖沒有）
    rel_path         TEXT NOT NULL,           -- 相對 media/ 的路徑
    filename         TEXT,
    mime             TEXT,
    size_bytes       INTEGER,
    width            INTEGER,
    height           INTEGER,
    sha256           TEXT,                    -- 去重（同一張圖在文件裡出現多次只存一份）
    caption          TEXT,                    -- 描述（題注 / 人工 / AI）
    caption_source   VARCHAR(10),             -- doc|manual|ai
    caption_status   VARCHAR(10) NOT NULL DEFAULT 'pending',  -- pending|done|failed
    bbox             JSONB,                   -- PDF 座標（溯源用）
    embedding        REAL[],                  -- caption 的向量（讓圖本身也能被檢索）
    embedding_status VARCHAR(8) NOT NULL DEFAULT 'pending',
    enabled          BOOLEAN NOT NULL DEFAULT true,
    filtered_reason  TEXT,                    -- 非空 = 啟發式判為裝飾圖（仍保留，可人工啟用）
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 圖片 ↔ 知識塊的綁定（多對多：一頁可能有多塊、一塊可能有多圖）
CREATE TABLE IF NOT EXISTS kb_asset_chunk (
    asset_id BIGINT NOT NULL REFERENCES kb_asset(id) ON DELETE CASCADE,
    chunk_id BIGINT NOT NULL REFERENCES kb_chunk(id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc       ON kb_chunk(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_embed     ON kb_chunk(embedding_status, enabled);
CREATE INDEX IF NOT EXISTS idx_kb_document_coll   ON kb_document(collection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_document_status ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_job_status      ON kb_job(status, id);
CREATE INDEX IF NOT EXISTS idx_kb_asset_doc       ON kb_asset(doc_id, id);
CREATE INDEX IF NOT EXISTS idx_kb_asset_coll      ON kb_asset(collection_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_asset_sha       ON kb_asset(sha256);
CREATE INDEX IF NOT EXISTS idx_kb_asset_chunk_c   ON kb_asset_chunk(chunk_id);
"""

# 舊庫升級：kb_param 原本是 `collection_id BIGINT NOT NULL` + PK(collection_id, key)，
# 沒辦法存「全域」那批（NULL）。這裡把它改成可空 + NULLS NOT DISTINCT 唯一約束 ——
# 少了 NULLS NOT DISTINCT，PG 會把每一筆 NULL 當成互不相等，同一組參數能插無限多筆。
# 兩個步驟都可重複執行（DROP NOT NULL 已是 nullable 也不會錯；DO 區塊先檢查約束在不在）。
# ⚠️ 順序不能顛倒：`ALTER COLUMN ... DROP NOT NULL` 在 PK 欄位上會直接報
#    `column "collection_id" is in a primary key`，所以必須先拆掉舊 PK。
_LEGACY_MIGRATE_UNUSED = """
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
-- ★ 2026-10-12：kb_collection 分兩型（doc | image）。舊資料一律視為文檔知識庫。
ALTER TABLE kb_collection ADD COLUMN IF NOT EXISTS kind VARCHAR(8) NOT NULL DEFAULT 'doc';
ALTER TABLE kb_collection ADD COLUMN IF NOT EXISTS image_of BIGINT;
-- ⚠️ 索引要放在 ADD COLUMN **之後**（放在 _DDL 裡會在舊庫上先撞
--    `column "kind" does not exist`）。
CREATE INDEX IF NOT EXISTS idx_kb_collection_kind ON kb_collection(kind, id);
CREATE INDEX IF NOT EXISTS idx_kb_collection_of   ON kb_collection(image_of);
"""

# 知識庫型別
KIND_DOC = "doc"          # 文檔知識庫：放文件
KIND_IMAGE = "image"      # 圖片知識庫：放圖片（用描述檢索）

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
    """建立 RAG 知識庫相關資料表。可重複執行。

    ★ CRM 版：表一律由 **`app.core.schema`**（crm_db 的單一真相）建立。
    本檔舊版自帶的 `_LEGACY_DDL_UNUSED` / `_LEGACY_MIGRATE_UNUSED` 已停用 ——
    留著會與 crm schema 打架（它建出的 `kb_param` 沒有 `id`、`value` 是 TEXT、
    `kb_collection` 沒有 `business_id`）。
    """
    from app.core import db as core_db
    core_db.init_schema_sync()


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

def list_collections(enabled_only: bool = False,
                     business_id: int | None = None) -> list[dict]:
    """列出知識庫，附文件數 / 塊數 / 圖片數統計（概覽頁用）。

    `kind` 決定它是文檔庫還是圖片庫；圖片庫（kind='image'）的分數看 `asset_count`。

    ★ CRM 側：`kb_collection` 有 `business_id`（NOT NULL）—— 傳 `business_id` 只列該商家的；
      不傳（None）= 列全部（僅供內部／遷移腳本用，前端一律要傳）。
    """
    conds: list[str] = []
    params: list = []
    if enabled_only:
        conds.append("c.enabled")
    if business_id is not None:
        conds.append("c.business_id=%s")
        params.append(int(business_id))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return query(f"""
        SELECT c.*,
               COALESCE(d.doc_count, 0)   AS doc_count,
               COALESCE(d.ready_count, 0) AS ready_count,
               COALESCE(d.chunk_count, 0) AS chunk_count,
               -- 文檔庫的圖片數＝**它自己的圖片庫**裡的圖（圖都落在那裡）；
               -- 圖片庫就是自己那批。
               COALESCE(a.asset_count, 0) + COALESCE(ia.asset_count, 0) AS asset_count,
               COALESCE(ia.asset_count, 0) AS image_lib_count
        FROM kb_collection c
        LEFT JOIN (
            SELECT collection_id,
                   count(*)                                                   AS doc_count,
                   count(*) FILTER (WHERE status = 'ready')                    AS ready_count,
                   COALESCE(sum(chunk_count), 0)                              AS chunk_count
            FROM kb_document GROUP BY collection_id
        ) d ON d.collection_id = c.id
        LEFT JOIN (
            SELECT collection_id, count(*) AS asset_count
            FROM kb_asset GROUP BY collection_id
        ) a ON a.collection_id = c.id
        LEFT JOIN (
            SELECT lib.image_of, count(*) AS asset_count
            FROM kb_asset x
            JOIN kb_collection lib ON lib.id = x.collection_id
            WHERE lib.image_of IS NOT NULL
            GROUP BY lib.image_of
        ) ia ON ia.image_of = c.id
        {where}
        ORDER BY c.sort_order, c.id
    """, tuple(params))


def get_collection(cid: int) -> dict | None:
    rows = query("SELECT * FROM kb_collection WHERE id=%s", (cid,))
    return rows[0] if rows else None


def create_collection(name: str, description: str = "", *, business_id: int,
                      kind: str = KIND_DOC, image_of: int | None = None) -> dict:
    """新增知識庫。`kind='image'` = 圖片知識庫（放圖、用描述檢索）。

    ★ CRM 側 `kb_collection.business_id` 是 NOT NULL → `business_id` 必填。

    ⚠️ 文檔知識庫建立時**不會**在這裡自動建圖片庫 —— 由 `ensure_image_collection()`
    負責（可重複呼叫），這樣「補救舊資料」也能用同一個入口。
    """
    k = KIND_IMAGE if str(kind or "").lower() == KIND_IMAGE else KIND_DOC
    row = execute_returning(
        "INSERT INTO kb_collection(business_id, name, description, kind, image_of) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (int(business_id), name.strip(), description.strip(), k,
         image_of if k == KIND_IMAGE else None),
    )
    # ★ 上傳即進「基準啟用集」（設計案 §9.4）—— 沒這一步，新建的庫不會被檢索到
    #   （`load_ready_chunks(business_id=…)` 是走 business_kb_binding 篩的）。
    #   已存在就不動，避免覆蓋人工的取消勾選。
    execute("INSERT INTO business_kb_binding (business_id, kb_collection_id, enabled)"
            " VALUES (%s, %s, true)"
            " ON CONFLICT (business_id, kb_collection_id) DO NOTHING",
            (int(business_id), int(row[0])))
    return get_collection(row[0]) or {}


def get_image_collection(doc_cid: int) -> dict | None:
    """文檔知識庫 → 它的圖片庫（kind='image' 且 image_of 指向它）。"""
    if not doc_cid:
        return None
    rows = query("SELECT * FROM kb_collection WHERE kind=%s AND image_of=%s "
                 "ORDER BY id LIMIT 1", (KIND_IMAGE, int(doc_cid)))
    return rows[0] if rows else None


def ensure_image_collection(doc_cid: int, *, create: bool = True) -> dict | None:
    """取得（必要時建立）某個文檔知識庫的「<名稱>_圖片庫」。

    回傳圖片庫那筆 collection；`doc_cid` 不存在或它是圖片庫本身 → 回 None。
    """
    if not doc_cid:
        return None
    doc = get_collection(int(doc_cid))
    if not doc or (doc.get("kind") or KIND_DOC) != KIND_DOC:
        return None
    got = get_image_collection(int(doc_cid))
    if got or not create:
        return got
    name = f"{doc.get('name') or ('庫' + str(doc_cid))}_圖片庫"
    return create_collection(name, f"自動建立：{doc.get('name')} 抽出／上傳的圖片",
                             business_id=int(doc.get("business_id")),
                             kind=KIND_IMAGE, image_of=int(doc_cid))


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


def set_collection_enabled(cid: int, enabled: bool) -> dict:
    """啟用／停用知識庫。**文檔庫停用時，它的圖片庫一併連動**（設計上的綁定）。

    回傳 {"ok", "cascaded": [受影響的圖片庫 id]}。
    """
    cid = int(cid)
    enabled = bool(enabled)
    update_collection(cid, {"enabled": enabled})
    cascaded: list[int] = []
    got = get_image_collection(cid)
    if got:
        update_collection(int(got["id"]), {"enabled": enabled})
        cascaded.append(int(got["id"]))
    return {"ok": True, "cascaded": cascaded}


def delete_collection(cid: int) -> bool:
    """刪知識庫。文件不會被連帶刪除（FK 是 SET NULL），只是變成「未分組」。

    ⚠️ 刪**文檔庫**時會連帶刪掉它的圖片庫（圖片庫是它的一部分）；
    圖片本身同樣不會被刪（`kb_asset.collection_id` 是 SET NULL）→ 變成未分組圖片。
    """
    cid = int(cid)
    got = get_image_collection(cid)
    gone = [cid] + ([int(got["id"])] if got else [])
    # ⚠️ 順序不能顛倒：`business_kb_binding` / `post_kb_binding` 對 kb_collection 的外鍵
    #    沒有 CASCADE（設計案的表），**必須先清綁定再刪庫**，否則會撞 FK。
    execute("DELETE FROM business_kb_binding WHERE kb_collection_id = ANY(%s)", (gone,))
    execute("DELETE FROM post_kb_binding WHERE kb_collection_id = ANY(%s)", (gone,))
    if got:
        execute("DELETE FROM kb_collection WHERE id=%s", (int(got["id"]),))
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
    from app.rag import rag_config

    spec = rag_config.PARAM_SPECS.get(name) or {}
    return "collection" if spec.get("group") == "chunk" else "global"


def _param_target(name: str, cid: int | None) -> int | None:
    """這個參數該落在哪一行：檢索參數恆為 None（全域），分塊參數為該知識庫。"""
    return None if _param_scope(name) == "global" else cid


def get_param_overrides(cid: int | None = None, *, scope: str | None = None) -> dict:
    """取覆寫值。`scope='global'` → 讀全域那批；否則讀「這個知識庫」那批。

    ⚠️ CRM 側 `kb_param.value` 是 **jsonb**（舊庫是 TEXT），psycopg2 會把它解成
    Python 原生值（int / bool / str），所以回傳的是 `dict[str, Any]` 而不是
    `dict[str, str]`。下游一律走 `rag_config.coerce()`，兩邊都吃得下。
    """
    if scope == "global" or (scope is None and not cid):
        rows = query("SELECT key, value FROM kb_param WHERE collection_id IS NULL")
    else:
        rows = query("SELECT key, value FROM kb_param WHERE collection_id=%s", (cid,))
    return {r["key"]: r["value"] for r in rows}


def get_global_param_overrides() -> dict[str, str]:
    """全域那批覆寫（檢索參數 + 未分組文件的分塊預設）。"""
    return get_param_overrides(None, scope="global")


def _param_json_text(raw) -> str:
    """把參數值轉成能寫進 `kb_param.value`（**jsonb**）的 JSON 文字。

    ⚠️ 這欄在舊庫是 TEXT、寫入端直接 `str(raw)`；搬進 CRM 後型別變 jsonb，
    `'auto'`（chunk_strategy）與整段提示詞這種**不是合法 JSON 的字面量**會當場
    `InvalidTextRepresentation: invalid input syntax for type json`。

    規則：先當 JSON 解析（讓 `'600'` → 數字、`'true'` → 布林，維持舊行為），
    解析不了就當純字串、用 `json.dumps` 包成 JSON 字串。
    """
    if raw is None or isinstance(raw, bool) or isinstance(raw, (int, float)):
        return json.dumps(raw)
    s = str(raw)
    try:
        return json.dumps(json.loads(s), ensure_ascii=False)
    except (ValueError, TypeError):
        return json.dumps(s, ensure_ascii=False)


def set_params(cid: int | None, values: dict) -> int:
    """寫入/覆寫參數。值為 None / 空字串 = 刪除覆寫（回到 .env 預設）。

    ⚠️ 檢索參數一律寫到**全域**，所以 `set_params(某個庫, {"top_k": 5})` 改的是
    所有知識庫共用的那個值（這是刻意的：檢索參數不分庫）。
    """
    from app.rag import rag_config

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
        val = _param_json_text(raw)
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
    from app.rag import rag_config

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
    from app.rag import rag_config

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


def load_ready_chunks(collection_id: int | None = None,
                      business_id: int | None = None,
                      collection_ids: list[int] | None = None) -> list[dict]:
    """載入可檢索的塊（含向量），供混合檢索在記憶體算 cosine。

    只取 status='ready' 且啟用、embedding 完成的塊；知識庫也要是啟用的。

    ★ CRM 側：傳 `business_id` 只取**該商家基準啟用集**（`business_kb_binding`）裡的知識庫。
      未分組文件（`collection_id IS NULL`）不屬於任何商家 → 不會被撈到。
      不傳（None）= 全部（僅內部／除錯用）。
    ★ 傳 `collection_ids`（貼文覆寫集）→ 只取這幾個知識庫，且**空清單回空**
      （空集 = 不跑，設計案 §9.2）；此時忽略 `business_id`。
    """
    if collection_ids is not None and not collection_ids:
        return []
    where = ["d.status='ready'", "d.enabled", "c.embedding_status='done'", "c.enabled"]
    params: list = []
    if collection_ids is not None:
        where.append("d.collection_id = ANY(%s)")
        params.append([int(i) for i in collection_ids])
    elif collection_id is not None:
        where.append("d.collection_id=%s")
        params.append(collection_id)
    elif business_id is not None:
        where.append(
            "d.collection_id IN (SELECT kb_collection_id FROM business_kb_binding"
            " WHERE business_id=%s AND enabled)")
        params.append(int(business_id))
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
# 圖片資產（kb_asset / kb_asset_chunk）
#
# 兩條來源：
#   ① 從文件抽出來的（doc_id 有值）→ **一定會綁到知識塊**（見 bind_assets_for_doc）
#   ② 使用者在知識庫頁單獨上傳的（doc_id IS NULL）→ 只掛在知識庫底下
# caption 的來源記在 caption_source：doc(題注) / manual(人工) / ai(按鈕生成)。
# ⚠️ manual 的不會被 AI 覆蓋 —— 跟 kb_chunk.edited 同一個保護思路。
# ======================================================================

_ASSET_COLS = """id, collection_id, doc_id, page_no, rel_path, filename, mime,
                 size_bytes, width, height, sha256, caption, caption_source,
                 caption_status, bbox, embedding_status, enabled, filtered_reason,
                 created_at"""


def create_asset(*, rel_path: str, collection_id: int | None = None,
                 doc_id: int | None = None, page_no: int | None = None,
                 filename: str = "", mime: str = "", size_bytes: int = 0,
                 width: int = 0, height: int = 0, sha256: str = "",
                 caption: str = "", caption_source: str = "", bbox: dict | None = None,
                 enabled: bool = True, filtered_reason: str = "") -> dict:
    """新增一張圖片。caption 有值就直接標成 done（題注的情況）。

    `caption_source` 沒給但有 caption → 當成 'doc'（唯一會在建立時就帶著描述的
    就是文件裡的題注；人工／AI 都是事後才寫）。
    """
    has_cap = bool((caption or "").strip())
    src = (caption_source or ("doc" if has_cap else "")) or None
    row = execute_returning(f"""
        INSERT INTO kb_asset(collection_id, doc_id, page_no, rel_path, filename, mime,
                             size_bytes, width, height, sha256, caption, caption_source,
                             caption_status, bbox, enabled, filtered_reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
        RETURNING id
    """, (collection_id, doc_id, page_no, rel_path, filename, mime, size_bytes,
          width, height, sha256, (caption or "").strip() or None,
          src, "done" if has_cap else "pending",
          json.dumps(bbox, ensure_ascii=False) if bbox else None,
          enabled, filtered_reason or None))
    return get_asset(row[0])


def get_asset(aid: int) -> dict | None:
    rows = query(f"SELECT {_ASSET_COLS} FROM kb_asset WHERE id=%s", (aid,))
    return rows[0] if rows else None


def list_assets(*, collection_id: int | None = None, doc_id: int | None = None,
                enabled_only: bool = False, include_filtered: bool = True,
                ungrouped: bool = False, limit: int = 500) -> list[dict]:
    """列圖片。`include_filtered=False` = 只看「像內容圖」的那些。"""
    where, params = [], []
    if doc_id is not None:
        where.append("a.doc_id=%s")
        params.append(doc_id)
    if ungrouped:
        where.append("a.collection_id IS NULL")
    elif collection_id is not None:
        where.append("a.collection_id=%s")
        params.append(collection_id)
    if enabled_only:
        where.append("a.enabled")
    if not include_filtered:
        where.append("(a.filtered_reason IS NULL OR a.filtered_reason='')")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT a.id, a.collection_id, a.doc_id, a.page_no, a.rel_path, a.filename,
               a.mime, a.size_bytes, a.width, a.height, a.sha256, a.caption,
               a.caption_source, a.caption_status, a.bbox, a.embedding_status,
               a.enabled, a.filtered_reason, a.created_at,
               d.filename AS doc_filename, d.title AS doc_title,
               COALESCE((SELECT count(*) FROM kb_asset_chunk ac WHERE ac.asset_id = a.id), 0)
                   AS chunk_count
        FROM kb_asset a
        LEFT JOIN kb_document d ON d.id = a.doc_id
        {clause}
        ORDER BY a.doc_id NULLS LAST, a.page_no NULLS LAST, a.id
        LIMIT %s
    """
    return query(sql, tuple(params + [max(1, int(limit))]))


def update_asset(aid: int, **fields) -> bool:
    # ⚠️ 少了 rel_path 會讓「搬檔後改路徑」靜默失敗（踩過：遷移腳本以為改了，
    #    其實 allowed 沒收，資料庫還是舊路徑）。
    allowed = ("caption", "caption_source", "caption_status", "enabled",
               "filtered_reason", "collection_id", "embedding",
               "embedding_status", "page_no", "rel_path", "filename")
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=%s")
        params.append(v)
    if not sets:
        return False
    params.append(aid)
    return execute(f"UPDATE kb_asset SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def set_asset_caption(aid: int, caption: str, source: str = "manual") -> dict | None:
    """寫描述。source 記來源；空描述 = 清掉（回到待填）。"""
    cap = (caption or "").strip()
    execute("""
        UPDATE kb_asset SET caption=%s, caption_source=%s, caption_status=%s,
                            embedding=NULL, embedding_status='pending'
        WHERE id=%s
    """, (cap or None, (source or "manual") if cap else None,
          "done" if cap else "pending", aid))
    return get_asset(aid)


def delete_asset(aid: int) -> bool:
    return execute("DELETE FROM kb_asset WHERE id=%s", (aid,)) > 0


def delete_assets_for_doc(doc_id: int) -> int:
    """刪文件時連帶清圖片（FK 也會 cascade，這裡是為了能回報數量）。"""
    return execute("DELETE FROM kb_asset WHERE doc_id=%s", (doc_id,))


def find_asset_by_sha(sha256: str, *, doc_id: int | None = None,
                      collection_id: int | None = None) -> dict | None:
    """同一張圖是否已經存過（去重）。"""
    if not sha256:
        return None
    where = ["sha256=%s"]
    params: list = [sha256]
    if doc_id is not None:
        where.append("doc_id=%s")
        params.append(doc_id)
    if collection_id is not None:
        where.append("collection_id=%s")
        params.append(collection_id)
    rows = query(f"SELECT {_ASSET_COLS} FROM kb_asset WHERE {' AND '.join(where)} "
                 f"ORDER BY id LIMIT 1", tuple(params))
    return rows[0] if rows else None


def bind_asset_chunks(asset_id: int, chunk_ids: list[int]) -> int:
    """把圖綁到知識塊（多對多，重複綁不會報錯）。"""
    ids = sorted({int(c) for c in (chunk_ids or [])})
    if not ids:
        return 0
    executemany("INSERT INTO kb_asset_chunk(asset_id, chunk_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING", [(asset_id, c) for c in ids])
    return len(ids)


def clear_asset_bindings(asset_id: int) -> int:
    return execute("DELETE FROM kb_asset_chunk WHERE asset_id=%s", (asset_id,))


def clear_doc_bindings(doc_id: int) -> int:
    """重跑分塊前先清掉這份文件所有圖的綁定（chunk_id 會整批換掉）。"""
    return execute("""
        DELETE FROM kb_asset_chunk WHERE asset_id IN
            (SELECT id FROM kb_asset WHERE doc_id=%s)
    """, (doc_id,))


def assets_for_chunks(chunk_ids: list[int]) -> dict[int, list[dict]]:
    """`chunk_id → [圖片]`（檢索命中時把圖帶出來用）。

    只回**啟用**的圖；被啟發式判為裝飾的預設不帶出來（`enabled` 由入庫時決定）。
    """
    ids = sorted({int(c) for c in (chunk_ids or [])})
    if not ids:
        return {}
    rows = query("""
        SELECT ac.chunk_id, a.id, a.rel_path, a.filename, a.mime, a.caption,
               a.caption_source, a.page_no, a.width, a.height, a.doc_id,
               a.filtered_reason, a.enabled
        FROM kb_asset_chunk ac
        JOIN kb_asset a ON a.id = ac.asset_id
        WHERE ac.chunk_id = ANY(%s) AND a.enabled
        ORDER BY ac.chunk_id, a.id
    """, (ids,))
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["chunk_id"]), []).append(r)
    return out


def asset_stats(*, collection_id: int | None = None, doc_id: int | None = None) -> dict:
    where, params = [], []
    if doc_id is not None:
        where.append("doc_id=%s")
        params.append(doc_id)
    if collection_id is not None:
        where.append("collection_id=%s")
        params.append(collection_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = query(f"""
        SELECT count(*)                                                   AS total,
               count(*) FILTER (WHERE enabled)                            AS enabled,
               count(*) FILTER (WHERE caption IS NOT NULL)                AS with_caption,
               count(*) FILTER (WHERE caption_source='doc')               AS from_doc,
               count(*) FILTER (WHERE caption_source='manual')            AS manual,
               count(*) FILTER (WHERE caption_source='ai')                AS ai,
               count(*) FILTER (WHERE filtered_reason IS NOT NULL
                                  AND filtered_reason <> '')              AS filtered,
               count(*) FILTER (WHERE doc_id IS NOT NULL)                 AS from_docs,
               count(*) FILTER (WHERE doc_id IS NULL)                     AS uploaded
        FROM kb_asset {clause}
    """, tuple(params))
    return rows[0] if rows else {}


def pending_asset_captions(limit: int = 50, doc_id: int | None = None) -> list[dict]:
    """還沒描述、且沒被過濾掉的圖（AI 批次補描述用）。

    ⚠️ **不會**挑 caption_source='manual' 的 —— 人工寫過的不重生成。
    """
    where = ["enabled", "caption IS NULL",
             "(filtered_reason IS NULL OR filtered_reason='')"]
    params: list = []
    if doc_id is not None:
        where.append("doc_id=%s")
        params.append(doc_id)
    rows = query(f"SELECT {_ASSET_COLS} FROM kb_asset WHERE {' AND '.join(where)} "
                 f"ORDER BY doc_id, page_no NULLS LAST, id LIMIT %s",
                 tuple(params + [max(1, int(limit))]))
    return rows


def pending_asset_embeddings(limit: int = 200, doc_id: int | None = None,
                             collection_id: int | None = None) -> list[dict]:
    """還沒向量化的圖片（**有描述**的那些）。

    ★ 這是「圖片庫能被檢索」的前提 —— 以前完全沒有這一步，
    所以圖片庫的圖永遠 `embedding_status='pending'`、永遠搜不到。
    只挑有 caption 的：沒有描述就沒有可檢索文字。
    """
    where = ["enabled", "caption IS NOT NULL", "caption <> ''",
             "embedding_status <> 'done'",
             "(filtered_reason IS NULL OR filtered_reason='')"]
    params: list = []
    if doc_id is not None:
        where.append("doc_id=%s")
        params.append(doc_id)
    if collection_id is not None:
        where.append("collection_id=%s")
        params.append(collection_id)
    rows = query(f"SELECT {_ASSET_COLS} FROM kb_asset WHERE {' AND '.join(where)} "
                 f"ORDER BY id LIMIT %s", tuple(params + [max(1, int(limit))]))
    return rows


def save_asset_embeddings(items: list[tuple[int, list[float]]], model: str, dim: int) -> int:
    n = 0
    for aid, vec in items or []:
        n += execute("""
            UPDATE kb_asset SET embedding=%s, embedding_status='done' WHERE id=%s
        """, (list(vec), int(aid)))
    return n


def load_ready_assets(collection_id: int | None = None,
                      business_id: int | None = None,
                      collection_ids: list[int] | None = None) -> list[dict]:
    """可檢索的圖（有描述、有向量、已啟用）。

    讓「這張圖表在講什麼」這種問題查得到 —— caption 就是它的可檢索文字。
    傳 `business_id` 只取該商家基準啟用集（`business_kb_binding`）裡的圖片庫。
    傳 `collection_ids`（貼文覆寫集）→ 只取這幾個；**空清單回空**（§9.2）。
    """
    if collection_ids is not None and not collection_ids:
        return []
    where = ["a.enabled", "a.embedding_status='done'", "a.caption IS NOT NULL"]
    params: list = []
    if collection_ids is not None:
        where.append("a.collection_id = ANY(%s)")
        params.append([int(i) for i in collection_ids])
    elif collection_id is not None:
        where.append("a.collection_id=%s")
        params.append(collection_id)
    elif business_id is not None:
        where.append(
            "a.collection_id IN (SELECT kb_collection_id FROM business_kb_binding"
            " WHERE business_id=%s AND enabled)")
        params.append(int(business_id))
    sql = f"""
        SELECT a.id, a.doc_id, a.collection_id, a.page_no, a.rel_path, a.filename,
               a.mime, a.caption, a.caption_source, a.width, a.height, a.embedding,
               d.title AS doc_title, d.filename AS doc_filename, d.doc_type
        FROM kb_asset a
        LEFT JOIN kb_document d ON d.id = a.doc_id
        LEFT JOIN kb_collection cc ON cc.id = a.collection_id
        WHERE {' AND '.join(where)}
          AND (cc.id IS NULL OR cc.enabled)
        ORDER BY a.id
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
