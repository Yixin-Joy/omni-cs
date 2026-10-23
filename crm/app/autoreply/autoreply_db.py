"""AI 自動回復的資料層（★ CRM 版）：FAQ / 業務表格 / 開關 / 日誌讀寫。

資料庫固定為 `crm_db`（見 crm/.env 的 DATABASE_URL），不碰 Connector 庫。

與舊版（platform_forwarder）的差異：
  * 表由 `app.core.schema`（crm_db 單一真相）建立，這裡**不再自己建表**。
  * `autoreply_setting` **廢棄** → 開關改讀 `business_ai_switch`（按商家 + 平台 + 場景）。
  * `autoreply_faq` / `autoreply_business_table` 多了 `business_id` → 相關函式都帶上。
  * `autoreply_log` 欄位換成 CRM 版（`platform`/`business_id`/`outbound_task_id`…），
    不再有 `channel`/`reply_scope`（改用 `platform` + `scope`）。
  * 訊息表（`forward_log` / `igfb_dm` / `igfb_comment`）在 **Connector** 庫 →
    一律走內網 API（`app.internal.connector_client`），不再直連。
"""
import asyncio
import json
import logging
from urllib.parse import urlsplit, urlunsplit

import psycopg2

from app.core import config

log = logging.getLogger("crm")

# ---------- （已作廢）舊 DDL ----------
# CRM 側的表一律由 `app.core.schema` 建立（crm_db 單一真相）。
# 以下舊 DDL 僅留存供比對，**不執行** —— 執行它會在 crm_db 建出欄位不對的表。

_LEGACY_DDL_UNUSED = """
CREATE TABLE IF NOT EXISTS autoreply_faq (
    id          BIGSERIAL PRIMARY KEY,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    keywords    TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    hit_count   BIGINT NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_business_table (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    columns     JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_business_row (
    id          BIGSERIAL PRIMARY KEY,
    table_id    BIGINT NOT NULL REFERENCES autoreply_business_table(id) ON DELETE CASCADE,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_setting (
    scope_key   TEXT PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS autoreply_log (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    channel        VARCHAR(20) NOT NULL,
    reply_scope    VARCHAR(20) NOT NULL,
    tier           VARCHAR(20) NOT NULL,
    detail         TEXT,
    reason         TEXT,
    question       TEXT,
    answer         TEXT,
    conversation_key TEXT,
    message_id     TEXT,
    post_id        TEXT,
    comment_id     TEXT,
    faq_id         BIGINT,
    table_id       BIGINT,
    matched_rows   JSONB,
    score          DOUBLE PRECISION,
    rewrote        BOOLEAN NOT NULL DEFAULT false,
    rewritten_query TEXT,
    history_used   INTEGER NOT NULL DEFAULT 0,
    send_status    VARCHAR(20) NOT NULL DEFAULT 'not_sent',
    -- 這次決策的用量（為什麼要一分鐘？看這兩欄就知道：模型呼叫幾次、各花多久）
    elapsed_ms     INTEGER NOT NULL DEFAULT 0,
    llm_calls      INTEGER NOT NULL DEFAULT 0,
    llm_seconds    DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- 一則訊息只處理一次的認領鍵（見下方唯一索引）
    claim_key      TEXT
);

CREATE INDEX IF NOT EXISTS idx_autoreply_log_created ON autoreply_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_channel ON autoreply_log(channel, reply_scope);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_tier ON autoreply_log(tier);
CREATE INDEX IF NOT EXISTS idx_autoreply_log_rewrote ON autoreply_log(rewrote);
CREATE INDEX IF NOT EXISTS idx_autoreply_business_row_table ON autoreply_business_row(table_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_autoreply_faq_enabled ON autoreply_faq(enabled, sort_order);

-- 「一則訊息只處理一次」的原子認領鍵。
-- Meta / 微信 都可能對同一個事件重複投遞，加上聊天模式會併發處理，
-- 用唯一索引 + INSERT ... ON CONFLICT (claim_key) DO NOTHING 保證只有一個處理者能認領成功。
-- 註：必須是「非部分」唯一索引，ON CONFLICT (claim_key) 才認得；
-- PostgreSQL 的唯一索引允許多個 NULL，所以未認領的列不受影響。
CREATE UNIQUE INDEX IF NOT EXISTS idx_autoreply_log_claim
    ON autoreply_log(claim_key);
"""

