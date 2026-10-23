"""階段一自檢：登入 / Session / CSRF / RBAC / 管理員後台 CRUD。

跑法（從專案根；專案路徑含中文，`.venv` python.exe 起不來，用底層 python）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/auth_check.py [--password <admin密碼>]

用 `fastapi.testclient` 真打路由（含 lifespan 建表），不是「應該可以」。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import config  # noqa: E402
import main as crm_main  # noqa: E402

TAG = "ZZ測試"
TEST_ACC = "zz_auth_check_agent"
MULTI_ACC = "zz_auth_check_multi"      # 一帳號多身分（admin + ops）

fails: list[str] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def db():
    return psycopg2.connect(config.DATABASE_URL)


def cleanup():
    """清掉本腳本可能留下的測試資料（可重複執行）。"""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _ids AS SELECT id FROM crm_staff WHERE login_account IN (%s, %s)",
                        (TEST_ACC, MULTI_ACC))
            cur.execute("DELETE FROM crm_session WHERE staff_id IN (SELECT id FROM _ids)")
            cur.execute("DELETE FROM crm_staff_business_permission WHERE staff_id IN (SELECT id FROM _ids)")
            cur.execute("DELETE FROM crm_staff WHERE id IN (SELECT id FROM _ids)")
            cur.execute("CREATE TEMP TABLE _bids AS SELECT id FROM crm_business WHERE business_name LIKE %s", (TAG + "%",))
            cur.execute("DELETE FROM business_ai_switch WHERE business_id IN (SELECT id FROM _bids)")
            cur.execute("DELETE FROM crm_business_platform_account WHERE business_id IN (SELECT id FROM _bids)")
            cur.execute("DELETE FROM crm_staff_business_permission WHERE business_id IN (SELECT id FROM _bids)")
            cur.execute("DELETE FROM crm_business WHERE id IN (SELECT id FROM _bids)")
        conn.commit()
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="admin")
    ap.add_argument("--password", default=os.environ.get("CRM_ADMIN_PASSWORD", ""))
    args = ap.parse_args()
    if not args.password:
        print("  !! 沒給 admin 密碼：加 --password，或在 crm/.env 設 CRM_ADMIN_PASSWORD。")
        sys.exit(2)

    cleanup()

    with TestClient(crm_main.app, follow_redirects=False) as c:
        # ---------------- 未登入 ----------------
        print("\n[1] 未登入的存取控制")
        r = c.get("/")
        check(r.status_code == 302 and r.headers.get("location") == "/login", "GET / 未登入 → 302 /login")
        r = c.get("/api/admin/businesses")
        check(r.status_code == 401, f"GET /api/admin/businesses 未登入 → 401（實得 {r.status_code}）")
        r = c.get("/api/auth/me")
        check(r.status_code == 401, f"GET /api/auth/me 未登入 → 401（實得 {r.status_code}）")

        # ---------------- 登入 ----------------
        # admin 是「超集」→ 可選身分 = [admin, agent, ops] → 登入走兩步（先選身分）
        print("\n[2] 登入（admin 超集 → 先選身分再登入）")
        r = c.post("/api/auth/login", json={"account": args.account, "password": "definitely-wrong"})
        check(r.status_code == 401, f"錯誤密碼 → 401（實得 {r.status_code}）")
        check("還可嘗試" in (r.json().get("error") or ""), "錯誤訊息含剩餘次數")

        r = c.post("/api/auth/login", json={"account": args.account, "password": args.password})
        if r.status_code != 200:
            print(f"  !! 登入失敗：{r.status_code} {r.text[:200]}")
            print("  !! 若你改過 admin 密碼，請用 --password 指定。")
            sys.exit(2)
        body = r.json()
        if body.get("need_role"):
            check(sorted(body.get("available_roles") or []) == ["admin", "agent", "ops"],
                  f"admin 為超集 → 可選 admin/agent/ops（實得 {body.get('available_roles')}）")
            r = c.post("/api/auth/login", json={"ticket": body["ticket"], "role": "admin"})
            if r.status_code != 200:
                print(f"  !! 選身分登入失敗：{r.status_code} {r.text[:200]}")
                sys.exit(2)
            body = r.json()
        csrf = body["csrf_token"]
        check(bool(csrf), "登入成功並取得 csrf_token")
        check(config.SESSION_COOKIE_NAME in c.cookies, f"已下發 Cookie {config.SESSION_COOKIE_NAME}")

        r = c.get("/api/auth/me")
        check(r.status_code == 200 and r.json()["staff"]["login_account"] == args.account, "GET /api/auth/me 回自身身分")

        # ---------------- CSRF ----------------
        print("\n[3] CSRF")
        r = c.post("/api/admin/businesses", json={"business_name": TAG + "無CSRF"})
        check(r.status_code == 403, f"缺 X-CSRF-Token → 403（實得 {r.status_code}）")
        r = c.post("/api/admin/businesses", json={"business_name": TAG + "錯CSRF"},
                   headers={"X-CSRF-Token": "wrong-token"})
        check(r.status_code == 403, f"錯 X-CSRF-Token → 403（實得 {r.status_code}）")

        H = {"X-CSRF-Token": csrf}

        # ---------------- 商家 ----------------
        print("\n[4] 商家 CRUD")
        r = c.post("/api/admin/businesses", json={"business_name": TAG + "商家A", "business_memo": "自檢"},
                   headers=H)
        check(r.status_code == 200 and r.json().get("ok"), "建立商家成功")
        biz_id = r.json()["id"]

        r = c.get("/api/admin/businesses")
        names = [b["business_name"] for b in r.json()["items"]]
        check(TAG + "商家A" in names, "商家列表含新建商家")

        r = c.post("/api/admin/businesses/update",
                   json={"id": biz_id, "business_name": TAG + "商家A改", "enabled": True}, headers=H)
        check(r.status_code == 200, "更新商家成功")

        r = c.post("/api/admin/businesses", json={"business_name": "  "}, headers=H)
        check(r.status_code == 400, f"空名稱 → 400（實得 {r.status_code}）")

        # ---------------- 平台帳號 ----------------
        print("\n[5] 平台帳號綁定")
        r = c.post("/api/admin/accounts",
                   json={"business_id": biz_id, "platform": "whatsapp",
                         "platform_business_account_id": "zz_native_1", "display_name": "自檢號"},
                   headers=H)
        check(r.status_code == 200, "綁定平台帳號成功")
        acct_id = r.json()["id"]

        r = c.post("/api/admin/accounts",
                   json={"business_id": biz_id, "platform": "whatsapp",
                         "platform_business_account_id": "zz_native_1"},
                   headers=H)
        check(r.status_code == 409, f"同平台同原生 ID 重複綁定 → 409（實得 {r.status_code}）")

        r = c.post("/api/admin/accounts",
                   json={"business_id": biz_id, "platform": "telegram",
                         "platform_business_account_id": "zz_native_2"}, headers=H)
        check(r.status_code == 400, f"不合法平台 → 400（實得 {r.status_code}）")

        # ---------------- 員工與 RBAC ----------------
        print("\n[6] 員工建立與 RBAC")
        r = c.post("/api/admin/staff",
                   json={"login_account": TEST_ACC, "password": "agentpw12345", "staff_name": "自檢坐席",
                         "roles": ["agent"], "is_super_agent": False, "is_kb_editor": True}, headers=H)
        check(r.status_code == 200, f"建立 agent 員工成功（實得 {r.status_code} {r.text[:120]}）")
        staff_id = r.json()["id"]

        r = c.post("/api/admin/staff",
                   json={"login_account": TEST_ACC, "password": "agentpw12345", "staff_name": "重複",
                         "roles": ["agent"]}, headers=H)
        check(r.status_code == 409, f"重複帳號 → 409（實得 {r.status_code}）")

        r = c.post("/api/admin/staff",
                   json={"login_account": "zz_short", "password": "123", "staff_name": "短密碼",
                         "roles": ["agent"]}, headers=H)
        check(r.status_code == 400, f"密碼過短 → 400（實得 {r.status_code}）")

        r = c.post("/api/admin/staff",
                   json={"login_account": "zz_norole", "password": "agentpw12345", "staff_name": "無角色",
                         "roles": []}, headers=H)
        check(r.status_code == 400, f"無角色 → 400（實得 {r.status_code}）")

        # ---------------- 一帳號多身分：登入選身分 + 頭像切換 ----------------
        print("\n[6b] 一帳號多身分：登入選身分與切換")
        r = c.post("/api/admin/staff",
                   json={"login_account": MULTI_ACC, "password": "multipw12345", "staff_name": "自檢多身分",
                         "roles": ["admin", "ops"]}, headers=H)
        check(r.status_code == 200, f"建立多身分員工成功（實得 {r.status_code} {r.text[:120]}）")
        multi_id = r.json()["id"]

        c3 = TestClient(crm_main.app, follow_redirects=False)
        r = c3.post("/api/auth/login", json={"account": MULTI_ACC, "password": "multipw12345"})
        check(r.status_code == 200 and r.json().get("need_role") is True,
              f"多身分登入第一步回 need_role（實得 {r.status_code} {r.text[:140]}）")
        check(sorted(r.json().get("available_roles") or []) == ["admin", "agent", "ops"],
              f"含 admin → 超集展開為 admin/agent/ops（實得 {r.json().get('available_roles')}）")
        ticket = r.json().get("ticket") or ""
        check(bool(ticket), "取得一次性 ticket")
        check(config.SESSION_COOKIE_NAME not in c3.cookies, "選身分前不下發 Cookie（尚未建立 session）")
        r = c3.get("/api/auth/me")
        check(r.status_code == 401, f"選身分前沒有有效 session（實得 {r.status_code}）")

        # ★ 多身分不得繞過選身分：第一步即使夾帶 role，也必須回 need_role（伺服器端強制）
        cby = TestClient(crm_main.app, follow_redirects=False)
        r = cby.post("/api/auth/login",
                     json={"account": MULTI_ACC, "password": "multipw12345", "role": "admin"})
        check(r.status_code == 200 and r.json().get("need_role") is True,
              f"第一步夾帶 role 仍必須回 need_role（實得 {r.status_code} {r.text[:120]}）")
        check(config.SESSION_COOKIE_NAME not in cby.cookies, "繞過嘗試也不下發 Cookie")

        r = c3.post("/api/auth/login", json={"ticket": ticket, "role": "admin"})
        check(r.status_code == 200 and r.json()["staff"]["active_role"] == "admin",
              f"以 admin 身分完成登入（實得 {r.status_code} {r.text[:140]}）")
        check(config.SESSION_COOKIE_NAME in c3.cookies, "第二步登入後下發 Cookie")
        csrf3 = r.json()["csrf_token"]
        H3 = {"X-CSRF-Token": csrf3}
        check(r.json()["staff"]["can_switch"] is True, "多身分帳號 can_switch=true")

        r = c3.get("/api/admin/businesses")
        check(r.status_code == 200, "admin 身分可訪問管理端點")
        r = c3.get("/")
        check(r.status_code == 200 and 'id="ovBiz"' in r.text, "admin 身分首頁是管理員後台")

        # ---- 切換到 ops ----
        r = c3.post("/api/auth/switch-role", json={"role": "ops"}, headers=H3)
        check(r.status_code == 200 and r.json().get("home") == "/ops",
              f"切換到 ops 回 home=/ops（實得 {r.status_code} {r.text[:120]}）")
        r = c3.get("/api/auth/me")
        check(r.json()["staff"]["active_role"] == "ops", "me 顯示當前身分＝ops")
        r = c3.get("/api/admin/businesses")
        check(r.status_code == 403, f"ops 身分不能訪問管理端點（實得 {r.status_code}）")
        r = c3.get("/")
        check(r.status_code == 200 and 'id="ovTask"' in r.text,
              "ops 身分首頁是運維頁（ops.html）")
        r = c3.get("/admin")
        check(r.status_code == 302 and r.headers.get("location") == "/", "ops 身分訪問 /admin → 302 回首頁")

        # ---- 切回 admin ----
        r = c3.post("/api/auth/switch-role", json={"role": "admin"}, headers=H3)
        check(r.status_code == 200 and r.json().get("home") == "/admin", "切回 admin 回 home=/admin")
        r = c3.get("/api/admin/businesses")
        check(r.status_code == 200, "切回後恢復管理端權限")

        # ---- 拒絕與邊界 ----
        r = c3.post("/api/auth/switch-role", json={"role": "zzz"}, headers=H3)
        check(r.status_code == 403, f"切換到不存在的身分 → 403（實得 {r.status_code}）")
        r = c3.post("/api/auth/switch-role", json={"role": "ops"})
        check(r.status_code == 403, f"切換缺 CSRF → 403（實得 {r.status_code}）")
        r = c3.post("/api/auth/login", json={"ticket": "not-a-real-ticket", "role": "admin"})
        check(r.status_code == 400, f"無效 ticket → 400（實得 {r.status_code}）")

        c4 = TestClient(crm_main.app, follow_redirects=False)
        r = c4.post("/api/auth/login", json={"account": MULTI_ACC, "password": "multipw12345"})
        tk2 = r.json()["ticket"]
        r = c4.post("/api/auth/login", json={"ticket": tk2, "role": "ops"})
        check(r.status_code == 200, "以 ops 身分完成登入（第二個 session）")
        r = c4.post("/api/auth/login", json={"ticket": tk2, "role": "admin"})
        check(r.status_code == 400, f"ticket 只能使用一次（實得 {r.status_code}）")

        # ---- 角色變更即時生效：當前身分被移除 → 自動退回 ----
        r = c.post("/api/admin/staff/update",
                   json={"id": multi_id, "staff_name": "自檢多身分", "roles": ["ops"], "enabled": True},
                   headers=H)
        check(r.status_code == 200, "admin 把該帳號角色改為只剩 ops")
        r = c3.get("/api/auth/me")
        act = r.json()["staff"]["active_role"] if r.status_code == 200 else None
        check(r.status_code == 200 and act == "ops", f"當前身分（admin）被移除 → 自動退回 ops（實得 {act}）")
        check(r.json()["staff"]["can_switch"] is False, "只剩一個身分 → can_switch=false")
        r = c3.get("/api/admin/businesses")
        check(r.status_code == 403, f"即時生效後失去管理端權限（實得 {r.status_code}）")

        # ---- admin 超集：可切換全部身分，客服身分自動是超級坐席 ----
        print("\n[6c] admin 超集：可切換全部身分")
        c5 = TestClient(crm_main.app, follow_redirects=False)
        r = c5.post("/api/auth/login", json={"account": args.account, "password": args.password})
        check(r.status_code == 200 and r.json().get("need_role") is True,
              f"admin 登入第一步回 need_role（實得 {r.status_code} {r.text[:120]}）")
        tk = r.json().get("ticket") or ""
        r = c5.post("/api/auth/login", json={"ticket": tk, "role": "agent"})
        check(r.status_code == 200, f"admin 帳號可以『客服坐席』身分登入（實得 {r.status_code}）")
        st = r.json()["staff"]
        check(st["active_role"] == "agent", f"當前身分＝agent（實得 {st['active_role']}）")
        check(st["is_super_agent"] is True, "admin 的客服身分自動是超級坐席（可看全部商家）")
        check(st["can_switch"] is True, "admin 可切換身分")
        check(sorted(st["available_roles"]) == ["admin", "agent", "ops"], "admin 三個身分都可用")
        r = c5.get("/agent")
        check(r.status_code == 200, f"客服身分可進 /agent（實得 {r.status_code}）")
        r = c5.get("/")
        check(r.status_code == 200 and 'id="ovPost"' in r.text,
              "客服身分首頁顯示工作台（console_ui.html）")
        r = c5.get("/admin")
        check(r.status_code == 302 and r.headers.get("location") == "/",
              "客服身分訪問 /admin → 302 回首頁（切換後不再有管理權限）")

        # 授權該坐席只能看商家 A
        r = c.post("/api/admin/staff/permissions",
                   json={"staff_id": staff_id, "business_ids": [biz_id]}, headers=H)
        check(r.status_code == 200, "設定商家授權成功")

        # 以 agent 身分登入
        c2 = TestClient(crm_main.app, follow_redirects=False)
        r = c2.post("/api/auth/login", json={"account": TEST_ACC, "password": "agentpw12345"})
        check(r.status_code == 200, "agent 員工可登入")
        check(r.json()["staff"]["can_switch"] is False, "單一身分帳號 can_switch=false（不顯示切換入口）")
        check("need_role" not in r.json(), "單一身分帳號不會被要求選身分")
        csrf2 = r.json()["csrf_token"]

        r = c2.get("/api/admin/businesses")
        check(r.status_code == 403, f"agent 訪問管理端點 → 403（實得 {r.status_code}）")

        # agent 應該拿到客服工作台，而不是管理員後台（admin.html 的特徵節點）
        r = c2.get("/")
        check(r.status_code == 200 and 'id="ovPost"' in r.text and 'id="ovBiz"' not in r.text,
              "agent 首頁是客服工作台，不是管理員後台")

        # ---------------- 權限即時生效 / 踢下線 ----------------
        print("\n[7] 角色即時生效與踢下線")
        r = c2.get("/api/auth/me")
        check(r.status_code == 200, "踢下線前 agent session 有效")
        r = c.post("/api/admin/staff/kick", json={"id": staff_id}, headers=H)
        check(r.status_code == 200 and r.json().get("killed", 0) >= 1,
              f"踢下線回報清掉 {r.json().get('killed')} 個登入")
        r = c2.get("/api/auth/me")
        check(r.status_code == 401, f"踢下線後 agent session 立即失效（實得 {r.status_code}）")

        # 把自己停用應該被擋
        me = c.get("/api/auth/me").json()["staff"]
        r = c.post("/api/admin/staff/update",
                   json={"id": me["staff_id"], "staff_name": me["staff_name"],
                         "roles": ["admin"], "enabled": False}, headers=H)
        check(r.status_code == 400, f"停用自己 → 400（實得 {r.status_code}）")
        r = c.post("/api/admin/staff/update",
                   json={"id": me["staff_id"], "staff_name": me["staff_name"],
                         "roles": ["agent"], "enabled": True}, headers=H)
        check(r.status_code == 400, f"移除自己的 admin 角色 → 400（實得 {r.status_code}）")

        # ---------------- AI 開關 ----------------
        print("\n[8] AI 開關（第一層：商家 × 平台 × 場景）")
        r = c.get(f"/api/admin/ai-switch?business_id={biz_id}")
        items = r.json()["items"]
        check(len(items) == 6, f"回傳 6 種平台×場景組合（實得 {len(items)}）")
        # ★ 新商家建立時就寫入 6 把實列（全開）→ 與客服端讀的是同一份資料。
        #   不再有「讀不到就當 true」的 fallback，也沒有「預設」這個概念。
        check(all(i["enabled"] for i in items), "新建商家 → 6 把場景開關全部開啟（實列）")
        check(all("configured" not in i for i in items), "回傳不含 configured（不再有預設概念）")
        from app.autoreply import autoreply_db as _ardb
        same = all(_ardb.effective_scope_mode(biz_id, i["platform"], i["scope"]) == i["enabled"]
                   for i in items)
        check(same, "★ 管理員端顯示 == 客服端 effective_scope_mode（兩端同步）")

        r = c.post("/api/admin/ai-switch",
                   json={"business_id": biz_id, "platform": "ig", "scope": "comment", "enabled": False},
                   headers=H)
        check(r.status_code == 200, "單獨關閉 IG 留言開關")
        items = c.get(f"/api/admin/ai-switch?business_id={biz_id}").json()["items"]
        igc = [i for i in items if i["platform"] == "ig" and i["scope"] == "comment"][0]
        check(igc["enabled"] is False, "IG 留言＝關")
        check(_ardb.effective_scope_mode(biz_id, "ig", "comment") is False,
              "★ 客服端同步為關（不是各看各的）")

        r = c.post("/api/admin/ai-switch",
                   json={"business_id": biz_id, "platform": "wechat", "scope": "comment", "enabled": True},
                   headers=H)
        check(r.status_code == 400, f"微信+留言 不合法 → 400（實得 {r.status_code}）")

        r = c.post("/api/admin/ai-switch/bulk", json={"business_id": biz_id, "enabled": False}, headers=H)
        items = c.get(f"/api/admin/ai-switch?business_id={biz_id}").json()["items"]
        check(r.status_code == 200 and all(not i["enabled"] for i in items), "全部關閉（逐筆寫入 6 把）")

        # ---------------- 審計 ----------------
        print("\n[9] 審計日誌")
        r = c.get("/api/admin/audit?limit=200")
        types = [a["operate_type"] for a in r.json()["items"] if a["business_id"] == biz_id]
        for want in ("business_create", "platform_account_bind", "ai_switch_change"):
            check(want in types, f"審計含 {want}")
        r = c.get("/api/admin/audit?operate_type=login")
        check(any(a["operate_type"] == "login" for a in r.json()["items"]), "審計含 login")
        r = c.get("/api/admin/audit?operate_type=role_switch")
        check(any(a["operate_type"] == "role_switch" for a in r.json()["items"]), "審計含 role_switch")

        # ---------------- 登出 ----------------
        print("\n[10] 登出")
        r = c.post("/api/auth/logout")
        check(r.status_code == 200, "登出成功")
        r = c.get("/api/auth/me")
        check(r.status_code == 401, f"登出後 session 失效（實得 {r.status_code}）")

    cleanup()
    print("\n" + ("全部通過" if not fails else f"失敗 {len(fails)} 項："))
    for f in fails:
        print("  - " + f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
