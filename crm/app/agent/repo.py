"""客服工作台資料層（CRM 側）—— 商家範圍、帳號、人工回覆建任務。

★ 跨庫一律走內網 API（`app.internal.connector_client`），**不碰 fdw**（設計案 §7.1）。
★ 每次查詢／回覆都由 router 先做 RBAC + 商家權限校驗，這裡只負責資料。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg2.extras

from app import audit
from app.core import db
from app.internal import repo as internal_repo


@contextmanager
def tx():
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def ro():
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------- 商家 / 帳號

def list_businesses() -> list[dict]:
    with ro() as cur:
        cur.execute(
            "SELECT b.id, b.business_name, b.enabled FROM crm_business b"
            " WHERE b.enabled IS NOT FALSE ORDER BY b.id"
        )
        return [dict(r) for r in cur.fetchall()]


def list_accounts(business_id: int, platform: str = "") -> list[dict]:
    """該商家的平台帳號（工作台左側「渠道」切換用）。"""
    sql = ("SELECT id, business_id, platform, platform_business_account_id, display_name,"
           "       enabled, ai_auto_reply_enabled"
           "  FROM crm_business_platform_account WHERE business_id = %s AND enabled IS NOT FALSE")
    args: list = [int(business_id)]
    if platform:
        sql += " AND platform = %s"
        args.append(platform)
    sql += " ORDER BY platform, id"
    with ro() as cur:
        cur.execute(sql, tuple(args))
        return [dict(r) for r in cur.fetchall()]


def tasks_status(task_ids: list[int], business_id: int) -> list[dict]:
    """批次查外送任務狀態（工作台「送出後即時反饋」用）。

    ⚠️ 一定要帶 `business_id` 條件 —— 任務 id 是自增的，不擋就能拿別家商家的
      任務狀態（雖然只是狀態，也是越權）。
    """
    ids = [int(i) for i in (task_ids or [])][:20]
    if not ids:
        return []
    with ro() as cur:
        cur.execute(
            "SELECT id, status, fail_reason, finished_at FROM crm_outbound_task"
            " WHERE id = ANY(%s) AND business_id = %s",
            (ids, int(business_id)),
        )
        return [{
            "task_id": r["id"],
            "status": r["status"],
            "fail_reason": r["fail_reason"],
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        } for r in cur.fetchall()]


def ai_switch_row(business_id: int, platform: str, scope: str) -> bool:
    """該商家（平台×場景）的 AI 總開關（工作台顯示用）。"""
    with ro() as cur:
        cur.execute(
            "SELECT enabled FROM business_ai_switch"
            " WHERE business_id = %s AND platform = %s AND scope = %s",
            (int(business_id), platform, scope),
        )
        row = cur.fetchone()
        return bool(row and row["enabled"])


# ---------------------------------------------------------------- 人工回覆

def create_manual_reply(sess: dict, *, business_id: int, platform: str, bp_account_id: int,
                        sender_id: str, text: str = "", assets: list[dict] | None = None,
                        extend_history: bool = False, scope: str = "dm") -> dict:
    """建一筆人工回覆任務（設計案 §10.2 步驟 2）——**建任務與審計同一事務**。

    Connector 之後靠 claim 認領送出。回傳 `{task_id}`。

    `scope='dm'`（預設）＝私訊回覆；`scope='comment'`＝貼文留言回覆
    （此時 `sender_id` 是 `comment_id`，Connector 走 Graph API 留言端點）。
    """
    cfg, _ = internal_repo.ops_config()
    mr = int(cfg.get("outbound.max_retry", 3))
    bs = int(cfg.get("outbound.backoff_seconds", 1))
    # ★ 只保留 Connector 認得的欄位，並且**一定要把 filename / mime 帶下去**：
    #   Connector 存檔後 `media_key` 只剩 `<sha16>.<ext>`，原始檔名只有這裡有。
    #   少了它 → WhatsApp 的 document 顯示 sha 檔名、工作台氣泡也只有 sha，
    #   而且 Connector 判斷「是不是圖」只能靠副檔名（PDF 會被當成圖 → 破圖）。
    clean_assets = []
    for a in (assets or []):
        if not isinstance(a, dict) or not a.get("media_key"):
            continue
        clean_assets.append({
            "media_key": str(a["media_key"]).strip(),
            "filename": str(a.get("filename") or "").strip(),
            "mime": str(a.get("mime") or "").strip(),
            "caption": str(a.get("caption") or "").strip(),
        })
    content = {
        "text": (text or "").strip(),
        "assets": clean_assets,
        "source": "manual",
        "scope": scope,
    }
    with tx() as cur:
        cur.execute(
            "INSERT INTO crm_outbound_task"
            " (business_id, staff_id, source, platform, business_platform_account_id,"
            "  end_user_sender_id, message_content, status, max_retry, backoff_seconds)"
            " VALUES (%s, %s, 'manual', %s, %s, %s, %s::jsonb, 'pending', %s, %s)"
            " RETURNING id",
            (int(business_id), sess.get("staff_id"), platform, int(bp_account_id),
             sender_id, json.dumps(content, ensure_ascii=False), mr, bs),
        )
        task_id = cur.fetchone()["id"]
        audit.log(cur, sess, "reply_send", business_id=business_id, platform=platform,
                  bp_account_id=bp_account_id, end_user_sender_id=sender_id,
                  payload={"task_id": task_id, "text": content["text"],
                           "assets": len(content["assets"]),
                           "extend_history": bool(extend_history)})
    return {"task_id": task_id}


# ---------------------------------------------------------------- 該會話的 AI 留痕

def conversation_logs(business_id: int, platform: str, sender_id: str,
                      limit: int = 20) -> list[dict]:
    """該會話的自動回覆紀錄（工作台在對話旁顯示「這句 AI 怎麼處理的」）。

    ⚠️ `score` 一定要選出來 —— 前端 `renderConv()` 的 AI 留痕會顯示它
    （`前端渲染自檢` 抓到的：少了這一欄，那段判斷式永遠不成立，變成死碼）。
    """
    with ro() as cur:
        cur.execute(
            "SELECT id, tier, scope, question, answer, score, send_status, detail,"
            "       has_matched_rows, created_at"
            "  FROM (SELECT *, (matched_rows IS NOT NULL) AS has_matched_rows"
            "          FROM autoreply_log"
            "         WHERE business_id = %s AND platform = %s AND end_user_sender_id = %s"
            "         ORDER BY id DESC LIMIT %s) t"
            " ORDER BY id",
            (int(business_id), platform, sender_id, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]


def business_summary(business_id: int) -> dict:
    """工作台抬頭要用的幾個計數（知識庫 / FAQ / 表格）。"""
    with ro() as cur:
        cur.execute(
            "SELECT"
            "  (SELECT count(*) FROM kb_collection WHERE business_id = %s AND enabled) AS kb,"
            "  (SELECT count(*) FROM autoreply_faq WHERE business_id = %s AND enabled) AS faq,"
            "  (SELECT count(*) FROM autoreply_business_table WHERE business_id = %s AND enabled) AS tbl",
            (int(business_id), int(business_id), int(business_id)),
        )
        return dict(cur.fetchone())


# ---------------------------------------------------------------- 貼文特殊配置（§9.3）

# 三組貼文綁定表（與 `autoreply_db.POST_BINDING_SPECS` 同一組表，這裡只做讀寫）
POST_BINDING_TABLES = {
    "faq": ("post_faq_binding", "faq_id"),
    "table": ("post_table_binding", "business_table_id"),
    "kb": ("post_kb_binding", "kb_collection_id"),
}


def get_post_config(platform: str, post_id: str) -> dict | None:
    with ro() as cur:
        cur.execute(
            "SELECT id, platform, post_id, business_platform_account_id, enabled, configured,"
            "       kb_enabled, table_enabled, faq_enabled, updated_by, updated_at"
            "  FROM post_ai_config WHERE platform = %s AND post_id = %s",
            (platform, post_id),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    out = dict(rows[0])
    v = out.get("updated_at")
    if v is not None and hasattr(v, "isoformat"):
        out["updated_at"] = v.isoformat()
    return out


def post_binding_ids(post_config_id: int, kind: str) -> list[int]:
    table, col = POST_BINDING_TABLES[kind]
    with ro() as cur:
        cur.execute(f"SELECT {col} AS id FROM {table}"
                    " WHERE post_config_id = %s AND enabled ORDER BY id",
                    (int(post_config_id),))
        return [int(r["id"]) for r in cur.fetchall()]


def save_post_config(sess: dict, *, business_id: int, platform: str, bp_account_id: int,
                     post_id: str, enabled: bool, layers: dict, bindings: dict) -> dict:
    """★ 儲存貼文配置 —— **配置與三組綁定同一事務**（半套寫入比沒寫更糟）。

    `configured` 一律寫 True：走到這裡就是「人在彈窗裡按了儲存」，
    之後這則貼文完全按自己的儲存跑，不再 fallback 回私訊基準集（§9.2）。

    ⚠️ `layers` 的知識庫鍵在兩邊叫法不同：`resolve_scope_set()` 用 `rag`，
      貼文表欄位叫 `kb_enabled`。這裡兩個都吃，避免其中一邊默默變成預設值。
    """
    kb_on = bool(layers.get("kb", layers.get("rag", True)))
    with tx() as cur:
        cur.execute(
            "INSERT INTO post_ai_config"
            " (platform, post_id, business_platform_account_id, enabled, configured,"
            "  kb_enabled, table_enabled, faq_enabled, updated_by, updated_at)"
            " VALUES (%s, %s, %s, %s, true, %s, %s, %s, %s, now())"
            " ON CONFLICT (platform, post_id) DO UPDATE SET"
            "   business_platform_account_id = EXCLUDED.business_platform_account_id,"
            "   enabled = EXCLUDED.enabled, configured = true,"
            "   kb_enabled = EXCLUDED.kb_enabled, table_enabled = EXCLUDED.table_enabled,"
            "   faq_enabled = EXCLUDED.faq_enabled, updated_by = EXCLUDED.updated_by,"
            "   updated_at = now()"
            " RETURNING id",
            (platform, post_id, int(bp_account_id) if bp_account_id else None,
             bool(enabled), kb_on, bool(layers.get("table", True)),
             bool(layers.get("faq", True)), sess.get("staff_id")),
        )
        pid = cur.fetchone()["id"]
        for kind, (table, col) in POST_BINDING_TABLES.items():
            ids = sorted({int(i) for i in (bindings.get(kind) or [])})
            # 覆蓋式：先清掉這則貼文在該組的舊綁定，再寫入新的。
            cur.execute(f"DELETE FROM {table} WHERE post_config_id = %s", (pid,))
            for i in ids:
                cur.execute(f"INSERT INTO {table} (post_config_id, {col}, enabled)"
                            f" VALUES (%s, %s, true) ON CONFLICT DO NOTHING", (pid, i))
    return {"post_config_id": pid}


def delete_post_config(platform: str, post_id: str) -> bool:
    """移除該貼文的特殊配置 → 之後回到「與私訊相同」的預設行為。"""
    with tx() as cur:
        cur.execute("SELECT id FROM post_ai_config WHERE platform=%s AND post_id=%s",
                    (platform, post_id))
        rows = cur.fetchall()
        if not rows:
            return False
        pid = rows[0]["id"]
        for table, _col in POST_BINDING_TABLES.values():
            cur.execute(f"DELETE FROM {table} WHERE post_config_id = %s", (pid,))
        cur.execute("DELETE FROM post_ai_config WHERE id = %s", (pid,))
    return True
