"""自檢：客服自助改密碼 + 「管理員看不到密碼、但能重設」的完整語義。

★ 需求（2026-10-29 小jo）：
  「我想讓每個客服可以修改自己的密碼，這個密碼對於平臺管理員不可見，
    但是平臺管理員可以修改每個客服的密碼」

  三句話拆成三個可驗證的性質：
    P1 客服能改**自己**的密碼（要驗舊密碼）
    P2 平台管理員**看不到**任何人的密碼（連 hash 都拿不到）
    P3 平台管理員能**重設**每個客服的密碼（不需要舊密碼）

本自檢分兩段：
  [A] 靜態：三張頁面都有常駐的「修改密碼」入口；端點與前端元件都在
  [B] 真實 HTTP：直接打 `/api/auth/password` 與 `/api/admin/staff/password`
      · 舊密碼錯 → 400（且告知剩餘次數）
      · 新密碼太短 / 與舊相同 → 400
      · 正確 → 200；舊密碼失效、新密碼可用
      · 當前 session 保留、其他裝置 session 被踢
      · 審計有記、且**不含任何形式的密碼**
      · 管理員列表 / `/api/auth/me` 都沒有 password_hash
      · P3：管理員不改舊密碼就能重設客服密碼，且客服被踢下線

跑法（repo 根；CRM 服務要在跑）：
    PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
      PYTHONIOENCODING=utf-8 NO_PROXY=127.0.0.1,localhost \\
      <python> crm/tests/password_change_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.core import config  # noqa: E402
from app.core.security import hash_password, verify_password  # noqa: E402

CRM = "http://127.0.0.1:8020"
UI = ROOT / "crm" / "ui"
PREFIX = "__CK_pwd"
OLD_PW = "old-pass-1234"
NEW_PW = "new-pass-5678"

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


# ---------------------------------------------------------------- HTTP 小工具

def _req(path: str, *, sid: str = "", method: str = "GET", body=None,
         csrf: str = "") -> tuple[int, dict]:
    url = CRM + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if sid:
        headers["Cookie"] = f"{config.SESSION_COOKIE_NAME}={sid}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # ★ 繞開系統代理
    try:
        with opener.open(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, {"_raw": raw[:300]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"_raw": raw[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"_err": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- DB 小工具

def _db():
    return psycopg2.connect(config.DATABASE_URL)


def _mk_staff(login: str, roles: list[str], password: str) -> dict | None:
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles, enabled)"
                " VALUES (%s, %s, %s, %s, true)"
                " ON CONFLICT (login_account) DO UPDATE SET password_hash = EXCLUDED.password_hash,"
                "   roles = EXCLUDED.roles, enabled = true"
                " RETURNING id, login_account, staff_name, roles, is_super_agent, is_kb_editor",
                (login, hash_password(password), login.replace(PREFIX, ""), roles),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def _hash_of(staff_id: int) -> str:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM crm_staff WHERE id = %s", (staff_id,))
            r = cur.fetchone()
            return r[0] if r else ""
    finally:
        conn.close()


def _audit_rows(staff_id: int) -> list[dict]:
    """取「與這個員工有關」的密碼審計。

    ★★ 注意兩個 staff_id 的語義不同（這裡踩過）：
      · 欄位 `staff_id`      = **操作者**（自己改 → 本人；管理員重設 → 管理員）
      · `payload.staff_id`   = **被操作對象**（管理員重設時的目標客服）
    只查欄位 `staff_id` 會漏掉「管理員重設別人」那條 —— 錯的是查詢，不是功能。
    """
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT operate_type, payload::text AS payload, staff_id,"
                "       payload->>'staff_id' AS target"
                "  FROM crm_audit_log"
                " WHERE operate_type IN ('staff_password_change','staff_password_reset')"
                "   AND (staff_id = %s OR (payload->>'staff_id')::bigint = %s)"
                " ORDER BY id DESC LIMIT 20", (staff_id, staff_id))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _drop() -> None:
    conn = _db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_session WHERE staff_id IN"
                        " (SELECT id FROM crm_staff WHERE login_account LIKE %s)",
                        (PREFIX + "%",))
            cur.execute("DELETE FROM crm_audit_log WHERE staff_id IN"
                        " (SELECT id FROM crm_staff WHERE login_account LIKE %s)",
                        (PREFIX + "%",))
            cur.execute("DELETE FROM crm_staff WHERE login_account LIKE %s", (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- [A] 靜態

def part_a() -> None:
    print("[A] 靜態：三張頁面都從「⋮」選單進得到修改密碼")

    for name, label in (("console_ui.html", "客服台"),
                        ("admin.html", "管理台"),
                        ("ops.html", "運維台")):
        html = (UI / name).read_text(encoding="utf-8")
        print(f"\n  -- {name}（{label}）")
        # ★ 2026-10-29 小jo 要求把兩顆大按鈕收進「用戶信息旁的三點選單」。
        #   入口從 #btnPwd 改成 #btnMore → 選單 → 修改密碼。
        check('id="btnMore"' in html, "有 #btnMore（三點入口）")
        check("UserMenu.mount" in html, "有接 UserMenu（選單由它渲染）")
        check('id="moreMenu"' in html, "有 #moreMenu 容器")
        # 舊的常駐按鈕應該已經收掉（否則會出現「兩套入口」）
        check('id="btnPwd"' not in html, "舊的 #btnPwd 已移除（不重複入口）")

        # ★ 核心不變：入口不能在 #roleMenu（隱藏選單）之內，否則單身分點不到
        i_menu = html.find('id="roleMenu"')
        i_btn = html.find('id="btnMore"')
        if i_menu == -1:
            check(True, "#roleMenu 不存在（無隱藏選單風險）")
        else:
            inside = False
            if i_btn > i_menu:           # ★ 必須在之後才可能落在裡面
                depth, j, end = 0, html.find(">", i_menu) + 1, -1
                while j < len(html):
                    if html.startswith("<div", j):
                        depth += 1
                    elif html.startswith("</div>", j):
                        if depth == 0:
                            end = j
                            break
                        depth -= 1
                    j += 1
                inside = end != -1 and i_menu < i_btn < end
            check(not inside, "★ 三點入口**不在** #roleMenu 內（單身分也點得到）",
                  f"menu={i_menu} btn={i_btn}")

    print("\n  -- common.js")
    js = (UI / "common.js").read_text(encoding="utf-8")
    check("global.PwdDialog" in js, "導出 PwdDialog")
    check("/api/auth/password" in js, "打 /api/auth/password")
    check("confirm(" not in js, "沒有用原生 confirm（本專案禁止）")
    check("iNew2" in js and "不一致" in js, "有「兩次新密碼要一致」的校驗")
    check("current-password" in js, "舊密碼框 autocomplete=current-password")
    check(js.count("autocomplete=\"new-password\"") >= 2, "兩個新密碼框都是 new-password")
    check("管理員看不到你的密碼" in js, "★ 彈窗內明說「管理員看不到你的密碼」")

    print("\n  -- 後端")
    rp = (ROOT / "crm" / "app" / "auth" / "router.py").read_text(encoding="utf-8")
    check('@router.post("/password")' in rp, "有 POST /api/auth/password")
    check("verify_password(old" in rp, "★ 有驗舊密碼")
    check("delete_staff_sessions_except" in rp, "改完只踢其他 session（保留當前）")
    au = (ROOT / "crm" / "app" / "audit.py").read_text(encoding="utf-8")
    check("staff_password_change" in au, "審計類型有 staff_password_change")
    # 需求 P2：管理員看不到密碼 —— 列表查詢不能選 password_hash
    ar = (ROOT / "crm" / "app" / "admin" / "repo.py").read_text(encoding="utf-8")
    seg = ar[ar.find("def list_staff"):]
    seg = seg[:seg.find("def create_staff")] if "def create_staff" in seg else seg
    check("password_hash" not in seg, "★ 管理員員工列表**不查** password_hash")


# ---------------------------------------------------------------- [B] 真實 HTTP

def part_b() -> None:
    print("\n[B] 真實 HTTP：P1 自助改密碼 / P2 管理員看不到 / P3 管理員可重設")

    # --- 造一個客服 + 一個管理員
    login = PREFIX + "_agent__"
    st = _mk_staff(login, ["agent"], OLD_PW)
    if not st:
        check(False, "造客服帳號失敗")
        return
    sid_a, csrf_a = sess_mod.create_session(st, "agent")
    sid_b, _csrf_b = sess_mod.create_session(st, "agent")   # 模擬「另一台裝置」
    print(f"\n  客服 {login} id={st['id']}；已開 2 個 session")

    try:
        # ---- 沒有 CSRF → 403
        code, d = _req("/api/auth/password", sid=sid_a, method="POST",
                       body={"old_password": OLD_PW, "new_password": NEW_PW})
        check(code == 403, "沒帶 CSRF → 403", f"HTTP {code}")

        # ---- 未登入 → 401
        code, d = _req("/api/auth/password", method="POST", csrf="x",
                       body={"old_password": OLD_PW, "new_password": NEW_PW})
        check(code in (401, 403), "未登入 → 401/403", f"HTTP {code}")

        # ---- 舊密碼錯 → 400（含剩餘次數）
        code, d = _req("/api/auth/password", sid=sid_a, method="POST", csrf=csrf_a,
                       body={"old_password": "wrong-pass", "new_password": NEW_PW})
        check(code == 400, "舊密碼錯 → 400", f"HTTP {code} {d.get('error','')}")
        check("還可嘗試" in (d.get("error") or ""), "錯誤訊息有剩餘次數",
              (d.get("error") or "")[:40])
        check(verify_password(OLD_PW, _hash_of(st["id"])), "失敗後舊密碼仍然有效（沒被改壞）")

        # ---- 新密碼太短 → 400
        code, d = _req("/api/auth/password", sid=sid_a, method="POST", csrf=csrf_a,
                       body={"old_password": OLD_PW, "new_password": "1234567"})
        check(code == 400 and "8" in (d.get("error") or ""), "新密碼 7 位 → 400",
              f"HTTP {code} {d.get('error','')}")

        # ---- 新密碼與舊相同 → 400
        code, d = _req("/api/auth/password", sid=sid_a, method="POST", csrf=csrf_a,
                       body={"old_password": OLD_PW, "new_password": OLD_PW})
        check(code == 400 and "相同" in (d.get("error") or ""), "新密碼＝舊密碼 → 400",
              f"HTTP {code} {d.get('error','')}")

        # ---- 正確改密碼 → 200
        code, d = _req("/api/auth/password", sid=sid_a, method="POST", csrf=csrf_a,
                       body={"old_password": OLD_PW, "new_password": NEW_PW})
        check(code == 200 and d.get("ok"), "★ 正確改密碼 → 200",
              f"HTTP {code} {d}")
        h = _hash_of(st["id"])
        check(verify_password(NEW_PW, h), "★ 新密碼已生效")
        check(not verify_password(OLD_PW, h), "★ 舊密碼已失效")
        check(h.startswith("pbkdf2_sha256$"), "存的是 PBKDF2 雜湊（不是明文）",
              h[:24] + "…")
        check(NEW_PW not in h, "★ 雜湊裡不含明文密碼")

        # ---- 當前 session 保留、其他裝置被踢
        code, _ = _req("/api/auth/me", sid=sid_a)
        check(code == 200, "★ 改完當前 session 仍有效（沒把自己踢出去）")
        code, _ = _req("/api/auth/me", sid=sid_b)
        check(code in (401, 403), "★ 另一台裝置的 session 已被踢掉", f"HTTP {code}")
        check(d.get("killed_sessions", 0) >= 1,
              "回報踢掉幾個 session", f"killed={d.get('killed_sessions')}")

        # ---- 審計：有記、無密碼
        rows = _audit_rows(st["id"])
        chg = [r for r in rows if r["operate_type"] == "staff_password_change"]
        check(bool(chg), "★ 審計有記 staff_password_change", f"{len(rows)} 筆")
        if chg:
            blob = json.dumps(chg, ensure_ascii=False)
            check(NEW_PW not in blob, "★ 審計不含新密碼明文")
            check(OLD_PW not in blob, "★ 審計不含舊密碼明文")
            check("pbkdf2" not in blob, "★ 審計不含雜湊")

        # ---- P2：管理員看不到密碼
        adm_login = PREFIX + "_admin__"
        adm = _mk_staff(adm_login, ["admin"], "admin-pass-9999")
        sid_adm, csrf_adm = sess_mod.create_session(adm, "admin") if adm else ("", "")
        if adm:
            code, d = _req("/api/admin/staff", sid=sid_adm)
            items = (d or {}).get("items") or (d or {}).get("data") or []
            blob = json.dumps(d, ensure_ascii=False)
            check(code == 200, "管理員拿得到員工列表", f"HTTP {code}")
            check("password_hash" not in blob and "password" not in blob.lower(),
                  "★ 員工列表裡沒有任何密碼欄位")
            check(NEW_PW not in blob, "★ 列表裡看不到該客服的密碼")
            me_code, me_d = _req("/api/auth/me", sid=sid_a)
            check(me_code == 200 and "password" not in json.dumps(me_d).lower(),
                  "★ /api/auth/me 也沒有密碼欄位")

            # ---- P3：管理員不需舊密碼就能重設
            reset_to = "reset-by-admin-77"
            code, d = _req("/api/admin/staff/password", sid=sid_adm, method="POST",
                           csrf=csrf_adm, body={"id": st["id"], "password": reset_to})
            check(code == 200 and d.get("ok"), "★ P3 管理員可重設客服密碼（不用舊密碼）",
                  f"HTTP {code} {d.get('error','')}")
            h2 = _hash_of(st["id"])
            check(verify_password(reset_to, h2), "★ 重設後新密碼生效")
            check(not verify_password(NEW_PW, h2), "重設後舊（客服自設的）密碼失效")
            code, _ = _req("/api/auth/me", sid=sid_a)
            check(code in (401, 403), "★ 被管理員重設後，客服 session 被踢下線",
                  f"HTTP {code}")
            rows = _audit_rows(st["id"])
            rst = [r for r in rows if r["operate_type"] == "staff_password_reset"]
            check(bool(rst), "★ 審計有記 staff_password_reset")
            if rst:
                check(reset_to not in json.dumps(rst, ensure_ascii=False),
                      "★ 管理員重設也不落明文")

            # ---- 管理員重設也要擋短密碼
            code, d = _req("/api/admin/staff/password", sid=sid_adm, method="POST",
                           csrf=csrf_adm, body={"id": st["id"], "password": "123"})
            check(code == 400, "管理員重設短密碼 → 400", f"HTTP {code}")
        else:
            check(False, "造管理員帳號失敗")

    finally:
        for s in (sid_a, sid_b, locals().get("sid_adm", "")):
            try:
                sess_mod.delete_session(s)
            except Exception:  # noqa: BLE001
                pass
        _drop()


def part_c() -> None:
    """[C] 真瀏覽器：三張頁面的彈窗真的打得開、校驗訊息對、能改成功。"""
    print("\n[C] 真瀏覽器：三張頁面的「修改密碼」彈窗")

    chrome = ""
    for p in (Path("C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe"),
              Path("C:/Program Files/Google/Chrome/Application/chrome.exe")):
        if p.exists():
            chrome = str(p).replace("\\", "/")
            break
    if not chrome:
        check(False, "找不到 Chrome，略過瀏覽器驗證")
        return

    js = Path(__file__).with_name("_pwd_dialog_ui.js")
    if not js.exists():
        check(False, "缺 _pwd_dialog_ui.js")
        return

    ui_old, ui_new = "ui-old-pass-4321", "ui-new-pass-8765"
    login = PREFIX + "_ui__"
    st = _mk_staff(login, ["agent"], ui_old)
    if not st:
        check(False, "造瀏覽器測試帳號失敗")
        return
    sid, _csrf = sess_mod.create_session(st, "agent")
    sid_adm = sid_ops = ""

    try:
        cases = [{"sid": sid, "label": login + " 客服台", "path": "/agent"}]
        # 另外兩張頁面用臨時 admin / ops 帳號開（只驗彈窗能開，不改密碼）
        for lp, roles, home in ((PREFIX + "_uiadmin__", ["admin"], "/admin"),
                                (PREFIX + "_uiops__", ["ops"], "/ops")):
            s2 = _mk_staff(lp, roles, "tmp-pass-1234")
            if not s2:
                continue
            s, _c = sess_mod.create_session(s2, roles[0])
            if roles[0] == "admin":
                sid_adm = s
            else:
                sid_ops = s
            cases.append({"sid": s, "label": lp + " " + home, "path": home})

        env = dict(os.environ)
        # ★ playwright 啟動時要在 temp 建 `playwright-artifacts-*`；系統 temp
        #   在本機被安全策略擋掉（EPERM mkdtemp），改用專案內的可寫目錄。
        tmp = ROOT / ".tmp_pw"
        tmp.mkdir(exist_ok=True)
        env.update({
            "CK_CHROME": chrome,
            "CK_CASES": json.dumps(cases, ensure_ascii=False),
            "CK_OLD": ui_old,
            "CK_NEW": ui_new,
            "NODE_PATH": "C:/Users/its-admin/.workbuddy/binaries/node/workspace/node_modules",
            "NO_PROXY": "127.0.0.1,localhost",
            "TMP": str(tmp).replace("\\", "/"),
            "TEMP": str(tmp).replace("\\", "/"),
            "TMPDIR": str(tmp).replace("\\", "/"),
        })
        r = subprocess.run(["node", str(js)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            if not line.startswith("__RESULT__"):
                print(line)
        m = None
        for line in out.splitlines():
            if line.startswith("__RESULT__"):
                try:
                    m = json.loads(line[len("__RESULT__"):])
                except Exception:  # noqa: BLE001
                    m = None
        if m is None:
            check(False, "Node 沒吐 RESULT", out[-400:])
            return
        global _pass
        _pass += int(m["pass"])
        for msg in m["fails"]:
            _fail.append(msg)
        check(len(m["fails"]) == 0, "瀏覽器斷言全過", f"fail={len(m['fails'])}")

        # 真的改過去了？回 DB 獨立驗一次（不信前端）
        if m.get("changed"):
            h = _hash_of(st["id"])
            check(verify_password(ui_new, h), "★ 瀏覽器改的密碼真的落庫了")
            check(not verify_password(ui_old, h), "★ 瀏覽器改完舊密碼已失效")
    finally:
        for s in (sid, sid_adm, sid_ops):
            try:
                sess_mod.delete_session(s)
            except Exception:  # noqa: BLE001
                pass
        _drop()


def main() -> int:
    part_a()
    part_b()
    part_c()
    print(f"\n{'=' * 62}")
    print(f"  自助改密碼 / 管理員重設：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        for m in _fail:
            print("    FAIL:", m)
    return 0 if not _fail else 1


if __name__ == "__main__":
    sys.exit(main())
