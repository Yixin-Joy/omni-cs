#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RAG 知識庫（P1 端點）自檢：用 TestClient 真打 /console/kb/*。

可重複執行：會自建臨時知識庫、跑完自己刪乾淨。
（入庫管道本身請用 `kb_check.py`。）

用法：
    python kb_api_check.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 專案根
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from app import main
from app.rag import kb_db
from app.rag import rag_config

FAIL = []
TMP = os.path.join(HERE, "_p1_samples")


def check(cond, label, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)
    return cond


def body(res):
    """TestClient 回的是 httpx.Response（用 .content），不是 Starlette Response（.body）。"""
    raw = getattr(res, "content", None)
    if raw is None:
        raw = getattr(res, "body", b"")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def ensure_samples() -> list[str]:
    """自建最小測試檔（不依賴 kb_check.py，兩支腳本各自可獨立執行）。"""
    os.makedirs(TMP, exist_ok=True)
    names = []

    p = os.path.join(TMP, "api_manual.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 商品說明\n\n## 藍牙耳機\nA 款售價 1280 元，續航 30 小時。\n"
                "B 款售價 890 元，支援主動降噪。\n\n## 退貨\n到貨 7 天內包裝完整可無理由退貨。\n")
    names.append("api_manual.md")

    p = os.path.join(TMP, "api_price.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("產品,價格,庫存\n")
        for i in range(1, 21):
            f.write(f"耳機-{i},{800 + i * 13},{50 + i}\n")
    names.append("api_price.csv")

    try:
        import docx
        p = os.path.join(TMP, "api_doc.docx")
        d = docx.Document()
        d.add_heading("保固條款", level=1)
        d.add_paragraph("電子產品自購買日起提供一年原廠保固，人為損壞需自費維修。")
        d.save(p)
        names.append("api_doc.docx")
    except Exception as e:
        print(f"  （略過 docx 樣本：{e}）")
    return names


with TestClient(main.app) as c:
    print("=" * 72)
    print(" RAG 知識庫端點自檢（TestClient 真打 /console/kb/*）")
    print("=" * 72)

    print("\n[1] 概覽 / 集合")
    r = c.get("/console/kb/overview")
    d = body(r)
    check(r.status_code == 200 and "collections" in d, "GET /overview",
          f"model={d.get('model', {}).get('model')} downloaded={d.get('model', {}).get('downloaded')}")
    check(bool(d.get("model", {}).get("max_tokens")), "模型資訊含 token 上限",
          f"max_tokens={d.get('model', {}).get('max_tokens')}")

    r = c.post("/console/kb/collections/create", json={"name": "端點驗證庫", "description": "t"})
    d = body(r)
    cid = (d.get("collection") or {}).get("id")
    check(r.status_code == 200 and cid, f"POST /collections/create → #{cid}")

    r = c.post("/console/kb/collections/create", json={"name": "  "})
    check(r.status_code == 400, "空名稱被擋下", body(r).get("error"))

    r = c.post("/console/kb/collections/update", json={"id": cid, "description": "更新後"})
    check(r.status_code == 200 and body(r)["collection"]["description"] == "更新後",
          "POST /collections/update")

    print("\n[2] 參數配置")
    # ⚠️ 檢索參數與**提示詞**都是全域的，這一段會寫到正式設定 → 先整批存起來，最後還原。
    #    沒有存就還原的話，會把人家調好的 min_score / 提示詞清成預設（踩過一次）。
    saved_global = kb_db.get_global_param_overrides()      # 全量，含提示詞
    saved_chunk = {k: kb_db.get_param_overrides(cid).get(k) for k in ("chunk_strategy",)}
    r = c.get(f"/console/kb/params?collection_id={cid}")
    d = body(r)
    keys = [i["key"] for i in d.get("items", [])]
    check(r.status_code == 200 and "top_k" in keys and "chunk_size" in keys,
          f"GET /params 回 {len(keys)} 個參數")
    # retrieval 6 → 7：多了 query_rewrite（多查詢檢索開關）
    # prompt 2 → 3：多了 image_caption_prompt（圖片 AI 描述按鈕用）
    check(len(d.get("retrieval", [])) == 7 and len(d.get("chunk", [])) == 6
          and len(d.get("prompts", [])) == 3,
          "參數分組正確（檢索 7 / 分塊 6 / 提示詞 3）",
          f"prompts={[i['key'] for i in d.get('prompts', [])]}")
    check("query_rewrite" in [i["key"] for i in d.get("retrieval", [])],
          "檢索參數含 query_rewrite（查詢改寫開關）",
          f"scope={next((i.get('scope') for i in d.get('retrieval', []) if i['key'] == 'query_rewrite'), None)}")
    check(all(i.get("scope") == "global" for i in d.get("retrieval", [])),
          "檢索參數全部標成全域（改了全庫一起生效）")

    # ★ 提示詞（C 區）：也是全域參數，可在頁面直接編輯
    pmap = {i["key"]: i for i in d.get("prompts", [])}
    check(all(i.get("scope") == "global" and i.get("type") == "text"
              for i in d.get("prompts", [])),
          "提示詞全部標成全域、型別 text（前端才會 render 多行框）")
    ans_p = pmap.get("rag_answer_prompt") or {}
    rw_p = pmap.get("rewrite_prompt") or {}
    check(all(v in (ans_p.get("value") or "") for v in ("{context}", "{sentinel}")),
          "知識庫問答提示詞帶 {context} 與 {sentinel}")
    check(all(v in (rw_p.get("value") or "") for v in ("{question}", "{n}")),
          "查詢改寫提示詞帶 {question} 與 {n}")
    check(bool(ans_p.get("required")) and bool(ans_p.get("vars")),
          "提示詞帶 required / vars（前端要標「必須保留」與可用變數）")

    r = c.post("/console/kb/params", json={"collection_id": cid,
                                          "values": {"top_k": 99, "min_score": "-5",
                                                     "chunk_strategy": "不存在"}})
    d = body(r)
    vals = {i["key"]: i["value"] for i in d["params"]["items"]}
    check(vals["top_k"] == 30 and vals["min_score"] == 0.0 and vals["chunk_strategy"] == "auto",
          "超出範圍被夾到邊界 / 非法選項回預設",
          f"top_k={vals['top_k']} min_score={vals['min_score']} strategy={vals['chunk_strategy']}")
    # ★ 檢索參數存到「全域」：換另一個知識庫讀，值應該一模一樣（分塊參數不會跟著跑）
    cols = body(c.get("/console/kb/collections"))["collections"]
    other = next((x["id"] for x in cols if x["id"] != cid), None)
    if other:
        o = {i["key"]: i["value"] for i in
             body(c.get(f"/console/kb/params?collection_id={other}"))["items"]}
        check(o["top_k"] == 30,
              f"檢索參數全庫共用（另一庫 #{other} 讀到同一組）",
              f"top_k={o['top_k']}（本庫剛設 30）")
    else:
        check(True, "只有一個知識庫，跨庫共用略過")

    # ★ 提示詞：缺必需變數要當場擋下（不然使用者會以為「存了卻沒生效」）
    r = c.post("/console/kb/params",
               json={"values": {"rag_answer_prompt": "這條沒有佔位符"}})
    check(r.status_code == 400 and "缺少" in str(body(r).get("error")),
          "提示詞缺必需變數被擋下（400）", str(body(r).get("error"))[:56])

    # ★ 合法提示詞存進 DB → 執行時（kb_search.prompt_of）立刻取到新值
    from app.rag import kb_search
    marker = "【自檢】{context} ／ {sentinel}"
    r = c.post("/console/kb/params", json={"values": {"rag_answer_prompt": marker}})
    got = kb_search.prompt_of("rag_answer_prompt")
    check(r.status_code == 200 and got == marker,
          "提示詞存進 DB 後執行時立刻取到新值（prompt_of）", repr(got[:16]))
    if other:
        o2 = {i["key"]: i["value"] for i in
              body(c.get(f"/console/kb/params?collection_id={other}"))["prompts"]}
        check(o2.get("rag_answer_prompt") == marker,
              f"提示詞全庫共用（另一庫 #{other} 讀到同一條）")

    # 還原：先清掉自檢寫入的**全部全域項**（檢索參數 + 提示詞），再把原值整批寫回。
    # ⚠️ 只清 scope='global' 的 key —— 全部清會把「未分組文件的分塊預設」也刪掉。
    clear_keys = [k for k in rag_config.PARAM_SPECS
                  if kb_db._param_scope(k) == "global"]
    c.post("/console/kb/params", json={"values": {k: None for k in clear_keys}})
    c.post("/console/kb/params", json={"collection_id": cid,
                                       "values": dict(saved_global, **saved_chunk)})
    kb_search.invalidate()
    after = kb_db.get_global_param_overrides()
    check(after == saved_global,
          "自檢沒有改動正式的全域設定（檢索參數 + 提示詞）",
          f"top_k={after.get('top_k')} min_score={after.get('min_score')} "
          f"提示詞覆寫={'有' if after.get('rag_answer_prompt') else '無'}")

    print("\n[3] 上傳 → 解析 → 分塊")
    samples = ensure_samples()
    files = []
    for fn in samples:
        with open(os.path.join(TMP, fn), "rb") as f:
            files.append(("files", (fn, f.read(), "application/octet-stream")))
    r = c.post("/console/kb/upload", files=files, data={"collection_id": str(cid)})
    d = body(r)
    ok_all = all(x.get("ok") for x in d.get("results", []))
    doc_ids = [x["doc_id"] for x in d.get("results", []) if x.get("ok")]
    check(r.status_code == 200 and ok_all and len(doc_ids) == len(samples),
          f"POST /upload {len(samples)} 檔 → doc {doc_ids}")

    # 等背景 parse+chunk 完成
    for _ in range(40):
        docs = body(c.get(f"/console/kb/documents?collection_id={cid}"))["documents"]
        if all(x["chunk_count"] > 0 or x["status"] == "failed" for x in docs):
            break
        time.sleep(0.25)
    docs = body(c.get(f"/console/kb/documents?collection_id={cid}"))["documents"]
    check(all(x["chunk_count"] > 0 for x in docs),
          "背景分塊完成", str([(x["filename"], x["status"], x["chunk_count"]) for x in docs]))

    # 不支援格式
    r = c.post("/console/kb/upload",
               files=[("files", ("bad.xyz", b"hello", "application/octet-stream"))],
               data={"collection_id": str(cid)})
    e = body(r)["results"][0]
    check(not e["ok"] and "格式" in e["error"], "不支援格式給人話原因", e["error"])

    print("\n[4] 文件詳情 / 預覽 / 重新分塊")
    did = doc_ids[0]
    r = c.get(f"/console/kb/document?id={did}&chunks=1")
    d = body(r)
    check(r.status_code == 200 and d["doc"]["id"] == did and len(d["chunks"]) > 0,
          f"GET /document?id={did} 回 {len(d['chunks'])} 塊")

    r = c.post("/console/kb/document/preview",
               json={"id": did, "params": {"chunk_size": 120, "chunk_min": 40, "chunk_max": 150}})
    d = body(r)
    check(r.status_code == 200 and d["count"] > 0, "POST /document/preview",
          f"{d['count']} 塊 / 平均 {d['avg_chars']} 字 / 策略 {d['strategy']}")

    r = c.post("/console/kb/document/reparse", json={"id": did})
    check(r.status_code == 200, "POST /document/reparse（背景）", str(body(r)))
    # 等背景重新分塊跑完，否則下一步 embed 會被「正在 reparse」擋下（守門正常運作）
    for _ in range(60):
        d = body(c.get(f"/console/kb/document?id={did}&chunks=0"))
        if not d.get("running") and (d.get("doc") or {}).get("chunk_count", 0) > 0:
            break
        time.sleep(0.25)
    check(not body(c.get(f"/console/kb/document?id={did}&chunks=0")).get("running"),
          "重新分塊完成（背景任務釋放）")

    print("\n[5] embedding → 入庫")
    r = c.post("/console/kb/document/embed", json={"id": did, "mode": "pending"})
    check(r.status_code == 200, "POST /document/embed（背景）", str(body(r)))
    done = False
    for _ in range(120):
        st = c.get(f"/console/kb/document?id={did}&chunks=0")
        d = body(st)
        s = d.get("stats") or {}
        if int(s.get("done") or 0) > 0 and not d.get("running"):
            done = True
            break
        time.sleep(0.5)
    check(done, "向量化完成（輪詢 prog）",
          f"done={((body(c.get(f'/console/kb/document?id={did}&chunks=0')).get('stats')) or {}).get('done')}")

    r = c.post("/console/kb/document/activate", json={"id": did})
    check(r.status_code == 200 and body(r)["doc"]["status"] == "ready", "POST /document/activate")

    # 其餘兩份也入庫（直接走同步函式，省時間）
    from app.rag import kb_ingest
    for x in doc_ids[1:]:
        kb_ingest.embed_document(x, "pending")
        kb_ingest.activate_document(x)
    ready = kb_db.query("SELECT count(*) c FROM kb_document WHERE collection_id=%s AND status='ready'",
                        (cid,))
    check(int(ready[0]["c"]) == len(doc_ids), f"{len(doc_ids)} 份文件都 ready（{ready[0]['c']}）")

    print("\n[5b] 檢索端點")
    r = c.get(f"/console/kb/index-stats?collection_id={cid}")
    d = body(r)
    check(r.status_code == 200 and d["stats"]["chunks"] > 0,
          f"GET /index-stats → {d['stats']['chunks']} 塊 / dim {d['stats']['dim']}")
    r = c.get(f"/console/kb/search?q=%E8%97%8D%E7%89%99%E8%80%B3%E6%A9%9F&collection_id={cid}")
    d = body(r)
    check(r.status_code == 200 and d.get("hits"),
          f"GET /search（藍牙耳機）→ top1={d.get('top1')} 入選 {len(d.get('hits') or [])} 塊",
          f"passed={d.get('passed')} threshold={d.get('threshold')}")
    r = c.post("/console/kb/search", json={"query": "退貨", "collection_id": cid, "top_k": 1})
    d = body(r)
    check(r.status_code == 200 and len(d.get("hits") or []) == 1, "POST /search top_k=1 只回 1 塊")
    check(bool(d.get("context")), "回傳可餵給模型的 context", f"{len(d.get('context') or '')} 字")
    check(bool(d.get("candidates")), "回傳候選明細（溯源用）",
          f"{len(d.get('candidates') or [])} 筆")

    print("\n[6] 知識塊微調端點")
    ch = kb_db.list_chunks(did)
    if len(ch) >= 2:
        a, b = ch[0], ch[1]
        r = c.post("/console/kb/chunk/update", json={"id": a["id"], "text": "端點手改文字"})
        check(r.status_code == 200 and body(r)["chunk"]["edited"], "POST /chunk/update")

        r = c.post("/console/kb/chunk/split", json={"id": a["id"], "at": 3})
        d = body(r)
        check(r.status_code == 200 and d.get("new_chunk_id"), "POST /chunk/split",
              f"new=#{d.get('new_chunk_id')} 共 {len(d.get('chunks') or [])} 塊")

        r = c.post("/console/kb/chunk/merge", json={"id": a["id"]})
        d = body(r)
        check(r.status_code == 200, "POST /chunk/merge", f"共 {len(d.get('chunks') or [])} 塊")

        r = c.post("/console/kb/chunk/split", json={"id": a["id"], "at": 999999})
        check(r.status_code == 400, "超界拆分位置被擋", body(r).get("error"))

        last = c.get(f"/console/kb/document?id={did}&chunks=0")
        chs = kb_db.list_chunks(did)
        r = c.post("/console/kb/chunk/delete", json={"id": chs[-1]["id"]})
        check(r.status_code == 200, "POST /chunk/delete", f"共 {len(body(r).get('chunks') or [])} 塊")
    else:
        check(False, "知識塊不足，無法測微調")

    print("\n[7] 停用 / 刪除 / 搬移")
    r = c.post("/console/kb/document/enabled", json={"id": did, "enabled": False})
    check(r.status_code == 200 and body(r)["doc"]["enabled"] is False, "POST /document/enabled")
    c.post("/console/kb/document/enabled", json={"id": did, "enabled": True})

    # 搬移到另一個知識庫（會改變它套用的檢索參數 → 索引快取要失效）
    r = c.post("/console/kb/collections/create", json={"name": "端點驗證庫-搬移目標"})
    cid2 = body(r)["collection"]["id"]
    r = c.post("/console/kb/document/move", json={"id": did, "collection_id": cid2})
    d = body(r)
    check(r.status_code == 200 and d["doc"]["collection_id"] == cid2,
          f"POST /document/move → 知識庫 #{cid2}")
    moved = body(c.get(f"/console/kb/documents?collection_id={cid2}"))["documents"]
    check(any(x["id"] == did for x in moved), "搬移後出現在新知識庫的文件列表")
    r = c.post("/console/kb/document/move", json={"id": did, "collection_id": 99999999})
    check(r.status_code == 400, "搬到不存在的知識庫被擋", str(body(r).get("error"))[:40])
    r = c.post("/console/kb/document/move", json={"id": did, "collection_id": cid})
    check(r.status_code == 200 and body(r)["doc"]["collection_id"] == cid, "搬回來")
    c.post("/console/kb/collections/delete", json={"id": cid2})

    r = c.post("/console/kb/collections/delete", json={"id": cid})
    check(r.status_code == 400, "非空知識庫刪除需二次確認", body(r).get("error", "")[:40])

    # 清理：刪文件（連檔案）+ 集合
    for x in doc_ids:
        c.post("/console/kb/document/delete", json={"id": x})
    left = kb_db.query("SELECT count(*) c FROM kb_document WHERE collection_id=%s", (cid,))
    check(int(left[0]["c"]) == 0, "刪文件後 DB 無殘留")
    r = c.post("/console/kb/collections/delete", json={"id": cid})
    check(r.status_code == 200, "清空後可刪知識庫")

    print("\n[7b] 未分組文件（UI 的「未分組」頁靠這個）")
    # 為什麼要專測：collection_id 在 DB 是 NULL，用 0 去查會一條都查不到。
    with open(os.path.join(TMP, "api_manual.md"), "rb") as f:
        blob = f.read()
    r = c.post("/console/kb/upload",
               files=[("files", ("api_ungrouped.md", blob, "text/markdown"))],
               data={"auto_start": "0"})       # 不帶 collection_id = 未分組
    ug_id = next((x["doc_id"] for x in body(r).get("results", []) if x.get("ok")), None)
    check(bool(ug_id), "上傳一份不帶知識庫的文件（未分組）", f"doc#{ug_id}")

    if ug_id:
        # ⚠ 這裡要自己開一顆目標庫：[7] 的 cid 在收尾時已經被刪掉了，
        #   拿它當搬移目標會 400，文件留在未分組，斷言就會假失敗。
        r = c.post("/console/kb/collections/create", json={"name": "端點驗證庫-未分組歸位"})
        ug_cid = body(r)["collection"]["id"]
        try:
            mine = body(c.get("/console/kb/documents?ungrouped=1"))["documents"]
            check(any(x["id"] == ug_id for x in mine), "GET /documents?ungrouped=1 找得到它")
            check(all(not x.get("collection_id") for x in mine), "ungrouped 清單裡都沒有 collection_id")
            in_col = body(c.get(f"/console/kb/documents?collection_id={ug_cid}"))["documents"]
            check(not any(x["id"] == ug_id for x in in_col), "它不會出現在知識庫的清單裡")
            check(not any(x["id"] == ug_id for x in body(c.get("/console/kb/documents"))["documents"]
                          if x.get("collection_id")), "「全部」清單裡它也是無歸屬的那一筆")
            ug_n = body(c.get("/console/kb/overview"))["totals"].get("ungrouped_docs")
            check(isinstance(ug_n, int) and ug_n >= 1, "overview 提供 ungrouped_docs 計數", f"={ug_n}")
            # 搬進知識庫後就不該再出現在未分組清單
            r = c.post("/console/kb/document/move", json={"id": ug_id, "collection_id": ug_cid})
            check(r.status_code == 200, "把未分組文件搬進知識庫", f"#{ug_cid}")
            left = body(c.get("/console/kb/documents?ungrouped=1"))["documents"]
            check(not any(x["id"] == ug_id for x in left), "搬進知識庫後離開未分組清單")
            now_in = body(c.get(f"/console/kb/documents?collection_id={ug_cid}"))["documents"]
            check(any(x["id"] == ug_id for x in now_in), "搬進去後出現在新知識庫的清單")
        finally:
            c.post("/console/kb/document/delete", json={"id": ug_id})
            c.post("/console/kb/collections/delete", json={"id": ug_cid})

    print("\n[8] 端點齊全性（路由存在）")
    for path in ("/console/kb/overview", "/console/kb/collections", "/console/kb/params",
                 "/console/kb/documents", "/console/kb/doc-types", "/console/kb/search",
                 "/console/kb/index-stats"):
        r = c.get(path)
        check(r.status_code == 200, f"GET {path} → {r.status_code}")

    print("\n[9] 自動回復端點的第三層（P3 新增欄位 / 端點）")
    r = c.get("/console/autoreply/settings")
    d = body(r)
    check(r.status_code == 200 and "rag_layer" in d, "GET /autoreply/settings 有 rag_layer",
          f"rag_layer={d.get('rag_layer')}")
    check(isinstance(d.get("kb"), dict) and "chunks" in (d.get("kb") or {}),
          "settings 帶知識庫規模摘要", str(d.get("kb")))
    check(d["kb"].get("error") is None, "知識庫摘要沒有異常", str(d["kb"].get("error")))

    # 逐層開關：設 rag → 讀回 → 還原（**一定要還原**，這是正式開關）
    before = c.get("/console/autoreply/settings").json()["rag_layer"]
    r = c.post("/console/autoreply/settings/layer", json={"layer": "rag", "enabled": not before})
    d = body(r)
    check(r.status_code == 200 and d.get("ok") and d.get("enabled") == (not before),
          f"POST /settings/layer layer=rag → enabled={d.get('enabled')}")
    after = body(c.get("/console/autoreply/settings")).get("rag_layer")
    check(after == (not before), f"settings 讀回已變更（rag_layer={after}）")
    r = c.post("/console/autoreply/settings/layer", json={"layer": "rag", "enabled": before})
    check(body(c.get("/console/autoreply/settings")).get("rag_layer") == before,
          f"已還原為 rag_layer={before}")
    r = c.post("/console/autoreply/settings/layer", json={"layer": "不存在", "enabled": True})
    check(r.status_code == 400, "不認得的層被擋", str(body(r).get("error"))[:40])

    # 紀錄列表：新增 has_matched_rows（UI 靠它決定哪一列可以點開）
    r = c.get("/console/autoreply/logs?limit=5")
    d = body(r)
    check(r.status_code == 200 and isinstance(d.get("logs"), list), "GET /autoreply/logs")
    if d.get("logs"):
        check("has_matched_rows" in d["logs"][0], "列表帶 has_matched_rows（輕量布林）",
              f"範例 id={d['logs'][0]['id']} has={d['logs'][0]['has_matched_rows']}")
        check("matched_rows" not in d["logs"][0], "列表仍刻意不帶 matched_rows（避免肥回應）")
        lid = d["logs"][0]["id"]
        r = c.get(f"/console/autoreply/logs/{lid}")
        dd = body(r)
        check(r.status_code == 200 and isinstance(dd.get("matched_rows"), list),
              f"GET /logs/{lid} 回 list（不假設型別）", f"{len(dd.get('matched_rows') or [])} 筆")
        check("tier_label" in dd, "詳情帶 tier_label", str(dd.get("tier_label")))
    else:
        print("  （目前沒有自動回復紀錄，略過列表欄位檢查）")
    r = c.get("/console/autoreply/logs/99999999")
    check(r.status_code == 404, "不存在的紀錄回 404")

    # FAQ 搜尋（後端參數化 ILIKE）
    r = c.get("/console/autoreply/faq")
    d = body(r)
    check(r.status_code == 200 and "total" in d, "GET /autoreply/faq 帶 total/filtered",
          f"total={d.get('total')} filtered={d.get('filtered')} q={d.get('q')!r}")
    r = c.get("/console/autoreply/faq?q=" + "%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E5%95%8F%E6%B3%95xyz")
    d = body(r)
    check(r.status_code == 200 and (d.get("faqs") or []) == [],
          "搜不到的關鍵字回空陣列（不是 500）", f"filtered={d.get('filtered')}")
    r = c.get("/console/autoreply/faq?q=%25")     # 單獨一個 % —— 參數化沒做好這裡會炸
    check(r.status_code == 200, "LIKE 特殊字元不會炸（參數化正確）", f"filtered={body(r).get('filtered')}")

    # 試跑端點：layers 逐層勾選，且**不寫任何紀錄**
    from app.autoreply import autoreply_db as _ardb
    log_before = _ardb.query("SELECT count(*) c FROM autoreply_log")[0]["c"]
    r = c.post("/console/autoreply/test/engine",
               json={"text": "你們有賣藍牙耳機嗎", "channel": "whatsapp", "scope": "dm",
                     "layers": ["faq"], "with_history": False})
    d = body(r)
    check(r.status_code == 200 and d.get("ok"), "POST /test/engine layers=['faq']",
          f"tier={d.get('tier')} label={d.get('label')}")
    check(d.get("layers") == ["faq"], "回傳實際跑的層（正規化後）", str(d.get("layers")))
    r = c.post("/console/autoreply/test/engine",
               json={"text": "x", "channel": "whatsapp", "scope": "dm",
                     "layers": ["沒這層"], "with_history": False})
    check(r.status_code == 400, "不認得的層被擋", str(body(r).get("error"))[:50])
    r = c.post("/console/autoreply/test/engine",
               json={"text": "x", "channel": "whatsapp", "scope": "dm",
                     "layers": "faq", "with_history": False})
    check(r.status_code == 400, "layers 不是陣列被擋", str(body(r).get("error"))[:50])
    log_after = _ardb.query("SELECT count(*) c FROM autoreply_log")[0]["c"]
    check(log_before == log_after, "試跑不寫任何紀錄", f"{log_before} → {log_after}")

    print("\n[10] 前端（console_ui.html 每個請求都重讀檔案，改完不必重啟）")
    r = c.get("/console")
    html = r.text if hasattr(r, "text") else r.content.decode("utf-8")
    check(r.status_code == 200 and "客服工作台" in html, f"GET /console → {r.status_code}（{len(html)} 字）")
    for tab in ("autoreply", "faq", "table", "kb", "hittest", "logs"):
        check('data-tab="' + tab + '"' in html, f"分頁籤 {tab} 存在")
    order = [html.find('data-tab="' + t + '"') for t in
             ("autoreply", "faq", "table", "kb", "hittest", "logs")]
    check(order == sorted(order), "分頁順序：自動回復 → FAQ → 業務表格 → 知識庫 → 命中測試 → 紀錄")
    for fn in ("kbTabHtml", "hittestTabHtml", "runHitTest", "renderMatchedRows",
               "kbOpenDoc", "kbUploadFiles", "kbSaveParams", "toggleLogDetail",
               "onFaqSearchInput", "kbMoveDoc", "openKbParams"):
        check(("function " + fn) in html, f"前端函式 {fn} 已定義")
    check("kbParamsOverlay" in html, "參數配置彈窗標記存在")

    # 知識庫是四層「跳轉」的頁面，不是一頁塞滿
    for fn in ("kbListPageHtml", "kbDocsPageHtml", "kbDocPageHtml", "kbNewPageHtml",
               "kbCrumbs", "kbGoList", "kbOpenCollection", "kbOpenUngrouped",
               "kbNewDoc", "kbBackToDocs", "kbForgetDoc"):
        check(("function " + fn) in html, f"知識庫分頁函式 {fn} 已定義")
    check("function kbWizardHtml" not in html, "舊的單頁向導 kbWizardHtml 已移除")
    check("kbDocFilter" not in html and "kbUploadTarget" not in html,
          "舊的「同一頁篩選/上傳目標」控制項已移除")
    check("kbUngrouped" in html and "ungrouped=1" in html, "前端會呼叫未分組清單")
    check('id="kbParamTarget"' in html, "參數彈窗自帶知識庫切換（不必先選好再開）")

print("\n" + "=" * 72)
print(f" 結果：{'全部通過 ✅' if not FAIL else str(len(FAIL)) + ' 項失敗'}")
for f in FAIL:
    print("   ✗", f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
