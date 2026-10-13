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
