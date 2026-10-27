"""管理員後台端點 —— 商家、平台帳號、員工、授權、AI 開關。

全部要求 `admin` 角色 + CSRF；每個變更都在**同一事務**寫審計日誌。
"""
from __future__ import annotations

import logging

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
# ★ 審計查詢與分組定義跟運維台共用同一份（見檔尾「審計」段）
from app.ops import repo as ops_repo
from app.ops import router as ops_router

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(deps.require_csrf)])

PLATFORMS = tuple(config.CHANNEL_LABELS.keys())          # wechat / whatsapp / ig / fb
VALID_SCOPES = set(repo.AI_SWITCH_SCOPES)                # {(platform, scope)}


def _push_account_map(replace: bool = False) -> None:
    """綁定/解綁後主動把帳號映射推給 Connector（加速器；失敗無妨，有拉取兜底）。

    ★ `replace=True` 用於**解綁**：要求 Connector 以這份清單為準覆蓋本地快取。
      少了它，剛解綁那一列會殘留到下一次 60s 定時拉取 —— 那段窗口內舊憑證還能收發。
    """
    try:
        connector_client.push_account_map(replace=replace)
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


def _cred_field_label(platform: str, key: str) -> str:
    for k, label, _ in config.CREDENTIAL_FIELDS.get(platform, []):
        if k == key:
            return label
    return key


