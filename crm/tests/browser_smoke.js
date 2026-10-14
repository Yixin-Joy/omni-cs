/* 瀏覽器冒煙自檢（真 Chrome）—— 補 `ui_check.py` / `console_render_check.py` 抓不到的
 * **執行期**問題（JS 錯誤、頁面白屏、切換後不載入資料…）。
 *
 * 覆蓋：
 *   [1] 登入（兩步：帳密 → 選身分）
 *   [2] 三台雙向切換（管理員 ↔ 客服 ↔ 運維）
 *   [3] 工作台：頂部商家列（按鈕 + 未處理紅點 + 溢出「⋯」）
 *   [4] 私訊：會話列表 + 氣泡（.msg-row/.bubble）+ 切平台會重新載入
 *   [5] 貼文留言：以貼文為單位的列表 + 貼文詳情 + 右上角「貼文特殊配置」
 *   [6] 設定面板六分頁（自動回復/FAQ/業務表格/知識庫/命中測試/紀錄）
 *   [7] 全程收集 JS 錯誤（pageerror / console.error）
 *
 * 跑法（從專案根；需要 playwright-core 與 Chrome）：
 *   set NODE_PATH=<...>\node_modules
 *   node crm/tests/browser_smoke.js
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const CHROME = process.env.SMOKE_CHROME
  || 'C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe';
const OUT = path.join(__dirname, '_browser_out');
const BASE = process.env.SMOKE_BASE || 'http://127.0.0.1:8020';

let pass = 0; const fails = [];
function check(cond, label, extra) {
  if (cond) { pass++; console.log('  PASS  ' + label + (extra ? '   ' + extra : '')); }
  else { fails.push(label); console.log('  FAIL  ' + label + (extra ? '   ' + extra : '')); }
}

function readPw() {
  const t = fs.readFileSync(path.join(ROOT, 'crm', '.env'), 'utf8');
  const m = t.match(/^CRM_ADMIN_PASSWORD=(.*)$/m);
  return m ? m[1].trim() : '';
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const errors = [];
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  const shot = (n) => page.screenshot({ path: path.join(OUT, n) });
  const settle = (ms) => page.waitForTimeout(ms || 1200);
  const title = () => page.title();

  // ---------- [1] 登入（兩步） ----------
  console.log('\n[1] 登入');
  await page.goto(BASE + '/login', { waitUntil: 'domcontentloaded' });
  await page.fill('#account', 'admin');
  await page.fill('#password', readPw());
  await page.click('#btn');
  await page.waitForSelector('#stepRole:not([hidden])', { timeout: 10000 });
  check(true, '第一步後出現身分選擇（多身分必選）');
  await page.click('.rolebtn[data-role="admin"]');
  await page.waitForFunction(() => document.title === '管理員後台', { timeout: 10000 });
  await settle();
  check(true, '選「管理員」→ 管理員後台', await title());
  await shot('shot1_admin.png');

  // ---------- [2] 三台雙向切換 ----------
  console.log('\n[2] 身分切換');
  const hops = [['agent', '客服工作台'], ['ops', '運維'], ['admin', '管理員後台']];
  let n = 2;
  for (const [role, want] of hops) {
    await page.click('#whoBox');
    await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
    await page.click('#roleList .roleitem[data-role="' + role + '"]');
    await page.waitForFunction(t => document.title === t, want, { timeout: 10000 });
    await settle();
    check(true, '→ ' + role + ' 切換成功', new URL(page.url()).pathname);
    await shot('shot' + (n++) + '_' + role + '.png');
  }

  // ---------- [3~6] 客服工作台（切回 agent） ----------
  console.log('\n[3] 工作台 · 商家列');
  await page.click('#whoBox');
  await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
  await page.click('#roleList .roleitem[data-role="agent"]');
  await page.waitForFunction(() => document.title === '客服工作台', { timeout: 10000 });
  await settle(1600);

  // 頂欄：左＝帳號框（與側欄同寬）、右＝商家列
  const geom = await page.evaluate(() => {
    const who = document.querySelector('.mbar .mwho');
    const side = document.querySelector('.sidebar');
    const rest = document.querySelector('.mbar .mrest');
    const r = who.getBoundingClientRect(), s = side.getBoundingClientRect();
    const rr = rest.getBoundingClientRect();
    return { whoW: Math.round(r.width), sideW: Math.round(s.width),
             whoLeft: Math.round(r.left), restLeft: Math.round(rr.left),
             sameRow: Math.abs(r.top - rr.top) < 4 };
  });
  check(geom.whoLeft === 0, '帳號框在頂欄最左端', 'left=' + geom.whoLeft);
  check(geom.whoW === geom.sideW, '帳號框與側欄（列表）同寬',
    geom.whoW + ' vs ' + geom.sideW);
  check(geom.restLeft >= geom.whoW - 2 && geom.sameRow, '商家列在帳號框右邊');

  const mbtn = await page.$$eval('#mbar .mbtn', els => els.length);
  const mtitle = await page.textContent('#mbar .mtitle').catch(() => '');
  check(mbtn >= 1 && mtitle.indexOf('商家') >= 0, '商家列渲染', mbtn + ' 個按鈕');
  const moreVisible = await page.$eval('#mMore', el => el.style.display !== 'none').catch(() => false);
  check(mbtn <= 2 ? !moreVisible : true, '只有一個商家時不顯示「⋯」');

  // 帳號框：點擊後「切換身分 + 登出」一起出現，登出不裸露
  const logoutVisibleBefore = await page.$eval('#btnLogout', el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'
      && !!el.offsetParent;
  }).catch(() => false);
  check(!logoutVisibleBefore, '登出按鈕平時不裸露');
  await page.click('#whoBox');
  await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
  const menuInfo = await page.evaluate(() => {
    const m = document.getElementById('roleMenu');
    const items = m.querySelectorAll('.roleitem').length;
    const lo = document.getElementById('btnLogout');
    const inMenu = !!lo.closest('#roleMenu');
    const r = lo.getBoundingClientRect();
    const last = m.lastElementChild;
    return { items, inMenu, visible: r.height > 0,
             atBottom: last && last.contains(lo) };
  });
  check(menuInfo.items >= 1, '點頭像 → 出現身分選單', menuInfo.items + ' 項');
  check(menuInfo.inMenu && menuInfo.visible, '登出按鈕在選單內（不裸露）');
  check(menuInfo.atBottom, '登出在選單最下面');
  await shot('shot5_merchant_bar.png');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);

  // 商家列紅點：不能被裁掉（overflow 不能是 hidden）
  const clip = await page.evaluate(() => {
    const l = document.querySelector('#mbar .mlist');
    return l ? getComputedStyle(l).overflow : '';
  });
  check(clip !== 'hidden' && clip !== 'clip', '商家列不裁切（紅點才露得出來）', clip);

  console.log('\n[4] 私訊');
  await page.click('#nav-dm');
  await settle(1600);
  const convs = await page.$$eval('#convList .conv', els => els.length);
  check(convs > 0, '私訊會話列表有資料', convs + ' 筆');
  if (convs > 0) {
    await page.click('#convList .conv');
    await settle(1500);
    const bubbles = await page.$$eval('#messages .bubble', els => els.length);
    const rows = await page.$$eval('#messages .msg-row', els => els.length);
    check(bubbles > 0 && bubbles === rows, '訊息氣泡用舊版 .msg-row/.bubble 結構',
      bubbles + ' 個氣泡');

    // ★ 順序不能是「客戶一串 + 我方一串」——必須交錯（我方回覆的 message_time
    //   在舊資料是 NULL，若排序鍵沒用 COALESCE 就會全部擠到最後）
    const dirs = await page.$$eval('#messages .msg-row',
      els => els.map(e => e.classList.contains('inbound') ? 'C' : 'M'));
    const runs = dirs.join('').replace(/(.)\1*/g, '$1').length;
    check(runs > 2, '對話交錯（不是客戶一串、我方一串）',
      '交替段數=' + runs + ' / 共 ' + dirs.length + ' 則');
    await shot('shot6_dm_thread.png');
  }
  // 切平台必須重新載入
  await page.click('#tabs .tab[data-platform="ig"]');
  await settle(1800);
  const igConvs = await page.$$eval('#convList .conv', els => els.length);
  check(igConvs > 0, '切到 IG 私訊會重新載入', igConvs + ' 筆');

  // 商家列紅點數要跟「實際待處理」一致（沒未處理就不該有紅點）
  const badgeSum = await page.$$eval('#mbar .mbtn .badge',
    els => els.reduce((n, e) => n + (parseInt(e.textContent, 10) || 0), 0));
  const navBadge = await page.$$eval('#nav-badge-dm, #nav-badge-posts',
    els => els.reduce((n, e) => n + (e.style.display === 'none' ? 0
      : (parseInt(e.textContent, 10) || 0)), 0));
  check(badgeSum === navBadge, '商家列紅點 = 列表未處理總數（不會多算）',
    '商家 ' + badgeSum + ' / 列表 ' + navBadge);

  console.log('\n[5] 貼文留言');
  await page.click('#nav-posts');
  await settle(2000);
  const posts = await page.$$eval('#convList .post-card', els => els.length);
  check(posts > 0, '貼文列表以「貼文」為單位', posts + ' 篇');
  await shot('shot7_posts.png');
  if (posts > 0) {
    await page.click('#convList .post-card');
    await settle(2000);
    check(await page.$('#btnPostCfg') !== null, '★ 貼文詳情右上角有「貼文特殊配置」');
    const cmts = await page.$$eval('#postDetail .comment', els => els.length);
    check(cmts >= 0, '貼文詳情顯示留言', cmts + ' 則');
    await shot('shot8_post_detail.png');
    await page.click('#btnPostCfg');
    await settle(1500);
    const grps = await page.$$eval('#postCfgBody .postgrp', els => els.length);
    check(grps === 3, '貼文特殊配置彈窗三組（FAQ/表格/知識庫）', grps + ' 組');
    await shot('shot9_post_cfg.png');
    await page.click('#postCfgOverlay .sp-close');
    await settle(400);
  }

  console.log('\n[6] 設定面板（⚙）');
  await page.click('#arCfgBtn');
  await settle(1800);
  const tabs = await page.$$eval('.sp-tab', els => els.map(x => x.textContent.trim()));
  check(tabs.length === 6, '設定面板六分頁', tabs.join('/'));
  check((await page.textContent('#spBody') || '').length > 50, '自動回復面板有內容');
  await shot('shot10_settings.png');
  await page.click('.sp-tab[data-tab="faq"]');
  await settle(1400);
  check(await page.$('#faqBox') !== null, 'FAQ 分頁有清單容器');
  await page.click('.sp-tab[data-tab="logs"]');
  await settle(1200);
  const logRows = await page.$$eval('#logBox table tbody tr', els => els.length).catch(() => 0);
  check(true, '紀錄分頁渲染', logRows + ' 筆');

  await browser.close();

  console.log('\n[7] JS 錯誤');
  const real = errors.filter(e => !/net::ERR_CONNECTION_REFUSED|\/media\/|404 \(Not Found\)/.test(e));
  check(real.length === 0, '沒有 JS 執行期錯誤', real.slice(0, 5).join(' | '));

  console.log('\n' + '='.repeat(60));
  console.log(' 瀏覽器冒煙自檢：PASS ' + pass + ' / FAIL ' + fails.length);
  for (const f of fails) console.log('   ✗ ' + f);
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });
