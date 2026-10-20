"""驗證：商家在該平台**沒有綁定帳號**時，頁面要說「本商戶暫無綁定的【X】賬號」，
        而不是含糊的「尚無訊息／尚無貼文」。

★ 為什麼要有這支（2026-10-20 小jo）：
  「對於商家名下沒有的賬號，點擊進入該平臺頁面後不能顯示尚無貼文/訊息，
    要顯示本商戶暫無綁定的【平臺名】賬號」

  舊行為：`renderConversations()` / `renderPosts()` 只要是空清單就寫死
  「尚無訊息」／「尚無貼文」—— 沒綁帳號跟「有帳號但真的還沒訊息」長得一模一樣，
  使用者只會覺得資料沒同步或系統壞了。

  判準：`/console/agent/overview` 的 `accounts`（後端 `repo.list_accounts(bid)`，
  已按商家隔離、且 `enabled IS NOT FALSE`）。三個必須守住的不變式：
    ① overview 還沒載到 → **不知道**，不可以畫成「沒有」（否則一打開就閃錯文案）；
    ② overview 是別家的（切換商家時的殘留）→ 也不可以用；
    ③ 有／沒有翻轉時要**重繪**（空清單的指紋沒變，不補這段就永遠停在錯文案）。

本自檢：
  [A] 前端原始碼接線（釘住不回歸）
  [B] 把 `boundAccountsOf/noAccountText/syncAccountGating/renderConversations/renderPosts`
      抽到 Node 實跑，斷言上面三條不變式與實際文案

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> crm/tests/account_gating_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

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


UI = (_ROOT / "crm" / "ui" / "console_ui.html").read_text(encoding="utf-8")


def body_of(name: str, size: int = 2200) -> str:
    i = UI.find("function " + name)
    return UI[i:i + size] if i >= 0 else ""


# ---------------------------------------------------------------- [A] 接線
def part_a() -> None:
    print("[A] 前端接線")

    check("function boundAccountsOf" in UI, "有 boundAccountsOf()（判斷該平台有無綁定帳號）")
    check("function noAccountText" in UI, "有 noAccountText()（產生文案）")
    check("function syncAccountGating" in UI, "有 syncAccountGating()（overview 到貨後補判斷）")
    check("本商戶暫無綁定的【" in UI and "】賬號" in UI,
          "★ 文案就是「本商戶暫無綁定的【X】賬號」")

    ba = body_of("boundAccountsOf", 1200)
    check("Array.isArray(o.accounts)" in ba and "return null" in ba,
          "★ overview 沒載到 → 回 null（不確定，不當成「沒有」）")
    check("o.business_id" in ba and "Number(o.business_id) !== Number(state.bizId)" in ba,
          "★ 驗 business_id：切換商家時殘留的 overview 不能用")
    check("o.accounts.filter" in ba, "按 platform 過濾")

    rc = body_of("renderConversations", 2600)
    check("noAccountText(state.platform)" in rc, "★ 私訊列表空清單會先問有沒有綁定帳號")
    check("esc(txt)" in rc or "esc(na ||" in rc, "文案有轉義")
    check("(noAccountText(state.platform) ? 'na' : '')" in rc,
          "★ 列表指紋含「有沒有帳號」→ 翻轉時才會重繪")

    rp = body_of("renderPosts", 1800)
    check("noAccountText(state.platform)" in rp, "★ 貼文列表空清單也會先問")
    check("esc(na ||" in rp, "文案有轉義")
    check("(noAccountText(state.platform) ? 'na' : '')" in rp,
          "★ 貼文列表指紋同樣含該旗標")

    rch = body_of("resetChat", 900)
    check("noAccountText(state.platform)" in rch, "右側空白區也講同一句話（不重複解釋）")

    la = body_of("loadAutoSettings", 700)
    check("syncAccountGating()" in la, "★ loadAutoSettings（切平台/切欄目）會補判斷")
    lab = body_of("loadAutoSettingsAnd", 800)
    check("syncAccountGating()" in lab, "★ boot 時的 loadAutoSettingsAnd 也會")

    sg = body_of("syncAccountGating", 900)
    check("flag === state.accGate" in sg and "return" in sg,
          "狀態沒翻轉就不重繪（避免換掉節點打斷點擊）")
    check("state.convsSig = null" in sg and "state.postsSig = null" in sg,
          "翻轉時清指紋，強制重畫空清單")

    # 空清單的文案不可以再寫死
    check("(state.search ? '沒有符合的用戶' : '尚無訊息')</div>'" not in UI,
          "舊的『尚無訊息』直出已移除")
    check("(state.search ? '沒有符合的貼文' : '尚無貼文')</div>'" not in UI,
          "舊的『尚無貼文』直出已移除")


# ---------------------------------------------------------------- [B] 真跑
def part_b() -> None:
    print("\n[B] 抽取函式在 Node 裡實跑")

    def cut(start: str, end: str) -> str:
        i = UI.index(start)
        j = UI.index(end, i)
        return UI[i:j]

    code = (cut("function boundAccountsOf", "function renderTabs") + "\n"
            + cut("function renderConversations", "function openConversation") + "\n"
            + cut("function renderPosts", "function loadMorePosts"))

    harness = r"""
