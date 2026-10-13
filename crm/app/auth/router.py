"""認證端點：登入 / 選身分 / 切換身分 / 登出 / 當前身分。

★ 一帳號多身分（多角色）的登入是兩步：
  ① `POST /login {account, password}` → 回 `need_role=true` + 一次性 `ticket` + 可選身分清單
  ② `POST /login {ticket, role}` → 才真正建立 session（`active_role` = 所選身分）
  單一身分帳號①就直接建 session，前端無感。
"""
from __future__ import annotations

import threading
import time

import psycopg2.extras
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.auth import deps, rbac
from app.auth import session as session_mod
from app.core import config
from app.core import db
from app.core.security import verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 帳號不存在時用來消耗等量時間，避免用回應時間探測帳號是否存在
_DUMMY_HASH = (
    "pbkdf2_sha256$260000$00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

# ---- 待選身分的臨時憑證（密碼驗過、但還沒決定用哪個身分）----
# 只存在進程記憶體、120 秒有效；單機部署足夠，重啟即清空（前端重登即可）。
_PENDING_TTL = 120
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _put_pending(staff: dict) -> str:
    tk = session_mod.new_token(24)
    now = time.time()
    with _pending_lock:
        for k in [k for k, v in _pending.items() if v["exp"] < now]:
            _pending.pop(k, None)
        _pending[tk] = {"staff": staff, "exp": now + _PENDING_TTL}
    return tk


def _take_pending(tk: str) -> dict | None:
    """取用即失效（一次性）。"""
    now = time.time()
    with _pending_lock:
        rec = _pending.pop(tk, None)
    if not rec or rec["exp"] < now:
        return None
    return rec["staff"]


class LoginIn(BaseModel):
    account: str = ""
    password: str = ""
    role: str = ""     # 選定身分（第二步）
    ticket: str = ""   # 第一步回傳的臨時憑證


class SwitchRoleIn(BaseModel):
    role: str = ""


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _staff_public(sess: dict) -> dict:
    all_roles = rbac.roles_all(sess)
    act = rbac.active_role(sess)
    return {
        "staff_id": sess.get("staff_id"),
        "staff_name": sess.get("staff_name"),
        "login_account": sess.get("login_account"),
        # roles = 當前生效身分（保持與舊呼叫端相容：陣列，0 或 1 個元素）
        "roles": [act] if act else [],
        "active_role": act,
        "available_roles": all_roles,
        "can_switch": rbac.can_switch_role(sess),
        "role_labels": dict(rbac.ROLE_LABEL),
        "home": rbac.home_path(sess),
        # ★ admin 一律視為超級坐席 → 用函式而非直接讀旗標
        "is_super_agent": rbac.is_super_agent(sess),
        "is_kb_editor": rbac.is_kb_editor(sess),
    }


def _finish_login(staff: dict, role: str | None, response: Response) -> dict:
    """真的建立 session 並下發 cookie（單一身分 / 選完身分後都走這裡）。"""
    sid, csrf = session_mod.create_session(staff, role)
    sess = dict(staff)
    sess["active_role"] = role
    audit.log_standalone(sess, "login", payload={"account": staff["login_account"], "role": role})
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=sid,
        max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True, "staff": _staff_public(sess), "csrf_token": csrf}


@router.post("/login")
def login(payload: LoginIn, response: Response, request: Request):
    # ---- 第二步：帶 ticket 選定身分 ----
    ticket = (payload.ticket or "").strip()
    if ticket:
        role = (payload.role or "").strip()
        staff = _take_pending(ticket)
        if not staff:
            return _err(400, "身分選擇已逾時，請重新登入")
        if role not in (staff.get("roles") or []):
            return _err(400, "沒有這個身分")
        return _finish_login(staff, role, response)

    account = (payload.account or "").strip()
    password = payload.password or ""
    role = (payload.role or "").strip()
    if not account or not password:
        return _err(400, "請輸入帳號與密碼")

    left = deps.login_locked_seconds(account)
    if left > 0:
        return _err(403, f"登入失敗次數過多，請 {max(1, left // 60 + 1)} 分鐘後再試")

    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, login_account, password_hash, staff_name, roles,"
                "       is_super_agent, is_kb_editor, enabled"
                "  FROM crm_staff WHERE login_account = %s",
                (account,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok or not row["enabled"]:
        remain = deps.record_login_failure(account)
        audit.log_standalone(
            None, "login_failed",
            payload={"account": account, "reason": "帳號或密碼錯誤" if remain >= 0 else "已鎖定"},
        )
        if remain < 0:
            return _err(403, f"登入失敗次數過多，帳號已鎖定 {config.LOGIN_LOCK_MINUTES} 分鐘")
        return _err(401, f"帳號或密碼錯誤（還可嘗試 {remain} 次）")

    deps.clear_login_failures(account)
    # ★ 可選身分 = admin 超集展開後的清單（admin 帳號 → admin/agent/ops）
    roles = rbac.expand_roles(row["roles"])
    staff = {
        "id": row["id"],
        "staff_id": row["id"],
        "staff_name": row["staff_name"],
        "login_account": row["login_account"],
        "roles": roles,
        "is_super_agent": row["is_super_agent"],
        "is_kb_editor": row["is_kb_editor"],
    }

    # ★ 只要身分多於一個，**一律**先回可選清單（不建 session）——
    #   即使請求夾帶 role 也不放行，避免繞過「多身分必選」的前端步驟。
    if len(roles) > 1:
        return {
            "ok": True,
            "need_role": True,
            "ticket": _put_pending(staff),
            "staff": {"staff_name": staff["staff_name"], "login_account": staff["login_account"]},
            "available_roles": roles,
            "role_labels": dict(rbac.ROLE_LABEL),
        }
    if role and role not in roles:
        return _err(400, "沒有這個身分")
    return _finish_login(staff, role or (roles[0] if roles else None), response)


@router.post("/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get(config.SESSION_COOKIE_NAME)
    sess = session_mod.load_session(sid)
    session_mod.delete_session(sid)
    if sess:
        audit.log_standalone(sess, "logout")
    response.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/switch-role")
def switch_role(payload: SwitchRoleIn, sess: dict = Depends(deps.require_csrf)):
    """切換當前登入身分（不換 session、不重新登入）。

    ★ 角色一律以 **DB 最新值**為準，不信 session 快照 —— 避免 admin 剛移除某角色，
    舊 session 還能切過去。
    """
    role = (payload.role or "").strip()
    latest = rbac.expand_roles(rbac.staff_roles_from_db(sess.get("staff_id")))
    if not latest:
        return _err(403, "帳號不存在或已停用")
    if role not in latest:
        return _err(403, "沒有這個身分")

    old = rbac.active_role(sess)
    if role != old:
        session_mod.set_active_role(sess.get("session_id"), role)
        audit.log_standalone({**sess, "active_role": role}, "role_switch",
                             payload={"from": old, "to": role})
    return {
        "ok": True,
        "active_role": role,
        "home": rbac.home_path_for_role(role),
        "staff": _staff_public({**sess, "roles": latest, "active_role": role}),
    }


@router.get("/me")
def me(sess: dict = Depends(deps.require_login)):
    return {"ok": True, "staff": _staff_public(sess), "csrf_token": sess.get("csrf_token")}
