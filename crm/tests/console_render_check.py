#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CRM 客服工作台的**渲染自檢** —— 用真實後端 payload 去跑前端真正的渲染函式。

## 為什麼要這支（以及它跟根 `tests/ui_check.py` 的關係）

根 `tests/ui_check.py`（1410 行）驗的是**舊單體**的知識庫 console：它的 `arState`、
`renderSettingsTab()`、`#kbNewOverlay` 在 CRM 版 `crm/ui/console_ui.html` 裡 **一個都沒有**
（CRM 版是階段四的客服工作台：會話 / 自動回覆 / 命中測試 / 回覆紀錄）。
所以這不是「把路徑改一改」的移植，而是**為 CRM 版另寫一份**。兩支要並存：

* 舊單體 console（8001）仍然在服務知識庫管理 → 根那支照跑，不要刪。
* CRM console（8020）→ 這支。

共同的手法沿用根版：這台機器**不允許子進程建立監聽 socket**（chromedriver /
playwright 都起不來），所以用「Node + 最小 DOM 樁」把 `console_ui.html` 的 `<script>`
整段載進來，把**從真的後端拿回來的 payload** 塞進 `state`，逐一呼叫渲染函式，
再檢查產出的 HTML。

## 為什麼能把 `state` 塞進去

CRM 版把整份 script 包在 IIFE 裡（`(function(){ 'use strict'; … })()`），外面摸不到
`state`。這裡在載入前把外層 IIFE **拆掉**（不改檔案本身），`state` 與渲染函式就變成
頂層可見 —— 這是唯一不污染 production HTML 的做法。

## 真 vs 合成

* **真的**（直接呼叫 CRM 的端點函式 / repo，走真 DB）：`overview`、`faqs`、`tables`、
  `collections`、`post-config`（未配置 + 已配置兩份）、`logs`、`conversation_logs`、
  命中測試結果（用 FAQ 原句命中 → **0 次模型呼叫**）。
* **合成**（來自 Connector，這支不啟 Connector）：會話清單 `convs`、訊息串 `thread`、
  附件訊息。欄位形狀照 `connector/app/internal/router.py` 的 `/conversations`、
  `/conversation` 實際回傳抄，並比對 `crm/app/agent/router.py` 合併後補的
  `bp_account_id` / `account_name`。

跑法（從專案根，cwd 隨意）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \\
      <底層 python> crm/tests/console_render_check.py

可重複執行：資料一律掛在 `__CK__` 自檢商家底下，跑完 `purge_business()` 清乾淨。

⚠️ 沒被覆蓋的：瀏覽器**真的把 HTML 畫出來**、CSS 版型、真實事件（點擊 / 輸入）。
   那些要人工開一次頁面看。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time

from _ck import check, report, section, ensure_business, purge_business, sql  # noqa: E402

from app.agent import console_router, repo           # noqa: E402
from app.autoreply import autoreply_db               # noqa: E402
from app.rag import kb_db                            # noqa: E402

UI_HTML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "ui", "console_ui.html")
NODE = os.environ.get(
    "UI_NODE",
    r"node")

MARK = "__CK__"
BIZ = ensure_business("前端渲染自檢", "console_render_check 專用")
STAMP = str(int(time.time()))
KEY = f"ck-render-{STAMP}"           # 會話 key（＝end_user_sender_id）
IMG_MD = "![A 款耳機](/media/ab12cd34ef56ab78.png)"   # 回覆裡的 markdown 圖
POST_NEW = f"ck_post_new_{STAMP}"    # 從沒配置過的貼文
POST_SET = f"ck_post_set_{STAMP}"    # 會配置過的貼文
FAQ_Q = f"{MARK}渲染自檢問句{STAMP}"
FAQ_A = "渲染自檢答案：A 款耳機 1280 元。"

# ★ 端點函式直接呼叫用的假 session（等同一個「超級坐席 + 知識庫編輯者」）
SESS = {"roles": ["agent"], "active_role": "agent", "staff_id": 1,
        "is_super_agent": True, "is_kb_editor": True}


# ======================================================================
# 1. 造資料 + 抓真實 payload
# ======================================================================

