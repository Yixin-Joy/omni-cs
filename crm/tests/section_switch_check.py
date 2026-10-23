"""驗證：一級導覽「私訊 ↔ 貼文留言」切換時，左側列表**一定要換**。

★ 為什麼要有這支（2026-10-20 小jo）：
  「我再點擊私訊/facebook 後點擊貼文留言/facebook，訊息欄仍然停留在私訊/facebook，
    只有先點了貼文留言下其他 tag 再切換回來才會正常顯示」

根因：`renderConversations()` 與 `renderPosts()` **共用同一個容器 `#convList`**，
  但各自記「已畫過的指紋」（`convsSig` / `postsSig`），而指紋都**不含欄目**。
  於是「私訊/FB → 貼文留言/FB」（平台沒變）時：
    · 貼文指紋仍等於上次看貼文時的值 → `renderPosts()` 直接 `return`；
    · `#convList` 裡其實還掛著私訊列表 → 畫面像「切了沒反應」。
  點別的 tag（platform 變了）才會讓指紋變化 → 才「正常」。這解釋了為什麼時好時壞。

修法（兩層，缺一不可）：
  ① `switchSection` / `switchPlatform` 把兩個指紋與 owner 一起作廢；
  ② `_claimList(owner)`：每次寫入前認領容器，換手就強制重繪
     —— 防以後有人加第三種列表又踩同一個坑。

本自檢：
  [A] 接線（兩個 render 都認領、兩支切換都清、直接寫容器的地方也清）
  [B] 把 renderConversations / renderPosts / _claimList 抽到 Node，
      **完整模擬「私訊/FB → 貼文留言/FB」**，斷言列表真的換了

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> crm/tests/section_switch_check.py
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


def body_of(name: str, size: int = 2400) -> str:
    i = UI.find("function " + name)
    return UI[i:i + size] if i >= 0 else ""


# ---------------------------------------------------------------- [A] 接線
def part_a() -> None:
    print("[A] 前端接線")

    check("function _claimList" in UI, "★ 有 _claimList()（容器擁有者）")
    check("listOwner: ''" in UI, "state 有 listOwner")

    cl = body_of("_claimList", 500)
    check("state.listOwner === owner" in cl and "return false" in cl,
          "同一擁有者 → 指紋可信任")
    check("state.listOwner = owner" in cl and "return true" in cl,
          "★ 換手 → 回 true（強制重繪）")

    rc = body_of("renderConversations", 2600)
    check("_claimList('dm')" in rc, "★ renderConversations 認領 dm")
    check("!fresh && sig === state.convsSig" in rc,
          "★ 只有「本來就是我畫的」才允許因指紋相同而跳過")

    rp = body_of("renderPosts", 2400)
    check("_claimList('posts')" in rp, "★ renderPosts 認領 posts")
    check("!fresh && sig === state.postsSig" in rp, "★ 同上")

    ss = body_of("switchSection", 900)
    check("state.convsSig = null" in ss and "state.postsSig = null" in ss,
          "★ switchSection 把兩個指紋都作廢")
    check("state.listOwner = ''" in ss, "★ switchSection 也清 owner")

    sp = body_of("switchPlatform", 800)
    check("state.convsSig = null" in sp and "state.postsSig = null" in sp,
          "★ switchPlatform 也作廢（同平台重按 tab 也要重畫）")
    check("state.listOwner = ''" in sp, "★ switchPlatform 也清 owner")

    # 直接寫容器的第三處（沒有商家）
    i = UI.find("沒有可操作的商家")
    around = UI[max(0, i - 200):i + 400]
    check("state.listOwner = ''" in around,
          "★ 直接寫 #convList 的地方也要清（否則換商家後永不重繪）")


# ---------------------------------------------------------------- [B] 真跑
def part_b() -> None:
    print("\n[B] 抽取函式在 Node 裡實跑（模擬「私訊/FB → 貼文留言/FB」）")

    def cut(start: str, end: str) -> str:
        i = UI.index(start)
        j = UI.index(end, i)
        return UI[i:j]

    code = (cut("function _claimList", "function _convsSig")
            + cut("function _convsSig", "function renderConversations")
            + cut("function renderConversations", "/* `bpId` 一定要帶")
            + cut("function renderPosts", "function loadMorePosts"))

    harness = r"""
