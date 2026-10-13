"""建立 CRM DB（`crm_db`）並建表 + 種子資料。

可重複執行，不會清掉既有資料。DDL 單一真相在 `crm/app/core/schema.py`。

用法（從專案根執行）：
    python scripts/init_crm_db.py
    python scripts/init_crm_db.py --admin-account admin --admin-password 'ChangeMe123'

連線資訊從 `crm/.env` 的 `DATABASE_URL` 讀取。
"""
import argparse
import json
import os
import sys
import urllib.parse as up

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "crm"))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

from app.core import config as crm_config  # noqa: E402
from app.core import schema  # noqa: E402
from app.core import security  # noqa: E402

# 建庫時要連一個「已存在」的庫（不能連著要建的庫建它）
ADMIN_DB = os.getenv("ADMIN_DB", "platform_forwarder")
DEFAULT_BUSINESS = os.getenv("DEFAULT_BUSINESS_NAME", "Epro_test")


def _conn_parts():
    u = up.urlparse(crm_config.DATABASE_URL)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "user": u.username,
        "password": u.password,
        "dbname": (u.path or "/crm_db").lstrip("/"),
    }


def _connect(**kw):
    return psycopg2.connect(**kw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin-account", default="")
    ap.add_argument("--admin-password", default="")
    ap.add_argument("--admin-name", default="系統管理員")
    args = ap.parse_args()

    p = _conn_parts()
    lines: list[str] = [f"目標 CRM DB: {p['dbname']} @ {p['host']}:{p['port']}"]

    # 1) 建庫（若不存在）
    admin = _connect(host=p["host"], port=p["port"], user=p["user"], password=p["password"], dbname=ADMIN_DB)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (p["dbname"],))
            if cur.fetchone():
                lines.append(f"database {p['dbname']} 已存在")
            else:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(p["dbname"])))
                lines.append(f"已建立 database {p['dbname']}")
    finally:
        admin.close()

    # 2) 建表 + 運維預設參數
    conn = _connect(**p)
    try:
        with conn.cursor() as cur:
            cur.execute(schema.DDL)
            for key, val in schema.OPS_CONFIG_DEFAULTS.items():
                cur.execute(
                    "INSERT INTO crm_ops_config (config_key, config_value) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (config_key) DO NOTHING",
                    (key, json.dumps(val)),
                )

            # 3) 種子：預設商家
            cur.execute("SELECT id FROM crm_business WHERE business_name = %s", (DEFAULT_BUSINESS,))
            row = cur.fetchone()
            if row:
                business_id = row[0]
                lines.append(f"商家 {DEFAULT_BUSINESS} 已存在 (id={business_id})")
            else:
                cur.execute(
                    "INSERT INTO crm_business (business_name, business_memo) VALUES (%s, %s) RETURNING id",
                    (DEFAULT_BUSINESS, "遷移自舊系統 platform_forwarder"),
                )
                business_id = cur.fetchone()[0]
                lines.append(f"已建立商家 {DEFAULT_BUSINESS} (id={business_id})")

            # 4) 種子：可選 admin
            if args.admin_account and args.admin_password:
                ph = security.hash_password(args.admin_password)
                cur.execute(
                    "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles, is_super_agent, is_kb_editor) "
                    "VALUES (%s, %s, %s, %s, true, true) ON CONFLICT (login_account) DO NOTHING",
                    (args.admin_account, ph, args.admin_name, ["admin"]),
                )
                lines.append(f"admin 帳號 {args.admin_account} 已就緒")
        conn.commit()

        # 驗證：列出 CRM DB 的表
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' ORDER BY table_name"
            )
            tables = [r[0] for r in cur.fetchall()]
        lines.append(f"CRM DB 共 {len(tables)} 張表：" + ", ".join(tables))
    finally:
        conn.close()

    lines.append("DONE")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
