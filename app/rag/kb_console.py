"""知識庫（RAG 第三層）的設定介面 API（掛在 `/console/kb` 之下）。

端點：
  GET  /console/kb/overview                 知識庫概覽（集合列表 + 模型狀態 + 統計）
  GET  /console/kb/collections              知識庫列表（含 kind：doc=文檔庫 / image=圖片庫）
  POST /console/kb/collections/create       新增知識庫（kind=doc 會自動附帶「<名>_圖片庫」）
  POST /console/kb/collections/update       更新知識庫（停/啟用會**連動圖片庫**）
  POST /console/kb/collections/delete       刪除知識庫（文檔庫連帶刪它的圖片庫）
  GET  /console/kb/params                   參數規格 + 目前值（檢索=全域、分塊=該知識庫）
  POST /console/kb/params                   寫入參數覆寫（檢索存全域／分塊存該庫，5 秒生效）
  POST /console/kb/upload                   上傳文件（可多檔）
  GET  /console/kb/documents                文件列表
  GET  /console/kb/document                 文件詳情（含分塊）
  POST /console/kb/document/preview         分塊預覽（不寫庫，調參用）
  POST /console/kb/document/reparse         重新分塊（寫庫）
  POST /console/kb/document/embed           開始 / 重跑 embedding（會連帶向量化圖片描述）
  POST /console/kb/document/activate        確認入庫（status → ready）
  POST /console/kb/document/enabled         停用 / 啟用文件
  POST /console/kb/document/delete          刪除文件
  POST /console/kb/document/move            改放到別的知識庫（圖會跟著搬到新庫的圖片庫）
  POST /console/kb/chunk/update             編輯 / 停用單一知識塊
  POST /console/kb/chunk/split              拆分知識塊
  POST /console/kb/chunk/merge              與下一塊合併
  POST /console/kb/chunk/delete             刪除知識塊
  GET  /console/kb/search                   混合檢索（調參／除錯用，不寫任何紀錄）
  POST /console/kb/search                   同上（POST 版，參數多時好用）
                                            · 帶 rewrite=1 → 先改寫查詢再多查詢檢索
                                            · history 可給上下文（純文字或 [{direction,text}]）
                                            · 候選池＝知識塊 + 圖片庫（圖的 text 是 markdown）
  GET  /console/kb/assets                   圖片列表（含來源／綁定數／描述狀態）+ 統計
  POST /console/kb/asset/upload             單獨上傳圖片（不屬於任何文件）
  POST /console/kb/asset/caption            人工寫／改描述（寫完立刻向量化、馬上可檢索）
  POST /console/kb/asset/caption/ai         按鈕觸發 AI 描述（逐張呼叫視覺模型）
  POST /console/kb/asset/embed              批次補向量化（舊資料：有描述但從未向量化）
  POST /console/kb/asset/update             啟用 / 停用單張圖（救回被誤判的裝飾圖）
  POST /console/kb/asset/delete             刪除圖片
  POST /console/kb/asset/rebind             重新綁定某份文件的圖到知識塊
  GET  /console/kb/asset/file/{id}          取圖檔（舊圖的退路；新圖走公開的 /media/…）
"""
import asyncio
import json
import logging
import os

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse, Response

