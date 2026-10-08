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

import config
import faq_matcher
from deepseek_client import DeepSeekClient, DeepSeekError, looks_answered

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
    import autoreply_db
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
    import autoreply_db
    autoreply_db.set_setting(f"{layer}_enabled", bool(enabled))
    invalidate_cache()


# ---------- FAQ 層 ----------

def _faq_entries():
    """回傳 [(faq_dict, 已預處理問法清單)]，依 sort_order 排序。"""
    import autoreply_db

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


def _rewrite_enabled() -> bool:
    return (config.FAQ_REWRITE_MODE or "off").lower() not in ("off", "none", "false", "0", "")


def _faq_questions() -> list[str]:
    return [f["question"] for f, _ in _faq_entries()]


# ---------- 表格層 ----------

def _tables():
    import autoreply_db

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
                 "history_used", "matched_rows", "rag_threshold", "rag_candidates")

    def __init__(self, tier: str, text: str = "", *, faq_id: int | None = None,
                 score: float | None = None, rewrote: bool = False, reason: str = "",
                 detail: str = "", matched_question: str = "", table_context_chars: int = 0,
                 rewritten_query: str = "", history_used: int = 0,
                 matched_rows: list | None = None, rag_threshold: float | None = None,
                 rag_candidates: int = 0):
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
                "rag_candidates": self.rag_candidates}


def _layer_on(tier: str, layers) -> bool:
    """這一層這次要不要跑。`layers=None` = 依 DB 開關（正式流程）。"""
    return layers is None or tier in layers


# ---------- 第二層：業務表格（抽成可 fallthrough 的形式） ----------

async def _table_try(question: str, context: str, flavor: str, post_context: str,
                     history_text: str, history_used: int,
                     score: float | None) -> tuple[Answer | None, str]:
    """試著用業務表格回答。回傳 (命中就 Answer, 未命中就說明字串)。"""
    client = DeepSeekClient()
    try:
        out = await client.answer_with_table(question, context, flavor=flavor,
                                            post_context=post_context)
    except DeepSeekError as e:
        log.warning("業務表格回答失敗: %s", e)
        return None, f"業務表格：呼叫模型失敗（{e}）"

    if looks_answered(out):
        return Answer(TIER_TABLE, _tidy(out), score=score,
                      detail=f"依業務表格生成（{len(context)} 字上下文）",
                      table_context_chars=len(context)), ""

    ctx_note = f"（帶入最近 {history_used} 則對話）" if history_used else "（無更早對話）"
    try:
        rewritten = await client.rewrite_table_query(question, history_text)
    except DeepSeekError as e:
        log.warning("業務表格 query 改寫失敗: %s", e)
        return None, f"業務表格：未命中且改寫失敗（{e}）"

    if not rewritten or rewritten == question:
        return None, f"業務表格：未命中，改寫後問句無變化{ctx_note}"

    try:
        out2 = await client.answer_with_table(rewritten, context, flavor=flavor,
                                             post_context=post_context)
    except DeepSeekError as e:
        log.warning("業務表格改寫後回答失敗: %s", e)
        return None, f"業務表格：改寫為「{rewritten}」後呼叫失敗（{e}）"

    if looks_answered(out2):
        return Answer(TIER_TABLE, _tidy(out2), score=score, rewrote=True,
                      rewritten_query=rewritten, history_used=history_used,
                      detail=f"改寫後命中{ctx_note}：原問句「{question}」 → 改寫「{rewritten}」",
                      table_context_chars=len(context)), ""

    return None, f"業務表格：改寫為「{rewritten}」後仍未命中{ctx_note}"


# ---------- 第三層：RAG 知識庫 ----------

