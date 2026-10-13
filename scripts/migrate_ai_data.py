"""把舊系統（`platform_forwarder`）的 **AI 資料**全量搬進 CRM（`crm_db`）。

對應 `docs/MIGRATION_PLAN.md` §3.3 步驟 3 與階段三 T18。

搬什麼
------
* 知識庫 7 表：`kb_collection` / `kb_document` / `kb_chunk` / `kb_job` / `kb_param` /
  `kb_asset` / `kb_asset_chunk`
* AI 資料 4 表：`autoreply_faq` / `autoreply_business_table` / `autoreply_business_row` /
  `autoreply_log`

搬完**必須**順手把這三件「新模型才有的東西」補出來，否則 AI 會靜默失效：

1. **`business_*_binding`（基準啟用集）** —— 設計案 §9.2 鐵則「**空集 = 不跑**」。
   只搬資料不補 binding，等於三層全部沒勾，AI 一聲不吭。
   （舊系統沒有這個概念：資料是全域的，所以這裡＝把搬過來的每一項都綁上去，
   並沿用該項自己的 `enabled`。）—— 真的不知道要綁什麼，就別搬。
2. **`business_ai_switch`（第一層開關）** —— 舊 `autoreply_setting` 是
   `channel:scope` / `channel` / `global` 的**優先序鏈**，新表只有精確的
   `(platform, scope)`。這裡把鏈**展開成平台 × 場景的完整笛卡兒積**，
   讓每個組合都寫下明確的一列 —— 展開後的行為與舊系統**逐格相同**。
3. **`business_layer_switch`** —— 舊 `faq_enabled` / `table_enabled` / `rag_enabled`。

還有一個**第二層開關**的坑：`crm_business_platform_account` 是空的，而
`autoreply_db.platform_account_enabled()` 對「查不到的帳號」回 **False**（兩層 AND
的第二層）。所以 `--seed-accounts`（預設開）會依 `connector/.env` 的渠道身分補上帳號列。
⚠️ 這裡用的「平台原生帳號 ID」慣例必須跟 Connector 的 webhook 正規化一致
（wechat→APPID、whatsapp→PHONE_NUMBER_ID、fb→PAGE_ID、ig→IG_USER_ID）；
webhook 那一頭若改了，這裡要跟著改。

用法（**從專案根執行**）
------------------------
    python scripts/migrate_ai_data.py --dry-run     # 只報告，不寫任何東西、不建 FDW
    python scripts/migrate_ai_data.py               # 真正搬（目標表非空時拒絕）
    python scripts/migrate_ai_data.py --force       # 先清掉 AI 相關表再搬（會印警告）
    python scripts/migrate_ai_data.py --verify      # 搬完對帳（行數 / 孤兒 / 開關 / 綁定）
    python scripts/migrate_ai_data.py --no-accounts # 不補平台帳號（自己做過就跳過）
    python scripts/migrate_ai_data.py --business Epro_test

媒體檔案**不在本腳本範圍**（DB 只存相對路徑）：
    `media/kb_assets/` → `crm/media/kb_assets/`（`RAG_MEDIA_DIR=media`，相對 cwd）。
    本腳本會檢查並提醒，需要時加 `--copy-media` 一併複製。

DSN
---
* 來源：環境變數 `LEGACY_DATABASE_URL`，否則讀專案根 `.env` 的 `DATABASE_URL`
* 目標：`crm/.env` 的 `DATABASE_URL`（與 `scripts/init_crm_db.py` 同一套）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402
from psycopg2.extras import Json  # noqa: E402

from app.core import config as crm_config  # noqa: E402
from app.core import schema as crm_schema  # noqa: E402

DEFAULT_BUSINESS = os.getenv("DEFAULT_BUSINESS_NAME", "Epro_test")

# FDW 掛載來源庫用的 schema 名（只放 foreign table 的 metadata，每次重跑重建）
FDW_SCHEMA = "legacy_src"
FDW_SERVER = "legacy_ai_src"

# 要掛進來的來源表
FDW_TABLES = (
    "kb_collection", "kb_document", "kb_chunk", "kb_job", "kb_param",
    "kb_asset", "kb_asset_chunk",
    "autoreply_faq", "autoreply_business_table", "autoreply_business_row",
    "autoreply_log", "autoreply_setting",
)

# 舊系統的渠道 × 場景（展開 `autoreply_setting` 的優先序鏈用）
PLATFORMS = ("wechat", "whatsapp", "ig", "fb")
SCOPES = ("dm", "comment")

# 補 `crm_business_platform_account` 時，各渠道的原生帳號 ID 取自哪個環境變數
PLATFORM_IDENTITY_KEYS = (
    ("wechat", "WECHAT_APPID"),
    ("whatsapp", "WHATSAPP_PHONE_NUMBER_ID"),
    ("fb", "META_PAGE_ID"),
    ("ig", "META_IG_USER_ID"),
)

# 會被本腳本寫入的目標表（`--force` 清空用；順序＝TRUNCATE 的 FK 安全順序）
# ⚠️ `post_*` 只是「被參照」的空表，但它們指名引用 kb_collection /
#    autoreply_business_table / autoreply_faq → 不一起 TRUNCATE 就會
#    `cannot truncate a table referenced in a foreign key constraint`。
TARGET_TABLES = (
    "post_kb_binding", "post_table_binding", "post_faq_binding", "post_ai_config",
    "kb_asset_chunk", "kb_asset", "kb_chunk", "kb_job", "kb_document",
    "kb_collection", "kb_param",
    "autoreply_business_row", "autoreply_business_table", "autoreply_faq",
    "autoreply_log",
    "business_kb_binding", "business_table_binding", "business_faq_binding",
    "business_ai_switch", "business_layer_switch",
)

# 保留舊 id 的表（bigserial 要 setval，否則新資料會撞主鍵）
ID_PRESERVING = (
    "kb_collection", "kb_document", "kb_chunk", "kb_job", "kb_asset",
    "autoreply_faq", "autoreply_business_table", "autoreply_business_row",
    "autoreply_log",
)

_lines: list[str] = []


def say(msg: str = "") -> None:
    _lines.append(msg)
    print(msg)


# ======================================================================
# DSN / 連線
# ======================================================================

def parse_env_file(path: Path) -> dict[str, str]:
    """極簡 .env 解析（不污染 os.environ，避免與 crm/.env 互相覆蓋）。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def conn_parts(dsn: str, default_db: str) -> dict:
    from urllib.parse import urlparse, unquote

    u = urlparse(dsn)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "dbname": (u.path or f"/{default_db}").lstrip("/") or default_db,
    }


