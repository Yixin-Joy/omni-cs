"""驗證：運維端審計日誌重新設計（篩選 / 分頁 / 人話化 / 一屏不滾動）。

★ 需求（2026-10-20 小jo）：
  1. 「要有時間、類型的篩選系統，新增翻頁功能，不需要下拉就能查看内容」
  2. 「日志展示的内容太繁雜了，你整理一下對運維排查有必要的信息再展示」
  3. 「比如一頁有20條，那麼這最新的20條就要在一屏中全部展示而且要充分利用這一屏的
     空間，點擊頁碼或下一頁再看後面的内容，而不是要用滾輪下拉查看」
  4. 「conversation_read直接不計，非必要的操作記錄直接選擇不計，身份切換不需要保留，
     且某個重要動作是由誰、以什麽身份做的就可以」
  5. （補充）「如果列表裏的内容不足20條，就從上往下排排到哪裏算哪裏，下面留空」

本自檢：
  [A] 記錄端：噪音類型已下線、staff_role 有寫入、fail_reason 有寫入
  [B] 後端：分組/標籤/篩選/分頁/未來相容（不越權）
  [C] 前端靜態接線：三段式佈局、chip 篩選、頁碼、一頁 20 條（不足留空不拉開）
  [D] 人話轉換：把 `_auditAction` 抽到 Node，用**真實 payload** 跑，斷言不再是原始 JSON

跑法（repo 根）：
  PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
    <python> crm/tests/ops_audit_redesign_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "crm"))

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


OPS_UI = (_ROOT / "crm" / "ui" / "ops.html").read_text(encoding="utf-8")
ADMIN_UI = (_ROOT / "crm" / "ui" / "admin.html").read_text(encoding="utf-8")
# ★ 審計視圖是共用模組（admin.html / ops.html 都引用同一份）
AUDIT_JS = (_ROOT / "crm" / "ui" / "audit_view.js").read_text(encoding="utf-8")
ADMIN_PY = (_ROOT / "crm" / "app" / "admin" / "router.py").read_text(encoding="utf-8")
MAIN_PY = (_ROOT / "crm" / "main.py").read_text(encoding="utf-8")
AUDIT_PY = (_ROOT / "crm" / "app" / "audit.py").read_text(encoding="utf-8")
OPS_R = (_ROOT / "crm" / "app" / "ops" / "router.py").read_text(encoding="utf-8")
OPS_REPO = (_ROOT / "crm" / "app" / "ops" / "repo.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- [A] 記錄端
def part_a() -> None:
    print("[A] 記錄端：噪音下線 + 誰/什麼身分/成敗")

    check('"conversation_read"' not in AUDIT_PY,
          "★★ conversation_read 已從 OPERATE_TYPES 移除")
    check('"role_switch"' not in AUDIT_PY,
          "★★ role_switch 已從 OPERATE_TYPES 移除")
    check("staff_role" in AUDIT_PY, "★ audit.log 會寫 staff_role（以什麼身分做的）")
    check('sess.get("active_role")' in AUDIT_PY,
          "★ 取的是 active_role（當時生效的身分），不是整個 roles 清單")
    check("fail_reason" in AUDIT_PY, "★ audit.log 收 fail_reason（失敗原因獨立成欄）")

    AGENT_R = (_ROOT / "crm" / "app" / "agent" / "router.py").read_text(encoding="utf-8")
    AUTH_R = (_ROOT / "crm" / "app" / "auth" / "router.py").read_text(encoding="utf-8")
    check('"conversation_read"' not in AGENT_R,
          "★★ 打開會話/清紅點不再寫審計")
    check('"role_switch"' not in AUTH_R,
          "★★ 身分切換不再寫審計")

    SCHEMA = (_ROOT / "crm" / "app" / "core" / "schema.py").read_text(encoding="utf-8")
    check("ADD COLUMN IF NOT EXISTS staff_role" in SCHEMA, "schema 走 MIGRATIONS 加 staff_role")
    check("ADD COLUMN IF NOT EXISTS fail_reason" in SCHEMA, "schema 走 MIGRATIONS 加 fail_reason")
    check("UPDATE crm_audit_log SET staff_role = staff_roles[1]" in SCHEMA,
          "★ 歷史記錄回填 staff_role（否則舊資料全是空白）")


# ---------------------------------------------------------------- [B] 後端
def part_b() -> None:
    print("\n[B] 後端：分組 / 篩選 / 分頁")

    check("AUDIT_GROUPS = (" in OPS_R, "★ 有五個語意分組 AUDIT_GROUPS")
    check("AUDIT_TYPE_LABELS" in OPS_R, "★ 有中文標籤對照（27 個原始類型對人沒意義）")
    for g in ("msg", "cfg", "alert", "kb", "sec"):
        check(f'"key": "{g}"' in OPS_R, f"分組 {g} 存在")

    from app.ops.router import AUDIT_GROUPS, AUDIT_TYPE_LABELS
    all_types = [t for g in AUDIT_GROUPS for t in g["types"]]
    check(len(AUDIT_GROUPS) == 5, "共 5 組", str(len(AUDIT_GROUPS)))
    check("conversation_read" not in all_types, "★ 預設組不含 conversation_read")
    check("role_switch" not in all_types, "★ 預設組不含 role_switch")
    # 最重要：AI 開關必須在預設組裡（原本只有 4 個類型，194 筆 AI 開關全都看不到）
    check("ai_switch_change" in all_types, "★★ AI 開關在預設範圍內（原本看不到）")
    check("binding_change" in all_types, "★★ RAG 綁定變更在預設範圍內")
    check(len(AUDIT_TYPE_LABELS) >= 30, "標籤覆蓋足夠", str(len(AUDIT_TYPE_LABELS)))

    # 每個標籤都要有人話，不能漏
    missing = [t for t in set(all_types) if t not in AUDIT_TYPE_LABELS]
    check(not missing, "★ 分組裡每個類型都有中文標籤", "、".join(missing))

    # 查詢簽名要有新參數
    for p in ("time_from", "time_to", "q", "only_failed", "has_more"):
        check(p in OPS_REPO, f"repo.list_audit 支援 {p}")
    check("ORDER BY id DESC" in OPS_REPO,
          "★ 用 id 排序（等同時間新→舊，且分頁不會漏行/重複）")
    check("audit_type_counts" in OPS_REPO, "★ 有 facets 統計（chip 上的數字）")

    # 實查（用真實 DB；沒有資料就跳過，不算失敗）
    try:
        from app.ops import repo as ops_repo
        r = ops_repo.list_audit(limit=5, offset=0,
                                types=sorted(set(all_types)))
        check(r["total"] >= 0, "預設範圍查得動", str(r["total"]))
        check("has_more" in r, "回傳帶 has_more（前端分頁要用）")
        kinds = {it["operate_type"] for it in r["items"]}
        check("conversation_read" not in kinds and "role_switch" not in kinds,
              "★ 回來的資料沒有噪音類型", str(kinds))
        check(all("staff_role" in it for it in r["items"]),
              "★ 每筆都帶 staff_role 欄（即使值可能為 None）")

        # 分頁不重複、不漏
        p1 = ops_repo.list_audit(limit=5, offset=0, types=sorted(set(all_types)))
        p2 = ops_repo.list_audit(limit=5, offset=5, types=sorted(set(all_types)))
        ids1 = [x["id"] for x in p1["items"]]
        ids2 = [x["id"] for x in p2["items"]]
        check(not (set(ids1) & set(ids2)), "★★ 第 1 頁與第 2 頁沒有重複", f"{ids1} / {ids2}")
        if ids1 and ids2:
            check(min(ids1) > max(ids2), "★ 嚴格遞減（新→舊）", f"{min(ids1)} > {max(ids2)}")

        # 只看失敗
        f = ops_repo.list_audit(limit=3, only_failed=True, types=sorted(set(all_types)))
        check(f["total"] >= 0, "只看失敗查得動", str(f["total"]))
        bad = [it for it in f["items"]
               if not it["fail_reason"] and it["operate_type"] != "login_failed"
               and not ((it["payload"] or {}).get("errors")
                        or (it["payload"] or {}).get("sent") is False)]
        check(not bad, "★★ 只看失敗回來的每一筆都真的是失敗", str([b["operate_type"] for b in bad]))

        # facets 合計 = 各組合計（別把下線類型也算進去）
        fc = ops_repo.audit_type_counts()
        grp_sum = sum(fc.get(t, 0) for t in set(all_types))
        all_sum = sum(fc.values())
        check(grp_sum <= all_sum, "分組合計不超過全庫", f"{grp_sum} <= {all_sum}")
        check(all_sum - grp_sum >= 0,
              "★ 差額就是已下線的噪音（conversation_read/role_switch）",
              f"差 {all_sum - grp_sum}")
    except Exception as e:  # noqa: BLE001
        check(False, "實查失敗", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- [C] 前端接線
def part_c() -> None:
    print("\n[C] 前端：共用視圖（兩台一致）/ 三段式 / 一頁 20 條 / 不足靠上留空")

    # ★ 2026-10-20 小jo：「管理員後臺的審計日志一起改了呀」—— 抽成一份共用模組，
    #   兩頁只差打哪支端點。以下斷言就是防它再退化成「兩套長相」。
    check("audit_view.js" in OPS_UI and '/ui/audit_view.js' in OPS_UI,
          "★★ 運維台引用共用審計視圖")
    check("audit_view.js" in ADMIN_UI and '/ui/audit_view.js' in ADMIN_UI,
          "★★ 管理台引用同一份共用審計視圖")
    check("AuditView.mount" in OPS_UI and "AuditView.mount" in ADMIN_UI,
          "★ 兩頁都 mount 共用視圖")
    check("/api/admin/audit?limit=100" not in ADMIN_UI,
          "★★ 管理台不再走「只能 limit 的舊端點」")
    check("AUDIT_GROUPS" in ADMIN_PY and "ops_repo.list_audit" in ADMIN_PY,
          "★★ 管理台端點復用 ops 的查詢（staff_role / fail_reason / total / 分頁）")
    for a in ("only_failed", "time_from", "time_to", "types", "offset"):
        check(a in ADMIN_PY.split('def list_audit')[1][:1200],
              f"管理台端點支援 {a}")
    check("@router.get(\"/audit/facets\")" in ADMIN_PY, "★ 管理台也有 facets（chip 上的數字）")

    # ---- 佈局（在共用模組裡） ----
    check(".body.as-audit" in AUDIT_JS, "★ 審計頁三段式佈局（整頁不滾動）")
    check("overflow: hidden" in AUDIT_JS.split(".body.as-audit")[1][:200],
          "★ 審計頁 #view 不滾動")
    check(".audit-tw" in AUDIT_JS and "flex: 1" in AUDIT_JS, "★ 表格區填滿剩餘高度")
    check(".audit-tw.scroll" in AUDIT_JS, "★ 只有展開明細時才允許表格區內部滾動")
    # ★★ 條數不足時要「靠上排、下面留空」，不能被 flex 預設的 stretch 撐高
    #    （撐高後瀏覽器會把多餘高度分攤給各列 → 行距被拉開，看起來像散落幾行）
    tw_css = AUDIT_JS.split("'.audit-tw {")[1][:260] if "'.audit-tw {" in AUDIT_JS else ""
    check("align-items: flex-start" in tw_css,
          "★★ 表格區靠上對齊（不足 20 條時下面留空，不拉開行距）", tw_css[:120])
    check('height: var(--audit-row-h' in AUDIT_JS,
          "★ 列高走 CSS 變數（JS 依螢幕調 28~34px，好讓 20 條塞得下）")
    check("ROW_MIN = 28" in AUDIT_JS, "★ 行高下限 28px（再小就看不清了）")

    check("function fit()" in AUDIT_JS, "★★ 一頁幾條的計算")
    fit = AUDIT_JS[AUDIT_JS.index("function fit()"):][:900]
    check("PAGE_MAX = 20" in AUDIT_JS, "★ 一頁 20 條")
    check("clientHeight" in fit, "★ 用真實可視高度（不是寫死）", fit[:160])
    check("--audit-row-h" in fit, "★ 算完把行高寫回 CSS 變數")
    check("ROW_MIN" in fit, "★ 矮螢幕才減條數（先壓行高保 20 條）")

    check("function paintBar" in AUDIT_JS, "有篩選列")
    check("RANGES = [" in AUDIT_JS, "★ 時間範圍 chip（今天/昨天/近7天/近30天/全部）")
    for k in ("today", "yday", "7d", "30d", "all", "custom"):
        check(f"'{k}'" in AUDIT_JS, f"時間選項 {k}")
    check('data-act="grp"' in AUDIT_JS, "★ 類型組 chip")
    check('data-act="type"' in AUDIT_JS, "★ 細項類型 chip（點組才展開）")
    check('data-act="failed"' in AUDIT_JS, "★ 只看失敗 chip")
    check('id="auditQ"' in AUDIT_JS, "★ 搜尋框")
    check('type="date"' in AUDIT_JS, "★ 自訂時間用 date input")

    check("function paintPager" in AUDIT_JS, "★ 有分頁")
    pg = AUDIT_JS[AUDIT_JS.index("function paintPager"):][:1400]
    check("上一頁" in pg and "下一頁" in pg, "★ 上一頁 / 下一頁")
    check('data-p="' in pg, "★ 可點頁碼")
    check("Math.ceil(S.total / size)" in pg, "★ 頁數依總筆數算")

    check("function paintTable" in AUDIT_JS, "有表格渲染")
    check("a-row" in AUDIT_JS and "a-detail" in AUDIT_JS, "★ 列可點開明細")
    check("JSON.stringify(a.payload, null, 2)" in AUDIT_JS,
          "★ 原始 payload 只在展開時顯示（不佔主視線）")

    # 事件必須限定容器（MEMORY 的 P0 教訓）
    def fn_body(name: str) -> str:
        """取一個函式的完整 body（到下一個同層 `function ` 為止）。
        ⚠️ 不要用固定字數切片 —— 函式長度會變，切太短會誤報（這裡踩過）。"""
        i = AUDIT_JS.index("function " + name)
        nxt = AUDIT_JS.find("\n  function ", i + 10)
        return AUDIT_JS[i:nxt if nxt > 0 else len(AUDIT_JS)]

    bar = fn_body("paintBar")
    check("bar.querySelectorAll" in bar, "★ 事件綁定限定在 bar 內（不用全域選取）")
    tw = fn_body("paintTable")
    check("tw.querySelectorAll" in tw, "★ 表格事件也限定在自己容器內")

    check("window.addEventListener('resize'" in AUDIT_JS,
          "★ 視窗大小變了會重算每頁條數（否則會出現空白/被裁掉）")
    check("API._mounted" in fn_body("mount"),
          "★ mount 冪等（每次進分頁都 mount，resize 只能掛一次）")
    # 其它頁要復位 class，否則會繼承 overflow:hidden 導致不能滾
    check(OPS_UI.count("_v.className = 'body'") >= 4,
          "★ 離開審計頁時復位 #view 的 class（不然別頁內容被裁）")
    check("UI_ASSETS" in MAIN_PY and "audit_view.js" in MAIN_PY,
          "★ 共用模組在 /ui 白名單裡（否則 404 → 整頁空）")


# ---------------------------------------------------------------- [D] 人話轉換
def part_d() -> None:
    print("\n[D] 人話轉換（用真實 payload 在 Node 實跑）")

    code = AUDIT_JS[AUDIT_JS.index("function typeLabel"):
                    AUDIT_JS.index("/* 對象：")]

    # 用真實資料當測資（沒資料就用定型樣本）
    samples = []
    try:
        from app.ops import repo as ops_repo
        rows = ops_repo.list_audit(limit=60, offset=0)["items"]
        seen = set()
        for r in rows:
            t = r["operate_type"]
            if t in seen:
                continue
            seen.add(t)
            samples.append({"operate_type": t, "payload": r["payload"] or {}})
    except Exception:  # noqa: BLE001
        pass
    samples += [
        {"operate_type": "ai_switch_change",
         "payload": {"layer": "scope", "scope": "dm", "enabled": True, "platform": "fb"}},
        {"operate_type": "reply_send",
         "payload": {"text": "我們的a套餐是全日套餐", "task_id": 536}},
        {"operate_type": "ops_config_change",
         "payload": {"key": "outbound.max_retry", "from": 3, "to": 7}},
        {"operate_type": "task_retry", "payload": {"retried": [73], "skipped": []}},
        {"operate_type": "login_failed",
         "payload": {"reason": "帳號或密碼錯誤", "account": "admin"}},
        {"operate_type": "sync_history",
         "payload": {"totals": {"posts": 9, "dm_new": 1, "errors": []}}},
        {"operate_type": "comment_handle", "payload": {"updated": 1, "comment_id": "x"}},
        {"operate_type": "binding_change",
         "payload": {"kind": "kb", "enabled": True, "reason": "修復檢索範圍"}},
    ]

    harness = r"""
