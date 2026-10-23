"""核對 media1 / media2 的設定與檔案是否自洽（部署前預演、部署後驗收都跑這支）。

## 為什麼要有這支

搬 media 時最容易出的不是「搬失敗」，而是「搬成功了但服務讀不到」：

* `KB_CACHE_DIR` 指到 `MEDIA_DIR` 外面 → 圖寫得進去、`resolve()` 永遠找不到（靜默丟失）
* `kb_asset.rel_path` 是**相對 MEDIA_DIR** 的（`kb_assets/<sha16>.png`），
  檔案放錯根 → 知識庫帶圖回覆全部沒圖
* `kb_document.source_path` 存的是**本機 Windows 絕對路徑** → 搬到 Linux 後
  「重新解析 / 重新分塊」直接失敗（本機實測 22/22 筆都是 `C:\\Users\\...`）

這些都不會在啟動時報錯，只會在真正用到時靜默失效。所以先驗，不要等出事再查。

## 不 import 任何 app 模組

`connector/app` 與 `crm/app` **package 都叫 `app`** —— 同一進程 import 兩邊必衝突。
這支只做兩件事：解析兩個 `.env`、查 DB。

## 用法（專案根目錄）

    # 預設：用 crm/.env 的 DATABASE_URL，讀 connector/.env 與 crm/.env
    python scripts/check_media_setup.py

    # 換根目錄（例如在伺服器上的 /srv/multi-cs）
    python scripts/check_media_setup.py --root /srv/multi-cs

    # 只驗路徑不查 DB（DB 還沒還原時用）
    python scripts/check_media_setup.py --no-db

離開碼：0 = 全過；1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    return bool(ok)


# ---------------------------------------------------------------- .env 解析

def read_env(path: Path) -> dict[str, str]:
    """極簡 .env 解析（只取 KEY=VALUE，忽略註解與空行）。

    刻意不用 python-dotenv：這支要能在**沒裝依賴**的環境跑（例如剛建好機器）。
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve(base: Path, value: str) -> str:
    """相對路徑以 `base`（服務根）為基準解析 —— 與 `config._path()` 同語意。"""
    if not value:
        return ""
    p = Path(value)
    return str(p if p.is_absolute() else (base / p).resolve())


