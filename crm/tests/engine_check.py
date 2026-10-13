"""自動回復四層引擎自檢：FAQ → 業務表格 → RAG 知識庫 → 人工。

★ CRM 側有兩件事與單體版不同，這支自檢也跟著改：

1. **引擎要 `business_id`** —— 知識庫／FAQ／表格都是按商家隔離的（設計案 §9），
   所以自檢自建一個 `__CK__引擎自檢` 商家，跑完連資料一起清掉。
2. **不再走 HTTP**。單體版打 `/console/autoreply/test/engine`；CRM 的工作台端點是
   `/console/agent/test`，要登入 + CSRF + 商家權限 —— 那條線已經由
   `phase4_workbench_check.py` 驗過了。這裡直接呼叫 `reply_engine.answer()`，
   驗的是**引擎的決策**（tier／命中哪一層／模型呼叫次數／召回明細／附件／耗時）。

會真的呼叫 DeepSeek（知識庫層要生成答案），所以需要有可用的 API Key。
環境類失敗（429／逾時）會標成 `[環境]`，不算 FAIL（見 `check_or_env`）。
**可重複執行**：自建資料、跑完自己刪乾淨，不動你的既有資料。

跑法（cwd 隨意，路徑由 __file__ 決定）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/engine_check.py
"""
import asyncio
import os
import sys
import time

from _ck import (  # noqa: E402
    check, report, sandbox_dir, ensure_business, purge_business, check_or_env,
)

from app.autoreply import autoreply_db
from app.llm import reply_engine
from app.rag import kb_db
from app.rag import kb_ingest
from app.rag import kb_search

TMP = str(sandbox_dir())
CHECK_COLLECTION = "RAG 引擎自檢庫"
BIZ = ensure_business("引擎自檢", "engine_check 專用")

FAQ_Q = "引擎自檢用問句XYZ"
FAQ_A = "這是引擎自檢用的固定答案。"
CH = "whatsapp"
SCOPE = "dm"

# ⚠️ 環境類失敗（閘道限流／逾時）的判定與標記集中在 `_ck.check_or_env`
#    —— 跟 `model_check` 共用同一份 `ENV_HINTS`，不要在這裡再寫一套。


KB_DOC = """# 產品手冊

## A 款藍牙耳機
A 款藍牙耳機建議售價 1280 元，續航 30 小時，支援藍牙 5.3 與主動降噪。
充電盒採用 Type-C 介面。

## 保固條款
耳機自購買日起提供一年原廠保固，非人為損壞免費維修。
"""


def ask(text, *, layers=None, query_rewrite=None, history=None, scope_set=None):
    """跑一次引擎（同步包裝）。"""
    reply_engine.invalidate_cache()
    return asyncio.run(reply_engine.answer(
        text, business_id=BIZ, channel=CH, scope=SCOPE, layers=layers,
        query_rewrite=query_rewrite, history=history, scope_set=scope_set))


def build_kb() -> int:
    cid = None
    for c in kb_db.list_collections(business_id=BIZ):
        if c["name"] == CHECK_COLLECTION:
            cid = c["id"]
            # ⚠️ 整庫被停用 → `load_ready_chunks` 一塊都撈不到，這裡會整段 FAIL 看起來像壞了。
            #    自檢庫是自己建的測試資料，直接確保它是開的（並講出來）。
            if not c.get("enabled", True):
                print(f"  （自檢庫 #{cid} 原本是停用的 → 自動啟用）")
                kb_db.update_collection(cid, {"enabled": True})
    if not cid:
        cid = kb_db.create_collection(CHECK_COLLECTION, "引擎自檢用，可刪",
                                      business_id=BIZ)["id"]
    for d in kb_db.list_documents(cid):
        kb_ingest.delete_document(int(d["id"]))
    os.makedirs(TMP, exist_ok=True)
    with open(os.path.join(TMP, "engine_kb.md"), "w", encoding="utf-8") as f:
        f.write(KB_DOC)
    saved = kb_ingest.save_upload("engine_kb.md", KB_DOC.encode("utf-8"), "text/markdown", cid)
    did = saved["doc"]["id"]
    kb_ingest.parse_and_chunk(did)
    kb_ingest.embed_document(did, "pending")
    kb_ingest.activate_document(did)
    kb_search.invalidate()
    return cid


