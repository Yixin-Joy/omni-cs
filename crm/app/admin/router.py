"""管理員後台端點 —— 商家、平台帳號、員工、授權、AI 開關。

全部要求 `admin` 角色 + CSRF；每個變更都在**同一事務**寫審計日誌。
"""
from __future__ import annotations

import psycopg2
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import audit
from app.admin import repo
from app.auth import deps, rbac
from app.auth import session as session_mod
from app.core import config
from app.core.security import hash_password
from app.internal import connector_client

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(deps.require_csrf)])

PLATFORMS = tuple(config.CHANNEL_LABELS.keys())          # wechat / whatsapp / ig / fb
VALID_SCOPES = set(repo.AI_SWITCH_SCOPES)                # {(platform, scope)}


def _push_account_map() -> None:
    """綁定/解綁後主動把帳號映射推給 Connector（加速器；失敗無妨，有拉取兜底）。"""
    try:
        connector_client.push_account_map()
    except Exception:  # noqa: BLE001
        pass


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


def _clean_roles(roles) -> list[str] | None:
    rs = [r for r in (roles or []) if r in rbac.ALL_ROLES]
    return sorted(set(rs)) if rs else None


def _clean_credential(platform: str, credential: dict | None) -> dict | None:
    """只保留該平台合法的憑證欄位，清掉空值。全部為空 → None（不寫入）。"""
    if not credential:
        return None
    keys = {k for k, _, _ in config.CREDENTIAL_FIELDS.get(platform, [])}
    out = {k: str(v).strip() for k, v in credential.items()
           if k in keys and v is not None and str(v).strip() != ""}
    return out or None


# ================================================================ 商家

@router.get("/businesses")
def list_businesses(sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "items": repo.list_businesses()}


class BusinessIn(BaseModel):
    business_name: str = ""
    business_memo: str | None = None


@router.post("/businesses")
def create_business(payload: BusinessIn, sess: dict = Depends(deps.require_admin)):
    name = (payload.business_name or "").strip()
    if not name:
        return _err(400, "商家名稱不能為空")
    with repo.tx() as (_, cur):
        bid = repo.create_business(cur, name, (payload.business_memo or "").strip() or None)
        audit.log(cur, sess, "business_create", business_id=bid,
                  payload={"business_name": name})
    return {"ok": True, "id": bid}


class BusinessUpdateIn(BaseModel):
    id: int
    business_name: str = ""
    business_memo: str | None = None
    enabled: bool = True


@router.post("/businesses/update")
def update_business(payload: BusinessUpdateIn, sess: dict = Depends(deps.require_admin)):
    name = (payload.business_name or "").strip()
    if not name:
        return _err(400, "商家名稱不能為空")
    with repo.tx() as (_, cur):
        if not repo.business_exists(cur, payload.id):
            return _err(404, "商家不存在")
        repo.update_business(cur, payload.id, name, (payload.business_memo or "").strip() or None,
                             payload.enabled)
        audit.log(cur, sess, "business_update", business_id=payload.id,
                  payload={"business_name": name, "enabled": payload.enabled})
    return {"ok": True}


# ================================================================ 平台帳號

@router.get("/accounts")
def list_accounts(business_id: int | None = Query(default=None),
                  sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "items": repo.list_accounts(business_id)}


class AccountIn(BaseModel):
    business_id: int
    platform: str = ""
    platform_business_account_id: str = ""
    display_name: str | None = None
    credential: dict | None = None


@router.post("/accounts")
def create_account(payload: AccountIn, sess: dict = Depends(deps.require_admin)):
    platform = (payload.platform or "").strip()
    native = (payload.platform_business_account_id or "").strip()
    if platform not in PLATFORMS:
        return _err(400, f"平台不合法（可選：{'/'.join(PLATFORMS)}）")
    if not native:
        return _err(400, "平台原生帳號 ID 不能為空（這是兩庫對齊鍵）")
    cred = _clean_credential(platform, payload.credential)
    try:
        with repo.tx() as (_, cur):
            if not repo.business_exists(cur, payload.business_id):
                return _err(404, "商家不存在")
            aid = repo.create_account(cur, payload.business_id, platform, native,
                                      (payload.display_name or "").strip() or None, cred)
            audit.log(cur, sess, "platform_account_bind", business_id=payload.business_id,
                      platform=platform, bp_account_id=aid,
                      payload={"platform_business_account_id": native,
                               "has_credential": bool(cred)})
    except psycopg2.errors.UniqueViolation:
        return _err(409, "該平台帳號已被綁定（同一平台的一個帳號只能屬於一個商家）")
    _push_account_map()
    return {"ok": True, "id": aid}


