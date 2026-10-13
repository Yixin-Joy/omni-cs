"""檢索結果的重排序（rerank）介面。

設計依據：RAG_DESIGN.md §7。

**第一版不啟用**（`NoopReranker` 直接回前 N 個），但介面先留好：
  * CPU 上 cross-encoder 每組 (query, chunk) 都要跑一次完整前向，
    top-20 × 500 字估計 **+1~3 秒**；多個客戶同時問會排隊。
  * 要裝時只需 `pip install` 已完成 + 下載模型，然後把開關打開 —— 呼叫端不用改。

之後要裝的模型（已核實 fastembed 的下載來源）：
    BAAI/bge-reranker-v2-m3-int8 → 570MB，多語含中文（最小的可用選項）
"""
from __future__ import annotations

import logging
import os
import time
from typing import Protocol

from app.rag import rag_config

log = logging.getLogger("wechat-forwarder")


class Reranker(Protocol):
    name: str

    def rerank(self, query: str, chunks: list, top_n: int) -> list:
        """回傳重排後的前 top_n 個（chunks 元素為 dict，需含 'text' 欄位）。"""
        ...


class NoopReranker:
    """預設實作：不排序，直接取前 top_n。"""

    name = "noop"

    def rerank(self, query: str, chunks: list, top_n: int) -> list:
        return list(chunks[:top_n])


class FastEmbedCrossEncoderReranker:
    """fastembed 的 cross-encoder reranker（第一版不啟用，程式碼先備好）。

    ⚠️ 首次使用會下載 570MB 模型；`rerank` 內自帶逾時保護 ——
    超過 `RAG_RERANK_TIMEOUT` 秒就放棄重排、直接用原始順序，不讓檢索被拖死。
    """

    name = "fastembed-cross-encoder"

    def __init__(self, model_name: str | None = None, timeout: float | None = None):
        self.model_name = model_name or rag_config.RAG_RERANK_MODEL
        self.timeout = float(timeout or os.getenv("RAG_RERANK_TIMEOUT") or 5.0)
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            self._model = TextCrossEncoder(model_name=self.model_name,
                                           cache_dir=rag_config.RAG_MODEL_DIR)
        return self._model

    def rerank(self, query: str, chunks: list, top_n: int) -> list:
        if not chunks:
            return []
        if not query.strip():
            return list(chunks[:top_n])
        t0 = time.time()
        try:
            model = self._ensure()
            docs = [c.get("text", "") for c in chunks]
            scores = list(model.rerank(query, docs))
        except Exception as e:
            log.warning("rerank 失敗，改用原始順序：%s", e)
            return list(chunks[:top_n])
        if time.time() - t0 > self.timeout:
            log.warning("rerank 逾時（%.1fs），改用原始順序", time.time() - t0)
            return list(chunks[:top_n])
        order = sorted(range(len(chunks)), key=lambda i: -(scores[i] if i < len(scores) else 0.0))
        out = []
        for i in order[:top_n]:
            item = dict(chunks[i])
            if i < len(scores):
                item["rerank_score"] = float(scores[i])
            out.append(item)
        return out


def get_reranker(enabled: bool, model_name: str | None = None) -> Reranker:
    """依參數挑 reranker；任何載入問題都退回 Noop（RAG 層不會整層失效）。"""
    if not enabled:
        return NoopReranker()
    return FastEmbedCrossEncoderReranker(model_name)
