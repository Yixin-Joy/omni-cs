#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RAG 知識庫檢索自檢（P2）：混合召回的 top-1 命中率、分數分佈與多查詢合併。

可重複執行：自建一份小知識庫（先清空重建）→ 跑一組已知問句 → 印出召回明細。
**重點是分數分佈**：`min_score` 的預設值 0.55 是估的，這支腳本印出真實分佈，
方便你在 UI 的「參數配置」裡校準。

用法：
    python kb_search_check.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import kb_db
import kb_ingest
import kb_search

CHECK_COLLECTION = "RAG 檢索自檢庫"

# 迷你知識庫：檔名 → 內容（用 md，標題階層會變成前綴，這是最貼近實務的形狀）
DOCS = {
    "耳機產品手冊.md": """# 藍牙耳機產品手冊

## A 款耳機
A 款藍牙耳機建議售價 1280 元，續航 30 小時，支援藍牙 5.3 與主動降噪。
充電盒採用 Type-C 介面，完整充電約 90 分鐘。

## B 款耳機
B 款藍牙耳機建議售價 890 元，支援主動降噪（ANC），續航 20 小時。
重量僅 4.2 公克，適合運動配戴。

## 保固條款
耳機自購買日起提供一年原廠保固，非人為損壞免費維修。
人為損壞（進水、摔裂）需自費更換零件。
""",
    "售後政策.md": """# 售後政策

## 退貨規則
商品到貨後 7 天內，包裝完整可無理由退貨。
退貨需附上發票與原廠包裝盒，缺件恕不受理。

## 運費說明
單筆訂單滿 500 元免運費，未滿則收 60 元運費。
離島地區（澎湖、金門、馬祖）另收 100 元。

## 付款方式
支援信用卡、ATM 轉帳、超商取貨付款三種方式。
信用卡可分期，3 期零利率。

## 客服時間
線上客服時間為週一至週五 09:00-18:00，國定假日休息。
""",
    "門市資訊.csv": "門市名稱,城市,電話,營業時間\n台北旗艦店,台北市,02-2712-3456,11:00-21:30\n"
                    "台中中港店,台中市,04-2251-7788,11:00-21:00\n"
                    "高雄漢神店,高雄市,07-215-9900,11:00-21:30\n",
}

# 已知問句 → 期望命中的關鍵片段（取前幾個字做比對）
CASES = [
    ("藍牙耳機多少錢", "1280"),
    ("B 款耳機價格", "890"),
    ("不想要了可以退嗎", "7 天內"),
    ("運費怎麼算", "免運費"),
    ("保固多久", "一年原廠保固"),
    ("可以用信用卡嗎", "信用卡"),
    ("客服幾點上班", "09:00-18:00"),
    ("高雄有門市嗎", "高雄"),
]

FAIL = []


def check(cond, label, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)
    return cond


