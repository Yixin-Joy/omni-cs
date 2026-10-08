"""AI 自動回復的設定介面 API（掛在 /console/autoreply 之下）。

端點：
  GET  /console/autoreply/settings                 開關總覽 + FAQ/表格層狀態 + DeepSeek 狀態
  POST /console/autoreply/settings/global          全域自動回復總開關
  POST /console/autoreply/settings/scope           單一渠道＋場景開關
  POST /console/autoreply/settings/layer           FAQ 層 / 業務表格層 / 知識庫層 停啟用
  GET  /console/autoreply/faq                      FAQ 清單
  POST /console/autoreply/faq                      新增 FAQ
  POST /console/autoreply/faq/update               更新 FAQ
  POST /console/autoreply/faq/delete               刪除 FAQ（可批次）
  POST /console/autoreply/faq/import               匯入 FAQ（xlsx / csv / JSON）
  GET  /console/autoreply/faq/export               匯出 FAQ（xlsx）
  GET  /console/autoreply/faq/template             FAQ 範本檔
  GET  /console/autoreply/table                    業務表格清單（含資料列）
  POST /console/autoreply/table/create             新增業務表格
  POST /console/autoreply/table/update             更新業務表格（名稱 / 說明 / 欄位 / 停啟用）
  POST /console/autoreply/table/delete             刪除業務表格
  POST /console/autoreply/table/row                新增 / 更新 / 刪除資料列
  POST /console/autoreply/table/row/bulk-delete    批次刪除資料列
  POST /console/autoreply/table/import             匯入業務表格（xlsx / csv / JSON）
  GET  /console/autoreply/table/export             匯出業務表格（xlsx）
  GET  /console/autoreply/table/template           業務表格範本檔
  POST /console/autoreply/test/engine              不發訊息，試跑四層邏輯（可指定 layers）
  POST /console/autoreply/test/match               只試 FAQ 匹配分數（除錯用）
  GET  /console/autoreply/logs                     自動回復日誌（列表輕，不含召回明細）
  GET  /console/autoreply/logs/{id}                單筆日誌詳情（含 RAG 召回明細 matched_rows）
  GET  /console/autoreply/stats                    各渠道＋場景統計與未命中數
  GET  /console/autoreply/deepseek/test            DeepSeek 連線測試
"""
import asyncio
import json
import logging

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

import autoreply
import autoreply_db
import config
import reply_engine
import xlsx_io

log = logging.getLogger("wechat-forwarder")

router = APIRouter(prefix="/console/autoreply", tags=["autoreply"])

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ok(data) -> Response:
    return Response(content=json.dumps(data, ensure_ascii=False, default=str),
                    media_type="application/json")


def _err(msg: str, status: int = 400) -> Response:
    return Response(content=json.dumps({"error": msg}, ensure_ascii=False),
                    media_type="application/json", status_code=status)


