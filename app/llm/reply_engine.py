"""AI 自動回復的四層決策引擎。

流程（與需求一致）：

    客戶來訊
      │
      ├─ 第一層：FAQ 問答對 ── 規則打分命中 ──► 直接回該答案（不呼叫 AI）
      │        （整層停用，或個別問答對停用，或未達門檻）
      │        未命中且開啟改寫 → 交給 DeepSeek 改寫問句後再匹配一次
      │
      ├─ 第二層：業務表格 ── 把表格內容交給 DeepSeek 生成答案
      │        （整層停用，或模型回 NO_ANSWER → 視為未命中）
      │
      ├─ 第三層：RAG 知識庫 ── 混合檢索命中文件，交給 DeepSeek 依文件生成答案
      │        （整層停用，或 top1 未過門檻 → 不呼叫模型，直接降級）
      │        勾了「查詢改寫」→ **檢索前**先改寫出 N 條查詢（修同音錯別字＋補上下文），
      │        與原問句一起檢索再合併取高分（多查詢檢索，見 kb_search.search_multi）
      │
      └─ 第四層：不自動回覆 → 降級人工，該則訊息標為「自動回復未命中，待處理」

對外只暴露 `engine.answer()`，回傳 `Answer`；呼叫端（autoreply.py）負責送訊息、
寫自動回復標記與日誌。引擎本身不發訊息（只讀 FAQ / 表格 / 知識庫與開關）。

層順序**固定**（FAQ → 表格 → RAG → 人工），不做可切換開關：少一個會被設錯的開關，
行為也更好預期。
"""
import asyncio
import hashlib
import json
import logging
import time

from app.core import config
from app.llm import faq_matcher
from app.rag import rag_config
from app.llm.deepseek_client import DeepSeekClient, DeepSeekError, looks_answered

log = logging.getLogger("wechat-forwarder")

TIER_FAQ = "faq"
TIER_TABLE = "table"
TIER_RAG = "rag"
TIER_MISS = "miss"

# 可用 `answer(..., layers=[...])` 指定的層（命中測試頁用；None = 依 DB 開關）
ALL_LAYERS = (TIER_FAQ, TIER_TABLE, TIER_RAG)

# 快取 TTL（秒）：FAQ / 業務表格 / 開關都是低頻變動，避免每則訊息重查 DB
_CACHE_TTL = 5.0
_cache: dict[str, dict] = {}

# ---------- 本次請求的模型用量統計 ----------
# 為什麼要記：一次知識庫回覆到底呼叫幾次模型、各花多久，光看程式碼看不出來，
# 而這正是「為什麼要一分鐘」的唯一線索（實測 8 次 / 48.9 秒）。
# 用 contextvar 而不是全域變數 —— 自動回復是併發的，兩則訊息會同時跑，
# 全域變數會互相蓋掉。
import contextvars  # noqa: E402

_LLM_STATS: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "reply_engine_llm_stats", default=None)


def _stats_start() -> dict:
    st = {"calls": 0, "errors": 0, "seconds": 0.0}
    _LLM_STATS.set(st)
    return st


def _stats() -> dict:
    return _LLM_STATS.get() or {"calls": 0, "errors": 0, "seconds": 0.0}


def _stats_note(label: str, seconds: float, ok: bool) -> None:
    """記一次模型呼叫（由 `_calls_with_stats()` 包住實際呼叫）。"""
    st = _LLM_STATS.get()
    if st is None:
        return
    st["calls"] += 1
    st["seconds"] += seconds
    if not ok:
        st["errors"] += 1
    log.debug("模型呼叫 %s：%.2fs %s", label, seconds, "ok" if ok else "失敗")

def invalidate_cache() -> None:
    """FAQ / 表格 / 開關被修改後呼叫，讓下一則訊息立刻讀到新設定。"""
    _cache.clear()


def _cached(key: str, loader):
    now = time.time()
    item = _cache.get(key)
    if item and now - item["ts"] < _CACHE_TTL:
        return item["value"]
    value = loader()
    _cache[key] = {"ts": now, "value": value}
    return value


# ---------- 開關 ----------

