#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RAG 本地模型自檢（embedding 載入 + 編碼 + 檢索召回），**CRM 版**。

## 與根 `tests/rag_model_check.py` 的差別

| | 根版（單體） | 這版（CRM） |
|---|---|---|
| 模型從哪來 | 自己 `TextEmbedding(model_name=..., cache_dir=<root>/models/fastembed)` | **走 `kb_embed.get_provider()`** —— 就是服務真正用的那條路 |
| 參數 | 讀 `os.getenv` + 寫死預設 | 讀 `app.rag.rag_config`（與服務同一份 .env） |
| 快取自愈 | 自帶一份 `_heal_snapshot_cache`（70 行） | 直接叫 **`kb_embed.heal_snapshot_cache`**（同一份實作，不再各寫一遍） |
| 統計輸出 | 自己的 `results` list | 共用 `_ck.check / _ck.report` |
| 新增 | — | `[0]` CRM 設定對齊（`rag_config` ↔ `kb_embed.model_info()` ↔ 實際載入的模型） |

⚠️ 這支**不碰 DB**（純模型層），所以不需要自檢商家。

用法（與其他自檢相同，cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/rag_model_check.py [-q] [-m 模型名] [-t 執行緒數]

    -q  只測「能否載入 + 編碼一句」（首次會下載模型 ~640MB）
    -m  臨時換模型（會提示這不是 CRM 設定裡的模型）
    -t  覆寫 onnxruntime 執行緒數（預設走 CRM 設定 `RAG_EMBED_THREADS`）
