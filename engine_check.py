#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自動回復四層引擎自檢（P3）：FAQ → 業務表格 → RAG 知識庫 → 人工。

會真的呼叫 DeepSeek（表格／知識庫層要生成答案），所以需要有可用的 API Key。
**可重複執行**：自建臨時知識庫與 FAQ，跑完自己刪乾淨，不動你的既有資料。

用法：
    python engine_check.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
import main
import autoreply_db
import kb_db
import kb_ingest
import kb_search
import reply_engine

FAIL = []
TMP = os.path.join(HERE, "_p1_samples")
CHECK_COLLECTION = "RAG 引擎自檢庫"

FAQ_Q = "引擎自檢用問句XYZ"
FAQ_A = "這是引擎自檢用的固定答案。"


def check(cond, label, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)
    return cond


def body(res):
    raw = getattr(res, "content", None) or getattr(res, "body", b"")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


KB_DOC = """# 產品手冊

## A 款藍牙耳機
A 款藍牙耳機建議售價 1280 元，續航 30 小時，支援藍牙 5.3 與主動降噪。
充電盒採用 Type-C 介面。

## 保固條款
耳機自購買日起提供一年原廠保固，非人為損壞免費維修。
"""


def build_kb() -> int:
    cid = None
    for c in kb_db.list_collections():
        if c["name"] == CHECK_COLLECTION:
            cid = c["id"]
            # ⚠️ 整庫被停用 → `load_ready_chunks` 一塊都撈不到，這裡會整段 FAIL 看起來像壞了。
            #    自檢庫是自己建的測試資料，直接確保它是開的（並講出來）。
            if not c.get("enabled", True):
                print(f"  （自檢庫 #{cid} 原本是停用的 → 自動啟用）")
                kb_db.update_collection(cid, {"enabled": True})
    if not cid:
        cid = kb_db.create_collection(CHECK_COLLECTION, "引擎自檢用，可刪")["id"]
    for d in kb_db.list_documents(cid):
        kb_ingest.delete_document(int(d["id"]))
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, "engine_kb.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(KB_DOC)
    saved = kb_ingest.save_upload("engine_kb.md", KB_DOC.encode("utf-8"), "text/markdown", cid)
    did = saved["doc"]["id"]
    kb_ingest.parse_and_chunk(did)
    kb_ingest.embed_document(did, "pending")
    kb_ingest.activate_document(did)
    kb_search.invalidate()
    return cid


