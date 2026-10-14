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

  // ★ 客服工作台：平台切換 + 分頁切換必須「真的去抓資料」
  //   （曾真實發生：onchange 只呼叫 render() 重繪、不呼叫 loadTab() 載入 →
  //     切平台/切分頁畫面清空、卻不發請求。）
  await page.click('#whoBox');
  await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
  await page.click('#roleList .roleitem[data-role="agent"]');
  await waitTitle('客服工作台');
  await settle();

  const convReqs = [];
  page.on('response', r => { if (r.url().includes('/conversations')) convReqs.push(r.url()); });

  for (const plat of ['ig', 'fb', 'wechat', 'whatsapp']) {
    const before = convReqs.length;
    await page.selectOption('#selPlat', plat);
    await page.waitForTimeout(1500);
    check(convReqs.length > before, '切到 ' + plat + ' → 有發出 /conversations 請求');
  }
  // IG 的「貼文留言」scope
  await page.selectOption('#selPlat', 'ig');
  await page.waitForTimeout(1200);
  await page.selectOption('#selScope', 'comment');
  await page.waitForTimeout(1500);
  const cmt = await page.$$eval('.conv-item', els => els.length).catch(() => 0);
  check(cmt > 0, 'IG 貼文留言 scope 有會話項', String(cmt));
  await shot('shot' + (n++) + '_ig_comment.png');

  // 分頁「回覆紀錄」要有內容
  await page.selectOption('#selPlat', 'whatsapp');
  await page.waitForTimeout(1200);
  const tab = await page.$('#nav >> text=回覆紀錄');
  if (tab) {
    await tab.click();
    await page.waitForTimeout(1800);
    const has = await page.evaluate(() => {
      const t = (document.querySelector('.page') || document.body).innerText;
      return t.includes('時間') || t.includes('層級');
    });
    check(has, '分頁「回覆紀錄」有載入內容');
    await shot('shot' + (n++) + '_logs.png');
  } else {
    check(false, '找得到「回覆紀錄」分頁');
  }

  // ★ admin 審計日誌分頁：資料不在 reloadAll 裡，切分頁才載入
  //   （曾真實發生：切到此分頁顯示「暫無紀錄」，其實 DB 有紀錄。）
  await page.click('#whoBox');
  await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
  await page.click('#roleList .roleitem[data-role="admin"]');
  await waitTitle('管理員後台');
  await settle();
  const aTab = await page.$('#nav >> text=審計日誌');
  if (aTab) {
    await aTab.click();
    await page.waitForTimeout(1800);
    const hasTable = await page.evaluate(() => !!document.querySelector('#view table'));
    check(hasTable, 'admin「審計日誌」分頁有載入紀錄（非「暫無紀錄」）');
    await shot('shot' + (n++) + '_admin_audit.png');
  } else {
    check(false, '找得到「審計日誌」分頁');
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
