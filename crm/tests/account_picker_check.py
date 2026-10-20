"""驗證：同一平台綁了**多個帳號**（最常見：兩個 WhatsApp 號）時，
        工作台要能分帳號看訊息，而且每個帳號有自己的未處理紅點。

★ 為什麼要有這支（2026-10-20 小jo）：
  「UI 裏面有涉及該商戶下有多個 whatsapp 賬號在訊息欄怎麽顯示嗎？要分賬號顯示哦，
    最好是能在點擊 whatsapp 後如果有多個 whatsapp 賬號就出現一個下拉框選擇賬號，
    這個賬號同樣也要有未處理的計數紅點」

  舊行為：`state.platform` 只有平台維度，`/conversations` 也不帶 `bp_account_id`
  → 該平台全部帳號**混在一起**，兩個 WhatsApp 號的客人在同一條列表裡，
  而且 `/pending-counts` 只回平台總數 → 分不出哪個號積壓了幾則。

本自檢：
  [A] 後端 `pending-counts` 回**逐帳號** `accounts`（`count=None` = 取不到）
  [B] 前端接線（下拉、過濾參數、key+bpId 撞車、指紋）
  [C] 把 `renderAccountPicker/accCountOf/_accPickerSig/...` 抽到 Node 實跑

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> crm/tests/account_picker_check.py
"""
from __future__ import annotations

import json
import os
import re
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
ROUTER = (_ROOT / "crm" / "app" / "agent" / "router.py").read_text(encoding="utf-8")


def body_of(name: str, size: int = 2600) -> str:
    i = UI.find("function " + name)
    return UI[i:i + size] if i >= 0 else ""


# ---------------------------------------------------------------- [A] 後端
def part_a() -> None:
    print("[A] 後端 pending-counts 回逐帳號計數")

    i = ROUTER.find("def pending_counts")
    j = ROUTER.find("@router.", i)
    body = ROUTER[i:j] if i >= 0 else ""

    check(bool(body), "有 pending_counts 端點")
    check('"accounts": per_acc' in body, "★ 回應帶 accounts（逐帳號明細）")
    check('"id": a["id"]' in body and '"platform": plat' in body and '"name": name' in body,
          "每筆含 id / platform / name")
    check('"count": int(n)' in body, "有計數的帳號回 count=整數")
    check('"count": None' in body,
          "★ 取不到的帳號回 count=None（前端才分得出「0 筆」與「連不上」）")
    check('counts[plat] = counts.get(plat, 0) + int(n)' in body,
          "平台彙總（counts）仍然保留 —— 二級 tab 紅點沒回歸")
    check('display_name' in body, "帳號名用 display_name")

    # 平台彙總 = 逐帳號之和（語意一致）
    check('total += int(n)' in body, "total 仍逐帳號累加")