var PLATFORM_LABEL = { wechat: '微信', whatsapp: 'WhatsApp', ig: 'Instagram', fb: 'Facebook' };
var state = {
  bizId: 1, platform: 'fb', section: 'dm', search: '', activeKey: null, activePost: null,
  convs: [], posts: [], postsHasMore: false,
  convsSig: null, postsSig: null, accGate: null,
  overview: null
};
var NODES = {};
function $(id) { if (!NODES[id]) NODES[id] = { innerHTML: '', style: {} }; return NODES[id]; }
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtTime(t) { return 'TS'; }
function avatarChar(n) { return 'A'; }
function mdPreview(t, n) { return String(t || '').slice(0, n); }
function _convsSig(items) {
  return (items || []).map(function (c) { return c.key + ':' + c.pending; }).join('|');
}
function renderBadges() {}
// 帳號下拉模組在文件層掛了「點空白收起」的監聽；這裡只需要它不要炸。
var document = { addEventListener: function () {} };
""" + code + r"""
// 計數器：包住兩支 render，用來驗「syncAccountGating 只在翻轉時重繪」
var nRC = 0, nRP = 0;
var _rc0 = renderConversations, _rp0 = renderPosts;
renderConversations = function () { nRC++; return _rc0.apply(null, arguments); };
renderPosts = function () { nRP++; return _rp0.apply(null, arguments); };

var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }
function reset() {
  state.convs = []; state.posts = []; state.convsSig = null; state.postsSig = null;
  state.accGate = null; state.platform = 'fb'; state.bizId = 1; state.search = '';
  NODES['convList'] = { innerHTML: '' };
  nRC = 0; nRP = 0;
}

// ---- 1) overview 沒載到 = 不知道 → 不可以說「沒有帳號」
reset(); state.overview = null;
t(boundAccountsOf('fb') === null, '★ overview 未載入 → boundAccountsOf 回 null');
t(noAccountText('fb') === '', '★ overview 未載入 → 不出現「暫無綁定」文案');
renderConversations();
t(NODES['convList'].innerHTML.indexOf('尚無訊息') >= 0,
  '未載入 → 空清單仍是「尚無訊息」（不會閃錯文案）',
  NODES['convList'].innerHTML);

// ---- 2) overview 是別家商家的（切換商家殘留）
reset(); state.overview = { business_id: 999, accounts: [] };
t(boundAccountsOf('fb') === null, '★ 別的商家的 overview → 視為不知道（回 null）');
t(noAccountText('fb') === '', '別家 overview → 不出文案');

// ---- 3) 有綁定這個平台 → 正常「尚無訊息」
reset(); state.overview = { business_id: 1, accounts: [
  { id: 3, platform: 'fb', display_name: '粉專 A' }] };
t(boundAccountsOf('fb').length === 1, '有綁定 → 回 1 筆');
t(noAccountText('fb') === '', '有綁定 → 沒有「暫無綁定」文案');
renderConversations();
t(NODES['convList'].innerHTML.indexOf('尚無訊息') >= 0, '有綁定但沒訊息 → 「尚無訊息」');
t(NODES['convList'].innerHTML.indexOf('暫無綁定') < 0, '★ 有綁定時絕不出現「暫無綁定」');

// ---- 4) 該平台沒綁定 → 「本商戶暫無綁定的【Facebook】賬號」
reset(); state.overview = { business_id: 1, accounts: [
  { id: 7, platform: 'ig', display_name: 'IG A' }] };     // 只有 IG，沒有 FB
t(noAccountText('fb') === '本商戶暫無綁定的【Facebook】賬號',
  '★ 文案＝本商戶暫無綁定的【Facebook】賬號', noAccountText('fb'));
t(noAccountText('ig') === '', '同一個商家：有綁定的平台不出文案');
renderConversations();
t(NODES['convList'].innerHTML.indexOf('本商戶暫無綁定的【Facebook】賬號') >= 0,
  '★ 私訊頁：空清單顯示「暫無綁定」而不是「尚無訊息」',
  NODES['convList'].innerHTML);