def legacy_dsn() -> str:
    env = os.getenv("LEGACY_DATABASE_URL")
    if env:
        return env
    return parse_env_file(ROOT / ".env").get(
        "DATABASE_URL", "postgresql://postgres@localhost:5432/platform_forwarder")


def crm_dsn() -> str:
    return crm_config.DATABASE_URL


def conn(dsn: str, default_db: str):
    p = conn_parts(dsn, default_db)
    return psycopg2.connect(**p), p


# ======================================================================
# 冪等的 FDW 掛載（只在真正搬遷時做）
# ======================================================================

def setup_fdw(cur, src: dict) -> None:
    cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
    cur.execute("SELECT 1 FROM pg_foreign_server WHERE srvname = %s", (FDW_SERVER,))
    if not cur.fetchone():
        cur.execute(sql.SQL(
            "CREATE SERVER {} FOREIGN DATA WRAPPER postgres_fdw"
            " OPTIONS (host %s, port %s, dbname %s)"
        ).format(sql.Identifier(FDW_SERVER)),
            (src["host"], str(src["port"]), src["dbname"]))
    cur.execute("SELECT 1 FROM pg_user_mappings WHERE srvname = %s AND usename = current_user",
                (FDW_SERVER,))
    if not cur.fetchone():
        cur.execute(sql.SQL(
            "CREATE USER MAPPING FOR CURRENT_USER SERVER {}"
            " OPTIONS (user %s, password %s)"
        ).format(sql.Identifier(FDW_SERVER)), (src["user"], src["password"]))

    # 重建 foreign table（IMPORT FOREIGN SCHEMA 遇到已存在的表會報錯）
    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(FDW_SCHEMA)))
    cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(FDW_SCHEMA)))
    cur.execute(sql.SQL(
        "IMPORT FOREIGN SCHEMA public LIMIT TO ({}) FROM SERVER {} INTO {}"
    ).format(
        sql.SQL(", ").join(sql.Identifier(t) for t in FDW_TABLES),
        sql.Identifier(FDW_SERVER),
        sql.Identifier(FDW_SCHEMA),
    ))


def teardown_fdw(cur) -> None:
    """卸掉 foreign table、server 與 user mapping。

    ⚠️ user mapping 裡存著來源庫的密碼 —— 搬完就該清掉，不要留在 `crm_db` 裡。
    （extension 本身留著無害，下次重跑也不用再建。）
    """
    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(FDW_SCHEMA)))
    cur.execute(sql.SQL("DROP SERVER IF EXISTS {} CASCADE").format(sql.Identifier(FDW_SERVER)))


def ft(name: str) -> str:
    return f"{FDW_SCHEMA}.{name}"


# ======================================================================
# 逐表搬遷 SQL（`%(biz)s` 由 dict 帶入）
# ======================================================================

# ---- 知識庫 ----
SQL_KB_COLLECTION = """
INSERT INTO kb_collection (id, business_id, name, description, kind, image_of,
                           enabled, sort_order, created_at, updated_at)
SELECT id, %(biz)s, name, description,
       CASE WHEN kind IN ('doc','image') THEN kind ELSE 'doc' END,
       image_of,
       COALESCE(enabled, true), COALESCE(sort_order, 0),
       COALESCE(created_at, now()), COALESCE(updated_at, now())
  FROM {ft_kb_collection}
"""

SQL_KB_CHUNK = """
INSERT INTO kb_chunk (id, doc_id, ordinal, text, prefix, char_count, page_no,
                      heading_path, sheet, row_from, row_to, embedding,
                      embedding_model, embedding_dim, embedding_status,
                      edited, enabled, created_at)
SELECT id, doc_id, ordinal, text, prefix, char_count, page_no,
       heading_path, sheet, row_from, row_to, embedding,
       embedding_model, embedding_dim, embedding_status,
       COALESCE(edited, false), COALESCE(enabled, true), COALESCE(created_at, now())
  FROM {ft_kb_chunk}
"""

SQL_KB_JOB = """
INSERT INTO kb_job (id, doc_id, stage, status, attempts, last_error,
                    started_at, finished_at, created_at)
SELECT id, doc_id, stage, status, COALESCE(attempts, 0), last_error,
       started_at, finished_at, COALESCE(created_at, now())
  FROM {ft_kb_job}
"""

SQL_KB_ASSET = """
INSERT INTO kb_asset (id, collection_id, doc_id, page_no, rel_path, filename, mime,
                      size_bytes, width, height, sha256, caption, caption_source,
                      caption_status, bbox, embedding, embedding_status, enabled,
                      filtered_reason, created_at)
SELECT id, collection_id, doc_id, page_no, rel_path, filename, mime,
       size_bytes, width, height, sha256, caption, caption_source,
       caption_status, bbox, embedding, embedding_status,
       COALESCE(enabled, true), filtered_reason, COALESCE(created_at, now())
  FROM {ft_kb_asset}
"""

SQL_KB_ASSET_CHUNK = """
INSERT INTO kb_asset_chunk (asset_id, chunk_id)
SELECT asset_id, chunk_id FROM {ft_kb_asset_chunk}
"""

# ---- FAQ / 業務表格 / 日誌 ----
SQL_FAQ = """
INSERT INTO autoreply_faq (id, business_id, question, answer, keywords, enabled,
                           hit_count, sort_order, created_at, updated_at)
SELECT id, %(biz)s, question, answer, keywords,
       COALESCE(enabled, true),
       LEAST(COALESCE(hit_count, 0), 2147483647)::int,
       COALESCE(sort_order, 0),
       COALESCE(created_at, now()), COALESCE(updated_at, now())
  FROM {ft_autoreply_faq}
"""