"""
import argparse
import os
import sys
import time

from _ck import check, report  # noqa: E402   ← 這行就把 crm/ 塞進 sys.path 了

from app.rag import kb_embed       # noqa: E402
from app.rag import rag_config     # noqa: E402

# ⚠️ 必須在 import fastembed 之前設定（它一被 import 就會載入 huggingface_hub）。
#    kb_embed 模組頂層已經做過同一件事，這裡是為了「本腳本獨立可跑」再保險一次。
os.environ.setdefault("HF_ENDPOINT", rag_config.RAG_HF_ENDPOINT or "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

CONF_MODEL = rag_config.RAG_EMBED_MODEL
CONF_DIM = rag_config.RAG_EMBED_DIM
CONF_DIR = rag_config.RAG_MODEL_DIR

# 迷你知識庫：塊 → 查詢 → 期望命中的塊索引集合
MINI_CHUNKS = [
    "A 款藍牙耳機的價格是 1280 元，續航 30 小時，支援藍牙 5.3。",
    "B 款藍牙耳機的價格是 890 元，支援主動降噪（ANC）。",
    "退貨政策：商品到貨後 7 天內，包裝完整可無理由退貨。",
    "運費說明：單筆訂單滿 500 元免運費，未滿則收 60 元運費。",
    "保固條款：電子產品自購買日起提供一年原廠保固。",
    "客服時間：週一至週五 09:00–18:00，國定假日休息。",
]
MINI_QUERIES = [
    ("藍牙耳機多少錢", {0, 1}),
    ("不想要了可以退嗎", {2}),
    ("運費怎麼算", {3}),
    ("保固多久", {4}),
]


def _cosine_scores(qvec, mat):
    """回傳 (排序後的索引 list, 對應分數 list)。純 numpy，不依賴 DB。"""
    import numpy as np
    a = np.asarray(mat, dtype="float32")
    q = np.asarray(qvec, dtype="float32")
    a = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    q = q / (np.linalg.norm(q) + 1e-9)
    s = a @ q
    order = list(np.argsort(-s))
    return order, [float(s[i]) for i in order]


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 本地模型自檢（CRM 版）")
    ap.add_argument("-q", "--quick", action="store_true", help="只測能否載入與編碼")
    ap.add_argument("-m", "--model", default=CONF_MODEL,
                    help=f"模型名（預設走 CRM 設定：{CONF_MODEL}）")
    ap.add_argument("-t", "--threads", type=int, default=None,
                    help="onnxruntime 執行緒數（預設走 CRM 設定 RAG_EMBED_THREADS）")
    args = ap.parse_args()

    info = kb_embed.model_info()

    print("=" * 78)
    print(" RAG 本地模型自檢（CRM 版）")
    print("=" * 78)
    print(f"  HF_ENDPOINT  : {os.environ['HF_ENDPOINT']}")
    print(f"  模型         : {args.model}")
    print(f"  快取目錄     : {CONF_DIR}")
    print(f"  執行緒       : {args.threads if args.threads is not None else info['threads']}"
          f"（CRM 設定 RAG_EMBED_THREADS）")
    print("-" * 78)

    def _give_up(err):
        check(False, "模型載入失敗", f"{type(err).__name__}: {err}")
        print("\n>>> 載入失敗。常見原因：")
        print("    · 網路／鏡像不通 → 檢查 RAG_HF_ENDPOINT、或先 curl 測 https://hf-mirror.com")
        print("    · 磁碟不足       → 模型約 640MB（含暫存約 1GB）")
        print(f"    · 快取組裝異常   → 刪掉 {CONF_DIR} 整個重跑本腳本")
        return report("RAG 本地模型自檢（CRM 版）")

    def _load_provider():
        if args.threads is not None:
            return kb_embed.FastEmbedProvider(args.model, threads=args.threads)
        return kb_embed.get_provider(args.model)

    # ── 0) CRM 設定對齊（這版新增）───────────────────────────────
    print("\n[0] CRM 設定對齊（rag_config ↔ kb_embed ↔ 實際模型）")
    check(os.path.isabs(CONF_DIR), "RAG_MODEL_DIR 是絕對路徑（不受 cwd 影響）", CONF_DIR)
    check(CONF_DIM > 0 and kb_embed.model_info()["dim"] == CONF_DIM,
          f"rag_config 與 kb_embed.model_info() 的維度一致（{CONF_DIM}）")
    check(kb_embed.model_info()["max_tokens"] == rag_config.RAG_EMBED_MAX_TOKENS,
          f"最大 token 上限一致（{rag_config.RAG_EMBED_MAX_TOKENS}）"
          " —— 塊長要靠它才不會被無聲截斷")
    downloaded = kb_embed._model_cached(CONF_DIR, CONF_MODEL)
    check(True, f"模型快取{'已存在（可離線啟動）' if downloaded else '不存在（這次會下載）'}",
          f"{CONF_MODEL} @ {CONF_DIR}")
    check(args.model == CONF_MODEL,
          "本次測的就是 CRM 設定的那個模型",
          "" if args.model == CONF_MODEL
          else f"⚠️ 你測的是 {args.model}，服務跑的是 {CONF_MODEL}")

    # ── 1) 載入模型（走服務那條路；首次會下載）──────────────────
    print("\n[1] 載入模型（首次執行會下載 ~640MB，請耐心等）…")
    t0 = time.time()
    prov = _load_provider()
    try:
        prov._ensure()
        check(True, "模型載入成功（經 kb_embed，與服務同一條路）",
              f"耗時 {time.time() - t0:.1f}s")
    except Exception as e:
        # kb_embed 內部已經會自愈一次；到這裡就是真的不行了。
        print(f"  載入失敗（{type(e).__name__}），最後一次嚐試快取自愈…")
        msg = kb_embed.heal_snapshot_cache(args.model, CONF_DIR)
        if not msg:
            return _give_up(e)
        print(f"  已修復：{msg}")
        try:
            prov._model = None
            prov._ensure()
            check(True, "模型載入成功（經快取自愈）", f"耗時 {time.time() - t0:.1f}s")
        except Exception as e2:
            return _give_up(e2)

    # ── 2) 編碼一句，驗維度與延遲 ───────────────────────────────
    print("\n[2] 編碼測試（單句）…")
    t0 = time.time()
    vec = prov.encode_one("藍牙耳機多少錢？")
    dt0 = time.time() - t0
    dim = len(vec)
    check(dim == CONF_DIM, f"向量維度 = {dim}", f"（預期 {CONF_DIM}）單句耗時 {dt0:.3f}s")

    if args.quick:
        return report("RAG 本地模型自檢（CRM 版，-q 快速模式）")

    # ── 3) 編碼吞吐 ─────────────────────────────────────────────
    print("\n[3] 編碼吞吐（迷你知識庫 6 塊）…")
    t0 = time.time()
    chunk_vecs = prov.encode(MINI_CHUNKS, batch_size=len(MINI_CHUNKS))
    dt = time.time() - t0
    per = dt / len(MINI_CHUNKS)
    check(True, f"{len(MINI_CHUNKS)} 塊耗時 {dt:.2f}s", f"約 {per * 1000:.0f} ms/塊")

    # ── 4) 檢索自檢 ─────────────────────────────────────────────
    print("\n[4] 檢索自檢（語義召回，cosine）…")
    qvecs = prov.encode([q for q, _ in MINI_QUERIES], batch_size=len(MINI_QUERIES))
    hit_top1 = 0
    for (query, expected), qv in zip(MINI_QUERIES, qvecs):
        order, scores = _cosine_scores(qv, chunk_vecs)
        top1 = order[0]
        ok = top1 in expected
        hit_top1 += ok
        mark = "✓" if ok else "✗"
        print(f"    查詢「{query}」")
        for rank, idx in enumerate(order[:3]):
            flag = "…" if rank else " "
            exp = "  ← 期望" if idx in expected else ""
            print(f"      {flag} top{rank + 1} = {scores[rank]:+.3f}  [{idx}] "
                  f"{MINI_CHUNKS[idx][:34]}{exp}")
        print(f"      → top-1 {'命中' if ok else '未命中'} {mark}")
    check(hit_top1 >= 3, f"top-1 命中 {hit_top1}/{len(MINI_QUERIES)}",
          "（≥3 視為通過；模型語義能力正常）")

    # ── 5) 重複編碼一致性（同句兩次結果應相同）─────────────────
    print("\n[5] 一致性（同句編碼兩次應一致）…")
    import numpy as np
    v1 = prov.encode_one("運費怎麼算")
    v2 = prov.encode_one("運費怎麼算")
    same = float(np.max(np.abs(np.asarray(v1) - np.asarray(v2)))) < 1e-5
    check(same, "兩次編碼結果一致")

    return report("RAG 本地模型自檢（CRM 版）")


if __name__ == "__main__":
    raise SystemExit(main())