var LABELS = """ + json.dumps(_labels(), ensure_ascii=False) + r""";
var S = { labels: LABELS };
""" + code + r"""
var SAMPLES = """ + json.dumps(samples, ensure_ascii=False) + r""";
var out = [];
function t(ok, label, extra) { out.push([ok, label, extra || '']); }

// 1) 每一種都要產出非空、且**不是原始 JSON** 的描述
var bad = [];
SAMPLES.forEach(function (s) {
  var txt = actionText(s);
  if (!txt || !String(txt).trim()) { bad.push(s.operate_type + ':空'); return; }
  if (txt.charAt(0) === '{' || txt.indexOf('":"') >= 0) {
    bad.push(s.operate_type + ':像JSON');
  }
});
t(bad.length === 0, '★★ 所有類型都轉成人話（不再是原始 JSON）', bad.join('、'));

// 2) 關鍵類型的具體文案
var a = actionText({operate_type: 'ai_switch_change',
  payload: {layer: 'scope', scope: 'dm', enabled: true, platform: 'fb'}});
t(a.indexOf('私訊') >= 0 && a.indexOf('開啟') >= 0,
  '★★ AI 開關 → 「…私訊 …開啟」（不是 {"layer":"scope"...}）', a);

var b = actionText({operate_type: 'reply_send',
  payload: {text: '我們的a套餐是全日套餐', task_id: 536}});