def _attachment(data: bytes, filename: str, mime: str) -> Response:
    from urllib.parse import quote
    return Response(
        content=data, media_type=mime,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _check_target(channel: str, scope: str) -> str:
    """驗證渠道＋場景；合法回空字串。"""
    if (channel, scope) not in config.AUTOREPLY_SCOPES:
        return f"不支援的渠道／場景：{channel}/{scope}"
    return ""


def _refresh() -> None:
    reply_engine.invalidate_cache()


# ---------- 開關總覽 ----------

@router.get("/settings")
async def get_settings():
    stored = autoreply_db.all_settings()
    global_on = stored.get(autoreply_db.KEY_GLOBAL, False)
    scopes = []
    for channel, scope in config.AUTOREPLY_SCOPES:
        key = autoreply_db.scope_key(channel, scope)
        scopes.append({
            "channel": channel,
            "scope": scope,
            "key": key,
            "label": config.scope_label(channel, scope),
            "stored": stored.get(key),                 # None = 未單獨設定（跟隨上層）
            "effective": autoreply_db.effective_scope_mode(channel, scope),
        })
    return _ok({
        "global": global_on,
        "scopes": scopes,
        "faq_layer": reply_engine.faq_layer_enabled(),
        "table_layer": reply_engine.table_layer_enabled(),
        "rag_layer": reply_engine.rag_layer_enabled(),
        "deepseek": {
            "configured": bool(config.DEEPSEEK_API_KEY),
            "masked_key": _masked(config.DEEPSEEK_API_KEY),
            "model": config.DEEPSEEK_MODEL,
            "base_url": config.DEEPSEEK_BASE_URL,
            # 查詢改寫是**唯一**的改寫開關（原本另有 FAQ_REWRITE_MODE，
            # 兩個開關會讓人搞不清「關了為什麼還在呼叫模型」，已統一）
            "query_rewrite": reply_engine._rewrite_enabled(),
            "aux_timeout": config.DEEPSEEK_AUX_TIMEOUT,
            "threshold": config.FAQ_MATCH_THRESHOLD,
        },
        "guards": {
            "cooldown_seconds": config.AUTOREPLY_COOLDOWN_SECONDS,
            "max_per_day": config.AUTOREPLY_MAX_PER_DAY,
        },
        "faq_count": len(autoreply_db.list_faqs()),
        "faq_enabled_count": len(autoreply_db.list_faqs(enabled_only=True)),
        "table_count": len(autoreply_db.list_tables()),
        "row_count": len(autoreply_db.list_rows(enabled_only=True)),
        # 第三層的規模摘要（沒有知識庫時整層會自己降級，不影響前兩層）
        "kb": _kb_summary(),
    })


def _kb_summary() -> dict:
    """知識庫現況摘要（給「自動回復」頁顯示第三層有沒有料）。"""
    try:
        import kb_db
        rows = kb_db.query("""
            SELECT count(*) FILTER (WHERE status='ready')            AS ready_docs,
                   count(*)                                          AS docs,
                   (SELECT count(*) FROM kb_chunk
                     WHERE embedding_status='done' AND enabled)      AS chunks
            FROM kb_document
        """)
        cols = kb_db.list_collections(enabled_only=True)
        r = rows[0] if rows else {}
        return {"collections": len(cols), "documents": int(r.get("docs") or 0),
                "ready_documents": int(r.get("ready_docs") or 0),
                "chunks": int(r.get("chunks") or 0)}
    except Exception as e:
        log.debug("取知識庫摘要失敗: %s", e)
        return {"collections": 0, "documents": 0, "ready_documents": 0, "chunks": 0,
                "error": str(e)}


def _masked(key: str) -> str:
    if not key:
        return ""
    return f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"


@router.post("/settings/global")
async def set_global(body: dict):
    autoreply_db.set_global(bool(body.get("enabled")))
    _refresh()
    return _ok({"ok": True, "global": autoreply_db.global_enabled()})


@router.post("/settings/scope")
async def set_scope(body: dict):
    channel = (body.get("channel") or "").strip().lower()
    scope = (body.get("scope") or "").strip().lower()
    if err := _check_target(channel, scope):
        return _err(err)
    key = autoreply_db.scope_key(channel, scope)
    # enabled = null → 清除覆寫，回到跟隨全域
    if body.get("enabled") is None:
        autoreply_db.execute("DELETE FROM autoreply_setting WHERE scope_key=%s", (key,))
    else:
        autoreply_db.set_setting(key, bool(body.get("enabled")))
    _refresh()
    return _ok({"ok": True, "key": key,
                "enabled": autoreply_db.effective_scope_mode(channel, scope)})


@router.post("/settings/layer")
async def set_layer(body: dict):
    layer = (body.get("layer") or "").strip().lower()
    if layer not in ("faq", "table", "rag"):
        return _err("layer 必須是 faq、table 或 rag")
    reply_engine.set_layer_enabled(layer, bool(body.get("enabled")))
    return _ok({"ok": True, "layer": layer, "enabled": bool(body.get("enabled"))})


# ---------- FAQ ----------

@router.get("/faq")
async def list_faq(q: str = ""):
    """FAQ 清單。`q` 有值時對問題／答案／關鍵詞做內文檢索（客服要「用內文找」時用）。

    同時回 `total`（全部筆數）與 `filtered`（這次命中的筆數），UI 才好顯示
    「符合 N 筆 / 共 M 筆」。
    """
    items = autoreply_db.list_faqs(q=q)
    total = len(autoreply_db.list_faqs()) if (q or "").strip() else len(items)
    return _ok({"faqs": items, "total": total, "filtered": len(items), "q": q})


@router.get("/miss-info")
async def miss_info(channel: str = "", scope: str = "dm", comment_id: str = "",
                    conversation_key: str = ""):
    """客服剛回覆的那則客戶訊息，是不是「自動回復未命中」？

    工作台在回覆成功後用它決定要不要彈「把這次回覆加進 FAQ 庫」的視窗，
    並順便取回客戶原問句與引擎改寫後的問句（後者只當參考提示）。
    """
    channel = (channel or "").strip().lower()
    scope = (scope or "dm").strip().lower()
    if err := _check_target(channel, scope):
        return _err(err)

    question = ""
    message_id = ""
    cid = ""
    state = ""
    if scope == "comment":
        cid = (comment_id or "").strip()
        if not cid:
            return _err("missing comment_id")
        rows = autoreply_db.query("""
            SELECT text, coalesce(auto_reply_state, '') AS st
            FROM igfb_comment WHERE platform=%s AND comment_id=%s
        """, (channel, cid))
        if not rows:
            return _ok({"miss": False, "reason": "找不到這則留言"})
        question = rows[0].get("text") or ""
        state = rows[0].get("st") or ""
    else:
        key = (conversation_key or "").strip()
        if not key:
            return _err("missing conversation_key")
        row = (autoreply_db.latest_inbound_dm(channel, key) if channel in ("ig", "fb")
               else autoreply_db.latest_inbound_forward_log(channel, key)) or {}
        if not row:
            return _ok({"miss": False, "reason": "找不到客戶訊息"})
        question = row.get("text") or ""
        state = row.get("auto_reply_state") or ""
        message_id = str(row.get("message_id") or "")

    # 引擎改寫後的問句（只在業務表格「第一次沒查到、帶對話改寫後才命中」時才有）——純參考
    rewritten = ""
    try:
        rows = autoreply_db.query("""
            SELECT rewritten_query FROM autoreply_log
            WHERE channel=%s AND reply_scope=%s AND tier <> %s
              AND (comment_id=%s OR message_id=%s)
              AND coalesce(rewritten_query, '') <> ''
            ORDER BY id DESC LIMIT 1
        """, (channel, scope, autoreply_db.TIER_CLAIM, cid, message_id))
        rewritten = (rows[0].get("rewritten_query") or "") if rows else ""
    except Exception:
        rewritten = ""

    return _ok({"miss": state == "miss", "auto_reply_state": state, "question": question,
                "rewritten_query": rewritten, "message_id": message_id, "comment_id": cid})


@router.post("/faq")
async def create_faq(body: dict):
    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not question or not answer:
        return _err("問題與答案都不可為空")
    row = autoreply_db.create_faq(question, answer, (body.get("keywords") or "").strip(),
                                  bool(body.get("enabled", True)))
    _refresh()
    return _ok({"ok": True, "faq": row})


@router.post("/faq/update")
async def update_faq(body: dict):
    faq_id = body.get("id")
    if not faq_id:
        return _err("missing id")
    fields = {k: body[k] for k in ("question", "answer", "keywords", "enabled", "sort_order")
              if k in body}
    for k in ("question", "answer"):
        if k in fields and not str(fields[k]).strip():
            return _err("問題與答案都不可為空")
    if not autoreply_db.update_faq(int(faq_id), fields):
        return _err("找不到該 FAQ 或沒有可更新欄位", 404)
    _refresh()
    return _ok({"ok": True})


@router.post("/faq/delete")
async def delete_faq(body: dict):
    ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
    ids = [int(i) for i in ids]
    if not ids:
        return _err("missing ids")
    n = 0
    for i in ids:
        n += 1 if autoreply_db.delete_faq(i) else 0
    _refresh()
    return _ok({"ok": True, "deleted": n})


@router.post("/faq/import")
async def import_faq(mode: str = Form("replace"), file: UploadFile | None = File(None),
                     text: str = Form("")):
    """匯入 FAQ。mode = replace（先清空）或 append。"""
    mode = (mode or "replace").lower()
    if mode not in ("replace", "append"):
        return _err("mode 必須是 replace 或 append")
    try:
        if file is not None:
            raw = await file.read()
            items = xlsx_io.parse_faq_rows(xlsx_io.read_rows(file.filename or "", raw))
        elif text.strip():
            items = _json_faqs(text)
        else:
            return _err("請選擇檔案或提供 JSON 內容")
    except xlsx_io.SpreadsheetError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"解析失敗：{e}")

    result = autoreply_db.replace_faqs(items, mode=mode)
    _refresh()
    return _ok({"ok": True, **result, "total": len(autoreply_db.list_faqs())})


