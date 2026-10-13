"""Connector DB（`platform_forwarder`）連線與建表。

Connector 是這個庫的**唯一寫入者**；CRM 一律透過內網 API 讀取（設計案 §3.1 / §7）。
"""
import asyncio
import logging

import psycopg2

from app.core import config

log = logging.getLogger("connector")

# ---------- Connector 新增結構（設計案 §6 + §1.2 帳號映射） ----------
_DDL = """
-- 帳號映射快取：對齊鍵 = (platform, platform_business_account_id) 平台原生 ID。
-- 真相在 CRM 的 crm_business_platform_account；這裡只是同步來的快取（見 MIGRATION_PLAN §1）。
CREATE TABLE IF NOT EXISTS connector_account_map (
    platform                      text NOT NULL,
    platform_business_account_id  text NOT NULL,
    business_id                   bigint,
    bp_account_id                 bigint,
    enabled                       boolean NOT NULL DEFAULT true,
    credential                    jsonb,
    synced_at                     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, platform_business_account_id)
);

-- 發送嘗試（子項冪等：UNIQUE(crm_task_id, sub_seq)）
CREATE TABLE IF NOT EXISTS connector_outbound_attempt (
    id              bigserial PRIMARY KEY,
    crm_task_id     bigint NOT NULL,
    sub_seq         int NOT NULL,
    platform        text NOT NULL,
    target_id       text NOT NULL,
    status          text NOT NULL CHECK (status IN ('sent','failed')),
    platform_msg_id text,
    error           text,
    attempted_at    timestamptz DEFAULT now(),
    UNIQUE (crm_task_id, sub_seq)
);
CREATE INDEX IF NOT EXISTS idx_attempt_task ON connector_outbound_attempt(crm_task_id);

-- 回調重試佇列（設計案 §6.3）
CREATE TABLE IF NOT EXISTS connector_callback_queue (
    id            bigserial PRIMARY KEY,
    crm_task_id   bigint NOT NULL,
    result        jsonb NOT NULL,
    retry_count   int DEFAULT 0,
    max_retry     int NOT NULL DEFAULT 5,
    next_retry_at timestamptz DEFAULT now(),
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
    created_at    timestamptz DEFAULT now(),
    UNIQUE (crm_task_id)
);

-- 同步狀態（給「帳號映射過期」告警用，見 MIGRATION_PLAN §1.3）
CREATE TABLE IF NOT EXISTS connector_sync_state (
    key        text PRIMARY KEY,
    value      jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

# 訊息表新增欄位：先可 NULL → 回填 → 再 SET NOT NULL（MIGRATION_PLAN §3.3 步驟 5）
_MSG_TABLES = ("forward_log", "igfb_dm", "igfb_comment", "igfb_post")
_NEW_COLUMNS = (
    ("platform_business_account_id", "TEXT"),   # 對齊鍵（平台原生 ID）
    ("business_id", "BIGINT"),                  # CRM PK 快取，真相在 CRM
    ("bp_account_id", "BIGINT"),                # crm_business_platform_account.id 快取
)


def connect():
    """回傳一個新的 psycopg2 連線（呼叫端負責關閉）。"""
    return psycopg2.connect(config.DATABASE_URL)


def _init_sync() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
            # 既有庫補欄位（冪等）
            cur.execute("ALTER TABLE connector_account_map"
                        " ADD COLUMN IF NOT EXISTS credential jsonb")
            for tbl in _MSG_TABLES:
                for col, typ in _NEW_COLUMNS:
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {typ}")
        conn.commit()
    finally:
        conn.close()


async def init_schema_async() -> None:
    try:
        await asyncio.to_thread(_init_sync)
    except Exception as e:
        log.warning("Connector schema 初始化失敗: %s", e)
