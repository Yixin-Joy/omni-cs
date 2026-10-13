"""前端靜態自檢：JS 語法 + 小jo 的三個 UI 約定 + 身分切換元件。

跑法（從專案根）：
    PYTHONPATH='<root>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      <底層 python> crm/tests/ui_check.py

檢查項：
  [1] 每個 ui/*.html 的內嵌 JS 與 ui/*.js 都能被 Node 解析（語法錯誤會當場炸）
  [2] 不得出現 `confirm(`（小jo 明確要求：永遠不要「是/否」彈窗）
  [3] 頁面要少字 —— 定義了 infotip 就必須真的用到
  [4] 彈窗必須是靜態節點 —— 檢查 .ov 節點寫在 <script> 之前
  [5] 身分切換 —— 選單節點是靜態節點、只有多身分才給入口、登入頁有兩步流程
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


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        fails.append(msg)


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
