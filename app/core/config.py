"""集中管理配置，全部從環境變數讀取。範例見 .env.example。"""
import os

from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# 微信伺服器配置（後台「伺服器配置」頁填的 Token）
WECHAT_TOKEN = _get("WECHAT_TOKEN")

# 微信開發者 ID（下載語音/視頻 media 用；個人號權限需實測）
WECHAT_APPID = _get("WECHAT_APPID")
WECHAT_APPSECRET = _get("WECHAT_APPSECRET")

# 消息加解密方式：plain / compatible / safe（三種都支援，安全/兼容模式需填 EncodingAESKey）
WECHAT_ENCRYPT_MODE = _get("WECHAT_ENCRYPT_MODE", "plain")

# 安全/兼容模式的加密金鑰（後台「伺服器配置」頁點隨機生成的 43 字元）
WECHAT_ENCODING_AES_KEY = _get("WECHAT_ENCODING_AES_KEY")

# 註：本服務已不再轉發任何外部系統（原 TARGET_WEBHOOK_URL 指向的 9033 已切斷），
#     微信 / WhatsApp 來訊一律只落地本機 DB，由客服工作台讀取。

# 寫進來訊 payload 的渠道標識（純識別用，不再外送）
CHANNEL = _get("CHANNEL", "epro-web")
CHANNEL_IDENTITY_ID = _get("CHANNEL_IDENTITY_ID", "62ea24d2-adcb-48a2-a013-9b2a10d2ebcf")

# 本服務對外可訪問的 https 基址（ngrok 產生的位址，去掉結尾斜線）
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL", "")

# 媒體檔落地目錄
MEDIA_DIR = _get("MEDIA_DIR", "media")

# ===== WhatsApp（Meta Cloud API）=====
# webhook 驗證用 token（Meta 後台填的 verify token）
WHATSAPP_VERIFY_TOKEN = _get("WHATSAPP_VERIFY_TOKEN")

# 下載 WhatsApp 媒體用（Meta App 的 access token）
WHATSAPP_ACCESS_TOKEN = _get("WHATSAPP_ACCESS_TOKEN")

# 發送 WhatsApp 消息用（Meta 後台 WhatsApp > API Setup 的 Phone Number ID）
WHATSAPP_PHONE_NUMBER_ID = _get("WHATSAPP_PHONE_NUMBER_ID")

# WhatsApp 渠道標識（純識別用，不再外送）
WHATSAPP_CHANNEL = _get("WHATSAPP_CHANNEL", "epro-web")
WHATSAPP_CHANNEL_IDENTITY_ID = _get("WHATSAPP_CHANNEL_IDENTITY_ID", "62ea24d2-adcb-48a2-a013-9b2a10d2ebcf")

# Meta Graph API 版本
GRAPH_API_VERSION = _get("GRAPH_API_VERSION", "v22.0")

# ===== Facebook / Instagram（私訊 + 貼文留言）=====
# App 編號（App 後台 → 設定 → 基本資料）；延長/衍生權杖時需要
META_APP_ID = _get("META_APP_ID")

# webhook 驗證 token（Meta 後台自行設定的任意字串）
META_VERIFY_TOKEN = _get("META_VERIFY_TOKEN")

# (選用) 驗證 webhook 簽章（X-Hub-Signature-256）用的 App Secret
META_APP_SECRET = _get("META_APP_SECRET")

# 是否強制驗證 webhook 簽章（true 才驗，預設關閉，跟 WhatsApp 現況一致）
META_VERIFY_SIGNATURE = _get("META_VERIFY_SIGNATURE", "false").lower() in ("1", "true", "yes", "on")

# Graph API 版本（FB / IG 共用）
META_GRAPH_VERSION = _get("META_GRAPH_VERSION", "v21.0")

# Instagram 發送主機：留空 = 依 token 前綴自動判斷
#   IGAA… → graph.instagram.com（Instagram 登入路徑）
#   EAA…  → graph.facebook.com（Facebook 登入路徑）
META_IG_GRAPH_HOST = _get("META_IG_GRAPH_HOST")