from app.core import config
from app.llm import deepseek_client
from app.rag import kb_chunk
from app.rag import kb_db
from app.rag import kb_embed
from app.rag import kb_ingest
from app.rag import kb_parse
from app.rag import kb_search
from app.rag import rag_config
from app.rag.kb_parse import ParseError

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
            kb_search.invalidate()   # 分塊／向量變了，檢索索引要重載

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
               (SELECT count(*) FROM kb_document WHERE collection_id IS NULL) AS ungrouped_docs,
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
    """新增知識庫。`kind='image'` = **圖片知識庫**（放圖、用描述檢索）。

    · `kind='doc'`（預設）：文檔知識庫，同時會自動建立它的「<名>_圖片庫」。
    · `kind='image'`：圖片知識庫；`independent=1`（或不給 image_of）→ 完全獨立，
      不綁任何文檔庫；要掛在某個文檔庫底下才給 `image_of`。
    """
    name = (body.get("name") or "").strip()
    if not name:
        return _err("知識庫名稱不能是空的")
    kind = str(body.get("kind") or "doc").strip().lower()
    if kind not in ("doc", "image"):
        return _err("kind 只能是 doc（文檔知識庫）或 image（圖片知識庫）")

    image_of = _int_or_none(body.get("image_of"))
    col = await asyncio.to_thread(kb_db.create_collection, name,
                                  body.get("description") or "",
                                  kind=kind, image_of=image_of)
    out = {"collection": col}
    if kind == "doc":
        # 文檔知識庫一律帶一個圖片庫（從文件抽出來的圖都落那裡，啟停用連動）
        img = await asyncio.to_thread(kb_db.ensure_image_collection, int(col["id"]))
        out["image_collection"] = img
    kb_search.invalidate()
    return _ok(out)


@router.post("/collections/update")
async def collection_update(body: dict):
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識庫 id")
    # 啟停用要**連動圖片庫**，所以走 set_collection_enabled；其餘欄位照舊。
    if "enabled" in body:
        casc = await asyncio.to_thread(kb_db.set_collection_enabled, cid,
                                       bool(body["enabled"]))
    else:
        casc = {"cascaded": []}
    fields = {k: body[k] for k in ("name", "description", "sort_order")
              if k in body}
    if fields:
        await asyncio.to_thread(kb_db.update_collection, cid, fields)
    kb_search.invalidate()
    return _ok({"ok": True, "cascaded": casc.get("cascaded") or [],
                "collection": await asyncio.to_thread(kb_db.get_collection, cid)})


@router.post("/collections/delete")
async def collection_delete(body: dict):
    """刪知識庫。

    · 文檔庫：底下文件會變成「未分組」（資料不消失）；它的**圖片庫會一起被刪**
      （圖片庫是它的一部分）→ 圖片本身變成未分組，不會遺失。
    · 圖片庫：裡面的圖變成未分組（不會刪檔）。
    """
    cid = int(body.get("id") or 0)
    if not cid:
        return _err("缺少知識庫 id")
    col = await asyncio.to_thread(kb_db.get_collection, cid)
    if not col:
        return _err(f"找不到知識庫 #{cid}", 404)
    if not body.get("force"):
        n = await asyncio.to_thread(kb_db.query,
                                    "SELECT count(*) AS c FROM kb_document WHERE collection_id=%s",
                                    (cid,))
        a = await asyncio.to_thread(kb_db.query,
                                    "SELECT count(*) AS c FROM kb_asset WHERE collection_id=%s",
                                    (cid,))
        nd, na = int((n or [{}])[0].get("c") or 0), int((a or [{}])[0].get("c") or 0)
        if nd or na:
            return _err(f"這個知識庫底下還有 {nd} 份文件、{na} 張圖片，刪除後都會變成"
                        "「未分組」（資料不會消失）。確定要刪請再按一次。")
    ok = await asyncio.to_thread(kb_db.delete_collection, cid)
    kb_search.invalidate()
    return _ok({"ok": ok})


# ======================================================================
# 參數配置
# ======================================================================

def _params_payload(collection_id: int | None) -> dict:
    """參數規格 + 目前值。

    ⚠️ 檢索那組（group='retrieval'）與提示詞（group='prompt'）**永遠是全域值**、
    不隨 collection_id 變動；只有分塊那組才是這個知識庫的
    （collection_id 空 → 未分組文件用的全域預設）。
    `scope` 欄位讓前端知道「這一項改到哪一層」。
    """
    glob = kb_db.get_global_param_overrides()
    own = kb_db.get_param_overrides(collection_id) if collection_id else {}
    items = []
    for key, spec in rag_config.PARAM_SPECS.items():
        is_chunk = spec["group"] == "chunk"
        raw = (own if (is_chunk and collection_id) else glob).get(key)
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
            # ── 提示詞（type='text'）專用：前端要 render 多行框、標出可用變數 ──
            "required": spec.get("required") or [],
            "vars": spec.get("vars") or [],
            "rows": spec.get("rows") or 0,
            "scope": ("library" if collection_id else "unassigned") if is_chunk else "global",
        })
    return {
        "collection_id": collection_id,
        "items": items,
        "model": kb_embed.model_info(),
        "retrieval": [i for i in items if i["group"] == "retrieval"],
        "chunk": [i for i in items if i["group"] == "chunk"],
        "prompts": [i for i in items if i["group"] == "prompt"],
    }


