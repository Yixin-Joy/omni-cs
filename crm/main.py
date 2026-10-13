"""CRM 服務入口 —— 大腦 + 資料 + 工作台。

- 員工工作台（工作台 / 管理員後台 / 運維頁）
- AI 四層決策引擎 + RAG 知識庫 + 業務表格 + FAQ
- 消費 `crm_ai_inbound_queue`（背景 asyncio worker，單一序列）
- 對 Connector 提供內網 API（claim / update-status / ai-inbound / account-map ...）

啟動（在本目錄下執行）：
    uvicorn main:app --host 127.0.0.1 --port 8020
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.admin.router import router as admin_router
from app.agent.console_router import router as agent_console_router
from app.agent.router import router as agent_router
from app.auth import deps, rbac
from app.auth.router import router as auth_router
from app.core import config
from app.core import db
from app.internal.router import router as internal_router
from app.ops.router import router as ops_router
from app.worker import runner as worker_runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("crm")

UI_DIR = Path(__file__).resolve().parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_schema_async()
    worker_runner.start()
    log.info("CRM 啟動：port=%s", config.PORT)
    yield
    await worker_runner.stop()


app = FastAPI(title="multi-cs-crm", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(agent_console_router)
app.include_router(ops_router)
app.include_router(internal_router)

# ★ 知識庫圖片的公開目錄（從舊單體搬入）—— 只掛**圖片專用**目錄，
#   這樣 markdown 裡的 `![題注](/media/xxx.png)` 能直接渲染。
#   客戶來訊媒體（/files）不在這裡 —— 那是 Connector 的職責（拆服務後各管各的）。
from app.rag import rag_config as _rag_config  # noqa: E402
Path(_rag_config.RAG_MEDIA_DIR).mkdir(parents=True, exist_ok=True)
app.mount(_rag_config.RAG_MEDIA_URL_PREFIX.rstrip("/") or "/media",
          StaticFiles(directory=_rag_config.RAG_MEDIA_DIR), name="kb-media")


@app.get("/health")
async def health():
    return {"ok": True, "service": config.SERVICE_NAME}


# ---------------------------------------------------------------- 頁面

def _ui(name: str, media: str = "text/html; charset=utf-8") -> HTMLResponse:
    """每請求重讀（沿用舊專案習慣：改 HTML 不必重啟服務）。"""
    p = UI_DIR / name
    if not p.exists():
        return HTMLResponse(f"<h1>404</h1><p>找不到頁面：{name}</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"), media_type=media)


# 可公開取的 UI 靜態資源（白名單，避免任意路徑讀檔）
UI_ASSETS = {"common.js": "application/javascript; charset=utf-8"}


@app.get("/ui/{name}")
def page_ui_asset(name: str):
    media = UI_ASSETS.get(name)
    if not media:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return _ui(name, media)


# 身分 → 首頁（admin / agent / ops 三台都已接線）
PAGE_BY_ROLE = {
    rbac.ROLE_ADMIN: "admin.html",
    rbac.ROLE_AGENT: "console_ui.html",
    rbac.ROLE_OPS: "ops.html",
}


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def page_login(request: Request):
    if deps.current_session(request):
        return RedirectResponse("/", status_code=302)
    return _ui("login.html")


@app.get("/")
def page_index(request: Request):
    """★ 依**當前登入身分**決定首頁 —— 一帳號多身分時，選了哪個身分就進哪個台。"""
    sess = deps.current_session(request)
    if not sess:
        return _redirect_login()
    return _ui(PAGE_BY_ROLE.get(rbac.active_role(sess) or "", "placeholder.html"))


@app.get("/admin")
def page_admin(request: Request):
    sess = deps.current_session(request)
    if not sess:
        return _redirect_login()
    if not rbac.is_admin(sess):
        return RedirectResponse("/", status_code=302)
    return _ui("admin.html")


@app.get("/agent")
def page_agent(request: Request):
    sess = deps.current_session(request)
    if not sess:
        return _redirect_login()
    if not rbac.is_agent(sess):
        return RedirectResponse("/", status_code=302)
    return _ui("console_ui.html")


@app.get("/ops")
def page_ops(request: Request):
    sess = deps.current_session(request)
    if not sess:
        return _redirect_login()
    if not rbac.can_manage_ops(sess):
        return RedirectResponse("/", status_code=302)
    return _ui("ops.html")


@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)
