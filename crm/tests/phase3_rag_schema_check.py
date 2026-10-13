"""階段三自檢 —— RAG 表結構完整性（每一條寫入路徑都真的跑一次）。

為什麼要有這支：CRM 的 `schema.py` 是照設計案抄的，而 `app/rag/*` 是從舊庫整包搬過來的。
**只要有一個欄位漏抄，那條路徑一跑到就 `UndefinedColumn`** —— 而且通常要等到真的上傳文件
或用戶改知識庫名稱時才會炸（已經抓過兩例：`kb_job.started_at/finished_at`、
`kb_collection.updated_at`）。

所以這支不做「單元測試」，而是**把所有會寫 DB 的函式都實際呼一次**，用真 DB 當閘門。
不呼叫 embedding 模型（向量用假資料），所以很快、離線可跑。

跑法（cwd 必須是 crm/）：
    python tests/phase3_rag_schema_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2                      # noqa: E402
import psycopg2.extras               # noqa: E402

from app.core import config, db      # noqa: E402
from app.rag import kb_db            # noqa: E402

MARK = "P3R"
_ok = [0, 0]
_errors: list[str] = []


def check(cond, msg: str) -> None:
    _ok[0 if cond else 1] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)


def step(name: str, fn):
    """跑一個寫入路徑；拋錯就記下來（這是這支腳本的主要目的）。"""
    try:
        out = fn()
        _ok[0] += 1
        print(f"  PASS  {name}")
        return out
    except Exception as e:  # noqa: BLE001
        _ok[1] += 1
        _errors.append(f"{name}: {type(e).__name__}: {e}")
        print(f"  FAIL  {name} → {type(e).__name__}: {e}")
        return None


def sql(query: str, params=None, fetch: bool = True):
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params or ())
            rows = cur.fetchall() if (fetch and cur.description is not None) else None
        conn.commit()
        return rows
    finally:
        conn.close()


def cleanup() -> None:
    doc_ids = "(SELECT id FROM kb_document WHERE filename LIKE %s)"
    cids = "(SELECT id FROM kb_collection WHERE name LIKE %s)"
    sql(f"DELETE FROM kb_asset_chunk WHERE asset_id IN"
        f" (SELECT id FROM kb_asset WHERE collection_id IN {cids}"
        f"     OR doc_id IN {doc_ids})", (MARK + "%", MARK + "%"), fetch=False)
    sql(f"DELETE FROM kb_asset WHERE collection_id IN {cids} OR doc_id IN {doc_ids}",
        (MARK + "%", MARK + "%"), fetch=False)
    sql(f"DELETE FROM kb_chunk WHERE doc_id IN {doc_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM kb_job WHERE doc_id IN {doc_ids}", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM kb_param WHERE collection_id IN {cids}", (MARK + "%",), fetch=False)
    sql("DELETE FROM business_kb_binding WHERE business_id IN"
        " (SELECT id FROM crm_business WHERE business_name LIKE %s)", (MARK + "%",), fetch=False)
    sql(f"DELETE FROM kb_document WHERE filename LIKE %s", (MARK + "%",), fetch=False)
    sql("DELETE FROM kb_collection WHERE name LIKE %s", (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_business WHERE business_name LIKE %s", (MARK + "%",), fetch=False)


def main() -> int:
    print("=" * 68)
    print("階段三自檢：RAG 表結構完整性（每條寫入路徑實跑）")
    print("=" * 68)

    db.init_schema_sync()
    cleanup()
    biz = sql("INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id",
              (MARK + "商家",))[0]["id"]
    print(f"\n[0] 場地：business={biz}")

    # ---------------------------------------------------------------- [1] 知識庫
    print("\n[1] kb_collection（含 update → 漏 updated_at 就會炸）")
    c1 = step("create_collection（文檔庫）", lambda: kb_db.create_collection(
        MARK + "文檔庫", "測試", business_id=biz, kind="doc"))
    cid = c1["id"] if c1 else 0
    step("update_collection（改名 / 排序 / 啟用）", lambda: kb_db.update_collection(
        cid, {"name": MARK + "文檔庫B", "description": "d", "sort_order": 3, "enabled": True}))
    got = kb_db.get_collection(cid) or {}
    check(got.get("name") == MARK + "文檔庫B" and got.get("sort_order") == 3,
          f"改名與排序已落庫（實得 {got.get('name')}/{got.get('sort_order')}）")
    check(sql("SELECT business_id FROM kb_collection WHERE id=%s", (cid,))[0]["business_id"] == biz,
          "knowledge base 綁在正確商家上")
    img = step("ensure_image_collection（建圖片庫）", lambda: kb_db.ensure_image_collection(cid))
    step("set_collection_enabled", lambda: kb_db.set_collection_enabled(cid, True))
    lists = step("list_collections(business_id=)", lambda: kb_db.list_collections(business_id=biz))
    check(lists is not None and any(c["id"] == cid for c in lists),
          "list_collections 依商家過濾得到它")

    # ---------------------------------------------------------------- [2] 文件 / 塊
    print("\n[2] kb_document / kb_chunk（含向量）")
    doc = step("create_document", lambda: kb_db.create_document(
        filename=MARK + "_a.md", title="測試文件", mime="text/markdown", size_bytes=10,
        source_path="/tmp/x", doc_type="md", collection_id=cid))
    did = doc["id"] if doc else 0
    step("update_document（status/process）", lambda: kb_db.update_document(
        did, {"status": "ready", "chunk_count": 2, "char_count": 20, "progress": 100}))
    step("replace_chunks", lambda: kb_db.replace_chunks(did, [
        {"text": "第一塊內容 ABC", "prefix": "H1", "page_no": 1},
        {"text": "第二塊內容 XYZ", "prefix": "H2", "page_no": 1}]))
    step("insert_chunk", lambda: kb_db.insert_chunk(did, 2, "第三塊內容", prefix="H3"))
    step("update_chunk / renumber_chunks / shift_ordinals", lambda: (
        kb_db.update_chunk(kb_db.list_chunks(did)[0]["id"], text="第一塊改過"),
        kb_db.renumber_chunks(did), kb_db.shift_ordinals(did, 0)))

    pend = step("pending_chunks", lambda: kb_db.pending_chunks(did))
    check(pend is not None and len(pend) == 3, f"有 3 塊待向量化（實得 {len(pend or [])}）")
    step("save_embeddings（假向量 dim=3）", lambda: kb_db.save_embeddings(
        [(r["id"], [0.1, 0.2, 0.3]) for r in (pend or [])], "fake-model", 3))
    ready = step("load_ready_chunks(collection_id)", lambda: kb_db.load_ready_chunks(cid))
    check(ready is not None and len(ready) == 3,
          f"3 塊可檢索（實得 {len(ready or [])}）")
    step("chunk_stats", lambda: kb_db.chunk_stats(did))
    step("find_document_by_sha / list_documents", lambda: (
        kb_db.find_document_by_sha("deadbeef", cid), kb_db.list_documents(collection_id=cid)))
    step("mark_embedding_failed", lambda: kb_db.mark_embedding_failed([]))

    # ---------------------------------------------------------------- [3] 圖片資產
    print("\n[3] kb_asset / kb_asset_chunk")
    icid = (img or {}).get("id") or cid
    a = step("create_asset", lambda: kb_db.create_asset(
        rel_path=MARK + "/p1.png", collection_id=icid, doc_id=did, page_no=1,
        filename="p1.png", mime="image/png", width=10, height=10))
    aid = a["id"] if a else 0
    step("set_asset_caption(source=manual)", lambda: kb_db.set_asset_caption(
        aid, "這是一張測試圖", "manual"))
    step("update_asset", lambda: kb_db.update_asset(aid, enabled=True))
    chunk_id = (ready or [{}])[0].get("id") if ready else 0
    step("bind_asset_chunks", lambda: kb_db.bind_asset_chunks(aid, [chunk_id] if chunk_id else []))
    step("assets_for_chunks", lambda: kb_db.assets_for_chunks([chunk_id] if chunk_id else []))
    step("asset_stats", lambda: kb_db.asset_stats(collection_id=icid))
    # ⚠️ 只撈「本測試 doc」的待向量資產，免得把假向量寫到真實資料上
    step("pending_asset_embeddings + save_asset_embeddings", lambda: kb_db.save_asset_embeddings(
        [(r["id"], [1.0, 0.0, 0.0]) for r in kb_db.pending_asset_embeddings(limit=50, doc_id=did)],
        "fake-model", 3))
    step("load_ready_assets(collection_id)", lambda: kb_db.load_ready_assets(icid))
    step("assets_scope（clear/rebind）", lambda: (
        kb_db.clear_asset_bindings(aid), kb_db.clear_doc_bindings(did)))
    step("delete_assets_for_doc / list_assets", lambda: (
        kb_db.list_assets(doc_id=did), kb_db.list_assets(collection_id=icid)))

    # ---------------------------------------------------------------- [4] 工作佇列
    print("\n[4] kb_job（★ create_job/finish_job 曾因缺 started_at/finished_at 直接炸）")
    job = step("create_job", lambda: kb_db.create_job(did, "parse"))
    step("finish_job", lambda: kb_db.finish_job(job or 0, True))
    jobs = step("list_jobs", lambda: kb_db.list_jobs(did))
    check(jobs is not None and jobs and jobs[0]["status"] == "done",
          f"任務狀態為 done（實得 {(jobs or [{}])[0].get('status')}）")
    check(jobs and jobs[0].get("started_at") is not None,
          "started_at 有值（欄位真的存在）")
    step("list_running_jobs", lambda: kb_db.list_running_jobs())
    step("reset_stuck_jobs", lambda: kb_db.reset_stuck_jobs())

    # ---------------------------------------------------------------- [5] 參數
    print("\n[5] kb_param（★ 只寫「按知識庫」的分塊參數，不動全域檢索參數）")
    step("set_params(collection_id=…)", lambda: kb_db.set_params(cid, {"chunk_size": 480}))
    loaded = step("load_params(cid)", lambda: kb_db.load_params(cid))
    check(isinstance(loaded, dict) and str(loaded.get("chunk_size")) == "480",
          f"分塊參數已落庫（實得 {loaded}）")
    step("effective_param / get_param_overrides", lambda: (
        kb_db.effective_param(cid, "chunk_size"), kb_db.get_param_overrides(cid)))
    # ★ 這條是閘門：`kb_param.value` 在 CRM 是 jsonb（舊庫是 TEXT），寫入端若還用
    #   `str(raw)`，`'auto'` 這種非法 JSON 字面量會當場 InvalidTextRepresentation。
    step("set_params(字串值)", lambda: kb_db.set_params(cid, {"chunk_strategy": "auto"}))
    loaded_s = step("load_params(cid)（字串值）", lambda: kb_db.load_params(cid))
    check(str((loaded_s or {}).get("chunk_strategy")) == "auto",
          f"字串參數落庫且讀得回（實得 {(loaded_s or {}).get('chunk_strategy')!r}）")
    check(kb_db.get_global_param_overrides() is not None, "全域參數可讀（未被本自檢改動）")

    # ---------------------------------------------------------------- [6] 刪除路徑
    print("\n[6] 刪除路徑")
    step("delete_asset", lambda: kb_db.delete_asset(aid))
    step("delete_chunk", lambda: kb_db.delete_chunk((ready or [{}])[0].get("id") or 0))
    step("delete_document", lambda: kb_db.delete_document(did))
    step("delete_collection（文檔庫 → 連帶圖片庫 + 清 binding）",
         lambda: kb_db.delete_collection(cid))

    # ---------------------------------------------------------------- 收尾
    print("\n[7] 收尾")
    cleanup()
    left = sql("SELECT count(*) AS n FROM kb_collection WHERE name LIKE %s", (MARK + "%",))[0]["n"]
    check(left == 0, "殘骸已清空")

    print("\n" + "=" * 68)
    print(f"結果：PASS {_ok[0]} / FAIL {_ok[1]}")
    if _errors:
        print("失敗明細：")
        for e in _errors:
            print("  -", e)
    print("=" * 68)
    return 1 if _ok[1] else 0


if __name__ == "__main__":
    raise SystemExit(main())
