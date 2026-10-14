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
        try:
            _c = _db.connect()
            try:
                with _c.cursor() as _cur:
                    _cur.execute("SELECT current_database()")
                    dbg["dbname"] = _cur.fetchone()[0]
                    _cur.execute(
                        "SELECT message_type, text, attachments FROM igfb_dm"
                        " WHERE platform=%s AND conversation_key=%s AND message_id=%s",
                        (task.get("platform") or "fb", task.get("end_user_sender_id") or "",
                         f"out-{task.get('task_id')}"))
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
        files = _router._files_of(None, None, row.get("attachments")) if row else []
        out = {
            "items": [{"kind": i["kind"], "media_key": i.get("media_key")} for i in items],
            "sends": sends,
            "db": {"message_type": row.get("message_type"),
                   "attachments": row.get("attachments")},
            "files": files,
            "media_dir": _cfg.MEDIA_DIR,
            "kb_cache_dir": _cfg.KB_CACHE_DIR,
            "debug": dbg,
        }
    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        return 2

    print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