var PLATFORM_LABEL = { wechat: '微信', whatsapp: 'WhatsApp', ig: 'Instagram', fb: 'Facebook' };
var NODES = {};
function $(id) { if (!NODES[id]) NODES[id] = { innerHTML: '', style: {} }; return NODES[id]; }
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtTime() { return 'TS'; }
function avatarChar() { return 'A'; }
function mdPreview(t, n) { return String(t || '').slice(0, n); }
function renderBadges() {}
function renderAccountPicker() {}
function accLabel(a) { return a.display_name || a.id; }
function accCountOf() { return 0; }
function accTotalOf() { return 0; }

var state = {
  bizId: 1, platform: 'fb', section: 'dm', scope: 'dm', search: '',
  bpAccount: 0, accPickOpen: false, accPickSig: '',
  convs: [], posts: [], convsSig: null, postsSig: null, listOwner: '',
  activeKey: null, activeAcc: null, activePost: null,
  overview: { business_id: 1, accounts: [
    { id: 3, platform: 'fb', display_name: '粉專 A' },
    { id: 4, platform: 'fb', display_name: '粉專 B' }] }
};
function boundAccountsOf(p) {
  var o = state.overview;
  if (!o || !Array.isArray(o.accounts)) return null;
  return o.accounts.filter(function (a) { return String(a.platform || '') === String(p); });
}
function noAccountText() { return ''; }
function currentAccount() { return null; }
""" + code + r"""
var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }

function reset() {
  state.section = 'dm'; state.platform = 'fb'; state.scope = 'dm'; state.search = '';
  state.bpAccount = 0; state.activeKey = null; state.activeAcc = null; state.activePost = null;
  state.convs = [{ key: 'dm-customer-1', sender_name: '客人甲', pending: 1,
                   last_ts: '1', direction: 'inbound', text: '私訊內容', bp_account_id: 3 }];
  state.posts = [{ post_id: 'p1', name: '貼文一', new_comments: 2, comment_count: 5,
                   created_at: '2', bp_account_id: 3 }];
  NODES['convList'] = { innerHTML: '', style: {} };
  NODES['chatHead'] = { innerHTML: '', style: {} };
}

// ★ 模擬 switchSection：把指紋與 owner 一起作廢，然後畫新的那一邊
function fakeSwitchSection(section) {
  state.section = section;
  state.scope = section === 'posts' ? 'comment' : 'dm';
  state.bpAccount = 0;
  state.convsSig = null; state.postsSig = null; state.listOwner = '';
  if (section === 'posts') renderPosts(); else renderConversations();
}
// ★ 模擬 switchPlatform
function fakeSwitchPlatform(p) {
  state.platform = p;
  state.bpAccount = 0;
  state.convsSig = null; state.postsSig = null; state.listOwner = '';
  if (state.section === 'posts') renderPosts(); else renderConversations();
}

// ---- 1) 先看私訊（列表＝私訊）
reset();
renderConversations();
var h = NODES['convList'].innerHTML;
t(h.indexOf('客人甲') >= 0, '前置：私訊列表畫出來了', h);
t(state.listOwner === 'dm', '容器擁有者＝dm', state.listOwner);

// ---- 2) 切到貼文留言（平台沒變）→ 列表一定要換成貼文
fakeSwitchSection('posts');
h = NODES['convList'].innerHTML;
t(h.indexOf('貼文一') >= 0, '★ 切到貼文留言 → 列表換成貼文', h);
t(h.indexOf('客人甲') < 0, '★ 貼文列表裡不可以還掛著私訊客人', h);
t(state.listOwner === 'posts', '容器擁有者換成 posts', state.listOwner);