class AccountUpdateIn(BaseModel):
    id: int
    display_name: str | None = None
    ai_auto_reply_enabled: bool = True
    enabled: bool = True
    credential: dict | None = None


@router.post("/accounts/update")
def update_account(payload: AccountUpdateIn, sess: dict = Depends(deps.require_admin)):
    with repo.tx() as (_, cur):
        cur.execute(
            "SELECT business_id, platform FROM crm_business_platform_account WHERE id = %s",
            (payload.id,),
        )
        row = cur.fetchone()
        if not row:
            return _err(404, "平台帳號不存在")
        cred = _clean_credential(row["platform"], payload.credential)
        repo.update_account(cur, payload.id, (payload.display_name or "").strip() or None,
                            payload.ai_auto_reply_enabled, payload.enabled, cred)
        audit.log(cur, sess, "platform_account_unbind" if not payload.enabled else "platform_account_bind",
                  business_id=row["business_id"], platform=row["platform"], bp_account_id=payload.id,
                  payload={"ai_auto_reply_enabled": payload.ai_auto_reply_enabled,
                           "enabled": payload.enabled, "credential_updated": cred is not None})
    _push_account_map()
    return {"ok": True}


# ================================================================ 員工

@router.get("/staff")
def list_staff(sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "items": repo.list_staff()}


class StaffIn(BaseModel):
    login_account: str = ""
    password: str = ""
    staff_name: str = ""
    roles: list[str] = []
    is_super_agent: bool = False
    is_kb_editor: bool = False


@router.post("/staff")
def create_staff(payload: StaffIn, sess: dict = Depends(deps.require_admin)):
    account = (payload.login_account or "").strip()
    name = (payload.staff_name or "").strip()
    roles = _clean_roles(payload.roles)
    if not account:
        return _err(400, "登入帳號不能為空")
    if not name:
        return _err(400, "員工姓名不能為空")
    if not roles:
        return _err(400, "至少要選一個角色")
    if len(payload.password or "") < 8:
        return _err(400, "密碼至少 8 位")
    is_agent = rbac.ROLE_AGENT in roles
    try:
        with repo.tx() as (_, cur):
            sid = repo.create_staff(cur, account, hash_password(payload.password), name, roles,
                                    payload.is_super_agent and is_agent,
                                    payload.is_kb_editor and is_agent)
            audit.log(cur, sess, "staff_create", payload={
                "login_account": account, "roles": roles,
                "is_super_agent": payload.is_super_agent and is_agent,
                "is_kb_editor": payload.is_kb_editor and is_agent,
            })
    except psycopg2.errors.UniqueViolation:
        return _err(409, "登入帳號已存在")
    return {"ok": True, "id": sid}


class StaffUpdateIn(BaseModel):
    id: int
    staff_name: str = ""
    roles: list[str] = []
    is_super_agent: bool = False
    is_kb_editor: bool = False
    enabled: bool = True


@router.post("/staff/update")
def update_staff(payload: StaffUpdateIn, sess: dict = Depends(deps.require_admin)):
    name = (payload.staff_name or "").strip()
    roles = _clean_roles(payload.roles)
    if not name:
        return _err(400, "員工姓名不能為空")
    if not roles:
        return _err(400, "至少要選一個角色")
    if payload.id == sess["staff_id"]:
        # 防止把自己鎖在門外
        if not payload.enabled:
            return _err(400, "不能停用自己的帳號")
        if rbac.ROLE_ADMIN not in roles:
            return _err(400, "不能移除自己的管理員角色")
    is_agent = rbac.ROLE_AGENT in roles
    sa, kb = payload.is_super_agent and is_agent, payload.is_kb_editor and is_agent

    with repo.tx() as (_, cur):
        old = repo.get_staff(cur, payload.id)
        if not old:
            return _err(404, "員工不存在")
        repo.update_staff(cur, payload.id, name, roles, sa, kb, payload.enabled)
        changed_role = (sorted(old["roles"] or []) != roles
                        or bool(old["is_super_agent"]) != sa
                        or bool(old["is_kb_editor"]) != kb)
        if changed_role:
            audit.log(cur, sess, "staff_role_change", payload={
                "staff_id": payload.id, "before": sorted(old["roles"] or []), "after": roles,
                "is_super_agent": sa, "is_kb_editor": kb,
            })
        if old["enabled"] and not payload.enabled:
            audit.log(cur, sess, "staff_disable", payload={"staff_id": payload.id})
        elif not changed_role:
            audit.log(cur, sess, "staff_update", payload={"staff_id": payload.id, "enabled": payload.enabled})

    # 交易成功後才動 session（角色即時生效 / 停用即踢下線）
    if changed_role:
        session_mod.refresh_staff_sessions(payload.id, roles, sa, kb)
    if old["enabled"] and not payload.enabled:
        session_mod.delete_staff_sessions(payload.id)
    return {"ok": True}


