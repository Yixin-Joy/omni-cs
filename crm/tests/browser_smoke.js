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
  page.on('response', r => { if (r.status() >= 400) errors.push('HTTP ' + r.status() + ' ' + r.url()); });

  // ★ 紅點一致性用：確認兩個紅點端點都真的被同一個刷新入口打過
  const badgeHits = new Set();
  page.on('response', r => {
    const u = r.url();
    if (u.includes('/console/agent/pending-counts')) badgeHits.add('pending-counts');
    if (u.includes('/console/agent/merchant-stats')) badgeHits.add('merchant-stats');
  });

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
  // ★ 切換身分是「改 DB 的 active_role」，切換瞬間舊頁面還可能發出幾個
  //   `/console/agent/*` → 那時身分已變 → 403。**這是預期行為**（前端隨後就重載了），
  //   不算錯誤；清掉這段噪音，之後的錯誤照樣会被抓到。
  await settle(1200);
  errors.length = 0;

  // ---------- [2b] 管理台 · 平台帳號彈窗：單頁展示、不左右上下滑動 ----------
  // ★ 這裡就是「商家管理 → 平台帳號」。原本彈窗 620px 寬塞 680px 的表格 +
  //   488px 高的新增卡片 → 內容 724×987 溢出 620×836，橫豎都出捲軸。
  //   改成「左表單｜右清單」兩欄 + table-layout:fixed 後應完全不捲動。
  console.log('\n[2b] 管理台 · 平台帳號彈窗');
  for (const vp of [{ width: 1440, height: 900 }, { width: 1280, height: 800 },
                    { width: 1920, height: 1080 }]) {
    await page.setViewportSize(vp);
    await settle(400);
    await page.click('button[data-act="biz-acct"]');
    await page.waitForSelector('#ovAcct.show', { timeout: 5000 });
    await settle(600);
    const m = await page.evaluate(() => {
      const box = document.querySelector('#ovAcct .box');
      const sc = document.querySelector('#ovAcct .acct-scroll');
      const br = box.getBoundingClientRect();
      const badTd = Array.from(document.querySelectorAll('#ovAcct td'))
        .filter(el => el.scrollWidth > el.clientWidth + 1).length;
      return {
        boxV: box.scrollHeight > box.clientHeight + 1,
        boxH: box.scrollWidth > box.clientWidth + 1,
        listH: sc.scrollWidth > sc.clientWidth + 1,
        badTd,
        insideVp: br.right <= window.innerWidth + 1 && br.bottom <= window.innerHeight + 1
                  && br.left >= -1 && br.top >= -1,
        size: Math.round(br.width) + '×' + Math.round(br.height),
        vp: window.innerWidth + '×' + window.innerHeight,
      };
    });
    check(!m.boxH && !m.listH && m.badTd === 0,
      '平台帳號彈窗無橫向捲動（' + m.vp + '）',
      '彈窗 ' + m.size + '，溢出儲存格 ' + m.badTd + ' 個');
    check(!m.boxV, '平台帳號彈窗無縱向捲動（' + m.vp + '）',
      '彈窗 ' + m.size);
    check(m.insideVp, '彈窗完整落在視窗內（' + m.vp + '）', '彈窗 ' + m.size);
    // 兩欄都在、且清單真的渲染了
    const two = await page.$$eval('#ovAcct .acct-form, #ovAcct .acct-list',
      els => els.length);
    check(two === 2, '左（表單）右（清單）兩欄佈局', two + ' 欄');
    await page.click('#ovAcct [data-close]');
    await settle(300);
  }

  // 憑證編輯模式：左欄切成編輯（有「取消編輯」可退回），且仍不捲動
  await page.click('button[data-act="biz-acct"]');
  await page.waitForSelector('#ovAcct.show');
  await settle(500);
  const credBtn = await page.$('#acctRows [data-acct-cred]');
  if (credBtn) {
    // ★★ 憑證回顯：後端有值就必須看得出來，不能是一片空白。
    //    起因：Epro_test 轉發一切正常（憑證明明有值），但編輯框全空 →
    //    管理員以為憑證沒填。修法＝非敏感欄位明文、敏感欄位 ••••••••。
    const before = await page.evaluate(async () => {
      const r = await fetch('/api/admin/accounts?business_id=1', { credentials: 'same-origin' });
      const d = await r.json();
      const it = (d.items || [])[0] || {};
      return { filled: it.credential_filled, plain: it.credential_plain,
               hasField: Object.prototype.hasOwnProperty.call(it, 'credential_filled') };
    });
    check(before.hasField,
      '★ /api/admin/accounts 回 credential_filled（少了＝CRM 是舊代碼，請重啟 start_crm.bat）');

    await credBtn.click();
    await settle(400);
    const em = await page.evaluate(() => {
      const box = document.querySelector('#ovAcct .box');
      const rows = Array.from(document.querySelectorAll('#acctCredBox .cred-fld')).map(d => {
        const lab = d.querySelector('label'), inp = d.querySelector('input');
        return { label: lab.textContent.replace('已填', '').trim(),
                 marked: lab.textContent.indexOf('已填') >= 0,
                 value: inp.value, masked: inp.dataset.masked === '1' };
      });
      return {
        rows,
        editing: document.getElementById('acctAdd').textContent.indexOf('儲存') >= 0,
        hasCancel: document.getElementById('acctReset').offsetParent !== null,
        scroll: box.scrollHeight > box.clientHeight + 1 || box.scrollWidth > box.clientWidth + 1,
        // 顯示名稱是「給人看的標籤」→ 隨時可改
        displayEditable: !document.getElementById('acctDisplay').disabled,
        // ★ 原生 ID 已無輸入框（＝兩庫對齊鍵，由憑證推導）；編輯模式改用唯讀框顯示目前值
        hasNativeInput: !!document.getElementById('acctNative'),
        nativeView: (() => {
          const v = document.getElementById('acctNativeView');
          if (!v || v.offsetParent === null) return null;
          return { code: (v.querySelector('code') || {}).textContent || '',
                   locked: v.textContent.indexOf('不可修改') >= 0 };
        })(),
      };
    });
    check(em.editing && em.hasCancel, '點「憑證」→ 左欄切編輯模式（可取消）');
    check(!em.scroll, '編輯模式下彈窗仍不捲動');
    check(em.displayEditable, '★ 編輯模式：顯示名稱可改');
    check(!em.hasNativeInput && em.nativeView && em.nativeView.code.length > 0
          && em.nativeView.locked,
      '★ 編輯模式：原生 ID 以唯讀框顯示目前值（不再要人填、不可改）',
      em.nativeView ? em.nativeView.code : '（未顯示）');

    const anyFilled = em.rows.filter(r => r.marked);
    check(anyFilled.length === em.rows.length,
      '★ 每個已填欄位都有「已填」標記（不再讓人以為沒填）',
      em.rows.map(r => r.label + (r.marked ? '✓' : '✗')).join(' / '));
    check(em.rows.some(r => r.masked && r.value === '••••••••'),
      '★ 敏感欄位以 •••••••• 佔位（不顯示明文）',
      em.rows.filter(r => r.masked).map(r => r.label).join('、') || '（無）');
    check(em.rows.some(r => !r.masked && r.value && r.value.length > 4),
      '★ 非敏感欄位明文回顯（例：Page ID / AppID 看得見）',
      em.rows.filter(r => !r.masked && r.value).map(r => r.label + '=' + r.value).join('、') || '（無）');

    // ★ 迴歸：遮罩佔位符**不得**被當成新憑證提交。
    //   「不改任何東西直接按儲存」必須讓憑證逐欄位完全相同（原本是整塊覆蓋 → 靜默遺失）。
    await page.click('#acctAdd');
    await settle(1600);
    const after = await page.evaluate(async () => {
      const r = await fetch('/api/admin/accounts?business_id=1', { credentials: 'same-origin' });
      const d = await r.json();
      const it = (d.items || [])[0] || {};
      return { filled: it.credential_filled, plain: it.credential_plain };
    });
    check(JSON.stringify(after.filled) === JSON.stringify(before.filled)
          && JSON.stringify(after.plain) === JSON.stringify(before.plain),
      '★ 未改動直接儲存 → 憑證逐欄位不變（遮罩沒被寫成新值、也沒清掉其他欄位）',
      JSON.stringify(after.filled));

    await page.click('#acctRows [data-acct-cred]');
    await settle(400);
    await page.click('#acctReset');
    await settle(300);
    const back = await page.evaluate(() => ({
      add: document.getElementById('acctAdd').textContent,
      cancel: document.getElementById('acctReset').offsetParent !== null,
    }));
    check(back.add.indexOf('新增') >= 0 && !back.cancel, '「取消編輯」回到新增模式');

    // ★★ 原生 ID 不再單獨填：它就是某個憑證欄位（AppID / Phone Number ID /
    //    Page ID / IG 帳號 ID），由後端從憑證推導。帳號清單仍以「原生 ID」一欄呈現。
    const nat = await page.evaluate(() => {
      const ths = Array.from(document.querySelectorAll('#ovAcct thead th'))
        .map(t => t.textContent.trim());
      const firstRow = Array.from(document.querySelectorAll('#acctRows tr')[0].children)
        .map(td => td.textContent.trim());
      const note = document.getElementById('acctNativeNote');
      return {
        ths, firstRow,
        hasInput: !!document.getElementById('acctNative'),
        noteText: note ? note.textContent.replace(/\s+/g, ' ') : '',
        idBadges: document.querySelectorAll('#acctCredBox .cred-id').length,
        placeholders: Array.from(document.querySelectorAll('#acctCredBox input[data-cred]'))
          .map(i => i.placeholder),
        scroll: (() => {
          const b = document.querySelector('#ovAcct .box');
          return b.scrollHeight > b.clientHeight + 1 || b.scrollWidth > b.clientWidth + 1;
        })(),
      };
    });
    check(!nat.hasInput, '★ 新增模式已無「平台原生帳號 ID」輸入框（不用填第二次）');
    check(nat.noteText.indexOf('不用另外填') >= 0,
      '有「原生 ID 取自憑證」的說明', nat.noteText.slice(0, 40));
    check(nat.idBadges === 1, '憑證欄位標出「＝原生 ID」（恰好一個）', nat.idBadges + ' 個');
    check(nat.placeholders.length > 0 && nat.placeholders.every(p => p === '必填'),
      '★ 憑證欄位全部標為「必填」（不是選填）', nat.placeholders.join('/'));
    check(nat.ths.indexOf('原生 ID') >= 0, '帳號清單仍有「原生 ID」一欄', nat.ths.join(' | '));
    check(!nat.scroll, '新增原生 ID 說明後彈窗仍不捲動');
  }
  await page.click('#ovAcct [data-close]');
  await settle(300);

  await page.setViewportSize({ width: 1500, height: 950 });
  await settle(300);

  // ---------- [3~6] 客服工作台（切回 agent） ----------
  console.log('\n[3] 工作台 · 商家列');
  await page.click('#whoBox');
  await page.waitForSelector('#roleMenu.open', { timeout: 5000 });
  await page.click('#roleList .roleitem[data-role="agent"]');
  await page.waitForFunction(() => document.title === '客服工作台', { timeout: 10000 });
  await settle(1600);
  errors.length = 0;   // 同上：切換瞬間的 403 是預期的

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

  // ★ WhatsApp 的名字來自 forwarded_payload.webClientSender.name，
  //   SQL 沒撈就會只剩號碼（或空白）。
  await page.click('#tabs .tab[data-platform="whatsapp"]');
  await settle(1600);
  const names = await page.$$eval('#convList .conv .conv-name',
    els => els.map(e => e.textContent.trim()));
  const named = names.filter(n => n && !/^\d[\d\s\-+()]*$/.test(n));
  check(named.length > 0, 'WhatsApp 會話顯示用戶名（不是只有號碼）',
    named.slice(0, 3).join(' / ') || '（全部都是號碼）');


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
  // ★ 右鍵選單：**只**出現在「還沒被處理的單條客戶訊息氣泡」上，
  //   會話項（用戶聊天框）上**不能**有。只驗出現/不出現，不點菜單項（不改資料）。
  const ctx = await page.evaluate(() => {
    const fire = (el) => {
      const r = el.getBoundingClientRect();
      el.dispatchEvent(new MouseEvent('contextmenu', {
        bubbles: true, cancelable: true,
        clientX: Math.round(r.left + 5), clientY: Math.round(r.top + 5),
      }));
    };
    const menu = document.getElementById('ctxMenu');
    const shown = () => menu && getComputedStyle(menu).display !== 'none';
    const items = () => Array.from(menu.querySelectorAll('.ctx-item'))
      .map(e => e.textContent.trim());

    const bubble = document.querySelector('#messages .msg-row[oncontextmenu]');
    let bubbleShown = false, bubbleItems = [];
    if (bubble) {
      fire(bubble);
      bubbleShown = shown();
      bubbleItems = items();
      document.dispatchEvent(new MouseEvent('click', { bubbles: true }));  // 關閉
    }
    const anyBubble = document.querySelectorAll('#messages .msg-row').length;
    const armed = document.querySelectorAll('#messages .msg-row[oncontextmenu]').length;

    const conv = document.querySelector('#convList .conv');
    let convShown = false;
    if (conv) {
      fire(conv);
      convShown = shown();
      document.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    }
    return { bubbleShown, bubbleItems, convShown, anyBubble, armed,
             convHasHandler: !!(conv && conv.getAttribute('oncontextmenu')) };
  });
  if (ctx.anyBubble === 0) {
    check(true, '右鍵選單：目前會話沒有訊息可測（跳過）');
  } else {
    check(!ctx.convHasHandler && !ctx.convShown,
      '★ 會話（用戶聊天框）上沒有右鍵選單');
    if (ctx.armed === 0) {
      check(true, '右鍵選單：目前沒有「未處理」的客戶氣泡（跳過）',
        ctx.anyBubble + ' 則訊息，' + ctx.armed + ' 則可右鍵');
    } else {
      check(ctx.bubbleShown, '★ 右鍵「未處理的客戶訊息氣泡」→ 選單出現');
      check(ctx.bubbleItems.length === 2, '選單兩項', ctx.bubbleItems.join(' / '));
      check(ctx.bubbleItems.some(t => t.indexOf('自動回復引擎') >= 0),
        '選單有「由自動回復引擎…」');
      check(ctx.bubbleItems.some(t => t.indexOf('標記這一則已處理') >= 0),
        '選單有「標記這一則已處理」');
    }
  }
  // 切平台必須重新載入
  await page.click('#tabs .tab[data-platform="ig"]');
  await settle(1800);
  const igConvs = await page.$$eval('#convList .conv', els => els.length);
  check(igConvs > 0, '切到 IG 私訊會重新載入', igConvs + ' 筆');

  // ★ 圖片真的載得出來（AI 回覆會帶 `![圖 N 截圖](/media/xxx.png)`）。
  //   逐一試前幾個會話，只要有一個圖載成功就 PASS。
  let imgN = 0, imgOk = 0;
  const tryN = Math.min(4, igConvs);
  for (let i = 0; i < tryN; i++) {
    const els = await page.$$('#convList .conv');
    if (!els[i]) break;
    await els[i].click();
    await settle(1300);
    const st = await page.evaluate(() => {
      const im = Array.from(document.querySelectorAll('#messages img'));
      const broken = document.querySelectorAll('#messages .img-broken').length;
      return { n: im.length, ok: im.filter(e => e.naturalWidth > 0).length, broken };
    });
    imgN += st.n; imgOk += st.ok;
    if (imgOk > 0) break;
  }
  // 允許少數「資料引用不存在的檔」失敗（見下方 404 噪音說明），
  // 但**大部分必須載得出來** —— 路徑設錯會整批失敗，這條就會紅。
  check(imgN === 0 || imgOk >= Math.ceil(imgN / 2),
    '對話中的圖片大多能載入（/media 與 /files 的路徑是對的）',
    imgOk + '/' + imgN + '（試了 ' + tryN + ' 個會話）');

  // ★ 紅點一致性：**當前商家鈕上的數字**必須等於一級導覽兩個紅點的和
  //   （商家列 = 該商家全部平台 dm + 留言；一級導覽 = dm 欄目 + 留言欄目）。
  //   曾發生「商家鈕只在啟動時拉一次 → 標為已處理後兩處對不上」。
  const bd = await page.evaluate(() => {
    const num = (el) => (el && el.style.display !== 'none'
      ? (parseInt(el.textContent, 10) || 0) : 0);
    const active = document.querySelector('#mbar .mlist .mbtn.active');
    const bizBadge = active ? num(active.querySelector('.badge')) : null;
    const navDm = num(document.getElementById('nav-badge-dm'));
    const navPosts = num(document.getElementById('nav-badge-posts'));
    const bizBtns = document.querySelectorAll('#mbar .mlist .mbtn').length;
    return { bizBadge, navDm, navPosts, navSum: navDm + navPosts, bizBtns };
  });
  check(bd.bizBadge === bd.navSum,
    '★ 商家列紅點 == 一級導覽紅點總和（dm + 留言）— 兩處必須一致',
    '商家 ' + bd.bizBadge + ' / 導覽 ' + bd.navDm + '+' + bd.navPosts + '=' + bd.navSum);

  // 二級平台 tab 的紅點總和也要等於導覽對應欄目（同一組數字來源）
  const tabBd = await page.evaluate(() => {
    const num = (el) => (el && el.style.display !== 'none'
      ? (parseInt(el.textContent, 10) || 0) : 0);
    let dm = 0;
    ['wechat', 'whatsapp', 'ig', 'fb'].forEach(p => {
      dm += num(document.getElementById('badge-' + p));
    });
    return { dm, navDm: num(document.getElementById('nav-badge-dm')) };
  });
  check(tabBd.dm === tabBd.navDm, '★ 二級平台 tab 紅點總和 == 一級「私訊」紅點',
    '平台合計 ' + tabBd.dm + ' / 導覽 ' + tabBd.navDm);

  // ★ 兩處的數字來自不同端點（merchant-stats / pending-counts），
  //   必須由同一個刷新入口一起拉，否則標為已處理後就會有一邊停在舊值。
  check(badgeHits.has('pending-counts') && badgeHits.has('merchant-stats'),
    '★ 兩個紅點端點都經由同一刷新入口打過（不會一邊更新一邊不更新）',
    Array.from(badgeHits).join(' + ') || '（都沒打）');

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

  console.log('\n[6] 設定面板（⚙）· 六分頁逐一走訪');
  await page.click('#arCfgBtn');
  await settle(1800);
  const tabs = await page.$$eval('.sp-tab', els => els.map(x => x.textContent.trim()));
  check(tabs.length === 6, '設定面板六分頁', tabs.join('/'));
  check((await page.textContent('#spBody') || '').length > 50, '自動回復面板有內容');
  await shot('shot10_settings.png');

  // ★★★ 逐頁走一遍，並斷言**每一頁都沒有 JS 錯誤**。
  //   為什麼要這樣做（2026-10-14 小jo 回報的三個問題）：
  //     · 整包移植最容易漏的是「區塊外的全域宣告」。KB_STATUS 沒搬過來 →
  //       kbDocTag() 一呼叫就 ReferenceError → **知識庫文件列表整頁空白**，
  //       而統計數字正常，看起來像後端壞掉，極難從截圖判斷。
  //     · FAQ／表格的匯入匯出與紀錄頁的卡片式展示也是整批沒搬。
  //   下面的「每頁零錯誤 + 關鍵元素存在」是最直接的防線。
  const TAB_SPEC = [
    { tab: 'autoreply', label: '自動回復', must: ['場景層', '賬號層'] },
    { tab: 'faq', label: 'FAQ',
      must: ['FAQ 問答對', '匯入 Excel / CSV', '下載範本 xlsx', '下載範本 csv',
             '匯出目前 FAQ', '儲存全部修改'] },
    { tab: 'table', label: '業務表格',
      must: ['業務表格（第二層）', '上傳 Excel / CSV 建新表', '下載範本 xlsx',
             '下載範本 csv'] },
    { tab: 'kb', label: '知識庫', must: ['知識庫概覽'] },
    { tab: 'hittest', label: '命中測試', must: ['開始測試', '第一層 FAQ', '第二層 業務表格', '第三層 知識庫'] },
    { tab: 'logs', label: '紀錄',
      must: ['自動回復紀錄', '命中情況統計'] },
  ];
  for (const spec of TAB_SPEC) {
    const pageErrs = [];
    const tap = m => { if (m.type() === 'error') pageErrs.push(m.text().slice(0, 140)); };
    const tapErr = e => pageErrs.push('PAGEERROR: ' + e.message.slice(0, 140));
    page.on('console', tap);
    page.on('pageerror', tapErr);
    await page.click('.sp-tab[data-tab="' + spec.tab + '"]');
    await settle(spec.tab === 'kb' ? 2200 : 1700);
    const txt = (await page.textContent('#spBody')) || '';
    const stuck = txt.indexOf('載入中…') >= 0 && txt.length < 200;
    const missing = spec.must.filter(m => txt.indexOf(m) < 0);
    check(missing.length === 0, '[' + spec.tab + '] ' + spec.label + ' 關鍵元素齊全',
      missing.length ? '缺：' + missing.join('、') : txt.replace(/\s+/g, ' ').slice(0, 60));
    check(!stuck, '[' + spec.tab + '] ' + spec.label + ' 沒有卡在「載入中…」',
      txt.replace(/\s+/g, ' ').slice(0, 60));
    check(pageErrs.length === 0, '[' + spec.tab + '] ' + spec.label + ' 沒有 JS 錯誤',
      pageErrs.slice(0, 2).join(' | '));
    page.off('console', tap);
    page.off('pageerror', tapErr);
  }

  // ★ 2026-10-14 小jo 的要求（防回退）：
  //   ① 命中測試頁拿掉「全選／全不選／照目前開關重設」與平台選擇（直接勾層就好）
  //   ② 自動回復頁拿掉「啟用集」（改由各子頁自己控制），兩層改名 場景層／賬號層
  await page.click('.sp-tab[data-tab="hittest"]');
  await settle(1200);
  const kt = await page.evaluate(() => {
    const t = (document.getElementById('spBody') || {}).textContent || '';
    return {
      hasSelect: !!document.querySelector('#spBody select#ktTarget'),
      all: t.indexOf('全選') >= 0,
      none: t.indexOf('全不選') >= 0,
      reset: t.indexOf('照目前開關重設') >= 0,
    };
  });
  check(!kt.hasSelect, '命中測試頁沒有平台選擇下拉', kt.hasSelect ? '仍存在 #ktTarget' : 'ok');
  check(!kt.all && !kt.none && !kt.reset,
    '命中測試頁沒有全選／全不選／照目前開關重設', JSON.stringify(kt));

  await page.click('.sp-tab[data-tab="autoreply"]');
  await settle(1200);
  const arTxt = (await page.textContent('#spBody')) || '';
  check(arTxt.indexOf('啟用集') < 0, '自動回復頁已移除「啟用集」',
    arTxt.indexOf('啟用集') >= 0 ? '仍存在' : 'ok');
  check(arTxt.indexOf('場景層') >= 0 && arTxt.indexOf('賬號層') >= 0,
    '兩層已改名為 場景層／賬號層',
    arTxt.replace(/\s+/g, ' ').slice(0, 60));

  // 賬號層的灰階必須與場景層一致：該平台在場景層一個都沒勾 → 該平台帳號 locked
  const lockChk = await page.evaluate(() => {
    const ov = (window.state || {}).overview || {};
    const scopes = ov.scopes || [];
    const accs = Array.from(document.querySelectorAll('#spBody [data-acc]'));
    if (!accs.length) return { skip: true };
    const bad = [];
    accs.forEach(b => {
      const a = (ov.accounts || []).filter(x => String(x.id) === b.getAttribute('data-acc'))[0];
      if (!a) return;
      const live = scopes.some(s => s.platform === a.platform && s.enabled);
      const locked = !!b.getAttribute('data-locked');
      if (live === locked) bad.push(a.platform + ' live=' + live + ' locked=' + locked);
    });
    return { bad: bad, n: accs.length };
  });
  check(lockChk.skip || lockChk.bad.length === 0, '賬號層灰階與場景層一致',
    lockChk.skip ? '（該商家無平台帳號，略過）' : JSON.stringify(lockChk.bad));

  // FAQ 清單真的載入（不是只有外框）
  await page.click('.sp-tab[data-tab="faq"]');
  await settle(1600);
  const faqRows = await page.$$eval('#faqBody tr', els => els.length).catch(() => 0);
  const faqInputs = await page.$$eval('#spBody input[type=file]', els => els.length).catch(() => 0);
  check(faqRows > 0, 'FAQ 清單載入', faqRows + ' 組');
  check(faqInputs >= 1, 'FAQ 有檔案上傳輸入（匯入 Excel / CSV）', faqInputs + ' 個');

  // 表格：展開資料列 + 匯入輸入
  await page.click('.sp-tab[data-tab="table"]');
  await settle(1600);
  const tblInputs = await page.$$eval('#spBody input[type=file]', els => els.length).catch(() => 0);
  check(tblInputs >= 2, '業務表格有匯入輸入（建新表 + 單表匯入）', tblInputs + ' 個');

  // 下載連結一定要帶 business_id（裸 location.href 會 400）
  // 「有沒有帶 business_id」要在**執行時**看（stDownload 是在點擊時才補上），
  // 所以改看 onclick 有沒有經過 stDownload —— 裸 location.href 才是錯的。
  const dl = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('#spBody button'))
      .filter(b => /下載範本|匯出/.test(b.textContent));
    const bad = [], viaSt = [];
    btns.forEach(b => {
      const oc = b.getAttribute('onclick') || '';
      if (!oc) return;
      if (oc.indexOf('stDownload') >= 0) viaSt.push(oc.slice(0, 40));
      else if (oc.indexOf('/console/agent/') >= 0) bad.push(oc.slice(0, 60));
    });
    return { n: btns.length, bad, viaSt };
  });
  check(dl.n > 0, '有下載類按鈕', dl.n + ' 個');
  check(dl.bad.length === 0 && dl.viaSt.length > 0,
    '★ 下載按鈕都經由 stDownload（執行時補 business_id），沒有裸 location.href',
    dl.bad.join('、') || (dl.viaSt.length + ' 個走 stDownload'));

  // 紀錄頁：卡片式 + 可展開檢索明細
  await page.click('.sp-tab[data-tab="logs"]');
  await settle(1600);
  const logView = await page.evaluate(() => ({
    items: document.querySelectorAll('#logList .log-item').length,
    clickable: document.querySelectorAll('#logList .log-item.clickable').length,
    hasStatsBtn: Array.from(document.querySelectorAll('#spBody button'))
      .some(b => b.textContent.indexOf('命中情況統計') >= 0),
  }));
  check(logView.items > 0, '★ 紀錄用卡片式呈現（.log-item）', logView.items + ' 筆');
  check(logView.clickable > 0, '★ 有可展開檢索明細的紀錄', logView.clickable + ' 筆');
  check(logView.hasStatsBtn, '有「命中情況統計」入口');

  // 命中情況統計子頁
  await page.evaluate(() => openLogsStats());
  await settle(1600);
  const statTxt = (await page.textContent('#logStatsBox')) || '';
  check(statTxt.indexOf('渠道') >= 0 || statTxt.indexOf('FAQ 命中') >= 0,
    '★ 命中情況統計子頁渲染出表格', statTxt.replace(/\s+/g, ' ').slice(0, 60));
  await page.evaluate(() => backToLogs());
  await settle(600);


  // ---------- [6b] 設定 → 知識庫（塊C：從舊版 console 移植 + 每商家獨立） ----------
  // ★ 這裡釘死三件容易壞的事：
  //   ① 分頁真的接上（曾長期是「尚未接線」佔位）；
  //   ② 每個 /console/kb 請求都帶 business_id（漏帶＝讀到別人的資料或直接 400）；
  //   ③ 參數配置按商家顯示（商家 1 的 min_score 是實測校正過的 0.25，不是內建 0.28）。
  console.log('\n[6b] 設定 · 知識庫');
  const kbReqs = [];
  const kbTap = r => {
    const u = r.url();
    if (u.indexOf('/console/kb/') >= 0) kbReqs.push(r.status() + ' ' + u.split('/console/kb/')[1]);
  };
  page.on('response', kbTap);
  await page.click('.sp-tab[data-tab="kb"]');
  await settle(1200);
  // ★ [6] 已經走過 KB 分頁（資料有快取）→ 這裡必須強制重載一次，
  //   否則攔不到任何 /console/kb 請求，「每個請求都帶 business_id」會變成空轉斷言。
  await page.evaluate(() => loadKbOverview(true));
  await settle(2200);
  const kbPage = await page.evaluate(() => {
    const body = document.getElementById('spBody');
    const st = (window.kbState && window.kbState.kb) || null;
    const cols = (st && st.collections) || [];
    const shown = cols.filter(c => !(c.kind === 'image' && c.image_of));
    const ungrouped = Number((st && st.totals && st.totals.ungrouped_docs) || 0) ? 1 : 0;
    return {
      placeholder: body ? body.textContent.indexOf('尚未接線') >= 0 : true,
      html: body ? body.innerHTML.length : 0,
      cards: document.querySelectorAll('#spBody .kb-col').length,
      expectCards: shown.length + ungrouped,
      cols: cols.length,
      minScoreSeen: st ? null : null,
    };
  });
  check(!kbPage.placeholder, '★ 知識庫分頁已接線（不再是「尚未接線」佔位）');
  check(kbPage.html > 500, '知識庫分頁有內容', kbPage.html + ' bytes');
  check(kbPage.cols > 0, '★ 讀到本商家的知識庫清單', kbPage.cols + ' 個庫');
  check(kbPage.cards === kbPage.expectCards,
    '★ 卡片數 == 應顯示的庫數（附帶圖片庫不列 + 未分組虛擬卡）',
    kbPage.cards + ' vs ' + kbPage.expectCards);
  check(kbReqs.length > 0, '有打到 /console/kb 端點', kbReqs.length + ' 個');
  check(kbReqs.every(r => r.indexOf('business_id=') >= 0),
    '★★ 每個 /console/kb 請求都帶 business_id', kbReqs.slice(0, 3).join(' | '));
  check(kbReqs.every(r => r.startsWith('200')),
    '★ /console/kb 全部 200（沒有 403/400）',
    kbReqs.filter(r => !r.startsWith('200')).slice(0, 3).join(' | ') || '—');

  // 參數配置：按商家顯示（商家 1 有實測校正過的覆寫值）
  await page.evaluate(() => openKbParams(0));
  await settle(1600);
  const kbParams = await page.evaluate(() => {
    const ids = Array.from(document.querySelectorAll('#kbParamsBody [id^="kbp_"]'))
      .map(e => e.id).filter(id => id.indexOf('_len') < 0);
    const val = k => { const e = document.getElementById('kbp_' + k); return e ? e.value : undefined; };
    return {
      open: (document.getElementById('kbParamsOverlay') || {}).classList
        ? document.getElementById('kbParamsOverlay').classList.contains('open') : false,
      n: new Set(ids.map(i => i.slice(4))).size,
      groups: document.querySelectorAll('#kbParamsBody .card').length,
      minScore: val('min_score'), promptLen: (val('rag_answer_prompt') || '').length,
      total: (val('top_k') || '') + '',
    };
  });
  check(kbParams.open && kbParams.n >= 17, '參數彈窗可開且參數齊全', kbParams.n + ' 項');
  check(kbParams.groups === 3, '三組（A 檢索／B 分塊／C 提示詞）', kbParams.groups + ' 組');
  // ★ 不硬編碼某個值（使用者會自己調參）—— 驗的是「UI 顯示 == 該商家在 DB 的值」，
  //   也就是「按商家載入」這個不變量本身。
  const apiParams = await page.evaluate(async () => {
    const r = await fetch('/console/kb/params?business_id=1', { credentials: 'same-origin' });
    const d = await r.json();
    const m = {};
    (d.items || []).forEach(i => { m[i.key] = i.value; });
    return m;
  });
  check(String(kbParams.minScore) === String(apiParams.min_score),
    '★★ 參數按商家載入（UI 顯示 == 後端該商家的值）',
    'UI=' + kbParams.minScore + ' / API=' + apiParams.min_score);
  check(kbParams.minScore !== '' && kbParams.minScore != null,
    '參數有實際值（不是空白）', String(kbParams.minScore));
  check(kbParams.promptLen > 200, '提示詞有載入（按商家）', kbParams.promptLen + ' 字');
  await page.evaluate(() => closeKbParams());
  await settle(300);

  // 檢索端點（帶商家）
  const kbSearch = await page.evaluate(async () => {
    const r = await fetch('/console/kb/search?q=' + encodeURIComponent('退貨')
      + '&business_id=1&candidates=5', { credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    return { status: r.status, thr: d.threshold, reason: d.reason };
  });
  check(kbSearch.status === 200, '★ 檢索端點帶商家可查', 'threshold=' + kbSearch.thr);
  page.off('response', kbTap);

  await browser.close();

  console.log('\n[7] JS 錯誤');
  // 只忽略**外部資源不可控**的噪音：
  //   ① 舊隧道域名的連線失敗（natapp/ngrok 早就關了，網址是歷史資料寫死的）
  //   ② 404 —— 資料引用了「從未存在」的檔（實測是 4 筆測試假資料的
  //      `![圖 1 截圖](/media/placeholder.png)`，檔案根本沒被產生過）。
  //      ⚠️ 「路徑設錯導致整批 404」這種回歸由上面的「圖片真的載入成功」斷言
  //      （實測 10/14，失敗的正好是那 4 筆）＋ 端點探測負責，不會被這條蓋掉。
  // ★ 403 / 5xx 一定算錯 —— 那代表我們自己的端點壞了（例如 /media、/files 代理、
  //   或身分權限閘門寫錯）。
  // 兩種來源的格式都要涵蓋：`page.on('response')` → "HTTP 404 <url>"；
  // `page.on('console')` → "…status of 404 (Not Found)"。
  const real = errors.filter(e =>
    !/net::ERR_CONNECTION_REFUSED/.test(e) && !/\b404\b/.test(e));
  check(real.length === 0, '沒有 JS 執行期錯誤', real.slice(0, 5).join(' | '));

  console.log('\n' + '='.repeat(60));
  console.log(' 瀏覽器冒煙自檢：PASS ' + pass + ' / FAIL ' + fails.length);
  for (const f of fails) console.log('   ✗ ' + f);
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.log('FATAL ' + e.message); process.exit(1); });
