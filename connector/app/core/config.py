"""Connector 服務配置 —— 只認渠道憑證與媒體目錄，不含任何 AI / 知識庫設定。

從舊專案 `app/core/config.py` 拆出的「渠道側」部分。憑證只存在這裡（設計案 §3.1）。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 固定載入本服務的 .env（connector/.env），不受啟動時 cwd 影響
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# 服務根目錄（connector/）—— 相對路徑一律以它為基準，避免受啟動 cwd 影響
_ROOT = Path(__file__).resolve().parents[2]


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _path(key: str, default: str) -> str:
    """路徑型設定：相對路徑一律解析成 <connector 根>/<value>（並正規化）。

    ★ 用 `.resolve()` 正規化：`MEDIA_DIR=../media`（指向 monorepo 根的 media/，
    那裡放著舊單體寫入、還沒搬過來的客戶來訊媒體）會被正規化成絕對路徑，
    免得 `connector/../media` 這種帶 `..` 的字串在別處（StaticFiles 檢查）出怪。
    """
    v = _get(key, default) or default
    p = Path(v)
    return str(p if p.is_absolute() else (_ROOT / p).resolve())


SERVICE_NAME = "connector"
PORT = int(_get("PORT", "8010") or 8010)

# ===== 資料庫（Connector DB）=====
DATABASE_URL = _get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/platform_forwarder")

# ===== 內網互通 =====
# CRM 內網基址（Connector 呼叫 CRM：ai/inbound、claim、update-status、ops/config）
CRM_INTERNAL_BASE = _get("CRM_INTERNAL_BASE", "http://127.0.0.1:8020")
INTERNAL_API_TOKEN = _get("INTERNAL_API_TOKEN")
INTERNAL_BIND_HOST = _get("INTERNAL_BIND_HOST", "127.0.0.1")

# ===== 媒體 =====
MEDIA_DIR = _path("MEDIA_DIR", "media")
KB_CACHE_DIR = _path("KB_CACHE_DIR", "media/kb_cache")
KB_CACHE_TTL_DAYS = int(_get("KB_CACHE_TTL_DAYS", "7") or 7)
# 對外可訪問基址（ngrok / 正式域名），拼媒體 URL 用
PUBLIC_BASE_URL = _get("PUBLIC_BASE_URL", "")

# ===== 出站 worker =====
# live = 真的呼叫平台 API；mock = 不發網路請求（自檢/演示用）
OUTBOUND_SEND_MODE = (_get("OUTBOUND_SEND_MODE", "live") or "live").lower()
WORKER_ENABLED = _get("WORKER_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# 帳號映射定時拉取兜底（§1.3 防線②）
ACCOUNT_MAP_SYNC_SECONDS = float(_get("ACCOUNT_MAP_SYNC_SECONDS", "60") or 60)
# 超過這個秒數沒成功同步 → 上報 account_map_stale 告警（§1.3 防線③）
ACCOUNT_MAP_STALE_SECONDS = float(_get("ACCOUNT_MAP_STALE_SECONDS", "300") or 300)

# ===== 任務派發（不用 Redis；輪詢 CRM claim API，靠 PG 行鎖保證不重派）=====
POLL_SECONDS = float(_get("POLL_SECONDS", "1") or 1)
REDIS_URL = _get("REDIS_URL", "")  # 保留但留空；未來才用

# ===== 微信 =====
WECHAT_TOKEN = _get("WECHAT_TOKEN")
WECHAT_APPID = _get("WECHAT_APPID")
WECHAT_APPSECRET = _get("WECHAT_APPSECRET")
WECHAT_ENCRYPT_MODE = _get("WECHAT_ENCRYPT_MODE", "plain")
WECHAT_ENCODING_AES_KEY = _get("WECHAT_ENCODING_AES_KEY")

# ===== WhatsApp（Meta Cloud API）=====
WHATSAPP_VERIFY_TOKEN = _get("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_ACCESS_TOKEN = _get("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = _get("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_CHANNEL = _get("WHATSAPP_CHANNEL", "epro-web")
WHATSAPP_CHANNEL_IDENTITY_ID = _get("WHATSAPP_CHANNEL_IDENTITY_ID")

# ===== Facebook / Instagram =====
GRAPH_API_VERSION = _get("GRAPH_API_VERSION", "v22.0")
META_APP_ID = _get("META_APP_ID")
META_VERIFY_TOKEN = _get("META_VERIFY_TOKEN")
META_APP_SECRET = _get("META_APP_SECRET")
META_VERIFY_SIGNATURE = _get("META_VERIFY_SIGNATURE", "false").lower() in ("1", "true", "yes", "on")
META_GRAPH_VERSION = _get("META_GRAPH_VERSION", "v21.0")
META_IG_GRAPH_HOST = _get("META_IG_GRAPH_HOST")
META_PAGE_ID = _get("META_PAGE_ID")
META_USER_ACCESS_TOKEN = _get("META_USER_ACCESS_TOKEN")
META_PAGE_ACCESS_TOKEN = _get("META_PAGE_ACCESS_TOKEN")
META_IG_USER_ID = _get("META_IG_USER_ID")
META_IG_ACCESS_TOKEN = _get("META_IG_ACCESS_TOKEN")

# ===== 渠道標識 =====
CHANNEL = _get("CHANNEL", "epro-web")
CHANNEL_IDENTITY_ID = _get("CHANNEL_IDENTITY_ID")


def ig_graph_host() -> str:
    """IG 私訊主機：有設 META_IG_GRAPH_HOST 用設定值，否則依 token 前綴判斷。"""
    if META_IG_GRAPH_HOST:
        return META_IG_GRAPH_HOST.rstrip("/")
    if META_IG_ACCESS_TOKEN.startswith("IGAA"):
        return "https://graph.instagram.com"
    return "https://graph.facebook.com"