def _json_faqs(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("faqs") or data.get("items") or []
    if not isinstance(data, list):
        raise ValueError("JSON 需為陣列")
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or it.get("問題") or it.get("问题") or "").strip()
        a = str(it.get("answer") or it.get("答案") or "").strip()
        if not q or not a:
            continue
        out.append({"question": q, "answer": a,
                    "keywords": str(it.get("keywords") or it.get("關鍵詞") or "").strip(),
                    "enabled": xlsx_io.parse_bool(str(it.get("enabled", "")), default=True)})
    if not out:
        raise ValueError("沒有解析到任何有效的問答對")
    return out


@router.get("/faq/export")
async def export_faq():
    faqs = autoreply_db.list_faqs()
    rows = [[f["question"], f["answer"], f.get("keywords") or "",
             "是" if f["enabled"] else "否"] for f in faqs]
    data = xlsx_io.to_xlsx_bytes(xlsx_io.FAQ_HEADERS, rows, "FAQ")
    return _attachment(data, "faq.xlsx", _XLSX_MIME)


@router.get("/faq/template")
async def template_faq(fmt: str = "xlsx"):
    return await _template(xlsx_io.FAQ_HEADERS, xlsx_io.FAQ_TEMPLATE_ROWS, "FAQ", fmt)


# ---------- 業務表格 ----------

