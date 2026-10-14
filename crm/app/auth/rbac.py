"""RBAC —— 設計案 §8 的規則落地。

角色：admin / agent / ops（roles 陣列，可複合）；旗標：is_super_agent / is_kb_editor。

★ 後端鐵則：每次查詢、每次操作都呼叫這裡校驗，**不能只靠前端隱藏 UI**。
"""
from __future__ import annotations

import psycopg2.extras

from app.core import db

ROLE_ADMIN = "admin"
ROLE_AGENT = "agent"
ROLE_OPS = "ops"
ALL_ROLES = (ROLE_ADMIN, ROLE_AGENT, ROLE_OPS)


def expand_roles(roles: list[str] | None) -> list[str]:
    """回傳帳號的「可選身分」—— ★ 不再有「admin 是超集」的特權（2026-10-14 改）。

    **可選身分 = `crm_staff.roles` 欄位本身**（管理員在「員工與權限」勾選的）。

    為什麼改：原設計讓「凡具備 admin 角色者自動取得 agent / ops」，
    這在多管理員情境下不對 —— 新加的管理員會憑空獲得客服台 / 運維台。
    現在改成：

    - **系統第一個管理員**（`init_crm_db.py` 建立的那個）roles 直接給
      `[admin, agent, ops]`，也就是「預設擁有全部身分」。
    - **後續新增的管理員**預設只有 `[admin]`；要有客服 / 運維介面，
      **必須由既有管理員在員工編輯裡授權**（勾選角色）。

    其他帳號 = 管理員勾選的角色（本來就是）。

    展開後順序固定 admin → agent → ops，**第一個即登入時的預設身分**。
    """
    return [r for r in ALL_ROLES if r in set(roles or [])]


def roles_all(sess: dict | None) -> list[str]:
    """該帳號**全部**可用身分（登入時的角色快照；admin 已展開為三種）。"""
    return list((sess or {}).get("roles") or [])


def active_role(sess: dict | None) -> str | None:
    """當前登入身分。舊 session 沒有 active_role 時退回角色清單第一個。"""
    r = (sess or {}).get("active_role")
    if r:
        return r
    allr = roles_all(sess)
    return allr[0] if allr else None


def roles_of(sess: dict | None) -> list[str]:
    """★ 當前**生效**角色 —— 一次只會有一個身分生效，所以回傳 0 或 1 個元素。

    全部權限判定都走這裡（`is_admin` / `is_agent` / `can_*`），
    因此「選了哪個身分」直接決定這次登入能看到什麼。
    """
    r = active_role(sess)
    return [r] if r else []


# ---- 身分 → 首頁 ----

ROLE_HOME = {ROLE_ADMIN: "/admin", ROLE_AGENT: "/agent", ROLE_OPS: "/ops"}
ROLE_LABEL = {ROLE_ADMIN: "管理員", ROLE_AGENT: "客服坐席", ROLE_OPS: "運維"}


def home_path_for_role(role: str | None) -> str:
    return ROLE_HOME.get(role or "", "/login")


def home_path(sess: dict | None) -> str:
    return home_path_for_role(active_role(sess))


def can_switch_role(sess: dict | None) -> bool:
    """★ 只有「一帳號多身分」才顯示切換入口。"""
    return len(roles_all(sess)) > 1


def staff_roles_from_db(staff_id: int | None) -> list[str]:
    """讀該員工最新角色（切換身分時用 —— 不信 session 快照）。

    員工不存在或已停用 → 回空清單。
    """
    if not staff_id:
        return []
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT roles FROM crm_staff WHERE id = %s AND enabled = true",
                (staff_id,),
            )
            row = cur.fetchone()
            return list(row[0] or []) if row else []
    finally:
        conn.close()


def is_admin(sess: dict | None) -> bool:
    return ROLE_ADMIN in roles_of(sess)


def is_agent(sess: dict | None) -> bool:
    return ROLE_AGENT in roles_of(sess)


def is_ops(sess: dict | None) -> bool:
    return ROLE_OPS in roles_of(sess)


def is_super_agent(sess: dict | None) -> bool:
    """超級坐席 —— 跳過商家白名單，可訪問全部商家。

    ★ admin 一律視為超級坐席（管理員能看全部商家的會話）。
    """
    if ROLE_ADMIN in roles_all(sess):
        return True
    return bool((sess or {}).get("is_super_agent"))


def is_kb_editor(sess: dict | None) -> bool:
    """可編輯 FAQ / 表格 / 知識庫內容、建刪 collection（需同時是 agent）。"""
    return is_agent(sess) and bool((sess or {}).get("is_kb_editor"))


# ---- 功能開關（對應設計案 §8.2 權限對照表）----

def can_view_conversation(sess: dict | None) -> bool:
    """看會話 / 回覆：agent（ops 單獨不行，複合 agent 可以）。"""
    return is_agent(sess)


def can_configure_binding(sess: dict | None) -> bool:
    """配置啟用集（勾知識庫/表格/FAQ、貼文特殊配置）：agent（有該商家權限）。"""
    return is_agent(sess)


def can_edit_kb_content(sess: dict | None) -> bool:
    return is_kb_editor(sess)


def can_manage_people_and_business(sess: dict | None) -> bool:
    """員工 / 商家 / 平台帳號 / AI 開關：只有 admin。"""
    return is_admin(sess)


def can_manage_ops(sess: dict | None) -> bool:
    """重試參數 / 失敗任務：ops。"""
    return is_ops(sess)


def can_view_audit(sess: dict | None) -> bool:
    """審計日誌：admin 全部；ops 只看運維相關（由查詢層過濾）。"""
    return is_admin(sess) or is_ops(sess)


# ---- 商家範圍 ----

def allowed_business_ids(sess: dict | None) -> list[int]:
    """回傳該 session 可訪問的 business_id 清單。

    ★ is_super_agent = True 時回傳 `None` 語意（全部）—— 用 `None` 表示不限，
    呼叫端一律用 `can_access_business()` 判斷，不要自己比對 list。
    """
    if is_super_agent(sess):
        return []
    sid = (sess or {}).get("staff_id")
    if not sid:
        return []
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT business_id FROM crm_staff_business_permission WHERE staff_id = %s",
                (sid,),
            )
            return [r["business_id"] for r in cur.fetchall()]
    finally:
        conn.close()


def can_access_business(sess: dict | None, business_id: int | None) -> bool:
    """有無該商家權限。super_agent 一律 True。"""
    if business_id is None:
        return False
    if is_super_agent(sess):
        return True
    if not is_agent(sess):
        return False
    return int(business_id) in allowed_business_ids(sess)