async def _rag_try(question: str, flavor: str, post_context: str, score: float | None,
                   history_text: str = "", history_used: int = 0
                   ) -> tuple[Answer | None, str, dict]:
    """試著用知識庫回答。回傳 (命中就 Answer, 未命中就說明字串, 召回明細)。

    ⚠️ 第三個回傳值（明細）在**未命中時也要帶**：設計稿 §9 講得很明白 ——
    「未命中的記錄也要能點開，看它檢索到什麼、為什麼沒過門檻，這才是調參最需要的」。
    只在命中時才寫明細的話，最該看的那一類紀錄反而空白。

    指代型追問（「那這個多少錢？」）第一次檢索通常撈不到東西 —— 這時才帶對話請模型
    改寫問句再檢索一次。**只在第一次沒過門檻時才做**，平常不會多花一次模型呼叫。
    """
    import kb_search

    async def _search(q: str):
        return await asyncio.to_thread(kb_search.search, q)

    try:
        res = await _search(question)
    except Exception as e:
        log.warning("知識庫檢索失敗: %s", e)
        return None, f"知識庫：檢索失敗（{e}）", {}

    rewrote = False
    rewritten = ""
    if not res.passed and history_text.strip() and config.DEEPSEEK_API_KEY:
        try:
            rewritten = await DeepSeekClient().rewrite_table_query(question, history_text)
        except DeepSeekError as e:
            log.debug("知識庫 query 改寫失敗（沿用原問句）: %s", e)
            rewritten = ""
        if rewritten and rewritten != question:
            try:
                res2 = await _search(rewritten)
            except Exception as e:
                log.warning("知識庫改寫後檢索失敗: %s", e)
                res2 = None
            if res2 is not None and res2.top1 > res.top1:
                res, rewrote = res2, True

    rows = [h.as_dict() for h in res.candidates[:30]]
    extra = dict(matched_rows=rows, rag_threshold=res.threshold,
                 rag_candidates=len(res.candidates), top1=res.top1)

    if res.reason == "empty_index":
        return None, "知識庫：還沒有已上線的文件", extra
    if not res.hits:
        return None, "知識庫：沒有檢索到任何知識塊", extra
    if not res.passed:
        return None, (f"知識庫：最高分 {res.top1:.3f} 未達門檻 {res.threshold:.2f}"
                      f"（已擋下，不呼叫模型）"), extra

    context = res.context(int(res.params.get("max_context_chars") or 0))
    try:
        out = await DeepSeekClient().answer_with_rag(question, context, flavor=flavor,
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
        detail = f"改寫後命中（「{question}」 → 「{rewritten}」）：{detail}"
    return Answer(TIER_RAG, _tidy(out), score=res.top1, rewrote=rewrote,
                  rewritten_query=rewritten, history_used=history_used if rewrote else 0,
                  detail=detail, matched_rows=rows, rag_threshold=res.threshold,
                  rag_candidates=len(res.candidates)), "", extra


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
                 layers: list[str] | None = None) -> Answer:
    """依四層邏輯產生回覆。channel: whatsapp|ig|fb；scope: dm|comment。

    history 是「當前這句之前」的最近對話（見 autoreply_db.get_conversation_history），
    只在某層第一次查不到時拿來改寫 query。回答本身仍只依該層的資料，
    避免模型改用對話裡的舊資訊回答。

    `layers`：只跑指定的層（命中測試頁用）。**None = 依 DB 開關**（正式流程用）。
    ⚠️ 一定要用 None 當預設值 —— 若在這裡直接讀 DB 開關當預設，就等於「測試時改了線上開關」。
    """
    question = (text or "").strip()
    if not question:
        return Answer(TIER_MISS, reason="empty_text")

    flavor = "comment" if scope == "comment" else "dm"
    history_text = format_history(history)
    history_used = len([h for h in (history or []) if (h.get("text") or "").strip()])
    static_detail = "" if layers is None else "（命中測試：只跑勾選的層）"

    # ---- 第一層：FAQ ----
    score = None
    if _layer_on(TIER_FAQ, layers) and faq_layer_enabled():
        faq, score, variant = await asyncio.to_thread(_match_faq, question)
        if faq:
            return Answer(TIER_FAQ, faq["answer"].strip(), faq_id=faq["id"], score=score,
                          matched_question=variant,
                          detail=f"FAQ #{faq['id']}「{variant}」分數 {score:.2f}")

        api_key = config.DEEPSEEK_API_KEY
        if _rewrite_enabled() and api_key:
            try:
                client = DeepSeekClient()
                rewritten = await client.rewrite_query(question, _faq_questions())
                if rewritten and rewritten != question:
                    faq2, score2, variant2 = await asyncio.to_thread(_match_faq, rewritten)
                    if faq2:
                        return Answer(TIER_FAQ, faq2["answer"].strip(), faq_id=faq2["id"],
                                      score=score2, rewrote=True, matched_question=variant2,
                                      detail=f"FAQ #{faq2['id']}「{variant2}」改寫後分數 {score2:.2f}"
                                             f"（原問句：{rewritten}）")
            except DeepSeekError as e:
                # 改寫失敗不阻斷，直接進下一層
                log.warning("FAQ 改寫失敗，改走下一層: %s", e)

    notes: list[str] = []

    # ---- 第二層：業務表格 ----
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
            ans, note = await _table_try(question, context, flavor, post_context,
                                         history_text, history_used, score)
            if ans:
                return ans
            notes.append(note)

    # ---- 第三層：RAG 知識庫 ----
    rag_extra: dict = {}
    if not _layer_on(TIER_RAG, layers):
        notes.append("知識庫層未勾選")
    elif not rag_layer_enabled():
        notes.append("知識庫層已停用")
    elif not config.DEEPSEEK_API_KEY:
        return Answer(TIER_MISS, score=score, reason="no_api_key",
                      detail="未設定 DEEPSEEK_API_KEY，AI 層（業務表格／知識庫）無法運作")
    else:
        ans, note, rag_extra = await _rag_try(question, flavor, post_context, score,
                                             history_text, history_used)
        if ans:
            return ans
        notes.append(note)

    # ---- 第四層：降級人工 ----
    # ★ 未命中也要帶上候選明細（調門檻／top-k 全靠它）
    return Answer(TIER_MISS, score=(score if score is not None else rag_extra.get("top1")),
                  reason="no_match",
                  detail="；".join(notes) + static_detail,
                  matched_rows=rag_extra.get("matched_rows"),
                  rag_threshold=rag_extra.get("rag_threshold"),
                  rag_candidates=int(rag_extra.get("rag_candidates") or 0))


def _tidy(text: str) -> str:
    """整理模型輸出：去掉 NO_ANSWER 標記與多餘分隔線。"""
    from deepseek_client import NO_ANSWER_SENTINEL
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
