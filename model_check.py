#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""model_check.py — AI 模型連線與對話自檢

讀 .env 的**實際設定**（DEEPSEEK_*），依序檢查：
  1. 設定摘要
  2. 連通 ping
  3. 簡單對話（3 句中文）
  4. 指令遵循（要求只回 OK）
  5. 表格 sentinel 遵循（__NO_ANSWER__）← 本地模型最常出錯的一項

用法：
    python model_check.py            # 全部測
    python model_check.py -q         # 只測連通（第 1~2 項）
    python model_check.py -m xxx     # 臨時換模型名（不寫回 .env）

路徑說明：本專案路徑含中文，.venv\\Scripts\\python.exe 會因 venv launcher 的
編碼問題起不來（failed to open '...\\??\\...pyvenv.cfg'）。本腳本會自動把
.venv 的 site-packages 掛進 sys.path，所以用「系統 python」直接跑即可。
"""
import argparse
import asyncio
import os
import sys
import time

# ---------- 環境修補（必須在任何專案 import 之前）----------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SP = os.path.join(_HERE, ".venv", "Lib", "site-packages")
if os.path.isdir(_SP) and _SP not in sys.path:
    sys.path.insert(0, _SP)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:  # Windows 主控台中文輸出
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config  # noqa: E402
from deepseek_client import (  # noqa: E402
    DeepSeekClient, DeepSeekError, NO_ANSWER_SENTINEL, masked_key,
)

PASS, FAIL, WARN = "[OK]", "[X] ", "[!] "
_results: list[tuple[str, bool]] = []


def hr(title: str) -> None:
    print("\n" + "─" * 64)
    print(title)
    print("─" * 64)


def show(label: str, text: str, limit: int = 160) -> None:
    t = (text or "").replace("\n", " ").strip()
    print(f"   {label}：{t[:limit]}{'…' if len(t) > limit else ''}")


def record(name: str, ok: bool, note: str = "") -> None:
    _results.append((name, ok))
    print(f"   {PASS if ok else FAIL} {name}{('  — ' + note) if note else ''}")


async def run(args) -> int:
    client = DeepSeekClient(model=(args.model or None))

    # ---------- 1) 設定摘要 ----------
    hr("1) 設定摘要（來自 .env）")
    print(f"   base_url : {client.base_url}")
    print(f"   model    : {client.model}")
    print(f"   api_key  : {masked_key() or '（未設定）'}")
    print(f"   timeout  : {client.timeout}s")

    if not client.api_key:
        print(f"\n   {FAIL} DEEPSEEK_API_KEY 是空的，無法測試。")
        return 1

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
        print(f"   {FAIL} {e}")
        record("連線與回應正常", False, str(e)[:90])
        print("\n   連線就失敗了，後續測試跳過。")
        return _summary()

    if args.quick:
        return _summary()

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
            print(f"   {FAIL} 「{q}」→ {e}")
            record(f"對話「{q}」有回應", False, str(e)[:60])

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
        print(f"   {FAIL} {e}")
        record("遵循簡短指令", False, str(e)[:60])

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
        print(f"   {FAIL} {e}")
        record("表格外問題 → 正確回 sentinel", False, str(e)[:60])

    # 5b：表格裡「有」→ 應該答出正確價格
    q_hit = "藍牙耳機多少錢？"
    try:
        out2 = await client.answer_with_table(q_hit, table, flavor="dm")
        show(f"問「{q_hit}」", out2)
        ok = ("2990" in out2 or "2,990" in out2) and NO_ANSWER_SENTINEL not in out2
        record("表格內問題 → 答出正確價格 2990", ok,
               "" if ok else "沒答出 2990（或誤回 sentinel）")
    except DeepSeekError as e:
        print(f"   {FAIL} {e}")
        record("表格內問題 → 答出正確價格 2990", False, str(e)[:60])

    return _summary()


def _summary() -> int:
    hr("總結")
    for name, ok in _results:
        print(f"   {PASS if ok else FAIL} {name}")
    n_ok = sum(1 for _, o in _results if o)
    print(f"\n   通過 {n_ok}/{len(_results)}")
    if n_ok == len(_results):
        print("   模型可用，對話與業務判定都正常。")
        return 0
    print("   有項目未通過 —— 上方標 [X] 的那幾項就是要處理的地方。")
    return 2


def main() -> None:
    p = argparse.ArgumentParser(description="AI 模型連線與對話自檢")
    p.add_argument("-q", "--quick", action="store_true", help="只測連通（第 1~2 項）")
    p.add_argument("-m", "--model", default="", help="臨時覆寫模型名稱（不寫回 .env）")
    args = p.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\n已中止。")
        sys.exit(130)


if __name__ == "__main__":
    main()