def _settings():
    from app.autoreply import autoreply_db
    return _cached("settings", autoreply_db.all_settings)


def faq_layer_enabled() -> bool:
    """第一層（FAQ）是否啟用，預設啟用。"""
    return _settings().get("faq_enabled", True)


def table_layer_enabled() -> bool:
    """第二層（業務表格）是否啟用，預設啟用。"""
    return _settings().get("table_enabled", True)


def rag_layer_enabled() -> bool:
    """第三層（RAG 知識庫）是否啟用，預設啟用。

    沒有知識庫資料時這一層會自己回「沒有可檢索的知識塊」而降級，
    所以預設開啟是安全的（不會因為忘了開而少一層）。
    """
    return _settings().get("rag_enabled", True)


def set_layer_enabled(layer: str, enabled: bool) -> None:
    from app.autoreply import autoreply_db
    autoreply_db.set_setting(f"{layer}_enabled", bool(enabled))
    invalidate_cache()


# ---------- FAQ 層 ----------

def _faq_entries():
    """回傳 [(faq_dict, 已預處理問法清單)]，依 sort_order 排序。"""
    from app.autoreply import autoreply_db

    def loader():
        out = []
        for f in autoreply_db.list_faqs(enabled_only=True):
            # 問法 = 主問句 + 額外關鍵詞（逗號／換行／頓號分隔）
            variants = [f["question"]]
            raw = (f.get("keywords") or "").replace("\n", ",").replace("，", ",").replace("、", ",")
            variants += [k.strip() for k in raw.split(",") if k.strip()]
            out.append((f, faq_matcher.prepare(variants)))
        return out

    return _cached("faq", loader)


def _match_faq(text: str) -> tuple[dict | None, float, str]:
    """在啟用的 FAQ 中找最佳匹配，回傳 (faq, 分數, 命中的問法)。"""
    nu, ut = faq_matcher.preprocess(text)
    threshold = config.FAQ_MATCH_THRESHOLD
    best, best_score, best_variant = None, 0.0, ""
    for faq, prepared in _faq_entries():
        for raw, (nf, ft) in prepared:
            s = faq_matcher.score_pair(nu, ut, nf, ft)
            if s > best_score:
                best, best_score, best_variant = faq, s, raw
    if best is not None and best_score >= threshold:
        return best, best_score, best_variant
    return None, best_score, best_variant


def _match_faq_multi(queries: list[str]) -> tuple[dict | None, float, str, str]:
    """在多條查詢中找最佳 FAQ 匹配。

    回傳 `(faq|None, 分數, 命中的問法, 命中的那條查詢)`。
    ⚠️ 這是**純規則匹配，不呼叫模型** —— 改寫已經在引擎開頭統一做過一次，
    FAQ 只是拿同一組查詢多試幾次（原句沒中、改寫句中了也算命中）。
    """
    best: tuple[dict | None, float, str, str] = (None, 0.0, "", "")
    top: tuple[dict | None, float, str, str] = (None, 0.0, "", "")
    for q in (queries or []):
        text = (q or "").strip()
        if not text:
            continue
        faq, sc, variant = _match_faq(text)
        if faq is not None and sc > best[1]:
            best = (faq, sc, variant, text)
        if sc > top[1]:
            top = (faq, sc, variant, text)
    # 有命中就回命中最好的那條；都沒過門檻就回「分數最高的未命中」（供 detail 顯示）
    return best if best[0] is not None else top


def _rewrite_enabled() -> bool:
    """查詢改寫是否開啟（讀知識庫的全域檢索參數，5 秒 TTL）。

    ⚠️ 2026-10-08 起這是**唯一**的改寫開關。
    原本 FAQ 另有一個 `FAQ_REWRITE_MODE`（.env），兩個開關會讓人搞不清
    「我明明關了為什麼還在呼叫模型」—— 而且那條 FAQ 專用改寫一次要 4.8 秒。
    現在統一：開了就用同一組改寫結果服務三層，關了就完全不改寫。
    """
    from app.rag import kb_search

    return kb_search.query_rewrite_enabled()


def _faq_questions() -> list[str]:
    """現有 FAQ 問句（當改寫提示詞的「用語風格參考」用）。"""
    return [f["question"] for f, _ in _faq_entries()]


