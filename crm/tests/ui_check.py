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
