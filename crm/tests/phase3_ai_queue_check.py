"""階段三自檢 —— AI 入站佇列消費鏈路（`crm_ai_inbound_queue`）。

驗的是「真訊息進來 → CRM 跑引擎 → 命中建任務 → 日誌留痕」這條鏈，
用真 DB、真引擎（FAQ 層是純規則，不呼叫 LLM），只有 worker 崩潰情境用 mock。

跑法（cwd 必須是 crm/）：
    python tests/phase3_ai_queue_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2                      # noqa: E402
import psycopg2.extras               # noqa: E402

from app.autoreply import autoreply  # noqa: E402
from app.autoreply import autoreply_db  # noqa: E402
from app.core import config, db      # noqa: E402
from app.internal import repo        # noqa: E402
from app.worker import ai_queue      # noqa: E402

MARK = "P3Q"
NATIVE = MARK + "_native"
QUESTION = "測試問句" + MARK + "ABC"
ANSWER = "測試答案" + MARK + "XYZ"

_ok = [0, 0]                # [PASS, FAIL]


def check(cond, msg: str) -> None:
    _ok[0 if cond else 1] += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg)


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


# ---------------------------------------------------------------- 場地

def cleanup() -> None:
    sql("DELETE FROM autoreply_log WHERE business_id IN"
        " (SELECT id FROM crm_business WHERE business_name LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_outbound_task WHERE business_id IN"
        " (SELECT id FROM crm_business WHERE business_name LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_ai_inbound_queue WHERE business_id IN"
        " (SELECT id FROM crm_business WHERE business_name LIKE %s)", (MARK + "%",), fetch=False)
    for t in ("business_faq_binding", "business_table_binding", "business_kb_binding",
              "business_ai_switch", "business_layer_switch", "autoreply_faq"):
        sql(f"DELETE FROM {t} WHERE business_id IN"
            " (SELECT id FROM crm_business WHERE business_name LIKE %s)", (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_business_platform_account WHERE platform_business_account_id LIKE %s",
        (MARK + "%",), fetch=False)
    sql("DELETE FROM crm_business WHERE business_name LIKE %s", (MARK + "%",), fetch=False)


def setup() -> int:
    cleanup()
    biz = sql("INSERT INTO crm_business (business_name) VALUES (%s) RETURNING id",
              (MARK + "商家",))[0]["id"]
    sql("INSERT INTO crm_business_platform_account"
        " (business_id, platform, platform_business_account_id, display_name)"
        " VALUES (%s, 'wechat', %s, %s)", (biz, NATIVE, MARK))
    return biz


def faq(biz: int, question: str, answer: str) -> int:
    """建 FAQ —— 走 `create_faq`，它會自動把這條寫進基準啟用集（設計案 §9.4）。

    ⚠️ 不能用裸 SQL：沒有 binding 的 FAQ 在引擎眼裡等於不存在（空集 = 不跑）。
    """
    return autoreply_db.create_faq(biz, question, answer)["id"]


def queue_row(mid: str) -> dict | None:
    rows = sql("SELECT * FROM crm_ai_inbound_queue WHERE message_id = %s", (mid,))
    return rows[0] if rows else None


def row_id(mid: str) -> int:
    return queue_row(mid)["id"]


def log_rows(biz: int) -> list[dict]:
    return sql("SELECT * FROM autoreply_log WHERE business_id = %s ORDER BY id", (biz,))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print("=" * 68)
    print("階段三自檢：AI 入站佇列（crm_ai_inbound_queue）")
    print("=" * 68)

    db.init_schema_sync()
    biz = setup()
    print(f"\n[0] 場地：business={biz}，native={NATIVE}")

    # ---------------------------------------------------------------- [1] 入隊
    print("\n[1] 入隊（Connector → /internal/api/ai/inbound 等價路徑）")
    mid = MARK + "_m1"
    r = repo.enqueue_ai_inbound(mid, "wechat", NATIVE, "u_" + MARK,
                                {"text": QUESTION, "assets": [], "scope": "dm"})
    check(r.get("queued") is True and r.get("business_id") == biz,
          f"已註冊帳號 → 入隊並認到商家（實得 {r}）")
    row = queue_row(mid)
    check(row and row["status"] == "pending" and row["retry_count"] == 0,
          f"佇列項為 pending、retry_count=0（實得 {row and (row['status'], row['retry_count'])}）")
    r2 = repo.enqueue_ai_inbound(mid, "wechat", NATIVE, "u_" + MARK,
                                 {"text": QUESTION, "scope": "dm"})
    check(r2.get("duplicate") is True, f"同一 message_id 重送 → duplicate（實得 {r2}）")
    r3 = repo.enqueue_ai_inbound(MARK + "_unreg", "wechat", MARK + "_nope", "u",
                                 {"text": "x", "scope": "dm"})
    check(r3.get("queued") is False and r3.get("reason") == "unregistered_account",
          f"未註冊帳號 → 不入隊並回報原因（實得 {r3}）")

    # ---------------------------------------------------------------- [2] 開關關閉
    print("\n[2] 開關關閉（business_ai_switch 無此列 = 關）")
    stats = run(ai_queue.process_once())
    check(stats.get("skipped") == 1 and stats.get("claimed") == 1,
          f"未開開關 → skipped（實得 {stats}）")
    row = queue_row(mid)
    check(row["status"] == "done" and row["retry_count"] == 1,
          f"已消費：status=done、retry_count=1（實得 {row['status']}/{row['retry_count']}）")
    check(sql("SELECT count(*) AS n FROM crm_outbound_task WHERE business_id=%s", (biz,))[0]["n"] == 0,
          "未開開關 → 不建外送任務")

    # ---------------------------------------------------------------- [3] 命中 → 建任務
    print("\n[3] 命中 → 建任務 + 日誌留痕")
    autoreply_db.set_scope_mode(biz, "wechat", "dm", True)
    fid = faq(biz, QUESTION, ANSWER)
    check(autoreply.is_enabled(biz, "wechat", "dm") is True, "開關已開（business_ai_switch）")

    mid2 = MARK + "_m2"
    repo.enqueue_ai_inbound(mid2, "wechat", NATIVE, "u_" + MARK,
                            {"text": QUESTION, "assets": [], "scope": "dm"})
    stats = run(ai_queue.process_once())
    check(stats.get("hit") == 1, f"本輪 hit=1（實得 {stats}）")
    row = queue_row(mid2)
    check(row["status"] == "done" and row["processed_at"] is not None,
          f"佇列項收斂為 done（實得 {row['status']}）")

    tasks = sql("SELECT * FROM crm_outbound_task WHERE business_id=%s", (biz,))
    check(len(tasks) == 1 and tasks[0]["status"] == "pending" and tasks[0]["source"] == "ai",
          f"建了 1 筆 pending/ai 任務（實得 {[(t['id'], t['status'], t['source']) for t in tasks]}）")
    check(tasks and tasks[0]["message_content"].get("text") == ANSWER,
          f"任務內容＝FAQ 答案（實得 {tasks and tasks[0]['message_content']}）")

    logs = log_rows(biz)
    hit = [r for r in logs if r["tier"] == "faq"]
    check(len(hit) == 1, f"日誌有 1 筆 tier=faq（實得 {[(r['tier'], r['send_status']) for r in logs]}）")
    if hit:
        L = hit[0]
        check(L["send_status"] == "queued" and L["outbound_task_id"] == tasks[0]["id"],
              f"日誌 send_status=queued、outbound_task_id 對得上任務（實得 "
              f"{L['send_status']}/{L['outbound_task_id']} vs task {tasks and tasks[0]['id']}）")
        check(L["platform"] == "wechat" and L["scope"] == "dm" and L["question"] == QUESTION,
              f"日誌帶 platform/scope/question（實得 {L['platform']}/{L['scope']}/{L['question']}）")
        check(L["business_platform_account_id"] is not None and L["end_user_sender_id"] == "u_" + MARK,
              "日誌帶平台帳號與 end_user_sender_id")
    check(sql("SELECT hit_count FROM autoreply_faq WHERE id=%s", (fid,))[0]["hit_count"] == 1,
          "FAQ 命中次數 +1")

    # ---------------------------------------------------------------- [3b] 兩層 AND 生效於佇列鏈路
    print("\n[3b] 兩層 AND（帳號層）生效於佇列鏈路")
    acc_id = sql("SELECT id FROM crm_business_platform_account"
                 " WHERE platform_business_account_id=%s", (NATIVE,))[0]["id"]
    autoreply_db.set_platform_account_ai(acc_id, False)
    repo.enqueue_ai_inbound(MARK + "_m_acc", "wechat", NATIVE, "u_" + MARK,
                            {"text": QUESTION, "assets": [], "scope": "dm"})
    stats = run(ai_queue.process_once())
    check(stats.get("skipped") == 1, f"帳號層關 → 佇列這筆被略過（實得 {stats}）")
    check(len(sql("SELECT id FROM crm_outbound_task WHERE business_id=%s", (biz,))) == 1,
          "帳號層關 → 沒有多建任務")
    autoreply_db.set_platform_account_ai(acc_id, True)

    # ---------------------------------------------------------------- [4] 第二道鎖（claim_key）
    print("\n[4] 冪等第二道鎖：同一則訊息重複投遞")
    again = run(autoreply.handle_inbound(
        business_id=biz, channel="wechat", scope="dm", text=QUESTION,
        platform_account_id=0, to="u_" + MARK, message_id=mid2))
    check(again.get("state") == autoreply.STATE_SKIPPED and again.get("reason") == "duplicate",
          f"同 message_id 再跑 → duplicate（實得 {again}）")
    check(len(sql("SELECT id FROM crm_outbound_task WHERE business_id=%s", (biz,))) == 1,
          "沒有多建任務")

    # ---------------------------------------------------------------- [5] 逾時回收
    print("\n[5] 逾時回收（worker 崩掉留下的 processing）")
    mid3 = MARK + "_m3"
    repo.enqueue_ai_inbound(mid3, "wechat", NATIVE, "u_" + MARK, {"text": "x", "scope": "dm"})
    sql("UPDATE crm_ai_inbound_queue SET status='processing', retry_count=1,"
        " claimed_at = now() - make_interval(secs => 3600) WHERE message_id=%s", (mid3,), fetch=False)
    res = repo.reap_stuck_ai_inbound(timeout=600, max_retry=2)
    check(sql("SELECT status FROM crm_ai_inbound_queue WHERE message_id=%s",
              (mid3,))[0]["status"] == "pending" and res["requeued"] == [row_id(mid3)],
          f"還有重試額度 → 退回 pending（實得 {res}）")
    sql("UPDATE crm_ai_inbound_queue SET status='processing', retry_count=9,"
        " claimed_at = now() - make_interval(secs => 3600) WHERE message_id=%s", (mid3,), fetch=False)
    res = repo.reap_stuck_ai_inbound(timeout=600, max_retry=2)
    check(sql("SELECT status FROM crm_ai_inbound_queue WHERE message_id=%s",
              (mid3,))[0]["status"] == "failed",
          f"重試額度用完 → failed，不再無限重跑（實得 {res}）")

    # ---------------------------------------------------------------- [6] worker 崩潰 → 重排／標失敗
    print("\n[6] 引擎層崩潰 → 退回重試 / 超過額度標 failed")
    orig = autoreply.handle_inbound

    async def boom(**kw):
        raise RuntimeError("模擬 worker 崩潰")

    autoreply.handle_inbound = boom
    try:
        mid4 = MARK + "_m4"
        repo.enqueue_ai_inbound(mid4, "wechat", NATIVE, "u_" + MARK, {"text": "x", "scope": "dm"})
        stats = run(ai_queue.process_once())
        row = queue_row(mid4)
        check(stats.get("retry") == 1 and row["status"] == "pending",
              f"第 1 次崩潰 → retry、退回 pending（實得 {stats}／{row['status']}）")
        run(ai_queue.process_once())     # retry_count=2
        run(ai_queue.process_once())     # retry_count=3 > max_retry(2) → failed
        row = queue_row(mid4)
        check(row["status"] == "failed" and "模擬 worker 崩潰" in (row["fail_reason"] or ""),
              f"超過重試上限 → failed 並留原因（實得 {row['status']}／{row['fail_reason']}）")
    finally:
        autoreply.handle_inbound = orig

    # ---------------------------------------------------------------- [7] 快照防守
    print("\n[7] 快照防守（舊系統踩過的『jsonb 是純量』）")
    check(ai_queue._inbound_text({"text": " 你好 "}) == " 你好 ",
          "_inbound_text 取 text")
    check(ai_queue._inbound_text({"caption": "說明文字"}) == "說明文字",
          "沒有 text 時退回 caption")
    check(ai_queue._inbound_text({"text": "   "}) == "",
          "空白文字 → 空字串（交人工）")
    mid5 = MARK + "_m5"
    sql("INSERT INTO crm_ai_inbound_queue (message_id, business_id, platform,"
        " business_platform_account_id, end_user_sender_id, content)"
        " VALUES (%s, %s, 'wechat', (SELECT id FROM crm_business_platform_account"
        " WHERE platform_business_account_id=%s), 'u', '123')", (mid5, biz, NATIVE), fetch=False)
    stats = run(ai_queue.process_once())
    row = queue_row(mid5)
    check(stats.get("claimed") == 1 and row["status"] in ("done", "failed"),
          f"純量快照不會讓 worker 炸（實得 {stats}／{row['status']}／{row['fail_reason']}）")

    # ---------------------------------------------------------------- 收尾
    print("\n[8] 收尾")
    stats = repo.ai_queue_stats()
    print(f"  佇列狀態分佈：{stats}")
    cleanup()
    check(sql("SELECT count(*) AS n FROM crm_ai_inbound_queue WHERE message_id LIKE %s",
              (MARK + "%",))[0]["n"] == 0, "殘骸已清空")

    print("\n" + "=" * 68)
    print(f"結果：PASS {_ok[0]} / FAIL {_ok[1]}")
    print("=" * 68)
    return 1 if _ok[1] else 0


if __name__ == "__main__":
    raise SystemExit(main())
