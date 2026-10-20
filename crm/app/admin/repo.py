"""管理員後台的資料存取 —— 全部走 CRM DB（唯一寫入者）。

`tx()` 提供「一個連線 + 一個 cursor + 自動 commit/rollback」的交易包裝；
審計日誌與業務寫入共用同一個 cursor，保證同生同死。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg2.extras

from app.core import config
from app.core import db

# AI 開關的合法組合（設計案 §9.1）：微信/WhatsApp 只有私訊，IG/FB 有私訊 + 留言
AI_SWITCH_SCOPES = list(config.AUTOREPLY_SCOPES)


@contextmanager
def tx():
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def ro():
    """唯讀查詢用（自動關閉）。"""
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------- 商家

def list_businesses() -> list[dict]:
    with ro() as cur:
        cur.execute(
            "SELECT b.id, b.business_name, b.business_memo, b.enabled, b.created_at,"
            "  b.meta_app_id,"
            # ★ secret 不回明文，只回「有沒有填」—— 前端只需要知道狀態
            "  (COALESCE(b.meta_app_secret, '') <> '') AS has_meta_app_secret,"
            "  (SELECT count(*) FROM crm_business_platform_account a"
            "    WHERE a.business_id = b.id AND a.enabled) AS account_count,"
            "  (SELECT count(*) FROM kb_collection c"
            "    WHERE c.business_id = b.id AND c.enabled) AS kb_count,"
            "  (SELECT count(*) FROM autoreply_faq f"
            "    WHERE f.business_id = b.id AND f.enabled) AS faq_count,"
            "  (SELECT count(*) FROM autoreply_business_table t"
            "    WHERE t.business_id = b.id AND t.enabled) AS table_count"
            " FROM crm_business b ORDER BY b.id"
        )
        return [dict(r) for r in cur.fetchall()]


def create_business(cur, name: str, memo: str | None,
                    meta_app_id: str | None = None,
                    meta_app_secret: str | None = None) -> int:
    cur.execute(
        "INSERT INTO crm_business (business_name, business_memo, meta_app_id, meta_app_secret)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (name, memo, meta_app_id, meta_app_secret),
    )
    return cur.fetchone()["id"]


def update_business(cur, bid: int, name: str, memo: str | None, enabled: bool,
                    meta_app_id: str | None = None,
                    meta_app_secret: str | None = None) -> None:
    """更新商家。`meta_app_*` 傳 `None` = **不動**（避免「只改名字」把 App 清掉）。"""
    sets = ["business_name = %s", "business_memo = %s", "enabled = %s", "updated_at = now()"]
    args: list = [name, memo, enabled]
    if meta_app_id is not None:
        sets.append("meta_app_id = %s")
        args.append(meta_app_id or None)
    if meta_app_secret is not None:
        sets.append("meta_app_secret = %s")
        args.append(meta_app_secret or None)
    args.append(bid)
    cur.execute("UPDATE crm_business SET " + ", ".join(sets) + " WHERE id = %s", tuple(args))


def business_exists(cur, bid: int) -> bool:
    cur.execute("SELECT 1 FROM crm_business WHERE id = %s", (bid,))
    return cur.fetchone() is not None


# ---------------------------------------------------------------- 平台帳號

def credential_view(platform: str, credential) -> tuple[list[str], dict[str, str]]:
    """把 credential 拆成「已填欄位清單」與「可安全回顯的明文值」。

    ★ 為什麼不全回、也不全遮：
      - **非敏感**欄位（`CREDENTIAL_FIELDS` 第三個元素為 False）＝ appid / page_id /
        phone_number_id / ig_user_id 這類**識別碼**，本來就出現在 webhook URL 與平台後台，
        明文回顯是安全的，也才不會讓管理員誤以為「憑證沒填」。
      - **敏感**欄位（appsecret / access_token / encoding_aes_key）**值一律不回前端**，
        只回「已填」的事實；前端用 `••••••••` 佔位。
    """
    cred = credential if isinstance(credential, dict) else {}
    filled: list[str] = []
    plain: dict[str, str] = {}
    for key, _label, sensitive in config.CREDENTIAL_FIELDS.get(platform, []):
        val = cred.get(key)
        if val is None or str(val).strip() == "":
            continue
        filled.append(key)
        if not sensitive:
            plain[key] = str(val)
    return filled, plain


def list_accounts(business_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT a.id, a.business_id, a.platform, a.platform_business_account_id, a.display_name,"
        "       a.ai_auto_reply_enabled, a.enabled, a.created_at, b.business_name, a.credential"
        "  FROM crm_business_platform_account a"
        "  LEFT JOIN crm_business b ON b.id = a.business_id"
    )
    params: tuple = ()
    if business_id is not None:
        sql += " WHERE a.business_id = %s"
        params = (business_id,)
    sql += " ORDER BY a.business_id, a.platform, a.id"
    with ro() as cur:
        cur.execute(sql, params)
        out = []
        for r in cur.fetchall():
            d = dict(r)
            cred = d.pop("credential", None)
            filled, plain = credential_view(d["platform"], cred)
            d["has_credential"] = bool(filled)
            d["credential_filled"] = filled      # 已填欄位的 key（永不泄露值）
            d["credential_plain"] = plain        # 僅非敏感欄位的明文
            out.append(d)
        return out


def create_account(cur, business_id: int, platform: str, native_id: str, display_name: str | None,
                   credential: dict | None = None) -> int:
    cur.execute(
        "INSERT INTO crm_business_platform_account"
        " (business_id, platform, platform_business_account_id, display_name, credential)"
        " VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
        (business_id, platform, native_id, display_name,
         json.dumps(credential, ensure_ascii=False) if credential else None),
    )
    return cur.fetchone()["id"]


def update_account(cur, aid: int, display_name: str | None, ai_enabled: bool, enabled: bool,
                   credential: dict | None = None) -> None:
    """更新平台帳號。`updated_at` 供 Connector 的帳號映射增量同步判斷變更（階段二）。

    ★★ credential 的語義（**逐欄合併，不是整塊覆蓋**）：
      - 傳 `None`  → 完全不動（呼叫端沒提供憑證）
      - 傳 dict    → 用 JSONB `||` **只覆蓋有提供的 key**，其餘原封不動

    ⚠️ 這裡原本是 `credential = %s::jsonb`（整塊覆蓋）。因為前端編輯框對敏感欄位
      只顯示佔位符、不會回填原值，使用者「只想改 appsecret」就會把 appid 與
      aes_key 一起清成 NULL —— **靜默的憑證遺失**，而且收發會偷偷退回 .env，
      症狀是「憑證明明填過、卻像沒填」。改成合併後才對得上 UI 的「留空＝不改」。
    """
    if credential is None:
        cur.execute(
            "UPDATE crm_business_platform_account SET display_name = %s, ai_auto_reply_enabled = %s,"
            " enabled = %s, updated_at = now() WHERE id = %s",
            (display_name, ai_enabled, enabled, aid),
        )
    else:
        cur.execute(
            "UPDATE crm_business_platform_account SET display_name = %s, ai_auto_reply_enabled = %s,"
            " enabled = %s, credential = COALESCE(credential, '{}'::jsonb) || %s::jsonb,"
            " updated_at = now() WHERE id = %s",
            (display_name, ai_enabled, enabled,
             json.dumps(credential, ensure_ascii=False), aid),
        )


# ---------------------------------------------------------------- 員工

def list_staff() -> list[dict]:
    with ro() as cur:
        cur.execute(
            "SELECT s.id, s.login_account, s.staff_name, s.roles, s.is_super_agent,"
            "       s.is_kb_editor, s.enabled, s.created_at,"
            "  COALESCE((SELECT array_agg(p.business_id ORDER BY p.business_id)"
            "    FROM crm_staff_business_permission p WHERE p.staff_id = s.id), '{}') AS business_ids,"
            "  (SELECT count(*) FROM crm_session ss WHERE ss.staff_id = s.id) AS active_sessions"
            " FROM crm_staff s ORDER BY s.id"
        )
        return [dict(r) for r in cur.fetchall()]


def create_staff(cur, account: str, password_hash: str, name: str, roles: list[str],
                 is_super_agent: bool, is_kb_editor: bool) -> int:
    cur.execute(
        "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles,"
        " is_super_agent, is_kb_editor) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (account, password_hash, name, roles, is_super_agent, is_kb_editor),
    )
    return cur.fetchone()["id"]


def update_staff(cur, sid: int, name: str, roles: list[str], is_super_agent: bool,
                 is_kb_editor: bool, enabled: bool) -> None:
    cur.execute(
        "UPDATE crm_staff SET staff_name = %s, roles = %s, is_super_agent = %s, is_kb_editor = %s,"
        " enabled = %s, updated_at = now() WHERE id = %s",
        (name, roles, is_super_agent, is_kb_editor, enabled, sid),
    )


def set_password(cur, sid: int, password_hash: str) -> None:
    cur.execute(
        "UPDATE crm_staff SET password_hash = %s, updated_at = now() WHERE id = %s",
        (password_hash, sid),
    )


def get_staff(cur, sid: int) -> dict | None:
    cur.execute("SELECT * FROM crm_staff WHERE id = %s", (sid,))
    r = cur.fetchone()
    return dict(r) if r else None


def set_staff_permissions(cur, staff_id: int, business_ids: list[int]) -> None:
    """整批覆蓋該員工的商家授權（先刪後插，同一事務）。"""
    cur.execute("DELETE FROM crm_staff_business_permission WHERE staff_id = %s", (staff_id,))
    for bid in sorted(set(int(b) for b in business_ids)):
        cur.execute(
            "INSERT INTO crm_staff_business_permission (staff_id, business_id) VALUES (%s, %s)"
            " ON CONFLICT (staff_id, business_id) DO NOTHING",
            (staff_id, bid),
        )


# ---------------------------------------------------------------- AI 開關

def list_ai_switches(business_id: int) -> list[dict]:
    """回傳該商家全部 6 種「平台 × 場景」的開關（表裡沒有的預設 true）。"""
    with ro() as cur:
        cur.execute(
            "SELECT platform, scope, enabled FROM business_ai_switch WHERE business_id = %s",
            (business_id,),
        )
        saved = {(r["platform"], r["scope"]): r["enabled"] for r in cur.fetchall()}
    return [
        {
            "platform": p,
            "scope": s,
            "enabled": bool(saved.get((p, s), True)),
            "configured": (p, s) in saved,
        }
        for (p, s) in AI_SWITCH_SCOPES
    ]


def upsert_ai_switch(cur, business_id: int, platform: str, scope: str, enabled: bool) -> None:
    cur.execute(
        "INSERT INTO business_ai_switch (business_id, platform, scope, enabled)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (business_id, platform, scope)"
        " DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
        (business_id, platform, scope, enabled),
    )


# ---------------------------------------------------------------- 審計

def list_audit(*, business_id=None, staff_id=None, operate_type=None, limit: int = 100) -> list[dict]:
    sql = (
        "SELECT id, business_id, staff_id, staff_name, staff_roles, operate_type, platform,"
        "       business_platform_account_id, end_user_sender_id, payload, operate_at"
        "  FROM crm_audit_log WHERE 1=1"
    )
    params: list = []
    if business_id is not None:
        sql += " AND business_id = %s"
        params.append(business_id)
    if staff_id is not None:
        sql += " AND staff_id = %s"
        params.append(staff_id)
    if operate_type:
        sql += " AND operate_type = %s"
        params.append(operate_type)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(max(1, min(int(limit), 500)))
    with ro() as cur:
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]