# 既有表的自動回復欄位（逐條 ALTER）—— ★ CRM 版作廢：
#   這些表（forward_log / igfb_dm / igfb_comment）在 Connector 庫，不歸 CRM 管。
_LEGACY_COLUMN_MIGRATIONS_UNUSED = (
    ("forward_log", "auto_reply_state", "VARCHAR(20)"),
    ("forward_log", "auto_reply_tier", "VARCHAR(20)"),
    ("forward_log", "auto_reply_detail", "TEXT"),
    ("igfb_dm", "auto_reply_state", "VARCHAR(20)"),
    ("igfb_dm", "auto_reply_tier", "VARCHAR(20)"),
    ("igfb_dm", "auto_reply_detail", "TEXT"),
    # 用量欄位（2026-10-08）：列表／詳情都要顯示，所以用 ALTER 補進既有表
    ("autoreply_log", "elapsed_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("autoreply_log", "llm_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("autoreply_log", "llm_seconds", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("igfb_comment", "auto_reply_state", "VARCHAR(20)"),
    ("igfb_comment", "auto_reply_tier", "VARCHAR(20)"),
    ("igfb_comment", "auto_reply_detail", "TEXT"),
    # 業務表格 query 改寫的留痕
    ("autoreply_log", "rewritten_query", "TEXT"),
    ("autoreply_log", "history_used", "INTEGER NOT NULL DEFAULT 0"),
    ("autoreply_log", "claim_key", "TEXT"),
)

# 自動回復狀態值
STATE_MISS = "miss"          # 自動回復未命中 → 待人工處理
STATE_HIT = "hit"            # 自動回復已送出
STATE_FAILED = "failed"      # 有命中但發送失敗
STATE_SKIPPED = "skipped"    # 未觸發（開關關閉 / 非文字訊息 / 冷卻中）

# 層級值
TIER_FAQ = "faq"
TIER_TABLE = "table"
TIER_RAG = "rag"
TIER_MISS = "miss"
TIER_ERROR = "error"
# 處理中的佔位層級：認領列在被後續 update_claim 改寫前用它，不會出現在紀錄列表
TIER_CLAIM = "claim"

TIER_LABELS = {TIER_FAQ: "FAQ 命中", TIER_TABLE: "表格命中", TIER_RAG: "知識庫命中"}

# 紀錄頁 / 統計要認得的層級（其他值一律歸到 error）
KNOWN_TIERS = (TIER_FAQ, TIER_TABLE, TIER_RAG, TIER_MISS, TIER_ERROR)


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier or "", tier or "")


# ---------- 連線 ----------

def _connect(dbname: str | None = None):
    url = config.DATABASE_URL
    if dbname:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, f"/{dbname}", parts.query, parts.fragment))
    return psycopg2.connect(url)


