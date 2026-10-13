"""CRM 背景迴圈 —— AI 入站佇列消費 + 逾時清理 + session 清理。

在 FastAPI lifespan 啟動；`config.WORKER_ENABLED=false` 可關掉（自檢時避免干擾）。

兩條迴圈各自獨立、互不阻塞：
* `_ai_loop()`    —— 每 `AI_WORKER_POLL_SECONDS`（預設 1s）消費一輪 `crm_ai_inbound_queue`
* `_sweep_loop()` —— 每 `TIMEOUT_SWEEP_SECONDS`（預設 60s）逾時清理 + session 清理
                     + 回收卡住的 AI 佇列項
"""
from __future__ import annotations

import asyncio
import logging

from app.core import config
from app.worker import ai_queue
from app.worker import sweep

log = logging.getLogger("crm")

_ai_task: asyncio.Task | None = None
_sweep_task: asyncio.Task | None = None


async def _ai_loop() -> None:
    """AI 入站佇列 —— 高頻輪詢（佇列裡沒東西時幾乎零成本）。"""
    interval = float(config.AI_WORKER_POLL_SECONDS or 1)
    while True:
        try:
            stats = await ai_queue.process_once()
            if stats["claimed"]:
                log.info("AI 佇列本輪：%s", stats)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 單輪失敗不能弄死迴圈
            log.warning("AI 佇列回合失敗: %s", e)
        await asyncio.sleep(interval)


async def _sweep_loop() -> None:
    interval = float(config.TIMEOUT_SWEEP_SECONDS or 60)
    while True:
        try:
            await asyncio.to_thread(sweep.sweep_once)
            await asyncio.to_thread(sweep.sweep_sessions_once)
            await asyncio.to_thread(ai_queue.reap_stuck)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 單輪失敗不能弄死迴圈
            log.warning("背景清理回合失敗: %s", e)
        await asyncio.sleep(interval)


def start() -> None:
    global _ai_task, _sweep_task
    if not config.WORKER_ENABLED:
        log.info("背景 worker 已停用（WORKER_ENABLED=false）")
        return
    if not (_ai_task and not _ai_task.done()):
        _ai_task = asyncio.create_task(_ai_loop())
        log.info("AI 佇列消費者啟動：每 %.1fs 一輪", float(config.AI_WORKER_POLL_SECONDS or 1))
    if not (_sweep_task and not _sweep_task.done()):
        _sweep_task = asyncio.create_task(_sweep_loop())
        log.info("背景清理啟動：每 %.0fs 一輪", float(config.TIMEOUT_SWEEP_SECONDS or 60))


async def stop() -> None:
    global _ai_task, _sweep_task
    for task in (_ai_task, _sweep_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _ai_task = None
    _sweep_task = None