@router.get("/table")
async def list_tables(rows: int = 1):
    tables = autoreply_db.list_tables()
    all_rows = autoreply_db.list_rows()
    by_table: dict[int, list[dict]] = {}
    for r in all_rows:
        by_table.setdefault(r["table_id"], []).append(r)
    for t in tables:
        t["rows"] = by_table.get(t["id"], []) if rows else []
        t["row_count"] = len(by_table.get(t["id"], []))
    return _ok({"tables": tables, "total": len(tables)})


@router.post("/table/create")
async def create_table(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        return _err("請填表名")
    columns = [str(c).strip() for c in (body.get("columns") or []) if str(c).strip()]
    t = autoreply_db.create_table(name, (body.get("description") or "").strip(), columns,
                                  bool(body.get("enabled", True)))
    _refresh()
    return _ok({"ok": True, "table": t})


@router.post("/table/update")
async def update_table(body: dict):
    table_id = body.get("id")
    if not table_id:
        return _err("missing id")
    fields = {k: body[k] for k in ("name", "description", "enabled", "sort_order", "columns")
              if k in body}
    if "columns" in fields:
        fields["columns"] = [str(c).strip() for c in (fields["columns"] or []) if str(c).strip()]
    if not autoreply_db.update_table(int(table_id), fields):
        return _err("找不到該表或沒有可更新欄位", 404)
    _refresh()
    return _ok({"ok": True})


@router.post("/table/delete")
async def delete_table(body: dict):
    table_id = body.get("id")
    if not table_id:
        return _err("missing id")
    if not autoreply_db.delete_table(int(table_id)):
        return _err("找不到該表", 404)
    _refresh()
    return _ok({"ok": True})


@router.post("/table/row")
async def row_op(body: dict):
    """action = create / update / delete。"""
    action = (body.get("action") or "create").lower()
    if action == "create":
        table_id = body.get("table_id")
        if not table_id:
            return _err("missing table_id")
        data = body.get("data") or {}
        if not any(str(v).strip() for v in data.values()):
            return _err("資料列不可全空")
        row = autoreply_db.create_row(int(table_id), {k: str(v) for k, v in data.items()})
        _refresh()
        return _ok({"ok": True, "row": row})
    if action == "update":
        row_id = body.get("id")
        if not row_id:
            return _err("missing id")
        data = body.get("data")
        enabled = body.get("enabled")
        if data is not None:
            data = {k: str(v) for k, v in data.items()}
        if not autoreply_db.update_row(int(row_id), data, enabled):
            return _err("找不到該列或沒有可更新欄位", 404)
        _refresh()
        return _ok({"ok": True})
    if action == "delete":
        row_id = body.get("id")
        if not row_id:
            return _err("missing id")
        if not autoreply_db.delete_row(int(row_id)):
            return _err("找不到該列", 404)
        _refresh()
        return _ok({"ok": True})
    return _err("action 必須是 create / update / delete")


@router.post("/table/row/bulk-delete")
async def bulk_delete_rows(body: dict):
    ids = [int(i) for i in (body.get("ids") or [])]
    if not ids:
        return _err("missing ids")
    n = autoreply_db.delete_rows_bulk(ids)
    _refresh()
    return _ok({"ok": True, "deleted": n})


@router.post("/table/import")
async def import_table(
    table_id: int = Form(0),
    name: str = Form(""),
    description: str = Form(""),
    mode: str = Form("replace"),
    file: UploadFile | None = File(None),
    text: str = Form(""),
):
    """匯入業務表格。

    table_id = 0 → 依檔案第一列欄位名新建一張表；否則覆寫 / 附加到既有表。
    """
    mode = (mode or "replace").lower()
    if mode not in ("replace", "append"):
        return _err("mode 必須是 replace 或 append")
    try:
        if file is not None:
            raw = await file.read()
            columns, rows = xlsx_io.parse_grid(xlsx_io.read_rows(file.filename or "", raw))
        elif text.strip():
            columns, rows = _json_table(text)
        else:
            return _err("請選擇檔案或提供 JSON 內容")
    except xlsx_io.SpreadsheetError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"解析失敗：{e}")

    if table_id:
        t = next((x for x in autoreply_db.list_tables() if x["id"] == int(table_id)), None)
        if t is None:
            return _err("找不到該表", 404)
        if columns and columns != [str(c) for c in (t.get("columns") or [])]:
            autoreply_db.update_table(t["id"], {"columns": columns})
        n = (autoreply_db.replace_rows(t["id"], rows) if mode == "replace"
             else autoreply_db.append_rows(t["id"], rows))
        _refresh()
        return _ok({"ok": True, "table_id": t["id"], "written": n, "columns": columns,
                    "mode": mode})

    t = autoreply_db.create_table(name.strip() or "業務表格",
                                  (description or "").strip(), columns)
    n = autoreply_db.replace_rows(t["id"], rows)
    _refresh()
    return _ok({"ok": True, "table_id": t["id"], "written": n, "columns": columns,
                "table": t})


