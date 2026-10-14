"""CRM 頁面跳轉自檢 —— 對**真服務**跑完整「登入 → 分流 → 切換身分 → 登出」鏈。

驗證目標：所有該 302 的 302、該 200 的 200、登入兩步流程通、`/` 依身分回對應頁面、
角色不符會重導、白名單擋掉任意靜態檔。

覆蓋：
  [1] 匿名訪問：/ 、/admin 、/agent 、/ops → 302 /login；/login、/ui/common.js → 200
  [2] 白名單：/ui/<非白名單> → 404；/favicon.ico → 204
  [3] 登入兩步：多身分先 need_role+ticket → 帶 ticket+role 才建 session
  [4] 登入後：/api/auth/me 正確；/ 依身分回對應頁；/admin /agent /ops 都 200
  [5] 切換身分：home 正確、/ 跟著變、角色不符 302
  [6] CSRF 閘門：switch-role 缺 token → 403
  [7] 登出：/ 回到 302 /login

跑法（從專案根，用底層 python）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/pages_check.py [http://127.0.0.1:8020]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020").rstrip("/")
ROOT = Path(__file__).resolve().parents[2]

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
ADMIN_PW = ENV.get("CRM_ADMIN_PASSWORD", "")


def title(html: str) -> str:
    m = re.search(r"<title>([^<]*)</title>", html or "")
    return (m.group(1) if m else "").strip()


def is_redirect_to(resp, path: str) -> bool:
    return resp.status_code == 302 and path in (resp.headers.get("location") or "")


def main() -> int:
    print(f"BASE = {BASE}\n")
    if not ADMIN_PW:
        print("!! crm/.env 沒有 CRM_ADMIN_PASSWORD，無法測登入。")
        return 2

    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=15)

    # ---------------------------------------------------------- [1] 匿名
    print("[1] 匿名訪問（未登入）")
    check(c.get("/health").status_code == 200, "GET /health → 200")
    for p in ("/", "/admin", "/agent", "/ops"):
        r = c.get(p)
        check(is_redirect_to(r, "/login"), f"GET {p} → 302 /login", f"got {r.status_code} {r.headers.get('location')}")
    r = c.get("/login")
    check(r.status_code == 200 and title(r.text) == "登入 · 客服系統", "GET /login → 200 登入頁",
          f"{r.status_code} / {title(r.text)}")
    r = c.get("/ui/common.js")
    check(r.status_code == 200, "GET /ui/common.js → 200（白名單）")

    # ---------------------------------------------------------- [2] 白名單
    print("\n[2] 靜態資源白名單")
    r = c.get("/ui/admin.html")
    check(r.status_code == 404, "GET /ui/admin.html → 404（非白名單，不可直取）", f"got {r.status_code}")
    r = c.get("/favicon.ico")
    check(r.status_code == 204, "GET /favicon.ico → 204")
    # ★ 迴歸閘門：204 不可以帶 body。`JSONResponse(204, content=None)` 會產生 b'null'，
    #   而 204 不帶 content-length → uvicorn 判定 body 超長而斷連線（曾真實發生）。
    check(len(r.content) == 0, "favicon 回應沒有 body（204 帶 body 會讓 uvicorn 斷連線）",
          f"body={r.content[:20]!r}")

    # ---------------------------------------------------------- [3] 登入兩步
    print("\n[3] 登入（多身分兩步）")
    r = c.post("/api/auth/login", json={"account": "admin", "password": ADMIN_PW})
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check(r.status_code == 200 and d.get("need_role") is True, "第一步 → need_role + ticket",
          f"{r.status_code} need_role={d.get('need_role')}")
    ticket = d.get("ticket")
    roles = sorted(d.get("available_roles") or [])
    check(roles == ["admin", "agent", "ops"], "available_roles = admin/agent/ops", str(roles))
    check(bool(ticket), "回傳一次性 ticket")
    check(not any(k.lower() == "set-cookie" for k in r.headers.keys()),
          "第一步**不**下 cookie（還沒選身分）")

    # 錯誤 ticket
    r = c.post("/api/auth/login", json={"ticket": "bogus-ticket", "role": "admin"})
    check(r.status_code == 400, "錯誤 ticket → 400", f"got {r.status_code}")

    r2 = c.post("/api/auth/login", json={"ticket": ticket, "role": "admin"})
    d2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
    check(r2.status_code == 200 and d2.get("ok") is True, "第二步（ticket+role）→ 登入成功", f"{r2.status_code}")
    csrf = d2.get("csrf_token") or ""
    check(bool(csrf), "回傳 csrf_token")
    check(any(k.lower() == "set-cookie" for k in r2.headers.keys()), "設定 session cookie")

    # ticket 用過即失效
    r = c.post("/api/auth/login", json={"ticket": ticket, "role": "admin"})
    check(r.status_code == 400, "ticket 用過即失效 → 400", f"got {r.status_code}")

    # ---------------------------------------------------------- [4] 登入後
    print("\n[4] 登入後（身分=admin）")
    r = c.get("/api/auth/me")
    d = r.json() if r.status_code == 200 else {}
    me = d.get("staff") or {}          # ★ /me 回 {ok, staff:{...}, csrf_token}，欄位在 staff 底下
    check(r.status_code == 200 and me.get("active_role") == "admin", "GET /api/auth/me → active_role=admin",
          str(me.get("active_role")))
    check(me.get("can_switch") is True, "can_switch=true（一帳號多身分）")
    check(me.get("home") == "/admin", "home=/admin", str(me.get("home")))
    check(sorted(me.get("available_roles") or []) == ["admin", "agent", "ops"],
          "available_roles = admin/agent/ops", str(me.get("available_roles")))
    check(bool(d.get("csrf_token")), "回傳 csrf_token")

    # ★ 一次只生效一個身分：admin 身分下只有 / 與 /admin 屬它，/agent /ops → 302 回首頁
    for p, t in (("/", "管理員後台"), ("/admin", "管理員後台")):
        r = c.get(p)
        check(r.status_code == 200 and title(r.text) == t,
              f"GET {p} → 200《{t}》", f"{r.status_code} / {title(r.text)}")
    for p in ("/agent", "/ops"):
        r = c.get(p)
        check(r.status_code == 302, f"admin 身分 GET {p} → 302（屬其他身分）",
              f"{r.status_code} {r.headers.get('location')}")

    # ---------------------------------------------------------- [5] 切換身分
    print("\n[5] 切換身分")
    for role, home, t in (("agent", "/agent", "客服工作台"), ("ops", "/ops", "運維"), ("admin", "/admin", "管理員後台")):
        r = c.post("/api/auth/switch-role", json={"role": role}, headers={"X-CSRF-Token": csrf})
        d = r.json() if r.status_code == 200 else {}
        check(r.status_code == 200 and d.get("home") == home, f"切到 {role} → home={home}", str(d.get("home")))
        r = c.get("/api/auth/me")
        cur = ((r.json() or {}).get("staff") or {}).get("active_role")
        check(cur == role, f"/me 的 active_role 變成 {role}", str(cur))
        r = c.get("/")
        check(r.status_code == 200 and title(r.text) == t, f"切到 {role} 後 GET / → 《{t}》",
              f"{r.status_code} / {title(r.text)}")

    # 角色不符：admin 之外的頁面在 agent 身分下應重導
    c.post("/api/auth/switch-role", json={"role": "agent"}, headers={"X-CSRF-Token": csrf})
    r = c.get("/admin")
    check(is_redirect_to(r, "/") or r.status_code == 302, "agent 身分 GET /admin → 302（角色不符）",
          f"got {r.status_code} {r.headers.get('location')}")
    r = c.get("/ops")
    check(r.status_code == 302, "agent 身分 GET /ops → 302", f"got {r.status_code}")
    r = c.get("/agent")
    check(r.status_code == 200, "agent 身分 GET /agent → 200")
    c.post("/api/auth/switch-role", json={"role": "admin"}, headers={"X-CSRF-Token": csrf})

    # ---------------------------------------------------------- [6] CSRF
    print("\n[6] CSRF 閘門")
    r = c.post("/api/auth/switch-role", json={"role": "agent"})
    check(r.status_code == 403, "switch-role 缺 X-CSRF-Token → 403", f"got {r.status_code}")
    r = c.post("/api/auth/switch-role", json={"role": "agent"}, headers={"X-CSRF-Token": "wrong"})
    check(r.status_code == 403, "switch-role 錯誤 token → 403", f"got {r.status_code}")

    # 角色不存在
    r = c.post("/api/auth/switch-role", json={"role": "root"}, headers={"X-CSRF-Token": csrf})
    check(r.status_code in (400, 403), "切到不存在的角色 → 拒絕", f"got {r.status_code}")

    # ---------------------------------------------------------- [7] 登出
    print("\n[7] 登出")
    r = c.post("/api/auth/logout")
    check(r.status_code == 200, "POST /api/auth/logout → 200", f"got {r.status_code}")
    r = c.get("/")
    check(is_redirect_to(r, "/login"), "登出後 GET / → 302 /login", f"got {r.status_code}")
    r = c.get("/api/auth/me")
    check(r.status_code in (401, 403), "登出後 /api/auth/me → 未授權", f"got {r.status_code}")

    # ---------------------------------------------------------- 收尾
    c.close()
    print(f"\n結果：{'PASS' if not fails else 'FAIL'}  （PASS {oks[0]} / FAIL {len(fails)}）")
    for f in fails:
        print("   FAIL:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