SQL_TABLE = """
INSERT INTO autoreply_business_table (id, business_id, name, description, columns,
                                      enabled, sort_order, created_at, updated_at)
SELECT id, %(biz)s, name, description, columns,
       COALESCE(enabled, true), COALESCE(sort_order, 0),
       COALESCE(created_at, now()), COALESCE(updated_at, now())
  FROM {ft_autoreply_business_table}
"""

# ⚠️ 舊表有 created_at、新表沒有；舊表沒有 updated_at、新表有 → 用舊 updated_at 填
SQL_ROW = """
INSERT INTO autoreply_business_row (id, table_id, data, enabled, sort_order, updated_at)
SELECT id, table_id, data, COALESCE(enabled, true), COALESCE(sort_order, 0),
       COALESCE(updated_at, created_at, now())
  FROM {ft_autoreply_business_row}
"""

# ⚠️ 兩邊**欄名完全不同**：channel→platform、reply_scope→scope、
#    conversation_key→end_user_sender_id、history_used(int)→history_used(bool)。
#    舊表的 faq_id / table_id / reason 在新表沒有對應欄：
#      · reason（sent / send_failed / no_match / manual）併進 detail 尾巴，文字不丟
#      · faq_id / table_id 是舊版診斷欄，新模型改由 matched_rows 承載 → 略過（會回報筆數）
SQL_LOG = """
INSERT INTO autoreply_log (id, business_id, platform, scope,
                           business_platform_account_id, end_user_sender_id,
                           message_id, tier, question, answer, score, detail,
                           matched_rows, rewrote, rewritten_query, history_used,
                           send_status, elapsed_ms, llm_calls, llm_seconds,
                           claim_key, created_at)
SELECT id, %(biz)s, channel, reply_scope,
       NULL, conversation_key,
       NULLIF(message_id, ''),
       CASE WHEN tier IN ('faq','table','rag','miss','claim','error') THEN tier ELSE 'error' END,
       question, answer, score::real,
       NULLIF(TRIM(COALESCE(detail, '') ||
              CASE WHEN COALESCE(reason, '') = '' THEN '' ELSE ' ｜ ' || reason END), ''),
       matched_rows, rewrote, rewritten_query,
       CASE WHEN COALESCE(history_used, 0) > 0 THEN true ELSE false END,
       send_status, elapsed_ms, COALESCE(llm_calls, 0), llm_seconds::real,
       claim_key, COALESCE(created_at, now())
  FROM {ft_autoreply_log}
"""

# 只有上面這 9 張走 FDW 的 INSERT…SELECT（含 embedding 的兩張靠它省掉來回傳陣列）
FDW_STEPS: tuple[tuple[str, str], ...] = (
    ("kb_collection", SQL_KB_COLLECTION),
    ("kb_chunk", SQL_KB_CHUNK),
    ("kb_job", SQL_KB_JOB),
    ("kb_asset", SQL_KB_ASSET),
    ("kb_asset_chunk", SQL_KB_ASSET_CHUNK),
    ("autoreply_faq", SQL_FAQ),
    ("autoreply_business_table", SQL_TABLE),
    ("autoreply_business_row", SQL_ROW),
    ("autoreply_log", SQL_LOG),
)

# ★ 完整順序（**FK 拓撲序**，不能亂）：
#   kb_collection → kb_document → kb_chunk / kb_job / kb_asset → kb_asset_chunk
#   → kb_param；autoreply_faq → autoreply_business_table → autoreply_business_row。
#   `("py", …)` 是 Python 逐列轉型的兩張（kb_document 要改寫 source_path、
#   kb_param 要 text→jsonb），位置照樣由這串決定。
STEPS: tuple[tuple[str, str], ...] = (
    ("fdw", "kb_collection"),
    ("py", "kb_document"),
    ("fdw", "kb_chunk"),
    ("fdw", "kb_job"),
    ("fdw", "kb_asset"),
    ("fdw", "kb_asset_chunk"),
    ("py", "kb_param"),
    ("fdw", "autoreply_faq"),
    ("fdw", "autoreply_business_table"),
    ("fdw", "autoreply_business_row"),
    ("fdw", "autoreply_log"),
)
_FDW_SQL = dict(FDW_STEPS)


# ======================================================================
# Python 側處理的兩張表（需要逐列轉型）
# ======================================================================

def _rewrite_source_path(raw: str | None, filename: str | None) -> tuple[str | None, bool]:
    """舊 `source_path` 指向**另一台機器**的絕對路徑（`…\\備份\\dsh test\\…`），早就沒了。

    本機檔案其實在 `media/kb/<sha>/<filename>`（`<sha>` = 舊路徑的上一層目錄名）。
    對得上就改寫成本機路徑，對不上就原樣保留（不猜）。
    """
    if not raw:
        return raw, False
    p = Path(raw)
    local = ROOT / "media" / "kb" / p.parent.name / (filename or p.name)
    if local.exists():
        return str(local), True
    return raw, False


def migrate_kb_document(cur, biz: int, dry: bool) -> tuple[int, int]:
    """回傳 (筆數, 改寫 source_path 的筆數)。"""
    cur.execute(f"SELECT * FROM {ft('kb_document')}")  # noqa: S608 - 表名為常數
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    fixed = 0
    if dry:
        for r in rows:
            _, ok = _rewrite_source_path(r.get("source_path"), r.get("filename"))
            fixed += 1 if ok else 0
        return len(rows), fixed

    fields = ("id", "collection_id", "title", "filename", "mime", "size_bytes", "sha256",
              "source_path", "doc_type", "version", "superseded_by", "status", "progress",
              "chunk_count", "char_count", "parse_meta", "parse_params", "error",
              "enabled", "created_at", "updated_at")
    # jsonb 欄位讀回來是 dict/list，psycopg2 不會自動轉 → 必須包 Json()（純量字串照舊）
    jsonb_fields = ("parse_meta", "parse_params")

    def _val(r: dict, f: str):
        v = r.get(f)
        if f in jsonb_fields and isinstance(v, (dict, list)):
            return Json(v)
        return v

    for r in rows:
        sp, ok = _rewrite_source_path(r.get("source_path"), r.get("filename"))
        r["source_path"] = sp
        fixed += 1 if ok else 0
    cur.executemany(
        f"INSERT INTO kb_document ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))})",
        [tuple(_val(r, f) for f in fields) for r in rows],
    )
    return len(rows), fixed


