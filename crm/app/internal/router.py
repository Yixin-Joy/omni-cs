"""CRM 內網 API 端點 —— 供 Connector 呼叫（設計案 §4.1）。

全部要求 `X-Internal-Token`（router 層統一掛閘門）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import config
from app.internal import repo
from app.internal.auth import require_internal

router = APIRouter(
    prefix="/internal/api",
    tags=["internal"],
    dependencies=[Depends(require_internal)],
)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


# ---------------------------------------------------------------- 帳號映射

@router.get("/account-map")
def account_map(since: str | None = Query(default=None)):
    """回傳 `platform_business_account` 映射；`since` 為 ISO 時間（增量，省略＝全量）。"""
    return {"ok": True, "items": repo.account_map(since)}


# ---------------------------------------------------------------- 任務認領

@router.get("/outbound-task/claim")
def claim(limit: int = Query(default=0), per_business_limit: int = Query(default=0)):
    """★ 原子認領。預設值取自 `crm_ops_config`（claim_batch / per_business_limit）。"""
    flat, _ = repo.ops_config()
    n = int(limit) if limit else int(flat.get("outbound.claim_batch") or 50)
    k = int(per_business_limit) if per_business_limit else int(
        flat.get("outbound.per_business_limit") or 10)
    tasks = repo.claim_tasks(limit=n, per_business_limit=k)
    return {"ok": True, "items": tasks, "count": len(tasks)}


class UpdateStatusIn(BaseModel):
    task_id: int
    status: str
    fail_reason: str | None = None
    is_temp_error: bool | None = None


@router.post("/outbound-task/update-status")
def update_status(payload: UpdateStatusIn):
    res = repo.update_task_status(
        payload.task_id, (payload.status or "").strip(),
        payload.fail_reason, payload.is_temp_error,
    )
    if res is None:
        return _err(404, "任務不存在")
    if res.get("error"):
        return _err(400, res["error"])
    return {"ok": True, **res}


# ---------------------------------------------------------------- AI 入站

class AiInboundIn(BaseModel):
    message_id: str
    platform: str
    platform_business_account_id: str
    end_user_sender_id: str
    scope: str = "dm"
    content: dict = {}
    post_id: str | None = None


@router.post("/ai/inbound")
def ai_inbound(payload: AiInboundIn):
    """★ 非同步：只入隊（`crm_ai_inbound_queue`），立刻 202；AI 由 CRM 背景 worker 跑。

    未註冊帳號 → 不丟棄，回 200 並在回應標記（Connector 據此上報告警）。
    """
    content = dict(payload.content or {})
    content["scope"] = payload.scope or "dm"
    if payload.post_id:
        content["post_id"] = payload.post_id
    res = repo.enqueue_ai_inbound(
        payload.message_id, payload.platform, payload.platform_business_account_id,
        payload.end_user_sender_id, content,
    )
    status = 200 if res.get("queued") or res.get("duplicate") else 200
    return JSONResponse(status_code=status, content={"ok": True, **res})


# ---------------------------------------------------------------- 運維參數

@router.get("/ops/config")
def ops_config():
    flat, nested = repo.ops_config()
    return {"ok": True, "config": nested, "flat": flat}


# ---------------------------------------------------------------- 告警

class AlertIn(BaseModel):
    alert_type: str
    severity: str = "warning"
    title: str
    detail: dict | None = None
    dedupe_key: str | None = None
    increment: int = 1


@router.post("/alert")
def alert(payload: AlertIn, request: Request):
    """Connector 上報告警 → 寫 `crm_alert`（同 dedupe_key 合併計數）。"""
    aid = repo.upsert_alert(
        payload.alert_type, payload.severity, payload.title,
        payload.detail, payload.dedupe_key, payload.increment,
    )
    return {"ok": True, "alert_id": aid}


# ---------------------------------------------------------------- 連通性自檢

@router.get("/ping")
def ping():
    """Connector 啟動時探活用。"""
    return {"ok": True, "service": config.SERVICE_NAME}
