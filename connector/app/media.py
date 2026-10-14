"""Connector 的媒體實體存儲。

- 客戶來訊媒體：永久（`media/`，由 webhook 側寫入 —— 階段三搬入）。
- 知識庫圖片**發送快取**：`media/kb_cache/`，sha256 去重，7 天清理（設計案 §12.2）。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from app.core import config

_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
SCOPES = ("kb_cache", "outbound")


def _scope_dirs() -> dict[str, Path]:
    return {
        "kb_cache": Path(config.KB_CACHE_DIR),
        "outbound": Path(config.MEDIA_DIR) / "outbound",
    }


def scope_dir(scope: str) -> Path:
    d = _scope_dirs().get(scope)
    if d is None:
        raise ValueError(f"不支援的 scope: {scope}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_ext(filename: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext if _EXT_RE.match(ext) else ""


def save_bytes(scope: str, data: bytes, filename: str = "") -> dict:
    """存檔（sha256 去重），回傳 media_key / sha256 / size。"""
    sha = hashlib.sha256(data).hexdigest()
    name = sha[:16] + safe_ext(filename)
    path = scope_dir(scope) / name
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)          # 原子落盤，避免半截檔案被讀到
    return {"media_key": f"{scope}/{name}", "sha256": sha, "size": len(data),
            "path": str(path)}


def resolve(media_key: str) -> Path:
    """把 media_key 解析成絕對路徑 —— ★ 擋路徑穿越（`..` / 絕對路徑）。"""
    key = (media_key or "").replace("\\", "/").strip()
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ValueError("不合法的 media_key")
    root = Path(config.MEDIA_DIR).resolve()
    p = (root / key).resolve()
    if root != p and root not in p.parents:
        raise ValueError("不合法的 media_key")
    if not p.is_file():
        raise FileNotFoundError(media_key)
    return p


def public_url(media_key: str, base_url: str | None = None) -> str:
    """`media_key` → **外網可達的絕對網址**（給 IG／FB 用）。

    為什麼 IG／FB 一定要絕對網址：它們的 attachment 只收 `payload.url`，
    由 **Meta 的伺服器自己去下載**那個檔案 —— 所以網址必須外網可達、不需登入。

    路徑用 `/files/<media_key>`（Connector 的 StaticFiles 掛的就是 MEDIA_DIR），
    而 ngrok 隧道指向 Connector → 這條路徑外網真的抓得到
    （CRM 端的 `/files` 只是**代理**給站內頁面用）。

    沒設 `PUBLIC_BASE_URL` 就回空字串 —— 呼叫端要**記下原因**，不要靜靜略過。
    """
    key = (media_key or "").replace("\\", "/").strip().lstrip("/")
    if not key or ".." in key.split("/"):
        return ""
    base = (base_url if base_url is not None else getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/files/{key}"


def find_in_kb_cache(sha256: str) -> Path | None:
    """發送前先看快取有沒有（避免重複上傳）。"""
    d = scope_dir("kb_cache")
    for f in d.glob(f"{sha256[:16]}*"):
        if f.is_file() and not f.name.endswith(".tmp"):
            return f
    return None


def purge_kb_cache(ttl_days: int | None = None) -> int:
    """清理超過 TTL 的知識庫圖片發送快取（設計案 §12.2：7 天）。"""
    days = int(ttl_days if ttl_days is not None else config.KB_CACHE_TTL_DAYS)
    cutoff = time.time() - days * 86400
    n = 0
    d = scope_dir("kb_cache")
    for f in d.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            n += 1
    return n