# ---------- 表格層 ----------

def _tables():
    from app.autoreply import autoreply_db

    def loader():
        tables = autoreply_db.list_tables(enabled_only=True)
        rows = autoreply_db.list_rows(enabled_only=True)
        by_table: dict[int, list[dict]] = {}
        for r in rows:
            by_table.setdefault(r["table_id"], []).append(r)
        return [(t, by_table.get(t["id"], [])) for t in tables]

    return _cached("tables", loader)


def build_table_context(flavor: str = "dm") -> str:
    """把啟用的業務表格拼成給模型看的文字（有列數 / 字數上限）。"""
    max_rows = config.TABLE_CONTEXT_MAX_ROWS
    max_chars = config.TABLE_CONTEXT_MAX_CHARS

    parts: list[str] = []
    for table, rows in _tables():
        cols = [str(c) for c in (table.get("columns") or [])]
        lines = [f"【表名】{table['name']}"]
        if table.get("description"):
            lines.append(f"說明：{table['description']}")
        if cols:
            lines.append("欄位：" + "、".join(cols))

        shown = 0
        for r in rows:
            if max_rows > 0 and shown >= max_rows:
                lines.append(f"…（其餘 {len(rows) - shown} 列省略）")
                break
            data = r.get("data") or {}
            if cols:
                cells = [f"{c}：{data.get(c, '')}" for c in cols]
                lines.append(f"列 {shown + 1}：" + "；".join(cells))
            elif data:
                lines.append(f"列 {shown + 1}：" + "；".join(f"{k}：{v}" for k, v in data.items()))
            shown += 1
        parts.append("\n".join(lines))

    context = "\n\n".join(parts)
    if max_chars > 0 and len(context) > max_chars:
        context = context[:max_chars] + "\n…（表格內容過長，已截斷）"
    return context


# ---------- 回覆結果 ----------

class Answer:
    """引擎的決策結果。"""

    __slots__ = ("tier", "text", "faq_id", "score", "rewrote", "reason", "detail",
                 "matched_question", "table_context_chars", "rewritten_query",
                 "history_used", "matched_rows", "rag_threshold", "rag_candidates",
                 "rag_queries", "elapsed_ms", "llm_calls", "llm_seconds", "llm_errors")

    def __init__(self, tier: str, text: str = "", *, faq_id: int | None = None,
                 score: float | None = None, rewrote: bool = False, reason: str = "",
                 detail: str = "", matched_question: str = "", table_context_chars: int = 0,
                 rewritten_query: str = "", history_used: int = 0,
                 matched_rows: list | None = None, rag_threshold: float | None = None,
                 rag_candidates: int = 0, rag_queries: list | None = None,
                 elapsed_ms: int = 0, llm_calls: int = 0, llm_seconds: float = 0.0,
                 llm_errors: int = 0):
        self.tier = tier
        self.text = text
        self.faq_id = faq_id
        self.score = score
        self.rewrote = rewrote
        self.reason = reason
        self.detail = detail
        self.matched_question = matched_question
        self.table_context_chars = table_context_chars
        self.rewritten_query = rewritten_query
        self.history_used = history_used
        # RAG 溯源：候選清單（含未入選的）寫進 autoreply_log.matched_rows
        self.matched_rows = matched_rows
        self.rag_threshold = rag_threshold
        self.rag_candidates = rag_candidates
        # 這次實際拿去檢索的查詢句（多查詢改寫時第 0 條是原問句，其餘是改寫句）
        self.rag_queries = rag_queries or []
        # 這次決策的用量（寫進紀錄頁，才看得出「為什麼要一分鐘」）
        self.elapsed_ms = int(elapsed_ms or 0)
        self.llm_calls = int(llm_calls or 0)
        self.llm_seconds = float(llm_seconds or 0.0)
        self.llm_errors = int(llm_errors or 0)

    @property
    def hit(self) -> bool:
        """是否有可送出的回覆（false = 降級人工）。"""
        return self.tier in (TIER_FAQ, TIER_TABLE, TIER_RAG) and bool(self.text.strip())

    @property
    def label(self) -> str:
        """工作台／日誌用的標記，例如「自動回復-FAQ命中」。

        改寫後才命中的仍屬第二層，標記不變，改由 detail 註明「改寫後命中」。
        """
        if self.tier == TIER_FAQ:
            return "自動回復-FAQ命中"
        if self.tier == TIER_TABLE:
            return "自動回復-表格命中"
        if self.tier == TIER_RAG:
            return "自動回復-知識庫命中"
        return "自動回復未命中"

    def as_dict(self) -> dict:
        return {"tier": self.tier, "text": self.text, "faq_id": self.faq_id,
                "score": self.score, "rewrote": self.rewrote, "reason": self.reason,
                "detail": self.detail, "label": self.label,
                "rewritten_query": self.rewritten_query, "history_used": self.history_used,
                "matched_rows": self.matched_rows,
                "rag_threshold": self.rag_threshold,
                "rag_candidates": self.rag_candidates,
                "rag_queries": self.rag_queries,
                "elapsed_ms": self.elapsed_ms,
                "llm_calls": self.llm_calls,
                "llm_seconds": round(self.llm_seconds, 3),
                "llm_errors": self.llm_errors}