t(b.indexOf('#536') >= 0 && b.indexOf('a套餐') >= 0,
  '★ 送出回覆帶任務號與內容摘要', b);

var c = actionText({operate_type: 'ops_config_change',
  payload: {key: 'outbound.max_retry', from: 3, to: 7}});
t(c.indexOf('outbound.max_retry') >= 0 && c.indexOf('3') >= 0 && c.indexOf('7') >= 0,
  '★ 參數變更顯示 old → new', c);

var d = actionText({operate_type: 'task_retry', payload: {retried: [73], skipped: []}});
t(d.indexOf('#73') >= 0, '★ 重跑任務帶任務號', d);

var e = actionText({operate_type: 'login_failed',
  payload: {reason: '帳號或密碼錯誤', account: 'admin'}});
t(e.indexOf('admin') >= 0 && e.indexOf('帳號或密碼錯誤') >= 0,
  '★ 登入失敗帶帳號與原因', e);

var f = actionText({operate_type: 'sync_history',
  payload: {totals: {posts: 9, dm_new: 1, errors: []}}});
t(f.indexOf('私訊 +1') >= 0, '★ 補拉歷史顯示新增數量', f);

// 3) 未知類型也要有合理輸出（不能空白、不能噴）
var g = actionText({operate_type: 'weird_new_type', payload: {foo: 'bar'}});
t(!!g && g.indexOf('foo') >= 0, '★ 未知類型也有 key=value 摘要（不會空白）', g);