def _json_table(text: str) -> tuple[list[str], list[dict]]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or []
    if not isinstance(data, list) or not data:
        raise ValueError("JSON 需為非空陣列")
    columns: list[str] = []
    for it in data:
        if isinstance(it, dict):
            for k in it:
                if str(k) not in columns:
                    columns.append(str(k))
    rows = [{str(k): str(v) for k, v in it.items()} for it in data if isinstance(it, dict)]
    if not columns or not rows:
        raise ValueError("沒有解析到任何資料列")
    return columns, rows


@router.get("/table/export")
async def export_table(table_id: int = 0):
    if table_id:
        tables = [t for t in autoreply_db.list_tables() if t["id"] == table_id]
    else:
        tables = autoreply_db.list_tables()
    if not tables:
        return _err("找不到該表", 404)

    t = tables[0]
    columns = [str(c) for c in (t.get("columns") or [])]
    rows = autoreply_db.list_rows(t["id"])
    if not columns and rows:
        for r in rows:
            for k in (r.get("data") or {}):
                if k not in columns:
                    columns.append(k)
    body = [[str((r.get("data") or {}).get(c, "")) for c in columns] for r in rows]
    data = xlsx_io.to_xlsx_bytes(columns, body, t["name"] or "業務表格")
    return _attachment(data, f"{t['name'] or 'business_table'}.xlsx", _XLSX_MIME)


@router.get("/table/template")
async def template_table(fmt: str = "xlsx"):
    rows = xlsx_io.TABLE_TEMPLATE_ROWS
    return await _template(rows[0], rows[1:], "業務表格", fmt)


async def _template(headers: list[str], rows: list[list[str]], title: str, fmt: str) -> Response:
    if (fmt or "xlsx").lower() == "csv":
        return _attachment(xlsx_io.to_csv_bytes(headers, rows), f"{title}_template.csv",
                           "text/csv; charset=utf-8")
    return _attachment(xlsx_io.to_xlsx_bytes(headers, rows, title),
                       f"{title}_template.xlsx", _XLSX_MIME)


# ---------- 測試 ----------

