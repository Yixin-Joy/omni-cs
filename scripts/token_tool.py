"""Meta 權杖工具：檢查 / 延長 / 衍生粉專權杖。

背景：直接從 Graph API Explorer 複製的權杖是短效的（1～2 小時）。
要做成「永不過期的粉專權杖」，必須先把短效 user token 換成長效（60 天），
再用它從 /me/accounts 取出粉專權杖。

用法：
  python token_tool.py check              檢查目前 .env 內兩顆權杖的狀態
  python token_tool.py extend <短效USER>  短效 user token → 長效，寫回 .env
  python token_tool.py derive             用 .env 的長效 user token 取粉專權杖
  python token_tool.py renew <短效USER>   extend + derive 一次做完（建議用這個）
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 專案根

import httpx

from app.core import config

VER = config.META_GRAPH_VERSION or "v21.0"
GRAPH = "https://graph.facebook.com"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


# ---------- .env 讀寫（每次直接讀檔，避免拿到程式啟動時的快照） ----------

def read_env(key: str) -> str:
    try:
        for ln in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def write_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.startswith(key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------- Meta 操作 ----------

def debug_token(token: str) -> dict:
    r = httpx.get(f"{GRAPH}/debug_token",
                  params={"input_token": token, "access_token": token}, timeout=30)
    return (r.json() or {}).get("data") or {"error": (r.json() or {}).get("error")}


def extend_user_token(short_token: str) -> str:
    """短效 user token → 長效（約 60 天）。"""
    r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token",
        "client_id": read_env("META_APP_ID") or config.META_APP_ID,
        "client_secret": read_env("META_APP_SECRET") or config.META_APP_SECRET,
        "fb_exchange_token": short_token,
    })
    data = r.json() or {}
    if not data.get("access_token"):
        raise RuntimeError(f"延長失敗：{json.dumps(data, ensure_ascii=False)[:300]}")
    return data["access_token"]


def derive_page_tokens(long_user_token: str) -> list[dict]:
    """用長效 user token 取所有粉專的權杖（這些粉專權杖不會過期）。"""
    r = httpx.get(f"{GRAPH}/{VER}/me/accounts",
                  params={"access_token": long_user_token}, timeout=30)
    data = r.json() or {}
    if "error" in data:
        raise RuntimeError(f"取粉專清單失敗：{json.dumps(data['error'], ensure_ascii=False)[:300]}")
    return data.get("data", [])


# ---------- 子命令 ----------

def cmd_check() -> None:
    for key in ("META_USER_ACCESS_TOKEN", "META_PAGE_ACCESS_TOKEN", "META_IG_ACCESS_TOKEN"):
        tok = read_env(key) or getattr(config, key, "")
        print(f"[{key}]")
        if not tok:
            print("  未設定\n")
            continue
        if key == "META_IG_ACCESS_TOKEN":
            r = httpx.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                          params={"grant_type": "ig_refresh_token", "access_token": tok})
            j = r.json() or {}
            ok = bool(j.get("access_token"))
            days = round((j.get("expires_in") or 0) / 86400, 1) if ok else "-"
            print(f"  可用={ok} 效期約 {days} 天（IG 登入權杖；本專案會自動續期）\n")
            continue
        d = debug_token(tok)
        if d.get("error"):
            print(f"  ✗ {json.dumps(d['error'], ensure_ascii=False)[:200]}\n")
            continue
        exp = d.get("expires_at")
        life = "永不過期" if exp == 0 else f"到期時間戳 {exp}"
        print(f"  有效={d.get('is_valid')} 型態={d.get('type')} {life}\n")


def cmd_extend(short_token: str) -> None:
    long_tok = extend_user_token(short_token)
    write_env("META_USER_ACCESS_TOKEN", long_tok)
    print("✓ 已換成長效 user token 並寫回 .env（META_USER_ACCESS_TOKEN）")


def cmd_derive() -> None:
    long_tok = read_env("META_USER_ACCESS_TOKEN")   # 直接讀檔，不用快照
    if not long_tok:
        raise RuntimeError("META_USER_ACCESS_TOKEN 為空，請先執行 extend")
    pages = derive_page_tokens(long_tok)
    if not pages:
        raise RuntimeError("查不到任何粉專，請確認帳號是該粉專的管理員、且 token 有 pages_show_list 權限")
    target = read_env("META_PAGE_ID")
    print(f"共 {len(pages)} 個粉專：")
    chosen = None
    for p in pages:
        mark = ""
        if p.get("id") == target:
            chosen = p
            mark = "  ← 本專案用的粉專"
        print(f"  {p.get('name')}  id={p.get('id')}{mark}")
    if chosen is None:
        chosen = pages[0]
        print(f"（未比對到 META_PAGE_ID，改用第一個：{chosen.get('name')}）")
    write_env("META_PAGE_ACCESS_TOKEN", chosen["access_token"])
    print("✓ 已寫回 .env（META_PAGE_ACCESS_TOKEN）；這顆粉專權杖不會過期")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in ("check", "extend", "derive", "renew"):
        print(__doc__)
        return
    cmd = args[0]
    try:
        if cmd == "check":
            cmd_check()
        elif cmd == "extend":
            if len(args) < 2:
                raise RuntimeError("用法：python token_tool.py extend <短效USER_TOKEN>")
            cmd_extend(args[1])
        elif cmd == "derive":
            cmd_derive()
        elif cmd == "renew":
            if len(args) < 2:
                raise RuntimeError("用法：python token_tool.py renew <短效USER_TOKEN>")
            cmd_extend(args[1])
            cmd_derive()
            print("\n完成。請重啟服務讓新的權杖生效。")
    except Exception as e:
        print(f"錯誤：{e}")


if __name__ == "__main__":
    main()
