/*! 共用元件：左下角用戶頭像 + 身分切換（一帳號多身分才顯示）
 *
 * 用法：
 *   TopUser.init({ me: < GET /api/auth/me 的 staff >, onError: function (msg) {} })
 *
 * 各頁需要的**靜態節點**（不進重繪區，避免被輪詢重繪弄丟）：
 *   #whoBox   頭像列（可點）
 *   #whoCaret 下拉指示（單身分時自動隱藏）
 *   #roleMenu 選單容器（.open 顯示）
 *   #roleList 選項清單（由本檔渲染）
 */
(function (global) {
  'use strict';

  var ROLE_LABEL = { admin: '管理員', agent: '客服坐席', ops: '運維' };

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  var me = null, csrf = '', menu = null, onErr = null;

  function closeMenu() { if (menu) menu.classList.remove('open'); }

  function render() {
    var box = document.getElementById('roleList');
    if (!box) return;
    box.innerHTML = (me.available_roles || []).map(function (r) {
      var cur = r === me.active_role;
      return '<button type="button" class="roleitem' + (cur ? ' cur' : '') + '"'
        + (cur ? ' aria-current="true"' : ' data-role="' + esc(r) + '"') + '>'
        + '<span class="ic">' + esc((ROLE_LABEL[r] || r).slice(0, 1)) + '</span>'
        + '<span>' + esc(ROLE_LABEL[r] || r) + '</span>'
        + (cur ? '<span class="now">目前</span>' : '')
        + '</button>';
    }).join('');
  }

  function switchTo(role) {
    fetch('/api/auth/switch-role', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: JSON.stringify({ role: role })
    }).then(function (r) {
      return r.json().catch(function () { return {}; })
        .then(function (d) { return { s: r.status, d: d || {} }; });
    }).then(function (res) {
      if (res.s === 200 && res.d.ok) {
        global.location.href = res.d.home || '/';
        return;
      }
      closeMenu();
      if (onErr) onErr(res.d.error || '切換身分失敗');
    }).catch(function () {
      closeMenu();
      if (onErr) onErr('網路異常，請稍後再試');
    });
  }

  global.TopUser = {
    init: function (opts) {
      opts = opts || {};
      me = opts.me || null;
      csrf = opts.csrf || '';
      onErr = opts.onError || null;

      var trigger = document.getElementById('whoBox');
      menu = document.getElementById('roleMenu');
      if (!trigger || !menu || !me || !me.can_switch) return;   // 單身分：不顯示任何切換入口

      trigger.classList.add('clickable');
      trigger.setAttribute('title', '切換身分');
      var caret = document.getElementById('whoCaret');
      if (caret) caret.hidden = false;

      render();

      trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        menu.classList.toggle('open');
      });
      document.addEventListener('click', function (e) {
        if (menu.classList.contains('open') && !menu.contains(e.target)) closeMenu();
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeMenu();
      });
      document.getElementById('roleList').addEventListener('click', function (e) {
        var b = e.target && e.target.closest ? e.target.closest('.roleitem[data-role]') : null;
        if (b) switchTo(b.getAttribute('data-role'));
      });
    }
  };
})(window);


/*! 共用元件：修改我的密碼（三個頁面共用一份實作）
 *
 * 用法：
 *   PwdDialog.mount({ csrf: state.csrf, onError: function (msg) {} })
 *   PwdDialog.open()          // 由「修改密碼」按鈕呼叫
 *
 * ★ 為什麼放這裡而不是各頁各寫一份：三頁（工作台／管理台／運維）都要有這功能，
 *   複製三份必然走偏（其中一頁漏了「兩次新密碼要一致」這種低級錯誤最難查）。
 * ★ 為什麼**不放進** `#roleMenu`（身分切換選單）：`TopUser.init()` 對單身分帳號
 *   直接 return，選單永遠不會展開 —— 客服坐席（通常只有 agent 一個身分）
 *   就等於「看不到也點不到」。這是本專案已經踩過的坑（登出按鈕踩過一次）。
 * ★ 彈窗節點由 `mount()` 一次性注入 body，之後只切 `class="open"`，
 *   **不隨任何 render 重建** —— 輸入到一半被輪詢重繪清空會讓人抓狂。
 */