// ---- 3) 再切回私訊 → 也要換回來（這是原本「時好時壞」的另一半）
fakeSwitchSection('dm');
h = NODES['convList'].innerHTML;
t(h.indexOf('客人甲') >= 0, '★ 切回私訊 → 列表換回私訊', h);
t(h.indexOf('貼文一') < 0, '★ 私訊列表裡不可以還掛著貼文', h);

// ---- 4) 來回切 3 次都要正確（不是只有第一次對）
var okAll = true;
for (var i = 0; i < 3; i++) {
  fakeSwitchSection('posts');
  if (NODES['convList'].innerHTML.indexOf('貼文一') < 0) { okAll = false; break; }
  fakeSwitchSection('dm');
  if (NODES['convList'].innerHTML.indexOf('客人甲') < 0) { okAll = false; break; }
}
t(okAll, '★ 連續來回切 3 次都正確（指紋不會卡住）');

// ---- 5) 貼文頁按同一個平台 tab（switchPlatform，平台沒變）也要重畫
state.section = 'posts';
renderPosts();
NODES['convList'].innerHTML = 'STALE';        // 假裝容器被清掉/被別的東西蓋了
fakeSwitchPlatform('fb');
t(NODES['convList'].innerHTML.indexOf('貼文一') >= 0,
  '★ 貼文頁重按同平台 tab → 會重畫（不會因為指紋相同而留著 STALE）',
  NODES['convList'].innerHTML);

// ---- 6) 同一邊的指紋優化仍然有效（沒換手時不重畫）
reset();
renderConversations();
NODES['convList'].innerHTML = 'SENTINEL';
renderConversations();          // 同樣資料、同樣 owner
t(NODES['convList'].innerHTML === 'SENTINEL',
  '★ 同一列表資料沒變 → 仍然不重繪（省效能、不打斷點擊）',
  NODES['convList'].innerHTML);

// ---- 7) _claimList 換手一律強制（不依賴指紋）
reset();
renderPosts();
t(_claimList('dm') === true, '_claimList 換手 → true');
t(_claimList('dm') === false, '同一 owner 再一次 → false');

// ---- 8) 空清單也要換（不能因為「兩邊都空」就不重畫）
reset();
state.convs = [];
renderConversations();
h = NODES['convList'].innerHTML;
t(h.indexOf('尚無訊息') >= 0, '前置：私訊空清單');
state.posts = [];
fakeSwitchSection('posts');
h = NODES['convList'].innerHTML;
t(h.indexOf('尚無貼文') >= 0, '★ 兩邊都空 → 文案也要換成貼文的', h);

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
            check(False, "Node 執行沒吐 RESULT", (r.stderr or "")[:400])
        else:
            check(m["fail"] == 0, f"B 段斷言全過（{m['pass']} 項）", f"fail={m['fail']}")
            global _pass
            _pass += int(m["pass"])
    finally:
        f.unlink(missing_ok=True)


def _node() -> Path:
    return Path("node")