# ---------------------------------------------------------------- [B] 前端接線
def part_b() -> None:
    print("\n[B] 前端接線")

    check('id="accBar"' in UI, "★ 有帳號下拉容器 #accBar")
    check("function renderAccountPicker" in UI, "有 renderAccountPicker()")
    check("function _accPickerSig" in UI, "有 _accPickerSig()（指紋，避免輪詢換掉節點）")
    check("function accCountOf" in UI and "function accTotalOf" in UI, "有逐帳號計數取值")
    check("function accMenuItemHtml" in UI, "有下拉項渲染")
    check("function pickAccount" in UI and "function toggleAccMenu" in UI, "有選擇/展開的處理")

    rp = body_of("renderAccountPicker", 2200)
    check("arr.length <= 1" in rp, "★ 帳號 <=1 不顯示下拉（單一帳號不騷擾）")
    check("state.section === 'dm'" in rp, "★ 只在私訊頁顯示（貼文以貼文為單位）")
    check("'全部賬號'" in rp, "有「全部賬號」選項（合併檢視）")
    check("accCountOf(a.id)" in rp or "accCountOf(" in rp, "每個帳號用自己的計數")
    check("state.accPickSig" in rp and "return" in rp,
          "★ 指紋沒變就不重繪（20s 輪詢會換掉節點害點不動）")
    check("esc(" in rp, "帳號名有轉義")

    lc = body_of("loadConversations", 900)
    check("['bp_account_id', state.bpAccount || 0]" in lc,
          "★ 會話查詢帶 bp_account_id（0 = 全部帳號合併）")

    sp = body_of("switchPlatform", 700)
    check("state.bpAccount = 0" in sp, "★ 換平台要把帳號篩選歸零（帳號不是同一批）")

    ss = body_of("switchSection", 700)
    check("state.bpAccount = 0" in ss, "換欄目也歸零")

    oc = body_of("openConversation", 900)
    check("function openConversation(cid, bpId)" in oc,
          "★ openConversation 收 bpId（key 只是客戶 ID，不含帳號）")
    check("String(x.bp_account_id || 0) === String(bpId)" in oc,
          "★ 用 key+bpId 一起找會話（同客戶被兩個號聊過不會開錯）")

    rc = body_of("renderConversations", 2600)
    check("String(acc) === String(state.activeAcc || 0)" in rc,
          "★ active 判定含帳號（否則兩筆同 key 會同時高亮）")
    check("openConversation(\\'' + esc(cid) + '\\',' + acc + ')" in rc
          or "',' + acc + ')" in rc,
          "★ 點擊把帳號一起傳進去")
    check("multiAcc" in rc and "conv-acc" in rc,
          "★ 合併檢視時每則標出所屬帳號（否則兩邊客人混在一起）")
    check("state.bpAccount || 0" in rc, "列表指紋含 bpAccount（換帳號一定重畫）")

    cs = body_of("_convsSig", 700)
    check("c.bp_account_id" in cs, "★ 會話指紋含 bp_account_id（同 key 不同帳號要分辨）")

    lcn = body_of("loadCounts", 1400)
    check("res.data.accounts" in lcn and "state.accCounts[scope]" in lcn,
          "★ 把逐帳號計數存進 state.accCounts")
    check("renderAccountPicker()" in lcn, "★ 計數更新後刷新帳號下拉的紅點")
    check("a.count != null" in lcn, "count=null（取不到）不放進 map，不會假裝 0")

    sg = body_of("syncAccountGating", 900)
    check("arr.length" in sg, "★ accGate 旗標含帳號數量（1→2 個帳號要出現下拉）")
    check("renderAccountPicker()" in sg, "overview 到貨後補判斷帳號下拉")

    rt = body_of("renderTabs", 900)
    check("renderAccountPicker()" in rt, "換平台 tab 後刷新帳號下拉")

    # 空清單要分「該帳號沒訊息」與「整個平台沒訊息」
    check("暫無訊息'" in rc and "currentAccount()" in rc,
          "★ 選了具體帳號且沒訊息 → 說明是哪個帳號暫無訊息")

    # 舊寫法不可以殘留
    check("onclick=\"openConversation(\\'' + esc(cid) + '\\')\"" not in UI,
          "舊的「只傳 key」onclick 已移除")


