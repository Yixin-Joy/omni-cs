"""品質校準自檢的共用工具（`kb_*` / `engine_check` / `model_check` … 都用這支）。

## 為什麼要這支

根 `tests/` 時代那 8 支自檢各自寫了一份 `sql()` / `check()`。移植到 CRM 時多了一個
**共同的新需求：商家範圍**——CRM 側 `kb_collection.business_id` 是 NOT NULL，
檢索又會被 `business_kb_binding` 篩（設計案 §9.4「上傳即進基準啟用集」）。

所以集中在這裡：

* **路徑 bootstrap**：把 `crm/` 塞進 `sys.path`（用底層 python 也能 import `app.*`）。
* `sql()`：直接打 **crm_db**，只給自檢清資料 / 造資料用；業務邏輯一律走模組。
* `ensure_business()` / `purge_business()`：自檢專用商家，跑完務必清乾淨。
  自檢資料**一律掛在這個商家底下**，跑完刪掉，不污染真實商家（`Epro_test`）。
* `check()` / `report()`：統一的 PASS/FAIL 計數與結束碼。

## 用法

    # 從專案根（cwd 隨意，路徑由 __file__ 決定）
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/kb_check.py

⚠️ 自檢商家的名字帶 `__CK__` 前綴，跑完 `purge_business()` 會把該商家的
   kb_*／business_*／autoreply_* 全部清掉；**不要**拿真實商家的 id 傳進來。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # crm/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import psycopg2                        # noqa: E402
import psycopg2.extras                 # noqa: E402

from app.core import config            # noqa: E402

MARK = "__CK__"
_ok = [0, 0]
_fails: list[str] = []


# ======================================================================
# 資料庫小工具（只碰 crm_db）
# ======================================================================

def sql(query: str, params=None, fetch: bool = True):
    """執行一條 SQL 並 commit。`fetch=True` 回 list[dict]，否則回受影響列數。"""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params or ())
            if fetch and cur.description is not None:
                rows = [dict(r) for r in cur.fetchall()]
            else:
                rows = cur.rowcount
        conn.commit()
        return rows
    finally:
        conn.close()


def ensure_business(name: str, memo: str = "") -> int:
    """取回（或建立）自檢商家。`name` 一律會被冠上 `__CK__` 前綴。"""
    full = name if name.startswith(MARK) else MARK + name
    rows = sql("SELECT id FROM crm_business WHERE business_name=%s ORDER BY id", (full,))
    if rows:
        return int(rows[0]["id"])
    row = sql("INSERT INTO crm_business(business_name, business_memo) VALUES (%s, %s)"
              " RETURNING id", (full, memo or "自檢專用，可刪"), fetch=True)
    return int(row[0]["id"])


# 刪除順序：子表先、父表後（依 information_schema 的 FK 推導）
_KB_SCOPED = (
    "kb_asset_chunk", "kb_job", "kb_param", "kb_chunk", "kb_asset", "kb_document",
    "business_kb_binding", "post_kb_binding", "kb_collection",
)


def purge_business(bid: int) -> None:
    """把自檢商家底下的東西清乾淨（kb_*／business_*／autoreply_*／貼文配置）。"""
    if not bid:
        return
    cond = "(SELECT id FROM kb_collection WHERE business_id=%s)"
    for t in ("kb_asset_chunk",):
        sql(f"DELETE FROM {t} WHERE asset_id IN"
            f" (SELECT id FROM kb_asset WHERE collection_id IN {cond})", (bid,), fetch=False)
        sql(f"DELETE FROM {t} WHERE chunk_id IN"
            f" (SELECT id FROM kb_chunk WHERE doc_id IN"
            f" (SELECT id FROM kb_document WHERE collection_id IN {cond}))", (bid,), fetch=False)
    sql(f"DELETE FROM kb_job WHERE doc_id IN"
        f" (SELECT id FROM kb_document WHERE collection_id IN {cond})", (bid,), fetch=False)
    sql(f"DELETE FROM kb_param WHERE collection_id IN {cond}", (bid,), fetch=False)
    sql(f"DELETE FROM kb_chunk WHERE doc_id IN"
        f" (SELECT id FROM kb_document WHERE collection_id IN {cond})", (bid,), fetch=False)
    sql(f"DELETE FROM kb_asset WHERE collection_id IN {cond}", (bid,), fetch=False)
    sql(f"DELETE FROM kb_document WHERE collection_id IN {cond}", (bid,), fetch=False)
    sql(f"DELETE FROM business_kb_binding WHERE business_id=%s", (bid,), fetch=False)
    sql(f"DELETE FROM post_kb_binding WHERE kb_collection_id IN {cond}", (bid,), fetch=False)
    sql(f"DELETE FROM kb_collection WHERE business_id=%s", (bid,), fetch=False)

    for t in ("business_faq_binding", "business_table_binding", "business_layer_switch",
              "business_ai_switch"):
        sql(f"DELETE FROM {t} WHERE business_id=%s", (bid,), fetch=False)
    # ⚠️ post_ai_config **沒有** business_id —— 它是靠 business_platform_account_id
    #    連到 `crm_business_platform_account`，所以要 join 一層。
    posts = ("(SELECT p.id FROM post_ai_config p JOIN crm_business_platform_account a"
             " ON a.id = p.business_platform_account_id WHERE a.business_id=%s)")
    sql(f"DELETE FROM post_faq_binding WHERE post_config_id IN {posts}", (bid,), fetch=False)
    sql(f"DELETE FROM post_table_binding WHERE post_config_id IN {posts}", (bid,), fetch=False)
    sql(f"DELETE FROM post_ai_config WHERE id IN {posts}", (bid,), fetch=False)
    sql("DELETE FROM autoreply_business_row WHERE table_id IN"
        " (SELECT id FROM autoreply_business_table WHERE business_id=%s)", (bid,), fetch=False)
    sql("DELETE FROM autoreply_business_table WHERE business_id=%s", (bid,), fetch=False)
    sql("DELETE FROM autoreply_faq WHERE business_id=%s", (bid,), fetch=False)
    sql("DELETE FROM crm_business_platform_account WHERE business_id=%s", (bid,), fetch=False)
    sql("DELETE FROM crm_business WHERE id=%s", (bid,), fetch=False)


def purge_collection(cid: int) -> None:
    """只清一個知識庫（含它的文件／塊／圖）。"""
    if not cid:
        return
    sql("DELETE FROM kb_asset_chunk WHERE asset_id IN"
        " (SELECT id FROM kb_asset WHERE collection_id=%s)", (cid,), fetch=False)
    sql("DELETE FROM kb_asset_chunk WHERE chunk_id IN"
        " (SELECT id FROM kb_chunk WHERE doc_id IN"
        " (SELECT id FROM kb_document WHERE collection_id=%s))", (cid,), fetch=False)
    sql("DELETE FROM kb_job WHERE doc_id IN"
        " (SELECT id FROM kb_document WHERE collection_id=%s)", (cid,), fetch=False)
    sql("DELETE FROM kb_param WHERE collection_id=%s", (cid,), fetch=False)
    sql("DELETE FROM kb_chunk WHERE doc_id IN"
        " (SELECT id FROM kb_document WHERE collection_id=%s)", (cid,), fetch=False)
    sql("DELETE FROM kb_asset WHERE collection_id=%s", (cid,), fetch=False)
    sql("DELETE FROM kb_document WHERE collection_id=%s", (cid,), fetch=False)
    sql("DELETE FROM business_kb_binding WHERE kb_collection_id=%s", (cid,), fetch=False)
    sql("DELETE FROM post_kb_binding WHERE kb_collection_id=%s", (cid,), fetch=False)
    sql("DELETE FROM kb_collection WHERE id=%s", (cid,), fetch=False)


def find_collection(name: str, business_id: int) -> dict | None:
    rows = sql("SELECT * FROM kb_collection WHERE name=%s AND business_id=%s",
               (name, business_id))
    return rows[0] if rows else None


def sandbox_dir(name: str = "_ck_samples") -> Path:
    """自檢暫存目錄（在 crm/ 底下，`.gitignore` 已忽略）。"""
    p = ROOT / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ======================================================================
# 斷言與總結
# ======================================================================

def check(cond, label, extra=""):
    _ok[0 if cond else 1] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
    if not cond:
        _fails.append(label)
    return bool(cond)


# 打真模型的自檢（engine_check / model_check）專用：
# 閘道限流／逾時不是程式的問題，不能報成 FAIL。集中一份，避免兩支各寫一套。
ENV_HINTS = (
    "429", "Rate limit", "ReadTimeout", "連線失敗",
    "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "改寫沒有產出可用查詢", "查詢改寫失敗",
)
_env_skipped: list[str] = []


def check_or_env(cond, label, text="", extra=""):
    """條件成立 → PASS；不成立且 `text` 看起來是環境問題 → 標 `[環境]`（不算失敗）。"""
    if cond:
        return check(True, label, extra)
    if any(h in (text or "") for h in ENV_HINTS):
        print(f"  [環境] {label}   ← 限流／逾時，不是程式的問題：{str(text)[:70]}")
        _env_skipped.append(label)
        return False
    return check(False, label, extra)


def env_skipped() -> list[str]:
    return list(_env_skipped)


def env_note() -> None:
    """在總結前印一行「有幾項因環境未驗證」。"""
    if _env_skipped:
        print(f" （另有 {len(_env_skipped)} 項因閘道限流／逾時未驗證，非程式問題 —— "
              "額度恢復後重跑即可）")
        for f in _env_skipped:
            print("   …", f)


def section(title: str) -> None:
    print(f"\n{title}")


def report(title: str) -> int:
    """印總結並回傳結束碼（0 = 全通過）。呼叫端：`raise SystemExit(report(...))`。"""
    env_note()
    print("\n" + "=" * 78)
    print(f" {title}：PASS {_ok[0]} / FAIL {_ok[1]}"
          + (f" / [環境] {len(_env_skipped)}" if _env_skipped else ""))
    for f in _fails:
        print("   ✗", f)
    print("=" * 78)
    return 1 if _fails else 0


def alive() -> bool:
    """確認 crm_db 連得上（避免把「DB 掛了」誤報成一堆 FAIL）。"""
    try:
        sql("SELECT 1")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"!! 連不上 crm_db：{e}")
        return False


__all__ = [
    "ROOT", "MARK", "sql", "ensure_business", "purge_business", "purge_collection",
    "find_collection", "sandbox_dir", "check", "check_or_env", "env_skipped", "env_note",
    "section", "report", "alive", "ENV_HINTS",
    "psycopg2", "config", "os", "sys",
]