def main() -> int:
    print("=" * 78)
    print(" 自動回復四層引擎自檢（FAQ → 業務表格 → 知識庫 → 人工）")
    print("=" * 78)

    print("\n[0] 準備：自檢商家 + 臨時知識庫 + 臨時 FAQ…")
    kb_db.init_schema()
    cid = build_kb()
    st = kb_search.index_stats(cid)
    check(st["chunks"] > 0, f"臨時知識庫 #{cid} 就緒（{st['chunks']} 塊）")
    faq = autoreply_db.create_faq(BIZ, FAQ_Q, FAQ_A, keywords="自檢,selftest")
    check(bool(faq.get("id")), f"臨時 FAQ #{faq.get('id')} 就緒")
    check(autoreply_db.list_binding_ids(BIZ, "kb") == [cid],
          "知識庫自動進基準啟用集（§9.4 上傳即進）",
          str(autoreply_db.list_binding_ids(BIZ, "kb")))
    check(autoreply_db.list_binding_ids(BIZ, "faq") == [faq["id"]],
          "FAQ 自動進基準啟用集")
    reply_engine.invalidate_cache()

    log_ids: list[int] = []
    try:
        print("\n[1] 四層順序只有一個來源…")
        check(tuple(reply_engine.ALL_LAYERS) == (reply_engine.TIER_FAQ,
                                                 reply_engine.TIER_TABLE,
                                                 reply_engine.TIER_RAG),
              "ALL_LAYERS = faq → table → rag", str(tuple(reply_engine.ALL_LAYERS)))

        print("\n[2] 第一層：FAQ（layers=['faq']）…")
        a = ask(FAQ_Q, layers=["faq"])
        check(a.tier == "faq" and a.label == "自動回復-FAQ命中",
              "FAQ 命中", f"tier={a.tier} label={a.label}")
        check(int(a.llm_calls) == 0, "FAQ 命中等於純規則 → 0 次模型呼叫",
              f"llm_calls={a.llm_calls} 耗時 {a.elapsed_ms}ms")
        check(a.hit and a.text == FAQ_A, "回覆就是 FAQ 的答案（一字不改）", a.text[:40])

        print("\n[3] 只勾知識庫（layers=['rag']，跳過 FAQ）…")
        a = ask(FAQ_Q, layers=["rag"])
        check(a.tier != "faq", "勾 rag 時不會走到 FAQ 層", f"tier={a.tier}")

        print("\n[4] 第三層：知識庫命中（真實模型呼叫）…")
        a = ask("A 款藍牙耳機多少錢？", layers=["rag"])
        ok = a.tier == "rag" and a.label == "自動回復-知識庫命中"
        check_or_env(ok, "RAG 命中並生成回覆", str(a.detail or ""),
                     f"tier={a.tier} score={a.score}")
        print(f"       回覆：{str(a.text)[:80]}")
        check(bool(a.matched_rows), "回傳召回明細（matched_rows）",
              f"{len(a.matched_rows or [])} 筆")
        check(bool(a.rag_threshold), "回傳門檻值", f"門檻={a.rag_threshold}")
        if a.matched_rows:
            r0 = a.matched_rows[0]
            check(all(k in r0 for k in ("vec", "lex", "final", "picked", "text")),
                  "明細欄位齊全（vec/lex/final/picked/text）",
                  f"final={r0.get('final')} pick={r0.get('picked')}")

        print("\n[4b] 勾「查詢改寫」的多查詢檢索（真實模型呼叫）…")
        # 錯別字型問句：「藍芽」→「藍牙」、「多少千」→「多少錢」
        a = ask("你們的藍芽耳機多少千？", layers=["rag"], query_rewrite=True)
        rq = a.rag_queries or []
        det = str(a.detail or "")
        print(f"       實際檢索的查詢：{rq}")
        check_or_env(len(rq) >= 2, "原問句 + 改寫句一起檢索（多查詢）", det, f"{len(rq)} 條")
        check(bool(rq) and rq[0] == "你們的藍芽耳機多少千？", "第 0 條固定是客戶原問句")
        check_or_env(a.rewrote is True, "標記 rewrote=True（日誌才看得出改寫過）",
                     det, f"llm_errors={a.llm_errors}")
        check(bool(a.matched_rows), "改寫後仍帶召回明細",
              f"{len(a.matched_rows or [])} 筆")
        # 對照組：同一句把改寫強制關掉 → 只有一條查詢、也不標改寫，且**不動 DB 參數**
        a0 = ask("你們的藍芽耳機多少千？", layers=["rag"], query_rewrite=False)
        check(len(a0.rag_queries or []) == 1 and not a0.rewrote,
              "強制關閉時只有原問句、也不標改寫",
              f"{a0.rag_queries} rewrote={a0.rewrote}")

        print("\n[4c] 一次改寫、三層共用（呼叫次數回歸）…")
        fq = ask(FAQ_Q, layers=None, query_rewrite=True)
        check(int(fq.llm_calls or 0) == 0 and fq.tier == "faq",
              "FAQ 命中時 0 次模型呼叫（改寫發生在 FAQ 之後）",
              f"tier={fq.tier} llm_calls={fq.llm_calls} 耗時 {fq.elapsed_ms}ms")
        allq = ask("有目標系統接收你們的文字訊息的範例嗎？", layers=None, query_rewrite=True)
        calls = int(allq.llm_calls or 0)
        rq2 = allq.rag_queries or []
        print(f"       tier={allq.tier} llm_calls={calls} elapsed={allq.elapsed_ms}ms")
        print(f"       共用查詢（{len(rq2)} 條）：{rq2}")
        check(calls <= 4, "一次回覆的模型呼叫 ≤ 4 次（改寫1＋表格≤2＋知識庫≤1）",
              f"實際 {calls} 次（優化前是 8 次）")
        check_or_env(len(rq2) >= 2, "三層共用同一組查詢（原句 + 改寫句）",
                     str(allq.detail or ""), f"{len(rq2)} 條")
        check(int(allq.elapsed_ms or 0) > 0, "回傳耗時（紀錄頁要看）", f"{allq.elapsed_ms}ms")
        check(float(allq.llm_seconds or 0) >= 0, "回傳模型耗時佔比",
              f"{allq.llm_seconds}s / {allq.elapsed_ms}ms")
        nq = ask("有目標系統接收你們的文字訊息的範例嗎？", layers=None, query_rewrite=False)
        check(len(nq.rag_queries or []) == 1,
              "改寫關 → 只有原問句（一個開關管到底）", f"{nq.rag_queries}")

        print("\n[4d] 檢索命中 → 模型不得否決（圖片類答案）…")
        # 這是實測抓到的 bug（紀錄 #260「服务器端转发成功日志长啥样？」）：
        # top1=0.641 命中的**就是那張截圖**，但純文字模型看不到圖 → 一定回哨兵
        # → 舊版當成未命中 → 命中得再準也不回覆。閘門由 rag_answer_gate 控制。
        #
        # 這一節**不呼叫真模型**（把 search_multi 與 answer_with_rag 都換成假的），
        # 所以額度、網路都不影響判定。
        import re as _re

        from app.llm import deepseek_client as _ds

        _saved_gate = kb_db.get_global_param_overrides().get("rag_answer_gate")
        _orig_search = kb_search.search_multi
        _orig_rag = _ds.DeepSeekClient.answer_with_rag
        _calls = {"n": 0}

        def _fake_result(gate):
            h = kb_search.Hit(
                chunk_id=900001, doc_id=-1, collection_id=None,
                doc_title="自檢手冊", doc_filename="selfcheck.docx", doc_type="docx",
                ordinal=0, text="![圖 1 服務端日誌截圖](/media/selfcheck.png)",
                kind="image", asset_id=900002)
            asset = {"id": 900002, "caption": "圖 1 服務端日誌截圖",
                     "url": "/media/selfcheck.png", "rel_path": "kb_assets/selfcheck.png"}
            return kb_search.SearchResult(
                query="日誌長什麼樣", queries=["日誌長什麼樣"], top1=0.641,
                passed=True, reason="ok", threshold=0.25,
                params={"rag_answer_gate": gate, "max_context_chars": 6000},
                hits=[h], candidates=[h], assets=[asset])

        # ⚠️ 必須是**同步**函式：`_rag_try` 是用 `asyncio.to_thread(search_multi, …)`
        #    呼叫的，async 的話會回一個 coroutine（踩過）。
        def _fake_search(queries, **kw):
            gate = str(kb_db.get_global_param_overrides().get("rag_answer_gate")
                       or "image")
            return _fake_result(gate)

        async def _fake_rag(self, question, rag_context, flavor="dm",
                            post_context="", require_answer=False):
            _calls["n"] += 1
            if _calls.get("stubborn"):
                return _ds.NO_ANSWER_SENTINEL          # 連第二次機會也不領情
            if require_answer:
                m = _re.search(r"!\[[^\]\n]*\]\([^)\s]+\)", rag_context or "")
                return ("服務器端轉發成功的日志如下圖所示。\n" + (m.group(0) if m else ""))
            return _ds.NO_ANSWER_SENTINEL              # 第一次：模型說資料不足

        kb_search.search_multi = _fake_search
        _ds.DeepSeekClient.answer_with_rag = _fake_rag
        try:
            for gate, want_hit in (("image", True), ("on", True), ("off", False)):
                kb_db.set_params(None, {"rag_answer_gate": gate})
                kb_search.invalidate()
                a = ask("服務端日誌長什麼樣？", layers=["rag"])
                if want_hit:
                    check(a.tier == "rag" and bool(a.assets),
                          f"gate={gate}：命中帶圖時**不讓模型否決**（照樣回覆）",
                          f"tier={a.tier} assets={[x.get('id') for x in (a.assets or [])]}")
                    check("![" in (a.text or ""),
                          f"gate={gate}：回覆保留了圖片標記（前端渲染／附圖通道）",
                          repr((a.text or "")[:60]))
                else:
                    check(a.tier == "miss",
                          f"gate={gate}：完全聽模型（舊行為，維持不硬掰）",
                          f"tier={a.tier} detail={str(a.detail)[:60]}")
            check(_calls["n"] >= 2, "模型說資料不足時有給第二次機會（追問一次）",
                  f"answer_with_rag 共 {_calls['n']} 次")

            # 最硬的保險：模型**兩次**都拒絕，但命中的就是圖 → 用題注組回覆，不准不回
            _calls["stubborn"] = True
            kb_db.set_params(None, {"rag_answer_gate": "image"})
            kb_search.invalidate()
            a = ask("服務端日誌長什麼樣？", layers=["rag"])
            check(a.tier == "rag" and "![" in (a.text or ""),
                  "★ 模型兩次都拒絕時，改用圖片題注作答（絕不因為「模型說答不出」而不回）",
                  f"tier={a.tier} text={repr((a.text or '')[:70])}")
            check("圖 1 服務端日誌截圖" in (a.text or ""),
                  "後備回覆用了題注當人話說明", repr((a.text or "")[:60]))
            check(bool(a.assets), "後備回覆也帶上圖片（附圖通道會真的發出去）",
                  [x.get("id") for x in (a.assets or [])])
        finally:
            kb_search.search_multi = _orig_search
            _ds.DeepSeekClient.answer_with_rag = _orig_rag
            kb_db.set_params(None, {"rag_answer_gate": _saved_gate})
            kb_search.invalidate()
        check(kb_db.get_global_param_overrides().get("rag_answer_gate") == _saved_gate,
              "自檢沒有改動正式的 rag_answer_gate 設定",
              f"{kb_db.get_global_param_overrides().get('rag_answer_gate')}")

        print("\n[5] 不相關問題 → 降級人工（不硬掰）…")
        a = ask("請問你們有賣冰箱嗎？", layers=["rag"])
        check(a.tier == "miss" and not a.hit, "無關問題降級人工",
              f"tier={a.tier} reason={a.reason} score={a.score}")
        print(f"       說明：{str(a.detail)[:110]}")
        check(len(a.matched_rows or []) >= 1,
              "未命中仍回候選明細（調參最需要的線索）",
              f"{len(a.matched_rows or [])} 筆，top1={a.score}")

        print("\n[6] 四層預設順序（layers 不給 = 依 DB 開關）…")
        a = ask("A 款藍牙耳機多少錢？")
        check_or_env(a.tier in ("rag", "table"), "不指定 layers 時會依開關跑",
                     str(a.detail or ""), f"tier={a.tier}")

        print("\n[7] layers 參數語意…")
        check(ask("測試", layers=[]) .tier == "miss", "layers=[] → 直接降級人工")
        # 非法層名：引擎不會炸（UI 端會先擋），只確認它不會誤跑
        a = ask("測試", layers=["nope"])
        check(a.tier == "miss", "layers 只含未知層名 → 沒有任何一層會跑",
              f"tier={a.tier}")
        check(ask("", layers=None).tier == "miss", "空問句 → 降級人工")

        print("\n[8] 紀錄端點（matched_rows 明細）…")
        marker = f"selftest-{int(time.time())}"
        autoreply_db.insert_log({
            "business_id": BIZ, "platform": CH, "scope": SCOPE, "tier": "rag",
            "reason": "sent", "question": "測試問句", "answer": "測試答案",
            "detail": "自檢寫入", "send_status": "sent", "score": 0.5,
            "matched_rows": [{"chunk_id": 1, "doc_id": 1, "doc": "x.md",
                              "heading": "章節", "page": 1, "vec": 0.6, "lex": 0.3,
                              "final": 0.5, "picked": True, "text": "片段"}],
            "end_user_sender_id": marker,
        })
        new_id = autoreply_db.query(
            "SELECT id FROM autoreply_log WHERE end_user_sender_id=%s ORDER BY id DESC LIMIT 1",
            (marker,))[0]["id"]
        log_ids.append(int(new_id))
        row = autoreply_db.get_log(new_id)
        check(row and row.get("tier") == "rag" and len(row.get("matched_rows") or []) == 1,
              f"get_log({new_id}) 回召回明細", f"tier={row.get('tier') if row else None}")
        lst = autoreply_db.list_logs(BIZ, limit=5)
        logs = lst.get("logs") or []
        check(bool(logs) and "matched_rows" not in logs[0]
              and "has_matched_rows" in logs[0],
              "列表刻意不帶 matched_rows（改用 has_matched_rows 布林，保持輕）",
              f"{len(logs)} 筆 / total={lst.get('total')}")
        check(bool(logs) and logs[0].get("has_matched_rows") is True,
              "剛剛那筆在列表上被標成「有明細可展開」")
        check(autoreply_db.get_log(99999999) is None, "不存在的紀錄回 None")

        print("\n[9] FAQ 內文檢索（含參數化陷阱回歸）…")
        got = autoreply_db.list_faqs(BIZ, q=FAQ_Q)
        check(any(x["question"] == FAQ_Q for x in got), "依問題內文找到 FAQ",
              f"{len(got)} 條")
        got = autoreply_db.list_faqs(BIZ, q="自檢")
        check(len(got) >= 1, "中文關鍵詞可搜", f"{len(got)} 條")
        # ⚠️ 這是最容易踩的坑：SQL 裡若寫字面 LIKE '%x%'，參數化後 % 會被當佔位符 → 500
        bad = []
        for weird in ("100%", "%", "_", "a%b_c", "'"):
            try:
                autoreply_db.list_faqs(BIZ, q=weird)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{weird!r}: {e}")
        check(not bad, "含 % / _ / 引號 的查詢都安全（ILIKE 已參數化）", str(bad))

        print("\n[10] 層開關（rag）…")
        check(reply_engine.rag_layer_enabled(BIZ) is True, "預設啟用知識庫層")
        reply_engine.set_layer_enabled(BIZ, "rag", False)
        check(reply_engine.rag_layer_enabled(BIZ) is False, "可以停用知識庫層")
        a = ask("A 款藍牙耳機多少錢？")
        check(a.tier != "rag", "停用後不會命中知識庫層",
              f"tier={a.tier} detail={str(a.detail)[:60]}")
        reply_engine.set_layer_enabled(BIZ, "rag", True)
        check(reply_engine.rag_layer_enabled(BIZ) is True, "可以重新啟用知識庫層")

    finally:
        print("\n[11] 清理…")
        if log_ids:
            autoreply_db.execute("DELETE FROM autoreply_log WHERE id = ANY(%s)",
                                 (log_ids,))
        purge_business(BIZ)
        reply_engine.invalidate_cache()
        left = kb_db.list_collections(business_id=BIZ)
        check(not left, "自檢商家已被清空（庫／文件／塊／FAQ／紀錄全刪）",
              f"剩 {len(left)} 個庫")

    return report("自動回復四層引擎自檢")


if __name__ == "__main__":
    raise SystemExit(main())
