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

HERE = os.path.dirname(os.path.abspath(__file__))
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
    import autoreply
    import autoreply_db
    import kb_search

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
        import autoreply_db
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
has(tabsHtml, '命中測試（逐層勾選，只影響這一次）', '標題說明「只影響這次」');
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
// 知識庫查詢改寫：三態選擇（跟隨設定 / 這次強制開 / 這次強制關）
has(tabsHtml, '知識庫查詢改寫', '有查詢改寫的選擇');
has(tabsHtml, '依知識庫設定', '查詢改寫預設「依知識庫設定」');
has(tabsHtml, 'arState.ktRewrite=this.value', '選擇會寫進 arState.ktRewrite');
ck(arState.ktRewrite === '', '查詢改寫預設不覆寫（不影響正式設定）',
   JSON.stringify(arState.ktRewrite));

console.log('\n[4b] 試跑卡片已從「自動回復」頁搬走（設計稿 §11.2）');
const arHtml = autoreplyTabHtml();
hasNot(arHtml, 'arTestResult', '自動回復頁不再有試跑結果區');
hasNot(arHtml, 'arTestText', '自動回復頁不再有試跑輸入框');
has(arHtml, '去命中測試', '自動回復頁有指路按鈕');
has(arHtml, "openSettings('hittest')", '指路按鈕真的切到命中測試分頁');
ck(typeof testEngine === 'undefined', 'testEngine() 已移除（不留死碼）');

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
has(withQ, '知識庫實際檢索的查詢', '多查詢時列出實際用的查詢');
has(withQ, '原句', '標出哪一條是原問句');
has(withQ, '改寫1', '標出改寫句');
ck(ktResultHtml(Object.assign({}, FX.test_rag_hit, { rag_queries: ['只有一句'] }))
     .indexOf('知識庫實際檢索的查詢') < 0,
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
has(at, '管理知識庫', '有跳分頁的按鈕');
ck(at.indexOf('未設定 Key') >= 0 || /\*{4}/.test(at), 'DeepSeek 有顯示 Key 狀態（遮罩）');
has(at, '測試連線', 'DeepSeek 連線測試鈕');
has(at, '同一會話冷卻', '顯示防護（冷卻）設定');
has(at, '各渠道成效', '有成效統計卡片');

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
    path = os.path.join(HERE, "console_ui.html")
    txt = io.open(path, encoding="utf-8").read()
    js = txt.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    return js


def main() -> int:
    if not os.path.exists(NODE):
        print(f"  ⚠ 找不到 node：{NODE}")
        return 2

    print("=" * 72)
    print(f" 客服工作台前端自檢（真實 payload → 真實渲染函式）→ {BASE}")
    print("=" * 72)

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
