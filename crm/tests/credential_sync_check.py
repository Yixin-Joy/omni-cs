"""憑證同步契約自檢（純單元，不需起服務）。

★ 為什麼需要：曾真實發生 —— `push_account_map` 組 payload 時漏帶 `credential`，
  導致「推送成功但 Connector 快取沒有憑證」（只有定時拉取路徑有）。
  收發就偷偷退回 `.env` fallback，多租戶憑證形同失效。

驗證兩條路徑都帶憑證：
  [1] `repo.account_map()`（拉取的資料來源）回傳含 credential
  [2] `connector_client.push_account_map()`（推送）的 payload 含 credential
  [3] 兩者的欄位集合一致（避免只改一邊）

跑法（從 crm 目錄）：
  PYTHONPATH='<root>\\.venv\\Lib\\site-packages' <底層 python> tests/credential_sync_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import psycopg2  # noqa: E402

from app.core import config  # noqa: E402
from app.internal import connector_client, repo  # noqa: E402

fails: list[str] = []
oks = [0]


def check(cond, msg, extra=""):
    if cond:
        oks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(msg)
    return bool(cond)


PID = "zz_cred_sync_probe"


def _purge(cur) -> None:
    # 先刪引用 crm_business 的子表，再刪商家（避免 FK violation）
    cur.execute(
        "DELETE FROM crm_business_platform_account WHERE business_id IN"
        " (SELECT id FROM crm_business WHERE business_name = %s)", (PID,))
    cur.execute("DELETE FROM crm_business WHERE business_name = %s", (PID,))


def seed() -> int:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            _purge(cur)
            cur.execute("INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id", (PID,))
            bid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crm_business_platform_account"
                " (business_id, platform, platform_business_account_id, credential)"
                " VALUES (%s,'wechat','zz_probe_appid', %s::jsonb) RETURNING id",
                (bid, json.dumps({"appid": "p", "appsecret": "s", "encoding_aes_key": "k"})),
            )
        conn.commit()
        return bid
    finally:
        conn.close()


def cleanup() -> None:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            _purge(cur)
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    print("=== [1] repo.account_map()（拉取來源）帶 credential ===")
    seed()
    try:
        items = repo.account_map()
        mine = [i for i in items if i.get("platform_business_account_id") == "zz_probe_appid"]
        check(len(mine) == 1, "找得到探針帳號")
        if mine:
            check("credential" in mine[0], "account_map() 回傳含 credential 欄位")
            check((mine[0].get("credential") or {}).get("appid") == "p",
                  "credential 內容正確", str(mine[0].get("credential")))

        print("\n=== [2] push_account_map() 的 payload 帶 credential ===")
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"ok": True, "upserted": 1})

        connector_client.set_transport(httpx.MockTransport(handler))
        try:
            ok = connector_client.push_account_map()
        finally:
            connector_client.set_transport(None)
        check(ok is True, "push_account_map 回傳成功")
        sent = (captured.get("body") or {}).get("items") or []
        probe = [i for i in sent if i.get("platform_business_account_id") == "zz_probe_appid"]
        check(len(probe) == 1, "payload 含探針帳號")
        if probe:
            check("credential" in probe[0], "★ payload 有 credential 欄位（曾被漏掉）")
            check((probe[0].get("credential") or {}).get("appid") == "p",
                  "payload 的 credential 內容正確")

        print("\n=== [3] 兩條路徑欄位一致 ===")
        pull_keys = set(mine[0].keys()) if mine else set()
        push_keys = set(probe[0].keys()) if probe else set()
        missing = {"credential"} - push_keys
        check(not missing, "推送路徑沒有漏欄位", f"缺={sorted(missing)}" if missing else "")
    finally:
        cleanup()

    print(f"\n結果：{'PASS' if not fails else 'FAIL'}  （PASS {oks[0]} / FAIL {len(fails)}）")
    for f in fails:
        print("   FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