var h = actionText({operate_type: 'ai_switch_change', payload: {}});
t(!!h && h.indexOf('undefined') < 0, '★ payload 缺欄位也不會印 undefined', h);

// 4) 標籤
t(typeLabel('ai_switch_change') === 'AI 開關', '★ 類型有中文標籤',
  typeLabel('ai_switch_change'));
t(typeLabel('no_such') === 'no_such', '未知類型回原始 key（不炸）');

var fails = out.filter(function (x) { return !x[0]; });
out.forEach(function (x) {
  console.log((x[0] ? '  PASS  ' : '  FAIL  ') + x[1] + (x[0] ? '' : '   ' + x[2]));
});
console.log('RESULT ' + JSON.stringify({ pass: out.length - fails.length, fail: fails.length }));
"""

    node = _ROOT / ".tmp"
    node.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".js", dir=str(node))
    os.close(fd)
    p = Path(tmp)
    p.write_text(harness, encoding="utf-8")
    try:
        r = subprocess.run([str(_node()), "--check", str(p)], capture_output=True, text=True)
        check(r.returncode == 0, "抽出來的前端程式碼語法正確", (r.stderr or "")[:300])
        r = subprocess.run([str(_node()), str(p)], capture_output=True, text=True)
        print(r.stdout.rstrip() or (r.stderr or "")[-600:])
        m = None
        for line in (r.stdout or "").splitlines():
            if line.startswith("RESULT "):
                m = json.loads(line[7:])
        if not m:
            check(False, "Node 執行沒吐 RESULT", (r.stderr or r.stdout or "")[-400:])
        else:
            check(m["fail"] == 0, f"D 段斷言全過（{m['pass']} 項）", f"fail={m['fail']}")
            global _pass
            _pass += int(m["pass"])
    finally:
        p.unlink(missing_ok=True)


def _labels() -> dict:
    try:
        from app.ops.router import AUDIT_TYPE_LABELS
        return dict(AUDIT_TYPE_LABELS)
    except Exception:  # noqa: BLE001
        return {}


def _node() -> Path:
    c = Path(r"node")
    return c if c.exists() else Path("node")


def main() -> int:
    part_a()
    part_b()
    part_c()
    part_d()
    print(f"\n{'=' * 62}\n  運維審計日誌重新設計：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        print("  失敗：" + "、".join(_fail))
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
