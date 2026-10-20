/* 審計日誌共用視圖（ops.html / admin.html 共用一份，避免兩套 UI 走樣）
 * ============================================================================
 * 需求（2026-10-20 小jo）：
 *   ① 時間 / 類型可篩選，有翻頁；② 列表內容人話化（不是原始 JSON）；
 *   ③ **一頁 20 條要在一屏內全部顯示**，不要滾輪；
 *   ④ **不足 20 條就從上往下排、排到哪裡算哪裡，下面留空**（不拉開行距、不置中）。
 *
 * 為什麼要共用：先前 ops.html 先改好、admin.html 仍是舊版（raw payload、
 * 無篩選無分頁）→ 同一個功能兩套長相。抽成一份後兩頁只差「打哪支端點」。
 *
 * 用法：
 *   AuditView.mount({
 *     api: api, esc: esc, host: function () { return document.getElementById('view'); },
 *     listUrl: '/console/ops/audit', facetsUrl: '/console/ops/audit/facets',
 *     plats: PLAT_LABEL, roles: ROLE_LABEL
 *   });
 *   AuditView.show();      // 建 DOM（每次切到審計分頁時呼叫，會重繪但保留篩選與展開）
 *   AuditView.reload();    // 只重拉資料
 *   AuditView.state.all    // 是否「顯示全部類型」（頁面自己畫那顆按鈕時讀寫）
 */
