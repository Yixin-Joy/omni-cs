"""Connector 背景迴圈 —— 任務派發 / 回調重試、帳號映射同步、媒體快取清理。

在 FastAPI lifespan 啟動；`config.WORKER_ENABLED=false` 可整組關掉（自檢時用）。
"""
from __future__ import annotations

import asyncio
import logging

from app import media
from app.core import config
from app import history_sync
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


async def _post_media_loop() -> None:
    """貼文縮圖續命（Meta CDN 簽名約 24h 到期，過期就 403）。

    週期拉一次最近貼文，讓 `upsert_post` 把還沒有本地快取的圖存進
    `media/post_cache/`；已有快取的不會重下。`POST_MEDIA_REFRESH_HOURS=0` 可關閉。
    """
    hours = float(config.POST_MEDIA_REFRESH_HOURS or 0)
    if hours <= 0:
        log.info("貼文縮圖續命已停用（POST_MEDIA_REFRESH_HOURS=0）")
        return
    # 開機先等一下，別和映射同步／出站搶資源
    await asyncio.sleep(max(0.0, float(config.POST_MEDIA_FIRST_DELAY_SECONDS or 0)))
    while True:
        try:
            stats = await history_sync.refresh_post_media_once()
            if stats.get("errors"):
                log.info("貼文縮圖續命有 %s 項警告", len(stats["errors"]))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("貼文縮圖續命失敗: %s", e)
        await asyncio.sleep(max(300.0, hours * 3600.0))


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
        asyncio.create_task(_post_media_loop()),
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