# ---------------------------------------------------------------- [C] 反證
def part_c() -> None:
    """★ 反證：把 owner 保護拿掉（＝修復前的寫法），同一段流程**必須壞**。

    沒有這一段，閘門只能證明「現在是好的」，證明不了「它抓得到這個 bug」。
    做法：把抽取出來的原始碼做一次字串替換，得到「舊版」再跑同樣的切換。
    """
    print("\n[C] 反證：拿掉 owner 保護（＝修復前）應該重現 bug")

    def cut(start: str, end: str) -> str:
        i = UI.index(start)
        j = UI.index(end, i)
        return UI[i:j]

    code = (cut("function _claimList", "function _convsSig")
            + cut("function _convsSig", "function renderConversations")
            + cut("function renderConversations", "/* `bpId` 一定要帶")
            + cut("function renderPosts", "function loadMorePosts"))

    old = code.replace("if (!fresh && sig === state.convsSig) return;",
                       "if (sig === state.convsSig) return;")
    old = old.replace("if (!fresh && sig === state.postsSig) return;",
                      "if (sig === state.postsSig) return;")
    check(old != code, "能組出「舊版」判斷（字串替換成功）")

    harness = r"""
var PLATFORM_LABEL = { fb: 'Facebook' };
var NODES = {};
function $(id) { if (!NODES[id]) NODES[id] = { innerHTML: '', style: {} }; return NODES[id]; }
function esc(s) { return String(s === null || s === undefined ? '' : s); }
function fmtTime() { return 'TS'; }
function avatarChar() { return 'A'; }
function mdPreview(t, n) { return String(t || '').slice(0, n); }
function renderBadges() {}
function renderAccountPicker() {}
function accLabel(a) { return a.id; }
function accCountOf() { return 0; }
function accTotalOf() { return 0; }
function boundAccountsOf() { return [{ id: 3, platform: 'fb' }]; }
function noAccountText() { return ''; }
function currentAccount() { return null; }

var state = {
  bizId: 1, platform: 'fb', section: 'dm', scope: 'dm', search: '',
  bpAccount: 0, accPickOpen: false, accPickSig: '',
  convs: [], posts: [], convsSig: null, postsSig: null, listOwner: '',
  activeKey: null, activeAcc: null, activePost: null, overview: null
};
""" + old + r"""
state.convs = [{ key: 'c1', sender_name: '客人甲', pending: 1,
                 last_ts: '1', direction: 'inbound', text: '私訊內容', bp_account_id: 3 }];
state.posts = [{ post_id: 'p1', name: '貼文一', new_comments: 0, comment_count: 1,
                 created_at: '2', bp_account_id: 3 }];

// ★ 真實序列（小jo 的描述）：先看過貼文 → 切到私訊 → 再切回貼文。
//   關鍵在「切回」那一步：舊版 postsSig 還留著上次的值 → 指紋相同 → 不重繪，
//   而容器此時裝的是私訊列表。
state.section = 'posts'; state.scope = 'comment';
renderPosts();                                   // postsSig 現在有值
var hPost = NODES['convList'].innerHTML;

state.section = 'dm'; state.scope = 'dm';
renderConversations();                           // 舊版：不清指紋，容器變私訊
var hDm = NODES['convList'].innerHTML;

state.section = 'posts'; state.scope = 'comment';
renderPosts();                                   // 切回貼文
var hBack = NODES['convList'].innerHTML;

console.log('RESULT ' + JSON.stringify({
  postOk: hPost.indexOf('貼文一') >= 0,
  dmOk: hDm.indexOf('客人甲') >= 0,
  backStillDm: hBack.indexOf('客人甲') >= 0,
  backHasPost: hBack.indexOf('貼文一') >= 0
}));
"""

    node = _ROOT / ".tmp"
    node.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".js", dir=str(node))
    os.close(fd)
    f = Path(tmp)
    f.write_text(harness, encoding="utf-8")
    try:
        r = subprocess.run([str(_node()), str(f)], capture_output=True, text=True)
        m = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT "):
                m = json.loads(line[7:])
        if not m:
            check(False, "反證腳本沒吐 RESULT", (r.stderr or "")[:300])
            return
        check(m["postOk"] and m["dmOk"], "反證前置：兩邊各自都畫得出來")
        check(m["backStillDm"] and not m["backHasPost"],
              "★★ 舊版確實會「切回貼文但列表還是私訊」——證明這支自檢抓得到這個 bug",
              f"stillDm={m['backStillDm']} hasPost={m['backHasPost']}")
    finally:
        f.unlink(missing_ok=True)


def main() -> int:
    part_a()
    part_b()
    part_c()
    print(f"\n{'=' * 62}\n  欄目切換（列表一定要換）：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
