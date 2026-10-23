#!/usr/bin/env python3
"""改寫 connector/.env 與 crm/.env 的部署設定（只動指定鍵，其餘原樣保留）。

★ 為什麼不手改：`.env` 裡有 20+ 個渠道憑證（微信 AppSecret、Meta Page Token…），
  手改容易漏、也容易在貼上時把長 token 弄斷。這支只換「部署相關的 6+6 個鍵」，
  其餘一個字都不碰，而且會先備份。

用法（在伺服器上，**不需要 sudo**）：

    cd ~/multi-cs
    python3 scripts/patch_env.py \
        --db-password '你的新密碼' \
        --internal-token "$(openssl rand -hex 32)" \
        --public-base https://media1.plexusai.app

預設路徑（家目錄版）已寫死為 /home/joyli/...，可用 --root/--media1/--media2 覆蓋。

安全：不印出任何密碼/token 明文，只印「已設定」與長度。
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-password", required=True, help="PostgreSQL joyli 角色的密碼")
    ap.add_argument("--internal-token", required=True, help="兩服務共用的 X-Internal-Token")
    ap.add_argument("--public-base", default="https://media1.plexusai.app",
                    help="connector 的對外基址（Meta 來抓圖用）")
    ap.add_argument("--root", default="/home/joyli/multi-cs")
    ap.add_argument("--media1", default="/home/joyli/media1", help="connector 的 MEDIA_DIR")
    ap.add_argument("--media2", default="/home/joyli/media2", help="crm 的 MEDIA_DIR")
    ap.add_argument("--db-user", default="joyli")
    ap.add_argument("--db-host", default="127.0.0.1")
    ap.add_argument("--yes", action="store_true", help="不詢問，直接寫入")
    return ap.parse_args()


def patch(path: Path, updates: dict[str, str], add_missing: dict[str, str] | None = None) -> list[str]:
    """逐行改寫；只動 `KEY=` 開頭的行（忽略註解）。回傳變更摘要（不含值）。"""
    if not path.is_file():
        raise SystemExit(f"[FAIL] 找不到 {path} —— 先把 .env scp 上來")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{stamp}")
    shutil.copy2(path, backup)

    lines = io.open(path, encoding="utf-8").read().split("\n")
    done: set[str] = set()
    changed: list[str] = []
    out: list[str] = []

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            done.add(key)
            changed.append(key)
        else:
            out.append(line)

    # 補上原本沒有的鍵（例如 RAG_KB_DIR）
    missing = {k: v for k, v in (add_missing or {}).items() if k not in done}
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- 部署時補上（patch_env.py 生成）----")
        for k, v in missing.items():
            out.append(f"{k}={v}")
            changed.append(k + "(新增)")

    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"          # 檔尾保留換行，避免後續 sed/grep 行為怪異
    io.open(path, "w", encoding="utf-8", newline="\n").write(text)
    print(f"[ OK ] {path}")
    print(f"       備份 → {backup.name}")
    for k in changed:
        print(f"       已更新 {k}")
    return changed


def main() -> int:
    a = parse_args()
    root = Path(a.root)
    conn_env = root / "connector" / ".env"
    crm_env = root / "crm" / ".env"

    print("==== 目標 ====")
    print(f"  connector/.env : {conn_env}")
    print(f"  crm/.env       : {crm_env}")
    print(f"  media1         : {a.media1}")
    print(f"  media2         : {a.media2}")
    print(f"  public base    : {a.public_base}")
    print(f"  db user        : {a.db_user}")
    print()

    if not a.yes:
        ans = input("確認寫入？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return 1

    db_conn = f"postgresql://{a.db_user}:{quote(a.db_password, safe='')}@{a.db_host}:5432"
    db_crm = f"postgresql://{a.db_user}:{quote(a.db_password, safe='')}@{a.db_host}:5432"

    print("\n==== connector/.env（→ media1，connector 8010）====")
    patch(conn_env, {
        "DATABASE_URL": f"{db_conn}/platform_forwarder",
        "MEDIA_DIR": a.media1,
        "KB_CACHE_DIR": f"{a.media1}/kb_cache",
        "INTERNAL_API_TOKEN": a.internal_token,
        "PUBLIC_BASE_URL": a.public_base.rstrip("/"),
        "META_VERIFY_SIGNATURE": "true",
    })

    print("\n==== crm/.env（→ media2，crm 8020）====")
    patch(crm_env, {
        "DATABASE_URL": f"{db_crm}/crm_db",
        "INTERNAL_API_TOKEN": a.internal_token,
        "RAG_MODEL_DIR": f"{a.root}/models/fastembed",
        "MEDIA_DIR": a.media2,
        "RAG_MEDIA_DIR": f"{a.media2}/kb_assets",
        "PUBLIC_BASE_URL": "",          # crm 端目前沒有程式讀它
    }, add_missing={
        "RAG_KB_DIR": f"{a.media2}/kb",  # 原本沒設（用預設 media/kb），這裡顯式指定
    })

    print("\n==== 驗證：路徑必須自洽 ====")
    ok = True
    checks = [
        ("connector KB_CACHE_DIR 在 MEDIA_DIR 底下",
         f"{a.media1}/kb_cache".startswith(a.media1)),
        ("crm RAG_KB_DIR 在 MEDIA_DIR 底下", f"{a.media2}/kb".startswith(a.media2)),
        ("crm RAG_MEDIA_DIR 在 MEDIA_DIR 底下", f"{a.media2}/kb_assets".startswith(a.media2)),
        ("media1 != media2", a.media1.rstrip("/") != a.media2.rstrip("/")),
        ("internal token 長度 >= 32", len(a.internal_token) >= 32),
    ]
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n★ 密碼與 token 不顯示明文。若要核對長度：")
    print(f"  db password 長度 = {len(a.db_password)}")
    print(f"  internal token 長度 = {len(a.internal_token)}")
    print()
    print("下一步：sudo bash scripts/server_apply.sh   # 裝 PG + 建庫 + 還原")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
