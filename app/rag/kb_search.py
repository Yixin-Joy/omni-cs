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

5. **多查詢檢索**（`search_multi()`，勾了「查詢改寫」才會走）：原問句 + 模型依上下文
   改寫出的 N 條查詢各打一次分，**同一個塊取最高分**後才做一次 top_k / max_per_doc。
   順序很要緊 —— 若每條 query 各自 `_pick(top_k)` 再合併，召回數會變成 N 倍、
   `max_per_doc` 也形同失效。每個塊會記下「被哪幾條查詢撈到」（`Hit.queries`），
   一眼看得出這塊是靠改寫救回來的，還是原問句本來就撈得到。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from app.llm import faq_matcher
from app.rag import kb_chunk
from app.rag import kb_db
from app.rag import kb_embed
from app.rag import kb_rank
from app.rag import rag_config

log = logging.getLogger("wechat-forwarder")

_INDEX_TTL = 5.0
_PARAM_TTL = 5.0

_index_cache: dict[str, dict] = {}
_asset_cache: dict[str, dict] = {}
_param_cache: dict[str, dict] = {}
_lock = threading.RLock()


def invalidate() -> None:
    """知識庫內容或參數變更後呼叫（上線 / 刪除 / 改塊 / 存參數 / 改圖片描述）。"""
    with _lock:
        _index_cache.clear()
        _asset_cache.clear()
        _param_cache.clear()


# ---------- 參數 ----------

