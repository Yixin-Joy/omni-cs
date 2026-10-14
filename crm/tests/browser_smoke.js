/**
 * 瀏覽器冒煙自檢 —— 用真 Chrome 走完整「登入 → 三台雙向切換」，逐頁截圖 + 收集 JS 錯誤。
 *
 * 為什麼需要它：`pages_check.py` / `ui_check.py` 驗的是 HTTP 與靜態碼，
 * **抓不到前端執行期錯誤**（例如切換時漏帶 CSRF → 只顯示 403 toase，狀態碼層面看不出來）。
 * 這支真的跑一遍 JS，才是「頁面打得開」的證據。
 *
 * 前置：
 *   1. CRM 服務跑著（預設 http://127.0.0.1:8020）
 *   2. Chromium/Chrome 已裝（agent-browser install，或自備）
 *   3. 有 playwright-core（本機在 codex-runtimes 內）
 *
 * 跑法（從專案根）：
 *   NODE_PATH="<有 playwright-core 的 node_modules>" \
 *   CHROME_PATH="C:\\path\\to\\chrome.exe" \
 *   <node> crm/tests/browser_smoke.js
 *
 * 環境變數：
 *   BASE        CRM 位址（預設 http://127.0.0.1:8020）
 *   CHROME_PATH Chrome 執行檔（預設自動找 agent-browser 下載的）
 *   OUT         截圖輸出目錄（預設 crm/tests/_browser_out，已 gitignore）
 */
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const BASE = (process.env.BASE || 'http://127.0.0.1:8020').replace(/\/+$/, '');
const OUT = process.env.OUT || path.join(__dirname, '_browser_out');

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const base = 'C\\Users\\its-admin\\.agent-browser\\browsers';
  const home = process.env.USERPROFILE || 'C:\\Users\\its-admin';
  const abDir = path.join(home, '.agent-browser', 'browsers');
  if (fs.existsSync(abDir)) {
    for (const d of fs.readdirSync(abDir)) {
      const exe = path.join(abDir, d, 'chrome.exe');
      if (fs.existsSync(exe)) return exe;
    }
  }
  return null;
}

function readPw() {
  const env = fs.readFileSync(path.join(ROOT, 'crm', '.env'), 'utf8');
  for (const line of env.split(/\r?\n/)) {
    const m = /^CRM_ADMIN_PASSWORD=(.*)$/.exec(line.trim());
    if (m) return m[1].trim().replace(/^["']|["']$/g, '');
  }
  return '';
}

const steps = [];
const errors = [];
const fails = [];

function check(cond, msg, extra) {
  if (!cond) fails.push(msg);
  console.log((cond ? '  PASS  ' : '  FAIL  ') + msg + (extra ? '   ' + extra : ''));
}

(async () => {
  const chrome = findChrome();
  if (!chrome) {
    console.error('找不到 Chrome；請設 CHROME_PATH 或先跑 agent-browser install');
    process.exit(2);
  }
  if (!readPw()) {
    console.error('crm/.env 沒有 CRM_ADMIN_PASSWORD');
    process.exit(2);
  }
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await chromium.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push('[console] ' + m.text()); });
  page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

  const shot = (n) => page.screenshot({ path: path.join(OUT, n) });
  const settle = () => page.waitForTimeout(1000);
  const waitTitle = (t) => page.waitForFunction((w) => document.title.includes(w), t, { timeout: 10000 });

  // 登入（兩步）
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.fill('#account', 'admin');
  await page.fill('#password', readPw());
  await page.click('#btn');
  await page.waitForSelector('#stepRole:not([hidden])', { timeout: 10000 });
  await page.click('.rolebtn[data-role="admin"]');
  await waitTitle('管理員後台');
  await settle();
  check(true, '登入 → 管理員後台', new URL(page.url()).pathname);
  await shot('shot1_admin.png');

  // 三台雙向切換
  const hops = [
    ['agent', '客服工作台'], ['ops', '運維'], ['admin', '管理員後台'],
  ];
  let n = 2;
  for (const [role, title] of hops) {
    await page.click('#whoBox');
    await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
    await page.click('#roleList .roleitem[data-role="' + role + '"]');
    await waitTitle(title);
    await settle();
    check(true, '→ ' + role + ' 切換成功', new URL(page.url()).pathname);
    await shot('shot' + (n++) + '_' + role + '.png');
  }

  await browser.close();

  check(errors.length === 0, '全程零 JS 錯誤', errors.join(' | ').slice(0, 300));
  console.log('\n截圖：' + OUT);
  console.log('結果：' + (fails.length ? 'FAIL（' + fails.length + '）' : 'PASS'));
  fails.forEach(f => console.log('  - ' + f));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.log('  FAIL  執行例外：' + ((e && e.message) || e));
  steps.forEach(s => console.log('    ' + s));
  process.exit(1);
});
