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
    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        return 2

    print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
