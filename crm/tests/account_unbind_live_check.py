"""真實端到端：走真 HTTP 對 8020 解綁，檢查 8010 的 account_map 是否**立刻**清掉。

這是 `replace=True` 的唯一證明方式 —— 只跑 TestClient 看不到 Connector 那一側。
關鍵斷言：解綁後**不等 60 秒定時拉取**，`connector_account_map` 就必須沒有那一列。

跑法（兩個服務都要在跑）：
    PYTHONPATH='.venv/Lib/site-packages' python crm/tests/account_unbind_live_check.py
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import requests  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.core import config  # noqa: E402

CRM = "http://127.0.0.1:8020"
CONN = "http://127.0.0.1:8010"
TAG = "ZZ解綁在線"
NATIVE = f"__CK_live_{uuid.uuid4().hex[:10]}__"

_pass = 0
_fail: list[str] = []


def check(cond, msg, extra="") -> bool:
    global _pass
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if cond:
        _pass += 1
    else:
        _fail.append(msg)
    return bool(cond)


def crm_db():
    return psycopg2.connect(config.DATABASE_URL)


def conn_db():
    """Connector 的另一個 DB（platform_forwarder）。DSN 從 connector/.env 讀。"""
    env = ROOT / "connector" / ".env"
    dsn = ""
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL=") and "platform_forwarder" in line:
                dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not dsn:
        dsn = config.DATABASE_URL.replace("/crm_db", "/platform_forwarder")
    return psycopg2.connect(dsn)


def q1(sql, args=()):
    c = crm_db()
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        c.close()


def exec_sql(sql, args=()) -> int:
    c = crm_db()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        c.commit()
        return n
    finally:
        c.close()


def conn_rows(native: str) -> list[tuple]:
    c = conn_db()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT platform, platform_business_account_id, bp_account_id, enabled"
                        " FROM connector_account_map"
                        " WHERE platform_business_account_id = %s", (native,))
            return cur.fetchall()
    finally:
        c.close()


def cleanup() -> None:
    a = crm_db()
    try:
        with a.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _aid AS SELECT id FROM crm_business_platform_account"
                        " WHERE platform_business_account_id LIKE %s", (NATIVE[:12] + "%",))
            cur.execute("DELETE FROM crm_outbound_task WHERE business_platform_account_id"
                        " IN (SELECT id FROM _aid)")
            cur.execute("DELETE FROM post_ai_config WHERE business_platform_account_id"
                        " IN (SELECT id FROM _aid)")
            cur.execute("DELETE FROM crm_business_platform_account WHERE id IN (SELECT id FROM _aid)")
            cur.execute("CREATE TEMP TABLE _bid AS SELECT id FROM crm_business"
                        " WHERE business_name LIKE %s", (TAG + "%",))
            cur.execute("DELETE FROM business_ai_switch WHERE business_id IN (SELECT id FROM _bid)")
            cur.execute("DELETE FROM crm_business WHERE id IN (SELECT id FROM _bid)")
            cur.execute("DELETE FROM crm_audit_log WHERE payload::text LIKE %s",
                        ("%" + NATIVE + "%",))
        a.commit()
    finally:
        a.close()
    b = conn_db()
    try:
        with b.cursor() as cur:
            cur.execute("DELETE FROM connector_account_map"
                        " WHERE platform_business_account_id LIKE %s", (NATIVE[:12] + "%",))
        b.commit()
    finally:
        b.close()


def main() -> int:
    cleanup()

    staff = q1("SELECT id, login_account, staff_name, roles, is_super_agent, is_kb_editor, enabled"
               "  FROM crm_staff WHERE 'admin' = ANY(roles) AND enabled ORDER BY id LIMIT 1")
    if not staff:
        print("  !! 找不到 admin 帳號")
        return 2
    sid, csrf = sess_mod.create_session(dict(staff), "admin")
    S = requests.Session()
    S.cookies.set(config.SESSION_COOKIE_NAME, sid, domain="127.0.0.1", path="/")
    H = {"X-CSRF-Token": csrf}

    try:
        print("[0] 服務可達")
        check(requests.get(CONN + "/health", timeout=5).status_code == 200, "Connector 8010 活著")
        check(S.get(CRM + "/api/auth/me", timeout=10).status_code == 200, "CRM session 有效")

        print("\n[1] 建商家 + 綁帳號")
        r = S.post(CRM + "/api/admin/businesses", json={"business_name": TAG + "L"}, headers=H)
        check(r.status_code == 200, f"建商家（{r.status_code}）", r.text[:100])
        biz = r.json()["id"]
        r = S.post(CRM + "/api/admin/accounts", json={
            "business_id": biz, "platform": "fb", "display_name": TAG,
            "credential": {"page_id": NATIVE, "page_access_token": "live-" + NATIVE},
        }, headers=H)
        check(r.status_code == 200, f"綁定（{r.status_code}）", r.text[:120])
        if r.status_code != 200:
            return 2
        aid = r.json()["id"]

        print("\n[2] 綁定後 Connector 快取應有這把憑證")
        # 綁定會 push（replace=False 的 upsert），稍等一下讓推送落地
        ok = False
        for _ in range(15):
            if conn_rows(NATIVE):
                ok = True
                break
            import time
            time.sleep(1)
        check(ok, "connector_account_map 已收到這個帳號")
        rows = conn_rows(NATIVE)
        check(bool(rows) and rows[0][2] == aid, "快取裡的 bp_account_id 對得上",
              str(rows[:1]))

        print("\n[3] 解綁 → ★ 不等 60s 定時拉取，快取必須立刻消失")
        r = S.post(CRM + "/api/admin/accounts/unbind", json={"id": aid}, headers=H)
        check(r.status_code == 200, f"解綁（{r.status_code}）", r.text[:140])
        # 立刻查，不 sleep —— 這就是 replace=True 的價值
        left = conn_rows(NATIVE)
        check(not left, "★ 解綁後 Connector 快取**立即**沒有這把憑證（舊 token 不能再收發）",
              f"殘留 {left}")

        print("\n[4] 再驗一次 DB 側")
        check(q1("SELECT 1 FROM crm_business_platform_account WHERE id=%s", (aid,)) is None,
              "CRM 帳號列已刪")

        print("\n[5] 對照：單純停用（enabled=false）不該刪快取列")
        r = S.post(CRM + "/api/admin/accounts", json={
            "business_id": biz, "platform": "fb", "display_name": TAG + "-2",
            "credential": {"page_id": NATIVE + "_2", "page_access_token": "live2"},
        }, headers=H)
        check(r.status_code == 200, f"再綁一個（{r.status_code}）", r.text[:100])
        if r.status_code == 200:
            aid2 = r.json()["id"]
            import time
            for _ in range(15):
                if conn_rows(NATIVE + "_2"):
                    break
                time.sleep(1)
            S.post(CRM + "/api/admin/accounts/update", json={
                "id": aid2, "display_name": TAG + "-2",
                "ai_auto_reply_enabled": True, "enabled": False,
            }, headers=H)
            time.sleep(2)
            rows = conn_rows(NATIVE + "_2")
            check(bool(rows), "停用後快取列**還在**（只是 enabled=false）", f"rows={rows[:1]}")
            check(bool(rows) and rows[0][3] is False, "快取列 enabled 已變 false")

    finally:
        try:
            sess_mod.delete_session(sid)
        except Exception:  # noqa: BLE001
            pass
        cleanup()

    print(f"\n{'=' * 62}")
    print(f"  在線解綁：PASS {_pass} / FAIL {len(_fail)}")
    for m in _fail:
        print("    FAIL:", m)
    return 0 if not _fail else 1


if __name__ == "__main__":
    sys.exit(main())
