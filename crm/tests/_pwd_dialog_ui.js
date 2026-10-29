// 真瀏覽器自檢：三張頁面的「修改密碼」彈窗
//   ① 按鈕可見可點  ② 彈窗真的打開（三個輸入框都在）  ③ 校驗訊息正確
//   ④ 真的改一次密碼 → 成功提示 → 舊密碼失效、新密碼可用（回 DB 由 python 端驗）
// 由 password_change_check.py 呼叫，環境變數：
//   CK_CHROME / CK_CASES(JSON) / CK_OLD / CK_NEW
const { chromium } = require('playwright-core');

const CRM = 'http://127.0.0.1:8020';
const CHROME = process.env.CK_CHROME;
const cases = JSON.parse(process.env.CK_CASES);
const OLD = process.env.CK_OLD;
const NEW = process.env.CK_NEW;

let pass = 0; const fails = [];
function check(c, m, extra) {
  console.log((c ? '  PASS  ' : '  FAIL  ') + m + (extra ? '   ' + extra : ''));
  if (c) pass++; else fails.push(m);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  let changed = false;         // 只在「主帳號」那頁真的改一次
  try {
    for (const c of cases) {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await ctx.addCookies([{ name: 'mcrm_sid', value: c.sid, domain: '127.0.0.1', path: '/' }]);
      const page = await ctx.newPage();
      const errs = [];
      page.on('pageerror', e => errs.push(String(e).slice(0, 120)));
      console.log(`\n  -- ${c.label}  ${c.path}`);
      try {
        await page.goto(CRM + c.path, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(800);

        // ① 「⋮」入口（2026-10-29 起，修改密碼收進三點選單）
        const more = page.locator('#btnMore');
        const n = await more.count();
        const vis = n ? await more.first().isVisible().catch(() => false) : false;
        const box = n ? await more.first().boundingBox().catch(() => null) : null;
        const vp = page.viewportSize();
        const inVp = !!(box && box.x >= 0 && box.y >= 0
          && box.x + box.width <= vp.width && box.y + box.height <= vp.height);
        check(n === 1, '有 #btnMore（三點入口）', `count=${n}`);
        check(vis, '★ 三點可見');
        check(inVp, '三點在視窗內可點', box ? `x=${Math.round(box.x)} y=${Math.round(box.y)}` : '—');
        check(errs.length === 0, '載入無 JS 錯誤', errs[0] || '');

        // ② 三點 → 修改密碼 → 彈窗
        await more.first().click();
        await page.waitForTimeout(350);
        const menuOpen = await page.evaluate(() =>
          document.getElementById('moreMenu').classList.contains('open'));
        check(menuOpen, '★ 三點 → 選單打開');
        const hasPwdItem = await page.locator('#morePwd').isVisible().catch(() => false);
        check(hasPwdItem, '選單裡有「修改密碼」');
        await page.locator('#morePwd').click();
        await page.waitForTimeout(450);
        const ov = page.locator('#pwdOv');
        check(await ov.isVisible().catch(() => false), '★ 彈窗打開了');
        for (const id of ['#pwdOld', '#pwdNew1', '#pwdNew2']) {
          check(await page.locator(id).isVisible().catch(() => false), `有 ${id}`);
        }
        const typ = await page.getAttribute('#pwdOld', 'type');
        check(typ === 'password', '舊密碼輸入框是 password 型別', typ || '—');
        // 三個框初始都是空的
        const empties = await page.evaluate(() =>
          ['pwdOld', 'pwdNew1', 'pwdNew2'].map(i => document.getElementById(i).value));
        check(empties.every(v => v === ''), '★ 三個欄位初始皆空（不預填任何東西）', JSON.stringify(empties));
        // 說明文字：管理員看不到密碼
        check(await page.locator('#pwdOv').innerText().then(t => t.includes('管理員看不到')),
          '★ 彈窗有「管理員看不到你的密碼」說明');

        // ③ 校驗：兩次新密碼不一致
        await page.fill('#pwdOld', 'whatever123');
        await page.fill('#pwdNew1', 'abcdefgh1');
        await page.fill('#pwdNew2', 'abcdefgh2');
        await page.click('#pwdOk');
        await page.waitForTimeout(300);
        let err = await page.locator('#pwdErr').innerText();
        check(err.includes('不一致'), '兩次不一致 → 提示「不一致」', err);

        // ③ 校驗：太短
        await page.fill('#pwdNew1', 'abc');
        await page.fill('#pwdNew2', 'abc');
        await page.click('#pwdOk');
        await page.waitForTimeout(300);
        err = await page.locator('#pwdErr').innerText();
        check(err.includes('8'), '少於 8 位 → 提示「至少 8 位」', err);

        // ③ 校驗：舊密碼錯（真的打後端）
        if (!changed) {
          await page.fill('#pwdOld', 'definitely-wrong-99');
          await page.fill('#pwdNew1', NEW);
          await page.fill('#pwdNew2', NEW);
          await page.click('#pwdOk');
          await page.waitForTimeout(1200);
          err = await page.locator('#pwdErr').innerText();
          check(err.includes('舊密碼'), '★ 舊密碼錯 → 後端拒絕並回報', err);
          check(err.includes('還可嘗試'), '有剩餘次數提示', err);

          // ④ 真的改成功
          await page.fill('#pwdOld', OLD);
          await page.fill('#pwdNew1', NEW);
          await page.fill('#pwdNew2', NEW);
          await page.click('#pwdOk');
          await page.waitForTimeout(1800);
          const closed = !(await page.locator('#pwdOv').isVisible().catch(() => false));
          check(closed, '★ 改成功後彈窗自動關閉');
          const toast = await page.evaluate(() => {
            // console_ui 用 #appToast / #appToastText；admin、ops 用 #toast
            const a = document.getElementById('appToastText');
            if (a && a.textContent) return a.textContent;
            const b = document.getElementById('toast');
            return b ? b.innerText : '';
          });
          check(toast.includes('密碼已修改'), '★ 有成功提示', toast.slice(0, 60));
          changed = true;
        } else {
          // 其他頁面只驗彈窗能開、能關（不動密碼，否則後面的 case 會登不進去）
          await page.click('#pwdCancel');
          await page.waitForTimeout(250);
          check(!(await page.locator('#pwdOv').isVisible().catch(() => false)),
            '取消 → 彈窗關閉');
        }

        // 關閉後欄位清空（密碼不留在 DOM）
        const after = await page.evaluate(() =>
          ['pwdOld', 'pwdNew1', 'pwdNew2'].map(i => document.getElementById(i).value));
        check(after.every(v => v === ''), '★ 關閉後欄位已清空（不留在 DOM）',
          JSON.stringify(after));

        check(errs.length === 0, '整個流程無 JS 錯誤', errs[0] || '');
      } catch (e) {
        check(false, '頁面斷言失敗', String(e).slice(0, 160));
      }
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  console.log('__RESULT__' + JSON.stringify({ pass, fails, changed }));
})();