def _param_json_text(raw):
    """`kb_param.value` 舊庫是 TEXT、新庫是 **jsonb** → 必須轉成合法 JSON 文字。

    `'auto'` / 整段提示詞這種不是合法 JSON 的字面量直接寫會
    `InvalidTextRepresentation`。規則同 `kb_db._param_json_text()`：
    先當 JSON 解析（`'600'`→數字、`'true'`→布林），解析不了就包成 JSON 字串。
    """
    if raw is None or isinstance(raw, bool) or isinstance(raw, (int, float)):
        return json.dumps(raw)
    s = str(raw)
    try:
        return json.dumps(json.loads(s), ensure_ascii=False)
    except (ValueError, TypeError):
        return json.dumps(s, ensure_ascii=False)


def migrate_kb_param(cur, dry: bool) -> int:
    cur.execute(f"SELECT collection_id, key, value, updated_at FROM {ft('kb_param')}")
    rows = cur.fetchall()
    if dry:
        return len(rows)
    cur.executemany(
        "INSERT INTO kb_param (collection_id, key, value, updated_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT ON CONSTRAINT kb_param_scope_key"
        " DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        [(r[0], r[1], _param_json_text(r[2]), r[3]) for r in rows],
    )
    return len(rows)


# ======================================================================
# 開關 / 綁定 / 帳號
# ======================================================================

def legacy_settings(cur) -> dict[str, bool]:
    cur.execute(f"SELECT scope_key, enabled FROM {ft('autoreply_setting')}")
    return {r[0]: bool(r[1]) for r in cur.fetchall()}


def legacy_effective_scope(settings: dict[str, bool], platform: str, scope: str) -> bool:
    """舊系統 `effective_scope_mode` 的優先序：渠道+場景 > 渠道 > 全域 > 預設關閉。"""
    for key in (f"{platform}:{scope}", platform, "global"):
        if key in settings:
            return settings[key]
    return False


def migrate_switches(cur, biz: int, settings: dict[str, bool]) -> dict:
    """展開成平台 × 場景的完整集合，並補上三個層開關。"""
    grid: dict[str, bool] = {}
    for plat in PLATFORMS:
        for sc in SCOPES:
            on = legacy_effective_scope(settings, plat, sc)
            grid[f"{plat}:{sc}"] = on
            cur.execute("""
                INSERT INTO business_ai_switch (business_id, platform, scope, enabled, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (business_id, platform, scope)
                DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
            """, (biz, plat, sc, on))

    # 舊層開關沒有預設列時 = 啟用（`business_layer_switch` 無此列也預設啟用，行為一致）
    layers = {lay: bool(settings.get(key, True))
              for lay, key in (("faq", "faq_enabled"), ("table", "table_enabled"), ("rag", "rag_enabled"))}
    for lay, on in layers.items():
        cur.execute("""
            INSERT INTO business_layer_switch (business_id, layer, enabled, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (business_id, layer)
            DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()
        """, (biz, lay, on))
    return {"grid": grid, "layers": layers}


UNGROUPED_LIB_NAME = "未分組文件（遷移）"


def adopt_ungrouped_docs(cur, biz: int, dry: bool = False) -> dict:
    """★ 收養「未分組」文件（`collection_id IS NULL`）。

    舊系統的檢索是**全域**的，未分組文件一樣查得到；新模型只檢索**已綁定**的知識庫
    （`business_kb_binding` 只存 collection id），未分組文件會**徹底消失**。
    這不是設計要的效果，是資料搬家時的坑 —— 所以預設把「ready 且啟用」的未分組文件
    收進一個明講是遷移產物的知識庫，讓它照樣被检索到、也在 UI 裡看得見（要重組隨時可搬）。

    `draft` 的未分組文件不動（本來就檢索不到，留在 UI 的「未分組」視圖等人工處理）。
    """
    cur.execute("SELECT id FROM kb_document"
                " WHERE collection_id IS NULL AND status = 'ready' AND enabled"
                " ORDER BY id")
    ids = [int(r[0]) for r in cur.fetchall()]
    if not ids:
        return {"docs": 0, "collection": None}
    if dry:
        return {"docs": len(ids), "collection": None, "dry": True}

    cur.execute("SELECT id FROM kb_collection WHERE business_id = %s AND name = %s",
                (biz, UNGROUPED_LIB_NAME))
    row = cur.fetchone()
    if row:
        cid = int(row[0])
    else:
        # 直接 SQL INSERT（不經 `kb_db.create_collection`，因為會自動補 binding ——
        # 這裡刻意**不補**，交給後面的 `migrate_bindings` 統一做，綁定清單只有一個來源）
        cur.execute("""
            INSERT INTO kb_collection (business_id, name, description, kind, enabled, sort_order)
            VALUES (%s, %s, %s, 'doc', true, 999) RETURNING id
        """, (biz, UNGROUPED_LIB_NAME,
              "遷移自舊系統的『未分組』文件。舊版檢索是全域的，這些文件也查得到；"
              "新模型只檢索已綁定的知識庫，故收進本庫避免靜默失效。可自由搬去別的庫。"))
        cid = int(cur.fetchone()[0])
    cur.execute("UPDATE kb_document SET collection_id = %s WHERE id = ANY(%s)", (cid, ids))
    return {"docs": len(ids), "collection": cid, "name": UNGROUPED_LIB_NAME}


def migrate_bindings(cur, biz: int) -> dict:
    """★ 把搬過來的每一項綁進基準啟用集（`enabled` 沿用該項自己的值）。

    不做的話依設計案 §9.2「空集 = 不跑」，三層全部沉默。
    """
    out: dict[str, int] = {}
    for name, table, col in (
        ("kb", "business_kb_binding", "kb_collection_id"),
        ("table", "business_table_binding", "business_table_id"),
        ("faq", "business_faq_binding", "faq_id"),
    ):
        cur.execute(sql.SQL("""
            INSERT INTO {bind} (business_id, {col}, enabled)
            SELECT %s, id, COALESCE(enabled, true)
              FROM {src}
             WHERE business_id = %s
            ON CONFLICT (business_id, {col})
            DO UPDATE SET enabled = EXCLUDED.enabled
        """).format(bind=sql.Identifier(table), col=sql.Identifier(col),
                    src=sql.Identifier("kb_collection" if name == "kb"
                                       else "autoreply_business_table" if name == "table"
                                       else "autoreply_faq")),
            (biz, biz))
        out[name] = cur.rowcount
    return out


