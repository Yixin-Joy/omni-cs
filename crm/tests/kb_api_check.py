"""RAG 知識庫端點自檢：直接呼叫 `kb_console` 的端點處理函式。

## 為什麼不是打 HTTP

單體版是 `TestClient(main.app)` 真打 `/console/kb/*`。**CRM 還沒把這支路由器
掛上去**（階段四決策：`kb_console.py` 仍是舊 API，接線要連 auth + 商家權限一起改；
見 `docs/MIGRATION_PLAN.md`）。所以這裡改成**直接呼叫端點函式** ——
它們的參數就是 plain `dict` / `int` / `str`，不需要 request context，
驗到的還是同一份邏輯本體。等真的接線後，把 `_call()` 換成 TestClient 即可。

## 不覆蓋的部分（都有別的地方驗）

`/upload`、`/document/reparse`、`/document/embed` 走 `_spawn()` 開
`asyncio.create_task` 的背景任務 —— 直接呼叫時那個 task 會在 `asyncio.run`
結束時被取消。這三條背後的入庫邏輯（解析／分塊／向量化）由
`kb_check.py` 與 `kb_asset_check.py` 直接呼叫 `kb_ingest` 驗過了。

跑法（cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/kb_api_check.py
"""
import asyncio
import json
import os
import sys

from _ck import check, report, sandbox_dir, ensure_business, purge_business  # noqa: E402

from app.rag import kb_console
from app.rag import kb_db
from app.rag import kb_ingest
from app.rag import kb_search
from app.rag import rag_config

TMP = str(sandbox_dir())
BIZ = ensure_business("知識庫端點自檢", "kb_api_check 專用")


def body(res):
    raw = getattr(res, "body", None)
    if raw is None:
        raw = getattr(res, "content", b"")
    try:
        return json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        return {}


def code(res) -> int:
    return int(getattr(res, "status_code", 200) or 200)


def _call(fn, *a, **kw):
    """呼叫 async 端點函式（`_ok/_err` 回 Starlette Response）。"""
    return body(asyncio.run(fn(*a, **kw)))


def _code(fn, *a, **kw) -> int:
    return code(asyncio.run(fn(*a, **kw)))


def make_samples() -> list[str]:
    """自建最小測試檔（不依賴 kb_check.py，兩支腳本各自可獨立執行）。"""
    os.makedirs(TMP, exist_ok=True)
    names = []

    with open(os.path.join(TMP, "api_manual.md"), "w", encoding="utf-8") as f:
        f.write("# 商品說明\n\n## 藍牙耳機\nA 款售價 1280 元，續航 30 小時。\n"
                "B 款售價 890 元，支援主動降噪。\n\n## 退貨\n到貨 7 天內包裝完整可無理由退貨。\n")
    names.append("api_manual.md")

    with open(os.path.join(TMP, "api_price.csv"), "w", encoding="utf-8") as f:
        f.write("產品,價格,庫存\n")
        for i in range(1, 21):
            f.write(f"耳機-{i},{800 + i * 13},{50 + i}\n")
    names.append("api_price.csv")
    return names


