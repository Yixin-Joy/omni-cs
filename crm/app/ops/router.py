"""運維端點（設計案 §15 / §8）—— 失敗任務、重試參數、手動重跑、告警、郵件、審計。

- 讀取：`require_ops`；變更：`require_csrf` + `rbac.can_manage_ops`。
- 發送嘗試紀錄在 Connector 庫 → 走內網 API（`/internal/api/outbound-attempt/batch`）。
- 每個變更都寫 `crm_audit_log`（ops 身分在審計頁只看得到這幾類）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.auth import deps, rbac
from app.internal import connector_client
from app.ops import alerting
from app.ops import repo

router = APIRouter(prefix="/console/ops", tags=["ops"])

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
    flat, nested = repo.ops_config()
    items = [{"key": k, "value": v, "type": repo.config_type_of(k),
              "is_default": v == repo.schema.OPS_CONFIG_DEFAULTS.get(k)}
             for k, v in sorted(flat.items())]
    return {"ok": True, "config": nested, "flat": flat, "items": items,
            "editable": sorted(repo.CONFIG_KEYS)}


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

@router.get("/mail")
def mail_status(sess: dict = Depends(deps.require_ops)):
    return {"ok": True, **alerting.status()}


class MailTestIn(BaseModel):
    to: list[str] | None = None


@router.post("/mail/test")
def mail_test(payload: MailTestIn, sess: dict = Depends(deps.require_csrf)):
    """寄一封測試信驗證 SMTP 設定（未配置時回 `ok=False` 講清楚缺什麼）。"""
    bad = _ops_guard(sess)
    if bad:
        return bad
    to = [str(x).strip() for x in (payload.to or []) if str(x).strip()] or None
    if not alerting.is_configured():
        st = alerting.status()
        missing = []
        if not st["enabled"]:
            missing.append("ALERT_EMAIL_ENABLED")
        if not st["smtp_host"]:
            missing.append("SMTP_HOST")
        if not st["recipients"]:
            missing.append("alert.email_recipients")
        return _err(400, "郵件告警尚未配置，缺少：" + "、".join(missing))
    ok = alerting.send_mail("[客服系統] 測試信", "這是一封測試信，收到代表 SMTP 設定正確。", to=to)
    audit.log_standalone(sess, "alert_notify", payload={"test": True, "to": to, "sent": bool(ok)})
    if not ok:
        return _err(502, "寄送失敗，請看服務日誌")
    return {"ok": True, "to": to or alerting.recipients()}


# ================================================================ 審計

@router.get("/audit")
def audit_logs(limit: int = Query(default=100), offset: int = Query(default=0),
               business_id: int = Query(default=0), all: bool = Query(default=False),
               sess: dict = Depends(deps.require_ops)):
    """★ ops 只看運維相關類型；**管理員帳號**可 `all=true` 看全部。

    ⚠️ 這裡不能用 `rbac.is_admin(sess)` —— 能進到這個端點就代表當前生效身分是
      `ops`（`require_ops`），而 `roles_of()` 一次只回一個生效身分，`is_admin` 必為
      False。所以要看的是**登入快照裡有沒有 admin**（`roles_all`）：
      管理員切成運維身分時，仍然是同一個帳號，該看得到全部。
    """
    types = None if (all and rbac.ROLE_ADMIN in rbac.roles_all(sess)) else list(repo.OPS_AUDIT_TYPES)
    return {"ok": True, **repo.list_audit(limit=int(limit), offset=int(offset),
                                          types=types, business_id=int(business_id or 0)),
            "scoped": types is not None, "types": types}
