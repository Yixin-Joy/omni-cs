"""自檢：登入後**任何**管理 / 客服 / 運維界面都看得到、點得到「登出」。

★ 為什麼要有這支（2026-10-29 小jo）：
  「登陸後進入到任何管理、客服、運維界面都要有登出按鈕」

  實際缺陷：客服台的登出按鈕原本塞在 `#roleMenu` 裡 —— 那是**預設隱藏、點頭像才展開**
  的身分選單。而 `TopUser.init()` 開頭 `if (!me.can_switch) return;`：
  `can_switch = len(roles) > 1`，所以**只有一個身分的帳號（客服坐席通常就是）**
  連點擊事件都不綁 → 選單永遠打不開 → 按鈕存在但完全摸不到，等於無法登出。
  管理台 / 運維台 / 佔位頁一直是側欄常駐按鈕，只有客服台這樣。

修法（本自檢釘住的）：
  1. 客服台的登出移出 `#roleMenu`，改成頂欄右側 `.mact` 常駐（不依賴身分數量）
  2. 四張頁面都有 `#btnLogout`，且**都不在 `#roleMenu` 之內**（防止又退回隱藏選單）
  3. 真的點下去：跳 `/login`、舊 session 失效、直接訪問管理頁被踢回登入

本自檢：
  [A] 靜態：四張頁面的按鈕存在、位置正確（不在隱藏選單裡）、各自的登出處理有接線
  [B] 真瀏覽器：單身分 / 多身分 × 各界面 → 按鈕可見且在視窗內；點擊真的登出

跑法（repo 根；兩個服務都要在跑）：
    PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
      PYTHONIOENCODING=utf-8 NO_PROXY=127.0.0.1,localhost \\
      <python> crm/tests/logout_button_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.core import config  # noqa: E402

CRM = "http://127.0.0.1:8020"
UI = ROOT / "crm" / "ui"

# 四張「登入後會看到」的頁面（login.html 不算 —— 它本來就是登入頁）
PAGES = {
    "admin.html": ("管理台", "/admin"),
    "console_ui.html": ("客服台", "/agent"),
    "ops.html": ("運維台", "/ops"),
    "placeholder.html": ("佔位頁", "/"),   # 身分對不到頁面時落到這裡
}

_pass = 0
_fail: list[str] = []


def check(cond, msg, extra="") -> bool:
    global _pass
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"   {extra}" if extra else ""))
    if cond:
        _pass += 1
    else:
        _fail.append(msg)
    return bool(cond)


# ---------------------------------------------------------------- [A] 靜態

def part_a() -> None:
    print("[A] 靜態：四張頁面都有常駐的登出按鈕")
    for name, (label, _path) in PAGES.items():
        html = (UI / name).read_text(encoding="utf-8")
        print(f"\n  -- {name}（{label}）")
        check('id="btnLogout"' in html, "有 #btnLogout")
        check(html.count('id="btnLogout"') == 1, "只出現一次（沒有重複 id）",
              f"實得 {html.count('id=\"btnLogout\"')}")

        # ★ 核心：不能在 #roleMenu 之內
        #   做法：取出 #roleMenu 的內容範圍，看 btnLogout 是否落在裡面
        i_menu = html.find('id="roleMenu"')
        i_btn = html.find('id="btnLogout"')
        if i_menu == -1:
            check(True, "#roleMenu 不存在（無隱藏選單風險）")
        else:
            # roleMenu 區塊結束：從 roleMenu 開始往後找第一個「同層收尾」的 </div>
            # 粗略但夠用：計算 div 深度
            depth = 0
            j = html.find(">", i_menu) + 1
            end = -1
            while j < len(html):
                if html.startswith("<div", j):
                    depth += 1
                elif html.startswith("</div>", j):
                    if depth == 0:
                        end = j
                        break
                    depth -= 1
                j += 1
            inside = end != -1 and i_menu < i_btn < end
            check(not inside,
                  "★ 登出按鈕**不在** #roleMenu 內（單身分也能看到）",
                  f"roleMenu={i_menu} btn={i_btn} end={end}")

        # 各自的登出處理有接線
        check("/api/auth/logout" in html, "有打 /api/auth/logout")
        has_handler = ("btnLogout" in html and
                       ("btnLogout').onclick" in html
                        or "btnLogout\").onclick" in html
                        or "getElementById('btnLogout').onclick" in html))
        check(has_handler, "有綁 onclick")


# ---------------------------------------------------------------- [B] 真瀏覽器

_JS = r"""
const { chromium } = require('playwright-core');
const CRM = 'http://127.0.0.1:8020';
const CHROME = process.env.CK_CHROME;
const cases = JSON.parse(process.env.CK_CASES);   // [{sid,label,path}]

