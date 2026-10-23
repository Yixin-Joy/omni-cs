"""自檢：管理員端 AI 開關顯示 == 客服端實際生效狀態（不再有「預設」這層）。

驗的是三件事：
  1. 新建商家 → 6 把「平台 × 場景」開關**寫成實列且全開**（不是讀取時 fallback）。
  2. 管理員端 `/api/admin/ai-switch` 回的 enabled，與客服端
     `autoreply_db.effective_scope_mode()` 逐把相同（含單把關、全部關）。
  3. `backfill_ai_switches()`：只補缺列（舊商家全開），**不會把手動關掉的蓋回來**。

★ 不用密碼登入（小jo 改過 admin 密碼後 `.env` 的就過期了）：
  直接 `session.create_session()` 造 admin session + cookie。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/ai_switch_sync_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.autoreply import autoreply_db  # noqa: E402
from app.core import config  # noqa: E402
import main as crm_main  # noqa: E402

TAG = "ZZ開關同步測試"

fails: list[str] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def db():
    return psycopg2.connect(config.DATABASE_URL)


def cleanup() -> None:
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _bids AS SELECT id FROM crm_business"
                        " WHERE business_name LIKE %s", (TAG + "%",))
            cur.execute("DELETE FROM business_ai_switch WHERE business_id IN (SELECT id FROM _bids)")
            cur.execute("DELETE FROM crm_business WHERE id IN (SELECT id FROM _bids)")
        conn.commit()
    finally:
        conn.close()


def switches_in_db(bid: int) -> dict[tuple[str, str], bool]:
    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT platform, scope, enabled FROM business_ai_switch"
                        " WHERE business_id=%s", (bid,))
            return {(r["platform"], r["scope"]): bool(r["enabled"]) for r in cur.fetchall()}
    finally:
        conn.close()


def main() -> int:
    cleanup()

    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, login_account, staff_name, roles, is_super_agent,"
                        " is_kb_editor, enabled FROM crm_staff"
                        " WHERE 'admin' = ANY(roles) AND enabled ORDER BY id LIMIT 1")
            staff = cur.fetchone()
    finally:
        conn.close()
    if not staff:
        print("  !! 找不到可用的 admin 帳號，無法自檢。")
        return 2

    sid, csrf = sess_mod.create_session(dict(staff), "admin")
    H = {"X-CSRF-Token": csrf}

    try:
        with TestClient(crm_main.app, follow_redirects=False) as c:
            c.cookies.set(config.SESSION_COOKIE_NAME, sid,
                          domain="testserver.local", path="/")
            me = c.get("/api/auth/me")
            if me.status_code != 200:
                print(f"  !! 造 session 失敗：{me.status_code} {me.text[:200]}")
                return 2

            # ---------------- 1. 新商家 = 6 把全開（實列）
            print("\n[1] 新建商家 → 開關寫成實列且全開")
            r = c.post("/api/admin/businesses", json={"business_name": TAG + "A"}, headers=H)
            check(r.status_code == 200, f"建立測試商家（實得 {r.status_code}）")
            bid = r.json()["id"]

            saved = switches_in_db(bid)
            check(len(saved) == 6, f"DB 有 6 列 business_ai_switch（實得 {len(saved)}）")
            check(all(saved.values()), "6 把全部 = 開啟")

            items = c.get(f"/api/admin/ai-switch?business_id={bid}").json()["items"]
            check(len(items) == 6, f"管理員端回 6 把（實得 {len(items)}）")
            check(all(i["enabled"] for i in items), "管理員端顯示全部開啟")
            check(all("configured" not in i for i in items), "回傳不含 configured（UI 也沒有「預設」）")

            # ---------------- 2. 兩端逐把相同
            print("\n[2] 管理員端 == 客服端 effective_scope_mode")
            eff = {(p, s): autoreply_db.effective_scope_mode(bid, p, s)
                   for (p, s) in config.AUTOREPLY_SCOPES}
            check(all(eff[(i["platform"], i["scope"])] == i["enabled"] for i in items),
                  "初始狀態：6 把逐把一致（全開）")

            r = c.post("/api/admin/ai-switch",
                       json={"business_id": bid, "platform": "ig", "scope": "comment",
                             "enabled": False}, headers=H)
            check(r.status_code == 200, "關閉 IG 留言")
            items = c.get(f"/api/admin/ai-switch?business_id={bid}").json()["items"]
            igc = [i for i in items if i["platform"] == "ig" and i["scope"] == "comment"][0]
            check(igc["enabled"] is False, "管理員端 IG 留言 = 關")
            check(autoreply_db.effective_scope_mode(bid, "ig", "comment") is False,
                  "客服端同步 = 關（不是各看各的）")

            r = c.post("/api/admin/ai-switch/bulk",
                       json={"business_id": bid, "enabled": False}, headers=H)
            items = c.get(f"/api/admin/ai-switch?business_id={bid}").json()["items"]
            check(r.status_code == 200 and all(not i["enabled"] for i in items),
                  "全部關閉 → 管理員端 6 把全關")
            check(not any(autoreply_db.effective_scope_mode(bid, p, s)
                          for (p, s) in config.AUTOREPLY_SCOPES),
                  "全部關閉 → 客服端 6 把也全關")

            # ---------------- 3. backfill 只補缺列、不覆寫手動值
            print("\n[3] backfill_ai_switches：舊商家補齊、不蓋手動值")
            conn = db()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM business_ai_switch WHERE business_id=%s", (bid,))
                conn.commit()
            finally:
                conn.close()
            check(not switches_in_db(bid), "先清空（模擬舊商家沒有開關列）")

            n = autoreply_db.backfill_ai_switches()
            saved = switches_in_db(bid)
            check(n >= 6 and len(saved) == 6 and all(saved.values()),
                  f"backfill 補回 6 列且全開（補了 {n} 列）")

            conn = db()
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE business_ai_switch SET enabled=false"
                                " WHERE business_id=%s AND platform='fb' AND scope='dm'", (bid,))
                conn.commit()
            finally:
                conn.close()
            autoreply_db.backfill_ai_switches()
            saved = switches_in_db(bid)
            check(saved[("fb", "dm")] is False, "★ backfill 不會把手動關掉的 fb:dm 蓋回開啟")
            check(all(v for k, v in saved.items() if k != ("fb", "dm")),
                  "其餘 5 把仍是開啟")
    finally:
        sess_mod.delete_session(sid)
        cleanup()

    print()
    if fails:
        print(f"結果：{len(fails)} 項失敗")
        for f in fails:
            print("  - " + f)
        return 1
    print("結果：全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
