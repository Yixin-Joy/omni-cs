"""知識庫的向量化層：本地 embedding 模型（fastembed / onnxruntime）。

設計依據：RAG_DESIGN.md §7、§12。

模型（2026-10-08 定案）：
    jinaai/jina-embeddings-v2-base-zh   640MB / dim 768 / 上限 8192 token
選它的關鍵理由：bge-small-zh 的 512 token 上限會讓「塊長 500 字」被**無聲截斷**。

幾個實作要點（都是踩過的坑）：
  * `HF_ENDPOINT` 必須在 import fastembed **之前**設定（它一被 import 就會載入
    huggingface_hub）。所以 fastembed 一律延後到 `_ensure()` 內才 import。
  * Windows 上 huggingface_hub 有時組裝不出 snapshot（指標檔變 0 位元組），
    但檔案本體已完整下載 → 內建自愈（見 `heal_snapshot_cache`）。
  * 模型快取已存在時預設走**離線**（避免每次啟動都在等外網）；失敗才自動退回線上。
  * onnxruntime 是 CPU 密集：整個服務共用**一個** provider 實例，並用鎖序列化，
    避免多個入庫任務同時搶 12 顆核心把 web 服務拖垮。
"""
from __future__ import annotations

import json as _json
import logging
import os
import shutil
import threading
import urllib.request
from pathlib import Path

from app.rag import rag_config

log = logging.getLogger("wechat-forwarder")

