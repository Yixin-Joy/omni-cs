#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""客服工作台前端自檢：用真實後端 payload 去跑真正的渲染函式，抓 JS 例外與壞掉的字串。

為什麼要這支：後端端點全部 200 不代表畫面能用 —— `renderSettingsTab()` 只要有一個
未定義的函式，或某個欄位是 undefined，整個分頁就白屏，而 HTTP 層完全看不出來。

為什麼不用真瀏覽器：這台機器的執行環境**不允許子進程建立監聽 socket**
（chromedriver 回 `CreatePlatformSocket() failed / 10106 WSAEPROVIDERFAILEDINIT`，
playwright 內建的 node.exe 也直接在 `ncrypto::CSPRNG` abort）。所以改成
「Node + 最小 DOM 樁」直接把 console_ui.html 裡的 `<script>` 整段載進來，
再把**從真的 API 抓回來的 payload** 塞進 arState，逐一呼叫渲染函式。

這樣驗到的是「後端真實資料 → 前端真的產生 HTML」這條路，
包含空狀態、壞資料、以及最常見的樣板 bug（undefined / [object Object] / 標籤沒收）。

唯一沒被覆蓋的是「瀏覽器真的把 HTML 畫出來」與 CSS 版型 —— 那部分請人工開一次頁面看。

可重複執行。唯一的寫入：會用 production 的 `autoreply.log_manual_miss` 真的寫一筆
「帶召回明細」的紀錄（否則 DB 裡一筆都沒有，「點開看明細」這條路等於沒驗），
**不發任何訊息**，驗完立刻刪掉並確認刪乾淨。

用法（服務要跑起來）：
    python ui_check.py                          # 預設打 http://127.0.0.1:8001
    UI_BASE=http://127.0.0.1:8002 python ui_check.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 專案根
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.environ.get("UI_BASE", "http://127.0.0.1:8001").rstrip("/")
NODE = os.environ.get(
    "UI_NODE",
    r"node")

FAIL = []


