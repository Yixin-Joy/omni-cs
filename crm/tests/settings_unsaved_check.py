"""驗證：AI 自動回復設定面板 **只能按 ✕ 關閉**，且有未保存改動時要先問。

★ 為什麼要有這支（2026-10-20 小jo）：
  「在AI自動回復設置頁面，鼠標點到子窗口之外就會自動關閉當前彈窗，這個不行，
    只有點了叉以後才能關閉；
    如果用戶做了FAQ,業務表等這些配置沒有保存就點擊了叉按鈕退出，
    要彈窗提醒是否保存更改」

  舊行為：`<div id="settingsOverlay" onclick="if(event.target===this)closeSettings()">`
    → 點到面板外任何地方就關掉。而 FAQ 問答對是「編輯 → 按保存」才寫庫的模式，
    業務表格雖然逐格 onchange 自動存，但**正在打的那一格還沒失焦**（沒觸發 change）。
    → 兩種情況都會**靜默丟失**改動，使用者還以為存好了。

修法（這支自檢釘住的）：
  1. 移除 overlay 的點擊關閉；ESC 也走同一條 `closeSettings()`（不能繞過守門）
  2. `spMarkDirty/spClearDirty/spIsDirty` 追蹤「哪個分頁有未保存改動」
  3. `closeSettings()` 髒了就 `spAskUnsaved('close')`：保存 / 不保存 / 取消
  4. 切分頁時若另一頁還髒 → 同樣先問（不然也是靜默丟失）
  5. 分頁上出現橘色小圓點，讓使用者事先知道「這頁還沒存」

本自檢：
  [A] 接線（靜態檢查原始碼）
  [B] 抽到 Node 實跑：乾淨/髒、三種選擇、切分頁、圓點、保存失敗要停住

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> crm/tests/settings_unsaved_check.py
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


def body_of(name: str, size: int = 2600) -> str:
    i = UI.find("function " + name)
    return UI[i:i + size] if i >= 0 else ""


# ---------------------------------------------------------------- [A] 接線
def part_a() -> None:
    print("[A] 靜態接線")

    # 1) 點窗外不可以關
    check('id="settingsOverlay" onclick=' not in UI,
          "★★ 設定面板的 overlay 不再掛「點窗外就關」")
    check('id="settingsOverlay">' in UI, "settingsOverlay 節點仍在（只是沒有 onclick）")
    # 註解裡出現過 onclick 字樣沒關係，但 property 不能有
    ov_tag = UI[UI.index('id="settingsOverlay"'):][:80]
    check("onclick" not in ov_tag.split(">")[0],
          "★ 該元素的開標籤裡沒有任何 onclick", ov_tag[:80])

    # 2) ✕ 走守門
    check('onclick="closeSettings()"' in UI, "★ ✕ 按鈕呼叫 closeSettings()")
    cs = body_of("closeSettings", 400)
    check("spIsDirty()" in cs and "spAskUnsaved('close')" in cs,
          "★★ closeSettings 髒了要先問，乾淨才直接關")
    check("closeSettingsNow()" in cs, "乾淨時走 closeSettingsNow()")

    # 3) 未保存追蹤
    check("var _spDirty" in UI, "★ 有 _spDirty 追蹤")
    for fn in ("spMarkDirty", "spClearDirty", "spIsDirty",
               "spPaintDirtyDots", "spAskUnsaved", "spSaveCurrentTab"):
        check(("function " + fn) in UI, f"有 {fn}()")

    # 4) 確認彈窗是靜態節點（不用原生 confirm）
    check('id="spUnsavedOverlay"' in UI, "★ 未保存確認彈窗是靜態節點 #spUnsavedOverlay")
    check("confirm(" not in body_of("spAskUnsaved", 2600),
          "★ 不用瀏覽器原生 confirm()")
    pick = body_of("spUnsavedPick", 200)
    check("spUnsavedCb" in pick, "有 spUnsavedPick() 統一接三種選擇")
    check("'discard'" in body_of("spAskUnsaved", 2600)
          and "'save'" in body_of("spAskUnsaved", 2600),
          "★ 三選一：save / discard / cancel")
    check('id="spUnsavedSave"' in UI, "「保存並…」按鈕有 id（文案會依情境改）")

    # 5) 切分頁也要守（不然一樣靜默丟失）
    sst = body_of("switchSettingsTab", 700)
    check("spIsDirty(state.settingsTab)" in sst and "spAskUnsaved('switch')" in sst,
          "★★ 切分頁時若當前頁還髒 → 先問")
    check("spDoSwitchTab" in sst, "乾淨時走 spDoSwitchTab()")

    # 6) FAQ 編輯要標記（不自動保存）
    mf = body_of("markFaqDirty", 300)
    check("spMarkDirty('faq'" in mf, "★ FAQ 任一欄編輯 → 標記未保存")
    check("faq-update" not in mf, "★ 標記時**不**自動送存（避免半成品進庫被檢索）")
    check('oninput="markFaqDirty(this)"' in UI, "FAQ textarea 有掛 oninput")

    # 7) 業務表格：oninput 標記 + onchange 保存後清標記
    check('oninput="spMarkDirty(\\\'table\\\', this)"' in UI
          or "oninput=\"spMarkDirty('table', this)\"" in UI,
          "★ 業務表格輸入時標記未保存")
    ur = body_of("updateRow", 900)
    check("spClearDirty('table', el)" in ur, "★ 該格存好後清掉標記")

    # 8) 保存 FAQ 後清標記
    sf = body_of("saveAllFaqs", 900)
    check("spClearDirty('faq')" in sf, "★ FAQ 保存成功後清標記")

    # 9) ESC 不能繞過
    esc = UI[UI.index("if (e.key !== 'Escape') return;"):][:900]
    check("closeSettings()" in esc, "★★ ESC 走同一條 closeSettings（不能繞過守門）")
    check("spUnsavedPick('cancel')" in esc, "ESC 在確認彈窗上 = 取消（不是丟棄）")

    # 10) 圓點樣式
    check(".sp-tab .sp-dirty" in UI, "★ 有未保存小圓點樣式")
    check("spPaintDirtyDots()" in body_of("spDoSwitchTab", 400),
          "切分頁後重畫圓點")

    # 11) 開啟時重置
    os_ = body_of("openSettings", 500)
    check("_spDirty = {}" in os_, "★ 每次開啟重置髒標記（不吃上次殘留）")


# ---------------------------------------------------------------- [B] Node 實跑
def part_b() -> None:
    print("\n[B] 抽到 Node 實跑（三種選擇 + 切分頁）")

    def cut(start: str, end: str) -> str:
        i = UI.index(start)
        j = UI.index(end, i)
        return UI[i:j]

    code = (cut("function openSettings", "function renderSettingsTab")
            + "\n" + cut("var _spDirty = {}", "function closeSettingsNow")
            + "\n" + cut("function closeSettingsNow", "function spDoSwitchTab")
            + "\n" + cut("function spDoSwitchTab", "function renderSettingsTab")
            if "function spDoSwitchTab" in UI else "")

    harness = r"""
