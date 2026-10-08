"""知識庫（RAG 第三層）的設定介面 API（掛在 `/console/kb` 之下）。

端點：
  GET  /console/kb/overview                 知識庫概覽（集合列表 + 模型狀態 + 統計）
  GET  /console/kb/collections              知識庫列表
  POST /console/kb/collections/create       新增知識庫
  POST /console/kb/collections/update       更新知識庫（名稱 / 說明 / 停啟用 / 排序）
  POST /console/kb/collections/delete       刪除知識庫
  GET  /console/kb/params                   參數規格 + 目前值（概覽頁「參數配置」）
  POST /console/kb/params                   寫入參數覆寫（改完 5 秒生效）
  POST /console/kb/upload                   上傳文件（可多檔）
  GET  /console/kb/documents                文件列表
  GET  /console/kb/document                 文件詳情（含分塊）
  POST /console/kb/document/preview         分塊預覽（不寫庫，調參用）
  POST /console/kb/document/reparse         重新分塊（寫庫）
  POST /console/kb/document/embed           開始 / 重跑 embedding
  POST /console/kb/document/activate        確認入庫（status → ready）
  POST /console/kb/document/enabled         停用 / 啟用文件
  POST /console/kb/document/delete          刪除文件
  POST /console/kb/chunk/update             編輯 / 停用單一知識塊
  POST /console/kb/chunk/split              拆分知識塊
  POST /console/kb/chunk/merge              與下一塊合併
  POST /console/kb/chunk/delete             刪除知識塊
"""
import asyncio
import json
import logging

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response

import kb_db
import kb_embed
import kb_ingest
import kb_parse
import rag_config
from kb_parse import ParseError

log = logging.getLogger("wechat-forwarder")

router = APIRouter(prefix="/console/kb", tags=["kb"])


def _ok(data) -> Response:
    return Response(content=json.dumps(data, ensure_ascii=False, default=str),
                    media_type="application/json")


def _err(msg: str, status: int = 400) -> Response:
    return Response(content=json.dumps({"error": msg}, ensure_ascii=False),
                    media_type="application/json", status_code=status)


# ---------- 背景任務（同一份文件同時只能跑一個） ----------

_RUNNING: dict[int, str] = {}


def _is_running(doc_id: int) -> str:
    return _RUNNING.get(doc_id, "")


def _spawn(label: str, doc_id: int, fn, *args, **kwargs) -> bool:
    if doc_id in _RUNNING:
        return False
    _RUNNING[doc_id] = label

    async def runner():
        try:
            await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            log.warning("知識庫背景任務失敗（%s doc#%s）: %s", label, doc_id, e)
        finally:
            _RUNNING.pop(doc_id, None)

    asyncio.create_task(runner())
    return True


def _doc_payload(doc: dict, *, with_chunks: bool = False, chunks_limit: int = 400) -> dict:
    out = {"doc": doc, "running": _is_running(int(doc["id"]))}
    out["stats"] = kb_db.chunk_stats(int(doc["id"]))
    out["jobs"] = kb_db.list_jobs(int(doc["id"]), limit=8)
    if with_chunks:
        out["chunks"] = kb_db.list_chunks(int(doc["id"]), limit=chunks_limit)
    return out


# ======================================================================
# 概覽 / 知識庫 CRUD
# ======================================================================

@router.get("/overview")
async def overview():
    cols = await asyncio.to_thread(kb_db.list_collections)
    totals = await asyncio.to_thread(kb_db.query, """
        SELECT (SELECT count(*) FROM kb_document)                          AS documents,
               (SELECT count(*) FROM kb_document WHERE status='ready')     AS ready_docs,
               (SELECT count(*) FROM kb_chunk)                             AS chunks,
               (SELECT count(*) FROM kb_chunk WHERE embedding_status='done') AS embedded
    """)
    return _ok({
        "collections": cols,
        "totals": totals[0] if totals else {},
        "model": await asyncio.to_thread(kb_embed.model_info),
        "running": len(_RUNNING),
        "max_upload_mb": kb_ingest.MAX_UPLOAD_MB,
    })


