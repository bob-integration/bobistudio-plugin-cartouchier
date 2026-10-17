// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Cartouchier — control console. The orchestrator calls window.MXLPlugins.cartouchier.mount(el, vmid, ctx).
//
// Rules this file follows, each for a failure already seen elsewhere:
//   1. ONE function builds every URL (`api`), so the console works behind a public token (ctx.base);
//      in public mode it is READ-ONLY and says so — a POST through the relay would fail in 405;
//   2. the poll stops on unmount;
//   3. iPad is the reference: every gesture has a TAP equivalent on a 44 px target. Assigning a sound
//      (0.1.2) works three ways — HTML5 drag from the media panel (mouse), a POINTER drag (finger: long
//      press on the media, then slide onto a key), and TAP-TAP (tap the media, it stays highlighted;
//      tap the key). Moving keys and playlist items is done with buttons (← → ↑ ↓). Desktop files may
//      also be DROPPED on a key (quick deposit), also reachable by the editor's "Send a file" button;
//   4. colours are theme tokens only (var(--…)), never a literal;
//   5. every label goes through T(key, fallback), keys in i18n/{fr,en}.json, prefix plugin.cartouchier.
//      An error from the container is a CODE, translated here — never a sentence from JSON.
//
// The countdown is INTERPOLATED between two polls (requestAnimationFrame): the poll gives the
// truth every 250 ms, the frame clock makes the tenths move smoothly. A paused or idle player is
// never interpolated.
//
// INHERITED BEHAVIOUR (0.1.2). A key's stop fade, second press, loop, exclusive group and assigned
// player are `null` when the key inherits them — from its page's defaults, else from the cart's
// (engine.py: resolve). The editor shows "default (value)" or the key's own value, with a way back
// to the default; `effectiveOf` below is the same resolution as the engine's, checked against it by
// tests/verif_plugin_cartouchier.py.
(function () {
  'use strict';
  const P = 'plugin.cartouchier.';
  const T = (k, d) => {
    const key = P + k;
    const v = (typeof window !== 'undefined' && window.t) ? window.t(key)
      : ((typeof window !== 'undefined' && window.I18N) || {})[key];
    return (v && v !== key) ? v : d;
  };
  const TF = (k, d) => {             // full key (media library component)
    const v = window.t ? window.t(k) : ((window.I18N || {})[k]);
    return (v && v !== k) ? v : d;
  };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const COLORS = ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple', 'grey'];
  const SECOND = ['stop', 'pause', 'restart'];
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── pure helpers (also driven by the offline test, through window.MXLPlugins.cartouchier._pure) ──
  const DEFAULTS0 = {fade_out_ms: 0, second_press: 'stop', loop: false, group: '', player: 0};
  const KEY_F = ['fade_out_ms', 'second_press', 'loop', 'group', 'player'];
  const ITEM_F = ['fade_out_ms'];
  // Same resolution as engine.resolve: the item's own value, else the page's, else the cart's.
  function effectiveOf(item, pageDef, cartDef, fields) {
    const out = {};
    (fields || KEY_F).forEach(f => {
      let v = item ? item[f] : null;
      if (v == null) v = (pageDef || {})[f];
      if (v == null) v = (cartDef || {})[f];
      if (v == null) v = DEFAULTS0[f];
      out[f] = v;
    });
    return out;
  }
  // Which quick deposit an uploaded file belongs to. The upload event carries its session: the
  // file's SIZE and modification time identify the file the operator dropped. Before 0.1.2 the
  // first deposit waiting took the next upload, whatever it was — an import made meanwhile from
  // the library landed on the wrong key. An event without a session (a file already in the base,
  // filed instead of sent) is attributed only when a single deposit is waiting: unambiguous.
  function matchUpload(list, detail) {
    const s = detail && detail.session;
    if (s && s.size != null) {
      return list.findIndex(t => t.size === +s.size
        && (t.mtime == null || s.mtime == null || Math.abs(t.mtime - +s.mtime) < 2));
    }
    return (!s && list.length === 1) ? 0 : -1;
  }
  // "key:<slot>" | "pl:<index>" (data-drop of a drop target) → target.
  function parseDrop(s) {
    const m = /^(key|pl):(\d+)$/.exec(String(s || ''));
    if (!m) return null;
    return m[1] === 'key' ? {kind: 'key', slot: +m[2]} : {kind: 'pl', index: +m[2]};
  }

  let EL = null, VMID = null, BASE = null, PUBLIC = false, TOAST = () => {};
  let timer = null, raf = null;
  let S = null;                       // last /state
  let S_AT = 0;                       // performance.now() of that /state
  let DOC = null, DOC_REV = -1;
  let EDIT = false;
  let editing = null;                 // {page, slot, key} in the key editor
  let lib = null, libTarget = null, libTargetTitle = '';   // media library + what « Assign » assigns to
  let armed = null;                   // media selected for tap-tap: {asset, path, name}
  let PD = null;                      // pointer drag in progress
  let pressing = false;               // a long press may become a drag (blocks the row's context menu)
  let undoTimer = null;
  const waves = {};                   // "<player>|<key>" -> {points, duration_ms}
  const pendingDrops = [];            // quick deposits waiting for their upload to finish

  const api = p => BASE ? (BASE + '/plugin' + p) : ('/api/containers/' + VMID + '/plugin' + p);
  const $ = sel => EL && EL.querySelector(sel);
  const canDeploy = () => !!(window.hasPerm && window.hasPerm('containers.deploy'));

  async function get(p) {
    const r = await fetch(api(p), {cache: 'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function errText(d, status) {
    const code = (d && d.error) || ('http_' + status);
    return T('err_' + code, T('err_generic', 'Refusé : {c}').replace('{c}', code));
  }

  async function post(p, body) {
    if (PUBLIC) { TOAST(T('read_only', 'Lien public : lecture seule'), 'warning'); return null; }
    let r, d = {};
    try {
      r = await fetch(api(p), {method: 'POST', headers: {'Content-Type': 'application/json'},
                               body: JSON.stringify(body || {})});
      d = await r.json().catch(() => ({}));
    } catch (e) {
      TOAST(T('unreachable', 'Conteneur injoignable'), 'error'); return null;
    }
    if (!r.ok || d.ok === false) { TOAST(errText(d, r.status), 'error'); return null; }
    if (d.doc) { DOC = d.doc; renderMain(); }
    poll();
    return d;
  }

  // ── time formatting ──
  function fmtCount(ms) {
    if (ms == null) return '';
    const neg = ms < 0; ms = Math.abs(ms);
    const t = Math.floor(ms / 100), m = Math.floor(t / 600), s = Math.floor(t / 10) % 60, d = t % 10;
    return (neg ? '-' : '') + m + ':' + String(s).padStart(2, '0') + '.' + d;
  }
  function fmtDur(ms) {
    if (!(ms > 0)) return '—';
    const s = Math.round(ms / 1000);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  const baseName = p => { const s = String(p || ''); const i = s.lastIndexOf('/'); return i >= 0 ? s.slice(i + 1) : s; };
  const mediaName = n => String(n || '').replace(/__(?:[0-9a-f]{8}|[0-9a-f]{6})(?:_[a-z0-9]{1,16})?(\.[^./]{1,8})?$/, '$1');
  const curPage = () => DOC.pages.find(p => p.id === DOC.page) || DOC.pages[0];

  // ── skeleton ──
  function skeleton() {
    EL.innerHTML = `
      <div class="cart-root">
        <div class="cart-bar">
          <div class="cart-seg" role="group" aria-label="${esc(T('mode', 'Mode'))}">
            <button type="button" class="cart-btn" data-mode="grid">${esc(T('mode_grid', 'Grille'))}</button>
            <button type="button" class="cart-btn" data-mode="playlist">${esc(T('mode_playlist', 'Playlist'))}</button>
          </div>
          <div class="cart-pages" role="tablist"></div>
          <span class="cart-spacer"></span>
          <label class="cart-edit-sw"${PUBLIC ? ' hidden' : ''}><input type="checkbox" class="ctl-switch cart-edit-in"> ${esc(T('edit', 'Édition'))}</label>
          <button type="button" class="cart-btn cart-saves-btn"${PUBLIC ? ' hidden' : ''}>${esc(T('sv_title', 'Sauvegardes'))}</button>
          <button type="button" class="cart-btn cart-settings-btn"${PUBLIC ? ' hidden' : ''}>${esc(T('settings', 'Réglages'))}</button>
          <div class="cart-listen"${PUBLIC ? ' hidden' : ''}></div>
          <button type="button" class="cart-btn cart-stopall"${PUBLIC ? ' hidden' : ''}>${esc(T('stop_all', 'Tout arrêter'))}</button>
        </div>
        ${PUBLIC ? `<p class="cart-note">${esc(T('read_only', 'Lien public : lecture seule'))}</p>` : ''}
        <div class="cart-undo" role="status" aria-live="polite" hidden>
          <span class="cart-undo-txt"></span>
          <button type="button" class="cart-btn cart-undo-btn">${esc(T('undo', 'Annuler'))}</button>
        </div>
        <div class="cart-players"></div>
        <div class="cart-work">
          <div class="cart-main"></div>
          <div class="cart-panel cart-libwrap" hidden>
            <div class="cart-panel-head"><strong class="cart-lib-title"></strong>
              <span class="cart-spacer"></span>
              <button type="button" class="cart-btn cart-lib-cancel" hidden>${esc(T('lib_cancel_target', 'Annuler le choix'))}</button>
              <button type="button" class="cart-btn cart-lib-close">${esc(T('close', 'Fermer'))}</button></div>
            <p class="cart-note cart-lib-hint"></p>
            <div class="cart-armed" hidden><span class="cart-armed-txt"></span>
              <button type="button" class="cart-btn cart-armed-clear">${esc(T('unselect', 'Désélectionner'))}</button></div>
            <div class="cart-lib"></div>
          </div>
        </div>
        <div class="cart-panel cart-editor" hidden></div>
        <div class="cart-panel cart-settings" hidden></div>
        <div class="cart-panel cart-saves" hidden></div>
      </div>`;
    EL.querySelectorAll('[data-mode]').forEach(b => { b.onclick = () => post('/mode', {mode: b.dataset.mode}); });
    $('.cart-edit-in').onchange = e => { EDIT = e.target.checked; syncLib(); renderMain(); };
    $('.cart-settings-btn').onclick = () => openSettings();
    $('.cart-saves-btn').onclick = () => openSaves();
    $('.cart-stopall').onclick = () => post('/stop_all', {cut: false});
    $('.cart-lib-close').onclick = () => { libTarget = null; if (!EDIT) $('.cart-libwrap').hidden = true; paintLibHead(); };
    $('.cart-lib-cancel').onclick = () => { libTarget = null; if (!EDIT) $('.cart-libwrap').hidden = true; paintLibHead(); };
    $('.cart-armed-clear').onclick = () => disarm();
  }

  // ── undo banner (in the page, never a browser dialog) ──
  function showUndo(text, fn) {
    const box = $('.cart-undo');
    if (!box) return;
    clearTimeout(undoTimer);
    box.querySelector('.cart-undo-txt').textContent = text;
    box.hidden = false;
    box.querySelector('.cart-undo-btn').onclick = async () => {
      box.hidden = true; clearTimeout(undoTimer);
      try { await fn(); } catch (e) { TOAST(T('undo_failed', 'Annulation impossible'), 'error'); }
    };
    undoTimer = setTimeout(() => { if (EL) box.hidden = true; }, 8000);
  }

  // ── players ──
  function renderPlayers() {
    const host = $('.cart-players');
    if (!host || !S) return;
    if (host.childElementCount !== S.players.length) {
      host.innerHTML = S.players.map(p => `
        <div class="cart-player" data-player="${p.player}">
          <div class="cart-player-head">
            <span class="cart-player-n">${esc(T('player_short', 'L'))}${p.player}</span>
            <span class="cart-state"></span>
            <span class="cart-player-title"></span>
          </div>
          <div class="cart-count" aria-live="off"></div>
          <div class="cart-sub"><span class="cart-elapsed"></span>
            <span class="cart-intro"></span><span class="cart-outro"></span></div>
          <canvas class="cart-wave" height="44"></canvas>
          <div class="cart-player-acts"${PUBLIC ? ' hidden' : ''}>
            <button type="button" class="cart-btn cart-pp" data-act="play" aria-label="${esc(T('play', 'Lancer'))}">▶</button>
            <button type="button" class="cart-btn cart-pp" data-act="pause" aria-label="${esc(T('pause', 'Pause'))}">❚❚</button>
            <button type="button" class="cart-btn cart-pp" data-act="stop" aria-label="${esc(T('stop', 'Arrêter'))}">■</button>
          </div>
        </div>`).join('');
      host.querySelectorAll('.cart-player').forEach(card => {
        const n = +card.dataset.player;
        card.querySelectorAll('[data-act]').forEach(b => {
          b.onclick = () => post('/player/' + b.dataset.act, {player: n});
        });
      });
    }
    S.players.forEach(p => {
      const card = host.querySelector(`[data-player="${p.player}"]`);
      if (!card) return;
      card.dataset.state = p.state;
      card.querySelector('.cart-state').textContent = T('st_' + p.state, p.state);
      card.querySelector('.cart-player-title').textContent = p.title || (p.state === 'idle' ? '' : (p.key || ''));
      const wk = p.player + '|' + (p.key || '');
      if (p.key && !waves[wk]) {
        waves[wk] = {points: [], duration_ms: 0, loading: true};
        get('/waveform?player=' + p.player).then(w => { waves[wk] = w; }).catch(() => { delete waves[wk]; });
      }
    });
  }

  // Interpolated view of a player at frame time.
  function live(p, now) {
    const run = p.state === 'playing' || p.state === 'fading';
    const dt = run ? Math.max(0, now - S_AT) : 0;
    let el = p.elapsed_ms + dt;
    if (p.duration_ms && el > p.duration_ms) el = p.loop ? el % p.duration_ms : p.duration_ms;
    return {el, rem: p.state === 'idle' ? 0 : Math.max(0, p.duration_ms - el)};
  }

  function frame() {
    raf = null;
    if (!EL || !S) return;
    const now = performance.now();
    const alertMs = ((S.settings || {}).alert_s || 0) * 1000;
    S.players.forEach(p => {
      const card = EL.querySelector(`.cart-player[data-player="${p.player}"]`);
      if (!card) return;
      const v = live(p, now);
      const cnt = card.querySelector('.cart-count');
      cnt.textContent = p.state === 'idle' ? '—' : fmtCount(v.rem);
      cnt.classList.toggle('is-alert', (p.state === 'playing' || p.state === 'fading') && !p.loop
        && alertMs > 0 && v.rem <= alertMs);
      card.querySelector('.cart-elapsed').textContent = p.state === 'idle' ? '' :
        fmtCount(v.el).replace(/\.\d$/, '') + ' / ' + fmtDur(p.duration_ms);
      const intro = card.querySelector('.cart-intro'), outro = card.querySelector('.cart-outro');
      const ir = p.intro_ms && p.state !== 'idle' ? p.intro_ms - v.el : null;
      const or_ = p.outro_ms && p.state !== 'idle' ? p.outro_ms - v.el : null;
      intro.hidden = !(ir > 0); outro.hidden = !(or_ > 0);
      if (ir > 0) intro.textContent = T('intro', 'INTRO') + ' ' + fmtCount(ir);
      if (or_ > 0) outro.textContent = T('outro', 'FIN') + ' ' + fmtCount(or_);
      drawWave(card.querySelector('.cart-wave'), p, v.el);
    });
    // keys of the grid: remaining on the key itself
    EL.querySelectorAll('.cart-key[data-player]').forEach(b => {
      const p = S.players[+b.dataset.player - 1];
      if (!p) return;
      const v = live(p, now);
      const r = b.querySelector('.cart-key-rem');
      if (r) r.textContent = fmtCount(v.rem);
      const bar = b.querySelector('.cart-key-bar');
      if (bar && p.duration_ms) bar.style.width = (100 * v.el / p.duration_ms).toFixed(2) + '%';
      b.classList.toggle('is-alert', alertMs > 0 && !p.loop && v.rem <= alertMs
        && (p.state === 'playing' || p.state === 'fading'));
    });
    if (S.players.some(p => p.state === 'playing' || p.state === 'fading')) raf = requestAnimationFrame(frame);
  }

  function cssVar(name) {
    return getComputedStyle(EL).getPropertyValue(name).trim() || 'currentColor';
  }

  function drawWave(cv, p, el) {
    const w = waves[p.player + '|' + (p.key || '')];
    const W = cv.clientWidth || 300, H = cv.height;
    if (cv.width !== W) cv.width = W;
    const g = cv.getContext('2d');
    g.clearRect(0, 0, W, H);
    if (!w || !w.points || !w.points.length || p.state === 'idle') return;
    const pts = w.points, n = pts.length, dur = w.duration_ms || p.duration_ms || 1;
    const xh = Math.round(W * Math.min(1, el / dur));
    const played = cssVar('--text-muted'), ahead = cssVar('--accent');
    for (let x = 0; x < W; x++) {
      const v = pts[Math.min(n - 1, Math.floor(x * n / W))] / 100;
      const h = Math.max(1, v * (H - 2));
      g.fillStyle = x < xh ? played : ahead;
      g.fillRect(x, (H - h) / 2, 1, h);
    }
    const mark = (ms, color) => { if (!(ms > 0)) return; const x = Math.round(W * ms / dur);
      g.fillStyle = color; g.fillRect(x, 0, 2, H); };
    mark(p.intro_ms, cssVar('--status-warning-fg'));
    mark(p.outro_ms, cssVar('--status-error-fg'));
    g.fillStyle = cssVar('--text-strong');
    g.fillRect(xh, 0, 2, H);
  }

  // Nom d'une page sans nom propre : son numéro de CRÉATION (id « p7 » → « Page 7 »), pas sa position — sinon
  // déplacer une page la renommait d'après sa nouvelle place et le geste avait l'air d'un simple changement de page
  function pageLabel(p, i) {
    if (p && p.name) return p.name;
    const m = /(\d+)$/.exec((p && p.id) || '');
    return T('page_n', 'Page {n}').replace('{n}', m ? m[1] : i + 1);
  }
  // ── bar (mode, pages) ──
  function renderBar() {
    if (!DOC) return;
    EL.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('is-on', b.dataset.mode === DOC.mode));
    const host = $('.cart-pages');
    host.hidden = DOC.mode !== 'grid';
    host.innerHTML = DOC.pages.map((p, i) => `<button type="button" role="tab" class="cart-btn cart-page${p.id === DOC.page ? ' is-on' : ''}"
       data-page="${esc(p.id)}" aria-selected="${p.id === DOC.page}">${esc(pageLabel(p, i))}</button>`).join('');
    host.querySelectorAll('[data-page]').forEach(b => { b.onclick = () => post('/page', {page: b.dataset.page}); });
  }

  // ── drop targets (keys, playlist positions): files, media from the panel ──
  const isFiles = e => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  const isMedia = e => !!e.dataTransfer && Array.from(e.dataTransfer.types || [])
    .some(t => t === 'application/x-bobi-asset' || t === 'application/x-bobi-media');
  function assetName(aid, path) {
    const a = aid && lib && lib.assets ? (lib.assets() || []).find(x => x.id === aid) : null;
    return (a && ((a.meta || {}).title || a.name)) || mediaName(baseName(path)) || '';
  }
  // What an HTML5 drop from the media library carries: the dragged row, or the whole selection.
  function mediaFromTransfer(dt) {
    let ids = [];
    try { ids = JSON.parse(dt.getData('application/x-bobi-assets') || '[]'); } catch (e) { ids = []; }
    const aid = dt.getData('application/x-bobi-asset') || '', path = dt.getData('application/x-bobi-media') || '';
    const first = (aid || path) ? {asset: aid || null, path, name: assetName(aid, path)} : null;
    const all = ids.length > 1 ? ids.map(id => ({asset: id, path: '', name: assetName(id, '')})) : (first ? [first] : []);
    return {first, all};
  }
  function wireDrop(el, target, onFiles) {
    if (PUBLIC) return;
    el.addEventListener('dragover', e => {
      if (isMedia(e) || (onFiles && isFiles(e))) { e.preventDefault(); el.classList.add('is-drop'); }
    });
    el.addEventListener('dragleave', () => el.classList.remove('is-drop'));
    el.addEventListener('drop', e => {
      el.classList.remove('is-drop');
      if (isMedia(e)) {
        e.preventDefault();
        const m = mediaFromTransfer(e.dataTransfer);
        if (target.kind === 'pl') dropMedia(target, m.all); else if (m.first) dropMedia(target, m.first);
        return;
      }
      if (onFiles && isFiles(e)) { e.preventDefault(); onFiles(e.dataTransfer.files); }
    });
  }

  // ── grid ──
  function keyOnPlayer(id) {
    return S ? S.players.find(p => p.origin === 'grid' && p.key === id && p.state !== 'idle') : null;
  }

  function renderGrid(host) {
    const pg = curPage();
    const n = DOC.grid.cols * DOC.grid.rows;
    const bySlot = {};
    pg.keys.forEach(k => { bySlot[k.slot] = k; });
    const hidden = pg.keys.filter(k => k.slot >= n).length;
    const media = (S && S.key_media) || {};
    let html = `<div class="cart-grid" style="grid-template-columns:repeat(${DOC.grid.cols},minmax(0,1fr))">`;
    for (let s = 0; s < n; s++) {
      const k = bySlot[s];
      if (!k) {
        html += `<button type="button" class="cart-key is-empty" data-slot="${s}" data-drop="key:${s}"${EDIT ? '' : ' tabindex="-1"'}>${EDIT ? '+' : ''}</button>`;
        continue;
      }
      const m = media[k.id] || {};
      const p = keyOnPlayer(k.id);
      const e = effectiveOf(k, pg.defaults, DOC.defaults);
      const flags = [e.loop ? '⟲' : '', k.next ? '→' : '', e.group ? esc(e.group) : '',
                     e.player ? T('player_short', 'L') + e.player : ''].filter(Boolean).join(' ');
      html += `<button type="button" class="cart-key${p ? ' is-' + p.state : ''}${m.state && m.state !== 'ready' ? ' is-media-' + m.state : ''}"
          data-slot="${s}" data-drop="key:${s}" data-key="${esc(k.id)}" data-color="${esc(k.color || '')}"${p ? ` data-player="${p.player}"` : ''}>
          <span class="cart-key-title">${esc(k.title || mediaName(baseName(k.media.file)) || T('untitled', 'Sans titre'))}</span>
          ${EDIT && !PUBLIC ? `<span class="cart-key-pfl" data-pfl="${esc(k.id)}" role="button" aria-label="${esc(T('pfl_key', 'Pré-écouter'))}">🎧</span>` : ''}
          <span class="cart-key-meta"><span>${esc(fmtDur(m.duration_ms))}</span><span>${flags}</span></span>
          ${p ? `<span class="cart-key-rem"></span><span class="cart-key-bar"></span>` : ''}
          ${m.state && m.state !== 'ready' ? `<span class="cart-key-media">${esc(T('media_' + m.state, m.state))}</span>` : ''}
        </button>`;
    }
    html += '</div>';
    if (hidden) html += `<p class="cart-note">${esc(T('hidden_keys', '{n} touche(s) hors de la grille actuelle : agrandissez la grille pour les voir.').replace('{n}', hidden))}</p>`;
    host.innerHTML = html;
    host.querySelectorAll('.cart-key').forEach(b => {
      const s = +b.dataset.slot;
      b.onclick = ev => {
        if (EDIT && armed) { dropMedia({kind: 'key', page: pg.id, slot: s}, armed); return; }   // tap-tap
        if (EDIT && pflTap(b, ev)) return;                                                          // 🎧 on the key: pre-listen
        if (EDIT) { openEditor(pg.id, s, bySlot[s] || null); return; }
        if (b.dataset.key) post('/fire', {key: b.dataset.key});
      };
      // Quick deposit: a desktop FILE dropped on a key (or an empty slot) — the editor's "Send a
      // file" button does the same; and a media dragged from the panel (mouse).
      wireDrop(b, {kind: 'key', page: pg.id, slot: s},
        files => deposit(files, {kind: 'key', page: pg.id, slot: s}));
    });
  }

  // ── quick deposit (upload to this container's node + project, into the media base) ──
  async function uploader() {
    if (window.MediaUpload) return window.MediaUpload;
    await new Promise((res, rej) => {
      const sc = document.createElement('script');
      sc.src = '/static/js/media_upload.js'; sc.onload = res; sc.onerror = () => rej(new Error('media_upload.js'));
      document.head.appendChild(sc);
    });
    return window.MediaUpload;
  }
  function purgeDrops() {
    const old = Date.now() - 3600 * 1000;           // an upload that never ends must not wait forever
    for (let i = pendingDrops.length - 1; i >= 0; i--) if (pendingDrops[i].at < old) pendingDrops.splice(i, 1);
  }
  async function deposit(files, target) {
    const f = Array.from(files || [])[0];
    if (!f) return;
    let up;
    try { up = await uploader(); } catch (e) { TOAST(e.message, 'error'); return; }
    purgeDrops();
    pendingDrops.push(Object.assign({name: f.name.replace(/\.[^.]+$/, ''), size: f.size,
                                     mtime: f.lastModified / 1000, at: Date.now()}, target));
    TOAST(T('upload_started', 'Envoi de « {n} »… la touche sera affectée à la fin.').replace('{n}', f.name), 'info');
    up.enqueue([f], {vmid: VMID}, {});
  }
  function onUploaded(e) {
    if (!EL) return;
    const aid = e.detail && e.detail.asset_id;
    if (!aid) return;
    purgeDrops();
    const i = matchUpload(pendingDrops, e.detail);
    if (i < 0) return;
    const t = pendingDrops.splice(i, 1)[0];
    dropMedia({kind: 'key', page: t.page, slot: t.slot}, {asset: aid, path: '', name: t.name, local: true});
  }

  // ── direct assignment (drag, tap-tap, deposit): immediate, persisted, undoable ──
  async function dropMedia(t, m) {
    if (PUBLIC || !DOC) return;
    const list = (Array.isArray(m) ? m : [m]).filter(x => x && (x.asset || x.path));
    if (!list.length) return;
    disarm();
    if (t.kind === 'key') {
      const x = list[0], pageId = t.page || curPage().id;
      const d = await post('/assign', {page: pageId, slot: t.slot, media: {asset: x.asset || null, file: x.path || ''},
                                       title: x.name || ''});
      if (!d) return;
      const doc = d.doc || {}, kid = doc.key_id, prev = doc.previous;
      showUndo((prev ? T('undo_replaced', 'Son de « {k} » remplacé par « {n} ».') : T('undo_assigned', '« {n} » affecté à une touche.'))
        .replace('{k}', (prev && (prev.title || mediaName(baseName(prev.media.file)))) || '').replace('{n}', x.name || ''),
        () => prev ? post('/key', {page: pageId, slot: prev.slot, key: prev}) : post('/key', {id: kid, delete: true}));
      if (editing && editing.page === pageId && editing.slot === t.slot) {
        const pg = DOC.pages.find(p => p.id === pageId);
        openEditor(pageId, t.slot, pg ? pg.keys.find(k => k.id === kid) : null);
      }
    } else {
      const items = list.map(x => ({media: {asset: x.asset || null, file: x.path || ''}, title: x.name || ''}));
      const d = await post('/playlist', {op: 'add', at: t.index, items});
      if (!d) return;
      const ids = (d.doc && d.doc.added) || [];
      showUndo(T('undo_inserted', '{n} élément(s) ajouté(s) à la playlist.').replace('{n}', ids.length),
        async () => { for (const id of ids) await post('/playlist', {op: 'remove', id}); });
    }
    // A media of the base that is not on this node yet: fetched now, the key says "not on this
    // node" until the copy is verified, then the cart rescans and decodes it.
    list.forEach(x => { if (x.asset && !x.path && !x.local) fetchToNode(x.asset, x.name); });
  }

  async function fetchToNode(aid, name) {
    let j = {};
    try {
      const r = await fetch('/api/media/fetch', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({vmid: VMID, asset_id: aid})});
      j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
      for (let i = 0; j.state !== 'done' && j.state !== 'failed' && i < 1800 && EL; i++) {
        await sleep(1000);
        const rr = await fetch('/api/media/fetch/' + encodeURIComponent(j.job), {cache: 'no-store'});
        if (rr.status === 404) throw new Error('job');
        if (rr.ok) j = Object.assign({job: j.job}, await rr.json().catch(() => ({})));
      }
      if (j.state === 'failed') throw new Error(j.error || 'failed');
      if (j.state === 'done' && EL) post('/media/rescan', {});
    } catch (e) {
      if (EL) TOAST(T('fetch_failed', 'Copie de « {n} » sur le nœud impossible : {e}').replace('{n}', name || aid)
        .replace('{e}', e.message), 'warning');
    }
  }

  // ── media panel (shared MediaLibrary component, docked in edit mode) ──
  function ensureLib() {
    if (lib) return true;
    const host = $('.cart-lib');
    if (!window.MediaLibrary) { host.textContent = T('lib_missing', 'Bibliothèque indisponible'); return false; }
    lib = window.MediaLibrary.mount(host, {
      vmid: VMID, T: TF, toast: TOAST, kinds: ['audio', 'video'], view: 'table',
      actions: [{id: 'assign', label: T('lib_assign', 'Affecter'), primary: true},
                {id: 'add', label: T('lib_add', '+ Playlist'), multi: true}],
      onAction: (id, m) => {
        const a = m.asset || null;
        const media = {asset: a ? a.id : null, file: m.path || ''};
        const title = (a && ((a.meta || {}).title || a.name)) || mediaName(baseName(m.path));
        if (id === 'add') { assignMedia({playlist: true}, media, title); return; }
        if (libTarget) {
          const t = libTarget;
          libTarget = null; paintLibHead();
          if (!EDIT) $('.cart-libwrap').hidden = true;
          assignMedia(t, media, title);
          return;
        }
        // No target (docked panel): « Assign » selects the media for tap-tap.
        armed = {asset: media.asset, path: media.file, name: title};
        paintArmed();
      },
    });
    // After the library has handled the tap (its own selection, highlighted): one selected media = armed.
    host.addEventListener('click', e => {
      if (!EDIT || e.target.closest('button,input,select,a,video,audio')) return;
      setTimeout(syncArmed, 0);
    });
    host.addEventListener('pointerdown', onLibPointerDown);
    host.addEventListener('touchmove', e => { if (PD) e.preventDefault(); }, {passive: false});
    // A long press is the start of a drag here, not the row's context menu (the ⋯ button keeps it).
    host.addEventListener('contextmenu', e => { if (pressing || PD) { e.preventDefault(); e.stopPropagation(); } }, true);
    return true;
  }
  function syncLib() {
    const wrap = $('.cart-libwrap'), work = $('.cart-work');
    if (!wrap) return;
    const on = EDIT && !PUBLIC;
    work.classList.toggle('is-edit', on);
    if (on) { wrap.hidden = false; ensureLib(); } else { wrap.hidden = true; libTarget = null; armed = null; }
    paintLibHead(); paintArmed();
  }
  function paintLibHead() {
    if (!EL) return;
    $('.cart-lib-title').textContent = libTarget ? libTargetTitle : T('lib_dock_title', 'Médias');
    $('.cart-lib-hint').textContent = libTarget ? '' : T('lib_dock_hint',
      'Glissez un son sur une touche ou dans la playlist — ou touchez-le, puis touchez la touche.');
    $('.cart-lib-hint').hidden = !!libTarget;
    $('.cart-lib-cancel').hidden = !(libTarget && EDIT);
    $('.cart-lib-close').hidden = EDIT;
  }
  function openLibrary(target, titleText) {
    libTarget = target; libTargetTitle = titleText;
    const wrap = $('.cart-libwrap');
    wrap.hidden = false;
    ensureLib(); paintLibHead();
    if (wrap.scrollIntoView) wrap.scrollIntoView({block: 'nearest'});
  }
  function syncArmed() {
    if (!lib || !lib.selected) return;
    const sel = lib.selected() || [];
    const r = sel.length === 1 ? sel[0] : null;
    armed = r && !r.ext && (r.asset || r.path) ? {asset: r.asset ? r.asset.id : null, path: r.path || '',
      name: (r.asset && ((r.asset.meta || {}).title || r.asset.name)) || mediaName(baseName(r.path)) || r.name || ''} : null;
    paintArmed();
  }
  function disarm() {
    if (armed && lib && lib.clearSelection) { try { lib.clearSelection(); } catch (e) { /* gone */ } }
    armed = null; paintArmed();
  }
  function paintArmed() {
    if (!EL) return;
    const box = $('.cart-armed');
    box.hidden = !armed;
    if (armed) box.querySelector('.cart-armed-txt').textContent =
      T('armed', '« {n} » sélectionné : touchez une touche, ou un emplacement de la playlist.').replace('{n}', armed.name || '');
    EL.classList.toggle('cart-is-armed', !!armed);
  }

  // Finger / pen: long press on a media of the panel, slide, release over a key or a playlist
  // position. The mouse keeps the library's own HTML5 drag (wireDrop receives it).
  function onLibPointerDown(ev) {
    if (!EDIT || PUBLIC || ev.pointerType === 'mouse' || ev.button !== 0 || PD) return;
    const row = ev.target.closest('[data-k]');
    if (!row || ev.target.closest('button,input,select,a')) return;
    const aid = row.dataset.aid || '', path = row.dataset.path || '';
    if (!aid && !path) return;
    pointerLift(row, ev, {asset: aid || null, path, name: assetName(aid, path)});
  }
  function pointerLift(el, ev, m) {
    const x0 = ev.clientX, y0 = ev.clientY;
    let last = ev;
    pressing = true;
    const track = e => {
      PD.ghost.style.left = (e.clientX + 14) + 'px'; PD.ghost.style.top = (e.clientY + 10) + 'px';
      const hit = document.elementFromPoint(e.clientX, e.clientY);
      let t = hit && hit.closest ? hit.closest('[data-drop]') : null;
      if (t && !EL.contains(t)) t = null;
      if (PD.over && PD.over !== t) PD.over.classList.remove('is-drop');
      PD.over = t;
      if (t) t.classList.add('is-drop');
    };
    const start = () => {
      tm = null;
      const g = document.createElement('div');
      g.className = 'cart-ghost'; g.textContent = m.name || '…';
      document.body.appendChild(g);
      PD = {m, ghost: g, over: null};
      el.classList.add('cart-lift');
      track(last);
    };
    let tm = setTimeout(start, 350);
    const move = e => {
      last = e;
      if (!PD) {
        if (tm && Math.hypot(e.clientX - x0, e.clientY - y0) > 10) finish(true);   // a scroll, not a drag
        return;
      }
      e.preventDefault(); track(e);
    };
    const finish = cancel => {
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
      window.removeEventListener('pointercancel', cc);
      if (tm) { clearTimeout(tm); tm = null; }
      setTimeout(() => { pressing = false; }, 50);
      if (!PD) return;
      const d = PD; PD = null;
      d.ghost.remove(); el.classList.remove('cart-lift');
      if (d.over) d.over.classList.remove('is-drop');
      // the click that ends a drag must not also select the row
      const swallow = e => { e.stopPropagation(); e.preventDefault(); };
      el.addEventListener('click', swallow, {capture: true, once: true});
      setTimeout(() => el.removeEventListener('click', swallow, {capture: true}), 400);
      const t = !cancel && d.over ? parseDrop(d.over.dataset.drop) : null;
      if (t) dropMedia(t.kind === 'key' ? Object.assign(t, {page: curPage().id}) : t, d.m);
    };
    const up = () => finish(false), cc = () => finish(true);
    window.addEventListener('pointermove', move, {passive: false});
    window.addEventListener('pointerup', up);
    window.addEventListener('pointercancel', cc);
  }

  // Older path, kept: the key editor's « Media base… » → « Assign » (target chosen first).
  function assignMedia(t, media, title) {
    if (t.clip) { t.clip(media, title); return; }        // clip editor: a segment appended
    if (t.item) {
      post('/playlist', {op: 'update', item: {id: t.item.id, media, title: t.item.title || title}});
      return;
    }
    if (t.playlist) { post('/playlist', {op: 'add', items: [{media, title}]}); return; }
    const key = Object.assign({}, t.key || {}, {media});
    if (!key.title) key.title = title;
    post('/key', {page: t.page, slot: t.slot, key}).then(d => {
      if (d && editing && editing.slot === t.slot && editing.page === t.page) {
        const id = (d.doc && d.doc.key_id) || key.id;
        const pg = DOC.pages.find(p => p.id === t.page);
        openEditor(t.page, t.slot, pg ? pg.keys.find(k => k.id === id) : null);
      }
    });
  }

  // ── inheritable fields (key editor, item editor, page defaults) ──
  function field(label, html) {
    return `<label class="cart-field"><span>${esc(label)}</span>${html}</label>`;
  }
  function fmtVal(f, v) {
    if (f === 'fade_out_ms') return (+v || 0) + ' ms';
    if (f === 'second_press') return T('sp_' + v, v);
    if (f === 'loop') return v ? T('yes', 'oui') : T('no', 'non');
    if (f === 'group') return v || T('group_none', 'aucun');
    if (f === 'player') return v ? T('player_n', 'Lecteur {n}').replace('{n}', v) : T('player_any', 'Lecteur libre');
    return String(v);
  }
  function inputFor(f, v, nPl) {
    if (f === 'fade_out_ms') return `<input type="number" data-f="fade_out_ms" step="100" min="0" max="30000" value="${+v || 0}">`;
    if (f === 'second_press') return `<select data-f="second_press">${SECOND.map(x =>
      `<option value="${x}"${v === x ? ' selected' : ''}>${esc(T('sp_' + x, x))}</option>`).join('')}</select>`;
    if (f === 'loop') return `<input type="checkbox" class="ctl-switch" data-f="loop"${v ? ' checked' : ''}>`;
    if (f === 'group') return `<input type="text" data-f="group" maxlength="32" value="${esc(v || '')}" placeholder="${esc(T('f_group_ph', 'vide = aucun'))}">`;
    return `<select data-f="player"><option value="0">${esc(T('player_any', 'Lecteur libre'))}</option>${
      Array.from({length: nPl}, (_, i) => `<option value="${i + 1}"${+v === i + 1 ? ' selected' : ''}>${esc(T('player_n', 'Lecteur {n}').replace('{n}', i + 1))}</option>`).join('')}</select>`;
  }
  function readInput(el) {
    const f = el.dataset.f;
    if (el.type === 'checkbox') return el.checked;
    if (f === 'fade_out_ms' || f === 'player') return +el.value || 0;
    return el.value;
  }
  const LABELS = {fade_out_ms: ['f_fade', 'Fondu d\'arrêt (ms)'], second_press: ['f_second', 'Deuxième appui'],
                  loop: ['f_loop', 'Boucle'], group: ['f_group', 'Groupe d\'exclusivité'], player: ['f_player', 'Lecteur affecté']};
  const lbl = f => T(LABELS[f][0], LABELS[f][1]);
  // own: the value set at this level (null = inherited); inh: what it inherits; from: label with {v}.
  function inhField(f, own, inh, from, nPl) {
    const isOwn = own != null;
    return `<div class="cart-field cart-inh" data-inh="${f}" data-own="${isOwn ? 1 : 0}">
      <span>${esc(lbl(f))}</span>
      <div class="cart-inh-own"${isOwn ? '' : ' hidden'}>${inputFor(f, isOwn ? own : inh, nPl)}
        <button type="button" class="cart-btn cart-inh-reset" title="${esc(from.replace('{v}', fmtVal(f, inh)))}">${esc(T('back_default', 'Revenir au défaut'))}</button></div>
      <div class="cart-inh-def"${isOwn ? ' hidden' : ''}><span class="cart-inh-val">${esc(from.replace('{v}', fmtVal(f, inh)))}</span>
        <button type="button" class="cart-btn cart-inh-set">${esc(T('customize', 'Personnaliser'))}</button></div>
    </div>`;
  }
  function wireInh(box) {
    box.querySelectorAll('.cart-inh').forEach(w => {
      const set = own => {
        w.dataset.own = own ? '1' : '0';
        w.querySelector('.cart-inh-own').hidden = !own;
        w.querySelector('.cart-inh-def').hidden = own;
      };
      w.querySelector('.cart-inh-set').onclick = () => set(true);
      w.querySelector('.cart-inh-reset').onclick = () => set(false);
    });
  }
  function readInh(box, o) {
    box.querySelectorAll('.cart-inh').forEach(w => {
      o[w.dataset.inh] = w.dataset.own === '1' ? readInput(w.querySelector('[data-f]')) : null;
    });
    return o;
  }

  // ── key editor ──
  function openEditor(pageId, slot, key) {
    editing = {page: pageId, slot, key};
    const k = key || {title: '', media: {asset: null, file: ''}, gain_db: 0, next: '', intro_ms: 0, outro_ms: 0, color: ''};
    const pg = DOC.pages.find(p => p.id === pageId) || curPage();
    const media = key && S && S.key_media ? (S.key_media[key.id] || {}) : {};
    const keys = DOC.pages.flatMap((p, i) => p.keys.filter(x => !key || x.id !== key.id).map(x =>
      ({id: x.id, label: pageLabel(p, i) + ' · ' + (x.title || x.id)})));
    const nPl = (S && S.n_players) || 2;
    const inh = effectiveOf({}, pg.defaults, DOC.defaults);
    const from = f => ((pg.defaults || {})[f] != null ? T('inh_page', 'Défaut de la page ({v})') : T('inh_cart', 'Défaut du cartouchier ({v})'));
    const box = $('.cart-editor');
    box.hidden = false;
    box.innerHTML = `
      <div class="cart-panel-head"><strong>${esc(key ? T('edit_key', 'Touche') : T('new_key', 'Nouvelle touche'))}</strong>
        <button type="button" class="cart-btn cart-ed-close">${esc(T('close', 'Fermer'))}</button></div>
      <div class="cart-form">
        ${field(T('f_title', 'Titre'), `<input type="text" name="title" maxlength="80" value="${esc(k.title)}">`)}
        <div class="cart-field"><span>${esc(T('f_media', 'Média'))}</span>
          <div class="cart-media-line"><span class="cart-media-name">${esc(k.media.file ? mediaName(baseName(k.media.file)) : (k.media.asset || T('no_media', 'aucun')))}</span>
          ${media.state ? `<span class="cart-media-st">${esc(T('media_' + media.state, media.state))}</span>` : ''}</div>
          <div class="cart-media-acts">
            <button type="button" class="cart-btn cart-ed-lib">${esc(T('choose_media', 'Base des médias…'))}</button>
            <button type="button" class="cart-btn cart-ed-file">${esc(T('send_file', 'Envoyer un fichier…'))}</button>
            ${key ? `<button type="button" class="cart-btn cart-ed-pfl">🎧 ${esc(T('pfl_key', 'Pré-écouter'))}</button>
            <button type="button" class="cart-btn cart-ed-clip">✂ ${esc(T('clip_edit', 'Éditer le clip…'))}</button>` : ''}
            <input type="file" class="cart-ed-filein" accept="audio/*,video/*" hidden>
          </div>
          ${media.lufs != null ? `<small>${esc(T('measured', 'Mesuré {l} LUFS, crête {p} dBFS → normalisation {g} dB')
            .replace('{l}', media.lufs).replace('{p}', media.peak_dbfs).replace('{g}', media.norm_db))}${media.norm_limited ? ' — ' + esc(T('limited_' + media.norm_limited, media.norm_limited)) : ''}</small>` : ''}
        </div>
        ${field(T('f_gain', 'Gain de la touche (dB)'), `<input type="number" name="gain_db" step="0.5" min="-40" max="20" value="${k.gain_db}">`)}
        ${inhField('fade_out_ms', k.fade_out_ms, inh.fade_out_ms, from('fade_out_ms'), nPl)}
        ${inhField('second_press', k.second_press, inh.second_press, from('second_press'), nPl)}
        ${inhField('group', k.group, inh.group, from('group'), nPl)}
        ${field(T('f_next', 'Enchaîner sur'), `<select name="next"><option value="">—</option>${keys.map(x =>
          `<option value="${esc(x.id)}"${k.next === x.id ? ' selected' : ''}>${esc(x.label)}</option>`).join('')}</select>`)}
        ${inhField('player', k.player, inh.player, from('player'), nPl)}
        ${field(T('f_intro', 'Fin de l\'intro (ms)'), `<input type="number" name="intro_ms" step="100" min="0" value="${k.intro_ms}">`)}
        ${field(T('f_outro', 'Début de la fin (ms)'), `<input type="number" name="outro_ms" step="100" min="0" value="${k.outro_ms}">`)}
        ${field(T('f_color', 'Couleur'), `<select name="color">${COLORS.map(c =>
          `<option value="${c}"${(k.color || '') === c ? ' selected' : ''}>${esc(T('color_' + (c || 'none'), c || '—'))}</option>`).join('')}</select>`)}
        ${inhField('loop', k.loop, inh.loop, from('loop'), nPl)}
      </div>
      <div class="cart-form-acts">
        ${key ? `<button type="button" class="cart-btn cart-ed-left" aria-label="${esc(T('move_left', 'Déplacer à gauche'))}">←</button>
        <button type="button" class="cart-btn cart-ed-right" aria-label="${esc(T('move_right', 'Déplacer à droite'))}">→</button>
        <button type="button" class="cart-btn cart-danger cart-ed-del">${esc(T('delete', 'Supprimer'))}</button>` : ''}
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-ed-save">${esc(T('save', 'Enregistrer'))}</button>
      </div>`;
    wireInh(box);
    const val = () => {
      const o = Object.assign({}, key || {});
      box.querySelectorAll('[name]').forEach(i => {
        if (i.type === 'checkbox') o[i.name] = i.checked;
        else if (i.type === 'number') o[i.name] = +i.value || 0;
        else o[i.name] = i.value;
      });
      readInh(box, o);                 // null = inherited (the engine keeps it so)
      o.media = (key && key.media) || {asset: null, file: ''};
      return o;
    };
    box.querySelector('.cart-ed-close').onclick = () => { box.hidden = true; editing = null; };
    box.querySelector('.cart-ed-save').onclick = async () => {
      const d = await post('/key', {page: pageId, slot, key: val()});
      if (d) { box.hidden = true; editing = null; }
    };
    box.querySelector('.cart-ed-lib').onclick = () => openLibrary({page: pageId, slot, key: val()}, T('choose_for_key', 'Choisir le son de la touche'));
    if (key) { box.querySelector('.cart-ed-pfl').onclick = () => previewPlay({key: key.id});
               box.querySelector('.cart-ed-clip').onclick = () => openClipEditor({key: key.id, page: pageId, slot}); }
    const fin = box.querySelector('.cart-ed-filein');
    box.querySelector('.cart-ed-file').onclick = () => { fin.value = ''; fin.click(); };
    fin.onchange = () => deposit(fin.files, {kind: 'key', page: pageId, slot});
    if (key) {
      box.querySelector('.cart-ed-del').onclick = async () => {
        if (box.dataset.confirm !== key.id) {
          box.dataset.confirm = key.id;
          TOAST(T('confirm_delete', 'Appuyez encore sur Supprimer pour confirmer'), 'warning');
          return;
        }
        delete box.dataset.confirm;
        const d = await post('/key', {id: key.id, delete: true});
        if (d) { box.hidden = true; editing = null; }
      };
      const move = async delta => {
        const n = DOC.grid.cols * DOC.grid.rows;
        const to = Math.max(0, Math.min(n - 1, slot + delta));
        if (to === slot) return;
        const d = await post('/key', {page: pageId, slot: to, key: {id: key.id}});
        if (d) { const p2 = DOC.pages.find(p => p.id === pageId); openEditor(pageId, to, p2.keys.find(x => x.id === key.id)); }
      };
      box.querySelector('.cart-ed-left').onclick = () => move(-1);
      box.querySelector('.cart-ed-right').onclick = () => move(+1);
    }
  }

  // ── playlist ──
  function renderPlaylist(host) {
    const pl = DOC.playlist;
    const v = (S && S.playlist) || {};
    const media = (S && S.item_media) || {};
    host.innerHTML = `
      <div class="cart-pl-bar">
        <button type="button" class="cart-btn cart-primary cart-go"${PUBLIC ? ' disabled' : ''}>${esc(T('go', 'GO'))}</button>
        <button type="button" class="cart-btn cart-pl-prev"${PUBLIC ? ' disabled' : ''} aria-label="${esc(T('pl_prev', 'Précédent'))}">◀</button>
        <button type="button" class="cart-btn cart-pl-next"${PUBLIC ? ' disabled' : ''} aria-label="${esc(T('pl_next', 'Suivant'))}">▶</button>
        <label class="cart-sw"><input type="checkbox" class="ctl-switch cart-ab"${pl.ab ? ' checked' : ''}${PUBLIC || ((S && S.n_players) || 2) < 2 ? ' disabled' : ''}> ${esc(T('ab', 'A/B'))}</label>
        <label class="cart-sw"><input type="checkbox" class="ctl-switch cart-auto"${pl.auto ? ' checked' : ''}${PUBLIC ? ' disabled' : ''}> ${esc(T('auto', 'Enchaînement auto'))}</label>
        <label class="cart-sw"><input type="checkbox" class="ctl-switch cart-plloop"${pl.loop ? ' checked' : ''}${PUBLIC ? ' disabled' : ''}> ${esc(T('pl_loop', 'Boucler la liste'))}</label>
        <span class="cart-spacer"></span>
        ${EDIT ? `<button type="button" class="cart-btn cart-pl-add">${esc(T('pl_add', 'Ajouter…'))}</button>` : ''}
      </div>
      ${pl.items.length || EDIT ? '' : `<p class="cart-note">${esc(T('pl_empty', 'Playlist vide : passez en Édition puis « Ajouter… ».'))}</p>`}
      <ol class="cart-pl">${pl.items.map((it, i) => {
        const m = media[it.id] || {};
        const onAir = it.id === v.on_air, ready = it.id === v.ready;
        return `<li class="cart-pl-row${onAir ? ' is-onair' : ''}${ready ? ' is-ready' : ''}" data-item="${esc(it.id)}" data-index="${i}" data-color="${esc(it.color || '')}"${EDIT ? ` data-drop="pl:${i}"` : ''}>
          <button type="button" class="cart-pl-pick">
            <span class="cart-pl-n">${i + 1}</span>
            <span class="cart-pl-title">${esc(it.title || mediaName(baseName(it.media.file)) || T('untitled', 'Sans titre'))}</span>
            <span class="cart-pl-tag">${onAir ? esc(T('on_air', 'ANTENNE')) : ready ? esc(T('ready', 'PRÊT')) : ''}</span>
            <span class="cart-pl-dur">${esc(fmtDur(m.duration_ms))}</span>
            ${m.state && m.state !== 'ready' ? `<span class="cart-key-media">${esc(T('media_' + m.state, m.state))}</span>` : ''}
          </button>
          ${EDIT ? `<span class="cart-pl-acts">
            <button type="button" class="cart-btn" data-op="up" aria-label="${esc(T('move_up', 'Monter'))}">↑</button>
            <button type="button" class="cart-btn" data-op="down" aria-label="${esc(T('move_down', 'Descendre'))}">↓</button>
            <button type="button" class="cart-btn" data-op="edit" aria-label="${esc(T('edit_item', 'Modifier'))}">✎</button>
            <button type="button" class="cart-btn" data-op="pfl" aria-label="${esc(T('pfl_key', 'Pré-écouter'))}">🎧</button>
            <button type="button" class="cart-btn" data-op="clip" aria-label="${esc(T('clip_edit', 'Éditer le clip…'))}">✂</button>
            <button type="button" class="cart-btn cart-danger" data-op="remove" aria-label="${esc(T('delete', 'Supprimer'))}">✕</button></span>` : ''}
        </li>`; }).join('')}</ol>
      ${EDIT && !PUBLIC ? `<button type="button" class="cart-pl-end" data-drop="pl:${pl.items.length}">${esc(T('pl_drop_end', 'Déposer ici pour ajouter à la fin'))}</button>` : ''}`;
    host.querySelector('.cart-go').onclick = () => post('/pl/go', {});
    host.querySelector('.cart-pl-prev').onclick = () => post('/pl/prev', {});
    host.querySelector('.cart-pl-next').onclick = () => post('/pl/next', {});
    host.querySelector('.cart-ab').onchange = e => post('/pl/options', {ab: e.target.checked});
    host.querySelector('.cart-auto').onchange = e => post('/pl/options', {auto: e.target.checked});
    host.querySelector('.cart-plloop').onchange = e => post('/pl/options', {loop: e.target.checked});
    const add = host.querySelector('.cart-pl-add');
    if (add) add.onclick = () => openLibrary({playlist: true}, T('choose_for_pl', 'Ajouter à la playlist'));
    const end = host.querySelector('.cart-pl-end');
    if (end) {
      end.onclick = () => { if (armed) dropMedia({kind: 'pl', index: pl.items.length}, armed);
        else TOAST(T('armed_none', 'Touchez d\'abord un média du panneau, ou glissez-le ici.'), 'info'); };
      wireDrop(end, {kind: 'pl', index: pl.items.length});
    }
    host.querySelectorAll('.cart-pl-row').forEach(row => {
      const id = row.dataset.item, idx = +row.dataset.index;
      row.querySelector('.cart-pl-pick').onclick = () => {
        if (PUBLIC) return;
        if (EDIT && armed) { dropMedia({kind: 'pl', index: idx}, armed); return; }   // tap-tap: insert here
        post('/pl/goto', {index: idx});
      };
      if (EDIT) wireDrop(row, {kind: 'pl', index: idx});
      row.querySelectorAll('[data-op]').forEach(b => {
        b.onclick = () => {
          const op = b.dataset.op;
          if (op === 'up' || op === 'down') post('/playlist', {op: 'move', id, delta: op === 'up' ? -1 : 1});
          else if (op === 'remove') post('/playlist', {op: 'remove', id});
          else if (op === 'pfl') previewPlay({item: id});
          else if (op === 'clip') openClipEditor({item: id});
          else openItemEditor(pl.items.find(x => x.id === id));
        };
      });
    });
  }

  function openItemEditor(it) {
    if (!it) return;
    const box = $('.cart-editor');
    box.hidden = false;
    box.innerHTML = `
      <div class="cart-panel-head"><strong>${esc(T('edit_item', 'Modifier'))}</strong>
        <button type="button" class="cart-btn cart-ed-close">${esc(T('close', 'Fermer'))}</button></div>
      <div class="cart-form">
        ${field(T('f_title', 'Titre'), `<input type="text" name="title" maxlength="80" value="${esc(it.title)}">`)}
        ${field(T('f_gain', 'Gain de la touche (dB)'), `<input type="number" name="gain_db" step="0.5" min="-40" max="20" value="${it.gain_db}">`)}
        ${inhField('fade_out_ms', it.fade_out_ms, (DOC.defaults || {}).fade_out_ms || 0, T('inh_cart', 'Défaut du cartouchier ({v})'), 2)}
        ${field(T('f_intro', 'Fin de l\'intro (ms)'), `<input type="number" name="intro_ms" step="100" min="0" value="${it.intro_ms}">`)}
        ${field(T('f_outro', 'Début de la fin (ms)'), `<input type="number" name="outro_ms" step="100" min="0" value="${it.outro_ms}">`)}
        <label class="cart-field cart-field-sw"><span>${esc(T('f_loop', 'Boucle'))}</span><input type="checkbox" class="ctl-switch" name="loop"${it.loop ? ' checked' : ''}></label>
      </div>
      <div class="cart-form-acts"><button type="button" class="cart-btn cart-ed-media">${esc(T('choose_media', 'Base des médias…'))}</button>
        <button type="button" class="cart-btn" data-ed="pfl">🎧 ${esc(T('pfl_key', 'Pré-écouter'))}</button>
        <button type="button" class="cart-btn" data-ed="clip">✂ ${esc(T('clip_edit', 'Éditer le clip…'))}</button>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-ed-save">${esc(T('save', 'Enregistrer'))}</button></div>`;
    wireInh(box);
    box.querySelector('.cart-ed-close').onclick = () => { box.hidden = true; };
    box.querySelector('.cart-ed-media').onclick = () => openLibrary({item: it}, T('choose_for_item', 'Changer le son'));
    box.querySelector('[data-ed="pfl"]').onclick = () => previewPlay({item: it.id});
    box.querySelector('[data-ed="clip"]').onclick = () => openClipEditor({item: it.id});
    box.querySelector('.cart-ed-save').onclick = async () => {
      const o = {id: it.id};
      box.querySelectorAll('[name]').forEach(i => {
        o[i.name] = i.type === 'checkbox' ? i.checked : i.type === 'number' ? (+i.value || 0) : i.value;
      });
      readInh(box, o);
      if (await post('/playlist', {op: 'update', item: o})) box.hidden = true;
    };
  }

  // ── settings ──
  // Réglages en SECTIONS encadrées (2026-10-17) : chaque titre ouvre un cadre qui garde ses champs ET son bouton
  // Enregistrer — avec quatre boutons identiques dans une seule colonne, on ne voyait plus lequel valait pour quoi
  function sectionize(box) {
    let sec = null;
    Array.from(box.children).forEach(el => {
      if (el.classList.contains('cart-panel-head')) return;
      if (el.matches('h3.cart-h')) { sec = document.createElement('section'); sec.className = 'cart-sec'; box.insertBefore(sec, el); }
      if (sec) sec.appendChild(el);
    });
  }
  function refreshSettings() {
    const b = $('.cart-settings');
    if (b && !b.hidden) { b.hidden = true; openSettings(); }
  }
  function openSettings() {
    const box = $('.cart-settings');
    if (!box.hidden) { box.hidden = true; return; }
    const s = DOC.settings, pg = curPage(), dfl = Object.assign({}, DEFAULTS0, DOC.defaults || {});
    const nPl = (S && S.n_players) || 2;
    const pgName = pageLabel(pg, DOC.pages.indexOf(pg));
    box.hidden = false;
    box.innerHTML = `
      <div class="cart-panel-head"><strong>${esc(T('settings', 'Réglages'))}</strong>
        <button type="button" class="cart-btn cart-st-close">${esc(T('close', 'Fermer'))}</button></div>
      <h3 class="cart-h">${esc(T('sec_levels', 'Niveaux (EBU R128)'))}</h3>
      <div class="cart-form">
        <label class="cart-field cart-field-sw"><span>${esc(T('normalize', 'Normaliser au chargement'))}</span><input type="checkbox" class="ctl-switch" name="normalize"${s.normalize ? ' checked' : ''}></label>
        ${field(T('target', 'Cible (LUFS)'), `<input type="number" name="target_lufs" step="0.5" min="-40" max="-5" value="${s.target_lufs}">`)}
        ${field(T('max_gain', 'Gain maximal (dB)'), `<input type="number" name="max_gain_db" step="0.5" min="0" max="30" value="${s.max_gain_db}">`)}
        ${field(T('ceiling', 'Plafond de crête (dBFS)'), `<input type="number" name="peak_ceiling_db" step="0.5" min="-20" max="0" value="${s.peak_ceiling_db}">`)}
        ${field(T('alert', 'Alerte du décompte (s)'), `<input type="number" name="alert_s" step="1" min="0" max="120" value="${s.alert_s}">`)}
      </div>
      <div class="cart-form-acts"><span class="cart-spacer"></span><button type="button" class="cart-btn cart-primary cart-st-save">${esc(T('save_levels', 'Enregistrer les niveaux'))}</button></div>

      <h3 class="cart-h">${esc(T('sec_defaults', 'Défauts des touches'))}</h3>
      <p class="cart-note">${esc(T('defaults_help', 'Chaque touche hérite de ces réglages, sauf ceux qu\'elle personnalise. Les changer s\'applique à toutes les touches qui en héritent ; le fondu d\'arrêt, même à un son déjà lancé.'))}</p>
      <div class="cart-form cart-dfl-cart">
        ${KEY_F.map(f => f === 'loop'
          ? `<label class="cart-field cart-field-sw"><span>${esc(lbl(f))}</span>${inputFor(f, dfl[f], nPl)}</label>`
          : field(lbl(f), inputFor(f, dfl[f], nPl))).join('')}
        <label class="cart-field cart-field-sw"><span>${esc(T('dfl_auto', 'Enchaînement automatique (playlist)'))}</span><input type="checkbox" class="ctl-switch" name="pl_auto"${DOC.playlist.auto ? ' checked' : ''}></label>
      </div>
      <div class="cart-form-acts"><span class="cart-spacer"></span><button type="button" class="cart-btn cart-primary cart-dfl-save">${esc(T('save_defaults', 'Enregistrer les défauts'))}</button></div>
      <h3 class="cart-h">${esc(T('sec_page_defaults', 'Défauts de la page « {p} »').replace('{p}', pgName))}</h3>
      <p class="cart-note">${esc(T('page_defaults_help', 'Une page peut s\'écarter des défauts du cartouchier pour ses touches.'))}</p>
      <div class="cart-form cart-dfl-page">
        ${KEY_F.map(f => inhField(f, (pg.defaults || {})[f], dfl[f], T('inh_cart_same', 'Comme le cartouchier ({v})'), nPl)).join('')}
      </div>
      <div class="cart-form-acts"><span class="cart-spacer"></span><button type="button" class="cart-btn cart-primary cart-pdfl-save">${esc(T('save_page_defaults', 'Enregistrer les défauts de la page'))}</button></div>

      <h3 class="cart-h">${esc(T('sec_grid', 'Grille et pages'))}</h3>
      <div class="cart-form">
        ${field(T('cols', 'Colonnes'), `<input type="number" name="cols" min="1" max="12" value="${DOC.grid.cols}">`)}
        ${field(T('rows', 'Rangées'), `<input type="number" name="rows" min="1" max="12" value="${DOC.grid.rows}">`)}
        ${field(T('page_name', 'Nom de la page affichée'), `<input type="text" name="page_name" maxlength="40" value="${esc(pg.name)}" placeholder="${esc(pgName)}">`)}
      </div>
      <p class="cart-note">${esc(T('pages_here', 'Pages ({n}) — la page en surbrillance est celle affichée ; touchez-en une pour l\'afficher :').replace('{n}', DOC.pages.length))}</p>
      <div class="cart-pages cart-pages-set" role="tablist">${DOC.pages.map((p2, i) => `<button type="button" role="tab" class="cart-btn cart-page${p2.id === DOC.page ? ' is-on' : ''}" data-spage="${esc(p2.id)}" aria-selected="${p2.id === DOC.page}">${esc(pageLabel(p2, i))}</button>`).join('')}</div>
      <div class="cart-form-acts">
        <button type="button" class="cart-btn cart-pg-add">${esc(T('page_add', 'Ajouter une page'))}</button>
        <button type="button" class="cart-btn" data-pmove="-1">${esc(T('page_move_left', '← Déplacer la page'))}</button>
        <button type="button" class="cart-btn" data-pmove="1">${esc(T('page_move_right', 'Déplacer la page →'))}</button>
        <button type="button" class="cart-btn cart-danger cart-pg-del">${esc(T('page_delete', 'Supprimer la page'))}</button>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-grid-save">${esc(T('save_grid', 'Enregistrer la grille'))}</button>
      </div>

      <h3 class="cart-h">${esc(T('sec_players', 'Lecteurs (sorties MXL)'))}</h3>
      <p class="cart-note">${esc(T('players_help', 'Chaque lecteur est une sortie audio stéréo sur le bus MXL. Changer leur nombre change les sorties et REDÉPLOIE le cartouchier : ce qui joue s\'arrête, et les câbles des lecteurs supprimés tombent.'))}</p>
      <div class="cart-players-set"></div>`;
    sectionize(box);
    const v = n => box.querySelector(`[name="${n}"]`);
    wireInh(box.querySelector('.cart-dfl-page'));
    box.querySelector('.cart-st-close').onclick = () => { box.hidden = true; };
    box.querySelector('.cart-st-save').onclick = () => post('/settings', {
      normalize: v('normalize').checked, target_lufs: +v('target_lufs').value,
      max_gain_db: +v('max_gain_db').value, peak_ceiling_db: +v('peak_ceiling_db').value, alert_s: +v('alert_s').value});
    box.querySelector('.cart-dfl-save').onclick = async () => {
      const o = {};
      box.querySelectorAll('.cart-dfl-cart [data-f]').forEach(i => { o[i.dataset.f] = readInput(i); });
      const d = await post('/defaults', o);
      if (d && v('pl_auto').checked !== !!DOC.playlist.auto) await post('/pl/options', {auto: v('pl_auto').checked});
    };
    box.querySelector('.cart-pdfl-save').onclick = () =>
      post('/defaults', readInh(box.querySelector('.cart-dfl-page'), {page: pg.id}));
    box.querySelector('.cart-grid-save').onclick = async () => {
      await post('/grid', {cols: +v('cols').value, rows: +v('rows').value});
      if (v('page_name').value !== pg.name) await post('/pages', {op: 'rename', page: pg.id, name: v('page_name').value});
    };
    // les gestes de page se VOIENT ici (2026-10-17) : la barre d'onglets est en haut de la grille, loin du panneau — on
    // ajoutait des pages sans rien voir bouger ; le panneau se redessine et l'onglet affiché est en surbrillance
    const pageOp = async (body, msg) => { const d = await post('/pages', body); if (d) { refreshSettings(); if (msg) TOAST(msg(), 'info'); } };
    box.querySelector('.cart-pg-add').onclick = () => pageOp({op: 'add'}, () =>
      T('page_added', 'Page ajoutée et affichée ({n} pages)').replace('{n}', DOC.pages.length));
    box.querySelectorAll('[data-pmove]').forEach(b => { b.onclick = () => pageOp({op: 'move', page: DOC.page, delta: +b.dataset.pmove}); });
    box.querySelectorAll('[data-spage]').forEach(b => { b.onclick = async () => { if (await post('/page', {page: b.dataset.spage})) refreshSettings(); }; });
    box.querySelector('.cart-pg-del').onclick = () => {
      if (box.dataset.confirm !== DOC.page) {
        box.dataset.confirm = DOC.page;
        TOAST(T('confirm_page_delete', 'Appuyez encore pour supprimer la page et ses touches'), 'warning');
        return;
      }
      delete box.dataset.confirm;
      pageOp({op: 'delete', page: DOC.page});
    };
    renderPlayersSetting(box.querySelector('.cart-players-set'), nPl);
  }

  // Number of players = a config_schema field (`players`), written by the orchestrator's own path
  // (POST /api/containers/<vmid>/plugin_config, the one the ⚙ popover uses), which REDEPLOYS. A costly
  // setting: explained, and confirmed in the page. Only with containers.deploy (a `system` field).
  async function renderPlayersSetting(host, running) {
    if (!host || PUBLIC) return;
    let cur = running;
    try {
      const r = await fetch('/api/containers/' + VMID + '/plugin_config', {cache: 'no-store'});
      if (r.ok) { const j = await r.json(); if (j.params && j.params.players) cur = +j.params.players; }
    } catch (e) { /* the running count stands in */ }
    if (!EL || !host.isConnected) return;
    if (!canDeploy()) {
      host.innerHTML = `<p class="cart-note"><strong>${esc(T('players_count', '{n} lecteur(s)').replace('{n}', cur))}</strong> — ${esc(T('players_no_perm', 'modifier ce nombre exige le droit de déployer.'))}</p>`;
      return;
    }
    host.innerHTML = `
      <div class="cart-form">${field(T('players_label', 'Nombre de lecteurs'), `<input type="number" class="cart-npl" min="1" max="8" step="1" value="${cur}">`)}</div>
      <div class="cart-form-acts"><span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-danger cart-npl-apply">${esc(T('players_apply', 'Appliquer (redéploie le cartouchier)'))}</button></div>
      <div class="cart-confirm" hidden>
        <p class="cart-confirm-txt"></p>
        <div class="cart-form-acts"><button type="button" class="cart-btn cart-npl-cancel">${esc(T('cancel', 'Annuler'))}</button>
          <span class="cart-spacer"></span>
          <button type="button" class="cart-btn cart-primary cart-npl-go">${esc(T('players_confirm', 'Redéployer maintenant'))}</button></div>
      </div>`;
    const inp = host.querySelector('.cart-npl'), conf = host.querySelector('.cart-confirm');
    host.querySelector('.cart-npl-apply').onclick = () => {
      const n = Math.round(+inp.value);
      if (!(n >= 1 && n <= 8)) { inp.reportValidity && inp.reportValidity(); return; }
      if (n === cur) { TOAST(T('players_same', 'Nombre de lecteurs inchangé'), 'info'); return; }
      const playing = S ? S.players.filter(p => p.state === 'playing' || p.state === 'fading').length : 0;
      conf.querySelector('.cart-confirm-txt').textContent = T('players_confirm_txt',
        'Passer de {a} à {b} lecteur(s) : le cartouchier redémarre. {p} lecteur(s) en cours s\'arrêteront.')
        .replace('{a}', cur).replace('{b}', n).replace('{p}', playing)
        + (n < cur ? ' ' + T('players_confirm_less', 'Les câbles des lecteurs {r} à {c} tomberont.').replace('{r}', n + 1).replace('{c}', cur) : '');
      conf.hidden = false;
    };
    host.querySelector('.cart-npl-cancel').onclick = () => { conf.hidden = true; };
    host.querySelector('.cart-npl-go').onclick = async () => {
      conf.hidden = true;
      const n = Math.round(+inp.value);
      let r, j = {};
      try {
        r = await fetch('/api/containers/' + VMID + '/plugin_config', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({params: {players: n}, confirm: true})});
        j = await r.json().catch(() => ({}));
      } catch (e) { TOAST(T('unreachable', 'Conteneur injoignable'), 'error'); return; }
      if (r.ok) {
        cur = n;
        TOAST(j.redeploye === false ? T('players_saved', 'Enregistré : appliqué au prochain démarrage du cartouchier.')
          : T('players_applying', 'Redéploiement du cartouchier en cours…'), 'info');
      } else if (r.status === 403) TOAST(T('err_forbidden', 'Non autorisé'), 'error');
      else if (j.pending) TOAST(T('players_pending', 'Un redéploiement est déjà en cours'), 'warning');
      else TOAST(T('players_failed', 'Refusé : {e}').replace('{e}', j.error || ('HTTP ' + r.status)), 'error');
    };
  }

  // ═══ MONITORING (0.1.3) ══════════════════════════════════════════════════════════════════════
  // The audio console's listening, reused: the container serves GET /listen.pcm (declared in
  // `control.streams`, relayed as is by the orchestrator's app/plugin_streams.py), int16 LE stereo
  // 48 kHz from the live edge; here an AudioWorklet (ScriptProcessor when the page is not a secure
  // context) plays it through a ~60 ms ring. ONE choice at a time: a player's output (what really
  // goes to the console) or the PRE-LISTEN (`pfl`: the cart's off-air player, no MXL flow). Changing
  // the choice closes the stream and opens the other one; unmounting closes it; the container stops
  // the pre-listen when its last listener leaves. Not on the public page (the relay serves no stream).
  class Ring {
    constructor(frames) { this.buf = new Float32Array(frames * 2); this.cap = frames; this.r = 0; this.w = 0; this.n = 0;
      this.target = 2880; this.maxF = 7200; this.primed = false; this.under = 0; this.dropped = 0; }
    push(f) {
      const nf = f.length >> 1;
      for (let i = 0; i < nf; i++) {
        if (this.n >= this.cap) { this.r = (this.r + 1) % this.cap; this.n--; }
        this.buf[this.w * 2] = f[i * 2]; this.buf[this.w * 2 + 1] = f[i * 2 + 1];
        this.w = (this.w + 1) % this.cap; this.n++;
      }
      if (this.n > this.maxF) { const d = this.n - this.target; this.r = (this.r + d) % this.cap; this.n -= d; this.dropped++; }
    }
    pull(L, R) {
      const len = L.length;
      if (!this.primed) { if (this.n >= this.target) this.primed = true; else { L.fill(0); R.fill(0); return; } }
      for (let i = 0; i < len; i++) {
        if (this.n <= 0) { this.primed = false; this.under++; for (; i < len; i++) { L[i] = 0; R[i] = 0; } return; }
        L[i] = this.buf[this.r * 2]; R[i] = this.buf[this.r * 2 + 1];
        this.r = (this.r + 1) % this.cap; this.n--;
      }
    }
  }
  const LISTEN_WORKLET = Ring.toString() + `
    class CartLP extends AudioWorkletProcessor {
      constructor() { super(); this.ring = new Ring(48000); this.port.onmessage = (e) => this.ring.push(e.data); }
      process(i, o) { const out = o[0]; this.ring.pull(out[0], out[1] || out[0]); return true; }
    }
    registerProcessor("cart-listen", CartLP);`;
  const LST = {on: false, src: 'pfl', abort: null, ctx: null, node: null, ring: null, err: '', gen: 0};
  const lstLabel = src => src === 'pfl' ? T('lst_pfl', 'Pré-écoute') : T('player_n', 'Lecteur {n}').replace('{n}', src);

  function listenMount() {
    const host = $('.cart-listen');
    if (!host || PUBLIC) return;
    try { const v = localStorage.getItem('cart.listen.src.' + VMID); if (v) LST.src = v; } catch (e) { /* private window */ }
    host.innerHTML = `
      <button type="button" class="cart-btn cart-lst-btn" aria-pressed="false">🎧 ${esc(T('listen', 'Écoute'))}</button>
      <select class="cart-lst-src" aria-label="${esc(T('listen_src', 'Source écoutée'))}"></select>
      <button type="button" class="cart-btn cart-pfl-stop" hidden>■ ${esc(T('pfl_stop', 'Arrêter la pré-écoute'))}</button>
      <span class="cart-lst-msg" role="status"></span>`;
    host.querySelector('.cart-lst-btn').onclick = () => { if (LST.on) listenStop(); else listenStart(); };
    host.querySelector('.cart-lst-src').onchange = e => listenSet(e.target.value, LST.on);
    host.querySelector('.cart-pfl-stop').onclick = () => previewStop();
    listenPaint();
  }
  function listenPaint() {
    const host = $('.cart-listen');
    if (!host || PUBLIC) return;
    const sel = host.querySelector('.cart-lst-src');
    const n = (S && S.n_players) || 0;
    if (sel && sel.options.length !== n + 1) {
      sel.innerHTML = ['pfl'].concat(Array.from({length: n}, (_, i) => String(i + 1)))
        .map(v => `<option value="${v}">${esc(lstLabel(v))}</option>`).join('');
    }
    if (sel) sel.value = LST.src;
    const b = host.querySelector('.cart-lst-btn');
    b.classList.toggle('is-on', LST.on); b.setAttribute('aria-pressed', String(LST.on));
    const pv = S && S.preview;
    host.querySelector('.cart-pfl-stop').hidden = !(pv && pv.state !== 'idle');
    host.querySelector('.cart-lst-msg').textContent = LST.err || (pv && pv.state !== 'idle'
      ? T('pfl_playing', 'Pré-écoute : {t}').replace('{t}', pv.title || '') : '');
  }
  async function listenSet(src, restart) {
    LST.src = String(src);
    try { localStorage.setItem('cart.listen.src.' + VMID, LST.src); } catch (e) { /* private window */ }
    if (restart || LST.on) { listenStop(); await listenStart(); }
    listenPaint();
  }
  async function listenStart() {
    if (LST.on || PUBLIC || !EL) return;
    const gen = ++LST.gen;
    LST.on = true; LST.err = ''; listenPaint();
    LST.abort = new AbortController();
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      LST.ctx = new Ctx({sampleRate: 48000, latencyHint: 'interactive'});
      if (window.isSecureContext && LST.ctx.audioWorklet) {
        try {
          const url = URL.createObjectURL(new Blob([LISTEN_WORKLET], {type: 'application/javascript'}));
          await LST.ctx.audioWorklet.addModule(url);
          LST.node = new AudioWorkletNode(LST.ctx, 'cart-listen', {outputChannelCount: [2]});
        } catch (e) { LST.node = null; }
      }
      if (!LST.node) {
        const ring = LST.ring = new Ring(48000);
        LST.node = LST.ctx.createScriptProcessor(1024, 0, 2);
        LST.node.onaudioprocess = e => ring.pull(e.outputBuffer.getChannelData(0), e.outputBuffer.getChannelData(1));
      }
      LST.node.connect(LST.ctx.destination);
      if (LST.ctx.state === 'suspended') LST.ctx.resume().catch(() => {});
      const resp = await fetch(api('/listen.pcm') + '?src=' + encodeURIComponent(LST.src), {signal: LST.abort.signal, cache: 'no-store'});
      if (!resp.ok || !resp.body) {
        const d = await resp.json().catch(() => ({}));
        throw new Error(errText(d, resp.status));
      }
      const rd = resp.body.getReader();
      let carry = new Uint8Array(0);
      for (;;) {
        const {value, done} = await rd.read();
        if (done || gen !== LST.gen) break;
        let u8 = value;
        if (carry.length) { u8 = new Uint8Array(carry.length + value.length); u8.set(carry); u8.set(value, carry.length); }
        const n = u8.length - (u8.length % 4);
        carry = u8.slice(n);
        const dv = new DataView(u8.buffer, u8.byteOffset, n);
        const f = new Float32Array(n / 2);
        for (let i = 0; i < f.length; i++) f[i] = dv.getInt16(i * 2, true) / 32768;
        if (LST.ring) LST.ring.push(f); else LST.node.port.postMessage(f, [f.buffer]);
      }
      if (gen === LST.gen && LST.on) LST.err = T('lst_ended', 'Écoute interrompue');
    } catch (e) {
      if (!(e && e.name === 'AbortError') && gen === LST.gen) LST.err = String((e && e.message) || e);
    }
    if (gen === LST.gen) listenStop(true);
  }
  function listenStop(keepErr) {
    LST.gen++;
    LST.on = false;
    if (LST.abort) { try { LST.abort.abort(); } catch (e) { /* done */ } LST.abort = null; }
    try { if (LST.node) LST.node.disconnect(); } catch (e) { /* done */ }
    try { if (LST.ctx) LST.ctx.close(); } catch (e) { /* done */ }
    LST.node = null; LST.ctx = null; LST.ring = null;
    if (!keepErr) LST.err = '';
    listenPaint();
  }
  // Pre-listen a key / an item (or the clip editor's draft): the listening switches to `pfl` if it
  // was elsewhere — one choice at a time — and starts if it was off (this tap is the user gesture
  // the browser needs to open audio).
  async function previewPlay(body) {
    if (PUBLIC) return null;
    if (!(LST.on && LST.src === 'pfl')) listenSet('pfl', true);
    return post('/preview', body);
  }
  function previewStop() { return post('/preview/stop', {}); }
  // 🎧 on a key in edit mode (a span inside the key's button: the tap must not open the editor).
  function pflTap(btn, ev) {
    const t = ev && ev.target && ev.target.closest ? ev.target.closest('[data-pfl]') : null;
    if (!t || !btn.contains(t)) return false;
    previewPlay({key: t.dataset.pfl});
    return true;
  }

  // ═══ CLIP EDITOR (0.1.3) ═════════════════════════════════════════════════════════════════════
  // Non-destructive: In / Out, cuts, assembly with crossfades, fades, volume envelope and gain are
  // the EDIT LIST of the key or item (src/montage.py has the format and the gain laws); the file is
  // never touched. The container renders the list at play time, serves the waveform windows (never
  // computed here on a whole file) and renders "save as new media". Two views: MONTAGE (the edited
  // timeline: fades, envelope, intro / end, crossfades) and SOURCE (the selected segment's file:
  // In, Out, cuts). iPad first: 44 px buttons for every action, a tap places the cursor, one finger
  // pans, two fingers pinch, an envelope point is dragged with the finger. Undo / redo is a local
  // stack of drafts; nothing is written before « Save » (or « Save as new media »).
  const SHAPES = ['lin', 'eq', 's', 'exp'];
  const EDIT0 = () => ({segments: [{media: null, in_ms: 0, out_ms: 0, cuts: [], xfade_ms: 0, xfade_shape: 'eq'}],
    fade_in_ms: 0, fade_in_shape: 'lin', fade_out_ms: 0, fade_out_shape: 'lin', envelope: []});
  // Source time t of segment `seg` -> edited time, through the layout the container returns (pieces
  // in ms). A point inside a removed passage maps to the joint that replaces it.
  function srcToMix(layout, seg, t) {
    const ps = (layout || []).filter(p => p.seg === seg);
    if (!ps.length) return 0;
    for (const p of ps) {
      if (t < p.src_ms) return p.at_ms;
      if (t <= p.src_ms + p.len_ms) return p.at_ms + (t - p.src_ms);
    }
    const l = ps[ps.length - 1];
    return l.at_ms + l.len_ms;
  }
  // Edited time -> {seg, t} in source time (playhead in the source view), or null.
  function mixToSrc(layout, t) {
    const ps = layout || [];
    for (let i = ps.length - 1; i >= 0; i--) {
      const p = ps[i];
      if (t >= p.at_ms && t < p.at_ms + p.len_ms) return {seg: p.seg, t: p.src_ms + (t - p.at_ms)};
    }
    return null;
  }
  // Envelope value (dB) at t: linear between points, flat outside (montage.py's law).
  function envDbAt(env, t) {
    if (!env || !env.length) return 0;
    if (t <= env[0][0]) return env[0][1];
    for (let i = 1; i < env.length; i++) {
      if (t < env[i][0]) return env[i - 1][1] + (env[i][1] - env[i - 1][1]) * (t - env[i - 1][0]) / (env[i][0] - env[i - 1][0]);
    }
    return env[env.length - 1][1];
  }
  const fmtMs = ms => { ms = Math.max(0, Math.round(ms || 0)); const m = Math.floor(ms / 60000);
    return m + ':' + String(Math.floor(ms / 1000) % 60).padStart(2, '0') + '.' + String(ms % 1000).padStart(3, '0'); };
  const clone = o => JSON.parse(JSON.stringify(o));

  let CE = null;
  const tb = () => CE.t.key ? {key: CE.t.key} : {item: CE.t.item};
  const findKeyDoc = id => { for (const p of DOC.pages) { const k = p.keys.find(x => x.id === id); if (k) return {k, p}; } return null; };
  async function jget(p) {
    const r = await fetch(api(p), {cache: 'no-store'});
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) { const e = new Error(errText(d, r.status)); e.code = d.error; throw e; }
    return d;
  }
  async function jpost(p, body) {
    const r = await fetch(api(p), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) { const e = new Error(errText(d, r.status)); e.code = d.error; throw e; }
    return d;
  }

  function openClipEditor(t) {
    if (PUBLIC || !DOC) return;
    const it = t.key ? (findKeyDoc(t.key) || {}).k : DOC.playlist.items.find(x => x.id === t.item);
    if (!it) return;
    clipClose(true);
    const draft = {edit: it.edit ? clone(it.edit) : EDIT0(), gain_db: it.gain_db || 0,
                   intro_ms: it.intro_ms || 0, outro_ms: it.outro_ms || 0};
    CE = {t: t.key ? {key: t.key} : {item: t.item}, id: it.id, media: it.media,
          title: it.title || mediaName(baseName(it.media.file)) || '', draft, saved: JSON.stringify(draft),
          undo: [], redo: [], view: 'mix', seg: 0, cursor: 0, sel: null, win: null, dom: 0, wave: null, layout: [],
          envSel: null, ptrs: new Map(), drag: null, pinch: null, confirm: null, job: null, err: '', seq: 0,
          pre: 2, post: 2, fetchT: null, tick: null};
    const ed = $('.cart-editor');
    if (ed) { ed.hidden = true; editing = null; }
    const box = document.createElement('div');
    box.className = 'cart-clip';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    $('.cart-root').appendChild(box);
    CE.box = box;
    CE.tick = setInterval(() => { if (CE && S && S.preview && S.preview.state !== 'idle') clipDraw(); }, 40);
    clipRender();
    clipFetch(true);
  }
  function clipClose(force) {
    if (!CE) return;
    if (!force && clipDirty()) { CE.confirm = 'abandon'; clipRender(); return; }
    const pv = S && S.preview;
    if (pv && pv.state !== 'idle' && (pv.key === CE.t.key || pv.item === CE.t.item) && EL) previewStop();
    clearInterval(CE.tick); clearTimeout(CE.fetchT);
    if (CE.box) CE.box.remove();
    if (libTarget && libTarget.clip) libTarget = null;
    CE = null;
  }
  const clipDirty = () => !!CE && JSON.stringify(CE.draft) !== CE.saved;
  const segOf = i => CE.draft.edit.segments[i];
  const segMedia = i => (segOf(i) && segOf(i).media) || CE.media;
  const segName = i => { const m = segMedia(i); return (i === 0 && !segOf(0).media && CE.title) || mediaName(baseName(m.file)) || m.asset || '?'; };
  function materialize() { CE.draft.edit.segments.forEach(s => { if (!s.media) s.media = Object.assign({}, CE.media); }); }
  function change(fn, refetch) {
    CE.undo.push(JSON.stringify(CE.draft)); if (CE.undo.length > 200) CE.undo.shift();
    CE.redo = [];
    fn(CE.draft);
    clipRender();
    if (refetch !== false) clipFetchSoon();
  }
  function undoRedo(from, to) {
    if (!from.length) return;
    to.push(JSON.stringify(CE.draft));
    CE.draft = JSON.parse(from.pop());
    if (CE.seg >= CE.draft.edit.segments.length) CE.seg = 0;
    clipRender(); clipFetchSoon();
  }
  function clipFetchSoon(fit) { clearTimeout(CE.fetchT); CE.fetchT = setTimeout(() => clipFetch(fit), 90); }

  async function clipFetch(fit) {
    if (!CE) return;
    const seq = ++CE.seq;
    const cv = CE.box.querySelector('.cart-clip-wave');
    const pts = Math.min(4000, Math.max(200, Math.round((cv ? cv.clientWidth : 800) * (window.devicePixelRatio || 1))));
    const a = (!fit && CE.win) ? CE.win[0] : 0, b = (!fit && CE.win) ? CE.win[1] : 0;
    let d = null;
    try {
      if (CE.view === 'mix') {
        d = await jpost('/montage/wave', Object.assign(tb(), {edit: CE.draft.edit, from_ms: a, to_ms: b, points: pts}));
        if (CE && seq === CE.seq) CE.layout = d.layout || [];
      } else {
        const m = segMedia(CE.seg);
        d = await jget('/wave?asset=' + encodeURIComponent(m.asset || '') + '&file=' + encodeURIComponent(m.file || '')
                       + '&from_ms=' + a + '&to_ms=' + b + '&points=' + pts);
        // the layout (source ↔ edited time) is what the pre-listen of a source point needs
        jpost('/montage/wave', Object.assign(tb(), {edit: CE.draft.edit, from_ms: 0, to_ms: 1, points: 1}))
          .then(l => { if (CE && seq === CE.seq) CE.layout = l.layout || []; }).catch(() => {});
      }
      if (!CE || seq !== CE.seq) return;
      CE.err = '';
      CE.dom = d.duration_ms;
      if (fit || !CE.win || CE.win[1] > CE.dom + 1) CE.win = [0, CE.dom];
      CE.wave = d;
    } catch (e) {
      if (!CE || seq !== CE.seq) return;
      CE.err = e.message;
      if (e.code === 'media_not_ready') CE.fetchT = setTimeout(() => clipFetch(fit), 2000);   // decoding: retried
    }
    clipPaintMeta();
    clipDraw();
  }

  // ── view window: zoom (buttons, pinch, wheel) and pan ──
  function setWin(a, b) {
    const dom = CE.dom || 1, minW = Math.min(dom, 20);
    let w = Math.max(minW, Math.min(dom, b - a));
    a = Math.max(0, Math.min(dom - w, a));
    CE.win = [a, a + w];
    clipDraw(); clipPaintMeta(); clipFetchSoon();
  }
  function zoomAt(c, f) { const [a, b] = CE.win; setWin(c - (c - a) * f, c + (b - c) * f); }
  function tAt(clientX) {
    const cv = CE.box.querySelector('.cart-clip-wave'), r = cv.getBoundingClientRect();
    return CE.win[0] + (clientX - r.left) / Math.max(1, r.width) * (CE.win[1] - CE.win[0]);
  }
  const ENV_DB_MIN = -60, ENV_DB_MAX = 12;
  const envY = (db, H) => H * (1 - (db - ENV_DB_MIN) / (ENV_DB_MAX - ENV_DB_MIN));
  function hitEnv(e) {
    if (CE.view !== 'mix' || !CE.win) return null;
    const cv = CE.box.querySelector('.cart-clip-wave'), r = cv.getBoundingClientRect();
    const env = CE.draft.edit.envelope;
    for (let i = 0; i < env.length; i++) {
      const x = r.left + (env[i][0] - CE.win[0]) / (CE.win[1] - CE.win[0]) * r.width;
      const y = r.top + envY(env[i][1], r.height);
      if (Math.hypot(e.clientX - x, e.clientY - y) <= 22) return i;
    }
    return null;
  }
  function wireCanvas(cv) {
    cv.addEventListener('pointerdown', e => {
      if (!CE || !CE.win) return;
      cv.setPointerCapture(e.pointerId);
      CE.ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (CE.ptrs.size === 2) {
        const [p, q] = [...CE.ptrs.values()];
        CE.pinch = {d0: Math.max(10, Math.abs(p.x - q.x)), win0: CE.win.slice(), c: tAt((p.x + q.x) / 2)};
        CE.drag = null;
      } else if (CE.ptrs.size === 1) {
        CE.drag = {x0: e.clientX, y0: e.clientY, win0: CE.win.slice(), moved: false, env: hitEnv(e), pushed: false};
      }
    });
    cv.addEventListener('pointermove', e => {
      if (!CE || !CE.ptrs.has(e.pointerId)) return;
      CE.ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
      const r = cv.getBoundingClientRect();
      if (CE.pinch && CE.ptrs.size >= 2) {
        const [p, q] = [...CE.ptrs.values()];
        const f = CE.pinch.d0 / Math.max(10, Math.abs(p.x - q.x)), [a, b] = CE.pinch.win0, c = CE.pinch.c;
        setWin(c - (c - a) * f, c + (b - c) * f);
        return;
      }
      const d = CE.drag;
      if (!d) return;
      if (!d.moved && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < 8) return;
      d.moved = true;
      if (d.env != null) {               // dragging an envelope point: time and level
        if (!d.pushed) { CE.undo.push(JSON.stringify(CE.draft)); CE.redo = []; d.pushed = true; }
        const env = CE.draft.edit.envelope, i = d.env;
        const lo = i > 0 ? env[i - 1][0] + 1 : 0, hi = i < env.length - 1 ? env[i + 1][0] - 1 : CE.dom;
        env[i][0] = Math.round(Math.max(lo, Math.min(hi, tAt(e.clientX))));
        const db = ENV_DB_MAX - (e.clientY - r.top) / Math.max(1, r.height) * (ENV_DB_MAX - ENV_DB_MIN);
        env[i][1] = Math.round(Math.max(ENV_DB_MIN, Math.min(ENV_DB_MAX, db)) * 2) / 2;
        CE.envSel = i;
        clipDraw();
        return;
      }
      const [a, b] = d.win0, dt = (d.x0 - e.clientX) / Math.max(1, r.width) * (b - a);
      setWin(a + dt, b + dt);
    });
    const up = e => {
      if (!CE || !CE.ptrs.has(e.pointerId)) return;
      CE.ptrs.delete(e.pointerId);
      if (CE.pinch) { if (CE.ptrs.size < 2) CE.pinch = null; return; }
      const d = CE.drag;
      CE.drag = null;
      if (!d) return;
      if (!d.moved) {                    // a tap: select a point, or place the cursor
        if (d.env != null) CE.envSel = d.env;
        else { CE.cursor = Math.max(0, Math.min(CE.dom, tAt(e.clientX))); CE.envSel = null; }
        clipRender(); clipDraw();
      } else if (d.env != null) { clipRender(); clipFetchSoon(); }
    };
    cv.addEventListener('pointerup', up);
    cv.addEventListener('pointercancel', up);
    cv.addEventListener('wheel', e => { if (!CE || !CE.win) return; e.preventDefault(); zoomAt(tAt(e.clientX), e.deltaY > 0 ? 1.25 : 0.8); },
                        {passive: false});
  }

  function clipDraw() {
    if (!CE || !CE.box) return;
    const cv = CE.box.querySelector('.cart-clip-wave');
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(cv.clientWidth * dpr)), H = Math.max(1, Math.round(cv.clientHeight * dpr));
    if (cv.width !== W) cv.width = W;
    if (cv.height !== H) cv.height = H;
    const g = cv.getContext('2d');
    g.clearRect(0, 0, W, H);
    if (!CE.win || !CE.wave) return;
    const [a, b] = CE.win, span = Math.max(1e-6, b - a);
    const X = t => (t - a) / span * W;
    const band = (t0, t1, color) => { const x0 = Math.max(0, X(t0)), x1 = Math.min(W, X(t1)); if (x1 > x0) { g.fillStyle = color; g.fillRect(x0, 0, x1 - x0, H); } };
    const line = (t, color, w) => { const x = X(t); if (x >= -2 && x <= W + 2) { g.fillStyle = color; g.fillRect(Math.round(x) - (w || 2) / 2, 0, w || 2, H); } };
    const removed = cssVar('--bg-hover'), sel = cssVar('--accent-soft');
    const s = segOf(CE.seg);
    if (CE.view === 'src' && s) {
      band(0, s.in_ms, removed);
      if (s.out_ms) band(s.out_ms, CE.dom, removed);
      s.cuts.forEach(c => band(c[0], c[1], removed));
    } else {
      CE.layout.forEach(p => { if (p.xf_ms) band(p.at_ms, p.at_ms + p.xf_ms, sel); });
    }
    if (CE.sel) band(Math.min(CE.sel[0], CE.sel[1]), Math.max(CE.sel[0], CE.sel[1]), sel);
    const w = CE.wave, pts = w.points || [], n = pts.length, wa = w.from_ms, ws = Math.max(1e-6, w.to_ms - w.from_ms);
    const on = cssVar('--accent'), off = cssVar('--text-muted');
    const cut = t => CE.view === 'src' && s && (t < s.in_ms || (s.out_ms && t >= s.out_ms) || s.cuts.some(c => t >= c[0] && t < c[1]));
    for (let x = 0; x < W; x++) {
      const t = a + x / W * span;
      const i = Math.floor((t - wa) / ws * n);
      if (i < 0 || i >= n) continue;
      const h = Math.max(1, pts[i] / 100 * (H - 4));
      g.fillStyle = cut(t) ? off : on;
      g.fillRect(x, (H - h) / 2, 1, h);
    }
    if (CE.view === 'src' && s) {
      line(s.in_ms, cssVar('--status-running-fg'), 3);
      if (s.out_ms) line(s.out_ms, cssVar('--status-error-fg'), 3);
    } else {
      const ed = CE.draft.edit;
      CE.layout.forEach((p, i) => { if (i && p.seg !== CE.layout[i - 1].seg) line(p.at_ms, cssVar('--border-strong'), 2); });
      g.strokeStyle = cssVar('--status-warning-fg'); g.lineWidth = 2 * dpr;
      if (ed.fade_in_ms) { g.beginPath(); g.moveTo(X(0), H); g.lineTo(X(ed.fade_in_ms), 0); g.stroke(); }
      if (ed.fade_out_ms) { g.beginPath(); g.moveTo(X(CE.dom - ed.fade_out_ms), 0); g.lineTo(X(CE.dom), H); g.stroke(); }
      const env = ed.envelope;
      if (env.length) {
        g.strokeStyle = cssVar('--status-running-fg'); g.beginPath();
        g.moveTo(0, envY(envDbAt(env, a), H));
        env.forEach(p => g.lineTo(X(p[0]), envY(p[1], H)));
        g.lineTo(W, envY(envDbAt(env, b), H)); g.stroke();
        env.forEach((p, i) => { g.fillStyle = i === CE.envSel ? cssVar('--text-strong') : cssVar('--status-running-fg');
          g.beginPath(); g.arc(X(p[0]), envY(p[1], H), (i === CE.envSel ? 9 : 6) * dpr, 0, 2 * Math.PI); g.fill(); });
      }
      if (CE.draft.intro_ms) line(CE.draft.intro_ms, cssVar('--status-warning-fg'), 2);
      if (CE.draft.outro_ms) line(CE.draft.outro_ms, cssVar('--status-error-fg'), 2);
    }
    line(CE.cursor, cssVar('--text-strong'), 2);
    const pv = S && S.preview;
    if (pv && pv.state !== 'idle' && (pv.key === CE.t.key || pv.item === CE.t.item)) {
      let t = pv.elapsed_ms + (pv.state === 'playing' ? Math.max(0, performance.now() - S_AT) : 0);
      if (CE.view === 'src') { const m = mixToSrc(CE.layout, t); t = m && m.seg === CE.seg ? m.t : null; }
      if (t != null) line(t, cssVar('--status-running-fg'), 2);
    }
  }

  function clipPaintMeta() {
    if (!CE || !CE.box) return;
    const m = CE.box.querySelector('.cart-clip-time');
    if (m) m.textContent = T('clip_cursor', 'Curseur {c}').replace('{c}', fmtMs(CE.cursor))
      + (CE.sel ? ' · ' + T('clip_sel', 'Sélection {a} – {b}').replace('{a}', fmtMs(Math.min(...CE.sel))).replace('{b}', fmtMs(Math.max(...CE.sel))) : '')
      + (CE.win ? ' · ' + T('clip_window', 'Vue {a} – {b}').replace('{a}', fmtMs(CE.win[0])).replace('{b}', fmtMs(CE.win[1])) : '');
    const e = CE.box.querySelector('.cart-clip-err');
    if (e) { e.textContent = CE.err; e.hidden = !CE.err; }
    const d = CE.box.querySelector('.cart-clip-dur');
    if (d) d.textContent = CE.view === 'mix' ? T('clip_dur_edit', 'Durée montée {d}').replace('{d}', fmtMs(CE.dom))
      : T('clip_dur_file', 'Fichier {d}').replace('{d}', fmtMs(CE.dom));
  }

  function shapeSel(name, v) {
    return `<select data-cf="${name}">${SHAPES.map(x => `<option value="${x}"${v === x ? ' selected' : ''}>${esc(T('shape_' + x, x))}</option>`).join('')}</select>`;
  }
  function numIn(name, v, step, min, max) {
    return `<input type="number" data-cf="${name}" step="${step}"${min != null ? ` min="${min}"` : ''}${max != null ? ` max="${max}"` : ''} value="${v}">`;
  }

  function clipRender() {
    if (!CE || !CE.box) return;
    const d = CE.draft, ed = d.edit, s = segOf(CE.seg), src = CE.view === 'src';
    const media = (S && (CE.t.key ? S.key_media : S.item_media) || {})[CE.id] || {};
    const box = CE.box;
    box.classList.toggle('is-picking', !!CE.picking);
    if (CE.picking) {
      box.innerHTML = `<div class="cart-clip-head"><span>${esc(T('clip_pick_hint', 'Choisissez dans le panneau Médias le son à ajouter à la suite.'))}</span>
        <span class="cart-spacer"></span><button type="button" class="cart-btn" data-ca="pick-cancel">${esc(T('cancel', 'Annuler'))}</button></div>`;
      box.querySelector('[data-ca="pick-cancel"]').onclick = () => { CE.picking = false; libTarget = null; paintLibHead(); clipRender(); clipFetchSoon(); };
      return;
    }
    const conf = CE.confirm === 'abandon' ? `<div class="cart-confirm">
        <p>${esc(T('clip_abandon_q', 'Abandonner les modifications du clip ?'))}</p>
        <div class="cart-form-acts"><button type="button" class="cart-btn" data-ca="conf-no">${esc(T('clip_keep', 'Continuer l\'édition'))}</button>
          <button type="button" class="cart-btn cart-danger" data-ca="conf-yes">${esc(T('clip_abandon', 'Abandonner'))}</button></div></div>`
      : CE.confirm === 'newmedia' ? `<div class="cart-confirm">
        <p>${esc(T('clip_new_help', 'Le montage est rendu dans un NOUVEAU fichier WAV de la base des médias, à côté du premier son ; l\'original n\'est pas touché.'))}</p>
        <div class="cart-form">${field(T('clip_new_name', 'Nom du nouveau média'), `<input type="text" class="cart-clip-newname" maxlength="80" value="${esc(CE.newName || (CE.title + ' montage'))}">`)}</div>
        <p class="cart-note cart-clip-jobmsg">${esc(CE.jobMsg || '')}</p>
        <div class="cart-form-acts"><button type="button" class="cart-btn" data-ca="conf-no">${esc(T('close', 'Fermer'))}</button>
          ${CE.job && CE.job.state === 'done' ? `<button type="button" class="cart-btn cart-primary" data-ca="use-new">${esc(T('clip_use_new', 'Utiliser le nouveau média ici'))}</button>`
            : `<button type="button" class="cart-btn cart-primary" data-ca="render"${CE.job && CE.job.state === 'running' ? ' disabled' : ''}>${esc(T('clip_new_go', 'Créer le média'))}</button>`}</div></div>` : '';
    box.innerHTML = `
      <div class="cart-clip-head">
        <strong class="cart-clip-title">✂ ${esc(CE.title || T('untitled', 'Sans titre'))}</strong>
        <span class="cart-clip-dur"></span>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn" data-ca="undo" aria-label="${esc(T('clip_undo', 'Annuler la dernière modification'))}"${CE.undo.length ? '' : ' disabled'}>↶</button>
        <button type="button" class="cart-btn" data-ca="redo" aria-label="${esc(T('clip_redo', 'Rétablir'))}"${CE.redo.length ? '' : ' disabled'}>↷</button>
        <button type="button" class="cart-btn" data-ca="abandon">${esc(T('clip_abandon', 'Abandonner'))}</button>
        <button type="button" class="cart-btn" data-ca="newmedia">${esc(T('clip_new', 'Enregistrer comme nouveau média…'))}</button>
        <button type="button" class="cart-btn cart-primary" data-ca="save"${clipDirty() ? '' : ' disabled'}>${esc(T('save', 'Enregistrer'))}</button>
      </div>
      ${conf}
      <div class="cart-clip-bar">
        <div class="cart-seg" role="group">
          <button type="button" class="cart-btn${src ? '' : ' is-on'}" data-ca="view-mix">${esc(T('clip_view_mix', 'Montage'))}</button>
          <button type="button" class="cart-btn${src ? ' is-on' : ''}" data-ca="view-src">${esc(T('clip_view_src', 'Source'))}</button>
        </div>
        <div class="cart-clip-segs">${ed.segments.map((x, i) => `<button type="button" class="cart-btn${i === CE.seg ? ' is-on' : ''}" data-segpick="${i}">${i + 1} · ${esc(segName(i))}</button>`).join('')}
          <button type="button" class="cart-btn" data-ca="seg-add">+ ${esc(T('clip_seg_add', 'Ajouter un son à la suite'))}</button></div>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn" data-ca="zoom-out" aria-label="${esc(T('clip_zoom_out', 'Dézoomer'))}">−</button>
        <button type="button" class="cart-btn" data-ca="zoom-in" aria-label="${esc(T('clip_zoom_in', 'Zoomer'))}">+</button>
        <button type="button" class="cart-btn" data-ca="zoom-fit">${esc(T('clip_zoom_fit', 'Tout'))}</button>
        <button type="button" class="cart-btn" data-ca="pan-l" aria-label="${esc(T('clip_pan_left', 'Vers le début'))}">◀</button>
        <button type="button" class="cart-btn" data-ca="pan-r" aria-label="${esc(T('clip_pan_right', 'Vers la fin'))}">▶</button>
      </div>
      <canvas class="cart-clip-wave"></canvas>
      <p class="cart-note cart-clip-time"></p>
      <p class="cart-note cart-clip-err" role="alert" hidden></p>
      <div class="cart-clip-tools">
        <button type="button" class="cart-btn" data-ca="sel-a">${esc(T('clip_sel_a', 'Début de sélection'))}</button>
        <button type="button" class="cart-btn" data-ca="sel-b">${esc(T('clip_sel_b', 'Fin de sélection'))}</button>
        <button type="button" class="cart-btn" data-ca="sel-clear"${CE.sel ? '' : ' disabled'}>${esc(T('clip_sel_clear', 'Effacer la sélection'))}</button>
        ${src ? `<button type="button" class="cart-btn" data-ca="in">${esc(T('clip_set_in', 'In au curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="out">${esc(T('clip_set_out', 'Out au curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="cut"${CE.sel ? '' : ' disabled'}>${esc(T('clip_cut', 'Couper la sélection'))}</button>`
        : `<button type="button" class="cart-btn" data-ca="env-add">${esc(T('clip_env_add', 'Point de volume au curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="env-del"${CE.envSel != null ? '' : ' disabled'}>${esc(T('clip_env_del', 'Supprimer le point'))}</button>
        <button type="button" class="cart-btn" data-ca="fade-in-here">${esc(T('clip_fade_in_here', 'Fondu d\'entrée jusqu\'au curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="fade-out-here">${esc(T('clip_fade_out_here', 'Fondu de sortie depuis le curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="intro-here">${esc(T('clip_intro_here', 'Fin de l\'intro au curseur'))}</button>
        <button type="button" class="cart-btn" data-ca="outro-here">${esc(T('clip_outro_here', 'Début de la fin au curseur'))}</button>`}
      </div>
      <div class="cart-clip-tools cart-clip-pfl">
        <strong>🎧</strong>
        <button type="button" class="cart-btn" data-pv="all">${esc(T('clip_pv_all', 'Tout le clip'))}</button>
        <button type="button" class="cart-btn" data-pv="sel"${CE.sel ? '' : ' disabled'}>${esc(T('clip_pv_sel', 'La sélection'))}</button>
        <button type="button" class="cart-btn" data-pv="cursor">${esc(T('clip_pv_cursor', 'Autour du curseur'))}</button>
        ${src ? `<button type="button" class="cart-btn" data-pv="in">${esc(T('clip_pv_in', 'Autour de In'))}</button>
        <button type="button" class="cart-btn" data-pv="out">${esc(T('clip_pv_out', 'Autour de Out'))}</button>` : ''}
        <label class="cart-clip-small">${esc(T('clip_pre', 'Avant (s)'))} <input type="number" data-cv="pre" min="0" max="30" step="0.5" value="${CE.pre}"></label>
        <label class="cart-clip-small">${esc(T('clip_post', 'Après (s)'))} <input type="number" data-cv="post" min="0" max="30" step="0.5" value="${CE.post}"></label>
        <button type="button" class="cart-btn" data-pv="stop" aria-label="${esc(T('pfl_stop', 'Arrêter la pré-écoute'))}">■</button>
      </div>
      <h3 class="cart-h">${esc(T('clip_sec_seg', 'Son {n} : {t}').replace('{n}', CE.seg + 1).replace('{t}', segName(CE.seg)))}</h3>
      <div class="cart-form">
        ${field(T('clip_in', 'In (ms dans le fichier)'), numIn('in_ms', s.in_ms, 10, 0))}
        ${field(T('clip_out', 'Out (ms dans le fichier, 0 = fin)'), numIn('out_ms', s.out_ms, 10, 0))}
        ${CE.seg > 0 ? field(T('clip_xfade', 'Fondu enchaîné avec le son précédent (ms)'), numIn('xfade_ms', s.xfade_ms, 50, 0, 30000))
          + field(T('clip_xfade_shape', 'Forme du fondu enchaîné'), shapeSel('xfade_shape', s.xfade_shape)) : ''}
      </div>
      <ul class="cart-clip-list">${s.cuts.map((c, i) => `<li><span>${esc(T('clip_cut_n', 'Coupe {a} – {b}').replace('{a}', fmtMs(c[0])).replace('{b}', fmtMs(c[1])))}</span>
          <button type="button" class="cart-btn" data-cutpv="${i}">🎧 ${esc(T('clip_pv_cut', 'Écouter le raccord'))}</button>
          <button type="button" class="cart-btn cart-danger" data-cutdel="${i}" aria-label="${esc(T('delete', 'Supprimer'))}">✕</button></li>`).join('')}</ul>
      ${ed.segments.length > 1 ? `<div class="cart-form-acts">
        <button type="button" class="cart-btn" data-ca="seg-left">${esc(T('clip_seg_left', 'Placer plus tôt'))}</button>
        <button type="button" class="cart-btn" data-ca="seg-right">${esc(T('clip_seg_right', 'Placer plus tard'))}</button>
        <button type="button" class="cart-btn cart-danger" data-ca="seg-del">${esc(T('clip_seg_del', 'Retirer ce son du montage'))}</button></div>` : ''}
      <h3 class="cart-h">${esc(T('clip_sec_mix', 'Montage'))}</h3>
      <div class="cart-form">
        ${field(T('clip_fade_in', 'Fondu d\'entrée (ms)'), numIn('fade_in_ms', ed.fade_in_ms, 50, 0, 60000))}
        ${field(T('clip_fade_in_shape', 'Forme du fondu d\'entrée'), shapeSel('fade_in_shape', ed.fade_in_shape))}
        ${field(T('clip_fade_out', 'Fondu de sortie (ms)'), numIn('fade_out_ms', ed.fade_out_ms, 50, 0, 60000))}
        ${field(T('clip_fade_out_shape', 'Forme du fondu de sortie'), shapeSel('fade_out_shape', ed.fade_out_shape))}
        ${field(T('f_gain', 'Gain de la touche (dB)'), numIn('gain_db', d.gain_db, 0.5, -40, 20))}
        ${field(T('f_intro', 'Fin de l\'intro (ms)'), numIn('intro_ms', d.intro_ms, 100, 0))}
        ${field(T('f_outro', 'Début de la fin (ms)'), numIn('outro_ms', d.outro_ms, 100, 0))}
      </div>
      <p class="cart-note">${esc(media.norm_db != null ? T('clip_norm', 'Normalisation R128 : chaque son est amené à la cible du cartouchier ({g} dB pour le premier) — réglage dans Réglages, Niveaux.').replace('{g}', media.norm_db)
        : T('clip_norm_off', 'Normalisation R128 : selon Réglages, Niveaux (chaque son du montage à la cible).'))}</p>
      <ul class="cart-clip-list">${ed.envelope.map((p, i) => `<li class="${i === CE.envSel ? 'is-on' : ''}">
          <span>${esc(T('clip_env_n', 'Point {n}').replace('{n}', i + 1))}</span>
          <label class="cart-clip-small">ms <input type="number" data-envt="${i}" step="10" min="0" value="${p[0]}"></label>
          <label class="cart-clip-small">dB <input type="number" data-envdb="${i}" step="0.5" min="-60" max="12" value="${p[1]}"></label>
          <button type="button" class="cart-btn cart-danger" data-envdel="${i}" aria-label="${esc(T('delete', 'Supprimer'))}">✕</button></li>`).join('')}</ul>`;
    clipWire();
    clipPaintMeta();
    requestAnimationFrame(clipDraw);
  }

  function clipWire() {
    const box = CE.box, on = (sel, fn) => box.querySelectorAll(sel).forEach(b => { b.onclick = () => fn(b); });
    const cv = box.querySelector('.cart-clip-wave');
    if (cv) wireCanvas(cv);
    const act = {
      undo: () => undoRedo(CE.undo, CE.redo), redo: () => undoRedo(CE.redo, CE.undo),
      abandon: () => clipClose(false), save: () => clipSave(),
      newmedia: () => { CE.confirm = 'newmedia'; CE.job = null; CE.jobMsg = ''; clipRender(); },
      'conf-no': () => { CE.confirm = null; clipRender(); }, 'conf-yes': () => clipClose(true),
      render: () => clipRenderMedia(), 'use-new': () => clipUseNew(),
      'view-mix': () => { if (CE.view !== 'mix') { CE.view = 'mix'; CE.sel = null; CE.cursor = 0; clipRender(); clipFetch(true); } },
      'view-src': () => { if (CE.view !== 'src') { CE.view = 'src'; CE.sel = null; CE.cursor = segOf(CE.seg).in_ms; clipRender(); clipFetch(true); } },
      'zoom-in': () => zoomAt(CE.cursor >= CE.win[0] && CE.cursor <= CE.win[1] ? CE.cursor : (CE.win[0] + CE.win[1]) / 2, 0.5),
      'zoom-out': () => zoomAt((CE.win[0] + CE.win[1]) / 2, 2), 'zoom-fit': () => setWin(0, CE.dom),
      'pan-l': () => { const w = CE.win[1] - CE.win[0]; setWin(CE.win[0] - w / 2, CE.win[1] - w / 2); },
      'pan-r': () => { const w = CE.win[1] - CE.win[0]; setWin(CE.win[0] + w / 2, CE.win[1] + w / 2); },
      'sel-a': () => { CE.sel = [CE.cursor, CE.sel ? CE.sel[1] : CE.cursor]; clipRender(); },
      'sel-b': () => { CE.sel = [CE.sel ? CE.sel[0] : CE.cursor, CE.cursor]; clipRender(); },
      'sel-clear': () => { CE.sel = null; clipRender(); },
      in: () => { const s = segOf(CE.seg); if (s.out_ms && CE.cursor >= s.out_ms) return TOAST(T('clip_bad_in', 'In après Out'), 'warning');
        change(() => { s.in_ms = Math.round(CE.cursor); }); },
      out: () => { const s = segOf(CE.seg); if (CE.cursor <= s.in_ms) return TOAST(T('clip_bad_out', 'Out avant In'), 'warning');
        change(() => { s.out_ms = Math.round(CE.cursor); }); },
      cut: () => { const a = Math.round(Math.min(...CE.sel)), b = Math.round(Math.max(...CE.sel)); if (b - a < 1) return;
        change(() => { segOf(CE.seg).cuts.push([a, b]); segOf(CE.seg).cuts.sort((x, y) => x[0] - y[0]); }); CE.sel = null; },
      'env-add': () => { const env = CE.draft.edit.envelope, t = Math.round(CE.cursor);
        if (env.some(p => p[0] === t)) return;
        change(d => { d.edit.envelope.push([t, Math.round(envDbAt(env, t) * 2) / 2]); d.edit.envelope.sort((x, y) => x[0] - y[0]); });
        CE.envSel = CE.draft.edit.envelope.findIndex(p => p[0] === t); clipRender(); },
      'env-del': () => { const i = CE.envSel; CE.envSel = null; change(d => { d.edit.envelope.splice(i, 1); }); },
      'fade-in-here': () => change(d => { d.edit.fade_in_ms = Math.round(CE.cursor); }),
      'fade-out-here': () => change(d => { d.edit.fade_out_ms = Math.round(Math.max(0, CE.dom - CE.cursor)); }),
      'intro-here': () => change(d => { d.intro_ms = Math.round(CE.cursor); }, false),
      'outro-here': () => change(d => { d.outro_ms = Math.round(CE.cursor); }, false),
      'seg-add': () => { CE.picking = true; clipRender();
        openLibrary({clip: (media, title) => clipAddSegment(media, title)}, T('clip_pick', 'Son à ajouter au montage')); },
      'seg-left': () => moveSeg(-1), 'seg-right': () => moveSeg(1),
      'seg-del': () => { const i = CE.seg; change(d => { materialize(); d.edit.segments.splice(i, 1); }); CE.seg = Math.max(0, i - 1); clipRender(); },
    };
    on('[data-ca]', b => { const f = act[b.dataset.ca]; if (f) f(); });
    on('[data-segpick]', b => { CE.seg = +b.dataset.segpick; CE.sel = null; if (CE.view === 'src') { CE.cursor = segOf(CE.seg).in_ms; clipRender(); clipFetch(true); } else clipRender(); });
    on('[data-pv]', b => clipPreview(b.dataset.pv));
    on('[data-cutpv]', b => { const c = segOf(CE.seg).cuts[+b.dataset.cutpv]; clipPreviewAt(srcToMix(CE.layout, CE.seg, c[1])); });
    on('[data-cutdel]', b => change(() => { segOf(CE.seg).cuts.splice(+b.dataset.cutdel, 1); }));
    on('[data-envdel]', b => { CE.envSel = null; change(d => { d.edit.envelope.splice(+b.dataset.envdel, 1); }); });
    box.querySelectorAll('[data-cf]').forEach(i => {
      i.onchange = () => {
        const f = i.dataset.cf, v = i.tagName === 'SELECT' ? i.value : (+i.value || 0);
        change(d => {
          if (f === 'gain_db' || f === 'intro_ms' || f === 'outro_ms') d[f] = v;
          else if (f in d.edit) d.edit[f] = v;
          else segOf(CE.seg)[f] = v;
        }, !(f === 'intro_ms' || f === 'outro_ms'));
      };
    });
    box.querySelectorAll('[data-envt],[data-envdb]').forEach(i => {
      i.onchange = () => change(d => {
        const k = i.dataset.envt != null ? +i.dataset.envt : +i.dataset.envdb;
        if (i.dataset.envt != null) d.edit.envelope[k][0] = Math.max(0, Math.round(+i.value || 0));
        else d.edit.envelope[k][1] = Math.max(ENV_DB_MIN, Math.min(ENV_DB_MAX, +i.value || 0));
        d.edit.envelope.sort((x, y) => x[0] - y[0]);
      });
    });
    box.querySelectorAll('[data-cv]').forEach(i => { i.onchange = () => { CE[i.dataset.cv] = Math.max(0, Math.min(30, +i.value || 0)); }; });
    const nn = box.querySelector('.cart-clip-newname');
    if (nn) nn.oninput = () => { CE.newName = nn.value; };
  }
  function moveSeg(delta) {
    const i = CE.seg, j = i + delta, n = CE.draft.edit.segments.length;
    if (j < 0 || j >= n) return;
    change(d => { materialize(); const s = d.edit.segments; s.splice(j, 0, s.splice(i, 1)[0]); });
    CE.seg = j; clipRender();
  }
  function clipAddSegment(media, title) {
    if (!CE) return;
    CE.picking = false;
    change(d => { materialize(); d.edit.segments.push({media, in_ms: 0, out_ms: 0, cuts: [], xfade_ms: 0, xfade_shape: 'eq'}); });
    CE.seg = CE.draft.edit.segments.length - 1;
    clipRender();
    if (media.asset && !media.file) fetchToNode(media.asset, title);       // not on this node yet: fetched
  }

  // ── pre-listen inside the editor (always on the EDITED timeline: a source point is mapped) ──
  function clipPreviewAt(c) {
    return clipPreviewRange(Math.max(0, c - CE.pre * 1000), c + CE.post * 1000);
  }
  function clipPreviewRange(a, b) {
    a = Math.max(0, Math.round(a)); b = Math.round(b);
    if (b && b <= a) b = a + 1;
    return previewPlay(Object.assign(tb(), {edit: CE.draft.edit, gain_db: CE.draft.gain_db, start_ms: a, end_ms: b}));
  }
  function clipPreview(what) {
    const src = CE.view === 'src', s = segOf(CE.seg), map = t => src ? srcToMix(CE.layout, CE.seg, t) : t;
    if (what === 'stop') return previewStop();
    if (what === 'all') return clipPreviewRange(0, 0);
    if (what === 'sel' && CE.sel) return clipPreviewRange(map(Math.min(...CE.sel)), map(Math.max(...CE.sel)));
    if (what === 'cursor') return clipPreviewAt(map(CE.cursor));
    if (what === 'in') return clipPreviewAt(srcToMix(CE.layout, CE.seg, s.in_ms));
    if (what === 'out') return clipPreviewAt(srcToMix(CE.layout, CE.seg, s.out_ms || 1e12));
    return null;
  }

  async function clipSave() {
    const d = CE.draft, body = {id: CE.id, edit: d.edit, gain_db: d.gain_db, intro_ms: d.intro_ms, outro_ms: d.outro_ms};
    let r;
    if (CE.t.key) {
      const f = findKeyDoc(CE.t.key);
      if (!f) return TOAST(T('err_unknown_key', 'Touche inconnue'), 'error');
      r = await post('/key', {page: f.p.id, key: body});
    } else r = await post('/playlist', {op: 'update', item: body});
    if (r && CE) { CE.saved = JSON.stringify(CE.draft); clipClose(true); TOAST(T('clip_saved', 'Montage enregistré'), 'info'); }
  }

  // « Save as new media »: rendered by the container (24-bit WAV + sidecar carrying the identity,
  // origin="cartouchier", derived_from), then the node's media scan is asked for so the base sees it.
  async function clipRenderMedia() {
    const name = (CE.newName || (CE.title + ' montage')).trim();
    CE.job = {state: 'running', pct: 0}; CE.jobMsg = T('clip_rendering', 'Rendu en cours…'); clipRender();
    const d = await post('/montage/render', Object.assign(tb(), {edit: CE.draft.edit, gain_db: CE.draft.gain_db, name}));
    if (!d || !CE) { if (CE) { CE.job = null; CE.jobMsg = ''; clipRender(); } return; }
    for (let i = 0; i < 1200 && CE; i++) {
      await sleep(500);
      let j;
      try { j = await jget('/montage/job?id=' + encodeURIComponent(d.job)); } catch (e) { j = {state: 'failed', error: e.code}; }
      if (!CE) return;
      CE.job = j;
      if (j.state === 'running') { CE.jobMsg = T('clip_rendering_pct', 'Rendu en cours… {p} %').replace('{p}', j.pct || 0); clipPaintJob(); continue; }
      if (j.state === 'done') {
        CE.jobMsg = T('clip_new_done', 'Nouveau média « {f} » créé ({d}).').replace('{f}', mediaName(baseName(j.file))).replace('{d}', fmtMs(j.duration_ms))
          + (j.clipped ? ' ' + T('clip_new_clipped', '{n} échantillon(s) écrêté(s) : baissez le gain.').replace('{n}', j.clipped) : '');
        fetch('/api/media/scan', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({vmid: VMID})}).catch(() => {});
      } else CE.jobMsg = T('err_' + (j.error || 'render_failed'), T('err_render_failed', 'Rendu impossible'));
      clipRender();
      return;
    }
  }
  function clipPaintJob() { const m = CE && CE.box.querySelector('.cart-clip-jobmsg'); if (m) m.textContent = CE.jobMsg || ''; }
  async function clipUseNew() {
    const j = CE.job, media = {asset: j.asset, file: j.file};
    let r;
    if (CE.t.key) { const f = findKeyDoc(CE.t.key); r = f && await post('/key', {page: f.p.id, key: {id: CE.id, media, edit: null, gain_db: 0}}); }
    else r = await post('/playlist', {op: 'update', item: {id: CE.id, media, edit: null, gain_db: 0}});
    if (r && CE) clipClose(true);
  }

  // ── SAVE / RECALL (0.1.3) ───────────────────────────────────────────────────────────────────
  // Saves live in the COMMON library of the installation (orchestrator: plugin_store, generic kinded
  // presets — app/plugin_library.py), one list per KIND: the grid (every page), the playlist, one page.
  // Recalling replaces only that part, never what plays. Rights = the presets': list and export with
  // the login, save / delete / import / recall with `plugins.operate`. Every call is outside the
  // plugin proxy: skipped in public mode (the button is hidden there).
  const LIB_T = 'cartouchier';
  const canOperate = () => !!(window.hasPerm && window.hasPerm('plugins.operate'));
  let SV = {kind: null, confirm: null, report: null, lists: {}};
  const KIND_LABEL = {grid: () => T('sv_kind_grid', 'Grille'), playlist: () => T('sv_kind_playlist', 'Playlist'),
                      page: () => T('sv_kind_page', 'Page')};

  async function svFetch(url, opts) {
    let r, d = {};
    try {
      r = await fetch(url, Object.assign({cache: 'no-store'}, opts || {}));
      d = await r.json().catch(() => ({}));
    } catch (e) { return {ok: false, status: 0, data: {error: 'unreachable'}}; }
    return {ok: r.ok && d.ok !== false, status: r.status, data: d};
  }
  const svJson = (method, url, body) => svFetch(url, {method, headers: {'Content-Type': 'application/json'},
                                                      body: JSON.stringify(body || {})});
  function svErr(res) {
    if (res.status === 403) return T('err_forbidden', 'Droit insuffisant');
    return errText(res.data, res.status);
  }
  function fmtWhen(s) {
    if (!s) return '';
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleString(undefined, {dateStyle: 'short', timeStyle: 'short'});
  }
  function svSummary(sv) {
    const m = sv.summary || {}, parts = [];
    if (m.pages != null) parts.push(T('sv_n_pages', '{n} page(s)').replace('{n}', m.pages));
    if (m.keys != null) parts.push(T('sv_n_keys', '{n} touche(s)').replace('{n}', m.keys));
    if (m.items != null) parts.push(T('sv_n_items', '{n} élément(s)').replace('{n}', m.items));
    return parts.join(' · ');
  }
  async function svLoad(kind) {
    const res = await svFetch('/api/plugins/' + LIB_T + '/library?kind=' + encodeURIComponent(kind));
    SV.lists[kind] = res.ok ? (res.data.saves || []) : [];
    return res;
  }
  function openSaves() {
    if (PUBLIC) return;
    const box = $('.cart-saves');
    if (!box.hidden) { box.hidden = true; return; }
    SV = {kind: null, confirm: null, report: null, lists: {}, sig: DOC ? DOC.mode + '|' + DOC.page : null};
    box.hidden = false;
    svRefresh();
  }
  async function svRefresh() {
    const box = $('.cart-saves');
    if (!box || box.hidden || !DOC) return;
    const kinds = DOC.mode === 'playlist' ? ['playlist'] : ['grid', 'page'];
    await Promise.all(kinds.map(svLoad));
    svPaint();
  }
  function svPaint() {
    const box = $('.cart-saves');
    if (!box || box.hidden || !DOC) return;
    const op = canOperate();
    const pg = curPage();
    const pgName = pageLabel(pg, DOC.pages.indexOf(pg));
    const kinds = DOC.mode === 'playlist' ? ['playlist'] : ['grid', 'page'];
    const saveLbl = {grid: T('sv_save_grid', 'Enregistrer la grille sous…'),
                     playlist: T('sv_save_playlist', 'Enregistrer la playlist sous…'),
                     page: T('sv_save_page', 'Enregistrer cette page sous…')};
    const title = {grid: T('sv_h_grid', 'Grille (toutes les pages)'), playlist: T('sv_h_playlist', 'Playlist'),
                   page: T('sv_h_page', 'Page affichée : {p}').replace('{p}', pgName)};
    const rows = kind => (SV.lists[kind] || []).map(sv => {
      const c = SV.confirm && SV.confirm.id === sv.id ? SV.confirm : null;
      const recallBtns = kind === 'page'
        ? `<button type="button" class="cart-btn" data-sv="recall" data-id="${sv.id}">${esc(T('sv_recall_here', 'Rappeler ici'))}</button>
           <button type="button" class="cart-btn" data-sv="add" data-id="${sv.id}">${esc(T('sv_recall_add', 'Ajouter comme nouvelle page'))}</button>`
        : `<button type="button" class="cart-btn" data-sv="recall" data-id="${sv.id}">${esc(T('sv_recall', 'Rappeler'))}</button>`;
      return `<li class="cart-sv-row" data-id="${sv.id}">
        <div class="cart-sv-info"><strong>${esc(sv.name)}</strong>
          <span class="cart-note">${esc([fmtWhen(sv.updated_at || sv.created_at), sv.author, svSummary(sv)].filter(Boolean).join(' · '))}</span>
          ${sv.missing_media ? `<span class="cart-sv-miss">${esc(T('sv_missing_n', '{n} son(s) introuvable(s) dans la base').replace('{n}', sv.missing_media))}</span>` : ''}</div>
        <div class="cart-sv-acts">
          ${op ? recallBtns : ''}
          <button type="button" class="cart-btn" data-sv="export" data-id="${sv.id}">${esc(T('sv_export', 'Exporter'))}</button>
          ${op ? `<button type="button" class="cart-btn cart-danger" data-sv="delete" data-id="${sv.id}">${esc(T('delete', 'Supprimer'))}</button>` : ''}
        </div>
        ${c ? `<div class="cart-sv-confirm" role="alert"><span>${esc(c.text)}</span>
          <button type="button" class="cart-btn cart-primary" data-sv="yes">${esc(c.yes)}</button>
          <button type="button" class="cart-btn" data-sv="no">${esc(T('cancel', 'Annuler'))}</button></div>` : ''}
      </li>`;
    }).join('') || `<li class="cart-note">${esc(T('sv_none', 'Aucune sauvegarde de ce genre.'))}</li>`;
    const nameConfirm = SV.confirm && SV.confirm.overwrite ? SV.confirm : null;
    box.innerHTML = `
      <div class="cart-panel-head"><strong>${esc(T('sv_title', 'Sauvegardes'))}</strong>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-sv-close">${esc(T('close', 'Fermer'))}</button></div>
      <p class="cart-note">${esc(T('sv_hint', 'Bibliothèque commune à l\'installation : rappelable depuis tout cartouchier. Un rappel ne coupe rien : ce qui joue continue jusqu\'au bout.'))}</p>
      ${SV.report ? `<div class="cart-sv-report" role="status">${SV.report}</div>` : ''}
      ${kinds.map(kind => `
        <h3 class="cart-h">${esc(title[kind])}</h3>
        ${op ? `<div class="cart-sv-save" data-kind="${kind}">
          <label class="cart-field"><span>${esc(T('sv_name', 'Nom'))}</span><input type="text" maxlength="60" class="cart-sv-name"></label>
          <button type="button" class="cart-btn cart-primary" data-sv="save" data-kind="${kind}">${esc(saveLbl[kind])}</button>
          ${nameConfirm && nameConfirm.kind === kind ? `<div class="cart-sv-confirm" role="alert"><span>${esc(nameConfirm.text)}</span>
            <button type="button" class="cart-btn cart-primary" data-sv="yes">${esc(nameConfirm.yes)}</button>
            <button type="button" class="cart-btn" data-sv="no">${esc(T('cancel', 'Annuler'))}</button></div>` : ''}
        </div>` : ''}
        <ul class="cart-sv-list">${rows(kind)}</ul>
        <div class="cart-form-acts">
          <button type="button" class="cart-btn" data-sv="export_all" data-kind="${kind}">${esc(T('sv_export_all', 'Tout exporter ({k})').replace('{k}', KIND_LABEL[kind]()))}</button>
          ${op ? `<label class="cart-btn cart-sv-import">${esc(T('sv_import', 'Importer un fichier…'))}<input type="file" accept="application/json,.json" data-kind="${kind}" hidden></label>
          <label class="cart-sw"><input type="checkbox" class="ctl-switch cart-sv-overwrite"> ${esc(T('sv_import_overwrite', 'Remplacer les noms déjà pris'))}</label>` : ''}
        </div>`).join('')}`;
    sectionize(box);
    box.querySelector('.cart-sv-close').onclick = () => { box.hidden = true; };
    box.querySelectorAll('[data-sv]').forEach(b => { b.onclick = () => svAction(b.dataset.sv, b); });
    box.querySelectorAll('.cart-sv-import input').forEach(inp => { inp.onchange = () => svImport(inp); });
    if (nameConfirm) {
      const inp = box.querySelector(`.cart-sv-save[data-kind="${nameConfirm.kind}"] .cart-sv-name`);
      if (inp) inp.value = nameConfirm.name;
    }
  }
  // The panel follows the mode and the page on screen (each mode has its own saves).
  function svFollow() {
    const box = $('.cart-saves');
    if (!box || box.hidden || !DOC) return;
    const sig = DOC.mode + '|' + DOC.page;
    if (SV.sig === sig) return;
    const first = SV.sig == null;
    SV.sig = sig;
    if (!first) { SV.confirm = null; svRefresh(); }
  }
  function svFind(id) {
    for (const k in SV.lists) { const s = (SV.lists[k] || []).find(x => x.id === +id); if (s) return s; }
    return null;
  }
  async function svAction(what, b) {
    if (what === 'no') { SV.confirm = null; return svPaint(); }
    if (what === 'yes') { const c = SV.confirm; SV.confirm = null; svPaint(); return c && c.run(); }
    if (what === 'save') {
      const kind = b.dataset.kind;
      const name = (b.parentElement.querySelector('.cart-sv-name').value || '').trim();
      if (!name) { TOAST(T('err_bad_name', 'Nom invalide (1 à 60 caractères)'), 'warning'); return; }
      return svSave(kind, name, false);
    }
    if (what === 'export_all') return svExport(b.dataset.kind, null);
    const sv = svFind(b.dataset.id);
    if (!sv) return;
    if (what === 'export') return svExport(sv.kind, sv);
    if (what === 'delete') {
      SV.confirm = {id: sv.id, text: T('sv_confirm_delete', 'Supprimer « {n} » de la bibliothèque commune ?').replace('{n}', sv.name),
                    yes: T('delete', 'Supprimer'), run: () => svDelete(sv)};
      return svPaint();
    }
    if (what === 'recall' || what === 'add') {
      const pg = curPage();
      const pgName = pageLabel(pg, DOC.pages.indexOf(pg));
      const text = sv.kind === 'grid' ? T('sv_confirm_grid', 'Remplacer toute la grille par « {n} » ? Ce qui joue continue.')
        : sv.kind === 'playlist' ? T('sv_confirm_playlist', 'Remplacer la playlist par « {n} » ? Ce qui joue continue.')
        : what === 'add' ? T('sv_confirm_add', 'Ajouter « {n} » comme nouvelle page ?')
        : T('sv_confirm_page', 'Remplacer la page « {p} » par « {n} » ? Ce qui joue continue.').replace('{p}', pgName);
      const body = sv.kind === 'page' ? (what === 'add' ? {add: true} : {page: pg.id}) : {};
      SV.confirm = {id: sv.id, text: text.replace('{n}', sv.name), yes: T('sv_recall', 'Rappeler'), run: () => svRecall(sv, body)};
      return svPaint();
    }
  }
  async function svSave(kind, name, overwrite) {
    const body = {kind, name, overwrite};
    if (kind === 'page') body.page = curPage().id;
    const res = await svJson('POST', '/api/containers/' + VMID + '/library', body);
    if (res.status === 409 && res.data.error === 'name_taken' && !overwrite) {
      SV.confirm = {kind, name, overwrite: true, yes: T('sv_replace', 'Remplacer'),
                    text: T('sv_confirm_overwrite', '« {n} » existe déjà : le remplacer ?').replace('{n}', name),
                    run: () => svSave(kind, name, true)};
      return svPaint();
    }
    if (!res.ok) { TOAST(svErr(res), 'error'); return; }
    TOAST(T('sv_saved', 'Enregistré : {n}').replace('{n}', name), 'success');
    await svLoad(kind); svPaint();
  }
  async function svDelete(sv) {
    const res = await svFetch('/api/plugins/' + LIB_T + '/library/' + sv.id, {method: 'DELETE'});
    if (!res.ok) { TOAST(svErr(res), 'error'); return; }
    await svLoad(sv.kind); svPaint();
  }
  function missingHtml(list, count) {
    if (!count) return '';
    const names = (list || []).slice(0, 12).map(m => esc(m.title || mediaName(baseName(m.file)) || m.asset || '?')).join(', ');
    return `<p class="cart-sv-miss">${esc(T('sv_missing_report', '{n} son(s) introuvable(s) — touches gardées, son marqué manquant :').replace('{n}', count))} ${names}${count > 12 ? ' …' : ''}</p>`;
  }
  async function svRecall(sv, body) {
    const res = await svJson('POST', '/api/containers/' + VMID + '/library/' + sv.id + '/recall', body);
    if (!res.ok) { TOAST(svErr(res), 'error'); return; }
    const r = res.data.recall || {};
    const lines = [esc(T('sv_recalled', 'Rappelé : {n}').replace('{n}', sv.name))];
    if (r.relocated) lines.push(esc(T('sv_relocated', '{n} touche(s) déplacée(s) pour tenir dans la grille').replace('{n}', r.relocated)));
    if (r.hidden) lines.push(esc(T('sv_hidden', '{n} touche(s) hors de la grille actuelle').replace('{n}', r.hidden)));
    SV.report = `<p>${lines.join(' · ')}</p>` + missingHtml(r.missing, r.missing_count);
    DOC_REV = -1;                      // the next poll fetches the recalled document
    await poll();
    svPaint();
  }
  async function svExport(kind, sv) {
    let url = '/api/plugins/' + LIB_T + '/presets-export?kind=' + encodeURIComponent(kind);
    if (sv) url += '&id=' + sv.id;
    const res = await svFetch(url);
    if (!res.status || res.status >= 400) { TOAST(svErr(res), 'error'); return; }
    const blob = new Blob([JSON.stringify(res.data, null, 2)], {type: 'application/json'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'cartouchier-' + kind + '-' + (sv ? sv.name : 'all').replace(/[^\w.-]+/g, '_') + '.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  async function svImport(inp) {
    const f = inp.files && inp.files[0];
    if (!f) return;
    let doc;
    try { doc = JSON.parse(await f.text()); } catch (e) { TOAST(T('err_bad_file', 'Fichier illisible (JSON attendu)'), 'error'); return; }
    const ow = inp.closest('.cart-form-acts').querySelector('.cart-sv-overwrite');
    const res = await svJson('POST', '/api/plugins/' + LIB_T + '/presets-import',
                             {document: doc, mode: ow && ow.checked ? 'overwrite' : 'skip'});
    inp.value = '';
    if (!res.ok) { TOAST(res.data.error && typeof res.data.error === 'string' && /^[a-z_]+$/.test(res.data.error) ? svErr(res) : T('err_bad_file', 'Fichier illisible (JSON attendu)'), 'error'); return; }
    const c = res.data.counts || {};
    let html = `<p>${esc(T('sv_import_report', 'Import : {i} importée(s), {s} ignorée(s) (nom déjà pris), {r} refusée(s).')
      .replace('{i}', c.imported || 0).replace('{s}', c.skipped || 0).replace('{r}', c.rejected || 0))}</p>`;
    (res.data.rejected || []).forEach(x => {
      html += `<p class="cart-note">${esc(x.name || ('#' + x.index))} : ${esc(T('err_' + x.reason, x.reason))}</p>`;
    });
    (res.data.missing_media || []).forEach(m => {
      html += `<p><strong>${esc(m.name)}</strong></p>` + missingHtml(m.media, m.count);
    });
    SV.report = html;
    await svRefresh();
  }

  // ── main render + poll ──
  function renderMain() {
    if (!EL || !DOC) return;
    renderBar();
    const host = $('.cart-main');
    if (DOC.mode === 'playlist') renderPlaylist(host); else renderGrid(host);
    svFollow();
    if (!raf) raf = requestAnimationFrame(frame);
  }

  let _sig = '';
  async function poll() {
    if (!EL) return;
    let st;
    try { st = await get('/state'); } catch (e) { EL.classList.add('is-offline'); return; }
    if (!EL) return;
    EL.classList.remove('is-offline');
    S = st; S_AT = performance.now();
    if (st.doc_rev !== DOC_REV || !DOC) {
      try { DOC = await get('/doc'); DOC_REV = st.doc_rev; } catch (e) { return; }
    }
    renderPlayers();
    listenPaint();
    // Re-render the grid / playlist only when something it shows has changed — not every 250 ms,
    // which would eat taps on a slow tablet. Never during a pointer drag: it would drop the target.
    const sig = JSON.stringify([DOC_REV, EDIT, st.players.map(p => [p.player, p.state, p.key, p.origin]),
      st.playlist, st.key_media, st.item_media]);
    if (sig !== _sig && !PD) { _sig = sig; renderMain(); }
    if (!raf) raf = requestAnimationFrame(frame);
  }

  function mount(el, vmid, ctx) {
    EL = el; VMID = vmid;
    TOAST = (ctx && ctx.toast) || (() => {});
    BASE = (ctx && ctx.base) || null;
    PUBLIC = !!BASE;
    skeleton();
    listenMount();
    document.addEventListener('ml:uploaded', onUploaded);
    poll();
    timer = setInterval(poll, 250);
  }

  function unmount() {
    listenStop(); clipClose(true);
    if (timer) { clearInterval(timer); timer = null; }
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    clearTimeout(undoTimer);
    document.removeEventListener('ml:uploaded', onUploaded);
    if (PD) { try { PD.ghost.remove(); } catch (e) { /* gone */ } PD = null; }
    if (lib && lib.destroy) { try { lib.destroy(); } catch (e) { /* already gone */ } }
    lib = null; EL = null; VMID = null; BASE = null; PUBLIC = false; S = null; DOC = null; DOC_REV = -1;
    EDIT = false; armed = null; libTarget = null; editing = null;
    _sig = '';
  }

  if (typeof window === 'undefined') return;
  window.MXLPlugins = window.MXLPlugins || {};
  window.MXLPlugins.cartouchier = {mount, unmount, _pure: {effectiveOf, matchUpload, parseDrop, DEFAULTS0, srcToMix, mixToSrc, envDbAt}};
})();
