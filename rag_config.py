"""RAG 知識庫（自動回覆第三層）的參數與範圍定義。

`.env` 可覆寫，留空 = 用這裡的預設（與提示詞的慣例一致）。

⚠️ 這裡只是**預設值**。實際執行時：
      執行時取值 = DB(kb_param) 有設定 ? DB 值 : 這裡的預設
   因為 `config._get()` 是 **import 時求值** —— UI 改參數要「立刻生效、不用重啟」
   就不能只靠 .env。DB 覆寫見 `kb_db.py` 的 kb_param，解析入口 `load_params()`。
   設計依據：RAG_DESIGN.md §4.1。

模型選型（2026-10-08 定案）：
    embedding = jinaai/jina-embeddings-v2-base-zh   640MB / dim 768 / 上限 8192 token
    rerank    = 第一版不裝（介面先留 NoopReranker）
    → 因為 8192 token 上限，塊長可放心到 1200 字，不會被無聲截斷。
"""
import os

import config  # noqa: F401  確保 .env 已載入（config 模組頂層會 load_dotenv）


def _get(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v not in (None, "") else default


# ---------- 模型 ----------

# 本地 embedding 模型（fastembed 走 onnxruntime，不需 torch）
RAG_EMBED_MODEL = _get("RAG_EMBED_MODEL", "jinaai/jina-embeddings-v2-base-zh")
RAG_EMBED_DIM = int(_get("RAG_EMBED_DIM", "768"))
# 模型能吃多少 token —— 塊長上限要 ≤ 它（jina-zh 是 8192）
RAG_EMBED_MAX_TOKENS = int(_get("RAG_EMBED_MAX_TOKENS", "8192"))

# 第一版不裝 reranker，但介面先留好；要開只需下模型 + 改這個開關
RAG_RERANK_MODEL = _get("RAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3-int8")

# 下載鏡像（hf-mirror 實測可用）與快取目錄
RAG_HF_ENDPOINT = _get("RAG_HF_ENDPOINT", "https://hf-mirror.com")
_here = os.path.dirname(os.path.abspath(__file__))
RAG_MODEL_DIR = _get("RAG_MODEL_DIR", os.path.join(_here, "models", "fastembed"))

# 上傳檔案落地目錄（media/kb/<uuid>/<原始檔>）
RAG_KB_DIR = _get("RAG_KB_DIR", os.path.join(_here, "media", "kb"))

# ---------- 混合檢索權重 ----------
# 純向量在中文短問句（含指代）上不穩，混入 jieba 字面分數補強（RAG_DESIGN.md §8）
RAG_VECTOR_WEIGHT = float(_get("RAG_VECTOR_WEIGHT", "0.65"))
RAG_LEXICAL_WEIGHT = float(_get("RAG_LEXICAL_WEIGHT", "0.35"))


# ---------- RAG 生成提示詞 ----------
# ⚠️ 放在這裡而不是 config.py：RAG 的東西集中一處，config.py 不必為了第三層再長一截。
#    {context} 放檢索到的資料，{sentinel} 放「答不出來」的標記（沿用 __NO_ANSWER__）。
RAG_PROMPT = _get(
    "DEEPSEEK_RAG_PROMPT",
    "以下是知识库检索到的资料（每段前有编号与来源）：\n"
    "─────\n{context}\n─────\n\n"
    "请「只根据上述资料」回答使用者的問題。\n"
    "涉及数据或条款的输出请组织成通顺的语句，不要带 '*'、'-' 这类分隔符号。\n"
    "问什么答什么，严禁给出使用者没问到的额外资讯，但仍需用完整语句礼貌回答。\n"
    "═══ 最高优先规则（覆盖以上所有叙述）═══\n"
    "若上述资料完全无法回答此问题，你唯一允许的输出就是这串字元：{sentinel}\n"
    "不可用「没有」「查不到」「抱歉」等自然语言代替，也不要加上任何说明、标点或多余文字。\n"
    "宁可只输出这个标记，也不要编造或补充资料以外的资讯。",
)


# ---------- 參數規格（單一真相：UI 渲染 / 後端驗證 / 預設值都讀這裡） ----------
# group: retrieval = 概覽頁「參數配置」彈窗（改完立即生效）
#        chunk     = 向導分塊步驟就地調（改了要重建）
# rebuild: True 表示改動後需要「重新切塊 + 重跑 embedding」
PARAM_SPECS: dict[str, dict] = {
    # ── A. 檢索參數（5 秒生效，不用重啟、不用重建）──
    "top_k": dict(
        env="RAG_TOP_K", label="召回數 top-k", group="retrieval", type="int",
        default=6, min=1, max=30, rebuild=False,
        help="送進模型的知識塊數",
    ),
    "min_score": dict(
        env="RAG_MIN_SCORE", label="入選門檻", group="retrieval", type="float",
        # ⚠️ 0.28 是**實測校正**出來的，不是設計稿原本估的 0.55。
        #    見 `kb_search_check.py` 的分數分佈：這套計分（0.65×cosine + 0.35×字面）
        #    連「近乎完美命中」的問句也只有 ~0.61；用 0.55 會讓 8 條相關問句只過 2 條，
        #    整個第三層形同關閉。實測「最大值：不相關 0.193 / 相關 0.608」，
        #    取兩者中點 0.28 → 相關過 7 條、不相關 0 條。
        #    P4 校準時請用你自己的真實問句重跑 kb_search_check.py 再定。
        default=0.28, min=0.0, max=0.95, rebuild=False,
        help="低於此分數 → 直接降級人工，不呼叫模型（實測校正，非 0.55）",
    ),
    "max_per_doc": dict(
        env="RAG_MAX_PER_DOC", label="單檔最多入選", group="retrieval", type="int",
        default=3, min=1, max=10, rebuild=False,
        help="防單一文件洗版",
    ),
    "max_context_chars": dict(
        env="RAG_MAX_CONTEXT_CHARS", label="上下文上限（字）", group="retrieval", type="int",
        default=6000, min=1000, max=20000, rebuild=False,
        help="拼給模型的總字數",
    ),
    "rerank_enabled": dict(
        env="RAG_RERANK_ENABLED", label="啟用 rerank", group="retrieval", type="bool",
        default=False, min=None, max=None, rebuild=False,
        help="第一版不裝；開啟前提是模型已下載",
    ),
    "rerank_top_n": dict(
        env="RAG_RERANK_TOP_N", label="rerank 候選數", group="retrieval", type="int",
        default=20, min=5, max=100, rebuild=False,
        help="rerank 前先取幾塊做候選（介面先留）",
    ),
    # ── B. 分塊參數（改了要重建）──
    "chunk_strategy": dict(
        env="RAG_CHUNK_STRATEGY", label="分塊策略", group="chunk", type="choice",
        choices=["auto", "paragraph", "heading", "fixed", "table_row"],
        default="auto", min=None, max=None, rebuild=True,
        help="auto = 依 doc_type 自動挑（見 RAG_DESIGN.md §6）",
    ),
    "chunk_size": dict(
        env="RAG_CHUNK_SIZE", label="目標塊長（字）", group="chunk", type="int",
        default=500, min=100, max=1200, rebuild=True,
        help="分塊目標字數",
    ),
    "chunk_min": dict(
        env="RAG_CHUNK_MIN", label="塊長下限（字）", group="chunk", type="int",
        default=200, min=50, max=800, rebuild=True,
        help="小於此值併入前一塊",
    ),
    "chunk_max": dict(
        env="RAG_CHUNK_MAX", label="塊長上限（字）", group="chunk", type="int",
        default=600, min=200, max=1200, rebuild=True,
        help="超過就切（受模型 token 上限約束）",
    ),
    "chunk_overlap": dict(
        env="RAG_CHUNK_OVERLAP", label="塊重疊（字）", group="chunk", type="int",
        default=80, min=0, max=300, rebuild=True,
        help="約 15%",
    ),
    "chunk_prefix": dict(
        env="RAG_CHUNK_PREFIX", label="每塊加前綴", group="chunk", type="bool",
        default=True, min=None, max=None, rebuild=True,
        help="{文件} › {章節}，與內文一起 embed",
    ),
}


# ---------- 型別轉換 / 邊界夾 ----------

def as_int(raw, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def as_float(raw, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


_TRUE = {"1", "true", "yes", "y", "on", "是", "開", "开"}
_FALSE = {"0", "false", "no", "n", "off", "否", "關", "关"}


def as_bool(raw, default: bool) -> bool:
    s = str(raw).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def coerce(name: str, raw):
    """把原始值（env 字串 / DB 值）轉成該參數的正確型別，並夾到允許範圍。

    超出範圍就夾到邊界（避免客服填 top-k=999 把模型打爆）。
    """
    spec = PARAM_SPECS[name]
    t = spec["type"]
    if t == "int":
        v = as_int(raw, spec["default"])
        if spec["min"] is not None:
            v = max(spec["min"], min(spec["max"], v))
        return v
    if t == "float":
        v = as_float(raw, spec["default"])
        if spec["min"] is not None:
            v = max(spec["min"], min(spec["max"], v))
        return v
    if t == "bool":
        return as_bool(raw, spec["default"])
    if t == "choice":
        s = str(raw).strip()
        return s if s in spec["choices"] else spec["default"]
    return raw


def defaults() -> dict:
    """全部參數的預設值（解析後型別）。"""
    return {name: coerce(name, spec["default"]) for name, spec in PARAM_SPECS.items()}