# ⚠️ 必須在 fastembed 之前 —— 見模組說明
os.environ.setdefault("HF_ENDPOINT", rag_config.RAG_HF_ENDPOINT or "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

DEFAULT_BATCH = int(os.getenv("RAG_EMBED_BATCH") or "16")
DEFAULT_THREADS = int(os.getenv("RAG_EMBED_THREADS") or "6")
# auto（預設）= 快取存在就離線；true/1 = 一律離線；false/0 = 一律線上
_OFFLINE_MODE = (os.getenv("RAG_HF_OFFLINE") or "auto").strip().lower()

# 全域序列化鎖：onnxruntime 是 CPU 密集，多任務同時跑只會互相拖慢
INFER_LOCK = threading.RLock()


class EmbedError(Exception):
    """向量化失敗（模型載入不了、檔案損壞、記憶體不足…）。"""


def _model_cached(cache_dir: str, model_name: str) -> bool:
    repo = Path(cache_dir) / f"models--{model_name.replace('/', '--')}"
    snap = repo / "snapshots"
    return snap.is_dir() and any(p.is_dir() for p in snap.iterdir())


def heal_snapshot_cache(model_name: str, cache_dir: str) -> str | None:
    """修復 Windows 上 huggingface_hub 組裝失敗留下的 0 位元組 snapshot 檔。

    症狀（實測）：檔案本體已完整下載進 `blobs/`，但 `snapshots/<rev>/` 下的指標檔
    是 **0 位元組的實體檔**（不是 symlink），於是 fastembed 的 size 校驗判定
    「corrupted」而拒絕載入。

    做法：向 HF API 取檔案清單（含 size）→ 掃描非空 blob 建「size → 真實檔」索引
    → snapshot 裡大小不符的檔，按 size **唯一**配對後硬連結重建。
    """
    endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    try:
        url = f"{endpoint}/api/models/{model_name}?blobs=true"
        with urllib.request.urlopen(url, timeout=20) as r:
            info = _json.loads(r.read().decode())
    except Exception:
        return None

    sizes = {s["rfilename"]: (s.get("size") or 0) for s in info.get("siblings", [])}
    if not sizes:
        return None

    cache = Path(cache_dir)
    repo = cache / f"models--{model_name.replace('/', '--')}"
    rev = info.get("sha")
    snap = repo / "snapshots" / rev if rev else None
    if snap is None or not snap.exists():
        sdir = repo / "snapshots"
        revs = [d for d in sdir.iterdir() if d.is_dir()] if sdir.exists() else []
        if not revs:
            return None
        snap = revs[0]

    by_size: dict[int, list[Path]] = {}
    for base in (cache / "blobs", repo / "blobs"):
        if base.exists():
            for p in base.rglob("*"):
                try:
                    if p.is_file() and p.stat().st_size > 0:
                        by_size.setdefault(p.stat().st_size, []).append(p)
                except OSError:
                    continue

    fixed: list[str] = []
    for rel, size in sizes.items():
        if size <= 0:
            continue
        dst = snap / rel
        try:
            if dst.exists() and dst.stat().st_size == size:
                continue
        except OSError:
            continue
        srcs = by_size.get(size) or []
        if len(srcs) != 1:      # 有歧義就不動，寧可少修不要修錯
            continue
        try:
            if dst.exists():
                os.remove(dst)
            try:
                os.link(srcs[0], dst)
            except OSError:
                shutil.copyfile(srcs[0], dst)
            fixed.append(rel)
        except OSError:
            continue

    return f"重建 {len(fixed)} 檔（{', '.join(fixed[:4])}{'…' if len(fixed) > 4 else ''}）" if fixed else None


class FastEmbedProvider:
    """fastembed 的薄封裝。介面刻意保持極簡，方便日後換 OpenAI 相容 / Ollama。"""

    kind = "fastembed"

    def __init__(self, model_name: str, cache_dir: str | None = None,
                 threads: int = DEFAULT_THREADS):
        self.model_name = model_name
        self.cache_dir = cache_dir or rag_config.RAG_MODEL_DIR
        self.threads = threads
        self._model = None
        self._dim: int | None = None
        self._offline_set_by_us = False

    # ---------- 載入 ----------

    def _load_once(self):
        from fastembed import TextEmbedding
        return TextEmbedding(model_name=self.model_name, cache_dir=self.cache_dir,
                             threads=self.threads)

    def _ensure(self):
        if self._model is not None:
            return self._model

        cache_ok = _model_cached(self.cache_dir, self.model_name)
        if _OFFLINE_MODE in ("auto", "true", "1", "yes", "on") and cache_ok:
            if not os.environ.get("HF_HUB_OFFLINE"):
                os.environ["HF_HUB_OFFLINE"] = "1"
                self._offline_set_by_us = True

        with INFER_LOCK:
            if self._model is not None:
                return self._model
            try:
                self._model = self._load_once()
            except Exception as e1:
                log.warning("embedding 模型載入失敗（%s），嘗試快取自愈…", type(e1).__name__)
                healed = heal_snapshot_cache(self.model_name, self.cache_dir)
                if healed:
                    log.info("模型快取已修復：%s", healed)
                try:
                    self._model = self._load_once()
                except Exception as e2:
                    # 離線是我們自己設的 → 收回，改走線上重試一次
                    if self._offline_set_by_us:
                        os.environ.pop("HF_HUB_OFFLINE", None)
                        self._offline_set_by_us = False
                        log.warning("離線載入失敗，改回線上重試…")
                        try:
                            self._model = self._load_once()
                        except Exception as e3:
                            raise EmbedError(self._friendly(e3)) from e3
                    else:
                        raise EmbedError(self._friendly(e2)) from e2
        log.info("embedding 模型就緒：%s（%s 緒）", self.model_name, self.threads)
        return self._model

    @staticmethod
    def _friendly(err: Exception) -> str:
        msg = f"{type(err).__name__}: {err}"
        hint = ""
        if any(k in msg.lower() for k in ("connection", "timed out", "timeout", "urlopen", "resolve")):
            hint = "（看起來是網路問題，模型需先下載一次；可檢查 RAG_HF_ENDPOINT）"
        elif "corrupt" in msg.lower() or "size" in msg.lower():
            hint = "（模型快取損壞：刪掉 models/fastembed 後重跑一次即可重新下載）"
        elif "memory" in msg.lower():
            hint = "（記憶體不足）"
        return f"embedding 模型載入失敗：{msg}{hint}"

    # ---------- 屬性 ----------

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.encode(["維度探測"])[0])
        return self._dim

    @property
    def max_tokens(self) -> int:
        return int(rag_config.RAG_EMBED_MAX_TOKENS)

    # ---------- 編碼 ----------

    def encode(self, texts: list[str], batch_size: int = DEFAULT_BATCH) -> list[list[float]]:
        """把文字編碼成向量。空的輸入直接回空清單（不觸發模型載入）。"""
        items = [(t or "").strip() or " " for t in (texts or [])]
        if not items:
            return []
        model = self._ensure()
        out: list[list[float]] = []
        with INFER_LOCK:
            for vec in model.embed(items, batch_size=max(1, batch_size)):
                out.append([float(x) for x in vec])
        return out

    def encode_one(self, text: str) -> list[float]:
        """編碼單句（查詢用）。jina-v2 是對稱模型，查詢與文件不需不同前綴。"""
        res = self.encode([text], batch_size=1)
        return res[0] if res else []

    def warmup(self) -> None:
        """預熱：載入模型 + 跑一次推論（避免第一則訊息卡 2 秒）。"""
        self.encode(["預熱"], batch_size=1)


# ---------- 單例管理 ----------

_PROVIDERS: dict[str, FastEmbedProvider] = {}
_P_LOCK = threading.Lock()


def get_provider(model_name: str | None = None) -> FastEmbedProvider:
    name = (model_name or rag_config.RAG_EMBED_MODEL).strip()
    with _P_LOCK:
        p = _PROVIDERS.get(name)
        if p is None:
            p = FastEmbedProvider(name)
            _PROVIDERS[name] = p
        return p


def model_info() -> dict:
    """給 UI 顯示：模型名 / 維度 / 上限 token / 是否已下載。"""
    name = rag_config.RAG_EMBED_MODEL
    return {
        "model": name,
        "dim": rag_config.RAG_EMBED_DIM,
        "max_tokens": rag_config.RAG_EMBED_MAX_TOKENS,
        "cache_dir": rag_config.RAG_MODEL_DIR,
        "downloaded": _model_cached(rag_config.RAG_MODEL_DIR, name),
        "loaded": name in _PROVIDERS and _PROVIDERS[name]._model is not None,
        "offline_mode": _OFFLINE_MODE,
        "threads": DEFAULT_THREADS,
        "batch_size": DEFAULT_BATCH,
    }
