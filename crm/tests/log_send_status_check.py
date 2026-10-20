"""驗證：任務終態會回寫 `autoreply_log.send_status`（記錄頁不再卡「未送出」）。

★ 為什麼要有這支（2026-10-14 小jo 回報）：
  「記錄中最新幾條自動回復明明已經發出了，為麼顯示未送出」
  → `autoreply_log.send_status` 只在建立紀錄時寫 `queued`，之後**沒人回寫**；
    前端只認 `sent`，其餘一律「未送出」。
  ⚠️ 更嚴重的連帶影響：`recent_reply_count()` / `today_reply_count()` 靠
    `send_status='sent'` → 一直算 0 → **冷卻與每日上限形同不存在**。

⚠️ 測試任務一律插成 `status='processing'`：
   Connector worker 只 claim `pending`（`_CLAIM_SQL`），插 pending 會被在線 worker
   搶走並**真的發到平台**（曾踩過）。

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> crm/tests/log_send_status_check.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.core import db                                # noqa: E402
from app.internal import repo                          # noqa: E402

_pass = 0
_fail: list[str] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    global _pass
    if ok:
        _pass += 1
        print(f"  PASS  {what}")
    else:
        _fail.append(what)
        print(f"  FAIL  {what}   {detail}")


def _conn():
    return db.connect()


def make_task(status: str = "processing", *, max_retry: int = 0,
              retry_count: int = 0) -> int:
    """造一筆任務（預設 processing → 不會被 worker 搶）。"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_outbound_task"
                " (business_id, source, platform, end_user_sender_id, message_content,"
                "  status, max_retry, retry_count, backoff_seconds, processing_at)"
                " VALUES (1, 'ai', 'whatsapp', %s, '{\"text\":\"自檢\"}'::jsonb,"
                "         %s, %s, %s, 1, now()) RETURNING id",
                (f"__CK__logstatus_{status}_{max_retry}_{retry_count}", status,
                 int(max_retry), int(retry_count)))
            tid = int(cur.fetchone()[0])
        conn.commit()
        return tid
    finally:
        conn.close()


def make_log(task_id: int, send_status: str = "queued") -> int:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO autoreply_log"
                " (business_id, platform, tier, outbound_task_id, end_user_sender_id,"
                "  question, answer, send_status)"
                " VALUES (1, 'whatsapp', 'rag', %s, %s, '自檢問句', '自檢答案', %s)"
                " RETURNING id",
                (int(task_id), f"__CK__logstatus_{task_id}", send_status))
            lid = int(cur.fetchone()[0])
        conn.commit()
        return lid
    finally:
        conn.close()


def log_status(lid: int) -> str:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT send_status FROM autoreply_log WHERE id=%s", (lid,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def cleanup(tids: list[int], lids: list[int]) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if lids:
                cur.execute("DELETE FROM autoreply_log WHERE id = ANY(%s)", (lids,))
            if tids:
                cur.execute("DELETE FROM crm_outbound_task WHERE id = ANY(%s)", (tids,))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    tids: list[int] = []
    lids: list[int] = []

    print("[1] 任務 sent → 紀錄 queued 應該變 sent")
    t1 = make_task()
    l1 = make_log(t1)
    tids.append(t1)
    lids.append(l1)
    check(log_status(l1) == "queued", "前置：紀錄初始為 queued")
    repo.update_task_status(t1, "sent")
    check(log_status(l1) == "sent", "★ 回寫成功（這是本 bug 的核心）", f"實際={log_status(l1)}")

    print("\n[2] 任務 uncertain → 紀錄變 uncertain")
    t2 = make_task()
    l2 = make_log(t2)
    tids.append(t2)
    lids.append(l2)
    repo.update_task_status(t2, "uncertain", fail_reason="自檢：不確定")
    check(log_status(l2) == "uncertain", "uncertain 有回寫", f"實際={log_status(l2)}")

    print("\n[3] 任務 failed（重試耗盡，max_retry=0）→ 紀錄變 failed")
    t3 = make_task(max_retry=0, retry_count=0)
    l3 = make_log(t3)
    tids.append(t3)
    lids.append(l3)
    repo.update_task_status(t3, "failed", fail_reason="自檢：失敗", is_temp_error=False)
    check(log_status(l3) == "failed", "failed 終態有回寫", f"實際={log_status(l3)}")

    print("\n[4] 任務 failed 但『還會重試』→ 紀錄維持 queued（還在傳，不該標失敗）")
    t4 = make_task(max_retry=3, retry_count=0)
    l4 = make_log(t4)
    tids.append(t4)
    lids.append(l4)
    res = repo.update_task_status(t4, "failed", fail_reason="自檢：臨時錯誤", is_temp_error=True)
    check(res and res.get("status") == "pending", "任務退回 pending 重試",
          f"實際={res}")
    check(log_status(l4) == "queued", "重試中 → 紀錄不變（仍為傳送中）",
          f"實際={log_status(l4)}")

    print("\n[5] 逾時補記成功（mark_sent_recovered）→ 紀錄也要變 sent")
    t5 = make_task()
    l5 = make_log(t5)
    tids.append(t5)
    lids.append(l5)
    check(repo.mark_sent_recovered(t5, "自檢：逾時補記") is True, "補記回傳 True")
    check(log_status(l5) == "sent", "補記也回寫 sent", f"實際={log_status(l5)}")

    print("\n[6] 逾時判不確定（mark_uncertain）→ 紀錄變 uncertain")
    t6 = make_task()
    l6 = make_log(t6)
    tids.append(t6)
    lids.append(l6)
    check(repo.mark_uncertain(t6, "自檢：逾時不確定") is True, "mark_uncertain 回傳 True")
    check(log_status(l6) == "uncertain", "uncertain 有回寫", f"實際={log_status(l6)}")

    print("\n[7] 冷卻／每日上限的計數真的算得到（原本永遠 0）")
    from app.autoreply import autoreply_db
    t7 = make_task(status="sent")
    l7 = make_log(t7, send_status="sent")
    tids.append(t7)
    lids.append(l7)
    n1 = autoreply_db.recent_reply_count(1, f"__CK__logstatus_{t7}", 3600)
    check(n1 >= 1, "recent_reply_count 數得到（冷卻判斷生效）", f"實際={n1}")
    n2 = autoreply_db.today_reply_count(1, f"__CK__logstatus_{t7}")
    check(n2 >= 1, "today_reply_count 數得到（每日上限生效）", f"實際={n2}")

    print("\n[8] 沒有對應紀錄的任務 → 不該爆（人工回覆就是這樣）")
    t8 = make_task()
    tids.append(t8)
    try:
        repo.update_task_status(t8, "sent")
        check(True, "無對應紀錄時安靜跳過（rowcount=0）")
    except Exception as e:  # noqa: BLE001
        check(False, "無對應紀錄時安靜跳過", f"{type(e).__name__}: {e}")

    cleanup(tids, lids)
    print(f"\n{'=' * 60}\n  紀錄送出狀態：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
