"""階段二自檢的 CRM 側執行器 —— 單次動作，供 `phase2_check.py` 以子進程呼叫。

用法（cwd 必須是 crm/）：
    python tests/_crm_driver.py sweep
    python tests/_crm_driver.py sweep-sessions
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # crm/


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else "sweep"

    if mode == "sweep":
        from app.worker import sweep
        out = sweep.sweep_once()
    elif mode == "sweep-sessions":
        from app.worker import sweep
        out = {"purged": sweep.sweep_sessions_once()}
    else:
        print(json.dumps({"error": f"unknown mode: {mode}"}))
        return 2

    print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
