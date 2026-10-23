"""運維端點（設計案 §15 / §8）—— 失敗任務、重試參數、手動重跑、告警、郵件、審計。

- 讀取：`require_ops`；變更：`require_csrf` + `rbac.can_manage_ops`。
- 發送嘗試紀錄在 Connector 庫 → 走內網 API（`/internal/api/outbound-attempt/batch`）。
- 每個變更都寫 `crm_audit_log`（ops 身分在審計頁只看得到這幾類）。
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from email.message import EmailMessage

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.auth import deps, rbac
from app.internal import connector_client
from app.internal import repo as internal_repo
from app.ops import alerting
from app.ops import repo

router = APIRouter(prefix="/console/ops", tags=["ops"])
log = logging.getLogger("crm")

PLATFORMS = ("wechat", "whatsapp", "ig", "fb")


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _ops_guard(sess: dict):
    if not rbac.can_manage_ops(sess):
        return _err(403, "需要運維權限")


# ================================================================ 概覽

@router.get("/dashboard")
def dashboard(business_id: int = Query(default=0), sess: dict = Depends(deps.require_ops)):
    data = repo.dashboard(int(business_id or 0))
    data["mail"] = alerting.status()
    return {"ok": True, **data}


# ================================================================ 重試參數

@router.get("/config")
def get_config(sess: dict = Depends(deps.require_ops)):
    """運維參數清單。

    ★ 敏感欄（SMTP 密碼）**不回明文**，只回 `"••••••••"` + `password_set`；
      而且它不在 `editable` 裡 —— 要改請走「郵件設定」彈窗（先測試才能存）。
    """
    flat, nested = repo.ops_config()
    safe = dict(flat)
    for k in alerting.SENSITIVE_KEYS:
        if k in safe:
            safe[k] = alerting.MASK if str(flat.get(k) or "") else ""
    if "alert" in (nested or {}):
        nested = dict(nested)
        nested["alert"] = {k: (alerting.MASK if f"alert.{k}" in alerting.SENSITIVE_KEYS
                               and flat.get(f"alert.{k}") else v)
                           for k, v in nested["alert"].items()}
    items = [{"key": k, "value": safe.get(k), "type": repo.config_type_of(k),
              "is_default": flat.get(k) == repo.schema.OPS_CONFIG_DEFAULTS.get(k),
              "masked": k in alerting.SENSITIVE_KEYS,
              "locked": k in repo.MAIL_SMTP_KEYS}
             for k in sorted(flat)]
    return {"ok": True, "config": nested, "flat": safe, "items": items,
            "editable": sorted(repo.CONFIG_KEYS),
            "mail_managed": sorted(repo.MAIL_SMTP_KEYS)}


class ConfigIn(BaseModel):
    key: str
    value: object = None


@router.post("/config")
def set_config(payload: ConfigIn, sess: dict = Depends(deps.require_csrf)):
    bad = _ops_guard(sess)
    if bad:
        return bad
    key = (payload.key or "").strip()
    if key not in repo.CONFIG_KEYS:
        return _err(400, f"不認識的運維參數：{key}")
    old_flat, _ = repo.ops_config()
    ok, err = repo.set_config(key, payload.value)
    if not ok:
        return _err(400, err or "儲存失敗")
    flat, _ = repo.ops_config()
    audit.log_standalone(sess, "ops_config_change",
                         payload={"key": key, "from": old_flat.get(key), "to": flat.get(key)})
    return {"ok": True, "key": key, "value": flat.get(key)}


# ================================================================ 失敗任務

@router.get("/tasks")
def tasks(status: str = Query(default=""), business_id: int = Query(default=0),
          platform: str = Query(default=""), keyword: str = Query(default=""),
          limit: int = Query(default=50), offset: int = Query(default=0),
          sess: dict = Depends(deps.require_ops)):
    """預設只回 `failed` + `uncertain`（真正需要人處理的）；`status=all` 回全部。"""
    if status == "all":
        statuses = list(repo.TASK_STATUSES)
    elif status:
        sts = [s.strip() for s in status.split(",") if s.strip() in repo.TASK_STATUSES]
        if not sts:
            return _err(400, f"不支援的任務狀態：{status}")
        statuses = sts
    else:
        statuses = list(repo.DEFAULT_FAIL_STATUSES)
    return {"ok": True, **repo.list_tasks(statuses=statuses, business_id=int(business_id or 0),
                                         platform=(platform if platform in PLATFORMS else ""),
                                         keyword=keyword.strip(), limit=int(limit),
                                         offset=int(offset))}


@router.get("/task")
def task_detail(id: int = Query(...), sess: dict = Depends(deps.require_ops)):
    row = repo.get_task(int(id))
    if not row:
        return _err(404, "找不到任務")
    attempts = connector_client.fetch_attempts(int(id))
    return {"ok": True, "item": row, "attempts": attempts,
            "attempts_reachable": attempts is not None}


class AttemptsIn(BaseModel):
    task_ids: list[int] = []


@router.post("/attempts")
def attempts(payload: AttemptsIn, sess: dict = Depends(deps.require_ops)):
    ids = [int(i) for i in (payload.task_ids or [])][:200]
    if not ids:
        return {"ok": True, "items": {}}
    got = connector_client.fetch_attempts_batch(ids)
    if got is None:
        return _err(502, "連不上 Connector，暫時取不到發送紀錄")
    return {"ok": True, "items": got}


class RetryIn(BaseModel):
    id: int = 0
    ids: list[int] = []


@router.post("/retry")
def retry(payload: RetryIn, sess: dict = Depends(deps.require_csrf)):
    """★ 手動重跑：把 `failed` / `uncertain` 打回 `pending`，Connector 下一輪就會認領。"""
    bad = _ops_guard(sess)
    if bad:
        return bad
    ids = list(payload.ids or [])
    if payload.id:
        ids = [int(payload.id)] + ids
    ids = [int(i) for i in ids]
    if not ids:
        return _err(400, "請指定要重跑的任務")
    res = repo.retry_tasks(ids)
    audit.log_standalone(sess, "task_retry",
                         payload={"requested": ids, "retried": res["retried"],
                                  "skipped": res["skipped"]})
    return {"ok": True, **res}


# ================================================================ 告警

@router.get("/alerts")
def alerts(only_open: bool = Query(default=True), severity: str = Query(default=""),
           limit: int = Query(default=100), offset: int = Query(default=0),
           sess: dict = Depends(deps.require_ops)):
    return {"ok": True, **repo.list_alerts(only_open=only_open, severity=severity.strip(),
                                           limit=int(limit), offset=int(offset))}


class AckIn(BaseModel):
    id: int = 0
    all: bool = False


@router.post("/alert/ack")
def ack(payload: AckIn, sess: dict = Depends(deps.require_csrf)):
    bad = _ops_guard(sess)
    if bad:
        return bad
    if payload.all:
        n = repo.ack_all_alerts(sess.get("staff_id"))
        audit.log_standalone(sess, "alert_ack", payload={"all": True, "count": n})
        return {"ok": True, "acked": n}
    if not payload.id:
        return _err(400, "請指定告警")
    ok = repo.ack_alert(int(payload.id), sess.get("staff_id"))
    if not ok:
        return _err(404, "找不到告警，或已經確認過了")
    audit.log_standalone(sess, "alert_ack", payload={"id": int(payload.id), "count": 1})
    return {"ok": True, "acked": 1}


# ================================================================ 郵件告警
#
# ★★ 「先測試、成功才准保存」（2026-10-23 小jo 要求）——**伺服器端強制**，不是前端 disable：
#   測試成功時發一張短效 ticket（記憶體，10 分鐘），保存時必須帶上它，
#   而且參數的 hash 要與測試當時**完全一致**。
#   否則「測 A 存 B」（測好用的主機、存另一個）會讓這條規則形同虛設。
_MAIL_TICKETS: dict[str, dict] = {}
_MAIL_TICKET_TTL = 600.0
_MAIL_TICKET_KEEP = 50

MAIL_KEYS = tuple(alerting.PARAM_KEYS)          # 8 個參數（**不含**啟用開關）
MAIL_SWITCH_KEY = "alert.email_enabled"          # 開關走獨立端點（見 /alert/email/toggle）


class MailSettingsIn(BaseModel):
    """郵件**參數**表單（2026-10-23 起不含啟用開關 —— 開關在概覽頁那一欄）。"""
    host: str = ""
    port: int = 587
    user: str = ""
    # ★ 留空或傳遮蔽值＝沿用已存的密碼（不然每改一次主機就要重打一次密碼）
    password: str = ""
    sender: str = ""
    tls: bool = True
    recipients: list[str] = []
    min_severity: str = "warning"


def _norm_recipients(v) -> list[str]:
    raw = v if isinstance(v, list) else str(v or "").replace(";", ",").split(",")
    out: list[str] = []
    for x in raw:
        s = str(x or "").strip()
        if s and "@" in s and s not in out:
            out.append(s)
    return out


def _effective_password(given: str) -> str:
    """表單傳空 / 遮蔽值 → 用目前實際生效的那一組（DB 或 .env）。"""
    p = str(given or "").strip()
    if p and p != alerting.MASK and set(p) != {"•"}:
        return p
    return alerting.settings()["alert.smtp_password"]


def _mail_settings(payload: MailSettingsIn) -> dict:
    """把表單組成 `alerting.settings()` 那種格式（含 `alert.` 前綴的完整 key）。"""
    sev = (payload.min_severity or "warning").strip().lower()
    if sev not in repo.SEVERITIES:
        sev = "warning"
    return {
        "alert.smtp_host": (payload.host or "").strip(),
        "alert.smtp_port": int(payload.port or 587),
        "alert.smtp_user": (payload.user or "").strip(),
        "alert.smtp_password": _effective_password(payload.password),
        "alert.smtp_from": (payload.sender or "").strip(),
        "alert.smtp_tls": bool(payload.tls),
        "alert.email_recipients": _norm_recipients(payload.recipients),
        "alert.email_min_severity": sev,
    }


def _fingerprint(st: dict) -> str:
    """參數指紋 —— ticket 綁定它，防止「測 A 存 B」。"""
    import hashlib
    import json as _json
    raw = _json.dumps({k: st.get(k) for k in MAIL_KEYS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gc_tickets() -> None:
    import time as _t
    now = _t.time()
    for k in [k for k, v in _MAIL_TICKETS.items() if v["exp"] < now]:
        _MAIL_TICKETS.pop(k, None)
    if len(_MAIL_TICKETS) > _MAIL_TICKET_KEEP:
        for k in sorted(_MAIL_TICKETS, key=lambda x: _MAIL_TICKETS[x]["exp"])[:-_MAIL_TICKET_KEEP]:
            _MAIL_TICKETS.pop(k, None)


@router.get("/alert/email")
def mail_settings(sess: dict = Depends(deps.require_ops)):
    """郵件告警表單初值。**密碼不回明文**，只回 `password_set`。

    `source`：'db' = 參數來自運維端存的值；'env' = 還沒存過，目前用 `.env`。
    """
    st = alerting.status()
    return {"ok": True, "settings": st, "editable": list(MAIL_KEYS),
            "password_mask": alerting.MASK}


@router.post("/alert/email/test")
def mail_settings_test(payload: MailSettingsIn, sess: dict = Depends(deps.require_csrf)):
    """用**表單上的參數**（還沒存進 DB）連線＋登入＋寄一封測試信。

    成功 → 回 `{ok, ticket, to, ...}`；失敗 → 400/502 + **人看得懂的原因**
    （認證失敗 / TLS 不匹配 / 連不上…），前端據此顯示、且不給保存。
    """
    bad = _ops_guard(sess)
    if bad:
        return bad
    st = _mail_settings(payload)

    # ---- 先做能講清楚的靜態檢查（不用連線就知道錯的）
    missing: list[str] = []
    if not st["alert.smtp_host"]:
        missing.append("SMTP 主機")
    if not st["alert.email_recipients"]:
        missing.append("收件人")
    if not (st["alert.smtp_from"] or st["alert.smtp_user"]):
        missing.append("寄件人（或 SMTP 帳號）")
    if not st["alert.smtp_user"] and st["alert.smtp_password"]:
        missing.append("SMTP 帳號（填了密碼卻沒有帳號）")
    if missing:
        return _err(400, "請先填寫：" + "、".join(missing))
    port = int(st["alert.smtp_port"])
    if not (1 <= port <= 65535):
        return _err(400, f"連接埠不合法：{port}（應在 1–65535）")

    to = st["alert.email_recipients"]
    subject = "[客服系統] 郵件告警設定測試"
    body = ("這是一封測試信，收到代表 SMTP 參數正確。\n\n"
            f"主機：{st['alert.smtp_host']}:{port}\n"
            f"TLS：{'開' if st['alert.smtp_tls'] else '關'}\n"
            f"帳號：{st['alert.smtp_user'] or '（未設定，匿名寄送）'}\n"
            f"寄件人：{st['alert.smtp_from'] or st['alert.smtp_user']}\n"
            f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # ★ 這裡**不看** enabled（正在設定的人可能還沒勾啟用），直接試寄
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = st["alert.smtp_from"] or st["alert.smtp_user"]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    try:
        with alerting._connect(st) as sm:
            if st["alert.smtp_user"]:
                sm.login(st["alert.smtp_user"], st["alert.smtp_password"])
            sm.send_message(msg)
    except Exception as e:  # noqa: BLE001 —— 全部翻成人看得懂的原因
        reason = alerting.explain_error(e, st)
        log.warning("郵件設定測試失敗（%s:%s）: %s", st["alert.smtp_host"], port, e)
        audit.log_standalone(sess, "alert_notify", payload={
            "test": "smtp", "ok": False, "host": st["alert.smtp_host"],
            "port": port, "error": reason[:200]})
        # ★ 文案由後端統一回（前端直接顯示，不再自己加前綴，否則會出現
        #   「測試失敗，請重試：測試失敗：…」這種重複兩次前綴的句子 —— 實測踩過）。
        return _err(400, "測試失敗，請重試：" + reason)

    # ---- 成功 → 發 ticket（保存時要用）
    _gc_tickets()
    ticket = uuid.uuid4().hex
    _MAIL_TICKETS[ticket] = {
        "fp": _fingerprint(st),
        "exp": time.time() + _MAIL_TICKET_TTL,
        "staff_id": sess.get("staff_id"),
    }
    audit.log_standalone(sess, "alert_notify", payload={
        "test": "smtp", "ok": True, "host": st["alert.smtp_host"],
        "port": port, "to": to})
    return {"ok": True, "ticket": ticket, "to": to,
            "expires_in": int(_MAIL_TICKET_TTL),
            "message": f"測試信已寄給 {', '.join(to)}"}


class MailSaveIn(MailSettingsIn):
    ticket: str = ""


@router.post("/alert/email/save")
def mail_settings_save(payload: MailSaveIn, sess: dict = Depends(deps.require_csrf)):
    """保存郵件**參數** —— **必須先測試成功**（帶測試時拿到的 ticket）。

    ★ 只寫 8 個參數，**不動啟用開關**（那是概覽頁的滑動開關，走 `/alert/email/toggle`）。
      所以「保存參數」不會把本來開著的提醒關掉，也不會偷偷打開。

    三道檢查（任何一道不過都不會寫 DB）：
      ① ticket 存在且未過期；
      ② ticket 是這位登入者拿的（不能被別人撿去用）；
      ③ **參數與測試當時完全一致**（改了任何一個欄位 → 要重新測試）。
    """
    bad = _ops_guard(sess)
    if bad:
        return bad
    st = _mail_settings(payload)

    _gc_tickets()
    tk = _MAIL_TICKETS.get((payload.ticket or "").strip())
    if not tk:
        return _err(400, "請先按「測試」，測試成功後才能儲存（測試連結已過期或不存在）")
    if tk.get("staff_id") != sess.get("staff_id"):
        return _err(403, "這張測試憑證不是你的，請自己按一次測試")
    if tk["fp"] != _fingerprint(st):
        # 差異在哪裡要講出來，否則使用者只會覺得「明明測試過了」
        cur = alerting.settings()
        diff = [k for k in MAIL_KEYS if str(st.get(k)) != str(cur.get(k))]
        return _err(400, "參數在測試之後被改過（" + "、".join(
            k.split(".", 1)[-1] for k in diff[:4]) + "），請重新測試再儲存")

    old = alerting.settings()
    internal_repo.set_ops_config_many({k: st[k] for k in MAIL_KEYS})
    _MAIL_TICKETS.pop((payload.ticket or "").strip(), None)   # 一張 ticket 只能用一次

    # ★ 審計不落密碼，只記「有沒有換」
    audit.log_standalone(sess, "ops_config_change", payload={
        "scope": "alert_email",
        "from": {k: ("***" if k in alerting.SENSITIVE_KEYS else old.get(k)) for k in MAIL_KEYS},
        "to": {k: ("***" if k in alerting.SENSITIVE_KEYS else st.get(k)) for k in MAIL_KEYS},
        "password_changed": old["alert.smtp_password"] != st["alert.smtp_password"],
    })
    return {"ok": True, "settings": alerting.status()}


class MailToggleIn(BaseModel):
    enabled: bool


@router.post("/alert/email/toggle")
def mail_toggle(payload: MailToggleIn, sess: dict = Depends(deps.require_csrf)):
    """★ 郵件提醒的**啟用開關**（2026-10-23 與參數配置分開）。

    ★★ 鐵則：**參數沒配置好就不准開**。使用者按下去如果參數是空的，
      要回一句「請先配置參數」，而不是默默把開關打開然後永遠寄不出信
      （那種「顯示已開啟、其實一封都沒寄」是最難排查的狀態）。

    ⚠️ 這個端點不要求測試 ticket —— 開關本身不改任何連線參數，
       而且「能不能開」已經由 `configured()` 保證了（參數是測試過才存得進來的）。
    """
    bad = _ops_guard(sess)
    if bad:
        return bad
    st = alerting.status()
    if payload.enabled and not st["configured"]:
        missing = []
        if not st["smtp_host"]:
            missing.append("SMTP 主機")
        if not st["recipients"]:
            missing.append("收件人")
        return _err(400, "請先配置郵件參數"
                         + (f"（還缺：{'、'.join(missing)}）" if missing else "")
                         + " —— 按右上角「參數配置」完成測試後就能開啟")
    internal_repo.set_ops_config(MAIL_SWITCH_KEY, bool(payload.enabled))
    audit.log_standalone(sess, "ops_config_change", payload={
        "scope": "alert_email_switch",
        "from": bool(st["enabled"]), "to": bool(payload.enabled)})
    return {"ok": True, "settings": alerting.status()}


@router.get("/mail")
def mail_status(sess: dict = Depends(deps.require_ops)):
    return {"ok": True, **alerting.status()}


class MailTestIn(BaseModel):
    to: list[str] | None = None


@router.post("/mail/test")
def mail_test(payload: MailTestIn, sess: dict = Depends(deps.require_csrf)):
    """寄一封測試信驗證**目前生效**的 SMTP 設定（未配置時回 `ok=False` 講清楚缺什麼）。

    ⚠️ 這支只測「已保存/已生效」的參數；要測「還沒保存的表單值」請用
       `POST /alert/email/test`。
    """
    bad = _ops_guard(sess)
    if bad:
        return bad
    to = [str(x).strip() for x in (payload.to or []) if str(x).strip()] or None
    if not alerting.is_configured():
        st = alerting.status()
        missing = []
        if not st["enabled"]:
            missing.append("啟用開關")
        if not st["smtp_host"]:
            missing.append("SMTP 主機")
        if not st["recipients"]:
            missing.append("收件人")
        return _err(400, "郵件告警尚未配置，缺少：" + "、".join(missing))
    ok = alerting.send_mail("[客服系統] 測試信", "這是一封測試信，收到代表 SMTP 設定正確。", to=to)
    audit.log_standalone(sess, "alert_notify", payload={"test": True, "to": to, "sent": bool(ok)})
    if not ok:
        return _err(502, "寄送失敗，請看服務日誌")
    return {"ok": True, "to": to or alerting.recipients()}


# ================================================================ 審計

# ★ 類型分組（2026-10-20 重新設計）：運維排查時問的問題其實只有幾種，
#   與其給 27 個原始類型，不如按「你會問什麼」分組。
#   每組都附一個具體類型清單，前端點組就會帶這些 types。
AUDIT_GROUPS = (
    {"key": "msg", "label": "訊息與發送",
     "hint": "回覆送出、歷史補拉、留言處理、任務重跑",
     "types": ["reply_send", "sync_history", "message_handle", "task_retry"]},
    {"key": "cfg", "label": "配置變更",
     "hint": "AI 開關、運維參數、貼文配置、帳號綁定、商家設定、RAG 綁定",
     "types": ["ai_switch_change", "ops_config_change", "post_config_change",
               "post_ai_config_change", "business_update", "business_create",
               "platform_account_bind", "platform_account_unbind",
               "binding_change", "kb_collection_toggle"]},
    {"key": "alert", "label": "告警處置",
     "hint": "告警確認、告警通知",
     "types": ["alert_ack", "alert_notify"]},
    {"key": "kb", "label": "內容維護",
     "hint": "FAQ 與業務表格的新增/修改/刪除、知識庫文件",
     "types": ["faq_create", "faq_update", "faq_delete", "faq_import",
               "business_table_create", "business_table_update", "business_table_delete",
               "kb_document_upload", "kb_document_delete", "kb_document_reparse",
               "kb_collection_create", "kb_collection_update", "kb_collection_delete"]},
    {"key": "sec", "label": "身分與安全",
     "hint": "登入、登入失敗、登出、強制登出、員工與權限異動",
     "types": ["login", "login_failed", "logout", "force_logout",
               "staff_create", "staff_update", "staff_disable", "staff_role_change",
               "staff_password_reset", "staff_business_permission_grant",
               "staff_business_permission_revoke"]},
)

# 給前端的「人話標籤」—— 27 個原始類型對人沒有意義
AUDIT_TYPE_LABELS = {
    "reply_send": "送出回覆", "sync_history": "補拉歷史", "message_handle": "處理訊息",
    "task_retry": "重跑任務",
    "ai_switch_change": "AI 開關", "ops_config_change": "運維參數",
    "post_config_change": "貼文配置", "post_ai_config_change": "貼文 AI 配置",
    "business_update": "更新商家", "business_create": "新增商家",
    "business_disable": "停用商家",
    "platform_account_bind": "綁定平台帳號", "platform_account_unbind": "解綁平台帳號",
    "binding_change": "綁定變更", "kb_collection_toggle": "知識庫開關",
    "alert_ack": "確認告警", "alert_notify": "發送告警",
    "faq_create": "新增 FAQ", "faq_update": "修改 FAQ", "faq_delete": "刪除 FAQ",
    "faq_import": "匯入 FAQ",
    "business_table_create": "新增表格", "business_table_update": "修改表格",
    "business_table_delete": "刪除表格",
    "kb_collection_create": "新增知識庫", "kb_collection_update": "修改知識庫",
    "kb_collection_delete": "刪除知識庫",
    "kb_document_upload": "上傳文件", "kb_document_delete": "刪除文件",
    "kb_document_reparse": "重新解析文件",
    "kb_asset_delete": "刪除圖片", "kb_asset_caption_edit": "改圖片說明",
    "login": "登入", "login_failed": "登入失敗", "logout": "登出",
    "force_logout": "強制登出",
    "staff_create": "新增員工", "staff_update": "修改員工",
    "staff_disable": "停用員工", "staff_role_change": "變更員工角色",
    "staff_password_reset": "重設密碼",
    "staff_business_permission_grant": "授予商家權限",
    "staff_business_permission_revoke": "回收商家權限",
}


@router.get("/audit")
def audit_logs(limit: int = Query(default=50), offset: int = Query(default=0),
               business_id: int = Query(default=0), all: bool = Query(default=False),
               types: str = Query(default=""), q: str = Query(default=""),
               time_from: str = Query(default=""), time_to: str = Query(default=""),
               only_failed: bool = Query(default=False),
               sess: dict = Depends(deps.require_ops)):
    """審計列表（篩選 + 分頁）。

    ★ 重新設計（2026-10-20 小jo）：
      · `types`：逗號分隔的多選類型；**空 = 預設組**（訊息/配置/告警/內容/安全，
        不含已下線的 conversation_read / role_switch）
      · `q`：模糊搜尋（操作人／身分／失敗原因／payload 全文／商家 id）
      · `time_from` / `time_to`：ISO 時間（含頭不含尾）
      · `only_failed`：只看失敗
      · 回應帶 `total` / `has_more`，前端才能做「一屏 20 條 + 頁碼」

    ⚠️ 這裡不能用 `rbac.is_admin(sess)` —— 能進到這個端點就代表當前生效身分是
      `ops`（`require_ops`），而 `roles_of()` 一次只回一個生效身分，`is_admin` 必為
      False。所以要看的是**登入快照裡有沒有 admin**（`roles_all`）：
      管理員切成運維身分時，仍然是同一個帳號，該看得到全部。
    """
    is_admin = rbac.ROLE_ADMIN in rbac.roles_all(sess)
    want_all = bool(all) and is_admin

    picked = [t.strip() for t in (types or "").split(",") if t.strip()]
    if picked and not want_all:
        # ★ 非管理員：只能在自己看得到的類型裡挑，不能靠傳 types 越權看別人的操作
        allowed = {t for g in AUDIT_GROUPS for t in g["types"]}
        picked = [t for t in picked if t in allowed]
    # ★ 切「顯示全部類型」時**必須忽略 types**：前端永遠會帶上 5 組的 types，
    #   若這裡再要求「picked 為空才 all」，那顆按鈕就永遠只改文字、資料不變
    #   （2026-10-20 真瀏覽器自檢抓到：643 → 643）。
    types_arg = None if want_all else (picked or None)

    # 不傳 types 且不是「全部」→ 用預設組（排除已下線的噪音類型）
    if types_arg is None and not want_all:
        types_arg = sorted({t for g in AUDIT_GROUPS for t in g["types"]})

    return {"ok": True,
            **repo.list_audit(limit=int(limit), offset=int(offset),
                              types=types_arg, business_id=int(business_id or 0),
                              time_from=(time_from or None), time_to=(time_to or None),
                              q=(q or ""), only_failed=bool(only_failed)),
            "all": want_all, "groups": AUDIT_GROUPS,
            "type_labels": AUDIT_TYPE_LABELS}


@router.get("/audit/facets")
def audit_facets(business_id: int = Query(default=0),
                 time_from: str = Query(default=""), time_to: str = Query(default=""),
                 all: bool = Query(default=False),
                 sess: dict = Depends(deps.require_ops)):
    """各組／各類型在**當前時間範圍**內有幾筆 —— 前端 chip 上顯示數字用。

    ★ 為什麼要獨立一支：使用者要先知道「哪一組有東西」才會去點它。
      跟著列表一起回也可以，但列表帶分頁、facets 要全範圍統計，分開更清楚也更好快取。
    """
    is_admin = rbac.ROLE_ADMIN in rbac.roles_all(sess)
    # ★ 前端切「顯示全部類型」時要一起算（否則 chip 的數字跟列表對不上）
    want_all = bool(all) and is_admin
    allowed = sorted({t for g in AUDIT_GROUPS for t in g["types"]}) if not want_all \
        else None
    counts = repo.audit_type_counts(business_id=int(business_id or 0),
                                    time_from=(time_from or None),
                                    time_to=(time_to or None),
                                    types=allowed)
    groups = []
    for g in AUDIT_GROUPS:
        groups.append({"key": g["key"], "label": g["label"], "hint": g["hint"],
                       "types": g["types"],
                       "count": sum(counts.get(t, 0) for t in g["types"])})
    # ★ total 只算「這 5 組」的合計，不要把已下線的 conversation_read / role_switch
    #   也算進去 —— 否則畫面上「合計 1195」跟各組加起來 880 對不上，看起來像 bug。
    default_total = sum(g["count"] for g in groups)
    return {"ok": True, "groups": groups, "counts": counts,
            "total": default_total,
            # 全部類型的真實總數（管理員切「顯示全部」時對照用）
            "total_all": sum(counts.values())}
