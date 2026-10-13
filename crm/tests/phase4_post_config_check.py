"""階段四自檢 —— 貼文特殊配置（`/console/agent/post-config`）。

純 CRM 側（不需要 Connector）：驗的是「貼文覆寫集」這條規則鏈 ——
彈窗拿到什麼、存下去寫進哪裡、解析器（`resolve_scope_set`）讀到什麼。

覆蓋：
  [1] 未配置過 → `configured=false` 且勾選＝私訊基準集（★ 預設＝與私訊相同）
  [2] 入參校驗（平台沒有留言場景 / 缺 post_id）
  [3] ★ 鐵則：開關開著卻空勾 → 400（三層各驗一次）
  [4] 跨商家項目 → 400（不能勾別人的資料）
  [5] 正常儲存 → configured=true，解析器 source='post'
  [6] 貼文總開關關 → 三層全空、source='post_off'
  [7] 移除特殊配置 → 回到 source='business'
  [8] 權限（未授權商家 403 / 純 ops 403 / 缺 CSRF 403）
  [9] 留言總開關狀態反映在 `comment_switch_on`
  [10] 覆蓋式儲存（先前勾的被清掉，不會殘留）

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/phase4_post_config_check.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
sys.path.insert(0, str(CRM_DIR))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MARK = "zz_p4_post"
BIZ_NAME = f"{MARK}_商家"
OTHER_BIZ = f"{MARK}_別家"
NATIVE_ACC = f"{MARK}_acc"
AGENT = f"{MARK}_agent"
OPS_USER = f"{MARK}_ops"
NOBODY = f"{MARK}_nobody"
PW = "postpw_p4_pc1"
POST_ID = f"{MARK}_post_1"
POST_ID2 = f"{MARK}_post_2"

failures: list[str] = []
passes = 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def read_env(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


CRM_DSN = read_env(CRM_DIR / ".env")["DATABASE_URL"]


def q(sql: str, args=None, fetch=False):
    conn = psycopg2.connect(CRM_DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


def biz_ids(names):
    rows = q("SELECT id FROM crm_business WHERE business_name = ANY(%s)", (list(names),), True)
    return [r["id"] for r in rows]


def cleanup():
    ids = biz_ids([BIZ_NAME, OTHER_BIZ])
    # post_* 綁定沒有 ON DELETE CASCADE → 必須先刪
    for pid_sql in ("DELETE FROM post_kb_binding WHERE post_config_id IN"
                    " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)",
                    "DELETE FROM post_faq_binding WHERE post_config_id IN"
                    " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)",
                    "DELETE FROM post_table_binding WHERE post_config_id IN"
                    " (SELECT id FROM post_ai_config WHERE post_id LIKE %s)"):
        q(pid_sql, (f"{MARK}%",))
    q("DELETE FROM post_ai_config WHERE post_id LIKE %s", (f"{MARK}%",))
    # ⚠️ 員工授權要先刪 —— 它引用 crm_business，順序顛倒會撞 FK
    q("DELETE FROM crm_session WHERE staff_id IN"
      " (SELECT id FROM crm_staff WHERE login_account IN (%s, %s, %s))", (AGENT, OPS_USER, NOBODY))
    q("DELETE FROM crm_staff_business_permission WHERE staff_id IN"
      " (SELECT id FROM crm_staff WHERE login_account IN (%s, %s, %s))", (AGENT, OPS_USER, NOBODY))
    q("DELETE FROM crm_staff WHERE login_account IN (%s, %s, %s)", (AGENT, OPS_USER, NOBODY))
    if ids:
        q("DELETE FROM crm_audit_log WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM business_layer_switch WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM business_ai_switch WHERE business_id = ANY(%s)", (ids,))
        for t in ("business_faq_binding", "business_table_binding", "business_kb_binding"):
            q(f"DELETE FROM {t} WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM kb_param WHERE collection_id IN"
          " (SELECT id FROM kb_collection WHERE business_id = ANY(%s))", (ids,))
        q("DELETE FROM kb_asset_chunk WHERE asset_id IN"
          " (SELECT id FROM kb_asset WHERE collection_id IN"
          "   (SELECT id FROM kb_collection WHERE business_id = ANY(%s)))", (ids,))
        q("DELETE FROM kb_asset WHERE collection_id IN"
          " (SELECT id FROM kb_collection WHERE business_id = ANY(%s))", (ids,))
        q("DELETE FROM kb_chunk WHERE doc_id IN"
          " (SELECT id FROM kb_document WHERE collection_id IN"
          "   (SELECT id FROM kb_collection WHERE business_id = ANY(%s)))", (ids,))
        q("DELETE FROM kb_job WHERE doc_id IN"
          " (SELECT id FROM kb_document WHERE collection_id IN"
          "   (SELECT id FROM kb_collection WHERE business_id = ANY(%s)))", (ids,))
        q("DELETE FROM kb_document WHERE collection_id IN"
          " (SELECT id FROM kb_collection WHERE business_id = ANY(%s))", (ids,))
        q("DELETE FROM kb_collection WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM autoreply_business_row WHERE table_id IN"
          " (SELECT id FROM autoreply_business_table WHERE business_id = ANY(%s))", (ids,))
        q("DELETE FROM autoreply_business_table WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM autoreply_faq WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM crm_outbound_task WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM crm_business_platform_account WHERE business_id = ANY(%s)", (ids,))
        q("DELETE FROM crm_business WHERE id = ANY(%s)", (ids,))


def seed():
    """兩個商家：主商家有 fb/ig 帳號 + FAQ/表格/知識庫各一；別家也有一份 FAQ（測跨商家）。"""
    from app.core.security import hash_password
    from app.autoreply import autoreply_db as adb
    from app.rag import kb_db

    conn = psycopg2.connect(CRM_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crm_business (business_name, enabled) VALUES (%s,true)"
                        " RETURNING id", (BIZ_NAME,))
            biz = cur.fetchone()[0]
            cur.execute("INSERT INTO crm_business (business_name, enabled) VALUES (%s,true)"
                        " RETURNING id", (OTHER_BIZ,))
            other = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crm_business_platform_account"
                " (business_id, platform, platform_business_account_id, display_name, enabled,"
                "  ai_auto_reply_enabled) VALUES (%s,'fb',%s,'FB 自檢號',true,true) RETURNING id",
                (biz, NATIVE_ACC))
            acc = cur.fetchone()[0]
            staff = {}
            for login, roles, name in ((AGENT, ["agent"], "自檢坐席"),
                                       (OPS_USER, ["ops"], "自檢運維"),
                                       (NOBODY, ["agent"], "無權限坐席")):
                cur.execute(
                    "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles,"
                    " is_super_agent, is_kb_editor, enabled)"
                    " VALUES (%s,%s,%s,%s,false,true,true) RETURNING id",
                    (login, hash_password(PW), name, roles))
                staff[login] = cur.fetchone()[0]
            cur.execute("INSERT INTO crm_staff_business_permission (staff_id, business_id)"
                        " VALUES (%s,%s)", (staff[AGENT], biz))
        conn.commit()
    finally:
        conn.close()

    faq = adb.create_faq(biz, f"{MARK} 有現貨嗎", "有")
    tbl = adb.create_table(biz, f"{MARK} 價格表")
    kb_doc = kb_db.create_collection(f"{MARK} 說明庫", business_id=biz, kind="doc")
    kb_img = kb_db.create_collection(f"{MARK} 圖片庫", business_id=biz, kind="image")
    other_faq = adb.create_faq(other, f"{MARK} 別家問句", "別的")
    adb.set_scope_mode(biz, "fb", "comment", True)
    adb.set_scope_mode(biz, "fb", "dm", True)
    return {"biz": biz, "other": other, "acc": acc,
            "faq": faq["id"], "table": tbl["id"],
            "kb_doc": kb_doc["id"], "kb_img": kb_img["id"], "other_faq": other_faq["id"]}


def login(c: TestClient, account: str, password: str, role: str = "agent"):
    r = c.post("/api/auth/login", json={"account": account, "password": password})
    body = r.json()
    if body.get("need_role"):
        r = c.post("/api/auth/login", json={"ticket": body["ticket"], "role": role})
        body = r.json()
    return body.get("csrf_token") or "", body


def main() -> int:
    cleanup()
    S = seed()
    biz, acc = S["biz"], S["acc"]
    try:
        os.environ["WORKER_ENABLED"] = "false"
        from app.core import config
        config.WORKER_ENABLED = False
        from app.autoreply import autoreply_db as adb

        import main as crm_main
        with TestClient(crm_main.app, follow_redirects=False) as c:
            csrf, me = login(c, AGENT, PW)
            check(bool(csrf), f"agent 登入（{me.get('staff', {}).get('login_account')}）")
            H = {"X-CSRF-Token": csrf}
            P = {"business_id": biz, "platform": "fb", "post_id": POST_ID}

            print("\n[1] 未配置過 → 預設＝私訊基準集")
            r = c.get("/console/agent/post-config", params=P)
            d = r.json()
            check(r.status_code == 200 and d.get("ok"), f"200（實得 {r.status_code}）")
            check(d.get("configured") is False and d.get("exists") is False, "★ configured=false")
            check({k: sorted(d["picked"][k]) == sorted(d["base"][k]) for k in ("faq", "table", "kb")}
                  == {"faq": True, "table": True, "kb": True},
                  f"★ 勾選預設＝基準集（{d.get('picked')} vs {d.get('base')}）")
            check(sorted(d["base"]["faq"]) == [S["faq"]], "基準集含本商家 FAQ")
            check(sorted(d["base"]["table"]) == [S["table"]], "基準集含本商家表格")
            check(set(d["base"]["kb"]) == {S["kb_doc"], S["kb_img"]}, "基準集含兩個知識庫")
            cand = d["candidates"]
            check(S["other_faq"] not in [x["id"] for x in cand["faq"]], "★ 候選不含別家 FAQ")
            check(any(x["kind"] == "image" for x in cand["kb"]), "候選標出圖片庫 kind")
            check(d.get("comment_switch_on") is True, "留言總開關為開")
            check(d.get("layers") == {"faq": True, "table": True, "rag": True},
                  f"三層開關預設全開（{d.get('layers')}）")
            check(d.get("can_configure") is True, "can_configure=true")

            print("\n[2] 入參校驗")
            r = c.get("/console/agent/post-config",
                      params={"business_id": biz, "platform": "whatsapp", "post_id": POST_ID})
            check(r.status_code == 400, f"whatsapp 沒有留言場景 → 400（實得 {r.status_code}）")
            r = c.get("/console/agent/post-config",
                      params={"business_id": biz, "platform": "fb", "post_id": " "})
            check(r.status_code == 400, f"缺 post_id → 400（實得 {r.status_code}）")

            print("\n[3] ★ 開關開著卻空勾 → 400")
            for layers, label in (({"faq": True, "table": False, "kb": False}, "FAQ"),
                                  ({"faq": False, "table": True, "kb": False}, "業務表格"),
                                  ({"faq": False, "table": False, "kb": True}, "知識庫")):
                r = c.post("/console/agent/post-config",
                           json={**P, "bp_account_id": acc, "enabled": True,
                                 "layers": layers,
                                 "bindings": {"faq": [], "table": [], "kb": []}}, headers=H)
                check(r.status_code == 400 and label in r.json().get("error", ""),
                      f"{label} 開著空勾 → 400（實得 {r.status_code} {r.json().get('error')}）")
            r = c.post("/console/agent/post-config",
                       json={**P, "bp_account_id": acc, "enabled": True,
                             "layers": {"faq": False, "table": False, "kb": False},
                             "bindings": {"faq": [], "table": [], "kb": []}}, headers=H)
            check(r.status_code == 200, "三層全關 → 允許空勾（不是空集合觸發）")
            q("DELETE FROM post_ai_config WHERE post_id = %s", (POST_ID,))

            print("\n[4] 跨商家項目 → 400")
            r = c.post("/console/agent/post-config",
                       json={**P, "bp_account_id": acc, "enabled": True,
                             "layers": {"faq": True, "table": False, "kb": False},
                             "bindings": {"faq": [S["other_faq"]], "table": [], "kb": []}},
                       headers=H)
            check(r.status_code == 400 and "不屬於該商家" in r.json().get("error", ""),
                  f"勾別家 FAQ → 400（實得 {r.status_code}）")

            print("\n[5] 正常儲存")
            r = c.post("/console/agent/post-config",
                       json={**P, "bp_account_id": acc, "enabled": True,
                             "layers": {"faq": True, "table": False, "kb": True},
                             "bindings": {"faq": [S["faq"]], "table": [],
                                          "kb": [S["kb_doc"]]}}, headers=H)
            check(r.status_code == 200 and r.json().get("post_config_id"),
                  f"儲存成功（實得 {r.status_code} {r.text[:90]}）")
            pid = r.json()["post_config_id"]
            r = c.get("/console/agent/post-config", params=P)
            d = r.json()
            check(d["configured"] is True, "configured=true")
            check(d["picked"]["faq"] == [S["faq"]], "FAQ 覆寫集生效")
            check(sorted(d["picked"]["kb"]) == [S["kb_doc"]], "★ 沒勾的被清掉（覆蓋式，非追加）")
            check(d["layers"] == {"faq": True, "table": False, "rag": True},
                  f"三層開關（表格關）（{d.get('layers')}）")
            ss = adb.resolve_scope_set(biz, scope="comment", post_config_id=pid)
            check(ss["source"] == "post", f"★ 解析器走貼文覆寫集（source={ss['source']}）")
            check(ss["faq"] == [S["faq"]] and ss["kb"] == [S["kb_doc"]] and ss["table"] == [],
                  f"解析結果與儲存一致（{ {k: ss[k] for k in ('faq','table','kb')} }）")
            check(ss["layers"]["table"] is False, "解析出的層開關＝表格關")
            au = q("SELECT operate_type, payload FROM crm_audit_log WHERE business_id=%s"
                   " AND operate_type='post_config_change' ORDER BY id DESC LIMIT 1",
                   (biz,), True)
            check(bool(au) and au[0]["payload"].get("post_id") == POST_ID, "寫了審計 post_config_change")

            print("\n[6] 貼文總開關關 → 三層全空")
            r = c.post("/console/agent/post-config",
                       json={**P, "bp_account_id": acc, "enabled": False,
                             "layers": {"faq": True, "table": True, "kb": True},
                             "bindings": {"faq": [S["faq"]], "table": [S["table"]],
                                          "kb": [S["kb_doc"]]}}, headers=H)
            check(r.status_code == 200, f"關閉儲存成功（實得 {r.status_code}）")
            ss = adb.resolve_scope_set(biz, scope="comment", post_config_id=pid)
            check(ss["source"] == "post_off", f"★ source=post_off（實得 {ss['source']}）")
            check(ss["faq"] == [] and ss["table"] == [] and ss["kb"] == [], "★ 三層全空")
            check(not any(ss["layers"].values()), "★ 三層開關全關")

            print("\n[7] 移除特殊配置")
            r = c.post("/console/agent/post-config",
                       json={"business_id": biz, "platform": "fb", "post_id": POST_ID,
                             "remove": True}, headers=H)
            check(r.status_code == 200 and r.json().get("removed") is True, "移除成功")
            ss = adb.resolve_scope_set(biz, scope="comment", post_config_id=pid)
            check(ss["source"] == "business", f"★ 回到基準集（source={ss['source']}）")
            left = q("SELECT count(*) AS n FROM post_kb_binding WHERE post_config_id=%s",
                     (pid,), True)[0]["n"]
            check(int(left) == 0, "綁定也一併清掉（不留孤兒）")
            r = c.get("/console/agent/post-config", params=P)
            check(r.json().get("configured") is False, "GET 回到未配置")

            print("\n[8] 權限")
            with TestClient(crm_main.app, follow_redirects=False) as c2:
                csrf2, _ = login(c2, NOBODY, PW)
                r = c2.get("/console/agent/post-config", params=P)
                check(r.status_code == 403, f"未授權商家 → 403（實得 {r.status_code}）")
                r = c2.post("/console/agent/post-config",
                            json={**P, "enabled": True, "layers": {}, "bindings": {}},
                            headers={"X-CSRF-Token": csrf2})
                check(r.status_code == 403, f"未授權商家寫入 → 403（實得 {r.status_code}）")
            with TestClient(crm_main.app, follow_redirects=False) as c3:
                _, me3 = login(c3, OPS_USER, PW, "ops")
                check(me3.get("staff", {}).get("active_role") == "ops", "ops 身分登入")
                r = c3.get("/console/agent/post-config", params=P)
                check(r.status_code == 403, f"純 ops → 403（實得 {r.status_code}）")
            r = c.post("/console/agent/post-config",
                       json={**P, "enabled": True, "layers": {}, "bindings": {}})
            check(r.status_code == 403, f"缺 CSRF → 403（實得 {r.status_code}）")

            print("\n[9] 留言總開關反映到 comment_switch_on")
            adb.set_scope_mode(biz, "fb", "comment", False)
            r = c.get("/console/agent/post-config", params=P)
            check(r.json().get("comment_switch_on") is False, "★ 關掉後 comment_switch_on=false")
            adb.set_scope_mode(biz, "fb", "comment", True)
            r = c.get("/console/agent/post-config", params=P)
            check(r.json().get("comment_switch_on") is True, "開回來 → true")

            print("\n[10] 兩則貼文互不干擾")
            r = c.post("/console/agent/post-config",
                       json={"business_id": biz, "platform": "fb", "post_id": POST_ID,
                             "bp_account_id": acc, "enabled": True,
                             "layers": {"faq": True, "table": False, "kb": False},
                             "bindings": {"faq": [S["faq"]], "table": [], "kb": []}}, headers=H)
            pid1 = r.json()["post_config_id"]
            r = c.post("/console/agent/post-config",
                       json={"business_id": biz, "platform": "fb", "post_id": POST_ID2,
                             "bp_account_id": acc, "enabled": True,
                             "layers": {"faq": False, "table": True, "kb": False},
                             "bindings": {"faq": [], "table": [S["table"]], "kb": []}}, headers=H)
            pid2 = r.json()["post_config_id"]
            check(pid1 != pid2, "兩則貼文各自一列配置")
            ss1 = adb.resolve_scope_set(biz, scope="comment", post_config_id=pid1)
            ss2 = adb.resolve_scope_set(biz, scope="comment", post_config_id=pid2)
            check(ss1["faq"] == [S["faq"]] and ss1["table"] == [], "貼文 1 用自己的 FAQ")
            check(ss2["table"] == [S["table"]] and ss2["faq"] == [], "★ 貼文 2 不受貼文 1 影響")
            r = c.get("/console/agent/post-config",
                      params={"business_id": biz, "platform": "fb", "post_id": POST_ID2})
            check(r.json()["picked"]["table"] == [S["table"]], "貼文 2 的 GET 也對得上")
    finally:
        cleanup()

    print("\n" + "=" * 68)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    for f in failures:
        print("   -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