def _layer_on(tier: str, layers) -> bool:
    """這一層這次要不要跑。`layers=None` = 依 DB 開關（正式流程）。"""
    return layers is None or tier in layers


# ---------- 第二層：業務表格（抽成可 fallthrough 的形式） ----------

async def _table_try(queries: list[str], context: str, flavor: str, post_context: str,
                     score: float | None) -> tuple[Answer | None, str]:
    """試著用業務表格回答。回傳 (命中就 Answer, 未命中就說明字串)。

    `queries` 是引擎開頭算好的**共用查詢**：第 0 條固定是客戶原問句，其餘是改寫句。
    逐條拿去問，**第一條命中就停** —— 所以正常情況只花 1 次生成呼叫。
    ⚠️ 這裡**不再自己做改寫**（舊版會多呼叫一次 `rewrite_table_query`）——
    改寫已在引擎開頭統一做過，這層只是「拿現成的查詢多試一條」。

    ⚠️ 逐條「生成」比檢索貴得多（每條一次模型呼叫），所以有上限
    （`RAG_TABLE_QUERY_MAX`，預設 2）：表格真的沒這筆資料時，試完幾條也問不出來，
    多試只是浪費時間。
    """
    client = DeepSeekClient()
    tries: list[str] = []
    limit = int(rag_config.RAG_TABLE_QUERY_MAX or 0)
    use = list(queries or [])[:limit] if limit > 0 else list(queries or [])
    for i, q in enumerate(use):
        try:
            out = await client.answer_with_table(q, context, flavor=flavor,
                                                post_context=post_context)
        except DeepSeekError as e:
            log.warning("業務表格回答失敗（查詢 %d）: %s", i, e)
            tries.append(f"「{q}」呼叫失敗（{e}）")
            continue
        if looks_answered(out):
            if i == 0:
                return Answer(TIER_TABLE, _tidy(out), score=score,
                              detail=f"依業務表格生成（{len(context)} 字上下文）",
                              table_context_chars=len(context)), ""
            return Answer(TIER_TABLE, _tidy(out), score=score, rewrote=True,
                          rewritten_query=q,
                          detail=f"用改寫查詢命中：「{q}」（原問句「{queries[0]}」）",
                          table_context_chars=len(context)), ""
        tries.append(f"「{q}」未命中")

    if len(use) <= 1:
        return None, "業務表格：未命中"
    extra_note = (f"（只試了前 {len(use)} 條查詢）" if len(use) < len(queries or []) else "")
    return None, "業務表格：" + "；".join(tries) + extra_note


# ---------- 第三層：RAG 知識庫 ----------