def is_under(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    try:
        return os.path.commonpath([os.path.abspath(child), os.path.abspath(parent)]) \
            == os.path.abspath(parent)
    except ValueError:      # Windows 跨磁碟會拋
        return False


# ---------------------------------------------------------------- DB

def db_query(url: str, sql: str) -> list[tuple]:
    import psycopg2       # 延遲 import：--no-db 時不需要
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="",
                    help="monorepo 根（含 connector/ 與 crm/）。預設＝本檔上兩層")
    ap.add_argument("--no-db", action="store_true", help="只驗路徑，不查 DB")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else \
        Path(__file__).resolve().parents[1]
    conn_dir, crm_dir = root / "connector", root / "crm"

    print(f"root      = {root}")
    print(f"connector = {conn_dir}")
    print(f"crm       = {crm_dir}\n")

    # ---------- A. 兩個 .env 存在 ----------
    conn_env = read_env(conn_dir / ".env")
    crm_env = read_env(crm_dir / ".env")
    check("connector/.env 存在", bool(conn_env), str(conn_dir / ".env"))
    check("crm/.env 存在", bool(crm_env), str(crm_dir / ".env"))

    # ---------- B. connector 的兩個路徑 ----------
    m1 = resolve(conn_dir, conn_env.get("MEDIA_DIR", ""))
    cache = resolve(conn_dir, conn_env.get("KB_CACHE_DIR", ""))
    check("connector MEDIA_DIR 已設且是絕對路徑",
          bool(m1) and os.path.isabs(m1), m1 or "（空）")
    check("connector KB_CACHE_DIR 已設且是絕對路徑",
          bool(cache) and os.path.isabs(cache), cache or "（空）")
    check("★ KB_CACHE_DIR 在 MEDIA_DIR 底下（坑 3）",
          is_under(cache, m1), f"cache={cache} / media={m1}")
    check("MEDIA_DIR 目錄存在", os.path.isdir(m1), m1)
    check("MEDIA_DIR 可寫", os.access(m1, os.W_OK) if os.path.isdir(m1) else False, m1)
    check("kb_cache 目錄存在（可為空）", os.path.isdir(cache), cache)
    check("post_cache 目錄存在（可為空）",
          os.path.isdir(os.path.join(m1, "post_cache")),
          os.path.join(m1, "post_cache"))

    # ---------- C. crm 的三個路徑 ----------
    m2 = resolve(crm_dir, crm_env.get("MEDIA_DIR", ""))
    kb = resolve(crm_dir, crm_env.get("RAG_KB_DIR", "") or os.path.join(m2, "kb"))
    assets = resolve(crm_dir, crm_env.get("RAG_MEDIA_DIR", "")
                     or os.path.join(m2, "kb_assets"))
    check("crm MEDIA_DIR 已設且是絕對路徑",
          bool(m2) and os.path.isabs(m2), m2 or "（空）")
    check("★ RAG_KB_DIR 在 crm MEDIA_DIR 底下",
          is_under(kb, m2), f"kb={kb} / media={m2}")
    check("★ RAG_MEDIA_DIR 在 crm MEDIA_DIR 底下",
          is_under(assets, m2), f"assets={assets} / media={m2}")
    check("RAG_MEDIA_DIR 目錄名是 kb_assets（rel_path 前綴要對得上）",
          os.path.basename(assets) == "kb_assets", os.path.basename(assets))
    check("crm MEDIA_DIR 目錄存在", os.path.isdir(m2), m2)
    check("crm MEDIA_DIR 可寫", os.access(m2, os.W_OK) if os.path.isdir(m2) else False, m2)
    check("RAG_KB_DIR 目錄存在", os.path.isdir(kb), kb)
    check("RAG_MEDIA_DIR 目錄存在", os.path.isdir(assets), assets)

    # ---------- D. media1 / media2 不能是同一個根 ----------
    check("media1 與 media2 不是同一目錄",
          os.path.abspath(m1) != os.path.abspath(m2), f"{m1} vs {m2}")

    # ---------- E. media2 底下不該混進 connector 的東西 ----------
    for stray in ("kb_cache", "post_cache"):
        p = os.path.join(m2, stray)
        existed = os.path.isdir(p)
        check(f"media2 沒有殘留 {stray}（應該在 media1）", not existed,
              f"不該存在：{p}" if existed else "未發現（正確）")

    # ---------- F. 檔案數量 ----------
    if os.path.isdir(kb):
        n = len([d for d in os.listdir(kb) if os.path.isdir(os.path.join(kb, d))])
        # 本機基準：19（舊單體）+ 102（CRM）＝121。搬齊了不該更少。
        check("kb/ 子目錄數 ≥ 121（19 舊 + 102 CRM）", n >= 121, f"實際 {n}")
    if os.path.isdir(assets):
        n = len([f for f in os.listdir(assets) if os.path.isfile(os.path.join(assets, f))])
        check("kb_assets/ 檔案數 ≥ 9", n >= 9, f"實際 {n}")

    # ---------- G. DB 對照 ----------
    url = crm_env.get("DATABASE_URL", "")
    if args.no_db:
        print("\n[跳過 DB 檢查（--no-db）]")
    elif not url:
        check("crm DATABASE_URL 已設（DB 檢查需要）", False, "（空）")
    else:
        try:
            rows = db_query(url, "select rel_path from kb_asset where rel_path is not null")
            miss = [r[0] for r in rows
                    if not os.path.isfile(os.path.join(m2, str(r[0]).replace("\\", "/")))]
            check(f"kb_asset 全部 {len(rows)} 筆的 rel_path 都落到實體檔",
                  not miss, "缺：" + ", ".join(miss[:5]) if miss else "全中")

            rows = db_query(url,
                            "select count(*) from kb_document where source_path like 'C:%'")
            win = int(rows[0][0]) if rows else 0
            check("kb_document.source_path 沒有殘留 Windows 絕對路徑", win == 0,
                  f"{win} 筆仍是 C:\\ 開頭（跑 §3.4 的修復 SQL）")

            rows = db_query(url,
                            "select source_path from kb_document where source_path is not null")
            miss = [r[0] for r in rows if not os.path.isfile(str(r[0]))]
            check(f"kb_document 全部 {len(rows)} 筆的 source_path 都落到實體檔",
                  not miss, "缺：" + ", ".join(str(x) for x in miss[:5]) if miss else "全中")
        except Exception as e:      # noqa: BLE001
            check("DB 查詢成功", False, f"{type(e).__name__}: {e}")

    # ---------- 輸出 ----------
    print()
    ok_n = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  →  {detail}" if detail else ""))
    print(f"\n合計 {ok_n}/{len(RESULTS)} 通過")
    if ok_n != len(RESULTS):
        print("有 FAIL —— 先修完再啟動服務，這些問題都不會在啟動時報錯。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