(function (global) {
  'use strict';

  var csrf = '', onErr = null, built = false;
  var root, errBox, iOld, iNew1, iNew2, btnOk;

  /* csrf 支援「字串」或「取值函式」。
     ★ 為什麼要支援函式：admin/ops 的 `state.csrf` 是 `loadMe()` 回來才填的，
       而按鈕是靜態節點、可能在 csrf 到位前就綁好。傳函式 → 每次送出才取最新值，
       不會拿到空字串（空字串會被後端當 CSRF 失敗回 403，看起來像「壞了」）。 */
  function csrfNow() {
    return typeof csrf === 'function' ? (csrf() || '') : (csrf || '');
  }

  var CSS = ''
    + '.pwdov{position:fixed;inset:0;background:rgba(15,30,50,.38);'
    + 'display:none;align-items:center;justify-content:center;z-index:9999;padding:20px}'
    + '.pwdov.open{display:flex}'
    + '.pwdbox{background:var(--panel,#fff);border-radius:12px;width:380px;max-width:100%;'
    + 'box-shadow:0 12px 40px rgba(15,30,50,.22);overflow:hidden;'
    + 'font-family:inherit;color:var(--text,#1f2a37)}'
    + '.pwdhead{display:flex;align-items:center;gap:8px;padding:16px 18px;'
    + 'border-bottom:1px solid var(--border,#e5e9f0)}'
    + '.pwdhead h3{margin:0;font-size:15px;font-weight:700;flex:1}'
    + '.pwdx{border:0;background:transparent;font-size:18px;line-height:1;'
    + 'color:var(--text-soft,#6b7280);cursor:pointer;padding:2px 6px;border-radius:6px}'
    + '.pwdx:hover{background:var(--bg,#f4f6f8);color:var(--text,#1f2a37)}'
    + '.pwdbody{padding:16px 18px;display:flex;flex-direction:column;gap:6px}'
    + '.pwdbody label{font-size:12.5px;font-weight:600;color:var(--text-soft,#6b7280);'
    + 'margin-top:6px}'
    + '.pwdbody input{width:100%;box-sizing:border-box;padding:9px 11px;font-size:13.5px;'
    + 'border:1px solid var(--border,#e5e9f0);border-radius:8px;font-family:inherit;'
    + 'color:var(--text,#1f2a37);background:#fff;outline:none}'
    + '.pwdbody input:focus{border-color:var(--accent,#1a7f5c);'
    + 'box-shadow:0 0 0 3px rgba(26,127,92,.12)}'
    + '.pwderr{display:none;font-size:12.5px;color:var(--red,#e02020);'
    + 'background:var(--red-soft,#fdecec);border-radius:8px;padding:8px 10px;margin-top:8px}'
    + '.pwderr.on{display:block}'
    + '.pwdnote{font-size:11.5px;color:var(--text-soft,#6b7280);line-height:1.6;'
    + 'margin-top:10px;padding-top:10px;border-top:1px dashed var(--border,#e5e9f0)}'
    + '.pwdacts{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px 16px}'
    + '.pwdacts button{padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;'
    + 'cursor:pointer;font-family:inherit;border:1px solid var(--border,#e5e9f0);'
    + 'background:#fff;color:var(--text-soft,#6b7280)}'
    + '.pwdacts button:hover{border-color:#c9d2de;color:var(--text,#1f2a37)}'
    + '.pwdacts .pwdok{background:var(--accent,#1a7f5c);border-color:var(--accent,#1a7f5c);'
    + 'color:#fff}'
    + '.pwdacts .pwdok:hover{background:var(--accent-dark,#116a4b);'
    + 'border-color:var(--accent-dark,#116a4b);color:#fff}'
    + '.pwdacts .pwdok:disabled{opacity:.6;cursor:not-allowed}';

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    return n;
  }

  function showErr(msg) {
    if (!errBox) return;
    if (!msg) { errBox.className = 'pwderr'; errBox.textContent = ''; return; }
    errBox.className = 'pwderr on';
    errBox.textContent = msg;
  }

  function build() {
    if (built) return;
    var st = el('style');
    st.textContent = CSS;
    document.head.appendChild(st);

    root = el('div', 'pwdov');
    root.id = 'pwdOv';
    root.innerHTML =
      '<div class="pwdbox">'
      + '<div class="pwdhead"><h3>修改我的密碼</h3>'
      + '<button type="button" class="pwdx" id="pwdX" title="關閉">✕</button></div>'
      + '<div class="pwdbody">'
      + '<label for="pwdOld">舊密碼</label>'
      + '<input type="password" id="pwdOld" autocomplete="current-password" placeholder="目前的密碼">'
      + '<label for="pwdNew1">新密碼</label>'
      + '<input type="password" id="pwdNew1" autocomplete="new-password" placeholder="至少 8 位">'
      + '<label for="pwdNew2">再輸入一次新密碼</label>'
      + '<input type="password" id="pwdNew2" autocomplete="new-password" placeholder="確認新密碼">'
      + '<div class="pwderr" id="pwdErr"></div>'
      + '<div class="pwdnote">管理員看不到你的密碼（系統只存單向雜湊，無法還原），'
      + '若忘記密碼需請管理員重設。</div>'
      + '</div>'
      + '<div class="pwdacts">'
      + '<button type="button" id="pwdCancel">取消</button>'
      + '<button type="button" class="pwdok" id="pwdOk">確定修改</button>'
      + '</div></div>';
    document.body.appendChild(root);

    errBox = root.querySelector('#pwdErr');
    iOld = root.querySelector('#pwdOld');
    iNew1 = root.querySelector('#pwdNew1');
    iNew2 = root.querySelector('#pwdNew2');
    btnOk = root.querySelector('#pwdOk');

    root.querySelector('#pwdX').addEventListener('click', close);
    root.querySelector('#pwdCancel').addEventListener('click', close);
    btnOk.addEventListener('click', submit);
    // Enter 直接送出（三個欄位都支援）
    [iOld, iNew1, iNew2].forEach(function (inp) {
      inp.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
      });
    });
    // ESC 關閉。★ 只掛在 document 一次（build 只跑一次，不會疊加）
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.classList.contains('open')) { e.preventDefault(); close(); }
    });
    built = true;
  }

  function reset() {
    showErr('');
    if (iOld) iOld.value = '';
    if (iNew1) iNew1.value = '';
    if (iNew2) iNew2.value = '';
    if (btnOk) { btnOk.disabled = false; btnOk.textContent = '確定修改'; }
  }

  function open() {
    build();
    reset();
    root.classList.add('open');
    setTimeout(function () { if (iOld) iOld.focus(); }, 30);
  }

  function close() {
    if (!root) return;
    root.classList.remove('open');
    reset();                    // ★ 關閉即清空：密碼不留在 DOM 裡
  }

  function submit() {
    var oldp = iOld.value || '';
    var p1 = iNew1.value || '';
    var p2 = iNew2.value || '';
    if (!oldp) { showErr('請輸入舊密碼'); iOld.focus(); return; }
    if (p1.length < 8) { showErr('新密碼至少 8 位'); iNew1.focus(); return; }
    if (p1 !== p2) { showErr('兩次輸入的新密碼不一致'); iNew2.focus(); return; }
    if (p1 === oldp) { showErr('新密碼不能與舊密碼相同'); iNew1.focus(); return; }

    showErr('');
    btnOk.disabled = true;
    btnOk.textContent = '儲存中…';
    fetch('/api/auth/password', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfNow() },
      body: JSON.stringify({ old_password: oldp, new_password: p1 })
    }).then(function (r) {
      return r.json().catch(function () { return {}; })
        .then(function (d) { return { s: r.status, d: d || {} }; });
    }).then(function (res) {
      btnOk.disabled = false;
      btnOk.textContent = '確定修改';
      if (res.s === 200 && res.d.ok) {
        var k = res.d.killed_sessions || 0;
        close();
        if (onErr) {
          onErr(k > 0
            ? '密碼已修改（其他 ' + k + ' 個裝置已被登出）'
            : '密碼已修改');
        }
        return;
      }
      showErr(res.d.error || res.d.detail || '修改失敗，請稍後再試');
    }).catch(function () {
      btnOk.disabled = false;
      btnOk.textContent = '確定修改';
      showErr('網路異常，請稍後再試');
    });
  }

  global.PwdDialog = {
    mount: function (opts) {
      opts = opts || {};
      csrf = opts.csrf || '';
      onErr = opts.onError || null;
      build();
    },
    open: open,
    close: close
  };
})(window);

