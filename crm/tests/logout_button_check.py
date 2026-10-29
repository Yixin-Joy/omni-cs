"""自檢：「⋮」使用者選單（修改密碼 / 登出）在**任何**界面都打得開、點得到。

★ 沿革（為什麼這支存在）：
  2026-10-29 小jo：「登陸後進入到任何管理、客服、運維界面都要有登出按鈕」——
  當時客服台的登出塞在 `#roleMenu` 裡，那是「點頭像才展開」的身分選單，
  而 `TopUser.init()` 開頭 `if (!me.can_switch) return;`：
  單一身分的帳號（客服坐席幾乎都是）**連點擊都不綁** → 按鈕存在但摸不到。

  同日稍後小jo 覺得兩顆大按鈕太醜，要求改成「用戶信息旁邊三點 → 選單」。
  ★ 這次改動**沒有**放回 `#roleMenu`，而是新做一個**完全獨立**的 `UserMenu`
    （common.js）：不看角色、不看身分數量，任何人點三點都能開。
  這支自檢就是用來釘住「獨立」這件事 —— 一旦有人把選單搬回 `#roleMenu`
  或讓它依賴 `can_switch`，這裡會立刻失敗。

本自檢：
  [A] 靜態：三頁都有 #btnMore / #moreMenu、選單**不在** #roleMenu 內、
            UserMenu.mount 有接線、舊的常駐 .logout 按鈕已移除
  [B] 真瀏覽器：單身分 × 三種身分 → 三點可見可點、選單打得開、兩個項目都在、
            點「登出」真的登出、點外面/ESC 能關

跑法（repo 根；CRM 服務要在跑）：
    PYTHONPATH='<根>\\crm;<根>\\.venv\\Lib\\site-packages' PYTHONUTF8=1 \\
      PYTHONIOENCODING=utf-8 NO_PROXY=127.0.0.1,localhost \\
      TMP=<根>\\.tmp_pw <python> crm/tests/logout_button_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crm"))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402

from app.auth import session as sess_mod  # noqa: E402
from app.core import config  # noqa: E402

CRM = "http://127.0.0.1:8020"
UI = ROOT / "crm" / "ui"

PAGES = {
    "admin.html": ("管理台", "/admin"),
    "console_ui.html": ("客服台", "/agent"),
    "ops.html": ("運維台", "/ops"),
    "placeholder.html": ("佔位頁", "/"),
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


def _inside(html: str, outer_id: str, inner_id: str) -> bool | None:
    """inner 是否落在 outer 那個 div 之內（用 div 深度推算）。outer 不存在回 None。

    ★ 必須先比對位置：inner 在 outer **之前**就絕不可能在裡面。
      少了這一步，`inner_pos < 結束位置` 會恆為真 → 誤判「在裡面」
      （本自檢第一版就這樣誤報了 moreMenu「在 #roleMenu 內」）。
    """
    i_out = html.find(f'id="{outer_id}"')
    i_in = html.find(f'id="{inner_id}"')
    if i_out == -1:
        return None
    if i_in == -1 or i_in < i_out:
        return False
    depth = 0
    j = html.find(">", i_out) + 1
    while j < len(html):
        if html.startswith("<div", j):
            depth += 1
        elif html.startswith("</div>", j):
            if depth == 0:
                return i_in < j
            depth -= 1
        j += 1
    return False


# ---------------------------------------------------------------- [A] 靜態

def part_a() -> None:
    print("[A] 靜態：三頁都有「⋮」+ 選單容器，且不依賴 #roleMenu")

    for name, (label, _path) in PAGES.items():
        html = (UI / name).read_text(encoding="utf-8")
        print(f"\n  -- {name}（{label}）")

        if name == "placeholder.html":
            # 佔位頁沒有側欄/頂欄，允許沒有選單（但要能登出，靠頁面自己的按鈕）
            continue

        check('id="btnMore"' in html, "有 #btnMore（三點按鈕）")
        check(html.count('id="btnMore"') == 1, "只出現一次（沒有重複 id）",
              f'實得 {html.count(chr(34) + "btnMore" + chr(34))}')
        check('id="moreMenu"' in html, "有 #moreMenu（選單容器）")
        check(html.count('id="moreMenu"') == 1, "只出現一次")

        # ★★ 核心：不能放進 #roleMenu（那是 TopUser 管的，單身分不綁點擊）
        inside_menu = _inside(html, "roleMenu", "moreMenu")
        check(inside_menu is False,
              "★ 選單**不在** #roleMenu 內（單身分也打不開的容器）",
              f"inside={inside_menu}")
        inside_btn = _inside(html, "roleMenu", "btnMore")
        check(inside_btn is False, "★ 三點按鈕也不在 #roleMenu 內", f"inside={inside_btn}")

        # 三點在 whoBox 內（= 用戶信息旁邊）
        check(_inside(html, "whoBox", "btnMore") is True, "三點在 #whoBox 內（用戶信息旁）")

        # 接線
        check("UserMenu.mount" in html, "有呼叫 UserMenu.mount")
        check("csrf: function" in html, "★ csrf 傳取值函式（不是當時的值）")

        # 舊的常駐大按鈕應該都收掉了
        check('class="logout"' not in html, "舊的常駐 .logout 按鈕已移除")
        check('id="btnLogout"' not in html, "舊的 #btnLogout 已移除")

    print("\n  -- common.js")
    js = (UI / "common.js").read_text(encoding="utf-8")
    check("global.UserMenu" in js, "導出 UserMenu")
    check("global.PwdDialog" in js, "導出 PwdDialog（改密碼仍由它負責）")
    check("/api/auth/logout" in js, "登出仍打 /api/auth/logout")
    # ★ 只看 UserMenu 那一段（TopUser 的 `if (!me.can_switch) return;` 是切換身分
    #   的正當邏輯，不該算進來）。從 UserMenu 的檔頭註解切到檔尾。
    i_um = js.find("使用者選單")
    um = js[i_um:] if i_um != -1 else ""
    check(bool(um), "找得到 UserMenu 區塊")
    um_code = [ln for ln in um.splitlines()
               if ln.strip() and not ln.strip().startswith(("//", "*", "/*"))]
    check(not any("can_switch" in ln for ln in um_code),
          "★ UserMenu 的程式碼不看 can_switch（不依賴身分數量）")
    check("confirm(" not in js, "沒有用原生 confirm（本專案禁止）")
    check('id="morePwd"' in js and 'id="moreLogout"' in js,
          "選單渲染出「修改密碼 / 登出」兩個項目")
    check("Escape" in um, "ESC 可關閉選單")
    check("moresep" in um, "有分隔線（視覺分組）")
    check("below < 180" in um or "below<180" in um, "★ 展開方向自動判斷（貼底往上彈）")


# ---------------------------------------------------------------- [B] 真瀏覽器

_JS = r"""
const { chromium } = require('playwright-core');
const CRM = 'http://127.0.0.1:8020';
const CHROME = process.env.CK_CHROME;
const cases = JSON.parse(process.env.CK_CASES);

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
      page.on('pageerror', e => errs.push(String(e).slice(0, 110)));
      console.log(`\n  -- ${c.label}  ${c.path}`);
      try {
        await page.goto(CRM + c.path, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(800);

        // ① 三點可見、在用戶信息旁邊
        const btn = page.locator('#btnMore');
        const n = await btn.count();
        const vis = n ? await btn.first().isVisible().catch(() => false) : false;
        const geo = await page.evaluate(() => {
          const r = (id) => { const e = document.getElementById(id);
            if (!e) return null; const b = e.getBoundingClientRect();
            return { x: b.x, y: b.y, w: b.width, h: b.height, right: b.right }; };
          return { btn: r('btnMore'), who: r('whoBox'),
                   vw: window.innerWidth, vh: window.innerHeight };
        });
        check(n === 1, '有 #btnMore', `count=${n}`);
        check(vis, '★ 三點可見');
        check(!!(geo.btn && geo.who
          && Math.abs((geo.btn.y + geo.btn.h / 2) - (geo.who.y + geo.who.h / 2)) <= 5),
          '★ 與用戶信息同一列（在旁邊，不是掉到下一行）');
        check(!!(geo.btn && geo.btn.right <= geo.vw + 1), '三點在視窗內');

        // ② 點開選單
        await btn.first().click();
        await page.waitForTimeout(350);
        const menu = await page.evaluate(() => {
          const m = document.getElementById('moreMenu');
          if (!m) return null;
          const b = m.getBoundingClientRect();
          const items = Array.from(m.querySelectorAll('.moreitem'));
          return {
            open: m.classList.contains('open'),
            x: b.x, y: b.y, right: b.right, bottom: b.bottom,
            vw: window.innerWidth, vh: window.innerHeight,
            labels: items.map(i => i.innerText.trim()),
            expanded: document.getElementById('btnMore').getAttribute('aria-expanded'),
          };
        });
        check(!!(menu && menu.open), '★★ 點三點 → 選單打開了（不依賴身分數量）');
        check(!!(menu && menu.labels.join('/') === '修改密碼/登出'),
          '選單有「修改密碼 / 登出」兩項', menu ? menu.labels.join(' / ') : '—');
        check(!!(menu && menu.y >= 0 && menu.bottom <= menu.vh + 1
                 && menu.right <= menu.vw + 1), '選單不超出視窗',
          menu ? `y=${Math.round(menu.y)} bottom=${Math.round(menu.bottom)} vh=${menu.vh}` : '—');
        check(!!(menu && menu.expanded === 'true'), 'aria-expanded=true（無障礙）');

        // ③ 兩個項目都點得到（isEnabled + 在視窗內）
        for (const id of ['#morePwd', '#moreLogout']) {
          const it = page.locator(id);
          const ivis = await it.isVisible().catch(() => false);
          const ibox = await it.boundingBox().catch(() => null);
          const okBox = !!(ibox && ibox.x >= 0 && ibox.y >= 0
            && ibox.x + ibox.width <= geo.vw && ibox.y + ibox.height <= geo.vh);
          check(ivis && okBox, `★ ${id} 可見且可點`, ibox ? `y=${Math.round(ibox.y)}` : '—');
        }

        // ④ 點外面關閉
        await page.mouse.click(geo.vw - 8, geo.vh - 8);
        await page.waitForTimeout(300);
        check(await page.evaluate(() =>
          !document.getElementById('moreMenu').classList.contains('open')),
          '點外面 → 選單關閉');

        // ⑤ ESC 關閉
        await btn.first().click();
        await page.waitForTimeout(250);
        await page.keyboard.press('Escape');
        await page.waitForTimeout(250);
        check(await page.evaluate(() =>
          !document.getElementById('moreMenu').classList.contains('open')),
          'ESC → 選單關閉');

        // ⑥ 點「修改密碼」→ 密碼彈窗開（不是導航到別頁）
        await btn.first().click();
        await page.waitForTimeout(250);
        await page.locator('#morePwd').click();
        await page.waitForTimeout(450);
        const pwd = await page.evaluate(() => {
          const ov = document.getElementById('pwdOv');
          return { open: !!ov && ov.classList.contains('open'),
                   menuClosed: !document.getElementById('moreMenu').classList.contains('open'),
                   url: location.pathname };
        });
        check(pwd.open, '★ 點「修改密碼」→ 彈窗打開');
        check(pwd.menuClosed, '同時選單已關閉');
        check(pwd.url === c.path, '沒有跳頁', pwd.url);
        await page.locator('#pwdCancel').click().catch(() => {});
        await page.waitForTimeout(250);

        check(errs.length === 0, '無 JS 錯誤', errs[0] || '');
      } catch (e) {
        check(false, '頁面斷言失敗', String(e).slice(0, 150));
      }
      await ctx.close();
    }

    // ⑦ 功能：真的點「登出」
    if (cases.length) {
      const c = cases[cases.length - 1];
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addCookies([{ name: 'mcrm_sid', value: c.sid, domain: '127.0.0.1', path: '/' }]);
      const page = await ctx.newPage();
      console.log(`\n  -- 功能驗證：三點 → 登出（${c.label}）`);
      try {
        await page.goto(CRM + c.path, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(700);
        await page.locator('#btnMore').click();
        await page.waitForTimeout(300);
        await page.locator('#moreLogout').click();
        await page.waitForURL(u => u.toString().includes('/login'), { timeout: 15000 })
          .catch(() => {});
        check(page.url().includes('/login'), '★ 點「登出」→ 跳到 /login',
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


def _chrome() -> str:
    for p in (Path("C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe"),
              Path("C:/Program Files/Google/Chrome/Application/chrome.exe")):
        if p.exists():
            return str(p).replace("\\", "/")
    return ""


def _mk_staff(login: str, roles: list[str]) -> dict | None:
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


def _drop() -> None:
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
    print("\n[B] 真瀏覽器：單身分 × 三種身分")
    chrome = _chrome()
    if not chrome:
        check(False, "找不到 Chrome，略過瀏覽器驗證")
        return

    sids: list[tuple[str, str, str]] = []
    try:
        for login, roles, role in (("__CK_logout_agent__", ["agent"], "agent"),
                                   ("__CK_logout_ops__", ["ops"], "ops"),
                                   ("__CK_logout_admin1__", ["admin"], "admin")):
            st = _mk_staff(login, roles)
            if not st:
                check(False, f"造 {login} 失敗")
                continue
            sid, _csrf = sess_mod.create_session(st, role)
            sids.append((f"{login} {roles}", sid, role))

        cases = [{"sid": sid, "label": label,
                  "path": {"admin": "/admin", "agent": "/agent", "ops": "/ops"}[role]}
                 for label, sid, role in sids]
        if not cases:
            check(False, "沒有任何 case")
            return

        f = Path(ROOT) / "crm" / "tests" / "_logout_ui.js"
        f.write_text(_JS, encoding="utf-8")
        tmp = ROOT / ".tmp_pw"
        tmp.mkdir(exist_ok=True)
        env = dict(os.environ)
        env.update({
            "CK_CHROME": chrome,
            "CK_CASES": json.dumps(cases, ensure_ascii=False),
            "NODE_PATH": "C:/Users/its-admin/.workbuddy/binaries/node/workspace/node_modules",
            "NO_PROXY": "127.0.0.1,localhost",
            # ★ playwright 要在 temp 建 playwright-artifacts-*；系統 temp 被擋（EPERM）
            "TMP": str(tmp).replace("\\", "/"),
            "TEMP": str(tmp).replace("\\", "/"),
            "TMPDIR": str(tmp).replace("\\", "/"),
        })
        r = subprocess.run(["node", str(f)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
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
            check(False, "Node 沒吐 RESULT", out[-500:])
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
        _drop()


def main() -> int:
    part_a()
    part_b()
    print(f"\n{'=' * 62}")
    print(f"  使用者選單（修改密碼 / 登出）：PASS {_pass} / FAIL {len(_fail)}")
    if _fail:
        for m in _fail:
            print("    FAIL:", m)
    return 0 if not _fail else 1


if __name__ == "__main__":
    sys.exit(main())
