"""自檢：貼文的「特殊配置」真的會生效（2026-10-22 P0）。

背景：小jo 在 IG 某篇貼文取消了 FAQ「怎麼退貨？」的啟用，留言卻照樣命中。
根因是 `post_ai_config.id`（CRM 主鍵）Connector 不可能知道 → `content` 裡
**從來沒有** `post_config_id` → worker 永遠傳 0 → 一路用商家基準集，
貼文覆寫集形同不存在。修法：`resolve_scope_set()` 用 `platform + post_id` 自己反查。

驗 5 件事：
  [1] `post_config_id=0` + 有 `post_id` → 解析成 **post** 覆寫集（不是 business）
  [2] 有正確 `post_config_id` → 同上（兩條路等價）
  [3] 傳錯 `post_config_id`（屬於別的貼文）→ 不會套錯配置，改用 `post_id` 反查
  [4] 引擎決策：取消的那條 FAQ 不再命中（修前會命中，分數 1.00）
  [5] `handle_inbound()` 端到端：**只傳 post_id**（模擬 worker 的真實參數）也能生效

★ 全程不建任務、不發訊息：`enqueue_reply` 被換成記錄器；`is_enabled` / `_guard`
  臨時改成放行（**不動 DB 開關**，避免影響線上行為）。跑完刪掉本測試寫的 autoreply_log。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/post_config_check.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app.autoreply import autoreply, autoreply_db  # noqa: E402
from app.core import config  # noqa: E402
from app.llm import reply_engine  # noqa: E402

BIZ = 1
PLAT = "ig"
POST = "17978252924918238"          # Epro_test 真實貼文（有 6 則留言）
OTHER_POST = "17903112072369114"    # 同商家的另一篇（用來測「傳錯 id」）
FAQ_KEEP = [32, 34, 35]
FAQ_DROP = 33                        # 「怎麼退貨？」
Q = "怎麼退貨？"

fails: list[str] = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def db():
    return psycopg2.connect(config.DATABASE_URL)


def make_cfg(platform: str, post_id: str, faq_ids: list[int]) -> int:
    conn = db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO post_ai_config (platform, post_id, business_platform_account_id,"
                " enabled, configured, kb_enabled, table_enabled, faq_enabled)"
                " VALUES (%s, %s, NULL, true, true, false, false, true) RETURNING id",
                (platform, post_id))
            pid = cur.fetchone()["id"]
            for fid in faq_ids:
                cur.execute("INSERT INTO post_faq_binding (post_config_id, faq_id, enabled)"
                            " VALUES (%s, %s, true)", (pid, fid))
        conn.commit()
        return pid
    finally:
        conn.close()


def drop_cfg(pid: int) -> None:
    conn = db()
    try:
        with conn.cursor() as cur:
            for t in ("post_faq_binding", "post_table_binding", "post_kb_binding"):
                cur.execute(f"DELETE FROM {t} WHERE post_config_id=%s", (pid,))
            cur.execute("DELETE FROM post_ai_config WHERE id=%s", (pid,))
        conn.commit()
    finally:
        conn.close()


async def main() -> int:
    # 前置：乾淨狀態 —— 真的沒配置過時，這裡要回 0
    pre = autoreply_db.post_config_id_for(PLAT, POST)
    check(pre == 0, f"前置：這則貼文目前沒有配置（實得 id={pre}）")

    pid = make_cfg(PLAT, POST, FAQ_KEEP)
    other = make_cfg(PLAT, OTHER_POST, FAQ_KEEP)
    print(f"\n測試資料：post_ai_config #{pid}（{PLAT}/{POST}），FAQ 勾 {FAQ_KEEP}，"
          f"刻意排除 #{FAQ_DROP}「怎麼退貨」；對照組 #{other}（另一篇貼文）\n")

    try:
        # ---------------- [1] 只有 post_id 也能解析（＝worker 的實際情形）
        print("[1] post_config_id=0 + post_id → 自己反查")
        got = autoreply_db.post_config_id_for(PLAT, POST)
        check(got == pid, f"post_config_id_for() 回 {got}（期望 {pid}）")
        ss = autoreply_db.resolve_scope_set(BIZ, scope="comment", platform=PLAT, post_id=POST)
        check(ss["source"] == "post", f"source=post（實得 {ss['source']}）")
        check(FAQ_DROP not in ss["faq"], f"★ 取消的 FAQ #{FAQ_DROP} 不在啟用集（實得 {ss['faq']}）")
        check(sorted(ss["faq"]) == FAQ_KEEP, f"其餘三条仍在（實得 {ss['faq']}）")

        # ---------------- [2] 傳正確 id 也一樣
        print("\n[2] 傳正確的 post_config_id → 結果相同")
        ss2 = autoreply_db.resolve_scope_set(BIZ, scope="comment", post_config_id=pid)
        check(ss2["source"] == "post" and ss2["faq"] == ss["faq"],
              f"兩條路等價（{ss2['source']} / {ss2['faq']}）")

        # ---------------- [3] 傳錯 id 不會套錯配置
        print("\n[3] 傳錯 post_config_id（屬於別的貼文）")
        ss3 = autoreply_db.resolve_scope_set(BIZ, scope="comment",
                                            post_config_id=other, platform=PLAT, post_id=POST)
        check(ss3["source"] == "post" and ss3["faq"] == ss["faq"],
              f"改用 post_id 反查，不是套 #{other}（{ss3['source']} / {ss3['faq']}）")

        # ---------------- [4] 引擎決策：取消的那條不再命中
        print("\n[4] 引擎決策（只跑 FAQ 層，0 次模型呼叫）")
        biz_ss = autoreply_db.business_scope_set(BIZ)
        a_old = await reply_engine.answer(Q, business_id=BIZ, channel=PLAT, scope="comment",
                                          layers=["faq"], query_rewrite=False, scope_set=biz_ss)
        check(a_old.hit and getattr(a_old, "faq_id", None) == FAQ_DROP,
              f"對照：用商家基準集 → 命中 #{FAQ_DROP}（tier={a_old.tier}）")
        a_new = await reply_engine.answer(Q, business_id=BIZ, channel=PLAT, scope="comment",
                                          layers=["faq"], query_rewrite=False, scope_set=ss)
        check(not a_new.hit, f"★ 用貼文覆寫集 → 不再命中（tier={a_new.tier}）")

        # ---------------- [5] handle_inbound 端到端（模擬 worker 參數）
        print("\n[5] handle_inbound：只傳 post_id（worker 的真實參數）")
        made: list[dict] = []
        orig = (autoreply.is_enabled, autoreply._guard, autoreply.enqueue_reply)
        autoreply.is_enabled = lambda *a, **k: True
        autoreply._guard = lambda *a, **k: ""
        autoreply.enqueue_reply = lambda **kw: (made.append(kw) or {"task_id": "ZZTEST"})
        mid = "zz-postcfg-" + uuid.uuid4().hex[:10]
        try:
            res = await autoreply.handle_inbound(
                business_id=BIZ, channel=PLAT, scope="comment", text=Q,
                platform_account_id=29, to="", message_id=mid,
                comment_id="zz-fake-" + mid, post_id=POST,
                conversation_key=mid)
        finally:
            autoreply.is_enabled, autoreply._guard, autoreply.enqueue_reply = orig
        check(res.get("state") == autoreply.STATE_MISS,
              f"★ 未命中（state={res.get('state')}，detail={res.get('detail')}）")
        check(not made, f"★ 沒有建任何發送任務（實得 {len(made)} 個）")
    finally:
        drop_cfg(pid)
        drop_cfg(other)
        # 清掉本測試寫的 autoreply_log（claim_key / message_id 都帶 zz-postcfg- 前綴）
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM autoreply_log WHERE message_id LIKE 'zz-postcfg-%'")
                n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        print(f"\n已清理：post_ai_config #{pid}/#{other} 與其綁定、autoreply_log {n} 筆")

    print()
    if fails:
        print(f"結果：{len(fails)} 項失敗")
        for f in fails:
            print("  - " + f)
        return 1
    print("結果：全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