/*! 共用元件：「⋮」使用者選單（修改密碼 / 登出）
 *
 * 用法：
 *   UserMenu.mount({ csrf: function () { return state.csrf; },
 *                    onError: function (msg) { toast(msg); } })
 *
 * 需要的靜態節點（三頁都有，位置隨版面不同）：
 *   #btnMore   三點按鈕（放在用戶信息旁邊）
 *   #moreMenu  選單容器（空殼，由本檔填內容）
 *
 * ★★★ 為什麼要自己實作、**不能**放進 `#roleMenu`：
 *   `#roleMenu` 是「切換身分」選單，由 `TopUser.init()` 控制，而它開頭是
 *   `if (!me.can_switch) return;` —— 只有一個身分的帳號（客服坐席幾乎都是）
 *   **連點擊事件都不綁**，選單永遠打不開。之前把「登出」放進去就是這個下場：
 *   按鈕存在、但使用者完全摸不到（本專案實際踩過，見 logout_button_check.py）。
 *   → 所以這個選單**完全獨立**：不看角色、不看身分數量，任何人點三點都能開。
 *
 * ★ 展開方向自動判斷：貼近視窗底部時往上彈（側欄底部就是這種情況），
 *   否則往下。硬寫死一個方向，總有一頁會被切掉。
 */
