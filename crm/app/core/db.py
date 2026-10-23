"""CRM DB（`crm_db`）連線與建表。CRM 是這個庫的唯一寫入者。

跨庫讀取渠道訊息一律走內網 API（不得直接 fdw JOIN，設計案 §7.1.3）。
"""
import asyncio
import json
import logging

import psycopg2

from app.core import config
from app.core import schema

log = logging.getLogger("crm")


def connect():
    """回傳一個新的 psycopg2 連線（呼叫端負責關閉）。"""
    return psycopg2.connect(config.DATABASE_URL)


def init_schema_sync() -> None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(schema.DDL)
            # 輕量 migration：既有庫補欄位（新庫由上面的 DDL 直接建好）。
            # 一律用 IF NOT EXISTS，可重複執行。
            for stmt in schema.MIGRATIONS:
                cur.execute(stmt)
            for key, val in schema.OPS_CONFIG_DEFAULTS.items():
                # ⚠️ 有些 key 的「有沒有這一列」本身就是語義（見 `OPS_CONFIG_NO_SEED`）：
                #    寫了預設列就等於「運維端設過」，會把 .env 的值靜默蓋掉。
                if key in schema.OPS_CONFIG_NO_SEED:
                    continue
                cur.execute(
                    "INSERT INTO crm_ops_config (config_key, config_value) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (config_key) DO NOTHING",
                    (key, json.dumps(val)),
                )
        conn.commit()
    finally:
        conn.close()


async def init_schema_async() -> None:
    try:
        await asyncio.to_thread(init_schema_sync)
    except Exception as e:
        log.warning("CRM schema 初始化失敗: %s", e)
