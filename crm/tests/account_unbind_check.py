"""自檢：商家管理頁「解綁平台帳號」= 真刪除，且各處副作用都對。

驗七件事：
  1. **解綁是真刪**（`crm_business_platform_account` 該列消失），不是切 enabled。
  2. **唯一鍵釋放** —— 同一個平台原生 ID 解綁前綁到別家會 409，解綁後可以綁成功。
     這是「解綁」的原始訴求；軟刪除做不到（`UNIQUE` 不看 enabled）。
  3. **歷史發送任務保留**（只把外鍵置 NULL，`platform` 欄還在），任務數不會少。
  4. **貼文 AI 配置被清掉**（帳號沒了，配置沒有歸屬）。
  5. **審計有 `platform_account_unbind` 記錄**，且帶得動 removed 的細節。
  6. **帳號映射不再含它** —— Connector 靠這份清單決定收發，殘留就等於「解綁了還在用」。
  7. **前端有入口**：解綁按鈕、確認彈窗（靜態節點，不是 window.confirm）、CSRF。

★ 不用密碼登入（小jo 改過 admin 密碼後 `.env` 的就過期了）：
  直接 `session.create_session()` 造 admin session + cookie。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/account_unbind_check.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.core import config  # noqa: E402
from app.internal import repo as internal_repo  # noqa: E402
import main as crm_main  # noqa: E402

TAG = "ZZ解綁測試"
# 平台原生 ID 用固定前綴 + 隨機尾巴：避免撞到真實帳號，也方便清乾淨
NATIVE = f"__CK_unbind_{uuid.uuid4().hex[:10]}__"

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


def db():
    return psycopg2.connect(config.DATABASE_URL)


def _biz_ids() -> list[int]:
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM crm_business WHERE business_name LIKE %s", (TAG + "%",))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def cleanup() -> None:
    """清掉本測試造的所有資料（含殘留的帳號 / 任務 / 貼文配置）。"""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _aid AS SELECT id FROM crm_business_platform_account"
                " WHERE platform_business_account_id LIKE %s", (NATIVE[:12] + "%",))
            cur.execute("DELETE FROM crm_outbound_task WHERE business_platform_account_id"
                        " IN (SELECT id FROM _aid)")
            cur.execute("DELETE FROM post_ai_config WHERE business_platform_account_id"
                        " IN (SELECT id FROM _aid)")
            cur.execute("DELETE FROM crm_business_platform_account WHERE id IN (SELECT id FROM _aid)")
            cur.execute("CREATE TEMP TABLE _bid AS SELECT id FROM crm_business"
                        " WHERE business_name LIKE %s", (TAG + "%",))
            cur.execute("DELETE FROM crm_outbound_task WHERE business_id IN (SELECT id FROM _bid)")
            cur.execute("DELETE FROM business_ai_switch WHERE business_id IN (SELECT id FROM _bid)")
            cur.execute("DELETE FROM crm_business WHERE id IN (SELECT id FROM _bid)")
            # 本測試造的審計（payload 帶得到原生 ID；不刪別人的）
            cur.execute("DELETE FROM crm_audit_log WHERE payload::text LIKE %s",
                        ("%" + NATIVE + "%",))
        conn.commit()
    finally:
        conn.close()


def q1(sql, args=()):
    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        conn.close()


def qall(sql, args=()):
    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def exec_sql(sql, args=()) -> int:
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def main() -> int:  # noqa: C901
    cleanup()

    staff = q1("SELECT id, login_account, staff_name, roles, is_super_agent,"
               "       is_kb_editor, enabled FROM crm_staff"
               " WHERE 'admin' = ANY(roles) AND enabled ORDER BY id LIMIT 1")
    if not staff:
        print("  !! 找不到可用的 admin 帳號，無法自檢。")
        return 2

    sid, csrf = sess_mod.create_session(dict(staff), "admin")
    H = {"X-CSRF-Token": csrf}

    try:
        with TestClient(crm_main.app, follow_redirects=False) as c:
            c.cookies.set(config.SESSION_COOKIE_NAME, sid, domain="testserver.local", path="/")
            me = c.get("/api/auth/me")
            if me.status_code != 200:
                print(f"  !! 造 session 失敗：{me.status_code} {me.text[:200]}")
                return 2

            # ---------------- 建兩個測試商家
            print("\n[1] 準備：兩個商家 + A 家一個平台帳號")
            ra = c.post("/api/admin/businesses", json={"business_name": TAG + "A"}, headers=H)
            rb = c.post("/api/admin/businesses", json={"business_name": TAG + "B"}, headers=H)
            check(ra.status_code == 200 and rb.status_code == 200,
                  "建立兩個測試商家", f"A={ra.status_code} B={rb.status_code}")
            if ra.status_code != 200 or rb.status_code != 200:
                return 2
            biz_a, biz_b = ra.json()["id"], rb.json()["id"]

            r = c.post("/api/admin/accounts", json={
                "business_id": biz_a, "platform": "fb",
                "display_name": TAG + "號",
                "credential": {"page_id": NATIVE, "page_access_token": "ck-token-" + NATIVE},
            }, headers=H)
            check(r.status_code == 200, "A 家綁定成功", f"{r.status_code} {r.text[:120]}")
            if r.status_code != 200:
                return 2
            aid = r.json()["id"]

            # ---------------- 2. 同一原生 ID 綁到 B → 必須 409（解綁前）
            print("\n[2] 解綁前：同一平台帳號綁到別家 → 409")
            r = c.post("/api/admin/accounts", json={
                "business_id": biz_b, "platform": "fb",
                "credential": {"page_id": NATIVE, "page_access_token": "x"},
            }, headers=H)
            check(r.status_code == 409, f"唯一鍵擋住重複綁定（實得 {r.status_code}）",
                  r.text[:120])

            # ---------------- 3. 造歷史任務與貼文配置（驗副作用）
            print("\n[3] 造引用資料：1 筆歷史任務 + 1 條貼文配置")
            exec_sql(
                "INSERT INTO crm_outbound_task"
                " (business_id, source, platform, business_platform_account_id,"
                "  end_user_sender_id, message_content, status)"
                " VALUES (%s, 'manual', 'fb', %s, %s, %s::jsonb, 'sent')",
                (biz_a, aid, "ck-user-" + NATIVE, json.dumps({"text": "ck"}, ensure_ascii=False)),
            )
            exec_sql(
                "INSERT INTO post_ai_config (platform, post_id, business_platform_account_id)"
                " VALUES ('fb', %s, %s)",
                ("ck-post-" + NATIVE, aid),
            )
            tasks_before = q1("SELECT count(*) AS n FROM crm_outbound_task"
                              " WHERE business_platform_account_id = %s", (aid,))["n"]
            check(tasks_before == 1, f"歷史任務已建立（{tasks_before} 筆）")

            # ---------------- 4. 解綁
            print("\n[4] 解綁 → 真刪除")
            r = c.post("/api/admin/accounts/unbind", json={"id": aid}, headers=H)
            check(r.status_code == 200, f"解綁成功（實得 {r.status_code}）", r.text[:160])
            body = r.json() if r.status_code == 200 else {}
            check(body.get("deleted") == 1, f"回報 deleted=1（實得 {body.get('deleted')}）")
            check(body.get("post_config_deleted") == 1,
                  f"回報清掉 1 條貼文配置（實得 {body.get('post_config_deleted')}）")
            check(body.get("tasks_detached") == 1,
                  f"回報解除 1 筆任務關聯（實得 {body.get('tasks_detached')}）")

            # ---------------- 5. DB 實際狀態
            print("\n[5] DB 實際狀態")
            check(q1("SELECT 1 FROM crm_business_platform_account WHERE id = %s", (aid,)) is None,
                  "★ 帳號列真的消失了（不是 enabled=false）")
            t = q1("SELECT id, platform, business_platform_account_id FROM crm_outbound_task"
                   " WHERE end_user_sender_id = %s", ("ck-user-" + NATIVE,))
            check(t is not None, "★ 歷史任務還在（沒有被連帶刪掉）")
            check(t is not None and t["business_platform_account_id"] is None,
                  "歷史任務的外鍵被置 NULL")
            check(t is not None and t["platform"] == "fb",
                  "歷史任務的 platform 欄還在（對帳不會斷）")
            check(q1("SELECT 1 FROM post_ai_config WHERE post_id = %s",
                     ("ck-post-" + NATIVE,)) is None,
                  "貼文配置已刪除")

            # ---------------- 6. 審計
            print("\n[6] 審計紀錄")
            a = q1("SELECT operate_type, business_id, platform, business_platform_account_id, payload"
                   "  FROM crm_audit_log WHERE operate_type = 'platform_account_unbind'"
                   "   AND payload::text LIKE %s ORDER BY id DESC LIMIT 1",
                   ("%" + NATIVE + "%",))
            check(a is not None, "有 platform_account_unbind 審計")
            if a:
                check(a["business_id"] == biz_a, "審計指向正確商家")
                check(a["platform"] == "fb", "審計記錄平台")
                check(a["business_platform_account_id"] == aid, "審計記錄被刪的帳號 id")

            # ---------------- 7. 帳號映射（Connector 的收發依據）
            print("\n[7] 帳號映射不再含它")
            amap = internal_repo.account_map()
            hit = [x for x in amap
                   if x.get("platform") == "fb"
                   and x.get("platform_business_account_id") == NATIVE]
            check(not hit, "★ account_map() 已不含被解綁的帳號（Connector 立刻收不到它）")

            # ---------------- 8. 唯一鍵釋放 → 可以綁到別家
            print("\n[8] 唯一鍵釋放：同一平台帳號改綁到 B 家")
            r = c.post("/api/admin/accounts", json={
                "business_id": biz_b, "platform": "fb",
                "display_name": TAG + "改綁",
                "credential": {"page_id": NATIVE, "page_access_token": "ck2-" + NATIVE},
            }, headers=H)
            check(r.status_code == 200, f"★ 改綁成功（實得 {r.status_code}）", r.text[:160])
            if r.status_code == 200:
                new_id = r.json()["id"]
                row = q1("SELECT business_id FROM crm_business_platform_account WHERE id = %s",
                         (new_id,))
                check(row and row["business_id"] == biz_b, "新帳號屬於 B 家")

            # ---------------- 9. 管理台清單
            print("\n[9] 管理台清單")
            items = c.get(f"/api/admin/accounts?business_id={biz_a}").json()["items"]
            check(not [i for i in items if i["id"] == aid],
                  "A 家的清單已不含被解綁帳號")

            # ---------------- 10. 對不存在的 id 解綁 → 404
            print("\n[10] 邊界：解綁不存在的 id")
            r = c.post("/api/admin/accounts/unbind", json={"id": 999999999}, headers=H)
            check(r.status_code == 404, f"回 404（實得 {r.status_code}）", r.text[:100])

            # ---------------- 11. 必須帶 CSRF
            print("\n[11] 邊界：解綁不帶 CSRF")
            r = c.post("/api/admin/accounts/unbind", json={"id": 999999999})
            check(r.status_code in (401, 403), f"擋下（實得 {r.status_code}）", r.text[:100])

            # ---------------- 12. 前端入口
            print("\n[12] 前端靜態檢查（admin.html）")
            html = (ROOT / "crm" / "ui" / "admin.html").read_text(encoding="utf-8")
            check('data-acct-del="' in html, "帳號列有「解綁」按鈕")
            check("/api/admin/accounts/unbind" in html, "前端打正確的解綁端點")
            check('id="ovUnbind"' in html, "有確認彈窗節點")
            # ★ 只認「真的呼叫」：`confirm(` 帶括號。註解裡提到 `window.confirm` 不算
            #   （本檔註解就寫了那句，粗暴比對會誤報）。
            check("confirm(" not in html, "★ 沒有用 window.confirm（彈窗一律靜態節點）")
            check("data-close" in html.split('id="ovUnbind"')[1][:2000],
                  "解綁彈窗可取消（data-close）")
            check("停用" in html.split('id="ovUnbind"')[1][:2500],
                  "彈窗有提示「只想暫停請用停用」（避免誤刪）")

    finally:
        try:
            sess_mod.delete_session(sid)
        except Exception:  # noqa: BLE001
            pass
        cleanup()

    print(f"\n{'=' * 62}")
    print(f"  解綁平台帳號：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        for m in _fail:
            print("    FAIL:", m)
    return 0 if not _fail else 1


if __name__ == "__main__":
    sys.exit(main())