var NODES = {};
function $(id) { if (!NODES[id]) NODES[id] = { innerHTML: '', style: {}, classList: mkClass() }; return NODES[id]; }
function mkClass() {
  var s = {};
  return {
    _s: s,
    add: function (c) { s[c] = 1; },
    remove: function (c) { delete s[c]; },
    contains: function (c) { return !!s[c]; }
  };
}
function esc(s) { return String(s === null || s === undefined ? '' : s); }
var TOASTS = [];
function showToast(m) { TOASTS.push(m); }

var state = { settingsTab: 'faq' };
var SAVED = 0, SAVE_OK = true;
function collectFaqs() { return [{ id: 1, question: 'q', answer: 'a', keywords: '' }]; }
function saveAllFaqs() { SAVED++; return Promise.resolve(SAVE_OK); }
function renderSettingsTab() {}
function loadFaqs() { return Promise.resolve(); }

var TABS = [];
function mkTabs() {
  TABS = ['autoreply', 'faq', 'table', 'kb'].map(function (t) {
    var el = {
      _cls: 'sp-tab',
      _attrs: { 'data-tab': t },
      children: [],
      getAttribute: function (k) { return this._attrs[k]; },
      appendChild: function (c) { c.parentNode = this; this.children.push(c); },
      removeChild: function (c) {
        var i = this.children.indexOf(c);
        if (i >= 0) this.children.splice(i, 1);
        c.parentNode = null;
        return c;
      },
      querySelector: function (sel) {
        return this.children.filter(function (c) { return '.' + c.className === sel; })[0] || null;
      },
      set className(v) { this._cls = v; },
      get className() { return this._cls; }
    };
    return el;
  });
}
mkTabs();
var document = {
  querySelectorAll: function (sel) { return sel === '.sp-tab[data-tab]' ? TABS : []; },
  createElement: function () { return { className: '', parentNode: null }; },
  activeElement: null
};
""" + code + r"""
var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }

