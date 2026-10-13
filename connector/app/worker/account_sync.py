"""帳號映射同步（設計案 §1.3 三道防線的②拉取兜底 + ③過期告警）。

① 主動推送由 CRM 在綁定/解綁時呼叫 Connector 的 `/internal/api/account-map/sync`（本檔不負責）。
   ——推送是加速器；**真正的保證是這裡的定時全量拉取**。
"""
from __future__ import annotations

import logging

from app import account_map
from app.core import config
from app.worker import client

log = logging.getLogger("connector")


async def sync_account_map() -> dict:
    """向 CRM 拉全量映射並覆蓋本地快取（冪等）。"""
    try:
        items = await client.fetch_account_map()
    except Exception as e:  # noqa: BLE001
        log.warning("拉取帳號映射失敗: %s", e)
        await check_stale()
        return {"ok": False, "error": str(e)}

    n = account_map.replace_all(items)
    account_map.mark_synced()
    log.info("帳號映射同步完成：%s 筆", n)
    return {"ok": True, "count": n}


async def check_stale() -> bool:
    """超過門檻沒成功同步 → 上報 `account_map_stale`（同一把只留一條）。"""
    secs = account_map.stale_seconds()
    threshold = float(config.ACCOUNT_MAP_STALE_SECONDS or 300)
    if secs is not None and secs <= threshold:
        return False
    await client.report_alert(
        "account_map_stale", "critical", "帳號映射同步過期",
        {"last_sync_seconds": secs, "threshold_seconds": threshold},
        dedupe_key="account_map",
    )
    return True
