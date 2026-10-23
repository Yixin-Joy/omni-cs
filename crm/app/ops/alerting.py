"""告警郵件與「大量失敗」巡檢 —— 設計案 §15.3。

兩個獨立職責：

1. **偵測**：近 `alert.failure_window_minutes` 分鐘內 `failed` 任務數 ≥
   `alert.mass_failure_threshold` → `upsert_alert('mass_failure')`（靠 dedupe_key 累計）。
2. **通知**：把 `acknowledged=false AND notified_at IS NULL` 且嚴重度達
   `alert.email_min_severity` 的告警彙整成**一封** digest 寄給 `alert.email_recipients`，
   成功後回寫 `notified_at`。

★ 郵件是通知、不是保證 —— **未配置 = no-op**（只記 log），寄信失敗只回 False，
  絕不往外拋；不然一封寄不出去的信會把整個 sweep 迴圈打斷。

★★ SMTP 參數有**兩個來源**（2026-10-23 小jo 要求「運維端要能填」）：
  · 運維端填的 → `crm_ops_config`（`alert.smtp_*` / `alert.email_*`）
  · `.env` 的   → `config.SMTP_*` / `config.ALERT_EMAIL_ENABLED`（升級前的唯一來源）
  判讀規則是「**DB 有沒有那一列**」，不是「DB 的值空不空」——
  否則運維端刻意把 `enabled` 存成 false、而 .env 是 true 時會被翻回來。
  沒有那一列就退回 .env → 既有部署行為完全不變。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core import config
from app.core import db
from app.internal import repo as internal_repo
from app.ops import repo

log = logging.getLogger("crm")

# 運維端可填的 SMTP 參數 → 對應的 .env 值
_ENV_KEYS = ("alert.email_enabled", "alert.smtp_host", "alert.smtp_port",
             "alert.smtp_user", "alert.smtp_password", "alert.smtp_from",
             "alert.smtp_tls")
# ★★ 「參數」＝不含啟用開關（2026-10-23 小jo：開關與參數分開）。
#   這 8 個是一組、必須一起測試一起存；`alert.email_enabled` 走獨立的開關端點
#   （`POST /console/ops/alert/email/toggle`），開之前會檢查參數是否已配置。
PARAM_KEYS = ("alert.smtp_host", "alert.smtp_port", "alert.smtp_user",
              "alert.smtp_password", "alert.smtp_from", "alert.smtp_tls",
              "alert.email_recipients", "alert.email_min_severity")
# 敏感欄：API 回應要遮蔽、審計不能落明文
SENSITIVE_KEYS = ("alert.smtp_password",)
MASK = "••••••••"


def _db_keys() -> set[str]:
    """DB 裡**真的存在**的參數 key（用來分辨「沒設過」與「設成空字串/false」）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT config_key FROM crm_ops_config WHERE config_key LIKE 'alert.%%'")
            return {str(r[0]) for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001 —— 讀不到就當「都沒設過」，退回 .env
        log.warning("讀取運維參數 key 失敗（改用 .env）: %s", e)
        return set()
    finally:
        conn.close()


def _env_smtp() -> dict:
    return {
        "alert.email_enabled": bool(config.ALERT_EMAIL_ENABLED),
        "alert.smtp_host": config.SMTP_HOST,
        "alert.smtp_port": int(config.SMTP_PORT or 587),
        "alert.smtp_user": config.SMTP_USER,
        "alert.smtp_password": config.SMTP_PASSWORD,
        "alert.smtp_from": config.SMTP_FROM,
        "alert.smtp_tls": bool(config.SMTP_TLS),
    }


def settings() -> dict:
    """這次寄信要用的 SMTP 參數（DB 優先、沒設過的 key 退回 .env）。"""
    flat, _ = repo.ops_config()
    present = _db_keys()
    env = _env_smtp()
    out: dict = {}
    for k in _ENV_KEYS:
        out[k] = flat.get(k) if k in present else env[k]
    # 型別收斂（DB 是 jsonb，可能有 "587" 這種字串）
    out["alert.email_enabled"] = bool(out["alert.email_enabled"])
    out["alert.smtp_tls"] = bool(out["alert.smtp_tls"])
    try:
        out["alert.smtp_port"] = int(out["alert.smtp_port"] or 587)
    except (TypeError, ValueError):
        out["alert.smtp_port"] = 587
    for k in _ENV_KEYS:
        if k not in ("alert.email_enabled", "alert.smtp_tls", "alert.smtp_port"):
            out[k] = str(out.get(k) or "").strip()
    out["source"] = "db" if any(k in present for k in _ENV_KEYS) else "env"
    return out


# ---------------------------------------------------------------- 收件人 / 可用性

def recipients() -> list[str]:
    """收件人清單（DB 參數優先；字串也接受，用逗號/分號切）。"""
    flat, _ = repo.ops_config()
    raw = flat.get("alert.email_recipients") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.replace(";", ",").split(",")]
    out: list[str] = []
    for x in raw if isinstance(raw, list) else []:
        s = str(x or "").strip()
        if s and "@" in s and s not in out:
            out.append(s)
    return out


