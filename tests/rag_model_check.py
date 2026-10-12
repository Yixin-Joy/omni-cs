#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RAG 知識庫的本地模型自檢（embedding 載入 + 檢索冒煙測試）。

這是 RAG 第三層的驗收工具：之後除錯、換模型、換機器都可以重跑同一支。
設計背景見 `RAG_DESIGN.md` §7。

用法：
    python rag_model_check.py                # 完整自檢（首次會下載模型 ~640MB）
    python rag_model_check.py -q             # 只測「能否載入 + 編碼一句」
    python rag_model_check.py -m <模型名>     # 臨時換模型
    python rag_model_check.py -t 4           # 限制 onnxruntime 執行緒數

環境變數：
    RAG_HF_ENDPOINT   下載鏡像（預設 https://hf-mirror.com）
    RAG_EMBED_MODEL   模型名（預設 jinaai/jina-embeddings-v2-base-zh）
    RAG_EMBED_DIM     預期維度（預設 768）
    RAG_MODEL_DIR     模型快取目錄（預設 <專案>/models/fastembed）
"""
import argparse
import json as _json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

# ⚠️ 必須在 import fastembed 之前設定：fastembed 一被 import 就會載入 huggingface_hub
os.environ.setdefault("HF_ENDPOINT", os.getenv("RAG_HF_ENDPOINT") or "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 專案根
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:  # 中文 Windows 主控台
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_MODEL = os.getenv("RAG_EMBED_MODEL") or "jinaai/jina-embeddings-v2-base-zh"
DEFAULT_DIM = int(os.getenv("RAG_EMBED_DIM") or "768")
MODEL_DIR = os.getenv("RAG_MODEL_DIR") or os.path.join(_HERE, "models", "fastembed")

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


def _heal_snapshot_cache(model_name: str, cache_dir: str) -> str | None:
    """修復 Windows 上 huggingface_hub 組裝失敗留下的 0 位元組 snapshot 檔。

    症狀（實測）：檔案本體已完整下載進 `blobs/`，但 `snapshots/<rev>/` 下的指標檔
    是 **0 位元組的實體檔**（不是 symlink），於是 fastembed 的 size 校驗
    （`Path.stat().st_size != meta["size"]`）判定「corrupted」而拒絕載入。

    做法：向 HF API 取檔案清單（含 size）→ 掃描非空 blob 建「size → 真實檔」索引
    → snapshot 裡大小不符的檔，按 size **唯一**配對後硬連結重建。

    回傳修復摘要字串；無需修復或無法修復時回傳 None。
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

    # 非空真實檔 → size 索引
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
    ap = argparse.ArgumentParser(description="RAG 本地模型自檢（embedding + 檢索）")
    ap.add_argument("-q", "--quick", action="store_true", help="只測能否載入與編碼")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL, help=f"模型名（預設 {DEFAULT_MODEL}）")
    ap.add_argument("-t", "--threads", type=int, default=8, help="onnxruntime 執行緒數（預設 8）")
    args = ap.parse_args()

    print("=" * 70)
    print(" RAG 本地模型自檢")
    print("=" * 70)
    print(f"  HF_ENDPOINT : {os.environ['HF_ENDPOINT']}")
    print(f"  模型         : {args.model}")
    print(f"  快取目錄     : {MODEL_DIR}")
    print(f"  執行緒       : {args.threads}")
    print("-" * 70)

    results: list[bool] = []

    def check(cond, label, extra=""):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
        return bool(cond)

    def _load():
        from fastembed import TextEmbedding
        return TextEmbedding(model_name=args.model, cache_dir=MODEL_DIR, threads=args.threads)

    def _give_up(err):
        check(False, "模型載入失敗", f"{type(err).__name__}: {err}")
        print("\n>>> 載入失敗。常見原因：")
        print("    · 網路／鏡像不通 → 檢查 HF_ENDPOINT、或先 curl 測 https://hf-mirror.com")
        print("    · 磁碟不足       → 模型約 640MB（含暫存約 1GB）")
        print("    · 快取組裝異常   → 刪掉 models/ 整個重跑本腳本")
        print(f"\n通過 {sum(results)}/{len(results)} 項")
        return 1

    # ── 1) 載入模型（首次會下載）────────────────────────────────
    print("[1] 載入模型（首次執行會下載 ~640MB，請耐心等）…")
    t0 = time.time()
    try:
        emb = _load()
        check(True, "模型載入成功", f"耗時 {time.time() - t0:.1f}s")
    except Exception as e:
        # Windows 上 huggingface_hub 有時組裝不出 snapshot（指標檔變 0 位元組），
        # 但檔案本體已完整下載 → 就地修復後重試一次。
        print(f"  載入失敗（{type(e).__name__}），嘗試快取自愈…")
        msg = _heal_snapshot_cache(args.model, MODEL_DIR)
        if not msg:
            return _give_up(e)
        print(f"  已修復：{msg}")
        try:
            emb = _load()
            check(True, "模型載入成功（經快取自愈）", f"耗時 {time.time() - t0:.1f}s")
        except Exception as e2:
            return _give_up(e2)

    # ── 2) 編碼一句，驗維度與延遲 ───────────────────────────────
    print("\n[2] 編碼測試（單句）…")
    t0 = time.time()
    vec = list(emb.embed(["藍牙耳機多少錢？"]))[0]
    dt0 = time.time() - t0
    dim = len(vec)
    check(dim == DEFAULT_DIM, f"向量維度 = {dim}", f"（預期 {DEFAULT_DIM}）單句耗時 {dt0:.3f}s")

    if args.quick:
        print(f"\n通過 {sum(results)}/{len(results)} 項（-q 快速模式）")
        return 0 if all(results) else 1

    # ── 3) 編碼吞吐 ─────────────────────────────────────────────
    print("\n[3] 編碼吞吐（迷你知識庫 6 塊）…")
    t0 = time.time()
    chunk_vecs = list(emb.embed(MINI_CHUNKS))
    dt = time.time() - t0
    per = dt / len(MINI_CHUNKS)
    check(True, f"{len(MINI_CHUNKS)} 塊耗時 {dt:.2f}s", f"約 {per * 1000:.0f} ms/塊")

    # ── 4) 檢索自檢 ─────────────────────────────────────────────
    print("\n[4] 檢索自檢（語義召回，cosine）…")
    qvecs = list(emb.embed([q for q, _ in MINI_QUERIES]))
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
            print(f"      {flag} top{rank + 1} = {scores[rank]:+.3f}  [{idx}] {MINI_CHUNKS[idx][:34]}{exp}")
        print(f"      → top-1 {'命中' if ok else '未命中'} {mark}")
    check(hit_top1 >= 3, f"top-1 命中 {hit_top1}/{len(MINI_QUERIES)}",
          "（≥3 視為通過；模型語義能力正常）")

    # ── 5) 重複編碼一致性（同句兩次結果應相同）─────────────────
    print("\n[5] 一致性（同句編碼兩次應一致）…")
    import numpy as np
    v1 = list(emb.embed(["運費怎麼算"]))[0]
    v2 = list(emb.embed(["運費怎麼算"]))[0]
    same = float(np.max(np.abs(np.asarray(v1) - np.asarray(v2)))) < 1e-5
    check(same, "兩次編碼結果一致")

    n_ok, n_all = sum(results), len(results)
    print("\n" + "=" * 70)
    print(f" 結果：{n_ok}/{n_all} 項通過" + ("  ✅ 全部通過" if n_ok == n_all else "  ⚠️ 有失敗項"))
    print("=" * 70)
    return 0 if n_ok == n_all else 1


if __name__ == "__main__":
    sys.exit(main())