let pass = 0; const fails = [];
function check(c, m, extra) {
  console.log((c ? '  PASS  ' : '  FAIL  ') + m + (extra ? '   ' + extra : ''));
  if (c) pass++; else fails.push(m);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  try {
    for (const c of cases) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addCookies([{ name: 'mcrm_sid', value: c.sid, domain: '127.0.0.1', path: '/' }]);
      const page = await ctx.newPage();
      const errs = [];
      page.on('pageerror', e => errs.push(String(e).slice(0, 100)));
      console.log(`\n  -- ${c.label}  ${c.path}`);
      try {
        await page.goto(CRM + c.path, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(700);

        const btn = page.locator('#btnLogout');
        const n = await btn.count();
        const vis = n ? await btn.first().isVisible().catch(() => false) : false;
        const box = n ? await btn.first().boundingBox().catch(() => null) : null;
        const vp = page.viewportSize();
        const inVp = !!(box && box.x >= 0 && box.y >= 0
          && box.x + box.width <= vp.width && box.y + box.height <= vp.height);

        check(n === 1, '有 #btnLogout', `count=${n}`);
        check(vis, '★ 按鈕可見（不是藏在隱藏選單裡）');
        check(inVp, '在視窗範圍內可點', box ? `x=${Math.round(box.x)} y=${Math.round(box.y)}` : '—');
        check(errs.length === 0, '無 JS 錯誤', errs[0] || '');

        // 切子欄目後按鈕仍要在（重繪不能把它弄不見）
        const tabs = await page.evaluate(() => {
          const out = [];
          document.querySelectorAll('.nav[id], nav.nav > *, .tabs > *').forEach((el, i) => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) out.push(i);
          });
          return out.slice(0, 6);
        });
        let switched = 0;
        for (const i of tabs) {
          const el = page.locator('.nav[id], nav.nav > *, .tabs > *').nth(i);
          if (!(await el.count())) continue;
          try { await el.click({ timeout: 2500 }); } catch (_) { continue; }
          await page.waitForTimeout(350);
          switched++;
          if (!(await page.locator('#btnLogout').isVisible().catch(() => false))) {
            check(false, `切到第 ${i} 個欄目後登出不見了`);
          }
        }
        if (switched) check(true, `切過 ${switched} 個欄目，登出都在`);

        // 客服台頂欄專屬：登出與商家列不能重疊（admin/ops 的登出在側欄，無此節點）
        const hasMact = await page.locator('.mact').count();
        if (hasMact) {
          const geo = await page.evaluate(() => {
            const mrest = document.querySelector('#mrest');
            const mact = document.querySelector('.mact');
            const g = (e) => e ? e.getBoundingClientRect() : null;
            return {
              mrest: g(mrest), mact: g(mact), vw: window.innerWidth,
              restScroll: mrest ? { sw: mrest.scrollWidth, cw: mrest.clientWidth } : null,
            };
          });
          const okGeo = geo.mrest && geo.mact
            && geo.mrest.right <= geo.mact.left + 1
            && geo.mact.right <= geo.vw + 1;
          check(okGeo, '頂欄：商家列與登出不重疊、不超出視窗',
            geo.mrest && geo.mact
              ? `mrest右=${Math.round(geo.mrest.right)} mact左=${Math.round(geo.mact.left)} vw=${geo.vw}`
              : '缺節點');
          if (geo.restScroll) {
            check(geo.restScroll.sw <= geo.restScroll.cw + 2,
              '商家列沒有被擠出橫向捲軸',
              `${geo.restScroll.sw}/${geo.restScroll.cw}`);
          }
        }
      } catch (e) {
        check(false, '頁面載入或斷言失敗', String(e).slice(0, 150));
      }
      await ctx.close();
    }

    // 功能：真的登出一次（最後一個 case 的 session）
    if (cases.length) {
      const c = cases[cases.length - 1];
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addCookies([{ name: 'mcrm_sid', value: c.sid, domain: '127.0.0.1', path: '/' }]);
      const page = await ctx.newPage();
      console.log(`\n  -- 功能驗證：點登出（${c.label} ${c.path}）`);
      try {
        await page.goto(CRM + c.path, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(600);
        await page.locator('#btnLogout').click();
        await page.waitForURL(u => u.toString().includes('/login'), { timeout: 15000 })
          .catch(() => {});
        check(page.url().includes('/login'), '點登出 → 跳到 /login',
          page.url().replace(CRM, ''));
        const r = await page.evaluate(async () => {
          const x = await fetch('/api/auth/me', { credentials: 'same-origin' });
          let d = {}; try { d = await x.json(); } catch (e) {}
          return { status: x.status, ok: d.ok };
        });
        check(r.status === 401 || r.ok === false, '舊 session 已失效', `HTTP ${r.status}`);
      } catch (e) {
        check(false, '登出功能驗證失敗', String(e).slice(0, 150));
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  console.log('__RESULT__' + JSON.stringify({ pass, fails }));
})();
"""


def _node() -> Path:
    return Path("node")


def _chrome() -> str:
    for p in (
        Path("C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ):
        if p.exists():
            return str(p).replace("\\", "/")
    return ""


def _mk_staff(login: str, roles: list[str]) -> dict | None:
    """造／更新一個臨時員工（自檢用）。回傳 staff dict。"""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO crm_staff (login_account, password_hash, staff_name, roles, enabled)"
                " VALUES (%s, 'x', %s, %s, true)"
                " ON CONFLICT (login_account) DO UPDATE SET roles = EXCLUDED.roles,"
                "   enabled = true"
                " RETURNING id, login_account, staff_name, roles, is_super_agent, is_kb_editor",
                (login, login.replace("__CK_", ""), roles),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        conn.close()


def _drop_staff() -> None:
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_session WHERE staff_id IN"
                        " (SELECT id FROM crm_staff WHERE login_account LIKE '__CK_logout%%')")
            cur.execute("DELETE FROM crm_staff WHERE login_account LIKE '__CK_logout%%'")
        conn.commit()
    finally:
        conn.close()


def part_b() -> None:
    print("\n[B] 真瀏覽器：單身分 / 多身分 × 各界面")
    chrome = _chrome()
    if not chrome:
        check(False, "找不到 Chrome，略過瀏覽器驗證")
        return

    node_modules = "C:/Users/its-admin/.workbuddy/binaries/node/workspace/node_modules"

    # 三種身分組合，各自造 session
    combos = [
        ("__CK_logout_agent__", ["agent"], "agent"),
        ("__CK_logout_ops__", ["ops"], "ops"),
        ("__CK_logout_admin1__", ["admin"], "admin"),
    ]
    sids: list[tuple[str, str, str]] = []   # (label, sid, home)
    try:
        for login, roles, role in combos:
            st = _mk_staff(login, roles)
            if not st:
                check(False, f"造 {login} 失敗")
                continue
            sid, _csrf = sess_mod.create_session(st, role)
            sids.append((f"{login} {roles}", sid, role))

        # 管理員多身分（真實帳號）
        conn = psycopg2.connect(config.DATABASE_URL)
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, login_account, staff_name, roles, is_super_agent,"
                            "       is_kb_editor, enabled FROM crm_staff"
                            " WHERE 'admin' = ANY(roles) AND login_account NOT LIKE '__CK%'"
                            "   AND enabled ORDER BY id LIMIT 1")
                ad = cur.fetchone()
        finally:
            conn.close()
        if ad:
            sid, _ = sess_mod.create_session(dict(ad), "admin")
            sids.append((f"{ad['login_account']}（多身分）", sid, "admin"))

        cases = []
        for label, sid, role in sids:
            home = {"admin": "/admin", "agent": "/agent", "ops": "/ops"}[role]
            cases.append({"sid": sid, "label": label, "path": home})
        # 額外：首頁 / 與佔位頁都要有（首頁就是該身分的台，等於再驗一次路由）
        for label, sid, role in sids:
            cases.append({"sid": sid, "label": label + " 首頁", "path": "/"})

        f = Path(tempfile.gettempdir()) / "_logout_ui.js"
        f.write_text(_JS, encoding="utf-8")
        env = dict(os.environ)
        env["CK_CHROME"] = chrome
        env["CK_CASES"] = json.dumps(cases, ensure_ascii=False)
        env["NODE_PATH"] = node_modules
        env["NO_PROXY"] = "127.0.0.1,localhost"
        r = subprocess.run([str(_node()), str(f)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        # 印出逐條結果（去掉 RESULT 行）
        for line in out.splitlines():
            if not line.startswith("__RESULT__"):
                print(line)
        m = None
        for line in out.splitlines():
            if line.startswith("__RESULT__"):
                try:
                    m = json.loads(line[len("__RESULT__"):])
                except Exception:  # noqa: BLE001
                    m = None
        if m is None:
            check(False, "Node 沒吐 RESULT", out[-400:])
        else:
            global _pass
            _pass += int(m["pass"])
            for msg in m["fails"]:
                _fail.append(msg)
            check(len(m["fails"]) == 0, "瀏覽器斷言全過", f"fail={len(m['fails'])}")
            f.unlink(missing_ok=True)
    finally:
        for _label, sid, _r in sids:
            try:
                sess_mod.delete_session(sid)
            except Exception:  # noqa: BLE001
                pass
        _drop_staff()


def main() -> int:
    part_a()
    part_b()
    print(f"\n{'=' * 62}")
    print(f"  登出按鈕（管理/客服/運維）：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        for m in _fail:
            print("    FAIL:", m)
    return 0 if not _fail else 1


if __name__ == "__main__":
    sys.exit(main())
