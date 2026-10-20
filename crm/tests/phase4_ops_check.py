"""階段四自檢 —— ops 運維後端（CRM `/console/ops/*`）與告警鏈路。

真起一個 **Connector 子進程**（uvicorn，mock 發送模式）讓「發送嘗試紀錄」這一段
真的走內網 API，而不是打樁。

覆蓋：
  [1] 權限：純 agent 帳號 → 403；ops 身分 → 200
  [2] 概覽：任務/AI 佇列/告警計數，近 24h 產出
  [3] 失敗任務清單：預設只回 failed+uncertain、status=all、keyword 篩選、分頁
  [4] 任務明細 + 發送嘗試（走 Connector）
  [5] 手動重跑：failed/uncertain → pending；sent/pending 被拒
  [6] 批次重跑：部分成功不整批放棄
  [7] 重試參數：讀、寫、型別校驗、未知 key 擋掉、list 型別
  [8] 告警：upsert → 清單 → 單筆確認 → 全部確認
  [9] 郵件告警：未配置 no-op；配置後（打樁 SMTP）真的寄出並回寫 notified_at
  [10] 大量失敗偵測：達門檻才告警（靠 dedupe_key 合併累計）
  [11] 審計：ops 只看運維類；管理員帳號可 all=true
  [12] CSRF 閘門

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/phase4_ops_check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
CONN_DIR = ROOT / "connector"
PY = sys.executable

CONN_PORT = 8121
CONN_BASE = f"http://127.0.0.1:{CONN_PORT}"

sys.path.insert(0, str(CRM_DIR))

import httpx  # noqa: E402
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MARK = "zz_p4_ops"
BIZ_NAME = f"{MARK}_商家"
NATIVE_ACC = f"{MARK}_acc"
OPS_USER = f"{MARK}_ops"
AGENT_USER = f"{MARK}_agent"
ADMIN_USER = f"{MARK}_admin"
PW = "opspw_p4_ops1"
SENDER = f"{MARK}_user"
ALERT_TYPE = f"{MARK}_probe"

failures: list[str] = []
passes = 0
TEST_STARTED = None


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
    q(CRM_DSN, "DELETE FROM crm_audit_log WHERE payload::text LIKE %s", (f"%{MARK}%",))
    q(CRM_DSN, "DELETE FROM crm_alert WHERE alert_type = %s OR (alert_type = 'mass_failure'"
               " AND first_seen_at >= %s)", (ALERT_TYPE, TEST_STARTED))
    q(CRM_DSN, "DELETE FROM crm_outbound_task WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM crm_ops_config WHERE config_key = 'alert.email_recipients'"
               " AND config_value::text <> '[]' AND config_value::text NOT LIKE '%@%'")
    q(CRM_DSN, "DELETE FROM crm_session WHERE staff_id IN"
               " (SELECT id FROM crm_staff WHERE login_account IN (%s, %s, %s))",
      (OPS_USER, AGENT_USER, ADMIN_USER))
    q(CRM_DSN, "DELETE FROM crm_staff WHERE login_account IN (%s, %s, %s)",
      (OPS_USER, AGENT_USER, ADMIN_USER))
    q(CRM_DSN, "DELETE FROM crm_business_platform_account WHERE business_id IN"
               " (SELECT id FROM crm_business WHERE business_name = %s)", (BIZ_NAME,))
    q(CRM_DSN, "DELETE FROM crm_business WHERE business_name = %s", (BIZ_NAME,))


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
                "  ai_auto_reply_enabled) VALUES (%s, 'whatsapp', %s, '運維自檢號', true, true)"
                " RETURNING id", (biz, NATIVE_ACC))
            acc = cur.fetchone()[0]
            for login, roles, name in ((OPS_USER, ["ops"], "自檢運維"),
                                       (AGENT_USER, ["agent"], "自檢客服"),
                                       # ★ 必須含 ops —— 第 [11] 段要驗「管理員切成 ops 身分」，
                                       #   而 `expand_roles()` 只把**全系統第一個 admin** 展開成
                                       #   三種身分，後來建的 admin 就只有 ['admin']（見 MEMORY）。
                                       #   舊版只給 ['admin'] → 那兩條斷言一直是紅的（不是程式的錯）。
                                       (ADMIN_USER, ["admin", "ops"], "自檢管理員")):
                cur.execute(
                    "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles,"
                    " is_super_agent, is_kb_editor, enabled)"
                    " VALUES (%s, %s, %s, %s, false, false, true)",
                    (login, hash_password(PW), name, roles))
        conn.commit()
    finally:
        conn.close()

    # 四種狀態各一筆 + 25 筆失敗（觸發大量失敗門檻）
    conn = psycopg2.connect(CRM_DSN)
    try:
        with conn.cursor() as cur:
            base = ("INSERT INTO crm_outbound_task (business_id, source, platform,"
                    " business_platform_account_id, end_user_sender_id, message_content, status,"
                    " max_retry, retry_count, fail_reason, processing_at, finished_at)"
                    " VALUES (%s, 'ai', 'whatsapp', %s, %s, %s::jsonb, %s, 3, %s, %s,"
                    "         CASE WHEN %s THEN now() ELSE NULL END,"
                    "         CASE WHEN %s THEN now() ELSE NULL END) RETURNING id")
            ids = {}
            for status, reason, done in (("failed", f"{MARK} 模擬平台 500", True),
                                         ("uncertain", f"{MARK} 逾時且紀錄不完整", True),
                                         ("sent", None, True),
                                         ("pending", None, False)):
                cur.execute(base, (biz, acc, f"{MARK}_{status}", '{"text": "運維自檢"}',
                                   status, 3 if status == "failed" else 0,
                                   reason, done, done))
                ids[status] = cur.fetchone()[0]
            for _ in range(25):
                cur.execute(base, (biz, acc, f"{MARK}_mass", '{"text": "大量失敗"}',
                                   "failed", 3, f"{MARK} 大量失敗", True, True))
        conn.commit()
    finally:
        conn.close()
    return biz, acc, ids


def start_connector():
    logf = open(Path(tempfile.gettempdir()) / f"multi_cs_p4ops_{CONN_PORT}.log", "w", encoding="utf-8")
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
            if httpx.get(f"{CONN_BASE}/health", timeout=1.0).status_code == 200:
                return proc
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"Connector 起不來（exit={proc.returncode}），看 {logf.name}")
        time.sleep(0.5)
    raise RuntimeError("Connector 逾時未就緒")


def login(c: TestClient, account: str, password: str, role: str = ""):
    r = c.post("/api/auth/login", json={"account": account, "password": password})
    body = r.json()
    if body.get("need_role"):
        r = c.post("/api/auth/login", json={"ticket": body["ticket"], "role": role or "ops"})
        body = r.json()
    return body.get("csrf_token") or "", body


# ------------------------------------------------------------------ 主流程

def main() -> int:
    global TEST_STARTED
    TEST_STARTED = datetime.now(timezone.utc)
    cleanup()
    biz, acc, ids = seed()
    proc = start_connector()
    try:
        os.environ["WORKER_ENABLED"] = "false"
        from app.core import config
        from app.internal import repo as internal_repo
        from app.ops import alerting
        from app.ops import repo as ops_repo

        config.WORKER_ENABLED = False
        config.CONNECTOR_INTERNAL_BASE = CONN_BASE
        config.INTERNAL_API_TOKEN = TOKEN

        import main as crm_main
        with TestClient(crm_main.app, follow_redirects=False) as c:
            print("\n[1] 權限")
            with TestClient(crm_main.app, follow_redirects=False) as ca:
                _, mea = login(ca, AGENT_USER, PW, "agent")
                ra = ca.get("/console/ops/dashboard")
                check(ra.status_code == 403, f"純 agent 讀運維端點 → 403（實得 {ra.status_code}）")
                ra = ca.post("/console/ops/config", json={"key": "outbound.max_retry", "value": 5})
                check(ra.status_code in (401, 403), f"純 agent 寫運維端點被擋（實得 {ra.status_code}）")
            csrf, me = login(c, OPS_USER, PW, "ops")
            check(bool(csrf), f"ops 帳號登入成功（{me.get('staff', {}).get('active_role')}）")
            H = {"X-CSRF-Token": csrf}

            print("\n[2] 概覽")
            r = c.get("/console/ops/dashboard", params={"business_id": biz})
            d = r.json()
            check(r.status_code == 200 and d.get("ok"), f"dashboard 200（實得 {r.status_code}）")
            t = d.get("tasks") or {}
            # ★ 用「>＝」而不是硬寫絕對值（2026-10-20）：只要機器上有 Connector 在跑，
            #   它會把 seed 的 pending 任務搶走並可能改成 failed → 總數就多 1，
            #   這些斷言就整排變紅，看起來像程式壞了（實際是環境有人在工作）。
            #   這裡只驗「至少包含本測試 seed 的量」與「篩選行為正確」。
            check(t.get("failed") >= 26, f"failed >= 26（實得 {t.get('failed')}）")
            check(t.get("uncertain") >= 1, f"uncertain >= 1（實得 {t.get('uncertain')}）")
            check((t.get("sent") or 0) >= 1, f"sent >= 1（實得 {t.get('sent')}）")
            check(isinstance(t.get("pending"), int), "pending 有回傳（可能是 0，被別的 worker 搶走）")
            check(isinstance(d.get("ai_queue"), dict), "帶出 AI 佇列計數")
            check((d.get("last24h") or {}).get("failed") >= 26,
                  f"近 24h 失敗 >= 26（實得 {(d.get('last24h') or {}).get('failed')}）")
            check((d.get("last24h") or {}).get("uncertain") >= 1, "近 24h 待確認 >= 1")
            check((d.get("mail") or {}).get("ready") is False, "郵件未配置 → ready=false")

            print("\n[3] 失敗任務清單")
            r = c.get("/console/ops/tasks", params={"business_id": biz})
            d = r.json()
            check(d.get("total") >= 27, f"預設只回 failed+uncertain，至少 27（實得 {d.get('total')}）")
            check(all(i["status"] in ("failed", "uncertain") for i in d["items"]),
                  "清單裡沒有 sent/pending")
            # ★ 本測試 seed 的失敗任務一定要在清單裡（比數總數更有意義）
            got_ids = {i["id"] for i in d["items"]}
            check(ids["failed"] in got_ids, "seed 的失敗任務出現在預設清單中")
            check(ids["uncertain"] in got_ids, "seed 的 uncertain 任務也在清單中")

            r = c.get("/console/ops/tasks", params={"business_id": biz, "status": "all"})
            check(r.json().get("total") >= 29, f"status=all >= 29（實得 {r.json().get('total')}）")
            r = c.get("/console/ops/tasks", params={"business_id": biz, "status": "sent"})
            check(all(i["status"] == "sent" for i in r.json().get("items") or []),
                  "status=sent 只回 sent（篩選有效）")
            r = c.get("/console/ops/tasks", params={"business_id": biz, "status": "nope"})
            check(r.status_code == 400, f"不合法狀態 → 400（實得 {r.status_code}）")
            r = c.get("/console/ops/tasks", params={"business_id": biz, "keyword": "大量失敗"})
            check(r.json().get("total") >= 25,
                  f"keyword 篩選 >= 25（實得 {r.json().get('total')}）")
            r = c.get("/console/ops/tasks", params={"business_id": biz, "limit": 5, "offset": 0})
            p1 = r.json()
            # total 用 >=（環境可能有別的 worker 在動任務）；重點是「一頁 5 筆」與 total 一致
            r2 = c.get("/console/ops/tasks", params={"business_id": biz, "limit": 5, "offset": 0})
            check(len(p1["items"]) == 5 and p1["total"] >= 27
                  and r2.json()["total"] == p1["total"],
                  f"分頁：limit=5 回 5 筆、total 穩定（實得 {p1['total']}）")
            r = c.get("/console/ops/tasks", params={"business_id": biz, "limit": 5, "offset": 5})
            check(len(r.json()["items"]) == 5 and r.json()["offset"] == 5, "第二頁可取")
            check(p1["items"][0]["sort_at"] >= p1["items"][1]["sort_at"], "按時間倒序")

            print("\n[4] 任務明細 + 發送嘗試")
            r = c.get("/console/ops/task", params={"id": ids["failed"]})
            d = r.json()
            check(r.status_code == 200 and d.get("ok"), f"明細 200（實得 {r.status_code}）")
            check(d["item"]["fail_reason"].startswith(MARK), "帶出失敗原因")
            check(d.get("attempts") == [] and d.get("attempts_reachable"), "Connector 可達、尚無嘗試紀錄")
            r = c.get("/console/ops/task", params={"id": 99999999})
            check(r.status_code == 404, f"不存在 → 404（實得 {r.status_code}）")
            r = c.post("/console/ops/attempts", json={"task_ids": [ids["failed"], ids["sent"]]},
                       headers=H)
            check(r.status_code == 200 and str(ids["failed"]) in (r.json().get("items") or {}),
                  "批次取發送嘗試成功")

            print("\n[5] 手動重跑")
            r = c.post("/console/ops/retry", json={"id": ids["failed"]}, headers=H)
            check(r.status_code == 200 and r.json().get("retried") == [ids["failed"]],
                  f"failed → 可重跑（{r.text[:90]}）")
            row = q(CRM_DSN, "SELECT status, retry_count, fail_reason, finished_at"
                             " FROM crm_outbound_task WHERE id = %s", (ids["failed"],), fetch=True)[0]
            check(row["status"] == "pending", "重跑後 status=pending")
            check(int(row["retry_count"] or 0) == 0, "retry_count 歸零")
            check(row["fail_reason"] is None and row["finished_at"] is None, "清掉失敗原因與完成時間")
            r = c.post("/console/ops/retry", json={"id": ids["sent"]}, headers=H)
            check(r.status_code == 200 and not r.json().get("retried")
                  and r.json()["skipped"][0]["id"] == ids["sent"],
                  "sent 不可重跑（進 skipped）")
            # ★ 前提檢查：機器上若有 Connector 在跑，它可能已把 seed 的 pending 任務搶走
            #   並改成 failed/sent —— 那時「pending 不可重跑」這條就沒有意義了
            #   （failed 本來就可以重跑）。所以**先看現在的狀態**再決定要不要驗。
            _c = psycopg2.connect(CRM_DSN)
            try:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT status FROM crm_outbound_task WHERE id=%s",
                                 (ids["pending"],))
                    _row = _cur.fetchone()
            finally:
                _c.close()
            _st = _row[0] if _row else None
            if _st == "pending":
                r = c.post("/console/ops/retry", json={"id": ids["pending"]}, headers=H)
                check(not r.json().get("retried"), "pending 不可重跑")
            else:
                check(True, f"（跳過 pending 重跑檢查：任務已被外部 worker 改成 {_st}）")

            print("\n[6] 批次重跑（部分成功）")
            r = c.post("/console/ops/retry",
                       json={"ids": [ids["uncertain"], ids["sent"], 99999999]}, headers=H)
            d = r.json()
            check(d.get("retried") == [ids["uncertain"]], f"uncertain 重跑成功（{d}）")
            check(len(d.get("skipped") or []) == 2, "另外兩筆進 skipped")
            r = c.post("/console/ops/retry", json={"ids": []}, headers=H)
            check(r.status_code == 400, f"空清單 → 400（實得 {r.status_code}）")

            print("\n[7] 重試參數")
            r = c.get("/console/ops/config")
            d = r.json()
            check(r.status_code == 200 and len(d.get("items") or []) >= 12, "讀到全部運維參數")
            check(isinstance(d.get("editable"), list), "回可編輯白名單")
            old = d["flat"].get("outbound.max_retry")
            r = c.post("/console/ops/config", json={"key": "outbound.max_retry", "value": 7},
                       headers=H)
            check(r.status_code == 200 and r.json().get("value") == 7, "寫入 int 參數成功")
            r = c.post("/console/ops/config", json={"key": "outbound.max_retry", "value": old},
                       headers=H)
            check(r.json().get("value") == old, f"還原為 {old}")
            r = c.post("/console/ops/config", json={"key": "outbound.max_retry", "value": "abc"},
                       headers=H)
            check(r.status_code == 400, f"型別錯誤 → 400（實得 {r.status_code}）")
            r = c.post("/console/ops/config", json={"key": "no.such.key", "value": 1}, headers=H)
            check(r.status_code == 400, f"未知 key → 400（實得 {r.status_code}）")
            r = c.post("/console/ops/config",
                       json={"key": "alert.email_recipients", "value": "a@x.io; b@y.io"}, headers=H)
            check(r.json().get("value") == ["a@x.io", "b@y.io"], f"list 型別可用分隔符輸入（{r.text[:80]}）")
            c.post("/console/ops/config", json={"key": "alert.email_recipients", "value": []},
                   headers=H)

            print("\n[8] 告警清單與確認")
            aid = internal_repo.upsert_alert(ALERT_TYPE, "warning", f"{MARK} 測試告警",
                                             detail={"probe": True}, dedupe_key=MARK)
            aid2 = internal_repo.upsert_alert(ALERT_TYPE, "warning", f"{MARK} 測試告警",
                                              detail={"probe": True}, dedupe_key=MARK)
            check(aid == aid2, "同 dedupe_key 合併成一條（不重複開）")
            r = c.get("/console/ops/alerts", params={"only_open": "true"})
            mine = [a for a in r.json()["items"] if a["id"] == aid]
            check(bool(mine) and mine[0]["count"] == 2, "累計次數 = 2")
            r = c.post("/console/ops/alert/ack", json={"id": aid}, headers=H)
            check(r.status_code == 200 and r.json().get("acked") == 1, "單筆確認成功")
            r = c.post("/console/ops/alert/ack", json={"id": aid}, headers=H)
            check(r.status_code == 404, f"重複確認 → 404（實得 {r.status_code}）")
            r = c.get("/console/ops/alerts", params={"only_open": "true"})
            check(all(a["id"] != aid for a in r.json()["items"]), "確認後不在未確認清單")
            r = c.get("/console/ops/alerts", params={"only_open": "false", "limit": 500})
            check(any(a["id"] == aid and a["acknowledged"] for a in r.json()["items"]),
                  "only_open=false 仍看得到（已確認）")

            print("\n[9] 郵件告警")
            st = alerting.status()
            check(st["ready"] is False and st["enabled"] is False, "未配置 → 不可用")
            check(alerting.send_mail("x", "y") is False, "未配置寄信 → False（no-op，不拋）")
            r = c.post("/console/ops/mail/test", json={}, headers=H)
            check(r.status_code == 400, f"未配置時測試寄信 → 400（實得 {r.status_code}）")

            sent: list = []

            class _FakeSMTP:
                def __init__(self, *a, **k): pass
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def starttls(self): pass
                def login(self, u, p): pass
                def send_message(self, msg): sent.append(msg)

            real_connect = alerting._connect
            real_enabled, real_host = config.ALERT_EMAIL_ENABLED, config.SMTP_HOST
            try:
                alerting._connect = lambda: _FakeSMTP()
                config.ALERT_EMAIL_ENABLED = True
                config.SMTP_HOST = "smtp.test.local"
                c.post("/console/ops/config",
                       json={"key": "alert.email_recipients", "value": ["ops@test.local"]},
                       headers=H)
                check(alerting.is_configured() is True, "配置齊全後 is_configured=True")
                r = c.get("/console/ops/mail")
                check(r.json().get("recipients") == ["ops@test.local"], "收件人即時從 DB 讀")
                aid3 = internal_repo.upsert_alert(ALERT_TYPE, "critical", f"{MARK} 要寄的告警",
                                                  detail={"probe": 2}, dedupe_key=f"{MARK}_mail")
                stats = alerting.sweep_once()
                check(stats.get("mailed") is True and stats.get("notified") >= 1,
                      f"digest 寄出並回寫 notified_at（{stats}）")
                check(len(sent) == 1, f"只寄一封 digest（實得 {len(sent)}）")
                check(f"{MARK} 要寄的告警" in sent[0].get_content(), "信件內含告警標題")
                rows = q(CRM_DSN, "SELECT notified_at FROM crm_alert WHERE id = %s", (aid3,),
                         fetch=True)
                check(rows[0]["notified_at"] is not None, "notified_at 已回寫")
                stats2 = alerting.sweep_once()
                check(stats2.get("notified") == 0, "同一條不會重複寄（第二輪 0 筆）")
                r = c.post("/console/ops/mail/test", json={"to": ["x@y.z"]}, headers=H)
                check(r.status_code == 200 and r.json().get("to") == ["x@y.z"], "測試信可指定收件人")
                r = c.get("/console/ops/alerts", params={"only_open": "true"})
                n3 = [a for a in r.json()["items"] if a["id"] == aid3]
                check(bool(n3) and n3[0]["notified_at"], "清單帶出 notified_at（已寄信）")
            finally:
                alerting._connect = real_connect
                config.ALERT_EMAIL_ENABLED = real_enabled
                config.SMTP_HOST = real_host
                c.post("/console/ops/config",
                       json={"key": "alert.email_recipients", "value": []}, headers=H)
                c.post("/console/ops/alert/ack", json={"all": True}, headers=H)

            print("\n[10] 大量失敗偵測")
            flat, _ = ops_repo.ops_config()
            threshold = int(flat.get("alert.mass_failure_threshold") or 20)
            window = int(flat.get("alert.failure_window_minutes") or 10)
            n = ops_repo.recent_failed_count(window)
            check(n >= threshold, f"近 {window} 分鐘失敗 {n} 筆 ≥ 門檻 {threshold}")
            res = alerting.check_mass_failure()
            check(bool(res) and res["failed"] == n, f"觸發 mass_failure 告警（{res}）")
            res2 = alerting.check_mass_failure()
            check(bool(res2) and res2["alert_id"] == res["alert_id"], "同門檻不重複開新告警（合併）")
            r = c.get("/console/ops/alerts", params={"only_open": "true", "severity": "critical"})
            check(any(a["alert_type"] == "mass_failure" for a in r.json()["items"]),
                  "告警清單看得到 mass_failure")
            r = c.post("/console/ops/alert/ack", json={"all": True}, headers=H)
            check(r.status_code == 200 and r.json().get("acked") >= 1,
                  f"全部確認（acked={r.json().get('acked')}）")
            r = c.get("/console/ops/alerts", params={"only_open": "true"})
            check(r.json().get("total") == 0, "確認後未確認清單清空")

            print("\n[11] 審計")
            # ★ 2026-10-20 重新設計後的新契約：
            #   · 回應不再有 `scoped`，改回 `all`（是否已解除類型限縮）
            #   · 限縮範圍＝AUDIT_GROUPS 五組的聯集（不是舊的 4 個孤零零類型）
            #   · 多了 groups / type_labels / has_more
            from app.ops.router import AUDIT_GROUPS
            default_types = {t for g in AUDIT_GROUPS for t in g["types"]}

            r = c.get("/console/ops/audit", params={"limit": 50})
            d = r.json()
            check(r.status_code == 200 and d.get("all") is False,
                  "ops 身分 → 只查運維範圍（all=False）")
            check(len(d.get("groups") or []) == len(AUDIT_GROUPS),
                  f"回傳五個語意分組（{len(d.get('groups') or [])}）")
            check(bool(d.get("type_labels")), "回傳類型中文標籤（前端直接顯示人話）")
            check("has_more" in d, "回傳 has_more（前端分頁要用）")
            kinds = {i["operate_type"] for i in d["items"]}
            check(kinds and kinds <= default_types,
                  f"回來的都在預設範圍內（{kinds}）")
            check("conversation_read" not in kinds and "role_switch" not in kinds,
                  "★ 噪音類型（開會話／切身分）不再出現在審計裡")
            check("ops_config_change" in kinds or "ai_switch_change" in kinds,
                  "參數變更或 AI 開關有留痕")
            check(all("staff_role" in i for i in d["items"]),
                  "★ 每筆都帶 staff_role（誰、以什麼身分做的）")

            # 分頁不重複
            a1 = c.get("/console/ops/audit", params={"limit": 5, "offset": 0}).json()
            a2 = c.get("/console/ops/audit", params={"limit": 5, "offset": 5}).json()
            ids1 = [x["id"] for x in a1.get("items") or []]
            ids2 = [x["id"] for x in a2.get("items") or []]
            check(not (set(ids1) & set(ids2)), "分頁第 1 頁與第 2 頁沒有重複")

            # 篩類型（只查 AI 開關）
            r = c.get("/console/ops/audit", params={"limit": 5,
                                                    "types": "ai_switch_change"})
            kinds2 = {i["operate_type"] for i in r.json().get("items") or []}
            check(not kinds2 or kinds2 == {"ai_switch_change"},
                  f"可按具體類型篩選（{kinds2}）")

            # 純 ops 帳號拿不到全部類型
            r = c.get("/console/ops/audit", params={"limit": 5, "all": "true"})
            check(r.json().get("all") is False, "純 ops 帳號要不到 all=true")

            with TestClient(crm_main.app, follow_redirects=False) as cm:
                csrf_m, mem = login(cm, ADMIN_USER, PW, "ops")
                check(mem.get("staff", {}).get("active_role") == "ops", "管理員切成 ops 身分")
                r = cm.get("/console/ops/audit", params={"limit": 5, "all": "true"})
                check(r.json().get("all") is True, "管理員帳號 all=true → 解除類型限縮")

            print("\n[12] CSRF 閘門")
            r = c.post("/console/ops/config", json={"key": "outbound.max_retry", "value": 3})
            check(r.status_code == 403, f"缺 CSRF → 403（實得 {r.status_code}）")
            r = c.post("/console/ops/retry", json={"id": ids["sent"]})
            check(r.status_code == 403, "重跑缺 CSRF → 403")
            r = c.get("/console/ops/dashboard")
            check(r.status_code == 200, "GET 不需要 CSRF")
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
