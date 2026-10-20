/* 審計頁版面自檢（真 Chrome）—— 驗「一頁 20 條 / 不足 20 條靠上留空 / 不滾動」。
 *
 * 為什麼要真瀏覽器：`ops_audit_redesign_check.py` 只能驗程式碼長什麼樣；
 * 「表格被 flex 撐高 → 各列被拉開」這種事**只有真的排版才看得出來**
 * （2026-10-20 小jo 回報：條數不足時行距被拉開、像散落幾行）。
 *
 * 驗的不變式：
 *   [1] 每頁 20 條（視窗 ≥880 高）；且每一列行高等於 CSS 變數 --audit-row-h（沒被拉伸）
 *   [2] 整頁不滾動、表格區不滾動、分頁列在視窗內
 *   [3] 篩到不足 20 條時：表格緊貼頂部、下方留空（不置中、不拉開）
 *   [4] 全程無 JS 錯誤
 *
 * 跑法（需要先造一個「運維身分」的臨時 session，跑完刪掉）：
 *   cd crm && PYTHONPATH=<根>\crm;<根>\.venv\Lib\site-packages python -c "
 *     import psycopg2.extras; from app.core import db; from app.auth import session as s
 *     c=db.connect(); cur=c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 *     cur.execute(\"SELECT id,staff_name,roles,is_super_agent,is_kb_editor FROM crm_staff WHERE login_account='admin'\")
 *     print(s.create_session(dict(cur.fetchone()),'ops')[0])"
 *   SID=<上一步的 sid> NODE_PATH=<node_modules 有 playwright-core> node crm/tests/audit_layout_check.js
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const ROOT = path.resolve(__dirname, '..', '..');
const CHROME = 'C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe';
const BASE = 'http://127.0.0.1:8020';

let pass = 0; const fails = [];
function check(c, l, e) {
  if (c) { pass++; console.log('  PASS  ' + l + (e ? '   ' + e : '')); }
  else { fails.push(l); console.log('  FAIL  ' + l + (e ? '   ' + e : '')); }
}
(async () => {
  if (!process.env.SID) { console.error('缺少 SID 環境變數（臨時 session id）'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, headless: true, args: ['--no-sandbox'] });
  const errs = [];
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  // 臨時 session（由外部 python 造，跑完刪）
  await ctx.addCookies([{ name: 'mcrm_sid', value: process.env.SID, domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();
  page.on('pageerror', e => errs.push('pageerror: ' + e.message));

  for (const vp of [{ width: 1600, height: 900 }, { width: 1920, height: 1080 },
                    { width: 1280, height: 720 }]) {
    await page.setViewportSize(vp);
    await page.goto(BASE + '/ops', { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#nav button', { timeout: 15000 });
    await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll('#nav button'))
        .find(x => x.textContent.indexOf('審計') >= 0);
      if (b) b.click();
    });
    await page.waitForSelector('.audit-tw table', { timeout: 15000 });
    await page.waitForTimeout(1200);

    const m = await page.evaluate(() => {
      const view = document.querySelector('#view');
      const tw = document.querySelector('.audit-tw');
      const tbl = tw.querySelector('table');
      const rows = Array.from(tw.querySelectorAll('tr.a-row'));
      const pg = document.querySelector('.audit-pg');
      const bodyH = view.clientHeight;
      const twBox = tw.getBoundingClientRect();
      const tblBox = tbl.getBoundingClientRect();
      const heights = rows.map(r => Math.round(r.getBoundingClientRect().height));
      const barTxt = (document.querySelector('#auditBar') || {}).textContent || '';
      const mm = barTxt.match(/共\s*(\d+)\s*筆，一頁\s*(\d+)\s*筆/);
      return {
        wantRowH: parseInt(getComputedStyle(document.documentElement)
          .getPropertyValue('--audit-row-h') || '34', 10),
        size: mm ? Number(mm[2]) : -1, total: mm ? Number(mm[1]) : -1, n: rows.length,
        viewScroll: view.scrollHeight > view.clientHeight + 1,
        twScroll: tw.scrollHeight > tw.clientHeight + 1,
        twH: Math.round(twBox.height),
        tblH: Math.round(tblBox.height),
        // 最後一列底邊 vs 表格區底邊：條數不足時應該有剩餘空白
        gap: Math.round(twBox.bottom - tblBox.bottom),
        pgVisible: pg ? pg.getBoundingClientRect().bottom <= window.innerHeight + 1 : false,
        rowH: Array.from(new Set(heights)),
        bodyH,
      };
    });
    const wantRowH = m.wantRowH;
    const tag = vp.width + '×' + vp.height;
    const expect20 = vp.height >= 880;   // 900+ 的視窗就該給滿 20 條
    check(expect20 ? m.size === 20 : (m.size <= 20 && m.size >= 10),
      '一頁 20 條（' + tag + '）', 'size=' + m.size + ' 共' + m.total + (expect20 ? '' : '（此視窗較矮）'));
    check(m.n === Math.min(m.size === 20 ? 20 : m.size, m.total), '本頁列數合理（' + tag + '）', m.n + '/' + m.total);
    check(!m.viewScroll, '整頁不滾動（' + tag + '）');
    check(!m.twScroll, '表格區不滾動（' + tag + '）');
    check(m.pgVisible, '分頁列在視窗內（' + tag + '）');
    check(m.rowH.length === 1 && m.rowH[0] === wantRowH,
      '★ 每一列都是同一個行高（沒被拉伸）（' + tag + '）',
      JSON.stringify(m.rowH) + ' want ' + wantRowH);
    console.log('       表格區 ' + m.twH + 'px / 表格 ' + m.tblH + 'px / 底部留空 ' + m.gap + 'px');
    await page.screenshot({ path: path.join(__dirname, '_browser_out', 'audit_' + tag + '.png') });

    // 篩選到不足 20 條：只看失敗
    await page.click('[data-act="failed"]');
    await page.waitForTimeout(1500);
    const m2 = await page.evaluate(() => {
      const tw = document.querySelector('.audit-tw');
      const tbl = tw.querySelector('table');
      const rows = Array.from(tw.querySelectorAll('tr.a-row'));
      return {
        total: (function () {
          const t = (document.querySelector('.audit-pg .info') || {}).textContent || '';
          const m = t.match(/共\s*(\d+)\s*筆/);
          return m ? Number(m[1]) : -1;
        })(), n: rows.length,
        gap: Math.round(tw.getBoundingClientRect().bottom - tbl.getBoundingClientRect().bottom),
        tblTop: Math.round(tbl.getBoundingClientRect().top - tw.getBoundingClientRect().top),
        rowH: Array.from(new Set(rows.map(r => Math.round(r.getBoundingClientRect().height)))),
        firstTop: rows.length ? Math.round(rows[0].getBoundingClientRect().top - tw.getBoundingClientRect().top) : -1,
      };
    });
    check(m2.tblTop <= 2, '★★ 條數不足時表格緊貼頂部（不置中）（' + tag + '）', 'top=' + m2.tblTop);
    check(m2.gap > 0, '★★ 條數不足時下方留空（' + tag + '）', '共' + m2.total + ' 條，留空 ' + m2.gap + 'px');
    check(m2.rowH.length <= 1 && (m2.rowH[0] || wantRowH) === wantRowH,
      '★ 不足 20 條時行高也沒有被拉開（' + tag + '）', JSON.stringify(m2.rowH));
    await page.screenshot({ path: path.join(__dirname, '_browser_out', 'audit_failed_' + tag + '.png') });
  }

  check(errs.length === 0, '無 JS 錯誤', errs.slice(0, 3).join(' | '));
  await browser.close();
  console.log('\nRESULT pass=' + pass + ' fail=' + fails.length + (fails.length ? ' ' + JSON.stringify(fails) : ''));
})();
