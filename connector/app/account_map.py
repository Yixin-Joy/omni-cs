"""帳號映射快取 —— 對齊鍵 `(platform, platform_business_account_id)`（MIGRATION_PLAN §1.2）。

這是 Connector 唯一的「CRM 概念」快取：靠它決定「這則來訊屬於哪個商家、要不要落地」。
真相在 CRM 的 `crm_business_platform_account`，這裡只是同步來的副本。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core import db

log = logging.getLogger("connector")

_SYNC_KEY = "account_map"

# 每個平台的業務憑證鍵（存進 `credential` JSONB 的欄位名）。
# ★ 握手 token（WECHAT_TOKEN / *_VERIFY_TOKEN / META_APP_SECRET）**不進 DB** ——
#   它們是「Connector 實例 vs 平台」的驗證身分，不是「商家 vs 平台」的業務憑證（見 MIGRATION_PLAN §3）。
#   進 DB 的只有「真正按商家區分」的憑證（appsecret / access_token / aes_key / page id 等）。
CREDENTIAL_KEYS = {
    "wechat": ("appid", "appsecret", "encoding_aes_key"),
    "whatsapp": ("access_token", "phone_number_id"),
    "fb": ("page_id", "page_access_token"),
    "ig": ("ig_user_id", "ig_access_token"),
}


def credential_of(platform: str, native_id: str) -> dict:
    """回傳某帳號的業務憑證（可能為空 dict）。收發時用，取代讀 config。"""
    acc = lookup(platform, native_id)
    return (acc or {}).get("credential") or {}


def lookup(platform: str, native_id: str) -> dict | None:
    """查單一帳號（收訊時用）。沒命中代表「未註冊」→ 呼叫端應丟棄訊息並上報告警。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, platform_business_account_id, business_id, bp_account_id,"
                "       enabled, credential"
                "  FROM connector_account_map"
                " WHERE platform = %s AND platform_business_account_id = %s",
                (platform, native_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "platform": row[0],
                "platform_business_account_id": row[1],
                "business_id": row[2],
                "bp_account_id": row[3],
                "enabled": bool(row[4]),
                "credential": row[5] or {},
            }
    finally:
        conn.close()


def upsert_many(items: list[dict]) -> int:
    """冪等 upsert。回傳處理筆數。"""
    if not items:
        return 0
    rows = []
    for it in items:
        plat = (it.get("platform") or "").strip()
        native = (it.get("platform_business_account_id") or "").strip()
        if not plat or not native:
            continue
        cred = it.get("credential")
        rows.append((plat, native, it.get("business_id"), it.get("bp_account_id"),
                     bool(it.get("enabled", True)),
                     json.dumps(cred, ensure_ascii=False) if cred else None))
    if not rows:
        return 0
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO connector_account_map"
                " (platform, platform_business_account_id, business_id, bp_account_id,"
                "  enabled, credential, synced_at)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())"
                " ON CONFLICT (platform, platform_business_account_id) DO UPDATE SET"
                "  business_id = EXCLUDED.business_id, bp_account_id = EXCLUDED.bp_account_id,"
                "  enabled = EXCLUDED.enabled, credential = EXCLUDED.credential,"
                "  synced_at = now()",
                rows,
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def replace_all(items: list[dict]) -> int:
    """全量同步：CRM 已刪/停用的帳號，本地也要跟著消失（避免殘留生效）。

    做法：先 upsert，再刪掉「這批清單以外」的列。
    """
    n = upsert_many(items)
    plats = [it.get("platform") for it in items
             if it.get("platform") and it.get("platform_business_account_id")]
    natives = [it.get("platform_business_account_id") for it in items
               if it.get("platform") and it.get("platform_business_account_id")]
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if plats:
                # ★ 用 unnest 展開兩欄鍵 —— psycopg2 不能把「tuple 清單」直接當陣列綁定
                cur.execute(
                    "DELETE FROM connector_account_map m"
                    " WHERE NOT EXISTS ("
                    "   SELECT 1 FROM unnest(%s::text[], %s::text[]) AS k(platform, native_id)"
                    "    WHERE k.platform = m.platform"
                    "      AND k.native_id = m.platform_business_account_id)",
                    (plats, natives),
                )
            else:
                cur.execute("DELETE FROM connector_account_map")
        conn.commit()
    finally:
        conn.close()
    return n


def mark_synced(error: str | None = None) -> None:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connector_sync_state (key, value, updated_at)"
                " VALUES (%s, %s::jsonb, now())"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (_SYNC_KEY, json.dumps({"error": error})),
            )
        conn.commit()
    finally:
        conn.close()


def last_sync_at():
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM connector_sync_state WHERE key = %s", (_SYNC_KEY,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def stale_seconds() -> float | None:
    """距離上次成功同步幾秒；從未同步過回 None。"""
    ts = last_sync_at()
    if ts is None:
        return None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - ts).total_seconds()
