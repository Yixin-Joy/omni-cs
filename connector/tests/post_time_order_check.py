"""自檢：貼文時間 + 訊息排序 + 補拉速度（2026-10-22 兩個回報）。

① 「服務運行期間新增帖文沒有時間顯示」
   根因：留言的 webhook 常比貼文資料早到 → `land_comment()` 先插一列**佔位貼文**
   （`created_at` 是 NULL、name='（貼文資訊待同步）'）。工作台照 `created_at`
   排序與顯示 → 時間欄空白，而且因為 `NULLS LAST` 被丟到清單最底下。
   修：列表/詳情一律 `COALESCE(created_at, inserted_at)`；收到留言時若貼文是
       佔位列，立刻背景補一次 Graph（`_bg_sync_post`）。

② 「重新整理後 facebook 私訊時間順序錯亂」
   根因：Meta 的 `created_time` 只到**秒**，同一秒的兩則訊息 `created_at` 完全相同，
   而 SQL 只排 `ts` → 資料庫每次回的順序可能不同 → 前端 20 秒輪詢一次就換一次順序。
   修：所有訊息排序加上 `id` 當第二鍵（`ORDER BY ts DESC, id DESC`）。

③ 「重新整理時間過長」
   修：帳號改**並行**（`SYNC_CONCURRENCY`）、附件共用 client 並**並行**下載。
   這裡量「3 個帳號各睡 1 秒」的實際牆鐘時間。

跑法（從 connector 目錄）：
    PYTHONPATH='<connector>;<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> tests/post_time_order_check.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONN_DIR))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import history_sync  # noqa: E402
from app.channels import igfb_store  # noqa: E402
from app.core import config  # noqa: E402
from app.internal.router import router as internal_router  # noqa: E402

MARK = "zz_pt"
PLAT = "fb"
NATIVE = f"{MARK}_page"
SENDER = f"{MARK}_u1"
POST_PLACEHOLDER = f"{MARK}_post_nosync"
POST_DONE = f"{MARK}_post_ok"
BIZ = 990902
BP = 880902

passes = 0
failures: list[str] = []


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


def q(sql: str, args=None, fetch: bool = False):
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            n = cur.rowcount
        conn.commit()
        return rows if fetch else n
    finally:
        conn.close()


def cleanup() -> None:
    q("DELETE FROM igfb_comment WHERE post_id LIKE %s", (MARK + "%",))
    q("DELETE FROM igfb_post WHERE post_id LIKE %s", (MARK + "%",))
    q("DELETE FROM igfb_dm WHERE sender_id LIKE %s OR conversation_key LIKE %s",
      (MARK + "%", MARK + "%"))


def seed() -> None:
    """① 佔位貼文（沒有 created_at，名字是待同步）② 已同步的貼文。"""
    q("""INSERT INTO igfb_post
           (platform, post_id, name, platform_business_account_id, business_id, bp_account_id)
         VALUES (%s, %s, '（貼文資訊待同步）', %s, %s, %s)""",
      (PLAT, POST_PLACEHOLDER, NATIVE, BIZ, BP))
    q("""INSERT INTO igfb_post
           (platform, post_id, name, created_at, comments_count,
            platform_business_account_id, business_id, bp_account_id)
         VALUES (%s, %s, '舊貼文', %s, 0, %s, %s, %s)""",
      (PLAT, POST_DONE, datetime.now(timezone.utc) - timedelta(days=30), NATIVE, BIZ, BP))
    # 同一秒的兩則訊息（Meta created_time 只到秒 → 這是真實會發生的情況）
    ts = datetime(2026, 10, 20, 3, 0, 0, tzinfo=timezone.utc)
    for i, (mid, txt) in enumerate(((f"{MARK}_m1", "第一則"), (f"{MARK}_m2", "第二則"))):
        q("""INSERT INTO igfb_dm
               (platform, conversation_key, thread_id, sender_id, direction, message_type,
                text, attachments, message_id, created_at, is_handled,
                platform_business_account_id, business_id, bp_account_id)
             VALUES (%s, %s, %s, %s, 'inbound', 'text', %s, '[]'::jsonb, %s, %s, false,
                     %s, %s, %s)""",
          (PLAT, SENDER, f"t_{MARK}", SENDER, txt, mid, ts, NATIVE, BIZ, BP))


def main() -> int:
    cleanup()
    seed()
    mini = FastAPI()
    mini.include_router(internal_router)
    tc = TestClient(mini)
    # 內網端點要帶 token（缺它一律 401 → 自檢會誤判成「查不到資料」）
    HDR = {"X-Internal-Token": config.INTERNAL_API_TOKEN or ""}

    try:
        # ---------------- [1] 佔位貼文也要有時間（而且排在最前面）
        print("\n[1] 佔位貼文（created_at 為 NULL）→ 工作台要有時間、且不會沉底")
        r = tc.get("/internal/api/posts", params={
            "platform": PLAT, "platform_business_account_id": NATIVE, "limit": 10},
            headers=HDR)
        items = r.json().get("items") or []
        got = {it["post_id"]: it for it in items}
        check(POST_PLACEHOLDER in got, "佔位貼文出現在列表裡")
        if POST_PLACEHOLDER in got:
            it = got[POST_PLACEHOLDER]
            check(bool(it.get("created_at")),
                  "★ 佔位貼文的 created_at 不是空的（退回 inserted_at）", str(it.get("created_at")))
            check(items[0]["post_id"] == POST_PLACEHOLDER,
                  "★ 它排在清單最前面（不是被 NULLS LAST 丟到最後）",
                  str([x["post_id"] for x in items]))

        # ---------------- [2] 詳情頁同樣不能空白
        r = tc.get("/internal/api/post", params={
            "platform": PLAT, "platform_business_account_id": NATIVE,
            "post_id": POST_PLACEHOLDER}, headers=HDR)
        d = r.json()
        check(bool(d.get("created_at")), "詳情頁的 created_at 也不是空的", str(d.get("created_at")))
        check(d.get("created_at_estimated") is True,
              "★ 明確標示 created_at_estimated=True（那不是真實發文時間）",
              str(d.get("created_at_estimated")))

        # ---------------- [3] post_needs_sync：佔位 True、已同步 False
        print("\n[3] `post_needs_sync()`：決定要不要補一次 Graph")
        check(igfb_store.post_needs_sync(PLAT, POST_PLACEHOLDER) is True,
              "佔位貼文 → True（要補）")
        check(igfb_store.post_needs_sync(PLAT, POST_DONE) is False,
              "已同步的貼文 → False（不重複打 Graph）")
        check(igfb_store.post_needs_sync(PLAT, f"{MARK}_nope") is True,
              "完全不存在的貼文 → True（也要補）")

        # ---------------- [4] 同一秒的訊息：排序必須穩定
        print("\n[4] 同一秒的兩則訊息 → 每次查詢順序都要一致（tie-break by id）")
        orders = []
        for _ in range(6):
            r = tc.get("/internal/api/messages/query", params={
                "platform": PLAT, "platform_business_account_id": NATIVE,
                "end_user_sender_id": SENDER, "scope": "dm", "limit": 20}, headers=HDR)
            its = [x for x in (r.json().get("items") or [])
                   if str(x.get("message_id", "")).startswith(MARK)]
            orders.append([x["message_id"] for x in its])
        uniq = {tuple(o) for o in orders}
        check(len(uniq) == 1, "★ 6 次查詢回傳順序完全一致（原版可能每次都不同）",
              f"{len(uniq)} 種順序")
        # Connector 回 DESC（新→舊），CRM 反轉 → 畫面是「舊→新」
        asc = list(reversed(orders[0]))
        check(asc == [f"{MARK}_m1", f"{MARK}_m2"],
              "★ 反轉後是 m1 → m2（先發的在前，同秒也不會顛倒）", str(asc))

        # ---------------- [5] 帳號並行：3 個各睡 1 秒 → 總時間要遠小於 3 秒
        print("\n[5] 批次補拉改成並行（3 個帳號各 1 秒）")
        orig = history_sync.sync_account

        async def _slow(platform, native, **kw):
            await asyncio.sleep(1.0)
            return {"platform": platform, "platform_business_account_id": native,
                    "supported": True, "dm_new": 1, "errors": []}

        history_sync.sync_account = _slow
        try:
            t0 = time.monotonic()
            res = asyncio.run(history_sync.sync_many(
                [{"platform": "fb", "platform_business_account_id": f"{MARK}_a"},
                 {"platform": "fb", "platform_business_account_id": f"{MARK}_b"},
                 {"platform": "fb", "platform_business_account_id": f"{MARK}_c"}]))
            spent = time.monotonic() - t0
        finally:
            history_sync.sync_account = orig
        check(spent < 1.9, f"★ 3 個帳號並行，牆鐘 {spent:.2f}s < 1.9s（串行會是 3s+）")
        check(res["totals"]["accounts"] == 3 and res["totals"]["dm_new"] == 3,
              "統計仍然正確彙總", str(res["totals"]))
        check([i["platform_business_account_id"] for i in res["items"]]
              == [f"{MARK}_a", f"{MARK}_b", f"{MARK}_c"],
              "結果順序與輸入一致（前端照這個順序顯示）")
    finally:
        cleanup()

    print("\n" + "=" * 64)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    if failures:
        for f in failures:
            print("   - " + f)
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