def seed() -> dict:
    """造一份小而完整的自檢資料，回傳渲染要用的 identifier。"""
    # --- 平台帳號（貼文配置 / 第二層開關都要它）---
    acc = sql("INSERT INTO crm_business_platform_account"
              " (business_id, platform, platform_business_account_id, display_name,"
              "  enabled, ai_auto_reply_enabled)"
              " VALUES (%s, 'ig', %s, '渲染自檢粉專', true, true) RETURNING id",
              (BIZ, f"ck_ig_{STAMP}"))[0]["id"]

    # --- FAQ（create_faq 會自動進基準啟用集）---
    faq = autoreply_db.create_faq(BIZ, FAQ_Q, FAQ_A, keywords="自檢,render")

    # --- 業務表格 ---
    tbl = autoreply_db.create_table(BIZ, f"{MARK}商品表{STAMP}", "渲染自檢用")
    autoreply_db.append_rows(int(tbl["id"]), [
        {"商品名稱": "A 款藍牙耳機", "價格": 1280, "庫存": "有貨"},
    ])

    # --- 文檔知識庫 + 圖片知識庫（圖片庫才會出現「圖片庫」pill）---
    doc_cid = kb_db.create_collection(f"{MARK}文檔庫{STAMP}", "渲染自檢用",
                                      business_id=BIZ)["id"]
    kb_db.create_collection(f"{MARK}圖片庫{STAMP}", "渲染自檢用，有圖",
                            business_id=BIZ, kind="image")

    # --- 回覆紀錄：一筆「回覆就是圖」的（驗 `[圖]` 預覽）＋ 一筆未命中 ---
    #     ★ 另外兩筆掛在會話 key 上，專門驗「AI 留痕」那塊：
    #       一筆帶 detail（顯示診斷字），一筆 detail 為空（顯示回覆 → 要經過 mdImages）
    other = f"ck-log-{STAMP}"
    img_row = [{"kind": "image", "chunk_id": -1, "doc_id": None,
                "doc": f"{MARK}圖片庫{STAMP}", "heading": "A 款耳機",
                "vec": 0.8, "lex": 0.4, "final": 0.71, "picked": True,
                "text": "A 款耳機正面圖"}]
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "rag",
        "reason": "sent", "question": f"{MARK}耳機多少錢", "answer": IMG_MD,
        "detail": "命中知識庫第 1 塊", "send_status": "sent", "score": 0.71,
        "end_user_sender_id": KEY, "matched_rows": img_row,
    })
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "rag",
        "reason": "sent", "question": f"{MARK}耳機有貨嗎", "answer": IMG_MD,
        "detail": "", "send_status": "sent", "score": 0.66,
        "end_user_sender_id": KEY, "matched_rows": img_row,
    })
    autoreply_db.insert_log({
        "business_id": BIZ, "platform": "ig", "scope": "dm", "tier": "miss",
        "reason": "no_answer", "question": f"{MARK}亂問", "answer": "",
        "detail": "三層都沒命中", "send_status": "skipped", "score": None,
        "end_user_sender_id": other,
    })
    # ★ 留言場景的 AI 總開關要開著，工作台才會顯示「貼文特殊配置」入口
    #   （沒這一列 → /conversations 的 ai_enabled=false → 前端顯示「留言 AI 已關」）
    autoreply_db.set_scope_mode(BIZ, "ig", "comment", True)
    return {"acc": int(acc), "faq": int(faq["id"]), "table": int(tbl["id"]),
            "doc_cid": int(doc_cid), "other_key": other}


def fetch_fx(ids: dict) -> dict:
    """把瀏覽器會收到的 JSON 原封不動抓回來（除會話外全部是真呼叫）。

    ⚠️ 直接呼叫端點函式時，`x: str = Query(default="")` 的預設值是 **`Query` 物件本身**
       （不是空字串）—— 那些參數一律要顯式傳，否則 `(q or "").strip()` 會炸。
    """
    # /overview ＋ loadOverview 之後補上的三個清單（前端就是這樣拼的）
    ov = console_router.overview(business_id=BIZ, sess=SESS)
    ov["faqs"] = console_router.faqs(business_id=BIZ, q="", sess=SESS)["items"]
    ov["tables"] = console_router.tables(business_id=BIZ, sess=SESS)["items"]
    ov["collections"] = console_router.collections(business_id=BIZ, sess=SESS)["items"]

    # 貼文特殊配置：一份「從沒配置過」、一份「配置過」
    pc_new = console_router.get_post_config(
        business_id=BIZ, platform="ig", post_id=POST_NEW,
        bp_account_id=ids["acc"], sess=SESS)
    console_router.save_post_config(
        console_router.PostConfigIn(
            business_id=BIZ, platform="ig", bp_account_id=ids["acc"], post_id=POST_SET,
            enabled=True, layers={"faq": True, "table": False, "kb": True},
            bindings={"faq": [ids["faq"]], "table": [], "kb": [ids["doc_cid"]]}),
        sess=SESS)
    pc_set = console_router.get_post_config(
        business_id=BIZ, platform="ig", post_id=POST_SET,
        bp_account_id=ids["acc"], sess=SESS)

    logs = console_router.logs(business_id=BIZ, platform="", scope="", tier="",
                               limit=100, offset=0, sess=SESS)
    # 會話上的 AI 留痕（真 repo：靠 end_user_sender_id 撈）
    conv_logs = repo.conversation_logs(BIZ, "ig", KEY)
    # 命中測試：FAQ 原句規則命中 → 引擎 0 次模型呼叫，所以是真 payload 又不花額度
    test_res = asyncio.run(console_router.test_engine(
        console_router.TestIn(business_id=BIZ, platform="ig", scope="dm",
                              text=FAQ_Q, layers=["faq"]),
        sess=SESS))

    # ---- 合成（Connector 才有的資料；欄位照 connector/app/internal/router.py 抄）----
    convs = [
        {"key": KEY, "sender_id": KEY, "sender_name": "渲染自檢客人",
         "last_ts": "2026-01-02T03:04:05+00:00", "pending": 1, "total": 3,
         "direction": "inbound", "message_type": "text", "text": IMG_MD,
         "post_id": POST_SET, "scope": "comment", "platform": "ig",
         "platform_business_account_id": f"ck_ig_{STAMP}",
         "bp_account_id": ids["acc"], "account_name": "渲染自檢粉專"},
        {"key": ids["other_key"], "sender_id": ids["other_key"], "sender_name": "另一位客人",
         "last_ts": "2026-01-02T01:00:00+00:00", "pending": 0, "total": 1,
         "direction": "outbound", "message_type": "text", "text": "您好，已為您處理。",
         "post_id": None, "scope": "comment", "platform": "ig",
         "platform_business_account_id": f"ck_ig_{STAMP}",
         "bp_account_id": ids["acc"], "account_name": "渲染自檢粉專"},
    ]
    thread = [
        {"direction": "inbound", "text": "你們的耳機多少錢？", "ts": "2026-01-02T03:00:00+00:00",
         "message_type": "text"},
        {"direction": "outbound", "text": IMG_MD, "ts": "2026-01-02T03:01:00+00:00",
         "message_type": "text"},
        {"direction": "inbound", "text": "", "ts": "2026-01-02T03:04:05+00:00",
         "message_type": "image"},
    ]
    return {
        "biz_id": BIZ, "acc_id": ids["acc"], "key": KEY, "post_set": POST_SET,
        "overview": ov, "postcfg_new": pc_new, "postcfg_set": pc_set,
        "logs": logs, "conv_logs": conv_logs, "test_result": test_res,
        "convs": convs, "thread": thread,
        "ai_enabled": repo.ai_switch_row(BIZ, "ig", "comment"),
        "img_md": IMG_MD,
        "faq_q": FAQ_Q, "faq_a": FAQ_A,
    }


