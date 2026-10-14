#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨知識庫檢索的三個疑點自檢：參數歸屬 / 降級門檻 / 索引重建成本。

為什麼要這支：`kb_search.py` 有三處「看起來有處理、其實沒定義清楚」的地方，
光讀碼看不出嚴重性，要用**真實資料量與真實問句**跑一遍才知道：

    [1] 檢索參數落在哪一層？（2026-10-08 起改成**全域**，這裡驗證它真的不分庫）
    [2] embedding 掛掉降級成純字面檢索時，門檻該是多少？（現在是 ×0.35）
    [3] `_INDEX_TTL = 5 秒` 過期後的「下一次請求」要付多少成本？併發時會不會重複付？
    [4] 未分組（`collection_id IS NULL`）的文件，全域檢索撈不撈得到？

這是**診斷腳本**，不是驗收閘門 —— 所以判定用 `[洞]` / `[OK]`，不是 PASS/FAIL。
結尾會列出確認存在的洞。

★ CRM 側：這支是**唯一**還需要「跨商家」視角的自檢 —— 它驗的正是「不給
  `business_id` 時，候選池會跨商家」這件事（設計案 §9.4 的洞）。
  它直接用 crm_db 裡既有資料（`Epro_test` 那批），不建／不刪知識庫。

**可重複執行**：會暫時寫 kb_param，跑完（含中途失敗）一定還原成原狀。
只會另外自建／刪掉一份「未分組文件」（[4] 用），其餘資料不動。

