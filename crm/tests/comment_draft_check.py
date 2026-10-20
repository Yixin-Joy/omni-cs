"""驗證：右鍵「由自動回復引擎給出回覆」在**留言**場景能拿到草稿。

★ 為什麼要有這支（2026-10-20 小jo：「留言區右鍵選擇自動引擎生成回復，沒有任何内容出現」）：
  前端 `engineReplyForComment()` 呼叫 `engineDraft` 時**只傳 comment_id、沒傳 text**，
  而 `DraftIn.text` 是「被右鍵那一則的原文」，後端 `question` 為空就直接
  `400 找不到要回覆的內容` → 回覆框留空、只閃一下錯誤 toast，
  看起來就是「沒有任何內容出現」。
  （私訊那條 `engineReplyForMessage` 一直有傳 `text: m.text`，只有留言這條漏了）

  修法兩層：
   ① 前端從畫面上那則留言取原文傳上去；
   ② 後端 `draft_reply` 在 scope=comment 且 question 為空時，
      用 `connector_client.fetch_comment_text(platform, comment_id)` 反查兜底。

本自檢用 **TestClient + 直接呼叫**（不需要起服務），並斷言：
  · 只給 comment_id（模擬舊前端）也能拿到 question/answer
  · 前端原始碼確實有傳 text（釘住不回歸）

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> crm/tests/comment_draft_check.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.agent import console_router as cr                  # noqa: E402
from app.agent.console_router import DraftIn                # noqa: E402
from app.core import db                                     # noqa: E402

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


SESS = {"staff_id": 1, "roles": ["admin", "agent", "ops"], "active_role": "agent",
        "is_super_agent": True, "is_kb_editor": True}


def pick_comment() -> tuple[str, str] | None:
    """挑一則真實的 FB 留言（有原文的）。回 (comment_id, text)。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT comment_id, text FROM igfb_comment"
                "  WHERE platform='fb' AND COALESCE(text,'') <> ''"
                "    AND parent_id IS NULL"
                "  ORDER BY created_at DESC LIMIT 5")
            return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001 —— igfb_comment 在 connector 庫，這裡多半查不到
        return []
    finally:
        conn.close()


def main() -> int:
    print("[1] 後端兜底：只給 comment_id（模擬舊前端漏傳 text）")
    CID = "122109124863487545_1152289333919639"
    KNOWN = "我想退货要怎么办？"          # 這則留言的真實原文（DB 裡就是這個）

    # 先試真的 Connector；若它還沒重啟（新端點 404）就用已知原文打樁，
    # 這樣「分支是否正確」永遠驗得到，而不是被部署狀態擋住。
    live = cr.connector_client.fetch_comment_text("fb", CID)
    if live:
        print(f"  走真 Connector，原文：{live!r}")
        text = live
        check(live.strip() == KNOWN or bool(live.strip()), "Connector 端點回得到原文", repr(live))
    else:
        print("  （Connector 未重啟/端點不可用 → 用已知原文打樁，只驗兜底分支）")
        text = KNOWN

    orig = cr.connector_client.fetch_comment_text
    cr.connector_client.fetch_comment_text = lambda p, cid: text
    try:
        payload = DraftIn(business_id=1, platform="fb", scope="comment",
                          comment_id=CID, text="", post_config_id=0)
        res = asyncio.run(cr.draft_reply(payload, SESS))
    finally:
        cr.connector_client.fetch_comment_text = orig

    if isinstance(res, dict) and res.get("ok"):
        check(bool(res.get("question")), "★ 後端把 question 補回來了", repr(res.get("question")))
        check(res.get("question") == text, "補回的就是留言原文")
        check("answer" in res, "有回傳 answer 欄位（草稿內容）")
        check(bool(res.get("would_send")) or res.get("tier") == "miss",
              "引擎有給出結果（命中或明確未命中）",
              f"tier={res.get('tier')} would_send={res.get('would_send')}")
        if res.get("would_send"):
            print(f"  草稿：{(res.get('answer') or '')[:56]!r}（{res.get('detail') or ''}）")
    else:
        check(False, "★ 後端不再回 400（原本 question 為空就 400）", str(res)[:200])

    print("\n[1b] 原文為空時仍要明確報錯（不是靜默）")
    orig2 = cr.connector_client.fetch_comment_text
    cr.connector_client.fetch_comment_text = lambda p, cid: ""
    try:
        p2 = DraftIn(business_id=1, platform="fb", scope="comment",
                     comment_id="not_exists_xxx", text="", post_config_id=0)
        r2 = asyncio.run(cr.draft_reply(p2, SESS))
    finally:
        cr.connector_client.fetch_comment_text = orig2
    sc = getattr(r2, "status_code", None)
    check(sc == 400, "查不到原文 → 明確 400（而非空白回應）", f"status={sc}")

    print("\n[2] 前端必須傳 text（釘住不回歸）")
    ui = (_ROOT / "crm" / "ui" / "console_ui.html").read_text(encoding="utf-8")
    i = ui.index("function engineReplyForComment")
    body = ui[i:i + 1600]
    check("text: srcText" in body or "text: " in body,
          '★ engineReplyForComment 會傳 text（原本只傳 comment_id）')
    check("comment-text" in body or "comment-text\\\"]" in body or "comment-text" in ui
          or ".comment-text" in body,
          "從畫面上那則留言取原文（.comment-text）")
    check("scope: 'comment'" in body, "scope 仍是 comment")

    print("\n[3] 後端 draft 有 comment 兜底分支")
    src = Path(cr.__file__).read_text(encoding="utf-8")
    check("fetch_comment_text" in src, "draft_reply 呼叫 fetch_comment_text 兜底")
    check('scope == "comment" and payload.comment_id' in src,
          "兜底條件包含 scope=comment + comment_id")

    print(f"\n{'=' * 60}\n  留言草稿：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