function reset() {
  _spDirty = {}; _spPendingTab = null;
  SAVED = 0; SAVE_OK = true; state.settingsTab = 'faq';
  NODES['settingsOverlay'] = { innerHTML: '', style: {}, classList: mkClass() };
  NODES['spUnsavedOverlay'] = { innerHTML: '', style: {}, classList: mkClass() };
  NODES['spUnsavedMsg'] = { innerHTML: '', style: {}, classList: mkClass() };
  NODES['spUnsavedSave'] = { textContent: '', style: {}, classList: mkClass() };
  mkTabs();
}

// ---- 1) 乾淨 → 直接關，不問
reset();
$('settingsOverlay').classList.add('open');
closeSettings();
t(!$('settingsOverlay').classList.contains('open'), '★ 沒有改動 → 按 ✕ 直接關閉');
t(!$('spUnsavedOverlay').classList.contains('open'), '★ 沒有改動 → 不彈確認');
t(SAVED === 0, '沒有改動 → 不會呼叫保存');

// ---- 2) 髒了 → 不關、先問
reset();
$('settingsOverlay').classList.add('open');
spMarkDirty('faq', 'ta1');
t(spIsDirty('faq'), 'spMarkDirty 後 spIsDirty=true');
closeSettings();
t($('settingsOverlay').classList.contains('open'),
  '★★ 有未保存改動 → 按 ✕ **不會**直接關閉');
t($('spUnsavedOverlay').classList.contains('open'), '★ 有未保存改動 → 彈出確認框');
t(SAVED === 0, '★ 還沒做選擇前不會偷偷保存');
t($('spUnsavedMsg').innerHTML.indexOf('FAQ 問答對') >= 0,
  '★ 訊息指出是哪個分頁（FAQ 問答對）', $('spUnsavedMsg').innerHTML);
t($('spUnsavedSave').textContent === '保存並關閉',
  '★ 按鈕文案＝保存並關閉', $('spUnsavedSave').textContent);

// ---- 3) 選「取消」→ 面板還在，改動還在
spUnsavedPick('cancel');
t($('settingsOverlay').classList.contains('open'), '★ 取消 → 面板保持開啟');
t(!$('spUnsavedOverlay').classList.contains('open'), '取消 → 確認框收起');
t(spIsDirty('faq'), '★★ 取消 → 未保存標記還在（改動沒丟）');
t(SAVED === 0, '取消 → 沒有保存');

// ---- 4) 再按 ✕ 選「不保存」→ 關閉且清標記
closeSettings();
spUnsavedPick('discard');
t(!$('settingsOverlay').classList.contains('open'), '★ 不保存 → 面板關閉');
t(!spIsDirty('faq'), '★ 不保存 → 標記清掉');