async def _rag_try(queries: list[str], flavor: str, post_context: str, score: float | None,
                   history_text: str = "", history_used: int = 0
                   ) -> tuple[Answer | None, str, dict]:
    """試著用知識庫回答。回傳 (命中就 Answer, 未命中就說明字串, 召回明細)。

    `queries` 是引擎開頭算好的**共用查詢**（第 0 條固定是原問句，其餘是改寫句）；
    一起送去 `kb_search.search_multi()` 合併取最高分。

    ★ 2026-10-08 定案：**這裡完全不做改寫**。整個流程的改寫只有引擎開頭那一次
    （受 `query_rewrite` 開關控制），結果三層共用；沒開就只有原問句一條。
    舊版這裡還有「第一次沒過門檻 → 單句改寫 → 再查一次」的兜底，已經移除 ——
    它讓「明明關了改寫卻還在呼叫模型」變得無法解釋，而且用的是另一條提示詞，
    與「改寫只有一條 prompt」的設計衝突。

    ⚠️ 第三個回傳值（明細）在**未命中時也要帶**：設計稿 §9 講得很明白 ——
    「未命中的記錄也要能點開，看它檢索到什麼、為什麼沒過門檻，這才是調參最需要的」。
    只在命中時才寫明細的話，最該看的那一類紀錄反而空白。
    """
    from app.rag import kb_search

    qs = [q for q in (queries or []) if (q or "").strip()] or [""]
    rewrote = len(qs) > 1
    rewritten = " ｜ ".join(qs[1:])

    try:
        res = await asyncio.to_thread(kb_search.search_multi, qs)
    except Exception as e:
        log.warning("知識庫檢索失敗: %s", e)
        return None, f"知識庫：檢索失敗（{e}）", {}

    rows = [h.as_dict() for h in res.candidates[:30]]
    extra = dict(matched_rows=rows, rag_threshold=res.threshold,
                 rag_candidates=len(res.candidates), top1=res.top1,
                 rag_queries=list(res.queries or []))

    if res.reason == "empty_index":
        return None, "知識庫：還沒有已上線的文件", extra
    if not res.hits:
        return None, "知識庫：沒有檢索到任何知識塊", extra
    if not res.passed:
        return None, (f"知識庫：最高分 {res.top1:.3f} 未達門檻 {res.threshold:.2f}"
                      f"（已擋下，不呼叫模型）"), extra

    context = res.context(int(res.params.get("max_context_chars") or 0))
    try:
        # ⚠️ 生成用**客戶原問句**（qs[0]），不是改寫句 —— 改寫只是為了「查得到」，
        #    回答還是要針對客戶真正問的那句話，免得答成另一種說法的問題。
        out = await DeepSeekClient().answer_with_rag(qs[0], context, flavor=flavor,
                                                     post_context=post_context)
    except DeepSeekError as e:
        log.warning("知識庫回答失敗: %s", e)
        return None, f"知識庫：命中 {len(res.hits)} 塊但生成失敗（{e}）", extra

    if not looks_answered(out):
        return None, f"知識庫：命中 {len(res.hits)} 塊，但模型判定資料不足以回答", extra

    top = res.hits[0]
    detail = (f"知識庫命中 {len(res.hits)} 塊（最高分 {res.top1:.3f}／門檻 "
              f"{res.threshold:.2f}），主要來源：{top.source}")
    if rewrote:
        detail = f"共用查詢命中（原問句「{qs[0]}」，連同改寫句「{rewritten}」）：{detail}"
    return Answer(TIER_RAG, _tidy(out), score=res.top1, rewrote=rewrote,
                  rewritten_query=rewritten, history_used=history_used if rewrote else 0,
                  detail=detail, matched_rows=rows, rag_threshold=res.threshold,
                  rag_candidates=len(res.candidates),
                  rag_queries=list(res.queries or [])), "", extra


# ---------- 主流程 ----------

def format_history(history: list[dict] | None) -> str:
    """把對話歷史排成給模型看的文字：客服＝我方，客戶＝對方。"""
    lines = []
    for h in history or []:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        who = "客服" if h.get("direction") == "outbound" else "客戶"
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