# ======================================================================
# 2. Node 樁 + 檢查碼
# ======================================================================

DOM_STUB = r"""
// ===== 最小 DOM 樁 =====
// 這台機器不允許子進程開監聽 socket（chromedriver / playwright 都起不來），
// 所以用這個樁把整份 UI script 載起來，只為了**呼叫渲染函式並檢查輸出的 HTML**。
function __mkEl(tag) {
  const e = {
    tagName: (tag || 'div').toUpperCase(), nodeType: 1,
    innerHTML: '', textContent: '', value: '', checked: false, defaultChecked: false,
    defaultValue: '', selectionStart: 0, selectionEnd: 0, children: [], dataset: {},
    style: {}, files: null, hidden: false, disabled: false,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, f) { if (f === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
                     else { f ? this._s.add(c) : this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {}, removeEventListener() {}, appendChild(c) { this.children.push(c); return c; },
    removeChild() {}, focus() {}, blur() {}, click() {}, setAttribute() {}, getAttribute() { return null; },
    removeAttribute() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, insertBefore() {}, getElementsByTagName() { return []; },
  };
  return e;
}
const __els = {};
// ★ 貼文彈窗的勾選框是**渲染進 postBody 的 HTML 字串**（不是 DOM 節點樹），
//   而 `collectPost()` 會用 `document.querySelectorAll('[data-pck="faq"]')` 把勾選收回來。
//   所以這裡讓 querySelectorAll 去掃那份 HTML —— 驗到的就是「真的畫出來的東西」。
function __scanCheckboxes(sel) {
  const m = /^\[data-pck="([^"]+)"\]$/.exec(sel);
  if (!m) return [];
  const html = __els['postBody'] ? __els['postBody'].innerHTML : '';
  const re = new RegExp('<input[^>]*data-pck="' + m[1] + '"[^>]*>', 'g');
  const out = [];
  let mm;
  while ((mm = re.exec(html)) !== null) {
    const tag = mm[0];
    const v = /value="(\d+)"/.exec(tag);
    out.push({ checked: / checked/.test(tag), value: v ? v[1] : '' });
  }
  return out;
}
global.document = {
  getElementById(id) { if (!__els[id]) __els[id] = __mkEl('div'); return __els[id]; },
  createElement(t) { return __mkEl(t); },
  querySelector() { return null; },
  querySelectorAll(sel) { return __scanCheckboxes(sel); },
  addEventListener() {}, removeEventListener() {},
  activeElement: null, title: '', body: __mkEl('body'),
  getElementsByTagName() { return []; },
};
global.window = global;
global.navigator = { userAgent: 'node' };
global.location = { href: 'http://localhost/console', search: '', pathname: '/console' };
global.alert = function () {}; global.confirm = function () { return false; };
global.prompt = function () { return null; };
global.fetch = function () {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
};
global.setInterval = function () { return 0; };
global.clearInterval = function () {};
global.setTimeout = function (fn) { return 0; };   // 不要讓輪詢真的跑起來
global.clearTimeout = function () {};
global.requestAnimationFrame = function () { return 0; };
"""