@router.post("/test/engine")
async def test_engine(body: dict):
    """不發送任何訊息，只試跑四層邏輯並回傳決策。

    可帶 history（[{direction, text}, ...]）模擬最近對話，驗證業務表格 / 知識庫的
    query 改寫；不帶時會自動去資料庫撈最近對話（等同真實流程）。

    `layers`：只跑指定的層，例如 `["faq","rag"]`。**只影響這次呼叫，不改 DB 開關**
    （`reply_engine.answer(layers=None)` 才是依開關的正式流程），也**不寫任何紀錄**。

    `query_rewrite`：知識庫層要不要做多查詢改寫。不給就依知識庫的全域參數；
    給 true / false 可單獨試「開與關的差別」，同樣**不改任何設定**。
    """
    text = (body.get("text") or "").strip()
    channel = (body.get("channel") or "whatsapp").strip().lower()
    scope = (body.get("scope") or "dm").strip().lower()
    if not text:
        return _err("請輸入測試訊息")
    if err := _check_target(channel, scope):
        return _err(err)

    layers = body.get("layers")
    if layers is not None:
        if not isinstance(layers, list):
            return _err("layers 要是陣列，例如 [\"faq\",\"table\",\"rag\"]")
        layers = [str(x).strip().lower() for x in layers]
        bad = [x for x in layers if x not in reply_engine.ALL_LAYERS]
        if bad:
            return _err(f"不認得的層：{'、'.join(bad)}"
                        f"（可用：{'、'.join(reply_engine.ALL_LAYERS)}）")
        layers = [x for x in reply_engine.ALL_LAYERS if x in layers]   # 固定順序

    history = body.get("history")
    if history is None and body.get("with_history", True):
        try:
            history = await asyncio.to_thread(
                autoreply_db.get_conversation_history, channel, scope,
                sender_id=(body.get("sender_id") or ""),
                comment_id=(body.get("comment_id") or ""),
                message_id="", current_text=text,
                limit=config.TABLE_REWRITE_HISTORY_TURNS,
            )
        except Exception:
            history = []
    qr = body.get("query_rewrite")
    if qr is not None and not isinstance(qr, bool):
        return _err("query_rewrite 要是 true / false（不給 = 依知識庫參數）")
    try:
        answer = await reply_engine.answer(text, channel=channel, scope=scope,
                                           history=history or [], layers=layers,
                                           query_rewrite=qr)
    except Exception as e:
        return _err(f"引擎錯誤：{e}", 500)
    return _ok({"ok": True, "label": answer.label, **answer.as_dict(),
                "would_send": answer.hit, "history": history or [],
                "layers": layers, "query_rewrite": qr})