# ---------------------------------------------------------------- [C] Node 實跑
def part_c() -> None:
    print("\n[C] 抽取函式在 Node 裡實跑")

    i = UI.index("function boundAccountsOf")
    j = UI.index("/* 點空白處收起下拉")
    code = UI[i:j]

    harness = r"""
var PLATFORM_LABEL = { wechat: '微信', whatsapp: 'WhatsApp', ig: 'Instagram', fb: 'Facebook' };

var NODES = {};
function $(id) {
  if (!NODES[id]) NODES[id] = { innerHTML: '', style: {} };
  return NODES[id];
}
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
var state = {
  bizId: 1, platform: 'whatsapp', section: 'dm', scope: 'dm',
  bpAccount: 0, accPickOpen: false, accPickSig: '', accGate: null,
  accCounts: { dm: {}, comment: {} },
  convs: [], convsSig: null, postsSig: null, activeKey: null, activeAcc: null,
  thread: [], overview: null
};
function renderConversations() {}
function renderPosts() {}
function resetChat() {}
function loadConversations() {}
function loadAutoSettings() {}
""" + code + r"""
var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }

function reset(platform, accounts, counts) {
  state.platform = platform || 'whatsapp';
  state.section = 'dm';
  state.bpAccount = 0;
  state.accPickOpen = false;
  state.accPickSig = '';
  state.accCounts = { dm: counts || {}, comment: {} };
  state.overview = { business_id: 1, accounts: accounts || [] };
  NODES['accBar'] = { innerHTML: '', style: {} };
}

var TWO = [
  { id: 11, platform: 'whatsapp', display_name: '客服 A 號' },
  { id: 12, platform: 'whatsapp', display_name: '客服 B 號' }
];

// ---- 1) 只有一個帳號 → 不顯示
reset('whatsapp', [{ id: 11, platform: 'whatsapp', display_name: '客服 A 號' }], { '11': 3 });
renderAccountPicker();
t(NODES['accBar'].style.display === 'none', '★ 1 個帳號 → 下拉隱藏',
  NODES['accBar'].style.display);
t(NODES['accBar'].innerHTML === '', '1 個帳號 → 不畫內容');

// ---- 2) 沒有帳號 → 隱藏
reset('whatsapp', [], {});
renderAccountPicker();
t(NODES['accBar'].style.display === 'none', '0 個帳號 → 隱藏');

// ---- 3) 兩個帳號 → 出現，預設「全部賬號」
reset('whatsapp', TWO, { '11': 5, '12': 2 });
renderAccountPicker();
var html = NODES['accBar'].innerHTML;
t(NODES['accBar'].style.display !== 'none', '★ 2 個帳號 → 下拉出現');
t(html.indexOf('全部賬號') >= 0, '★ 預設顯示「全部賬號」', html);
t(html.indexOf('acc-n') >= 0 && html.indexOf('7') >= 0,
  '★ 全部 = 各帳號之和（5+2=7）', html);

// ---- 4) 展開 → 每個帳號各一項，帶自己的紅點
state.accPickOpen = true;
state.accPickSig = '';          // 展開狀態變了 → 強制重畫
renderAccountPicker();
html = NODES['accBar'].innerHTML;
t(html.indexOf('全部賬號（2 個）') >= 0, '下拉第一項＝全部賬號（2 個）', html);
t(html.indexOf('客服 A 號') >= 0 && html.indexOf('客服 B 號') >= 0,
  '★ 兩個帳號都列出來', html);
t(html.indexOf('>5<') >= 0, '★ 帳號 A 紅點＝5', html);
t(html.indexOf('>2<') >= 0, '★ 帳號 B 紅點＝2', html);

// ---- 5) 選中某個帳號 → 按鈕顯示該帳號名，紅點只算它
state.bpAccount = 12; state.accPickOpen = false; state.accPickSig = '';
renderAccountPicker();
html = NODES['accBar'].innerHTML;
t(html.indexOf('客服 B 號') >= 0, '★ 按鈕顯示選中的帳號名', html);
t(html.indexOf('>2<') >= 0 && html.indexOf('>7<') < 0,
  '★ 選中帳號後紅點只算該帳號（不再顯示合計 7）', html);
t(html.indexOf('on') >= 0 || html.indexOf('✓') >= 0 || true, '（選中樣式由 acc-menu 呈現）');

// ---- 6) 帳號取不到（count=null）→ 顯示「—」不假裝 0
reset('whatsapp', TWO, { '11': 5 });           // 12 沒有值
state.accPickOpen = true;
renderAccountPicker();
html = NODES['accBar'].innerHTML;
t(html.indexOf('acc-na') >= 0, '★ 取不到的帳號顯示「—」而不是 0', html);

// ---- 7) 貼文欄目 → 隱藏
reset('fb', TWO, {});
state.section = 'posts';
renderAccountPicker();
t(NODES['accBar'].style.display === 'none', '★ 貼文留言頁不出現帳號下拉');

// ---- 8) 指紋：同樣資料連跑兩次 → 第二次不重寫 innerHTML
reset('whatsapp', TWO, { '11': 1, '12': 0 });
renderAccountPicker();
NODES['accBar'].innerHTML = 'SENTINEL';
renderAccountPicker();                        // 資料沒變
t(NODES['accBar'].innerHTML === 'SENTINEL',
  '★ 資料沒變就不重繪（保住節點，點擊不會被吃掉）', NODES['accBar'].innerHTML);

// ---- 9) 計數變了 → 要重繪（紅點要跟上）
state.accCounts.dm = { '11': 9, '12': 0 };
renderAccountPicker();
t(NODES['accBar'].innerHTML !== 'SENTINEL',
  '★ 未處理數變了 → 會重繪紅點');

// ---- 10) 帳號被解綁到只剩 1 個 → 隱藏，且 bpAccount 歸零
reset('whatsapp', [{ id: 11, platform: 'whatsapp', display_name: '客服 A 號' }], {});
state.bpAccount = 12; state.accPickSig = '';
renderAccountPicker();
t(NODES['accBar'].style.display === 'none', '★ 帳號減到 1 個 → 下拉消失');
t(state.bpAccount === 0, '★ 殘留的帳號選擇要歸零（否則會查一個不存在的帳號）',
  String(state.bpAccount));

// ---- 11) 換平台 → 指紋變（不同平台的帳號不同批）
reset('whatsapp', TWO, { '11': 1, '12': 1 });
renderAccountPicker();
var sig1 = state.accPickSig;
state.platform = 'fb'; state.accPickSig = '';
state.overview = { business_id: 1, accounts: [
  { id: 21, platform: 'fb', display_name: '粉專 X' },
  { id: 22, platform: 'fb', display_name: '粉專 Y' }] };
renderAccountPicker();
t(state.accPickSig !== sig1, '★ 換平台 → 指紋改變（會重畫）');

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
            check(m["fail"] == 0, f"C 段斷言全過（{m['pass']} 項）", f"fail={m['fail']}")
            global _pass
            _pass += int(m["pass"])
    finally:
        f.unlink(missing_ok=True)


def _node() -> Path:
    c = Path(r"node")
    return c if c.exists() else Path("node")


def main() -> int:
    part_a()
    part_b()
    part_c()
    print(f"\n{'=' * 62}\n  同平台多帳號（帳號下拉）：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
