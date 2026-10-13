"""CRM DB（`crm_db`）的完整建表 SQL —— 設計案 §5 的落地版本。

單一真相：`init_crm_db.py`（建庫 + 首次建表 + seed）與 CRM 服務啟動時都呼叫這裡。
全部 `IF NOT EXISTS`，可重複執行。
"""
from __future__ import annotations

DDL = """
-- ============================================================
-- 5.1 商家與平台賬號
-- ============================================================
CREATE TABLE IF NOT EXISTS crm_business (
    id            bigserial PRIMARY KEY,
    business_name text NOT NULL,
    business_memo text,
    enabled       boolean DEFAULT true,
    created_at    timestamptz DEFAULT now(),
    updated_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crm_business_platform_account (
    id                            bigserial PRIMARY KEY,
    business_id                   bigint REFERENCES crm_business(id),
    platform                      text NOT NULL,
    platform_business_account_id  text NOT NULL,
    display_name                  text,
    ai_auto_reply_enabled         boolean DEFAULT true,
    enabled                       boolean DEFAULT true,
    created_at                    timestamptz DEFAULT now(),
    UNIQUE (platform, platform_business_account_id)
);
CREATE INDEX IF NOT EXISTS idx_bp_account_business ON crm_business_platform_account(business_id, enabled);

CREATE TABLE IF NOT EXISTS business_ai_switch (
    id         bigserial PRIMARY KEY,
    business_id bigint REFERENCES crm_business(id),
    platform   text NOT NULL,
    scope      text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    updated_at timestamptz DEFAULT now(),
    UNIQUE (business_id, platform, scope)
);

-- 層級開關（FAQ / 業務表格 / RAG 三層，按商家）。
-- 舊系統這三個是全域鍵（autoreply_setting 的 faq_enabled/table_enabled/rag_enabled）
-- → 拆庫後按商家存；**無此列 = 啟用**（與舊預設一致）。
CREATE TABLE IF NOT EXISTS business_layer_switch (
    id          bigserial PRIMARY KEY,
    business_id bigint REFERENCES crm_business(id),
    layer       text NOT NULL,          -- 'faq' | 'table' | 'rag'
    enabled     boolean NOT NULL DEFAULT true,
    updated_at  timestamptz DEFAULT now(),
    UNIQUE (business_id, layer)
);

-- ============================================================
-- 5.2 員工與權限
-- ============================================================
CREATE TABLE IF NOT EXISTS crm_staff (
    id             bigserial PRIMARY KEY,
    login_account  text UNIQUE NOT NULL,
    password_hash  text NOT NULL,
    staff_name     text NOT NULL,
    roles          text[] NOT NULL,
    is_super_agent boolean DEFAULT false,
    is_kb_editor   boolean DEFAULT false,
    enabled        boolean DEFAULT true,
    created_at     timestamptz DEFAULT now(),
    updated_at     timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crm_staff_business_permission (
    id          bigserial PRIMARY KEY,
    staff_id    bigint REFERENCES crm_staff(id),
    business_id bigint REFERENCES crm_business(id),
    created_at  timestamptz DEFAULT now(),
    UNIQUE (staff_id, business_id)
);
CREATE INDEX IF NOT EXISTS idx_staff_perm_staff ON crm_staff_business_permission(staff_id);

CREATE TABLE IF NOT EXISTS crm_session (
    session_id     text PRIMARY KEY,
    staff_id       bigint REFERENCES crm_staff(id),
    roles          text[],          -- 登入時的角色全量快照（可用身分清單）
    active_role    text,            -- 當前登入身分；一次只生效一個
    is_super_agent boolean,
    is_kb_editor   boolean,
    csrf_token     text NOT NULL,
    created_at     timestamptz DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    last_seen_at   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_session_staff ON crm_session(staff_id);
CREATE INDEX IF NOT EXISTS idx_crm_session_expires ON crm_session(expires_at);

-- ============================================================
-- 5.3 任務與佇列
-- ============================================================
CREATE TABLE IF NOT EXISTS crm_outbound_task (
    id                           bigserial PRIMARY KEY,
    business_id                  bigint REFERENCES crm_business(id),
    staff_id                     bigint REFERENCES crm_staff(id),
    source                       text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','ai')),
    platform                     text NOT NULL,
    business_platform_account_id bigint REFERENCES crm_business_platform_account(id),
    end_user_sender_id           text NOT NULL,
    message_content              jsonb NOT NULL,
    status                       text NOT NULL CHECK (status IN ('pending','processing','sent','failed','uncertain')),
    is_temp_error                boolean,
    retry_count                  int DEFAULT 0,
    max_retry                    int NOT NULL DEFAULT 3,
    backoff_seconds              int NOT NULL DEFAULT 1,
    fail_reason                  text,
    processing_at                timestamptz,
    created_at                   timestamptz DEFAULT now(),
    next_retry_at                timestamptz,
    finished_at                  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_outbound_task_status ON crm_outbound_task(status, created_at);
CREATE INDEX IF NOT EXISTS idx_outbound_task_business ON crm_outbound_task(business_id, status);

CREATE TABLE IF NOT EXISTS crm_ai_inbound_queue (
    id                           bigserial PRIMARY KEY,
    message_id                   text NOT NULL,
    business_id                  bigint NOT NULL,
    platform                     text NOT NULL,
    business_platform_account_id bigint NOT NULL,
    end_user_sender_id           text NOT NULL,
    content                      jsonb NOT NULL,
    status                       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','failed')),
    retry_count                  int DEFAULT 0,
    fail_reason                  text,
    created_at                   timestamptz DEFAULT now(),
    claimed_at                   timestamptz,
    processed_at                 timestamptz,
    UNIQUE (message_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_queue_status ON crm_ai_inbound_queue(status, created_at);

-- ============================================================
-- 5.4 審計與運維
-- ============================================================
CREATE TABLE IF NOT EXISTS crm_audit_log (
    id                           bigserial PRIMARY KEY,
    business_id                  bigint,
    staff_id                     bigint,
    staff_name                   text,
    staff_roles                  text[],
    operate_type                 text NOT NULL,
    platform                     text,
    business_platform_account_id bigint,
    end_user_sender_id           text,
    payload                      jsonb,
    operate_at                   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_business ON crm_audit_log(business_id, operate_at);
CREATE INDEX IF NOT EXISTS idx_audit_staff ON crm_audit_log(staff_id, operate_at);
CREATE INDEX IF NOT EXISTS idx_audit_type ON crm_audit_log(operate_type, operate_at);

CREATE TABLE IF NOT EXISTS crm_ops_config (
    id           bigserial PRIMARY KEY,
    config_key   text UNIQUE NOT NULL,
    config_value jsonb NOT NULL,
    updated_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS crm_alert (
    id              bigserial PRIMARY KEY,
    alert_type      text NOT NULL,
    severity        text NOT NULL,
    title           text NOT NULL,
    detail          jsonb,
    first_seen_at   timestamptz DEFAULT now(),
    last_seen_at    timestamptz DEFAULT now(),
    count           int DEFAULT 1,
    acknowledged    boolean DEFAULT false,
    acknowledged_by bigint,
    acknowledged_at timestamptz,
    notified_at     timestamptz      -- 已寄出告警郵件的時間（NULL = 還沒通知過）
);
CREATE INDEX IF NOT EXISTS idx_alert_open ON crm_alert(acknowledged, last_seen_at);

-- ============================================================
-- 5.5 知識庫與 AI 資料
-- ============================================================
CREATE TABLE IF NOT EXISTS kb_collection (
    id          bigserial PRIMARY KEY,
    business_id bigint NOT NULL REFERENCES crm_business(id),
    name        text NOT NULL,
    description text,
    kind        text NOT NULL CHECK (kind IN ('doc','image')),
    image_of    bigint,
    enabled     boolean DEFAULT true,
    sort_order  int DEFAULT 0,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_collection_business ON kb_collection(business_id, enabled);

CREATE TABLE IF NOT EXISTS kb_document (
    id            bigserial PRIMARY KEY,
    collection_id bigint REFERENCES kb_collection(id) ON DELETE SET NULL,
    title         text, filename text, mime text, size_bytes bigint, sha256 text,
    source_path   text, doc_type text, version int DEFAULT 1, superseded_by bigint,
    status        text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','parsing','chunking','embedding','ready','failed','disabled')),
    progress      int DEFAULT 0, chunk_count int DEFAULT 0, char_count int DEFAULT 0,
    parse_meta    jsonb, parse_params jsonb, error text,
    enabled       boolean DEFAULT true,
    created_at    timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_document_collection ON kb_document(collection_id, status);

CREATE TABLE IF NOT EXISTS kb_chunk (
    id               bigserial PRIMARY KEY,
    doc_id           bigint REFERENCES kb_document(id) ON DELETE CASCADE,
    ordinal          int NOT NULL, text text NOT NULL, prefix text, char_count int,
    page_no          int, heading_path text, sheet text, row_from int, row_to int,
    embedding        real[], embedding_model text, embedding_dim int, embedding_status text,
    edited           boolean DEFAULT false, enabled boolean DEFAULT true,
    created_at       timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_doc ON kb_chunk(doc_id, ordinal);

CREATE TABLE IF NOT EXISTS kb_job (
    id         bigserial PRIMARY KEY,
    doc_id     bigint REFERENCES kb_document(id) ON DELETE CASCADE,
    stage      text NOT NULL, status text NOT NULL, attempts int DEFAULT 0,
    last_error text,
    started_at  timestamptz,
    finished_at timestamptz,
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_param (
    id            bigserial PRIMARY KEY,
    collection_id bigint REFERENCES kb_collection(id) ON DELETE CASCADE,
    key           text NOT NULL, value jsonb NOT NULL,
    updated_at    timestamptz DEFAULT now(),
    -- ★ 具名約束：`kb_db.set_params()` 用 `ON CONFLICT ON CONSTRAINT kb_param_scope_key`
    --   （與舊庫同名）。匿名 UNIQUE 的名字是 `kb_param_collection_id_key_key`，對不上。
    --   `NULLS NOT DISTINCT` 不可省 —— 否則每筆 NULL 都算互不相等，「全域」那批能插無限多筆。
    CONSTRAINT kb_param_scope_key UNIQUE NULLS NOT DISTINCT (collection_id, key)
);

CREATE TABLE IF NOT EXISTS kb_asset (
    id              bigserial PRIMARY KEY,
    collection_id   bigint REFERENCES kb_collection(id) ON DELETE SET NULL,
    doc_id          bigint REFERENCES kb_document(id) ON DELETE CASCADE,
    page_no         int, rel_path text, filename text, mime text, size_bytes bigint,
    width           int, height int, sha256 text,
    caption         text, caption_source text CHECK (caption_source IN ('doc','manual','ai')),
    caption_status  text, bbox jsonb,
    embedding       real[], embedding_status text,
    enabled         boolean DEFAULT true, filtered_reason text,
    created_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_asset_collection ON kb_asset(collection_id, enabled);

CREATE TABLE IF NOT EXISTS kb_asset_chunk (
    asset_id bigint REFERENCES kb_asset(id) ON DELETE CASCADE,
    chunk_id bigint REFERENCES kb_chunk(id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, chunk_id)
);

-- ============================================================
-- 5.6 FAQ、業務表格、AI 紀錄
-- ============================================================
CREATE TABLE IF NOT EXISTS autoreply_faq (
    id          bigserial PRIMARY KEY,
    business_id bigint NOT NULL REFERENCES crm_business(id),
    question    text NOT NULL, answer text NOT NULL, keywords text,
    enabled     boolean DEFAULT true, hit_count int DEFAULT 0, sort_order int DEFAULT 0,
    created_at  timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_faq_business ON autoreply_faq(business_id, enabled);

CREATE TABLE IF NOT EXISTS autoreply_business_table (
    id          bigserial PRIMARY KEY,
    business_id bigint NOT NULL REFERENCES crm_business(id),
    name        text NOT NULL, description text, columns jsonb,
    enabled     boolean DEFAULT true, sort_order int DEFAULT 0,
    created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_table_business ON autoreply_business_table(business_id, enabled);

CREATE TABLE IF NOT EXISTS autoreply_business_row (
    id         bigserial PRIMARY KEY,
    table_id   bigint REFERENCES autoreply_business_table(id),
    data       jsonb NOT NULL, enabled boolean DEFAULT true, sort_order int DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_business_row_table ON autoreply_business_row(table_id, sort_order);

CREATE TABLE IF NOT EXISTS autoreply_log (
    id                           bigserial PRIMARY KEY,
    business_id                  bigint NOT NULL,
    platform                     text,
    scope                        text,                              -- dm | comment
    business_platform_account_id bigint, end_user_sender_id text,
    message_id                   text,
    tier                         text NOT NULL CHECK (tier IN ('faq','table','rag','miss','claim','error')),
    outbound_task_id             bigint REFERENCES crm_outbound_task(id),
    question                     text, answer text,
    score                        real, detail text, matched_rows jsonb,
    rag_threshold                real, rag_candidates int, rag_queries jsonb,
    rewrote                      boolean, rewritten_query text, history_used boolean,
    send_status                  text,
    elapsed_ms                   int, llm_calls int, llm_seconds real,
    claim_key                    text,
    created_at                   timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_business ON autoreply_log(business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_tier ON autoreply_log(tier, created_at);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_task ON autoreply_log(outbound_task_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_log_claim ON autoreply_log(claim_key);

-- ============================================================
-- 5.7 基準啟用集與貼文配置
-- ============================================================
CREATE TABLE IF NOT EXISTS business_kb_binding (
    id               bigserial PRIMARY KEY,
    business_id      bigint REFERENCES crm_business(id),
    kb_collection_id bigint REFERENCES kb_collection(id),
    enabled          boolean DEFAULT true,
    UNIQUE (business_id, kb_collection_id)
);

CREATE TABLE IF NOT EXISTS business_table_binding (
    id                bigserial PRIMARY KEY,
    business_id       bigint REFERENCES crm_business(id),
    business_table_id bigint REFERENCES autoreply_business_table(id),
    enabled           boolean DEFAULT true,
    UNIQUE (business_id, business_table_id)
);

CREATE TABLE IF NOT EXISTS business_faq_binding (
    id          bigserial PRIMARY KEY,
    business_id bigint REFERENCES crm_business(id),
    faq_id      bigint REFERENCES autoreply_faq(id),
    enabled     boolean DEFAULT true,
    UNIQUE (business_id, faq_id)
);

CREATE TABLE IF NOT EXISTS post_ai_config (
    id                           bigserial PRIMARY KEY,
    platform                     text NOT NULL,
    post_id                      text NOT NULL,
    business_platform_account_id bigint REFERENCES crm_business_platform_account(id),
    enabled                      boolean NOT NULL DEFAULT true,
    configured                   boolean NOT NULL DEFAULT false,
    kb_enabled                   boolean DEFAULT true,
    table_enabled                boolean DEFAULT true,
    faq_enabled                  boolean DEFAULT true,
    updated_by                   bigint REFERENCES crm_staff(id),
    updated_at                   timestamptz DEFAULT now(),
    UNIQUE (platform, post_id)
);

CREATE TABLE IF NOT EXISTS post_kb_binding (
    post_config_id   bigint REFERENCES post_ai_config(id),
    kb_collection_id bigint REFERENCES kb_collection(id),
    enabled          boolean DEFAULT true,
    PRIMARY KEY (post_config_id, kb_collection_id)
);

CREATE TABLE IF NOT EXISTS post_table_binding (
    post_config_id    bigint REFERENCES post_ai_config(id),
    business_table_id bigint REFERENCES autoreply_business_table(id),
    enabled           boolean DEFAULT true,
    PRIMARY KEY (post_config_id, business_table_id)
);

CREATE TABLE IF NOT EXISTS post_faq_binding (
    post_config_id bigint REFERENCES post_ai_config(id),
    faq_id         bigint REFERENCES autoreply_faq(id),
    enabled        boolean DEFAULT true,
    PRIMARY KEY (post_config_id, faq_id)
);
"""