@router.post("/draft")
async def draft(body: dict):
    """只產生回覆、不發送：工作台右鍵「由自動回復引擎給出回復」用。

    與自動回復流程的差別：
      * 不檢查自動回復開關（關著的時候也要能用），也不套冷卻與每日上限（人工判斷優先）
      * **命中**時不寫任何標記或日誌；等客服真的按下送出，才由各回覆端點標成
        「人工確認的自動回復-FAQ／表格／知識庫命中」（autoreply.mark_manual_reply）
      * **未命中**時就在這裡寫一筆紀錄（autoreply.log_manual_miss，reason=manual_no_match）——
        記錄點＝前端跳出「引擎答不出這一則」彈窗的同一刻。不寫的話，紀錄頁看不出
        「這一則人工丟給引擎試過、答不出來」，也就無從知道該補哪條 FAQ／表格／文件。
    走的是同一套四層引擎（FAQ → 業務表格 → 知識庫 → 人工），與自動回復結果一致。
    """
    channel = (body.get("channel") or "").strip().lower()
    scope = (body.get("scope") or "dm").strip().lower()
    comment_id = (body.get("comment_id") or "").strip()
    conversation_key = (body.get("conversation_key") or "").strip()
    message_id = (body.get("message_id") or "").strip()
    question = (body.get("text") or "").strip()
    if err := _check_target(channel, scope):
        return _err(err)

    post_id = ""
    if scope == "comment":
        if not comment_id:
            return _err("missing comment_id")
        rows = autoreply_db.query("""
            SELECT text, post_id FROM igfb_comment
            WHERE platform=%s AND comment_id=%s
        """, (channel, comment_id))
        if not rows:
            return _err("找不到這則留言（可能還沒同步進來）", 404)
        question = question or (rows[0].get("text") or "").strip()
        post_id = str(rows[0].get("post_id") or "")
    else:
        if not conversation_key:
            return _err("missing conversation_key")
        if not question and message_id and channel in ("ig", "fb"):
            # 指定了某一則（工作台在聊天框右鍵某一則）→ 就用那一則當問句
            row = autoreply_db.dm_by_message_id(channel, conversation_key, message_id) or {}
            question = (row.get("text") or "").strip()
        if not question:
            # 沒指定就取「最新一則還沒被回覆」的客戶訊息
            if channel in ("ig", "fb"):
                question = ((autoreply_db.latest_unanswered_dm(channel, conversation_key) or {})
                            .get("text") or "").strip()
            else:
                row = autoreply_db.latest_unanswered_forward_log(channel, conversation_key) or {}
                payload = row.get("forwarded_payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                # JSONB 欄位不保證回 dict（可能是 list / 純量），一律收斂
                if not isinstance(payload, dict):
                    payload = {}
                question = (payload.get("messageContent") or "").strip()
    if not question:
        return _err("找不到可回覆的客戶訊息（可能已經回覆過，或不是文字訊息）", 404)

    history = []
    if config.TABLE_REWRITE_HISTORY_TURNS > 0:
        try:
            # message_id 當錨點：上下文＝「被回覆那一則往上數」的最近幾則
            # （沒指定 message_id 時，收斂成原本的「最新幾則」行為）
            history = await asyncio.to_thread(
                autoreply_db.get_conversation_history, channel, scope,
                sender_id=(conversation_key if scope == "dm" else ""),
                comment_id=(comment_id if scope == "comment" else ""),
                message_id=(message_id if scope == "dm" else ""),
                current_text=question,
                limit=config.TABLE_REWRITE_HISTORY_TURNS,
            )
        except Exception:
            history = []

    post_context = ""
    if scope == "comment" and post_id:
        try:
            import igfb_db
            post = await asyncio.to_thread(igfb_db.get_post, channel, post_id)
            if post and post.get("name"):
                post_context = f"【被留言的貼文內容】\n{str(post['name'])[:500]}"
        except Exception:
            post_context = ""

    try:
        answer = await reply_engine.answer(question, channel=channel, scope=scope,
                                           post_context=post_context, history=history or [])
    except Exception as e:
        return _err(f"引擎錯誤：{e}", 500)

    if not answer.hit:
        # 人工選擇「由引擎回覆」但沒命中 → 也要留在紀錄裡（命中的那半由送出時的
        # mark_manual_reply 記）。不做這一步，紀錄頁就看不到「這一則人工試過、答不出來」。
        try:
            await asyncio.to_thread(
                autoreply.log_manual_miss, channel, scope,
                text=question, detail=(answer.detail or answer.reason or ""),
                message_id=message_id, comment_id=comment_id, post_id=post_id,
                conversation_key=conversation_key, matched_rows=answer.matched_rows,
            )
        except Exception as e:
            log.warning("寫入人工未命中紀錄失敗: %s", e)

    return _ok({"ok": True, "question": question, "would_send": answer.hit,
                "message_id": message_id, "comment_id": comment_id,
                "history_recent": len(history or []), **answer.as_dict()})


@router.get("/pending")
async def pending():
    """各渠道＋場景的未處理訊息清單。

    工作台開啟自動回復（個別或全域）時，用這個問「要不要一併自動回復」，
    並取得「哪個客戶有多則未回覆」讓前端逐一彈窗問要逐條回還是只回最後一則。
    """
    import backfill
    return _ok(backfill.all_pending())


@router.post("/backfill")
async def backfill_start(body: dict):
    """開始批次補做（背景跑，回 job_id 讓工作台輪詢進度）。

    body: {"targets": [{"channel": "fb", "scope": "comment",
                        "plan": {"<會話/討論串 key>": "all" | "last"}}]}
    """
    import backfill
    targets = body.get("targets") or []
    if not targets:
        return _err("missing targets")
    info = backfill.start_job(targets)
    if not info["total"]:
        return _ok({"ok": True, "job_id": "", "total": 0, "note": "沒有未處理訊息"})
    return _ok({"ok": True, **info})


@router.get("/backfill/{job_id}")
async def backfill_progress(job_id: str):
    import backfill
    job = backfill.get_job(job_id)
    if not job:
        return _err("找不到這個批次（服務可能重啟過）", 404)
    return _ok(job)


@router.post("/test/match")
async def test_match(body: dict):
    """只試 FAQ 匹配分數（除錯用，不呼叫 AI）。"""
    text = (body.get("text") or "").strip()
    if not text:
        return _err("請輸入測試訊息")
    import faq_matcher
    prepared = [(f, faq_matcher.prepare([f["question"]] +
                [k.strip() for k in (f.get("keywords") or "").replace("，", ",").split(",") if k.strip()]))
                for f in autoreply_db.list_faqs(enabled_only=True)]
    nu, ut = faq_matcher.preprocess(text)
    scored = []
    for faq, variants in prepared:
        for raw, (nf, ft) in variants:
            scored.append({"faq_id": faq["id"], "variant": raw,
                           "score": round(faq_matcher.score_pair(nu, ut, nf, ft), 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return _ok({"ok": True, "threshold": config.FAQ_MATCH_THRESHOLD,
                "top": scored[:10],
                "matched": bool(scored and scored[0]["score"] >= config.FAQ_MATCH_THRESHOLD)})


@router.get("/deepseek/test")
async def deepseek_test():
    if not config.DEEPSEEK_API_KEY:
        return _ok({"ok": False, "message": "尚未設定 DEEPSEEK_API_KEY"})
    from deepseek_client import DeepSeekClient, DeepSeekError
    try:
        reply = await DeepSeekClient().ping()
        return _ok({"ok": True, "message": f"連線成功，模型回覆：{reply[:50]}",
                    "model": config.DEEPSEEK_MODEL})
    except DeepSeekError as e:
        return _ok({"ok": False, "message": str(e)})


# ---------- 日誌 / 統計 ----------

@router.get("/logs")
async def logs(channel: str = "", scope: str = "", tier: str = "", limit: int = 100,
               offset: int = 0):
    """自動回復紀錄列表。

    ⚠️ **列表刻意不帶 `matched_rows`**（召回明細）：那是幾十個候選塊的 JSON，
    列表動輒上百筆，一起撈會讓回應肥到沒必要。要看明細走 `GET /logs/{id}`。
    """
    data = autoreply_db.list_logs(channel, scope, tier, limit, offset)
    for r in data["logs"]:
        r["tier_label"] = autoreply_db.tier_label(r["tier"])
    return _ok(data)


@router.get("/logs/{log_id}")
async def log_detail(log_id: int):
    """單筆紀錄詳情：完整內容 + RAG 召回明細（`matched_rows`）。

    明細裡每一筆含：排名相關分數（vec / lex / final / rerank）、來源
    （文件 / 章節 / 頁碼 / 工作表）、片段、以及 `picked`（是否真的入選送進模型）。
    **未命中**的紀錄也會有明細 —— 那才是調門檻／top-k 最有用的線索。
    """
    row = autoreply_db.get_log(log_id)
    if not row:
        return _err("找不到這筆紀錄", 404)
    row["tier_label"] = autoreply_db.tier_label(row["tier"])
    rows = row.get("matched_rows")
    # 舊資料 / 壞資料可能不是 list（見專案的不變式：JSON 存 TEXT 欄位不可假設型別）
    row["matched_rows"] = rows if isinstance(rows, list) else []
    return _ok(row)


@router.get("/stats")
async def stats():
    miss = autoreply_db.miss_counts()
    scopes = []
    summary = autoreply_db.log_summary()
    for channel, scope in config.AUTOREPLY_SCOPES:
        key = autoreply_db.scope_key(channel, scope)
        s = summary.get(key, {"faq": 0, "table": 0, "rag": 0, "miss": 0, "error": 0,
                              "rewritten": 0, "total": 0})
        scopes.append({
            "channel": channel, "scope": scope, "label": config.scope_label(channel, scope),
            "enabled": autoreply_db.effective_scope_mode(channel, scope),
            "pending_miss": miss.get(key, 0),
            **s,
        })
    return _ok({"global": autoreply_db.global_enabled(), "scopes": scopes,
                "miss_total": sum(miss.values()),
                "rewritten_total": sum(s.get("rewritten", 0) for s in summary.values())})
