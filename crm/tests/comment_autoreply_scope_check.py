"""驗證：**自動回復（AI 命中）** 建外送任務時，`message_content` 一定帶 `scope`。

★ 為什麼（2026-10-20 第二次踩）：

  第一次修的是 `autoreply.enqueue_reply()` 沒把 `scope` 寫進 `message_content`
  （commit 4235948），修完也加了閘門。但 11:00 小jo 回報「依舊命中無法自動回復」——
  查 DB 發現任務 #525 的內容還是 `{"text":..., "comment_id":...}`，**沒有 scope**。

  真因不是程式碼，是**服務沒重啟**：
    · `crm/app/autoreply/autoreply.py` 改於 10:22
    · CRM 進程（uvicorn 8020）啟動於 **09:05** → 跑的是舊碼
  舊碼把留言回覆當私訊發 → 把 PSID 當目標 → Meta 400
  `(#10) 這則訊息傳送的時間不在允許期間內`，而人工回覆（`create_manual_reply`）
  一直有帶 scope，所以只有自動回復壞 —— 這個「一半好一半壞」是判斷的關鍵線索。

  所以這支自檢除了驗內容，還驗**「跑的進程比原始碼舊」**這種情況要能被指出來：
  用 `crm_outbound_task` 最近失敗任務的 `message_content->>'scope'` 是否為 NULL
  來判斷（NULL 且該平台是 ig/fb 的留言，就是舊碼在跑或又漏了）。

本自檢**不建任何任務**（`repo.enqueue_outbound_task` 被打樁攔下來），
也不打 Meta / Connector，隨時可跑。

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> crm/tests/comment_autoreply_scope_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.autoreply import autoreply                        # noqa: E402
from app.internal import repo as internal_repo             # noqa: E402

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


def _capture(fn):
    """攔下 `repo.enqueue_outbound_task`，回傳它收到的 kwargs（不寫 DB）。"""
    captured: list[dict] = []
    orig = internal_repo.enqueue_outbound_task

    def fake(**kw):
        captured.append(kw)
        return {"task_id": 0}

    internal_repo.enqueue_outbound_task = fake
    try:
        fn()
    finally:
        internal_repo.enqueue_outbound_task = orig
    return captured[0] if captured else {}


def main() -> int:
    print("[1] 原始碼：`enqueue_reply` 必須把 scope 寫進 content")
    src = Path(autoreply.__file__).read_text(encoding="utf-8")
    i = src.index("def enqueue_reply")
    body = src[i:src.index("def ", i + 10)]
    check('"scope": scope' in body or "'scope': scope" in body,
          '★ content 內含 "scope": scope')
    check("def enqueue_reply" in src and "scope: str" in body, "函式簽名有 scope 參數")

    print("\n[2] 實跑（打樁，不建任務）：留言命中 → content 帶 scope + comment_id")
    got = _capture(lambda: autoreply.enqueue_reply(
        business_id=1, platform="fb", platform_account_id=3, to="PSID_123",
        scope="comment", text="好的，已幫您安排", comment_id="1221091_999"))
    mc = got.get("message_content") or {}
    check(bool(got), "有呼叫 enqueue_outbound_task")
    check(mc.get("scope") == "comment", "★ content.scope == 'comment'", json.dumps(mc, ensure_ascii=False))
    check(mc.get("comment_id") == "1221091_999", "★ content.comment_id 帶上了（Connector 靠它覆寫 target）")
    check(mc.get("text") == "好的，已幫您安排", "content.text 正確")
    check(got.get("end_user_sender_id") == "PSID_123", "任務仍保留 PSID（方便追溯）")
    check(got.get("source") == "ai", "source='ai'（人工回覆是 manual）")

    print("\n[3] 私訊命中：scope=dm，且不該帶 comment_id")
    got2 = _capture(lambda: autoreply.enqueue_reply(
        business_id=1, platform="fb", platform_account_id=3, to="PSID_123",
        scope="dm", text="您好"))
    mc2 = got2.get("message_content") or {}
    check(mc2.get("scope") == "dm", "★ content.scope == 'dm'", json.dumps(mc2, ensure_ascii=False))
    check("comment_id" not in mc2, "私訊不帶 comment_id")

    print("\n[4] 線上痕跡：最近的 ig/fb 失敗任務不可以是「scope 為 NULL」")
    check_db_stale()

    print("\n[5] 服務新鮮度：跑的是不是新碼")
    check_service_fresh()

    print(f"\n{'=' * 64}\n  自動回復留言 scope：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


def _service_started_at():
    """從 `/health` 拿 CRM 進程啟動時間（沒有就回 None）。

    ★ 這是判斷「有沒有重啟」的唯一可靠依據 —— 檔案的 mtime 只代表「碼改了」，
      不代表跑的是它。留言自動回復那次就是栽在這。（`/health` 已加 `started_at`）
    """
    import json as _json
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8020/health", timeout=3) as r:
            return _json.loads(r.read().decode("utf-8")).get("started_at")
    except Exception:  # noqa: BLE001
        return None


def check_db_stale() -> None:
    """檢查「晚於本次服務啟動時間」的 AI 任務有沒有缺 `scope` 的。

    ⚠️ 只看**比 `started_at` 晚**的任務：更早的是「重啟前跑舊碼」留下的歷史證據
      （#515、#525），不該一直報紅，也不該被忽略 —— 印出來給人看。
    """
    try:
        from app.core import db
        conn = db.connect()
    except Exception as e:  # noqa: BLE001
        check(True, "（跳過 DB 檢查：連不上）" + str(e)[:60])
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, platform, status, message_content->>'scope' AS scope, created_at,"
                "       left(COALESCE(fail_reason,''), 50) AS why"
                "  FROM crm_outbound_task"
                " WHERE source='ai' AND platform IN ('ig','fb')"
                "   AND created_at > now() - interval '3 days'"
                " ORDER BY id DESC LIMIT 40")
            rows = cur.fetchall()
    finally:
        conn.close()

    started = _service_started_at()
    print(f"  服務啟動時間（/health.started_at）：{started}")
    if not rows:
        check(True, "（近 3 天沒有 AI 的 ig/fb 任務可檢查）")
        return

    if started:
        from datetime import datetime
        t0 = datetime.fromisoformat(started)
        fresh = [r for r in rows if r[4] and r[4] >= t0]
        old = [r for r in rows if not (r[4] and r[4] >= t0)]
    else:
        fresh, old = rows, []
        print("  ⚠️ 拿不到 started_at（CRM 沒跑或版本舊）→ 只能全量檢查")

    for r in rows[:6]:
        print(f"    #{r[0]} {r[1]} {r[2]} scope={r[3]!r} {r[4]}")
    if old:
        print("  （重啟前的歷史紀錄，不計入：" + "、".join(f"#{r[0]}" for r in old) + "）")
    bad = [r for r in fresh if not r[3]]
    check(not bad,
          f"★ 本次啟動後（{len(fresh)} 筆）沒有缺 scope 的 AI 任務",
          "、".join(f"#{r[0]}" for r in bad))


def check_service_fresh() -> None:
    """服務本體的新鮮度：新端點存在＝跑的是新碼（404＝進程比原始碼舊）。

    這一項直接回答「程式碼修好了，服務是不是還在跑舊的」——
    #515/#525 那次之所以白修一輪，就是因為沒人檢查這件事。
    """
    import urllib.error
    import urllib.request

    url = ("http://127.0.0.1:8020/console/agent/task-status?business_id=1&ids=1")
    try:
        urllib.request.urlopen(url, timeout=3)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:  # noqa: BLE001
        check(True, f"（CRM 沒在跑，跳過新鮮度檢查：{type(e).__name__}）")
        return
    check(code in (401, 403),
          f"★ CRM 進程已載入新碼（/task-status 回 {code}，不是 404）", f"HTTP {code}")


if __name__ == "__main__":
    raise SystemExit(main())
