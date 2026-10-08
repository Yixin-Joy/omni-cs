"""知識庫混合檢索：向量 cosine + jieba 字面分數，含去重、門檻與 rerank 掛點。

設計依據：RAG_DESIGN.md §7。

    q → 改寫（由呼叫端做）→ 混合召回 → 去重/截斷 → [rerank] → 門檻判斷 → 生成

幾個刻意的決定：

1. **向量不落索引庫，全部載進記憶體算 cosine。** 本機 PostgreSQL 沒有 pgvector
   （`pg_available_extensions` 查無），萬級塊用 `REAL[]` + numpy 完全夠（< 20ms）。
   索引有 5 秒 TTL 快取，另提供 `invalidate()` 讓「剛上線的文件」立刻可搜。

2. **檢索參數是全域的（2026-10-08 定案）。** `top_k` / `min_score` / `max_per_doc` /
   `max_context_chars` 所有知識庫共用一組，存在 `kb_param` 的 `collection_id IS NULL` 那批。
   舊版「每庫用自己的參數各自召回、再合併排序」有兩個問題：最終參數其實由
   **doc_id 最小的那個庫**決定（其他庫的設定等於白設），而且同一問句換批資料就換結果。
   分塊參數才按知識庫分開設（`kb_db.load_params(cid)`）。

3. **字面分數是「查詢詞覆蓋率」+ `faq_matcher.score_pair` 的混合。**
   `score_pair` 是為「短問句 vs 短問句」設計的；拿它直接對 500 字的知識塊打分，
   bigram/字元相似度會被長度稀釋到接近 0，等於白算。所以補上
   「查詢的實義詞有幾個在塊裡出現」（coverage）——這才是客服問句真正需要的信號。

4. **embedding 掛掉時自動降級成純字面檢索**，RAG 層不會整層失效（設計稿 §7.7）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

import faq_matcher
import kb_db
import kb_embed
import kb_rank
import rag_config

log = logging.getLogger("wechat-forwarder")

_INDEX_TTL = 5.0
_PARAM_TTL = 5.0

_index_cache: dict[str, dict] = {}
_param_cache: dict[str, dict] = {}
_lock = threading.RLock()


def invalidate() -> None:
    """知識庫內容或參數變更後呼叫（上線 / 刪除 / 改塊 / 存參數）。"""
    with _lock:
        _index_cache.clear()
        _param_cache.clear()


# ---------- 參數 ----------

def params_for(collection_id: int | None) -> dict:
    """執行時參數（DB 覆寫優先），5 秒 TTL。

    檢索用的那幾個（top_k / min_score / …）都是**全域**的，與 `collection_id` 無關；
    這裡仍帶 cid 是因為回傳的是整包參數（含分塊那組），方便呼叫端一次拿齊。
    """
    key = str(collection_id or 0)
    now = time.time()
    with _lock:
        item = _param_cache.get(key)
        if item and now - item["ts"] < _PARAM_TTL:
            return item["value"]
    value = kb_db.load_params(collection_id)
    with _lock:
        _param_cache[key] = {"ts": now, "value": value}
    return value


# ---------- 索引 ----------

def _index(collection_id: int | None) -> dict:
    """載入可檢索的塊 + 向量矩陣（5 秒 TTL）。"""
    key = str(collection_id or "*")
    now = time.time()
    with _lock:
        item = _index_cache.get(key)
        if item and now - item["ts"] < _INDEX_TTL:
            return item["value"]

    rows = kb_db.load_ready_chunks(collection_id)
    usable, vecs = [], []
    for r in rows:
        emb = r.get("embedding")
        if not emb:
            continue
        usable.append(r)
        vecs.append(emb)

    mat = np.asarray(vecs, dtype="float32") if vecs else np.zeros((0, 0), dtype="float32")
    if mat.size:
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / np.where(norms == 0, 1.0, norms)

    # 字面分數的預處理（jieba 分詞）也先算好，避免每次檢索重跑
    pre = []
    for r in usable:
        nu, ut = faq_matcher.preprocess(r["text"] or "")
        pre.append((nu, ut))

    value = {"rows": usable, "mat": mat, "pre": pre,
             "dims": int(mat.shape[1]) if mat.size else 0}
    with _lock:
        _index_cache[key] = {"ts": now, "value": value}
    return value


def index_stats(collection_id: int | None = None) -> dict:
    idx = _index(collection_id)
    return {"chunks": len(idx["rows"]), "dim": idx["dims"],
            "docs": len({r["doc_id"] for r in idx["rows"]})}


# ---------- 打分 ----------

def _cosine(mat: np.ndarray, qvec: list[float]) -> np.ndarray:
    if mat.size == 0 or not qvec:
        return np.zeros(0, dtype="float32")
    q = np.asarray(qvec, dtype="float32")
    n = float(np.linalg.norm(q))
    if n == 0:
        return np.zeros(mat.shape[0], dtype="float32")
    return mat @ (q / n)


def _lexical(nu: str, ut: set, nf: str, ft: set) -> tuple[float, float]:
    """回傳 (字面分, 查詢詞覆蓋率)。"""
    if not nu or not nf:
        return 0.0, 0.0
    pair = faq_matcher.score_pair(nu, ut, nf, ft)
    cov = (len(ut & ft) / len(ut)) if ut else 0.0
    return 0.5 * pair + 0.5 * cov, cov


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    collection_id: int | None
    doc_title: str
    doc_filename: str
    doc_type: str
    ordinal: int
    text: str
    heading_path: str = ""
    page_no: int | None = None
    sheet: str | None = None
    row_from: int | None = None
    row_to: int | None = None
    vec: float = 0.0
    lex: float = 0.0
    final: float = 0.0
    rerank: float | None = None
    picked: bool = False

    @property
    def source(self) -> str:
        """給模型／客服看的來源描述。"""
        parts = [self.doc_title or self.doc_filename or f"doc#{self.doc_id}"]
        if self.heading_path:
            parts.append(self.heading_path)
        elif self.sheet:
            parts.append(f"工作表 {self.sheet}")
        elif self.page_no:
            parts.append(f"第 {self.page_no} 頁")
        if self.row_from is not None and self.row_to is not None and self.row_to != self.row_from:
            parts.append(f"列 {self.row_from}-{self.row_to}")
        return " › ".join(parts)

    def as_dict(self) -> dict:
        """寫進 `autoreply_log.matched_rows` 的形狀（設計稿 §9）。"""
        return {
            "chunk_id": self.chunk_id, "doc_id": self.doc_id,
            "doc": self.doc_title or self.doc_filename,
            "heading": self.heading_path or "",
            "page": self.page_no, "sheet": self.sheet,
            "row_from": self.row_from, "row_to": self.row_to,
            "vec": round(self.vec, 4), "lex": round(self.lex, 4),
            "final": round(self.final, 4),
            "rerank": (round(self.rerank, 4) if self.rerank is not None else None),
            "picked": self.picked,
            "text": (self.text or "")[:300],
        }


@dataclass
class SearchResult:
    query: str = ""
    hits: list = field(default_factory=list)        # 入選的（已排序、已套 max_per_doc / top_k）
    candidates: list = field(default_factory=list)  # 全部候選（含未入選，供溯源）
    top1: float = 0.0
    passed: bool = False
    reason: str = ""
    threshold: float = 0.0
    params: dict = field(default_factory=dict)
    per_collection: list = field(default_factory=list)
    embed_ok: bool = True
    elapsed_ms: int = 0

    def context(self, max_chars: int = 0) -> str:
        """把入選的塊拼成給模型看的文字（每段前有編號與來源）。"""
        parts, used = [], 0
        limit = max_chars or 0
        for i, h in enumerate(self.hits, start=1):
            seg = f"[{i}]（{h.source}）{h.text}"
            if limit and used + len(seg) > limit and parts:
                break
            parts.append(seg)
            used += len(seg)
        return "\n".join(parts)

    def as_dict(self, *, with_text: bool = True, candidates: int = 30) -> dict:
        out = {
            "query": self.query, "top1": round(self.top1, 4), "passed": self.passed,
            "reason": self.reason, "threshold": self.threshold,
            "elapsed_ms": self.elapsed_ms, "embed_ok": self.embed_ok,
            "params": {k: self.params.get(k) for k in
                       ("top_k", "min_score", "max_per_doc", "max_context_chars",
                        "rerank_enabled", "rerank_top_n")},
            "hits": [self._hit_dict(h, with_text) for h in self.hits],
            "candidates": [self._hit_dict(h, with_text) for h in self.candidates[:candidates]],
            "per_collection": self.per_collection,
        }
        return out

    @staticmethod
    def _hit_dict(h: Hit, with_text: bool) -> dict:
        d = h.as_dict()
        if not with_text:
            d.pop("text", None)
        return d


def _score_group(rows: list, pre: list, mat: np.ndarray, collection_id: int | None,
                 qvec: list[float] | None, nu: str, ut: set, p: dict) -> list[Hit]:
    """對「同一個知識庫」的塊打分並回傳排序後的 Hit 清單。"""
    # 混合權重是「跨知識庫的召回哲學」，不放進 UI 可調（.env 可改）
    wv = float(rag_config.RAG_VECTOR_WEIGHT)
    wl = float(rag_config.RAG_LEXICAL_WEIGHT)
    total = wv + wl or 1.0
    cos = _cosine(mat, qvec) if qvec else np.zeros(len(rows), dtype="float32")

    hits: list[Hit] = []
    for i, r in enumerate(rows):
        nf, ft = pre[i]
        lex, _cov = _lexical(nu, ut, nf, ft)
        v = float(cos[i]) if i < len(cos) else 0.0
        final = (wv * v + wl * lex) / total
        hits.append(Hit(
            chunk_id=int(r["id"]), doc_id=int(r["doc_id"]),
            collection_id=collection_id if collection_id is not None else r.get("collection_id"),
            doc_title=r.get("doc_title") or "", doc_filename=r.get("doc_filename") or "",
            doc_type=r.get("doc_type") or "", ordinal=int(r.get("ordinal") or 0),
            text=r.get("text") or "", heading_path=r.get("heading_path") or "",
            page_no=r.get("page_no"), sheet=r.get("sheet"),
            row_from=r.get("row_from"), row_to=r.get("row_to"),
            vec=v, lex=lex, final=final,
        ))
    hits.sort(key=lambda h: -h.final)
    return hits


def _pick(hits: list[Hit], top_k: int, max_per_doc: int) -> list[Hit]:
    """依 final 排序取前 top_k，同一文件最多 max_per_doc 塊。"""
    picked, per_doc = [], {}
    for h in hits:
        if max_per_doc > 0 and per_doc.get(h.doc_id, 0) >= max_per_doc:
            continue
        picked.append(h)
        per_doc[h.doc_id] = per_doc.get(h.doc_id, 0) + 1
        if len(picked) >= top_k:
            break
    return picked


def search(query: str, *, collection_id: int | None = None,
           params: dict | None = None, top_k: int | None = None,
           min_score: float | None = None, max_per_doc: int | None = None,
           max_context_chars: int | None = None,
           rerank_enabled: bool | None = None) -> SearchResult:
    """混合檢索。

    `collection_id=None` → 搜尋「所有啟用的知識庫」（候選池是全部啟用庫的塊）。
    檢索參數（top_k / min_score / …）一律用**全域**那一組，與 collection_id 無關。
    顯式給的 top_k / min_score / … 會**覆蓋**參數（命中測試頁用）。
    """
    t0 = time.time()
    q = (query or "").strip()
    res = SearchResult(query=q)
    if not q:
        res.reason = "empty_query"
        return res

    idx = _index(collection_id)
    rows_all, mat_all, pre_all = idx["rows"], idx["mat"], idx["pre"]
    if not rows_all:
        res.reason = "empty_index"
        res.params = params_for(None)
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    # 查詢向量（失敗 → 純字面檢索）
    qvec = None
    try:
        qvec = kb_embed.get_provider().encode_one(q)
    except Exception as e:
        res.embed_ok = False
        log.warning("查詢向量化失敗，改用純字面檢索：%s", e)

    nu, ut = faq_matcher.preprocess(q)

    # 依知識庫分組
    groups: dict[int | None, list[int]] = {}
    for i, r in enumerate(rows_all):
        groups.setdefault(r.get("collection_id"), []).append(i)

    # ★ 檢索參數一律取**全域**那一組（所有知識庫共用）——
    #   以前是「每庫用自己的 top_k / min_score 各自召回」，結果最終參數其實
    #   由 `load_ready_chunks` 的 ORDER BY c.doc_id 決定（誰的 doc_id 小就聽誰）。
    p = dict(params or params_for(None))
    if top_k is not None:
        p["top_k"] = top_k
    if min_score is not None:
        p["min_score"] = min_score
    if max_per_doc is not None:
        p["max_per_doc"] = max_per_doc
    if max_context_chars is not None:
        p["max_context_chars"] = max_context_chars
    if rerank_enabled is not None:
        p["rerank_enabled"] = rerank_enabled

    thr = float(p.get("min_score") or 0.0)
    merged: list[Hit] = []
    per_collection = []

    for cid, idxs in groups.items():
        sub_rows = [rows_all[i] for i in idxs]
        sub_pre = [pre_all[i] for i in idxs]
        sub_mat = mat_all[idxs] if mat_all.size else mat_all

        best = _score_group(sub_rows, sub_pre, sub_mat, cid, qvec, nu, ut, p)
        if not best:
            continue
        # per_collection 純統計：「每個庫各自的最佳分數長怎樣」，不再參與參數決策
        per_collection.append({
            "collection_id": cid, "chunks": len(sub_rows),
            "top1": round(best[0].final, 4), "threshold": thr,
            "passed": best[0].final >= thr,
        })
        merged += best

    merged.sort(key=lambda h: -h.final)
    res.params = p
    res.candidates = merged
    res.top1 = merged[0].final if merged else 0.0
    res.threshold = float(res.params.get("min_score") or 0.0)
    res.per_collection = per_collection

    if not res.embed_ok and res.params.get("min_score"):
        # 純字面檢索時分數分佈不同（缺了 0.65 權重的向量項），門檻等比縮放
        res.threshold = round(res.threshold * (rag_config.RAG_LEXICAL_WEIGHT or 0.35), 4)

    # rerank（第一版 NoopReranker，直接回前 n 個；要裝時這裡不用改）
    n = int(res.params.get("rerank_top_n") or 20)
    head, tail = (merged[:n], merged[n:]) if len(merged) > n else (merged, [])
    rr = kb_rank.get_reranker(bool(res.params.get("rerank_enabled")))
    if rr.name != "noop" and head:
        docs = [dict(h.as_dict(), text=h.text) for h in head]
        by_id = {h.chunk_id: h for h in head}
        new_head = []
        for d in rr.rerank(q, docs, n):
            h = by_id.get(d.get("chunk_id"))
            if h is None:
                continue
            if d.get("rerank_score") is not None:
                h = Hit(**{**h.__dict__, "rerank": d["rerank_score"]})
            new_head.append(h)
        if new_head:
            head = new_head
    top_pool = head + tail

    picked = _pick(top_pool, int(res.params.get("top_k") or 6),
                   int(res.params.get("max_per_doc") or 0))
    max_chars = int(res.params.get("max_context_chars") or 0)
    if max_chars:
        kept, used = [], 0
        for h in picked:
            if used + len(h.text) > max_chars and kept:
                break
            kept.append(h)
            used += len(h.text)
        picked = kept
    for h in picked:
        h.picked = True
    res.hits = picked
    res.passed = bool(picked) and res.top1 >= res.threshold

    if not merged:
        res.reason = "no_candidates"
    elif not res.passed:
        res.reason = "below_threshold"
    else:
        res.reason = "ok"
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


def build_context(query: str, *, collection_id: int | None = None,
                  params: dict | None = None, **overrides) -> SearchResult:
    """`search()` 的別名（語意上「我要一段可以餵給模型的上下文」）。"""
    return search(query, collection_id=collection_id, params=params, **overrides)
