"""Connector 服務入口 —— 純通道：收訊息、存訊息、發訊息、存媒體。

- 對外公網只暴露 webhook（微信 / WhatsApp / IG / FB）。
- `/internal/api/*` 僅供 CRM 內網呼叫（認證用 `X-Internal-Token`）。
- AI 引擎與知識庫**不在這裡** —— 駐留 CRM 服務。

啟動（在本目錄下執行）：
    uvicorn main:app --host 127.0.0.1 --port 8010
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import config
from app.core import db
from app.internal.router import router as internal_router
from app.worker import runner as worker_runner

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


@app.get("/health")
async def health():
    return {"ok": True, "service": config.SERVICE_NAME}


# ---- 待搬入（階段三）----
# 1. webhook 路由：從 legacy app/main.py 搬（/wechat、/webhook、/whatsapp/webhook）
# 2. 通道接收與正規化：legacy app/channels/{wechat,whatsapp,igfb}.py
# 3. 來訊媒體下載與永久存儲（media/）
# 4. 收到 webhook → 落地 → 非同步通知 CRM /internal/api/ai/inbound
