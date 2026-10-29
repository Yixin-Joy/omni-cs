"""Session 管理 —— 存在 CRM DB `crm_session`（設計案 §13），不用 Redis。

設計要點：
- Session 內**快照** roles / is_super_agent / is_kb_editor；admin 改角色時由 admin 模組
  `refresh_staff_sessions()` 就地 UPDATE，達到「即時生效」。
- 踢下線 = 直接 DELETE session 列。
- 每次請求比對 `crm_staff.enabled`，員工被停用即刻失效（不必等 admin 踢）。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from app.auth import rbac
from app.core import config
from app.core import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def create_session(staff: dict, active_role: str | None = None) -> tuple[str, str]:
    """建立 session，回傳 (session_id, csrf_token)。

    `roles` 存登入時的角色**全量快照**（＝該帳號可選的身分清單）；
    `active_role` 是這次登入**實際生效**的那一個身分。
    """
    sid = new_token(32)
    csrf = new_token(24)
    expires = _now() + timedelta(hours=config.SESSION_TTL_HOURS)
    # ★ 存的是「可選身分清單」：admin 角色會展開為 admin/agent/ops
    roles = rbac.expand_roles(staff.get("roles"))
    # 傳進來的身分必須在角色清單內，否則退回第一個（或 None）
    if active_role not in roles:
        active_role = roles[0] if roles else None
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_session"
                " (session_id, staff_id, roles, active_role, is_super_agent, is_kb_editor,"
                "  csrf_token, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    sid,
                    staff["id"],
                    roles,
                    active_role,
                    bool(staff.get("is_super_agent")),
                    bool(staff.get("is_kb_editor")),
                    csrf,
                    expires,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return sid, csrf


def load_session(sid: str | None) -> dict | None:
    """讀取有效 session（含員工最新 name / enabled）。無效或過期回 None。"""
    if not sid:
        return None
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT s.session_id, s.staff_id, s.roles, s.active_role,"
                "       s.is_super_agent, s.is_kb_editor,"
                "       s.csrf_token, s.expires_at, s.last_seen_at,"
                "       st.staff_name, st.login_account, st.enabled AS staff_enabled"
                "  FROM crm_session s JOIN crm_staff st ON st.id = s.staff_id"
                " WHERE s.session_id = %s",
                (sid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            expired = row["expires_at"] is not None and row["expires_at"] < _now()
            if expired or not row["staff_enabled"]:
                with conn.cursor() as cur2:
                    cur2.execute("DELETE FROM crm_session WHERE session_id = %s", (sid,))
                conn.commit()
                return None
            with conn.cursor() as cur3:
                cur3.execute(
                    "UPDATE crm_session SET last_seen_at = now() WHERE session_id = %s", (sid,)
                )
            conn.commit()
            out = dict(row)
            # 身分失效防護：admin 改過角色、active_role 可能已不在清單內 → 退回第一個
            roles = list(out.get("roles") or [])
            if out.get("active_role") not in roles:
                out["active_role"] = roles[0] if roles else None
            return out
    finally:
        conn.close()


def set_active_role(sid: str, role: str) -> bool:
    """切換當前登入身分（僅改 active_role，不換 session_id／csrf）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_session SET active_role = %s WHERE session_id = %s",
                (role, sid),
            )
            n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        conn.close()


def delete_session(sid: str | None) -> None:
    """登出。"""
    if not sid:
        return
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_session WHERE session_id = %s", (sid,))
        conn.commit()
    finally:
        conn.close()


def delete_staff_sessions(staff_id: int) -> int:
    """踢某人全部下線（管理員踢人 / 員工被停用）。回傳刪除數。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_session WHERE staff_id = %s", (staff_id,))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def delete_staff_sessions_except(staff_id: int, keep_sid: str | None) -> int:
    """踢掉某人的**其他** session，保留 `keep_sid` 這一個。回傳刪除數。

    ★ 自己改密碼時用（`POST /api/auth/password`）：改完不該把自己也踢出去
      （那會讓人以為「改失敗了」），但**其他裝置/瀏覽器的登入必須失效** ——
      改密碼的意義就在這裡（懷疑密碼外洩時，把別人的 session 清掉）。
    `keep_sid` 為空就退化成「全踢」（等同 delete_staff_sessions）。
    """
    sid = (keep_sid or "").strip()
    if not sid:
        return delete_staff_sessions(staff_id)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM crm_session WHERE staff_id = %s AND session_id <> %s",
                (int(staff_id), sid))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def refresh_staff_sessions(staff_id: int, roles: list[str], is_super_agent: bool, is_kb_editor: bool) -> int:
    """角色變更即時生效 —— 就地更新該員工所有 session 的快照（設計案 §13.3）。

    ★ 同時校正 active_role：若當前登入身分已被移除，自動退回角色清單的第一個
    （清單為空則設 NULL，該 session 之後會被 require_login 擋掉）。
    """
    # ★ 傳進來的是 DB 原始角色 → 先展開（admin 補齊 agent/ops）再寫快照
    roles = rbac.expand_roles(roles)
    fallback = roles[0] if roles else None
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_session SET roles = %s, is_super_agent = %s, is_kb_editor = %s,"
                "       active_role = CASE WHEN active_role = ANY(%s) THEN active_role ELSE %s END"
                " WHERE staff_id = %s",
                (roles, bool(is_super_agent), bool(is_kb_editor), roles, fallback, staff_id),
            )
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def purge_expired() -> int:
    """清理過期 session（定時任務呼叫）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_session WHERE expires_at < now()")
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()
