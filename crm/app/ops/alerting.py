"""告警郵件與「大量失敗」巡檢 —— 設計案 §15.3。

兩個獨立職責：

1. **偵測**：近 `alert.failure_window_minutes` 分鐘內 `failed` 任務數 ≥
   `alert.mass_failure_threshold` → `upsert_alert('mass_failure')`（靠 dedupe_key 累計）。
2. **通知**：把 `acknowledged=false AND notified_at IS NULL` 且嚴重度達
   `alert.email_min_severity` 的告警彙整成**一封** digest 寄給 `alert.email_recipients`，
   成功後回寫 `notified_at`。

★ 郵件是通知、不是保證 —— **未配置 = no-op**（只記 log），寄信失敗只回 False，
  絕不往外拋；不然一封寄不出去的信會把整個 sweep 迴圈打斷。
★ 收件人每次讀 DB（`alert.email_recipients`），改參數**不需重啟**。
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.core import config
from app.internal import repo as internal_repo
from app.ops import repo

log = logging.getLogger("crm")


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


def is_configured() -> bool:
    """有沒有能力寄信（開關 + SMTP 主機 + 至少一個收件人）。"""
    return bool(config.ALERT_EMAIL_ENABLED and config.SMTP_HOST and recipients())


def status() -> dict:
    """給運維頁顯示「郵件告警到底通不通」。"""
    return {
        "enabled": bool(config.ALERT_EMAIL_ENABLED),
        "smtp_host": config.SMTP_HOST,
        "smtp_port": config.SMTP_PORT,
        "smtp_from": config.SMTP_FROM or config.SMTP_USER,
        "tls": config.SMTP_TLS,
        "recipients": recipients(),
        "ready": is_configured(),
    }


# ---------------------------------------------------------------- 寄信

def _connect():
    if int(config.SMTP_PORT) == 465:
        return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=10)
    s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10)
    if config.SMTP_TLS:
        s.starttls()
    return s


def send_mail(subject: str, body: str, to: list[str] | None = None) -> bool:
    """寄一封信。任何失敗都只回 False + log（**不拋**）。"""
    to = to if to is not None else recipients()
    if not config.ALERT_EMAIL_ENABLED:
        log.info("告警郵件未啟用（ALERT_EMAIL_ENABLED=false），略過：%s", subject)
        return False
    if not config.SMTP_HOST:
        log.info("告警郵件未配置 SMTP_HOST，略過：%s", subject)
        return False
    if not to:
        log.info("告警郵件沒有收件人（alert.email_recipients 為空），略過：%s", subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM or config.SMTP_USER or "no-reply@localhost"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with _connect() as s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
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
