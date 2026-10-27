/* 真瀏覽器檢查：商家管理頁的「解綁」入口與確認彈窗。
 *
 * 只輸出斷言結果，不截圖（小jo 明確要求：非必要不截圖）。
 *
 * 跑法（先由 python 造好 session 並傳入 sid）：
 *   NODE_PATH=<node_modules> node crm/tests/account_unbind_ui_check.js <sid>
 */
const { chromium } = require('playwright-core');

const CRM = 'http://127.0.0.1:8020';
const CHROME = 'C:/Users/its-admin/.agent-browser/browsers/chrome-155.0.8059.79/chrome.exe';
const COOKIE = 'mcrm_sid';
const sid = process.argv[2];
const csrf = process.argv[3];

let pass = 0;
const fails = [];
function check(cond, msg, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + msg + (extra ? '   ' + extra : ''));
  if (cond) pass++; else fails.push(msg);
}

(async () => {
  if (!sid) { console.log('  !! 缺少 session id'); process.exit(2); }

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.addCookies([{ name: COOKIE, value: sid, domain: '127.0.0.1', path: '/' }]);
  const page = await ctx.newPage();

  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });

  try {
    await page.goto(CRM + '/admin', { waitUntil: 'networkidle', timeout: 30000 });

    console.log('[1] 頁面載入');
    // 商家列表是動態渲染整張 table（沒有固定 tbody id）→ 用操作按鈕判斷已渲染
    check(await page.locator('button[data-act="biz-acct"]').count() > 0, '商家列表已渲染');
    check(errs.length === 0, '無 JS 錯誤', errs.slice(0, 2).join(' | '));

    console.log('\n[2] 打開「平台帳號」彈窗');
    // 找第一個有帳號的商家：直接點「平台帳號」按鈕
    const acctBtn = page.locator('button[data-act="biz-acct"]').first();
    check(await acctBtn.count() > 0, '有「平台帳號」按鈕');
    await acctBtn.click();
    await page.waitForTimeout(700);
    check(await page.locator('#ovAcct').evaluate(el => el.classList.contains('show')),
      '平台帳號彈窗已開啟');

    const rows = await page.locator('#acctRows tr').count();
    check(rows > 0, '已綁定帳號清單有資料', 'rows=' + rows);

    console.log('\n[3] 清單上的「解綁」按鈕');
    const delBtns = await page.locator('#acctRows button[data-acct-del]').count();
    check(delBtns === rows, '每個帳號列都有解綁按鈕', `${delBtns}/${rows}`);
    const delText = await page.locator('#acctRows button[data-acct-del]').first().textContent();
    check((delText || '').trim() === '解綁', '按鈕文字是「解綁」', JSON.stringify(delText));
    const delClass = await page.locator('#acctRows button[data-acct-del]').first().getAttribute('class');
    check((delClass || '').includes('danger'), '按鈕用危險樣式（不是普通 ghost）', delClass);

    console.log('\n[4] 點解綁 → 確認彈窗（不是原生 confirm）');
    await page.locator('#acctRows button[data-acct-del]').first().click();
    await page.waitForTimeout(400);

    check(await page.locator('#ovUnbind').evaluate(el => el.classList.contains('show')),
      '確認彈窗已顯示');
    console.log('        （若這裡卡住代表用了原生 confirm —— 它會阻塞，頁面就查不到節點）');

    const who = (await page.locator('#unbindWho').textContent()) || '';
    const plat = (await page.locator('#unbindPlat').textContent()) || '';
    const biz = (await page.locator('#unbindBiz').textContent()) || '';
    check(who.trim().length > 0 && who.trim() !== '—', '彈窗顯示帳號原生 ID', who.trim().slice(0, 40));
    check(plat.trim().length > 0 && plat.trim() !== '—', '彈窗顯示平台', plat.trim());
    check(biz.trim().length > 0 && biz.trim() !== '—', '彈窗顯示商家名', biz.trim());

    const noteText = (await page.locator('#ovUnbind .unbind-note').textContent()) || '';
    check(noteText.includes('憑證'), '說明了憑證會被刪');
    check(noteText.includes('貼文'), '說明了貼文配置會被刪');
    check(noteText.includes('歷史發送任務'), '說明了歷史任務保留');
    check(noteText.includes('不會再出現'), '說明了工作台不再顯示');

    const hint = (await page.locator('#ovUnbind .unbind-hint').textContent()) || '';
    check(hint.includes('停用'), '有「只想暫停請用停用」的提示', hint.trim().slice(0, 46));

    // ★ 版面要在彈窗**還開著**時量（關掉後 boundingBox 回 null）
    console.log('\n[4b] 版面：彈窗不溢出視窗');
    const box = await page.locator('#ovUnbind .box').boundingBox();
    const vp = page.viewportSize();
    check(box && box.x >= 0 && box.x + box.width <= vp.width + 1,
      '彈窗在視窗內（無橫向溢出）', box ? `x=${Math.round(box.x)} w=${Math.round(box.width)}` : 'null');
    const noteBox = await page.locator('#ovUnbind .unbind-note').boundingBox();
    check(noteBox && box && noteBox.y + noteBox.height <= box.y + box.height + 1,
      '注意事項區塊沒被裁掉');

    console.log('\n[5] 取消不應刪任何東西');
    await page.locator('#ovUnbind button[data-close]').click();
    await page.waitForTimeout(300);
    check(!(await page.locator('#ovUnbind').evaluate(el => el.classList.contains('show'))),
      '取消後彈窗關閉');
    const rowsAfter = await page.locator('#acctRows tr').count();
    check(rowsAfter === rows, '取消後帳號數量不變', `${rows} → ${rowsAfter}`);

    console.log('\n[6] 版面：帳號表格不出橫向捲軸');
    const tbl = await page.locator('#acctRows').evaluate(el => {
      const s = el.closest('.acct-scroll');
      return { sw: s.scrollWidth, cw: s.clientWidth };
    });
    check(tbl.sw <= tbl.cw + 1, '帳號表格沒有橫向捲軸（加了解綁欄也沒撐破）',
      `${tbl.sw}/${tbl.cw}`);

    check(errs.length === 0, '全程無 JS 錯誤', errs.slice(0, 3).join(' | '));

    // ---------------- 真實解綁：UI 點確認 → 真的刪掉 → 列表刷新
    console.log('\n[7] 真的按下「確認解綁」（用自建測試帳號，不碰真實資料）');
    const NATIVE = '__CK_ui_' + Math.random().toString(36).slice(2, 10) + '__';
    const made = await page.evaluate(async ({ native, csrf }) => {
      const post = (url, body) => fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(body),
      }).then(r => r.json());
      const b = await post('/api/admin/businesses', { business_name: 'ZZ解綁UI測試' });
      if (!b.ok) return { err: 'biz: ' + (b.error || '?') };
      const a = await post('/api/admin/accounts', {
        business_id: b.id, platform: 'fb', display_name: 'UI測試號',
        credential: { page_id: native, page_access_token: 'ui-' + native },
      });
      if (!a.ok) return { err: 'acct: ' + (a.error || '?') };
      return { biz: b.id, aid: a.id };
    }, { native: NATIVE, csrf });
    check(!made.err, '建立測試商家與帳號', made.err || `biz=${made.biz} acct=${made.aid}`);

    if (!made.err) {
      await page.reload({ waitUntil: 'networkidle' });
      // 找到該商家的「平台帳號」按鈕（按商家名定位）
      const rowBtn = page.locator('tr', { hasText: 'ZZ解綁UI測試' })
        .locator('button[data-act="biz-acct"]');
      await rowBtn.click();
      await page.waitForTimeout(700);
      const before = await page.locator('#acctRows button[data-acct-del]').count();
      const target = page.locator(`#acctRows button[data-acct-del="${made.aid}"]`);
      check(await target.count() === 1, '測試帳號出現在清單裡');

      await target.click();
      await page.waitForTimeout(350);
      await page.locator('#unbindOk').click();
      // 等 API + 兩層 loadAcct/loadBiz 回來
      await page.waitForFunction(
        (id) => !document.querySelector(`#acctRows button[data-acct-del="${id}"]`),
        made.aid, { timeout: 15000 },
      ).catch(() => {});

      // ★ 別數 <tr>：清單空了會渲染一行「尚未綁定平台帳號」的佔位列，
      //   數行數會誤判成「沒刪掉」。數「解綁按鈕」才是真的帳號數。
      const after = await page.locator('#acctRows button[data-acct-del]').count();
      check(after === before - 1, '★ 確認後該帳號列從清單消失', `${before} → ${after} 個帳號`);
      const emptyText = (await page.locator('#acctRows').textContent()) || '';
      check(before - 1 > 0 || emptyText.includes('尚未綁定'),
        '清單空了顯示「尚未綁定平台帳號」佔位', before - 1 > 0 ? '（還有其他帳號）' : '');
      check(!(await page.locator('#ovUnbind').evaluate(el => el.classList.contains('show'))),
        '解綁後彈窗自動關閉');
      const toastTxt = (await page.locator('#toast').textContent()) || '';
      check(toastTxt.includes('已解綁'), 'toast 有成功提示', toastTxt.trim());

      // 後端確認：真的沒了（不是只有畫面移除）
      const gone = await page.evaluate(async (id) => {
        const r = await fetch('/api/admin/accounts', { credentials: 'same-origin' });
        const d = await r.json();
        return !(d.items || []).some(x => x.id === id);
      }, made.aid);
      check(gone, '★ 後端清單也沒有它（真刪，不是 UI 假動作）');
    }

  } catch (e) {
    check(false, '腳本執行例外', String(e).slice(0, 200));
  } finally {
    await browser.close();
  }

  console.log('\n' + '='.repeat(62));
  console.log(`  解綁 UI：PASS ${pass} / FAIL ${fails.length}`);
  fails.forEach(m => console.log('    FAIL:', m));
  process.exit(fails.length ? 1 : 0);
})();
