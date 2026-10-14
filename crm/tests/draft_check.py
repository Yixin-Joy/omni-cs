"""自檢 —— 工作台右鍵「由自動回復引擎產生回覆」與「標記這一則已處理」。

覆蓋：
  [A] `POST /console/agent/draft` —— 參數校驗、權限、商家隔離
  [B] 私訊：對話上下文（以被右鍵那一則為界、轉成正序、只取最近 N 則）
  [C] 未命中寫 `manual_no_match` 紀錄；命中**不寫**（由送出時才記）
  [D] `POST /console/agent/read` 帶 `message_id` → **只標這一則**，不擴大到整個會話

打樁 `reply_engine.answer` / `connector_client` / `autoreply_db.resolve_scope_set`，
不起服務、不打真 Connector、不動 LLM。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/draft_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRM_DIR = ROOT / "crm"
sys.path.insert(0, str(CRM_DIR))

from app.agent import console_router, router as agent_router  # noqa: E402
from app.internal import connector_client  # noqa: E402
from app.llm import reply_engine  # noqa: E402

failures: list[str] = []
passes = 0


def check(cond, msg, extra=""):
    global passes
    if cond:
        passes += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        failures.append(msg)


SUPER = {"active_role": "agent", "roles": ["admin", "agent"], "staff_id": 1, "is_super_agent": True}
OPS = {"active_role": "ops", "roles": ["ops"], "staff_id": 3, "is_super_agent": False}

# Connector messages/query 回 DESC（新→舊）
HISTORY_DESC = [
    {"message_id": "m4", "direction": "inbound", "text": "現在有貨嗎", "is_handled": False},
    {"message_id": "m3", "direction": "outbound", "text": "稍等我看一下", "is_handled": True},
    {"message_id": "m2", "direction": "inbound", "text": "我要買", "is_handled": False},
    {"message_id": "m1", "direction": "inbound", "text": "你好", "is_handled": True},
]


def run() -> int:
    calls: dict = {"answer": [], "miss": [], "mark": [], "scope": None}
    orig = {
        "answer": reply_engine.answer,
        "resolve": console_router.autoreply_db.resolve_scope_set,
        "history": connector_client.fetch_history,
        "mark": agent_router.connector_client.mark_message,
        "audit": agent_router.audit.log_standalone,
        "can_biz": console_router.rbac.can_access_business,
        "can_biz2": agent_router.rbac.can_access_business,
        "can_view": agent_router.rbac.can_view_conversation,
        "miss": __import__("app.autoreply.autoreply", fromlist=["x"]).log_manual_miss,
    }
    from app.autoreply import autoreply as ar_mod

    async def fake_answer(text, *, business_id, channel, scope, **kw):
        calls["answer"].append({"text": text, "business_id": business_id,
                                "channel": channel, "scope": scope,
                                "history": kw.get("history"),
                                "scope_set": kw.get("scope_set")})
        if "不行" in text:
            return reply_engine.Answer(reply_engine.TIER_MISS, "", detail="未命中任何一層")
        return reply_engine.Answer(reply_engine.TIER_FAQ, "這是 FAQ 答案", score=0.91)

    try:
        reply_engine.answer = fake_answer
        console_router.reply_engine.answer = fake_answer
        console_router.autoreply_db.resolve_scope_set = (
            lambda biz, scope="dm", post_config_id=0: {"faq": [1], "table": [], "kb": [],
                                                       "layers": {}, "source": "baseline"})
        connector_client.fetch_history = lambda plat, key, **kw: list(HISTORY_DESC)
        ar_mod.log_manual_miss = lambda biz, ch, sc, **kw: calls["miss"].append((biz, ch, sc, kw))
        console_router.rbac.can_access_business = lambda sess, bid: True
        agent_router.rbac.can_access_business = lambda sess, bid: True
        agent_router.rbac.can_view_conversation = lambda sess: "agent" in (sess.get("roles") or [])

        def fake_mark(platform, **kw):
            calls["mark"].append({"platform": platform, **kw})
            return 1
        agent_router.connector_client.mark_message = fake_mark
        agent_router.audit.log_standalone = lambda sess, op, **kw: None

        print("\n[A] draft 端點：參數與權限")
        # ⚠️ 微信私訊在 `config.AUTOREPLY_SCOPES` 裡是**合法場景**（自動回復支援），
        #   所以後端不擋。工作台前端仍沿用舊版 `ENGINE_SUPPORTED`（微信不顯示右鍵）——
        #   因為微信未認證號發不出客服消息，讓客服白按一次沒有意義。兩邊刻意不同。
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="wechat", scope="dm", text="你好"),
            sess=SUPER))
        check(getattr(r, "status_code", 200) == 200, "微信私訊是合法場景（後端不擋）")
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="line", scope="dm", text="x"), sess=SUPER))
        check(getattr(r, "status_code", 200) == 400, "不支援的平台 → 400")
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="wechat", scope="comment", text="x"),
            sess=SUPER))
        check(getattr(r, "status_code", 200) == 400, "微信＋留言 → 400（不支援的場景）")
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="fb", scope="dm", text="", conversation_key=""),
            sess=SUPER))
        check(getattr(r, "status_code", 200) == 400, "沒有內容 → 400")

        console_router.rbac.can_access_business = lambda sess, bid: int(bid) == 1
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=2, platform="fb", scope="dm", text="x"), sess=SUPER))
        check(getattr(r, "status_code", 200) == 403, "沒有該商家權限 → 403")
        console_router.rbac.can_access_business = lambda sess, bid: True

        print("\n[B] 私訊：對話上下文（正序 + 以右鍵那一則為界）")
        calls["answer"].clear()
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="fb", scope="dm",
                                   text="現在有貨嗎", message_id="m4",
                                   conversation_key="psid_1"), sess=SUPER))
        check(r.get("ok") is True, "回 ok")
        check(r["would_send"] is True and r["answer"] == "這是 FAQ 答案", "命中 → 帶回答案")
        check(r["history_used"] == 3, f"上下文＝m1/m2/m3 三則（實得 {r['history_used']}）")
        sent = calls["answer"][-1]
        check(sent["text"] == "現在有貨嗎", "問句＝被右鍵那一則的原文")
        check(sent["business_id"] == 1, "★ 帶 business_id（多租戶隔離）")
        hist = sent["history"]
        check([h["text"] for h in hist] == ["你好", "我要買", "稍等我看一下"],
              f"上下文按時間正序、且不含被右鍵那一則本身（實得 {[h['text'] for h in hist]}）")
        check(hist[0]["direction"] == "inbound" and hist[2]["direction"] == "outbound",
              "上下文保留方向（客服/客戶）")

        print("\n[C] 未命中才寫紀錄")
        calls["miss"].clear()
        asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="fb", scope="dm", text="我要買",
                                   message_id="m2", conversation_key="psid_1"), sess=SUPER))
        check(len(calls["miss"]) == 0, "命中 → 不寫 manual_no_match（等送出才記）")

        asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="fb", scope="dm", text="這個不行",
                                   message_id="m2", conversation_key="psid_1"), sess=SUPER))
        check(len(calls["miss"]) == 1, "未命中 → 寫一筆 manual_no_match")
        if calls["miss"]:
            biz, ch, sc, kw = calls["miss"][0]
            check(biz == 1 and ch == "fb" and sc == "dm", "紀錄帶 business_id / 平台 / 場景")
            check(kw.get("message_id") == "m2", "紀錄錨定被回覆的那一則")
            check("未命中" in (kw.get("detail") or ""), "紀錄帶未命中原因")

        print("\n[D] 留言場景")
        calls["answer"].clear()
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="ig", scope="comment",
                                   text="這款多少錢", comment_id="c1"), sess=SUPER))
        check(r.get("ok") is True, "留言也可以產生草稿")
        check(calls["answer"][-1]["scope"] == "comment", "scope=comment")
        check(not calls["answer"][-1]["history"], "留言不取私訊對話上下文（空 = 等價 None）")
        r = asyncio.run(console_router.draft_reply(
            console_router.DraftIn(business_id=1, platform="fb", scope="dm",
                                   text="現在有貨嗎", message_id="m4",
                                   conversation_key="psid_1"), sess=SUPER))
        check(calls["answer"][-1]["scope"] == "dm" and calls["answer"][-1]["history"],
              "對照：私訊會帶上下文")

        print("\n[E] 單條標記：read 帶 message_id 只標這一則")
        calls["mark"].clear()
        res = agent_router.mark_read(
            agent_router.ReadIn(business_id=1, platform="fb", bp_account_id=11,
                                key="psid_1", scope="dm", message_id="m4"), sess=SUPER)
        check(res.get("ok") is True, "回 ok")
        mk = calls["mark"][-1] if calls["mark"] else {}
        check(mk.get("message_id") == "m4", f"傳 message_id（實得 {mk.get('message_id')}）")
        check(not mk.get("end_user_sender_id"),
              "★ **沒有**傳 end_user_sender_id（不擴大到整個會話）")
        check(mk.get("handled") is True, "handled=true")

        calls["mark"].clear()
        agent_router.mark_read(
            agent_router.ReadIn(business_id=1, platform="fb", bp_account_id=11,
                                key="psid_1", scope="dm"), sess=SUPER)
        check(not calls["mark"][-1].get("message_id"), "沒帶 message_id → 整會話模式（按鈕用）")
        check(calls["mark"][-1].get("end_user_sender_id") == "psid_1", "整會話模式傳 end_user_sender_id")

        calls["mark"].clear()
        agent_router.mark_read(
            agent_router.ReadIn(business_id=1, platform="ig", bp_account_id=13,
                                key="c1", scope="comment"), sess=SUPER)
        check(calls["mark"][-1].get("comment_id") == "c1", "留言模式傳 comment_id")

        print("\n[F] 權限：純運維不能標記")
        res = agent_router.mark_read(
            agent_router.ReadIn(business_id=1, platform="fb", bp_account_id=11,
                                key="psid_1", scope="dm", message_id="m4"), sess=OPS)
        check(getattr(res, "status_code", 200) == 403, f"ops → 403（實得 {getattr(res,'status_code',200)}）")
    finally:
        reply_engine.answer = orig["answer"]
        console_router.reply_engine.answer = orig["answer"]
        console_router.autoreply_db.resolve_scope_set = orig["resolve"]
        connector_client.fetch_history = orig["history"]
        agent_router.connector_client.mark_message = orig["mark"]
        agent_router.audit.log_standalone = orig["audit"]
        console_router.rbac.can_access_business = orig["can_biz"]
        agent_router.rbac.can_access_business = orig["can_biz2"]
        agent_router.rbac.can_view_conversation = orig["can_view"]
        ar_mod.log_manual_miss = orig["miss"]

    print("\n" + "=" * 64)
    print(f"結果：PASS {passes} / FAIL {len(failures)}")
    if failures:
        for f in failures:
            print("   -", f)
        return 1
    print("全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(run())