def _resolve_meta_app(business_id: int | None, cred: dict | None) -> tuple[str, str]:
    """這個帳號應該用**哪個 Meta App**？回 `(app_id, app_secret)`，缺的給空字串。

    ★★ 多租戶（2026-10-20 小jo「以後每個商戶都可能對應一個獨立的 meta app」）：
      一商戶一 App 是合法且常見的。所以**不能**拿 token 去跟系統 `META_APP_ID` 硬比 ——
      那只在「大家都用同一個 App」時才成立。
      優先序：**帳號 credential 自己填的** → **商家的** → **系統級**（connector/.env）。
      三者都沒有 → 回 `("", "")`，呼叫端要當成「不知道」，**不可以擋**。
    """
    cred = cred or {}
    app_id = str(cred.get("meta_app_id") or "").strip()
    secret = str(cred.get("meta_app_secret") or "").strip()
    if business_id:
        try:
            with repo.ro() as cur:
                cur.execute(
                    "SELECT meta_app_id, meta_app_secret FROM crm_business WHERE id = %s",
                    (int(business_id),))
                r = cur.fetchone()
            if r:
                if not app_id:
                    app_id = str(r["meta_app_id"] or "").strip()
                if not secret:
                    secret = str(r["meta_app_secret"] or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("讀商家 Meta App 設定失敗 business_id=%s: %s", business_id, e)
    if not app_id:
        app_id = str(getattr(config, "META_APP_ID", "") or "").strip()
    if not secret:
        secret = str(getattr(config, "META_APP_SECRET", "") or "").strip()
    return app_id, secret


def _reject_wrong_app_token(platform: str, cred: dict | None,
                            business_id: int | None = None) -> str:
    """保存 fb/ig 帳號前體檢 token：**不是這個商戶那個 App 簽的就擋**。回錯誤字串或 ""。

    ★★ 為什麼（2026-10-20 小jo「meta上顯示的是有pages_read_engagement權限啊」）：
      Meta 後台顯示的是**某個 App** 的權限，但填進來的 token 可能由**另一個 App** 簽發。
      實測商家123：token 來自 `console_eprotele`(1611577203333651)，
      本系統用 `agent`(1612033297309372) →
        · 在 agent 後台勾 pages_read_engagement **完全無效**；
        · `/comments` 永遠 400（貼文與留言「數量」卻讀得到，極易誤判）；
        · webhook 一個都收不到（App 層 callback 不是我們的網址）。
      這種 token「看起來很正常」（`/me` 回粉專名、`/feed` 回 200），
      不擋就是使用者反覆回去勾權限、永遠沒效果。

    ★ **多租戶**：比對的對象是「這個商戶解析出來的 App」（帳號 credential → 商家 → 系統），
      不是寫死系統 App。沒設定任何 App（`expected` 為空）→ **放過**，只當資訊。
    ⚠️ best-effort：Connector 連不上／回 None 就放過 —— 不能因為體檢服務掛了讓人存不了帳號。
    ⚠️ 只擋「token 屬於別的 App」；權限不足只記錄（可能只是還沒審核過），仍允許保存。
    """
    if platform not in ("fb", "ig") or not cred:
        return ""
    expected, _secret = _resolve_meta_app(business_id, cred)
    # ★ 不傳 expected 也可能發生（CRM 沒配、商家也留空）→ 讓 Connector 回退它的系統 App。
    #   判斷結果以 Connector 回的 `expected_app_id` / `is_our_app` 為準，
    #   這樣「一商戶一 App」與「大家共用一個 App」兩種部署都正確。
    res = connector_client.verify_credential(platform, cred, expected_app_id=expected)
    if not res:
        return ""
    if res.get("is_our_app") is False:
        tid = res.get("token_app_id") or "?"
        tname = res.get("token_app_name") or "未知"
        want = res.get("expected_app_id") or expected or "（未設定）"
        src = res.get("expected_source") or ""
        if src == "system":
            hint = f"（目前用的是系統預設 App {want}）"
        else:
            hint = f"（這個商戶設定的 App 是 {want}）"
        return (f"這把 token 是【{tname}】(App {tid}) 簽發的，"
                f"但{hint}。"
                f"在別的 App 後台勾權限對它完全無效，webhook 也收不到。"
                f"請用 App {want} 重新授權這個粉專再貼新的粉專令牌；"
                f"若這個商戶本來就該用 App {tid}，"
                f"請先在『編輯商家 → Meta App ID』填 {tid}（並填它的 Secret）。")
    return ""


# ================================================================ 商家

@router.get("/businesses")
def list_businesses(sess: dict = Depends(deps.require_admin)):
    return {"ok": True, "items": repo.list_businesses()}


class BusinessIn(BaseModel):
    business_name: str = ""
    business_memo: str | None = None
    # ★ 每商戶獨立的 Meta App（留空 = 用系統級 connector/.env）
    meta_app_id: str | None = None
    meta_app_secret: str | None = None


@router.post("/businesses")
def create_business(payload: BusinessIn, sess: dict = Depends(deps.require_admin)):
    name = (payload.business_name or "").strip()
    if not name:
        return _err(400, "商家名稱不能為空")
    with repo.tx() as (_, cur):
        bid = repo.create_business(
            cur, name, (payload.business_memo or "").strip() or None,
            (payload.meta_app_id or "").strip() or None,
            (payload.meta_app_secret or "").strip() or None)
        audit.log(cur, sess, "business_create", business_id=bid,
                  payload={"business_name": name,
                           "has_meta_app": bool((payload.meta_app_id or "").strip())})
    # ★ 種入「舊系統的調校值」→ 新商家一出生就與 Epro_test 的檢索參數一致。
    #   為什麼：`kb_param` 是按商家存的，不種就會退回 spec 預設
    #   （`query_rewrite=False` / `max_per_doc=3` / `min_score=0.28`），RAG 明顯變差。
    try:
        from app.rag import kb_db

        kb_db.seed_business_params(bid)
    except Exception as e:  # noqa: BLE001 種不進去不該擋住建商家
        log.warning("商家 %s 檢索參數初始值寫入失敗: %s", bid, e)
    return {"ok": True, "id": bid}


class BusinessUpdateIn(BaseModel):
    id: int
    business_name: str = ""
    business_memo: str | None = None
    enabled: bool = True
    # ★ 傳 `None`（不帶這個鍵）= **不動**；傳空字串 = 清空（回退系統級）
    meta_app_id: str | None = None
    meta_app_secret: str | None = None


@router.post("/businesses/update")
def update_business(payload: BusinessUpdateIn, sess: dict = Depends(deps.require_admin)):
    name = (payload.business_name or "").strip()
    if not name:
        return _err(400, "商家名稱不能為空")
    with repo.tx() as (_, cur):
        if not repo.business_exists(cur, payload.id):
            return _err(404, "商家不存在")
        repo.update_business(cur, payload.id, name, (payload.business_memo or "").strip() or None,
                             payload.enabled,
                             payload.meta_app_id, payload.meta_app_secret)
        audit.log(cur, sess, "business_update", business_id=payload.id,
                  payload={"business_name": name, "enabled": payload.enabled,
                           "meta_app_changed": payload.meta_app_id is not None
                           or payload.meta_app_secret is not None})
    # ★ App Secret 可能換了 → 讓 Connector 立刻拿到新映射（不然要等 60s 拉取，
    #   這段時間 webhook 驗簽還是用舊 secret → 403）。
    _push_account_map()
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
    if platform not in PLATFORMS:
        return _err(400, f"平台不合法（可選：{'/'.join(PLATFORMS)}）")
    cred = _clean_credential(platform, payload.credential)
    # ★ 憑證**不是選填**：缺任何一項就擋下。空殼帳號會讓 webhook 取不到憑證而靜默失敗。
    missing = [k for k in config.CREDENTIAL_REQUIRED.get(platform, ())
               if not (cred or {}).get(k)]
    if missing:
        labels = "、".join(_cred_field_label(platform, k) for k in missing)
        return _err(400, f"缺少必要憑證：{labels}")
    # ★ 擋「這把 token 屬於別的 Meta App」——那種 token 權限勾再多也沒用（見函式註解）
    wrong_app = _reject_wrong_app_token(platform, cred, payload.business_id)
    if wrong_app:
        return _err(400, wrong_app)
    # ★ 原生 ID 不再要使用者單獨填 —— 它在每個平台就是某一個憑證欄位
    #   （微信 AppID / WhatsApp phone_number_id / FB page id / IG ig user id）。
    #   表單若仍帶了值（舊前端）以表單為準，否則從憑證推導。
    native = (payload.platform_business_account_id or "").strip()
    if not native:
        key = config.NATIVE_ID_FIELD.get(platform, "")
        native = str((cred or {}).get(key) or "").strip()
        if not native:
            return _err(400, f"請填「{_cred_field_label(platform, key)}」"
                             "（它就是這個帳號的平台原生 ID，兩庫對齊鍵）")
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
    # ★ 先把 platform 查出來（不開事務），再體檢 token ——
    #   體檢要打 Connector（慢，可能 20s），絕對不能放在事務裡持鎖。
    with repo.ro() as cur:
        cur.execute(
            "SELECT business_id, platform FROM crm_business_platform_account WHERE id = %s",
            (payload.id,),
        )
        row = cur.fetchone()
    if not row:
        return _err(404, "平台帳號不存在")
    cred = _clean_credential(row["platform"], payload.credential)
    # ★ 換 token 時同樣要擋跨 App 的 token（同 create_account）
    if cred:
        wrong_app = _reject_wrong_app_token(row["platform"], cred, row["business_id"])
        if wrong_app:
            return _err(400, wrong_app)
    with repo.tx() as (_, cur):
        repo.update_account(cur, payload.id, (payload.display_name or "").strip() or None,
                            payload.ai_auto_reply_enabled, payload.enabled, cred)
        audit.log(cur, sess, "platform_account_unbind" if not payload.enabled else "platform_account_bind",
                  business_id=row["business_id"], platform=row["platform"], bp_account_id=payload.id,
                  payload={"ai_auto_reply_enabled": payload.ai_auto_reply_enabled,
                           "enabled": payload.enabled, "credential_updated": cred is not None})
    _push_account_map()
    return {"ok": True}


class AccountUnbindIn(BaseModel):
    id: int


@router.post("/accounts/unbind")
def unbind_account(payload: AccountUnbindIn, sess: dict = Depends(deps.require_admin)):
    """解綁平台帳號（物理刪除，釋放 UNIQUE 鍵 → 可以綁到別的商家）。

    ★ 與 `accounts/update` 傳 `enabled=false` 的差別：
      - 停用：帳號還在、憑證還在、**仍占著唯一鍵**，只是不收發。想暫時關掉用它。
      - 解綁：這條記錄整個消失。憑證一併帶走，貼文配置一起刪，
        歷史發送任務保留但解除關聯。要換商家 / 換帳號就必須解綁。

    ⚠️ 順序要求：先查資料（唯讀、不持鎖）→ 事務內刪 → **事務外**推映射。
      推送會打 HTTP，放進事務裡等於持著 DB 連線等網路，慢的時候會卡住整表。
    """
    with repo.ro() as cur:
        cur.execute(
            "SELECT business_id, platform, platform_business_account_id, display_name, enabled"
            "  FROM crm_business_platform_account WHERE id = %s",
            (payload.id,),
        )
        row = cur.fetchone()
    if not row:
        return _err(404, "平台帳號不存在（可能已被刪除，請重新整理）")

    with repo.tx() as (_, cur):
        result = repo.delete_account(cur, payload.id)
        if result["deleted"] == 0:
            return _err(404, "平台帳號不存在")
        audit.log(cur, sess, "platform_account_unbind",
                  business_id=row["business_id"], platform=row["platform"],
                  bp_account_id=payload.id,
                  payload={"platform_business_account_id": row["platform_business_account_id"],
                           "display_name": row["display_name"],
                           "was_enabled": row["enabled"],
                           "post_config_deleted": result["post_config_deleted"],
                           "tasks_detached": result["tasks_detached"]})
    # ★ 必須 replace=True：讓 Connector 立刻把這把憑證從快取裡抹掉。
    _push_account_map(replace=True)
    return {"ok": True, **result}


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

# ★ 2026-10-20 小jo：「管理員後臺的審計日志一起改了」—— 管理台與運維台
#   共用同一套前端（`/ui/audit_view.js`），所以後端也要給同樣的篩選能力。
#   這裡直接復用 `ops.repo.list_audit`（含 staff_role / fail_reason / total / 分頁），
#   不再走 `admin.repo.list_audit` 那個只能 limit 的舊版本。
@router.get("/audit")
def list_audit(limit: int = Query(default=20, ge=1, le=200),
               offset: int = Query(default=0, ge=0),
               types: str = Query(default=""),
               q: str = Query(default=""),
               time_from: str = Query(default=""),
               time_to: str = Query(default=""),
               only_failed: bool = Query(default=False),
               all: bool = Query(default=False),
               business_id: int = Query(default=0),
               sess: dict = Depends(deps.require_admin)):
    """審計列表（篩選 + 分頁）。

    `all=true` → 連已下線類型的歷史紀錄一起看（例如舊的 conversation_read／
    role_switch）。預設只看現行 5 組，否則「合計」會跟各組加起來對不上。
    """
    picked = [t.strip() for t in (types or "").split(",") if t.strip()]
    if all:
        picked = []        # ★ 全部類型＝不加 types 條件（前端帶的 5 組要忽略）
    elif not picked:
        # ⚠️ 這裡必須是 `elif`：寫成「先清空、再 if not picked 補預設」
        #    會又把 5 組塞回去 → all=true 形同無效（踩過）
        picked = sorted({t for g in ops_router.AUDIT_GROUPS for t in g["types"]})
    return {"ok": True,
            **ops_repo.list_audit(limit=int(limit), offset=int(offset),
                                  types=(picked or None),
                                  business_id=int(business_id or 0),
                                  time_from=(time_from or None), time_to=(time_to or None),
                                  q=(q or ""), only_failed=bool(only_failed)),
            "groups": ops_router.AUDIT_GROUPS,
            "type_labels": ops_router.AUDIT_TYPE_LABELS}


@router.get("/audit/facets")
def audit_facets(time_from: str = Query(default=""), time_to: str = Query(default=""),
                 all: bool = Query(default=False),
                 business_id: int = Query(default=0),
                 sess: dict = Depends(deps.require_admin)):
    """各組／各類型在當前時間範圍內有幾筆（chip 上的數字）。"""
    allowed = None if all else sorted(
        {t for g in ops_router.AUDIT_GROUPS for t in g["types"]})
    counts = ops_repo.audit_type_counts(business_id=int(business_id or 0),
                                        time_from=(time_from or None),
                                        time_to=(time_to or None), types=allowed)
    groups = []
    for g in ops_router.AUDIT_GROUPS:
        groups.append({"key": g["key"], "label": g["label"], "hint": g["hint"],
                       "types": g["types"],
                       "count": sum(counts.get(t, 0) for t in g["types"])})
    return {"ok": True, "groups": groups, "counts": counts,
            "total": sum(g["count"] for g in groups),
            "total_all": sum(counts.values())}
