"""審計日誌 —— 只插入，永不更新／刪除（設計案 §5.4 / §15.6）。

`log()` 收 cursor，讓呼叫端與業務寫入共用同一事務（要嘛都成功、要嘛都不寫）。

★★ 記錄原則（2026-10-20 小jo 重新定義）：
  「非必要的操作記錄直接選擇不計」「某個重要動作是由誰、以什麼身份做的就可以」
  → 只記**會改變系統狀態或影響客戶可見結果**的動作。
  **不記**：
    · `conversation_read`（每點開一個會話就一條，量最大、排查價值最低）
    · `role_switch`（身分切換本身不是業務動作；「當時用什麼身分」已寫進每條的
      `staff_role`，要追身分看那一欄就夠）
  保留登入/登入失敗/強制登出 —— 那是安全事件，不是操作噪音。
"""
from __future__ import annotations

import json

from app.core import db

# 操作類型（設計案 §15.5）
OPERATE_TYPES = {
    # 人員 / 權限
    "staff_create", "staff_update", "staff_disable", "staff_role_change",
    "staff_password_reset", "staff_password_change", "business_create", "business_update",
    "business_disable",
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
    "reply_send", "message_handle",
    # 認證（安全事件，保留）
    "login", "logout", "force_logout", "login_failed",
}


def log(cur, sess: dict | None, operate_type: str, *, business_id=None, platform=None,
        bp_account_id=None, end_user_sender_id=None, payload=None,
        fail_reason: str | None = None) -> None:
    """寫一條審計。`sess` 可為 None（例如登入失敗時）。

    `fail_reason`：**失敗才傳**。提到獨立欄位（不是塞在 payload）才能直接用 SQL 篩，
    列表也才能一眼看出「這筆失敗了」。
    """
    sess = sess or {}
    cur.execute(
        "INSERT INTO crm_audit_log"
        " (business_id, staff_id, staff_name, staff_roles, staff_role, operate_type,"
        "  platform, business_platform_account_id, end_user_sender_id, payload, fail_reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
        (
            business_id,
            sess.get("staff_id"),
            sess.get("staff_name"),
            list(sess.get("roles") or []) or None,
            # ★ 當時生效的身分（沒有就用 roles 的第一個，最後才 NULL）
            sess.get("active_role") or (list(sess.get("roles") or []) or [None])[0],
            operate_type,
            platform,
            bp_account_id,
            end_user_sender_id,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            (fail_reason or None),
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
