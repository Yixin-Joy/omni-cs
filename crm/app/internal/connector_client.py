"""CRM → Connector 的內網呼叫（只讀發送紀錄，供逾時清理判斷）。

逾時清理需要知道「這筆任務到底發出去了沒」，而發送紀錄在 Connector 庫 ——
所以走內網 API（`GET /internal/api/outbound-attempt`），不碰 fdw。

★ 測試可注入 transport：`set_transport(httpx.ASGITransport(app=connector_app))`。
"""
from __future__ import annotations

import logging

import httpx

from app.core import config

log = logging.getLogger("crm")

_transport: httpx.BaseTransport | None = None


def set_transport(t: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = t


def _client() -> httpx.Client:
    kw = {}
    if _transport is not None:
        kw["transport"] = _transport
    return httpx.Client(
        base_url=config.CONNECTOR_INTERNAL_BASE,
        timeout=5.0,
        headers={"X-Internal-Token": config.INTERNAL_API_TOKEN or ""},
        **kw,
    )


def fetch_attempts(task_id: int) -> list[dict] | None:
    """回傳發送嘗試清單；**連不上 / 非 200 回 None**（呼叫端據此保守處理，不可當成「沒發過」）。"""
    try:
        with _client() as c:
            r = c.get("/internal/api/outbound-attempt", params={"crm_task_id": int(task_id)})
            if r.status_code != 200:
                log.warning("取發送紀錄失敗 task=%s status=%s", task_id, r.status_code)
                return None
            return (r.json() or {}).get("items") or []
    except Exception as e:  # noqa: BLE001 —— 任何網路/解析異常都當「取不到」
        log.warning("取發送紀錄異常 task=%s: %s", task_id, e)
        return None


def fetch_attempts_batch(task_ids: list[int]) -> dict[str, list] | None:
    """一次取多筆任務的發送嘗試（運維頁列表展開用）。

    回 `{task_id_str: [attempt, ...]}`；**連不上 / 非 200 回 None**（呼叫端要區分
    「沒有紀錄」與「拿不到」）。空清單回 `{}`，不打網路。
    """
    ids = [int(i) for i in (task_ids or [])][:500]
    if not ids:
        return {}
    try:
        with _client() as c:
            r = c.post("/internal/api/outbound-attempt/batch", json={"task_ids": ids})
            if r.status_code != 200:
                log.warning("批次取發送紀錄失敗 status=%s", r.status_code)
                return None
            return (r.json() or {}).get("items") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("批次取發送紀錄異常: %s", e)
        return None


def push_account_map(items: list[dict] | None = None) -> bool:
    """①主動推送（MIGRATION_PLAN §1.3）：綁定/解綁後把映射推給 Connector。

    **加速器，不是保證** —— 失敗只記 log；真正的保證是 Connector 每 60s 的拉取兜底。
    """
    if items is None:
        from app.internal import repo  # 避免循環 import
        items = repo.account_map()
    payload = {
        "items": [
            {
                "platform": it.get("platform"),
                "platform_business_account_id": it.get("platform_business_account_id"),
                "business_id": it.get("business_id"),
                "bp_account_id": it.get("bp_account_id"),
                "enabled": bool(it.get("enabled", True)),
            }
            for it in items
        ]
    }
    try:
        with _client() as c:
            r = c.post("/internal/api/account-map/sync", json=payload)
            if r.status_code != 200:
                log.warning("推送帳號映射失敗 status=%s", r.status_code)
                return False
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("推送帳號映射異常: %s", e)
        return False


def _native_account_id(platform: str, bp_account_id) -> str:
    """CRM 的 `bp_account_id`（`crm_business_platform_account.id`）→ 平台原生 ID（text）。

    Connector 的訊息表用**平台原生 ID** 過濾（`platform_business_account_id`），
    所以呼叫前要先把 CRM 的自增 id 換成它。
    """
    if not bp_account_id:
        return ""
    from app.core import db as _db
    conn = _db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform_business_account_id FROM crm_business_platform_account"
                " WHERE id=%s AND platform=%s", (int(bp_account_id), platform))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else ""
    finally:
        conn.close()