def configured(st: dict | None = None, to: list[str] | None = None) -> bool:
    """★ 「參數已配置」＝ 有 SMTP 主機 + 至少一個收件人（**不看啟用開關**）。

    這是概覽頁那個「狀態」欄的判斷依據，也是「能不能把開關切到開」的前提。
    ★ 為什麼不看 `enabled`：參數配置與提醒開關是兩件事（小jo 2026-10-23 指定），
      使用者要能先看到「參數配置好了沒」，再決定要不要開。
    """
    s = st or settings()
    rc = to if to is not None else recipients()
    return bool(s["alert.smtp_host"]) and bool(rc)


def is_configured(s: dict | None = None) -> bool:
    """有沒有能力寄信（**開關開著** + 參數已配置）。"""
    st = s or settings()
    return bool(st["alert.email_enabled"]) and configured(st)


def status() -> dict:
    """給運維頁顯示「郵件告警到底通不通」。

    ⚠️ **不回密碼明文**，只回 `password_set`（有填 true / 沒填 false）。
    ★ `configured` 與 `enabled` 分開回：概覽頁的「狀態」看 `configured`，
      那個滑動開關看 `enabled`，而且 `configured=false` 時開關不給切到開。
    """
    s = settings()
    rc = recipients()
    ok = configured(s, rc)
    return {
        "enabled": bool(s["alert.email_enabled"]),
        "configured": ok,
        "smtp_host": s["alert.smtp_host"],
        "smtp_port": s["alert.smtp_port"],
        "smtp_user": s["alert.smtp_user"],
        "smtp_from": s["alert.smtp_from"] or s["alert.smtp_user"],
        "tls": bool(s["alert.smtp_tls"]),
        "password_set": bool(s["alert.smtp_password"]),
        "recipients": rc,
        "min_severity": (repo.ops_config()[0] or {}).get("alert.email_min_severity") or "warning",
        # `ready` 保留給舊呼叫端（自檢 / 儀表板相容）：＝ 開關開著而且參數齊
        "ready": bool(s["alert.email_enabled"]) and ok,
        "source": s["source"],
    }


# ---------------------------------------------------------------- 寄信

def _connect(s: dict | None = None):
    st = s or settings()
    host, port = st["alert.smtp_host"], int(st["alert.smtp_port"])
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=10)
    sm = smtplib.SMTP(host, port, timeout=10)
    if st["alert.smtp_tls"]:
        sm.starttls(context=ssl.create_default_context())
    return sm


def explain_error(e: Exception, s: dict | None = None) -> str:
    """把 smtplib 的例外翻成「人看得懂、而且知道下一步做什麼」的一句。

    ★ 為什麼要翻（2026-10-23）：運維端填錯參數時，原始例外長這樣
      `(535, b'5.7.8 Username and Password not accepted...')` 或
      `[SSL: WRONG_VERSION_NUMBER]` —— 使用者只會覺得「測試失敗」，不知道是
      密碼錯、還是 TLS 選錯、還是根本連不上。
    """
    st = s or settings()
    port, tls = int(st["alert.smtp_port"]), bool(st["alert.smtp_tls"])
    raw = str(e)
    low = raw.lower()
    if isinstance(e, smtplib.SMTPAuthenticationError) or "535" in raw \
            or "username and password not accepted" in low or "authentication" in low:
        return ("SMTP 認證失敗：帳號或密碼不對。"
                "Gmail / QQ 信箱要用「應用程式專用密碼」，不是登入密碼。")
    if isinstance(e, smtplib.SMTPSenderRefused) or "sender address rejected" in low:
        return ("寄件人被拒絕：`寄件人` 必須是這個 SMTP 帳號允許的地址"
                "（多數服務要求與帳號相同），或該帳號沒有這個寄件權限。")
    if isinstance(e, smtplib.SMTPRecipientsRefused) or "recipient address rejected" in low:
        return "收件人被拒絕：確認收件人信箱存在、且沒有被伺服器列入黑名單。"
    if "wrong_version_number" in low or "record layer failure" in low:
        return (f"TLS 版本不符：目前 port={port}、TLS={'開' if tls else '關'}。"
                "465 通常是「直接 SSL（TLS 可關）」，587 通常是「STARTTLS（TLS 要開）」。")
    if "certificate" in low:
        return "TLS 憑證驗證失敗：伺服器憑證不被信任（自架伺服器請用有效憑證）。"
    if isinstance(e, (smtplib.SMTPServerDisconnected, ConnectionResetError)) \
            or "disconnected" in low or "connection reset" in low:
        return (f"連線被中斷：port {port} 可能不是 SMTP 服務埠，或需要 TLS 才能通。"
                "試試 587（STARTTLS）或 465（SSL）。")
    if isinstance(e, TimeoutError) or "timed out" in low:
        return f"連線逾時：連不到 {st['alert.smtp_host']}:{port}，確認主機位址與防火牆。"
    # ⚠️ Windows 的 ConnectionRefusedError 訊息是「[WinError 10061] No connection could
    #    be made...」——裡面沒有 "refused" 這個字，只比對字串會漏掉（實測踩過）。
    if isinstance(e, ConnectionRefusedError) or "refused" in low \
            or "10061" in low or "no connection could be made" in low:
        return (f"連線被拒絕：{st['alert.smtp_host']}:{port} 沒有服務在聽，或防火牆擋掉了。"
                "確認主機與埠（587 / 465）是否正確。")
    if "name or service not known" in low or "nodename nor servname" in low \
            or "getaddrinfo" in low:
        return f"主機名稱解析失敗：找不到 {st['alert.smtp_host']}，確認 SMTP 主機拼字。"
    return f"{type(e).__name__}: {raw[:200]}"