@router.get("/params")
async def get_params(collection_id: str = ""):
    cid = int(collection_id) if str(collection_id).strip().isdigit() else None
    return _ok(await asyncio.to_thread(_params_payload, cid or None))


@router.post("/params")
async def set_params(body: dict):
    """寫參數。檢索參數與提示詞一律落到全域，分塊參數落到 `collection_id` 那個知識庫。

    所以 `collection_id` 可以留空 —— 那就是「只改全域的東西（檢索參數／提示詞）」。
    """
    raw_cid = body.get("collection_id")
    cid = int(raw_cid) if str(raw_cid if raw_cid is not None else "").strip().isdigit() else None
    cid = cid or None
    values = body.get("values") or {}

    # ★ 提示詞要先驗「必需佔位符」再寫。
    #   少了 {context} / {sentinel} 這種，讀取端雖然會退回預設，但使用者會以為
    #   「我明明存了卻沒生效」——不如當場擋下、直接告訴他少了哪一個。
    for key, raw in values.items():
        if key not in rag_config.PARAM_SPECS or raw is None:
            continue
        miss = rag_config.missing_placeholders(key, str(raw))
        if miss:
            label = rag_config.PARAM_SPECS[key]["label"]
            return _err(f"「{label}」缺少必要的變數：{'、'.join(miss)}"
                        f"（少了它模型就看不到資料／無法判定，請補回去再存）")

    n = await asyncio.to_thread(kb_db.set_params, cid, values)
    kb_search.invalidate()   # 參數／提示詞／索引快取立即失效（不用等 5 秒）
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
async def documents(collection_id: str = "", status: str = "", ungrouped: str = ""):
    # ungrouped=1 → 只列沒有歸屬知識庫的文件（collection_id IS NULL）
    cid = int(collection_id) if str(collection_id).strip().isdigit() else None
    is_ung = str(ungrouped).strip() not in ("", "0", "false", "False")
    docs = await asyncio.to_thread(kb_db.list_documents, cid, status, 500, is_ung)
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
        # 分塊參數是「按知識庫」的：基準值要取這份文件所屬知識庫的，不是全域
        # （未分組文件 collection_id 為 None，那才會落到全域那批）。
        doc = await asyncio.to_thread(kb_db.get_document, did)
        merged = await asyncio.to_thread(kb_db.load_params, (doc or {}).get("collection_id"))
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
    kb_search.invalidate()      # 剛上線的文件要能立刻被搜到
    return _ok({"doc": doc})


@router.post("/document/enabled")
async def document_enabled(body: dict):
    did = int(body.get("id") or 0)
    doc = await asyncio.to_thread(kb_ingest.set_document_enabled, did, bool(body.get("enabled")))
    kb_search.invalidate()
    return _ok({"doc": doc})


@router.post("/document/delete")
async def document_delete(body: dict):
    did = int(body.get("id") or 0)
    if _is_running(did):
        return _err(f"這份文件正在「{_is_running(did)}」，請等它跑完再刪")
    ok = await asyncio.to_thread(kb_ingest.delete_document, did)
    kb_search.invalidate()
    return _ok({"ok": ok})


