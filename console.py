"""客服工作台後台：給客服工作台 UI 用的查詢 + 回覆接口。

端點（都掛在 /console 之下）：
  GET  /console                       客服工作台 UI 頁面
  GET  /console/conversations         某平台下、以用戶為單位的會話列表（含未回覆數）
  GET  /console/conversation/{platform}/{conversation_id}/messages  完整聊天串
  POST /console/reply                 回覆（文字 / 檔案 / 圖片，multipart）

「未回覆」的定義：該用戶最後一筆客戶來訊（inbound）之後、還沒有任何我方回覆（outbound）。
即 unread = 時間晚於最後一筆 outbound 的 inbound 筆數（沒有 outbound 時就是全部 inbound）。
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import psycopg2
from fastapi import APIRouter, File, Form, Response, UploadFile
from fastapi.responses import HTMLResponse

import config
import db
import wechat_send
import whatsapp_send
from wechat import openid_to_conversation_id

log = logging.getLogger("wechat-forwarder")

router = APIRouter(prefix="/console", tags=["console"])

_UI_PATH = Path(__file__).parent / "console_ui.html"

# 允許的 media 副檔名白名單（避免寫入奇怪檔案名）
_SAFE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".doc", ".docx", ".xls",
    ".xlsx", ".ppt", ".pptx", ".txt", ".csv", ".mp4", ".mp3", ".amr", ".ogg",
    ".zip", ".json", ".xml", ".md",
}


# ---------- DB ----------

def _query(sql: str, params: tuple = ()) -> list[dict]:
    """同步查詢，回傳 list[dict]。"""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> None:
    """同步執行 UPDATE/INSERT/DELETE（無回傳值）。"""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


async def _rows_for_platform(platform: str) -> list[dict]:
    sql = """
        SELECT id, conversation_id, sender_id, direction, message_type,
               forwarded_payload, raw_content, forward_status, message_time, created_at,
               is_handled, auto_reply_state, auto_reply_tier, auto_reply_detail
        FROM forward_log
        WHERE platform = %s AND conversation_id IS NOT NULL AND conversation_id <> ''
        ORDER BY created_at ASC
    """
    return await asyncio.to_thread(_query, sql, (platform,))


# ---------- 正規化 ----------

def _public_to_local(url: str) -> str:
    """把指向 PUBLIC_BASE_URL 的外部 url 轉成同源 /files/ 路徑，方便本機 UI 載入。"""
    if not url:
        return ""
    base = config.PUBLIC_BASE_URL.rstrip("/")
    if base and url.startswith(base):
        rel = url[len(base):].lstrip("/")
        return "/" + rel
    return url


def _norm_file(f: dict) -> dict:
    mime = (f.get("mimeType") or "").lower()
    url = f.get("url", "") or ""
    return {
        "url": _public_to_local(url),
        "filename": f.get("filename", ""),
        "mimeType": mime,
        "fileSize": f.get("fileSize", 0),
        "isImage": mime.startswith("image/") or url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")),
    }


def _row_to_message(row: dict) -> dict:
    """把一筆 forward_log 正規化成 UI 可顯示的 message dict。"""
    payload = row.get("forwarded_payload")
    if not isinstance(payload, dict):
        payload = {}
    direction = row.get("direction", "inbound")
    text = ""
    files: list[dict] = []

    if direction == "outbound":
        # outbound 的完整內容（含檔案）記在 raw_content（回覆請求的 JSON）
        try:
            rc = json.loads(row.get("raw_content") or "{}")
        except Exception:
            rc = {}
        # raw_content 不一定合法／一定是物件（歷史上自動回復曾寫入純文字，
        # 若內容剛好是純數字如 "1280"，json.loads 會回 int）。這裡一律收斂成 dict，
        # 避免一筆壞資料讓整個會話列表 500。
        if not isinstance(rc, dict):
            rc = {}
        text = rc.get("messageContent", "") or payload.get("messageContent", "")
        src_files = (rc.get("uploadedFiles") or []) or (payload.get("uploadedFiles") or [])
        for f in src_files:
            files.append(_norm_file(f))
    else:
        text = payload.get("messageContent", "")
        for f in payload.get("uploadedFiles") or []:
            files.append(_norm_file(f))

    mtype = (row.get("message_type") or "").lower()
    if not mtype:
        mtype = "file" if files else "text"

    return {
        "id": row["id"],
        "direction": direction,
        "type": mtype,
        "text": text,
        "files": files,
        "time": _iso(row.get("message_time") or row.get("created_at")),
        "status": row.get("forward_status"),
        # AI 自動回復標記（未命中 / FAQ命中 / 表格命中 / 發送失敗）
        "auto_reply_state": row.get("auto_reply_state") or "",
        "auto_reply_tier": row.get("auto_reply_tier") or "",
        "auto_reply_detail": row.get("auto_reply_detail") or "",
    }


def _iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


def _preview(msg: dict) -> str:
    """會話列表用的一句預覽。圖文結合優先顯示 caption 文字。"""
    if msg["files"]:
        f = msg["files"][0]
        if msg["text"]:
            return msg["text"]
        if f["isImage"]:
            return "[圖片]"
        return f"[檔案] {f['filename'] or '未命名'}"
    return msg["text"] or "[空訊息]"


def _sender_name(rows: list[dict]) -> str:
    """取該用戶最新的 inbound payload 裡的顯示名稱，找不到就回退 sender_id。"""
    for row in reversed(rows):
        if row.get("direction") != "inbound":
            continue
        payload = row.get("forwarded_payload")
        if not isinstance(payload, dict):
            continue
        sender = payload.get("webClientSender") or {}
        if isinstance(sender, dict) and sender.get("name"):
            return sender["name"]
    for row in reversed(rows):
        if row.get("sender_id"):
            return row["sender_id"]
    return "未知用戶"


def _group_conversations(rows: list[dict]) -> list[dict]:
    """把一平台的所有列分組到以用戶為單位的會話。"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["conversation_id"], []).append(r)

    out = []
    for cid, rs in groups.items():
        # 未讀 = inbound 且 is_handled=false 的訊息數
        unread = sum(1 for r in rs
                     if r.get("direction") == "inbound" and not r.get("is_handled"))
        # 自動回復未命中且尚未處理 → 列表要標出來
        auto_miss = sum(1 for r in rs
                        if r.get("direction") == "inbound" and not r.get("is_handled")
                        and r.get("auto_reply_state") == "miss")

        last_row = rs[-1]
        last_msg = _row_to_message(last_row)
        out.append({
            "conversation_id": cid,
            "sender_id": last_row.get("sender_id") or "",
            "name": _sender_name(rs),
            "last_preview": _preview(last_msg),
            "last_direction": last_row.get("direction"),
            "last_time": _iso(last_row.get("created_at")),
            "unread_count": unread,
            "auto_miss_count": auto_miss,
        })

    out.sort(key=lambda c: c["last_time"] or "", reverse=True)
    return out


