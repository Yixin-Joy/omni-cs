"""把「已調通的平台憑證」從專案根的 `.env` 匯入 CRM 資料庫（多租戶）。

用途：新架構把平台業務憑證改存 DB（`crm_business_platform_account.credential`），
但歷史上這些憑證是寫在根的 `.env`（舊單體在用、已調通）。這支腳本把它們一次性搬進 DB，
之後 Connector 收發就會從 DB 取憑證（`.env` 只當 fallback）。

★ 只匯入「業務憑證」——
  握手 token（WECHAT_TOKEN / *_VERIFY_TOKEN / META_APP_SECRET）**不進 DB**，
  它們是「Connector 實例 vs 平台」的驗證身分，留在 `.env`（見 MIGRATION_PLAN §3）。

可重複執行（idempotent）：同名帳號已存在就覆蓋 credential。

跑法（從 crm 目錄）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> scripts/import_credentials.py [--business Epro_test] [--dry-run] [--no-push]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # crm/ 讓 `app` 可 import

import psycopg2  # noqa: E402
from app.core import config  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]  # 專案根（有 .env）

# 平台 → (原生 ID 來源 .env 鍵, credential 欄位 → .env 鍵)
PLATFORM_MAP = {
    "wechat": {
        "native": "WECHAT_APPID",
        "cred": {
            "appid": "WECHAT_APPID",
            "appsecret": "WECHAT_APPSECRET",
            "encoding_aes_key": "WECHAT_ENCODING_AES_KEY",
        },
    },
    "whatsapp": {
        "native": "WHATSAPP_PHONE_NUMBER_ID",
        "cred": {
            "access_token": "WHATSAPP_ACCESS_TOKEN",
            "phone_number_id": "WHATSAPP_PHONE_NUMBER_ID",
        },
    },
    "fb": {
        "native": "META_PAGE_ID",
        "cred": {
            "page_id": "META_PAGE_ID",
            "page_access_token": "META_PAGE_ACCESS_TOKEN",
        },
    },
    "ig": {
        "native": "META_IG_USER_ID",
        "cred": {
            "ig_user_id": "META_IG_USER_ID",
            "ig_access_token": "META_IG_ACCESS_TOKEN",
            # 留言回覆走 graph.facebook.com，用粉專 token（與舊單體 ig_reply_comment 一致）
            "page_access_token": "META_PAGE_ACCESS_TOKEN",
        },
    },
}


def read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(ROOT / ".env"), help="憑證來源 .env（預設專案根 .env）")
    ap.add_argument("--business", default="Epro_test", help="目標商家名（預設 Epro_test）")
    ap.add_argument("--dry-run", action="store_true", help="只顯示會寫什麼，不寫 DB、不推送")
    ap.add_argument("--no-push", action="store_true", help="寫完不推送給 Connector（等它定時拉取）")
    args = ap.parse_args()

    env = read_env(Path(args.env))
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM crm_business WHERE business_name = %s", (args.business,))
        row = cur.fetchone()
        if not row:
            print(f"!! 找不到商家 {args.business}（先跑 scripts/init_crm_db.py 建庫）")
            return 2
        biz_id = row[0]
        print(f"商家 {args.business} (id={biz_id})，憑證來源 {args.env}\n")

        results = []
        for platform, spec in PLATFORM_MAP.items():
            native = env.get(spec["native"], "")
            if not native:
                results.append((platform, "跳過", f".env 沒有 {spec['native']}"))
                continue
            cred = {k: env.get(src, "") for k, src in spec["cred"].items()}
            cred = {k: v for k, v in cred.items() if v}
            missing = [k for k in spec["cred"] if k not in cred]

            cur.execute(
                "SELECT id, (credential IS NOT NULL)"
                "  FROM crm_business_platform_account"
                " WHERE business_id = %s AND platform = %s AND platform_business_account_id = %s",
                (biz_id, platform, native),
            )
            acc = cur.fetchone()
            if not acc:
                results.append((platform, "找不到帳號",
                                f"{platform} 原生 ID {native} 未綁定到 {args.business}"))
                continue
            aid = acc[0]

            if not args.dry_run:
                cur.execute(
                    "UPDATE crm_business_platform_account"
                    "   SET credential = %s::jsonb, updated_at = now()"
                    " WHERE id = %s",
                    (json.dumps(cred, ensure_ascii=False), aid),
                )
            note = f"{len(cred)} 欄" + (f"，缺 {missing}" if missing else "")
            results.append((platform, "已寫入" if not args.dry_run else "將寫入（dry-run）",
                            f"原生 ID {native}，{note}"))

        if not args.dry_run:
            conn.commit()
        for platform, status, note in results:
            print(f"  {platform:9} {status}   {note}")
    finally:
        conn.close()

    if args.dry_run:
        print("\n（dry-run，未寫入、未推送）")
        return 0

    if not args.no_push:
        try:
            from app.internal import connector_client
            ok = connector_client.push_account_map()
            print(f"\n已推送帳號映射給 Connector：{'成功' if ok else '失敗（等定時拉取兜底）'}")
        except Exception as e:  # noqa: BLE001
            print(f"\n推送失敗（服務沒開？等 Connector 定時拉取即可）：{e}")

    print("\n完成。Connector 收發將從 DB 取憑證（.env 僅 fallback）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