@router.post("/document/move")
async def document_move(body: dict):
    """改放到別的知識庫（`collection_id` 給 null / 0 = 未分組）。

    ⚠️ 換知識庫會改變它套用的**分塊參數**（檢索參數是全庫共用的，不受影響），
    所以索引與參數快取仍要失效。
    """
    did = int(body.get("id") or 0)
    if not did:
        return _err("缺少文件 id")
    doc = await asyncio.to_thread(kb_db.get_document, did)
    if not doc:
        return _err("找不到這份文件", 404)

    raw = body.get("collection_id")
    cid = None
    if raw not in (None, "", 0, "0"):
        cid = _int_or_none(raw)
        if not cid:
            return _err("collection_id 要是數字，或留空表示未分組")
        target = await asyncio.to_thread(kb_db.get_collection, cid)
        if not target:
            return _err(f"找不到知識庫 #{cid}")

    await asyncio.to_thread(kb_db.update_document, did, {"collection_id": cid})
    # ★ 文件的圖要跟著搬家 —— 它們屬於「新知識庫的圖片庫」。
    try:
        new_img = await asyncio.to_thread(_image_cid_for, cid)
        await asyncio.to_thread(kb_db.query,
                                "UPDATE kb_asset SET collection_id=%s WHERE doc_id=%s",
                                (new_img, did))
    except Exception as e:
        log.debug("搬移文件時同步圖片所屬庫失敗（不影響搬移）：%s", e)
    kb_search.invalidate()
    return _ok({"ok": True, "doc": await asyncio.to_thread(kb_db.get_document, did)})


def _image_cid_for(cid: int | None) -> int | None:
    if not cid:
        return None
    got = kb_db.ensure_image_collection(int(cid))
    return int(got["id"]) if got else None


# ======================================================================
# 知識塊微調（向導第 ③ 步）
# ======================================================================