def main() -> int:
    print("=" * 74)
    print(" RAG 知識庫端點自檢（直接呼叫 kb_console 處理函式）")
    print("=" * 74)

    # ---------------------------------------------------------------- [0]
    print("\n[0] 路由齊全性（掛載前的靜態清單）")
    # ⚠️ `route.path` **已含 router 的 prefix**（`/console/kb/...`），不是子路徑。
    #    且 /params、/search 各有 GET + POST → 路由物件數 > 唯一路徑數。
    PREFIX = "/console/kb"
    all_paths = {getattr(r, "path", "") for r in kb_console.router.routes}
    sub = {p[len(PREFIX):] for p in all_paths if p.startswith(PREFIX)}
    check(all(p.startswith(PREFIX) for p in all_paths),
          f"所有路由都掛在同一個 prefix 底下（{PREFIX}）",
          f"例外：{sorted(p for p in all_paths if not p.startswith(PREFIX))}")
    expected = {
        "/overview", "/collections", "/collections/create", "/collections/update",
        "/collections/delete", "/params", "/upload", "/documents", "/document",
        "/document/preview", "/document/reparse", "/document/embed", "/document/activate",
        "/document/enabled", "/document/delete", "/document/move",
        "/chunk/update", "/chunk/split", "/chunk/merge", "/chunk/delete",
        "/search", "/index-stats", "/doc-types",
        "/assets", "/asset/upload", "/asset/caption", "/asset/caption/ai", "/asset/embed",
        "/asset/update", "/asset/delete", "/asset/file/{asset_id}", "/asset/rebind",
    }
    missing = sorted(expected - sub)
    extra = sorted(sub - expected)
    check(not missing and not extra,
          f"{len(expected)} 條路由都在（prefix {PREFIX}）",
          f"缺：{missing} 多：{extra}" if (missing or extra)
          else f"{len(kb_console.router.routes)} 個路由物件 / {len(sub)} 條唯一路徑")

    # ---------------------------------------------------------------- [1]
    print("\n[1] 概覽 / 知識庫 CRUD")
    d = _call(kb_console.overview)
    check("collections" in d, "GET /overview 回集合清單",
          f"model={d.get('model', {}).get('model')}")
    check(bool(d.get("model", {}).get("max_tokens")), "模型資訊含 token 上限",
          f"max_tokens={d.get('model', {}).get('max_tokens')}")
    check(isinstance((d.get("totals") or {}).get("ungrouped_docs"), int),
          "overview 提供 ungrouped_docs 計數",
          f"={((d.get('totals') or {}).get('ungrouped_docs'))}")

    d = _call(kb_console.collection_create, {"name": "端點自檢庫", "description": "t",
                                             "business_id": BIZ})
    cid = (d.get("collection") or {}).get("id")
    check(bool(cid), f"POST /collections/create → #{cid}")

    # ★ CRM 側建庫一定要 business_id（NOT NULL），沒帶要當場擋下
    d = _call(kb_console.collection_create, {"name": "沒有商家的庫"})
    check(code(asyncio.run(kb_console.collection_create({"name": "沒有商家的庫"}))) == 400,
          "建庫沒帶 business_id → 400（CRM 的庫必須歸屬一個商家）")
    check(_code(kb_console.collection_create, {"name": "  ", "business_id": BIZ}) == 400,
          "空名稱被擋下")

    d = _call(kb_console.collection_update, {"id": cid, "description": "更新後"})
    check(d["collection"]["description"] == "更新後", "POST /collections/update")

    # ---------------------------------------------------------------- [2]
    print("\n[2] 參數配置")
    # ⚠️ 檢索參數與**提示詞**都是全域的，這一段會寫到正式設定 → 先整批存起來，最後還原。
    saved_global = kb_db.get_global_param_overrides()
    saved_chunk = {k: kb_db.get_param_overrides(cid).get(k) for k in ("chunk_strategy",)}
    try:
        d = _call(kb_console.get_params, str(cid))
        keys = [i["key"] for i in d.get("items", [])]
        check("top_k" in keys and "chunk_size" in keys, f"GET /params 回 {len(keys)} 個參數")
        check(len(d.get("retrieval", [])) == 8 and len(d.get("chunk", [])) == 6
              and len(d.get("prompts", [])) == 3,
              "參數分組正確（檢索 8 / 分塊 6 / 提示詞 3）",
              f"prompts={[i['key'] for i in d.get('prompts', [])]}")
        _gate = next((i for i in d.get("retrieval", []) if i["key"] == "rag_answer_gate"), None)
        check(bool(_gate) and _gate.get("type") == "choice"
              and set(_gate.get("choices") or []) == {"image", "on", "off"},
              "檢索參數含 rag_answer_gate（choice image/on/off）",
              f"{_gate and _gate.get('value')} / {_gate and _gate.get('choices')}")
        check(all(i.get("scope") == "global" for i in d.get("retrieval", [])),
              "檢索參數全部標成全域（改了全庫一起生效）")
        pmap = {i["key"]: i for i in d.get("prompts", [])}
        check(all(i.get("scope") == "global" and i.get("type") == "text"
                  for i in d.get("prompts", [])),
              "提示詞全部標成全域、型別 text（前端才會 render 多行框）")
        ans_p = pmap.get("rag_answer_prompt") or {}
        check(all(v in (ans_p.get("value") or "") for v in ("{context}", "{sentinel}")),
              "知識庫問答提示詞帶 {context} 與 {sentinel}")
        check(bool(ans_p.get("required")) and bool(ans_p.get("vars")),
              "提示詞帶 required / vars（前端要標「必須保留」與可用變數）")

        d = _call(kb_console.set_params,
                  {"collection_id": cid,
                   "values": {"top_k": 99, "min_score": "-5", "chunk_strategy": "不存在"}})
        vals = {i["key"]: i["value"] for i in d["params"]["items"]}
        check(vals["top_k"] == 30 and vals["min_score"] == 0.0
              and vals["chunk_strategy"] == "auto",
              "超出範圍被夾到邊界 / 非法選項回預設",
              f"top_k={vals['top_k']} min_score={vals['min_score']} "
              f"strategy={vals['chunk_strategy']}")

        other = next((x["id"] for x in _call(kb_console.collections).get("collections") or []
                      if x["id"] != cid), None)
        if other:
            o = {i["key"]: i["value"]
                 for i in _call(kb_console.get_params, str(other))["items"]}
            check(o["top_k"] == 30, f"檢索參數全庫共用（另一庫 #{other} 讀到同一組）",
                  f"top_k={o['top_k']}（本庫剛設 30）")
        else:
            check(True, "只有一個知識庫，跨庫共用略過")

        # 提示詞缺必需變數要當場擋下（不然使用者會以為「存了卻沒生效」）
        check(_code(kb_console.set_params,
                    {"values": {"rag_answer_prompt": "這條沒有佔位符"}}) == 400,
              "提示詞缺必需變數被擋下（400）")

        marker = "【自檢】{context} ／ {sentinel}"
        _call(kb_console.set_params, {"values": {"rag_answer_prompt": marker}})
        got = kb_search.prompt_of("rag_answer_prompt")
        check(got == marker, "提示詞存進 DB 後執行時立刻取到新值（prompt_of）",
              repr(got[:16]))
    finally:
        # 還原：先清掉自檢寫入的**全部全域項**（檢索參數 + 提示詞），再把原值整批寫回。
        clear_keys = [k for k in rag_config.PARAM_SPECS
                      if kb_db._param_scope(k) == "global"]
        _call(kb_console.set_params, {"values": {k: None for k in clear_keys}})
        _call(kb_console.set_params,
              {"collection_id": cid, "values": dict(saved_global, **saved_chunk)})
        kb_search.invalidate()
    after = kb_db.get_global_param_overrides()
    check(after == saved_global, "自檢沒有改動正式的全域設定（檢索參數 + 提示詞）",
          f"top_k={after.get('top_k')} min_score={after.get('min_score')} "
          f"提示詞覆寫={'有' if after.get('rag_answer_prompt') else '無'}")

    # ---------------------------------------------------------------- [3]
    print("\n[3] 文件清單 / 詳情 / 預覽（入庫本身用 kb_ingest 直接做）")
    samples = make_samples()
    doc_ids = []
    for fn in samples:
        with open(os.path.join(TMP, fn), "rb") as f:
            up = kb_ingest.save_upload(fn, f.read(), "", cid)
        did = up["doc"]["id"]
        kb_ingest.parse_and_chunk(did)
        doc_ids.append(did)
    check(len(doc_ids) == len(samples), f"造了 {len(doc_ids)} 份文件 {doc_ids}")

    docs = _call(kb_console.documents, str(cid)).get("documents") or []
    check(all(x["chunk_count"] > 0 for x in docs), "GET /documents 分塊完成",
          str([(x["filename"], x["status"], x["chunk_count"]) for x in docs]))

    did = doc_ids[0]
    d = _call(kb_console.document, did, 1)
    check(d["doc"]["id"] == did and len(d["chunks"]) > 0,
          f"GET /document?id={did} 回 {len(d['chunks'])} 塊")
    d = _call(kb_console.document, did, 0)
    check(not d.get("chunks"), "chunks=0 時不撈塊（列表頁用，省流量）")
    check("running" in d and "stats" in d and "jobs" in d,
          "文件詳情帶 running / stats / jobs（前端輪詢要看）")

    d = _call(kb_console.document_preview,
              {"id": did, "params": {"chunk_size": 120, "chunk_min": 40, "chunk_max": 150}})
    check(d["count"] > 0, "POST /document/preview",
          f"{d['count']} 塊 / 平均 {d['avg_chars']} 字 / 策略 {d['strategy']}")

    # ---------------------------------------------------------------- [4]
    print("\n[4] 檢索端點")
    for x in doc_ids:
        kb_ingest.embed_document(x, "pending")
        kb_ingest.activate_document(x)
    kb_search.invalidate()
    d = _call(kb_console.index_stats, str(cid))
    check(d["stats"]["chunks"] > 0, f"GET /index-stats → {d['stats']['chunks']} 塊 / "
                                   f"dim {d['stats']['dim']}")
    d = _call(kb_console.search_get, q="藍牙耳機", collection_id=str(cid))
    check(d.get("hits"), f"GET /search（藍牙耳機）→ top1={d.get('top1')}",
          f"passed={d.get('passed')} threshold={d.get('threshold')}")
    d = _call(kb_console.search_post, {"query": "退貨", "collection_id": cid, "top_k": 1})
    check(len(d.get("hits") or []) == 1, "POST /search top_k=1 只回 1 塊")
    check(bool(d.get("context")), "回傳可餵給模型的 context", f"{len(d.get('context') or '')} 字")
    check(bool(d.get("candidates")), "回傳候選明細（溯源用）",
          f"{len(d.get('candidates') or [])} 筆")
    # ★ CRM 側：帶 business_id 的檢索要限定在該商家的基準啟用集
    d = _call(kb_console.search_post, {"query": "藍牙耳機", "business_id": BIZ})
    check(bool(d.get("hits")) and all(int(c["collection_id"]) == int(cid)
                                     for c in (d.get("per_collection") or [])),
          "POST /search 帶 business_id → 只回該商家的知識庫",
          str([c.get("collection_id") for c in (d.get("per_collection") or [])]))
    d = _call(kb_console.doc_types)
    check(bool(d.get("types") or d.get("doc_types") or d), "GET /doc-types 有回應", str(d)[:60])

    # ---------------------------------------------------------------- [5]
    print("\n[5] 知識塊微調端點")
    ch = kb_db.list_chunks(did)
    if len(ch) >= 2:
        a = ch[0]
        d = _call(kb_console.chunk_update, {"id": a["id"], "text": "端點手改文字"})
        check(d["chunk"]["edited"], "POST /chunk/update")
        d = _call(kb_console.chunk_split, {"id": a["id"], "at": 3})
        check(bool(d.get("new_chunk_id")), "POST /chunk/split",
              f"new=#{d.get('new_chunk_id')} 共 {len(d.get('chunks') or [])} 塊")
        d = _call(kb_console.chunk_merge, {"id": a["id"]})
        check(d.get("chunks") is not None, "POST /chunk/merge",
              f"共 {len(d.get('chunks') or [])} 塊")
        check(_code(kb_console.chunk_split, {"id": a["id"], "at": 999999}) == 400,
              "超界拆分位置被擋")
        chs = kb_db.list_chunks(did)
        d = _call(kb_console.chunk_delete, {"id": chs[-1]["id"]})
        check(d.get("chunks") is not None, "POST /chunk/delete",
              f"共 {len(d.get('chunks') or [])} 塊")
    else:
        check(False, "知識塊不足，無法測微調")

    # ---------------------------------------------------------------- [6]
    print("\n[6] 停用 / 搬移 / 刪除")
    d = _call(kb_console.document_enabled, {"id": did, "enabled": False})
    check(d["doc"]["enabled"] is False, "POST /document/enabled（停用）")
    _call(kb_console.document_enabled, {"id": did, "enabled": True})

    cid2 = _call(kb_console.collection_create,
                 {"name": "端點自檢庫-搬移目標", "business_id": BIZ})["collection"]["id"]
    d = _call(kb_console.document_move, {"id": did, "collection_id": cid2})
    check(d["doc"]["collection_id"] == cid2, f"POST /document/move → 知識庫 #{cid2}")
    moved = _call(kb_console.documents, str(cid2)).get("documents") or []
    check(any(x["id"] == did for x in moved), "搬移後出現在新知識庫的文件列表")
    check(_code(kb_console.document_move, {"id": did, "collection_id": 99999999}) == 400,
          "搬到不存在的知識庫被擋")
    d = _call(kb_console.document_move, {"id": did, "collection_id": cid})
    check(d["doc"]["collection_id"] == cid, "搬回來")
    _call(kb_console.collection_delete, {"id": cid2})

    d = _call(kb_console.collection_delete, {"id": cid})
    check(code(asyncio.run(kb_console.collection_delete({"id": cid}))) == 400,
          "非空知識庫刪除需二次確認")

    # ---------------------------------------------------------------- [7]
    print("\n[7] 未分組文件（未歸屬任何知識庫）")
    with open(os.path.join(TMP, "api_manual.md"), "rb") as f:
        blob = f.read()
    # ⚠️ collection_id 在 DB 是 NULL，用 0 去查會一條都查不到 —— 走 ungrouped=1。
    saved = kb_ingest.save_upload("api_ungrouped.md", blob, "text/markdown", None)
    ug_id = int(saved["doc"]["id"])
    check(saved["doc"].get("collection_id") is None,
          "造一份未分組文件（collection_id IS NULL）", f"doc#{ug_id}")
    mine = _call(kb_console.documents, "", "", "1").get("documents") or []
    check(any(x["id"] == ug_id for x in mine), "GET /documents?ungrouped=1 找得到它")
    check(all(not x.get("collection_id") for x in mine),
          "ungrouped 清單裡都沒有 collection_id")
    all_docs = _call(kb_console.documents).get("documents") or []
    check(any(x["id"] == ug_id and not x.get("collection_id") for x in all_docs),
          "「全部」清單裡它也是無歸屬的那一筆")
    ug_cid = _call(kb_console.collection_create,
                   {"name": "端點自檢庫-未分組歸位", "business_id": BIZ})["collection"]["id"]
    check(_code(kb_console.document_move, {"id": ug_id, "collection_id": ug_cid}) == 200,
          "把未分組文件搬進知識庫", f"#{ug_cid}")
    left = _call(kb_console.documents, "", "", "1").get("documents") or []
    check(not any(x["id"] == ug_id for x in left), "搬進知識庫後離開未分組清單")
    now_in = _call(kb_console.documents, str(ug_cid)).get("documents") or []
    check(any(x["id"] == ug_id for x in now_in), "搬進去後出現在新知識庫的清單")

    # ---------------------------------------------------------------- [8]
    print("\n[8] 掛載現況")
    import main as crm_main      # noqa: E402
    mounted = [getattr(rt, "path", "") for rt in crm_main.app.routes
               if str(getattr(rt, "path", "")).startswith("/console/kb")]
    if not mounted:
        print("  [未接線] main.app 目前沒有 /console/kb/* —— 上面全是直接呼叫處理函式，")
        print("           驗到的是邏輯本體。真的接線後請把 _call() 換成 TestClient 真打。")
    else:
        check(len(mounted) >= len(expected), "main.app 已掛載 /console/kb/*",
              f"{len(mounted)} 條")

    # ---------------------------------------------------------------- 收尾
    print("\n[9] 清理…")
    for x in doc_ids:
        _call(kb_console.document_delete, {"id": x})
    left_n = kb_db.query("SELECT count(*) c FROM kb_document WHERE collection_id=%s",
                         (cid,))[0]["c"]
    check(int(left_n) == 0, "刪文件後 DB 無殘留")
    check(_code(kb_console.collection_delete, {"id": cid}) == 200, "清空後可刪知識庫")
    purge_business(BIZ)
    check(not kb_db.list_collections(business_id=BIZ), "自檢商家已被清空")

    return report("RAG 知識庫端點自檢")


if __name__ == "__main__":
    raise SystemExit(main())
