"""自檢 —— 運維端「郵件告警參數」：可填、可測、**測試成功才能存**（2026-10-23）。

小jo 的需求原文：
  「郵件告警參數配置需要可以在運維端填寫，並且填寫完後點擊測試，
    測試成功後才能夠保存配置。」

所以這支要驗的不是「有沒有寄出去」，而是**那條規則擋不擋得住**：
  [1] 參數來源：DB 沒存過 → 退回 .env（既有部署行為不變）
  [2] 靜態校驗：缺主機/收件人/寄件人 → 400 且講清楚缺什麼
  [3] 測試失敗（認證錯 / TLS 錯 / 連不上）→ 400 + **人看得懂的原因**，且**不發 ticket**
  [4] 測試成功 → 回 ticket
  [5] ★ 沒有 ticket 直接保存 → 400（**不能繞過**）
  [6] ★ 測試後改了參數再存 → 400 + 指出哪一格變了（防「測 A 存 B」）
  [7] ★ 別人的 ticket → 403
  [8] ★ ticket 只能用一次（存完即失效）
  [9] 正常流程：測試 → 保存 → DB 真的有值 → status() 立刻反映（不用重啟）
  [10] 敏感欄：`GET /config` 與 `/alert/email` 都不回密碼明文
  [11] SMTP 那幾個 key 不在單鍵保存白名單（沒有「不測試也能存」的後門）
  [12] 審計有寫，但**不含密碼明文**

⚠️ 全程打樁 SMTP（`alerting._connect`），不連外網、不寄真信。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/alert_mail_config_check.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
sys.path.insert(0, str(CRM_DIR))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MARK = "zz_mailcfg"
OPS_USER = f"{MARK}_ops"
PW = "mailcfg_pw_1"

passes = 0
failures: list[str] = []


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


def q(sql: str, args=None, fetch: bool = False):
    from app.core import config
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            n = cur.rowcount
        conn.commit()
        return rows if fetch else n
    finally:
        conn.close()


def cleanup() -> None:
    """只清本測試自己造的東西 —— 運維參數是全域的，**不能整批刪**。

    ⚠️ 會把 SMTP 那 7 個 key 刪掉（測試前它們本來就沒存過；
       真的環境若有值，跑完這支等於把運維端的設定清空 → 所以一定要先確認）。
    """
    # ⚠️ 只刪**本測試造出來**的 7 個 SMTP key。
    #    `alert.email_recipients` / `alert.email_min_severity` 是原本就 seed 過的，
    #    刪掉會讓上層看不到「這一列存在」（實測踩過：跑完自檢，參數表少了兩列）。
    q("DELETE FROM crm_ops_config WHERE config_key = ANY(%s)",
      (["alert.email_enabled", "alert.smtp_host", "alert.smtp_port", "alert.smtp_user",
        "alert.smtp_password", "alert.smtp_from", "alert.smtp_tls"],))
    q("DELETE FROM crm_audit_log WHERE payload::text LIKE %s", (f"%{MARK}%",))
    q("DELETE FROM crm_session WHERE staff_id IN"
      " (SELECT id FROM crm_staff WHERE login_account = %s)", (OPS_USER,))
    q("DELETE FROM crm_staff WHERE login_account = %s", (OPS_USER,))


def seed() -> int:
    from app.core.security import hash_password
    rows = q("""INSERT INTO crm_staff (login_account, staff_name, password_hash,
                                       roles, is_super_agent, is_kb_editor, enabled)
                VALUES (%s, %s, %s, ARRAY['ops','admin']::text[], false, false, true)
                RETURNING id""",
             (OPS_USER, f"{MARK} 運維", hash_password(PW)), fetch=True)
    return int(rows[0]["id"])


def login(c: TestClient):
    r = c.post("/api/auth/login", json={"account": OPS_USER, "password": PW})
    body = r.json()
    if body.get("need_role"):
        r = c.post("/api/auth/login", json={"ticket": body["ticket"], "role": "ops"})
        body = r.json()
    return body.get("csrf_token") or ""


class FakeSMTP:
    """假的 SMTP —— 用類別變數記錄呼叫，方便斷言「有沒有真的連／登入／寄」。"""

    sent: list = []
    logins: list = []
    mode = "ok"          # ok | auth | tls | refused

    def __init__(self, host=None, port=None, timeout=None):
        FakeSMTP.last = (host, port)

    def __enter__(self):
        if FakeSMTP.mode == "refused":
            raise ConnectionRefusedError("[WinError 10061] No connection could be made")
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kw):
        if FakeSMTP.mode == "tls":
            raise __import__("ssl").SSLError(
                "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1006)")

    def login(self, u, p):
        FakeSMTP.logins.append((u, p))
        if FakeSMTP.mode == "auth":
            import smtplib
            raise smtplib.SMTPAuthenticationError(
                535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


# ⚠️ 2026-10-23 起「參數」不含啟用開關 —— 開關走 /alert/email/toggle
BASE = {"host": "smtp.test.local", "port": 587, "user": "bot@test.local",
        "password": "s3cret-pw", "sender": "bot@test.local", "tls": True,
        "recipients": ["ops@test.local"], "min_severity": "warning"}


def main() -> int:
    cleanup()
    cleanup_sessions()
    seed()

    os.environ["WORKER_ENABLED"] = "false"
    from app.core import config
    from app.internal import repo as internal_repo
    from app.ops import alerting

    config.WORKER_ENABLED = False
    import main as crm_main

    real_connect = alerting._connect

    def _fake_connect(s=None):
        """★ 必須重現 `_connect()` 的行為（包含 starttls），
        否則「TLS 選錯」這條路徑在自檢裡永遠不會被走到 —— 測了等於沒測。"""
        st = s or alerting.settings()
        sm = FakeSMTP(st["alert.smtp_host"], st["alert.smtp_port"])
        if st["alert.smtp_tls"] and int(st["alert.smtp_port"]) != 465:
            sm.starttls()
        return sm

    alerting._connect = _fake_connect

    try:
        with TestClient(crm_main.app, follow_redirects=False) as c:
            csrf = login(c)
            H = {"X-CSRF-Token": csrf}
            check(bool(csrf), "ops 帳號登入成功")

            # ---------------- [1] 沒存過 → 退回 .env
            print("\n[1] 參數來源：DB 沒存過 → 用 .env")
            st = alerting.settings()
            check(st["source"] == "env",
                  "★ 沒存過 → source=env（升級後行為不變）", str(st["source"]))
            r = c.get("/console/ops/alert/email")
            check(r.status_code == 200 and r.json()["settings"]["source"] == "env",
                  "GET /alert/email 也回 source=env")

            # ---------------- [2] 靜態校驗
            print("\n[2] 靜態校驗：缺必填就 400，且講清楚缺什麼")
            r = c.post("/console/ops/alert/email/test",
                       json={**BASE, "host": ""}, headers=H)
            check(r.status_code == 400 and "主機" in r.json().get("error", ""),
                  f"缺主機 → 400（{r.json().get('error')}）")
            r = c.post("/console/ops/alert/email/test",
                       json={**BASE, "recipients": []}, headers=H)
            check(r.status_code == 400 and "收件人" in r.json().get("error", ""),
                  f"缺收件人 → 400（{r.json().get('error')}）")
            r = c.post("/console/ops/alert/email/test",
                       json={**BASE, "port": 99999}, headers=H)
            check(r.status_code == 400 and "連接埠" in r.json().get("error", ""),
                  f"埠不合法 → 400（{r.json().get('error')}）")
            r = c.post("/console/ops/alert/email/test",
                       json={**BASE, "user": "", "password": "orphan"}, headers=H)
            check(r.status_code == 400, f"填了密碼卻沒帳號 → 400（{r.json().get('error')}）")

            # ---------------- [3] 測試失敗要把原因講清楚
            print("\n[3] 測試失敗 → 400 + 人看得懂的原因，且不發 ticket")
            for mode, expect in (("auth", "認證"), ("tls", "TLS"), ("refused", "拒絕")):
                FakeSMTP.mode = mode
                r = c.post("/console/ops/alert/email/test", json=BASE, headers=H)
                err = r.json().get("error", "")
                check(r.status_code == 400 and expect in err,
                      f"★ {mode} → 400 且原因含「{expect}」（{err[:70]}）")
                check("ticket" not in r.json(), f"{mode}：失敗不發 ticket")
            FakeSMTP.mode = "ok"

            # ---------------- [4] 測試成功 → ticket
            print("\n[4] 測試成功 → 回 ticket、真的寄了信")
            FakeSMTP.sent.clear()
            FakeSMTP.logins.clear()
            r = c.post("/console/ops/alert/email/test", json=BASE, headers=H)
            d = r.json()
            check(r.status_code == 200 and bool(d.get("ticket")),
                  "★ 測試成功回 ticket", str(d.get("ticket"))[:12])
            check(len(FakeSMTP.sent) == 1, f"真的寄了 1 封（實得 {len(FakeSMTP.sent)}）")
            check(FakeSMTP.logins and FakeSMTP.logins[0][0] == "bot@test.local",
                  "用表單上的帳號登入（不是 DB/.env 的）", str(FakeSMTP.logins[:1]))
            check(d.get("to") == ["ops@test.local"], "回報收件人", str(d.get("to")))
            ticket = d["ticket"]

            # ---------------- [5] 沒 ticket 不能存
            print("\n[5] ★ 沒有 ticket 直接保存 → 擋下來")
            n_before = int(q("SELECT count(*) AS n FROM crm_ops_config"
                             " WHERE config_key LIKE 'alert.smtp%%'", fetch=True)[0]["n"])
            r = c.post("/console/ops/alert/email/save", json=BASE, headers=H)
            check(r.status_code == 400 and "測試" in r.json().get("error", ""),
                  f"沒 ticket → 400（{r.json().get('error')}）")
            n_after = int(q("SELECT count(*) AS n FROM crm_ops_config"
                           " WHERE config_key LIKE 'alert.smtp%%'", fetch=True)[0]["n"])
            check(n_after == n_before, "★ 連一列都沒寫進 DB",
                  f"存前 {n_before} 列 / 存後 {n_after} 列")

            # ---------------- [6] 改參數後不能存（測 A 存 B）
            print("\n[6] ★ 測試後改了參數 → 400 + 指出哪一格變了")
            r = c.post("/console/ops/alert/email/save",
                       json={**BASE, "host": "smtp.evil.local", "ticket": ticket}, headers=H)
            check(r.status_code == 400 and "改過" in r.json().get("error", ""),
                  f"改 host 後保存 → 400（{r.json().get('error')}）")
            check("smtp_host" in r.json().get("error", "") or "smtp" in r.json().get("error", ""),
                  "錯誤訊息指出是哪一格變了", r.json().get("error", "")[:80])

            # ---------------- [7] 別人的 ticket
            print("\n[7] ★ 別人的 ticket → 403")
            from app.ops import router as ops_router
            ops_router._MAIL_TICKETS[ticket]["staff_id"] = 99999999
            r = c.post("/console/ops/alert/email/save", json={**BASE, "ticket": ticket}, headers=H)
            check(r.status_code == 403, f"別人拿的 ticket → 403（實得 {r.status_code}）")
            ops_router._MAIL_TICKETS.pop(ticket, None)

            # ---------------- [9] 正常流程：測試 → 保存
            print("\n[9] 正常流程：測試 → 保存 → DB 有值、status 立刻反映")
            r = c.post("/console/ops/alert/email/test", json=BASE, headers=H)
            ticket = r.json()["ticket"]
            r = c.post("/console/ops/alert/email/save",
                       json={**BASE, "ticket": ticket}, headers=H)
            check(r.status_code == 200, f"保存成功（實得 {r.status_code}）",
                  str(r.json())[:100])
            saved = r.json().get("settings") or {}
            check(saved.get("smtp_host") == "smtp.test.local", "回傳的 settings 已是新值")
            check(saved.get("source") == "db", "★ 存過之後 source=db")
            rows = q("SELECT config_key, config_value FROM crm_ops_config"
                     " WHERE config_key LIKE 'alert.smtp%%'"
                     "    OR config_key IN ('alert.email_recipients',"
                     "                      'alert.email_min_severity')"
                     " ORDER BY config_key", fetch=True)
            keys = {x["config_key"] for x in rows}
            check(len(keys) == 8,
                  f"★ 8 個參數都落庫（6 個 smtp.* + 收件人 + 級別），實得 {len(keys)}",
                  str(sorted(keys)))
            check("alert.email_enabled" not in keys,
                  "★ 保存參數**不會**寫入啟用開關（兩者分開）")
            check(alerting.settings()["alert.smtp_host"] == "smtp.test.local",
                  "alerting.settings() 立刻反映（不需重啟）")
            # ★ 2026-10-23：參數與開關分開後，「剛存好參數但開關沒開」＝不能寄信。
            #   這正是分開的體現（以前 enabled 跟參數一起寫，這條才成立）。
            check(alerting.is_configured() is False,
                  "★ 參數齊但開關未開 → is_configured=False（兩者獨立）")

            # ---------------- [8] ticket 只能用一次
            print("\n[8] ★ ticket 只能用一次")
            r = c.post("/console/ops/alert/email/save",
                       json={**BASE, "ticket": ticket}, headers=H)
            check(r.status_code == 400, f"重複用同一張 ticket → 400（實得 {r.status_code}）")

            # ---------------- [10] 敏感欄不外洩
            print("\n[10] 敏感欄：API 不回密碼明文")
            r = c.get("/console/ops/alert/email")
            d = r.json()["settings"]
            check("password" not in d or not d.get("password"), "GET /alert/email 不回密碼")
            check(d.get("password_set") is True, "只回 password_set=True")
            r = c.get("/console/ops/config")
            d = r.json()
            pw_val = d["flat"].get("alert.smtp_password")
            check(pw_val != "s3cret-pw", f"★ flat 裡不是明文（{pw_val!r}）")
            check(pw_val == alerting.MASK, f"是遮蔽字串（{pw_val!r}）")
            item = next((i for i in d["items"] if i["key"] == "alert.smtp_password"), {})
            check(item.get("masked") is True and item.get("locked") is True,
                  "items 標了 masked + locked")
            check("alert.smtp_password" not in (d.get("editable") or []),
                  "★ 密碼不在 editable 白名單")

            # ---------------- [11] 單鍵端點不能寫 SMTP 參數
            print("\n[11] ★ 單鍵 /config 不能寫 SMTP 參數（沒有「不測試也能存」的後門）")
            for k in ("alert.smtp_host", "alert.smtp_password", "alert.email_enabled"):
                r = c.post("/console/ops/config",
                           json={"key": k, "value": "smtp.bypass.local"}, headers=H)
                check(r.status_code == 400, f"{k} 走單鍵端點 → 400（實得 {r.status_code}）")
            check(alerting.settings()["alert.smtp_host"] == "smtp.test.local",
                  "值沒被偷改")

            # ---------------- [12] 審計不含密碼
            print("\n[12] 審計有寫、但沒有密碼明文")
            rows = q("SELECT payload::text AS p FROM crm_audit_log"
                     " WHERE operate_type IN ('ops_config_change','alert_notify')"
                     "   AND payload::text LIKE '%%alert_email%%'"
                     " ORDER BY id DESC LIMIT 5", fetch=True)
            check(bool(rows), f"有寫審計（{len(rows)} 筆）")
            joined = " ".join(x["p"] for x in rows)
            check("s3cret-pw" not in joined, "★ 審計裡沒有密碼明文")
            check('"password_changed"' in joined, "審計有記 password_changed 旗標")

            # ---------------- [13] 開關（與參數分開）
            print("\n[13] 啟用開關：與參數分離，且未配置不准開")
            st0 = alerting.status()
            check(st0["configured"] is True, "參數已配置 → configured=True")
            check(st0["enabled"] is False, "★ 保存參數**沒有**把開關打開（分開）")

            r = c.post("/console/ops/alert/email/toggle", json={"enabled": True}, headers=H)
            check(r.status_code == 200 and r.json()["settings"]["enabled"] is True,
                  f"開關切到開（實得 {getattr(r, 'status_code', '')}）")
            check(alerting.is_configured() is True, "開著 + 已配置 → is_configured=True")
            FakeSMTP.sent.clear()
            check(alerting.send_mail("x", "y") is True and len(FakeSMTP.sent) == 1,
                  "開著時真的寄得出去")

            # 再存一次參數（重新測試）→ 開關不能被關掉
            r = c.post("/console/ops/alert/email/test", json=BASE, headers=H)
            r = c.post("/console/ops/alert/email/save",
                       json={**BASE, "ticket": r.json()["ticket"]}, headers=H)
            check(r.status_code == 200, "重新保存參數成功")
            check(alerting.status()["enabled"] is True,
                  "★ 重新保存參數不會把已開的提醒關掉")

            r = c.post("/console/ops/alert/email/toggle", json={"enabled": False}, headers=H)
            check(r.status_code == 200 and r.json()["settings"]["enabled"] is False,
                  "開關切到關")
            FakeSMTP.sent.clear()
            check(alerting.send_mail("x", "y") is False, "關掉後 send_mail → False")
            check(not FakeSMTP.sent, "★ 而且完全沒連線/寄信")
            check(alerting.status()["configured"] is True,
                  "★ 關掉開關不影響「參數已配置」狀態")

            # ---- 未配置時不准開（把參數清掉，模擬全新環境）
            print("\n[14] 未配置參數時，開關切不到開")
            q("DELETE FROM crm_ops_config WHERE config_key = ANY(%s)",
              (["alert.smtp_host", "alert.smtp_port", "alert.smtp_user",
                "alert.smtp_password", "alert.smtp_from", "alert.smtp_tls"],))
            q("UPDATE crm_ops_config SET config_value = '[]'::jsonb"
              " WHERE config_key = 'alert.email_recipients'")
            st1 = alerting.status()
            check(st1["configured"] is False, "清掉參數後 configured=False")
            r = c.post("/console/ops/alert/email/toggle", json={"enabled": True}, headers=H)
            check(r.status_code == 400 and "先配置" in r.json().get("error", ""),
                  f"★ 未配置按開 → 400「請先配置郵件參數」（{r.json().get('error')[:60]}）")
            check(alerting.status()["enabled"] is False,
                  "★ 而且開關真的沒被切開（不是只有前端擋）")
    finally:
        alerting._connect = real_connect
        cleanup()

    print("\n" + "=" * 66)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    if failures:
        for f in failures:
            print("   - " + f)
        return 1
    print("全部通過")
    return 0


def cleanup_sessions() -> None:
    q("DELETE FROM crm_session WHERE staff_id IN"
      " (SELECT id FROM crm_staff WHERE login_account = %s)", (OPS_USER,))


if __name__ == "__main__":
    sys.exit(main())
