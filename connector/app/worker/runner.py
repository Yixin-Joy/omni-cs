"""Connector 背景迴圈 —— 任務派發 / 回調重試、帳號映射同步、媒體快取清理。

在 FastAPI lifespan 啟動；`config.WORKER_ENABLED=false` 可整組關掉（自檢時用）。
"""
from __future__ import annotations

import asyncio
import logging

from app import media
from app.core import config
from app.worker import account_sync, callback, outbound

log = logging.getLogger("connector")

_tasks: list[asyncio.Task] = []
CACHE_SWEEP_SECONDS = 3600.0


async def _outbound_loop() -> None:
    while True:
        try:
            stats = await outbound.tick()
            if stats.get("claimed"):
                log.info("出站：%s", stats)
            await callback.flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("出站回合失敗: %s", e)
        await asyncio.sleep(max(0.2, float(config.POLL_SECONDS or 1)))


async def _account_loop() -> None:
    while True:
        try:
            await account_sync.sync_account_map()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("帳號映射回合失敗: %s", e)
        await asyncio.sleep(max(5.0, float(config.ACCOUNT_MAP_SYNC_SECONDS or 60)))


async def _cache_loop() -> None:
    while True:
        try:
            n = await asyncio.to_thread(media.purge_kb_cache)
            if n:
                log.info("清理知識庫圖片快取：%s 個", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("快取清理失敗: %s", e)
        await asyncio.sleep(CACHE_SWEEP_SECONDS)


def start() -> None:
    global _tasks
    if not config.WORKER_ENABLED:
        log.info("Connector worker 已停用（WORKER_ENABLED=false）")
        return
    if _tasks:
        return
    _tasks = [
        asyncio.create_task(_outbound_loop()),
        asyncio.create_task(_account_loop()),
        asyncio.create_task(_cache_loop()),
    ]
    log.info("Connector worker 啟動：輪詢 %.1fs / 映射同步 %.0fs（發送模式 %s）",
             float(config.POLL_SECONDS or 1), float(config.ACCOUNT_MAP_SYNC_SECONDS or 60),
             config.OUTBOUND_SEND_MODE)


async def stop() -> None:
    global _tasks
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks = []
