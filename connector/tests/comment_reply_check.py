"""驗證：留言自動回復真的走「留言端點」、且任務內容一定帶 scope。

★ 為什麼要有這支（2026-10-20 小jo：「FB 推文留言自動回復命中不會回復，且記錄中沒有記錄」）：
  兩個獨立 bug：
  ① CRM `autoreply.enqueue_reply()` 建留言任務時**只寫 comment_id、沒寫 scope**
     → Connector `scope = mc.get("scope") or "dm"` 當成**私訊**發：
       · FB：把 comment_id 當 PSID → Meta 400 `(#10) 不在允許期間`
       · IG：真的發成一條 DM（客戶收不到公開回覆，`igfb_comment.replied` 仍 false）
     人工回覆（`agent/repo.create_manual_reply`）一直有帶 scope，所以只有自動回復壞。
  ② Connector `process_task()` 的 target 取 `end_user_sender_id`（自動回復那條是 PSID），
     即使 scope 對了也會 POST /{psid}/comments → 同樣失敗。

跑法（repo 根）：
  PYTHONPATH='<根>\\connector;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> connector/tests/comment_reply_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "connector"))

from app.channels import base                        # noqa: E402
from app.worker import outbound                      # noqa: E402

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


def test_build_sub_items() -> None:
    print("[1] 留言任務的 message_content 結構")
    mc = {"text": "您好", "scope": "comment", "comment_id": "P_123"}
    items = outbound.build_sub_items(mc)
    check(len(items) == 1 and items[0]["kind"] == "text", "拆成 1 個文字子項", str(items))
    check(mc.get("scope") == "comment", "帶 scope=comment")


def test_send_routing() -> None:
    """★★ 核心：scope=comment 一定要走 reply_comment（留言端點），不能走 send_text（私訊）。"""
    print("\n[2] 路由：scope=comment → reply_comment；scope=dm → send_text")
    called: list[tuple] = []

    async def fake_reply_comment(platform, comment_id, text, cred=None):
        called.append(("reply_comment", platform, comment_id, text))
        return base.SendResult(ok=True, platform_msg_id="mock-c")

    async def fake_send_text(platform, target, text, *a, **kw):
        called.append(("send_text", platform, target, text))
        return base.SendResult(ok=True, platform_msg_id="mock-d")

    orig_rc, orig_st = base.reply_comment, base.send_text
    base.reply_comment, base.send_text = fake_reply_comment, fake_send_text
    try:
        asyncio.run(outbound._send_one("fb", "COMMENT_1", {"kind": "text", "text": "hi"},
                                       {"page_access_token": "x"}, "comment"))
        check(called and called[0][0] == "reply_comment",
              "★ scope=comment 走留言端點", str(called[:1]))
        check(called and called[0][2] == "COMMENT_1", "留言端點收到 comment_id")
        called.clear()
        asyncio.run(outbound._send_one("fb", "PSID_1", {"kind": "text", "text": "hi"},
                                       {"page_access_token": "x"}, "dm"))
        check(called and called[0][0] == "send_text", "scope=dm 走私訊端點", str(called[:1]))
        called.clear()
        # 留言不支援附件
        r = asyncio.run(outbound._send_one("fb", "COMMENT_1",
                                           {"kind": "asset", "media_key": "x.png"},
                                           {"page_access_token": "x"}, "comment"))
        check(not r.ok and "附件" in (r.error or ""), "留言附件被拒（不支援）", r.error or "")
    finally:
        base.reply_comment, base.send_text = orig_rc, orig_st


def test_target_override() -> None:
    """★★ 核心：scope=comment 時 target 必須用 comment_id（不是 PSID）。"""
    print("\n[3] process_task 的 target 覆寫（scope=comment → comment_id）")
    seen: list[str] = []

    async def fake_send_one(platform, target, item, credential=None, scope="dm"):
        seen.append(target)
        return base.SendResult(ok=True, platform_msg_id="mock-1")

    orig = outbound._send_one
    outbound._send_one = fake_send_one
    try:
        task = {"task_id": 1, "platform": "fb", "end_user_sender_id": "PSID_999",
                "platform_business_account_id": "PAGE_1",
                "message_content": {"text": "hi", "scope": "comment",
                                    "comment_id": "COMMENT_777"}}
        # 打樁：不要真的寫 DB / 回調
        import app.worker.outbound as ob
        o_rec, o_sent, o_rep = ob.record_outbound_message, ob._sent_seqs, ob._report
        o_att = ob._record_attempt

        async def fake_report(*a, **kw):
            return None

        ob.record_outbound_message = lambda *a, **kw: None
        ob._sent_seqs = lambda tid: set()
        ob._record_attempt = lambda *a, **kw: None
        ob._report = fake_report
        try:
            asyncio.run(ob.process_task(task))
        finally:
            ob.record_outbound_message, ob._sent_seqs = o_rec, o_sent
            ob._record_attempt, ob._report = o_att, o_rep
        check(seen == ["COMMENT_777"], "★ 送到 comment_id 而不是 PSID", f"實際={seen}")
    finally:
        outbound._send_one = orig


def test_crm_enqueue_scope() -> None:
    """CRM 側：enqueue_reply 必須把 scope 寫進 message_content（原始 bug）。"""
    print("\n[4] CRM enqueue_reply 一定寫 scope")
    src_path = _ROOT / "crm" / "app" / "autoreply" / "autoreply.py"
    src = src_path.read_text(encoding="utf-8")
    # 找 enqueue_reply 函式體
    i = src.index("def enqueue_reply(")
    body = src[i:i + 1400]
    check('"scope": scope' in body or "'scope': scope" in body,
          '★ content 有 "scope": scope（少了它留言會被當私訊）')
    check('content["comment_id"] = comment_id' in body, "comment 仍會帶 comment_id")


def test_comment_text_endpoint() -> None:
    """新增的 /internal/api/comment-text（右鍵生成回覆缺 text 時的兜底）。"""
    print("\n[5] /internal/api/comment-text 端點")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core import config
    from app.internal import router as ir

    app = FastAPI()
    app.include_router(ir.router)
    H = {"X-Internal-Token": config.INTERNAL_API_TOKEN}
    with TestClient(app) as c:
        # 找一則真實留言來查
        from app.core import db
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT platform, comment_id FROM igfb_comment"
                            " WHERE COALESCE(text,'') <> '' ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            check(False, "DB 裡有一則可測的留言")
            return
        plat, cid = row
        r = c.get("/internal/api/comment-text",
                  params={"platform": plat, "comment_id": cid}, headers=H)
        check(r.status_code == 200, "查得到（200）", str(r.status_code))
        body = r.json() if r.status_code == 200 else {}
        check(bool((body or {}).get("text")), "回傳 text 原文", str((body or {}).get("text"))[:40])
        r2 = c.get("/internal/api/comment-text",
                   params={"platform": plat, "comment_id": "__nope__"}, headers=H)
        check(r2.status_code == 404, "查不到回 404", str(r2.status_code))
        r3 = c.get("/internal/api/comment-text",
                   params={"platform": "wechat", "comment_id": "x"}, headers=H)
        check(r3.status_code == 400, "非 ig/fb 回 400", str(r3.status_code))


def main() -> int:
    test_build_sub_items()
    test_send_routing()
    test_target_override()
    test_crm_enqueue_scope()
    test_comment_text_endpoint()
    print(f"\n{'=' * 60}\n  留言自動回復：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
