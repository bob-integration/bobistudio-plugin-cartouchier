// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
//
// Cartouchier — control console. The orchestrator calls window.MXLPlugins.cartouchier.mount(el, vmid, ctx).
//
// Rules this file follows, each for a failure already seen elsewhere:
//   1. ONE function builds every URL (`api`), so the console works behind a public token (ctx.base);
//      in public mode it is READ-ONLY and says so — a POST through the relay would fail in 405;
//   2. the poll stops on unmount;
//   3. iPad is the reference: every gesture is a TAP on a 44 px target. Moving keys and playlist
//      items is done with buttons (← → ↑ ↓), never with HTML5 drag alone; desktop files may also be
//      DROPPED on a key (quick deposit), but the same thing is reachable by a button;
//   4. colours are theme tokens only (var(--…)), never a literal;
//   5. every label goes through T(key, fallback), keys in i18n/{fr,en}.json, prefix plugin.cartouchier.
//      An error from the container is a CODE, translated here — never a sentence from JSON.
//
// The countdown is INTERPOLATED between two polls (requestAnimationFrame): the poll gives the
// truth every 250 ms, the frame clock makes the tenths move smoothly. A paused or idle player is
// never interpolated.
(function () {
  'use strict';
  const P = 'plugin.cartouchier.';
  const T = (k, d) => {
    const key = P + k;
    const v = window.t ? window.t(key) : ((window.I18N || {})[key]);
    return (v && v !== key) ? v : d;
  };
  const TF = (k, d) => {             // full key (media library component)
    const v = window.t ? window.t(k) : ((window.I18N || {})[k]);
    return (v && v !== k) ? v : d;
  };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const COLORS = ['', 'red', 'orange', 'yellow', 'green', 'blue', 'purple', 'grey'];

  let EL = null, VMID = null, BASE = null, PUBLIC = false, TOAST = () => {};
  let timer = null, raf = null;
  let S = null;                       // last /state
  let S_AT = 0;                       // performance.now() of that /state
  let DOC = null, DOC_REV = -1;
  let EDIT = false;
  let editing = null;                 // {page, slot, key} in the key editor
  let lib = null, libTarget = null;   // media library + what it assigns to
  const waves = {};                   // "<player>|<key>" -> {points, duration_ms}
  const pendingDrops = [];            // quick deposits waiting for their upload to finish

  const api = p => BASE ? (BASE + '/plugin' + p) : ('/api/containers/' + VMID + '/plugin' + p);
  const $ = sel => EL && EL.querySelector(sel);

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
          <button type="button" class="cart-btn cart-settings-btn"${PUBLIC ? ' hidden' : ''}>${esc(T('settings', 'Réglages'))}</button>
          <button type="button" class="cart-btn cart-stopall"${PUBLIC ? ' hidden' : ''}>${esc(T('stop_all', 'Tout arrêter'))}</button>
        </div>
        ${PUBLIC ? `<p class="cart-note">${esc(T('read_only', 'Lien public : lecture seule'))}</p>` : ''}
        <div class="cart-players"></div>
        <div class="cart-main"></div>
        <div class="cart-panel cart-editor" hidden></div>
        <div class="cart-panel cart-settings" hidden></div>
        <div class="cart-panel cart-libwrap" hidden>
          <div class="cart-panel-head"><strong class="cart-lib-title"></strong>
            <button type="button" class="cart-btn cart-lib-close">${esc(T('close', 'Fermer'))}</button></div>
          <div class="cart-lib"></div>
        </div>
      </div>`;
    EL.querySelectorAll('[data-mode]').forEach(b => { b.onclick = () => post('/mode', {mode: b.dataset.mode}); });
    $('.cart-edit-in').onchange = e => { EDIT = e.target.checked; renderMain(); };
    $('.cart-settings-btn').onclick = () => openSettings();
    $('.cart-stopall').onclick = () => post('/stop_all', {cut: false});
    $('.cart-lib-close').onclick = () => { $('.cart-libwrap').hidden = true; };
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

  // ── bar (mode, pages) ──
  function renderBar() {
    if (!DOC) return;
    EL.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('is-on', b.dataset.mode === DOC.mode));
    const host = $('.cart-pages');
    host.hidden = DOC.mode !== 'grid';
    host.innerHTML = DOC.pages.map((p, i) => `<button type="button" role="tab" class="cart-btn cart-page${p.id === DOC.page ? ' is-on' : ''}"
       data-page="${esc(p.id)}" aria-selected="${p.id === DOC.page}">${esc(p.name || T('page_n', 'Page {n}').replace('{n}', i + 1))}</button>`).join('');
    host.querySelectorAll('[data-page]').forEach(b => { b.onclick = () => post('/page', {page: b.dataset.page}); });
  }

  // ── grid ──
  function keyOnPlayer(id) {
    return S ? S.players.find(p => p.origin === 'grid' && p.key === id && p.state !== 'idle') : null;
  }

  function renderGrid(host) {
    const pg = DOC.pages.find(p => p.id === DOC.page) || DOC.pages[0];
    const n = DOC.grid.cols * DOC.grid.rows;
    const bySlot = {};
    pg.keys.forEach(k => { bySlot[k.slot] = k; });
    const hidden = pg.keys.filter(k => k.slot >= n).length;
    const media = (S && S.key_media) || {};
    let html = `<div class="cart-grid" style="grid-template-columns:repeat(${DOC.grid.cols},minmax(0,1fr))">`;
    for (let s = 0; s < n; s++) {
      const k = bySlot[s];
      if (!k) {
        html += `<button type="button" class="cart-key is-empty" data-slot="${s}"${EDIT ? '' : ' tabindex="-1"'}>${EDIT ? '+' : ''}</button>`;
        continue;
      }
      const m = media[k.id] || {};
      const p = keyOnPlayer(k.id);
      const flags = [k.loop ? '⟲' : '', k.next ? '→' : '', k.group ? esc(k.group) : '',
                     k.player ? T('player_short', 'L') + k.player : ''].filter(Boolean).join(' ');
      html += `<button type="button" class="cart-key${p ? ' is-' + p.state : ''}${m.state && m.state !== 'ready' ? ' is-media-' + m.state : ''}"
          data-slot="${s}" data-key="${esc(k.id)}" data-color="${esc(k.color || '')}"${p ? ` data-player="${p.player}"` : ''}>
          <span class="cart-key-title">${esc(k.title || mediaName(baseName(k.media.file)) || T('untitled', 'Sans titre'))}</span>
          <span class="cart-key-meta"><span>${esc(fmtDur(m.duration_ms))}</span><span>${flags}</span></span>
          ${p ? `<span class="cart-key-rem"></span><span class="cart-key-bar"></span>` : ''}
          ${m.state && m.state !== 'ready' ? `<span class="cart-key-media">${esc(T('media_' + m.state, m.state))}</span>` : ''}
        </button>`;
    }
    html += '</div>';
    if (hidden) html += `<p class="cart-note">${esc(T('hidden_keys', '{n} touche(s) hors de la grille actuelle : agrandissez la grille pour les voir.').replace('{n}', hidden))}</p>`;
    host.innerHTML = html;
    host.querySelectorAll('.cart-key').forEach(b => {
      b.onclick = () => {
        const s = +b.dataset.slot;
        if (EDIT) { openEditor(pg.id, s, bySlot[s] || null); return; }
        if (b.dataset.key) post('/fire', {key: b.dataset.key});
      };
      // Quick deposit: a desktop FILE dropped on a key (or an empty slot). The same is reachable
      // by the editor's "Send a file" button — this drop is a shortcut, never the only way.
      if (!PUBLIC) {
        b.addEventListener('dragover', e => { if (isFiles(e)) { e.preventDefault(); b.classList.add('is-drop'); } });
        b.addEventListener('dragleave', () => b.classList.remove('is-drop'));
        b.addEventListener('drop', e => {
          b.classList.remove('is-drop');
          if (!isFiles(e)) return;
          e.preventDefault();
          deposit(e.dataTransfer.files, {page: pg.id, slot: +b.dataset.slot, key: bySlot[+b.dataset.slot] || null});
        });
      }
    });
  }
  const isFiles = e => !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');

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
  async function deposit(files, target) {
    const f = Array.from(files || [])[0];
    if (!f) return;
    let up;
    try { up = await uploader(); } catch (e) { TOAST(e.message, 'error'); return; }
    pendingDrops.push(Object.assign({name: f.name.replace(/\.[^.]+$/, '')}, target));
    TOAST(T('upload_started', 'Envoi de « {n} »… la touche sera affectée à la fin.').replace('{n}', f.name), 'info');
    up.enqueue([f], {vmid: VMID}, {});
  }
  function onUploaded(e) {
    if (!EL) return;
    const aid = e.detail && e.detail.asset_id;
    const t = pendingDrops.shift();
    if (!aid || !t) return;
    assignMedia(t, {asset: aid, file: ''}, t.name);
  }
  function assignMedia(t, media, title) {
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

  // ── media library (shared component: base, bins, search, fetch to this node) ──
  function openLibrary(target, titleText) {
    libTarget = target;
    $('.cart-libwrap').hidden = false;
    $('.cart-lib-title').textContent = titleText;
    if (lib || !window.MediaLibrary) {
      if (!window.MediaLibrary) $('.cart-lib').textContent = T('lib_missing', 'Bibliothèque indisponible');
      return;
    }
    lib = window.MediaLibrary.mount($('.cart-lib'), {
      vmid: VMID, T: TF, toast: TOAST, kinds: ['audio', 'video'], view: 'table',
      actions: [{id: 'assign', label: T('lib_assign', 'Affecter'), primary: true},
                {id: 'add', label: T('lib_add', '+ Playlist'), multi: true}],
      onAction: (id, m) => {
        const a = m.asset || null;
        const media = {asset: a ? a.id : null, file: m.path || ''};
        const title = (a && ((a.meta || {}).title || a.name)) || mediaName(baseName(m.path));
        if (id === 'add') { assignMedia({playlist: true}, media, title); return; }
        if (libTarget) assignMedia(libTarget, media, title);
        $('.cart-libwrap').hidden = true;
      },
    });
  }

  // ── key editor ──
  function field(label, html) {
    return `<label class="cart-field"><span>${esc(label)}</span>${html}</label>`;
  }
  function openEditor(pageId, slot, key) {
    editing = {page: pageId, slot, key};
    const k = key || {title: '', media: {asset: null, file: ''}, gain_db: 0, group: '', loop: false, next: '',
                      fade_out_ms: 0, second_press: 'stop', intro_ms: 0, outro_ms: 0, player: null, color: ''};
    const media = key && S && S.key_media ? (S.key_media[key.id] || {}) : {};
    const keys = DOC.pages.flatMap((p, i) => p.keys.filter(x => !key || x.id !== key.id).map(x =>
      ({id: x.id, label: (p.name || T('page_n', 'Page {n}').replace('{n}', i + 1)) + ' · ' + (x.title || x.id)})));
    const nPl = (S && S.n_players) || 2;
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
            <input type="file" class="cart-ed-filein" accept="audio/*,video/*" hidden>
          </div>
          ${media.lufs != null ? `<small>${esc(T('measured', 'Mesuré {l} LUFS, crête {p} dBFS → normalisation {g} dB')
            .replace('{l}', media.lufs).replace('{p}', media.peak_dbfs).replace('{g}', media.norm_db))}${media.norm_limited ? ' — ' + esc(T('limited_' + media.norm_limited, media.norm_limited)) : ''}</small>` : ''}
        </div>
        ${field(T('f_gain', 'Gain de la touche (dB)'), `<input type="number" name="gain_db" step="0.5" min="-40" max="20" value="${k.gain_db}">`)}
        ${field(T('f_fade', 'Fondu d\'arrêt (ms)'), `<input type="number" name="fade_out_ms" step="100" min="0" max="30000" value="${k.fade_out_ms}">`)}
        ${field(T('f_second', 'Deuxième appui'), `<select name="second_press">${['stop', 'pause', 'restart'].map(v =>
          `<option value="${v}"${k.second_press === v ? ' selected' : ''}>${esc(T('sp_' + v, v))}</option>`).join('')}</select>`)}
        ${field(T('f_group', 'Groupe d\'exclusivité'), `<input type="text" name="group" maxlength="32" value="${esc(k.group)}" placeholder="${esc(T('f_group_ph', 'vide = aucun'))}">`)}
        ${field(T('f_next', 'Enchaîner sur'), `<select name="next"><option value="">—</option>${keys.map(x =>
          `<option value="${esc(x.id)}"${k.next === x.id ? ' selected' : ''}>${esc(x.label)}</option>`).join('')}</select>`)}
        ${field(T('f_player', 'Lecteur affecté'), `<select name="player"><option value="">${esc(T('player_any', 'Lecteur libre'))}</option>${
          Array.from({length: nPl}, (_, i) => `<option value="${i + 1}"${k.player === i + 1 ? ' selected' : ''}>${esc(T('player_n', 'Lecteur {n}').replace('{n}', i + 1))}</option>`).join('')}</select>`)}
        ${field(T('f_intro', 'Fin de l\'intro (ms)'), `<input type="number" name="intro_ms" step="100" min="0" value="${k.intro_ms}">`)}
        ${field(T('f_outro', 'Début de la fin (ms)'), `<input type="number" name="outro_ms" step="100" min="0" value="${k.outro_ms}">`)}
        ${field(T('f_color', 'Couleur'), `<select name="color">${COLORS.map(c =>
          `<option value="${c}"${(k.color || '') === c ? ' selected' : ''}>${esc(T('color_' + (c || 'none'), c || '—'))}</option>`).join('')}</select>`)}
        <label class="cart-field cart-field-sw"><span>${esc(T('f_loop', 'Boucle'))}</span><input type="checkbox" class="ctl-switch" name="loop"${k.loop ? ' checked' : ''}></label>
      </div>
      <div class="cart-form-acts">
        ${key ? `<button type="button" class="cart-btn cart-ed-left" aria-label="${esc(T('move_left', 'Déplacer à gauche'))}">←</button>
        <button type="button" class="cart-btn cart-ed-right" aria-label="${esc(T('move_right', 'Déplacer à droite'))}">→</button>
        <button type="button" class="cart-btn cart-danger cart-ed-del">${esc(T('delete', 'Supprimer'))}</button>` : ''}
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-ed-save">${esc(T('save', 'Enregistrer'))}</button>
      </div>`;
    const val = () => {
      const o = Object.assign({}, key || {});
      box.querySelectorAll('[name]').forEach(i => {
        if (i.type === 'checkbox') o[i.name] = i.checked;
        else if (i.type === 'number') o[i.name] = +i.value || 0;
        else o[i.name] = i.value;
      });
      o.player = o.player ? +o.player : null;
      o.media = (key && key.media) || {asset: null, file: ''};
      return o;
    };
    box.querySelector('.cart-ed-close').onclick = () => { box.hidden = true; editing = null; };
    box.querySelector('.cart-ed-save').onclick = async () => {
      const d = await post('/key', {page: pageId, slot, key: val()});
      if (d) { box.hidden = true; editing = null; }
    };
    box.querySelector('.cart-ed-lib').onclick = () => openLibrary({page: pageId, slot, key: val()}, T('choose_for_key', 'Choisir le son de la touche'));
    const fin = box.querySelector('.cart-ed-filein');
    box.querySelector('.cart-ed-file').onclick = () => { fin.value = ''; fin.click(); };
    fin.onchange = () => deposit(fin.files, {page: pageId, slot, key: val()});
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
        if (d) { const pg = DOC.pages.find(p => p.id === pageId); openEditor(pageId, to, pg.keys.find(x => x.id === key.id)); }
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
      ${pl.items.length ? '' : `<p class="cart-note">${esc(T('pl_empty', 'Playlist vide : passez en Édition puis « Ajouter… ».'))}</p>`}
      <ol class="cart-pl">${pl.items.map((it, i) => {
        const m = media[it.id] || {};
        const onAir = it.id === v.on_air, ready = it.id === v.ready;
        return `<li class="cart-pl-row${onAir ? ' is-onair' : ''}${ready ? ' is-ready' : ''}" data-item="${esc(it.id)}" data-index="${i}" data-color="${esc(it.color || '')}">
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
            <button type="button" class="cart-btn cart-danger" data-op="remove" aria-label="${esc(T('delete', 'Supprimer'))}">✕</button></span>` : ''}
        </li>`; }).join('')}</ol>`;
    host.querySelector('.cart-go').onclick = () => post('/pl/go', {});
    host.querySelector('.cart-pl-prev').onclick = () => post('/pl/prev', {});
    host.querySelector('.cart-pl-next').onclick = () => post('/pl/next', {});
    host.querySelector('.cart-ab').onchange = e => post('/pl/options', {ab: e.target.checked});
    host.querySelector('.cart-auto').onchange = e => post('/pl/options', {auto: e.target.checked});
    host.querySelector('.cart-plloop').onchange = e => post('/pl/options', {loop: e.target.checked});
    const add = host.querySelector('.cart-pl-add');
    if (add) add.onclick = () => openLibrary({playlist: true}, T('choose_for_pl', 'Ajouter à la playlist'));
    host.querySelectorAll('.cart-pl-row').forEach(row => {
      const id = row.dataset.item, idx = +row.dataset.index;
      row.querySelector('.cart-pl-pick').onclick = () => { if (!PUBLIC) post('/pl/goto', {index: idx}); };
      row.querySelectorAll('[data-op]').forEach(b => {
        b.onclick = () => {
          const op = b.dataset.op;
          if (op === 'up' || op === 'down') post('/playlist', {op: 'move', id, delta: op === 'up' ? -1 : 1});
          else if (op === 'remove') post('/playlist', {op: 'remove', id});
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
        ${field(T('f_fade', 'Fondu d\'arrêt (ms)'), `<input type="number" name="fade_out_ms" step="100" min="0" max="30000" value="${it.fade_out_ms}">`)}
        ${field(T('f_intro', 'Fin de l\'intro (ms)'), `<input type="number" name="intro_ms" step="100" min="0" value="${it.intro_ms}">`)}
        ${field(T('f_outro', 'Début de la fin (ms)'), `<input type="number" name="outro_ms" step="100" min="0" value="${it.outro_ms}">`)}
        <label class="cart-field cart-field-sw"><span>${esc(T('f_loop', 'Boucle'))}</span><input type="checkbox" class="ctl-switch" name="loop"${it.loop ? ' checked' : ''}></label>
      </div>
      <div class="cart-form-acts"><button type="button" class="cart-btn cart-ed-media">${esc(T('choose_media', 'Base des médias…'))}</button>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-ed-save">${esc(T('save', 'Enregistrer'))}</button></div>`;
    box.querySelector('.cart-ed-close').onclick = () => { box.hidden = true; };
    box.querySelector('.cart-ed-media').onclick = () => openLibrary({item: it}, T('choose_for_item', 'Changer le son'));
    box.querySelector('.cart-ed-save').onclick = async () => {
      const o = {id: it.id};
      box.querySelectorAll('[name]').forEach(i => {
        o[i.name] = i.type === 'checkbox' ? i.checked : i.type === 'number' ? (+i.value || 0) : i.value;
      });
      if (await post('/playlist', {op: 'update', item: o})) box.hidden = true;
    };
  }

  // ── settings ──
  function openSettings() {
    const box = $('.cart-settings');
    if (!box.hidden) { box.hidden = true; return; }
    const s = DOC.settings, pg = DOC.pages.find(p => p.id === DOC.page) || DOC.pages[0];
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
      <div class="cart-form-acts"><span class="cart-spacer"></span><button type="button" class="cart-btn cart-primary cart-st-save">${esc(T('save', 'Enregistrer'))}</button></div>
      <h3 class="cart-h">${esc(T('sec_grid', 'Grille et pages'))}</h3>
      <div class="cart-form">
        ${field(T('cols', 'Colonnes'), `<input type="number" name="cols" min="1" max="12" value="${DOC.grid.cols}">`)}
        ${field(T('rows', 'Rangées'), `<input type="number" name="rows" min="1" max="12" value="${DOC.grid.rows}">`)}
        ${field(T('page_name', 'Nom de la page affichée'), `<input type="text" name="page_name" maxlength="40" value="${esc(pg.name)}">`)}
      </div>
      <div class="cart-form-acts">
        <button type="button" class="cart-btn cart-pg-add">${esc(T('page_add', 'Ajouter une page'))}</button>
        <button type="button" class="cart-btn" data-pmove="-1" aria-label="${esc(T('move_left', 'Déplacer à gauche'))}">←</button>
        <button type="button" class="cart-btn" data-pmove="1" aria-label="${esc(T('move_right', 'Déplacer à droite'))}">→</button>
        <button type="button" class="cart-btn cart-danger cart-pg-del">${esc(T('page_delete', 'Supprimer la page'))}</button>
        <span class="cart-spacer"></span>
        <button type="button" class="cart-btn cart-primary cart-grid-save">${esc(T('save', 'Enregistrer'))}</button>
      </div>`;
    const v = n => box.querySelector(`[name="${n}"]`);
    box.querySelector('.cart-st-close').onclick = () => { box.hidden = true; };
    box.querySelector('.cart-st-save').onclick = () => post('/settings', {
      normalize: v('normalize').checked, target_lufs: +v('target_lufs').value,
      max_gain_db: +v('max_gain_db').value, peak_ceiling_db: +v('peak_ceiling_db').value, alert_s: +v('alert_s').value});
    box.querySelector('.cart-grid-save').onclick = async () => {
      await post('/grid', {cols: +v('cols').value, rows: +v('rows').value});
      if (v('page_name').value !== pg.name) await post('/pages', {op: 'rename', page: pg.id, name: v('page_name').value});
    };
    box.querySelector('.cart-pg-add').onclick = () => post('/pages', {op: 'add'});
    box.querySelectorAll('[data-pmove]').forEach(b => { b.onclick = () => post('/pages', {op: 'move', page: DOC.page, delta: +b.dataset.pmove}); });
    box.querySelector('.cart-pg-del').onclick = () => {
      if (box.dataset.confirm !== DOC.page) {
        box.dataset.confirm = DOC.page;
        TOAST(T('confirm_page_delete', 'Appuyez encore pour supprimer la page et ses touches'), 'warning');
        return;
      }
      delete box.dataset.confirm;
      post('/pages', {op: 'delete', page: DOC.page});
    };
  }

  // ── main render + poll ──
  function renderMain() {
    if (!EL || !DOC) return;
    renderBar();
    const host = $('.cart-main');
    if (DOC.mode === 'playlist') renderPlaylist(host); else renderGrid(host);
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
    // Re-render the grid / playlist only when something it shows has changed — not every 250 ms,
    // which would eat taps on a slow tablet.
    const sig = JSON.stringify([DOC_REV, EDIT, st.players.map(p => [p.player, p.state, p.key, p.origin]),
      st.playlist, st.key_media, st.item_media]);
    if (sig !== _sig) { _sig = sig; renderMain(); }
    if (!raf) raf = requestAnimationFrame(frame);
  }

  function mount(el, vmid, ctx) {
    EL = el; VMID = vmid;
    TOAST = (ctx && ctx.toast) || (() => {});
    BASE = (ctx && ctx.base) || null;
    PUBLIC = !!BASE;
    skeleton();
    document.addEventListener('ml:uploaded', onUploaded);
    poll();
    timer = setInterval(poll, 250);
  }

  function unmount() {
    if (timer) { clearInterval(timer); timer = null; }
    if (raf) { cancelAnimationFrame(raf); raf = null; }
    document.removeEventListener('ml:uploaded', onUploaded);
    if (lib && lib.destroy) { try { lib.destroy(); } catch (e) { /* already gone */ } }
    lib = null; EL = null; VMID = null; BASE = null; PUBLIC = false; S = null; DOC = null; DOC_REV = -1;
    _sig = '';
  }

  window.MXLPlugins = window.MXLPlugins || {};
  window.MXLPlugins.cartouchier = {mount, unmount};
})();
