"""FastAPI 依賴：讀 session、CSRF 校驗、角色閘門。

★ 全部同步函數 → FastAPI 自動丟 threadpool，不會卡 event loop（psycopg2 是同步的）。
"""
from __future__ import annotations

import hmac
import threading
import time

from fastapi import Depends, HTTPException, Request

from app.auth import rbac
from app.auth import session as session_mod
from app.core import config


# ---------------------------------------------------------------- Session

def current_session(request: Request) -> dict | None:
    return session_mod.load_session(request.cookies.get(config.SESSION_COOKIE_NAME))


def require_login(request: Request) -> dict:
    sess = current_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="未登入或登入已過期")
    return sess


def require_admin(sess: dict = Depends(require_login)) -> dict:
    if not rbac.is_admin(sess):
        raise HTTPException(status_code=403, detail="需要管理員權限")
    return sess


def require_agent(sess: dict = Depends(require_login)) -> dict:
    if not rbac.is_agent(sess):
        raise HTTPException(status_code=403, detail="需要客服坐席權限")
    return sess


def require_ops(sess: dict = Depends(require_login)) -> dict:
    if not rbac.can_manage_ops(sess):
        raise HTTPException(status_code=403, detail="需要運維權限")
    return sess


# ---------------------------------------------------------------- CSRF

def require_csrf(request: Request, sess: dict = Depends(require_login)) -> dict:
    """狀態變更請求必須帶 `X-CSRF-Token`（值 = session 的 csrf_token）。"""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return sess
    token = request.headers.get("X-CSRF-Token") or ""
    if not token or not hmac.compare_digest(token, sess.get("csrf_token") or ""):
        raise HTTPException(status_code=403, detail="CSRF 校驗失敗，請重新整理頁面")
    return sess


# ---------------------------------------------------------------- 登入失敗鎖定
# 設計案 §13.5：5 次失敗鎖 15 分鐘。單機部署用進程內計數即可（重啟即清空，可接受）。

_fail_lock: dict[str, list] = {}          # account -> [fail_count, locked_until_ts]
_lock_mutex = threading.Lock()


def login_locked_seconds(account: str) -> int:
    """還被鎖著回剩餘秒數，否則 0。"""
    now = time.time()
    with _lock_mutex:
        rec = _fail_lock.get(account)
        if not rec:
            return 0
        if rec[1] > now:
            return int(rec[1] - now)
    return 0


def record_login_failure(account: str) -> int:
    """記一次失敗，達門檻則鎖定。回傳「還剩幾次可試」（<0 表示已鎖）。"""
    now = time.time()
    with _lock_mutex:
        rec = _fail_lock.setdefault(account, [0, 0.0])
        # 鎖已過期 → 重新計數
        if rec[1] and rec[1] <= now:
            rec[0] = 0
            rec[1] = 0.0
        rec[0] += 1
        if rec[0] >= config.LOGIN_MAX_FAIL:
            rec[1] = now + config.LOGIN_LOCK_MINUTES * 60
            return -1
        return config.LOGIN_MAX_FAIL - rec[0]


def clear_login_failures(account: str) -> None:
    with _lock_mutex:
        _fail_lock.pop(account, None)
