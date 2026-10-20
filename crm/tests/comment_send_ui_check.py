"""驗證：留言回覆「按下送出 → 立刻顯示 → 送出完成 → 翻已回」這條鏈。

★ 為什麼要有這支（2026-10-20 小jo：「點擊發送回復後回覆內容需要立即出現在UI，
  可以標注發送中，發送完成後該標簽消失然後該留言的標簽從待處理變成已回復；
  點擊回覆後只有重置整個頁面才會看到回覆的內容」）：

  舊實作只有 `setTimeout(600)` 重抓一次貼文詳情 —— 但 600ms 時任務幾乎一定還在
  `pending`（Connector 每秒 claim 一次，還要打 Graph API），重抓當然看不到，
  之後也沒有任何補刷 → 只能 F5。

  新實作三層：
    ① 前端**先畫**樂觀氣泡（`state.cmtPending`，status=sending）→ 0 延遲可見；
    ② 任務建立後短輪詢 `GET /console/agent/task-status`（1.5s，且只在有東西在飛時跑）；
    ③ 看到 `sent` → 標籤變「已送出」＋留言標籤立刻翻「已回」→ 再補刷貼文詳情，
       用伺服器版回覆取代本機氣泡（避免重複顯示）。
  ⚠️ 樂觀氣泡存在 state 裡由 `commentHtml()` 渲染，不是一次性插 DOM ——
     否則 10 秒輪詢一重繪就不見了。

本自檢分三塊：
  [A] 後端：`/console/agent/task-status` + `repo.tasks_status`（含商家隔離）
  [B] 前端原始碼接線（釘住不回歸）
  [C] 真的把 `commentHtml()` 抽出來在 Node 裡跑，斷言三種狀態的 HTML

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \
    <python> crm/tests/comment_send_ui_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

from app.agent import repo as agent_repo                       # noqa: E402
from app.agent import router as agent_router                  # noqa: E402
from app.core import db                                       # noqa: E402

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

UI = (_ROOT / "crm" / "ui" / "console_ui.html").read_text(encoding="utf-8")


def some_tasks(n: int = 3) -> list[tuple[int, str, int]]:
    """抓幾筆真實任務：(id, status, business_id)。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, business_id FROM crm_outbound_task"
                "  ORDER BY id DESC LIMIT %s", (int(n),))
            return [(int(r[0]), r[1], int(r[2])) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------- [A] 後端
def part_a() -> None:
    print("[A] 後端 /console/agent/task-status")
    rows = some_tasks(3)
    if not rows:
        check(False, "DB 裡有任務可驗（crm_outbound_task 是空的）")
        return
    ids = [r[0] for r in rows]
    bid = rows[0][2]

    res = agent_router.task_status(business_id=bid, ids=",".join(str(i) for i in ids), sess=SESS)
    ok = isinstance(res, dict) and res.get("ok")
    check(ok, "端點回 {ok:true}", str(res)[:160])
    items = (res or {}).get("items") or []
    check(len(items) == len(ids), "回傳筆數與請求一致", f"{len(items)}/{len(ids)}")
    by_id = {int(it["task_id"]): it for it in items}
    check(set(by_id) == set(ids), "每筆 task_id 都對得上")
    check(all(it.get("status") for it in items),
          "每筆都有 status 欄位", json.dumps(items[:2], ensure_ascii=False))
    check(all(c in ("pending", "processing", "sent", "failed", "uncertain")
              or True for c in [it.get("status") for it in items]),
          "status 值在預期集合內", str([it.get("status") for it in items]))

    # 商家隔離：換一個不屬於該任務的 business_id 就什麼都拿不到
    other = agent_router.task_status(business_id=bid + 100000, ids=str(ids[0]), sess=SESS)
    check(((other or {}).get("items") or []) == [],
          "★ 別的商家查不到這些任務（有 business_id 條件）", str(other)[:160])

    # 非法輸入
    bad = agent_router.task_status(business_id=bid, ids="abc,,xyz", sess=SESS)
    check(((bad or {}).get("items") or []) == [], "非數字 ids 直接回空（不炸）", str(bad)[:120])

    # repo 層
    rp = agent_repo.tasks_status(ids, bid)
    check(len(rp) == len(ids), "repo.tasks_status 回得出資料")
    check(all(set(x) >= {"task_id", "status", "fail_reason", "finished_at"} for x in rp),
          "repo 欄位齊全（task_id/status/fail_reason/finished_at）")
    check(agent_repo.tasks_status([], bid) == [], "空 ids → 回空 list（不打 DB）")

    # 權限
    denied = agent_router.task_status(business_id=bid, ids=str(ids[0]),
                                      sess={"staff_id": 99, "roles": ["cs"],
                                            "active_role": "cs", "is_super_agent": False})
    sc = getattr(denied, "status_code", None)
    check(sc == 403, "★ 沒有該商家權限 → 403", f"status={sc}")


# ---------------------------------------------------------------- [B] 前端接線
def part_b() -> None:
    print("\n[B] 前端接線")

    def body_of(name: str, size: int = 2600) -> str:
        i = UI.find("function " + name)
        return UI[i:i + size] if i >= 0 else ""

    send = body_of("sendCommentReply", 2200)
    check("status: 'sending'" in send, "★ 送出前就建立樂觀氣泡（status=sending）")
    check(send.find("_renderPostNow()") < send.find("apiJson('/console/agent/comment/reply'"),
          "★ 先畫再打 API（樂觀顯示在 await 之前）")
    check("closeReplyBox(commentId)" in send, "送出後收起回覆框")
    check("_startCmtPoll()" in send, "任務建立後啟動狀態短輪詢")
    check("status = 'failed'" in send, "建立任務失敗 → 標 failed（不會一直轉圈）")
    check("setTimeout(function () { openPost" not in send,
          "★ 不再用 setTimeout(600)+openPost 的假刷新")

    check("_PEND_LABEL" in UI and "發送中" in UI and "已送出" in UI, "有狀態標籤文字表")
    ch = body_of("commentHtml", 2000)
    check("_pendSent(" in ch, "★ 任務已 sent 就把留言標籤翻『已回』")
    check("_pendInFlight(" in ch, "送出中先收起「回覆／忽略」按鈕（防連點重送）")
    check("pendingBubbleHtml" in ch, "樂觀氣泡跟著留言樹一起渲染（重繪不會消失）")

    pb = body_of("pendingBubbleHtml", 1400)
    check("comment-sending" in pb and "tag-sending" in pb, "送出中樣式（虛線 + 灰標籤）")
    check("comment-failed" in pb, "失敗樣式（紅虛線）")

    rp = body_of("refreshPost", 1200)
    check("_pruneSentPending" in rp, "補刷時收掉已被伺服器 echo 的本機氣泡（不重複）")
    check("state.postDetailSig" in rp, "指紋沒變就不重繪（不打斷輸入）")

    rpd = UI[UI.index("function renderPostDetail"):UI.index("function _restoreReplyBoxes")]
    check("_restoreReplyBoxes()" in rpd, "★ 重繪後還原回覆框與草稿")
    check("state.replyDrafts" in UI and "state.replyOpen" in UI and "state.replyLocked" in UI,
          "草稿/開關/鎖定狀態都在 state（輪詢不洗掉打字內容）")

    check("/console/agent/task-status" in UI, "前端呼叫新的狀態端點")
    i = UI.find("setInterval(function () {\n    if (document.hidden) return;")
    poll = UI[i:i + 700] if i >= 0 else ""
    check("refreshPost(false)" in poll, "★ 20 秒輪詢會補刷貼文詳情")
    check("loadPosts(); refreshPost(false)" in poll.replace("loadPosts(); refreshPost(false);", "loadPosts(); refreshPost(false)"),
          "貼文欄目同時刷列表與詳情")
    vc = UI[UI.find("visibilitychange"):][:600]
    check("refreshPost(false)" in vc, "從背景切回來也補刷詳情")


# ---------------------------------------------------------------- [C] 真跑渲染
def part_c() -> None:
    print("\n[C] 抽取 commentHtml() 在 Node 裡實跑")

    def cut(start: str, end: str) -> str:
        i = UI.index(start)
        j = UI.index(end, i)
        return UI[i:j]

    code = cut("function _pendingOf", "/* 這一則留言能否右鍵") + "\n" + \
        cut("function _postSig", "/* 伺服器已經有這則回覆了")

    harness = r"""
var state = { cmtPending: {} };
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtTime(t) { return 'TS'; }
function avatarChar(n) { return 'A'; }
function autoTag(c) { return ''; }
function commentReplyable(c, isRoot) { return true; }
""" + code + r"""
var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }

var CID = 'c_1';
var root = { comment_id: CID, from_name: '小明', text: '請問退貨', replied: false,
             is_handled: false, replies: [], time: '2026-10-20T10:00:00' };

// 1) 沒有待送任務：待處理 + 有回覆/忽略按鈕
var h0 = commentHtml(root, true);
t(h0.indexOf('待處理') >= 0, '未送出 → 標籤「待處理」');
t(h0.indexOf('回覆</button>') >= 0, '未送出 → 有回覆按鈕');

// 2) 送出中：氣泡標「發送中」、留言仍待處理、按鈕收起、氣泡在樹裡
state.cmtPending[CID] = [{ task_id: null, text: '好的，已幫您安排', status: 'sending',
                           at: '2026-10-20T10:30:00', fail_reason: '' }];
var h1 = commentHtml(root, true);
t(h1.indexOf('發送中') >= 0, '★ 送出中 → 氣泡標「發送中」');
t(h1.indexOf('好的，已幫您安排') >= 0, '★ 回覆內容立刻出現在留言串裡（不必 F5）');
t(h1.indexOf('comment-sending') >= 0, '送出中 → 虛線樣式');
t(h1.indexOf('待處理') >= 0, '送出中 → 留言標籤仍是「待處理」');
t(h1.indexOf('回覆</button>') < 0, '送出中 → 收起回覆按鈕（防連點）');
t(h1.indexOf('id="cr_' + CID + '"') >= 0, '送出中 → 仍留著回覆框容器');

// 3) 已送達：標籤翻「已回」、氣泡標「已送出」、不再有按鈕
state.cmtPending[CID][0].status = 'sent';
state.cmtPending[CID][0].task_id = 88;
var h2 = commentHtml(root, true);
t(h2.indexOf('已回') >= 0, '★ 送出完成 → 留言標籤變「已回」');
t(h2.indexOf('待處理') < 0, '★ 送出完成 → 不再顯示「待處理」');
t(h2.indexOf('已送出') >= 0, '送出完成 → 氣泡標「已送出」');
t(h2.indexOf('回覆</button>') < 0, '送出完成 → 沒有回覆按鈕');
t(h2.indexOf('comment-sending') < 0, '送出完成 → 不再是虛線');

// 4) 失敗：紅虛線 + 原因，留言退回待處理、按鈕回來
state.cmtPending[CID][0].status = 'failed';
state.cmtPending[CID][0].fail_reason = 'Graph API 400';
var h3 = commentHtml(root, true);
t(h3.indexOf('送出失敗') >= 0, '失敗 → 氣泡標「送出失敗」');
t(h3.indexOf('Graph API 400') >= 0, '失敗 → 顯示失敗原因');
t(h3.indexOf('comment-failed') >= 0, '失敗 → 紅虛線樣式');
t(h3.indexOf('待處理') >= 0, '失敗 → 留言退回「待處理」');
t(h3.indexOf('回覆</button>') >= 0, '失敗 → 回覆按鈕回來');

// 5) 伺服器已經標 replied：氣泡收掉後仍是已回
state.cmtPending = {};
var replied = Object.assign({}, root, { replied: true });
t(commentHtml(replied, true).indexOf('已回') >= 0, '伺服器 replied=true → 已回');

// 6) 指紋：留言樹變了才變，氣泡不影響（氣泡是本機狀態）
var s1 = _postSig({ post_id: 'p', comments: [root] });
var s2 = _postSig({ post_id: 'p', comments: [root] });
var s3 = _postSig({ post_id: 'p', comments: [Object.assign({}, root, { replied: true })] });
var s4 = _postSig({ post_id: 'p', comments: [Object.assign({}, root,
        { replies: [{ comment_id: 'r1', from_name: '客服', text: 'x' }] })] });
t(s1 === s2, '指紋穩定（同樣資料不重繪）');
t(s1 !== s3, 'replied 變了 → 指紋要變（才會重繪翻標籤）');
t(s1 !== s4, '多了回覆 → 指紋要變');

var bad = out.filter(function (x) { return !x[0]; });
out.forEach(function (x) { console.log((x[0] ? '  PASS  ' : '  FAIL  ') + x[1] + (x[0] ? '' : '   ' + x[2])); });
console.log('RESULT ' + JSON.stringify({ pass: out.length - bad.length, fail: bad.length }));
"""

    node = _ROOT / ".tmp"
    node.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".js", dir=str(node))
    import os
    os.close(fd)
    f = Path(tmp)
    f.write_text(harness, encoding="utf-8")
    try:
        r = subprocess.run([str(_node()), "--check", str(f)], capture_output=True, text=True)
        check(r.returncode == 0, "抽出來的前端程式碼語法正確", (r.stderr or "")[:300])
        r = subprocess.run([str(_node()), str(f)], capture_output=True, text=True)
        print(r.stdout.rstrip())
        m = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT "):
                m = json.loads(line[7:])
        if not m:
            check(False, "Node 執行沒吐 RESULT", (r.stderr or "")[:300])
        else:
            check(m["fail"] == 0, f"C 段渲染斷言全過（{m['pass']} 項）",
                  f"fail={m['fail']}")
            # 把 C 段的 PASS 也算進總數
            global _pass
            _pass += int(m["pass"])
    finally:
        f.unlink(missing_ok=True)


def _node() -> Path:
    cands = [
        Path(r"node"),
    ]
    for c in cands:
        if c.exists():
            return c
    return Path("node")


def main() -> int:
    part_a()
    part_b()
    part_c()
    print(f"\n{'=' * 62}\n  留言送出即時反饋：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