def _int_or_none(v):
    s = str(v if v is not None else "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _float_or_none(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


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
    kb_search.invalidate()
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
    kb_search.invalidate()
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
    kb_search.invalidate()
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
    kb_search.invalidate()
    return _ok({"ok": True,
                "chunks": await asyncio.to_thread(kb_db.list_chunks, int(c["doc_id"]),
                                                  False, 2000)})


# ======================================================================
# 檢索（命中測試／調參用；不寫任何紀錄、不改任何開關）
# ======================================================================

def _run_search(body: dict) -> dict:
    """同步檢索（不改任何設定、不寫任何紀錄）。`queries` 已算好時走多查詢版本。"""
    cid = _int_or_none(body.get("collection_id"))
    queries = body.get("queries")
    if not queries:
        queries = [(body.get("query") or body.get("q") or "").strip()]
    fn = kb_search.search_multi if len(queries) > 1 else kb_search.search
    res = fn(
        queries if len(queries) > 1 else queries[0],
        collection_id=cid,
        top_k=_int_or_none(body.get("top_k")),
        min_score=_float_or_none(body.get("min_score")),
        max_per_doc=_int_or_none(body.get("max_per_doc")),
        max_context_chars=_int_or_none(body.get("max_context_chars")),
    )
    out = res.as_dict(candidates=int(body.get("candidates") or 30))
    out["context"] = res.context(int(res.params.get("max_context_chars") or 0))
    return out


async def _search_any(body: dict) -> dict:
    """檢索端點的共同入口。帶 `rewrite=1` 就先用上下文改寫出 N 條查詢再一起檢索。

    這裡是**調參用**的端點：改寫結果會直接回在 `queries` 裡，方便肉眼確認提示詞
    有沒有抓到同音錯別字、有沒有把指代補齊（`history` 可傳純文字或
    `[{direction,text},…]`，不傳就沒有上下文可補）。
    """
    body = dict(body)
    query = (body.get("query") or body.get("q") or "").strip()
    queries = [query]
    if (body.get("rewrite") or body.get("query_rewrite")) and query:
        raw = body.get("history")
        if isinstance(raw, list):
            from app.llm import reply_engine
            history_text = reply_engine.format_history(raw)
        else:
            history_text = str(raw or "")
        try:
            from app.llm import deepseek_client
            extra = await deepseek_client.DeepSeekClient().rewrite_rag_queries(
                query, history_text, count=rag_config.RAG_QUERY_REWRITE_N)
            queries += extra
        except Exception as e:
            log.warning("查詢改寫失敗（改用原問句）: %s", e)
    body["queries"] = queries
    return await asyncio.to_thread(_run_search, body)


@router.get("/search")
async def search_get(q: str = "", collection_id: str = "", top_k: str = "",
                     min_score: str = "", candidates: int = 30,
                     rewrite: str = "", history: str = ""):
    return _ok(await _search_any({
        "query": q, "collection_id": collection_id, "top_k": top_k,
        "min_score": min_score, "candidates": candidates,
        "rewrite": rewrite, "history": history,
    }))


@router.post("/search")
async def search_post(body: dict):
    return _ok(await _search_any(body))


@router.get("/index-stats")
async def index_stats(collection_id: str = ""):
    """索引目前的規模（幾塊 / 幾份文件 / 維度），方便確認「剛上線的搜得到」。"""
    cid = _int_or_none(collection_id)
    stats = await asyncio.to_thread(kb_search.index_stats, cid)
    return _ok({"stats": stats})


@router.get("/doc-types")
async def doc_types():
    """支援的格式清單（UI 上傳區提示用）。"""
    return _ok({"types": list(kb_parse.DOC_TYPES),
                "max_upload_mb": kb_ingest.MAX_UPLOAD_MB})


# ======================================================================
# 圖片資產（知識庫的「圖」）
#
# 兩條來源：從文件抽出來的（一定會綁到知識塊）／使用者在這一頁單獨上傳的。
# 描述**預設由人工寫**；「AI 描述」是按鈕觸發（不自動跑，避免上傳大量圖時暴走）。
# ======================================================================

def _asset_path(row: dict) -> str:
    """圖檔的實際路徑。新資料在公開目錄（`media/kb_assets/`），舊資料在文件資料夾裡。"""
    rel = str(row.get("rel_path") or "").replace("\\", "/")
    base = os.path.basename(rel)
    if base and os.path.exists(os.path.join(rag_config.RAG_MEDIA_DIR, base)):
        return os.path.join(rag_config.RAG_MEDIA_DIR, base)
    return os.path.join(config.MEDIA_DIR, rel)


def _asset_view(row: dict) -> dict:
    """給前端的圖片視圖（含取圖 URL）。

    ★ URL 優先用**公開靜態路徑**（`/media/xxx.png`）—— 這樣它跟寫進 chunk 的
    markdown（`![題注](/media/xxx.png)`）是同一條路，前端與 LLM 看到的圖一致；
    還沒搬到公開目錄的舊圖才退回登入端點。
    """
    d = dict(row)
    d["url"] = (rag_config.media_url_of_rel(row.get("rel_path") or "")
                or f"/console/kb/asset/file/{row['id']}")
    d["from_doc"] = bool(row.get("doc_id"))
    return d


@router.get("/assets")
async def list_assets(collection_id: str = "", doc_id: str = "",
                      include_filtered: str = "1", ungrouped: str = ""):
    """列圖片（可依知識庫／文件過濾）+ 統計。

    `include_filtered=0` → 只看「像內容圖」的（預設含被啟發式判為裝飾的，
    方便使用者複查有沒有誤殺 —— 誤判可以手動啟用）。
    """
    cid = _int_or_none(collection_id)
    did = _int_or_none(doc_id)
    inc = str(include_filtered).strip().lower() not in ("0", "false", "no", "")
    ungr = str(ungrouped).strip().lower() in ("1", "true", "yes", "on")

    def _run():
        rows = kb_db.list_assets(collection_id=cid, doc_id=did,
                                 include_filtered=inc, ungrouped=ungr)
        return {"assets": [_asset_view(r) for r in rows],
                "stats": kb_db.asset_stats(collection_id=cid, doc_id=did),
                "include_filtered": inc}
    return _ok(await asyncio.to_thread(_run))


@router.post("/asset/upload")
async def upload_assets(files: list[UploadFile] = File(...),
                        collection_id: str = Form("")):
    """單獨上傳圖片（不屬於任何文件，只掛在知識庫底下）。"""
    cid = _int_or_none(collection_id)
    out = []
    for f in files:
        raw = await f.read()
        if not raw:
            out.append({"ok": False, "error": "檔案是空的"})
            continue
        try:
            row = await asyncio.to_thread(kb_ingest.save_uploaded_asset,
                                          f.filename or "image.png", raw,
                                          f.content_type or "", cid)
            out.append({"ok": True, "asset": _asset_view(row)})
        except Exception as e:
            log.warning("圖片上傳失敗 %s: %s", f.filename, e)
            out.append({"ok": False, "filename": f.filename, "error": str(e)})
    kb_search.invalidate()
    return _ok({"results": out})


@router.post("/asset/caption")
async def set_asset_caption(body: dict):
    """人工寫／改描述（source 固定 manual —— 之後 AI 按鈕不會覆蓋它）。

    ★ 寫完立刻向量化 + 讓檢索索引失效 —— 這樣「改完描述馬上就查得到」，
    不用等 5 秒 TTL（圖片能被檢索就是靠這個描述）。
    """
    aid = _int_or_none(body.get("id"))
    if not aid:
        return _err("缺少圖片 id")
    row = await asyncio.to_thread(kb_db.set_asset_caption, aid,
                                  str(body.get("caption") or ""), "manual")
    if not row:
        return _err(f"圖片不存在：#{aid}", 404)
    try:
        await asyncio.to_thread(_embed_one_asset, aid)
    except Exception as e:
        log.debug("圖片描述向量化失敗（不影響儲存）：%s", e)
    kb_search.invalidate()
    row = await asyncio.to_thread(kb_db.get_asset, aid) or row
    return _ok({"ok": True, "asset": _asset_view(row)})


def _embed_one_asset(aid: int) -> None:
    """把單張圖的描述向量化（若它還沒向量化）。"""
    row = kb_db.get_asset(aid)
    if not row or not (row.get("caption") or "").strip():
        return
    if row.get("embedding_status") == "done":
        return
    provider = kb_embed.get_provider()
    vec = provider.encode_one(kb_chunk.asset_embed_text(row))
    if vec:
        kb_db.save_asset_embeddings([(int(aid), vec)], provider.model_name, provider.dim)


@router.post("/asset/caption/ai")
async def ai_asset_caption(body: dict):
    """按鈕觸發：用視覺模型看圖寫描述。

    ⚠️ **不會覆蓋人工寫的**（`caption_source='manual'`）—— 除非帶 `force=1`。
    批次時逐張跑（併發會撞閘道限流），並回報每一張的結果。
    """
    ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
    ids = [_int_or_none(x) for x in ids]
    ids = [i for i in ids if i]
    if not ids:
        return _err("缺少圖片 id")
    if len(ids) > 50:
        return _err("一次最多 50 張（每張都要呼叫模型，量大請分批）")
    force = str(body.get("force") or "").strip().lower() in ("1", "true", "yes", "on")

    results = []
    for aid in ids:
        row = await asyncio.to_thread(kb_db.get_asset, aid)
        if not row:
            results.append({"id": aid, "ok": False, "error": "圖片不存在"})
            continue
        if row.get("caption_source") == "manual" and not force:
            results.append({"id": aid, "ok": False, "skipped": True,
                            "error": "已有手工描述（要覆蓋請帶 force=1）"})
            continue
        path = _asset_path(row)
        try:
            data = await asyncio.to_thread(lambda: open(path, "rb").read())
        except Exception as e:
            results.append({"id": aid, "ok": False, "error": f"讀不到圖檔（{e}）"})
            continue
        hint = " ｜ ".join(x for x in (row.get("caption"), row.get("filename")) if x)
        try:
            text = await deepseek_client.DeepSeekClient().describe_image(
                data, row.get("mime") or "image/png", hint=hint)
        except Exception as e:
            await asyncio.to_thread(kb_db.update_asset, aid,
                                   caption_status="failed")
            results.append({"id": aid, "ok": False, "error": f"AI 描述失敗：{e}"})
            continue
        updated = await asyncio.to_thread(kb_db.set_asset_caption, aid, text, "ai")
        try:
            await asyncio.to_thread(_embed_one_asset, aid)     # 描述→向量（能被檢索）
        except Exception as e:
            log.debug("圖片描述向量化失敗（不影響描述）：%s", e)
        results.append({"id": aid, "ok": True, "caption": text,
                        "asset": _asset_view(updated or row)})
    ok_n = len([r for r in results if r.get("ok")])
    kb_search.invalidate()
    return _ok({"ok": ok_n > 0, "done": ok_n, "total": len(ids), "results": results})


@router.post("/asset/embed")
async def embed_assets(body: dict):
    """批次把「有描述但還沒向量化」的圖送進 embedding。

    正常不需要手動按 —— 寫描述 / AI 描述 / 文件向量化時都會自動連帶做。
    這裡是給「舊資料（描述早就有了但從來沒向量化）」補課用的。
    """
    cid = _int_or_none(body.get("collection_id"))
    did = _int_or_none(body.get("doc_id"))
    res = await asyncio.to_thread(kb_ingest.embed_assets_for,
                                  doc_id=did, collection_id=cid)
    kb_search.invalidate()
    return _ok({**res, "ok": True})


@router.post("/asset/update")
async def update_asset(body: dict):
    """啟用 / 停用（被誤判成裝飾圖時，這裡可以把它救回來）。"""
    aid = _int_or_none(body.get("id"))
    if not aid:
        return _err("缺少圖片 id")
    fields = {}
    if "enabled" in body:
        fields["enabled"] = bool(body.get("enabled"))
    if "collection_id" in body:
        fields["collection_id"] = _int_or_none(body.get("collection_id"))
    if not fields:
        return _err("沒有要更新的欄位")
    ok = await asyncio.to_thread(kb_db.update_asset, aid, **fields)
    row = await asyncio.to_thread(kb_db.get_asset, aid)
    if not row:
        return _err(f"圖片不存在：#{aid}", 404)
    if row.get("enabled"):
        try:
            await asyncio.to_thread(_embed_one_asset, aid)
        except Exception as e:
            log.debug("圖片描述向量化失敗（不影響儲存）：%s", e)
    kb_search.invalidate()
    return _ok({"ok": ok, "asset": _asset_view(row)})


@router.post("/asset/delete")
async def delete_asset(body: dict):
    aid = _int_or_none(body.get("id"))
    if not aid:
        return _err("缺少圖片 id")
    row = await asyncio.to_thread(kb_db.get_asset, aid)
    ok = await asyncio.to_thread(kb_db.delete_asset, aid)
    # 只有「單獨上傳 / 抽出但沒文件」的圖才順手刪檔（文件抽出的圖跟文件共存亡）
    if ok and row and not row.get("doc_id"):
        try:
            os.remove(_asset_path(row))
        except Exception as e:
            log.debug("刪圖片檔失敗（忽略）：%s", e)
    kb_search.invalidate()
    return _ok({"ok": ok})


@router.get("/asset/file/{asset_id}")
async def asset_file(asset_id: int):
    """取圖片檔。

    刻意**走這條**而不是公開的 `/files/...`：一是可以統一換成有鑑權的讀法，
    二是這裡會擋掉「檔案不存在」的情況，前端拿到的永遠是圖片或 404。
    """
    row = await asyncio.to_thread(kb_db.get_asset, asset_id)
    if not row:
        return _err(f"圖片不存在：#{asset_id}", 404)
    path = _asset_path(row)
    if not os.path.exists(path):
        return _err("圖檔遺失（可能已被刪除）", 404)
    return FileResponse(path, media_type=row.get("mime") or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.post("/asset/rebind")
async def rebind_assets(body: dict):
    """重新綁定某份文件的圖（重跑分塊後、或綁定看起來不對時用）。"""
    did = _int_or_none(body.get("doc_id"))
    if not did:
        return _err("缺少 doc_id")
    n = await asyncio.to_thread(kb_ingest.rebind_doc_assets, did)
    return _ok({"ok": True, "bindings": n})
