"""Connector 內網 API 端點 —— 供 CRM 呼叫（設計案 §4.2）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import account_map, media
from app.core import config
from app.core import db
from app.internal.auth import require_internal

router = APIRouter(
    prefix="/internal/api",
    tags=["internal"],
    dependencies=[Depends(require_internal)],
)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": msg})


# ---------------------------------------------------------------- 帳號映射

class AccountMapIn(BaseModel):
    items: list[dict] = []


@router.post("/account-map/sync")
def account_map_sync(payload: AccountMapIn):
    """收 CRM 主動推送的映射（冪等 upsert）。"""
    items = payload.items or []
    n = account_map.upsert_many(items)
    account_map.mark_synced()
    return {"ok": True, "upserted": n}


@router.get("/account-map")
def account_map_list():
    """本地快取現況（除錯用）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT platform, platform_business_account_id, business_id, bp_account_id,"
                "       enabled, synced_at FROM connector_account_map"
                " ORDER BY platform, platform_business_account_id"
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    last = account_map.last_sync_at()
    return {
        "ok": True,
        "last_sync_at": last.isoformat() if last else None,
        "items": [
            {"platform": r[0], "platform_business_account_id": r[1], "business_id": r[2],
             "bp_account_id": r[3], "enabled": bool(r[4]),
             "synced_at": r[5].isoformat() if r[5] else None}
            for r in rows
        ],
    }


# ---------------------------------------------------------------- 媒體

@router.post("/media/upload")
async def media_upload(
    file: UploadFile = File(...),
    scope: str = Query(default="kb_cache"),
):
    """CRM 推二進制（知識庫圖片 / 客服附件）→ 存檔回 `media_key`。"""
    data = await file.read()
    if not data:
        return _err(400, "空檔案")
    try:
        res = media.save_bytes(scope, data, file.filename or "")
    except ValueError as e:
        return _err(400, str(e))
    return {"ok": True, **{k: v for k, v in res.items() if k != "path"}}


@router.get("/media/fetch")
def media_fetch(media_key: str = Query(...)):
    """CRM 代理取客戶來訊媒體 / 快取圖片。"""
    try:
        p = media.resolve(media_key)
    except ValueError as e:
        return _err(400, str(e))
    except FileNotFoundError:
        return _err(404, "找不到媒體")
    return FileResponse(str(p), filename=p.name)


# ---------------------------------------------------------------- 發送嘗試紀錄

def _attempts_of(task_id: int) -> list[dict]:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sub_seq, status, platform, target_id, platform_msg_id, error, attempted_at"
                "  FROM connector_outbound_attempt WHERE crm_task_id = %s ORDER BY sub_seq",
                (task_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"sub_seq": r[0], "status": r[1], "platform": r[2], "target_id": r[3],
         "platform_msg_id": r[4], "error": r[5],
         "attempted_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]


@router.get("/outbound-attempt")
def outbound_attempt(crm_task_id: int = Query(...)):
    """任務詳情 / ops 查看；CRM 逾時清理也靠這支判斷「到底發出去了沒」。"""
    return {"ok": True, "items": _attempts_of(int(crm_task_id))}


class BatchIn(BaseModel):
    task_ids: list[int] = []


@router.post("/outbound-attempt/batch")
def outbound_attempt_batch(payload: BatchIn):
    ids = [int(i) for i in (payload.task_ids or [])][:500]
    return {"ok": True, "items": {str(i): _attempts_of(i) for i in ids}}


# ---------------------------------------------------------------- 訊息查詢

def _msg_text(raw, payload) -> str:
    """統一取出來訊／回覆的純文字（forward_log 的 JSON 或 igfb_dm 的 text）。"""
    import json as _json

    def _pick(o):
        if isinstance(o, dict):
            v = o.get("messageContent")
            if v in (None, ""):
                v = o.get("text")
            return v if isinstance(v, str) else ""
        if isinstance(o, str):
            s = o.strip()
            if s[:1] in ("{", "["):
                try:
                    return _pick(_json.loads(s))
                except Exception:
                    return s
            return s
        return ""

    return (_pick(raw) or _pick(payload) or "").strip()


@router.get("/messages/query")
def messages_query(
    platform: str = Query(...),
    platform_business_account_id: str = Query(...),
    end_user_sender_id: str = Query(...),
    scope: str = Query(default="dm"),
    before: str | None = Query(default=None),
    limit: int = Query(default=50),
):
    """封裝跨庫查詢訊息（CRM 工作台渲染會話 / 引擎取對話上下文，設計案 §4.2）。

    回傳每筆都帶 `text`（已從 raw_content / forwarded_payload 正規化出的純文字），
    CRM 端不必再自己解析各家格式。
    """
    n = max(1, min(int(limit or 50), 200))
    if platform in ("wechat", "whatsapp"):
        sql = (
            "SELECT id, platform, direction, message_id, sender_id, message_type,"
            "       raw_content, forwarded_payload, message_time AS ts"
            "  FROM forward_log"
            " WHERE platform = %s AND platform_business_account_id = %s AND sender_id = %s"
            "   AND (%s IS NULL OR message_time < %s::timestamptz)"
            " ORDER BY message_time DESC LIMIT %s"
        )
        args = (platform, platform_business_account_id, end_user_sender_id, before, before, n)
    elif platform in ("ig", "fb"):
        sql = (
            "SELECT id, platform, direction, message_id, sender_id, message_type,"
            "       COALESCE(text, '') AS raw_content, NULL AS forwarded_payload, created_at AS ts"
            "  FROM igfb_dm"
            " WHERE platform = %s AND platform_business_account_id = %s"
            "   AND (sender_id = %s OR conversation_key = %s)"
            "   AND (%s IS NULL OR created_at < %s::timestamptz)"
            " ORDER BY created_at DESC LIMIT %s"
        )
        args = (platform, platform_business_account_id, end_user_sender_id,
                end_user_sender_id, before, before, n)
    else:
        return _err(400, f"不支援的平台: {platform}")

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "items": [
            {"id": r[0], "platform": r[1], "direction": r[2], "message_id": r[3],
             "sender_id": r[4], "message_type": r[5], "content": r[6],
             "text": _msg_text(r[6], r[7]),
             "ts": r[8].isoformat() if r[8] else None}
            for r in rows
        ],
    }