def send_mail(subject: str, body: str, to: list[str] | None = None,
              s: dict | None = None) -> bool:
    """寄一封信。任何失敗都只回 False + log（**不拋**）。

    `s`：要用的 SMTP 參數（預設 `settings()`；測試端點會傳「還沒存進 DB 的那組」）。
    """
    st = s or settings()
    to = to if to is not None else recipients()
    if not st["alert.email_enabled"]:
        log.info("告警郵件未啟用（alert.email_enabled=false），略過：%s", subject)
        return False
    if not st["alert.smtp_host"]:
        log.info("告警郵件未配置 SMTP 主機，略過：%s", subject)
        return False
    if not to:
        log.info("告警郵件沒有收件人（alert.email_recipients 為空），略過：%s", subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = st["alert.smtp_from"] or st["alert.smtp_user"] or "no-reply@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with _connect(st) as sm:
            if st["alert.smtp_user"]:
                sm.login(st["alert.smtp_user"], st["alert.smtp_password"])
            sm.send_message(msg)
        log.info("告警郵件已寄出：%s → %s", subject, to)
        return True
    except Exception as e:  # noqa: BLE001 —— 寄信失敗不能影響業務
        log.warning("告警郵件寄送失敗（%s）：%s", subject, e)
        return False


# ---------------------------------------------------------------- 巡檢

def _fmt(v) -> str:
    try:
        return v.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(v or "—")


def digest_subject(items: list[dict]) -> str:
    worst = max((repo.severity_rank(i.get("severity")) for i in items), default=1)
    tag = {0: "提醒", 1: "警告", 2: "嚴重"}.get(worst, "警告")
    return f"[客服系統] {tag}：{len(items)} 條待處理告警"


def digest_body(items: list[dict]) -> str:
    lines = [f"客服系統目前有 {len(items)} 條未確認告警：", ""]
    for i in items:
        lines.append(f"· [{str(i.get('severity') or '').upper()}] {i.get('title')}"
                     f"（{i.get('alert_type')}，累計 {i.get('count')} 次）")
        lines.append(f"  最後出現：{_fmt(i.get('last_seen_at'))}")
        detail = i.get("detail")
        if detail:
            lines.append(f"  明細：{detail}")
        lines.append("")
    lines.append("請登入運維頁確認：/ops")
    return "\n".join(lines)


def check_mass_failure() -> dict | None:
    """大量失敗偵測。低於門檻回 None（不製造噪音）。"""
    flat, _ = repo.ops_config()
    threshold = int(flat.get("alert.mass_failure_threshold") or 20)
    window = int(flat.get("alert.failure_window_minutes") or 10)
    if threshold <= 0:
        return None
    n = repo.recent_failed_count(window)
    if n < threshold:
        return None
    aid = internal_repo.upsert_alert(
        "mass_failure", "critical",
        f"近 {window} 分鐘內有 {n} 筆外送任務失敗（門檻 {threshold}）",
        detail={"failed": n, "window_minutes": window, "threshold": threshold},
        dedupe_key=f"mass_failure:{window}",
        increment=1,
    )
    log.warning("大量失敗告警：%s 筆 / %s 分鐘（alert_id=%s）", n, window, aid)
    return {"alert_id": aid, "failed": n, "window_minutes": window, "threshold": threshold}


def sweep_once() -> dict:
    """跑一輪：偵測大量失敗 → 寄未通知告警的 digest。"""
    stats: dict = {"mass_failure": None, "notified": 0, "mailed": False}
    try:
        stats["mass_failure"] = check_mass_failure()
    except Exception as e:  # noqa: BLE001
        log.warning("大量失敗偵測失敗：%s", e)

    try:
        flat, _ = repo.ops_config()
        min_sev = str(flat.get("alert.email_min_severity") or "warning")
        items = repo.open_alerts_for_email(min_sev, limit=50)
        if items:
            ok = send_mail(digest_subject(items), digest_body(items))
            stats["mailed"] = bool(ok)
            if ok:
                # ★ 只有真的寄出去才回寫 notified_at；否則下一輪還會再試
                stats["notified"] = repo.mark_alerts_notified([i["id"] for i in items])
    except Exception as e:  # noqa: BLE001
        log.warning("告警通知回合失敗：%s", e)
    return stats