// ---- 5) 選「保存」→ 先存再關
reset();
$('settingsOverlay').classList.add('open');
spMarkDirty('faq', 'ta1');
closeSettings();
spUnsavedPick('save');
setTimeout(function () {
  t(SAVED === 1, '★★ 保存 → 真的呼叫了 saveAllFaqs', 'SAVED=' + SAVED);
  t(!$('settingsOverlay').classList.contains('open'), '★ 保存成功 → 面板關閉');
  t(!spIsDirty('faq'), '保存成功 → 標記清掉');

  // ---- 6) 保存失敗 → 停在面板讓使用者修
  reset();
  SAVE_OK = false;
  $('settingsOverlay').classList.add('open');
  spMarkDirty('faq', 'ta1');
  closeSettings();
  spUnsavedPick('save');
  setTimeout(function () {
    t(SAVED === 1, '（保存被呼叫）');
    t($('settingsOverlay').classList.contains('open'),
      '★★ 保存失敗（例如答案空白）→ 面板**不關**，讓使用者修');
    t(spIsDirty('faq'), '★ 保存失敗 → 標記保留');

    // ---- 7) 切分頁：髒了要先問
    reset();
    state.settingsTab = 'faq';
    spMarkDirty('faq', 'ta1');
    switchSettingsTab('table');
    t(state.settingsTab === 'faq', '★★ 有未保存改動時切分頁 → **不會**直接切走',
      state.settingsTab);
    t($('spUnsavedOverlay').classList.contains('open'), '★ 切分頁也彈確認框');
    t($('spUnsavedMsg').innerHTML.indexOf('切換分頁') >= 0,
      '★ 訊息說明是「切換分頁」會丟', $('spUnsavedMsg').innerHTML);
    t($('spUnsavedSave').textContent === '保存並切換',
      '★ 按鈕文案＝保存並切換', $('spUnsavedSave').textContent);

    // 取消 → 留在原頁
    spUnsavedPick('cancel');
    t(state.settingsTab === 'faq', '取消切換 → 留在原分頁');

    // 不保存 → 切過去
    switchSettingsTab('table');
    spUnsavedPick('discard');
    t(state.settingsTab === 'table', '★ 不保存 → 切到目標分頁', state.settingsTab);
    t(!spIsDirty('faq'), '不保存切換 → 原分頁標記清掉');

    // ---- 8) 乾淨時切分頁 → 直接切
    reset();
    state.settingsTab = 'faq';
    switchSettingsTab('kb');
    t(state.settingsTab === 'kb', '★ 沒有未保存改動 → 切分頁不打擾');
    t(!$('spUnsavedOverlay').classList.contains('open'), '乾淨切頁不彈確認');

    // ---- 9) 小圓點：髒了才出現
    reset();
    spMarkDirty('faq', 'ta1');
    spPaintDirtyDots();
    var faqTab = TABS.filter(function (x) { return x.getAttribute('data-tab') === 'faq'; })[0];
    var tblTab = TABS.filter(function (x) { return x.getAttribute('data-tab') === 'table'; })[0];
    t(faqTab.children.length === 1, '★ 有改動的分頁出現小圓點');
    t(tblTab.children.length === 0, '沒有改動的分頁沒有圓點');
    spClearDirty('faq');
    spPaintDirtyDots();
    t(faqTab.children.length === 0, '★ 存好後圓點消失');

    // ---- 10) 多個元素：逐個清，全部清完才算乾淨
    reset();
    var e1 = {}, e2 = {};
    spMarkDirty('table', e1); spMarkDirty('table', e2);
    t(spIsDirty('table'), '兩格都髒 → 髒');
    spClearDirty('table', e1);
    t(spIsDirty('table'), '★ 存了一格但另一格還沒 → 仍算髒');
    spClearDirty('table', e2);
    t(!spIsDirty('table'), '★ 全部存完才算乾淨');

    // ---- 11) 業務表格分頁：保存時要把 focus 挪走（觸發 change）
    reset();
    state.settingsTab = 'table';
    spMarkDirty('table', { tag: 'INPUT' });
    var blurred = 0;
    var fakeInput = { tagName: 'INPUT', blur: function () { blurred++; } };
    document.activeElement = fakeInput;
    var bodyNode = $('spBody');
    bodyNode.contains = function () { return true; };
    spSaveCurrentTab();
    setTimeout(function () {
      t(blurred === 1, '★★ 業務表格保存時會 blur 正在編輯的格（觸發 onchange 存庫）',
        'blurred=' + blurred);

      var bad = out.filter(function (x) { return !x[0]; });
      out.forEach(function (x) {
        console.log((x[0] ? '  PASS  ' : '  FAIL  ') + x[1] + (x[0] ? '' : '   ' + x[2]));
      });
      console.log('RESULT ' + JSON.stringify({ pass: out.length - bad.length, fail: bad.length }));
    }, 500);
  }, 60);
}, 60);
"""

    node = _ROOT / ".tmp"
    node.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".js", dir=str(node))
    os.close(fd)
    f = Path(tmp)
    f.write_text(harness, encoding="utf-8")
    try:
        r = subprocess.run([str(_node()), "--check", str(f)], capture_output=True, text=True)
        check(r.returncode == 0, "抽出來的前端程式碼語法正確", (r.stderr or "")[:400])
        r = subprocess.run([str(_node()), str(f)], capture_output=True, text=True)
        print(r.stdout.rstrip() or (r.stderr or "")[-800:])
        m = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT "):
                m = json.loads(line[7:])
        if not m:
            check(False, "Node 執行沒吐 RESULT", (r.stderr or r.stdout or "")[-500:])
        else:
            check(m["fail"] == 0, f"B 段斷言全過（{m['pass']} 項）", f"fail={m['fail']}")
            global _pass
            _pass += int(m["pass"])
    finally:
        f.unlink(missing_ok=True)


def _node() -> Path:
    return Path("node")


def main() -> int:
    part_a()
    part_b()
    print(f"\n{'=' * 62}\n  設定面板未保存守門：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
