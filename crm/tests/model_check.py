#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 模型連線與對話自檢（**CRM 版**）。

## 與根 `tests/model_check.py` 的差別

| | 根版（單體） | 這版（CRM） |
|---|---|---|
| sys.path | 自己把 `.venv\\Lib\\site-packages` 硬掛進去 | 交給 `_ck`（用底層 python + `PYTHONPATH` 跑，與其他自檢一致） |
| 環境類失敗 | 一般 FAIL，紅一片 | 走 **`_ck.check_or_env`** → 標 `[環境]`，不算 FAIL（與 `engine_check` 共用同一份 `ENV_HINTS`） |
| 統計輸出 | 自己的 `_results` / `_summary()` | 共用 `_ck.report()` |
| 新增 | — | `[0]` 設定來源對齊：`DEEPSEEK_*` 有值、system 提示詞非空、DB 覆寫優先序 |

讀 CRM 的 `.env`（`crm/.env`）**實際設定**，依序檢查：
  0. 設定來源對齊（api_key / base_url / model / system 提示詞）
  1. 設定摘要
  2. 連通 ping
  3. 簡單對話（3 句中文）
  4. 指令遵循（要求只回 OK）
  5. 表格 sentinel 遵循（`__NO_ANSWER__`）← 本地模型最常出錯的一項
  6. 知識庫查詢改寫：6a 輸出解析（純函式）／ 6b 真實改寫（同音錯別字＋上下文補齊）

用法（與其他自檢相同，cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/model_check.py [-q] [-m 模型名]

    -q  只測連通（第 0~2 項）
    -m  臨時換模型名（不寫回 .env）

