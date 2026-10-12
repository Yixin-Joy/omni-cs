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

from app.core import config  # noqa: F401  確保 .env 已載入（config 模組頂層會 load_dotenv）


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
# 專案根（app/rag/rag_config.py → 上三層）；models/ 與 media/ 都在根目錄
_here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAG_MODEL_DIR = _get("RAG_MODEL_DIR", os.path.join(_here, "models", "fastembed"))

# 上傳檔案落地目錄（media/kb/<uuid>/<原始檔>）—— **不對外公開**
RAG_KB_DIR = _get("RAG_KB_DIR", os.path.join(_here, "media", "kb"))

# ★ 圖片公開目錄（2026-10-12）—— 抽出來／上傳的圖一律存這裡，掛在 `/media` 之下。
#   為什麼另開一個目錄而不是直接用 media/：
#     ① 圖片要能被 markdown 直接引用（`![題注](/media/xxx.png)`），所以必須是公開靜態路徑；
#     ② 但 `media/` 底下還有客戶來訊的檔案與上傳的來源文件，**不能**一起公開。
#   落地檔名 = `<sha256 前 16 碼>.<ext>`（天然去重、無中文檔名問題），
#   對外 URL = `/media/<檔名>`（見 `public_url()`）。
RAG_MEDIA_DIR = _get("RAG_MEDIA_DIR", os.path.join(_here, "media", "kb_assets"))

# 圖片對外網址的前綴（相對路徑 —— 換域名不用改資料）
RAG_MEDIA_URL_PREFIX = _get("RAG_MEDIA_URL_PREFIX", "/media")


def media_url(name: str) -> str:
    """圖片檔名 → 對外網址（`/media/xxx.png`）。"""
    n = os.path.basename(str(name or "").replace("\\", "/"))
    return f"{RAG_MEDIA_URL_PREFIX.rstrip('/')}/{n}" if n else ""


