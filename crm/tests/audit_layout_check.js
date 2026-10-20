/* 審計頁版面自檢（真 Chrome）—— 驗「一頁 20 條 / 不足 20 條靠上留空 / 不滾動」。
 *
 * 為什麼要真瀏覽器：`ops_audit_redesign_check.py` 只能驗程式碼長什麼樣；
 * 「表格被 flex 撐高 → 各列被拉開」這種事**只有真的排版才看得出來**
 * （2026-10-20 小jo 回報：條數不足時行距被拉開、像散落幾行）。
 *
 * ★ 審計視圖是共用模組（`/ui/audit_view.js`），所以**兩台都要驗**：
 *   運維台（/ops，運維身分）與管理台（/，管理員身分）。
 *
 * 驗的不變式：
 *   [1] 每頁 20 條（視窗 ≥880 高）；每一列行高等於 CSS 變數 --audit-row-h（沒被拉伸）
 *   [2] 整頁不滾動、表格區不滾動、分頁列在視窗內
 *   [3] 篩到不足 20 條時：表格緊貼頂部、下方留空（不置中、不拉開）
 *   [4] 內容是人話（不是原始 JSON）、型別有中文標籤
 *   [5] 全程無 JS 錯誤、無 4xx 資源
 *
 * 跑法（需要先造臨時 session，跑完刪掉；運維台要 ops 身分，管理台要 admin）：
 *   cd crm && PYTHONPATH=<根>\crm;<根>\.venv\Lib\site-packages python -c "
 *     import psycopg2.extras; from app.core import db; from app.auth import session as s
 *     c=db.connect(); cur=c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 *     cur.execute(\"SELECT id,staff_name,roles,is_super_agent,is_kb_editor FROM crm_staff WHERE login_account='admin'\")
 *     r=dict(cur.fetchone()); print('OPS='+s.create_session(r,'ops')[0]); print('ADMIN='+s.create_session(r,'admin')[0])"
 *   SID_OPS=<...> SID_ADMIN=<...> NODE_PATH=<有 playwright-core 的 node_modules> \
 *     node crm/tests/audit_layout_check.js
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const CHROME = process.env.SMOKE_CHROME
  || 'C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe';
const BASE = process.env.SMOKE_BASE || 'http://127.0.0.1:8020';

let pass = 0; const fails = [];
function check(c, l, e) {
  if (c) { pass++; console.log('  PASS  ' + l + (e ? '   ' + e : '')); }
  else { fails.push(l); console.log('  FAIL  ' + l + (e ? '   ' + e : '')); }
}
function readEnv(p) {
  const t = fs.readFileSync(path.join(ROOT, 'crm', '.env'), 'utf8');
  const m = t.match(new RegExp('^' + p + '=(.*)$', 'm'));
  return m ? m[1].trim() : '';
}

/* 一台（/ops 或 /）的完整驗證 */
async function auditPage(browser, opts) {
  const errs = [];
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  await ctx.addCookies([{ name: 'mcrm_sid', value: opts.sid, domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  page.on('pageerror', e => errs.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  page.on('response', r => {
    if (r.status() >= 400 && !r.url().includes('favicon')) {
      errs.push('HTTP ' + r.status() + ' ' + r.url().replace(BASE, ''));
    }
  });

  console.log('\n==== ' + opts.name + '（' + opts.url + '） ====');

  for (const vp of [{ width: 1600, height: 900 }, { width: 1920, height: 1080 },
                    { width: 1280, height: 720 }]) {
    await page.setViewportSize(vp);
    await page.goto(BASE + opts.url, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#nav button', { timeout: 15000 });
    await page.evaluate((label) => {
      const b = Array.from(document.querySelectorAll('#nav button'))
        .find(x => x.textContent.indexOf(label) >= 0);
      if (b) b.click();
    }, opts.tabLabel);
    await page.waitForSelector('.audit-tw table', { timeout: 15000 });
    await page.waitForTimeout(1200);

    const m = await page.evaluate(() => {
      const view = document.querySelector('#view');
      const tw = document.querySelector('.audit-tw');
      const tbl = tw.querySelector('table');
      const rows = Array.from(tw.querySelectorAll('tr.a-row'));
      const pg = document.querySelector('.audit-pg');
      const barTxt = (document.querySelector('#auditBar') || {}).textContent || '';
      const mm = barTxt.match(/共\s*(\d+)\s*筆，一頁\s*(\d+)\s*筆/);
      const twBox = tw.getBoundingClientRect();
      const tblBox = tbl.getBoundingClientRect();
      // 前 3 列的內容欄（第 4 欄）文字，檢查是不是人話
      const texts = rows.slice(0, 3).map(r => {
        const td = r.children[3];
        return td ? td.textContent.trim() : '';
      });
      const tags = rows.slice(0, 3).map(r => {
        const td = r.children[2];
        return td ? td.textContent.trim() : '';
      });
      return {
        wantRowH: parseInt(getComputedStyle(document.documentElement)
          .getPropertyValue('--audit-row-h') || '34', 10),
        size: mm ? Number(mm[2]) : -1, total: mm ? Number(mm[1]) : -1, n: rows.length,
        viewScroll: view.scrollHeight > view.clientHeight + 1,
        twScroll: tw.scrollHeight > tw.clientHeight + 1,
        twH: Math.round(twBox.height),
        tblH: Math.round(tblBox.height),
        gap: Math.round(twBox.bottom - tblBox.bottom),
        pgVisible: pg ? pg.getBoundingClientRect().bottom <= window.innerHeight + 1 : false,
        rowH: Array.from(new Set(rows.map(r => Math.round(r.getBoundingClientRect().height)))),
        texts: texts, tags: tags,
      };
    });
    const tag = opts.name + ' ' + vp.width + '×' + vp.height;
    const tall = vp.height >= 880;

    check(tall ? m.size === 20 : (m.size <= 20 && m.size >= 10),
      '一頁 20 條（' + tag + '）',
      'size=' + m.size + ' 共' + m.total + (tall ? '' : '（此視窗較矮，物理放不下）'));
    check(m.n === Math.min(m.size, m.total), '本頁列數合理（' + tag + '）',
      m.n + '/' + m.total);
    check(!m.viewScroll, '整頁不滾動（' + tag + '）');
    check(!m.twScroll, '表格區不滾動（' + tag + '）');
    check(m.pgVisible, '分頁列在視窗內（' + tag + '）');
    check(m.rowH.length === 1 && m.rowH[0] === m.wantRowH,
      '★ 每一列都是同一個行高（沒被拉伸）（' + tag + '）',
      JSON.stringify(m.rowH) + ' want ' + m.wantRowH);

    // ★ 內容人話化：不能是 {"layer":"scope"...} 這種
    const jsonish = m.texts.filter(x => x.charAt(0) === '{' || x.indexOf('":"') >= 0);
    check(jsonish.length === 0, '★★ 列表內容是人話不是原始 JSON（' + tag + '）',
      jsonish.slice(0, 2).join(' | '));
    const rawTags = m.tags.filter(x => /^[a-z_]+$/.test(x) && x.indexOf('_') > 0);
    check(rawTags.length === 0, '★ 動作物件是中文標籤（' + tag + '）',
      rawTags.slice(0, 2).join(' | '));

    // 篩到不足 20 條
    await page.click('[data-act="failed"]');
    await page.waitForTimeout(1600);
    const m2 = await page.evaluate(() => {
      const tw = document.querySelector('.audit-tw');
      const tbl = tw.querySelector('table');
      const rows = Array.from(tw.querySelectorAll('tr.a-row'));
      const twBox = tw.getBoundingClientRect();
      const tblBox = tbl.getBoundingClientRect();
      return {
        n: rows.length,
        gap: Math.round(twBox.bottom - tblBox.bottom),
        tblTop: Math.round(tblBox.top - twBox.top),
        rowH: Array.from(new Set(rows.map(r => Math.round(r.getBoundingClientRect().height)))),
      };
    });
    check(m2.tblTop <= 2, '★★ 條數不足時表格緊貼頂部（不置中）（' + tag + '）',
      'top=' + m2.tblTop);
    check(m2.gap > 0, '★★ 條數不足時下方留空（' + tag + '）',
      '共 ' + m2.n + ' 條，留空 ' + m2.gap + 'px');
    check(m2.rowH.length <= 1 && (m2.rowH[0] || m.wantRowH) === m.wantRowH,
      '★ 不足 20 條時行高也沒被拉開（' + tag + '）', JSON.stringify(m2.rowH));
  }

  // 翻頁：第 2 頁不重複第 1 頁
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(BASE + opts.url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#nav button');
  await page.evaluate((label) => {
    const b = Array.from(document.querySelectorAll('#nav button'))
      .find(x => x.textContent.indexOf(label) >= 0);
    if (b) b.click();
  }, opts.tabLabel);
  await page.waitForSelector('.audit-tw table');
  await page.waitForTimeout(1000);

  /* 「顯示全部類型」：加上已下線的歷史類型 → 總數要變多（兩台都有這顆） */
  const totalOf = () => page.evaluate(() => {
    const t = (document.querySelector('.audit-pg .info') || {}).textContent || '';
    const m = t.match(/共\s*(\d+)\s*筆/);
    return m ? Number(m[1]) : -1;
  });
  const tBefore = await totalOf();
  const btns = await page.$$('#pageActs button');
  let allBtn = null;
  for (const b of btns) {
    if ((await b.textContent()).indexOf('顯示全部') >= 0) { allBtn = b; break; }
  }
  if (allBtn) {
    await allBtn.click();
    await page.waitForTimeout(1600);
    const tAfter = await totalOf();
    check(tAfter > tBefore, '★★「顯示全部類型」能看到已下線的歷史紀錄（' + opts.name + '）',
      tBefore + ' → ' + tAfter);
    await allBtn.click();                // 切回預設範圍，後面翻頁用
    await page.waitForTimeout(1200);
  }

  const ids1 = await page.$$eval('.a-row', els => els.map(e => e.dataset.id));
  await page.click('.pg-btn[data-p="2"]');
  await page.waitForTimeout(1200);
  const ids2 = await page.$$eval('.a-row', els => els.map(e => e.dataset.id));
  check(ids1.length > 0 && ids2.length > 0 && !ids1.some(x => ids2.includes(x)),
    '★★ 翻到第 2 頁沒有重複紀錄（' + opts.name + '）',
    'p1=' + ids1.length + ' p2=' + ids2.length);

  check(errs.length === 0, '無 JS 錯誤 / 4xx（' + opts.name + '）',
    errs.slice(0, 3).join(' | '));

  await ctx.close();
  return errs;
}

(async () => {
  const sidOps = process.env.SID_OPS;
  const sidAdmin = process.env.SID_ADMIN;
  if (!sidOps || !sidAdmin) {
    console.error('缺少 SID_OPS / SID_ADMIN（臨時 session id）');
    process.exit(2);
  }
  const browser = await chromium.launch({ executablePath: CHROME, headless: true,
                                          args: ['--no-sandbox'] });
  try {
    await auditPage(browser, { name: '運維台', url: '/ops', tabLabel: '審計', sid: sidOps });
    await auditPage(browser, { name: '管理台', url: '/', tabLabel: '審計', sid: sidAdmin });
  } finally {
    await browser.close();
  }
  console.log('\nRESULT pass=' + pass + ' fail=' + fails.length
    + (fails.length ? '\n  ' + JSON.stringify(fails) : ''));
  process.exit(fails.length ? 1 : 0);
})();