跑法（cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/kb_scope_check.py
"""
import json
import os
import sys
import threading
import time

from _ck import ROOT                      # noqa: E402  （同時完成 sys.path bootstrap）

from app.llm import faq_matcher
from app.rag import kb_db
from app.rag import kb_embed
from app.rag import kb_ingest
from app.rag import kb_search
from app.rag import rag_config

HOLES: list[str] = []
TOUCHED = ("top_k", "min_score")


def hr(t):
    print("\n" + "=" * 78)
    print(" " + t)
    print("=" * 78)


def verdict(hole: str, label: str, confirmed: bool, extra: str = ""):
    """印出一個判定。confirmed=True 代表「這個洞成立」。"""
    print(f"  [{'洞' if confirmed else 'OK'}] {hole} {label}"
          + (f"   {extra}" if extra else ""))
    if confirmed:
        HOLES.append(f"{hole} {label}")
    return confirmed


def note(label, extra=""):
    print(f"  [資訊] {label}" + (f"   {extra}" if extra else ""))


def sanity(cond, label, extra=""):
    """腳本自身的健全性（不是洞的判定）。"""
    print(f"  [{'ok' if cond else '!!'}] {label}" + (f"   {extra}" if extra else ""))
    return cond


RELEVANT = ["藍牙耳機多少錢", "B 款耳機價格", "不想要了可以退嗎", "運費怎麼算",
            "保固多久", "可以用信用卡嗎", "客服幾點上班", "高雄有門市嗎"]
IRRELEVANT = ["你們有賣冰箱嗎", "如何申請房屋貸款", "台北捷運末班車時間"]


def restore_global(saved: dict, business_id=None) -> None:
    """把我動過的檢索參數還原：saved 裡沒有 → 傳 None 讓 set_params 刪掉覆寫。

    檢索參數落在「該商家的全域」（collection_id IS NULL），所以還原也寫 None。
    ★ 必須帶 business_id —— 少了它會寫到「無主」那批，真實商家的值就還不回來。
    """
    kb_db.set_params(None, {k: saved.get(k) for k in TOUCHED}, business_id=business_id)


# ======================================================================
# [1] 檢索參數落在哪一層？（2026-10-08 起：全域，不分庫）
# ======================================================================

def section1(cids: list[int]) -> None:
    hr("[1] 檢索參數的歸屬（全域，所有知識庫共用）")
    rows = kb_db.load_ready_chunks(None)
    if not rows:
        sanity(False, "沒有可檢索的塊，跳過")
        return
    note("可檢索的庫", str([c for c in dict.fromkeys(r["collection_id"] for r in rows)]))
    # ★ 檢索參數按商家隔離 → 存/還原都必須指定**同一個商家**，
    #   否則（寫 A 商家、還原 B 商家）會把真實商家的參數改壞。
    _biz0 = kb_db.business_of_collection(cids[0]) if cids else None
    saved = kb_db.get_global_param_overrides(_biz0)
    try:
        kb_db.set_params(None, {"top_k": 3, "min_score": 0.42}, business_id=_biz0)
        kb_search.invalidate()
        r = kb_search.search("藍牙耳機多少錢")
        note("設「全域」top_k=3 / min_score=0.42 → 檢索回報",
             json.dumps({k: r.params.get(k) for k in ("top_k", "min_score", "max_per_doc")}))
        note("實際用的 threshold", str(r.threshold))
        verdict("1a", "全域檢索的參數由「doc_id 最小的那個庫」決定（不是全域統一的一組）",
                not (int(r.params.get("top_k") or 0) == 3
                     and abs(float(r.params.get("min_score") or 0) - 0.42) < 1e-9),
                f"→ 實測 top_k={r.params.get('top_k')} min_score={r.params.get('min_score')}"
                f"（設的是 3 / 0.42）")

        if cids:
            c0 = cids[0]
            kb_db.set_params(c0, {"top_k": 9, "min_score": 0.77})
            # ⚠️ 上面這行會寫到 c0 所屬商家的全域（檢索參數不分庫、只分商家），
            #    所以 finally 的還原一定要針對**同一個商家**（見下一段的 restore）。
            kb_search.invalidate()
            r2 = kb_search.search("藍牙耳機多少錢")
            own = kb_db.get_param_overrides(c0)
            verdict("1b", "對著某個知識庫寫檢索參數會變成「該庫專屬」（每庫一套）",
                    not (int(r2.params.get("top_k") or 0) == 9 and "top_k" not in own),
                    f"→ 實測 top_k={r2.params.get('top_k')}，該庫的 kb_param key={sorted(own)}"
                    "（空 = 檢索參數沒落進這個庫）")
    finally:
        restore_global(saved, _biz0)
        kb_search.invalidate()
    after = kb_db.get_global_param_overrides(_biz0)
    sanity(all(after.get(k) == saved.get(k) for k in TOUCHED),
           "已還原該商家的檢索參數",
           f"biz={_biz0} top_k={after.get('top_k')} min_score={after.get('min_score')}")

    mixed = set()
    for q in RELEVANT:
        mixed |= {h.collection_id for h in kb_search.search(q).candidates}
    verdict("1c", "★ 不給 business_id 的檢索，候選池會跨知識庫（＝跨商家）—— "
                  "所以**正式流程一律要帶 business_id**（CRM 的引擎有帶；"
                  "只有內部／除錯與 kb_console 未帶時才會這樣）",
            len(mixed) >= 2,
            "→ 這次命中過的庫：" + str(sorted(mixed, key=lambda x: (x is None, x))))


# ======================================================================
# [2] 降級成純字面檢索時，門檻對不對？
# ======================================================================

def section2() -> None:
    hr("[2] embedding 掛掉 → 純字面檢索的門檻")
    normal = float(kb_search.params_for(None).get("min_score") or 0)
    wl = float(rag_config.RAG_LEXICAL_WEIGHT)
    scaled = round(normal * wl, 4)
    note("目前（全域）min_score", str(normal))
    note("降級做法（kb_search 的降級分支）", f"threshold = {normal} × {wl} = {scaled}")
    note("推導", f"降級時 final = {wl} × lex → 通過條件等價於 lex >= {normal}"
         "（拿融合尺度的數字去量字面尺度）")

    orig = kb_embed.get_provider

    def boom(model_name=None):
        raise kb_embed.EmbedError("自檢模擬：embedding 不可用")
    kb_embed.get_provider = boom
    try:
        kb_search.invalidate()
        rows_rel = [(q,) + tuple(_probe(q)) for q in RELEVANT]
        rows_irr = [(q,) + tuple(_probe(q)) for q in IRRELEVANT]
    finally:
        kb_embed.get_provider = orig
        kb_search.invalidate()

    if not rows_rel:
        sanity(False, "降級模式沒跑到任何查詢，跳過")
        return

    for title, rows in (("相關問句（期望通過）", rows_rel), ("不相關問句（期望擋下）", rows_irr)):
        print(f"\n  {title}：")
        print(f"    {'問句':<20}{'final':>8}{'門檻':>9}{'lex':>8}  通過")
        for q, t1, th, ok, lex in rows:
            print(f"    {q:<20}{t1:>8.3f}{th:>9.3f}{lex:>8.3f}  {'是' if ok else '否'}")

    # ★ 2a 用「實際跑出來的降級門檻」對比推導值 —— 不寫死 0.28，
    #   否則使用者在 UI 調過 min_score 之後這條會變成假陽性。
    degraded_thr = float(rows_rel[0][2])
    verdict("2a", "降級門檻不是獨立校準的，是「融合門檻 × 權重」反推出來的",
            abs(degraded_thr - scaled) < 1e-6,
            f"→ 實測降級門檻 {degraded_thr:.4f}；推導值 {scaled}"
            f"（= {normal} × {wl}）")

    rel_ok = sum(1 for x in rows_rel if x[3])
    irr_ok = sum(1 for x in rows_irr if x[3])
    lr = [x[4] for x in rows_rel]
    li = [x[4] for x in rows_irr]
    print()
    note("相關問句通過率", f"{rel_ok}/{len(rows_rel)}")
    note("不相關問句通過率", f"{irr_ok}/{len(rows_irr)}（越少越好）")
    note("lex 分佈（相關）", f"min {min(lr):.3f} / max {max(lr):.3f}")
    note("lex 分佈（不相關）", f"min {min(li):.3f} / max {max(li):.3f}")
    verdict("2b", "同一個門檻在兩種尺度上不可通用：降級模式下相關問句會被誤擋",
            rel_ok < len(rows_rel), f"→ 相關只過 {rel_ok}/{len(rows_rel)}")
    probe = kb_search.Hit(chunk_id=0, doc_id=0, collection_id=None, doc_title="",
                          doc_filename="", doc_type="", ordinal=0, text="")
    verdict("2c", "降級時的召回明細沒有 degraded 標記，事後分不出這筆是降級命中",
            "degraded" not in json.dumps(probe.as_dict()) and
            "degraded" not in json.dumps(rows_rel[0][1:]),
            "→ Hit.as_dict() 沒有這個欄位")
    # 降級模式真正的問題不是「門檻偏高/偏低」，是**語義改寫型問句在字面尺度上沒有信號**
    weak = [(q, lex) for q, _t, _th, _ok, lex in rows_rel if lex < 0.05]
    verdict("2d", "字面尺度擋不住的東西：改寫型問句（原問句的詞根本不在文件裡）",
            bool(weak),
            "、".join(f"{q}(lex={lex:.3f})" for q, lex in weak)
            + " → 調門檻救不了，只能靠向量或改寫")


def _probe(q: str):
    """在降級狀態下跑一次檢索，回傳 (top1, threshold, passed, top1的lex)。"""
    r = kb_search.search(q)
    lex = r.candidates[0].lex if r.candidates else 0.0
    return r.top1, r.threshold, r.passed, lex


def _warm() -> None:
    """背景執行緒用：暖一次索引。例外要吞掉 —— 不然 thread 的 traceback 會蓋掉輸出。"""
    try:
        kb_search._index(None)
    except Exception as e:  # noqa: BLE001
        print(f"  [!!] 暖索引失敗：{e}")

# ======================================================================
# [3] 索引 TTL：重載成本 & 併發時會不會重複付
# ======================================================================

def section3() -> None:
    hr("[3] _INDEX_TTL 過期後的重載成本與併發行為")
    n = len(kb_db.load_ready_chunks(None))
    note("可檢索塊數", str(n))
    note("_INDEX_TTL", f"{kb_search._INDEX_TTL} 秒（kb_search.py:41）")
    note("_PARAM_TTL", f"{kb_search._PARAM_TTL} 秒")
    if not n:
        sanity(False, "沒有可檢索的塊，跳過")
        return

    kb_search.invalidate()
    t0 = time.time()
    idx = kb_search._index(None)
    cold = (time.time() - t0) * 1000
    note("冷啟一次 _index(None)", f"{cold:.1f} ms（{n} 塊 / dim {idx['dims']}）")
    kb_search.invalidate()
    t0 = time.time()
    kb_search._index(None)
    reload_ms = (time.time() - t0) * 1000
    note("每次 TTL 過期要付的重載成本（jieba 已暖）", f"{reload_ms:.1f} ms")

    db1 = db2 = 0.0
    for k in range(2):
        t0 = time.time()
        kb_db.load_ready_chunks(None)
        ms = (time.time() - t0) * 1000
        if k == 0:
            db1 = ms
        else:
            db2 = ms
    note("`load_ready_chunks(None)`（DB + 反序列化 768 維向量）",
         f"第 1 次 {db1:.1f} ms → 第 2 次 {db2:.1f} ms"
         f"（差額＝連線/暖機，剩下的才是隨塊數長的成本；現在 {n} 塊）")
    note("常駐記憶體", f"向量矩陣 {idx['mat'].nbytes / 1024:.1f} KB，另加 {n} 筆文本 + jieba 分詞結果")

    sample = idx["rows"][0]["text"] or ("測" * 500)
    t0 = time.time()
    for _ in range(20):
        faq_matcher.preprocess(sample)
    per_jieba = (time.time() - t0) / 20 * 1000
    note("單塊 jieba 預處理", f"{per_jieba:.2f} ms/塊（{len(sample)} 字）")

    # 合成資料實測規模化成本。⚠ 用**語料庫裡的真實塊文本**拼接，
    #   不要用「同一個字重複 500 次」——那種字串對 jieba 不具代表性，會把成本量得誇張。
    base = [r["text"] for r in idx["rows"] if r["text"]] or ["測" * 100]
    synth, i = [], 0
    while len(synth) < 2000:
        t = ""
        while len(t) < 500:
            t += base[i % len(base)]
            i += 1
        synth.append(t[:500])
    chars = sum(len(t) for t in synth)
    t0 = time.time()
    for s in synth:
        faq_matcher.preprocess(s)
    cpu2k = time.time() - t0
    note(f"實測 {len(synth)} 塊（共 {chars} 字，真實語料拼接）的 jieba 預處理",
         f"{cpu2k:.2f} 秒（{cpu2k * 1000 / len(synth):.2f} ms/塊）")
    per_kchar = cpu2k * 1000 * 1000 / max(chars, 1)      # ms / 千字
    kchars_10k = 10000 * 500 / 1000                      # 10000 塊 × 500 字 = 5000 千字
    est_10k = per_kchar * kchars_10k / 1000              # 秒
    note("換算", f"{per_kchar:.2f} ms / 千字")
    note("外推 10000 塊 × 500 字（＝5000 千字）", f"約 {est_10k:.1f} 秒"
         "（每次 TTL 過期都要重付）")

    import numpy as _np
    big = _np.random.rand(10000, idx["dims"] or 768).astype("float32")
    q = _np.random.rand(idx["dims"] or 768).astype("float32")
    t0 = time.time()
    for _ in range(5):
        big @ (q / _np.linalg.norm(q))
    cos_ms = (time.time() - t0) / 5 * 1000
    note("實測 10000×768 的 cosine（矩陣乘一次）", f"{cos_ms:.1f} ms（這部分很便宜）")
    note("矩陣常駐記憶體", f"10000 塊 ≈ {big.nbytes / 1024 / 1024:.1f} MB")

    # 併發驚群
    calls = {"n": 0, "lock": threading.Lock()}
    orig = kb_db.load_ready_chunks

    def slow(*a, **kw):
        with calls["lock"]:
            calls["n"] += 1
        time.sleep(0.2)      # 本機幾十塊只要 ~1ms，看不出交錯；補一段模擬真實 DB round-trip
        return orig(*a, **kw)
    kb_db.load_ready_chunks = slow
    try:
        kb_search.invalidate()
        threads = [threading.Thread(target=_warm) for _ in range(8)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        herd = (time.time() - t0) * 1000
    finally:
        kb_db.load_ready_chunks = orig
        kb_search.invalidate()

    note("8 條執行緒同時撞上失效快取 → 實際發出的全量查詢數", str(calls["n"]))
    note("全部跑完耗時", f"{herd:.0f} ms")
    verdict("3a", "沒有 single-flight：取鎖只在讀/寫快取時，重載期間鎖是放開的 → 併發全部重載（驚群）",
            calls["n"] == 8, f"→ {calls['n']}/8 條各自付了一次全量查詢")
    verdict("3b", "TTL 目前是**唯一**的刷新機制，`invalidate()` 只是加速；hook 一漏就靠 5 秒兜底",
            True, "→ 見下方「hook 覆蓋」清單")
    verdict("3d", "重載的大頭不是 DB 查詢，是**全量 jieba 預處理**，而且整段都在請求路徑上",
            est_10k > 1.0,
            f"→ DB {db2:.0f} ms vs jieba {est_10k:.1f} 秒（10000 塊規模）"
            "；並發時再乘上驚群倍數")


def section3_hooks() -> None:
    """列出寫入路徑 vs. 有沒有掛 invalidate（靜態掃原始碼）。"""
    hr("[3-補] invalidate() 的覆蓋率（靜態掃 kb_console.py）")
    src = (ROOT / "app" / "rag" / "kb_console.py").read_text(
        encoding="utf-8").splitlines()
    cur, rows = None, []
    for i, line in enumerate(src, start=1):
        s = line.strip()
        for verb in ("@router.get", "@router.post"):
            if s.startswith(verb):
                cur = (i, s)
        if "kb_search.invalidate" in s and cur:
            rows.append((cur[0], cur[1], i))
    covered = {r[1] for r in rows}
    print(f"  直接掛了 invalidate 的端點 {len(rows)} 個：")
    for a, dec, b in rows:
        print(f"    {dec:<48}（第 {a} 行 → invalidate 於 {b}）")
    print("  ⚠ 另有 1 個間接覆蓋：`_spawn()` 的背景任務 finally（kb_console.py:79）"
          "\n     → /document/reparse、/document/embed、上傳自動解析都靠它")

    writes = []
    for i, line in enumerate(src, start=1):
        s = line.strip()
        if s.startswith("@router.post") and any(
                k in s for k in ("create", "update", "delete", "enabled", "move",
                                 "params", "reparse", "embed", "activate")):
            writes.append((i, s))
    missing = [(i, d) for i, d in writes if d not in covered]
    print(f"\n  寫入類端點中【沒有直接掛】的 {len(missing)} 個：")
    for i, d in missing:
        tail = " ← 由 _spawn 間接覆蓋" if ("reparse" in d or "embed" in d) else " ← 真的沒掛"
        print(f"    {d:<48}（第 {i} 行）{tail}")
    real = [d for _, d in missing if "reparse" not in d and "embed" not in d]
    verdict("3c", "有寫入路徑真的沒掛 invalidate（停用整庫 / 刪庫）→ 目前只能靠 5 秒 TTL 兜底",
            bool(real), "、".join(real) or "無")


# ======================================================================
# [4] 未分組（collection_id IS NULL）的文件，全域檢索撈不撈得到？
# ======================================================================

def section4(biz_id: int | None) -> None:
    hr("[4] 未分組文件的檢索可見性（`collection_id IS NULL`）")
    note("load_ready_chunks 的閘門",
         "d.status='ready' AND d.enabled AND c.embedding_status='done' AND c.enabled "
         "AND (cc.id IS NULL OR cc.enabled)")
    note("不帶 business_id（除錯路徑）", "`cc.id IS NULL` 成立 → 這一關擋不住未分組文件")
    note("帶 business_id（正式路徑）",
         "SQL 走 `d.collection_id IN (SELECT … FROM business_kb_binding)`"
         " → 未分組文件的 collection_id 是 NULL，IN 不成立 → **被擋掉**")

    marker = "量子咖啡機的保固期"
    name = "_scope_check_未分組.md"
    text = ("# 自檢專用未分組文件\n"
            "本文件的唯一用途是驗證未分組文件會不會被第三層檢索撈到。\n"
            f"關鍵詞：{marker}是三年，全球保固。\n")
    did = None
    try:
        saved = kb_ingest.save_upload(name, text.encode("utf-8"), "text/markdown", None)
        did = int(saved["doc"]["id"])
        sanity(saved["doc"].get("collection_id") is None, "自建文件確實是未分組", f"doc#{did}")
        kb_ingest.parse_and_chunk(did)
        kb_ingest.embed_document(did, "pending")
        kb_ingest.activate_document(did)
        kb_search.invalidate()

        rows = kb_db.load_ready_chunks(None)
        ung = [r for r in rows if r["collection_id"] is None]
        verdict("4a", "未分組文件的塊會被 `load_ready_chunks(None)`（**除錯路徑**）撈進索引",
                len(ung) > 0, f"→ {len(ung)} 塊（chunk#{[r['id'] for r in ung]}）")

        if biz_id:
            rows_biz = kb_db.load_ready_chunks(None, biz_id)
            ung_biz = [r for r in rows_biz if r["collection_id"] is None]
            verdict("4b", "★ 正式路徑（帶 business_id）也會撈到未分組文件（＝真的洞）",
                    len(ung_biz) > 0,
                    f"→ 商家 #{biz_id} 的前 500 塊裡有 {len(ung_biz)} 塊是未分組的")
            kb_search.invalidate()
            r_biz = kb_search.search(marker, business_id=biz_id)
            in_biz = [h for h in r_biz.candidates if h.collection_id is None]
            verdict("4c", "帶 business_id 檢索時，未分組文件仍進候選池／被餵給模型",
                    len(in_biz) > 0,
                    f"→ candidates={len(in_biz)}，top1={r_biz.top1:.4f} reason={r_biz.reason}")
        else:
            note("沒有可用的 business_id → 4b/4c 跳過")
    except Exception as e:
        sanity(False, "自建未分組文件失敗（略過）", str(e)[:120])
    finally:
        if did:
            try:
                kb_ingest.delete_document(did)
                kb_search.invalidate()
                left = kb_db.get_document(did)
                sanity(left is None, "已刪掉自檢用的未分組文件", f"doc#{did}")
            except Exception as e:
                sanity(False, "自檢文件清理失敗，請手動刪", f"doc#{did}: {e}")


print("=" * 78)
print(" 跨知識庫檢索 / 降級門檻 / 索引成本 —— 疑點自檢")
print("=" * 78)

cids = [c["id"] for c in kb_db.list_collections() if c["enabled"]]
biz_ids = [c.get("business_id") for c in kb_db.list_collections() if c.get("business_id")]
BIZ_ID = biz_ids[0] if biz_ids else None
print("\n啟用中的知識庫：" + str(cids))
for cid in cids:
    print("  " + str(kb_search.index_stats(cid)))
print("  全域：" + str(kb_search.index_stats(None)))
print(f"  用來測「正式路徑」的商家：#{BIZ_ID}")

section1(cids)
section2()
section3()
section3_hooks()
section4(BIZ_ID)

print("\n" + "=" * 78)
if HOLES:
    print(f" 確認存在的洞：{len(HOLES)} 條")
    for h in HOLES:
        print("   ✗ " + h)
else:
    print(" 沒有發現問題")
print("=" * 78)