# Facebook 粉絲專頁 ID（FB 私訊發送、貼文/留言的 owner）
META_PAGE_ID = _get("META_PAGE_ID")

# 使用者權杖（type=USER）：用來衍生 / 延長粉專權杖，程式本身不使用
META_USER_ACCESS_TOKEN = _get("META_USER_ACCESS_TOKEN")

# 粉專權杖（FB 私訊 + FB 貼文/留言 + IG 貼文/留言，走 graph.facebook.com）
META_PAGE_ACCESS_TOKEN = _get("META_PAGE_ACCESS_TOKEN")

# Instagram 專業帳號 ID（FB 登入路徑看到的 ID，用於讀取/回覆 IG 貼文留言、辨識「我方」）
META_IG_USER_ID = _get("META_IG_USER_ID")

# Instagram 登入路徑權杖（IGAA…，走 graph.instagram.com，用於 IG 私訊）
META_IG_ACCESS_TOKEN = _get("META_IG_ACCESS_TOKEN")


def ig_graph_host() -> str:
    """IG 私訊主機：有設 META_IG_GRAPH_HOST 用設定值，否則依 token 前綴判斷。"""
    if META_IG_GRAPH_HOST:
        return META_IG_GRAPH_HOST.rstrip("/")
    if META_IG_ACCESS_TOKEN.startswith("IGAA"):
        return "https://graph.instagram.com"
    return "https://graph.facebook.com"


# 轉發日誌資料庫
DATABASE_URL = _get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/platform_forwarder")


# ===== AI 自動回復（DeepSeek）=====
# 留空則自動回復的第二層（業務表格）與第三層（知識庫）會停用，直接降級人工。
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")

DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# 模型 ID（本專案預設 deepseek-flash）
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-flash")

# 單次呼叫逾時（秒）—— 只給「必要呼叫」（生成答案）用
DEEPSEEK_TIMEOUT = float(_get("DEEPSEEK_TIMEOUT", "30") or 30)

# 輔助呼叫（改寫問句）的逾時（秒）：短、且不重試。
# ⚠️ 實測：用 30 秒超時 + 自動重試，一次 ReadTimeout 就吃掉 60 秒，把整則回覆拖垮。
#    改寫失敗沿用原問句即可，不值得為它等那麼久。
DEEPSEEK_AUX_TIMEOUT = float(_get("DEEPSEEK_AUX_TIMEOUT", "5") or 5)

# 同時送出的模型呼叫上限（本地排隊，避免撞上閘道的 max_parallel_requests 而被 429）
DEEPSEEK_MAX_CONCURRENCY = int(_get("DEEPSEEK_MAX_CONCURRENCY", "2") or 2)

# 視覺呼叫（看圖生描述）的逾時（秒）。比一般呼叫給得多 ——
# reasoning 模型 + 圖片 base64 本來就慢（實測一張 16KB 圖約 5~15 秒）。
DEEPSEEK_VISION_TIMEOUT = float(_get("DEEPSEEK_VISION_TIMEOUT", "90") or 90)

# FAQ 規則匹配門檻（0~1，越高越嚴格）
FAQ_MATCH_THRESHOLD = float(_get("FAQ_MATCH_THRESHOLD", "0.45") or 0.45)

# 餵給模型的業務表格內容上限（列數 / 字元數；0 = 不限制）
TABLE_CONTEXT_MAX_ROWS = int(_get("TABLE_CONTEXT_MAX_ROWS", "200") or 0)
TABLE_CONTEXT_MAX_CHARS = int(_get("TABLE_CONTEXT_MAX_CHARS", "12000") or 0)

# 業務表格第一次沒查到的「query 改寫」：
#   帶入該會話最近 N 則訊息（客戶與我方交替）讓模型把追問 / 指代不明的問句
#   改寫成可獨立理解的問句，再查一次表格；查不到才降級人工。
TABLE_REWRITE_HISTORY_TURNS = int(_get("TABLE_REWRITE_HISTORY_TURNS", "5") or 0)

# 自動回復防護：同一會話的冷卻秒數、每日每會話上限
AUTOREPLY_COOLDOWN_SECONDS = int(_get("AUTOREPLY_COOLDOWN_SECONDS", "30") or 0)
AUTOREPLY_MAX_PER_DAY = int(_get("AUTOREPLY_MAX_PER_DAY", "30") or 0)

