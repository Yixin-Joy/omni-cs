"""CRM 服務配置 —— AI 引擎 / RAG / 業務資料側。

渠道憑證**不在這裡**（在 Connector）。從舊專案 `app/core/config.py` 拆出的「大腦側」部分。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 固定載入本服務的 .env（crm/.env），不受啟動時 cwd 影響
load_dotenv(Path(__file__).resolve().parents[2] / ".env")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


SERVICE_NAME = "crm"
PORT = int(_get("PORT", "8020") or 8020)

# ===== 資料庫（CRM DB）=====
DATABASE_URL = _get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/crm_db")

# ===== 內網互通 =====
CONNECTOR_INTERNAL_BASE = _get("CONNECTOR_INTERNAL_BASE", "http://127.0.0.1:8010")
INTERNAL_API_TOKEN = _get("INTERNAL_API_TOKEN")
INTERNAL_BIND_HOST = _get("INTERNAL_BIND_HOST", "127.0.0.1")

# ===== Session / 登入安全（設計案 §13.5）=====
SESSION_TTL_HOURS = int(_get("SESSION_TTL_HOURS", "8") or 8)
SESSION_COOKIE_NAME = _get("SESSION_COOKIE_NAME", "mcrm_sid")
LOGIN_MAX_FAIL = int(_get("LOGIN_MAX_FAIL", "5") or 5)
LOGIN_LOCK_MINUTES = int(_get("LOGIN_LOCK_MINUTES", "15") or 15)

# ===== 媒體（知識庫圖片公開目錄；rag_config 也讀這組）=====
RAG_MEDIA_DIR = _get("RAG_MEDIA_DIR", "media")
RAG_MEDIA_URL_PREFIX = _get("RAG_MEDIA_URL_PREFIX", "/media")

# 知識庫抽圖／來源文件的落地根目錄（舊專案概念）。
# `kb_asset.rel_path` 形如 `kb/<uuid>/assets/x.png`，autoreply._send_assets 會 join 這個前綴找檔。
MEDIA_DIR = _get("MEDIA_DIR", "media")

# 對外可訪問的 https 基址（ngrok 隧道，去掉結尾斜線）。
# ⚠️ IG / FB 的圖片必須是「外網可達的絕對網址」—— Meta 會自己來下載；
#    沒設這個，IG/FB 的附圖就發不出去（見 autoreply.public_asset_url）。
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL", "")

# ===== AI 供應商（DeepSeek）=====
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_TIMEOUT = float(_get("DEEPSEEK_TIMEOUT", "30") or 30)
DEEPSEEK_AUX_TIMEOUT = float(_get("DEEPSEEK_AUX_TIMEOUT", "5") or 5)
DEEPSEEK_MAX_CONCURRENCY = int(_get("DEEPSEEK_MAX_CONCURRENCY", "2") or 2)
DEEPSEEK_VISION_TIMEOUT = float(_get("DEEPSEEK_VISION_TIMEOUT", "90") or 90)

FAQ_MATCH_THRESHOLD = float(_get("FAQ_MATCH_THRESHOLD", "0.45") or 0.45)

# ===== 系統提示詞（從舊專案 app/core/config.py 原樣帶過來）=====
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

TABLE_CONTEXT_MAX_ROWS = int(_get("TABLE_CONTEXT_MAX_ROWS", "200") or 0)
TABLE_CONTEXT_MAX_CHARS = int(_get("TABLE_CONTEXT_MAX_CHARS", "12000") or 0)
TABLE_REWRITE_HISTORY_TURNS = int(_get("TABLE_REWRITE_HISTORY_TURNS", "5") or 0)

AUTOREPLY_COOLDOWN_SECONDS = int(_get("AUTOREPLY_COOLDOWN_SECONDS", "30") or 0)
AUTOREPLY_MAX_PER_DAY = int(_get("AUTOREPLY_MAX_PER_DAY", "30") or 30)

# ===== 背景 worker =====
WORKER_ENABLED = _get("WORKER_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AI_WORKER_POLL_SECONDS = float(_get("AI_WORKER_POLL_SECONDS", "1") or 1)
TIMEOUT_SWEEP_SECONDS = float(_get("TIMEOUT_SWEEP_SECONDS", "60") or 60)

# ===== Redis（第一版不裝：任務派發靠 Connector 輪詢 claim API，PG 行鎖保證不重派）=====
REDIS_URL = _get("REDIS_URL", "")

# ===== 自動回復支援的渠道與場景（沿用舊系統語義）=====
AUTOREPLY_DM_CHANNELS = ("wechat", "whatsapp", "ig", "fb")
AUTOREPLY_COMMENT_CHANNELS = ("ig", "fb")
AUTOREPLY_SCOPES = (
    tuple((c, "dm") for c in AUTOREPLY_DM_CHANNELS)
    + tuple((c, "comment") for c in AUTOREPLY_COMMENT_CHANNELS)
)

CHANNEL_LABELS = {"wechat": "微信", "whatsapp": "WhatsApp", "ig": "Instagram", "fb": "Facebook"}
SCOPE_LABELS = {"dm": "私訊", "comment": "貼文留言"}


def scope_label(channel: str, scope: str) -> str:
    return f"{CHANNEL_LABELS.get(channel, channel)} {SCOPE_LABELS.get(scope, scope)}"