def run() -> int:
    print("=" * 78)
    print(" 自動回復四層引擎自檢（FAQ → 業務表格 → 知識庫 → 人工）")
    print("=" * 78)

    print("\n[0] 準備：臨時知識庫 + 臨時 FAQ…")
    cid = build_kb()
    st = kb_search.index_stats(cid)
    check(st["chunks"] > 0, f"臨時知識庫 #{cid} 就緒（{st['chunks']} 塊）")
    faq = autoreply_db.create_faq(FAQ_Q, FAQ_A, keywords="自檢,selftest")
    check(bool(faq.get("id")), f"臨時 FAQ #{faq.get('id')} 就緒")

    log_ids: list[int] = []
    try:
        with TestClient(main.app) as c:
            print("\n[1] 開關總覽要認得第三層…")
            d = body(c.get("/console/autoreply/settings"))
            check("rag_layer" in d, "GET /settings 有 rag_layer",
                  f"值={d.get('rag_layer')}")
            kb = d.get("kb") or {}
            check("chunks" in kb, "GET /settings 帶知識庫摘要",
                  f"知識庫 {kb.get('collections')} 個 / 已上線 {kb.get('ready_documents')} 份"
                  f" / {kb.get('chunks')} 塊")

            print("\n[2] 第一層：FAQ（layers=['faq']）…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": FAQ_Q, "layers": ["faq"]}))
            check(d.get("tier") == "faq" and d.get("label") == "自動回復-FAQ命中",
                  "FAQ 命中", f"tier={d.get('tier')} label={d.get('label')}")

            print("\n[3] 只勾知識庫（layers=['rag']，跳過 FAQ）…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": FAQ_Q, "layers": ["rag"]}))
            check(d.get("tier") != "faq", "勾 rag 時不會走到 FAQ 層", f"tier={d.get('tier')}")

            print("\n[4] 第三層：知識庫命中（真實模型呼叫）…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "A 款藍牙耳機多少錢？", "layers": ["rag"],
                                  "with_history": False}))
            ok = d.get("tier") == "rag" and d.get("label") == "自動回復-知識庫命中"
            check(ok, "RAG 命中並生成回覆", f"tier={d.get('tier')} score={d.get('score')}")
            print(f"       回覆：{str(d.get('text'))[:80]}")
            check(bool(d.get("matched_rows")), "回傳召回明細（matched_rows）",
                  f"{len(d.get('matched_rows') or [])} 筆")
            check(bool(d.get("rag_threshold")), "回傳門檻值",
                  f"門檻={d.get('rag_threshold')}")

            print("\n[4b] 勾「查詢改寫」的多查詢檢索（真實模型呼叫）…")
            # 錯別字型問句：「藍芽」→「藍牙」、「多少千」→「多少錢」
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "你們的藍芽耳機多少千？", "layers": ["rag"],
                                  "with_history": False, "query_rewrite": True}))
            rq = d.get("rag_queries") or []
            print(f"       實際檢索的查詢：{rq}")
            check(len(rq) >= 2, "原問句 + 改寫句一起檢索（多查詢）", f"{len(rq)} 條")
            check(bool(rq) and rq[0] == "你們的藍芽耳機多少千？",
                  "第 0 條固定是客戶原問句")
            check(d.get("query_rewrite") is True, "回報這次是強制開啟（不動 DB 參數）")
            check(d.get("rewrote") is True, "標記 rewrote=True（日誌才看得出改寫過）",
                  f"rewritten_query={str(d.get('rewritten_query'))[:40]}")
            check(bool(d.get("matched_rows")), "改寫後仍帶召回明細",
                  f"{len(d.get('matched_rows') or [])} 筆")
            # 對照組：同一個問句把改寫強制關掉 → 只有一條查詢，且不標改寫
            d0 = body(c.post("/console/autoreply/test/engine",
                             json={"text": "你們的藍芽耳機多少千？", "layers": ["rag"],
                                   "with_history": False, "query_rewrite": False}))
            check(len(d0.get("rag_queries") or []) == 1 and not d0.get("rewrote"),
                  "強制關閉時只有原問句、也不標改寫",
                  f"{d0.get('rag_queries')} rewrote={d0.get('rewrote')}")
            # 型別驗證
            d1 = body(c.post("/console/autoreply/test/engine",
                             json={"text": "測試", "layers": ["rag"], "query_rewrite": "yes"}))
            check("error" in d1, "query_rewrite 非布林被擋下", str(d1.get("error"))[:40])

            print("\n[5] 不相關問題 → 降級人工（不硬掰）…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "請問你們有賣冰箱嗎？", "layers": ["rag"],
                                  "with_history": False}))
            check(d.get("tier") == "miss" and not d.get("would_send"),
                  "無關問題降級人工", f"tier={d.get('tier')} reason={d.get('reason')}")
            print(f"       說明：{str(d.get('detail'))[:110]}")

            print("\n[6] 四層預設順序（layers 不給 = 依 DB 開關）…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "A 款藍牙耳機多少錢？", "with_history": False}))
            check(d.get("tier") in ("rag", "table"), "不指定 layers 時會依開關跑",
                  f"tier={d.get('tier')} layers={d.get('layers')}")

            print("\n[7] layers 參數驗證…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "測試", "layers": ["nope"]}))
            check("error" in d, "非法層名被擋下", str(d.get("error"))[:40])
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "測試", "layers": []}))
            check(d.get("tier") == "miss", "layers=[] → 直接降級人工",
                  f"reason={d.get('reason')}")

            print("\n[8] 紀錄詳情端點（matched_rows）…")
            row = autoreply_db.query("SELECT COALESCE(max(id),0) AS m FROM autoreply_log")[0]
            marker = f"selftest-{int(time.time())}"
            autoreply_db.insert_log({
                "channel": "whatsapp", "reply_scope": "dm", "tier": "rag",
                "reason": "sent", "question": "測試問句",
                "detail": "自檢寫入", "send_status": "sent", "score": 0.5,
                "matched_rows": [{"chunk_id": 1, "doc_id": 1, "doc": "x.md",
                                  "heading": "章節", "page": 1, "vec": 0.6, "lex": 0.3,
                                  "final": 0.5, "picked": True, "text": "片段"}],
                "conversation_key": marker,
            })
            new_id = autoreply_db.query(
                "SELECT id FROM autoreply_log WHERE conversation_key=%s ORDER BY id DESC LIMIT 1",
                (marker,))[0]["id"]
            log_ids.append(int(new_id))
            d = body(c.get(f"/console/autoreply/logs/{new_id}"))
            check(d.get("tier") == "rag" and len(d.get("matched_rows") or []) == 1,
                  f"GET /logs/{new_id} 回召回明細", f"tier_label={d.get('tier_label')}")
            d2 = body(c.get("/console/autoreply/logs?tier=rag&limit=5"))
            check(d2.get("logs") is not None and "matched_rows" not in (d2.get("logs") or [{}])[0],
                  "列表刻意不帶 matched_rows（保持輕）")
            d = body(c.get("/console/autoreply/logs/99999999"))
            check("error" in d, "不存在的紀錄回 404 訊息")
            d = body(c.get("/console/autoreply/stats"))
            scopes = d.get("scopes") or []
            check(scopes and "rag" in scopes[0], "統計含 rag bucket",
                  f"rag={scopes[0].get('rag') if scopes else '—'}")

            print("\n[9] FAQ 內文檢索（含參數化陷阱回歸）…")
            d = body(c.get(f"/console/autoreply/faq?q={'自检用问句' if False else FAQ_Q}"))
            check(any(x["question"] == FAQ_Q for x in d.get("faqs") or []),
                  "依問題內文找到 FAQ", f"filtered={d.get('filtered')}/{d.get('total')}")
            d = body(c.get("/console/autoreply/faq?q=%E8%87%AA%E6%AA%A2"))   # 「自檢」
            check(d.get("faqs") is not None, "中文關鍵詞可搜",
                  f"filtered={d.get('filtered')}")
            # ⚠️ 這是最容易踩的坑：SQL 裡若寫字面 LIKE '%x%'，參數化後 % 會被當佔位符 → 500
            for weird in ("100%", "%", "_", "a%b_c", "'"):
                d = body(c.get("/console/autoreply/faq", params={"q": weird}))
                if "error" in d:
                    check(False, f"含特殊字元的查詢不該炸（q={weird!r}）", str(d["error"])[:60])
                    break
            else:
                check(True, "含 % / _ / 引號 的查詢都安全（ILIKE 已參數化）")

            print("\n[10] 未命中的紀錄也帶召回明細…")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "請問你們有賣冰箱嗎？", "layers": ["rag"],
                                  "with_history": False}))
            rows = d.get("matched_rows") or []
            check(d.get("tier") == "miss" and len(rows) >= 1,
                  "未命中仍回候選明細（調參最需要的線索）",
                  f"{len(rows)} 筆，top1={d.get('score')}")
            if rows:
                r0 = rows[0]
                check(all(k in r0 for k in ("vec", "lex", "final", "picked", "text")),
                      "明細欄位齊全（vec/lex/final/picked/text）",
                      f"final={r0.get('final')} pick={r0.get('picked')}")

            print("\n[11] 層開關（rag）…")
            check(reply_engine.rag_layer_enabled() is True, "預設啟用知識庫層")
            c.post("/console/autoreply/settings/layer", json={"layer": "rag", "enabled": False})
            check(reply_engine.rag_layer_enabled() is False, "可以停用知識庫層")
            d = body(c.post("/console/autoreply/test/engine",
                            json={"text": "A 款藍牙耳機多少錢？", "with_history": False}))
            check(d.get("tier") != "rag", "停用後不會命中知識庫層",
                  f"tier={d.get('tier')} detail={str(d.get('detail'))[:60]}")
            c.post("/console/autoreply/settings/layer", json={"layer": "rag", "enabled": True})
            check(reply_engine.rag_layer_enabled() is True, "可以重新啟用知識庫層")
            d = body(c.post("/console/autoreply/settings/layer",
                            json={"layer": "nope", "enabled": True}))
            check("error" in d, "非法 layer 被擋下")
    finally:
        print("\n[清理] 移除臨時知識庫 / FAQ / 紀錄…")
        for d in kb_db.list_documents(cid):
            kb_ingest.delete_document(int(d["id"]))
        kb_db.delete_collection(cid)
        kb_db.query("SELECT 1")
        autoreply_db.delete_faq(faq["id"])
        if log_ids:
            autoreply_db.execute("DELETE FROM autoreply_log WHERE id = ANY(%s)", (log_ids,))
        reply_engine.invalidate_cache()
        print("  已清理")

    print("\n" + "=" * 78)
    if FAIL:
        print(f" 結果：{len(FAIL)} 項失敗")
        for f in FAIL:
            print("   ✗", f)
    else:
        print(" 結果：全部通過 ✅")
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