def seed_platform_accounts(cur, biz: int, envs: dict[str, str], dry: bool) -> list[tuple]:
    """補 `crm_business_platform_account`（兩層 AND 的第二層）。

    ⚠️ `platform_account_enabled()` 對查不到的帳號回 **False** → 沒有這批列，
    所有來訊都會被第二層擋掉。原生 ID 的慣例必須與 Connector webhook 正規化一致。
    """
    rows: list[tuple] = []
    for plat, key in PLATFORM_IDENTITY_KEYS:
        native = (envs.get(key) or "").strip()
        if native:
            rows.append((plat, native, f"{plat}:{key}"))
    if dry:
        return rows
    for plat, native, display in rows:
        cur.execute("""
            INSERT INTO crm_business_platform_account
                (business_id, platform, platform_business_account_id, display_name,
                 ai_auto_reply_enabled, enabled)
            VALUES (%s, %s, %s, %s, true, true)
            ON CONFLICT (platform, platform_business_account_id)
            DO UPDATE SET business_id = EXCLUDED.business_id
        """, (biz, plat, native, display))
    return rows


def resync_sequences(cur) -> None:
    """搬資料時保留了舊 id → bigserial 必須跟上，否則下一筆 INSERT 撞主鍵。"""
    for t in ID_PRESERVING:
        cur.execute(sql.SQL(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'),"
            " GREATEST(COALESCE((SELECT max(id) FROM {t}), 0), 1), true)"
        ).format(t=sql.Identifier(t)), (t,))


# ======================================================================
# 前置檢查 / 清空
# ======================================================================

def resolve_business(cur, name: str) -> int:
    cur.execute("SELECT id FROM crm_business WHERE business_name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"× CRM 找不到商家 {name!r}。先跑：python scripts/init_crm_db.py\n"
            f"  （或用 --business <名稱> 指定既有商家）")
    return int(row[0])


def target_counts(cur) -> dict[str, int]:
    out = {}
    for t in TARGET_TABLES:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t)))
        out[t] = int(cur.fetchone()[0])
    return out


def truncate_all(cur) -> None:
    """清空本腳本會碰的所有表。

    `CASCADE` 只是保險（清單已含所有被參照者）；真的被 cascade 到的表會少於預期時，
    TRUNCATE 會一併處理，不會留孤兒。
    """
    cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
        sql.SQL(", ").join(sql.Identifier(t) for t in TARGET_TABLES)))


# ======================================================================
# 驗證
# ======================================================================

