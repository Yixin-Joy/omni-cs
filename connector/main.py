"""Connector 服務入口 —— 純通道：收訊息、存訊息、發訊息、存媒體。

- 對外公網只暴露 webhook（微信 / WhatsApp / IG / FB）。
- `/internal/api/*` 僅供 CRM 內網呼叫（認證用 `X-Internal-Token`）。
- AI 引擎與知識庫**不在這裡** —— 駐留 CRM 服務。

啟動（在本目錄下執行）：
    uvicorn main:app --host 127.0.0.1 --port 8010
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core import config
from app.core import db
from app.internal.router import router as internal_router
from app.worker import runner as worker_runner
from app.webhook import router as webhook_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("connector")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_schema_async()
    worker_runner.start()
    log.info("Connector 啟動：port=%s", config.PORT)
    yield
    await worker_runner.stop()


app = FastAPI(title="multi-cs-connector", lifespan=lifespan)
app.include_router(internal_router)

# 客戶來訊媒體的公開靜態目錄（webhook 下載的語音/視頻/圖片）
Path(config.MEDIA_DIR).mkdir(exist_ok=True)
app.mount("/files", StaticFiles(directory=config.MEDIA_DIR), name="files")

# 對外收訊入口（微信 / WhatsApp / IG / FB）
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"ok": True, "service": config.SERVICE_NAME}