def _current_dbname() -> str:
    return urlsplit(config.DATABASE_URL).path.lstrip("/") or "platform_forwarder"


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            # 沒有參數時不要讓 psycopg2 介入：否則 SQL 裡的 % 會被當成佔位符
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> int:
    """執行 INSERT/UPDATE/DELETE，回傳受影響列數。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def execute_returning(sql: str, params: tuple = ()):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            row = cur.fetchone()
        conn.commit()
        return row
    finally:
        conn.close()


def executemany(sql: str, seq: list[tuple]) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, seq)
        conn.commit()
    finally:
        conn.close()


# ---------- 建表 / 遷移 ----------

def init_schema() -> None:
    """★ CRM 版：表統一由 `app.core.schema` 建立，這裡只保留同名 API 供舊呼叫端相容。"""
    from app.core import db as _crm_db
    _crm_db.init_schema_sync()


async def init_schema_async() -> None:
    """非同步包裝（服務啟動時用），失敗只記 log 不擋啟動。"""
    try:
        await asyncio.to_thread(init_schema)
        log.info("自動回復資料表就緒（%s）", _current_dbname())
    except Exception as e:
        log.warning("自動回復資料表初始化失敗: %s", e)


# ---------- 開關（business_ai_switch / business_layer_switch） ----------
#
# ★ 舊版 `autoreply_setting`（scope_key = channel / channel:scope，外加 global）已廢棄：
#   CRM 側所有開關都**按商家**存，不再有單一全域開關。

def scope_key(channel: str, scope: str) -> str:
    """可讀的 (渠道, 場景) 標籤（日誌 / UI 用）。"""
    return f"{channel}:{scope}"


def effective_scope_mode(business_id: int, platform: str, scope: str) -> bool:
    """某商家 + 平台 + 場景是否開啟自動回復（`business_ai_switch`；無此列 = 關）。"""
    if not business_id:
        return False
    rows = query(
        "SELECT enabled FROM business_ai_switch"
        " WHERE business_id=%s AND platform=%s AND scope=%s",
        (int(business_id), platform, scope),
    )
    return bool(rows[0]["enabled"]) if rows else False


def set_scope_mode(business_id: int, platform: str, scope: str, enabled: bool) -> None:
    execute("""
        INSERT INTO business_ai_switch (business_id, platform, scope, enabled, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (business_id, platform, scope)
        DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=now()
    """, (int(business_id), platform, scope, bool(enabled)))


def all_scope_modes(business_id: int) -> dict[str, bool]:
    """該商家所有已設定的「平台:場景」開關。"""
    rows = query("SELECT platform, scope, enabled FROM business_ai_switch"
                 " WHERE business_id=%s", (int(business_id),))
    return {f"{r['platform']}:{r['scope']}": bool(r["enabled"]) for r in rows}


def backfill_ai_switches() -> int:
    """給「建商家的年代還沒有 seed」的舊商家補齊 6 把開關列（全開）。

    ★ 只補**缺的列**（`ON CONFLICT DO NOTHING`）—— 手動關掉的那把不會被蓋回來，
      所以這一步可以每次啟動都跑，且與「預設值」無關（就是補成實際可用的狀態）。
    ★ 為什麼要補：管理員端的舊版是「讀不到就當 true」，客服端是「讀不到就當 false」，
      同一個商家兩邊顯示相反。補成實列後兩端讀同一份資料。
    """
    total = 0
    for platform, scope in config.AUTOREPLY_SCOPES:
        total += execute(
            "INSERT INTO business_ai_switch (business_id, platform, scope, enabled)"
            " SELECT b.id, %s, %s, true FROM crm_business b"
            " ON CONFLICT (business_id, platform, scope) DO NOTHING",
            (platform, scope),
        )
    return total


async def backfill_ai_switches_async() -> None:
    """非同步包裝（服務啟動時用），失敗只記 log 不擋啟動。"""
    try:
        n = await asyncio.to_thread(backfill_ai_switches)
        if n:
            log.info("舊商家 AI 開關補齊 %s 把（缺列補成開啟）", n)
    except Exception as e:  # noqa: BLE001
        log.warning("舊商家 AI 開關補齊失敗: %s", e)


# ---- 層級開關（FAQ / 業務表格 / RAG；無此列 = 啟用，沿用舊預設） ----

def layer_enabled(business_id: int, layer: str) -> bool:
    if not business_id:
        return True
    rows = query("SELECT enabled FROM business_layer_switch"
                 " WHERE business_id=%s AND layer=%s", (int(business_id), layer))
    return bool(rows[0]["enabled"]) if rows else True


def set_layer_enabled(business_id: int, layer: str, enabled: bool) -> None:
    execute("""
        INSERT INTO business_layer_switch (business_id, layer, enabled, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (business_id, layer)
        DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=now()
    """, (int(business_id), layer, bool(enabled)))


def all_layer_switches(business_id: int) -> dict[str, bool]:
    rows = query("SELECT layer, enabled FROM business_layer_switch"
                 " WHERE business_id=%s", (int(business_id),))
    out = {r["layer"]: bool(r["enabled"]) for r in rows}
    for lay in ("faq", "table", "rag"):     # 未設定的層預設啟用
        out.setdefault(lay, True)
    return out


# ---------- 平台帳號層開關（第二層；設計案 §9.1） ----------

def platform_account_enabled(bp_account_id: int) -> bool:
    """`crm_business_platform_account.ai_auto_reply_enabled` —— 兩層 AND 的第二層。

    ⚠️ 查不到帳號 → **False**。寧可不回，也不要對一個已經解綁／停用的帳號發訊息。
    （`enabled=false` 的帳號連通道都不該收訊，一併擋掉。）
    """
    if not bp_account_id:
        return False
    rows = query("SELECT ai_auto_reply_enabled AS ai_on, enabled"
                 " FROM crm_business_platform_account WHERE id=%s", (int(bp_account_id),))
    if not rows:
        return False
    return bool(rows[0]["ai_on"]) and bool(rows[0]["enabled"])


def set_platform_account_ai(bp_account_id: int, enabled: bool) -> bool:
    return execute("UPDATE crm_business_platform_account SET ai_auto_reply_enabled=%s"
                   " WHERE id=%s", (bool(enabled), int(bp_account_id))) > 0


# ---------- 基準啟用集（商家層）與貼文覆寫集（設計案 §9.2 / §9.3） ----------
#
# 知識庫 / 業務表格 / FAQ 三組**互相獨立**的啟用清單，各配一個層開關。
# ★ **空集 = 不跑**，不是「沒勾 = 全部」—— 一個都沒勾還照跑，等於用使用者
#   以為已經停用的資料回答，這比不回覆更糟。
# ★ 上傳即進基準集（§9.4）：`create_faq` / `create_table` / `kb_db.create_collection`
#   都會自動補一列 `enabled=true`，所以正常流程不會莫名出現空集。

BINDING_SPECS = {
    "faq":   ("business_faq_binding", "faq_id"),
    "table": ("business_table_binding", "business_table_id"),
    "kb":    ("business_kb_binding", "kb_collection_id"),
}
POST_BINDING_SPECS = {
    "faq":   ("post_faq_binding", "faq_id"),
    "table": ("post_table_binding", "business_table_id"),
    "kb":    ("post_kb_binding", "kb_collection_id"),
}


def list_binding_ids(business_id: int, kind: str) -> list[int]:
    """該商家某一層的基準啟用集（id 清單）。沒勾任何一項 = 空清單。"""
    table, col = BINDING_SPECS[kind]
    rows = query(f"SELECT {col} AS id FROM {table}"
                 " WHERE business_id=%s AND enabled ORDER BY id", (int(business_id),))
    return [int(r["id"]) for r in rows]


def set_binding(business_id: int, kind: str, item_id: int, enabled: bool) -> None:
    table, col = BINDING_SPECS[kind]
    execute(f"INSERT INTO {table} (business_id, {col}, enabled) VALUES (%s, %s, %s)"
            f" ON CONFLICT (business_id, {col}) DO UPDATE SET enabled=EXCLUDED.enabled",
            (int(business_id), int(item_id), bool(enabled)))


def add_binding(business_id: int, kind: str, item_id: int) -> None:
    """上傳即進基準集（§9.4）。已存在就**不動** —— 不覆蓋人工的取消勾選。"""
    table, col = BINDING_SPECS[kind]
    execute(f"INSERT INTO {table} (business_id, {col}, enabled) VALUES (%s, %s, true)"
            f" ON CONFLICT (business_id, {col}) DO NOTHING", (int(business_id), int(item_id)))


def binding_counts(business_id: int) -> dict:
    """三層各有幾個啟用項（UI 顯示「這一層還沒綁東西」用）。"""
    return {k: len(list_binding_ids(business_id, k)) for k in BINDING_SPECS}


def business_scope_set(business_id: int) -> dict:
    """商家基準啟用集 + 商家層開關。私訊與「未配置過的貼文」都用這一組。"""
    out = {k: list_binding_ids(business_id, k) for k in BINDING_SPECS}
    out["layers"] = dict(all_layer_switches(business_id))   # {'faq','table','rag'}
    out["source"] = "business"
    return out


def post_config_id_for(platform: str, post_id: str) -> int:
    """用「平台 + 貼文原生 ID」反查 `post_ai_config.id`；沒配置過回 0。

    ★★ 為什麼一定要有這一步（2026-10-22 修的 P0）：
      `post_ai_config.id` 是 **CRM 內部主鍵**，Connector 推 inbound 時拿不到 ——
      它只知道 `post_id`（平台原生 ID）。所以 `crm_ai_inbound_queue.content` 裡
      **從來沒有** `post_config_id`，worker 讀出來永遠是 0 →
      `resolve_scope_set()` 一路走 `business_scope_set()`（商家基準集）
      → **貼文特殊配置形同不存在**（取消了某條 FAQ 照樣命中）。
      貼文配置的解析必須在 CRM 這一側做，用平台原生 ID 對齊。
    """
    plat = (platform or "").strip()
    pid = (post_id or "").strip()
    if not plat or not pid:
        return 0
    rows = query("SELECT id FROM post_ai_config WHERE platform=%s AND post_id=%s",
                 (plat, pid))
    return int(rows[0]["id"]) if rows else 0


def resolve_scope_set(business_id: int, *, scope: str = "dm",
                      post_config_id: int = 0, post_id: str = "",
                      platform: str = "") -> dict:
    """這次要用哪一組啟用集（§9.2）。

    * 私訊 → 商家基準集。
    * 貼文留言 → 該貼文的覆寫集；**沒配置過（`configured=false`）＝與私訊完全相同**，
      因為彈窗的預設候選就是基準集。一旦配置過就完全按自己的儲存，不再逐組 fallback。
    * 貼文總開關關 → 三層全空、三層開關全關（該貼文不用 AI）。

    ★ `post_config_id` 為 0 但有 `platform` + `post_id` 時**自己反查**
      （見 `post_config_id_for()` 的說明）：正式流程（worker / 補拉）走的就是這條，
      因為 Connector 不可能知道 CRM 的主鍵。
    ★ 傳進來的 `post_config_id` 若與 `post_id` 對不上（呼叫端傳錯），改用 `post_id` 反查，
      不讓一個錯 id 把整則貼文的配置指到別的貼文身上。

    回傳 `{"faq":[ids], "table":[ids], "kb":[ids], "layers":{...}, "source": str}`；
    某層是 `[]` 就代表那層這次不跑。
    """
    if scope == "comment":
        plat = (platform or "").strip()
        pid = (post_id or "").strip()
        if post_config_id:
            # 驗一下這個 id 是不是真的屬於這則貼文（呼叫端傳錯 id 會靜默套錯配置）
            rows = query("SELECT platform, post_id FROM post_ai_config WHERE id=%s",
                         (int(post_config_id),))
            if not rows:
                post_config_id = 0
            elif pid and (rows[0]["post_id"] or "") != pid:
                log.warning("post_config_id=%s 屬於貼文 %s，與這次的 %s 不符 → 改用 post_id 反查",
                            post_config_id, rows[0]["post_id"], pid)
                post_config_id = 0
        if not post_config_id and plat and pid:
            post_config_id = post_config_id_for(plat, pid)

    if scope == "comment" and post_config_id:
        cfg = query("SELECT enabled, configured, kb_enabled, table_enabled, faq_enabled"
                    " FROM post_ai_config WHERE id=%s", (int(post_config_id),))
        if cfg:
            c = cfg[0]
            if not c["enabled"]:
                return {"faq": [], "table": [], "kb": [],
                        "layers": {"faq": False, "table": False, "rag": False},
                        "source": "post_off"}
            if c["configured"]:
                out: dict = {}
                for kind, (table, col) in POST_BINDING_SPECS.items():
                    rows = query(f"SELECT {col} AS id FROM {table}"
                                 " WHERE post_config_id=%s AND enabled ORDER BY id",
                                 (int(post_config_id),))
                    out[kind] = [int(r["id"]) for r in rows]
                out["layers"] = {"faq": bool(c["faq_enabled"]),
                                 "table": bool(c["table_enabled"]),
                                 "rag": bool(c["kb_enabled"])}
                out["source"] = "post"
                return out
    return business_scope_set(business_id)


# ---------- FAQ ----------

def list_faqs(business_id: int, enabled_only: bool = False, q: str = "",
              ids: list[int] | None = None) -> list[dict]:
    """列出**該商家**的 FAQ。`q` 有值時對 question / answer / keywords 做內文檢索（ILIKE）。

    `ids`：只取這幾個 id（＝該商家基準啟用集，或貼文覆寫集）。
    ⚠️ 傳空清單回**空**（＝這一層不跑），不是「不篩選」—— 設計案 §9.2 的空集語義。

    ⚠️ 一定要參數化。`query(sql, params)` 即使傳空 tuple，psycopg2 仍會做 `%` 解析，
    SQL 裡若出現字面 `%` 會噴 `IndexError: tuple index out of range`
    （這個專案踩過）。所以用 `ILIKE %s` + 值裡自己加 `%`，不要寫字面 `LIKE '%x%'`。
    """
    if ids is not None and not ids:
        return []
    sql = """
        SELECT id, question, answer, keywords, enabled, hit_count, sort_order,
               created_at, updated_at
        FROM autoreply_faq
    """
    where, params = ["business_id = %s"], [int(business_id)]
    if enabled_only:
        where.append("enabled = true")
    if ids is not None:
        where.append("id = ANY(%s)")
        params.append([int(i) for i in ids])
    key = (q or "").strip()
    if key:
        where.append("(question ILIKE %s OR answer ILIKE %s OR COALESCE(keywords,'') ILIKE %s)")
        needle = f"%{key}%"
        params += [needle, needle, needle]
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_order, id"
    return query(sql, tuple(params))


def create_faq(business_id: int, question: str, answer: str,
               keywords: str = "", enabled: bool = True) -> dict:
    row = execute_returning("""
        INSERT INTO autoreply_faq (business_id, question, answer, keywords, enabled, sort_order)
        VALUES (%s, %s, %s, %s, %s,
                COALESCE((SELECT max(sort_order)+1 FROM autoreply_faq WHERE business_id=%s), 0))
        RETURNING id, question, answer, keywords, enabled, hit_count, sort_order
    """, (int(business_id), question, answer, keywords, enabled, int(business_id)))
    keys = ("id", "question", "answer", "keywords", "enabled", "hit_count", "sort_order")
    out = dict(zip(keys, row))
    add_binding(business_id, "faq", out["id"])       # 上傳即進基準集（§9.4）
    return out


def update_faq(faq_id: int, fields: dict) -> bool:
    allowed = ("question", "answer", "keywords", "enabled", "sort_order")
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=%s")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(faq_id)
    return execute(f"UPDATE autoreply_faq SET {', '.join(sets)} WHERE id=%s", tuple(params)) > 0


def delete_faq(faq_id: int) -> bool:
    # 先清基準集的自動綁定，避免留下指向不存在 FAQ 的懸掛列
    execute("DELETE FROM business_faq_binding WHERE faq_id=%s", (faq_id,))
    execute("DELETE FROM post_faq_binding WHERE faq_id=%s", (faq_id,))
    return execute("DELETE FROM autoreply_faq WHERE id=%s", (faq_id,)) > 0


def set_faq_enabled(faq_id: int, enabled: bool) -> bool:
    return update_faq(faq_id, {"enabled": bool(enabled)})


def bump_faq_hit(faq_id: int) -> None:
    execute("UPDATE autoreply_faq SET hit_count = hit_count + 1 WHERE id=%s", (faq_id,))


def replace_faqs(business_id: int, items: list[dict], mode: str = "replace") -> dict:
    """匯入該商家的 FAQ。mode: replace = 先清空（該商家）再寫入；append = 直接附加。

    匯入的每一條都**自動進基準集**（§9.4）—— 否則匯完一輪 FAQ 會因為
    `business_faq_binding` 是空的而整層不跑。
    """
    written = 0
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if mode == "replace":
                cur.execute("DELETE FROM business_faq_binding WHERE business_id=%s",
                            (int(business_id),))
                cur.execute("DELETE FROM autoreply_faq WHERE business_id=%s", (int(business_id),))
            for i, it in enumerate(items):
                cur.execute("""
                    INSERT INTO autoreply_faq
                        (business_id, question, answer, keywords, enabled, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """, (int(business_id), it["question"], it["answer"], it.get("keywords", ""),
                      bool(it.get("enabled", True)), i))
                fid = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO business_faq_binding (business_id, faq_id, enabled)"
                    " VALUES (%s, %s, true) ON CONFLICT (business_id, faq_id) DO NOTHING",
                    (int(business_id), fid))
                written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "mode": mode}


# ---------- 業務表格 ----------

def list_tables(business_id: int, enabled_only: bool = False,
                ids: list[int] | None = None) -> list[dict]:
    """`ids`：只取這幾個 id（基準啟用集 / 貼文覆寫集）。空清單 = 這一層不跑。"""
    if ids is not None and not ids:
        return []
    where, params = ["business_id = %s"], [int(business_id)]
    if enabled_only:
        where.append("enabled = true")
    if ids is not None:
        where.append("id = ANY(%s)")
        params.append([int(i) for i in ids])
    return query(
        "SELECT id, name, description, columns, enabled, sort_order, created_at, updated_at"
        " FROM autoreply_business_table WHERE " + " AND ".join(where)
        + " ORDER BY sort_order, id", tuple(params))


def create_table(business_id: int, name: str, description: str = "",
                 columns: list | None = None, enabled: bool = True) -> dict:
    import json
    row = execute_returning("""
        INSERT INTO autoreply_business_table
            (business_id, name, description, columns, enabled, sort_order)
        VALUES (%s, %s, %s, %s::jsonb, %s,
                COALESCE((SELECT max(sort_order)+1 FROM autoreply_business_table
                          WHERE business_id=%s), 0))
        RETURNING id, name, description, columns, enabled, sort_order
    """, (int(business_id), name, description,
          json.dumps(columns or [], ensure_ascii=False), enabled, int(business_id)))
    keys = ("id", "name", "description", "columns", "enabled", "sort_order")
    out = dict(zip(keys, row))
    add_binding(business_id, "table", out["id"])     # 上傳即進基準集（§9.4）
    return out


def update_table(table_id: int, fields: dict) -> bool:
    import json
    allowed = ("name", "description", "enabled", "sort_order")
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=%s")
            params.append(v)
    if "columns" in fields:
        sets.append("columns=%s::jsonb")
        params.append(json.dumps(fields["columns"] or [], ensure_ascii=False))
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(table_id)
    return execute(f"UPDATE autoreply_business_table SET {', '.join(sets)} WHERE id=%s",
                   tuple(params)) > 0


def delete_table(table_id: int) -> bool:
    execute("DELETE FROM business_table_binding WHERE business_table_id=%s", (table_id,))
    execute("DELETE FROM post_table_binding WHERE business_table_id=%s", (table_id,))
    return execute("DELETE FROM autoreply_business_table WHERE id=%s", (table_id,)) > 0


def list_rows(table_id: int | None = None, enabled_only: bool = False) -> list[dict]:
    sql = """
        SELECT id, table_id, data, enabled, sort_order
        FROM autoreply_business_row
    """
    where, params = [], []
    if table_id is not None:
        where.append("table_id=%s")
        params.append(table_id)
    if enabled_only:
        where.append("enabled=true")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY table_id, sort_order, id"
    return query(sql, tuple(params))


def replace_rows(table_id: int, rows: list[dict]) -> int:
    """整表覆寫資料列（匯入用），回傳寫入列數。"""
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM autoreply_business_row WHERE table_id=%s", (table_id,))
            for i, data in enumerate(rows):
                cur.execute("""
                    INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
                    VALUES (%s, %s::jsonb, true, %s)
                """, (table_id, json.dumps(data, ensure_ascii=False), i))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def append_rows(table_id: int, rows: list[dict]) -> int:
    import json
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(max(sort_order)+1, 0) FROM autoreply_business_row WHERE table_id=%s",
                        (table_id,))
            start = cur.fetchone()[0]
            for i, data in enumerate(rows):
                cur.execute("""
                    INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
                    VALUES (%s, %s::jsonb, true, %s)
                """, (table_id, json.dumps(data, ensure_ascii=False), start + i))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def create_row(table_id: int, data: dict) -> dict:
    import json
    row = execute_returning("""
        INSERT INTO autoreply_business_row (table_id, data, enabled, sort_order)
        VALUES (%s, %s::jsonb, true,
                COALESCE((SELECT max(sort_order)+1 FROM autoreply_business_row WHERE table_id=%s), 0))
        RETURNING id, table_id, data, enabled, sort_order
    """, (table_id, json.dumps(data, ensure_ascii=False), table_id))
    keys = ("id", "table_id", "data", "enabled", "sort_order")
    return dict(zip(keys, row))


def update_row(row_id: int, data: dict | None = None, enabled: bool | None = None) -> bool:
    import json
    sets, params = [], []
    if data is not None:
        sets.append("data=%s::jsonb")
        params.append(json.dumps(data, ensure_ascii=False))
    if enabled is not None:
        sets.append("enabled=%s")
        params.append(bool(enabled))
    if not sets:
        return False
    sets.append("updated_at=now()")
    params.append(row_id)
    return execute(f"UPDATE autoreply_business_row SET {', '.join(sets)} WHERE id=%s",
                   tuple(params)) > 0


def delete_row(row_id: int) -> bool:
    return execute("DELETE FROM autoreply_business_row WHERE id=%s", (row_id,)) > 0


def delete_rows_bulk(row_ids: list[int]) -> int:
    if not row_ids:
        return 0
    return execute("DELETE FROM autoreply_business_row WHERE id = ANY(%s)", (list(row_ids),))


# ---------- 日誌 / 認領 ----------

# 日誌與認領共用一份欄位清單，避免兩邊走鐘
_LOG_COLUMNS = (
    "claim_key", "business_id", "platform", "scope", "business_platform_account_id",
    "end_user_sender_id", "message_id", "tier", "outbound_task_id", "question",
    "answer", "score", "detail", "matched_rows", "rag_threshold", "rag_candidates",
    "rag_queries", "rewrote", "rewritten_query", "history_used", "send_status",
    "elapsed_ms", "llm_calls", "llm_seconds",
)

_LOG_SQL = f"""
    INSERT INTO autoreply_log ({', '.join(_LOG_COLUMNS)})
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)
"""


def _log_params(record: dict) -> tuple:
    matched = record.get("matched_rows")
    rag_q = record.get("rag_queries")
    return (
        record.get("claim_key"),
        int(record["business_id"]),
        record.get("platform"), record.get("scope"),
        record.get("business_platform_account_id"),
        record.get("end_user_sender_id"), record.get("message_id"),
        record.get("tier"), record.get("outbound_task_id"),
        record.get("question"), record.get("answer"), record.get("score"),
        record.get("detail"),
        json.dumps(matched, ensure_ascii=False) if matched is not None else None,
        record.get("rag_threshold"), record.get("rag_candidates"),
        json.dumps(rag_q, ensure_ascii=False) if rag_q is not None else None,
        bool(record.get("rewrote")),
        record.get("rewritten_query") or None,
        bool(record.get("history_used")),
        record.get("send_status", "not_sent"),
        int(record.get("elapsed_ms") or 0),
        int(record.get("llm_calls") or 0),
        float(record.get("llm_seconds") or 0),
    )


def insert_log(record: dict) -> None:
    execute(_LOG_SQL, _log_params(record))


def claim_message(claim_key: str, record: dict) -> bool:
    """原子認領一則訊息：回傳 True 表示由我處理，False 表示別人已處理過。

    靠 `idx_autoreply_log_claim` 唯一索引 + ON CONFLICT DO NOTHING 保證只有一個
    呼叫者能成功插入。這解決的是「同一則訊息被重複投遞 / 併發處理」造成的重複回覆
    —— 冷卻檢查是「先查再寫」、中間有窗口，擋不住同時進來的兩份。
    """
    if not claim_key:
        return True
    conn = _connect()
    try:
        with conn.cursor() as cur:
            params = list(_log_params(record))
            params[0] = claim_key
            cur.execute(_LOG_SQL + " ON CONFLICT (claim_key) DO NOTHING", tuple(params))
            inserted = cur.rowcount > 0
        conn.commit()
        return inserted
    finally:
        conn.close()


def update_claim(claim_key: str, record: dict) -> int:
    """認領列已存在，後續用它記下最終結果（不再插新列）。"""
    if not claim_key:
        insert_log(record)
        return 0
    matched = record.get("matched_rows")
    rag_q = record.get("rag_queries")
    return execute("""
        UPDATE autoreply_log SET
            tier=%s, detail=%s, question=%s, answer=%s, message_id=%s,
            outbound_task_id=%s, matched_rows=%s::jsonb, score=%s, rewrote=%s,
            rewritten_query=%s, history_used=%s, send_status=%s,
            rag_threshold=%s, rag_candidates=%s, rag_queries=%s::jsonb,
            elapsed_ms=%s, llm_calls=%s, llm_seconds=%s
        WHERE claim_key=%s
    """, (
        record.get("tier"), record.get("detail"),
        record.get("question"), record.get("answer"), record.get("message_id"),
        record.get("outbound_task_id"),
        json.dumps(matched, ensure_ascii=False) if matched is not None else None,
        record.get("score"), bool(record.get("rewrote")),
        record.get("rewritten_query") or None, bool(record.get("history_used")),
        record.get("send_status", "not_sent"),
        record.get("rag_threshold"), record.get("rag_candidates"),
        json.dumps(rag_q, ensure_ascii=False) if rag_q is not None else None,
        int(record.get("elapsed_ms") or 0),
        int(record.get("llm_calls") or 0),
        float(record.get("llm_seconds") or 0),
        claim_key,
    ))


def is_claimed(claim_key: str) -> bool:
    """這則訊息是否已被認領過（除錯用）。"""
    if not claim_key:
        return False
    rows = query("SELECT 1 FROM autoreply_log WHERE claim_key=%s LIMIT 1", (claim_key,))
    return bool(rows)


def recent_reply_count(business_id: int, end_user_sender_id: str, seconds: int) -> int:
    """該會話在近 N 秒內已成功送出的自動回復數（冷卻判斷用）。"""
    if not end_user_sender_id or seconds <= 0:
        return 0
    rows = query("""
        SELECT count(*) AS n FROM autoreply_log
        WHERE business_id=%s AND end_user_sender_id=%s AND send_status='sent'
          AND created_at > now() - (%s || ' seconds')::interval
    """, (int(business_id), end_user_sender_id, int(seconds)))
    return rows[0]["n"] if rows else 0


def today_reply_count(business_id: int, end_user_sender_id: str) -> int:
    """該會話今天的自動回復送出數（每日上限判斷用）。"""
    if not end_user_sender_id:
        return 0
    rows = query("""
        SELECT count(*) AS n FROM autoreply_log
        WHERE business_id=%s AND end_user_sender_id=%s AND send_status='sent'
          AND created_at >= date_trunc('day', now())
    """, (int(business_id), end_user_sender_id))
    return rows[0]["n"] if rows else 0


def list_logs(business_id: int, platform: str = "", scope: str = "", tier: str = "",
              limit: int = 100, offset: int = 0) -> dict:
    where, params = ["business_id=%s", "tier <> %s"], [int(business_id), TIER_CLAIM]
    if platform:
        where.append("platform=%s")
        params.append(platform)
    if scope:
        where.append("scope=%s")
        params.append(scope)
    if tier:
        where.append("tier=%s")
        params.append(tier)
    clause = " WHERE " + " AND ".join(where)
    # has_matched_rows：這筆有沒有檢索明細可以展開。列表不撈明細本體，但 UI 要能
    # 判斷「這列值不值得點」，所以在 SQL 端算一個布林值（很便宜，且 JSONB 有函式可用）。
    rows = query(f"""
        SELECT id, created_at, business_id, platform, scope,
               business_platform_account_id, end_user_sender_id, message_id,
               tier, outbound_task_id, question, answer, score, detail,
               rewrote, rewritten_query, history_used, send_status,
               rag_threshold, rag_candidates, elapsed_ms, llm_calls, llm_seconds,
               (matched_rows IS NOT NULL
                AND jsonb_typeof(matched_rows) = 'array'
                AND jsonb_array_length(matched_rows) > 0) AS has_matched_rows
        FROM autoreply_log{clause}
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
    """, tuple(params + [max(1, min(limit, 500)), max(0, offset)]))
    total = query(f"SELECT count(*) AS n FROM autoreply_log{clause}", tuple(params))
    return {"logs": rows, "total": total[0]["n"] if total else 0}


def get_log(log_id: int) -> dict | None:
    """單筆紀錄的完整內容（含 `matched_rows` 召回明細）。

    ⚠️ 為什麼列表不帶這個欄位：`matched_rows` 每筆可能是幾十個候選塊的 JSON，
    列表動輒上百筆，一起 SELECT 會讓回應肥到沒必要。**列表輕、詳情重。**
    """
    rows = query("SELECT * FROM autoreply_log WHERE id=%s", (log_id,))
    return rows[0] if rows else None


def log_summary(business_id: int) -> dict:
    """該商家各平台＋場景的自動回復統計（給 UI 顯示）。

    rewritten = 第二層「改寫 query 後才命中」的次數（本來會降級人工，被救回來的）。
    """
    rows = query("""
        SELECT platform, scope, tier,
               count(*) AS n,
               count(*) FILTER (WHERE tier='table' AND rewrote) AS rewritten
        FROM autoreply_log WHERE business_id=%s AND tier <> %s
        GROUP BY platform, scope, tier
    """, (int(business_id), TIER_CLAIM))
    out: dict[str, dict] = {}
    for r in rows:
        key = scope_key(r["platform"], r["scope"])
        bucket = out.setdefault(key, {"platform": r["platform"], "scope": r["scope"],
                                      "faq": 0, "table": 0, "rag": 0, "miss": 0, "error": 0,
                                      "rewritten": 0, "total": 0})
        tier = r["tier"] if r["tier"] in KNOWN_TIERS else "error"
        bucket[tier] += r["n"]
        bucket["rewritten"] += r["rewritten"] or 0
        bucket["total"] += r["n"]
    return out


# ---------- 工作台查詢（★ 階段四：改走 Connector 端點） ----------
#
# 這些查詢以前直連消息表；消息表在 Connector。階段三先保留函式名並回安全空值，
# 讓 autoreply_console 不至於 AttributeError；階段四接上 Connector 對應端點。

def latest_unanswered_dm(channel: str, conversation_key: str) -> dict | None:
    """（階段四）某 IG/FB 私訊會話最新未處理來訊。"""
    return None


def latest_unanswered_forward_log(channel: str, conversation_key: str) -> dict | None:
    """（階段四）微信／WhatsApp 某會話最新未處理來訊。"""
    return None


def latest_inbound_dm(channel: str, conversation_key: str) -> dict | None:
    """（階段四）該 IG/FB 會話最新一則客戶訊息。"""
    return None


def latest_inbound_forward_log(channel: str, conversation_key: str) -> dict | None:
    """（階段四）該 WhatsApp 會話最新一則客戶訊息。"""
    return None


def miss_counts() -> dict:
    """（階段四）未命中待處理數（導覽紅點）。"""
    return {}


# ---------- 訊息的自動回復標記（走 Connector `messages/mark`） ----------

def _mark(platform: str, *, message_ids=None, message_id: str = "", comment_id: str = "",
          conversation_id: str = "", state: str = "", tier: str = "", detail: str = "",
          handled=None) -> int:
    try:
        from app.internal import connector_client
        return int(connector_client.mark_message(
            platform=platform, message_ids=message_ids, message_id=message_id,
            comment_id=comment_id, conversation_id=conversation_id,
            state=state, tier=tier, detail=detail, handled=handled) or 0)
    except Exception as e:
        log.warning("標記訊息狀態失敗 %s: %s", platform, e)
        return 0


def mark_forward_log(message_ids: list[str], platform: str, state: str,
                     tier: str = "", detail: str = "") -> int:
    if not message_ids:
        return 0
    return _mark(platform, message_ids=list(message_ids),
                 state=state, tier=tier, detail=detail)


def mark_dm(platform: str, message_id: str, state: str, tier: str = "", detail: str = "") -> int:
    if not message_id:
        return 0
    return _mark(platform, message_id=message_id, state=state, tier=tier, detail=detail)


def mark_comment(platform: str, comment_id: str, state: str, tier: str = "",
                 detail: str = "") -> int:
    if not comment_id:
        return 0
    return _mark(platform, comment_id=comment_id, state=state, tier=tier, detail=detail)


def mark_forward_log_handled(platform: str, conversation_id: str) -> int:
    """自動回復送出成功後，把該會話的客戶來訊標為已處理（消除紅點）。"""
    if not conversation_id:
        return 0
    return _mark(platform, conversation_id=conversation_id, handled=True)


# ---------- 對話上下文（業務表格 query 改寫用；走 Connector `messages/query`） ----------
#
# 一律回傳「這次要處理的那則訊息之前」的最近 N 則訊息，由舊到新。
# 排除當前訊息用 id，不用文字比對 —— 客戶可能連續兩句講一模一樣的話。

def _trim_before(items: list[dict], exclude_id: str = "", exclude_text: str = "") -> list[dict]:
    """把當前這則訊息從歷史裡去掉（優先用 id，其次用文字）。"""
    out = list(items)
    if exclude_id:
        out = [x for x in out if str(x.get("id") or "") != str(exclude_id)]
    elif exclude_text:
        t = exclude_text.strip()
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("direction") == "inbound" and (out[i].get("text") or "").strip() == t:
                out.pop(i)
                break
    return [x for x in out if (x.get("text") or "").strip()]


def get_conversation_history(channel: str, scope: str, *, sender_id: str = "",
                             comment_id: str = "", message_id: str = "",
                             current_text: str = "", limit: int = 5,
                             platform_account_id: int = 0) -> list[dict]:
    """依渠道＋場景取得「當前訊息之前」的最近對話（走 Connector `messages/query`）。

    取不到（連不上 / comment 端點階段四才有）回空清單 —— 改寫退化為單句改寫。
    """
    if scope == "comment" or not sender_id or limit <= 0:
        return []              # ★ comment 歷史端點階段四補；先安全降級為空
    try:
        from app.internal import connector_client
        items = connector_client.fetch_history(
            platform=channel, end_user_sender_id=sender_id,
            platform_account_id=platform_account_id, limit=limit + 1) or []
        norm = [{"id": str(x.get("message_id") or x.get("id") or ""),
                 "direction": x.get("direction"),
                 "text": (x.get("text") or "")} for x in items]
        return _trim_before(norm, exclude_id=message_id, exclude_text=current_text)[-limit:]
    except Exception as e:  # 取歷史失敗不該影響自動回復
        log.warning("取對話上下文失敗 %s/%s: %s", channel, scope, e)
        return []