window.AuditView = (function () {
  'use strict';

  var o = null;                        // mount 注入的依賴

  /* ---------------- 狀態（模組自帶，不污染頁面的 state） ---------------- */
  var S = {
    q: '',                             // 搜尋字
    range: '7d',                       // today | yday | 7d | 30d | all | custom
    from: '', to: '',                  // custom 時的 yyyy-mm-dd
    groups: [],                        // 勾選的組 key（空 = 全部組）
    types: [],                         // 勾選的具體類型（優先於組）
    failed: false,                     // 只看失敗
    all: false,                        // 顯示全部類型（含已下線的歷史噪音）
    rows: [], total: 0, page: 1, size: 20,
    open: {},                          // id -> true（展開明細）
    facets: null, labels: {}, groupDefs: []
  };

  var ROW_H = 34;                      // 預設行高（也是上限：再高只是浪費空間）
  var ROW_MIN = 28;                    // 為了塞進 20 條可以壓到的最小行高
  var HEAD_H = 33;                     // 表頭高度
  var PAGE_MAX = 20;                   // ★ 一頁 20 條
  var _rowH = ROW_H;
  var _resizeTimer = null;

  var RANGES = [
    { k: 'today', label: '今天' }, { k: 'yday', label: '昨天' },
    { k: '7d', label: '近 7 天' }, { k: '30d', label: '近 30 天' },
    { k: 'all', label: '全部' }
  ];

  /* ---------------- 樣式（一次性注入，兩頁共用同一份） ---------------- */
  function injectStyle() {
    if (document.getElementById('auditViewStyle')) return;
    var css = ''
      /* ★ 三段式：篩選固定 / 表格填滿 / 分頁固定，整頁**不滾動** */
      + '.body.as-audit { overflow: hidden; padding: 14px 22px 12px; display: flex;'
      + '  flex-direction: column; gap: 10px; min-height: 0; }'
      + '.audit-bar { flex-shrink: 0; display: flex; flex-direction: column; gap: 8px; }'
      + '.audit-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }'
      + '.audit-line .lbl { font-size: 12.5px; font-weight: 700; color: var(--text-soft);'
      + '  min-width: 30px; flex-shrink: 0; }'
      + '.chip { padding: 5px 11px; border-radius: 999px; border: 1px solid var(--border);'
      + '  background: #fafbfc; color: var(--text-soft); cursor: pointer; font-size: 12.5px;'
      + '  font-weight: 600; font-family: inherit; transition: all .12s; white-space: nowrap;'
      + '  display: inline-flex; align-items: center; gap: 5px; }'
      + '.chip:hover { border-color: var(--accent); color: var(--accent); }'
      + '.chip.on { background: var(--accent-soft); border-color: var(--accent);'
      + '  color: var(--accent-dark); }'
      + '.chip .n { font-size: 11px; font-weight: 700; opacity: .62;'
      + '  font-variant-numeric: tabular-nums; }'
      + '.chip:disabled { opacity: .38; cursor: not-allowed; }'
      + '.chip:disabled:hover { border-color: var(--border); color: var(--text-soft); }'
      /* ★★ align-items:flex-start 是「不足 20 條、下面留空」的關鍵：
         預設 stretch 會把 table 撐成容器高度，瀏覽器再把多餘高度攤給各列
         → 行距被拉開，看起來像散落的幾行。 */
      + '.audit-tw { flex: 1; min-height: 0; overflow: hidden; display: flex;'
      + '  align-items: flex-start; }'
      + '.audit-tw > table { flex: 0 0 auto; width: 100%; margin: 0; }'
      + '.audit-tw > .empty { width: 100%; }'
      + '.audit-tw.scroll { overflow-y: auto; }'
      + '.audit-tw thead th { position: sticky; top: 0; z-index: 1; background: #fafbfc;'
      + '  padding: 8px 10px; }'
      + '.audit-tw td { padding: 0 10px; font-size: 12.5px; vertical-align: middle;'
      + '  overflow: hidden; }'
      + '.a-cell { display: block; max-width: 100%; overflow: hidden;'
      + '  text-overflow: ellipsis; white-space: nowrap; }'
      + '.a-row { cursor: pointer; height: var(--audit-row-h, 34px); }'
      + '.a-row td { border-bottom: 1px solid #f0f2f5; }'
      /* ★ 膠囊/標籤**必須 nowrap**：列變窄時「失敗」會被折成兩行
         → 那格 21px 變 34px → 整列被撐高 5px，行高就對不齊了
         （管理台欄寬分配不同，只有它會踩到）。 */
      + '.audit-tw .pill, .audit-tw .tag { white-space: nowrap; }'
      + '.a-row:hover td { background: #fafcff; }'
      + '.a-row.open { background: var(--accent-soft); }'
      + '.a-row.open td { border-bottom-color: transparent; }'
      + '.a-detail td { background: #fcfdfe; padding: 10px 12px 12px;'
      + '  border-bottom: 1px solid var(--border); }'
      + '.a-detail pre { margin: 0; padding: 9px 11px; background: #f7f9fb;'
      + '  border: 1px solid var(--border); border-radius: 8px; font-size: 12px;'
      + '  line-height: 1.6; white-space: pre-wrap; word-break: break-all;'
      + '  max-height: 220px; overflow-y: auto;'
      + '  font-family: ui-monospace, Consolas, monospace; }'
      + '.a-kv { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12px;'
      + '  color: var(--text-soft); margin-bottom: 8px; }'
      + '.a-kv b { color: var(--text); font-weight: 600; }'
      + '.audit-pg { flex-shrink: 0; display: flex; align-items: center; gap: 6px;'
      + '  padding-top: 2px; flex-wrap: wrap; }'
      + '.audit-pg .info { font-size: 12.5px; color: var(--text-soft); margin-right: auto; }'
      + '.pg-btn { min-width: 30px; padding: 5px 9px; border-radius: 7px;'
      + '  border: 1px solid var(--border); background: #fff; color: var(--text-soft);'
      + '  cursor: pointer; font-size: 12.5px; font-weight: 600; font-family: inherit;'
      + '  font-variant-numeric: tabular-nums; }'
      + '.pg-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }'
      + '.pg-btn.on { background: var(--accent); border-color: var(--accent); color: #fff; }'
      + '.pg-btn:disabled { opacity: .42; cursor: not-allowed; }'
      + '.pg-gap { color: var(--text-soft); padding: 0 2px; font-size: 12px; }';
    var st = document.createElement('style');
    st.id = 'auditViewStyle';
    st.textContent = css;
    document.head.appendChild(st);
  }

  /* ---------------- 工具 ---------------- */
  function $(id) { return document.getElementById(id); }

  /* 時間範圍 → [from, to) 的 ISO 字串。to 用「隔天 0 點」＝含今天整天。 */
  function rangeOf() {
    if (S.range === 'custom') {
      function iso(y, m, d) { return new Date(y, m, d).toISOString(); }
      var f = S.from ? iso(+S.from.slice(0, 4), +S.from.slice(5, 7) - 1, +S.from.slice(8, 10)) : '';
      var t = '';
      if (S.to) {
        // 含「結束日」整天 → 用隔天 0 點
        var d2 = new Date(+S.to.slice(0, 4), +S.to.slice(5, 7) - 1, +S.to.slice(8, 10));
        t = new Date(d2.getTime() + 864e5).toISOString();
      }
      return [f, t];
    }
    function midnight(d) { var x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
    var today = midnight(new Date());
    function iso(d) { return d.toISOString(); }
    if (S.range === 'today') return [iso(today), iso(new Date(today.getTime() + 864e5))];
    if (S.range === 'yday') {
      var y = new Date(today.getTime() - 864e5);
      return [iso(y), iso(today)];
    }
    if (S.range === '7d') return [iso(new Date(today.getTime() - 6 * 864e5)),
                                  iso(new Date(today.getTime() + 864e5))];
    if (S.range === '30d') return [iso(new Date(today.getTime() - 29 * 864e5)),
                                   iso(new Date(today.getTime() + 864e5))];
    return ['', ''];                   // all
  }

  /* 目前要查的 types：勾了具體類型就優先，否則用勾選的組，都沒有 = 全部組 */
  function wantTypes() {
    if (S.types.length) return S.types.slice();
    var gs = S.groups.length ? S.groups
      : (S.groupDefs || []).map(function (g) { return g.key; });
    var out = [];
    (S.groupDefs || []).forEach(function (g) {
      if (gs.indexOf(g.key) >= 0) out = out.concat(g.types);
    });
    return out;
  }

  function qs(page, size, facetsOnly) {
    var rg = rangeOf();
    var parts = [];
    function add(k, v) {
      if (v !== '' && v !== null && v !== undefined) parts.push(k + '=' + encodeURIComponent(v));
    }
    if (!facetsOnly) {
      add('limit', size); add('offset', (page - 1) * size);
      add('types', wantTypes().join(','));
      add('q', S.q);
      if (S.failed) add('only_failed', 'true');
    }
    add('time_from', rg[0]); add('time_to', rg[1]);
    if (S.all) add('all', 'true');
    return '?' + parts.join('&');
  }

  /* ★ 一頁幾條：**基準 20 條**，用可視高度反推行高（28~34px）讓 20 條一定塞得下。
     —— 螢幕夠高 → 行高 34px、20 條，下面留空；
        螢幕普通 → 行高降到 30/28px，還是 20 條（先保住 20 條，不要少給）；
        螢幕矮到 28×20 都放不下 → 才減條數（寧可少，也不要讓使用者滾輪）。 */
  function fit() {
    var tw = document.querySelector('.audit-tw');
    var avail = tw ? tw.clientHeight - HEAD_H - 4 : 0;
    if (avail <= 0) { applyRowH(ROW_H); return PAGE_MAX; }   // 還沒排版完
    var rh = Math.min(ROW_H, Math.floor(avail / PAGE_MAX));
    var size;
    if (rh >= ROW_MIN) size = PAGE_MAX;
    else { rh = ROW_MIN; size = Math.min(PAGE_MAX, Math.max(1, Math.floor(avail / ROW_MIN))); }
    applyRowH(rh);
    return size;
  }
  function applyRowH(h) {
    _rowH = h;
    document.documentElement.style.setProperty('--audit-row-h', h + 'px');
  }

  function typeLabel(t) { return S.labels[t] || t; }

  /* payload → 一句人話（列表主視線用） */
  function actionText(a) {
    var p = a.payload || {};
    var t = a.operate_type;
    function j(v) { return (v === null || v === undefined) ? '' : String(v); }
    switch (t) {
      case 'ai_switch_change': {
        var sc = p.scope === 'comment' ? '留言' : (p.scope === 'dm' ? '私訊' : '');
        var layer = p.layer === 'scope' ? '場景層'
          : (p.layer === 'account' ? '賬號層' : (p.layer || ''));
        return (p.platform ? j(p.platform) + ' ' : '') + (sc ? sc + ' ' : '')
          + layer + ' AI：' + (p.enabled ? '開啟' : '關閉');
      }
      case 'reply_send':
        return '送出回覆' + (p.task_id ? ' #' + p.task_id : '')
          + (p.text ? '：' + String(p.text).slice(0, 60) : '');
      case 'ops_config_change':
        return j(p.key) + '：' + j(p.from) + ' → ' + j(p.to);
      case 'task_retry':
        return '重跑任務 ' + ((p.retried || []).length
          ? (p.retried || []).map(function (x) { return '#' + x; }).join('、')
          : '（無）') + ((p.skipped || []).length ? '，跳過 ' + p.skipped.length + ' 筆' : '');
      case 'login_failed':
        return '登入失敗' + (p.account ? '（' + p.account + '）' : '') + '：' + j(p.reason);
      case 'login':
        return '登入' + (p.account ? '（' + p.account + '）' : '') + '，身分 ' + j(p.role);
      case 'logout': return '登出';
      case 'sync_history': {
        var tt = p.totals || {};
        var bits = [];
        if (tt.dm_new) bits.push('私訊 +' + tt.dm_new);
        if (tt.comments_new) bits.push('留言 +' + tt.comments_new);
        if (tt.posts) bits.push('貼文 ' + tt.posts);
        if (tt.errors && tt.errors.length) bits.push('錯誤 ' + tt.errors.length);
        return '補拉歷史' + (bits.length ? '：' + bits.join('、') : '（無新增）');
      }
      case 'message_handle': return '處理訊息' + (p.updated ? ' ' + p.updated + ' 則' : '');
      case 'comment_handle':
        return '標記留言已處理' + (p.updated ? '（' + p.updated + ' 則）' : '');
      case 'post_config_change':
        return (p.removed ? '移除貼文配置' : '更新貼文配置')
          + (p.post_id ? '（' + String(p.post_id).slice(-8) + '）' : '');
      case 'post_ai_config_change': return '更新貼文 AI 配置';
      case 'business_update':
        return '更新商家' + (p.business_name ? '「' + p.business_name + '」' : '')
          + (p.meta_app_changed ? '（含 Meta App 變更）' : '')
          + (p.enabled === false ? '（停用）' : '');
      case 'business_create':
        return '新增商家' + (p.business_name ? '「' + p.business_name + '」' : '');
      case 'platform_account_bind':
        return '綁定平台帳號' + (p.platform_business_account_id
          ? ' ' + p.platform_business_account_id : '');
      case 'platform_account_unbind':
        return '解綁平台帳號' + (p.enabled === false ? '（停用）' : '');
      case 'alert_ack':
        return '確認告警' + (p.all ? '（全部）' : (p.id ? ' #' + p.id : ''));
      case 'alert_notify':
        return '發送告警' + (p.test ? '（測試）' : '')
          + (p.to && p.to.length ? ' 至 ' + p.to.join('、') : '')
          + (p.sent === false ? '（失敗）' : '');
      case 'staff_role_change':
        return '變更員工角色：' + ((p.before || []).join('/') || '—')
          + ' → ' + ((p.after || []).join('/') || '—');
      case 'staff_business_permission_grant':
        return '授予商家權限 ' + ((p.business_ids || []).join('、') || '');
      case 'staff_create':
        return '新增員工' + (p.login_account ? '（' + p.login_account + '）' : '')
          + ((p.roles || []).length ? ' 角色 ' + p.roles.join('/') : '');
      case 'staff_update': return '更新員工' + (p.enabled === false ? '（停用）' : '');
      case 'staff_disable': return '停用員工' + (p.staff_id ? ' #' + p.staff_id : '');
      case 'staff_password_reset':
        return '重設員工密碼' + (p.staff_id ? '（#' + p.staff_id + '）' : '');
      case 'force_logout':
        return '強制登出' + (p.killed ? ' ' + p.killed + ' 個 session' : '');
      case 'binding_change':
        return '綁定變更（' + j(p.kind) + '）：' + (p.enabled ? '啟用' : '停用')
          + (p.reason ? '，' + String(p.reason).slice(0, 40) : '');
      case 'kb_collection_toggle':
        return '知識庫開關：' + (p.enabled ? '啟用' : '停用')
          + (p.reason ? '，' + String(p.reason).slice(0, 40) : '');
      case 'faq_import':
        return '匯入 FAQ' + (p.written ? ' ' + p.written + ' 組' : '')
          + (p.mode ? '（' + (p.mode === 'append' ? '附加' : '覆寫') + '）' : '');
      default:
        // 沒特別寫的類型：把 payload 壓成 key=value 一行，仍比原始 JSON 好讀
        var ks = Object.keys(p).slice(0, 4);
        return typeLabel(t) + (ks.length
          ? '：' + ks.map(function (k) { return k + '=' + j(p[k]).slice(0, 24); }).join('，')
          : '');
    }
  }

  /* 對象：優先顯示人能認的東西（商家 / 平台 / 帳號 / 客戶尾碼） */
  function targetText(a) {
    var bits = [];
    if (a.business_id !== null && a.business_id !== undefined) bits.push('商家 ' + a.business_id);
    if (a.platform) bits.push(o.plats[a.platform] || a.platform);
    if (a.business_platform_account_id) bits.push('帳號 ' + a.business_platform_account_id);
    if (a.end_user_sender_id) {
      var s = String(a.end_user_sender_id);
      bits.push('客戶 ' + (s.length > 12 ? s.slice(-8) : s));
    }
    return bits.length ? bits.join(' · ') : '—';
  }

  function isFailed(a) {
    if (a.fail_reason) return true;
    if (a.operate_type === 'login_failed') return true;
    var p = a.payload || {};
    if (p.sent === false) return true;
    if (p.errors && p.errors.length) return true;
    return false;
  }

  function roleName(r) { return (o.roles && o.roles[r]) || r; }

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  /* 列表的時間：今天只顯示時分秒，其他顯示月-日 時分（列要窄） */
  function shortTime(s) {
    if (!s) return '—';
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(s);
    var now = new Date();
    var sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
      && d.getDate() === now.getDate();
    if (sameDay) return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' '
      + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function fullTime(s) {
    if (!s) return '—';
    var d = new Date(s);
    if (isNaN(d.getTime())) return String(s);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  /* ---------------- 骨架 ---------------- */
  function show() {
    var v = o.host();
    if (!v) return;
    v.className = 'body as-audit';            // ★ 三段式：整頁不滾動
    v.innerHTML =
      '<div class="audit-bar" id="auditBar"></div>'
      + '<div class="audit-tw" id="auditTw"><div class="empty">載入中…</div></div>'
      + '<div class="audit-pg" id="auditPg"></div>';
    paintBar();
    paintTable();
    paintPager();
  }

  /* ---------------- 篩選列 ---------------- */
  function paintBar() {
    var bar = $('auditBar');
    if (!bar) return;
    var fc = S.facets;
    var h = '';

    // 第 1 行：時間
    h += '<div class="audit-line"><span class="lbl">時間</span>';
    RANGES.forEach(function (r) {
      h += '<button class="chip' + (S.range === r.k ? ' on' : '') + '"'
        + ' data-act="range" data-k="' + r.k + '">' + o.esc(r.label) + '</button>';
    });
    h += '<button class="chip' + (S.range === 'custom' ? ' on' : '') + '"'
      + ' data-act="range" data-k="custom">自訂</button>';
    if (S.range === 'custom') {
      h += '<input type="date" id="auditFrom" value="' + o.esc(S.from) + '" style="width:150px">'
        + '<span class="muted" style="font-size:12px">至</span>'
        + '<input type="date" id="auditTo" value="' + o.esc(S.to) + '" style="width:150px">';
    }
    h += '</div>';

    // 第 2 行：類型組（含筆數）
    h += '<div class="audit-line"><span class="lbl">類型</span>';
    h += '<button class="chip' + (!S.groups.length && !S.types.length ? ' on' : '')
      + '" data-act="grp" data-k="">全部</button>';
    (S.groupDefs || []).forEach(function (g) {
      var on = S.groups.indexOf(g.key) >= 0;
      var n = g.count !== undefined ? g.count : (fc && fc.groups
        ? ((fc.groups.filter(function (x) { return x.key === g.key; })[0] || {}).count || 0)
        : null);
      h += '<button class="chip' + (on ? ' on' : '') + '" data-act="grp"'
        + ' data-k="' + o.esc(g.key) + '" title="' + o.esc(g.hint || '') + '"'
        + (n === 0 ? ' disabled' : '') + '>'
        + o.esc(g.label)
        + (n !== null && n !== undefined ? '<span class="n">' + n + '</span>' : '')
        + '</button>';
    });
    h += '</div>';

    // 第 3 行：展開選中組的具體類型
    var sel = (S.groupDefs || []).filter(function (g) { return S.groups.indexOf(g.key) >= 0; });
    if (sel.length) {
      h += '<div class="audit-line"><span class="lbl">細項</span>';
      sel.forEach(function (g) {
        g.types.forEach(function (t) {
          var n = fc && fc.counts ? (fc.counts[t] || 0) : null;
          var on = S.types.indexOf(t) >= 0;
          h += '<button class="chip' + (on ? ' on' : '') + '" data-act="type"'
            + ' data-k="' + o.esc(t) + '"' + (n === 0 ? ' disabled' : '') + '>'
            + o.esc(typeLabel(t))
            + (n !== null ? '<span class="n">' + n + '</span>' : '') + '</button>';
        });
      });
      h += '</div>';
    }

    h += '<div class="audit-line">'
      + '<input type="text" id="auditQ" placeholder="搜尋 操作人 / 身分 / 商家 / 內容…"'
      + ' value="' + o.esc(S.q) + '" style="width:280px">'
      + '<button class="chip' + (S.failed ? ' on' : '') + '" data-act="failed">只看失敗</button>'
      + (S.q || S.groups.length || S.types.length || S.failed || S.range !== '7d'
        ? '<button class="chip" data-act="reset">重置</button>' : '')
      + '<span class="muted" style="font-size:12px;margin-left:auto">'
      + '共 <b>' + S.total + '</b> 筆，一頁 ' + S.size + ' 筆'
      + (S.size < 20 ? '（螢幕較矮）' : '')
      + '</span></div>';

    bar.innerHTML = h;

    // 事件（限定在 bar 內，絕不用全域選取）
    Array.prototype.forEach.call(bar.querySelectorAll('[data-act]'), function (b) {
      b.onclick = function () { onAct(this); };
    });
    var q = bar.querySelector('#auditQ');
    if (q) {
      var timer = null;
      q.oninput = function () {
        var val = this.value;
        clearTimeout(timer);
        timer = setTimeout(function () { S.q = val; S.page = 1; reload(); }, 300);
      };
    }
    ['auditFrom', 'auditTo'].forEach(function (id) {
      var el = bar.querySelector('#' + id);
      if (el) el.onchange = function () {
        if (id === 'auditFrom') S.from = this.value; else S.to = this.value;
        S.page = 1; reload();
      };
    });
  }

  function onAct(el) {
    var act = el.getAttribute('data-act');
    var k = el.getAttribute('data-k') || '';
    if (act === 'range') {
      S.range = k; S.page = 1;
      if (k !== 'custom') { S.from = ''; S.to = ''; }
      reload(); return;
    }
    if (act === 'grp') {
      if (!k) { S.groups = []; S.types = []; }
      else {
        var i = S.groups.indexOf(k);
        if (i >= 0) S.groups.splice(i, 1); else S.groups.push(k);
        S.types = [];                  // 換組 → 細項選擇作廢
      }
      S.page = 1; reload(); return;
    }
    if (act === 'type') {
      var j = S.types.indexOf(k);
      if (j >= 0) S.types.splice(j, 1); else S.types.push(k);
      S.page = 1; reload(); return;
    }
    if (act === 'failed') { S.failed = !S.failed; S.page = 1; reload(); return; }
    if (act === 'reset') {
      S.q = ''; S.range = '7d'; S.from = ''; S.to = '';
      S.groups = []; S.types = []; S.failed = false;
      S.page = 1; S.open = {}; reload(); return;
    }
  }

  /* ---------------- 表格 ---------------- */
  function paintTable() {
    var tw = $('auditTw');
    if (!tw) return;
    if (!S.rows.length) {
      tw.className = 'audit-tw';
      tw.innerHTML = '<div class="empty">沒有符合條件的紀錄。</div>';
      return;
    }
    var rows = '';
    S.rows.forEach(function (a) {
      var failed = isFailed(a);
      var open = !!S.open[a.id];
      rows += '<tr class="a-row' + (open ? ' open' : '') + '" data-id="' + a.id + '">'
        + '<td class="num" style="width:96px"><span class="a-cell">'
        + o.esc(shortTime(a.operate_at)) + '</span></td>'
        + '<td style="width:132px"><span class="a-cell">' + o.esc(a.staff_name || '—')
        + (a.staff_role ? ' <span class="pill">' + o.esc(roleName(a.staff_role)) + '</span>' : '')
        + '</span></td>'
        + '<td style="width:110px"><span class="a-cell"><span class="tag">'
        + o.esc(typeLabel(a.operate_type)) + '</span></span></td>'
        + '<td><span class="a-cell">' + o.esc(actionText(a)) + '</span></td>'
        + '<td style="width:230px"><span class="a-cell muted">'
        + o.esc(targetText(a)) + '</span></td>'
        + '<td style="width:64px">'
        + (failed ? '<span class="pill off">失敗</span>' : '<span class="muted">—</span>')
        + '</td></tr>';
      if (open) {
        var meta = [];
        meta.push('時間 <b>' + o.esc(fullTime(a.operate_at)) + '</b>');
        meta.push('操作人 <b>' + o.esc(a.staff_name || '—') + '</b>');
        if (a.staff_role) meta.push('生效身分 <b>' + o.esc(roleName(a.staff_role)) + '</b>');
        if ((a.staff_roles || []).length) {
          meta.push('可用身分 <b>' + o.esc(a.staff_roles.map(roleName).join('/')) + '</b>');
        }
        meta.push('類型 <b>' + o.esc(a.operate_type) + '</b>');
        if (a.business_id !== null && a.business_id !== undefined) {
          meta.push('商家 <b>' + a.business_id + '</b>');
        }
        if (a.platform) meta.push('平台 <b>' + o.esc(o.plats[a.platform] || a.platform) + '</b>');
        if (a.business_platform_account_id) {
          meta.push('帳號 <b>' + a.business_platform_account_id + '</b>');
        }
        if (a.end_user_sender_id) {
          meta.push('客戶 <b class="mono">' + o.esc(a.end_user_sender_id) + '</b>');
        }
        rows += '<tr class="a-detail"><td colspan="6">'
          + '<div class="a-kv">' + meta.join('') + '</div>'
          + (a.fail_reason ? '<div style="margin-bottom:8px"><span class="pill off">失敗原因</span> '
            + o.esc(a.fail_reason) + '</div>' : '')
          + (a.payload ? '<pre>' + o.esc(JSON.stringify(a.payload, null, 2)) + '</pre>' : '')
          + '</td></tr>';
      }
    });

    // ★ 有展開明細 → 允許表格區內部滾動（否則一屏放不下）
    var anyOpen = Object.keys(S.open).length > 0;
    tw.className = 'audit-tw' + (anyOpen ? ' scroll' : '');
    tw.innerHTML = '<table><thead><tr>'
      + '<th>時間</th><th>操作人</th><th>動作</th><th>內容</th><th>對象</th><th>結果</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>';

    Array.prototype.forEach.call(tw.querySelectorAll('.a-row'), function (tr) {
      tr.onclick = function () {
        var id = Number(this.getAttribute('data-id'));
        if (S.open[id]) delete S.open[id]; else S.open[id] = true;
        paintTable();
      };
    });
  }

  /* ---------------- 分頁 ---------------- */
  function paintPager() {
    var pg = $('auditPg');
    if (!pg) return;
    var size = S.size || 20;
    var pages = Math.max(1, Math.ceil(S.total / size));
    var cur = Math.min(Math.max(1, S.page), pages);
    var h = '<span class="info">第 <b>' + cur + '</b> / ' + pages + ' 頁'
      + '（共 ' + S.total + ' 筆）</span>';

    if (cur > 1) h += '<button class="pg-btn" data-p="' + (cur - 1) + '">‹ 上一頁</button>';
    // 頁碼：頭尾 + 當前附近，其餘用 …
    var nums = [];
    for (var i = 1; i <= pages; i++) {
      if (i === 1 || i === pages || Math.abs(i - cur) <= 2) nums.push(i);
    }
    var last = 0;
    nums.forEach(function (n) {
      if (last && n - last > 1) h += '<span class="pg-gap">…</span>';
      h += '<button class="pg-btn' + (n === cur ? ' on' : '') + '" data-p="' + n + '">'
        + n + '</button>';
      last = n;
    });
    if (cur < pages) h += '<button class="pg-btn" data-p="' + (cur + 1) + '">下一頁 ›</button>';

    pg.innerHTML = h;
    Array.prototype.forEach.call(pg.querySelectorAll('[data-p]'), function (b) {
      b.onclick = function () {
        S.page = Number(this.getAttribute('data-p')) || 1;
        S.open = {};
        reload();
      };
    });
  }

  /* ---------------- 載入 ---------------- */
  function reload() {
    if (!o) return Promise.resolve();
    var size = fit();          // ★ 先依螢幕算「一頁幾條」再查，回來剛好填滿一屏
    S.size = size;
    return o.api(o.listUrl + qs(S.page, size)).then(function (res) {
      if (!res || res.status !== 200 || !res.data || !res.data.ok) return;
      var d = res.data;
      S.rows = d.items || [];
      S.total = d.total || 0;
      if (d.limit) S.size = d.limit;
      if (d.groups) S.groupDefs = d.groups;
      if (d.type_labels) S.labels = d.type_labels;
      // 分頁越界（例如篩選後變少）→ 回到最後一頁重查
      var pages = Math.max(1, Math.ceil(S.total / S.size));
      if (S.page > pages) { S.page = pages; return reload(); }
      paintBar(); paintTable(); paintPager();
      loadFacets();
    });
  }

  /* 各組筆數（chip 上的數字）—— 只依時間範圍變，跟類型選擇無關 */
  function loadFacets() {
    if (!o) return Promise.resolve();
    return o.api(o.facetsUrl + qs(1, 20, true)).then(function (res) {
      if (!res || res.status !== 200 || !res.data || !res.data.ok) return;
      S.facets = res.data;
      if (res.data.groups) S.groupDefs = res.data.groups;
      var bar = $('auditBar');
      if (bar) paintBar();
    });
  }

  /* ★ 視窗大小變了 → 可視高度變了 → 「一頁幾條」也要跟著變。
     只在審計分頁可見時作用；debounce 300ms 避免拖曳時連發請求。 */
  function onResize() {
    if (!o || !document.querySelector('.audit-tw')) return;
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(function () {
      var size = fit();
      if (size === S.size) return;
      S.size = size; S.page = 1; S.open = {};
      reload();
    }, 300);
  }

  function mount(opts) {
    o = opts;
    injectStyle();
    // ★ 冪等：頁面每次進審計分頁都會 mount 一次，resize 只能掛一次
    if (!API._mounted) {
      window.addEventListener('resize', onResize);
      API._mounted = true;
    }
    return API;
  }

  var API = {
    mount: mount,
    show: show,
    reload: reload,
    onResize: onResize,
    state: S,
    /* 頁面若提供「顯示全部類型」按鈕，切換後呼叫這個 */
    setAll: function (v) { S.all = !!v; S.page = 1; return reload(); },
    /* 頁面切走時把 #view 的 class 復位（不然別頁內容被 overflow:hidden 裁掉） */
    unmountClass: function () {
      var v = o && o.host();
      if (v) v.className = 'body';
    },
    _internal: { fit: fit, actionText: actionText, typeLabel: typeLabel,
                 targetText: targetText, isFailed: isFailed, qs: qs, rangeOf: rangeOf }
  };
  return API;
})();