def params_for(collection_id: int | None) -> dict:
    """執行時參數（DB 覆寫優先），5 秒 TTL。

    檢索參數與提示詞都是**全域**的，與 `collection_id` 無關；
    只有分塊參數才分知識庫。這裡仍帶 cid 是因為回傳的是整包參數（含分塊那組），
    方便呼叫端一次拿齊。
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


def prompt_of(name: str) -> str:
    """提示詞的執行時值（DB 全域覆寫優先），共用參數快取（5 秒 TTL）。

    ★ 這是**所有提示詞讀取的唯一入口** —— `deepseek_client` 的知識庫問答
    （`rag_answer_prompt`）與查詢改寫（`rewrite_prompt`）都走這裡，
    所以「在知識庫 → 參數配置 → C. 提示詞」改完約 5 秒就會反映到線上行為
    （`POST /console/kb/params` 會立刻 `invalidate()`，通常不用等）。

    `rag_config.prompt_value()` 負責型別轉換與必需佔位符檢查：
    使用者萬一把 `{context}` 刪掉，這裡會有第二道保險退回內建預設，
    不讓 `.format()` 在生成答案那一刻炸掉整層。
    """
    return rag_config.prompt_value(name, params_for(None).get(name))


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

    # 字面分數的預處理（jieba 分詞）也先算好，避免每次檢索重跑。
    # ⚠️ 先剝掉 markdown 圖片標記 —— URL 這種雜訊對字面比對只有壞處
    #    （向量那邊也一樣，見 kb_ingest.embed_document）。
    pre = []
    for r in usable:
        nu, ut = faq_matcher.preprocess(kb_chunk.strip_md_images(r["text"] or ""))
        pre.append((nu, ut))

    value = {"rows": usable, "mat": mat, "pre": pre,
             "dims": int(mat.shape[1]) if mat.size else 0}
    with _lock:
        _index_cache[key] = {"ts": now, "value": value}
    return value


def index_stats(collection_id: int | None = None) -> dict:
    idx = _index(collection_id)
    ai = _asset_index(collection_id)
    return {"chunks": len(idx["rows"]), "dim": idx["dims"],
            "docs": len({r["doc_id"] for r in idx["rows"]}),
            "assets": len(ai["rows"])}


# ---------- 圖片索引（圖片知識庫） ----------
#
# ★ 2026-10-12：圖片庫要能**獨立被檢索** —— 用它的「描述」（caption）當檢索文字。
#   以前 kb_search 完全沒查圖片，`kb_db.load_ready_assets()` 是死代碼，
#   所以圖片庫的圖（含單獨上傳的）永遠命中不了（實測：上傳了截圖 + 寫了描述，
#   問「图文结合输出效果如何」查不到，就是這個原因）。
#
#   命中的 `Hit.text` 直接就是 markdown（`![題注](/media/xxx.png)`）——
#   前端解析就渲染出圖，「圖文合併輸出」不需要另一條資料通道。

def _asset_markdown(a: dict) -> str:
    """圖片的 markdown（前端渲染用；LLM 看到的是它的文字形式）。"""
    url = rag_config.media_url_of_rel(a.get("rel_path") or "")
    if not url:
        url = f"/console/kb/asset/file/{a.get('id')}"
    cap = re.sub(r"[\r\n]+", " ", (a.get("caption") or "").strip())
    cap = cap.replace("]", "】").replace("[", "【")
    return f"![{cap}]({url})"


def _asset_url(a: dict) -> str:
    """圖片對外網址：公開目錄的圖走 `/media/...`，舊圖退回登入端點。"""
    return (rag_config.media_url_of_rel(a.get("rel_path") or "")
            or f"/console/kb/asset/file/{a.get('id')}")


def _assets_bound_to(chunk_ids: list[int]) -> set:
    """這些知識塊上綁了哪些圖片（去重用）。"""
    ids = sorted({int(c) for c in (chunk_ids or []) if c and int(c) > 0})
    if not ids:
        return set()
    rows = kb_db.query("SELECT DISTINCT asset_id FROM kb_asset_chunk "
                       "WHERE chunk_id = ANY(%s)", (ids,))
    return {int(r["asset_id"]) for r in rows}


def _asset_index(collection_id: int | None) -> dict:
    """載入可檢索的圖片（有描述 + 有向量 + 圖片庫啟用），5 秒 TTL。"""
    key = f"a:{collection_id or '*'}"
    now = time.time()
    with _lock:
        item = _asset_cache.get(key)
        if item and now - item["ts"] < _INDEX_TTL:
            return item["value"]

    rows = kb_db.load_ready_assets(collection_id)
    usable, vecs, pre = [], [], []
    for r in rows:
        emb = r.get("embedding")
        if not emb:
            continue
        cap = kb_chunk.asset_embed_text(r)
        if not cap:
            continue
        usable.append(r)
        vecs.append(emb)
        nu, ut = faq_matcher.preprocess(cap)
        pre.append((nu, ut))

    mat = np.asarray(vecs, dtype="float32") if vecs else np.zeros((0, 0), dtype="float32")
    if mat.size:
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / np.where(norms == 0, 1.0, norms)
    value = {"rows": usable, "mat": mat, "pre": pre,
             "dims": int(mat.shape[1]) if mat.size else 0}
    with _lock:
        _asset_cache[key] = {"ts": now, "value": value}
    return value


def _score_assets(idx: dict, qvec: list[float] | None, nu: str, ut: set,
                  query: str = "") -> list[Hit]:
    """對圖片庫打分（向量 + 字面，權重與知識塊一致）。"""
    rows, mat, pre = idx["rows"], idx["mat"], idx["pre"]
    if not rows:
        return []
    wv = float(rag_config.RAG_VECTOR_WEIGHT)
    wl = float(rag_config.RAG_LEXICAL_WEIGHT)
    total = wv + wl or 1.0
    # 向量維度要對得上才用（換 embedding 模型後舊圖尚未重算時）
    use_vec = bool(qvec) and mat.size and mat.shape[1] == len(qvec)
    cos = _cosine(mat, qvec) if use_vec else np.zeros(len(rows), dtype="float32")

    hits: list[Hit] = []
    for i, r in enumerate(rows):
        nf, ft = pre[i]
        lex, _cov = _lexical(nu, ut, nf, ft)
        v = float(cos[i]) if i < len(cos) else 0.0
        final = (wv * v + wl * lex) / total
        aid = int(r["id"])
        hits.append(Hit(
            chunk_id=-aid, doc_id=int(r.get("doc_id") or 0),
            collection_id=r.get("collection_id"),
            doc_title=r.get("doc_title") or "", doc_filename=r.get("doc_filename") or "",
            doc_type="image", ordinal=0,
            text=_asset_markdown(r),
            page_no=r.get("page_no"),
            vec=v, lex=lex, final=final,
            queries=[query] if query else [],
            kind="image", asset_id=aid,
        ))
    hits.sort(key=lambda h: -h.final)
    return hits


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
    # 這塊是被「哪幾條查詢」撈到的（多查詢檢索才有意義；單查詢時就一條）。
    # 排錯時最有用：可以一眼看出某塊是靠改寫句救回來的，還是原問句本來就撈得到。
    queries: list = field(default_factory=list)
    # ★ 綁在這塊上的圖片（kb_asset_chunk）。命中後要「圖文結合輸出」就靠它 ——
    #   只有啟用的圖會進來（被啟發式判為裝飾的圖預設不帶出）。
    assets: list = field(default_factory=list)
    # ★ 這是「知識塊」還是「圖片庫的一張圖」（2026-10-12）。
    #   圖片命中的 text 就是 markdown（`![題注](/media/xxx.png)`），前端直接渲染。
    kind: str = "chunk"              # chunk | image
    asset_id: int | None = None

    @property
    def group_key(self):
        """`max_per_doc` 的分組依據：圖片以自己的 id 為一組（不受單檔上限影響）。"""
        if self.kind == "image":
            return ("img", self.asset_id)
        return ("doc", self.doc_id)

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
        d = {
            "kind": self.kind,
            "chunk_id": self.chunk_id, "doc_id": self.doc_id,
            "doc": self.doc_title or self.doc_filename,
            "heading": self.heading_path or "",
            "page": self.page_no, "sheet": self.sheet,
            "row_from": self.row_from, "row_to": self.row_to,
            "vec": round(self.vec, 4), "lex": round(self.lex, 4),
            "final": round(self.final, 4),
            "rerank": (round(self.rerank, 4) if self.rerank is not None else None),
            "picked": self.picked,
            "queries": list(self.queries or []),
            # 只留精簡資訊（完整圖檔走 /console/kb/asset/file/{id}）
            "assets": [{"id": a.get("id"), "caption": a.get("caption") or "",
                        "url": a.get("url") or f"/console/kb/asset/file/{a.get('id')}",
                        "width": a.get("width"), "height": a.get("height")}
                       for a in (self.assets or [])],
            "text": (self.text or "")[:300],
        }
        if self.asset_id is not None:
            d["asset_id"] = self.asset_id
        return d


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
    # 這次實際拿去檢索的查詢句（多查詢檢索時 > 1 條，第 0 條固定是客戶原問句）
    queries: list = field(default_factory=list)
    # ★ 這次命中帶出來的圖片（去重、依命中塊的順序）——「圖文結合輸出」的資料源
    assets: list = field(default_factory=list)

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
            "query": self.query, "queries": list(self.queries or []),
            "top1": round(self.top1, 4), "passed": self.passed,
            "reason": self.reason, "threshold": self.threshold,
            "elapsed_ms": self.elapsed_ms, "embed_ok": self.embed_ok,
            "params": {k: self.params.get(k) for k in
                       ("top_k", "min_score", "max_per_doc", "max_context_chars",
                        "rerank_enabled", "rerank_top_n")},
            "hits": [self._hit_dict(h, with_text) for h in self.hits],
            "candidates": [self._hit_dict(h, with_text) for h in self.candidates[:candidates]],
            "per_collection": self.per_collection,
            "assets": self.assets,
        }
        return out

    @staticmethod
    def _hit_dict(h: Hit, with_text: bool) -> dict:
        d = h.as_dict()
        if not with_text:
            d.pop("text", None)
        return d


def _score_group(rows: list, pre: list, mat: np.ndarray, collection_id: int | None,
                 qvec: list[float] | None, nu: str, ut: set, query: str = "") -> list[Hit]:
    """對「同一個知識庫」的塊打分並回傳排序後的 Hit 清單。

    `query` 只是記進 Hit.queries（溯源用），不影響分數。
    """
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
            queries=[query] if query else [],
        ))
    hits.sort(key=lambda h: -h.final)
    return hits


def _pick(hits: list[Hit], top_k: int, max_per_doc: int) -> list[Hit]:
    """依 final 排序取前 top_k，同一文件最多 max_per_doc 塊（圖片各自獨立成組）。"""
    picked, per_doc = [], {}
    for h in hits:
        key = h.group_key
        if max_per_doc > 0 and per_doc.get(key, 0) >= max_per_doc:
            continue
        picked.append(h)
        per_doc[key] = per_doc.get(key, 0) + 1
        if len(picked) >= top_k:
            break
    return picked


def _resolve_params(params: dict | None, top_k: int | None, min_score: float | None,
                    max_per_doc: int | None, max_context_chars: int | None,
                    rerank_enabled: bool | None) -> dict:
    """★ 檢索參數一律取**全域**那一組（所有知識庫共用）——
    以前是「每庫用自己的 top_k / min_score 各自召回」，結果最終參數其實
    由 `load_ready_chunks` 的 ORDER BY c.doc_id 決定（誰的 doc_id 小就聽誰）。
    顯式給的值覆蓋參數（命中測試／調參端點用）。
    """
    p = dict(params or params_for(None))
    for key, val in (("top_k", top_k), ("min_score", min_score), ("max_per_doc", max_per_doc),
                     ("max_context_chars", max_context_chars), ("rerank_enabled", rerank_enabled)):
        if val is not None:
            p[key] = val
    return p


def _score_all(idx: dict, query: str, collection_id: int | None
               ) -> tuple[list[Hit], list[dict], bool]:
    """對整個索引依 `query` 打分，回傳 (排序後的 Hit 清單, 每庫統計, embedding 是否可用)。"""
    rows_all, mat_all, pre_all = idx["rows"], idx["mat"], idx["pre"]

    # 查詢向量（失敗 → 純字面檢索）
    embed_ok = True
    qvec = None
    try:
        qvec = kb_embed.get_provider().encode_one(query)
    except Exception as e:
        embed_ok = False
        log.warning("查詢向量化失敗，改用純字面檢索：%s", e)

    nu, ut = faq_matcher.preprocess(query)

    # 依知識庫分組（只是為了算每庫的 top1 統計，參數不再分庫）
    groups: dict[int | None, list[int]] = {}
    for i, r in enumerate(rows_all):
        groups.setdefault(r.get("collection_id"), []).append(i)

    merged: list[Hit] = []
    per_collection = []
    for cid, idxs in groups.items():
        sub_rows = [rows_all[i] for i in idxs]
        sub_pre = [pre_all[i] for i in idxs]
        sub_mat = mat_all[idxs] if mat_all.size else mat_all

        best = _score_group(sub_rows, sub_pre, sub_mat, cid, qvec, nu, ut, query)
        if not best:
            continue
        # per_collection 純統計：「每個庫各自的最佳分數長怎樣」，不參與參數決策
        per_collection.append({
            "collection_id": cid, "chunks": len(sub_rows),
            "top1": round(best[0].final, 4),
        })
        merged += best

    merged.sort(key=lambda h: -h.final)

    # ★ 圖片庫（kind='image' 的知識庫）也進同一個候選池 ——
    #   它的檢索文字是「描述」，命中的 text 就是 `![描述](/media/xxx.png)`。
    aidx = _asset_index(collection_id)
    if aidx["rows"]:
        amerged = _score_assets(aidx, qvec, nu, ut, query)
        if amerged:
            per_collection.append({"collection_id": -1, "kind": "image",
                                   "chunks": len(aidx["rows"]),
                                   "top1": round(amerged[0].final, 4)})
            merged += amerged
            merged.sort(key=lambda h: -h.final)
    return merged, per_collection, embed_ok


def _has_index(collection_id: int | None) -> bool:
    """有沒有任何可檢索的東西（知識塊或圖片）。"""
    return bool(_index(collection_id)["rows"]) or bool(_asset_index(collection_id)["rows"])


def _finish(res: SearchResult, merged: list[Hit], query: str) -> SearchResult:
    """收尾：門檻縮放 → rerank → top_k / max_per_doc → 上下文截斷 → passed/reason。

    ⚠️ 這一段**只能對合併後的最終候選池做一次**。多查詢檢索若每條 query 各自
    `_pick(top_k)` 再合併，等於偷偷把 top_k 放大 N 倍，`max_per_doc` 也會失效。
    """
    p = res.params
    thr = float(p.get("min_score") or 0.0)
    for row in res.per_collection:
        row.setdefault("threshold", thr)
        row.setdefault("passed", float(row.get("top1") or 0) >= thr)

    res.candidates = merged
    res.top1 = merged[0].final if merged else 0.0
    res.threshold = thr

    if not res.embed_ok and p.get("min_score"):
        # 純字面檢索時分數分佈不同（缺了 0.65 權重的向量項），門檻等比縮放
        res.threshold = round(thr * (rag_config.RAG_LEXICAL_WEIGHT or 0.35), 4)

    # rerank（第一版 NoopReranker，直接回前 n 個；要裝時這裡不用改）
    n = int(p.get("rerank_top_n") or 20)
    head, tail = (merged[:n], merged[n:]) if len(merged) > n else (merged, [])
    rr = kb_rank.get_reranker(bool(p.get("rerank_enabled")))
    if rr.name != "noop" and head:
        docs = [dict(h.as_dict(), text=h.text) for h in head]
        by_id = {h.chunk_id: h for h in head}
        new_head = []
        for d in rr.rerank(query, docs, n):
            h = by_id.get(d.get("chunk_id"))
            if h is None:
                continue
            if d.get("rerank_score") is not None:
                h = Hit(**{**h.__dict__, "rerank": d["rerank_score"]})
            new_head.append(h)
        if new_head:
            head = new_head
    top_pool = head + tail

    picked = _pick(top_pool, int(p.get("top_k") or 6), int(p.get("max_per_doc") or 0))
    max_chars = int(p.get("max_context_chars") or 0)
    if max_chars:
        kept, used = [], 0
        for h in picked:
            if used + len(h.text) > max_chars and kept:
                break
            kept.append(h)
            used += len(h.text)
        picked = kept
    # ★ 「入選（picked）」的語意是：**這一塊真的會被送進模型**。
    #   為什麼不能無條件標 True（2026-10-12，小jo 回報「很多個入選但實際都沒過門檻」）：
    #   `_pick()` 只做「按分數排序取前 top_k」，**完全不看門檻**；
    #   而 `_rag_try()` 在 `top1 < 門檻` 時直接 return、**一塊都不送**。
    #   於是工作台顯示「入選 6 塊」，實際上模型一塊都沒看到 —— 兩邊的說法互相矛盾。
    #   現在沒過門檻就一塊都不標，畫面會顯示「實際送進模型 0 塊」。
    passed = bool(picked) and res.top1 >= res.threshold
    for h in picked:
        h.picked = passed

    # ★ 去重：同一張圖若**同時**被「內嵌它的知識塊」與「圖片庫自己的條目」命中，
    #   只留知識塊那一份 —— 圖已經在它的文字裡了（前端渲染一次就好）。
    #   判重靠 kb_asset_chunk 綁定：圖片庫那筆的 asset_id 有沒有綁在已入選的塊上。
    try:
        bound = _assets_bound_to([h.chunk_id for h in picked if h.kind != "image"])
        if bound:
            picked = [h for h in picked
                      if h.kind != "image" or h.asset_id not in bound]
    except Exception as e:
        log.debug("圖片去重失敗（不影響檢索）：%s", e)

    res.hits = picked

    # ★ 命中後帶出圖片（「圖文結合輸出」）。
    #   只在**入選的那幾塊**上查，最多 6 塊 → 一次索引查詢，成本可忽略；
    #   失敗也只是沒圖，不該讓整次檢索掛掉。
    try:
        amap = kb_db.assets_for_chunks([h.chunk_id for h in picked if h.kind != "image"])
        seen: set = set()
        for h in picked:
            if h.kind != "image":
                got = amap.get(int(h.chunk_id)) or []
                h.assets = got
                for a in got:
                    if a.get("id") in seen:
                        continue
                    seen.add(a.get("id"))
                    res.assets.append({**a, "url": _asset_url(a),
                                       "chunk_id": h.chunk_id,
                                       "chunk_heading": h.heading_path or "",
                                       "page": h.page_no})
            # 圖片庫自己命中的那張圖，也要出現在 assets 清單裡（前端統一渲染）
            if h.kind == "image" and h.asset_id not in seen:
                row = kb_db.get_asset(int(h.asset_id)) or {}
                if row:
                    seen.add(h.asset_id)
                    res.assets.append({**row, "url": _asset_url(row),
                                       "chunk_id": None,
                                       "chunk_heading": h.doc_title or "",
                                       "page": row.get("page_no")})
    except Exception as e:
        log.debug("帶出命中圖片失敗（不影響檢索）：%s", e)

    res.passed = passed          # 上面算過（門檻只看 top1；picked 只是前 top_k）

    if not merged:
        res.reason = "no_candidates"
    elif not res.passed:
        res.reason = "below_threshold"
    else:
        res.reason = "ok"
    return res


def search(query: str, *, collection_id: int | None = None,
           params: dict | None = None, top_k: int | None = None,
           min_score: float | None = None, max_per_doc: int | None = None,
           max_context_chars: int | None = None,
           rerank_enabled: bool | None = None) -> SearchResult:
    """混合檢索（單一查詢）。

    `collection_id=None` → 搜尋「所有啟用的知識庫」（候選池是全部啟用庫的塊）。
    檢索參數（top_k / min_score / …）一律用**全域**那一組，與 collection_id 無關。
    顯式給的 top_k / min_score / … 會**覆蓋**參數（命中測試頁用）。

    要拿「原問句 + 改寫句」一起檢索，用 `search_multi()`。
    """
    t0 = time.time()
    q = (query or "").strip()
    res = SearchResult(query=q, queries=[q] if q else [])
    if not q:
        res.reason = "empty_query"
        return res

    res.params = _resolve_params(params, top_k, min_score, max_per_doc,
                                 max_context_chars, rerank_enabled)
    idx = _index(collection_id)
    if not idx["rows"] and not _asset_index(collection_id)["rows"]:
        res.reason = "empty_index"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    merged, per_collection, embed_ok = _score_all(idx, q, collection_id)
    res.embed_ok = embed_ok
    res.per_collection = per_collection
    _finish(res, merged, q)
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


def search_multi(queries: list[str], *, collection_id: int | None = None,
                 params: dict | None = None, top_k: int | None = None,
                 min_score: float | None = None, max_per_doc: int | None = None,
                 max_context_chars: int | None = None,
                 rerank_enabled: bool | None = None) -> SearchResult:
    """多查詢檢索：把「原問句 + 改寫句」一起檢索，合併後才算一次 top_k。

    為什麼要這樣做（而不是各查各的再合併結果）：見 `_finish()` 的註解 ——
    每條 query 各自 `_pick(top_k)` 會把召回數偷偷放大 N 倍，`max_per_doc` 也會失去作用。

    合併規則：**同一個 chunk 只留最高分的那一份**，並把它被哪幾條查詢撈到記在
    `Hit.queries` 裡（`SearchResult.queries` 是這次實際用的全部查詢句）。
    第 0 條固定是客戶原問句 —— 原句一定是自己最準的查詢，改寫只是補漏。

    queries 只有 1 條（或空）時直接退化成 `search()`。
    """
    t0 = time.time()
    qs: list[str] = []
    for q in (queries or []):
        s = (q or "").strip()
        if s and s not in qs:
            qs.append(s)
    if not qs:
        return search("")
    if len(qs) == 1:
        return search(qs[0], collection_id=collection_id, params=params, top_k=top_k,
                      min_score=min_score, max_per_doc=max_per_doc,
                      max_context_chars=max_context_chars, rerank_enabled=rerank_enabled)

    res = SearchResult(query=qs[0], queries=qs)
    res.params = _resolve_params(params, top_k, min_score, max_per_doc,
                                 max_context_chars, rerank_enabled)
    idx = _index(collection_id)
    if not idx["rows"] and not _asset_index(collection_id)["rows"]:
        res.reason = "empty_index"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    by_id: dict[int, Hit] = {}
    hit_by: dict[int, list[str]] = {}
    embed_ok = True
    for q in qs:
        sub, _stats, ok = _score_all(idx, q, collection_id)
        embed_ok = embed_ok and ok
        for h in sub:
            hit_by.setdefault(h.chunk_id, []).append(q)
            cur = by_id.get(h.chunk_id)
            if cur is None or h.final > cur.final:
                by_id[h.chunk_id] = h        # 同一塊取最高分的那份（含它的 vec/lex 明細）
    for chunk_id, used in hit_by.items():
        by_id[chunk_id].queries = used

    merged = sorted(by_id.values(), key=lambda h: -h.final)
    res.embed_ok = embed_ok
    res.per_collection = _collection_stats(merged)
    _finish(res, merged, qs[0])              # rerank 用原問句（最貼近客戶意圖的那條）
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


def _collection_stats(merged: list[Hit]) -> list[dict]:
    """合併後候選池的「每庫最佳分」統計（給 UI／自檢看分佈用）。"""
    groups: dict[int | None, list[Hit]] = {}
    for h in merged:
        groups.setdefault(h.collection_id, []).append(h)
    out = []
    for cid, hits in groups.items():
        out.append({"collection_id": cid, "chunks": len(hits),
                    "matched": len(hits), "top1": round(hits[0].final, 4)})
    return out


def build_context(query: str, *, collection_id: int | None = None,
                  params: dict | None = None, **overrides) -> SearchResult:
    """`search()` 的別名（語意上「我要一段可以餵給模型的上下文」）。"""
    return search(query, collection_id=collection_id, params=params, **overrides)


def query_rewrite_enabled() -> bool:
    """檢索前是否做多查詢改寫（全域檢索參數，5 秒 TTL，改完不用重啟）。"""
    return bool(params_for(None).get("query_rewrite"))


def warmup_index() -> dict:
    """預熱：先把索引載進記憶體（含全量 jieba 預處理）。

    為什麼要：`_index()` 的重載成本大頭是 jieba 而不是 DB，且整段在請求路徑上。
    啟動時先付掉，第一個客人就不必等（實測第一次檢索 2.3 秒，預熱後 <0.1 秒）。
    """
    t0 = time.time()
    stats = index_stats(None)
    stats["seconds"] = round(time.time() - t0, 3)
    log.info("檢索索引預熱完成：%s 塊 / dim %s（%.2fs）",
             stats.get("chunks"), stats.get("dim"), stats["seconds"])
    return stats