class MarkIn(BaseModel):
    platform: str
    message_ids: list[str] | None = None
    message_id: str | None = None
    comment_id: str | None = None
    conversation_id: str | None = None
    state: str | None = None
    tier: str | None = None
    detail: str | None = None
    handled: bool | None = None


@router.post("/messages/mark")
def messages_mark(payload: MarkIn):
    """CRM 標記訊息的自動回復處置（`auto_reply_state` / tier / detail / `is_handled`）。

    表歸屬：wechat / whatsapp → `forward_log`；ig / fb → `igfb_dm`（有 comment_id 則 `igfb_comment`）。
    """
    sets, args = [], []
    if payload.state is not None:
        sets.append("auto_reply_state=%s"); args.append(payload.state)
    if payload.tier is not None:
        sets.append("auto_reply_tier=%s"); args.append(payload.tier)
    if payload.detail is not None:
        sets.append("auto_reply_detail=%s"); args.append(payload.detail)
    if payload.handled is not None:
        sets.append("is_handled=%s"); args.append(bool(payload.handled))
    if not sets:
        return {"ok": True, "updated": 0}

    p = payload.platform
    if p not in ("wechat", "whatsapp", "ig", "fb"):
        return _err(400, f"不支援的平台: {p}")

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            if p in ("wechat", "whatsapp"):
                table = "forward_log"
                where, wargs = ["platform=%s"], [p]
                if payload.message_ids:
                    where.append("message_id = ANY(%s)"); wargs.append(list(payload.message_ids))
                if payload.message_id:
                    where.append("message_id=%s"); wargs.append(payload.message_id)
                if payload.conversation_id:
                    where.append("conversation_id=%s"); wargs.append(payload.conversation_id)
            else:
                if payload.comment_id:
                    table = "igfb_comment"
                    where, wargs = ["platform=%s", "comment_id=%s"], [p, payload.comment_id]
                else:
                    table = "igfb_dm"
                    where, wargs = ["platform=%s"], [p]
                    if payload.message_ids:
                        where.append("message_id = ANY(%s)"); wargs.append(list(payload.message_ids))
                    if payload.message_id:
                        where.append("message_id=%s"); wargs.append(payload.message_id)
            if len(where) == 1:
                return _err(400, "缺少 message_id / message_ids / comment_id / conversation_id")
            cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE " + " AND ".join(where),
                        tuple(args + wargs))
            n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "updated": n}


@router.get("/ping")
def ping():
    return {"ok": True, "service": config.SERVICE_NAME}