CHECKS = r"""
// ===== 檢查工具 =====
const __FAILS = [];
function ck(cond, label, extra) {
  console.log('  [' + (cond ? 'PASS' : 'FAIL') + '] ' + label + (extra ? '   ' + extra : ''));
  if (!cond) __FAILS.push(label);
}
function view(id) { return __els[id] ? __els[id].innerHTML : ''; }
function count(hay, needle) { return hay.split(needle).length - 1; }
// 樣板最常見的三種意外 —— 渲染出來的字串不該有這些
const BAD_KEYS = /\b(undefined|NaN)\b|\[object Object\]/;
function junk(html) {
  const bad = [];
  if (/\bundefined\b/.test(html)) bad.push('undefined');
  if (html.indexOf('[object Object]') >= 0) bad.push('[object Object]');
  if (/\bNaN\b/.test(html)) bad.push('NaN');
  return bad;
}

// ---- 把真實 payload 塞進 state（= 瀏覽器收到回應後做的事）----
state.me = { staff_name: '自檢' };
state.businesses = [{ id: FX.biz_id, business_name: '__CK__前端渲染自檢' }];
state.bizId = FX.biz_id;
state.platform = 'ig';
state.scope = 'comment';
state.aiEnabled = FX.ai_enabled;
state.overview = FX.overview;
state.logRows = FX.logs.logs;
state.logTier = '';
state.testResult = FX.test_result;
state.testText = FX.faq_q;
state.convs = FX.convs;
state.activeKey = FX.key;
state.activeAcc = FX.acc_id;
state.thread = FX.thread;
state.logs = FX.conv_logs;
state.postId = FX.post_set;
state.postAcc = FX.acc_id;
state.postCfg = FX.postcfg_set;
state.postEnabled = !!FX.postcfg_set.enabled;
state.postLayers = {};
state.postChecked = {};
POST_KINDS.forEach(function (k) {
  state.postLayers[k] = !!FX.postcfg_set.layers[layerKeyOf(k)];
  state.postChecked[k] = (FX.postcfg_set.picked[k] || []).slice();
});

console.log('\n[1] 會話工作台 renderConv()');
try {
  state.tab = 'conv';
  render();
} catch (e) {
  ck(false, 'renderConv() 丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}
const vConv = view('view');
ck(!BAD_KEYS.test(vConv), '會話畫面沒有 undefined / NaN / [object Object]', junk(vConv).join(', '));
ck(/class="conv-list"/.test(vConv), '畫出會話清單容器');
ck(count(vConv, 'class="conv-item') === FX.convs.length,
   '兩筆會話都列出來', '實得 ' + count(vConv, 'class="conv-item'));
ck(/class="dot"><\/span>/.test(vConv), '有待處理的會話帶紅點（pending > 0）');
ck(vConv.indexOf('我：') >= 0, '自己發的那筆前面有「我：」');
ck(vConv.indexOf('渲染自檢客人') >= 0, '顯示 sender_name');
// 清單預覽：最後一則是 ![圖](/media/..) → 走 mdPreview，應該是「[圖]」
ck(vConv.indexOf('[圖]') >= 0, '會話清單的最後一則預覽把 markdown 圖換成「[圖]」');
ck(vConv.indexOf('![A 款耳機]') < 0, '清單預覽沒有露出 ![..](..) 原始字串');
// AI 留痕 box（留言場景 + aiEnabled）
ck(/class="aibox"/.test(vConv), '對話旁有 AI 留痕區');
ck(/class="pill (ok|info|warn|off)"/.test(vConv), 'AI 留痕帶層級 pill（有顏色 class）');
ck(vConv.indexOf('<img class="mdimg"') >= 0,
   'AI 留痕裡的回覆圖渲染成 <img class="mdimg">（mdImages 有生效）');
// 訊息氣泡
ck(/class="msg out"/.test(vConv), '我方訊息套了 out class（靠右）');
ck(vConv.indexOf('(非文字訊息)') >= 0, '空文字的訊息有「(非文字訊息)」佔位');
// 訊息氣泡 1 張（我方回覆帶圖）＋ AI 留痕 1 張（detail 為空的那筆顯示回覆）
ck(count(vConv, '<img class="mdimg"') === 2, '氣泡與留痕兩處的圖都畫出來了',
   count(vConv, '<img class="mdimg"') + ' 張');
ck(count(vConv, 'class="row"') === FX.conv_logs.length,
   'AI 留痕把該會話的紀錄都列出來', FX.conv_logs.length + ' 筆');
ck(vConv.indexOf('命中知識庫第 1 塊') >= 0, '留痕有 detail 時顯示診斷字');
ck(/class="pill info"/.test(vConv) && vConv.indexOf('分數 0.66') >= 0,
   '留痕顯示層級與分數');
ck(FX.conv_logs.some(function (g) { return !g.detail; })
   && /mdImages\(g\.detail \|\| g\.answer/.test(String(renderConv)),
   '★ detail 為空時退回顯示 answer（否則操作員看不到真正的回覆）');
// 留言場景才有貼文配置入口
ck(vConv.indexOf('貼文特殊配置') >= 0, '留言場景顯示「貼文特殊配置」入口');
ck(vConv.indexOf('留言 AI 已關') < 0, '留言 AI 開著時不顯示「已關」提示');
// 回覆框
ck(/id="replyText"/.test(vConv) && /id="btnSend"/.test(vConv), '有回覆輸入框與送出鍵');
// 導航列紅點 = pending 總數
const navBadge = (__els['nav'] ? __els['nav'].children : [])
  .map(function (b) { return (b.children || []).map(function (g) { return g.textContent; }).join(''); })
  .filter(function (s) { return s; });
ck(navBadge.length === 1 && navBadge[0] === '1', '導航列紅點 = 未處理總數（1）',
   JSON.stringify(navBadge));
ck(!/confirm\(/.test(String(renderConv)), 'renderConv 沒有用 confirm()');

console.log('\n[2] 貼文特殊配置彈窗 renderPost() / collectPost()');
try {
  renderPost();
} catch (e) {
  ck(false, 'renderPost() 丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}
const vPost = view('postBody');
ck(!BAD_KEYS.test(vPost), '彈窗內文沒有 undefined / NaN / [object Object]', junk(vPost).join(', '));
ck(count(vPost, 'class="postgrp"') === 3, '三組都畫出來（FAQ / 表格 / 知識庫）',
   '實得 ' + count(vPost, 'class="postgrp"'));
ck(vPost.indexOf('FAQ') >= 0 && vPost.indexOf('業務表格') >= 0
   && vPost.indexOf('知識庫') >= 0, '三組標題用的是中文層名（LAYER_LABEL）');
ck(count(vPost, 'data-pgroup=') === 3, '每組各有自己的層開關');
ck(vPost.indexOf('圖片庫') >= 0, '圖片知識庫的項目帶「圖片庫」pill');
ck(vPost.indexOf('私訊有') >= 0, '有在私訊基準集裡的項目帶「私訊有」pill');
ck(vPost.indexOf('有自己的配置') >= 0, '已配置過的貼文文案顯示「有自己的配置」');
ck(/class="chk off"/.test(vPost),
   '層開關關掉的那一組，勾選項套 off class（表格層已停用）');
ck(/class="switch off"[^>]*data-pgroup="table"/.test(vPost)
   || /data-pgroup="table"[^>]*/.test(vPost), '表格層的開關有畫出來');
// 未配置過的那一份：文案要說「與私訊完全相同」且勾選＝基準集
state.postCfg = FX.postcfg_new;
state.postLayers = { faq: true, table: true, kb: true };
state.postChecked = {};
POST_KINDS.forEach(function (k) { state.postChecked[k] = (FX.postcfg_new.picked[k] || []).slice(); });
renderPost();
const vPostNew = view('postBody');
ck(vPostNew.indexOf('尚未配置過') >= 0, '沒配置過的貼文文案顯示「尚未配置過」');
ck(vPostNew.indexOf('class="chk off"') < 0, '沒配置過時三組開關都是開的（沒有 off）');
ck(count(vPostNew, '" checked') === (FX.postcfg_new.picked.faq.length
    + FX.postcfg_new.picked.table.length + FX.postcfg_new.picked.kb.length),
   '沒配置過時，勾選預設＝私訊基準集',
   count(vPostNew, '" checked') + ' 項');
// collectPost 的鍵名（後端靠它定位）—— 先把 state 還原成「已配置」那份並重繪，
// 這樣收回來的勾選才真的是那一份的（不然驗的是上一段的殘留狀態）。
state.postCfg = FX.postcfg_set;
state.postEnabled = !!FX.postcfg_set.enabled;
state.postLayers = {};
state.postChecked = {};
POST_KINDS.forEach(function (k) {
  state.postLayers[k] = !!FX.postcfg_set.layers[layerKeyOf(k)];
  state.postChecked[k] = (FX.postcfg_set.picked[k] || []).slice();
});
renderPost();
let collected = null;
try { collected = collectPost(); } catch (e) { ck(false, 'collectPost() 丟出例外', String(e)); }
if (collected) {
  ck(collected.enabled === !!FX.postcfg_set.enabled && collected.post_id === FX.post_set,
     'collectPost() 帶 enabled 與 post_id');
  ck(collected.layers && 'faq' in collected.layers && 'table' in collected.layers
     && 'kb' in collected.layers, 'collectPost() 的 layers 三鍵齊全',
     JSON.stringify(collected.layers));
  ck(collected.layers.table === false && collected.layers.faq === true,
     'collectPost() 反映「表格層已停用」（跟前端開關一致）',
     JSON.stringify(collected.layers));
  ck(collected.bindings && 'faq' in collected.bindings && 'table' in collected.bindings
     && 'kb' in collected.bindings, 'collectPost() 的 bindings 三鍵齊全');
  ck(collected.business_id === FX.biz_id && collected.bp_account_id === FX.acc_id,
     'collectPost() 帶 business_id 與 bp_account_id');
  ck(JSON.stringify(Object.keys(collected.layers).sort())
     === JSON.stringify(['faq', 'kb', 'table']),
     '送出的鍵名是 faq / kb / table（後端 save_post_config 吃的就是這組）');
  ck(JSON.stringify(collected.bindings.faq)
     === JSON.stringify(FX.postcfg_set.picked.faq)
     && JSON.stringify(collected.bindings.kb)
     === JSON.stringify(FX.postcfg_set.picked.kb),
     '勾選項真的從畫面上收回來了（faq / kb）',
     'faq=' + JSON.stringify(collected.bindings.faq)
     + ' kb=' + JSON.stringify(collected.bindings.kb));
  ck(collected.bindings.table.length === 0,
     '停用層即使沒勾也送空清單（後端只在 enabled 時擋空勾）');
}
ck(!/confirm\(/.test(String(savePost)), 'savePost 沒有用 confirm()');

console.log('\n[3] 自動回覆配置 renderConfig()');
state.postCfg = FX.postcfg_set;
try {
  state.tab = 'config';
  render();
} catch (e) {
  ck(false, 'renderConfig() 丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}
const vCfg = view('view');
ck(!BAD_KEYS.test(vCfg), '配置頁沒有 undefined / NaN / [object Object]', junk(vCfg).join(', '));
ck(count(vCfg, 'data-scope=') === FX.overview.scopes.length,
   '第一層畫出全部商家×平台×場景開關',
   count(vCfg, 'data-scope=') + ' / ' + FX.overview.scopes.length);
ck(count(vCfg, 'data-acc=') === FX.overview.accounts.length,
   '第二層畫出全部平台帳號開關', '實得 ' + count(vCfg, 'data-acc='));
ck(count(vCfg, 'data-layer=') === 3, '層開關正好三個（faq / table / rag）');
ck(vCfg.indexOf('data-layer="rag"') >= 0, '第三層的 key 是 rag（跟引擎 ALL_LAYERS 對齊）');
ck(count(vCfg, 'data-bind="faq:') === FX.overview.faqs.length,
   'FAQ 啟用集列出全部候選', FX.overview.faqs.length + ' 項');
ck(count(vCfg, 'data-bind="table:') === FX.overview.tables.length,
   '業務表格啟用集列出全部候選', FX.overview.tables.length + ' 項');
ck(count(vCfg, 'data-bind="kb:') === FX.overview.collections.length,
   '知識庫啟用集列出全部候選', FX.overview.collections.length + ' 項');
ck(!/class="switch (true|false|undefined)"/.test(vCfg),
   '開關 class 不會出現 true/false/undefined 這種漏轉的字串');
ck(vCfg.indexOf(FX.faq_q) >= 0, 'FAQ 候選顯示的是問句原文');
ck(!!state.overview.can_edit_kb === !!FX.overview.can_edit_kb,
   'can_edit_kb 原封不動傳給前端（不會被渲染函式吃掉）');
// 空狀態：沒有任何候選時不能白屏
const savedOv = state.overview;
state.overview = { scopes: [], accounts: [], layers: {}, bindings: { faq: [], table: [], kb: [] },
                   faqs: [], tables: [], collections: [], counts: {}, can_edit_kb: false };
try {
  render();
  const vEmpty = view('view');
  ck(vEmpty.indexOf('尚未綁定平台帳號') >= 0, '沒有平台帳號時有「尚未綁定平台帳號」空狀態');
  ck(vEmpty.indexOf('沒有可選項目') >= 0, '沒有候選時有「沒有可選項目」空狀態');
} catch (e) {
  ck(false, '空資料渲染配置頁丟出例外', String(e));
}
state.overview = savedOv;

console.log('\n[4] 命中測試 renderTest()（真 payload：FAQ 原句規則命中）');
try {
  state.tab = 'test';
  render();
} catch (e) {
  ck(false, 'renderTest() 丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}
const vTest = view('view');
ck(!BAD_KEYS.test(vTest), '命中測試頁沒有 undefined / NaN / [object Object]', junk(vTest).join(', '));
ck(/id="testInput"/.test(vTest), '有測試問句輸入框');
ck(vTest.indexOf(FX.faq_q) >= 0, '輸入框帶回上次的問句');
ck(count(vTest, 'data-tl=') === 3, '三個層的勾選開關都在');
const tr = FX.test_result;
ck(tr && tr.ok === true, '後端回 ok', tr ? ('tier=' + tr.tier) : 'null');
ck(!!tr && tr.tier === 'faq', 'FAQ 原句規則命中（真端到端，0 次模型呼叫）',
   tr ? ('llm_calls=' + tr.llm_calls) : '');
ck(!!tr && tr.llm_calls === 0, 'FAQ 命中不花模型額度', tr ? ('llm_calls=' + tr.llm_calls) : '');
ck(vTest.indexOf('結果') >= 0, '有「結果」區塊');
ck(/class="pill ok"/.test(vTest), 'faq 命中套 pill ok（綠）');
ck(vTest.indexOf(FX.faq_a) >= 0, '結果顯示 FAQ 的答案原文');
ck(vTest.indexOf('啟用集：FAQ') >= 0, '結果顯示啟用集來源（scope_set）');
ck(!!state.testResult && state.testResult.scope_set
   && typeof state.testResult.scope_set.faq === 'number',
   'scope_set 的 faq/table/kb 是數字（前端直接 esc 它們）');
ck(vTest.indexOf('模型呼叫') >= 0, '顯示模型呼叫次數（省額度的證據）');

console.log('\n[5] 回覆紀錄 renderLogs()');
try {
  state.tab = 'logs';
  render();
} catch (e) {
  ck(false, 'renderLogs() 丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}
const vLogs = view('view');
ck(!BAD_KEYS.test(vLogs), '紀錄頁沒有 undefined / NaN / [object Object]', junk(vLogs).join(', '));
ck(/<table>/.test(vLogs) && count(vLogs, '<th>') === 6,
   '表頭六欄（時間 / 層級 / 平台 / 問句 / 分數 / 回覆）',
   '實得 ' + count(vLogs, '<th>') + ' 欄');
ck(count(vLogs, '<tr>') - 1 === state.logRows.length,
   '每筆紀錄一列', (count(vLogs, '<tr>') - 1) + ' 列 / ' + state.logRows.length + ' 筆');
ck(/class="pill (ok|info|warn|off)"/.test(vLogs), '層級套顏色 pill');
ck(vLogs.indexOf('[圖]') >= 0, '「回覆」欄把 markdown 圖換成「[圖]」');
ck(vLogs.indexOf('![A 款耳機]') < 0, '「回覆」欄沒有露出 ![..](..) 原始字串');
ck(vLogs.indexOf('<img') < 0, '列表不放 <img>（圖塞不進一行）');
ck(vLogs.indexOf('未命中') >= 0, 'miss 層顯示中文「未命中」');
// 空狀態
const savedRows = state.logRows;
state.logRows = [];
render();
ck(view('view').indexOf('沒有紀錄') >= 0, '沒有紀錄時有空狀態（不白屏）');
state.logRows = savedRows;
render();
ck(/selTier/.test(view('pageCtl')), '頂部有層級篩選下拉');

console.log('\n[6] mdImages() / mdPreview() 單元');
(function () {
  const html = mdImages('看這張 ![A 款](/media/abc123.png) 好看嗎');
  ck(html.indexOf('<img class="mdimg" src="/media/abc123.png" alt="A 款">') >= 0,
     'mdImages() 把 ![題注](/media/x.png) 渲染成 <img class="mdimg">');
  ck(html.indexOf('\n') < 0, 'mdImages() 把換行轉成 <br>（回傳的是單行 HTML）');
  ck(mdImages('/files/a.png').indexOf('<img') < 0,
     '/files/ 裸路徑（沒有 markdown 語法）不會變圖');
  ck(mdImages('![x](javascript:alert(1))').indexOf('<img') < 0,
     '★ 非白名單協定（javascript:）不會被渲染成 <img>');
  ck(mdImages('![x](https://cdn.example.com/a.png)').indexOf('<img') >= 0,
     'https:// 的圖放行（外部圖床）');
  const inj = mdImages('<img src=x onerror=alert(1)>');
  ck(inj.indexOf('&lt;img') >= 0 && inj.indexOf('<img src=x') < 0,
     '★ 內文裡的 HTML 會被逃逸（不會變成真的標籤）');
  ck(mdImages('a"b\'c').indexOf('&quot;') >= 0, '引號會被逃逸（不會破版）');
  ck(mdImages('第一行\n第二行').indexOf('<br>') >= 0, '換行變 <br>');
  const p = mdPreview('![A 款](/media/abc.png) 後面還有字', 40);
  ck(p.indexOf('[圖]') >= 0 && p.indexOf('<img') < 0,
     'mdPreview() 把圖換成「[圖]」且不放 <img>');
  ck(mdPreview('12345', 3) === '123', 'mdPreview() 會截斷（清單預覽用）');
  ck(mdPreview('', 10) === '', 'mdPreview() 吃空字串不炸');
  ck(mdPreview(null, 10) === '', 'mdPreview() 吃 null 不炸');
  ck(mdPreview(undefined, 10) === '', 'mdPreview() 吃 undefined 不炸');
})();

console.log('\n[7] 整份 script 載入無例外 + 六個分頁全渲染');
(function () {
  const tabs = ['conv', 'config', 'test', 'logs'];
  tabs.forEach(function (t) {
    try {
      state.tab = t;
      render();
      ck(true, 'render() 在 tab=' + t + ' 不丟例外');
    } catch (e) {
      ck(false, 'render() 在 tab=' + t + ' 不丟例外',
         String(e && e.stack ? e.stack.split('\n')[0] : e));
    }
  });
  ck(typeof esc === 'function' && typeof infotip === 'function'
     && typeof mdImages === 'function' && typeof mdPreview === 'function',
     '四個共用渲染工具都載到了（esc / infotip / mdImages / mdPreview）');
  ck(TABS.length === 4, '導航四個分頁', TABS.map(function (t) { return t.key; }).join(','));
  ck(TABS.every(function (t) { return t.tip && t.label && t.title; }),
     '每個分頁都有 label / title / tip（i 圓標要用的文案）');
})();

console.log('\n' + '='.repeat(78));
console.log(' 前端渲染自檢：' + (__FAILS.length ? (__FAILS.length + ' 項失敗') : '全部通過 ✅'));
__FAILS.forEach(function (f) { console.log('   ✗ ' + f); });
console.log('='.repeat(78));
process.exit(__FAILS.length ? 1 : 0);
"""