t(NODES['convList'].innerHTML.indexOf('尚無訊息') < 0, '私訊頁：不再出現「尚無訊息」');

// ---- 5) 貼文頁同樣行為（平台名換成 Instagram）
reset(); state.platform = 'ig'; state.section = 'posts';
state.overview = { business_id: 1, accounts: [{ id: 3, platform: 'fb' }] };
renderPosts();
t(NODES['convList'].innerHTML.indexOf('本商戶暫無綁定的【Instagram】賬號') >= 0,
  '★ 貼文頁：顯示「暫無綁定的【Instagram】賬號」', NODES['convList'].innerHTML);
t(NODES['convList'].innerHTML.indexOf('尚無貼文') < 0, '貼文頁：不再出現「尚無貼文」');

// ---- 6) 微信（DM-only 平台）文案用中文標籤
reset(); state.platform = 'wechat';
state.overview = { business_id: 1, accounts: [] };
t(noAccountText('wechat') === '本商戶暫無綁定的【微信】賬號',
  '微信 → 「本商戶暫無綁定的【微信】賬號」', noAccountText('wechat'));

// ---- 7) overview 晚到：先畫「尚無訊息」，到貨後自動翻成「暫無綁定」
reset(); state.overview = null;
renderConversations();
t(NODES['convList'].innerHTML.indexOf('尚無訊息') >= 0, '先到貨的空清單 → 尚無訊息');
state.overview = { business_id: 1, accounts: [] };        // overview 現在才回來
syncAccountGating();
t(NODES['convList'].innerHTML.indexOf('暫無綁定') >= 0,
  '★ overview 到貨後自動翻成「暫無綁定」（不用 F5）', NODES['convList'].innerHTML);

// ---- 8) syncAccountGating 只在翻轉時重繪
reset(); state.overview = { business_id: 1, accounts: [] };
state.section = 'dm';
syncAccountGating();
var first = nRC;
syncAccountGating();       // 狀態一樣
syncAccountGating();
t(first >= 1, '第一次（不知道 → 沒有帳號）會重繪一次', String(first));
t(nRC === first, '★ 狀態沒翻轉就不再重繪（不打斷點擊）', nRC + '/' + first);

// ---- 9) 帳號被移除 → 已經畫著「尚無訊息」也要翻過來（指紋含旗標）
reset(); state.section = 'dm';
state.overview = { business_id: 1, accounts: [{ id: 3, platform: 'fb' }] };
renderConversations();
t(NODES['convList'].innerHTML.indexOf('尚無訊息') >= 0, '前置：有帳號 → 尚無訊息');
state.overview = { business_id: 1, accounts: [] };        // 帳號被解綁
syncAccountGating();
t(NODES['convList'].innerHTML.indexOf('暫無綁定') >= 0,
  '★ 帳號解綁後重繪成「暫無綁定」（指紋含 na 旗標）', NODES['convList'].innerHTML);

// ---- 10) 貼文頁的 syncAccountGating 走 renderPosts
reset(); state.section = 'posts'; state.platform = 'fb';
state.overview = { business_id: 1, accounts: [] };
syncAccountGating();
t(nRP >= 1, '貼文欄目下 syncAccountGating 呼叫 renderPosts', String(nRP));
t(NODES['convList'].innerHTML.indexOf('本商戶暫無綁定的【Facebook】賬號') >= 0,
  '貼文欄目：同步後文案正確', NODES['convList'].innerHTML);

// ---- 11) 切換商家：accGate 旗標含 bizId → 會重算
reset(); state.overview = { business_id: 1, accounts: [] };
syncAccountGating();
var gate1 = state.accGate;
state.bizId = 2;
state.overview = { business_id: 2, accounts: [{ id: 9, platform: 'fb' }] };
syncAccountGating();
t(state.accGate !== gate1, '★ 換商家 → 旗標變（一定要重算）', gate1 + ' → ' + state.accGate);
t(noAccountText('fb') === '', '換到有綁定的商家 → 不出「暫無綁定」');

var bad = out.filter(function (x) { return !x[0]; });
out.forEach(function (x) {
  console.log((x[0] ? '  PASS  ' : '  FAIL  ') + x[1] + (x[0] ? '' : '   ' + x[2]));
});
console.log('RESULT ' + JSON.stringify({ pass: out.length - bad.length, fail: bad.length }));
"""

    node = _ROOT / ".tmp"
    node.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".js", dir=str(node))
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
            check(m["fail"] == 0, f"B 段斷言全過（{m['pass']} 項）", f"fail={m['fail']}")
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
    print(f"\n{'=' * 62}\n  無綁定帳號文案：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
