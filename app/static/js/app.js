/* ProxySQL Web UI - single page application (no external dependencies). */
(function () {
  'use strict';

  // ------------------------------------------------------------------ state & helpers
  const S = {
    me: null, instances: [], inst: null, serviceEnabled: false, serviceUnit: 'proxysql',
    pageTimers: [], pending: {}, hist: {}, route: null,
  };
  const ROLES = ['viewer', 'operator', 'admin'];
  const can = (role) => S.me && ROLES.indexOf(S.me.role) >= ROLES.indexOf(role);
  const $ = (sel, root) => (root || document).querySelector(sel);

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'style') el.style.cssText = v; // CSSOM: allowed by the strict CSP
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'value') el.value = v;
        else if (k === 'checked') el.checked = !!v;
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach((k) => append(el, k)); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  const store = {
    get(k, d) { try { const v = localStorage.getItem('psqlui.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('psqlui.' + k, JSON.stringify(v)); } catch (e) { /* ignore */ } },
  };

  class ApiError extends Error {}

  async function api(method, url, body) {
    const opts = { method, credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (method !== 'GET' && S.me) opts.headers['X-CSRF-Token'] = S.me.csrf;
    let res;
    try { res = await fetch(url, opts); } catch (e) { throw new ApiError('Network error: ' + e.message); }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && url !== '/api/login') { showLogin(); throw new ApiError('Session expired'); }
    if (!res.ok) throw new ApiError((data && data.error) || ('HTTP ' + res.status));
    return data;
  }
  const IU = (p) => '/api/i/' + encodeURIComponent(S.inst) + p;

  function toast(msg, kind) {
    const t = h('div', { class: 'toast ' + (kind || '') }, msg);
    $('#toasts').appendChild(t);
    setTimeout(() => t.remove(), kind === 'err' ? 9000 : 4500);
  }
  const fail = (e) => toast(e.message || String(e), 'err');

  function every(ms, fn) { const id = setInterval(fn, ms); S.pageTimers.push(id); return id; }

  // ------------------------------------------------------------------ formatting
  const isNum = (v) => v !== null && v !== '' && !isNaN(v) && isFinite(v);
  function fmtNum(v) {
    if (!isNum(v)) return v === null || v === undefined ? '' : String(v);
    const n = Number(v);
    return Math.abs(n) >= 1000 ? n.toLocaleString() : String(n);
  }
  function fmtCompact(n) {
    n = Number(n) || 0;
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (a >= 1e4) return (n / 1e3).toFixed(1) + 'k';
    return Number.isInteger(n) ? String(n) : n.toFixed(1);
  }
  function fmtBytes(v) {
    let n = Number(v) || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + ' ' + u[i];
  }
  function fmtDuration(sec) {
    sec = Math.floor(Number(sec) || 0);
    const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    if (d) return d + 'd ' + hh + 'h ' + m + 'm';
    if (hh) return hh + 'h ' + m + 'm';
    return m + 'm ' + (sec % 60) + 's';
  }
  function fmtUs(v) {
    if (!isNum(v)) return v;
    const n = Number(v);
    if (n >= 1e6) return (n / 1e6).toFixed(2) + ' s';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + ' ms';
    return n + ' µs';
  }
  function fmtEpoch(v) {
    if (!isNum(v) || Number(v) === 0) return v;
    return new Date(Number(v) * 1000).toLocaleString();
  }
  const fmtEpochUs = (v) => (isNum(v) && Number(v) > 0 ? new Date(Number(v) / 1000).toLocaleString() : v);

  const STATUS_KIND = { ONLINE: ['good', '●'], SHUNNED: ['bad', '✕'], OFFLINE_SOFT: ['warn', '◐'], OFFLINE_HARD: ['bad', '■'] };
  function statusPill(s) {
    const k = STATUS_KIND[s] || ['', '?'];
    return h('span', { class: 'pill ' + k[0] }, h('span', { 'aria-hidden': 'true' }, k[1]), s);
  }
  const yesNo = (v) => (v === null || v === undefined ? null : (String(v) === '1' ? 'yes' : 'no'));

  // ------------------------------------------------------------------ data table
  function DataTable(opts) {
    let rows = opts.rows || [];
    let filter = '';
    let sortKey = opts.sortKey || null, sortDesc = !!opts.sortDesc;
    const wrap = h('div', { class: 'table-wrap' });
    const foot = h('div', { class: 'table-foot' });
    const el = h('div', null, wrap, foot);
    const limit = opts.limit || 3000;

    function cellValue(row, c) { return c.get ? c.get(row) : row[c.key]; }
    function render() {
      clear(wrap);
      let view = rows;
      if (filter) {
        const f = filter.toLowerCase();
        view = rows.filter((r) => opts.columns.some((c) => {
          const v = cellValue(r, c); return v !== null && v !== undefined && String(v).toLowerCase().includes(f);
        }));
      }
      if (sortKey) {
        const c = opts.columns.find((x) => x.key === sortKey);
        if (c) {
          view = view.slice().sort((a, b) => {
            let x = cellValue(a, c), y = cellValue(b, c);
            if (isNum(x) && isNum(y)) { x = Number(x); y = Number(y); } else { x = x === null || x === undefined ? '' : String(x); y = y === null || y === undefined ? '' : String(y); }
            const r = x < y ? -1 : x > y ? 1 : 0;
            return sortDesc ? -r : r;
          });
        }
      }
      if (!view.length) {
        wrap.appendChild(h('div', { class: 'empty' }, filter ? 'No rows match the filter.' : (opts.empty || 'No rows.')));
        foot.textContent = '';
        return;
      }
      const thead = h('tr', null, opts.columns.map((c) => {
        const th = h('th', { class: (sortKey === c.key ? 'sorted' + (sortDesc ? ' desc' : '') : ''), title: c.title || null }, c.label || c.key);
        th.addEventListener('click', () => {
          if (sortKey === c.key) sortDesc = !sortDesc; else { sortKey = c.key; sortDesc = !!c.num; }
          render();
        });
        return th;
      }), opts.actions ? h('th', null, '') : null);
      const tbody = h('tbody');
      view.slice(0, limit).forEach((r) => {
        const tr = h('tr', { class: opts.rowClass ? opts.rowClass(r) : null });
        opts.columns.forEach((c) => {
          const raw = cellValue(r, c);
          let cls = c.num ? 'num' : (c.wrap ? 'wrap' : '');
          let content;
          if (c.fmt) content = c.fmt(raw, r);
          else content = c.num ? fmtNum(raw) : raw;
          if (raw === null && !c.fmt) { content = 'NULL'; cls += ' null'; }
          tr.appendChild(h('td', { class: cls }, content));
        });
        if (opts.actions) tr.appendChild(h('td', { class: 'actions' }, opts.actions(r)));
        if (opts.onRowClick) { tr.style.cursor = 'pointer'; tr.addEventListener('click', (ev) => { if (!ev.target.closest('button')) opts.onRowClick(r); }); }
        tbody.appendChild(tr);
      });
      wrap.appendChild(h('table', { class: 'data' }, h('thead', null, thead), tbody));
      foot.textContent = view.length + ' row' + (view.length === 1 ? '' : 's') +
        (view.length !== rows.length ? ' (filtered from ' + rows.length + ')' : '') +
        (view.length > limit ? ' — showing first ' + limit : '');
    }
    render();
    return {
      el,
      setRows(r) { rows = r || []; render(); },
      setFilter(f) { filter = f; render(); },
      searchInput() {
        const i = h('input', { class: 'search', type: 'search', placeholder: 'Filter…', 'aria-label': 'Filter rows' });
        i.addEventListener('input', () => { filter = i.value; render(); });
        return i;
      },
    };
  }

  // columns for a raw result set, with automatic formatting by column name
  function autoColumns(cols) {
    return cols.map((k) => {
      const c = { key: k, label: k };
      const lk = k.toLowerCase();
      if (lk === 'status') c.fmt = (v) => (STATUS_KIND[v] ? statusPill(v) : v);
      else if (lk === 'time_start_us') c.fmt = fmtEpochUs;
      else if (/_us$/.test(lk) || lk === 'latency_us' || /^(sum|min|max)_time$/.test(lk)) { c.num = true; c.fmt = fmtUs; }
      else if (lk === 'first_seen' || lk === 'last_seen' || lk === 'changed_at' || lk === 'updated_at') c.fmt = fmtEpoch;
      else if (/^bytes_|_bytes$|bytes_data/.test(lk)) { c.num = true; c.fmt = fmtBytes; }
      else if (lk === 'digest_text' || lk === 'info' || lk === 'last_error' || /error$/.test(lk) || lk === 'detail') c.wrap = true;
      else c.num = undefined;
      return c;
    });
  }
  function numericGuess(columns, rows) {
    columns.forEach((c) => {
      if (c.num === undefined && !c.wrap && rows.length && rows.every((r) => r[c.key] === null || isNum(r[c.key])) && !/port|hostgroup|_id$|^id$|checksum|digest|version/i.test(c.key)) c.num = true;
    });
    return columns;
  }

  // ------------------------------------------------------------------ modal & confirm
  function modal(title, bodyEl, buttons, opts) {
    opts = opts || {};
    const root = $('#modal-root');
    const footer = h('footer');
    const box = h('div', { class: 'modal' + (opts.wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('header', null, h('h2', null, title), h('button', { class: 'icon-btn', 'aria-label': 'Close', onclick: () => close() }, '✕')),
      h('div', { class: 'body' }, bodyEl), footer);
    const back = h('div', { class: 'modal-back' }, box);
    back.addEventListener('mousedown', (e) => { if (e.target === back && !opts.sticky) close(); });
    function onKey(e) { if (e.key === 'Escape') close(); }
    function close() { back.remove(); document.removeEventListener('keydown', onKey); }
    document.addEventListener('keydown', onKey);
    (opts.footerLeft ? [opts.footerLeft, h('span', { class: 'spacer' })] : []).concat(buttons || []).forEach((b) => footer.appendChild(b));
    root.appendChild(back);
    const first = box.querySelector('input:not([type=hidden]):not([disabled]), select, textarea');
    if (first) setTimeout(() => first.focus(), 30);
    return { close, box };
  }
  function confirmDlg(title, message, okLabel, danger) {
    return new Promise((resolve) => {
      let done = false;
      const ok = h('button', { class: 'btn ' + (danger ? 'danger' : 'primary') }, okLabel || 'Confirm');
      const cancel = h('button', { class: 'btn' }, 'Cancel');
      const m = modal(title, h('div', null, message), [cancel, ok]);
      ok.addEventListener('click', () => { done = true; m.close(); resolve(true); });
      cancel.addEventListener('click', () => { m.close(); });
      const obs = new MutationObserver(() => { if (!document.body.contains(m.box)) { obs.disconnect(); if (!done) resolve(false); } });
      obs.observe($('#modal-root'), { childList: true });
      setTimeout(() => ok.focus(), 40);
    });
  }

  // remembered "apply immediately" choices used by edit dialogs
  function applyControls() {
    const pref = store.get('apply', { load_runtime: false, save_disk: false });
    const rt = h('input', { type: 'checkbox', checked: pref.load_runtime });
    const dk = h('input', { type: 'checkbox', checked: pref.save_disk });
    const el = h('div', { class: 'toolbar', style: 'margin:0' },
      h('label', { class: 'check', title: 'Run LOAD … TO RUNTIME after saving' }, rt, 'Load to runtime'),
      h('label', { class: 'check', title: 'Run SAVE … TO DISK after saving' }, dk, 'Save to disk'));
    return {
      el,
      value() {
        const v = { load_runtime: rt.checked, save_disk: dk.checked };
        store.set('apply', v);
        return Object.keys(v).filter((k) => v[k]);
      },
    };
  }

  // ------------------------------------------------------------------ line chart (single series, SVG)
  function LineChart(title, fmtVal) {
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', title);
    const tip = h('div', { class: 'tooltip hidden' });
    const nowEl = h('span', { class: 'now' });
    const wrap = h('div', { class: 'chart-wrap' }, svg, tip);
    const el = h('div', { class: 'card' }, h('div', { class: 'chart-head' }, h('h2', null, title), nowEl), wrap);
    let pts = [];
    const pad = { l: 44, r: 8, t: 8, b: 22 };
    const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

    function niceMax(v) {
      if (v <= 0) return 4;
      const p = Math.pow(10, Math.floor(Math.log10(v))); const n = v / p;
      // steps divisible by the 4 grid intervals so tick labels stay round
      return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 4 ? 4 : n <= 8 ? 8 : 10) * p;
    }
    function draw() {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      const W = wrap.clientWidth || 400, H = wrap.clientHeight || 180;
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
      const max = niceMax(Math.max.apply(null, pts.map((p) => p.v).concat([0])));
      const t0 = pts.length ? pts[0].t : 0, t1 = pts.length ? pts[pts.length - 1].t : 1;
      const x = (t) => pad.l + (t1 === t0 ? iw : (t - t0) / (t1 - t0) * iw);
      const y = (v) => pad.t + ih - v / max * ih;
      for (let i = 0; i <= 4; i++) {
        const v = max * i / 4;
        svg.appendChild(mk('line', { class: 'gridline', x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v) }));
        const lbl = mk('text', { class: 'axis', x: pad.l - 6, y: y(v) + 4, 'text-anchor': 'end' });
        lbl.textContent = v < 10 ? String(Number(v.toFixed(2))) : fmtCompact(v);
        svg.appendChild(lbl);
      }
      if (pts.length) {
        [pts[0], pts[pts.length - 1]].forEach((p, i) => {
          const lbl = mk('text', { class: 'axis', x: x(p.t), y: H - 4, 'text-anchor': i ? 'end' : 'start' });
          lbl.textContent = new Date(p.t).toLocaleTimeString();
          svg.appendChild(lbl);
        });
      }
      if (pts.length > 1) {
        const d = pts.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(p.v).toFixed(1)).join('');
        svg.appendChild(mk('path', { class: 'area', d: d + 'L' + x(t1) + ',' + y(0) + 'L' + x(t0) + ',' + y(0) + 'Z' }));
        svg.appendChild(mk('path', { class: 'line', d }));
      }
      const hair = mk('line', { class: 'hair', y1: pad.t, y2: pad.t + ih, visibility: 'hidden' });
      const dot = mk('circle', { class: 'marker', r: 4, visibility: 'hidden' });
      svg.appendChild(hair); svg.appendChild(dot);
      const hit = mk('rect', { x: pad.l, y: 0, width: iw, height: H, fill: 'transparent' });
      svg.appendChild(hit);
      hit.addEventListener('pointermove', (ev) => {
        if (!pts.length) return;
        const r = svg.getBoundingClientRect();
        const px = (ev.clientX - r.left) * (W / r.width);
        let best = pts[0];
        pts.forEach((p) => { if (Math.abs(x(p.t) - px) < Math.abs(x(best.t) - px)) best = p; });
        const bx = x(best.t), by = y(best.v);
        hair.setAttribute('x1', bx); hair.setAttribute('x2', bx); hair.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', bx); dot.setAttribute('cy', by); dot.setAttribute('visibility', 'visible');
        clear(tip);
        tip.appendChild(h('strong', null, fmtVal(best.v)));
        tip.appendChild(h('div', { class: 'muted' }, new Date(best.t).toLocaleTimeString()));
        tip.classList.remove('hidden');
        const sx = bx * (r.width / W);
        tip.style.left = Math.min(Math.max(sx + 10, 0), r.width - tip.offsetWidth) + 'px';
        tip.style.top = '4px';
      });
      hit.addEventListener('pointerleave', () => { tip.classList.add('hidden'); hair.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); });
    }
    const ro = new ResizeObserver(() => draw());
    ro.observe(wrap);
    return {
      el,
      update(points) {
        pts = points;
        nowEl.textContent = pts.length ? fmtVal(pts[pts.length - 1].v) : '';
        draw();
      },
    };
  }

  // ------------------------------------------------------------------ navigation
  const NAV = [
    ['Overview', [['dashboard', 'Dashboard']]],
    ['Configuration', [
      ['servers', 'Servers & Hostgroups', null, ['MYSQL SERVERS']],
      ['users', 'MySQL Users', null, ['MYSQL USERS']],
      ['rules', 'Query Rules', null, ['MYSQL QUERY RULES']],
      ['variables', 'Global Variables', null, ['MYSQL VARIABLES', 'ADMIN VARIABLES']],
      ['cluster', 'ProxySQL Cluster', null, ['PROXYSQL SERVERS']],
      ['scheduler', 'Scheduler', null, ['SCHEDULER']],
      ['sync', 'Load / Save Config'],
    ]],
    ['Observability', [
      ['stats', 'Statistics'],
      ['digest', 'Query Digest'],
      ['processlist', 'Processlist'],
      ['monitor', 'Monitor Logs'],
    ]],
    ['Administration', [
      ['service', 'Service & Logs', 'operator'],
      ['sql', 'SQL Console', 'admin'],
      ['webui-users', 'Web UI Users', 'admin'],
      ['audit', 'Audit Log', 'admin'],
    ]],
  ];

  function renderNav() {
    const nav = clear($('#nav'));
    const route = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
    NAV.forEach(([group, items]) => {
      const visible = items.filter((it) => (!it[2] || can(it[2])) && (it[0] !== 'service' || S.serviceEnabled));
      if (!visible.length) return;
      nav.appendChild(h('div', { class: 'nav-group' }, group));
      visible.forEach(([id, label, , mods]) => {
        const pending = (mods || []).some((m) => S.pending[m]);
        nav.appendChild(h('a', { class: 'nav-link' + (route === id ? ' active' : ''), href: '#/' + id },
          label, pending ? h('span', { class: 'dot', title: 'Unapplied changes' }) : null));
      });
    });
  }

  async function refreshPending() {
    if (!S.inst) return;
    try {
      const r = await api('GET', IU('/sync-status'));
      S.pending = {};
      let n = 0;
      r.modules.forEach((m) => { if (m.runtime || m.disk) { S.pending[m.module] = m; n++; } });
      const b = $('#pending-badge');
      if (n) { b.textContent = '⚠ ' + n + ' module' + (n > 1 ? 's' : '') + ' not applied'; b.classList.remove('hidden'); }
      else b.classList.add('hidden');
      renderNav();
      return r;
    } catch (e) { return null; }
  }

  async function checkInstance() {
    const el = $('#instance-status');
    try {
      const r = await api('GET', IU('/ping'));
      clear(el);
      if (r.ok) { el.className = 'pill good'; append(el, ['● ', 'Connected', r.version ? ' · v' + r.version.split('-')[0] : '']); }
      else { el.className = 'pill bad'; el.title = r.error; append(el, '✕ Unreachable'); }
    } catch (e) { /* ignore */ }
  }

  const PAGES = {};

  async function route() {
    S.pageTimers.forEach(clearInterval);
    S.pageTimers = [];
    const id = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
    S.route = id;
    renderNav();
    $('#sidebar').classList.remove('open');
    const view = clear($('#view'));
    const page = PAGES[id] || PAGES.dashboard;
    try { await page(view); } catch (e) { view.appendChild(h('div', { class: 'notice warn' }, e.message || String(e))); }
  }

  function pageHead(title, sub, right) {
    return h('div', { class: 'page-head' },
      h('div', null, h('h1', null, title), sub ? h('div', { class: 'muted small' }, sub) : null),
      h('span', { class: 'spacer' }), right || null);
  }

  function tabs(defs, onSelect, initial) {
    const bar = h('div', { class: 'tabs', role: 'tablist' });
    let cur = initial || defs[0][0];
    function sel(id) {
      cur = id;
      Array.from(bar.children).forEach((b) => b.classList.toggle('active', b.dataset.id === id));
      onSelect(id);
    }
    defs.forEach(([id, label]) => {
      const b = h('button', { class: 'tab', role: 'tab', 'data-id': id }, label);
      b.addEventListener('click', () => sel(id));
      bar.appendChild(b);
    });
    setTimeout(() => sel(cur), 0);
    return bar;
  }

  async function runCommand(action, modules, label) {
    try {
      const r = await api('POST', IU('/command'), { action, modules });
      r.results.forEach((x) => toast((x.ok ? '✓ ' : '✕ ') + x.sql + (x.error ? ' — ' + x.error : ''), x.ok ? 'ok' : 'err'));
      refreshPending();
      return r.ok;
    } catch (e) { fail(e); return false; }
  }

  function moduleButtons(module, after) {
    if (!can('operator')) return null;
    return h('span', { class: 'toolbar', style: 'margin:0' },
      h('button', { class: 'btn', title: 'LOAD ' + module + ' TO RUNTIME', onclick: async () => { await runCommand('load_runtime', [module]); after && after(); } }, '⇡ Load to runtime'),
      h('button', { class: 'btn', title: 'SAVE ' + module + ' TO DISK', onclick: async () => { await runCommand('save_disk', [module]); after && after(); } }, '⤓ Save to disk'));
  }

  // ------------------------------------------------------------------ generic config-table page
  function TableEditor(container, table, opts) {
    opts = opts || {};
    let meta = null;
    let source = 'memory';
    let showAll = store.get('showAll.' + table, false);
    const dt = DataTable({ columns: [], rows: [], empty: 'No rows configured.' });
    const sourceSel = h('select', { 'aria-label': 'Data source', title: 'Which layer to display' },
      h('option', { value: 'memory' }, 'Memory (editable)'), h('option', { value: 'runtime' }, 'Runtime (active)'), h('option', { value: 'disk' }, 'Disk (saved)'));
    sourceSel.addEventListener('change', () => { source = sourceSel.value; load(); });
    const allCols = h('input', { type: 'checkbox', checked: showAll });
    allCols.addEventListener('change', () => { showAll = allCols.checked; store.set('showAll.' + table, showAll); build(); });
    const addBtn = can('operator') ? h('button', { class: 'btn primary', onclick: () => edit(null) }, '+ Add') : null;
    const notice = h('div');
    const toolbar = h('div', { class: 'toolbar' }, addBtn, dt.searchInput(), sourceSel,
      h('label', { class: 'check' }, allCols, 'All columns'),
      h('button', { class: 'btn', onclick: () => load() }, '↻ Refresh'),
      h('span', { class: 'spacer' }), null);
    container.appendChild(toolbar);
    container.appendChild(notice);
    container.appendChild(dt.el);

    function build() {
      if (!meta) return;
      const cols = meta.columns.filter((c) => showAll || !c.advanced).map((c) => {
        const col = { key: c.name, label: c.label };
        if (c.type === 'int') { col.num = true; if (/port|hostgroup|_id$|^id$/.test(c.name)) col.fmt = (v) => (v === null ? 'NULL' : String(v)); }
        if (c.type === 'bool') col.fmt = (v) => (v === null ? 'NULL' : yesNo(v));
        if (c.name === 'status') col.fmt = (v) => statusPill(v);
        if (c.type === 'password') col.fmt = (v) => (v ? (String(v).startsWith('*') && v.length === 41 ? h('span', { class: 'muted mono', title: v }, 'hashed') : (v === '***' ? '***' : h('span', { class: 'muted' }, '•••••• (clear text)'))) : h('span', { class: 'muted' }, 'empty'));
        if (c.type === 'textarea' || /match_|pattern|digest|msg/.test(c.name)) col.wrap = true;
        return col;
      });
      const editable = source === 'memory' && can('operator');
      dt.el.replaceWith((function () {
        const n = DataTable({
          columns: cols, rows: meta.rows, empty: 'No rows in ' + source + '.',
          actions: editable ? (r) => [
            h('button', { class: 'btn small', onclick: () => edit(r) }, 'Edit'), ' ',
            h('button', { class: 'btn small', onclick: () => edit(r, true) }, 'Clone'), ' ',
            h('button', { class: 'btn small danger', onclick: () => remove(r) }, 'Delete')] : null,
          onRowClick: editable ? (r) => edit(r) : null,
        });
        // keep filter input working with the new table
        const old = toolbar.querySelector('input.search');
        const neu = n.searchInput(); neu.value = old.value; old.replaceWith(neu); n.setFilter(old.value);
        Object.assign(dt, n);
        return n.el;
      })());
    }

    async function load() {
      try {
        meta = await api('GET', IU('/table/' + table + '?source=' + source));
        clear(notice);
        if (source !== 'memory') notice.appendChild(h('div', { class: 'notice' }, 'Showing the ' + source + ' layer (read-only). Switch to Memory to edit.'));
        if (opts.note) notice.appendChild(opts.note);
        build();
      } catch (e) { fail(e); }
    }

    function keyOf(r) { const k = {}; meta.pk.forEach((p) => { if (p in r) k[p] = r[p]; }); return k; }

    async function remove(r) {
      const desc = meta.pk.filter((p) => p in r).map((p) => p + '=' + r[p]).join(', ');
      const apply = applyControls();
      const ok = await confirmDlg('Delete row', h('div', null, h('p', null, 'Delete ', h('code', null, desc), ' from ', h('code', null, table), '?'), apply.el), 'Delete', true);
      if (!ok) return;
      try {
        await api('DELETE', IU('/table/' + table), { key: keyOf(r), apply: apply.value() });
        toast('Row deleted', 'ok');
        await load(); refreshPending();
      } catch (e) { fail(e); }
    }

    function edit(row, clone) {
      const isNew = !row || clone;
      const inputs = {};
      const basic = h('div', { class: 'form-grid' });
      const adv = h('div', { class: 'form-grid' });
      let hashBox = null;
      meta.columns.forEach((c) => {
        let v = row ? row[c.name] : c.default;
        if (clone && c.type === 'password') v = '';
        let input;
        if (c.type === 'bool') {
          input = h('select', null, h('option', { value: '1' }, 'yes'), h('option', { value: '0' }, 'no'));
          if (c.nullable) input.insertBefore(h('option', { value: '' }, 'NULL'), input.firstChild);
          input.value = v === null || v === undefined ? (c.nullable ? '' : '0') : String(v);
        } else if (c.type === 'enum') {
          input = h('select', null, (c.choices || []).map((o) => h('option', { value: o }, o)));
          input.value = v === null || v === undefined ? (c.choices || [''])[0] : String(v);
        } else if (c.type === 'textarea') {
          input = h('textarea', { rows: 3 }); input.value = v === null || v === undefined ? '' : v;
        } else if (c.type === 'password') {
          input = h('input', { type: 'password', autocomplete: 'new-password', placeholder: isNew ? '' : '(unchanged)' });
        } else {
          input = h('input', { type: c.type === 'int' ? 'number' : 'text', placeholder: c.nullable ? 'NULL' : '' });
          input.value = v === null || v === undefined ? '' : v;
        }
        inputs[c.name] = input;
        const field = h('label', { class: c.type === 'textarea' || /match_|pattern|msg/.test(c.name) || c.name === 'comment' ? 'full' : null },
          c.label + (c.required ? ' *' : ''), input, c.help ? h('span', { class: 'help' }, c.help) : null);
        (c.advanced ? adv : basic).appendChild(field);
        if (c.type === 'password') {
          hashBox = h('input', { type: 'checkbox' });
          basic.appendChild(h('label', { class: 'check full' }, hashBox, 'Store as mysql_native_password hash (do not use with caching_sha2_password users)'));
        }
      });
      const err = h('div', { class: 'form-error' });
      const apply = applyControls();
      const body = h('div', null, basic, adv.children.length ? h('details', { class: 'adv', open: showAll || null }, h('summary', null, 'Advanced fields'), adv) : null, err);
      const save = h('button', { class: 'btn primary' }, isNew ? 'Create' : 'Save');
      const cancel = h('button', { class: 'btn' }, 'Cancel');
      const m = modal((isNew ? 'New ' : 'Edit ') + meta.label.replace(/s$/, '').toLowerCase(), body, [cancel, save], { wide: true, footerLeft: apply.el, sticky: true });
      cancel.addEventListener('click', () => m.close());
      save.addEventListener('click', async () => {
        const values = {};
        Object.keys(inputs).forEach((k) => { values[k] = inputs[k].value; });
        save.disabled = true; err.textContent = '';
        try {
          const payload = { values, apply: apply.value(), hash_password: hashBox ? hashBox.checked : false };
          const r = isNew ? await api('POST', IU('/table/' + table), payload)
            : await api('PUT', IU('/table/' + table), Object.assign(payload, { key: keyOf(row) }));
          m.close();
          toast((isNew ? 'Created' : 'Saved') + (r.applied && r.applied.length ? ' · ' + r.applied.join(' · ') : ' in memory'), 'ok');
          await load(); refreshPending();
        } catch (e) { err.textContent = e.message; save.disabled = false; }
      });
    }

    load();
    return { reload: load };
  }

  function configPage(view, title, sub, tableTabs, module) {
    view.appendChild(pageHead(title, sub, moduleButtons(module)));
    const holder = h('div');
    if (tableTabs.length > 1) {
      view.appendChild(tabs(tableTabs.map((t) => [t[0], t[1]]), (id) => {
        clear(holder);
        const t = tableTabs.find((x) => x[0] === id);
        TableEditor(holder, id, { note: t[2] || null });
      }));
    } else {
      TableEditor(holder, tableTabs[0][0], { note: tableTabs[0][2] || null });
    }
    view.appendChild(holder);
  }

  // ------------------------------------------------------------------ pages: configuration
  PAGES.servers = (view) => configPage(view, 'Servers & Hostgroups',
    'Backend MariaDB servers and automatic hostgroup management. Changes go to memory; load them to runtime to activate.',
    [['mysql_servers', 'Backend servers', h('div', { class: 'notice' },
      'Servers have no user/password here. Client queries are sent to the backend using the credentials from MySQL Users, and health checks log in with mysql-monitor_username / mysql-monitor_password (Global Variables). Both accounts must exist on every backend server.')],
      ['mysql_replication_hostgroups', 'Replication hostgroups', h('div', { class: 'notice' }, 'Async replication: the monitor moves servers between writer and reader hostgroups based on read_only. Runtime server placement may therefore differ from memory.')],
      ['mysql_galera_hostgroups', 'Galera hostgroups', h('div', { class: 'notice' }, 'MariaDB Galera Cluster: the monitor checks wsrep state and places nodes in writer / backup writer / reader / offline hostgroups.')],
      ['mysql_group_replication_hostgroups', 'Group replication']],
    'MYSQL SERVERS');

  PAGES.users = (view) => configPage(view, 'MySQL Users', 'Accounts clients use to connect through ProxySQL. They must also exist on the backend servers.',
    [['mysql_users', 'Users']], 'MYSQL USERS');

  PAGES.rules = (view) => configPage(view, 'Query Rules', 'Routing, rewriting, caching and firewalling rules, evaluated in rule_id order.',
    [['mysql_query_rules', 'Rules']], 'MYSQL QUERY RULES');

  PAGES.scheduler = (view) => configPage(view, 'Scheduler', 'External scripts ProxySQL runs periodically.',
    [['scheduler', 'Jobs']], 'SCHEDULER');

  PAGES.cluster = async (view) => {
    view.appendChild(pageHead('ProxySQL Cluster', 'Peer ProxySQL nodes and configuration checksum propagation.', moduleButtons('PROXYSQL SERVERS')));
    const holder = h('div');
    view.appendChild(holder);
    TableEditor(holder, 'proxysql_servers');
    const own = h('div'), peers = h('div'), metrics = h('div');
    view.appendChild(h('div', { class: 'grid', style: 'margin-top:20px' },
      h('div', { class: 'card' }, h('h2', null, 'This node: runtime checksums'), own),
      h('div', { class: 'card' }, h('h2', null, 'Peer checksums'), peers),
      h('div', { class: 'card' }, h('h2', null, 'Peer metrics'), metrics)));
    async function load() {
      for (const [kind, el] of [['checksums', own], ['cluster_checksums', peers], ['cluster_metrics', metrics]]) {
        try {
          const r = await api('GET', IU('/stats/' + kind));
          clear(el).appendChild(DataTable({ columns: numericGuess(autoColumns(r.columns).map((c) => (/epoch|changed_at|updated_at/.test(c.key) ? Object.assign(c, { fmt: fmtEpoch, num: false }) : c)), r.rows), rows: r.rows, empty: 'No data (cluster not configured).' }).el);
        } catch (e) { clear(el).appendChild(h('div', { class: 'muted' }, e.message)); }
      }
    }
    load();
    every(15000, load);
  };

  PAGES.variables = async (view) => {
    view.appendChild(pageHead('Global Variables', 'mysql-* and admin-* settings. Rows highlighted have a memory value that differs from runtime.',
      can('operator') ? h('span', { class: 'toolbar', style: 'margin:0' },
        h('button', { class: 'btn', onclick: async () => { await runCommand('load_runtime', ['MYSQL VARIABLES', 'ADMIN VARIABLES']); load(); } }, '⇡ Load to runtime'),
        h('button', { class: 'btn', onclick: async () => { await runCommand('save_disk', ['MYSQL VARIABLES', 'ADMIN VARIABLES']); load(); } }, '⤓ Save to disk')) : null));
    let rows = [], prefix = store.get('varPrefix', 'mysql-'), onlyChanged = false;
    const holder = h('div');
    const dt = DataTable({
      columns: [
        { key: 'name', label: 'Variable', get: (r) => r.name },
        { key: 'value', label: 'Memory', wrap: true },
        { key: 'runtime', label: 'Runtime', wrap: true },
        { key: 'disk', label: 'Disk', wrap: true },
      ],
      rows: [],
      rowClass: (r) => (r.runtime !== null && r.value !== r.runtime ? 'changed' : null),
      actions: can('operator') ? (r) => h('button', { class: 'btn small', onclick: () => edit(r) }, 'Edit') : null,
      onRowClick: can('operator') ? (r) => edit(r) : null,
      limit: 5000,
    });
    const prefixSel = h('select', { 'aria-label': 'Variable group' },
      ['mysql-', 'admin-', 'other', 'all'].map((p) => h('option', { value: p }, p === 'other' ? 'other modules' : p === 'all' ? 'all variables' : p + '*')));
    prefixSel.value = prefix;
    prefixSel.addEventListener('change', () => { prefix = prefixSel.value; store.set('varPrefix', prefix); show(); });
    const changedBox = h('input', { type: 'checkbox' });
    changedBox.addEventListener('change', () => { onlyChanged = changedBox.checked; show(); });
    holder.appendChild(h('div', { class: 'toolbar' }, prefixSel, dt.searchInput(),
      h('label', { class: 'check' }, changedBox, 'Only not applied'),
      h('button', { class: 'btn', onclick: () => load() }, '↻ Refresh')));
    holder.appendChild(dt.el);
    view.appendChild(holder);

    function show() {
      dt.setRows(rows.filter((r) => {
        if (onlyChanged && !(r.value !== r.runtime || r.value !== r.disk)) return false;
        if (prefix === 'all') return true;
        if (prefix === 'other') return !r.name.startsWith('mysql-') && !r.name.startsWith('admin-');
        return r.name.startsWith(prefix);
      }));
    }
    async function load() {
      try { rows = (await api('GET', IU('/variables'))).rows; show(); } catch (e) { fail(e); }
    }
    function edit(r) {
      const secret = /password|credentials/.test(r.name);
      const input = h(String(r.value).length > 60 ? 'textarea' : 'input', { rows: 3, type: secret ? 'password' : 'text' });
      input.value = r.value === null ? '' : r.value;
      const apply = applyControls();
      const err = h('div', { class: 'form-error' });
      const save = h('button', { class: 'btn primary' }, 'Save');
      const body = h('div', { class: 'grid' },
        h('label', null, r.name, input),
        h('dl', { class: 'kv' }, h('dt', null, 'Runtime'), h('dd', { class: 'mono' }, secret ? '***' : String(r.runtime)), h('dt', null, 'Disk'), h('dd', { class: 'mono' }, secret ? '***' : String(r.disk))),
        r.module ? null : h('div', { class: 'notice warn' }, 'This variable belongs to a module without a Load/Save shortcut here; use the SQL console to apply it.'),
        err);
      const m = modal('Edit variable', body, [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save], { footerLeft: apply.el });
      const submit = async () => {
        save.disabled = true;
        try {
          const res = await api('PUT', IU('/variables'), { name: r.name, value: input.value, apply: apply.value() });
          m.close();
          if (res.applied.length && res.runtime !== null && res.runtime !== input.value) {
            toast('Saved, but runtime value is "' + res.runtime + '" — ProxySQL rejected or normalized the value, or it needs a restart.', 'err');
          } else toast('Saved' + (res.applied.length ? ' · ' + res.applied.join(' · ') : ' in memory'), 'ok');
          load(); refreshPending();
        } catch (e) { err.textContent = e.message; save.disabled = false; }
      };
      save.addEventListener('click', submit);
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && input.tagName === 'INPUT') submit(); });
    }
    load();
  };

  PAGES.sync = async (view) => {
    view.appendChild(pageHead('Load / Save Configuration', 'ProxySQL keeps three layers. Edits land in Memory; LOAD … TO RUNTIME activates them; SAVE … TO DISK persists them across restarts.',
      can('operator') ? h('button', {
        class: 'btn primary',
        onclick: async () => {
          if (!await confirmDlg('Apply everything', 'Load all modules to runtime and save them to disk?', 'Apply all')) return;
          const mods = lastStatus ? lastStatus.modules.map((m) => m.module) : [];
          await runCommand('load_runtime', mods); await runCommand('save_disk', mods); load();
        },
      }, 'Apply all (runtime + disk)') : null));
    view.appendChild(h('div', { class: 'flow' },
      h('span', { class: 'box' }, 'Disk'), '⇄ LOAD FROM DISK / SAVE TO DISK ⇄',
      h('span', { class: 'box' }, 'Memory'), '⇄ LOAD TO RUNTIME / SAVE FROM RUNTIME ⇄',
      h('span', { class: 'box' }, 'Runtime')));
    view.appendChild(h('div', { class: 'notice' }, 'ProxySQL Cluster: peers copy a module from this node only after it is loaded to RUNTIME here ' +
      '(and save it to their disk when admin-cluster_<module>_save_to_disk is true). Changes kept only in memory are not propagated.'));
    const grid = h('div', { class: 'sync-grid' });
    view.appendChild(grid);
    let lastStatus = null;

    const confirmTexts = {
      load_disk: 'This overwrites the in-memory configuration of %s with what is saved on disk. Unsaved edits are lost.',
      save_memory: 'This overwrites the in-memory configuration of %s with the currently active runtime configuration.',
      load_config: 'This overwrites the in-memory configuration of %s with the contents of proxysql.cnf.',
    };
    async function act(module, action) {
      if (confirmTexts[action] && !await confirmDlg('Confirm', confirmTexts[action].replace('%s', module), 'Continue', true)) return;
      await runCommand(action, [module]);
      load();
    }
    async function load() {
      const r = await refreshPending();
      if (!r) return;
      lastStatus = r;
      clear(grid);
      r.modules.forEach((m) => {
        const st = (n, what) => (m.error ? h('span', { class: 'pill bad' }, '✕ error') : n
          ? h('span', { class: 'pill warn' }, '⚠ ' + n + ' diff vs ' + what)
          : h('span', { class: 'pill good' }, '✓ ' + what + ' in sync'));
        grid.appendChild(h('div', { class: 'card sync-card' },
          h('h3', null, m.module),
          h('div', { class: 'states' }, st(m.runtime, 'runtime'), st(m.disk, 'disk')),
          m.error ? h('div', { class: 'small muted', style: 'margin-bottom:8px' }, m.error) : null,
          h('div', { class: 'actions' },
            can('operator') ? [
              h('button', { class: 'btn small primary', onclick: () => act(m.module, 'load_runtime') }, 'Load to runtime'),
              h('button', { class: 'btn small', onclick: () => act(m.module, 'save_disk') }, 'Save to disk'),
              h('button', { class: 'btn small', onclick: () => act(m.module, 'load_disk') }, 'Load from disk'),
              h('button', { class: 'btn small', onclick: () => act(m.module, 'save_memory') }, 'Save from runtime'),
            ] : null,
            h('button', { class: 'btn small', onclick: () => showDiff(m.module, 'runtime') }, 'Diff runtime'),
            h('button', { class: 'btn small', onclick: () => showDiff(m.module, 'disk') }, 'Diff disk'))));
      });
    }
    async function showDiff(module, against) {
      try {
        const r = await api('GET', IU('/diff?module=' + encodeURIComponent(module) + '&against=' + against));
        const body = h('div', null, h('p', { class: 'muted small' },
          'Green rows exist only in memory (will be applied); red rows exist only in ' + against + ' (will be replaced). ' +
          (against === 'runtime' ? 'Some differences are ignored because runtime legitimately differs: server status, user passwords/frontend/backend, and the hostgroup of servers inside replication/Galera hostgroups (shown as managed:<writer_hostgroup>).' : '')));
        r.tables.forEach((t) => {
          body.appendChild(h('h2', { style: 'margin-top:14px' }, t.table));
          if (t.error) { body.appendChild(h('div', { class: 'notice warn' }, t.error)); return; }
          if (!t.only_memory.length && !t.only_other.length) { body.appendChild(h('div', { class: 'muted' }, 'No differences.')); return; }
          const rows = t.only_memory.map((v) => ({ _side: 'memory', v })).concat(t.only_other.map((v) => ({ _side: against, v })));
          const cols = [{ key: '_side', label: 'layer', get: (x) => x._side }].concat(t.columns.map((c, i) => ({ key: c, label: c, get: (x) => x.v[i] })));
          body.appendChild(DataTable({ columns: cols, rows, rowClass: (x) => (x._side === 'memory' ? 'diff-add' : 'diff-del') }).el);
        });
        modal(module + ': memory vs ' + against, body, [], { wide: true });
      } catch (e) { fail(e); }
    }
    load();
    every(20000, load);
  };

  // ------------------------------------------------------------------ pages: dashboard
  PAGES.dashboard = async (view) => {
    view.appendChild(pageHead('Dashboard', S.instances.find((i) => i.key === S.inst).label + ' — ' + S.instances.find((i) => i.key === S.inst).host + ':' + S.instances.find((i) => i.key === S.inst).port));
    const tiles = h('div', { class: 'tiles' });
    const qps = LineChart('Queries per second', (v) => fmtCompact(v) + ' q/s');
    const conns = LineChart('Client connections', (v) => fmtNum(Math.round(v)));
    const charts = h('div', { class: 'charts' }, qps.el, conns.el);
    const servers = h('div'); const pool = h('div');
    view.appendChild(tiles); view.appendChild(charts);
    view.appendChild(h('div', { class: 'grid' },
      h('div', { class: 'card' }, h('h2', null, 'Backend servers (runtime)'), servers),
      h('div', { class: 'card' }, h('h2', null, 'Connection pool'), pool)));

    const key = S.inst;
    const hist = S.hist[key] = S.hist[key] || { qps: [], conns: [], last: null };
    const serversDt = DataTable({
      columns: [{ key: 'hostgroup_id', label: 'HG' }, { key: 'hostname', label: 'Host' }, { key: 'port', label: 'Port' },
        { key: 'status', label: 'Status', fmt: statusPill }, { key: 'weight', label: 'Weight', num: true },
        { key: 'max_connections', label: 'Max conns', num: true }, { key: 'comment', label: 'Comment' }],
      rows: [], empty: 'No backend servers in runtime.',
    });
    servers.appendChild(serversDt.el);
    const poolDt = DataTable({
      columns: [{ key: 'hostgroup', label: 'HG' }, { key: 'srv_host', label: 'Host' }, { key: 'srv_port', label: 'Port' },
        { key: 'status', label: 'Status', fmt: statusPill }, { key: 'ConnUsed', label: 'Used', num: true },
        { key: 'ConnFree', label: 'Free', num: true }, { key: 'ConnOK', label: 'OK', num: true }, { key: 'ConnERR', label: 'Errors', num: true },
        { key: 'MaxConnUsed', label: 'Max used', num: true }, { key: 'Queries', label: 'Queries', num: true },
        { key: 'Bytes_data_sent', label: 'Sent', num: true, fmt: fmtBytes }, { key: 'Bytes_data_recv', label: 'Received', num: true, fmt: fmtBytes },
        { key: 'Latency_us', label: 'Latency', num: true, fmt: fmtUs }],
      rows: [], empty: 'No pool entries.',
    });
    pool.appendChild(poolDt.el);

    function tile(label, value, sub, kind) {
      return h('div', { class: 'tile' }, h('div', { class: 'label' }, label),
        h('div', { class: 'value' }, kind ? h('span', { class: 'pill ' + kind, style: 'font-size:15px' }, value) : value),
        sub ? h('div', { class: 'sub' }, sub) : null);
    }

    async function load() {
      let r;
      try { r = await api('GET', IU('/overview')); } catch (e) {
        clear(tiles).appendChild(h('div', { class: 'notice warn', style: 'grid-column:1/-1' }, e.message)); return;
      }
      const g = r.glob, now = Date.now();
      const questions = Number(g.Questions || 0);
      if (hist.last && questions >= hist.last.q) {
        const dt = (now - hist.last.t) / 1000;
        if (dt > 0) hist.qps.push({ t: now, v: (questions - hist.last.q) / dt });
      }
      hist.last = { t: now, q: questions };
      hist.conns.push({ t: now, v: Number(g.Client_Connections_connected || 0) });
      const cutoff = now - 10 * 60 * 1000;
      hist.qps = hist.qps.filter((p) => p.t >= cutoff);
      hist.conns = hist.conns.filter((p) => p.t >= cutoff);
      qps.update(hist.qps); conns.update(hist.conns);

      // a server can sit in several hostgroups (writer is also reader): count each host:port once
      const hosts = {};
      r.servers.forEach((s) => { const k = s.hostname + ':' + s.port; hosts[k] = hosts[k] || s.status === 'ONLINE'; });
      const online = Object.values(hosts).filter(Boolean).length;
      const total = Object.keys(hosts).length;
      clear(tiles);
      [
        tile('Version', (r.version || '?').split('-')[0], r.version),
        tile('Uptime', fmtDuration(g.ProxySQL_Uptime)),
        tile('Backends online', online + ' / ' + total, total - online ? (total - online) + ' not online' : 'all healthy', total === 0 ? '' : (online === total ? 'good' : online ? 'warn' : 'bad')),
        tile('Client connections', fmtNum(g.Client_Connections_connected), fmtNum(g.Client_Connections_created) + ' created · ' + fmtNum(g.Client_Connections_aborted) + ' aborted'),
        tile('Server connections', fmtNum(g.Server_Connections_connected), fmtNum(g.Server_Connections_created) + ' created · ' + fmtNum(g.Server_Connections_aborted) + ' aborted'),
        tile('Queries', fmtCompact(g.Questions), hist.qps.length ? fmtCompact(hist.qps[hist.qps.length - 1].v) + ' q/s now' : 'measuring…'),
        tile('Slow queries', fmtNum(g.Slow_queries)),
        tile('Active transactions', fmtNum(g.Active_Transactions)),
        tile('Query cache', fmtBytes(g.Query_Cache_Memory_bytes), fmtNum(g.Query_Cache_count_GET_OK) + ' hits'),
        tile('Users / rules', (r.counts.runtime_mysql_users === null ? '?' : r.counts.runtime_mysql_users) + ' / ' + (r.counts.runtime_mysql_query_rules === null ? '?' : r.counts.runtime_mysql_query_rules), 'runtime user rows / query rules'),
      ].forEach((t) => tiles.appendChild(t));
      serversDt.setRows(r.servers);
      poolDt.setRows(r.pool);
    }
    await load();
    every(5000, () => { if (S.inst === key) load(); });
  };

  // ------------------------------------------------------------------ pages: observability
  function resultView(container, url, opts) {
    opts = opts || {};
    let dt = null;
    const out = h('div');
    const auto = h('input', { type: 'checkbox', checked: !!opts.auto });
    const bar = h('div', { class: 'toolbar' });
    const search = h('span');
    bar.appendChild(search);
    if (opts.extra) opts.extra.forEach((x) => bar.appendChild(x));
    bar.appendChild(h('button', { class: 'btn', onclick: () => load() }, '↻ Refresh'));
    bar.appendChild(h('label', { class: 'check' }, auto, 'Auto refresh'));
    if (opts.reset && can('operator')) {
      bar.appendChild(h('span', { class: 'spacer' }));
      bar.appendChild(h('button', {
        class: 'btn danger',
        onclick: async () => {
          if (!await confirmDlg('Reset statistics', 'Reset ' + opts.reset + ' counters? This cannot be undone.', 'Reset', true)) return;
          try { await api('POST', IU('/stats/' + opts.reset + '/reset')); toast('Statistics reset', 'ok'); load(); } catch (e) { fail(e); }
        },
      }, 'Reset counters'));
    }
    container.appendChild(bar);
    container.appendChild(out);
    let filterText = '';
    async function load() {
      try {
        const r = await api('GET', typeof url === 'function' ? url() : url);
        let cols = numericGuess(autoColumns(r.columns), r.rows);
        if (opts.columns) cols = opts.columns(cols);
        if (!dt || dt._sig !== r.columns.join()) {
          dt = DataTable({ columns: cols, rows: r.rows, empty: opts.empty || 'No data.', sortKey: opts.sortKey, sortDesc: opts.sortDesc });
          dt._sig = r.columns.join();
          const si = dt.searchInput(); si.value = filterText;
          si.addEventListener('input', () => { filterText = si.value; });
          clear(search).appendChild(si);
          clear(out).appendChild(dt.el);
          if (filterText) dt.setFilter(filterText);
        } else dt.setRows(r.rows);
      } catch (e) { clear(out).appendChild(h('div', { class: 'notice warn' }, e.message)); dt = null; }
    }
    load();
    every(opts.interval || 5000, () => { if (auto.checked && document.body.contains(out)) load(); });
    return { reload: load };
  }

  PAGES.stats = (view) => {
    view.appendChild(pageHead('Statistics', 'Live counters from the stats schema.'));
    const holder = h('div');
    const defs = [['pool', 'Connection pool', { reset: 'pool' }], ['commands', 'Commands'], ['users', 'Frontend users'],
      ['errors', 'Backend errors', { reset: 'errors' }], ['rules', 'Query rule hits'], ['global', 'Global counters'], ['memory', 'Memory'],
      ['free_connections', 'Free connections'], ['gtid', 'GTID executed']];
    view.appendChild(tabs(defs.map((d) => [d[0], d[1]]), (id) => {
      S.pageTimers.forEach(clearInterval); S.pageTimers = [];
      clear(holder);
      store.set('statsTab', id);
      const d = defs.find((x) => x[0] === id);
      resultView(holder, IU('/stats/' + id), Object.assign({ columns: (cols) => (id === 'memory' || id === 'global' ? cols.map((c) => (c.key === 'Variable_Value' ? Object.assign(c, { num: true, fmt: id === 'memory' ? fmtBytes : fmtNum }) : c)) : cols) }, d[2] || {}));
    }, store.get('statsTab', 'pool')));
    view.appendChild(holder);
  };

  PAGES.digest = (view) => {
    view.appendChild(pageHead('Query Digest', 'Normalized queries seen by ProxySQL, with timing and row counts. Use it to find heavy queries and to write query rules.'));
    const order = h('select', { 'aria-label': 'Order by' }, [['sum_time', 'Total time'], ['count_star', 'Executions'], ['max_time', 'Max time'], ['sum_rows_sent', 'Rows sent'], ['last_seen', 'Last seen']].map(([v, l]) => h('option', { value: v }, 'Top by ' + l)));
    const limit = h('select', { 'aria-label': 'Limit' }, [50, 200, 1000, 5000].map((n) => h('option', { value: n }, n + ' rows')));
    limit.value = '200';
    const rv = resultView(view, () => IU('/stats/digest?order=' + order.value + '&limit=' + limit.value), {
      extra: [order, limit], reset: 'digest', interval: 10000,
      columns: (cols) => {
        cols = cols.filter((c) => c.key !== 'digest');
        const avg = { key: 'avg_time', label: 'avg_time', num: true, get: (r) => (Number(r.count_star) ? Math.round(Number(r.sum_time) / Number(r.count_star)) : 0), fmt: fmtUs };
        const i = cols.findIndex((c) => c.key === 'sum_time');
        cols.splice(i + 1, 0, avg);
        return cols;
      },
    });
    order.addEventListener('change', rv.reload);
    limit.addEventListener('change', rv.reload);
  };

  PAGES.processlist = (view) => {
    view.appendChild(pageHead('Processlist', 'Client sessions currently open through ProxySQL (stats_mysql_processlist).'));
    resultView(view, IU('/stats/processlist'), { auto: true, interval: 3000, empty: 'No active client sessions.' });
  };

  PAGES.monitor = (view) => {
    view.appendChild(pageHead('Monitor Logs', 'Health checks ProxySQL runs against the backends (latest first).'));
    const holder = h('div');
    view.appendChild(tabs([['ping', 'Ping'], ['connect', 'Connect'], ['read_only', 'Read-only'], ['replication_lag', 'Replication lag'], ['galera', 'Galera'], ['group_replication', 'Group replication']], (id) => {
      S.pageTimers.forEach(clearInterval); S.pageTimers = [];
      clear(holder);
      resultView(holder, IU('/monitor/' + id + '?limit=300'), {
        interval: 10000, empty: 'No monitor entries (is monitoring enabled and mysql-monitor_username set?).',
        columns: (cols) => cols.map((c) => (/error/.test(c.key) ? Object.assign(c, { fmt: (v) => (v ? h('span', { class: 'pill bad' }, '✕ ' + v) : h('span', { class: 'pill good' }, '✓ ok')) }) : c)),
      });
    }));
    view.appendChild(holder);
  };

  // ------------------------------------------------------------------ pages: administration
  PAGES.service = async (view) => {
    if (!S.serviceEnabled) { view.appendChild(h('div', { class: 'notice' }, 'Service management is disabled.')); return; }
    const inst = S.instances.find((i) => i.key === S.inst);
    view.appendChild(pageHead('Service & Logs', 'systemd unit ' + S.serviceUnit + ' on the web UI host' + (inst && !inst.local ? ' (the selected instance is remote: this controls the LOCAL service only)' : '') + '.'));
    const info = h('dl', { class: 'kv' });
    const buttons = h('div', { class: 'toolbar', style: 'margin-top:14px' });
    const stateEl = h('span');
    view.appendChild(h('div', { class: 'split' }, h('div', { class: 'card' }, h('div', { class: 'chart-head' }, h('h2', null, 'Service status'), stateEl), info, buttons)));

    async function control(action) {
      const msgs = { stop: 'Stopping ProxySQL drops every client connection going through it.', restart: 'Restarting ProxySQL drops every client connection going through it.', start: 'Start the ProxySQL service?' };
      if (!await confirmDlg(action[0].toUpperCase() + action.slice(1) + ' ProxySQL', msgs[action], action[0].toUpperCase() + action.slice(1), action !== 'start')) return;
      try {
        const r = await api('POST', '/api/service/' + action);
        toast(r.ok ? 'systemctl ' + action + ' ' + S.serviceUnit + ': done' : 'Failed: ' + (r.output || 'rc ' + r.rc), r.ok ? 'ok' : 'err');
      } catch (e) { fail(e); }
      setTimeout(loadStatus, 1200);
      setTimeout(checkInstance, 2500);
    }
    if (can('admin')) {
      buttons.appendChild(h('button', { class: 'btn primary', onclick: () => control('start') }, '▶ Start'));
      buttons.appendChild(h('button', { class: 'btn', onclick: () => control('restart') }, '↻ Restart'));
      buttons.appendChild(h('button', { class: 'btn danger', onclick: () => control('stop') }, '■ Stop'));
    }

    async function loadStatus() {
      try {
        const s = await api('GET', '/api/service/status');
        clear(info); clear(stateEl);
        if (!s.available) { info.appendChild(h('dd', null, s.error)); return; }
        const kind = s.ActiveState === 'active' ? 'good' : s.ActiveState === 'failed' ? 'bad' : 'warn';
        stateEl.appendChild(h('span', { class: 'pill ' + kind, style: 'margin-left:auto' }, (kind === 'good' ? '● ' : kind === 'bad' ? '✕ ' : '◐ ') + s.ActiveState + ' (' + s.SubState + ')'));
        const mem = Number(s.MemoryCurrent);
        [['Unit', s.unit], ['Description', s.Description], ['Enabled at boot', s.UnitFileState], ['Main PID', s.MainPID],
          ['Active since', s.ActiveEnterTimestamp || '—'], ['Memory', isNum(mem) && mem < 1e18 ? fmtBytes(mem) : '—'],
          ['Tasks', s.TasksCurrent], ['Restarts', s.NRestarts], ['Unit file', s.FragmentPath]]
          .forEach(([k, v]) => { info.appendChild(h('dt', null, k)); info.appendChild(h('dd', null, v === undefined || v === '' ? '—' : v)); });
      } catch (e) { clear(info).appendChild(h('dd', null, e.message)); }
    }
    loadStatus();
    every(10000, loadStatus);

    // logs
    const src = h('select', { 'aria-label': 'Log source' }, h('option', { value: 'file' }, 'proxysql.log'), h('option', { value: 'journal' }, 'systemd journal'));
    const flt = h('input', { type: 'search', class: 'search', placeholder: 'Filter lines…' });
    const errOnly = h('input', { type: 'checkbox' });
    const follow = h('input', { type: 'checkbox' });
    const pre = h('pre', { class: 'log' });
    let text = '';
    function show() {
      const f = flt.value.toLowerCase();
      const lines = text.split('\n').filter((l) => (!f || l.toLowerCase().includes(f)) && (!errOnly.checked || /error|warn|shun|fail|crit/i.test(l)));
      pre.textContent = lines.join('\n') || '(no lines)';
      pre.scrollTop = pre.scrollHeight;
    }
    async function loadLogs() {
      try {
        const r = await api('GET', '/api/service/logs?source=' + src.value);
        text = r.ok ? r.text : 'Cannot read logs: ' + (r.error || 'unknown error');
        show();
      } catch (e) { pre.textContent = e.message; }
    }
    flt.addEventListener('input', show); errOnly.addEventListener('change', show); src.addEventListener('change', loadLogs);
    if (can('operator')) {
      view.appendChild(h('div', { class: 'card', style: 'margin-top:16px' },
        h('h2', null, 'Logs (last 500 lines)'),
        h('div', { class: 'toolbar' }, src, flt, h('label', { class: 'check' }, errOnly, 'Errors / warnings only'),
          h('button', { class: 'btn', onclick: loadLogs }, '↻ Refresh'), h('label', { class: 'check' }, follow, 'Follow')),
        pre));
      loadLogs();
      every(5000, () => { if (follow.checked) loadLogs(); });
    }
  };

  PAGES.sql = (view) => {
    view.appendChild(pageHead('SQL Console', 'Runs statements directly on the ProxySQL admin interface (SQLite dialect). Every statement is audited.'));
    const snippets = [
      ['Backends (runtime)', 'SELECT * FROM runtime_mysql_servers;'],
      ['Connection pool', 'SELECT * FROM stats_mysql_connection_pool;'],
      ['Top digests', 'SELECT hostgroup, count_star, sum_time, digest_text FROM stats_mysql_query_digest ORDER BY sum_time DESC LIMIT 20;'],
      ['Tables', 'SHOW TABLES;'],
      ['Monitor tables', 'SHOW TABLES FROM monitor;'],
      ['Checksums', 'SELECT * FROM runtime_checksums_values;'],
    ];
    const ta = h('textarea', { rows: 6, spellcheck: 'false', 'aria-label': 'SQL statement' });
    ta.value = store.get('sqlLast', 'SELECT * FROM runtime_mysql_servers;');
    const out = h('div', { style: 'margin-top:12px' });
    const run = h('button', { class: 'btn primary' }, '▶ Run (Ctrl+Enter)');
    const snip = h('select', { 'aria-label': 'Snippets' }, h('option', { value: '' }, 'Snippets…'), snippets.map((s, i) => h('option', { value: i }, s[0])));
    snip.addEventListener('change', () => { if (snip.value !== '') ta.value = snippets[snip.value][1]; snip.value = ''; ta.focus(); });
    const hist = store.get('sqlHistory', []);
    const histSel = h('select', { 'aria-label': 'History' }, h('option', { value: '' }, 'History…'), hist.map((q, i) => h('option', { value: i }, q.slice(0, 80))));
    histSel.addEventListener('change', () => { if (histSel.value !== '') ta.value = hist[histSel.value]; histSel.value = ''; });
    async function exec() {
      const sql = ta.value.trim();
      if (!sql) return;
      store.set('sqlLast', sql);
      const i = hist.indexOf(sql); if (i >= 0) hist.splice(i, 1); hist.unshift(sql); hist.splice(30); store.set('sqlHistory', hist);
      run.disabled = true;
      const t0 = performance.now();
      try {
        const r = await api('POST', IU('/sql'), { sql });
        const ms = Math.round(performance.now() - t0);
        clear(out);
        if (r.columns.length) {
          const rows = r.rows.map((arr) => { const o = {}; r.columns.forEach((c, j) => { o['c' + j] = arr[j]; }); return o; });
          const cols = r.columns.map((c, j) => ({ key: 'c' + j, label: c }));
          numericGuess(cols, rows);
          const dt = DataTable({ columns: cols, rows });
          out.appendChild(h('div', { class: 'toolbar' }, dt.searchInput(), h('span', { class: 'muted small' }, r.rows.length + ' rows · ' + ms + ' ms' + (r.truncated ? ' · truncated to 5000' : ''))));
          out.appendChild(dt.el);
        } else out.appendChild(h('div', { class: 'notice' }, 'OK · ' + (r.affected === null || r.affected < 0 ? '' : r.affected + ' rows affected · ') + ms + ' ms'));
        refreshPending();
      } catch (e) { clear(out).appendChild(h('div', { class: 'notice warn' }, e.message)); }
      run.disabled = false;
    }
    run.addEventListener('click', exec);
    ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); exec(); } });
    view.appendChild(h('div', { class: 'card' }, ta, h('div', { class: 'toolbar', style: 'margin:10px 0 0' }, run, snip, histSel)));
    view.appendChild(out);
  };

  PAGES['webui-users'] = async (view) => {
    view.appendChild(pageHead('Web UI Users', 'Accounts that can sign in to this web UI. viewer = read-only · operator = edit config, load/save, read logs · admin = everything incl. service control, SQL console and accounts.',
      h('button', { class: 'btn primary', onclick: () => edit(null) }, '+ Add user')));
    let roles = ROLES;
    const dt = DataTable({
      columns: [{ key: 'username', label: 'Username' }, { key: 'role', label: 'Role' },
        { key: 'active', label: 'Status', fmt: (v) => (v ? h('span', { class: 'pill good' }, '● active') : h('span', { class: 'pill' }, '○ disabled')) },
        { key: 'last_login', label: 'Last login', fmt: (v) => (v ? fmtEpoch(v) : 'never') }, { key: 'created_at', label: 'Created', fmt: fmtEpoch }],
      rows: [],
      actions: (r) => [h('button', { class: 'btn small', onclick: () => edit(r) }, 'Edit'), ' ',
        r.username !== S.me.username ? h('button', { class: 'btn small danger', onclick: () => del(r) }, 'Delete') : null],
    });
    view.appendChild(dt.el);
    async function load() { try { const r = await api('GET', '/api/webui-users'); roles = r.roles; dt.setRows(r.rows); } catch (e) { fail(e); } }
    async function del(r) {
      if (!await confirmDlg('Delete user', 'Delete web UI account ' + r.username + '?', 'Delete', true)) return;
      try { await api('DELETE', '/api/webui-users/' + r.id); toast('Deleted', 'ok'); load(); } catch (e) { fail(e); }
    }
    function edit(r) {
      const name = h('input', { value: r ? r.username : '', disabled: !!r, autocomplete: 'off' });
      const role = h('select', null, roles.map((x) => h('option', { value: x }, x)));
      role.value = r ? r.role : 'viewer';
      const active = h('input', { type: 'checkbox', checked: r ? !!r.active : true });
      const pw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: r ? '(unchanged)' : 'min. 10 characters' });
      const err = h('div', { class: 'form-error' });
      const save = h('button', { class: 'btn primary' }, 'Save');
      const m = modal(r ? 'Edit ' + r.username : 'New web UI user', h('div', { class: 'form-grid' },
        h('label', null, 'Username', name), h('label', null, 'Role', role),
        h('label', null, r ? 'Reset password' : 'Password', pw), h('label', { class: 'check' }, active, 'Active'), h('div', { class: 'full' }, err)),
      [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save]);
      save.addEventListener('click', async () => {
        try {
          if (r) await api('PUT', '/api/webui-users/' + r.id, { role: role.value, active: active.checked, password: pw.value || undefined });
          else await api('POST', '/api/webui-users', { username: name.value, role: role.value, password: pw.value });
          m.close(); toast('Saved', 'ok'); load();
        } catch (e) { err.textContent = e.message; }
      });
    }
    load();
  };

  PAGES.audit = async (view) => {
    view.appendChild(pageHead('Audit Log', 'Every login and change made through this web UI.'));
    resultView(view, '/api/audit?limit=2000', {
      columns: () => [
        { key: 'ts', label: 'Time', fmt: fmtEpoch }, { key: 'username', label: 'User' }, { key: 'ip', label: 'IP' },
        { key: 'instance', label: 'Instance' }, { key: 'action', label: 'Action' },
        { key: 'success', label: 'Result', fmt: (v) => (v ? h('span', { class: 'pill good' }, '✓ ok') : h('span', { class: 'pill bad' }, '✕ failed')) },
        { key: 'detail', label: 'Detail', wrap: true }],
      interval: 15000,
    });
  };

  PAGES.account = (view) => {
    view.appendChild(pageHead('My Account', 'Signed in as ' + S.me.username + ' (' + S.me.role + ')'));
    const cur = h('input', { type: 'password', autocomplete: 'current-password' });
    const n1 = h('input', { type: 'password', autocomplete: 'new-password' });
    const n2 = h('input', { type: 'password', autocomplete: 'new-password' });
    const err = h('div', { class: 'form-error' });
    const btn = h('button', { class: 'btn primary' }, 'Change password');
    btn.addEventListener('click', async () => {
      err.textContent = '';
      if (n1.value !== n2.value) { err.textContent = 'New passwords do not match'; return; }
      try { await api('POST', '/api/me/password', { current: cur.value, new: n1.value }); toast('Password changed', 'ok'); cur.value = n1.value = n2.value = ''; } catch (e) { err.textContent = e.message; }
    });
    view.appendChild(h('div', { class: 'card', style: 'max-width:420px' }, h('h2', null, 'Change password'),
      h('div', { class: 'grid' }, h('label', null, 'Current password', cur), h('label', null, 'New password (min. 10 characters)', n1), h('label', null, 'Repeat new password', n2), err, h('div', null, btn))));
  };

  // ------------------------------------------------------------------ boot / login
  function showLogin() {
    S.me = null;
    S.pageTimers.forEach(clearInterval);
    $('#app').classList.add('hidden');
    $('#login').classList.remove('hidden');
    $('#modal-root').textContent = '';
  }

  let globalTimers = [];
  async function startApp() {
    const r = await api('GET', '/api/instances');
    S.instances = r.instances; S.serviceEnabled = r.service_enabled; S.serviceUnit = r.service_unit;
    const sel = clear($('#instance-select'));
    S.instances.forEach((i) => sel.appendChild(h('option', { value: i.key }, i.label + ' (' + i.host + ':' + i.port + ')')));
    const saved = store.get('instance', null);
    S.inst = S.instances.some((i) => i.key === saved) ? saved : S.instances[0].key;
    sel.value = S.inst;
    sel.classList.toggle('hidden', S.instances.length < 2);
    $('#user-name').textContent = S.me.username + ' · ' + S.me.role;
    $('#login').classList.add('hidden');
    $('#app').classList.remove('hidden');
    globalTimers.forEach(clearInterval);
    globalTimers = [setInterval(refreshPending, 30000), setInterval(checkInstance, 20000)];
    checkInstance(); refreshPending();
    route();
  }

  function initTheme() {
    const t = store.get('theme', null);
    if (t) document.documentElement.setAttribute('data-theme', t);
    $('#theme-toggle').addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      store.set('theme', next);
      if (S.route === 'dashboard') route();
    });
  }

  document.addEventListener('DOMContentLoaded', async () => {
    initTheme();
    $('#login-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      $('#login-error').textContent = '';
      try {
        S.me = await api('POST', '/api/login', { username: f.username.value, password: f.password.value });
        f.password.value = '';
        await startApp();
      } catch (err) { $('#login-error').textContent = err.message; }
    });
    $('#logout-btn').addEventListener('click', async () => { try { await api('POST', '/api/logout'); } catch (e) { /* ignore */ } showLogin(); });
    $('#instance-select').addEventListener('change', (e) => { S.inst = e.target.value; store.set('instance', S.inst); checkInstance(); refreshPending(); route(); });
    $('#menu-toggle').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
    window.addEventListener('hashchange', () => { if (S.me) route(); });
    try {
      const res = await fetch('/api/me', { credentials: 'same-origin' });
      if (res.ok) { S.me = await res.json(); await startApp(); } else showLogin();
    } catch (e) { showLogin(); }
  });
})();