# 系統提示詞
DEEPSEEK_SYSTEM_PROMPT = _get(
    "DEEPSEEK_SYSTEM_PROMPT",
    "你是一位專業、友善的客服助手，請用簡潔的繁體中文回答使用者的問題。",
)

# 貼文留言是公開場合：疊加這組規則
DEEPSEEK_COMMENT_PROMPT = _get(
    "DEEPSEEK_COMMENT_PROMPT",
    "你正在回覆「公開」的社群貼文留言，所有人都看得到。請遵守：\n"
    "1. 長度 1~3 句，像真人小編在回話，不要寫成客服公告。\n"
    "2. 不要透露訂單、電話、地址、Email 等個人資料，也不要在公開留言裡報價細節。\n"
    "3. 涉及價格、庫存、下單等需要一對一處理的問題，請簡短回應並引導對方「私訊我們」。\n"
    "4. 表情符號最多一個。\n"
    "5. 不要提及自己是 AI 或語言模型。",
)

# 業務表格回答提示詞（{context} 放表格內容，{sentinel} 放答不出來的標記）
DEEPSEEK_TABLE_PROMPT = _get(
    "DEEPSEEK_TABLE_PROMPT",
    "以下是我們的業務資料表內容（格式為「表名 / 欄位 / 資料列」，每列以「欄位名：值」對齊標示）：\n"
    "─────\n{context}\n─────\n\n"
    "請「只根據上述表格內容」回答使用者的問題，回答要具體、直接引用表格中的資訊。\n"
    "若某欄位名稱不明確，請根據該表說明與欄位值自行判斷其代表什麼，再據以回答。\n"
    "涉及數據的輸出請組織成通順的語句，不要帶 '*'、'-' 這類分隔符號。\n"
    "問什麼答什麼，問價格就只回答價格，嚴禁給出使用者沒問到的額外資訊,但是仍然需要用完整的語句禮貌回答，不得只回答數字、產品名\n"
    "═══ 最高優先規則（覆蓋以上所有敘述）═══\n"
    "若表格中完全沒有可以回答此問題的資訊，你唯一允許的輸出就是這串字元：{sentinel}\n"
    "不可用「沒有」「查不到」「抱歉」等自然語言代替，也不要加上任何說明、標點或多餘文字。\n"
    "寧可只輸出這個標記，也不要編造或補充表格以外的資訊。",
)

# ⚠️ 「查詢改寫」的提示詞**不在這裡** ——
#    2026-10-08 起三層（FAQ／業務表格／知識庫）共用**同一條**改寫提示詞，
#    它是可在頁面編輯的參數 `rewrite_prompt`（`rag_config.PARAM_SPECS`，
#    工作台「知識庫 → 參數配置 → C. 提示詞」）。原本的 FAQ 專用／表格專用
#    提示詞與 `FAQ_REWRITE_MODE` 開關都已移除，避免「關了還在呼叫模型」。


# ===== 自動回復支援的渠道與場景 =====

# 私訊類場景
AUTOREPLY_DM_CHANNELS = ("whatsapp", "ig", "fb")

# 貼文留言類場景
AUTOREPLY_COMMENT_CHANNELS = ("ig", "fb")

# 全部可設定開關的 (渠道, 場景)
AUTOREPLY_SCOPES = (
    tuple((c, "dm") for c in AUTOREPLY_DM_CHANNELS)
    + tuple((c, "comment") for c in AUTOREPLY_COMMENT_CHANNELS)
)

CHANNEL_LABELS = {
    "wechat": "微信",
    "whatsapp": "WhatsApp",
    "ig": "Instagram",
    "fb": "Facebook",
}

SCOPE_LABELS = {"dm": "私訊", "comment": "貼文留言"}


def scope_label(channel: str, scope: str) -> str:
    """「WhatsApp 私訊」這類可讀標籤，用於日誌與 UI。"""
    return f"{CHANNEL_LABELS.get(channel, channel)} {SCOPE_LABELS.get(scope, scope)}"