(function (global) {
  'use strict';

  var csrf = '', onErr = null, built = false;
  var btn, menu, root;

  var CSS = ''
    + '.morebtn{display:flex;align-items:center;justify-content:center;width:26px;height:26px;'
    + 'flex-shrink:0;margin-left:auto;padding:0;border:0;border-radius:7px;background:transparent;'
    + 'color:var(--text-soft,#6b7280);cursor:pointer;font-family:inherit;'
    + 'transition:background .12s,color .12s}'
    + '.morebtn:hover{background:#eef1f5;color:var(--text,#1f2a37)}'
    + '.morebtn[aria-expanded="true"]{background:#eef1f5;color:var(--text,#1f2a37)}'
    + '.morebtn svg{display:block;pointer-events:none}'
    + '.moremenu{display:none;position:absolute;right:0;min-width:150px;background:var(--panel,#fff);'
    + 'border:1px solid var(--border,#e5e9f0);border-radius:10px;'
    + 'box-shadow:0 10px 28px rgba(15,30,50,.16);padding:5px;z-index:900}'
    + '.moremenu.open{display:block}'
    + '.moremenu.down{top:calc(100% + 6px)}'
    + '.moremenu.up{bottom:calc(100% + 6px)}'
    + '.moreitem{display:flex;align-items:center;gap:9px;width:100%;box-sizing:border-box;'
    + 'padding:8px 10px;border:0;border-radius:7px;background:transparent;text-align:left;'
    + 'font-size:13px;font-family:inherit;color:var(--text,#1f2a37);cursor:pointer;'
    + 'white-space:nowrap}'
    + '.moreitem:hover{background:#f2f5f8}'
    + '.moreitem.danger:hover{background:var(--red-soft,#fdecec);color:var(--red,#e02020)}'
    + '.moreitem .ic{display:flex;width:15px;height:15px;flex-shrink:0;'
    + 'align-items:center;justify-content:center;opacity:.75}'
    + '.moresep{height:1px;background:var(--border,#e5e9f0);margin:4px 6px}';

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function csrfNow() {
    return typeof csrf === 'function' ? (csrf() || '') : (csrf || '');
  }

  function close() {
    if (!menu) return;
    menu.classList.remove('open', 'up', 'down');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function place() {
    // 按鈕在視窗下半部 → 選單往上彈，否則往下。留 8px 邊界。
    var r = btn.getBoundingClientRect();
    var below = window.innerHeight - r.bottom;
    menu.classList.remove('up', 'down');
    menu.classList.add(below < 180 ? 'up' : 'down');
  }

  function openMenu() {
    if (!menu) return;
    place();
    menu.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
  }

  function toggle(e) {
    if (e) { e.stopPropagation(); e.preventDefault(); }
    if (menu.classList.contains('open')) close(); else openMenu();
  }

  function doLogout() {
    close();
    fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
      .then(function () { global.location.href = '/login'; })
      .catch(function () { global.location.href = '/login'; });
  }

  function doChangePwd() {
    close();
    if (global.PwdDialog) {
      // PwdDialog 的 csrf 也要用取值函式（見下方 mount 的說明）
      global.PwdDialog.mount({ csrf: csrf, onError: onErr });
      global.PwdDialog.open();
    } else if (onErr) {
      onErr('元件未載入，請重新整理頁面');
    }
  }

  function build(onChangePwd) {
    var css = document.getElementById('userMenuCss');
    if (!css) {
      css = document.createElement('style');
      css.id = 'userMenuCss';
      css.textContent = CSS;
      document.head.appendChild(css);
    }
    if (menu) {
      menu.innerHTML =
        '<button type="button" class="moreitem" id="morePwd">'
        + '<span class="ic"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" '
        + 'stroke="currentColor" stroke-width="1.4"><rect x="2.5" y="7" width="11" height="7" '
        + 'rx="1.8"/><path d="M5.2 7V5a2.8 2.8 0 0 1 5.6 0v2"/></svg></span>'
        + '<span>修改密碼</span></button>'
        + '<div class="moresep"></div>'
        + '<button type="button" class="moreitem danger" id="moreLogout">'
        + '<span class="ic"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" '
        + 'stroke="currentColor" stroke-width="1.4"><path d="M6 14H3.5A1.5 1.5 0 0 1 2 12.5'
        + 'v-9A1.5 1.5 0 0 1 3.5 2H6"/><path d="M10.5 11.5 14 8l-3.5-3.5"/>'
        + '<path d="M14 8H6"/></svg></span>'
        + '<span>登出</span></button>';
      menu.querySelector('#morePwd').addEventListener('click', doChangePwd);
      menu.querySelector('#moreLogout').addEventListener('click', doLogout);
    }
    built = true;
  }

  global.UserMenu = {
    mount: function (opts) {
      opts = opts || {};
      // ★★ csrf 一律傳「取值函式」：admin/ops 的 state.csrf 要等 /api/auth/me
      //   回來才填，而 mount 是同步執行的 —— 傳值會抓到空字串，
      //   改密碼時後端回 403「CSRF 校驗失敗」（本專案實際踩過）。
      csrf = opts.csrf;
      onErr = opts.onError || null;

      btn = document.getElementById('btnMore');
      menu = document.getElementById('moreMenu');
      if (!btn || !menu) return;          // 該頁沒有這組節點 → 靜靜跳過

      build(doChangePwd);

      btn.addEventListener('click', toggle);
      // 點選單內部不要冒泡到 document（否則一開就關）
      menu.addEventListener('click', function (e) { e.stopPropagation(); });
      if (!btn.__umBound) {
        btn.__umBound = true;
        document.addEventListener('click', function () {
          if (menu && menu.classList.contains('open')) close();
        });
        document.addEventListener('keydown', function (e) {
          if (e.key === 'Escape' && menu && menu.classList.contains('open')) {
            e.preventDefault(); close();
          }
        });
        window.addEventListener('resize', function () {
          if (menu && menu.classList.contains('open')) close();
        });
      }
    },
    close: close
  };
})(window);
