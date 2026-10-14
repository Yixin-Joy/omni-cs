"""前端靜態自檢：JS 語法 + 小jo 的三個 UI 約定 + 身分切換元件 + 貼文彈窗。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/ui_check.py

檢查項：
  [1] 每個 ui/*.html 的內嵌 JS 與 ui/*.js 都能被 Node 解析（語法錯誤會當場炸）
  [2] 不得出現 `confirm(`（小jo 明確要求：永遠不要「是/否」彈窗）
  [3] 頁面要少字 —— 定義了 infotip 就必須真的用到
  [4] 彈窗必須是靜態節點 —— 檢查 .ov 節點寫在 <script> 之前
  [5] 身分切換 —— 選單節點是靜態節點、只有多身分才給入口、登入頁有兩步流程
  [6] 貼文特殊配置彈窗（#ovPost）—— 靜態節點、三組開關+勾選、送出鍵名齊全
  [7] markdown 圖片渲染 —— `mdImages()` 是唯一渲染器，且「顯示回覆／內文」都走它

⚠️ 這支只做**靜態**檢查（讀 HTML 原始碼）。「真的把 payload 餵進渲染函式」那條路
   由 `tests/console_render_check.py` 負責（Node + 最小 DOM 樁）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "crm" / "ui"
NODE = r"node"

fails: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(msg)
    return bool(cond)


def scripts_of(html: str) -> list[str]:
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def node_check(js: str, label: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    r = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True, encoding="utf-8")
    check(r.returncode == 0,
          f"{label} 語法通過" + ("" if r.returncode == 0 else "：" + (r.stderr or "")[:300]))
    Path(tmp).unlink(missing_ok=True)


# ======================================================================
# [6] 貼文特殊配置彈窗（#ovPost）
# ======================================================================

def check_post_modal(html: str, js: str, first_script: int) -> None:
    """貼文彈窗的三條硬約束。

    為什麼要單獨驗：這個彈窗是**唯一**在重繪區（#spBody / #view）之外的動態表單，
    而它會被 30 秒輪詢重繪波及 —— 放進 <script> 裡動態產生就一定會被洗掉。
    另外三組（FAQ／表格／知識庫）的鍵名要跟後端對齊（後端第 3 組叫 `rag`，
    前端叫 `kb` → 靠 `layerKeyOf()` 轉），錯一個字就是「存了但沒生效」。
    """
    if 'id="ovPost"' not in html:
        return

    # ---- 靜態節點 ----
    check(html.find('id="ovPost"') < first_script,
          "[ovPost] 彈窗是靜態節點（寫在 <script> 之前，輪詢重繪洗不掉）")
    box = html.split('id="ovPost"', 1)[1].split("<script", 1)[0]
    check('id="postBody"' in box and 'id="postEnabled"' in box,
          "[ovPost] 有內文容器 #postBody 與總開關 #postEnabled")
    check('id="postSave"' in box and 'id="postReset"' in box,
          "[ovPost] 有「儲存」與「移除特殊配置」兩顆按鈕")

    # ---- 三組的鍵名 ----
    m = re.search(r"var POST_KINDS = \[([^\]]*)\]", js)
    kinds = re.findall(r"'([^']+)'", m.group(1)) if m else []
    check(kinds == ["faq", "table", "kb"],
          "[ovPost] 前端三組鍵名 = faq / table / kb（順序固定）", f"實得 {kinds}")
    check("function layerKeyOf(" in js and "=== 'kb' ? 'rag'" in js,
          "[ovPost] layerKeyOf() 把前端 kb 轉成後端的 rag（鍵名不對＝存了沒生效）")
    for k in ("faq", "table", "rag"):
        check(f"{k}:" in js, f"[ovPost] LAYER_LABEL 有 {k} 的中文標籤")

    # ---- 每組都要有「開關 + 勾選清單」----
    check("data-pgroup" in js and "data-pck" in js,
          "[ovPost] 每組都有層開關（data-pgroup）與勾選項（data-pck）")
    check(re.search(r"state\.postLayers\[k\] \? '啟用' : '停用'", js) is not None,
          "[ovPost] 層開關有明確的「啟用／停用」文字（不是只靠顏色）")
    check("var picked = state.postChecked[k] || [];" in js,
          "[ovPost] 勾選狀態從 state 讀（開關切換時先收回 DOM，重繪不洗掉使用者的勾選）")
    check(re.search(r"POST_KINDS\.forEach\(function \(k\) \{\s*state\.postChecked\[k\] = ", js)
          is not None,
          "[ovPost] 切換開關前會先把 DOM 上的勾選收回 state")

    # ---- 送出鍵名齊全 ----
    cp = js.split("function collectPost()", 1)[-1].split("\n  }", 1)[0]
    check("enabled: state.postEnabled" in cp, "[ovPost] 送出時帶 enabled")
    check("faq:" in cp and "table:" in cp and "kb:" in cp,
          "[ovPost] 送出時 layers 的 faq / table / kb 三鍵齊全")
    check("business_id" in cp and "post_id" in cp and "bp_account_id" in cp,
          "[ovPost] 送出時帶 business_id / post_id / bp_account_id（後端要靠它定位）")

    # ---- 後端鐵則：開關開著就不能空勾 ----
    router = ROOT / "crm" / "app" / "agent" / "console_router.py"
    rtxt = router.read_text(encoding="utf-8")
    check("至少要勾一個項目（或把開關關掉）" in rtxt,
          "[ovPost] 後端擋住「開關開著卻一項都沒勾」（回 400，前端顯示在 #ovPostErr）")
    check("payload.enabled" in rtxt and "not bindings[k]" in rtxt,
          "[ovPost] 那段閘門只在 enabled 時檢查（停用的貼文允許空集合）")


# ======================================================================
# [7] markdown 圖片渲染
# ======================================================================

def check_md_images(html: str, js: str) -> None:
    """`mdImages()` 是唯一的 markdown 圖片渲染器 —— 每處顯示「回覆／內文」都要走它。

    病灶：回覆可以是 `![題注](/media/<sha16>.png)`（圖文送出時文字被剝掉、圖留下）。
    漏一處就會在畫面上看到 `![A款耳機](/media/ab12….png)` 這種原始字串。

    ⚠️ 分兩層：**通用**（任何有 `mdImages` 的頁面都驗）＋ **客服工作台專屬**
       （只有 `#ovPost` 那一頁才驗那 4 個落點）—— `ops.html` 也有 `mdImages`
       （用在「要送出的內容」，內容完整不截斷，所以不需要 `mdPreview`）。
    """
    if "function mdImages(" not in js:
        return

    check(js.count("function mdImages(") == 1, "[md] mdImages() 只有一份實作")
    # 定義 1 + 呼叫 N
    calls = len(re.findall(r"(?<!function )mdImages\(", js))
    check(calls >= 1, "[md] mdImages() 真的被呼叫（不是定義了沒用）", f"{calls} 處")

    body = js.split("function mdImages(", 1)[1].split("\n  }", 1)[0]
    check(r"/^(\/media\/|\/files\/|https?:\/\/)/" in body,
          "[md] 只放行 /media/、/files/ 與 http(s):// 的圖片網址（擋 javascript: 之類）")
    check("var out = esc(s || '')" in body,
          "[md] 先 escape 再處理（不會因為 alt/url 帶引號就破版）")
    check("class=\"mdimg\"" in body and ".mdimg" in html,
          "[md] 圖片有 .mdimg 樣式（不會撐破氣泡）")

    if 'id="ovPost"' not in html:
        return          # 不是客服工作台 → 下面那些落點不存在

    # 客服工作台的 4 處「顯示回覆／內文」都要經過它：
    #   ① 會話訊息氣泡  ② 會話上的 AI 留痕  ③ 命中測試的回答  ④ 回覆紀錄的回覆欄
    check(re.search(r"mdImages\(m\.text", js) is not None,
          "[md] 會話訊息氣泡走 mdImages()")
    check(re.search(r"mdImages\(g\.detail \|\| g\.answer", js) is not None,
          "[md] 會話上的 AI 留痕走 mdImages()")
    check(re.search(r"mdImages\(r\.answer", js) is not None,
          "[md] 命中測試的回答走 mdImages()")
    # 列表／表格塞不下圖 → 走 mdPreview()（不放圖，但也不露出 ![..](..) 原字串）
    check("function mdPreview(" in js,
          "[md] 有 mdPreview()：清單預覽不放圖，但把 ![..](..) 換成「[圖]」")
    check(re.search(r"mdPreview\(g\.answer", js) is not None,
          "[md] 回覆紀錄的「回覆」欄走 mdPreview()")
    check(re.search(r"mdPreview\(c\.text", js) is not None,
          "[md] 會話清單的最後一則預覽走 mdPreview()")
    leak = re.findall(r"esc\(\(\w+\.(?:answer|text|detail) \|\| ''\)\.slice", js)
    check(not leak,
          "[md] 沒有漏掉的內文樣板（直接 esc + slice 會露出 ![..](..) 原始字串）",
          f"可疑 {len(leak)} 處" if leak else "")


def check_context_menu(html: str, js: str) -> None:
    """工作台右鍵選單閘門（迴歸）。

    ★ 需求：右鍵**只能**作用在「還沒被處理的單條訊息氣泡」或「留言內容框」上，
      **不能**掛在會話（用戶聊天框）上。兩處各要有兩個動作：
      由自動回復引擎產生回覆 + 標記已處理。
    """
    if "ctxMenu" not in html:
        return
    check("function showMsgMenu(" in js, "有私訊氣泡的右鍵選單 showMsgMenu()")
    check("function showCommentMenu(" in js, "有留言內容框的右鍵選單 showCommentMenu()")
    check("function hideCtxMenu(" in js, "有 hideCtxMenu()")
    # 掛載點
    check('oncontextmenu="showMsgMenu(event,' in js, "右鍵掛在單條私訊氣泡上")
    check("' oncontextmenu=\"showCommentMenu(event," in js, "右鍵掛在留言內容框（.comment-text）上")
    # ★ 不得掛在會話項（用戶聊天框）上
    check("showConvMenu" not in js, "★ 沒有會話項（聊天框）層級的右鍵選單")
    # 資格判斷（只有未處理的可右鍵）
    check("function msgReplyable(" in js and "is_handled === false" in js,
          "私訊右鍵限「未處理客戶來訊」")
    check("function commentReplyable(" in js, "留言右鍵限「未處理的頂層留言」")
    # 兩個動作都在
    check("標記這一則已處理" in js, "私訊氣泡選單有「標記這一則已處理」")
    check("標記為已處理（忽略）" in js, "留言選單有「標記為已處理（忽略）」")
    check("由自動回復引擎" in js, "選單有「由自動回復引擎…」")
    # 後端接線
    check("/console/agent/draft" in js, "引擎產生草稿打 /console/agent/draft")
    check("message_id" in js, "單條標記帶 message_id（只標這一則）")


def check_refresh_button(html: str, js: str) -> None:
    """「重新整理」（補拉歷史）按鈕閘門。

    ★ 需求：客服工作台旁一顆刷新鍵，點下去由 CRM 驅動 Connector 補拉歷史，
      處理「公網中斷沒推送到、訊息沒入庫」。
    """
    if "btnRefresh" not in html:
        return
    # 按鈕必須是靜態節點（在 script 之前），否則輪詢重繪會弄丟
    first_script = html.find("<script")
    check(html.rfind('id="btnRefresh"') < first_script, "刷新按鈕是靜態節點（在 script 之前）")
    check("function refreshHistory(" in js, "有 refreshHistory() 實作")
    check("/console/agent/sync-history" in js, "刷新打 /console/agent/sync-history")
    check("state.refreshing" in js, "有防重入（refreshing）旗標")
    check("dm_pending" in js and "dm_handled" in js, "結果會區分「補紅點」與「已回覆不補」")


def check_platform_badges(html: str, js: str) -> None:
    """工作台二級平台 tab 紅點（迴歸閘門）。

    ★ 曾發生的 bug：紅點只有在「先點進該平台」後才出現（一級導覽的數字靠
      當前平台的會話算出來），二級 tab 本身根本沒有紅點。修法＝每個 tab 自帶
      `badge-<platform>` 節點，數字由 `/console/agent/pending-counts` 拉。
    """
    if "id=\"tabs\"" not in html:
        return
    check("badge-" in js and "class=\"badge\" id=\"badge-" in js,
          "renderTabs() 為每個平台 tab 產生 badge-<platform> 紅點節點")
    check("/console/agent/pending-counts" in js,
          "打 /console/agent/pending-counts 取得各平台未處理數")
    check("state.convs.reduce" not in js,
          "一級紅點不再只靠當前平台會話算（renderBadges 不用 state.convs）")


def check_acct_modal(html: str) -> None:
    """後台「平台帳號」彈窗必須單頁展示（不左右上下滑動）。

    ★ 曾發生：彈窗 `box wide`（620px）+ 單欄堆疊 → 內容 724×987 溢出容器 620×836，
      橫豎都出捲軸。修法＝`.box.acct`（1180px）+ 左表單／右清單兩欄 +
      表格 `table-layout: fixed` + `colgroup` 固定欄寬 + 內容 `overflow-wrap: anywhere`。
    """
    if 'id="ovAcct"' not in html:
        return
    check('class="box acct"' in html, "平台帳號彈窗用 .box.acct（不再用 box wide）")
    check("box wide" not in html or html.find("box wide") > html.find('id="ovAcct"'),
          "平台帳號彈窗不再是單欄 box wide")
    check(".box.acct" in html and "max-width: min(1180px" in html,
          "彈窗放寬到 1180px（96vw 上限）")
    check('class="acct-cols"' in html and 'class="acct-form"' in html
          and 'class="acct-list"' in html, "左（表單）右（清單）兩欄佈局")
    check("<colgroup>" in html, "表格有 colgroup 固定欄寬")
    check("table-layout: fixed" in html, "表格 table-layout: fixed（長 ID 不撐寬）")
    check("overflow-wrap: anywhere" in html, "長 token 會折行（不橫向溢出）")
    check('class="acct-scroll"' in html and "overflow: auto" in html,
          "清單區自己滾（彈窗本體不滾）")


def check_badge_consistency(html: str, js: str) -> None:
    """紅點一致性閘門（迴歸）。

    ★ 曾發生的 bug：商家列鈕上的紅點只在啟動時拉一次 `merchant-stats`，
      標為已處理 / 新訊息進來後就與工作台下面的數字對不上。
      修法＝把 `loadMerchantStats()` 併進**唯一刷新入口** `loadAllCounts()`，
      並加紅點輪詢（webhook 不會主動通知前端）。
    """
    if "merchant-stats" not in js:
        return
    check("function loadMerchantStats(" in js, "有 loadMerchantStats()（商家列紅點）")
    # 唯一刷新入口：loadAllCounts 必須同時刷新兩邊
    i = js.find("function loadAllCounts(")
    seg = js[i:i + 400] if i >= 0 else ""
    check("loadMerchantStats()" in seg,
          "★ loadAllCounts() 同時刷新商家列與工作台紅點（唯一入口）")
    check("function renderMerchantBar(" in js and "_mbarSig" in js,
          "renderMerchantBar() 有簽名比較（輪詢時數字沒變就不重繪）")
    # 輪詢存在且會暫停
    check("setInterval(" in js and "COUNT_POLL_MS" in js, "有紅點輪詢")
    check("document.hidden" in js, "輪詢在頁面切到背景時暫停")
    check("visibilitychange" in js, "切回前景立刻補一次")


def check_port_completeness(html: str, js: str) -> None:
    """移植完整性：舊版 console 的設定分頁整包搬過來，**用到的全域識別字必須都存在**。

    ★ 為什麼需要這條：整包移植是機械替換（`arState.`→`stState.`、URL 改名…），
      最容易漏掉的不是邏輯，而是**區塊外的全域宣告**。
      實例（2026-10-14 小jo 回報的三個問題，全部由這一類疏漏造成）：
        · `KB_STATUS` / `KB_RUN_LABEL` 定義在舊檔 KB 區塊**之前** → 沒搬過來，
          `kbDocTag()` 一呼叫就 ReferenceError → **知識庫文件列表整頁空白**
          （統計有數字、列表看不到，看起來像後端壞了）。
        · FAQ／表格的「匯入 Excel/CSV／下載範本／匯出」按鈕與 `import_*` 端點整批沒搬。
        · 紀錄頁的卡片式展示（層級標籤／可展開檢索明細）被換成了簡易表格。
    """
    # [a] 用了但沒宣告的「常數型」識別字（ALL_CAPS）—— 這正是上述第一類漏搬。
    declared = set(re.findall(r"^\s*(?:function|var|let|const)\s+([A-Za-z_$][\w$]*)",
                              js, re.M))
    # 已知的瀏覽器／語言內建（大寫）
    builtin = {
        "JSON", "Math", "Date", "Number", "String", "Boolean", "Array", "Object", "Promise",
        "Set", "Map", "RegExp", "Error", "TypeError", "NaN", "Infinity", "URLSearchParams",
        "FormData", "FileReader", "Blob", "URL", "Image", "Event", "Node", "Element", "CSS",
        "HTMLElement", "XHR", "XMLHttpRequest", "DOMException", "Intl", "Symbol", "BigInt",
        "WeakMap", "WeakSet", "Proxy", "Reflect", "AbortController", "TextEncoder",
    }
    used = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", js))
    missing = sorted(
        n for n in used - declared - builtin
        if not n.startswith(("TODO", "XXX", "FIXME"))
        # 排除 HTML 標籤名與常見縮寫
        and n not in {"TMP", "API", "URLS", "ID", "UUID", "CSRF", "XLSX", "CSV", "PDF", "LLM"}
        # 排除在字串裡出現的（例如 'mode 必須是 replace'）——用引號包住的就不算引用
        and not re.search(r"['\"`][^'\"`]*\b" + re.escape(n) + r"\b[^'\"`]*['\"`]", js)
    )
    check(not missing,
          "★ 沒有「用了卻沒宣告」的全域常數（漏搬的典型症狀：整頁渲染失敗）",
          "缺：" + "、".join(missing) if missing else "")

    # [b] 設定頁四分頁（FAQ／表格／知識庫／紀錄）的入口都必須存在
    for fn in ("renderSettingsFaq", "renderSettingsTable", "renderSettingsKb",
               "renderSettingsHittest", "renderSettingsLogs"):
        check(("function " + fn) in js, f"設定分頁入口存在：{fn}()")

    # [c] 舊版有的功能按鈕（整批沒搬過一次，所以逐項釘住）
    if "function faqTabHtml" in js:
        for marker, why in (
            ("匯入 Excel / CSV", "FAQ 匯入按鈕"),
            ("faq-import", "FAQ 匯入端點"),
            ("faq-template?fmt=xlsx", "FAQ 範本 xlsx"),
            ("faq-template?fmt=csv", "FAQ 範本 csv"),
            ("faq-export", "FAQ 匯出"),
            ("儲存全部修改", "FAQ 批次儲存"),
            ("onFaqSearchInput", "FAQ 即時搜尋"),
        ):
            check(marker in js, f"FAQ 具備：{why}")
    if "function tableTabHtml" in js:
        for marker, why in (
            ("上傳 Excel / CSV 建新表", "表格匯入建新表按鈕"),
            ("table-import", "表格匯入端點"),
            ("table-template?fmt=xlsx", "表格範本 xlsx"),
            ("table-export?table_id=", "單表匯出"),
            ("editColumns", "欄位編輯"),
            ("addRow", "新增資料列"),
        ):
            check(marker in js, f"業務表格具備：{why}")

    # [d] 紀錄頁：卡片式展示 + 可展開檢索明細（不是簡易表格）
    if "function logsTabHtml" in js:
        for marker, why in (
            ("log-item", "紀錄用卡片式（.log-item）呈現"),
            ("toggleLogDetail", "可展開單筆檢索明細"),
            ("renderMatchedRows", "召回明細渲染"),
            ("kbHitHtml", "候選塊分數/入選標示"),
            ("命中情況統計", "命中情況統計子頁"),
            ("openLogsStats", "開啟統計子頁"),
        ):
            check(marker in js, f"紀錄頁具備：{why}")

    # [e] 命中測試：勾層 + 試跑 + 結果（含召回明細）
    if "function hittestTabHtml" in js:
        for marker, why in (
            ("ktLayers", "分層勾選"),
            ("runHitTest", "開始測試"),
            ("ktResultHtml", "結果渲染"),
            ("ktNoHistory", "可關掉帶入對話"),
            ("ktRewrite", "可切換查詢改寫"),
        ):
            check(marker in js, f"命中測試具備：{why}")
        # ★ 2026-10-14 小jo 要求：測試頁只要「勾層 + 開始測試」，
        #   平台選擇沒有意義、三個批量按鈕也拿掉。
        for gone, why in (
            ("ktTarget", "平台選擇下拉"),
            ("全選", "全選按鈕"),
            ("全不選", "全不選按鈕"),
            ("照目前開關重設", "照目前開關重設按鈕"),
            ("ktAllLayers", "批次勾選函式"),
            ("ktResetLayers", "重設函式"),
        ):
            check(gone not in js, f"命中測試已移除：{why}")

    # [e2] 自動回復：兩層改名 場景層／賬號層、移除「啟用集」、賬號層跟場景層連動
    if "function renderSettingsAutoreply" in js:
        for marker, why in (
            ("場景層", "第一層叫場景層"),
            ("賬號層", "第二層叫賬號層"),
            ("arPlatformScopeOn", "賬號層依場景層連動"),
            ("data-locked", "未勾場景的帳號標記為鎖定"),
        ):
            check(marker in js, f"自動回復設定具備：{why}")
        for gone, why in (
            ("啟用集", "啟用集區塊（改由各子頁自己控制）"),
            ("bindListHtml", "啟用集的渲染函式"),
            ("'商家 × 平台 × 場景", "舊的第一層標題"),
            ("平台帳號（第二層）", "舊的第二層標題"),
        ):
            check(gone not in js, f"自動回復設定已移除：{why}")

    # [e3] ★★ 會話／貼文項絕不能用 `data-acc`（P0：會被設定頁的 [data-acc] 綁定
    #      覆蓋 onclick，導致点不开详情，還會誤改該帳號的 AI 開關）
    check("data-bp=" in js, "會話／貼文項用 data-bp 記錄 bp_account_id（不再用 data-acc）")
    for gone, why in (
        ("' data-acc=\"' + esc(c.bp_account_id", "會話項上的 data-acc"),
        ("' data-acc=\"' + esc(p.bp_account_id", "貼文項上的 data-acc"),
    ):
        check(gone not in js, f"已移除：{why}")
    # 設定頁的綁定必須限定在 #spBody 內（不得用 document 全域選取 [data-acc]）
    check("box.querySelectorAll('[data-acc]')" in js,
          "設定頁的 [data-acc] 綁定限定在 #spBody 內")
    check("document.querySelectorAll('[data-acc]')" not in js,
          "沒有任何全域 [data-acc] 選取（否則會覆蓋會話項 onclick）")

    # [e4] 送出樂觀顯示 + 送達狀態 + 內容輪詢
    for marker, why in (
        ("outStatusTag", "我方訊息有送達狀態標籤"),
        ("正在送出", "送出中顯示「正在送出」"),
        ("已送達", "送完顯示「已送達」"),
        ("refreshThread", "可單獨刷新開啟中的會話訊息"),
        ("_threadSig", "訊息沒變不重繪（保住滾動位置）"),
        ("_convsSig", "會話列表沒變不重繪（不打断点击）"),
        ("_nearBottom", "只在底部時才自動捲到最新"),
    ):
        check(marker in js, f"送出／輪詢具備：{why}")
    # 輪詢不可以只刷紅點 —— 必須連列表與訊息一起刷
    if "setInterval(function () {" in js:
        tail = js[js.index("setInterval(function () {"):]
        tail = tail[:800]
        check("loadAllCounts()" in tail and "refreshThread(false)" in tail
              and ("loadConversations()" in tail or "loadPosts()" in tail),
              "輪詢同時刷新：紅點 + 列表 + 開啟中的訊息")

    # [f] 移植過來的那幾塊：全部走 stApi/stApiJson/stUpload/stDownload（自動帶
    #     business_id 與 CSRF）。⚠️ 只看移植區 —— CRM 原本就有的對話頁附件上傳
    #     (`/console/agent/upload`) 不屬於這批，它自己帶了 CSRF 也不需要 business_id。
    region = js
    for start_marker in ("知識庫（RAG 第三層）—— 塊C：從舊版 console 整包移植",
                         "FAQ／業務表格／命中測試／紀錄 —— 從舊版 console 整包移植"):
        if start_marker in region:
            region = region[region.index(start_marker):]
    end_marker = "尾端靜態節點用到的函式"
    if end_marker in region:
        region = region[:region.index(end_marker)]

    raw_calls = re.findall(r"fetch\(\s*['\"`](/console/agent/[^'\"`]*)['\"`]", region)
    check(not raw_calls,
          "★ 移植區沒有裸 fetch() 打 /console/agent/*（會漏帶 business_id 與 CSRF）",
          "裸呼叫：" + "、".join(raw_calls[:3]) if raw_calls else "")

    # 下載連結也必須帶 business_id（stDownload 會自動補）
    bad_dl = re.findall(r"location\.href\s*=\s*['\"`](/console/agent/[^'\"`]*)['\"`]", region)
    check(not bad_dl, "★ 下載連結沒有裸 location.href（要用 stDownload 補 business_id）",
          "裸連結：" + "、".join(bad_dl[:3]) if bad_dl else "")

    # 舊 URL 前綴一個都不能留
    for bad in ("/console/autoreply/",):
        check(bad not in region, f"移植區沒有殘留舊端點前綴 {bad}")

    # [g] 專案約定：不用瀏覽器原生彈窗（只看移植區，避免誤判其他既有程式）
    for name, pat in (("confirm", r"(?<![\w.])confirm\("),
                      ("alert", r"(?<![\w.])alert\("),
                      ("prompt", r"(?<![\w.])prompt\(")):
        check(not re.search(pat, region),
              f"移植區沒有原生 {name}()（二選一用 stChoose、確定用 askConfirm、輸入用 stPrompt）")


def main():
    pages = sorted(UI_DIR.glob("*.html"))
    js_files = sorted(UI_DIR.glob("*.js"))
    check(bool(pages), f"找到 {len(pages)} 個頁面：{[p.name for p in pages]}")
    check(bool(js_files), f"找到 {len(js_files)} 個共用 JS：{[p.name for p in js_files]}")

    # ---- 共用 JS ----
    for p in js_files:
        print(f"\n[{p.name}]")
        src = p.read_text(encoding="utf-8")
        node_check(src, "JS")
        check("confirm(" not in src, "沒有使用 confirm()")

    # ---- 頁面 ----
    for p in pages:
        html = p.read_text(encoding="utf-8")
        print(f"\n[{p.name}]")

        # [1] JS 語法
        js_all = "\n;\n".join(scripts_of(html))
        if js_all.strip():
            node_check(js_all, "JS")
        else:
            check(True, "無內嵌 script")

        # [2] 禁 confirm()
        check("confirm(" not in js_all, "沒有使用 confirm()")

        # [3] infotip：定義了樣式就必須真的用到
        if ".infotip" in html:
            check("infotip(" in js_all or 'class="infotip"' in html, "定義了 infotip 且確實使用")
        else:
            check(True, "此頁為極簡頁，無 infotip 需求")

        # [4] 彈窗為靜態節點（.ov 出現在 script 區塊之前）
        first_script = html.find("<script")
        if 'class="ov"' in html:
            check(html.rfind('class="ov"') < first_script, "彈窗 .ov 是靜態節點（在 script 之前）")

        # [6] 貼文特殊配置彈窗
        check_post_modal(html, js_all, first_script)

        # [7] markdown 圖片渲染
        check_md_images(html, js_all)

        # [8] 二級平台 tab 紅點
        check_platform_badges(html, js_all)

        # [9] 重新整理（補拉歷史）按鈕
        check_refresh_button(html, js_all)

        # [10] 右鍵選單（只在單條氣泡 / 留言框上）
        check_context_menu(html, js_all)

        # [11] 紅點一致性（商家列 ↔ 工作台）
        check_badge_consistency(html, js_all)

        # [12] 後台平台帳號彈窗（單頁展示）
        check_acct_modal(html)

        # [13] 移植完整性（漏搬全域宣告 → 整頁渲染失敗）
        #     ⚠️ 只對「有設定分頁」的頁面跑 —— admin/ops 沒有 FAQ/表格/知識庫分頁，
        #        對它們檢查「分頁入口存在」只會產生假失敗。
        if 'id="spBody"' in html and "function faqTabHtml" in js_all:
            check_port_completeness(html, js_all)
        else:
            check(True, "此頁沒有客服設定分頁，跳過移植完整性檢查")

        # [5] 身分切換元件有載入
        has_switch_ui = 'id="roleMenu"' in html or 'id="whoBox"' in html
        if has_switch_ui:
            check(has_switch_ui, "含身分切換的靜態節點")
            switch_js = '<script[^>]*src="/ui/common.js"'
            check(bool(re.search(switch_js, html)), "已載入 /ui/common.js")
            # 切換節點本身不可由 JS 動態產生（避免被輪詢重繪弄丟）
            check(html.find('id="roleMenu"') < first_script, "身分選單是靜態節點（在 script 之前）")

    # ---- 跨檔約定 ----
    print("\n[跨檔約定]")
    common = (UI_DIR / "common.js").read_text(encoding="utf-8") if (UI_DIR / "common.js").exists() else ""
    check("can_switch" in common, "common.js 依 can_switch 決定是否顯示切換入口（單身分不顯示）")
    check("/api/auth/switch-role" in common, "common.js 打 /api/auth/switch-role")
    # ★ 迴歸閘門：切換身分是 POST，後端有 CSRF 閘門（缺 token → 403）。
    #   `common.js` 的 switchTo 必須帶 X-CSRF-Token，且每個呼叫 TopUser.init 的頁面
    #   都必須把 csrf 傳進去 —— 漏傳 = csrf 為空字串 = 切換一律 403
    #   （曾真實發生：admin.html / ops.html / placeholder.html 三頁都漏，切換全壞）。
    check("X-CSRF-Token" in common, "common.js 的 switchTo 帶 X-CSRF-Token")
    for f in sorted(UI_DIR.glob("*.html")):
        t = f.read_text(encoding="utf-8")
        idx = 0
        while True:
            i = t.find("TopUser.init(", idx)
            if i < 0:
                break
            j = t.find(");", i)
            seg = t[i:j if j > 0 else len(t)]
            check("csrf" in seg, f"{f.name} 的 TopUser.init 有傳 csrf（否則切換 403）")
            idx = (j + 2) if j > 0 else len(t)

    login = (UI_DIR / "login.html").read_text(encoding="utf-8")
    check("need_role" in login, "登入頁處理 need_role（多身分需選身分）")
    check("ticket" in login, "登入頁以 ticket 完成第二步登入")
    check('id="stepRole"' in login, "登入頁有身分選擇區塊")

    print("\n" + ("全部通過" if not fails else f"失敗 {len(fails)} 項："))
    for f in fails:
        print("  - " + f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