def build_fixtures() -> int:
    cid = None
    for c in kb_db.list_collections():
        if c["name"] == CHECK_COLLECTION:
            cid = c["id"]
            # ⚠️ 整庫被停用時 `load_ready_chunks` 會一塊都撈不到（`cc.enabled` 那道閘門），
            #    然後這一整支腳本會「全部 FAIL」看起來像程式壞了 —— 其實只是自檢庫被停用。
            #    自檢庫是自己建的測試資料，這裡直接確保它是開的（並講出來，不偷偷改設定）。
            if not c.get("enabled", True):
                print(f"  （自檢庫 #{cid} 原本是停用的 → 自動啟用，跑完可自行停用）")
                kb_db.update_collection(cid, {"enabled": True})
    if not cid:
        cid = kb_db.create_collection(CHECK_COLLECTION, "RAG 檢索自檢用，可刪")["id"]
    old = kb_db.list_documents(cid)
    for d in old:
        kb_ingest.delete_document(int(d["id"]))
    if old:
        print(f"  （清掉上次自檢的 {len(old)} 份文件）")

    tmp = os.path.join(HERE, "_p1_samples")
    os.makedirs(tmp, exist_ok=True)
    for name, text in DOCS.items():
        p = os.path.join(tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        saved = kb_ingest.save_upload(name, text.encode("utf-8"), "text/markdown", cid)
        did = saved["doc"]["id"]
        kb_ingest.parse_and_chunk(did)
        kb_ingest.embed_document(did, "pending")
        kb_ingest.activate_document(did)
    kb_search.invalidate()
    return cid


print("=" * 78)
print(" RAG 知識庫檢索自檢（混合召回 top-1 命中率 + 分數分佈）")
print("=" * 78)

print("\n[1] 建立自檢知識庫…")
t0 = time.time()
cid = build_fixtures()
st = kb_search.index_stats(cid)
check(st["chunks"] > 0 and st["dim"] == 768,
      f"知識庫 #{cid} 索引就緒：{st['chunks']} 塊 / {st['docs']} 份 / dim {st['dim']}",
      f"{time.time() - t0:.1f}s")

print("\n[2] 已知問句的召回（top-1 命中率）…")
hit = 0
scores = []
rows_out = []
for q, expect in CASES:
    r = kb_search.search(q, collection_id=cid)
    top = r.hits[0] if r.hits else None
    ok = bool(top) and expect.replace(" ", "") in (top.text or "").replace(" ", "")
    hit += ok
    if r.candidates:
        scores.append(r.candidates[0].final)
    rows_out.append((q, r.top1, r.passed, (top.source if top else "—"), ok,
                     (r.candidates[0].vec if r.candidates else 0.0),
                     (r.candidates[0].lex if r.candidates else 0.0),
                     (top.text[:34] if top else "")))
    print(f"    {'✓' if ok else '✗'} 「{q}」 top1={r.top1:.3f} "
          f"(vec={rows_out[-1][5]:.3f} lex={rows_out[-1][6]:.3f}) "
          f"{'入選' if r.passed else '未過門檻'} ← {rows_out[-1][3]}")
    print(f"        {rows_out[-1][7]}")

rate = hit / len(CASES)
check(rate >= 0.8, f"top-1 命中率 {hit}/{len(CASES)} = {rate:.0%}（門檻 ≥ 80%）")

print("\n[3] 分數分佈（校準 min_score 的依據）…")
if scores:
    s = sorted(scores)
    print(f"    top-1 分數：min {s[0]:.3f} / 中位 {s[len(s) // 2]:.3f} / max {s[-1]:.3f}")
    print(f"    目前 min_score 預設 = {kb_search.params_for(cid)['min_score']}")
    below = [x for x in scores if x < kb_search.params_for(cid)["min_score"]]
    print(f"    以預設門檻判斷：{len(scores) - len(below)}/{len(scores)} 條會入選、"
          f"{len(below)} 條降級人工")
    check(True, "分數分佈已印出（請依此在 UI 調整 min_score）")

print("\n[4] 不相關問題應該要「不通過」（這是防硬掰的關鍵）…")
off_topic = ["你們有賣冰箱嗎", "如何申請房屋貸款", "台北捷運末班車時間"]
passed_off = 0
for q in off_topic:
    r = kb_search.search(q, collection_id=cid)
    if r.passed:
        passed_off += 1
    print(f"    「{q}」 top1={r.top1:.3f} → {'誤判入選 ⚠️' if r.passed else '正確擋下'}")
check(passed_off == 0, f"不相關問句全數擋下（{len(off_topic) - passed_off}/{len(off_topic)}）",
      "若這裡誤判，請把 min_score 調高")

print("\n[5] 參數與過濾…")
r = kb_search.search("藍牙耳機多少錢", collection_id=cid, top_k=1)
check(len(r.hits) == 1, "top_k=1 只回 1 塊")
r = kb_search.search("耳機", collection_id=cid, max_per_doc=1)
docs = [h.doc_id for h in r.hits]
check(len(docs) == len(set(docs)), "max_per_doc=1 時每份文件最多 1 塊",
      f"{len(r.hits)} 塊 / {len(set(docs))} 份文件")
r = kb_search.search("藍牙耳機多少錢", collection_id=cid, min_score=0.99)
check(not r.passed and r.reason == "below_threshold",
      "門檻 0.99 → 正確判定「不通過」", f"top1={r.top1:.3f} reason={r.reason}")
r = kb_search.search("")
check(r.reason == "empty_query", "空查詢回 empty_query")

print("\n[6] 上下文拼接…")
r = kb_search.search("退貨規則跟運費", collection_id=cid)
ctx = r.context(2000)
check(ctx.startswith("[1]（") and "[2]" in ctx, "context() 帶編號與來源",
      f"{len(ctx)} 字 / {len(r.hits)} 段")
r2 = kb_search.search("退貨規則跟運費", collection_id=cid)
check(r.context(60) != r2.context(60) or len(r.context(60)) <= 60 + 200,
      "max_context_chars 會截斷（不無限長）", f"{len(r.context(120))} 字")

print("\n[7] 跨知識庫檢索（collection_id=None）…")
r = kb_search.search("藍牙耳機多少錢")
check(bool(r.hits) and len(r.per_collection) >= 1,
      f"全域檢索命中 {len(r.hits)} 塊，涵蓋 {len(r.per_collection)} 個知識庫",
      f"top1={r.top1:.3f}")

print("\n[8] 多查詢檢索（勾「查詢改寫」時走的路）…")
# 8a 只有一條 → 直接退化成單查詢，結果要一致
one = kb_search.search_multi(["藍牙耳機多少錢"], collection_id=cid)
base = kb_search.search("藍牙耳機多少錢", collection_id=cid)
check(abs(one.top1 - base.top1) < 1e-9 and len(one.hits) == len(base.hits),
      "只有 1 條查詢時等於 search()", f"top1={one.top1:.3f} / {len(one.hits)} 塊")

# 8b 錯別字救援：原問句（打錯字）撈不到，靠改寫句補回來
TYPO = "藍芽耳機多少千"
FIXED = "藍牙耳機多少錢"
r_typo = kb_search.search(TYPO, collection_id=cid)
r_multi = kb_search.search_multi([TYPO, FIXED], collection_id=cid)
print(f"    純錯字問句「{TYPO}」：top1={r_typo.top1:.3f}，入選 {len(r_typo.hits)} 塊")
print(f"    加上改寫句「{FIXED}」：top1={r_multi.top1:.3f}，入選 {len(r_multi.hits)} 塊")
check(r_multi.top1 > r_typo.top1, "多查詢後最高分上升（改寫句真的救了回來）",
      f"{r_typo.top1:.3f} → {r_multi.top1:.3f}")

# 8c 合併語意：每個 chunk 只出現一次、取最高分、top_k 只套一次
r_top1 = kb_search.search_multi([TYPO, FIXED], collection_id=cid, top_k=1)
check(len(r_top1.hits) == 1, "多查詢時 top_k 只套一次（不會各查各的變成 N 倍）",
      f"top_k=1 → {len(r_top1.hits)} 塊")
check(r_top1.top1 == r_multi.top1, "top_k 不影響 top1（取的是最高分那塊）",
      f"{r_top1.top1:.3f} vs {r_multi.top1:.3f}")
ids = [h.chunk_id for h in r_multi.candidates]
check(len(ids) == len(set(ids)), "候選不重複（同一塊只留一份）",
      f"{len(ids)} 塊 / {len(set(ids))} 不重複")
check(sorted(r_multi.queries) == sorted([TYPO, FIXED]),
      "SearchResult.queries 記下這次實際用的查詢（原句固定排第 0）",
      str(r_multi.queries))
check(r_multi.query == TYPO, "SearchResult.query 仍是客戶原問句", r_multi.query)

# 8d 溯源：每一塊都記得「被哪幾條查詢撈到」
tagged = [h for h in r_multi.candidates if h.queries]
check(len(tagged) == len(r_multi.candidates), "每個候選塊都帶 queries（溯源用）",
      f"{len(tagged)}/{len(r_multi.candidates)}")
by_fixed = [h for h in r_multi.candidates if FIXED in h.queries]
check(bool(by_fixed), "改寫句確實參與了召回（有塊被它撈到）", f"{len(by_fixed)} 塊")
# 這組語料裡錯字句與正確句共用「耳機／多少」等詞，所以原句也撈得到同一批 →
# 「只有改寫句撈到」不必然存在，只印出來當觀察（真實客服語料才容易出現純錯字問句）。
only_fixed = [h for h in r_multi.candidates if FIXED in h.queries and TYPO not in h.queries]
print(f"    其中「只有改寫句撈到」的：{len(only_fixed)} 塊"
      "（這組語料的原句也撈得到同一批，數字為 0 屬正常；重點是上面的分數有被拉高）")
check(all(FIXED in h.queries or TYPO in h.queries for h in r_multi.candidates),
      "候選塊的 queries 都落在這次送出的查詢集合內")
check(bool(kb_search.search_multi([], collection_id=cid).reason == "empty_query"),
      "空查詢清單 → empty_query（不會炸）")
check(isinstance(kb_search.query_rewrite_enabled(), bool),
      "query_rewrite_enabled() 讀得到全域參數",
      f"目前 = {kb_search.query_rewrite_enabled()}")

print("\n" + "=" * 78)
if FAIL:
    print(f" 結果：{len(FAIL)} 項失敗")
    for f in FAIL:
        print("   ✗", f)
else:
    print(" 結果：全部通過 ✅")
print("=" * 78)
print(f"\n自檢資料：collection #{cid}（{CHECK_COLLECTION}）")
sys.exit(1 if FAIL else 0)