@router.get("/collections")
async def collections():
    return _ok({"collections": await asyncio.to_thread(kb_db.list_collections)})


@router.post("/collections/create")
async def collection_create(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        return _err("知識庫名稱不能是空的")
    doc = await asyncio.to_thread(kb_db.create_collection, name, body.get("description") or "")
    return _ok({"collection": doc})


@router.post("/collections/update")
async def collection_update(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識庫 id")
    fields = {k: body[k] for k in ("name", "description", "sort_order", "enabled")
              if k in body}
    ok = await asyncio.to_thread(kb_db.update_collection, cid, fields)
    return _ok({"ok": ok, "collection": await asyncio.to_thread(kb_db.get_collection, cid)})


@router.post("/collections/delete")
async def collection_delete(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識庫 id")
    n = await asyncio.to_thread(kb_db.query,
                                "SELECT count(*) AS c FROM kb_document WHERE collection_id=%s",
                                (cid,))
    if (n and int(n[0]["c"]) > 0) and not body.get("force"):
        return _err(f"這個知識庫底下還有 {n[0]['c']} 份文件，刪除後文件會變成「未分組」"
                    "（資料不會消失）。確定要刪請再按一次。")
    ok = await asyncio.to_thread(kb_db.delete_collection, cid)
    return _ok({"ok": ok})


# ======================================================================
# 參數配置
# ======================================================================

def _params_payload(collection_id: int | None) -> dict:
    overrides = kb_db.get_param_overrides(collection_id) if collection_id else {}
    items = []
    for key, spec in rag_config.PARAM_SPECS.items():
        raw = overrides.get(key)
        value = rag_config.coerce(key, raw if raw not in (None, "") else spec["default"])
        items.append({
            "key": key, "label": spec["label"], "group": spec["group"],
            "type": spec["type"], "help": spec["help"],
            "min": spec["min"], "max": spec["max"],
            "choices": spec.get("choices"),
            "default": rag_config.coerce(key, spec["default"]),
            "value": value,
            "env": spec["env"],
            "overridden": raw not in (None, ""),
            "rebuild": spec["rebuild"],
        })
    return {
        "collection_id": collection_id,
        "items": items,
        "model": kb_embed.model_info(),
        "retrieval": [i for i in items if i["group"] == "retrieval"],
        "chunk": [i for i in items if i["group"] == "chunk"],
    }


@router.get("/params")
async def get_params(collection_id: str = ""):
    cid = int(collection_id) if str(collection_id).strip().isdigit() else None
    return _ok(await asyncio.to_thread(_params_payload, cid))


@router.post("/params")
async def set_params(body: dict):
    cid = int(body.get("collection_id") or 0)
    if not cid:
        return _err("缺少知識庫 id（參數是按知識庫存的）")
    values = body.get("values") or {}
    n = await asyncio.to_thread(kb_db.set_params, cid, values)
    return _ok({"ok": True, "saved": n, "params": await asyncio.to_thread(_params_payload, cid)})


# ======================================================================
# 上傳 / 文件
# ======================================================================

@router.post("/upload")
async def upload(files: list[UploadFile] = File(...),
                 collection_id: str = Form(""),
                 auto_start: str = Form("1")):
    cid = int(collection_id) if str(collection_id).strip().isdigit() else None
    started_auto = str(auto_start).strip().lower() not in ("0", "false", "no", "")
    results = []
    for f in files:
        raw = await f.read()
        try:
            saved = await asyncio.to_thread(
                kb_ingest.save_upload, f.filename or "upload.bin", raw, f.content_type or "", cid)
        except ParseError as e:
            results.append({"filename": f.filename, "ok": False,
                            "error": kb_parse.describe_error(e.reason), "reason": e.reason})
            continue
        except Exception as e:
            results.append({"filename": f.filename, "ok": False, "error": str(e)})
            continue
        doc = saved["doc"]
        item = {"filename": doc["filename"], "ok": True, "doc_id": doc["id"],
                "doc_type": doc["doc_type"], "size": doc["size_bytes"],
                "duplicate_of": saved["duplicate_of"], "started": False}
        if started_auto:
            item["started"] = _spawn("parse+chunk", int(doc["id"]),
                                     kb_ingest.parse_and_chunk, int(doc["id"]))
        results.append(item)
    return _ok({"results": results})


@router.get("/documents")
async def documents(collection_id: str = "", status: str = ""):
    cid = int(collection_id) if str(collection_id).strip().isdigit() else None
    docs = await asyncio.to_thread(kb_db.list_documents, cid, status)
    for d in docs:
        d["running"] = _is_running(int(d["id"]))
    return _ok({"documents": docs})


@router.get("/document")
async def document(id: int, chunks: int = 1, chunks_limit: int = 400):
    doc = await asyncio.to_thread(kb_db.get_document, id)
    if not doc:
        return _err("找不到這份文件", 404)
    return _ok(_doc_payload(doc, with_chunks=bool(chunks), chunks_limit=chunks_limit))


@router.post("/document/preview")
async def document_preview(body: dict):
    did = int(body.get("id") or 0)
    if not did:
        return _err("缺少文件 id")
    try:
        out = await asyncio.to_thread(kb_ingest.preview_chunks, did,
                                      body.get("params") or {}, int(body.get("limit") or 30))
    except ParseError as e:
        return _err(kb_parse.describe_error(e.reason))
    except Exception as e:
        return _err(str(e))
    return _ok(out)


@router.post("/document/reparse")
async def document_reparse(body: dict):
    did = int(body.get("id") or 0)
    if not did:
        return _err("缺少文件 id")
    if _is_running(did):
        return _err(f"這份文件正在「{_is_running(did)}」，請等它跑完")
    stats = await asyncio.to_thread(kb_db.chunk_stats, did)
    if int(stats.get("edited") or 0) > 0 and not body.get("force"):
        return _err(f"這份文件有 {stats['edited']} 個知識塊被手動改過，"
                    "重新分塊會覆蓋手改內容。確定要繼續請帶 force=true。")
    params = body.get("params") or None
    if params:
        merged = await asyncio.to_thread(kb_db.load_params, None)
        merged.update({k: v for k, v in params.items() if k in merged})
        params = {k: rag_config.coerce(k, v) for k, v in merged.items()}
    ok = _spawn("reparse", did, kb_ingest.parse_and_chunk, did, params)
    return _ok({"ok": ok, "running": _is_running(did)})


@router.post("/document/embed")
async def document_embed(body: dict):
    did = int(body.get("id") or 0)
    if not did:
        return _err("缺少文件 id")
    if _is_running(did):
        return _err(f"這份文件正在「{_is_running(did)}」，請等它跑完")
    mode = (body.get("mode") or "pending").lower()
    if mode not in ("pending", "failed", "all"):
        return _err("mode 只能是 pending / failed / all")
    ok = _spawn("embedding", did, kb_ingest.embed_document, did, mode)
    return _ok({"ok": ok, "running": _is_running(did)})


@router.post("/document/activate")
async def document_activate(body: dict):
    did = int(body.get("id") or 0)
    try:
        doc = await asyncio.to_thread(kb_ingest.activate_document, did)
    except Exception as e:
        return _err(str(e))
    return _ok({"doc": doc})


@router.post("/document/enabled")
async def document_enabled(body: dict):
    did = int(body.get("id") or 0)
    doc = await asyncio.to_thread(kb_ingest.set_document_enabled, did, bool(body.get("enabled")))
    return _ok({"doc": doc})


@router.post("/document/delete")
async def document_delete(body: dict):
    did = int(body.get("id") or 0)
    if _is_running(did):
        return _err(f"這份文件正在「{_is_running(did)}」，請等它跑完再刪")
    ok = await asyncio.to_thread(kb_ingest.delete_document, did)
    return _ok({"ok": ok})


# ======================================================================
# 知識塊微調（向導第 ③ 步）
# ======================================================================

@router.post("/chunk/update")
async def chunk_update(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識塊 id")
    text = body.get("text")
    enabled = body.get("enabled")
    ok = await asyncio.to_thread(kb_db.update_chunk, cid,
                                 text if isinstance(text, str) else None,
                                 bool(enabled) if isinstance(enabled, bool) else None)
    return _ok({"ok": ok, "chunk": await asyncio.to_thread(kb_db.get_chunk, cid)})


@router.post("/chunk/split")
async def chunk_split(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識塊 id")
    at = int(body.get("at") or 0)
    c = await asyncio.to_thread(kb_db.get_chunk, cid)
    if not c:
        return _err("找不到知識塊", 404)
    text = c["text"] or ""
    if at <= 0 or at >= len(text):
        return _err(f"拆分位置要在 1 ~ {len(text) - 1} 之間")

    head = text[:at].strip()
    tail = text[at:].strip()
    if not head or not tail:
        return _err("拆分後有一邊是空的，請調整拆分位置")

    prefix = c.get("prefix") or ""
    tail_text = tail if (not prefix or tail.startswith(prefix)) else f"{prefix}\n{tail}"

    def work():
        kb_db.update_chunk(cid, head)
        doc_id = int(c["doc_id"])
        kb_db.shift_ordinals(doc_id, int(c["ordinal"]))
        new_id = kb_db.insert_chunk(
            doc_id, int(c["ordinal"]) + 1, tail_text, prefix=prefix,
            page_no=c.get("page_no"), heading_path=c.get("heading_path") or "",
            sheet=c.get("sheet"), row_from=c.get("row_from"), row_to=c.get("row_to"))
        kb_db.renumber_chunks(doc_id)
        return new_id

    try:
        new_id = await asyncio.to_thread(work)
    except Exception as e:
        return _err(f"拆分失敗：{e}")
    return _ok({"ok": True, "new_chunk_id": new_id,
                "chunks": await asyncio.to_thread(kb_db.list_chunks, int(c["doc_id"]),
                                                  False, 2000)})


@router.post("/chunk/merge")
async def chunk_merge(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識塊 id")
    c = await asyncio.to_thread(kb_db.get_chunk, cid)
    if not c:
        return _err("找不到知識塊", 404)
    siblings = await asyncio.to_thread(kb_db.list_chunks, int(c["doc_id"]), False, 5000)
    idx = next((i for i, x in enumerate(siblings) if int(x["id"]) == cid), -1)
    if idx < 0 or idx + 1 >= len(siblings):
        return _err("這已經是最後一塊了，沒有下一塊可以合併")
    nxt = siblings[idx + 1]

    def work():
        prefix = c.get("prefix") or ""
        body_next = nxt["text"] or ""
        if prefix and body_next.startswith(prefix):
            body_next = body_next[len(prefix):].lstrip("\n")
        merged = (c["text"] or "").rstrip() + "\n" + body_next.strip()
        kb_db.update_chunk(cid, merged)
        kb_db.delete_chunk(int(nxt["id"]))
        kb_db.renumber_chunks(int(c["doc_id"]))

    try:
        await asyncio.to_thread(work)
    except Exception as e:
        return _err(f"合併失敗：{e}")
    return _ok({"ok": True,
                "chunks": await asyncio.to_thread(kb_db.list_chunks, int(c["doc_id"]),
                                                  False, 2000)})


@router.post("/chunk/delete")
async def chunk_delete(body: dict):
    cid = int(body.get("id") or 0)
    c = await asyncio.to_thread(kb_db.get_chunk, cid)
    if not c:
        return _err("找不到知識塊", 404)

    def work():
        kb_db.delete_chunk(cid)
        kb_db.renumber_chunks(int(c["doc_id"]))

    await asyncio.to_thread(work)
    return _ok({"ok": True,
                "chunks": await asyncio.to_thread(kb_db.list_chunks, int(c["doc_id"]),
                                                  False, 2000)})


@router.get("/doc-types")
async def doc_types():
    """支援的格式清單（UI 上傳區提示用）。"""
    return _ok({"types": list(kb_parse.DOC_TYPES),
                "max_upload_mb": kb_ingest.MAX_UPLOAD_MB})