def media_url_of_rel(rel_path: str) -> str:
    """`kb_asset.rel_path` → 對外網址。

    ⚠️ 舊資料的圖存在 `kb/<uuid>/assets/xxx.png`（在 RAG_KB_DIR 底下）——
    那些**不在**公開目錄裡，回空字串讓呼叫端退回走登入端點，
    不要因為「看起來有 rel_path」就給出一個 404 的公開網址。
    """
    rel = str(rel_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return ""
    base = os.path.basename(rel)
    # 新的圖都在公開目錄（檔名就是 sha 前 16 碼 + 副檔名）
    if os.path.exists(os.path.join(RAG_MEDIA_DIR, base)):
        return media_url(base)
    return ""

# ---------- 混合檢索權重 ----------
# 純向量在中文短問句（含指代）上不穩，混入 jieba 字面分數補強（RAG_DESIGN.md §8）
RAG_VECTOR_WEIGHT = float(_get("RAG_VECTOR_WEIGHT", "0.65"))
RAG_LEXICAL_WEIGHT = float(_get("RAG_LEXICAL_WEIGHT", "0.35"))


# ---------- RAG 生成提示詞（知識庫問答的 system prompt） ----------
# ⚠️ 放在這裡而不是 config.py：RAG 的東西集中一處，config.py 不必為了第三層再長一截。
#    {context} 放檢索到的資料，{sentinel} 放「答不出來」的標記（沿用 __NO_ANSWER__）。
#
# ★ 2026-10-08：這條提示詞是**參數**（group='prompt'，所有知識庫共用一組），
#   可在工作台「知識庫 → 參數配置 → C. 提示詞」直接編輯、約 5 秒生效。
#   這裡只是**內建預設**；執行時 `kb_search.prompt_of("rag_answer_prompt")` 取 DB 覆寫。
#   少了 {context} / {sentinel} 會被寫入端擋下（見 `PROMPT_REQUIRED`），
#   讀取端 `prompt_value()` 也會退回預設 —— 不讓 .format() 直接炸。
RAG_PROMPT = _get(
    "DEEPSEEK_RAG_PROMPT",
    "以下是知识库检索到的资料：\n"
    "─────\n{context}\n─────\n\n"
    "请根据资料回答使用者的问题。\n\n"
    "1. 忠实指「事实一致」，不是字面照抄：资料没写的一律不说，"
    "但要用你自己的话组织成连贯段落，像工程师向同事说明；"
    "资料中的列表符号（·、-）和章节编号是排版格式，不用保留。\n"
    "2. 只有代码、JSON、表格、报错原文需要原样输出（代码块或表格，"
    "保留字段名与示例值），后接简短说明；步骤、建议、注意事项等"
    "文字内容禁止照抄。\n"
    "3. 同一内容只说一次：严禁「先罗列原文，再用『也就是…』总结一遍」"
    "的结构，禁止「原文如下」「资料中写道」等引用语。\n"
    "4. 问什么答什么，不主动补充；叙述用完整句子连成段落，不用「·」「-」分条。\n"
    "5. 资料完全无法回答此问题时，你唯一允许的输出就是：{sentinel}"
    "（前面后面都不加任何文字）\n\n"
    "例：资料是「先测纯文字回覆确认200，再测来讯转发，然后测档案，最后测错误路径」"
    "四条步骤，问联调怎么做 → 直接答「先确认纯文字回覆能返回 200，再验证来讯转发"
    "与解析，接着测档案传输，最后逐一验证错误情境」，不要先抄一遍步骤再换个说法总结。",
)


# ---------- 「查詢改寫」提示詞（一次改寫、三層共用） ----------
# ⚠️ 2026-10-08 定案：**改寫只呼叫一次模型**，結果給三層共用。
#    舊做法是每層各自改寫（FAQ 一次、表格一次、知識庫一次）→ 實測一次回覆 8 次模型呼叫。
#    現在是「FAQ 先用原句做規則匹配 → 沒中才改寫一次 → 這次的結果同時給
#    FAQ 二次匹配 / 業務表格生成 / 知識庫檢索」。所以提示詞必須**一次滿足三種用途**。
#
# 要它做到三件事，照這個優先序：
#   ① 同音／近音錯別字（客戶用手機打字，錯的常常是關鍵名詞：藍芽→藍牙、腿貨→退貨）
#   ② 上下文補齊（「這個多少錢」的主語在對話裡）
#   ③ 保留一條「客戶口語版」（只修錯字、保留原用詞）—— 本服務的標準說法萬一與客戶
#      用語差距太大，還有這條檢索得到
#
# {n} = 幾條、{history} = 最近對話、{question} = 客戶這一句、{faq_hint} = FAQ 問句參考。
# 用 replace 不用 format —— 免得使用者自訂的 .env 提示詞裡有 '{' 就炸。
RAG_QUERY_REWRITE_N = int(_get("RAG_QUERY_REWRITE_N", "2"))

# 注入提示詞的 FAQ 問句上限（太多會讓 prompt 膨脹，反而拖慢改寫）
RAG_REWRITE_FAQ_HINT_MAX = int(_get("RAG_REWRITE_FAQ_HINT_MAX", "40"))

# 業務表格最多用幾條查詢去「問答案」（第 0 條固定是原問句）。
# ⚠️ 為什麼要設上限：表格層是**逐條生成**（每條一次模型呼叫，約 2 秒），
#    不像知識庫檢索那麼便宜。而且表格若真的沒有這筆資料，2 條改寫句大概率也問不出來，
#    試完 3 條只是多花 2 秒。預設 2 = 「原句 + 客戶口語版」，涵蓋最常見的兩種寫法。
#    0 或負數 = 不限制（試完所有查詢）。
RAG_TABLE_QUERY_MAX = int(_get("RAG_TABLE_QUERY_MAX", "2"))

RAG_REWRITE_PROMPT = _get(
    "DEEPSEEK_RAG_REWRITE_PROMPT",
    "你是客服系統的「查詢改寫」助手。客戶用手機打字，問句常常又短、又有錯字、又缺主語，"
    "原句直接拿去查常常查不到東西。\n"
    "請把客戶這一句改寫成 {n} 條查詢句，**只輸出 {n} 行，一行一條**。\n\n"
    "⚠️ 這 {n} 條查詢會**同時**被用在三個地方，所以要讓它們「怎麼查都查得到」：\n"
    "  · 比對現有 FAQ 問答對 → 需要貼近本服務的標準說法\n"
    "  · 查業務資料表 → 需要明確的品名、項目或欄位\n"
    "  · 檢索知識庫文件 → 需要完整語義，不要留代名詞\n\n"
    "═══ 第一優先：同音／近音錯別字（最重要）═══\n"
    "客戶打錯的往往是**關鍵名詞**（產品名、功能、費用、程序）。看到不合常理的字，"
    "先想「這是不是打錯的音」，換成我們這一行會用的正確詞：\n"
    "  藍芽→藍牙、腿貨／推貨→退貨、飽固／保故→保固、多少千→多少錢、運费→運費、\n"
    "  付近／負近→附近、門事→門市、刷刷→刷卡、分奇→分期、續行→續航、發飄→發票\n"
    "⚠️ 拿不準是哪個詞時，**兩種寫法各出一條**（一條保留客戶原本的字、一條用你判斷的正確詞），"
    "這樣兩種寫法都檢索得到，不要只猜一種。\n\n"
    "═══ 第二優先：上下文補齊 ═══\n"
    "客戶常只說「這個多少錢」「那可以退嗎」「第二個呢」—— 主語在對話裡。\n"
    "請從下面「最近的對話」找出被指代的商品或項目，把主語補進查詢句，"
    "讓它不看對話也讀得懂。補不出來就維持原樣，**絕對不要编造**品名、價格或規格。\n\n"
    "═══ 第三優先：留一條「客戶口語版」═══\n"
    "若 {n} ≥ 2，請讓其中一條**盡量保留客戶原本的用詞**（只修正錯字、補上主語，"
    "不要換成另一種說法）。這樣即使我們系統的標準說法與客戶用語差距很大，"
    "也還有一條查得到。\n\n"
    "═══ 輸出規則（違反整條作廢）═══\n"
    "1. 只輸出 {n} 行查詢句；**不要編號、不要引號、不要任何說明或前置詞**。\n"
    "2. 每行都是能獨立理解的完整問句，不要加「請問」「麻煩」這類客套。\n"
    "3. 保留客戶原本的意圖，不要問客戶沒問的事，也不要在這裡回答問題。\n"
    "4. {n} 行不可以完全一樣。\n"
    "{faq_hint}\n"
    "最近的對話（由舊到新；客服＝我方，客戶＝對方；可能為空）：\n{history}\n\n"
    "客戶這一句：{question}",
)

# 注進提示詞的 FAQ 參考段落（{faq_hint} 的實際內容）
RAG_REWRITE_FAQ_HINT = _get(
    "DEEPSEEK_REWRITE_FAQ_HINT",
    "本服務現有的 FAQ 問句（僅供參考用語風格，改寫時盡量靠近這種說法；"
    "但不要照抄不相關的那幾條）：\n{faq_list}\n",
)


# ---------- 圖片描述提示詞（按鈕觸發 AI；預設由人工寫） ----------
# ★ 2026-10-12：知識庫的圖**預設由人工寫描述**；這條只有使用者按「AI 描述」時才用。
#   重點是「圖中文字要照抄」—— 實測本專案的模型有視覺能力、能讀出圖中文字，
#   把文字抄進描述等於順手讓「圖裡的字」也變成可檢索內容（不必另裝 OCR）。
DEFAULT_IMAGE_PROMPT = _get(
    "DEEPSEEK_IMAGE_CAPTION_PROMPT",
    "你是知識庫的「圖片描述」助手。請為這張圖片寫一段描述，"
    "用途是**讓這段描述日後能被檢索到**（客戶問到圖裡的內容時要能命中）。\n\n"
    "請照這個順序寫：\n"
    "1. 圖中**所有文字**照抄出來（標題、欄位名、按鈕文字、數字、代號、錯誤訊息…），"
    "保持原本的大小寫與符號 —— 這是最重要的部分。沒有文字就跳過這段。\n"
    "2. 這張圖是什麼（介面截圖／流程圖／表格／示意圖／照片…），以及它在講什麼。\n"
    "3. 關鍵資訊：涉及的欄位、參數、步驟、數值範圍。\n\n"
    "規則：\n"
    "- 只描述看得到的內容，**不要推測**沒出現的東西，也不要補充你自己的知識。\n"
    "  （圖上沒寫價格就不要寫價格。）\n"
    "- 不要用「這張圖顯示」這類開場白，直接寫內容。\n"
    "- 200 字以內，用繁體中文。",
)


# ---------- 參數規格（單一真相：UI 渲染 / 後端驗證 / 預設值都讀這裡） ----------
# group: retrieval = 檢索參數 → **全域**（存在 kb_param 的 collection_id IS NULL，
#                   所有知識庫共用同一組；2026-10-08 定案，改完約 5 秒生效）
#        prompt    = 提示詞   → **全域**（同上；可在參數彈窗 C 區直接編輯）
#        chunk     = 分塊參數 → **按知識庫**（在分塊配置界面單獨設；未分組文件用全域那批）
# rebuild: True 表示改動後需要「重新切塊 + 重跑 embedding」
# 分派規則的唯一實作在 `kb_db._param_target()`（set / load 共用同一套）——
# **只有 chunk 是按知識庫，其餘一律全域**。
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
    "query_rewrite": dict(
        env="RAG_QUERY_REWRITE", label="查詢改寫（三層共用一次）", group="retrieval", type="bool",
        default=False, min=None, max=None, rebuild=False,
        help="FAQ 規則未命中時改寫一次查詢，同一組結果同時給業務表格與知識庫用"
             "（不再讓每層各自改寫）。每次改寫多一次模型呼叫（約 1~3 秒）",
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
    # ── C. 提示詞（全域；可在參數彈窗直接編輯）──
    # type='text'：前端渲染成多行輸入框。required = 少了它就等於壞掉的佔位符，
    # 寫入時 kb_console 會擋下（400）、讀取端 prompt_value() 會退回預設。
    "rag_answer_prompt": dict(
        env="DEEPSEEK_RAG_PROMPT", label="知識庫問答提示詞（system）", group="prompt",
        type="text", default=RAG_PROMPT, min=None, max=None, rebuild=False,
        rows=18, required=["{context}", "{sentinel}"],
        vars=["{context}＝檢索到的資料", "{sentinel}＝答不出來的標記"],
        help="知識庫命中後用來生成答案的 system prompt。強調忠實於召回內容、"
             "設計與數據結構須原樣輸出再解釋。務必保留 {context} 與 {sentinel}",
    ),
    "rewrite_prompt": dict(
        env="DEEPSEEK_RAG_REWRITE_PROMPT", label="查詢改寫提示詞（三層共用）",
        group="prompt", type="text", default=RAG_REWRITE_PROMPT,
        min=None, max=None, rebuild=False,
        rows=16, required=["{question}", "{n}"],
        vars=["{n}＝要幾條", "{history}＝最近對話", "{question}＝客戶這一句",
              "{faq_hint}＝FAQ 參考清單"],
        help="FAQ 未命中時改寫查詢用（一次改寫、三層共用）。"
             "重點是同音錯別字與上下文補齊。務必保留 {question} 與 {n}",
    ),
    "image_caption_prompt": dict(
        env="DEEPSEEK_IMAGE_CAPTION_PROMPT", label="圖片描述提示詞（AI 描述按鈕）",
        group="prompt", type="text", default=DEFAULT_IMAGE_PROMPT,
        min=None, max=None, rebuild=False, rows=14, required=[], vars=[],
        help="按「AI 描述」時用。知識庫的圖**預設由人工寫描述**，這條只在按鈕觸發時生效。"
             "重點是要求模型把圖中文字照抄進描述（等於順手做了圖內文字檢索）",
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
    if t == "text":
        # 提示詞：只去頭尾空白，內部換行與排版原樣保留（那是它的一部分）
        s = str(raw if raw is not None else "").strip()
        return s or str(spec["default"] or "")
    return raw


def missing_placeholders(name: str, text: str) -> list[str]:
    """這條提示詞少了哪些「必需佔位符」（寫入端用來回 400）。沒有 = 可以存。"""
    spec = PARAM_SPECS.get(name) or {}
    body = str(text or "")
    return [p for p in (spec.get("required") or []) if p not in body]


def prompt_value(name: str, raw) -> str:
    """提示詞的**執行時值**：轉型別 + 檢查必需佔位符，缺了就退回內建預設。

    為什麼要這道保險：提示詞是用 `.format()` 填值的，使用者萬一把 `{context}`
    刪掉，就會在生成答案那一刻丟 KeyError、把整個知識庫層打掛。
    寧可讓那次編輯「看似沒生效」，也不能讓它變成線上事故。
    （寫入端 `kb_console` 已經會先擋一次，這裡是第二道 —— DB 被手改也擋得住。）
    """
    if name not in PARAM_SPECS:
        return str(raw or "")
    spec = PARAM_SPECS[name]
    s = coerce(name, raw)
    if missing_placeholders(name, s):
        return str(spec.get("default") or "")
    return s


def defaults() -> dict:
    """全部參數的預設值（解析後型別）。"""
    return {name: coerce(name, spec["default"]) for name, spec in PARAM_SPECS.items()}
