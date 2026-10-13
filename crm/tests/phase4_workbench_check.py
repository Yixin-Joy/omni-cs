"""階段四自檢 —— 客服工作台後端（CRM `/console/agent/*`）。

真起一個 **Connector 子進程**（uvicorn，mock 發送模式）讓 CRM 真打內網 API，
而不是把 `connector_client` 打樁 —— 這樣才驗得到「跨服務」這一段。

覆蓋：
  [1] 可訪問商家（super vs 有授權；未授權商家被排除）
  [2] 會話列表（多帳號合併、pending 紅點、按時間倒序）
  [3] 商家權限（未授權 → 403）
  [4] 對話（訊息正序 + 該會話 AI 留痕）
  [5] 人工回覆 → 建任務 source='manual' + 審計 reply_send（同一事務）
  [6] 回覆入參校驗（空內容 / 帳號不屬該商家 / 非客服角色）
  [7] 清紅點 → Connector is_handled=true + 審計 conversation_read
  [8] CSRF 閘門
  [9] Connector 不可達 → 502（明確區分「沒會話」與「拿不到」）

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/phase4_workbench_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
CONN_DIR = ROOT / "connector"
PY = sys.executable

CONN_PORT = 8120
CONN_BASE = f"http://127.0.0.1:{CONN_PORT}"

sys.path.insert(0, str(CRM_DIR))

import httpx  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MARK = "zz_p4_wb"
BIZ_NAME = f"{MARK}_商家"
NATIVE_ACC = f"{MARK}_acc"
AGENT = f"{MARK}_agent"
AGENT_PW = "agentpw_p4wb1"
NOBODY = f"{MARK}_nobody"
S1 = f"{MARK}_s1"
S2 = f"{MARK}_s2"
FAQ_Q = f"{MARK}_這一層測試問句"
FAQ_A = "自檢用答案"

failures: list[str] = []
passes = 0


def check(cond, msg):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


# ------------------------------------------------------------------ .env / DB

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


CRM_ENV = read_env(CRM_DIR / ".env")
CONN_ENV = read_env(CONN_DIR / ".env")
CRM_DSN = CRM_ENV["DATABASE_URL"]
CONN_DSN = CONN_ENV["DATABASE_URL"]
TOKEN = CONN_ENV.get("INTERNAL_API_TOKEN") or CRM_ENV.get("INTERNAL_API_TOKEN") or "CHANGE-ME-INTERNAL-TOKEN"


def q(dsn: str, sql: str, args=None, fetch=False):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()] if fetch else []
        conn.commit()
        return rows
    finally:
        conn.close()


# ------------------------------------------------------------------ 準備 / 清理

def cleanup():
    q(CRM_DSN, "DELETE FROM crm_audit_log WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM business_layer_switch WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM business_ai_switch WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM business_faq_binding WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM autoreply_faq WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM crm_outbound_task WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM crm_session WHERE staff_id IN"
               " (SELECT id FROM crm_staff WHERE login_account IN (%s, %s))", (AGENT, NOBODY))
    q(CRM_DSN, "DELETE FROM crm_staff_business_permission WHERE staff_id IN"
               " (SELECT id FROM crm_staff WHERE login_account IN (%s, %s))", (AGENT, NOBODY))
    q(CRM_DSN, "DELETE FROM crm_staff WHERE login_account IN (%s, %s)", (AGENT, NOBODY))
    q(CRM_DSN, "DELETE FROM crm_business_platform_account WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM crm_business WHERE business_name = %s", (BIZ_NAME,))
    q(CONN_DSN, "DELETE FROM forward_log WHERE platform_business_account_id = %s", (NATIVE_ACC,))
    q(CONN_DSN, "DELETE FROM igfb_dm WHERE platform_business_account_id = %s", (NATIVE_ACC,))


def seed():
    from app.core.security import hash_password
    conn = psycopg2.connect(CRM_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO crm_business (business_name, enabled) VALUES (%s, true)"
                        " RETURNING id", (BIZ_NAME,))
            biz = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crm_business_platform_account"
                " (business_id, platform, platform_business_account_id, display_name, enabled,"
                "  ai_auto_reply_enabled) VALUES (%s, 'whatsapp', %s, '自檢號', true, true)"
                " RETURNING id", (biz, NATIVE_ACC))
            acc = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles,"
                " is_super_agent, is_kb_editor, enabled)"
                " VALUES (%s, %s, '自檢坐席', %s, false, false, true) RETURNING id",
                (AGENT, hash_password(AGENT_PW), ["agent"]))
            staff = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles,"
                " is_super_agent, is_kb_editor, enabled)"
                " VALUES (%s, %s, '無權限坐席', %s, false, false, true) RETURNING id",
                (NOBODY, hash_password(AGENT_PW), ["agent"]))
            cur.execute("INSERT INTO crm_staff_business_permission (staff_id, business_id)"
                        " VALUES (%s, %s)", (staff, biz))
        conn.commit()
    finally:
        conn.close()

    t0 = datetime.now(timezone.utc)
    conn = psycopg2.connect(CONN_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, message_time, is_handled, platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'inbound', 'text', %s, %s, false, %s)",
                (S1, "請問有現貨嗎", t0 - timedelta(minutes=6), NATIVE_ACC))
            cur.execute(
                "INSERT INTO forward_log(platform, sender_id, direction, message_type,"
                " raw_content, message_time, is_handled, platform_business_account_id)"
                " VALUES ('whatsapp', %s, 'inbound', 'text', %s, %s, true, %s)",
                (S2, "已讀過了", t0 - timedelta(minutes=30), NATIVE_ACC))
        conn.commit()
    finally:
        conn.close()
    return biz, acc


def start_connector():
    logf = open(Path(tempfile.gettempdir()) / f"multi_cs_p4_{CONN_PORT}.log", "w", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": CONN_DSN, "INTERNAL_API_TOKEN": TOKEN,
        "WORKER_ENABLED": "false", "OUTBOUND_SEND_MODE": "mock",
        "PORT": str(CONN_PORT),
    })
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(CONN_PORT)],
        cwd=str(CONN_DIR), env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            r = httpx.get(f"{CONN_BASE}/health", timeout=1.0)
            if r.status_code == 200:
                return proc
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"Connector 起不來（exit={proc.returncode}），看 {logf.name}")
        time.sleep(0.5)
    raise RuntimeError("Connector 逾時未就緒")


def login(c: TestClient, account: str, password: str):
    r = c.post("/api/auth/login", json={"account": account, "password": password})
    body = r.json()
    if body.get("need_role"):
        r = c.post("/api/auth/login", json={"ticket": body["ticket"], "role": "agent"})
        body = r.json()
    return body.get("csrf_token") or "", body


# ------------------------------------------------------------------ 主流程

def main() -> int:
    cleanup()
    biz, acc = seed()
    proc = start_connector()
    try:
        os.environ["WORKER_ENABLED"] = "false"
        from app.core import config
        config.WORKER_ENABLED = False
        config.CONNECTOR_INTERNAL_BASE = CONN_BASE
        config.INTERNAL_API_TOKEN = TOKEN

        import main as crm_main
        with TestClient(crm_main.app, follow_redirects=False) as c:
            csrf, me = login(c, AGENT, AGENT_PW)
            check(bool(csrf), f"agent 登入成功（{me.get('staff', {}).get('login_account')}）")
            H = {"X-CSRF-Token": csrf}

            print("\n[1] 可訪問商家")
            r = c.get("/console/agent/businesses")
            names = [b["business_name"] for b in (r.json().get("items") or [])]
            check(f"{MARK}_商家" in names, "有授權商家在清單內")
            check(f"{MARK}_nobody" not in " ".join(names), "未授權商家被排除")
            check(not r.json().get("is_super_agent"), "非 super agent")

            print("\n[2] 會話列表")
            r = c.get("/console/agent/conversations",
                      params={"business_id": biz, "platform": "whatsapp"})
            items = r.json().get("items") or []
            check(r.status_code == 200, f"200（實得 {r.status_code}）")
            keys = {it["key"]: it for it in items}
            check(S1 in keys and S2 in keys, f"兩個會話都在（{sorted(keys)}）")
            check(keys.get(S1, {}).get("pending") == 1, "S1 pending=1（紅點）")
            check(keys.get(S2, {}).get("pending") == 0, "S2 pending=0")
            check(items[0]["key"] == S1, "按最後訊息時間倒序")
            check(all(it.get("bp_account_id") == acc for it in items), "帶出 bp_account_id")

            print("\n[3] 商家權限")
            with TestClient(crm_main.app, follow_redirects=False) as c2:
                csrf2, _ = login(c2, NOBODY, AGENT_PW)
                r = c2.get("/console/agent/conversations",
                           params={"business_id": biz, "platform": "whatsapp"})
                check(r.status_code == 403, f"未授權商家 → 403（實得 {r.status_code}）")
                r = c2.post("/console/agent/reply", json={
                    "business_id": biz, "platform": "whatsapp", "bp_account_id": acc,
                    "sender_id": S1, "text": "偷回"}, headers={"X-CSRF-Token": csrf2})
                check(r.status_code == 403, f"未授權商家回覆 → 403（實得 {r.status_code}）")

            print("\n[4] 對話（訊息正序 + AI 留痕）")
            r = c.get("/console/agent/conversation",
                      params={"business_id": biz, "platform": "whatsapp", "key": S1,
                              "bp_account_id": acc})
            body = r.json()
            check(r.status_code == 200 and body.get("ok"), f"200（實得 {r.status_code}）")
            msgs = body.get("items") or []
            check(len(msgs) == 1 and msgs[0]["text"] == "請問有現貨嗎", "取到該會話訊息")
            check(isinstance(body.get("logs"), list), "附帶該會話 AI 留痕（list）")

            print("\n[5] 人工回覆 → 建任務 + 審計")
            r = c.post("/console/agent/reply", json={
                "business_id": biz, "platform": "whatsapp", "bp_account_id": acc,
                "sender_id": S1, "text": "有現貨，今天可發"}, headers=H)
            check(r.status_code == 200 and r.json().get("ok"), f"回覆成功（實得 {r.status_code} {r.text[:90]}）")
            task_id = (r.json() or {}).get("task_id")
            rows = q(CRM_DSN, "SELECT source, status, staff_id, end_user_sender_id,"
                              " message_content FROM crm_outbound_task WHERE id = %s",
                     (task_id,), fetch=True)
            check(bool(rows) and rows[0]["source"] == "manual", "任務 source='manual'")
            check(bool(rows) and rows[0]["status"] == "pending", "任務 status='pending'")
            check(bool(rows) and rows[0]["staff_id"] is not None, "任務帶 staff_id（誰回的）")
            check(bool(rows) and (rows[0]["message_content"] or {}).get("text") == "有現貨，今天可發",
                  "message_content 內容正確")
            au = q(CRM_DSN, "SELECT operate_type, payload FROM crm_audit_log"
                            " WHERE business_id = %s AND operate_type = 'reply_send'"
                            " ORDER BY id DESC LIMIT 1", (biz,), fetch=True)
            check(bool(au) and (au[0]["payload"] or {}).get("task_id") == task_id,
                  "同一事務寫了審計 reply_send")

            print("\n[6] 回覆入參校驗")
            r = c.post("/console/agent/reply", json={
                "business_id": biz, "platform": "whatsapp", "bp_account_id": acc,
                "sender_id": S1, "text": "   "}, headers=H)
            check(r.status_code == 400, f"空內容 → 400（實得 {r.status_code}）")
            r = c.post("/console/agent/reply", json={
                "business_id": biz, "platform": "whatsapp", "bp_account_id": 999999,
                "sender_id": S1, "text": "hi"}, headers=H)
            check(r.status_code == 400, f"帳號不屬該商家 → 400（實得 {r.status_code}）")
            r = c.post("/console/agent/reply", json={
                "business_id": biz, "platform": "whatsapp", "bp_account_id": acc,
                "sender_id": S1, "text": "hi"})
            check(r.status_code == 403, f"缺 CSRF → 403（實得 {r.status_code}）")

            print("\n[7] 清紅點")
            r = c.post("/console/agent/read", json={
                "business_id": biz, "platform": "whatsapp", "bp_account_id": acc,
                "key": S1, "scope": "dm"}, headers=H)
            check(r.status_code == 200 and r.json().get("ok"), f"清紅點成功（實得 {r.status_code}）")
            handled = q(CONN_DSN, "SELECT count(*) AS n FROM forward_log"
                                  " WHERE sender_id = %s AND is_handled = true", (S1,), fetch=True)
            check(int(handled[0]["n"]) >= 1, "Connector 端 is_handled 已置 true")
            au = q(CRM_DSN, "SELECT count(*) AS n FROM crm_audit_log"
                            " WHERE business_id = %s AND operate_type = 'conversation_read'",
                   (biz,), fetch=True)
            check(int(au[0]["n"]) >= 1, "寫了審計 conversation_read")
            r = c.get("/console/agent/conversations",
                      params={"business_id": biz, "platform": "whatsapp"})
            s1 = next((it for it in (r.json().get("items") or []) if it["key"] == S1), {})
            check(s1.get("pending") == 0, "清完後 pending=0（紅點消失）")

            print("\n[8] 配置 / 清單 / 紀錄 / 命中測試")
            from app.autoreply import autoreply_db as adb
            adb.create_faq(biz, FAQ_Q, FAQ_A)          # 走 create_faq → 自動進基準集
            r = c.get("/console/agent/overview", params={"business_id": biz})
            ov = r.json()
            check(r.status_code == 200 and ov.get("ok"), f"overview 200（實得 {r.status_code}）")
            r = c.get("/console/agent/faqs", params={"business_id": biz})
            faq_items = r.json().get("items") or []
            picked = [f for f in faq_items if f["question"] == FAQ_Q]
            check(bool(picked), "新建 FAQ 出現在清單")
            faq_id = picked[0]["id"] if picked else 0
            check(faq_id in (ov.get("bindings") or {}).get("faq", []), "★ 上傳即進基準集")
            check(any(x["platform"] == "whatsapp" and x["scope"] == "dm" for x in ov.get("scopes") or []),
                  "overview 帶出兩層開關（scopes）")
            check(isinstance(ov.get("accounts"), list) and len(ov["accounts"]) == 1, "帶出平台帳號")

            r = c.get("/console/agent/logs", params={"business_id": biz})
            check(r.status_code == 200 and isinstance(r.json().get("logs"), list), "logs 端點回清單")

            r = c.post("/console/agent/layer",
                       json={"business_id": biz, "layer": "faq", "enabled": False}, headers=H)
            check(r.status_code == 200, "層開關可切")
            r = c.post("/console/agent/layer",
                       json={"business_id": biz, "layer": "faq", "enabled": True}, headers=H)
            check(r.status_code == 200, "層開關可還原")

            r = c.post("/console/agent/test",
                       json={"business_id": biz, "platform": "whatsapp", "scope": "dm",
                             "text": FAQ_Q, "layers": ["faq"]}, headers=H)
            tb = r.json()
            check(r.status_code == 200 and tb.get("tier") == "faq", f"命中測試命中 FAQ（實得 {tb.get('tier')}）")
            check(tb.get("llm_calls") == 0, f"純規則命中 0 次模型呼叫（實得 {tb.get('llm_calls')}）")
            r = c.post("/console/agent/test",
                       json={"business_id": biz, "platform": "whatsapp", "scope": "dm",
                             "text": "", "layers": ["faq"]}, headers=H)
            check(r.status_code == 400, f"空問句 → 400（實得 {r.status_code}）")

            print("\n[9] Connector 不可達 → 502")
            config.CONNECTOR_INTERNAL_BASE = "http://127.0.0.1:9"
            r = c.get("/console/agent/conversations",
                      params={"business_id": biz, "platform": "whatsapp"})
            check(r.status_code == 502, f"取不到會話 → 502（實得 {r.status_code}）")
            config.CONNECTOR_INTERNAL_BASE = CONN_BASE
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        cleanup()

    print("\n" + "=" * 68)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    for f in failures:
        print("   -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