⚠️ 這支會**真的呼叫模型**（花額度）。閘道限流／逾時會被標成 `[環境]`，不算失敗。
"""
import argparse
import asyncio
import sys
import time

from _ck import check_or_env, report  # noqa: E402   ← 這行就把 crm/ 塞進 sys.path 了

from app.core import config            # noqa: E402
from app.rag import rag_config         # noqa: E402
from app.llm.deepseek_client import (  # noqa: E402
    DeepSeekClient, DeepSeekError, NO_ANSWER_SENTINEL, clean_rewrite_list,
    masked_key, configured,
)

TITLE = "AI 模型連線與對話自檢（CRM 版）"
WARN = "[!] "


def hr(title: str) -> None:
    print("\n" + "─" * 70)
    print(title)
    print("─" * 70)


def show(label: str, text: str, limit: int = 160) -> None:
    t = (text or "").replace("\n", " ").strip()
    print(f"   {label}：{t[:limit]}{'…' if len(t) > limit else ''}")


def record(name: str, ok: bool, note: str = "", text: str = "") -> bool:
    """記一筆結果。`text` 是**原始錯誤字串**（給 check_or_env 判斷是否環境問題）。"""
    return check_or_env(ok, name, text=text or note, extra=note)


async def run(args) -> int:
    client = DeepSeekClient(model=(args.model or None))

    # ---------- 0) 設定來源對齊（這版新增）----------
    hr("0) 設定來源對齊（crm/.env ← config ← DeepSeekClient）")
    print(f"   base_url : {client.base_url}")
    print(f"   model    : {client.model}")
    print(f"   api_key  : {masked_key() or '（未設定）'}")
    print(f"   timeout  : {client.timeout}s")
    record("DEEPSEEK_API_KEY 已設定", bool(client.api_key),
           "" if client.api_key else "空的 → 底下所有呼叫都會失敗")
    record("client 的 base_url / model 來自 config（沒有偷偷寫死）",
           client.base_url == (config.DEEPSEEK_BASE_URL or "").rstrip("/")
           and client.model == (config.DEEPSEEK_MODEL or "").strip(),
           f"{client.base_url} / {client.model}")
    record("system 提示詞非空（表格／知識庫回答都靠它）",
           bool((config.DEEPSEEK_SYSTEM_PROMPT or "").strip()),
           f"{len(config.DEEPSEEK_SYSTEM_PROMPT or '')} 字")
    record("留言情境另有提示詞（flavor=comment 會疊上去）",
           bool((getattr(config, "DEEPSEEK_COMMENT_PROMPT", "") or "").strip()),
           f"{len(getattr(config, 'DEEPSEEK_COMMENT_PROMPT', '') or '')} 字")
    record("查詢改寫的條數設定讀得到（rag_config）",
           rag_config.RAG_QUERY_REWRITE_N >= 1,
           f"RAG_QUERY_REWRITE_N={rag_config.RAG_QUERY_REWRITE_N}")

    if not client.api_key:
        print("\n   [環境] 沒有 API key，後續測試全部跳過。")
        return report(TITLE)

    # 常見陷阱：base_url 少 /v1（LiteLLM / OpenAI 相容端點會 404）
    if not client.base_url.endswith("/v1") and "deepseek.com" not in client.base_url:
        print(f"\n   {WARN} base_url 結尾不是 /v1 —— 若對方是 LiteLLM/OpenAI 相容端點，")
        print("        會打到 /chat/completions 而非 /v1/chat/completions，可能 404。")

    # ---------- 2) 連通 ----------
    hr("2) 連通測試 ping()")
    t0 = time.perf_counter()
    try:
        reply = await client.ping()
        dt = time.perf_counter() - t0
        show("回覆", reply)
        print(f"   耗時：{dt:.2f}s")
        record("連線與回應正常", bool(reply and reply.strip()))
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("連線與回應正常", False, text=str(e))
        print("\n   連線就失敗了，後續測試跳過。")
        return report(TITLE)

    if args.quick:
        return report(TITLE + "（-q 快速模式）")

    # ---------- 3) 簡單對話 ----------
    hr("3) 簡單對話（3 句）")
    convos = ["你好", "你們賣什麼產品？", "謝謝你，再見"]
    for q in convos:
        t0 = time.perf_counter()
        try:
            ans = await client.complete(
                [{"role": "system", "content": config.DEEPSEEK_SYSTEM_PROMPT},
                 {"role": "user", "content": q}],
                temperature=0.0,
            )
            dt = time.perf_counter() - t0
            show(f"問「{q}」", ans)
            print(f"        ↑ {dt:.2f}s / {len(ans)} 字")
            record(f"對話「{q}」有回應", bool(ans and ans.strip()))
        except DeepSeekError as e:
            print(f"   [X] 「{q}」→ {e}")
            record(f"對話「{q}」有回應", False, text=str(e))

    # ---------- 4) 指令遵循 ----------
    hr("4) 指令遵循（要求只回 OK）")
    try:
        ans = await client.complete(
            [{"role": "user", "content": "請只回覆「OK」兩個字，不要任何其他內容。"}],
            temperature=0.0,
        )
        show("回覆", ans)
        ok = "OK" in ans.upper() and len(ans.strip()) <= 10
        record("遵循簡短指令", ok, "" if ok else "回覆過長／不符，模型不聽話")
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("遵循簡短指令", False, text=str(e))

    # ---------- 5) sentinel 遵循（業務關鍵）----------
    hr("5) 表格 sentinel 遵循  ← 本地模型最常出錯的一項")
    table = ("表名：商品\n"
             "- 商品名稱：無線藍牙耳機\n- 價格：2990\n- 庫存：有貨\n"
             "- 商品名稱：行動電源\n- 價格：890\n- 庫存：缺貨")
    print(f"   （測試用表格）{table.replace(chr(10), ' / ')}")

    # 5a：表格裡「沒有」→ 必須只回 __NO_ANSWER__
    q_miss = "你們有賣冰箱嗎？"
    try:
        out = await client.answer_with_table(q_miss, table, flavor="dm")
        show(f"問「{q_miss}」", out)
        ok = NO_ANSWER_SENTINEL in out
        record("表格外問題 → 正確回 sentinel", ok,
               "" if ok else "沒回 __NO_ANSWER__ → 會亂答或誤判命中")
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("表格外問題 → 正確回 sentinel", False, text=str(e))

    # 5b：表格裡「有」→ 應該答出正確價格
    q_hit = "藍牙耳機多少錢？"
    try:
        out2 = await client.answer_with_table(q_hit, table, flavor="dm")
        show(f"問「{q_hit}」", out2)
        ok = ("2990" in out2 or "2,990" in out2) and NO_ANSWER_SENTINEL not in out2
        record("表格內問題 → 答出正確價格 2990", ok,
               "" if ok else "沒答出 2990（或誤回 sentinel）")
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("表格內問題 → 答出正確價格 2990", False, text=str(e))

    # ---------- 6) 知識庫查詢改寫（多查詢檢索）----------
    # 勾了「查詢改寫」時，檢索前會先要模型吐出 N 條查詢。這裡分兩塊驗：
    #   6a 解析器（純函式，不呼叫模型）—— 模型經常多寫編號／說明，解析要吃得下
    #   6b 真實改寫 —— 錯別字有沒有改對、上下文有沒有補齊
    hr("6a) 查詢改寫的輸出解析（不呼叫模型，純函式）")
    cases = [
        ("編號＋引號", "1. 「藍牙耳機多少錢」\n2. A 款耳機價格", 2),
        ("項目符號＋說明行", "以下是改寫結果：\n- 藍牙耳機多少錢\n- A 款耳機價格", 2),
        ("JSON 陣列", '["藍牙耳機多少錢", "A 款耳機價格"]', 2),
        ("重複＋超長說明", "藍牙耳機多少錢\n藍牙耳機多少錢\n" + "說明" * 40, 1),
    ]
    for label, raw, want in cases:
        got = clean_rewrite_list(raw, limit=2)
        ok = len(got) == want
        record(f"解析「{label}」→ {want} 條", ok, f"實得 {got}")
    got = clean_rewrite_list("藍牙耳機多少錢\nA 款耳機價格", limit=2,
                             exclude=["藍牙耳機多少錢"])
    record("會排除與原問句相同的行", got == ["A 款耳機價格"], f"{got}")

    hr("6b) 真實改寫：同音錯別字 ＋ 上下文補齊")
    # 錯字：「藍芽」應改「藍牙」、「多少千」應改「多少錢」
    typo_q = "你們的藍芽耳機多少千？"
    try:
        out = await client.rewrite_rag_queries(
            typo_q, "", count=rag_config.RAG_QUERY_REWRITE_N)
        for i, q in enumerate(out, 1):
            print(f"   {i}. {q}")
        ok = bool(out) and any("牙" in q for q in out)
        record("錯別字有被修正（至少一條出現「藍牙」）", ok,
               "" if ok else f"沒改到：{out}")
        record(f"回傳條數 ≤ {rag_config.RAG_QUERY_REWRITE_N}",
               len(out) <= rag_config.RAG_QUERY_REWRITE_N, f"實得 {len(out)} 條")
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("錯別字有被修正（至少一條出現「藍牙」）", False, text=str(e))

    # 指代：「這個」的主語在對話裡
    hist = "客戶：B 款耳機還有貨嗎？\n客服：B 款目前現貨充足喔。"
    try:
        out2 = await client.rewrite_rag_queries(
            "那這個多少錢？", hist, count=rag_config.RAG_QUERY_REWRITE_N)
        for i, q in enumerate(out2, 1):
            print(f"   {i}. {q}")
        ok2 = bool(out2) and any(("B" in q or "耳機" in q) for q in out2)
        record("指代有被補齊（至少一條帶出「B 款／耳機」）", ok2,
               "" if ok2 else f"沒補到：{out2}")
    except DeepSeekError as e:
        print(f"   [X] {e}")
        record("指代有被補齊（至少一條帶出「B 款／耳機」）", False, text=str(e))

    return report(TITLE)


def main() -> None:
    p = argparse.ArgumentParser(description="AI 模型連線與對話自檢（CRM 版）")
    p.add_argument("-q", "--quick", action="store_true", help="只測連通（第 0~2 項）")
    p.add_argument("-m", "--model", default="", help="臨時覆寫模型名稱（不寫回 .env）")
    args = p.parse_args()
    print(f"（金鑰來源：{'已設定' if configured() else '未設定'}）")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\n已中止。")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