class PasswordIn(BaseModel):
    id: int
    password: str = ""


@router.post("/staff/password")
def reset_password(payload: PasswordIn, sess: dict = Depends(deps.require_admin)):
    if len(payload.password or "") < 8:
        return _err(400, "密碼至少 8 位")
    with repo.tx() as (_, cur):
        if not repo.get_staff(cur, payload.id):
            return _err(404, "員工不存在")
        repo.set_password(cur, payload.id, hash_password(payload.password))
        audit.log(cur, sess, "staff_password_reset", payload={"staff_id": payload.id})
    # 改密碼 → 踢掉該員工所有 session
    session_mod.delete_staff_sessions(payload.id)
    return {"ok": True}


class KickIn(BaseModel):
    id: int


@router.post("/staff/kick")
def kick_staff(payload: KickIn, sess: dict = Depends(deps.require_admin)):
    n = session_mod.delete_staff_sessions(payload.id)
    with repo.tx() as (_, cur):
        audit.log(cur, sess, "force_logout", payload={"staff_id": payload.id, "killed": n})
    return {"ok": True, "killed": n}


class PermissionIn(BaseModel):
    staff_id: int
    business_ids: list[int] = []


@router.post("/staff/permissions")
def set_permissions(payload: PermissionIn, sess: dict = Depends(deps.require_admin)):
    with repo.tx() as (_, cur):
        if not repo.get_staff(cur, payload.staff_id):
            return _err(404, "員工不存在")
        repo.set_staff_permissions(cur, payload.staff_id, payload.business_ids)
        audit.log(cur, sess, "staff_business_permission_grant",
                  payload={"staff_id": payload.staff_id, "business_ids": sorted(set(payload.business_ids))})
    return {"ok": True}


# ================================================================ AI 開關

@router.get("/ai-switch")
def list_ai_switch(business_id: int = Query(...), sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "business_id": business_id, "items": repo.list_ai_switches(business_id)}


class AiSwitchIn(BaseModel):
    business_id: int
    platform: str = ""
    scope: str = ""
    enabled: bool = True


@router.post("/ai-switch")
def set_ai_switch(payload: AiSwitchIn, sess: dict = Depends(deps.require_admin)):
    if (payload.platform, payload.scope) not in VALID_SCOPES:
        return _err(400, "平台 / 場景組合不合法（微信與 WhatsApp 只有私訊；留言只有 IG / FB）")
    with repo.tx() as (_, cur):
        if not repo.business_exists(cur, payload.business_id):
            return _err(404, "商家不存在")
        repo.upsert_ai_switch(cur, payload.business_id, payload.platform, payload.scope, payload.enabled)
        audit.log(cur, sess, "ai_switch_change", business_id=payload.business_id,
                  platform=payload.platform,
                  payload={"scope": payload.scope, "enabled": payload.enabled})
    return {"ok": True}


class AiSwitchBulkIn(BaseModel):
    business_id: int
    enabled: bool = True


@router.post("/ai-switch/bulk")
def bulk_ai_switch(payload: AiSwitchBulkIn, sess: dict = Depends(deps.require_admin)):
    """★ 逐把寫入 —— 設計案 §9.1 的「全域」不是總開關，沒有捷徑。"""
    with repo.tx() as (_, cur):
        if not repo.business_exists(cur, payload.business_id):
            return _err(404, "商家不存在")
        for p, s in repo.AI_SWITCH_SCOPES:
            repo.upsert_ai_switch(cur, payload.business_id, p, s, payload.enabled)
        audit.log(cur, sess, "ai_switch_change", business_id=payload.business_id,
                  payload={"bulk": True, "enabled": payload.enabled,
                           "scopes": [f"{p}/{s}" for p, s in repo.AI_SWITCH_SCOPES]})
    return {"ok": True}


# ================================================================ 審計

@router.get("/audit")
def list_audit(limit: int = Query(default=100, ge=1, le=500),
               business_id: int | None = Query(default=None),
               operate_type: str | None = Query(default=None),
               sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "items": repo.list_audit(limit=limit, business_id=business_id,
                                                 operate_type=operate_type)}
