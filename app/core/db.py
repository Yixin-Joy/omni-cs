"""轉發日誌寫入 PostgreSQL 的 forward_log 表。

寫入是非同步的、容錯的：失敗只記 log，不影響轉發主流程。
"""
import asyncio
import json
import logging

import psycopg2

from app.core import config

log = logging.getLogger("wechat-forwarder")


def _insert_sync(record: dict) -> None:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO forward_log (
                    platform, message_id, sender_id, conversation_id, message_type,
                    raw_content, forwarded_payload, forward_status,
                    target_http_status, target_response, error_message, message_time, direction
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.get("platform"),
                    record.get("message_id"),
                    record.get("sender_id"),
                    record.get("conversation_id"),
                    record.get("message_type"),
                    record.get("raw_content"),
                    json.dumps(record.get("forwarded_payload"), ensure_ascii=False),
                    record.get("forward_status"),
                    record.get("target_http_status"),
                    record.get("target_response"),
                    record.get("error_message"),
                    record.get("message_time"),
                    record.get("direction", "inbound"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


async def insert_forward_log(record: dict) -> None:
    """非同步包裝，用執行緒池避免阻塞事件循環。"""
    try:
        await asyncio.to_thread(_insert_sync, record)
    except Exception as e:
        log.warning("寫入轉發日誌失敗: %s", e)


def build_record(
    *,
    platform: str,
    payload: dict,
    raw_content: str,
    forward_status: str,
    direction: str = "inbound",
    target_http_status: int | None = None,
    target_response: str | None = None,
    error_message: str | None = None,
) -> dict:
    """從轉發 payload 組出一筆 forward_log 記錄。

    direction：inbound = 客戶來訊（客戶 → 中轉 → 本機 DB）；outbound = 我方回覆（工作台 → 中轉 → 客戶）。
    """
    sender = payload.get("webClientSender") or {}
    return {
        "platform": platform,
        "message_id": str(payload.get("id", "")),
        "sender_id": sender.get("webClientUUID", ""),
        "conversation_id": payload.get("conversationId", ""),
        "message_type": payload.get("messageType", ""),
        "raw_content": raw_content,
        "forwarded_payload": payload,
        "forward_status": forward_status,
        "target_http_status": target_http_status,
        "target_response": (target_response or "")[:1000],
        "error_message": (error_message or "")[:500] or None,
        "message_time": payload.get("updatedAt"),
        "direction": direction,
    }