def verify(crm_cur, leg_cur, biz: int, biz_name: str) -> int:
    fails = 0

    def chk(ok: bool, label: str, extra: str = "") -> None:
        nonlocal fails
        if not ok:
            fails += 1
        say(f"  {'PASS' if ok else 'FAIL'}  {label}{('  → ' + extra) if extra else ''}")

    say("\n[1] 行數對帳（來源 vs 目標）")
    # 「收養未分組文件」會多建一個知識庫 → kb_collection 的期望值是來源 +1
    crm_cur.execute("SELECT count(*) FROM kb_collection WHERE business_id = %s AND name = %s",
                    (biz, UNGROUPED_LIB_NAME))
    extra_kb = int(crm_cur.fetchone()[0])
    pairs = (
        ("kb_collection", "SELECT count(*) FROM kb_collection", "SELECT count(*) FROM kb_collection"),
        ("kb_document", "SELECT count(*) FROM kb_document", "SELECT count(*) FROM kb_document"),
        ("kb_chunk", "SELECT count(*) FROM kb_chunk", "SELECT count(*) FROM kb_chunk"),
        ("kb_job", "SELECT count(*) FROM kb_job", "SELECT count(*) FROM kb_job"),
        ("kb_param", "SELECT count(*) FROM kb_param", "SELECT count(*) FROM kb_param"),
        ("kb_asset", "SELECT count(*) FROM kb_asset", "SELECT count(*) FROM kb_asset"),
        ("kb_asset_chunk", "SELECT count(*) FROM kb_asset_chunk", "SELECT count(*) FROM kb_asset_chunk"),
        ("autoreply_faq", "SELECT count(*) FROM autoreply_faq",
         "SELECT count(*) FROM autoreply_faq WHERE business_id = %s"),
        ("autoreply_business_table", "SELECT count(*) FROM autoreply_business_table",
         "SELECT count(*) FROM autoreply_business_table WHERE business_id = %s"),
        ("autoreply_business_row", "SELECT count(*) FROM autoreply_business_row", "SELECT count(*) FROM autoreply_business_row"),
        ("autoreply_log", "SELECT count(*) FROM autoreply_log",
         "SELECT count(*) FROM autoreply_log WHERE business_id = %s"),
    )
    inst = ("autoreply_faq", "autoreply_business_table", "autoreply_log")
    for name, sq, tq in pairs:
        leg_cur.execute(sq)
        src = int(leg_cur.fetchone()[0])
        crm_cur.execute(tq, (biz,) if name in inst else None)
        dst = int(crm_cur.fetchone()[0])
        want = src + (extra_kb if name == "kb_collection" else 0)
        chk(want == dst, f"{name}: {src} → {dst}"
            + (f"（含收養庫 1）" if name == "kb_collection" and extra_kb else ""),
            "" if want == dst else f"期望 {want}")

    say("\n[2] 外鍵孤兒（目標庫內部）")
    orphans = (
        ("kb_document.collection_id → kb_collection",
         "SELECT count(*) FROM kb_document d WHERE d.collection_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM kb_collection c WHERE c.id = d.collection_id)"),
        ("kb_chunk.doc_id → kb_document",
         "SELECT count(*) FROM kb_chunk x WHERE x.doc_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM kb_document d WHERE d.id = x.doc_id)"),
        ("kb_job.doc_id → kb_document",
         "SELECT count(*) FROM kb_job j WHERE j.doc_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM kb_document d WHERE d.id = j.doc_id)"),
        ("kb_asset.doc_id → kb_document",
         "SELECT count(*) FROM kb_asset a WHERE a.doc_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM kb_document d WHERE d.id = a.doc_id)"),
        ("kb_asset_chunk → kb_asset / kb_chunk",
         "SELECT count(*) FROM kb_asset_chunk ac"
         " WHERE NOT EXISTS (SELECT 1 FROM kb_asset a WHERE a.id = ac.asset_id)"
         "    OR NOT EXISTS (SELECT 1 FROM kb_chunk c WHERE c.id = ac.chunk_id)"),
        ("autoreply_business_row.table_id → autoreply_business_table",
         "SELECT count(*) FROM autoreply_business_row r WHERE r.table_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM autoreply_business_table t WHERE t.id = r.table_id)"),
        ("kb_param.collection_id → kb_collection",
         "SELECT count(*) FROM kb_param p WHERE p.collection_id IS NOT NULL"
         "  AND NOT EXISTS (SELECT 1 FROM kb_collection c WHERE c.id = p.collection_id)"),
    )
    for label, q in orphans:
        crm_cur.execute(q)
        n = int(crm_cur.fetchone()[0])
        chk(n == 0, label, f"{n} 筆孤兒")

    say("\n[3] 關鍵欄位轉型（舊庫 text / int → 新庫 jsonb / bool）")
    crm_cur.execute("SELECT count(*) FROM kb_param WHERE value IS NULL")
    chk(int(crm_cur.fetchone()[0]) == 0, "kb_param.value 全部可解成 jsonb（無 NULL）")
    crm_cur.execute("SELECT jsonb_typeof(value), count(*) FROM kb_param GROUP BY 1 ORDER BY 1")
    say("       jsonb 型別分佈：" + ", ".join(f"{a}×{b}" for a, b in crm_cur.fetchall()))
    crm_cur.execute("SELECT value FROM kb_param WHERE key = 'chunk_strategy'")
    row = crm_cur.fetchone()
    chk(row is None or row[0] == "auto",
          "kb_param.chunk_strategy 讀得回 'auto'（不是空、不是 '\"auto\"'）",
          f"實得 {row[0]!r}" if row else "（舊庫無此列）")
    crm_cur.execute("SELECT count(*) FROM autoreply_log WHERE history_used IS NULL")
    n = int(crm_cur.fetchone()[0])
    leg_cur.execute("SELECT count(*) FROM autoreply_log WHERE history_used IS NULL")
    m = int(leg_cur.fetchone()[0])
    chk(n == m, f"autoreply_log.history_used 無意外 NULL（目標 {n} / 來源 {m}）")
    crm_cur.execute("SELECT count(*) FROM autoreply_log WHERE tier = 'error'")
    say(f"       tier='error'（欄位對不上而映射）筆數：{int(crm_cur.fetchone()[0])}")

    say("\n[4] 開關（第一層 + 層開關）")
    crm_cur.execute("SELECT platform, scope, enabled FROM business_ai_switch"
                    " WHERE business_id = %s ORDER BY platform, scope", (biz,))
    grid_rows = crm_cur.fetchall()
    chk(len(grid_rows) == len(PLATFORMS) * len(SCOPES),
        f"business_ai_switch 展開 {len(PLATFORMS)}×{len(SCOPES)} 格", f"實得 {len(grid_rows)}")
    for p, s, on in grid_rows:
        say(f"       {p}:{s} = {'開' if on else '關'}")
    crm_cur.execute("SELECT layer, enabled FROM business_layer_switch"
                    " WHERE business_id = %s ORDER BY layer", (biz,))
    lay = crm_cur.fetchall()
    chk(len(lay) == 3, "business_layer_switch 三層齊備", f"實得 {dict(lay)}")
    say("       " + " / ".join(f"{a}={'開' if b else '關'}" for a, b in lay))

    say("\n[5] 基準啟用集（★ 空集 = 不跑）")
    for kind, table, src in (
        ("kb", "business_kb_binding", "kb_collection"),
        ("table", "business_table_binding", "autoreply_business_table"),
        ("faq", "business_faq_binding", "autoreply_faq"),
    ):
        crm_cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE business_id = %s").format(
            sql.Identifier(table)), (biz,))
        bound = int(crm_cur.fetchone()[0])
        crm_cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE business_id = %s").format(
            sql.Identifier(src)), (biz,))
        total = int(crm_cur.fetchone()[0])
        chk(total == 0 or bound > 0, f"{kind}: {bound} 項已綁 / 共 {total} 項"
            + ("（無資料，允許空）" if total == 0 else ""))

    say("\n[5b] 未分組文件是否已收養（否則新模型下檢索不到）")
    leg_cur.execute("SELECT count(*) FROM kb_document"
                    " WHERE collection_id IS NULL AND status = 'ready' AND enabled")
    src_ug = int(leg_cur.fetchone()[0])
    crm_cur.execute("SELECT count(*) FROM kb_document"
                    " WHERE collection_id IS NULL AND status = 'ready' AND enabled")
    dst_ug = int(crm_cur.fetchone()[0])
    chk(src_ug == 0 or dst_ug == 0,
        f"ready 未分組文件已收進知識庫（舊庫 {src_ug} → 目標殘留 {dst_ug}）",
        "跑時加了 --no-adopt-ungrouped？這些文件不會被檢索到")

    say("\n[6] 第二層開關（平台帳號）")
    crm_cur.execute("SELECT platform, platform_business_account_id, ai_auto_reply_enabled, enabled"
                    " FROM crm_business_platform_account WHERE business_id = %s"
                    " ORDER BY platform", (biz,))
    acc = crm_cur.fetchall()
    chk(bool(acc), "crm_business_platform_account 已有列（否則兩層 AND 全擋）",
        f"{len(acc)} 列")
    for p, n, ai, en in acc:
        say(f"       {p} / {n} → ai_reply={ai} enabled={en}")

    say("\n[7] 檢索可用性（embedding 是否真的帶過來）")
    crm_cur.execute("SELECT count(*) FROM kb_chunk"
                    " WHERE embedding_status = 'done' AND embedding IS NOT NULL")
    n_ok = int(crm_cur.fetchone()[0])
    chk(n_ok > 0, f"kb_chunk 有 embedding_status='done' 且帶向量（{n_ok} 筆）")
    crm_cur.execute("SELECT count(*) FROM kb_chunk WHERE embedding IS NOT NULL")
    n_chunks = int(crm_cur.fetchone()[0])
    leg_cur.execute("SELECT count(*) FROM kb_chunk WHERE embedding IS NOT NULL")
    n_src = int(leg_cur.fetchone()[0])
    chk(n_chunks == n_src, f"帶向量的 chunk 數一致（來源 {n_src} → 目標 {n_chunks}）")
    crm_cur.execute("SELECT count(*) FROM kb_asset WHERE embedding IS NOT NULL")
    n_a = int(crm_cur.fetchone()[0])
    leg_cur.execute("SELECT count(*) FROM kb_asset WHERE embedding IS NOT NULL")
    n_as = int(leg_cur.fetchone()[0])
    chk(n_a == n_as, f"帶向量的 asset 數一致（來源 {n_as} → 目標 {n_a}）")

    say("\n[8] 媒體檔案（附圖要能發出去）")
    crm_cur.execute("SELECT count(*) FROM kb_asset WHERE rel_path IS NOT NULL")
    n_rel = int(crm_cur.fetchone()[0])
    media_dir = Path(crm_config.RAG_MEDIA_DIR)
    if not media_dir.is_absolute():
        media_dir = ROOT / "crm" / media_dir
    missing = 0
    if n_rel:
        crm_cur.execute("SELECT rel_path FROM kb_asset WHERE rel_path IS NOT NULL"
                        " AND collection_id IN (SELECT id FROM kb_collection WHERE business_id = %s)",
                        (biz,))
        for (rel,) in crm_cur.fetchall():
            if not (media_dir / rel).exists():
                missing += 1
    chk(n_rel == 0 or missing == 0,
        f"kb_asset 相對路徑檔案齊備（{media_dir}）",
        f"{missing} 個找不到 → 跑 --copy-media")

    say(f"\n結果：{'全部通過' if not fails else str(fails) + ' 項未通過'}｜商家 {biz_name} (id={biz})")
    return fails


# ======================================================================
# main
# ======================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default=DEFAULT_BUSINESS, help=f"目標商家名（預設 {DEFAULT_BUSINESS}）")
    ap.add_argument("--dry-run", action="store_true", help="只報告，不寫 DB、不建 FDW")
    ap.add_argument("--verify", action="store_true", help="只做搬後對帳")
    ap.add_argument("--force", action="store_true", help="目標表非空時先清空再搬（會印警告）")
    ap.add_argument("--no-accounts", action="store_true", help="不補 crm_business_platform_account")
    ap.add_argument("--no-adopt-ungrouped", action="store_true",
                    help="不把『未分組』文件收進知識庫（它們將檢索不到）")
    ap.add_argument("--copy-media", action="store_true", help="順手複製 media/kb_assets → crm/media/")
    args = ap.parse_args()

    src = conn_parts(legacy_dsn(), "platform_forwarder")
    dst = conn_parts(crm_dsn(), "crm_db")
    say(f"來源：{src['dbname']} @ {src['host']}:{src['port']} ({src['user']})")
    say(f"目標：{dst['dbname']} @ {dst['host']}:{dst['port']} ({dst['user']})")
    say(f"商家：{args.business}")

    # ---------- verify ----------
    if args.verify:
        crm, _ = conn(crm_dsn(), "crm_db")
        leg, _ = conn(legacy_dsn(), "platform_forwarder")
        try:
            ccur, lcur = crm.cursor(), leg.cursor()
            biz = resolve_business(ccur, args.business)
            fails = verify(ccur, lcur, biz, args.business)
            print("\n".join(["", "=" * 68, ""]))
            return 1 if fails else 0
        finally:
            crm.close()
            leg.close()

    # ---------- dry-run：兩條普通連線，完全不碰目標庫 ----------
    if args.dry_run:
        crm, _ = conn(crm_dsn(), "crm_db")
        leg, _ = conn(legacy_dsn(), "platform_forwarder")
        try:
            ccur, lcur = crm.cursor(), leg.cursor()
            biz = resolve_business(ccur, args.business)
            say("\n（dry-run：不寫任何東西、不建 FDW）\n")
            say("[1] 來源行數")
            for _k, t in STEPS:
                lcur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t)))
                say(f"       {t:<28} {int(lcur.fetchone()[0])}")

            say("\n[2] 目標表現況（非空會拒絕，除非 --force）")
            counts = target_counts(ccur)
            for t, n in counts.items():
                say(f"       {t:<28} {n}")

            say("\n[3] 開關展開預覽（舊 autoreply_setting → 新 business_ai_switch）")
            lcur.execute("SELECT scope_key, enabled FROM autoreply_setting ORDER BY scope_key")
            settings = {k: bool(v) for k, v in lcur.fetchall()}
            say("       舊鍵：" + ", ".join(f"{k}={v}" for k, v in settings.items()))
            for p in PLATFORMS:
                say(f"       {p:<9}" + "  ".join(
                    f"{s}={'開' if legacy_effective_scope(settings, p, s) else '關'}" for s in SCOPES))
            say("       層開關：" + ", ".join(
                f"{lay}={settings.get(k, True)}"
                for lay, k in (("faq", "faq_enabled"), ("table", "table_enabled"), ("rag", "rag_enabled"))))

            say("\n[4] 會補的平台帳號")
            envs = {**parse_env_file(ROOT / ".env"), **parse_env_file(ROOT / "connector" / ".env")}
            for p, n, d in seed_platform_accounts(ccur, biz, envs, dry=True):
                say(f"       {p:<9} {n}")

            lcur.execute("SELECT id, title FROM kb_document"
                        " WHERE collection_id IS NULL AND status = %s AND enabled ORDER BY id", ("ready",))
            ug = lcur.fetchall()
            say(f"\n[5] 未分組 ready 文件（會被收進 {UNGROUPED_LIB_NAME!r}）：{len(ug)} 份")
            for _i, _t in ug:
                say(f"       #{_i} {_t}")
            lcur.execute("SELECT count(*) FROM kb_document"
                        " WHERE collection_id IS NULL AND status <> %s", ("ready",))
            say(f"       另留在『未分組』的 draft 文件：{int(lcur.fetchone()[0])} 份")
            say("\n[6] 會被綁進基準啟用集的項目數")
            for kind, table, src_t in (("kb", "business_kb_binding", "kb_collection"),
                                       ("table", "business_table_binding", "autoreply_business_table"),
                                       ("faq", "business_faq_binding", "autoreply_faq")):
                lcur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(src_t)))
                say(f"       {kind:<6} {int(lcur.fetchone()[0])} 項 → {table}")

            say("\n[7] 媒體檢查")
            nd = ROOT / "crm" / "media"
            say(f"       crm/media 存在：{nd.exists()}")
            say(f"       來源 media/kb_assets 檔案數：{len(list((ROOT / 'media' / 'kb_assets').glob('*')))}")
            say("\n（dry-run 結束；確認無誤後拿掉 --dry-run 真正執行）")
        finally:
            crm.close()
            leg.close()
        return 0

    # ---------- 真正搬遷 ----------
    crm, _ = conn(crm_dsn(), "crm_db")
    report: dict = {}
    try:
        ccur = crm.cursor()
        biz = resolve_business(ccur, args.business)

        counts = target_counts(ccur)
        dirty = {t: n for t, n in counts.items() if n}
        if dirty and not args.force:
            raise SystemExit(
                "× 目標表已有資料，拒絕覆蓋：\n  "
                + "\n  ".join(f"{t}={n}" for t, n in dirty.items())
                + "\n  確定要重搬就加 --force（會 TRUNCATE 上列 AI 相關表）。")
        if dirty:
            say("\n⚠️  --force：先清空下列表（含其他商家的 AI 資料！）")
            for t, n in dirty.items():
                say(f"       TRUNCATE {t}（{n} 列）")
            truncate_all(ccur)

        say("\n[1] 掛載 postgres_fdw → 來源庫")
        setup_fdw(ccur, src)
        say(f"       server={FDW_SERVER}, schema={FDW_SCHEMA}, 表 {len(FDW_TABLES)} 張")

        say("\n[2] 搬表（依 FK 拓撲序；FDW INSERT…SELECT + 兩張逐列轉型）")
        for kind, name in STEPS:
            if kind == "fdw":
                ccur.execute(_FDW_SQL[name].format(
                    **{f"ft_{t}": ft(t) for t in FDW_TABLES}), {"biz": biz})
                report[name] = ccur.rowcount
                say(f"       {name:<28} {ccur.rowcount}  [fdw]")
            elif name == "kb_document":
                n_doc, n_fixed = migrate_kb_document(ccur, biz, dry=False)
                report["kb_document"] = n_doc
                say(f"       {'kb_document':<28} {n_doc}  [py]"
                    f"（source_path 改寫 {n_fixed} 筆）")
            elif name == "kb_param":
                n_par = migrate_kb_param(ccur, dry=False)
                report["kb_param"] = n_par
                say(f"       {'kb_param':<28} {n_par}  [py]（text → jsonb 轉型）")

        say("\n[3] 收養未分組文件（★ 不然它們在新模型下檢索不到）")
        if args.no_adopt_ungrouped:
            ccur.execute("SELECT count(*) FROM kb_document"
                         " WHERE collection_id IS NULL AND status='ready' AND enabled")
            say(f"       --no-adopt-ungrouped：略過（{int(ccur.fetchone()[0])} 份 ready 未分組文件"
                f"將不會被檢索到）")
        else:
            ad = adopt_ungrouped_docs(ccur, biz)
            if ad["docs"]:
                say(f"       收進知識庫 {ad['name']!r}（id={ad['collection']}）：{ad['docs']} 份文件")
            else:
                say("       沒有 ready 的未分組文件，略過")
        ccur.execute("SELECT count(*) FROM kb_document"
                     " WHERE collection_id IS NULL AND status <> 'ready'")
        say(f"       另留在『未分組』的 draft 文件：{int(ccur.fetchone()[0])} 份（本來就檢索不到）")

        say("\n[4] 開關（business_ai_switch / business_layer_switch）")
        settings = legacy_settings(ccur)
        sw = migrate_switches(ccur, biz, settings)
        for p in PLATFORMS:
            say(f"       {p:<9}" + "  ".join(
                f"{s}={'開' if sw['grid'][f'{p}:{s}'] else '關'}" for s in SCOPES))
        say("       層開關：" + ", ".join(f"{k}={'開' if v else '關'}" for k, v in sw["layers"].items()))

        say("\n[5] 基準啟用集（★ 空集 = 不跑）")
        binds = migrate_bindings(ccur, biz)
        for k, v in binds.items():
            say(f"       business_{k}_binding：{v} 列")

        if args.no_accounts:
            say("\n[6] 平台帳號：--no-accounts，略過")
        else:
            say("\n[6] 平台帳號（兩層 AND 的第二層）")
            envs = {**parse_env_file(ROOT / ".env"), **parse_env_file(ROOT / "connector" / ".env")}
            acc = seed_platform_accounts(ccur, biz, envs, dry=False)
            for p, n, _d in acc:
                say(f"       {p:<9} {n}")
            if not acc:
                say("       ⚠️  .env 沒找到任何渠道身分 → crm_business_platform_account 仍為空。")
                say("           AI 會被第二層擋掉，記得補上（或用 --no-accounts 明確跳過）。")

        say("\n[7] 重設 bigserial 序列（保留舊 id 的副作用）")
        resync_sequences(ccur)
        say("       " + ", ".join(ID_PRESERVING))

        teardown_fdw(ccur)
        crm.commit()
        say("\n[8] 已提交（teardown FDW）")
    except BaseException:
        crm.rollback()
        raise
    finally:
        crm.close()

    if args.copy_media:
        say("\n[9] 複製媒體")
        s = ROOT / "media" / "kb_assets"
        d = ROOT / "crm" / "media" / "kb_assets"
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(s.glob("*")):
            if f.is_file():
                shutil.copy2(f, d / f.name)
                n += 1
        say(f"       {s} → {d}（{n} 個檔案）")
    else:
        say("\n[9] 媒體：未複製。別忘了 `media/kb_assets/` → `crm/media/kb_assets/`"
            "（或加 --copy-media）。")

    say("\n下一步：python scripts/migrate_ai_data.py --verify")
    print("\n".join(["", "=" * 68, ""]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