# ---------- 端點 ----------

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def ui():
    if _UI_PATH.exists():
        return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>console_ui.html 不存在</h1>", status_code=404)


@router.get("/conversations")
async def conversations(platform: str = "whatsapp"):
    if platform not in ("wechat", "whatsapp"):
        return Response(
            content=json.dumps({"error": "platform 必須是 wechat 或 whatsapp"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )
    rows = await _rows_for_platform(platform)
    return {"platform": platform, "conversations": _group_conversations(rows)}


@router.get("/conversation/{platform}/{conversation_id}/messages")
async def messages(platform: str, conversation_id: str):
    if platform not in ("wechat", "whatsapp"):
        return Response(
            content=json.dumps({"error": "platform 必須是 wechat 或 whatsapp"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )
    rows = await asyncio.to_thread(
        _query,
        """
        SELECT id, conversation_id, sender_id, direction, message_type,
               forwarded_payload, raw_content, forward_status, message_time, created_at,
               auto_reply_state, auto_reply_tier, auto_reply_detail
        FROM forward_log
        WHERE platform = %s AND conversation_id = %s
        ORDER BY created_at ASC
        """,
        (platform, conversation_id),
    )
    sender_name = _sender_name(rows)
    sender_id = next((r["sender_id"] for r in reversed(rows) if r.get("sender_id")), "")
    return {
        "platform": platform,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "name": sender_name,
        "messages": [_row_to_message(r) for r in rows],
    }


@router.post("/conversation/read")
async def mark_read(platform: str = "whatsapp", conversation_id: str = ""):
    """把某個會話標為已處理（此次不回復），消除未讀紅點。"""
    if platform not in ("wechat", "whatsapp"):
        return Response(
            content=json.dumps({"error": "platform 必須是 wechat 或 whatsapp"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )
    if not conversation_id:
        return Response(
            content=json.dumps({"error": "missing conversation_id"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )
    await asyncio.to_thread(_execute, """
        UPDATE forward_log SET is_handled = true
        WHERE platform = %s AND conversation_id = %s AND direction = 'inbound'
    """, (platform, conversation_id))
    return {"ok": True}


def _sanitize_filename(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._") or "file"
    return name


@router.post("/reply")
async def reply(
    platform: str = Form(...),
    to: str = Form(...),
    text: str = Form(""),
    auto_tier: str = Form(""),
    file: UploadFile | None = File(None),
):
    """客服工作台回覆。文字 / 圖片 / 文件（WhatsApp）皆可。"""
    platform = platform.strip().lower()
    if platform not in ("wechat", "whatsapp"):
        return Response(
            content=json.dumps({"ok": False, "error": "platform 必須是 wechat 或 whatsapp"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )
    if not to:
        return Response(
            content=json.dumps({"ok": False, "error": "missing 'to'"}, ensure_ascii=False),
            media_type="application/json", status_code=400,
        )

    text = (text or "").strip()
    saved_path = ""
    stored_name = ""
    filename = ""
    mime = ""
    if file is not None:
        filename = _sanitize_filename(file.filename or "file")
        mime = (file.content_type or "").lower().split(";")[0].strip()
        if not mime:
            suffix = Path(filename).suffix.lower()
            if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                mime = "image/" + suffix.lstrip(".")
            else:
                mime = "application/octet-stream"
        stored_name = f"reply_{int(time.time())}_{filename}"
        saved_path = str(Path(config.MEDIA_DIR) / stored_name)
        Path(config.MEDIA_DIR).mkdir(exist_ok=True)
        with open(saved_path, "wb") as f:
            f.write(await file.read())

    # 組出記錄用 body（格式與來訊 payload 的 messageType / messageContent / uploadedFiles 一致）
    uploaded_files = []
    if saved_path:
        uploaded_files = [{
            "url": f"{config.PUBLIC_BASE_URL.rstrip('/')}/files/{stored_name}" if config.PUBLIC_BASE_URL else f"/files/{stored_name}",
            "filename": filename,
            "mimeType": mime,
        }]
    body = {
        "to": to,
        "messageType": "file" if saved_path else "text",
        "messageContent": text,
    }
    if uploaded_files:
        body["uploadedFiles"] = uploaded_files

    result = None
    try:
        if platform == "whatsapp":
            result = await _send_whatsapp(to, text, saved_path, mime, filename)
        else:
            result = await _send_wechat(to, text, saved_path, mime)
    except Exception as e:
        log.error("客服工作台 %s 回覆失敗: %s", platform, e)
        await db.insert_forward_log(_reply_record(platform, to, body, "failed", None, None, str(e)))
        return Response(
            content=json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
            media_type="application/json", status_code=500,
        )

    ok = result["status"] == 200
    await db.insert_forward_log(_reply_record(
        platform, to, body,
        "success" if ok else "failed",
        result["status"], result["body"],
        None if ok else result["body"],
    ))
    if ok:
        # 「由自動回復引擎產生、客服確認送出」→ 標記後再清未讀
        if (auto_tier or "").strip():
            try:
                import autoreply
                autoreply.mark_manual_reply(platform, "dm", auto_tier.strip().lower(),
                                            conversation_key=to)
            except Exception as e:
                log.warning("標記人工確認的引擎回覆失敗: %s", e)
        # 回覆成功後，把該會話的 inbound 全部標為已處理（消除紅點）
        await asyncio.to_thread(_execute, """
            UPDATE forward_log SET is_handled = true
            WHERE platform = %s AND conversation_id = %s AND direction = 'inbound'
        """, (platform, openid_to_conversation_id(to)))
    return Response(
        content=json.dumps({"ok": ok, "status": result["status"], "body": result["body"][:500]}, ensure_ascii=False),
        media_type="application/json", status_code=200 if ok else 502,
    )


async def _send_whatsapp(to: str, text: str, path: str, mime: str, filename: str) -> dict:
    if path:
        media_id = await whatsapp_send.upload_media(path, mime, filename)
        if mime.startswith("image/"):
            return await whatsapp_send.send_image(to, media_id, caption=text)
        return await whatsapp_send.send_document(to, media_id, filename, caption=text)
    return await whatsapp_send.send_text(to, text)


async def _send_wechat(to: str, text: str, path: str, mime: str) -> dict:
    if path:
        if not mime.startswith("image/"):
            raise RuntimeError("微信客服消息需認證公眾號才有權限，未認證號無法發送文件")
        return await asyncio.to_thread(wechat_send.send_image, to, path)
    return await asyncio.to_thread(wechat_send.send_text, to, text)


def _reply_record(platform: str, to: str, body: dict, forward_status: str,
                   target_http_status, target_response, error_message) -> dict:
    return {
        "platform": platform,
        "message_id": "",
        "sender_id": to,
        "conversation_id": openid_to_conversation_id(to),
        "message_type": body.get("messageType", "text"),
        "raw_content": json.dumps(body, ensure_ascii=False),
        "forwarded_payload": body,
        "forward_status": forward_status,
        "target_http_status": target_http_status,
        "target_response": (target_response or "")[:1000],
        "error_message": (error_message or "")[:500] or None,
        "message_time": None,
        "direction": "outbound",
    }