def fetch_conversations(platform: str, platform_account_id=0, *, scope: str = "dm",
                        limit: int = 50, before: str = "") -> list[dict] | None:
    """工作台會話列表（走 Connector `conversations`，設計案 §4.2）。

    `platform_account_id` 是 CRM 的 `crm_business_platform_account.id`；
    連不上 / 非 200 回 **None**（呼叫端要區分「沒會話」與「拿不到」）。
    """
    try:
        native = _native_account_id(platform, platform_account_id)
        if not native:
            return []
        params = {"platform": platform, "platform_business_account_id": native,
                  "scope": scope, "limit": int(limit)}
        if before:
            params["before"] = before
        with _client() as c:
            r = c.get("/internal/api/conversations", params=params)
            if r.status_code != 200:
                log.warning("取會話列表失敗 status=%s", r.status_code)
                return None
            return (r.json() or {}).get("items") or []
    except Exception as e:  # noqa: BLE001
        log.warning("取會話列表異常: %s", e)
        return None


def upload_media(data: bytes, filename: str, *, scope: str = "outbound") -> dict | None:
    """把二進制推給 Connector 存檔，回 `{media_key, sha256, size}`；失敗回 None。"""
    try:
        with _client() as c:
            r = c.post("/internal/api/media/upload",
                       params={"scope": scope},
                       files={"file": (filename or "upload.bin", data)})
            if r.status_code != 200:
                log.warning("推送媒體失敗 status=%s", r.status_code)
                return None
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("推送媒體異常: %s", e)
        return None


def fetch_history(platform: str, end_user_sender_id: str, *,
                  platform_account_id=0, limit: int = 20,
                  before: str = "", scope: str = "dm") -> list[dict] | None:
    """取會話訊息（引擎取對話上下文 / 工作台渲染；走 Connector `messages/query`）。

    回傳 `[{id, direction, message_id, text, ts, ...}]`；連不上回 None。
    """
    try:
        native = _native_account_id(platform, platform_account_id)
        if not native:
            return []
        params = {"platform": platform, "platform_business_account_id": native,
                  "end_user_sender_id": end_user_sender_id, "scope": scope,
                  "limit": int(limit)}
        if before:
            params["before"] = before
        with _client() as c:
            r = c.get("/internal/api/messages/query", params=params)
            if r.status_code != 200:
                log.warning("取會話訊息失敗 status=%s", r.status_code)
                return None
            return (r.json() or {}).get("items") or []
    except Exception as e:  # noqa: BLE001
        log.warning("取會話訊息異常: %s", e)
        return None


def mark_message(platform: str, *, message_ids=None, message_id: str = "",
                 comment_id: str = "", conversation_id: str = "",
                 end_user_sender_id: str = "", state: str = "",
                 tier: str = "", detail: str = "", handled=None) -> int:
    """標記訊息的自動回復處置狀態（走 Connector `messages/mark`）。回傳受影響列數。"""
    body: dict = {"platform": platform}
    if message_ids:
        body["message_ids"] = list(message_ids)
    if message_id:
        body["message_id"] = message_id
    if comment_id:
        body["comment_id"] = comment_id
    if conversation_id:
        body["conversation_id"] = conversation_id
    if end_user_sender_id:
        body["end_user_sender_id"] = end_user_sender_id
    if state:
        body["state"] = state
    if tier:
        body["tier"] = tier
    if detail:
        body["detail"] = detail
    if handled is not None:
        body["handled"] = bool(handled)
    try:
        with _client() as c:
            r = c.post("/internal/api/messages/mark", json=body)
            if r.status_code != 200:
                log.warning("標記訊息失敗 status=%s", r.status_code)
                return 0
            return int((r.json() or {}).get("updated") or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("標記訊息異常: %s", e)
        return 0
