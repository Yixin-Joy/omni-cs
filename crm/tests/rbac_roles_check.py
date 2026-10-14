"""RBAC 身分授權自檢 —— 對**真服務**跑。

驗證需求（2026-10-14）：
  「只有系統第一個管理員預設有所有身分（admin/agent/ops）；
    後續新增的管理員需由既有管理員授權才有客服 / 運維介面。」

覆蓋：
  [1] 第一個管理員（owner）：available_roles = 三種、can_switch = true
  [2] 新建「只有 admin」的管理員：active_role=admin、available_roles=[admin]、
      can_switch=false，且 GET /agent、/ops → 302（進不去）
  [3] 既有管理員授權 agent+ops → 該帳號 available_roles 變三種、can_switch=true、能切進 /agent /ops
  [4] 清理測試帳號

跑法（從專案根）：
  PYTHONPATH='<root>\\.venv\\Lib\\site-packages' <底層 python> crm/tests/rbac_roles_check.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020").rstrip("/")
ROOT = Path(__file__).resolve().parents[2]

NEW_ACC = "zz_rbac_bot"
NEW_PW = "Zz!23456789"

fails: list[str] = []
oks = [0]


def check(cond, msg, extra=""):
    if cond:
        oks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(msg)
    return bool(cond)


def read_env(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = read_env(ROOT / "crm" / ".env")
PW = ENV.get("CRM_ADMIN_PASSWORD", "")
DSN = ENV.get("DATABASE_URL", "")


def db(sql: str, args=None, fetch=False):
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


def login_full(c: httpx.Client, account: str, password: str, role: str | None = None):
    """完整登入（自動處理多身分的兩步流程）。回 (csrf_token, staff_dict)。"""
    r = c.post("/api/auth/login", json={"account": account, "password": password})
    d = r.json()
    if d.get("need_role"):
        r = c.post("/api/auth/login",
                   json={"ticket": d["ticket"], "role": role or d["available_roles"][0]})
        d = r.json()
    return d.get("csrf_token", ""), (d.get("staff") or d)


def cleanup():
    rows = db("SELECT id FROM crm_staff WHERE login_account=%s", (NEW_ACC,), fetch=True)
    if rows:
        db("DELETE FROM crm_session WHERE staff_id=%s", (rows[0]["id"],))
    db("DELETE FROM crm_staff WHERE login_account=%s", (NEW_ACC,))


def main() -> int:
    print(f"BASE = {BASE}\n")
    if not PW:
        print("!! crm/.env 沒有 CRM_ADMIN_PASSWORD")
        return 2
    cleanup()

    # ---------------------------------------------------------- [1]
    print("[1] 第一個管理員（系統 owner，預設全部身分）")
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=15)
    csrf, me = login_full(c, "admin", PW, "admin")
    check(me.get("active_role") == "admin", "active_role = admin", str(me.get("active_role")))
    check(sorted(me.get("available_roles") or []) == ["admin", "agent", "ops"],
          "available_roles = admin/agent/ops", str(me.get("available_roles")))
    check(me.get("can_switch") is True, "can_switch = true（有切換入口）")

    # ---------------------------------------------------------- [2]
    print("\n[2] 新建『只有 admin』的管理員 → 預設不該有其他介面")
    r = c.post("/api/admin/staff",
               json={"login_account": NEW_ACC, "password": NEW_PW,
                     "staff_name": "RBAC測試管理員", "roles": ["admin"]},
               headers={"X-CSRF-Token": csrf})
    check(r.status_code == 200 and r.json().get("ok"), "建立帳號成功",
          f"{r.status_code} {r.text[:120]}")
    sid = db("SELECT id FROM crm_staff WHERE login_account=%s", (NEW_ACC,), fetch=True)[0]["id"]

    c2 = httpx.Client(base_url=BASE, follow_redirects=False, timeout=15)
    _csrf2, me2 = login_full(c2, NEW_ACC, NEW_PW)
    check(me2.get("active_role") == "admin", "新帳號 active_role = admin", str(me2.get("active_role")))
    check(sorted(me2.get("available_roles") or []) == ["admin"],
          "★ available_roles 只有 admin（admin 不再自動超集）", str(me2.get("available_roles")))
    check(me2.get("can_switch") is False, "★ can_switch = false（沒有切換入口）")
    check(c2.get("/admin").status_code == 200, "GET /admin → 200（本職有權）")
    check(c2.get("/agent").status_code == 302, "★ GET /agent → 302（需授權）",
          str(c2.get("/agent").status_code))
    check(c2.get("/ops").status_code == 302, "★ GET /ops → 302（需授權）",
          str(c2.get("/ops").status_code))

    # ---------------------------------------------------------- [3]
    print("\n[3] 既有管理員授權（加 agent + ops）")
    r = c.post("/api/admin/staff/update",
               json={"id": sid, "staff_name": "RBAC測試管理員",
                     "roles": ["admin", "agent", "ops"], "enabled": True},
               headers={"X-CSRF-Token": csrf})
    check(r.status_code == 200 and r.json().get("ok"), "授權成功", f"{r.status_code} {r.text[:120]}")

    c3 = httpx.Client(base_url=BASE, follow_redirects=False, timeout=15)
    csrf3, me3 = login_full(c3, NEW_ACC, NEW_PW, "admin")
    check(sorted(me3.get("available_roles") or []) == ["admin", "agent", "ops"],
          "★ 授權後 available_roles = 三種", str(me3.get("available_roles")))
    check(me3.get("can_switch") is True, "授權後 can_switch = true")
    for role, path in (("agent", "/agent"), ("ops", "/ops")):
        r = c3.post("/api/auth/switch-role", json={"role": role},
                    headers={"X-CSRF-Token": csrf3})
        d = r.json() if r.status_code == 200 else {}
        check(r.status_code == 200 and d.get("home") == path,
              f"授權後可切到 {role}", f"{r.status_code} {d.get('home')}")
        # ★ 切換後「當下身分」就是該角色，這時才驗證頁面可達（切回 admin 後就會 302）
        rr = c3.get(path)
        check(rr.status_code == 200, f"授權後（身分={role}）GET {path} → 200", str(rr.status_code))
    c3.post("/api/auth/switch-role", json={"role": "admin"}, headers={"X-CSRF-Token": csrf3})

    # ---------------------------------------------------------- [4]
    print("\n[4] 清理測試帳號")
    cleanup()
    check(db("SELECT id FROM crm_staff WHERE login_account=%s", (NEW_ACC,), fetch=True) == [],
          "測試帳號已清空")

    print(f"\n結果：{'PASS' if not fails else 'FAIL'}  （PASS {oks[0]} / FAIL {len(fails)}）")
    for f in fails:
        print("   FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
