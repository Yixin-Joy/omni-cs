"""Connector → CRM 內網 HTTP 客戶端（設計案 §4.1）。

★ 測試可注入 transport：`set_transport(httpx.ASGITransport(app=crm_app))`
   —— 讓 worker 直接打進 CRM 的 ASGI app，不必真的起兩個服務。
"""
from __future__ import annotations

import logging

import httpx

from app.core import config

log = logging.getLogger("connector")

_transport: httpx.AsyncBaseTransport | None = None


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = t


def _client(timeout: float = 10.0) -> httpx.AsyncClient:
    kw = {}
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.AsyncClient(
        base_url=config.CRM_INTERNAL_BASE,
        timeout=timeout,
        headers={"X-Internal-Token": config.INTERNAL_API_TOKEN or ""},
        **kw,
    )


# ---------------------------------------------------------------- 任務

async def claim_tasks(limit: int = 0, per_business_limit: int = 0) -> list[dict]:
    params = {}
    if limit:
        params["limit"] = int(limit)
    if per_business_limit:
        params["per_business_limit"] = int(per_business_limit)
    async with _client() as c:
        r = await c.get("/internal/api/outbound-task/claim", params=params)
        r.raise_for_status()
        return (r.json() or {}).get("items") or []


async def update_task_status(task_id: int, status: str, fail_reason: str | None = None,
                             is_temp_error: bool | None = None) -> dict:
    body = {"task_id": int(task_id), "status": status, "fail_reason": fail_reason,
            "is_temp_error": is_temp_error}
    async with _client() as c:
        r = await c.post("/internal/api/outbound-task/update-status", json=body)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- 設定 / 映射

async def fetch_account_map(since: str | None = None) -> list[dict]:
    params = {"since": since} if since else {}
    async with _client() as c:
        r = await c.get("/internal/api/account-map", params=params)
        r.raise_for_status()
        return (r.json() or {}).get("items") or []


async def fetch_ops_config() -> dict:
    async with _client() as c:
        r = await c.get("/internal/api/ops/config")
        r.raise_for_status()
        return (r.json() or {}).get("flat") or {}


async def notify_ai_inbound(payload: dict) -> dict:
    async with _client() as c:
        r = await c.post("/internal/api/ai/inbound", json=payload)
        r.raise_for_status()
        return r.json() or {}


async def report_alert(alert_type: str, severity: str, title: str,
                       detail: dict | None = None, dedupe_key: str | None = None,
                       increment: int = 1) -> bool:
    """上報告警；**best-effort** —— 失敗只記 log，不影響主流程。"""
    body = {"alert_type": alert_type, "severity": severity, "title": title,
            "detail": detail or {}, "dedupe_key": dedupe_key, "increment": increment}
    try:
        async with _client(timeout=5.0) as c:
            r = await c.post("/internal/api/alert", json=body)
            return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("上報告警失敗 (%s): %s", alert_type, e)
        return False