async def answer(text: str, *, channel: str, scope: str,
                 post_context: str = "", history: list[dict] | None = None,
                 layers: list[str] | None = None,
                 query_rewrite: bool | None = None) -> Answer:
    """依四層邏輯產生回覆。channel: whatsapp|ig|fb；scope: dm|comment。

    history 是「當前這句之前」的最近對話（見 autoreply_db.get_conversation_history），
    用來改寫 query（表格層查不到時；知識庫層則是勾了「查詢改寫」才會在檢索前用）。
    回答本身仍只依該層的資料，避免模型改用對話裡的舊資訊回答。

    `layers`：只跑指定的層（命中測試頁用）。**None = 依 DB 開關**（正式流程用）。
    ⚠️ 一定要用 None 當預設值 —— 若在這裡直接讀 DB 開關當預設，就等於「測試時改了線上開關」。

    `query_rewrite`：知識庫層要不要做多查詢改寫。None = 依知識庫的全域參數；
    命中測試頁可傳 True / False 覆寫（同樣**不動 DB 參數**）。

    這裡是**外層 wrapper**（只負責計時與統計模型用量），真正的決策在 `_answer_inner()`。
    做成 wrapper 是為了不必在十幾個 return 上各加一次 —— 統計漏一個分支就白做了。
    """
    from app.llm import deepseek_client

    t0 = time.time()
    st = _stats_start()

    def _hook(ok: bool, seconds: float, err=None) -> None:
        _stats_note("", seconds, ok)

    token = deepseek_client.set_call_hook(_hook)
    try:
        ans = await _answer_inner(text, channel=channel, scope=scope,
                                  post_context=post_context, history=history,
                                  layers=layers, query_rewrite=query_rewrite)
    finally:
        deepseek_client.reset_call_hook(token)

    ans.elapsed_ms = int((time.time() - t0) * 1000)
    ans.llm_calls = int(st["calls"])
    ans.llm_seconds = float(st["seconds"])
    ans.llm_errors = int(st["errors"])
    log.info("引擎決策完成（%s/%s，tier=%s，%.1fs，模型 %d 次／%.1fs%s）",
             channel, scope, ans.tier, ans.elapsed_ms / 1000, ans.llm_calls,
             ans.llm_seconds, f"，{ans.llm_errors} 次失敗" if ans.llm_errors else "")
    return ans


