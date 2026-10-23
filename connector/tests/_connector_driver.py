"""階段二自檢的 Connector 側執行器 —— 單次動作，供 `phase2_check.py` 以子進程呼叫。

為什麼要子進程：connector 與 crm 兩個服務的 package 都叫 `app`（刻意的，見 MIGRATION_PLAN §2），
同一個 Python 進程不可能同時 import 兩邊。所以自檢用「起兩個服務 + 子進程跑單次動作」的方式。

用法（cwd 必須是 connector/）：
    python tests/_connector_driver.py tick [limit]
    python tests/_connector_driver.py sync-map
    python tests/_connector_driver.py enqueue-callback <task_id> <status>
    python tests/_connector_driver.py flush-callback
    python tests/_connector_driver.py purge-cache
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # connector/


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "tick"

    if mode == "tick":
        from app.worker import outbound
        limit = int(args[1]) if len(args) > 1 else 0
        out = asyncio.run(outbound.tick(limit))
    elif mode == "sync-map":
        from app.worker import account_sync
        out = asyncio.run(account_sync.sync_account_map())
    elif mode == "enqueue-callback":
        from app.worker import callback
        callback.enqueue(int(args[1]), args[2],
                         (args[3] if len(args) > 3 else None),
                         (args[4].lower() == "true") if len(args) > 4 else None)
        out = {"ok": True}
    elif mode == "flush-callback":
        from app.worker import callback
        out = asyncio.run(callback.flush())
    elif mode == "purge-cache":
        from app import media
        out = {"purged": media.purge_kb_cache()}
    elif mode == "send-assets-mock":
        # 圖文鏈路自檢用：從 stdin 讀 {task, content}，拆子項 → mock 送出 → 落地，
        # 回報每一項的結果與**實際寫進 DB 的 attachments**（讓 CRM 側驗 url）。
        os.environ["OUTBOUND_SEND_MODE"] = "mock"
        import importlib

        from app.core import config as _cfg
        import importlib
        importlib.reload(_cfg)
        from app import media
        from app.worker import outbound
        importlib.reload(outbound)

        payload = json.loads(sys.stdin.read() or "{}")
        task = payload.get("task") or {}
        content = payload.get("content") or {}

        # `seed_files`：把來源圖直接存進 Connector 的 kb_cache（回傳的 media_key 為準）。
        # 為什麼要這樣：CRM 的上傳是打**執行中的 Connector 服務**，而服務的
        # `KB_CACHE_DIR` 是啟動時載入的 —— 改了設定沒重啟，圖會落在舊目錄。
        # 自檢不該被「服務有沒有重啟」綁住，所以這裡自己播種，鏈路照樣全走一遍。
        seeds = payload.get("seed_files") or []
        if seeds:
            assets = content.get("assets") or []
            for i, src in enumerate(seeds):
                with open(src, "rb") as f:
                    data = f.read()
                got = media.save_bytes("kb_cache", data, os.path.basename(src))
                if i < len(assets):
                    assets[i] = {**assets[i], "media_key": got["media_key"]}
            content["assets"] = assets

        items = outbound.build_sub_items(content)
        sends = []

        async def _run():
            for it in items:
                r = await outbound._send_one(task.get("platform") or "fb",
                                             task.get("end_user_sender_id") or "",
                                             it, {}, (content.get("scope") or "dm"))
                sends.append({"kind": it["kind"], "ok": r.ok, "error": r.error,
                              "media_key": it.get("media_key")})
        asyncio.run(_run())
        # 把 `record_outbound_message` 內部的 log.warning 收進來（它 best-effort 吞例外，
        # 不看 log 就只會看到「沒落地」而不知原因）
        import logging as _logging

        _caught: list[str] = []

        class _Cap(_logging.Handler):
            def emit(self, rec):
                _caught.append(rec.getMessage())

        _cap = _Cap()
        _logging.getLogger("connector").addHandler(_cap)
        try:
            outbound.record_outbound_message(task, items)
        finally:
            _logging.getLogger("connector").removeHandler(_cap)

        # 讀回落地內容 + 用工作台真正的解析器看它變出什麼（含 url 有沒有）
        import app as _app
        from app.core import db as _db
        from app.internal import router as _router
        row, dbg = {}, {}
        _plat = task.get("platform") or "fb"
        _sender = task.get("end_user_sender_id") or ""
        _mid = f"out-{task.get('task_id')}"
        try:
            _c = _db.connect()
            try:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT current_database()")
                    dbg["dbname"] = _cur.fetchone()[0]
                    if _plat in ("wechat", "whatsapp"):
                        # ⚠️ 這兩個平台落地在 forward_log（不是 igfb_dm）——
                        #    只讀 igfb_dm 的自檢會永遠看到「沒落地」，誤判成鏈路壞掉。
                        _cur.execute(
                            "SELECT message_type, COALESCE(raw_content::text,''),"
                            "       COALESCE(forwarded_payload::text,'')"
                            "  FROM forward_log WHERE platform=%s AND message_id=%s",
                            (_plat, _mid))
                        got = _cur.fetchone()
                        if got:
                            row = {"message_type": got[0], "raw": got[1], "fwd": got[2]}
                    else:
                        _cur.execute(
                            "SELECT message_type, text, attachments FROM igfb_dm"
                            " WHERE platform=%s AND conversation_key=%s AND message_id=%s",
                            (_plat, _sender, _mid))
                        got = _cur.fetchone()
                        if got:
                            row = dict(zip(("message_type", "text", "attachments"), got))
            finally:
                _c.close()
        except Exception as e:  # noqa: BLE001 —— 回報而不是讓整支崩掉
            dbg["readback_error"] = f"{type(e).__name__}: {e}"
        dbg["app_file"] = _app.__file__
        dbg["dsn"] = _cfg.DATABASE_URL
        dbg["logs"] = _caught
        if _plat in ("wechat", "whatsapp"):
            files = _router._files_of(row.get("raw"), row.get("fwd"), None) if row else []
        else:
            files = _router._files_of(None, None, row.get("attachments")) if row else []
        out = {
            "items": [{"kind": i["kind"], "media_key": i.get("media_key")} for i in items],
            "sends": sends,
            "db": {"message_type": row.get("message_type"),
                   "attachments": row.get("attachments"),
                   "uploadedFiles": (json.loads(row["fwd"]).get("uploadedFiles")
                                     if row.get("fwd") else None)},
            "files": files,
            "media_dir": _cfg.MEDIA_DIR,
            "kb_cache_dir": _cfg.KB_CACHE_DIR,
            "debug": dbg,
        }
    elif mode == "sync-names":
        # 補名（歷史資料 + 現在缺的）：<platform> [native_id]
        from app import account_map
        from app.channels import igfb_store

        plat = args[1] if len(args) > 1 else "fb"
        if len(args) > 2:
            acc = account_map.lookup(plat, args[2]) or None
        else:
            from app.core import db as _db
            _c = _db.connect()
            try:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT platform, platform_business_account_id"
                                 " FROM connector_account_map"
                                 " WHERE platform=%s AND enabled ORDER BY bp_account_id LIMIT 1",
                                 (plat,))
                    _row = _cur.fetchone()
            finally:
                _c.close()
            acc = account_map.lookup(plat, _row[1]) if _row else None
        if not acc:
            out = {"error": f"找不到 {plat} 的帳號映射（先跑 sync-map）"}
        else:
            out = asyncio.run(igfb_store.sync_dm_names(plat, acc))
            out["account"] = acc.get("platform_business_account_id")
    elif mode == "mark-handled":
        # 清紅點：把該會話「早於現在」的客戶來訊標為已處理
        #   mark-handled <platform> <sender/target> <scope>
        from app.core import db as _db
        from app.worker import outbound as _ob

        plat, target = args[1], args[2]
        scope = args[3] if len(args) > 3 else "dm"
        _c = _db.connect()
        try:
            with _c.cursor() as _cur:
                n = _ob._mark_inbound_handled(_cur, plat, target, scope)
            _c.commit()
        finally:
            _c.close()
        out = {"handled": n}
    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        return 2

    print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