# ======================================================================
# 3. 取 UI script（拆掉外層 IIFE）
# ======================================================================

def extract_body() -> str:
    """取出 `<script>` 裡的內容，**拆掉外層 IIFE**（否則外面摸不到 state / 渲染函式）。"""
    with open(UI_HTML, encoding="utf-8") as f:
        txt = f.read()
    js = txt.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    head = "(function () {"
    i = js.index(head) + len(head)
    i = js.index("'use strict';", i) + len("'use strict';")
    j = js.rindex("})();")
    body = js[i:j]
    # 尾端那行 `boot();` 會去打 /api/auth/me（樁會回 {} → 跳 /login）。
    # 留著無害，但拿掉可以少一段非同步雜訊。
    return re.sub(r"\n[ \t]*boot\(\);[ \t]*\n?[ \t]*$", "\n", body)


# ======================================================================
# 4. 主流程
# ======================================================================

def main() -> int:
    if not os.path.exists(NODE):
        print(f"  ⚠ 找不到 node：{NODE}")
        return 2

    print("=" * 78)
    print(" CRM 客服工作台前端渲染自檢（真實 payload → 真實渲染函式）")
    print("=" * 78)

    ids = seed()
    print(f"\n[0] 自檢資料就緒：商家 #{BIZ} / 帳號 #{ids['acc']} / FAQ #{ids['faq']} /"
          f" 表格 #{ids['table']} / 文檔庫 #{ids['doc_cid']}")

    fx = fetch_fx(ids)
    check(fx["overview"].get("ok") is True, "GET /overview 回 ok")
    check(len(fx["overview"]["faqs"]) >= 1 and len(fx["overview"]["tables"]) >= 1
          and len(fx["overview"]["collections"]) >= 2,
          "overview 拼上 FAQ / 表格 / 知識庫清單（跟 loadOverview 一樣）",
          f"{len(fx['overview']['faqs'])} / {len(fx['overview']['tables'])} /"
          f" {len(fx['overview']['collections'])}")
    check(fx["postcfg_new"]["configured"] is False,
          "沒配置過的貼文回 configured=false（＝與私訊相同）")
    check(fx["postcfg_set"]["configured"] is True,
          "配置過的貼文回 configured=true 並帶回挑選結果")
    check(any(c.get("kind") == "image" for c in fx["postcfg_set"]["candidates"]["kb"]),
          "候選清單裡有圖片知識庫（才會出現「圖片庫」pill）")
    check(len(fx["logs"].get("logs") or []) >= 2, "回覆紀錄抓得到",
          f"{len(fx['logs'].get('logs') or [])} 筆")
    check(bool(fx["conv_logs"]), "會話 AI 留痕抓得到（repo.conversation_logs）",
          f"{len(fx['conv_logs'])} 筆")
    check(fx["test_result"].get("tier") == "faq" and fx["test_result"].get("llm_calls") == 0,
          "命中測試 payload 是真的（FAQ 規則命中、0 次模型呼叫）",
          f"tier={fx['test_result'].get('tier')} "
          f"llm_calls={fx['test_result'].get('llm_calls')}")

    try:
        body = extract_body()
        check("var state = {" in body, "UI script 抽取成功（外層 IIFE 已拆）",
              f"{len(body)} 字元")
        check("boot();" not in body, "尾端 boot() 已移除（不會去打 /api/auth/me）")

        harness = (DOM_STUB
                   + "\nconst FX = " + json.dumps(fx, ensure_ascii=False, default=str) + ";\n"
                   + body + "\n" + CHECKS)

        tmpdir = tempfile.mkdtemp(prefix="console_render_check_")
        path = os.path.join(tmpdir, "run.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(harness)
        section("[1~7] 渲染函式（Node + DOM 樁）")
        try:
            proc = subprocess.run([NODE, path], capture_output=True, timeout=180)
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            # 樁自己印的 PASS/FAIL 直接轉進 _ck 的計數
            for line in out.splitlines():
                m = re.match(r"^\s*\[(PASS|FAIL)\]\s+(.*)$", line)
                if m:
                    check(m.group(1) == "PASS", m.group(2))
                elif line.strip().startswith("✗") or line.strip().startswith("="):
                    continue
                elif line.strip():
                    print(line)
            if err.strip():
                print("\n--- node stderr ---")
                print(err[:3000])
                check(False, "node 沒有 stderr 輸出",
                      err.strip().splitlines()[0][:120])
        finally:
            try:
                os.remove(path)
                os.rmdir(tmpdir)
            except OSError:
                pass
    finally:
        print("\n[9] 清理…")
        purge_business(BIZ)
        left = kb_db.list_collections(business_id=BIZ)
        check(not left, "自檢商家已被清空（庫／文件／FAQ／表格／紀錄／貼文配置）",
              f"剩 {len(left)} 個庫")

    return report("CRM 客服工作台前端渲染自檢")


if __name__ == "__main__":
    raise SystemExit(main())