# 輕量 migration：每條都要能重複執行（冪等），用於既有庫補欄位。
MIGRATIONS = (
    # 一帳號多身分：記錄本次登入選擇的身分（2026-10-13）
    "ALTER TABLE crm_session ADD COLUMN IF NOT EXISTS active_role text",
    # 帳號映射增量同步用（階段二；Connector 的 since 拉取靠這欄判斷變更）
    "ALTER TABLE crm_business_platform_account ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()",
    # 既有 session 若沒有 active_role，回填為其角色清單的第一個
    "UPDATE crm_session SET active_role = roles[1]"
    " WHERE active_role IS NULL AND roles IS NOT NULL AND array_length(roles, 1) >= 1",
    # 階段三：autoreply_log 補 scope 欄（dm / comment）
    "ALTER TABLE autoreply_log ADD COLUMN IF NOT EXISTS scope text",
    # 階段三：放寬 tier 約束 —— 認領佔位 'claim' 與 'error' 也要能寫（舊版沒有這兩個）
    "ALTER TABLE autoreply_log DROP CONSTRAINT IF EXISTS autoreply_log_tier_check",
    "ALTER TABLE autoreply_log ADD CONSTRAINT autoreply_log_tier_check"
    " CHECK (tier IN ('faq','table','rag','miss','claim','error'))",
    # 階段三：AI 佇列紀錄認領時間（worker 崩掉時據此回收卡住的 processing 項）
    "ALTER TABLE crm_ai_inbound_queue ADD COLUMN IF NOT EXISTS claimed_at timestamptz",
    # 階段三：CRM schema 漏了這兩個時間欄（舊庫有）→ update_table / update_row 會 SET 它們
    "ALTER TABLE autoreply_business_table ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()",
    "ALTER TABLE autoreply_business_row ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()",
    # 階段三：kb_* 也漏了三個時間欄 —— `kb_db.update_collection()` SET updated_at、
    # `create_job()/finish_job()` 寫 started_at / finished_at，缺欄會直接 UndefinedColumn
    "ALTER TABLE kb_collection ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()",
    "ALTER TABLE kb_job ADD COLUMN IF NOT EXISTS started_at timestamptz",
    "ALTER TABLE kb_job ADD COLUMN IF NOT EXISTS finished_at timestamptz",
    # 階段三：`kb_param` 的唯一約束改成**具名**（`kb_db.set_params()` 用
    # `ON CONFLICT ON CONSTRAINT kb_param_scope_key`，匿名約束名對不上 → 改參數直接 UndefinedObject）
    "DO $$ BEGIN"
    " IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='kb_param_collection_id_key_key'"
    "            AND conrelid='kb_param'::regclass) THEN"
    "   ALTER TABLE kb_param RENAME CONSTRAINT kb_param_collection_id_key_key TO kb_param_scope_key;"
    " END IF; END $$;",
    # 階段三：kb_* 的外鍵**缺 ON DELETE 行為**（舊庫有）→ 刪文件/刪庫會撞 FK。
    # 逐條重建（DROP 是 IF EXISTS，可重複執行）。
    "ALTER TABLE kb_document DROP CONSTRAINT IF EXISTS kb_document_collection_id_fkey;"
    " ALTER TABLE kb_document ADD CONSTRAINT kb_document_collection_id_fkey"
    "   FOREIGN KEY (collection_id) REFERENCES kb_collection(id) ON DELETE SET NULL",
    "ALTER TABLE kb_chunk DROP CONSTRAINT IF EXISTS kb_chunk_doc_id_fkey;"
    " ALTER TABLE kb_chunk ADD CONSTRAINT kb_chunk_doc_id_fkey"
    "   FOREIGN KEY (doc_id) REFERENCES kb_document(id) ON DELETE CASCADE",
    "ALTER TABLE kb_job DROP CONSTRAINT IF EXISTS kb_job_doc_id_fkey;"
    " ALTER TABLE kb_job ADD CONSTRAINT kb_job_doc_id_fkey"
    "   FOREIGN KEY (doc_id) REFERENCES kb_document(id) ON DELETE CASCADE",
    "ALTER TABLE kb_asset DROP CONSTRAINT IF EXISTS kb_asset_collection_id_fkey;"
    " ALTER TABLE kb_asset ADD CONSTRAINT kb_asset_collection_id_fkey"
    "   FOREIGN KEY (collection_id) REFERENCES kb_collection(id) ON DELETE SET NULL",
    "ALTER TABLE kb_asset DROP CONSTRAINT IF EXISTS kb_asset_doc_id_fkey;"
    " ALTER TABLE kb_asset ADD CONSTRAINT kb_asset_doc_id_fkey"
    "   FOREIGN KEY (doc_id) REFERENCES kb_document(id) ON DELETE CASCADE",
    "ALTER TABLE kb_asset_chunk DROP CONSTRAINT IF EXISTS kb_asset_chunk_asset_id_fkey;"
    " ALTER TABLE kb_asset_chunk ADD CONSTRAINT kb_asset_chunk_asset_id_fkey"
    "   FOREIGN KEY (asset_id) REFERENCES kb_asset(id) ON DELETE CASCADE",
    "ALTER TABLE kb_asset_chunk DROP CONSTRAINT IF EXISTS kb_asset_chunk_chunk_id_fkey;"
    " ALTER TABLE kb_asset_chunk ADD CONSTRAINT kb_asset_chunk_chunk_id_fkey"
    "   FOREIGN KEY (chunk_id) REFERENCES kb_chunk(id) ON DELETE CASCADE",
    "ALTER TABLE kb_param DROP CONSTRAINT IF EXISTS kb_param_collection_id_fkey;"
    " ALTER TABLE kb_param ADD CONSTRAINT kb_param_collection_id_fkey"
    "   FOREIGN KEY (collection_id) REFERENCES kb_collection(id) ON DELETE CASCADE",
    # 階段四：告警已通知（寄過郵件）的時間 —— 沒這欄，同一條告警每輪巡檢都會重寄一次。
    # `notified_at IS NULL` = 還沒通知過（新告警或又累計過次數的告警）。
    "ALTER TABLE crm_alert ADD COLUMN IF NOT EXISTS notified_at timestamptz",
)

# 運維可配置參數（設計案 §15.3 建議值），首次建庫寫入；已存在不覆蓋。
OPS_CONFIG_DEFAULTS = {
    "outbound.max_retry": 3,
    "outbound.backoff_seconds": 1,
    "outbound.claim_batch": 50,
    "outbound.per_business_limit": 10,
    "outbound.processing_timeout": 300,
    "callback.max_retry": 5,
    # AI 入站佇列（worker 消費端）
    "ai.claim_batch": 5,
    "ai.queue_max_retry": 2,
    "ai.queue_stuck_timeout": 600,
    "alert.mass_failure_threshold": 20,
    # 判定「大量失敗」的時間窗（分鐘）——只數這個窗內的 failed 任務
    "alert.failure_window_minutes": 10,
    # 寄信的最低嚴重度：'info' | 'warning' | 'critical'（低於此值只留在清單、不寄）
    "alert.email_min_severity": "warning",
    "alert.email_recipients": [],
    "media.kb_cache_ttl_days": 7,
}
