"""審計日誌 —— 只插入，永不更新／刪除（設計案 §5.4 / §15.6）。

`log()` 收 cursor，讓呼叫端與業務寫入共用同一事務（要嘛都成功、要嘛都不寫）。
"""
from __future__ import annotations

import json

from app.core import db

# 操作類型（設計案 §15.5）
OPERATE_TYPES = {
    # 人員 / 權限
    "staff_create", "staff_update", "staff_disable", "staff_role_change",
    "staff_password_reset", "business_create", "business_update", "business_disable",
    "staff_business_permission_grant", "staff_business_permission_revoke",
    "platform_account_bind", "platform_account_unbind", "ai_switch_change",
    # 知識庫
    "kb_collection_create", "kb_collection_update", "kb_collection_delete",
    "kb_document_upload", "kb_document_delete", "kb_document_reparse",
    "kb_asset_delete", "kb_asset_caption_edit",
    "faq_create", "faq_update", "faq_delete",
    "business_table_create", "business_table_update", "business_table_delete",
    "post_ai_config_change", "binding_change",
    # 客服操作
    "reply_send", "message_handle", "conversation_read",
    # 認證
    "login", "logout", "force_logout", "login_failed", "role_switch",
}


def log(cur, sess: dict | None, operate_type: str, *, business_id=None, platform=None,
        bp_account_id=None, end_user_sender_id=None, payload=None) -> None:
    """寫一條審計。`sess` 可為 None（例如登入失敗時）。"""
    sess = sess or {}
    cur.execute(
        "INSERT INTO crm_audit_log"
        " (business_id, staff_id, staff_name, staff_roles, operate_type,"
        "  platform, business_platform_account_id, end_user_sender_id, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        (
            business_id,
            sess.get("staff_id"),
            sess.get("staff_name"),
            list(sess.get("roles") or []) or None,
            operate_type,
            platform,
            bp_account_id,
            end_user_sender_id,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )


def log_standalone(sess: dict | None, operate_type: str, **kw) -> None:
    """獨立寫一條（自開連線；登入失敗等沒有事務的場合用）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            log(cur, sess, operate_type, **kw)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
