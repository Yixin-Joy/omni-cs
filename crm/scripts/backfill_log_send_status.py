r"""把 `autoreply_log.send_status` 補成任務的真實終態（可重複執行、冪等）。

為什麼需要這支：
  `autoreply_log.send_status` 原本只在**建立紀錄那一刻**寫一次
  （`not_sent` / `queued` / `failed`），任務真的送成功、`crm_outbound_task`
  變成 `sent` 之後，**沒有任何地方回頭更新它** → 記錄頁永遠停在「未送出」
  （小jo 2026-10-14 回報：「記錄中明明已經發出了為麼顯示未送出」）。

  現在 `crm/app/internal/repo.py` 已經在回調／逾時補記時回寫
  （`_sync_log_send_status`）。這支腳本只用來**補歷史資料**：
  跑一次之後，舊紀錄的狀態就與任務對齊。

  附帶效果：`recent_reply_count()` / `today_reply_count()` 靠
  `send_status='sent'` 判斷 → 回填後**冷卻與每日上限才真的生效**。

用法（在 repo 根目錄跑）：
  PYTHONPATH='<根>\crm;<根>\.venv\Lib\site-packages' PYTHONUTF8=1 \
    <python> crm/scripts/backfill_log_send_status.py          # 只報告，不改
  ... backfill_log_send_status.py --apply                    # 真的回填
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.core import db                        # noqa: E402

# 任務終態 → 紀錄狀態。`pending`/`processing`（重試中）維持原樣，
# 因為那時確實還在傳。
_MAP = {"sent": "sent", "uncertain": "uncertain", "failed": "failed"}

_PENDING_LOG = ("queued", "pending", "processing")

_SELECT = (
    "SELECT l.id AS log_id, l.platform, l.outbound_task_id, l.send_status,"
    "       t.status AS task_status, l.created_at"
    "  FROM autoreply_log l"
    "  JOIN crm_outbound_task t ON t.id = l.outbound_task_id"
    " WHERE l.send_status IN (" + ",".join(["%s"] * len(_PENDING_LOG)) + ")"
    "   AND t.status IN (" + ",".join(["%s"] * len(_MAP)) + ")"
    " ORDER BY l.id"
)


def find_mismatched() -> list[dict]:
    conn = db.connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_SELECT, (*_PENDING_LOG, *_MAP))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def apply_fix() -> int:
    """一句 SQL 對齊（冪等：只動還在 queued/pending/processing 的）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE autoreply_log l SET send_status ="
                "       CASE t.status WHEN 'sent' THEN 'sent'"
                "                     WHEN 'uncertain' THEN 'uncertain'"
                "                     ELSE 'failed' END"
                "  FROM crm_outbound_task t"
                " WHERE t.id = l.outbound_task_id"
                "   AND l.send_status IN (" + ",".join(["%s"] * len(_PENDING_LOG)) + ")"
                "   AND t.status IN (" + ",".join(["%s"] * len(_MAP)) + ")",
                (*_PENDING_LOG, *_MAP),
            )
            n = cur.rowcount
        conn.commit()
        return int(n or 0)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的寫入（預設只報告）")
    a = ap.parse_args()

    rows = find_mismatched()
    print(f"需要回填的紀錄：{len(rows)} 筆")
    for r in rows:
        print(f"  log={r['log_id']:<5} {r['platform']:<9} task={r['outbound_task_id']:<5}"
              f" {r['send_status']} → {_MAP[r['task_status']]}"
              f"  ({r['created_at']:%Y-%m-%d %H:%M})")

    if not rows:
        print("\n沒有需要處理的紀錄（已是對齊狀態）。")
        return 0
    if not a.apply:
        print("\n（dry-run）加 --apply 才會真的寫入。")
        return 0

    n = apply_fix()
    print(f"\n已回填 {n} 筆。")
    left = find_mismatched()
    print(f"覆核：仍不一致 {len(left)} 筆。")
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
