"""知識庫入庫編排：上傳 → 解析 → 分塊 →（微調）→ embedding → 確認入庫。

設計依據：RAG_DESIGN.md §5（向導五步）。

狀態機（`kb_document.status`）：
    draft ──解析/分塊──► draft（有塊，待確認）──embedding──► draft（向量齊，待確認）──► ready
      └─ 任何一步失敗 → failed（error 存人話原因）

為什麼 ④ 之後還停在 `draft`：設計稿 §5 的「確認入庫」是一個**明確的人工關卡** ——
向量都算好了但還沒上線，客服可以先用「命中測試」驗一下再按入庫。

⚠️ 這裡的函式都是**同步**的（CPU 密集 / 阻塞 IO），由呼叫端用 `asyncio.to_thread` 包。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import uuid

from app.core import config
from app.rag import kb_chunk
from app.rag import kb_db
from app.rag import kb_embed
from app.rag import kb_parse
from app.rag import rag_config
from app.rag.kb_parse import ParseError

log = logging.getLogger("wechat-forwarder")

# 單檔上傳大小上限（MB）—— 太大的 PDF 解析會吃掉大量記憶體
MAX_UPLOAD_MB = int(os.getenv("RAG_MAX_UPLOAD_MB") or "50")

# 解析結果快取：改分塊參數重新預覽時不必每次重讀 PDF（key = doc_id）
_PARSE_CACHE: "dict[int, tuple[str, kb_parse.ParsedDoc]]" = {}
_PARSE_CACHE_MAX = 4
_PARSE_LOCK = threading.Lock()


class IngestError(Exception):
    """入庫流程的錯誤（reason 為穩定錯誤碼 / 人話訊息）。"""


def parse_cached(doc: dict) -> kb_parse.ParsedDoc:
    """解析文件，同一份檔案（sha256 相同）在記憶體快取內只解析一次。

    重新分塊預覽會反覆呼叫解析 —— 對 50MB 的 PDF 每次重讀都太慢。
    快取只保留最近 `_PARSE_CACHE_MAX` 份，避免吃記憶體。
    """
    did = int(doc["id"])
    key = f"{doc.get('source_path')}:{doc.get('sha256') or ''}"
    with _PARSE_LOCK:
        hit = _PARSE_CACHE.get(did)
        if hit and hit[0] == key:
            return hit[1]
    parsed = kb_parse.parse(doc["source_path"], doc.get("doc_type") or "",
                            doc.get("filename") or "")
    with _PARSE_LOCK:
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)), None)
        _PARSE_CACHE[did] = (key, parsed)
    return parsed


def drop_parse_cache(doc_id: int) -> None:
    with _PARSE_LOCK:
        _PARSE_CACHE.pop(int(doc_id), None)


def _safe_name(filename: str) -> str:
    """只留檔名、去掉路徑分隔與危險字元（防目錄穿越）。"""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    name = name.lstrip(".") or "upload.bin"
    return name[:180]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


# ======================================================================
# ① 上傳
# ======================================================================

def save_upload(filename: str, content: bytes, mime: str = "",
                collection_id: int | None = None) -> dict:
    """把上傳的檔案落地到 `media/kb/<uuid>/`，並建立 `kb_document`（draft）。

    重複偵測：同一知識庫內若已有相同 sha256，新版會把舊版標成
    `disabled` + `superseded_by`（避免新舊答案打架，設計稿 §16.8）。
    """
    if not content:
        raise IngestError("檔案是空的")

    size_mb = len(content) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise IngestError(f"檔案太大（{_human_size(len(content))}），上限 {MAX_UPLOAD_MB}MB")

    dt = kb_parse.detect_type(filename, mime)
    if not dt:
        raise ParseError("unsupported_format",
                         f"不支援的格式：{os.path.splitext(filename or '')[1] or filename}")

    sha = hashlib.sha256(content).hexdigest()
    dup = kb_db.find_document_by_sha(sha, collection_id)

    folder = os.path.join(rag_config.RAG_KB_DIR, uuid.uuid4().hex)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _safe_name(filename))
    with open(path, "wb") as f:
        f.write(content)

    title = os.path.splitext(os.path.basename(filename or ""))[0] or "未命名文件"
    doc = kb_db.create_document(
        filename=_safe_name(filename), title=title, mime=mime or "",
        size_bytes=len(content), sha256=sha, source_path=path,
        doc_type=dt, collection_id=collection_id,
    )

    if dup:
        kb_db.update_document(doc["id"], {"version": int(dup.get("version") or 1) + 1})
        kb_db.update_document(dup["id"], {"superseded_by": doc["id"],
                                          "status": kb_db.STATUS_DISABLED,
                                          "enabled": False})
        doc = kb_db.get_document(doc["id"]) or doc
        log.info("同一檔案重複上傳：doc#%s 取代 doc#%s", doc["id"], dup["id"])

    log.info("知識庫上傳：%s（%s，%s，doc#%s）", doc["filename"], dt,
             _human_size(len(content)), doc["id"])
    return {"doc": doc, "duplicate_of": dup["id"] if dup else None}


# ======================================================================
# ② 解析 + ③ 分塊
# ======================================================================

def preview_chunks(doc_id: int, params: dict | None = None, limit: int = 30) -> dict:
    """只用來「看」——依給定參數算出分塊結果，**不寫任何資料**。

    客服在向導第 ③ 步調塊長 / 重疊時，先預覽再決定要不要重建，
    避免「改個滑桿就把辛苦手改的塊全刪了」。
    """
    doc = kb_db.get_document(doc_id)
    if not doc:
        raise IngestError(f"文件不存在：#{doc_id}")
    use = kb_db.load_params(doc.get("collection_id"))
    if params:
        use.update({k: v for k, v in params.items() if k in use})
        use = {k: rag_config.coerce(k, v) for k, v in use.items()}
    parsed = parse_cached(doc)
    chunks = kb_chunk.chunk_blocks(parsed.blocks, doc_type=doc.get("doc_type") or "",
                                   title=doc.get("title") or "", params=use)
    sizes = [c.char_count for c in chunks] or [0]
    return {
        "doc_id": doc_id,
        "strategy": kb_chunk.resolve_strategy(doc.get("doc_type") or "",
                                             use.get("chunk_strategy", "auto")),
        "params": use,
        "count": len(chunks),
        "char_count": sum(sizes),
        "min_chars": min(sizes),
        "max_chars": max(sizes),
        "avg_chars": round(sum(sizes) / len(sizes), 1),
        "meta": parsed.meta,
        "chunks": [c.as_dict() for c in chunks[:limit]],
    }


# ======================================================================
# 圖片資產：落地 + 去重 + 綁定分塊
# ======================================================================

def _media_rel(path: str) -> str:
    """絕對路徑 → 相對 `media/` 的路徑（給 `/files/<rel>` 取用）。"""
    try:
        rel = os.path.relpath(path, config.MEDIA_DIR)
    except Exception:
        rel = path
    return rel.replace(os.sep, "/")


def _assets_dir(doc: dict) -> str:
    """圖片落地目錄：跟來源檔同一個 uuid 資料夾底下的 `assets/`。

    這樣「刪文件」時把整個資料夾砍掉就一併清了圖（見 delete_document）。
    """
    src = doc.get("source_path") or ""
    base = (os.path.dirname(src) if src
            else os.path.join(rag_config.RAG_KB_DIR, f"doc_{doc.get('id')}"))
    return os.path.join(base, "assets")


def save_doc_assets(doc: dict, parsed) -> list[tuple]:
    """把解析出來的圖落地存檔 + 寫 `kb_asset`（同 sha256 去重）。

    回傳 `[(ParsedAsset, asset_row), …]`，**順序與 `parsed.assets` 一致** ——
    綁定分塊時要靠這個對應關係（ParsedAsset 上的 anchor_block 不會存進 DB）。

    ⚠️ 沒有任何 AI 呼叫：題注有就填進去，沒有就留空等人填／等按鈕生成。
    """
    out: list[tuple] = []
    assets = list(getattr(parsed, "assets", None) or [])
    if not assets:
        return out
    cid = doc.get("collection_id")
    did = doc.get("id")
    folder = _assets_dir(doc)
    made_dir = False

    for pa in assets:
        sha = pa.sha256
        row = kb_db.find_asset_by_sha(sha, doc_id=did)
        if row:                       # 同一張圖（例如每頁都有的 LOGO）只存一份
            out.append((pa, row))
            continue
        if not made_dir:
            os.makedirs(folder, exist_ok=True)
            made_dir = True
        name = f"{sha[:16]}.{pa.ext}"          # 用 sha 命名：天然去重、無中文檔名問題
        abs_path = os.path.join(folder, name)
        try:
            if not os.path.exists(abs_path):
                with open(abs_path, "wb") as f:
                    f.write(pa.data)
        except Exception as e:
            log.warning("圖片落地失敗（跳過）%s: %s", name, e)
            continue
        row = kb_db.create_asset(
            rel_path=_media_rel(abs_path), collection_id=cid, doc_id=did,
            page_no=pa.page_no, filename=pa.name or name, mime=pa.mime,
            size_bytes=len(pa.data), width=pa.width, height=pa.height, sha256=sha,
            caption=pa.caption, caption_source=("doc" if pa.caption else ""),
            bbox=pa.bbox, enabled=not pa.filtered_reason,
            filtered_reason=pa.filtered_reason)
        out.append((pa, row))
    if out:
        log.info("文件 #%s 圖片：抽出 %d、落地 %d、有題注 %d",
                 did, len(assets), len(out),
                 len([p for p, _ in out if p.caption]))
    return out


def bind_doc_assets(created: list[tuple], chunks: list, chunk_ids: list[int]) -> int:
    """把圖片綁到知識塊 —— **這是「命中後圖片結合輸出」的關鍵**。

    優先按解析階段記下的 `anchor_block`（圖片最靠近哪個 Block）落在哪一塊的區間；
    沒有錨點（例如整份文件只有圖）才退化成「同頁碼的塊」。
    """
    pairs: list[tuple[int, int]] = []
    for pa, row in created or []:
        hits: list[int] = []
        if pa.anchor_block is not None:
            for i, c in enumerate(chunks):
                if c.block_from is None or c.block_to is None:
                    continue
                if c.block_from <= pa.anchor_block <= c.block_to:
                    hits.append(chunk_ids[i])
        if not hits and pa.page_no is not None:
            hits = [chunk_ids[i] for i, c in enumerate(chunks) if c.page_no == pa.page_no]
        for cid in hits:
            pairs.append((int(row["id"]), int(cid)))
    if not pairs:
        return 0
    kb_db.executemany("INSERT INTO kb_asset_chunk(asset_id, chunk_id) VALUES (%s, %s) "
                      "ON CONFLICT DO NOTHING", pairs)
    return len(pairs)


# 可上傳的圖片格式（單獨上傳走這條）
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}


def save_uploaded_asset(filename: str, content: bytes, mime: str = "",
                        collection_id: int | None = None) -> dict:
    """使用者**單獨上傳**的一張圖（不屬於任何文件，只掛在知識庫底下）。

    與文件抽出來的圖共用同一張表；差別是 `doc_id IS NULL`，
    所以不會（也不需要）綁到知識塊 —— 它靠自己的描述被檢索到。
    """
    if not content:
        raise IngestError("圖片是空的")
    ext = kb_parse._img_ext(filename or "", content)
    if ext.lstrip(".").lower() not in IMAGE_EXTS:
        raise ParseError("unsupported_format",
                         f"不支援的圖片格式：{os.path.splitext(filename or '')[1] or filename}")
    w, h = kb_parse._img_pixels(content)
    if not w or not h:
        raise IngestError("讀不出圖片尺寸，可能不是有效的圖片檔")

    sha = hashlib.sha256(content).hexdigest()
    dup = kb_db.find_asset_by_sha(sha, collection_id=collection_id)
    if dup and not dup.get("doc_id"):
        return dup                                  # 同一張圖上傳兩次 → 直接沿用

    folder = os.path.join(rag_config.RAG_KB_DIR, "uploads")
    os.makedirs(folder, exist_ok=True)
    name = f"{sha[:16]}.{ext}"
    abs_path = os.path.join(folder, name)
    if not os.path.exists(abs_path):
        with open(abs_path, "wb") as f:
            f.write(content)

    a = kb_parse.ParsedAsset(data=content, ext=ext, name=filename or name,
                             width=w, height=h)
    a.filtered_reason = kb_parse._decor_reason(a, 0.0, 0.0)
    row = kb_db.create_asset(
        rel_path=_media_rel(abs_path), collection_id=collection_id, doc_id=None,
        filename=filename or name, mime=mime or a.mime, size_bytes=len(content),
        width=w, height=h, sha256=sha, enabled=not a.filtered_reason,
        filtered_reason=a.filtered_reason)
    log.info("單獨上傳圖片：%s（%dx%d，asset#%s）", filename, w, h, row["id"])
    return row


def rebind_doc_assets(doc_id: int) -> int:
    """重算某份文件的「圖 ↔ 知識塊」綁定（重跑分塊後、或綁定看起來不對時用）。

    先試著用解析快取裡的 `anchor_block`（最準）；拿不到或塊數對不上，
    就退化成「同頁碼的塊」——PDF 一定有頁碼，DOCX 沒有頁碼則只能靠錨點。
    """
    doc = kb_db.get_document(doc_id)
    if not doc:
        raise IngestError(f"文件不存在：#{doc_id}")
    chunk_rows = kb_db.list_chunks(doc_id)
    kb_db.clear_doc_bindings(doc_id)
    if not chunk_rows:
        return 0

    try:
        parsed = parse_cached(doc)
        use = doc.get("parse_params") or kb_db.load_params(doc.get("collection_id"))
        chunks = kb_chunk.chunk_blocks(parsed.blocks, doc_type=doc.get("doc_type") or "",
                                       title=doc.get("title") or "", params=use)
        if len(chunks) == len(chunk_rows):
            created = [(pa, {"id": r["id"]}) for pa, r in
                       zip(parsed.assets, _assets_in_order(doc_id))]
            if len(created) == len(parsed.assets):
                return bind_doc_assets(created, chunks, [r["id"] for r in chunk_rows])
    except Exception as e:
        log.debug("重綁時解析快取不可用（改用頁碼）：%s", e)

    # 退路：按頁碼
    pairs = []
    for a in kb_db.list_assets(doc_id=doc_id):
        if a.get("page_no") is None:
            continue
        for c in chunk_rows:
            if c.get("page_no") == a["page_no"]:
                pairs.append((int(a["id"]), int(c["id"])))
    if pairs:
        kb_db.executemany("INSERT INTO kb_asset_chunk(asset_id, chunk_id) VALUES (%s, %s) "
                          "ON CONFLICT DO NOTHING", pairs)
    return len(pairs)


def _assets_in_order(doc_id: int) -> list[dict]:
    """文件抽出圖的**解析順序**（anchor_block 的索引就是對到這個順序）。

    ⚠️ `list_assets` 排的是 (page_no, id)，而 `parsed.assets` 是解析順序 ——
    兩者不一定一致，所以要用 id 排序還原「建立順序」。
    """
    rows = kb_db.list_assets(doc_id=doc_id)
    return sorted(rows, key=lambda r: int(r["id"]))


def parse_and_chunk(doc_id: int, params: dict | None = None) -> dict:
    """解析檔案並切塊（整批取代舊塊）。回傳更新後的文件。

    ⚠️ 重新分塊會**刪掉舊塊**（含客服手改的內容）。呼叫端要先確認
    （`kb_db.chunk_stats()['edited']` > 0 時由 API 層引導使用者）。
    """
    doc = kb_db.get_document(doc_id)
    if not doc:
        raise IngestError(f"文件不存在：#{doc_id}")

    cid = doc.get("collection_id")
    use = params or kb_db.load_params(cid)
    job = kb_db.create_job(doc_id, "parse")
    try:
        kb_db.update_document(doc_id, {"status": kb_db.STATUS_PARSING, "progress": 5,
                                       "error": None})
        parsed = parse_cached(doc)
        kb_db.update_document(doc_id, {"status": kb_db.STATUS_CHUNKING, "progress": 45,
                                       "parse_meta": parsed.meta})

        chunks = kb_chunk.chunk_blocks(parsed.blocks, doc_type=doc.get("doc_type") or "",
                                       title=doc.get("title") or "", params=use)
        if not chunks:
            raise ParseError("no_text_layer", "解析出 0 個知識塊（可能整份都是圖片）")

        kb_db.replace_chunks(doc_id, chunks)
        total_chars = sum(c.char_count for c in chunks)

        # ★ 圖片：落地 → 去重 → 綁到剛產生的知識塊
        created = save_doc_assets(doc, parsed)
        kb_db.clear_doc_bindings(doc_id)      # 舊塊已整批換掉，舊綁定要清
        rows = kb_db.list_chunks(doc_id)      # 依 ordinal，與 chunks 同序
        n_bind = bind_doc_assets(created, chunks, [r["id"] for r in rows])

        kb_db.update_document(doc_id, {
            "status": kb_db.STATUS_DRAFT, "progress": 0, "error": None,
            "chunk_count": len(chunks), "char_count": total_chars,
            "parse_params": use,
        })
        kb_db.finish_job(job, True)
        log.info("文件 #%s 分塊完成：%d 塊 / %d 字 / 圖片 %d 張（綁定 %d）",
                 doc_id, len(chunks), total_chars, len(created), n_bind)
    except Exception as e:
        reason = e.reason if isinstance(e, ParseError) else "parse_failed"
        msg = kb_parse.describe_error(reason) if isinstance(e, ParseError) else str(e)
        kb_db.update_document(doc_id, {"status": kb_db.STATUS_FAILED, "error": msg})
        kb_db.finish_job(job, False, f"{reason}: {e}")
        log.warning("文件 #%s 解析/分塊失敗（%s）：%s", doc_id, reason, e)
        raise
    return kb_db.get_document(doc_id) or doc


# ======================================================================
# ④ embedding
# ======================================================================

def embed_document(doc_id: int, mode: str = "pending", on_progress=None) -> dict:
    """向量化。`mode`：pending（預設，含 pending+failed）/ failed（只重跑失敗）/ all（全部重算）。

    `on_progress(done, total)` 可選，用來即時回報（每批呼叫一次）。
    """
    doc = kb_db.get_document(doc_id)
    if not doc:
        raise IngestError(f"文件不存在：#{doc_id}")

    all_chunks = kb_db.list_chunks(doc_id)
    if mode == "failed":
        targets = [c for c in all_chunks if c["enabled"] and c["embedding_status"] == "failed"]
    elif mode == "all":
        targets = [c for c in all_chunks if c["enabled"]]
    else:
        targets = [c for c in all_chunks if c["enabled"] and c["embedding_status"] != "done"]

    if not targets:
        return {"total": 0, "done": 0, "failed": 0, "message": "沒有需要向量化的知識塊"}

    job = kb_db.create_job(doc_id, "embed")
    keep_status = doc.get("status")
    total = len(targets)
    done = 0
    failed_ids: list[int] = []
    provider = kb_embed.get_provider()
    batch = kb_embed.DEFAULT_BATCH

    kb_db.update_document(doc_id, {"status": kb_db.STATUS_EMBEDDING, "progress": 1,
                                   "error": None})
    try:
        for i in range(0, total, batch):
            group = targets[i:i + batch]
            texts = [c["text"] for c in group]
            try:
                vecs = provider.encode(texts, batch_size=batch)
            except Exception as e:
                log.error("文件 #%s 向量化失敗：%s", doc_id, e)
                kb_db.mark_embedding_failed([c["id"] for c in group])
                failed_ids += [c["id"] for c in group]
                done += len(group)
                _set_progress(doc_id, done, total, on_progress)
                continue
            items = [(c["id"], v) for c, v in zip(group, vecs) if v]
            kb_db.save_embeddings(items, provider.model_name, provider.dim)
            done += len(group)
            _set_progress(doc_id, done, total, on_progress)

        stats = kb_db.chunk_stats(doc_id)
        ok = int(stats.get("done") or 0) > 0
        # 回到 draft：向量齊了但**還沒上線**，等客服按「確認入庫」
        kb_db.update_document(doc_id, {
            "status": kb_db.STATUS_DRAFT if ok else kb_db.STATUS_FAILED,
            "progress": 100 if ok else 0,
            "error": None if ok else "向量化失敗（模型無法載入或記憶體不足）",
        })
        kb_db.finish_job(job, ok, "" if ok else "embedding 全部失敗")
    except Exception as e:
        kb_db.update_document(doc_id, {"status": kb_db.STATUS_FAILED, "progress": 0,
                                       "error": f"向量化中斷：{e}"})
        kb_db.finish_job(job, False, str(e))
        raise

    result = {"total": total, "done": done - len(failed_ids), "failed": len(failed_ids),
              "keep_status": keep_status, "stats": kb_db.chunk_stats(doc_id)}
    log.info("文件 #%s 向量化：%d/%d 成功、%d 失敗", doc_id, result["done"], total,
             result["failed"])
    return result


def _set_progress(doc_id: int, done: int, total: int, on_progress=None) -> None:
    pct = max(1, min(99, int(done * 100 / max(1, total))))
    try:
        kb_db.update_document(doc_id, {"progress": pct})
    except Exception as e:
        log.debug("更新進度失敗: %s", e)
    if on_progress:
        try:
            on_progress(done, total)
        except Exception:
            pass


# ======================================================================
# ⑤ 確認入庫 / 停用
# ======================================================================

def activate_document(doc_id: int) -> dict:
    """確認入庫：只切 status → ready（設計稿 §5：不會有半成品上線）。"""
    doc = kb_db.get_document(doc_id)
    if not doc:
        raise IngestError(f"文件不存在：#{doc_id}")
    stats = kb_db.chunk_stats(doc_id)
    if int(stats.get("done") or 0) <= 0:
        raise IngestError("還沒有已向量化的知識塊，請先執行 embedding")
    kb_db.update_document(doc_id, {"status": kb_db.STATUS_READY, "enabled": True,
                                   "progress": 100, "error": None})
    log.info("文件 #%s 已上線（%s 塊）", doc_id, stats.get("done"))
    return kb_db.get_document(doc_id) or doc


def set_document_enabled(doc_id: int, enabled: bool) -> dict:
    """停用文件（不刪資料，只讓檢索看不到）。"""
    kb_db.update_document(doc_id, {"enabled": bool(enabled)})
    return kb_db.get_document(doc_id) or {}


def delete_document(doc_id: int) -> bool:
    """刪文件：先刪 DB（連帶 kb_chunk），再清掉落地檔案的資料夾。"""
    doc = kb_db.get_document(doc_id)
    if not doc:
        return False
    ok = kb_db.delete_document(doc_id)
    drop_parse_cache(doc_id)
    path = doc.get("source_path") or ""
    folder = os.path.dirname(path) if path else ""
    if folder and os.path.isdir(folder) and os.path.abspath(folder).startswith(
            os.path.abspath(rag_config.RAG_KB_DIR)):
        import shutil
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except Exception as e:
            log.debug("清理檔案目錄失敗 %s: %s", folder, e)
    return ok


# ======================================================================
# 重啟續跑
# ======================================================================

def resume_on_startup() -> dict:
    """服務啟動時：把上次中斷的工作收拾乾淨，讓文件回到可重跑狀態。"""
    out = {"stuck_jobs": 0, "reset_docs": 0}
    try:
        out["stuck_jobs"] = kb_db.reset_stuck_jobs()
        rows = kb_db.query("""
            UPDATE kb_document
               SET status='draft', progress=0,
                   error='上次處理被服務重啟中斷，請重新執行'
             WHERE status IN ('parsing','chunking','embedding')
            RETURNING id
        """)
        out["reset_docs"] = len(rows or [])
        if out["reset_docs"]:
            log.warning("啟動清理：%d 份文件回到 draft（上次未完成）", out["reset_docs"])
    except Exception as e:
        log.warning("啟動續跑清理失敗: %s", e)
    return out
