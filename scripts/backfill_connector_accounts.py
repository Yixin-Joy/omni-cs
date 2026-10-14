"""回填 Connector 訊息表的帳號歸屬 —— legacy 資料的 `platform_business_account_id` 全為 NULL。

## 為什麼需要這支

新架構下「會話列表」以 `(platform, platform_business_account_id)` 查詢（設計案 §4.2），
但**遷移前的歷史訊息**（`forward_log` / `igfb_dm` / `igfb_comment`）這三個欄位
（`platform_business_account_id` / `business_id` / `bp_account_id`）**全部是 NULL**
—— 舊單體時代根本沒有這些欄位。結果：工作台按帳號查**一筆都撈不到**，
行數對得上、介面卻是空的（與 T18「未分組文件」同類的隱形缺口）。

## 回填規則（保守、冪等）

只處理 `platform_business_account_id IS NULL` 的列，且**該平台只有唯一一個帳號**時才填
（來源＝CRM 的 `crm_business_platform_account`，它才是帳號歸屬的權威）。
一個平台有多個帳號 → **跳過並回報**（無法判斷哪筆屬於誰，寧可留空也不要填錯）。

## 用法（從專案根）

    # 先看（預設 dry-run，什麼都不寫）
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> scripts/backfill_connector_accounts.py

    # 真的寫入
    ... scripts/backfill_connector_accounts.py --apply

    # 只看覆蓋率（給任何一支單獨跑）
    ... scripts/backfill_connector_accounts.py --verify
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".venv" / "Lib" / "site-packages"))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

TABLES = ("forward_log", "igfb_dm", "igfb_comment", "igfb_post")


# ---------------------------------------------------------------- 環境

def parse_env_file(p: Path) -> dict:
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def dsn_of(env_file: Path, default_db: str) -> str:
    env = parse_env_file(env_file)
    dsn = env.get("DATABASE_URL") or ""
    if dsn:
        return dsn
    # 退回本機預設（帳密沿用 connector/.env 的慣例）
    return f"postgresql://postgres:postgres@localhost:5432/{default_db}"


def say(s: str = "") -> None:
    print(s, flush=True)


# ---------------------------------------------------------------- 帳號權威表

def crm_accounts(cur) -> list[dict]:
    cur.execute(
        "SELECT id, business_id, platform, platform_business_account_id"
        "  FROM crm_business_platform_account"
        " WHERE enabled IS NOT FALSE AND platform_business_account_id IS NOT NULL"
        "   AND platform_business_account_id <> ''"
        " ORDER BY platform, id"
    )
    return [
        {"id": r[0], "business_id": r[1], "platform": r[2], "account": r[3]}
        for r in cur.fetchall()
    ]


def uniq_map(rows: list[dict]) -> tuple[dict, dict]:
    """回 (唯一平台映射, 多帳號平台清單)。多帳號的平台不填，避免填錯。"""
    by_plat: dict[str, list[dict]] = {}
    for r in rows:
        by_plat.setdefault(r["platform"], []).append(r)
    uniq = {p: v[0] for p, v in by_plat.items() if len(v) == 1}
    multi = {p: len(v) for p, v in by_plat.items() if len(v) > 1}
    return uniq, multi


# ---------------------------------------------------------------- 主流程

def report_coverage(cur, label: str) -> dict:
    say(f"\n[{label}] 覆蓋率（platform_business_account_id 非空的列數 / 總列數）")
    out = {}
    for t in TABLES:
        cur.execute(
            sql.SQL("SELECT count(*), count(platform_business_account_id),"
                    " count(business_id) FROM {}").format(sql.Identifier(t)))
        tot, acc, biz = cur.fetchone()
        out[t] = (tot, acc, biz)
        pct = (acc / tot * 100) if tot else 100.0
        say(f"       {t:<16} {acc:>5} / {tot:<5}  ({pct:5.1f}%)   business_id 非空 {biz}")
    return out


def apply_backfill(conn_cur, uniq: dict) -> dict:
    stats = {}
    for t in TABLES:
        n = 0
        for plat, a in uniq.items():
            conn_cur.execute(
                sql.SQL(
                    "UPDATE {} SET platform_business_account_id = %s,"
                    "               business_id = %s, bp_account_id = %s"
                    " WHERE platform = %s AND platform_business_account_id IS NULL"
                ).format(sql.Identifier(t)),
                (a["account"], a["business_id"], a["id"], plat),
            )
            n += conn_cur.rowcount
        stats[t] = n
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 Connector 訊息表的帳號歸屬（legacy NULL）")
    ap.add_argument("--apply", action="store_true", help="真的寫入（預設只 dry-run）")
    ap.add_argument("--verify", action="store_true", help="只印覆蓋率，不做任何寫入")
    ap.add_argument("--connector-dsn", default="", help="覆寫 Connector DB DSN")
    ap.add_argument("--crm-dsn", default="", help="覆寫 CRM DB DSN")
    args = ap.parse_args()

    conn_dsn = args.connector_dsn or dsn_of(ROOT / "connector" / ".env", "platform_forwarder")
    crm_dsn = args.crm_dsn or dsn_of(ROOT / "crm" / ".env", "crm_db")
    say(f"Connector DB : {re.sub(r'://[^@]*@', '://***@', conn_dsn)}")
    say(f"CRM DB       : {re.sub(r'://[^@]*@', '://***@', crm_dsn)}")

    crm = psycopg2.connect(crm_dsn)
    try:
        with crm.cursor() as ccur:
            rows = crm_accounts(ccur)
    finally:
        crm.close()

    uniq, multi = uniq_map(rows)
    say(f"\n[1] 帳號權威表（crm_business_platform_account）：{len(rows)} 筆")
    for r in rows:
        say(f"       {r['platform']:<9} account={r['account']:<22} business_id={r['business_id']}")
    say(f"\n[2] 可回填的平台（唯一帳號）：{', '.join(sorted(uniq)) or '（無）'}")
    if multi:
        say(f"    ⚠️ 多帳號平台，跳過（無法判斷歸屬）："
            + ", ".join(f"{p}×{n}" for p, n in sorted(multi.items())))

    conn = psycopg2.connect(conn_dsn)
    try:
        with conn.cursor() as cur:
            before = report_coverage(cur, "3")

            if args.verify:
                say("\n--verify：只讀，未做任何寫入。")
                return 0

            if not uniq:
                say("\n沒有唯一帳號的平台 → 無事可做。")
                return 0

            if not args.apply:
                say("\n[4] dry-run：以下列數**將**被回填（加 --apply 才真的寫）")
                for t in TABLES:
                    n = 0
                    for plat, a in uniq.items():
                        cur.execute(
                            sql.SQL("SELECT count(*) FROM {} WHERE platform = %s"
                                    " AND platform_business_account_id IS NULL"
                            ).format(sql.Identifier(t)), (plat,))
                        n += int(cur.fetchone()[0])
                    say(f"       {t:<16} {n}")
                say("\n（dry-run 結束，未寫入。）")
                return 0

            say("\n[4] 回填中…")
            stats = apply_backfill(cur, uniq)
            conn.commit()
            for t, n in stats.items():
                say(f"       {t:<16} 更新 {n} 列")

            report_coverage(cur, "5")
    finally:
        conn.close()

    say("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