async def _answer_inner(text: str, *, channel: str, scope: str,
                        post_context: str = "", history: list[dict] | None = None,
                        layers: list[str] | None = None,
                        query_rewrite: bool | None = None) -> Answer:
    """四層決策本體（不負責計時；見 `answer()`）。"""
    question = (text or "").strip()
    if not question:
        return Answer(TIER_MISS, reason="empty_text")

    flavor = "comment" if scope == "comment" else "dm"
    history_text = format_history(history)
    history_used = len([h for h in (history or []) if (h.get("text") or "").strip()])
    static_detail = "" if layers is None else "（命中測試：只跑勾選的層）"

    # ---- 第一層：FAQ（先只用客戶原句做規則匹配 —— 命中就完全不必改寫）----
    score = None
    faq_on = _layer_on(TIER_FAQ, layers) and faq_layer_enabled()
    if faq_on:
        faq, score, variant, _q = await asyncio.to_thread(_match_faq_multi, [question])
        if faq:
            return Answer(TIER_FAQ, faq["answer"].strip(), faq_id=faq["id"], score=score,
                          matched_question=variant,
                          detail=f"FAQ #{faq['id']}「{variant}」分數 {score:.2f}")

    # ---- 統一改寫：整個流程唯一的一次改寫呼叫（FAQ 未命中才做）----
    # 這一組查詢會給**三層共用**：FAQ 二次匹配（純規則）、業務表格生成、知識庫檢索。
    # 舊做法是每層各自改寫（FAQ 一次、表格一次、知識庫一次），實測一次回覆因此吃掉
    # 8 次模型呼叫 / 48.9 秒。現在 FAQ 命中時 0 次、未命中時也只有 1 次。
    from app.rag import kb_search

    use_rewrite = (kb_search.query_rewrite_enabled() if query_rewrite is None
                   else bool(query_rewrite))
    queries: list[str] = [question]
    rewrite_note = ""

    if use_rewrite and config.DEEPSEEK_API_KEY:
        try:
            extra_q = await DeepSeekClient().rewrite_queries(
                question, history_text, count=rag_config.RAG_QUERY_REWRITE_N,
                faq_questions=_faq_questions() if faq_on else None,
                post_context=post_context)
        except DeepSeekError as e:
            log.warning("查詢改寫失敗（沿用原問句）: %s", e)
            extra_q = []
            rewrite_note = f"（查詢改寫失敗，沿用原問句：{e}）"
        if extra_q:
            queries += extra_q
            # FAQ 拿同一組查詢再匹配一次（**純規則、不再呼叫模型**）
            if faq_on:
                faq2, score2, variant2, q2 = await asyncio.to_thread(_match_faq_multi, queries)
                if faq2:
                    return Answer(TIER_FAQ, faq2["answer"].strip(), faq_id=faq2["id"],
                                  score=score2, rewrote=True, matched_question=variant2,
                                  rewritten_query=q2, history_used=history_used,
                                  detail=f"FAQ #{faq2['id']}「{variant2}」用改寫查詢 "
                                         f"「{q2}」命中（分數 {score2:.2f}）")
        else:
            rewrite_note = "（改寫沒有產出可用查詢，沿用原問句）"

    notes: list[str] = []

    # ---- 第二層：業務表格（用同一組查詢逐條問，第一條命中就停）----
    if not _layer_on(TIER_TABLE, layers):
        notes.append("業務表格層未勾選")
    elif not table_layer_enabled():
        notes.append("業務表格層已停用")
    else:
        context = await asyncio.to_thread(build_table_context, flavor)
        if not context.strip():
            notes.append("沒有已啟用的業務表格資料")
        elif not config.DEEPSEEK_API_KEY:
            return Answer(TIER_MISS, score=score, reason="no_api_key",
                          detail="未設定 DEEPSEEK_API_KEY，AI 層（業務表格／知識庫）無法運作")
        else:
            ans, note = await _table_try(queries, context, flavor, post_context, score)
            if ans:
                ans.rag_queries = list(queries)
                return ans
            notes.append(note)

    # ---- 第三層：RAG 知識庫（用同一組查詢一起檢索）----
    rag_extra: dict = {}
    if not _layer_on(TIER_RAG, layers):
        notes.append("知識庫層未勾選")
    elif not rag_layer_enabled():
        notes.append("知識庫層已停用")
    elif not config.DEEPSEEK_API_KEY:
        return Answer(TIER_MISS, score=score, reason="no_api_key",
                      detail="未設定 DEEPSEEK_API_KEY，AI 層（業務表格／知識庫）無法運作")
    else:
        ans, note, rag_extra = await _rag_try(queries, flavor, post_context, score,
                                             history_text, history_used)
        if ans:
            if rewrite_note and rewrite_note not in ans.detail:
                ans.detail += rewrite_note
            return ans
        notes.append(note + rewrite_note)

    # ---- 第四層：降級人工 ----
    # ★ 未命中也要帶上候選明細（調門檻／top-k 全靠它）
    return Answer(TIER_MISS, score=(score if score is not None else rag_extra.get("top1")),
                  reason="no_match",
                  detail="；".join(notes) + static_detail,
                  matched_rows=rag_extra.get("matched_rows"),
                  rag_threshold=rag_extra.get("rag_threshold"),
                  rag_candidates=int(rag_extra.get("rag_candidates") or 0),
                  rag_queries=rag_extra.get("rag_queries") or (list(queries) if len(queries) > 1 else None))


def _tidy(text: str) -> str:
    """整理模型輸出：去掉 NO_ANSWER 標記與多餘分隔線。"""
    from app.llm.deepseek_client import NO_ANSWER_SENTINEL
    t = (text or "").replace(NO_ANSWER_SENTINEL, "").strip()
    lines = [ln.strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip("-—─=* ")).strip() or t


def cache_fingerprint() -> str:
    """目前 FAQ/表格/開關設定的指紋（測試端點用來確認快取已更新）。"""
    payload = json.dumps({
        "faq": [(f["id"], f["question"], f.get("keywords") or "") for f, _ in _faq_entries()],
        "tables": [(t["id"], t["name"]) for t, _ in _tables()],
        "settings": sorted(_settings().items()),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