def check(cond, label, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {extra}" if extra else ""))
    if not cond:
        FAIL.append(label)
    return cond


# ======================================================================
# 1. 抓真實 payload
# ======================================================================

def get_json(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__error__": str(e)}


def post_json(path, body):
    try:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"__error__": f"HTTP {e.code}"}
    except Exception as e:
        return {"__error__": str(e)}


def collect_fixtures() -> dict:
    """把所有渲染函式需要的真實資料抓下來。抓不到的用空值，讓「空狀態」也被驗到。"""
    fx = {}
    print("\n[0] 抓真實 payload")

    fx["settings"] = get_json("/console/autoreply/settings")
    check("__error__" not in fx["settings"],
          "GET /console/autoreply/settings",
          f"rag_layer={fx['settings'].get('rag_layer')} kb={fx['settings'].get('kb')}")

    fx["faq"] = get_json("/console/autoreply/faq")
    check("__error__" not in fx["faq"], "GET /console/autoreply/faq",
          f"共 {fx['faq'].get('total')} 組")

    fx["logs"] = get_json("/console/autoreply/logs?limit=20")
    check("__error__" not in fx["logs"], "GET /console/autoreply/logs",
          f"共 {fx['logs'].get('total')} 筆")
    fx["log_detail"] = None          # 由 seed_log_fixture() 補上真資料

    fx["overview"] = get_json("/console/kb/overview")
    check("__error__" not in fx["overview"], "GET /console/kb/overview",
          f"知識庫 {len(fx['overview'].get('collections') or [])} 個 · "
          f"文件 {(fx['overview'].get('totals') or {}).get('documents')} 份")

    fx["documents"] = get_json("/console/kb/documents")
    check("__error__" not in fx["documents"], "GET /console/kb/documents",
          f"{len(fx['documents'].get('documents') or [])} 份")

    # 挑一份「有知識塊」的文件當向導樣本
    fx["document"] = None
    fx["params"] = None
    docs = sorted(fx["documents"].get("documents") or [],
                  key=lambda d: -(d.get("chunk_count") or 0))
    for d in docs:
        if (d.get("chunk_count") or 0) > 0:
            det = get_json(f"/console/kb/document?id={d['id']}&chunks=1&chunks_limit=6")
            if det.get("chunks"):
                fx["document"] = det
                cid = (det.get("doc") or {}).get("collection_id") or 0
                fx["params"] = get_json(f"/console/kb/params" + (f"?collection_id={cid}" if cid else ""))
                break
    check(fx["document"] is not None, "找到一份有知識塊的文件（向導樣本）",
          f"doc#{fx['document']['doc']['id']} · {len(fx['document']['chunks'])} 塊"
          if fx["document"] else "資料庫裡沒有可分塊的文件")
    check(fx["params"] is not None and "items" in (fx["params"] or {}),
          "GET /console/kb/params", f"{len((fx['params'] or {}).get('items') or [])} 個參數")

    fx["empty_documents"] = {"documents": []}
    fx["empty_overview"] = {"collections": [], "totals": {}, "model": {}, "max_upload_mb": 50}

    # 命中測試的結果：真跑一次（layers=[] 不會呼叫模型，也保證不寫紀錄）
    fx["test_miss"] = post_json("/console/autoreply/test/engine",
                                {"text": "前端自檢用問句", "channel": "whatsapp",
                                 "scope": "dm", "layers": [], "with_history": False})
    check("__error__" not in fx["test_miss"], "POST /autoreply/test/engine（layers=[]）",
          f"tier={fx['test_miss'].get('tier')}")

    # 召回明細：由 seed_log_fixture() 用真檢索產生（下面先放空，才不會誤用舊值）
    fx["matched_rows"] = []

    # 合成的試跑結果（含明細）在 seed_log_fixture() 拿到真召回資料後才組
    fx["test_rag_hit"] = None
    fx["test_rag_miss"] = None
    return fx


def build_synthetic(fx: dict) -> None:
    """用真的召回明細組出「知識庫命中 / 未命中」的試跑結果（只為了驗渲染）。

    真跑一次 RAG 命中會呼叫模型，而且得先有一份真的能命中的文件 —— 那是 `engine_check.py`
    的事。這裡只在意「後端會回的那個 JSON 形狀，前端畫不畫得出來」。
    """
    fx["test_rag_hit"] = {
        "ok": True, "tier": "rag", "label": "自動回復-知識庫命中",
        "text": "保固期內非人為損壞可免費維修。",
        "detail": "知識庫檢索命中（最高分 0.512 達門檻 0.28）",
        "reason": "", "score": 0.512, "rewritten_query": "", "history_used": 0,
        "rag_threshold": 0.28, "rag_candidates": len(fx["matched_rows"] or []),
        "matched_rows": fx["matched_rows"] or [],
        "would_send": True, "history": [],
    }
    # 知識庫未命中：分數沒過門檻，但明細要留著（調門檻最需要的線索）
    fx["test_rag_miss"] = dict(fx["test_rag_hit"], tier="miss", label="自動回復未命中",
                               would_send=False, text="",
                               detail="知識庫：最高分 0.164 未達門檻 0.28，降級人工")


def seed_log_fixture(fx: dict) -> list[int]:
    """真的寫一筆「帶召回明細」的紀錄，驗完要刪掉（回傳要清的 id）。

    為什麼要寫：資料庫裡原本一筆都沒有 → 「點開看明細」這條路完全沒被真資料驗到，
    而 `has_matched_rows`（SQL 端對 JSONB 的判斷）也就永遠是 false，等於沒驗。

    走的是 production 自己的 `autoreply.log_manual_miss`（人工右鍵未命中的同一條路）：
    **不發任何訊息、也不動訊息上的 auto_reply_state**。
    """
    from app.autoreply import autoreply
    from app.autoreply import autoreply_db
    from app.rag import kb_search

    q = os.environ.get("UI_CHECK_QUERY", "保固")
    res = kb_search.search(q, top_k=3, min_score=0.0)
    rows = [h.as_dict() for h in res.candidates[:12]]
    fx["matched_rows"] = rows
    check(bool(rows), "跑一次真檢索產生召回明細", f"「{q}」→ {len(rows)} 筆候選")
    if not rows:
        return []

    before_max = int(autoreply_db.query(
        "SELECT COALESCE(max(id),0) AS m FROM autoreply_log")[0]["m"] or 0)
    try:
        autoreply.log_manual_miss(
            "whatsapp", "dm",
            text="（前端自檢用問句，驗完自動刪除）" + q,
            detail=f"知識庫：最高分 {res.top1:.3f} 未達門檻 {res.threshold}，降級人工",
            conversation_key="__ui_check__", matched_rows=rows)
        got = autoreply_db.query("SELECT id FROM autoreply_log WHERE id > %s "
                                 "ORDER BY id DESC LIMIT 1", (before_max,))
        new_id = int(got[0]["id"]) if got else 0
    except Exception as e:
        new_id = 0
        check(False, "用 production 的 log_manual_miss 寫入一筆", str(e)[:90])
    if not new_id:
        check(False, "寫入帶明細的測試紀錄（拿不到 id）")
        return []

    check(True, f"用 production 的 log_manual_miss 寫入一筆 → #{new_id}")

    # 剛寫的這筆一定要出現在列表、且 has_matched_rows = true（SQL 的 JSONB 判斷正確）
    fresh = get_json("/console/autoreply/logs?limit=5")
    hit = next((r for r in (fresh.get("logs") or []) if int(r["id"]) == new_id), None)
    check(hit is not None, "剛寫入的紀錄出現在列表")
    check(bool(hit) and hit.get("has_matched_rows") is True,
          "SQL 算出的 has_matched_rows = true（UI 才知道這列可以點）",
          f"id={new_id} has={hit.get('has_matched_rows') if hit else '?'}")

    fx["logs"] = fresh
    fx["log_detail"] = get_json(f"/console/autoreply/logs/{new_id}")
    got_rows = (fx["log_detail"] or {}).get("matched_rows")
    check(isinstance(got_rows, list) and len(got_rows) == len(rows),
          "GET /logs/{id} 撈得回完整明細",
          f"{len(got_rows) if isinstance(got_rows, list) else '?'} / {len(rows)} 筆")
    return [new_id]


def cleanup_logs(ids: list[int]) -> None:
    """把自檢寫進去的臨時紀錄刪掉（只刪我們自己寫的 id）。"""
    if not ids:
        return
    try:
        from app.autoreply import autoreply_db
        for i in ids:
            autoreply_db.execute("DELETE FROM autoreply_log WHERE id=%s", (i,))
        left = autoreply_db.query(
            "SELECT count(*) AS c FROM autoreply_log WHERE id = ANY(%s)", (list(ids),))
        n = int(left[0]["c"]) if left else -1
        check(n == 0, f"自檢寫入的 {len(ids)} 筆臨時紀錄已清除", f"殘留 {n} 筆")
    except Exception as e:
        check(False, "清理臨時紀錄失敗", f"{e}｜請手動刪除 autoreply_log id={ids}")


# ======================================================================
# 2. 產生 Node 檢查程式
# ======================================================================

DOM_STUB = r"""
// ===== 最小 DOM 樁 =====
// 這台機器不允許子進程開監聽 socket，chromedriver / playwright 都起不來，
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
global.document = {
  getElementById(id) { if (!__els[id]) __els[id] = __mkEl('div'); return __els[id]; },
  createElement(t) { return __mkEl(t); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
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
let __FAILS = [];
function ck(cond, label, extra) {
  console.log('  [' + (cond ? 'PASS' : 'FAIL') + '] ' + label + (extra ? '   ' + extra : ''));
  if (!cond) __FAILS.push(label);
}
function badHtml(html) {
  const bad = [];
  if (/undefined/.test(html)) bad.push('undefined');
  if (/\[object Object\]/.test(html)) bad.push('[object Object]');
  if (/NaN/.test(html)) bad.push('NaN');
  if (/&lt;script|<script/i.test(html)) bad.push('script 注入');
  return bad;
}
function divBalance(html) {
  const o = (html.match(/<div\b/g) || []).length;
  const c = (html.match(/<\/div>/g) || []).length;
  return { o: o, c: c, ok: o === c };
}
function ok(html, label, minLen) {
  const bad = badHtml(html);
  const bal = divBalance(html);
  ck(typeof html === 'string' && html.length >= (minLen || 30), label + ' 有產出',
     html.length + ' 字');
  ck(bad.length === 0, label + ' 沒有壞字串', bad.join('、') || '乾淨');
  ck(bal.ok, label + ' div 標籤成對', bal.o + '/' + bal.c);
  return html;
}
function has(html, s, label) { ck(html.indexOf(s) >= 0, label, '"' + s + '"'); }
function hasNot(html, s, label) { ck(html.indexOf(s) < 0, label, '"' + s + '"'); }

console.log('\n[1] 知識庫第 1 頁：清單（首頁）');
arState.settings = FX.settings;
arState.kb = FX.overview;
arState.kbDocs = FX.documents.documents || [];
arState.kbCollection = 0;
arState.kbUngrouped = false;
arState.kbView = 'list';
arState.kbDocId = 0;
arState.kbDoc = null;
arState.kbUploadNote = '';
let kb = ok(kbTabHtml(), 'kbTabHtml()');
has(kb, '知識庫概覽（第三層）', '有概覽卡片');
has(kb, '已完成向量', '概覽有向量統計');
has(kb, '參數配置', '有參數配置入口');
hasNot(kb, '<script', '沒有未轉義的 script 標籤');
hasNot(kb, 'kbDocRowHtml', '沒有把函式名當內容輸出');
has(kb, 'kbOpenCollection(', '知識庫卡可點進內頁');
// 清單頁只做一件事：不該出現上傳區、也不該出現文件表格（那些是別頁的事）
hasNot(kb, 'kbDropZone', '清單頁沒有上傳區（上傳是另一頁）');
hasNot(kb, '拖曳檔案到這裡', '清單頁沒有拖放提示');
hasNot(kb, '<table class="data-table">', '清單頁沒有文件表格');
hasNot(kb, 'kb-crumbs', '清單頁是首頁，沒有麵包屑');
hasNot(kb, 'kbP_chunk_size', '清單頁沒有分塊參數表單');

console.log('\n[1b] 清單頁：有「未分組」文件時多一個入口');
const ovUg = JSON.parse(JSON.stringify(FX.overview));
ovUg.totals = Object.assign({}, ovUg.totals, { ungrouped_docs: 3 });
arState.kb = ovUg;
const kbUgList = ok(kbListPageHtml(), 'kbListPageHtml()（有未分組）');
has(kbUgList, '未分組', '出現「未分組」入口');
has(kbUgList, 'kbOpenUngrouped()', '未分組可點進去');
arState.kb = FX.overview;
// 這條不能依賴 FX.overview 的實況 —— 真實 DB 現在剛好有一份未分組文件。
// 自己造一個 ungrouped_docs=0 的副本，才驗得到「沒有就不出現」。
const ovNoUg = JSON.parse(JSON.stringify(FX.overview));
ovNoUg.totals = Object.assign({}, ovNoUg.totals, { ungrouped_docs: 0 });
arState.kb = ovNoUg;
ck(kbListPageHtml().indexOf('kbOpenUngrouped()') < 0, '沒有未分組文件時就不出現這個入口');
arState.kb = FX.overview;

console.log('\n[2] 知識庫分頁（空資料 / 空狀態）');
arState.kb = FX.empty_overview;
arState.kbDocs = [];
arState.kbView = 'list';
let kbe = ok(kbTabHtml(), 'kbTabHtml()（空）');
has(kbe, '尚無知識庫', '空狀態給了引導文字');

console.log('\n[3] 知識庫第 2 頁：某個知識庫的文件');
arState.kb = FX.overview;
// 挑文件最多的那個庫當樣本；沒有就用合成的，讓檢查不依賴資料庫現況
const colSample = (FX.overview.collections || []).slice()
  .sort((a, b) => (b.doc_count || 0) - (a.doc_count || 0))[0] ||
  { id: 1, name: '樣本庫', enabled: true, doc_count: 0, ready_count: 0, chunk_count: 0, description: '' };
const CID = colSample.id;
const docMine = { id: 8001, filename: '本庫文件.pdf', title: '本庫文件.pdf', collection_id: CID,
                  status: 'ready', enabled: true, chunk_count: 7, char_count: 900,
                  size_bytes: 2048, doc_type: 'pdf', updated_at: '2026-10-08T10:00:00' };
const docOther = { id: 8002, filename: '別的庫.pdf', title: '別的庫.pdf', collection_id: CID + 991,
                   status: 'ready', enabled: true, chunk_count: 1, char_count: 10, updated_at: '' };
const docNoCol = { id: 8003, filename: '未分組.txt', title: '未分組.txt', collection_id: null,
                   status: 'draft', enabled: true, chunk_count: 0, char_count: 5, updated_at: '' };
arState.kbDocs = [docMine, docOther, docNoCol];
arState.kbCollection = CID;
arState.kbUngrouped = false;
arState.kbView = 'docs';
arState.ktLayers = arState.ktLayers || {};
let kbd = ok(kbDocsPageHtml(), 'kbDocsPageHtml()');
has(kbd, 'kb-crumbs', '有麵包屑');
has(kbd, 'kbGoList()', '麵包屑可以回清單');
has(kbd, '＋ 新增文檔', '有「新增文檔」按鈕');
has(kbd, 'kbNewDoc()', '新增文檔是跳頁（不是就地展開一個區塊）');
hasNot(kbd, 'kbDropZone', '文件清單頁沒有上傳區');
has(kbd, 'kbOpenDoc(', '文件列可點進去看分塊');
hasNot(kbd, '知識庫概覽', '內頁不再重複概覽卡');
has(kbd, '參數配置', '內頁有這個庫的參數配置入口');
// 只列「這個庫」的文件
ck(kbd.indexOf('本庫文件.pdf') >= 0, '列出這個庫的文件');
ck(kbd.indexOf('別的庫.pdf') < 0, '濾掉別的知識庫的文件');
ck(kbd.indexOf('未分組.txt') < 0, '濾掉未分組的文件');
ck((kbd.match(/class="row-link"/g) || []).length === 1, '列數 = 這個庫的文件數',
   (kbd.match(/class="row-link"/g) || []).length + ' 列');
// 路由：kbView='docs' 時 kbTabHtml() 要走這一頁
ck(kbTabHtml() === kbd, 'kbTabHtml() 依 kbView 路由到文件頁');

console.log('\n[3a] 第 2 頁（未分組）');
arState.kbUngrouped = true;
arState.kbCollection = 0;
let kbu = ok(kbDocsPageHtml(), 'kbDocsPageHtml()（未分組）');
has(kbu, '未分組', '標題是「未分組」');
ck(kbu.indexOf('未分組.txt') >= 0 && kbu.indexOf('本庫文件.pdf') < 0, '只列未分組的文件');
hasNot(kbu, '刪除知識庫', '未分組沒有「刪除知識庫」這種動作');
// 當前頁是不可點的：麵包屑只該有「知識庫」（可點）與「未分組」（當前頁，純文字）
hasNot(kbu, 'kbOpenUngrouped()', '麵包屑的當前頁（未分組）不會自己連回自己');
has(kbu, 'kb-crumbs', '未分組頁有麵包屑');
has(kbu, 'kbGoList()', '麵包屑可以回清單');
arState.kbUngrouped = false;
arState.kbCollection = CID;

console.log('\n[3b] 知識庫第 3 頁：新增文檔（上傳）');
arState.kbView = 'new';
arState.kbUploadNote = '';
let kbn = ok(kbNewPageHtml(), 'kbNewPageHtml()');
has(kbn, 'kb-crumbs', '有麵包屑');
has(kbn, 'kbDropZone', '有拖放區');
has(kbn, 'kbPickAny', '有多選檔案輸入');
has(kbn, '拖曳檔案到這裡', '有拖放提示');
has(kbn, '取消，回文件清單', '可以取消回上一頁');
has(kbn, '① 上傳', '說明流程 ① 上傳');
has(kbn, '④ 確認入庫', '說明流程 ④ 確認入庫');
hasNot(kbn, 'kbP_chunk_size', '上傳頁沒有分塊參數表單（那是解析頁的事）');
hasNot(kbn, '<table class="data-table">', '上傳頁沒有文件表格');
has(kbn, esc(colSample.name), '寫明會放進哪個知識庫');
ck(arState.kbCollection === CID, '上傳頁沒有動到所在的知識庫',
   'kbCollection=' + arState.kbCollection);

console.log('\n[3c] 知識庫第 4 頁：文件解析與分塊');
arState.kbView = 'doc';
arState.kbUploadNote = '';
if (FX.document) {
  const cid = (FX.document.doc.collection_id) || 0;
  _kbParamCache[String(cid)] = FX.params;
  _kbParamCache[String(CID)] = FX.params;
  _kbParamCache['0'] = FX.params;
  arState.kbCollection = cid || CID;
  arState.kbDocId = FX.document.doc.id;
  arState.kbDoc = FX.document;
  arState.kbPreview = null;
  let wiz = ok(kbTabHtml(), 'kbTabHtml()（分塊頁）');
  has(wiz, 'kb-crumbs', '有麵包屑');
  has(wiz, '回文件清單', '有回上一頁的按鈕');
  has(wiz, '分塊參數', '有分塊參數列');
  has(wiz, 'kbP_chunk_size', '有塊長輸入框');
  has(wiz, '預覽分塊', '有預覽按鈕');
  has(wiz, '重新分塊', '有重新分塊按鈕');
  has(wiz, '確認入庫', '有確認入庫按鈕');
  has(wiz, 'kbC_', '有知識塊 textarea');
  has(wiz, 'kbChunkSplit', '有拆分動作');
  has(wiz, 'kbChunkMerge', '有合併動作');
  has(wiz, 'kbMoveTo', '有搬移目標選擇');
  hasNot(wiz, 'kbDropZone', '分塊頁沒有上傳區');
  hasNot(wiz, '知識庫概覽', '分塊頁不再重複概覽卡');
  has(wiz, '① 上傳', '有四個步驟的進度條');
  // 每一塊都要有自己的 textarea id
  const ids = [...wiz.matchAll(/id="kbC_(\d+)"/g)].map(m => m[1]);
  ck(ids.length === (FX.document.chunks || []).length,
     '每塊都有獨立 textarea', ids.length + '/' + (FX.document.chunks || []).length);
  ck(new Set(ids).size === ids.length, 'textarea id 不重複');

  console.log('\n[3d] 分塊頁 + 分塊預覽（草稿）');
  arState.kbPreview = { count: 12, avg_chars: 310.5, min_chars: 120, max_chars: 480,
                        strategy: 'heading', chunks: FX.document.chunks };
  let wp = ok(kbDocPageHtml(), 'kbDocPageHtml()（含預覽）');
  has(wp, '會切成', '預覽摘要出現');
  has(wp, '分塊預覽（草稿，未寫庫）', '預覽區標題正確');
  ck(wp.split('kb-chunk').length > wiz.split('kb-chunk').length, '預覽讓區塊數變多');
  arState.kbPreview = null;

  console.log('\n[3e] 分塊頁 + 處理中的文件（背景任務進行中）');
  const runningDoc = JSON.parse(JSON.stringify(FX.document));
  runningDoc.running = 'embedding';
  runningDoc.stats = Object.assign({}, runningDoc.stats || {}, { pending: 3, done: 2 });
  arState.kbDoc = runningDoc;
  let wr = ok(kbDocPageHtml(), 'kbDocPageHtml()（running）');
  has(wr, '向量化中…', '顯示背景任務狀態');
  has(wr, 'disabled', '未向量完時入庫鈕是停用的');
  arState.kbDoc = FX.document;

  console.log('\n[3f] 圖片庫（第 5 頁）：文件抽出的圖 + 單獨上傳 + 人工／AI 描述');
  arState.kbView = 'assets';
  arState.kbAssetCid = 0;
  arState.kbAssetAll = false;
  arState.kbAssets = null;
  let wa0 = ok(kbTabHtml(), 'kbTabHtml()（圖片庫尚未載入）', 80);
  has(wa0, '載入中', '還沒載到資料時顯示載入中');
  hasNot(wa0, '[object Object]', '載入中不會吐 [object Object]');

  arState.kbAssets = { assets: [], stats: { total: 0 }, error: null };
  arState.kbAssetAll = false;
  let waF = ok(kbTabHtml(), 'kbTabHtml()（圖片庫空／不含被過濾）', 80);
  has(waF, '沒有「像內容圖」的圖片', '空狀態說明「被過濾的不列出來」');
  arState.kbAssetAll = true;
  let waE = ok(kbTabHtml(), 'kbTabHtml()（圖片庫完全空）', 80);
  has(waE, '還沒有任何圖片', '整個庫都沒有圖時有說明');
  arState.kbAssetAll = false;

  // 三種圖各一：題注帶入／單獨上傳待填／太小被過濾
  const fxAssets = [
    { id: 11, url: '/console/kb/asset/file/11', caption: '圖 1-1 設定頁面',
      caption_source: 'doc', enabled: true, from_doc: true, doc_title: '操作手冊',
      page_no: 3, chunk_count: 2, width: 800, height: 500,
      filtered_reason: null, filename: 'a.png' },
    { id: 12, url: '/console/kb/asset/file/12', caption: '', caption_source: null,
      enabled: true, from_doc: false, chunk_count: 0, width: 400, height: 300,
      filtered_reason: null, filename: 'b.png' },
    { id: 13, url: '/console/kb/asset/file/13', caption: '', caption_source: null,
      enabled: false, from_doc: true, doc_title: '操作手冊', page_no: 1, chunk_count: 0,
      width: 40, height: 30, filtered_reason: '太小（40×30px）', filename: 'c.png' },
  ];
  arState.kbAssets = { assets: fxAssets,
                       stats: { total: 3, enabled: 2, with_caption: 1, from_doc: 2,
                                manual: 1, ai: 0, filtered: 1 } };
  let wa = ok(kbTabHtml(), 'kbTabHtml()（圖片庫有資料）', 300);
  has(wa, '圖片庫', '有圖片庫標題');
  has(wa, '上傳圖片', '有單獨上傳圖片的入口');
  has(wa, 'kbAssetInput', '有檔案選擇 input');
  has(wa, 'kbAiCaptionPending', '有「AI 描述（未填的）」批次按鈕');
  has(wa, '圖 1-1 設定頁面', '顯示題注帶入的描述');
  has(wa, '題注', '標出描述來源是題注');
  has(wa, '待填描述', '沒有描述時標「待填描述」');
  has(wa, '來自文件', '標出來源是文件');
  has(wa, '第 3 頁', '顯示來自第幾頁');
  has(wa, '綁定 2 塊', '顯示綁到幾個知識塊（圖文結合輸出的關鍵）');
  has(wa, '單獨上傳', '標出來源是單獨上傳');
  has(wa, '太小（40×30px）', '顯示被啟發式過濾的原因');
  has(wa, 'kbSaveCaption', '有儲存描述按鈕');
  has(wa, 'kbAiCaption', '有單張 AI 描述按鈕');
  has(wa, 'kbToggleAsset', '有啟用／停用');
  has(wa, 'kbDeleteAsset', '有刪除');
  has(wa, 'asset/file/11', '縮圖走取圖端點');
  hasNot(wa, '[object Object]', '沒有 [object Object]');
  hasNot(wa, 'undefined', '沒有 undefined 漏出來');
  ck(wa.split('<img').length - 1 === 3, '三張圖都畫出縮圖',
     (wa.split('<img').length - 1) + '/3');

  console.log('\n[3g] 分塊頁：這份文件的圖片條 + 命中明細帶圖');
  arState.kbView = 'doc';
  arState.kbDocAssets = fxAssets;
  let wd = ok(kbDocPageHtml(), 'kbDocPageHtml()（含文件圖片條）', 300);
  has(wd, '這份文件的圖片', '分塊頁顯示這份文件的圖');
  has(wd, '到圖片庫編輯', '有跳到圖片庫的按鈕');
  has(wd, '未填描述', '沒有描述的圖標「未填描述」');
  arState.kbDocAssets = null;

  const hitWithAssets = { chunk_id: 5, doc: 'd.md', final: 0.6, vec: 0.6, lex: 0.2,
                          picked: true, text: 'x',
                          assets: [{ id: 21, url: '/console/kb/asset/file/21',
                                     caption: '圖 2-1 流程' }] };
  const hh = ok(kbHitHtml(hitWithAssets, true, false), 'kbHitHtml(帶圖)', 120);
  has(hh, '命中附圖 1 張', '命中明細標出附圖張數');
  has(hh, 'asset/file/21', '命中明細顯示縮圖');
  has(hh, '圖 2-1 流程', '顯示圖片描述');
  // 舊資料（沒有 assets 欄位）不能炸，也不該冒出「附圖」
  const oldHit = ok(kbHitHtml({ chunk_id: 6, doc: 'd.md', final: 0.5, vec: 0.5,
                                lex: 0.1, picked: true, text: 'y' }, true, false),
                    'kbHitHtml(舊資料無 assets)', 60);
  ck(oldHit.indexOf('命中附圖') < 0, '沒有 assets 就不顯示附圖區');
  arState.kbView = 'doc';
} else {
  ck(false, '找不到有知識塊的文件，分塊頁沒被驗到');
}

console.log('\n[3f] 文件列（各種狀態）');
const statuses = ['draft', 'parsing', 'chunking', 'embedding', 'ready', 'failed', 'disabled'];
let rows = '';
statuses.forEach(function (s, i) {
  rows += kbDocRowHtml(Object.assign({}, docMine, {
    id: 900 + i, status: s, running: '', error: s === 'failed' ? '解析失敗：沒有文字層' : null,
    chunk_count: 3, char_count: 1200, updated_at: '2026-10-08T10:00:00',
  }));
});
rows += kbDocRowHtml(Object.assign({}, docMine, { id: 999, running: 'parse+chunk' }));
ok(rows, 'kbDocRowHtml() 全狀態', 100);
statuses.forEach(function (s) {
  const tag = KB_STATUS[s] ? KB_STATUS[s].text : s;
  ck(rows.indexOf(tag) >= 0, '狀態 ' + s + ' 有對應標籤', tag);
});
has(rows, '解析分塊中…', '背景任務列顯示進度文字');
ck(rows.split('row-link').length - 1 === statuses.length + 1,
   '每一列都是可點進分塊頁的列', (rows.split('row-link').length - 1) + ' 列');

console.log('\n[3g] 知識庫卡（啟用 / 停用 / 有無文件）');
let colCards = (FX.overview.collections || []).map(kbColCardHtml).join('');
if (!colCards) {
  colCards = kbColCardHtml({ id: 1, name: '測試庫', enabled: true, doc_count: 0,
                             ready_count: 0, chunk_count: 0, description: '' });
}
ok(colCards, 'kbColCardHtml()', 50);
has(colCards, 'clickable', '整張卡可點');
has(colCards, 'kbOpenCollection(', '點了會進內頁');
hasNot(colCards, 'onclick="kbRenameCollection', '改名鈕不在卡片上（在內頁）');

console.log('\n[3g2] 知識庫分兩型（文檔 / 圖片）+ markdown 圖片渲染');
const imgCard = kbColCardHtml({ id: 9, name: '截圖庫', kind: 'image', image_of: null,
                                enabled: true, asset_count: 3, doc_count: 0,
                                ready_count: 0, chunk_count: 0 });
has(imgCard, '圖片庫', '圖片知識庫卡片有「圖片庫」標籤');
has(imgCard, 'kbOpenAssets(9)', '圖片庫卡片點進去是圖片頁');
hasNot(imgCard, 'kbOpenCollection(9)', '圖片庫卡片不會進文件頁（那裡沒有文件）');
has(imgCard, '圖片 3 張', '圖片庫卡片顯示圖片數');
const docCard = kbColCardHtml({ id: 8, name: '手冊庫', kind: 'doc', enabled: true,
                                doc_count: 2, ready_count: 1, chunk_count: 5,
                                asset_count: 4 });
has(docCard, 'kbOpenCollection(8)', '文檔知識庫卡片進文件頁');
has(docCard, '圖片 4', '文檔庫卡片也顯示圖片數（自動圖片庫的圖）');

const md = mdImages('前一段\n![圖 3-2 請求流程](/media/ab12cd34.png)\n後一段');
has(md, '<img class="md-img"', 'mdImages() 把 ![題注](/media/…) 渲染成 <img>');
has(md, 'src="/media/ab12cd34.png"', '渲染出來的圖片網址正確');
has(md, 'alt="圖 3-2 請求流程"', 'alt 用了題注（沒題注才留空）');
has(md, '前一段', '圖片以外的文字保留');
ck(mdImages('沒有圖片') === '沒有圖片', 'mdImages() 沒有圖片標記時原樣回傳');
ck(mdImages('![x](javascript:alert(1))').indexOf('<img') < 0,
   'mdImages() 不渲染 javascript: 這類網址（注入防護）');
ck(mdImages(null) === '' && mdImages(undefined) === '', 'mdImages(null/undefined) 不炸');
ck(mdImageCount('![a](/media/a.png) xx ![b](/media/b.png)') === 2, 'mdImageCount 數得對');
ck(mdImageCount('沒有圖') === 0, 'mdImageCount 沒有圖回 0');

// ★ 回歸閘門：任何「會顯示回覆／內文」的地方都必須走 mdImages()，不能只 esc()。
//   實際踩過：客戶端收到圖了，工作台卻只顯示 `![圖 1 …](/media/xx.png)` 這行原始標記
//   （因為那幾處渲染只做了 esc）。這裡逐處盯住。
const IMG_TEXT = '伺服器轉發成功日誌如下：\n![圖 1 服務器端轉發成功日志截圖](/media/b3917526b7f36da3.png)';
const bubbleImg = msgHtml({ direction: 'outbound', text: IMG_TEXT, time: '2026-10-12 11:00', files: [] }, 0);
has(bubbleImg, '<img class="md-img"', '聊天氣泡內文的圖會渲染（不是原始 markdown）');
hasNot(bubbleImg, '!\[圖 1', '聊天氣泡不再顯示 ![…] 原始標記');
const pvImg = kbPreviewHtml({ count: 1, avg_chars: 30, min_chars: 30, max_chars: 30, strategy: 'plain',
                              chunks: [{ char_count: 30, text: IMG_TEXT }] });
has(pvImg, '<img class="md-img"', '分塊草稿預覽的圖會渲染');
arState.ktLayers = {};
const ktImg = ktResultHtml({ tier: 'rag', label: '自動回復-知識庫命中', would_send: true,
                             text: IMG_TEXT, matched_rows: [] });
has(ktImg, '<img class="md-img"', '命中測試「回覆內容」的圖會渲染');
const logImg = logItemHtml({ id: 264, tier: 'rag', send_status: 'sent', channel: 'whatsapp',
                             reply_scope: 'dm', created_at: '2026-10-12 11:00',
                             question: '服务器端转发成功日志长啥样？', answer: IMG_TEXT });
has(logImg, '<img class="md-img"', '紀錄列表「回覆」的圖會渲染');
// 明細／知識塊那兩處本來就接了，一併盯住免得被改回去
has(kbHitHtml({ final: 0.64, vec: 0.6, lex: 0.7, text: IMG_TEXT, picked: true }, true, false),
    '<img class="md-img"', '召回明細的圖會渲染');
has(kbChunkListHtml([{ id: 1, ordinal: 1, char_count: 30, text: IMG_TEXT, enabled: true,
                       embedding_status: 'done' }]),
    '<img class="md-img"', '知識塊清單的內嵌圖片預覽會渲染');

console.log('\n[3g3] 新增知識庫：小彈窗（兩個單選 + 庫名 + 添加／取消），不用 prompt / confirm');
arState.kb = FX.overview;
arState.kbView = 'list';

// 清單頁：一顆「＋ 新增知識庫」開彈窗；彈窗本體是靜態節點，不在清單 HTML 裡
const kbList = ok(kbTabHtml(), 'kbTabHtml()（知識庫清單）');
has(kbList, 'openKbNew()', '「＋ 新增知識庫」開彈窗');
has(kbList, '＋ 新增知識庫', '按鈕文字');
// 靜態彈窗節點（從 HTML 檔抓，不是執行期產生）
hasNot(kbList, 'kbNewOverlay', '彈窗不在清單 HTML 裡（是靜態節點，輪詢重繪不會弄丟它）');

// ★ 核心驗收：新增流程完全不彈原生「是/否」問句
const __create = kbCreateCollection.toString();
ck(__create.indexOf('confirm(') < 0, 'kbCreateCollection 不再用 confirm 問「是/否」');
ck(__create.indexOf('prompt(') < 0, 'kbCreateCollection 不再用 prompt 問名稱');
ck(__create.indexOf('kbNewKind()') >= 0, '類型取自彈窗的單選（kbNewKind）');
ck(openKbNew.toString().indexOf('confirm(') < 0 &&
   openKbNew.toString().indexOf('prompt(') < 0, '開彈窗也沒有原生彈窗');
ck(kbNewNameHint.toString().indexOf('confirm(') < 0, '提示函式也沒有原生彈窗');

// 單選：預設圖片知識庫；選 doc 才會是 doc（DOM 樁的 querySelector 回 null → 先接一根假的）
const __realQS = document.querySelector;
let __kindVal = '';
document.querySelector = function (sel) {
  if (String(sel).indexOf('kbNewKind') >= 0) return { value: __kindVal, checked: true };
  return __realQS ? null : null;
};
ck(kbNewKind() === 'image', '沒選時預設圖片知識庫（小jo 列在前面的那個）');
__kindVal = 'doc';
ck(kbNewKind() === 'doc', '選了文檔知識庫就回 doc');

// 名稱提示：有值時要說出「建哪一種、叫什麼」
__kindVal = 'image';
document.getElementById('kbNewName').value = '介面截圖庫';
kbNewNameHint();
const hintImg = document.getElementById('kbNewHint').innerHTML;
ck(hintImg.indexOf('介面截圖庫') >= 0, '名稱有值 → 提示說出要建的庫名', hintImg.slice(0, 40));
ck(hintImg.indexOf('只建這一個庫') >= 0, '圖片庫提示強調「只建這一個庫」');
__kindVal = 'doc';
document.getElementById('kbNewName').value = '產品保固條款';
kbNewKindPick('doc');
const hintDoc = document.getElementById('kbNewHint').innerHTML;
ck(hintDoc.indexOf('產品保固條款_圖片庫') >= 0,
   '文檔庫提示說明「上傳的文件真的有圖才開 庫名_圖片庫」', hintDoc.slice(0, 60));
ck(hintDoc.indexOf('不會先開圖片庫') >= 0, '文檔庫提示明說「現在不會先開圖片庫」');
document.querySelector = __realQS;

// 開／關彈窗（DOM 樁的 classList 有真的 add/remove，可以直接驗）
arState.kbView = 'list';
openKbNew();
ck(document.getElementById('kbNewOverlay').classList.contains('open'), 'openKbNew() 打開彈窗');
ck(document.getElementById('kbNewName').value === '', '開彈窗會清空上次輸入的庫名');
closeKbNew();
ck(!document.getElementById('kbNewOverlay').classList.contains('open'), 'closeKbNew() 關閉彈窗');

// 回清單要順手關掉彈窗（不留半開的殘影）
openKbNew();
kbGoList();
ck(!document.getElementById('kbNewOverlay').classList.contains('open'),
   'kbGoList() 會關掉新增彈窗');

console.log('\n[3g4] 圖片庫歸屬：文檔庫附帶的圖片庫不單獨列卡片');
const colWithLib = [
  { id: 1, name: '手冊庫', kind: 'doc', enabled: true, doc_count: 2, ready_count: 2,
    chunk_count: 10, asset_count: 3 },
  { id: 2, name: '手冊庫_圖片庫', kind: 'image', image_of: 1, enabled: true,
    asset_count: 3, doc_count: 0, chunk_count: 0 },
  { id: 3, name: '截圖庫', kind: 'image', image_of: null, enabled: true,
    asset_count: 5, doc_count: 0, chunk_count: 0 },
];
arState.kb = { collections: colWithLib, totals: {}, model: {} };
const listHtml = kbTabHtml();
has(listHtml, '手冊庫', '文檔庫列出來');
has(listHtml, '截圖庫', '獨立圖片庫列出來（image_of 空的照樣顯示）');
hasNot(listHtml, '手冊庫_圖片庫', '文檔庫附帶的圖片庫不單獨列卡片');
has(listHtml, '另有 1 個', '並說明少列了幾個附帶圖片庫');
ck(kbBoundImageLib(1) && kbBoundImageLib(1).name === '手冊庫_圖片庫', 'kbBoundImageLib 找得到附帶的圖片庫');
ck(kbBoundImageLib(3) === null, '獨立圖片庫不會被當成別人的附帶圖片庫');
ck(kbBoundImageLib(2) === null, '圖片庫自己沒有附帶圖片庫');

console.log('\n[3g5] 圖片庫的麵包屑：獨立圖片庫不該再多一層、也不能連到空的文檔頁');
// 小jo：「建立圖片庫（效果展示）後左上角顯示 知識庫 › 效果展示 › 圖片庫，
//        點『效果展示』還進到一個名為效果展示的空文檔知識庫 —— 不需要，應該是 知識庫 › 效果展示」
arState.kb = { collections: colWithLib, totals: {}, model: {} };
arState.kbAssets = { assets: [], stats: {}, include_filtered: true };

// ① 獨立圖片庫（image_of 空）：知識庫 › 名稱（就結束，不可點）
arState.kbAssetCid = 3;
const crumbOwn = kbAssetsPageHtml();
ck(crumbOwn.indexOf('圖片庫</span>') < 0 && crumbOwn.indexOf('圖片庫</a>') < 0,
   '獨立圖片庫的麵包屑不再多一層「圖片庫」');
has(crumbOwn, '截圖庫', '麵包屑有庫名');
hasNot(crumbOwn, 'kbOpenCollection(3)', '★ 庫名不可點（點下去會進空文檔頁）');
ck(crumbOwn.indexOf('c-cur">截圖庫') >= 0, '庫名是最後一層（c-cur）');
has(crumbOwn, '<h3>截圖庫<i class="infotip"', '標題直接用庫名（不再是「圖片庫 · 截圖庫」）');

// ② 文檔庫附帶的圖片庫：知識庫 › 文檔庫 › 圖片庫（這一層有意義，要能回去）
arState.kbAssetCid = 2;
const crumbBound = kbAssetsPageHtml();
has(crumbBound, '手冊庫</a>', '附帶圖片庫的上一層是文檔庫、且可點');
has(crumbBound, 'kbOpenCollection(1)', '點上一層回的是**文檔庫**（不是圖片庫自己）');
has(crumbBound, '圖片庫</span>', '附帶圖片庫保留「圖片庫」這一層');

// ③ 全部圖片：知識庫 › 圖片庫
arState.kbAssetCid = 0;
const crumbAll = kbAssetsPageHtml();
has(crumbAll, '全部知識庫', '全部圖片顯示「全部知識庫」');
has(crumbAll, '圖片庫</span>', '全部圖片有「圖片庫」這一層');

// ④ kbOpenCollection 遇到圖片庫 id → 改開圖片頁（堵住「空的文檔知識庫」那條路）
ck(kbIsImageLib(3) === true && kbIsImageLib(1) === false, 'kbIsImageLib 認得圖片庫');
arState.kbView = 'list';
kbOpenCollection(3);
ck(arState.kbView === 'assets' && arState.kbAssetCid === 3,
   '★ kbOpenCollection(圖片庫) 改開圖片頁，不會出現空文檔頁',
   'view=' + arState.kbView + ' cid=' + arState.kbAssetCid);

// ⑤ 就算狀態殘留讓它畫到文件頁，也要給一條跳去圖片頁的路（不裝成空的文檔庫）
arState.kbView = 'docs';
arState.kbCollection = 3;
arState.kbUngrouped = false;
const docsGuard = kbDocsPageHtml();
has(docsGuard, '這是一個<b>圖片知識庫</b>', '文件頁遇到圖片庫會說明「它沒有文件清單」');
has(docsGuard, 'kbOpenAssets(3)', '並給一顆「看這個圖片庫」的按鈕');
hasNot(docsGuard, '＋ 新增文檔', '★ 不會出現「＋ 新增文檔」（那才是讓人以為是文檔庫的元兇）');

// ⑥ 參數彈窗的「分塊參數目標」下拉不列圖片庫（它們沒有文件也沒有知識塊）
ck(kbRenderParams.toString().indexOf("x.kind !== 'image'") >= 0,
   '參數彈窗的目標下拉排除了圖片庫');

arState.kb = FX.overview;
arState.kbCollection = CID;
arState.kbAssetCid = 0;
arState.kbView = 'list';

console.log('\n[3h] 導覽：三頁互跳');
arState.kb = FX.overview;
arState.tab = 'kb';
arState.kbCollection = CID;
arState.kbDocId = 0;
arState.kbDoc = null;
arState.kbView = 'doc';
kbGoList();
ck(arState.kbView === 'list' && arState.kbDocId === 0 && arState.kbDoc === null,
   'kbGoList() 回清單並清掉文件狀態', 'view=' + arState.kbView);
kbNewDoc();
ck(arState.kbView === 'new', 'kbNewDoc() 進上傳頁', 'view=' + arState.kbView);
ck(arState.kbUploadNote === '', '跳頁會清掉上一次的上傳說明');
arState.kbView = 'list';

console.log('\n[4] 命中測試分頁');
arState.settings = FX.settings;
arState.ktSynced = false;
ktSyncLayers();
ck(arState.ktLayers.faq === !!FX.settings.faq_layer &&
   arState.ktLayers.table === !!FX.settings.table_layer &&
   arState.ktLayers.rag === !!FX.settings.rag_layer,
   '第一次進頁時，勾選狀態照「自動回復」頁的實際開關',
   JSON.stringify(arState.ktLayers));
const tabsHtml = hittestTabHtml();
ok(tabsHtml, 'hittestTabHtml()', 200);
has(tabsHtml, '<h3>命中測試', '標題是「命中測試」');
has(tabsHtml, 'infotip', '解釋性文字收進「i」圓標（不再佔版面）');
has(tabsHtml, 'kt_faq', '有第一層勾選');
has(tabsHtml, 'kt_table', '有第二層勾選');
has(tabsHtml, 'kt_rag', '有第三層勾選');
has(tabsHtml, 'ktQuery', '有問句輸入框');
has(tabsHtml, '開始測試', '有測試按鈕');
has(tabsHtml, 'ktNoHistory', '有「不帶對話」選項');
has(tabsHtml, '全選', '有全選');
has(tabsHtml, '全不選', '有全不選');
has(tabsHtml, '照目前開關重設', '有「照目前開關重設」');
has(tabsHtml, '目前開關：', '顯示實際開關狀態');
has(tabsHtml, 'ktTarget', '有渠道 / 場景選擇');
has(tabsHtml, '不會發送任何訊息、不會改上面的開關、也不寫任何紀錄', '明講不會有副作用');
// 查詢改寫：三態選擇（跟隨設定 / 這次強制開 / 這次強制關）
has(tabsHtml, '查詢改寫', '有查詢改寫的選擇（說明收進「i」圓標）');
has(tabsHtml, '三層共用', '「i」圓標說明仍是三層共用');
has(tabsHtml, '依知識庫設定', '查詢改寫預設「依知識庫設定」');
has(tabsHtml, 'arState.ktRewrite=this.value', '選擇會寫進 arState.ktRewrite');
ck(arState.ktRewrite === '', '查詢改寫預設不覆寫（不影響正式設定）',
   JSON.stringify(arState.ktRewrite));

console.log('\n[4b] 「自動回復」頁瘦身：試跑卡 / 總開關 / 成效統計都搬走');
arState.settings = FX.settings;
const arHtml = autoreplyTabHtml();
hasNot(arHtml, 'arTestResult', '自動回復頁不再有試跑結果區');
hasNot(arHtml, 'arTestText', '自動回復頁不再有試跑輸入框');
hasNot(arHtml, '總開關', '總開關那一欄已刪（右上角滑動開關取代）');
hasNot(arHtml, '各渠道成效', '各渠道成效已搬到「紀錄」頁');
ck(typeof testEngine === 'undefined', 'testEngine() 已移除（不留死碼）');
ck(typeof clearScopeOverride === 'undefined', '「跟隨全域」按鈕已刪（不留死碼）');

console.log('\n[4b2] 頁面瘦身：說明收進「i」圓標 + 右上角滑動開關');
// ① infotip() 是純 CSS tooltip（.infotip::after 讀 data-tip），要靠 esc() 防破版
const tip = infotip('測試"引號\' 與 <b>標籤</b>');
ck(tip.indexOf('data-tip="') > 0, 'infotip() 產生 data-tip', tip.slice(0, 60));
ck(tip.indexOf('<b>') < 0, '★ infotip() 會把 HTML 轉義（不會破版／注入）');
ck(tip.indexOf('&quot;') > 0 && tip.indexOf('&#39;') > 0, '引號被轉義');
ck(/^<i class="infotip"/.test(tip), 'infotip() 是 <i> 圓標');

// ② 各渠道那一區的右上角是滑動開關（不是把整塊按鈕塞在 header）
const arFull = autoreplyTabHtml();
has(arFull, 'sw-toggle', '有滑動開關樣式');
has(arFull, "setAllScopes(", '滑動開關綁 setAllScopes()');
has(arFull, '全部開', '開關標籤寫「全部開」');
ck(typeof setAllScopes === 'function', 'setAllScopes() 已定義');
// 「全部開」＝全域 + 每一把都開，標籤才名副其實
const __all = setAllScopes.toString();
ck(__all.indexOf('settings/global') >= 0 && __all.indexOf('settings/scope') >= 0,
   'setAllScopes(true) 會同時開全域與每個渠道');

// ③ 標籤要跟著「每一把開關的 effective」走，**不是看 global**
//    ⚠️ 全域只是「沒單獨設定」的預設值（優先序 渠道+場景 > 渠道 > 全域 > 預設關），
//    所以 global 值不能拿來當「全部」的依據。
const mk = effs => ({ ...FX.settings,
                      scopes: FX.settings.scopes.map((s, i) => ({ ...s, effective: effs[i] })) });
arState.settings = mk(FX.settings.scopes.map(() => true));
ck(autoreplyTabHtml().indexOf('全部開') > 0, '每把都開 → 顯示「全部開」');
arState.settings = mk(FX.settings.scopes.map(() => false));
ck(autoreplyTabHtml().indexOf('全部關') > 0, '每把都關 → 顯示「全部關」');
const mix = mk(FX.settings.scopes.map((_, i) => i > 0));
arState.settings = {...mix, global: false};
const mixHtml = autoreplyTabHtml();
ck(mixHtml.indexOf('部分開啟') > 0, '★ 有開有關 → 顯示「部分開啟」（標籤不騙人）');
ck(mixHtml.indexOf('全部關</span>') < 0, '不能因為 global=false 就騙人說「全部關」');
arState.settings = FX.settings;
// 全部開/關必須**逐把**設定（只改全域不會讓已單獨開啟的渠道停下來）
ck(setAllScopes.toString().indexOf('settings/scope') >= 0,
   '★ setAllScopes() 逐把設定每個渠道（只改 global 是沒用的）');

// ④ 各渠道成效：搬到紀錄頁，且用子頁切換（不是塞在列表下面）
hasNot(autoreplyTabHtml(), 'arStats', '自動回復頁不再有成效統計容器');
arState.logsSub = 0;
const logsMain = logsTabHtml();
has(logsMain, 'openLogsStats()', '紀錄頁有「命中情況統計」入口');
hasNot(logsMain, 'logStatsBox', '主頁不畫統計表（點了才去撈）');
arState.logsSub = 1;
const logsSub = logsTabHtml();
has(logsSub, 'logStatsBox', '子頁有統計表容器');
has(logsSub, 'backToLogs()', '子頁有「回紀錄列表」');
hasNot(logsSub, 'openLogsStats()', '子頁不再顯示入口自己');
backToLogs();
ck(arState.logsSub === 0, 'backToLogs() 回到列表子頁');
openLogsStats();
ck(arState.logsSub === 1, 'openLogsStats() 進入統計子頁');
switchSettingsTab('logs');
ck(arState.logsSub === 0, '切分頁會離開統計子頁（不留殘影）');
// 統計表本身（餵假資料，不打 API）
const stBox = document.getElementById('logStatsBox');
renderStatsInto(stBox, { rewritten_total: 3, scopes: [
  { channel: 'whatsapp', scope: 'dm', enabled: true, faq: 5, table: 2, rag: 7,
    rewritten: 3, miss: 1, pending_miss: 2 }] });
has(stBox.innerHTML, '改寫救回', '統計表有「改寫救回」欄');
has(stBox.innerHTML, '累計 3 次', '統計表顯示改寫救回累計');
has(stBox.innerHTML, 'pill miss', '待處理數字有醒目標記');
renderStatsInto(stBox, null);
has(stBox.innerHTML, '讀不到統計資料', '沒資料時給說明，不是白畫面');
arState.logsSub = 0;

console.log('\n[4c] 逐層勾選的操作');
ktAllLayers(false);
ck(!arState.ktLayers.faq && !arState.ktLayers.table && !arState.ktLayers.rag,
   '全不選 → 三層都關', JSON.stringify(arState.ktLayers));
ktAllLayers(true);
ck(arState.ktLayers.faq && arState.ktLayers.table && arState.ktLayers.rag,
   '全選 → 三層都開', JSON.stringify(arState.ktLayers));
ktSetLayer('table', false);
ck(!arState.ktLayers.table && arState.ktLayers.faq && arState.ktLayers.rag,
   '單獨取消一層，其他層不受影響', JSON.stringify(arState.ktLayers));
ktResetLayers();
ck(arState.ktLayers.faq === !!FX.settings.faq_layer &&
   arState.ktLayers.table === !!FX.settings.table_layer &&
   arState.ktLayers.rag === !!FX.settings.rag_layer,
   '照目前開關重設 → 回到實際開關', JSON.stringify(arState.ktLayers));
// 30 秒的設定同步不該把使用者手動改的勾選蓋掉
ktSetLayer('rag', false);
ktSyncLayers();          // 模擬 renderSettingsTab 的定時同步
ck(arState.ktLayers.rag === false, '設定同步不會蓋掉使用者手動改的勾選');
ktSyncLayers(true);
ck(arState.ktLayers.rag === !!FX.settings.rag_layer, '強制重設才會覆蓋');
// 還原成「照實際開關」，後面的測試才一致
ktSyncLayers(true);

console.log('\n[5] 命中測試結果卡（三種層級）');
[['faq', 'tag-auto-faq'], ['table', 'tag-auto-table'], ['rag', 'tag-auto-rag']].forEach(function (pv) {
  const tier = pv[0], cls = pv[1];
  const d = Object.assign({}, FX.test_rag_hit, { tier: tier, label: '自動回復-' + tier + '命中' });
  const h = ok(ktResultHtml(d), 'ktResultHtml(' + tier + ')', 150);
  has(h, cls, tier + ' 用對應的標籤樣式');
  has(h, '會自動回覆', '顯示會自動回覆');
});
let miss = ok(ktResultHtml(FX.test_rag_miss), 'ktResultHtml(miss)', 150);
has(miss, '降級人工', '未命中顯示降級人工');
has(miss, '知識庫召回明細', '未命中仍保留召回明細（調門檻的依據）');
let noRows = ktResultHtml({ tier: 'miss', label: '自動回復未命中', would_send: false,
                            detail: '三層都沒中', matched_rows: [] });
ok(noRows, 'ktResultHtml(無明細)', 60);
has(noRows, '沒有可送出的回覆', '沒有回覆時的文案');
hasNot(noRows, '知識庫召回明細', '沒有明細時不出明細區塊');
// 勾了查詢改寫時，結果卡要把「這次實際拿去檢索的每一條查詢」列出來
const withQ = ktResultHtml(Object.assign({}, FX.test_rag_hit, {
  rag_queries: ['藍芽耳機多少千', '藍牙耳機多少錢'], rag_threshold: 0.4,
  rag_candidates: 8, tier: 'rag', label: '自動回復-知識庫命中', would_send: true,
}));
ok(withQ, 'ktResultHtml(帶 rag_queries)', 150);
has(withQ, '本次共用的查詢', '多查詢時列出實際用的查詢（標明三層共用）');
has(withQ, '原句', '標出哪一條是原問句');
has(withQ, '改寫1', '標出改寫句');
ck(ktResultHtml(Object.assign({}, FX.test_rag_hit, { rag_queries: ['只有一句'] }))
     .indexOf('本次共用的查詢') < 0,
   '單查詢時不列查詢清單（不洗版）');

console.log('\n[6] 召回明細渲染（紀錄頁 / 命中測試共用）');
const mr = renderMatchedRows(FX.matched_rows, true);
if (FX.matched_rows.length) {
  ok(mr, 'renderMatchedRows()', 150);
  has(mr, '召回', '有召回摘要');
  has(mr, '入選', '標出入選');
  has(mr, '未入選', '標出未入選');
  has(mr, '總分', '顯示分數');
  ck((mr.match(/kb-hit/g) || []).length >= FX.matched_rows.length, '每一筆都畫出來',
     (mr.match(/kb-hit/g) || []).length + '/' + FX.matched_rows.length);
} else {
  ck(false, '沒有召回明細樣本可驗');
}
ck(renderMatchedRows(null, true) === '', 'renderMatchedRows(null) 回空字串（不炸）');
ck(renderMatchedRows([null, 'x', 3], true).indexOf('[object') < 0,
   'renderMatchedRows(壞資料) 不產生 [object Object]');
ck(renderMatchedRows([], true) === '', 'renderMatchedRows([]) 回空字串');

console.log('\n[6b] 多查詢檢索（查詢改寫）的顯示');
// 單查詢時不該多出「命中查詢」那一行（避免正常情況被洗版）
const singleRow = [{ chunk_id: 1, doc: 'a.md', final: 0.5, vec: 0.5, lex: 0.2,
                     picked: true, text: 't', queries: ['藍牙耳機多少錢'] }];
ck(renderMatchedRows(singleRow, true).indexOf('命中查詢') < 0,
   '單查詢時不顯示「命中查詢」（不洗版）');
const multiRow = [
  { chunk_id: 1, doc: 'a.md', final: 0.6, vec: 0.6, lex: 0.3, picked: true,
    text: 't', queries: ['藍芽耳機多少千', '藍牙耳機多少錢'] },
  { chunk_id: 2, doc: 'b.md', final: 0.2, vec: 0.2, lex: 0.1, picked: false,
    text: 'u', queries: ['藍芽耳機多少千'] },
];
const mrMulti = renderMatchedRows(multiRow, true);
ok(mrMulti, 'renderMatchedRows(多查詢)', 150);
has(mrMulti, '命中查詢', '多查詢時每塊標出命中查詢');
has(mrMulti, '藍牙耳機多少錢', '看得到改寫句');
has(mrMulti, '多查詢檢索', '摘要說明這次是多查詢檢索');
// 沒有 queries 欄位的舊資料不能因此爆掉
const oldRow = [{ chunk_id: 3, doc: 'c.md', final: 0.4, vec: 0.4, lex: 0.1,
                  picked: true, text: 'v' }];
ok(renderMatchedRows(oldRow, true), 'renderMatchedRows(舊資料沒有 queries)', 40);
ck(renderMatchedRows(oldRow, true).indexOf('命中查詢') < 0,
   '舊資料（無 queries）不會顯示命中查詢');

console.log('\n[6c] 紀錄列的耗時與模型呼叫次數（為什麼要一分鐘，一眼看到）');
const slowLog = { id: 901, created_at: '2026-10-08T15:57:10Z', channel: 'whatsapp',
                  reply_scope: 'dm', tier: 'rag', label: '自動回復-知識庫命中',
                  detail: '自動回復-知識庫命中（改寫後命中）', question: '有範例嗎？',
                  answer: '有，如下…', reason: 'sent', send_status: 'sent',
                  rewrote: true, rewritten_query: '目標系統接收訊息的範例',
                  elapsed_ms: 48930, llm_calls: 8, llm_seconds: 46.88,
                  has_matched_rows: false };
const slowHtml = ok(logItemHtml(slowLog), 'logItemHtml(慢紀錄)', 120);
has(slowHtml, '48.9s', '顯示總耗時');
has(slowHtml, '8</b> 次', '顯示模型呼叫次數');
has(slowHtml, '改寫問句', '改寫命中會標出改寫問句');
// 舊紀錄沒有這三個欄位 → 不能顯示「耗時 0.0s」也不能炸
const oldLog = Object.assign({}, slowLog, { elapsed_ms: 0, llm_calls: 0,
                                            llm_seconds: 0, rewrote: false,
                                            rewritten_query: '' });
const oldHtml = ok(logItemHtml(oldLog), 'logItemHtml(舊紀錄無用量欄位)', 80);
ck(oldHtml.indexOf('耗時') < 0, '沒有用量資料就不顯示耗時那一行');
ck(oldHtml.indexOf('改寫問句') < 0, '沒有改寫就不顯示改寫問句');
ok(logItemHtml(Object.assign({}, slowLog, { rewrote: true, rewritten_query: '' })),
   'rewrote=true 但沒有改寫問句（多查詢）也要能畫', 80);

console.log('\n[6d] 參數彈窗・C 區提示詞（可在頁面編輯 system prompt）');
const fxPrompt = { key: 'rag_answer_prompt', label: '知識庫問答提示詞（system）',
                   group: 'prompt', type: 'text', help: '強調忠實於召回內容',
                   min: null, max: null, choices: null, env: 'DEEPSEEK_RAG_PROMPT',
                   default: '內建{context}／{sentinel}', value: '目前{context}／{sentinel}',
                   overridden: false, rebuild: false, scope: 'global',
                   required: ['{context}', '{sentinel}'],
                   vars: ['{context}＝檢索到的資料', '{sentinel}＝答不出來的標記'], rows: 18 };
const pHtml = ok(kbParamRowHtml(fxPrompt), 'kbParamRowHtml(提示詞)', 120);
has(pHtml, '<textarea', '提示詞渲染成多行輸入框');
has(pHtml, '{context}', '列出可用變數');
has(pHtml, '必須保留', '標出「必須保留」的變數');
has(pHtml, 'kbFillDefault', '有「帶入內建預設」按鈕');
has(pHtml, '18', 'rows 帶進輸入框高度');
ck(pHtml.indexOf('[object') < 0, '提示詞渲染不會出現 [object Object]');
// 舊 / 壞資料（沒有 required / vars / rows）不能炸、也不能吐 undefined
const pHtml2 = kbParamRowHtml({ key: 'x', label: 'L', type: 'text', help: 'h',
                                env: 'E', value: 'v', scope: 'global' });
ck(pHtml2.indexOf('<textarea') >= 0 && pHtml2.indexOf('undefined') < 0,
   '缺 required/vars/rows 的提示詞不炸、不吐 undefined');
// 表單讀取：text 回原字串（空字串＝清掉覆寫、回內建預設）；數字維持原行為
ck(kbReadParamForm([{ key: 'rag_answer_prompt', type: 'text' }]).rag_answer_prompt === '',
   'kbReadParamForm 對 text 回字串（空 → 回預設）');
ck(kbReadParamForm([{ key: 'top_k', type: 'int' }]).top_k === null,
   'kbReadParamForm 對數字維持原行為（空 → null）');
ck(kbReadParamForm([{ key: 'q', type: 'bool' }]).q === false,
   'kbReadParamForm 對 bool 維持原行為');
// 彈窗必須真的把 C 區與 prompts 分組接上去（用函式原始碼斷言，DOM 樁看不到 innerHTML）
const rpSrc = kbRenderParams.toString();
has(rpSrc, 'C. 提示詞', '參數彈窗有 C 區');
has(rpSrc, 'd.prompts', 'C 區吃後端的 prompts 分組');
has(kbSaveParams.toString(), 'kbReadParamForm(d.prompts)', '儲存會一併送出提示詞');
ck(kbResetParams.toString().indexOf("'prompts'") >= 0,
   '回復預設會一併清掉提示詞覆寫');
let countOk = true;
try { kbCountPrompt('rag_answer_prompt'); } catch (e) { countOk = false; }
ck(countOk, 'kbCountPrompt 在 DOM 樁下不炸');

console.log('\n[7] 紀錄頁');
arState.settings = FX.settings;
arState.logs = FX.logs.logs || [];
arState.openLogId = null;
arState.logDetail = null;
ok(logsTabHtml(), 'logsTabHtml()', 200);
const logOpts = [];
(function () {
  const h = logsTabHtml();
  ['faq', 'table', 'rag', 'miss'].forEach(function (t) {
    if (h.indexOf('value="' + t + '"') >= 0) logOpts.push(t);
  });
})();
ck(logOpts.length === 4, 'tier 篩選四個選項都在', logOpts.join('、'));

if (arState.logs.length) {
  let items = arState.logs.map(logItemHtml).join('');
  ok(items, 'logItemHtml() 全部紀錄', 100);
  const ragLog = { id: 99999, created_at: '2026-10-08T10:00:00', channel: 'whatsapp',
                   reply_scope: 'dm', tier: 'rag', rewrote: true, send_status: 'sent',
                   question: '保固多久', answer: '一年原廠保固', detail: '知識庫命中',
                   reason: 'sent', rewritten_query: '產品保固期限', history_used: 2,
                   has_matched_rows: true };
  const lh = logItemHtml(ragLog);
  ok(lh, 'logItemHtml(知識庫命中)', 100);
  has(lh, 'tag-auto-rag', '用知識庫專屬標籤色');
  has(lh, 'clickable', '帶明細的紀錄可點開');
  has(lh, '檢索明細', '提示可以展開');
  const lh2 = logItemHtml(Object.assign({}, ragLog, { tier: 'miss', has_matched_rows: false,
                                                      send_status: null, answer: '' }));
  hasNot(lh2, 'clickable', '沒有明細的紀錄不可點');
  const lh3 = logItemHtml(Object.assign({}, ragLog, { tier: 'miss', has_matched_rows: true }));
  has(lh3, 'tag-auto-miss', '未命中用未命中標籤');
  ck(lh3.indexOf('clickable') >= 0, '未命中的紀錄也能點開看明細');
} else {
  console.log('  （目前沒有自動回復紀錄，logItemHtml 只驗合成資料）');
  const lh = logItemHtml({ id: 1, created_at: '2026-10-08T10:00:00', channel: 'wechat',
                           reply_scope: 'dm', tier: 'rag', send_status: 'sent',
                           question: 'q', answer: 'a', has_matched_rows: true });
  has(lh, 'tag-auto-rag', '合成紀錄用知識庫標籤');
}

console.log('\n[7b] 展開後的明細（openLogId 命中時）');
// 挑一筆「真的有明細」的紀錄（那才是使用者點得到的列）
const expandable = (arState.logs || []).filter(function (r) { return r.has_matched_rows; })[0];
const openTarget = expandable || { id: 88888, created_at: '', channel: 'wechat', reply_scope: 'dm',
                                   tier: 'rag', send_status: 'sent', question: 'q', answer: 'a',
                                   has_matched_rows: true };
arState.openLogId = openTarget.id;
arState.logDetail = { matched_rows: FX.matched_rows };
const openHtml = arState.logs.length
  ? arState.logs.map(logItemHtml).join('')
  : logItemHtml(openTarget);
ok(openHtml, '展開狀態的 logItemHtml()', 100);
has(openHtml, 'logDetailBox', '展開後有明細容器');
has(openHtml, '收起明細', '箭頭文字切換為收起');
has(openHtml, 'kb-hit', '明細真的畫進去了');
ck(openHtml.indexOf('收起明細') === openHtml.lastIndexOf('收起明細'), '一次只展開一筆');

console.log('\n[7c] 殘留的 openLogId 不該留下空明細框');
arState.openLogId = 424242;          // 不在這批結果裡的 id
arState.logDetail = { matched_rows: FX.matched_rows };
const stale = (arState.logs.length ? arState.logs.map(logItemHtml).join('')
                                   : logItemHtml(openTarget));
ok(stale, 'openLogId 對不上任何列', 100);
hasNot(stale, 'logDetailBox', '沒有孤兒明細框');
hasNot(stale, '收起明細', '沒有殘留的收起箭頭');
arState.openLogId = null;
arState.logDetail = null;

console.log('\n[8] FAQ 分頁（搜尋框 + 內文）');
arState.faqs = FX.faq.faqs || [];
arState.faqTotal = FX.faq.total || 0;
arState.faqQ = '';
let ft = ok(faqTabHtml(), 'faqTabHtml()', 200);
has(ft, 'faqQ', '有搜尋框');
has(ft, '搜尋問題 / 答案 / 關鍵詞', '搜尋框有說明');
ck(ft.indexOf('共 ' + (FX.faq.total || 0) + ' 組') >= 0, '顯示總筆數');
arState.faqQ = '保固';
arState.faqs = (FX.faq.faqs || []).slice(0, 1);
let ft2 = ok(faqTabHtml(), 'faqTabHtml()（搜尋中）', 200);
has(ft2, 'value="保固"', '搜尋字被填回輸入框（不會被重繪清掉）');
has(ft2, '符合', '顯示符合筆數');
has(ft2, '清除', '有清除鈕');
arState.faqQ = '';
arState.faqs = FX.faq.faqs || [];

console.log('\n[9] 自動回復分頁（三層開關 + DeepSeek + 防護）');
arState.settings = FX.settings;
let at = ok(autoreplyTabHtml(), 'autoreplyTabHtml()', 300);
has(at, '三層資料的停啟用', '標題已是三層');
has(at, '第一層：FAQ 問答對', '第一層');
has(at, '第二層：業務表格', '第二層');
has(at, '第三層：知識庫', '第三層');
has(at, "setLayer('rag'", '第三層的開關鈕綁 rag');
const kbLine = kbSummaryText(FX.settings.kb);
ck(at.indexOf(kbLine) >= 0, '顯示知識庫規模摘要', kbLine);
has(at, "'kb'", '第三層有跳去知識庫分頁的按鈕');
ck(at.indexOf('未設定 Key') >= 0 || /\*{4}/.test(at), 'DeepSeek 有顯示 Key 狀態（遮罩）');
has(at, '測試連線', 'DeepSeek 連線測試鈕');
has(at, '同一會話冷卻', '顯示防護（冷卻）設定');

console.log('\n[10] 訊息上的自動回復標記（autoTag）');
const tagCases = [
  [{ auto_reply_state: 'hit', auto_reply_tier: 'faq' }, 'tag-auto-faq', 'FAQ 命中'],
  [{ auto_reply_state: 'hit', auto_reply_tier: 'table' }, 'tag-auto-table', '表格命中'],
  [{ auto_reply_state: 'hit', auto_reply_tier: 'rag' }, 'tag-auto-rag', '知識庫命中'],
  [{ auto_reply_state: 'hit', auto_reply_tier: 'rag',
     auto_reply_detail: '人工確認的／改寫後命中' }, 'tag-auto-rag', '知識庫命中（改寫）'],
  [{ auto_reply_state: 'miss' }, 'tag-auto-miss', '未命中'],
  [{ auto_reply_state: 'failed' }, 'tag-auto-fail', '失敗'],
];
tagCases.forEach(function (c) {
  const h = autoTag(c[0]);
  ck(h.indexOf(c[1]) >= 0, 'autoTag → ' + c[2], h.slice(0, 90));
});
ck(autoTag({ auto_reply_state: '' }) === '', '沒有自動回復狀態時不產生標記');
const ragTag = autoTag({ auto_reply_state: 'hit', auto_reply_tier: 'rag',
                         auto_reply_detail: '人工確認的' });
has(ragTag, '人工確認的', '人工確認的知識庫命中有前綴');
const ragRw = autoTag({ auto_reply_state: 'hit', auto_reply_tier: 'rag',
                        auto_reply_detail: '知識庫：改寫後命中' });
has(ragRw, '·改寫', '改寫後才命中會加註記');

console.log('\n[11] 第三層規模摘要（各種狀態都要有話說）');
ck(kbSummaryText(null).indexOf('尚未讀取') >= 0, 'null → 尚未讀取');
ck(kbSummaryText({ error: 'db down' }).indexOf('失敗') >= 0, 'error → 說明失敗但不影響前兩層');
ck(kbSummaryText({ collections: 0 }).indexOf('跳過') >= 0, '沒有知識庫 → 說會跳過');
ck(kbSummaryText({ collections: 2, documents: 10, ready_documents: 9, chunks: 21 })
     .indexOf('9 / 10') >= 0, '有資料 → 顯示已上線 / 全部');
"""

RUNNER = r"""
console.log('\n[12] 端到端：整份分頁渲染後再檢查一次');
let finalHtml = '';
try {
  arState.settings = FX.settings;
  arState.kb = FX.overview;
  arState.kbDocs = FX.documents.documents || [];
  arState.faqs = FX.faq.faqs || [];
  arState.logs = FX.logs.logs || [];
  ['autoreply', 'faq', 'table', 'kb', 'hittest', 'logs'].forEach(function (t) {
    arState.tab = t;
    let h;
    if (t === 'kb') h = kbTabHtml();
    else if (t === 'hittest') h = hittestTabHtml();
    else if (t === 'faq') h = faqTabHtml();
    else if (t === 'table') h = tableTabHtml();
    else if (t === 'logs') h = logsTabHtml();
    else h = autoreplyTabHtml();
    const bad = badHtml(h);
    const bal = divBalance(h);
    ck(typeof h === 'string' && h.length > 100 && bad.length === 0 && bal.ok,
       '六個分頁全渲染：' + t, h.length + ' 字' + (bad.length ? ' 壞字串=' + bad.join('、') : '')
         + (bal.ok ? '' : ' div 不成對 ' + bal.o + '/' + bal.c));
    finalHtml += h;
  });
  // 知識庫四個頁面各自都要乾淨（一頁一件事，四頁都要能被渲染出來）
  arState.tab = 'kb';
  arState.kbCollection = CID;
  arState.kbUngrouped = false;
  arState.kbDocs = [docMine, docOther, docNoCol];
  [['list', null], ['docs', null], ['new', null],
   ['doc', FX.document || { doc: docMine, stats: {}, chunks: [] }]].forEach(function (pair) {
    arState.kbView = pair[0];
    if (pair[1]) arState.kbDoc = pair[1];
    const h = kbTabHtml();
    const bad = badHtml(h);
    const bal = divBalance(h);
    ck(typeof h === 'string' && h.length > 100 && bad.length === 0 && bal.ok,
       '知識庫四頁：' + pair[0], h.length + ' 字' + (bad.length ? ' 壞字串=' + bad.join('、') : '')
         + (bal.ok ? '' : ' div 不成對 ' + bal.o + '/' + bal.c));
  });
  arState.kbView = 'list';
  arState.kbDoc = null;
} catch (e) {
  ck(false, '六個分頁全渲染時丟出例外', String(e && e.stack ? e.stack.split('\n')[0] : e));
}

console.log('\n' + '='.repeat(72));
console.log(' 結果：' + (__FAILS.length ? __FAILS.length + ' 項失敗' : '全部通過 ✅'));
__FAILS.forEach(function (f) { console.log('   ✗ ' + f); });
console.log('='.repeat(72));
process.exit(__FAILS.length ? 1 : 0);
"""


def extract_ui_js() -> str:
    path = os.path.join(HERE, "ui", "console_ui.html")
    txt = io.open(path, encoding="utf-8").read()
    js = txt.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    return js


def check_kb_new_modal_markup() -> None:
    """「新增知識庫」彈窗的**靜態節點**（JS 抽取只看 <script>，抓不到這段 HTML）。

    驗到的是「這個彈窗真的存在於頁面裡」—— 沒有它，`openKbNew()` 會靜靜地什麼都不做。
    形狀要求（小jo 指定）：兩個單選（圖片／文檔）→ 庫名 → 添加／取消。
    """
    path = os.path.join(HERE, "ui", "console_ui.html")
    txt = io.open(path, encoding="utf-8").read()
    ok_ = True

    def need(cond, label, extra=""):
        nonlocal ok_
        check(cond, label, extra)
        if not cond:
            ok_ = False

    need('id="kbNewOverlay"' in txt, '彈窗節點 #kbNewOverlay 存在（靜態）')
    need('name="kbNewKind"' in txt, '有類型單選 input[name=kbNewKind]')
    need('value="image"' in txt and 'value="doc"' in txt, '單選有兩個值：image / doc')
    box = txt.split('id="kbNewOverlay"', 1)[-1].split('</div>\n</div>', 1)[0]
    need(box.count('type="radio"') == 2, '正好兩個單選（圖片 / 文檔）',
         f"找到 {box.count('type=\"radio\"')} 個")
    need('圖片知識庫' in box and '文檔知識庫' in box, '兩個選項的文字是「圖片知識庫 / 文檔知識庫」')
    need('id="kbNewName"' in box, '庫名輸入框 #kbNewName 存在')
    need('id="kbNewAddBtn"' in box, '「添加」按鈕 #kbNewAddBtn 存在')
    need('>添加<' in box, '按鈕文字是「添加」（不是「確定」）')
    need('>取消<' in box, '按鈕文字是「取消」')
    need('type="checkbox"' not in box, '不是勾選框（是單選）')
    need('確定' not in box and '是否' not in box, '沒有「是/否」這種問句文字')
    return ok_


def main() -> int:
    if not os.path.exists(NODE):
        print(f"  ⚠ 找不到 node：{NODE}")
        return 2

    print("=" * 72)
    print(f" 客服工作台前端自檢（真實 payload → 真實渲染函式）→ {BASE}")
    print("=" * 72)

    # 靜態 HTML 本體（<script> 以外的部分）也要驗 —— JS 抽取涵蓋不到彈窗的節點
    print("\n[0] 靜態頁面節點")
    check_kb_new_modal_markup()

    fx = collect_fixtures()
    ids = seed_log_fixture(fx)
    build_synthetic(fx)

    try:
        ui_js = extract_ui_js()
        harness = (
            DOM_STUB
            + "\nconst FX = " + json.dumps(fx, ensure_ascii=False, default=str) + ";\n"
            # 上面那些渲染函式都是函式宣告，會提升；但 arState / _kbParamCache 是 const，
            # 要等整份 script 跑完才存在 → 所以檢查碼放在最後。
            + ui_js
            + "\n" + CHECKS + "\n" + RUNNER
        )

        tmpdir = tempfile.mkdtemp(prefix="ui_check_")
        path = os.path.join(tmpdir, "run.js")
        io.open(path, "w", encoding="utf-8").write(harness)
        try:
            proc = subprocess.run([NODE, path], capture_output=True, timeout=180)
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            print(out, end="")
            if err.strip():
                print("\n--- node stderr ---")
                print(err[:3000])
            if proc.returncode != 0 and "結果：" not in out:
                FAIL.append("node 執行失敗")
        finally:
            try:
                os.remove(path)
                os.rmdir(tmpdir)
            except Exception:
                pass
    finally:
        cleanup_logs(ids)

    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
