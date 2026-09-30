/* ==========================================================================
   StreamBridge web interface
   Vanilla ES module, no build step, no external dependency.
   The browser only ever talks to this local server: searches, metadata, queue
   control and status all go through the JSON API below, and thumbnails are
   redirected by the backend.
   ========================================================================== */

'use strict';

/* --- Small helpers ------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Minimal DOM builder: el('div.foo', {attrs}, ...children) */
function el(spec, attrs = null, ...children) {
  const [tagPart, ...classParts] = spec.split('.');
  const node = document.createElement(tagPart || 'div');
  if (classParts.length) node.className = classParts.join(' ');
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = [node.className, value].filter(Boolean).join(' ');
      else if (key === 'text') node.textContent = String(value);
      // There is deliberately no 'html' key. innerHTML is an XSS sink, and an
      // upstream title is attacker-controlled: anyone can publish a video whose
      // title is the payload. A search result's title must reach the DOM as
      // text, which is what 'text' does. Keeping the escape hatch around
      // because it "might be useful" is how it eventually gets used.
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/**
 * Inline SVG from a path spec; keeps the markup free of external icons.
 *
 * The only remaining innerHTML in the UI, and it takes an ICON constant rather
 * than anything from the server. Icons are the one place markup is the point;
 * every text node uses textContent.
 */
function icon(paths, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  if (className) svg.setAttribute('class', className);
  svg.innerHTML = paths;
  return svg;
}

const ICON = {
  play: '<path d="M8 5.5v13l11-6.5z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="7" y="5" width="3.6" height="14" rx="1.2" fill="currentColor" stroke="none"/><rect x="13.4" y="5" width="3.6" height="14" rx="1.2" fill="currentColor" stroke="none"/>',
  heart: '<path d="M12 20s-7-4.4-7-9.2A4 4 0 0 1 12 8a4 4 0 0 1 7 2.8C19 15.6 12 20 12 20z"/>',
  plus: '<path d="M12 6v12M6 12h12"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 13h8l1-13"/>',
  queue: '<path d="M4 7h11M4 12h11M4 17h7"/><path d="M17 13v6"/><circle cx="15.5" cy="19.5" r="1.8"/><path d="M17 13l3-1.5V15"/>',
  up: '<path d="M12 19V6M6 12l6-6 6 6"/>',
  down: '<path d="M12 5v13M6 12l6 6 6-6"/>',
  external: '<path d="M14 5h5v5"/><path d="M19 5l-8 8"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
  note: '<path d="M9 18V6l9-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="15.5" cy="16" r="2.5"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
  warn: '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4.5M12 17.4v.2"/>',
  check: '<path d="M5 12.5 10 17.5 19 7"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.2"/>',
  grip: '<circle cx="9" cy="7" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="7" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="1.3" fill="currentColor" stroke="none"/><circle cx="9" cy="17" r="1.3" fill="currentColor" stroke="none"/><circle cx="15" cy="17" r="1.3" fill="currentColor" stroke="none"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  repeat: '<path d="M6 8h11a3 3 0 0 1 0 6H7"/><path d="m8.5 5.5-2.5 2.5 2.5 2.5"/><path d="M18 16H7a3 3 0 0 1 0-6h10"/><path d="m15.5 18.5 2.5-2.5-2.5-2.5"/>',
  more: '<circle cx="12" cy="6" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="18" r="1.4" fill="currentColor" stroke="none"/>',
};

function formatTime(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function formatRelative(unixSeconds) {
  if (!unixSeconds) return '';
  const diff = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (diff < 60) return 'gerade eben';
  if (diff < 3600) return `vor ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `vor ${Math.floor(diff / 3600)} h`;
  if (diff < 86400 * 7) return `vor ${Math.floor(diff / 86400)} d`;
  return new Date(unixSeconds * 1000).toLocaleDateString('de-DE');
}

const trackKey = (t) => (t && (t.video_id || t.id)) || '';

/** Cover element backed by the local thumbnail redirect. */
function coverNode(videoId, className = 'cover') {
  const wrap = el('div', { class: className, dataset: { empty: 'true' } });
  if (!videoId) {
    wrap.append(icon(ICON.note, 'cover__placeholder'));
    return wrap;
  }
  const img = el('img', { alt: '', loading: 'lazy', decoding: 'async', src: `/thumbnail/${videoId}` });
  img.addEventListener('load', () => { wrap.dataset.empty = 'false'; });
  img.addEventListener('error', () => { img.remove(); wrap.dataset.empty = 'true'; });
  wrap.append(img, icon(ICON.note, 'cover__placeholder'));
  return wrap;
}

/* --- API ---------------------------------------------------------------- */

class ApiError extends Error {
  constructor(message, { hint = '', code = '', status = 0 } = {}) {
    super(message);
    this.hint = hint;
    this.code = code;
    this.status = status;
  }
}

const api = {
  async request(path, { method = 'GET', body } = {}) {
    const init = { method, headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      throw new ApiError('Der Server ist nicht erreichbar.', {
        hint: 'Läuft streambridge-server?  systemctl --user status streambridge-server',
        code: 'NETWORK',
      });
    }
    const text = await response.text();
    let payload = {};
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = {}; }
    }
    if (!response.ok) {
      throw new ApiError(payload.message || `Anfrage fehlgeschlagen (HTTP ${response.status})`, {
        hint: payload.hint || '',
        code: payload.code || '',
        status: response.status,
      });
    }
    return payload;
  },
  get(path) { return api.request(path); },
  post(path, body) { return api.request(path, { method: 'POST', body: body ?? {} }); },
};

/* --- Application state -------------------------------------------------- */

const state = {
  view: 'start',
  status: { state: 'stopped', volume: 0, position: null, current: null, playlist_length: 0, elapsed: 0, duration: 0, shuffle: false, repeat: false, repeat_one: false, generation: 0 },
  queue: { items: [], length: 0, generation: -1 },
  favorites: { items: [], ids: [] },
  history: { items: [] },
  favoriteSet: new Set(),
  searchResults: null,
  searchQuery: '',
  lastQuery: '',
  elapsedBase: 0,
  statusAt: 0,
  scrubbing: false,
  scrubValue: 0,
  volumeDraft: null,
  connection: 'connecting',
  busy: new Set(),
};

const RECENT_KEY = 'streambridge.recent-queries';
const RECENT_MAX = 8;

function loadRecent() {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((x) => typeof x === 'string' && x.trim()).slice(0, RECENT_MAX) : [];
  } catch { return []; }
}

function rememberQuery(q) {
  const clean = q.trim();
  if (clean.length < 2) return;
  const next = [clean, ...loadRecent().filter((x) => x !== clean)].slice(0, RECENT_MAX);
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)); } catch { /* storage disabled */ }
}

/* --- Toasts ------------------------------------------------------------- */

const toastHost = $('#toasts');

function toast(message, { kind = 'info', hint = '', timeout = 4800 } = {}) {
  const node = el('div', { class: `toast toast--${kind}` },
    icon(kind === 'error' ? ICON.warn : kind === 'ok' ? ICON.check : ICON.info, 'toast__icon'),
    el('div', { class: 'toast__text' },
      el('div', { class: 'toast__title', text: message }),
      hint ? el('div', { class: 'toast__hint', text: hint }) : null),
    el('button', { class: 'toast__close', 'aria-label': 'Schließen', text: '×', onClick: () => dismiss() }),
  );
  const dismiss = () => {
    if (!node.isConnected) return;
    node.classList.add('is-leaving');
    setTimeout(() => node.remove(), 220);
  };
  toastHost.append(node);
  while (toastHost.children.length > 4) toastHost.firstElementChild.remove();
  if (timeout) setTimeout(dismiss, timeout);
}

const toastError = (err) => toast(
  err?.message || 'Unbekannter Fehler',
  { kind: 'error', hint: err?.hint || '' },
);

/* --- Confirm dialog ----------------------------------------------------- */

const sheet = $('#confirm-sheet');

function confirmAction({ title, text, ok = 'Leeren' }) {
  return new Promise((resolve) => {
    $('#confirm-title').textContent = title;
    $('#confirm-text').textContent = text;
    const okBtn = $('#confirm-ok');
    okBtn.textContent = ok;
    const cleanup = (result) => {
      okBtn.removeEventListener('click', onOk);
      $('#confirm-cancel').removeEventListener('click', onCancel);
      sheet.removeEventListener('close', onCancel);
      if (sheet.open) sheet.close();
      resolve(result);
    };
    const onOk = () => cleanup(true);
    const onCancel = () => cleanup(false);
    okBtn.addEventListener('click', onOk);
    $('#confirm-cancel').addEventListener('click', onCancel);
    sheet.addEventListener('close', onCancel);
    sheet.showModal();
    okBtn.focus();
  });
}

/* --- Context menu ------------------------------------------------------- */

let openMenu = null;

function closeMenu() {
  if (openMenu) { openMenu.remove(); openMenu = null; }
}

function contextMenu(anchor, items) {
  closeMenu();
  const menu = el('div', { class: 'menu', role: 'menu' });
  for (const item of items) {
    if (item === '-') { menu.append(el('div', { class: 'menu__sep' })); continue; }
    menu.append(el('button', {
      class: 'menu__item', type: 'button', role: 'menuitem',
      onClick: () => { closeMenu(); item.action(); },
    }, icon(item.icon), item.label));
  }
  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  const size = menu.getBoundingClientRect();
  let left = Math.min(rect.left, window.innerWidth - size.width - 12);
  let top = rect.bottom + 6;
  if (top + size.height > window.innerHeight - 12) top = Math.max(12, rect.top - size.height - 6);
  menu.style.left = `${Math.max(12, left)}px`;
  menu.style.top = `${top}px`;
  openMenu = menu;
}

document.addEventListener('pointerdown', (e) => {
  if (openMenu && !openMenu.contains(e.target)) closeMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });
window.addEventListener('resize', closeMenu);

/* --- Track actions ------------------------------------------------------ */

async function playNow(track) {
  const id = trackKey(track);
  if (!id) { toast('Dieser Eintrag kann nicht abgespielt werden.', { kind: 'error' }); return; }
  await withBusy('play', async () => {
    const position = state.status.position || null;
    const result = await api.post('/queue/add', { ids: [id], tracks: [trackPayload(track)], play: true, position });
    applyQueue(result.queue);
    await refreshStatus();
  });
}

async function enqueue(track) {
  const id = trackKey(track);
  if (!id) { toast('Dieser Eintrag kann nicht abgespielt werden.', { kind: 'error' }); return; }
  await withBusy(`queue:${id}`, async () => {
    const result = await api.post('/queue/add', { ids: [id], tracks: [trackPayload(track)], play: false });
    applyQueue(result.queue);
    toast(`„${shortTitle(track)}“ wurde zur Queue hinzugefügt.`, { kind: 'ok', timeout: 2600 });
  });
}

async function toggleFavorite(track) {
  const id = trackKey(track);
  if (!id) return false;
  // Optimistic: the heart reacts instantly, the server confirms afterwards.
  // A first-time favourite can cost a metadata lookup, and a two second wait
  // before the icon changes feels broken.
  const optimistic = !state.favoriteSet.has(id);
  applyFavoriteLocal(track, optimistic);
  paintFavorites();
  paintPlayer();
  if (state.view === 'favorites') renderFavorites();
  try {
    const result = await api.post('/favorites/toggle', { id });
    const confirmed = !!result.favorite;
    state.favoriteSet[confirmed ? 'add' : 'delete'](id);
    paintFavorites();
    paintPlayer();
    if (state.view === 'favorites') {
      // Reconcile with the server so ordering and stored metadata are right.
      await loadFavorites();
      renderFavorites();
    }
    toast(confirmed ? 'Zu Favoriten hinzugefügt.' : 'Aus Favoriten entfernt.', { kind: 'ok', timeout: 2200 });
    return confirmed;
  } catch (err) {
    // Roll back to the state before the click.
    applyFavoriteLocal(track, !optimistic);
    paintFavorites();
    paintPlayer();
    if (state.view === 'favorites') renderFavorites();
    throw err;
  }
}

/** Keep the local favourites list in step so the view updates without a fetch. */
function applyFavoriteLocal(track, add) {
  const id = trackKey(track);
  if (!id) return;
  if (add) {
    state.favoriteSet.add(id);
    const items = state.favorites.items.filter((item) => item.track.id !== id);
    state.favorites = {
      items: [{ track: trackPayload(track), added_at: Math.floor(Date.now() / 1000) }, ...items],
      ids: [id, ...state.favorites.ids.filter((x) => x !== id)],
    };
  } else {
    state.favoriteSet.delete(id);
    state.favorites = {
      items: state.favorites.items.filter((item) => item.track.id !== id),
      ids: state.favorites.ids.filter((x) => x !== id),
    };
  }
}

function trackPayload(track) {
  return {
    id: trackKey(track),
    title: track.title || '',
    artist: track.artist || '',
    album: track.album || '',
    duration: track.duration ?? null,
    url: track.url || '',
    thumbnail: track.thumbnail || '',
    channel: track.channel || '',
    uploader: track.uploader || '',
  };
}

const shortTitle = (track) => (track.title || track.name || '').split(' - ')[0] || 'Titel';

async function withBusy(key, fn) {
  if (state.busy.has(key)) return;
  state.busy.add(key);
  paintPlayer();
  try {
    await fn();
  } catch (err) {
    toastError(err);
  } finally {
    state.busy.delete(key);
    paintPlayer();
  }
}

/* --- Track row rendering ------------------------------------------------ */

function trackRow(track, opts = {}) {
  const row = $('#tpl-track-row').content.firstElementChild.cloneNode(true);
  const id = trackKey(track);
  const title = track.title || track.name || id;
  const artist = track.artist || track.channel || track.uploader || 'Unbekannter Kanal';
  const duration = track.duration;

  $('.track__title', row).textContent = title;
  $('.track__sub', row).textContent = artist;
  $('.track__duration', row).textContent = formatTime(duration);
  row.dataset.id = id;

  const art = $('.track__art', row);
  const img = el('img', { alt: '', loading: 'lazy', decoding: 'async' });
  if (id) {
    img.src = `/thumbnail/${id}`;
    img.addEventListener('load', () => { art.dataset.empty = 'false'; });
    img.addEventListener('error', () => { img.remove(); art.dataset.empty = 'true'; });
    art.append(img, icon(ICON.play, 'track__play'));
  } else {
    art.append(icon(ICON.play, 'track__play'));
  }
  art.addEventListener('click', () => playNow(track));

  const actions = $('.track__actions', row);
  if (opts.onRemove) {
    actions.append(el('button', {
      class: 'icon-btn track__act', 'data-act': 'remove',
      'aria-label': 'Aus der Queue entfernen', title: 'Entfernen',
      onClick: () => opts.onRemove(track),
    }, icon(ICON.trash)));
  }
  if (opts.onMove) {
    actions.append(
      el('button', { class: 'icon-btn track__act', 'data-act': 'up',
        'aria-label': 'Nach oben', title: 'Nach oben',
        onClick: () => opts.onMove(track, -1) }, icon(ICON.up)),
      el('button', { class: 'icon-btn track__act', 'data-act': 'down',
        'aria-label': 'Nach unten', title: 'Nach unten',
        onClick: () => opts.onMove(track, 1) }, icon(ICON.down)),
    );
  }
  actions.append(el('button', {
    class: 'icon-btn track__act', 'data-action': 'menu',
    'aria-label': 'Weitere Aktionen', title: 'Weitere Aktionen',
    onClick: (e) => openRowMenu(e.currentTarget, track, opts),
  }, icon(ICON.more)));

  const favBtn = $('[data-action="favorite"]', row);
  if (id) {
    const isFav = state.favoriteSet.has(id);
    favBtn.setAttribute('aria-pressed', String(isFav));
    favBtn.addEventListener('click', () => toggleFavorite(track).catch(toastError));
  } else {
    favBtn.remove();
  }

  $('[data-action="play"]', row).addEventListener('click', () => playNow(track));
  $('[data-action="queue"]', row).addEventListener('click', () => enqueue(track));
  return row;
}

function openRowMenu(anchor, track, opts) {
  const id = trackKey(track);
  const position = Number(anchor.closest('.track')?.dataset.position) || track.position;
  const items = [
    { label: 'Sofort abspielen', icon: ICON.play, action: () => playNow(track) },
    { label: 'Zur Queue hinzufügen', icon: ICON.plus, action: () => enqueue(track) },
    { label: state.favoriteSet.has(id) ? 'Aus Favoriten entfernen' : 'Zu Favoriten hinzufügen',
      icon: ICON.heart, action: () => toggleFavorite(track).catch(toastError) },
  ];
  // On a narrow screen the row shows only play / queue / heart / menu, so the
  // reorder and remove actions have to live here as well.
  if (opts?.onMove && position) {
    items.push('-',
      { label: 'Nach oben', icon: ICON.up, action: () => opts.onMove(track, -1) },
      { label: 'Nach unten', icon: ICON.down, action: () => opts.onMove(track, 1) });
  }
  if (opts?.onRemove) {
    items.push('-', { label: 'Aus der Queue entfernen', icon: ICON.trash,
      action: () => opts.onRemove(track) });
  }
  if (id) {
    items.push('-', {
      label: 'Auf YouTube öffnen',
      icon: ICON.external,
      action: () => window.open(`https://www.youtube.com/watch?v=${id}`, '_blank', 'noopener,noreferrer'),
    });
  }
  contextMenu(anchor, items);
}

function emptyState({ iconPath = ICON.note, title, text, action }) {
  return el('div', { class: 'state' },
    el('div', { class: 'state__icon' }, icon(iconPath)),
    el('div', { class: 'state__title', text: title }),
    text ? el('div', { class: 'state__text', text }) : null,
    action || null);
}

function skeletonList(count = 6) {
  return el('div', { class: 'skeleton' }, Array.from({ length: count }, () => el('div', { class: 'skeleton__row' })));
}

/* --- Shared list renderer ----------------------------------------------- */

/**
 * Re-render a list only when its content signature changed, so status ticks
 * never cause a full queue rebuild.
 */
function renderList(host, items, buildRow, signature) {
  const next = String(signature);
  if (host.dataset.sig === next) return;
  host.dataset.sig = next;
  host.replaceChildren(...items.map(buildRow));
}

function trackListNode(id) {
  let host = $(`#${id} .tracklist`);
  if (!host) { host = el('ul', { class: 'tracklist' }); $(`#${id}`).append(host); }
  return host;
}

/* --- Views: Start ------------------------------------------------------- */

function renderStart() {
  const host = $('#view-start');
  const current = state.status.current;
  const recent = state.history.items.slice(0, 8);
  const favs = state.favorites.items.slice(0, 8);
  const signature = JSON.stringify([
    current && trackKey(current), state.status.state, state.queue.generation,
    recent.map((i) => i.track.id), favs.map((i) => i.track.id), loadRecent(),
  ]);
  if (host.dataset.sig === signature) return;
  host.dataset.sig = signature;
  host.replaceChildren();

  if (current) {
    const id = trackKey(current);
    const cover = coverNode(id, 'cover hero__art');
    const isPlaying = state.status.state === 'playing';
    host.append(el('div', { class: 'hero' },
      cover,
      el('div', null,
        el('span', { class: 'hero__kicker' },
          el('span', { class: `eq${isPlaying ? '' : ' is-paused'}` }, el('i'), el('i'), el('i'), el('i')),
          isPlaying ? 'Jetzt läuft' : state.status.state === 'paused' ? 'Pausiert' : 'Bereit'),
        el('h1', { class: 'hero__title', text: current.title || 'Titel' }),
        el('p', { class: 'hero__artist', text: current.artist || 'Unbekannter Kanal' }),
        el('div', { class: 'hero__meta' },
          el('div', { class: 'hero__meta-item' }, el('span', { text: 'Position' }), el('b', { text: state.status.position || 1 })),
          el('div', { class: 'hero__meta-item' }, el('span', { text: 'In der Queue' }), el('b', { text: state.queue.length || 0 })),
          el('div', { class: 'hero__meta-item' }, el('span', { text: 'Dauer' }), el('b', { text: formatTime(state.status.duration) }))),
        el('div', { class: 'hero__actions' },
          el('button', { class: 'btn btn--primary', type: 'button', onClick: togglePlay },
            icon(isPlaying ? ICON.pause : ICON.play), isPlaying ? 'Pause' : 'Abspielen'),
          el('button', { class: 'btn btn--ghost', type: 'button', onClick: () => navigate('queue') },
            icon(ICON.queue), `Queue (${state.queue.length || 0})`),
          id ? el('button', { class: 'btn btn--ghost', type: 'button', onClick: () => toggleFavorite(current).catch(toastError) },
            icon(ICON.heart), state.favoriteSet.has(id) ? 'Favorit' : 'Favorisieren') : null))));
  } else {
    host.append(el('div', { class: 'hero' },
      coverNode('', 'cover hero__art'),
      el('div', null,
        el('span', { class: 'hero__kicker' }, 'Lokale Musik'),
        el('h1', { class: 'hero__title', text: 'Deine Musik, komplett lokal.' }),
        el('p', { class: 'hero__artist', text: 'Suche auf YouTube Music, verwalte die Queue und steuere MPD — alles über den Browser.' }),
        el('div', { class: 'hero__actions' },
          el('button', { class: 'btn btn--primary', type: 'button', onClick: () => { navigate('search'); $('#search-input').focus(); } },
            icon(ICON.search), 'Musik suchen')))));
  }

  const recents = loadRecent();
  if (recents.length) {
    host.append(el('section', { class: 'section' },
      el('div', { class: 'section__head' }, el('h2', { class: 'section__title', text: 'Zuletzt gesucht' })),
      el('div', { class: 'chip-row' }, recents.map((q) => el('button', {
        class: 'chip', type: 'button', onClick: () => { $('#search-input').value = q; navigate(`search?q=${encodeURIComponent(q)}`); },
      }, q)))));
  }

  if (recent.length) {
    const list = el('ul', { class: 'tracklist' });
    recent.forEach((item, index) => list.append(trackRow(item.track)));
    host.append(el('section', { class: 'section' },
      el('div', { class: 'section__head' },
        el('h2', { class: 'section__title', text: 'Zuletzt gespielt' }),
        el('a', { class: 'section__link', href: '#/history' }, icon(ICON.clock), 'Verlauf')),
      list));
  }

  if (favs.length) {
    const grid = el('div', { class: 'cards' });
    favs.forEach((item) => {
      const t = item.track;
      const card = el('button', { class: 'card', type: 'button', onClick: () => playNow(t) },
        coverNode(t.id, 'cover card__art'),
        el('div', { class: 'card__title', text: t.title || t.id }),
        el('div', { class: 'card__sub', text: t.artist || t.channel || 'Unbekannt' }));
      grid.append(card);
    });
    host.append(el('section', { class: 'section' },
      el('div', { class: 'section__head' },
        el('h2', { class: 'section__title', text: 'Favoriten' }),
        el('a', { class: 'section__link', href: '#/favorites' }, icon(ICON.heart), 'Alle')),
      grid));
  }

  if (!current && !recent.length && !favs.length) {
    host.append(el('div', { class: 'state' },
      el('div', { class: 'state__icon' }, icon(ICON.search)),
      el('div', { class: 'state__title', text: 'Leg los' }),
      el('div', { class: 'state__text', text: 'Suchst du nach einem Song, drücke auf die Suchleiste oben oder / auf der Tastatur.' })));
  }
}

/* --- Views: Search ------------------------------------------------------ */

let searchTimer = null;
let searchSeq = 0;

function renderSearch(initial = false) {
  const host = $('#view-search');
  const head = el('div', { class: 'view__head' },
    el('div', null,
      el('h1', { class: 'view__title', text: 'Suche' }),
      el('p', { class: 'view__subtitle', text: 'YouTube Music, mit YouTube als Rückfallebene.' })),
    el('div', { class: 'chip-row' },
      ['songs', 'videos'].map((type) => el('button', {
        class: `chip${state.searchType === type ? ' is-active' : ''}`, type: 'button',
        text: type === 'songs' ? 'Songs' : 'Videos',
        onClick: () => { state.searchType = type; if (state.lastQuery) runSearch(state.lastQuery, true); else renderSearch(); },
      }))));

  if (!host.dataset.built) {
    host.dataset.built = '1';
    host.append(head, el('div', { class: 'view__body' }));
  } else {
    host.replaceChild(head, host.firstElementChild);
    for (const chip of $$('.chip', head)) {
      chip.classList.toggle('is-active', chip.textContent === (state.searchType === 'songs' ? 'Songs' : 'Videos'));
    }
  }
  const body = $('.view__body', host);
  const results = state.searchResults;

  if (state.searchPending) { body.replaceChildren(skeletonList(7)); return; }
  if (!results) {
    const recents = loadRecent();
    const suggestions = [
      ...recents.map((q) => ({ label: q, hint: 'zuletzt gesucht' })),
      ...state.history.items.slice(0, 6).map((i) => ({ label: i.track.title, hint: i.track.artist || 'Verlauf' })),
      ...state.favorites.items.slice(0, 6).map((i) => ({ label: i.track.title, hint: 'Favorit' })),
    ].filter((s) => s.label);
    const seen = new Set();
    const unique = suggestions.filter((s) => (seen.has(s.label) ? false : seen.add(s.label)));
    body.replaceChildren(unique.length
      ? el('div', null,
          el('div', { class: 'section__head' }, el('h2', { class: 'section__title', text: 'Vorschläge' })),
          el('div', { class: 'chip-row' }, unique.slice(0, 14).map((s) => el('button', {
            class: 'chip', type: 'button', title: s.hint,
            onClick: () => { $('#search-input').value = s.label; runSearch(s.label); },
          }, s.label))),
          el('div', { class: 'state' },
            el('div', { class: 'state__icon' }, icon(ICON.search)),
            el('div', { class: 'state__title', text: 'Wonach suchst du?' }),
            el('div', { class: 'state__text', text: 'Titel, Interpret oder Album. Die Suche läuft lokal über yt-dlp.' })))
      : emptyState({
          iconPath: ICON.search,
          title: 'Wonach suchst du?',
          text: 'Titel, Interpret oder Album eingeben — die Suche läuft lokal über yt-dlp.',
        }));
    return;
  }

  if (results.error) {
    body.replaceChildren(emptyState({
      iconPath: ICON.warn, title: 'Suche fehlgeschlagen',
      text: results.error, action: el('button', { class: 'btn btn--ghost', type: 'button', onClick: () => runSearch(state.lastQuery, true) }, 'Erneut versuchen'),
    }));
    return;
  }

  if (!results.results.length) {
    body.replaceChildren(emptyState({
      iconPath: ICON.search,
      title: `Keine Treffer für „${results.query}“`,
      text: 'Versuche einen anderen Suchbegriff oder wechsle zu „Videos“.',
    }));
    return;
  }

  const list = el('ul', { class: 'tracklist' });
  results.results.forEach((t) => list.append(trackRow(t)));
  body.replaceChildren(
    el('div', { class: 'section__head' },
      el('h2', { class: 'section__title', text: `${results.count} Treffer für „${results.query}“` }),
      el('button', { class: 'btn btn--ghost btn--sm', type: 'button', onClick: enqueueAllResults },
        icon(ICON.plus), 'Alle zur Queue')),
    list,
  );
}

async function enqueueAllResults() {
  const results = state.searchResults;
  if (!results?.results?.length) return;
  await withBusy('queue-all', async () => {
    const ids = results.results.map(trackKey).filter(Boolean);
    let added = 0;
    // Chunked so a long result list never exceeds the server's batch limit.
    for (let i = 0; i < ids.length; i += 100) {
      const chunk = results.results.slice(i, i + 100);
      const payload = await api.post('/queue/add', { ids: chunk.map(trackKey), tracks: chunk.map(trackPayload) });
      applyQueue(payload.queue);
      added += payload.added;
    }
    toast(`${added} Titel zur Queue hinzugefügt.`, { kind: 'ok' });
  });
}

async function runSearch(query, force = false) {
  const q = (query || '').trim();
  if (!q) return;
  if (!force && q === state.lastQuery && state.searchResults && !state.searchResults.error) return;
  state.lastQuery = q;
  state.searchQuery = q;
  state.searchPending = true;
  const seq = ++searchSeq;
  $('#search-spinner').hidden = false;
  if (state.view !== 'search') { navigate(`search?q=${encodeURIComponent(q)}`); return; }
  renderSearch();
  try {
    const params = new URLSearchParams({ q, type: state.searchType || 'songs', limit: '30' });
    const payload = await api.get(`/search?${params.toString()}`);
    if (seq !== searchSeq) return;
    state.searchResults = payload;
    rememberQuery(q);
  } catch (err) {
    if (seq !== searchSeq) return;
    state.searchResults = { query: q, count: 0, results: [], error: err.message + (err.hint ? ` ${err.hint}` : '') };
  } finally {
    if (seq === searchSeq) {
      state.searchPending = false;
      $('#search-spinner').hidden = true;
      if (state.view === 'search') renderSearch();
    }
  }
}

/* --- Views: Queue ------------------------------------------------------- */

function renderQueue() {
  const host = $('#view-queue');
  const items = state.queue.items || [];
  const currentPos = state.status.position;
  // The list is rebuilt only when the queue itself changed. The highlighted
  // row is updated separately, so a status tick never detaches DOM nodes the
  // user is about to click.
  const signature = JSON.stringify([state.queue.generation, items.length]);

  const head = el('div', { class: 'view__head' },
    el('div', null,
      el('h1', { class: 'view__title', text: 'Warteschlange' }),
      el('p', { class: 'view__subtitle', text: items.length ? `${items.length} Titel · gleiche Queue wie in ncmpcpp` : 'Noch keine Titel' })),
    el('div', { class: 'chip-row' },
      el('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: !items.length,
        onClick: async () => {
          const ok = await confirmAction({ title: 'Queue leeren?', text: 'Alle Einträge der Warteschlange werden entfernt. Musikdateien auf der Festplatte bleiben unberührt.' });
          if (!ok) return;
          await withBusy('queue-clear', async () => {
            const result = await api.post('/queue/clear');
            applyQueue(result);
            toast('Queue geleert.', { kind: 'ok' });
          });
        } }, icon(ICON.trash), 'Queue leeren')));

  let body = $('.view__body', host);
  if (!body) { body = el('div', { class: 'view__body' }); host.replaceChildren(head, body); }
  else { host.replaceChild(head, host.firstElementChild); }

  if (!items.length) {
    body.replaceChildren(emptyState({
      iconPath: ICON.queue,
      title: 'Die Queue ist leer',
      text: 'Füge Titel über die Suche hinzu — sie landen direkt in MPD und damit auch in ncmpcpp.',
      action: el('button', { class: 'btn btn--primary', type: 'button', onClick: () => navigate('search') }, icon(ICON.search), 'Zur Suche'),
    }));
    host.dataset.sig = 'empty';
    return;
  }

  const list = body.querySelector('.tracklist') || el('ul', { class: 'tracklist' });
  if (!list.isConnected) body.replaceChildren(list);

  if (body.dataset.sig !== signature) {
    body.dataset.sig = signature;
    list.replaceChildren(...items.map((item) => {
      const row = trackRow(item, {
        // Read the position from the DOM at click time: a background metadata
        // update must not leave a handler pointing at a stale item.
        onRemove: (t) => removeFromQueue(Number(row.dataset.position)),
        onMove: (t, dir) => moveInQueue(Number(row.dataset.position), Number(row.dataset.position) + dir),
      });
      row.dataset.position = item.position;
      row.draggable = true;
      row.classList.toggle('is-current', item.position === currentPos);
      row.addEventListener('dragstart', (e) => {
        row.classList.add('is-dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(item.position));
      });
      row.addEventListener('dragend', () => {
        row.classList.remove('is-dragging');
        $$('.is-drop-target', list).forEach((n) => n.classList.remove('is-drop-target'));
      });
      row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('is-drop-target'); });
      row.addEventListener('dragleave', () => row.classList.remove('is-drop-target'));
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.classList.remove('is-drop-target');
        const from = Number(e.dataTransfer.getData('text/plain'));
        if (Number.isInteger(from) && from !== item.position) moveInQueue(from, item.position);
      });
      return row;
    }));
  }
  // Always refresh the "currently playing" marker, and the per-row duration
  // once background metadata arrives.
  $$('.track', list).forEach((row) => {
    row.classList.toggle('is-current', Number(row.dataset.position) === currentPos);
    const item = items[Number(row.dataset.position) - 1];
    if (item) {
      row.dataset.id = trackKey(item);
      const title = $('.track__title', row);
      const sub = $('.track__sub', row);
      if (title && title.textContent !== item.title) title.textContent = item.title;
      if (sub && item.artist && sub.textContent !== item.artist) sub.textContent = item.artist;
    }
  });
}

async function removeFromQueue(position) {
  await withBusy(`queue-remove-${position}`, async () => {
    const result = await api.post('/queue/remove', { position });
    applyQueue(result.queue);
  });
}

async function moveInQueue(from, to) {
  await withBusy(`queue-move-${from}`, async () => {
    const result = await api.post('/queue/move', { from, to });
    applyQueue(result.queue);
  });
}

/* --- Views: Favorites and history --------------------------------------- */

function renderFavorites() {
  const host = $('#view-favorites');
  const items = state.favorites.items || [];
  const signature = JSON.stringify(items.map((i) => i.track.id));
  const head = el('div', { class: 'view__head' },
    el('div', null,
      el('h1', { class: 'view__title', text: 'Favoriten' }),
      el('p', { class: 'view__subtitle', text: 'Lokal gespeichert, niemals hochgeladen.' })),
    el('div', { class: 'chip-row' },
      el('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: !items.length,
        onClick: async () => {
          const ok = await confirmAction({ title: 'Favoriten löschen?', text: 'Die lokale Favoritenliste wird vollständig entfernt.' });
          if (!ok) return;
          await withBusy('fav-clear', async () => {
            await api.post('/favorites/clear');
            await loadFavorites();
            renderFavorites();
            toast('Favoriten gelöscht.', { kind: 'ok' });
          });
        } }, icon(ICON.trash), 'Alle löschen')));
  let body = $('.view__body', host);
  if (!body) { body = el('div', { class: 'view__body' }); host.replaceChildren(head, body); }
  else host.replaceChild(head, host.firstElementChild);

  if (!items.length) {
    body.replaceChildren(emptyState({
      iconPath: ICON.heart,
      title: 'Noch keine Favoriten',
      text: 'Tippe auf das Herz neben einem Titel, um ihn hier zu sammeln. Die Liste bleibt auf diesem Rechner.',
    }));
    return;
  }
  const list = el('ul', { class: 'tracklist' });
  items.forEach((item) => list.append(trackRow(item.track)));
  body.replaceChildren(list, el('div', { class: 'sidebar__note', text: 'Gespeichert unter dem lokalen Zustandsverzeichnis des Servers.' }));
  host.dataset.sig = signature;
}

function renderHistory() {
  const host = $('#view-history');
  const items = state.history.items || [];
  const head = el('div', { class: 'view__head' },
    el('div', null,
      el('h1', { class: 'view__title', text: 'Verlauf' }),
      el('p', { class: 'view__subtitle', text: 'Zuletzt gespielte Titel, lokal gespeichert.' })),
    el('div', { class: 'chip-row' },
      el('button', { class: 'btn btn--ghost btn--sm', type: 'button', disabled: !items.length,
        onClick: async () => {
          const ok = await confirmAction({ title: 'Verlauf löschen?', text: 'Die lokale Historie wird vollständig entfernt.' });
          if (!ok) return;
          await withBusy('history-clear', async () => {
            await api.post('/history/clear');
            state.history = { items: [] };
            renderHistory();
            toast('Verlauf gelöscht.', { kind: 'ok' });
          });
        } }, icon(ICON.trash), 'Verlauf löschen')));
  let body = $('.view__body', host);
  if (!body) { body = el('div', { class: 'view__body' }); host.replaceChildren(head, body); }
  else host.replaceChild(head, host.firstElementChild);

  if (!items.length) {
    body.replaceChildren(emptyState({
      iconPath: ICON.clock,
      title: 'Noch kein Verlauf',
      text: 'Sobald du etwas abspielst, erscheint es hier — unabhängig davon, ob du ncmpcpp oder diese Oberfläche benutzt.',
    }));
    return;
  }
  const list = el('ul', { class: 'tracklist' });
  items.forEach((item) => {
    const row = trackRow(item.track);
    $('.track__sub', row).textContent = `${item.track.artist || item.track.channel || 'Unbekannt'} · ${formatRelative(item.played_at)}`;
    list.append(row);
  });
  body.replaceChildren(list);
}

/* --- Player ------------------------------------------------------------- */

const els = {
  title: $('#now-title'),
  artist: $('#now-artist'),
  cover: $('#now-cover'),
  coverImg: $('#now-cover-img'),
  fav: $('#now-favorite'),
  play: $('#btn-play'),
  prev: $('#btn-prev'),
  next: $('#btn-next'),
  shuffle: $('#btn-shuffle'),
  repeat: $('#btn-repeat'),
  mute: $('#btn-mute'),
  elapsed: $('#time-elapsed'),
  total: $('#time-total'),
  fill: $('#seek-fill'),
  buffered: $('#seek-buffered'),
  thumb: $('#seek-thumb'),
  track: $('#seek-track'),
  volume: $('#volume'),
  volumeFill: $('#volume-fill'),
  volumeThumb: $('#volume-thumb'),
  connection: $('#connection'),
  connectionText: $('#connection-text'),
  queueCount: $('#nav-queue-count'),
  statMpd: $('#stat-mpd'),
  statState: $('#stat-state'),
};

const STATE_LABEL = { playing: 'spielt', paused: 'pausiert', stopped: 'gestoppt' };

// A cover that 404s must fall back to the placeholder, not a broken-image icon.
els.coverImg.addEventListener('error', () => {
  els.coverImg.hidden = true;
  els.cover.dataset.empty = 'true';
});
els.coverImg.addEventListener('load', () => {
  els.coverImg.hidden = false;
  els.cover.dataset.empty = 'false';
});

function interpolatedElapsed() {
  if (state.scrubbing) return state.scrubValue;
  const base = state.elapsedBase;
  if (state.status.state !== 'playing') return base;
  const drift = state.statusAt ? Math.max(0, Date.now() / 1000 - state.statusAt) : 0;
  const duration = state.status.duration || 0;
  if (duration && base + drift > duration) return duration;
  return base + drift;
}

function paintPlayer() {
  const s = state.status;
  const current = s.current;
  const playing = s.state === 'playing';
  const duration = s.duration || 0;
  const elapsed = interpolatedElapsed();

  els.title.textContent = current?.title || 'Nichts wird abgespielt';
  els.artist.textContent = current
    ? (current.artist || 'Unbekannter Kanal')
    : state.queue.length ? 'Wähle einen Titel aus der Queue' : 'Suche Musik, um zu starten';

  const id = current ? trackKey(current) : '';
  if (els.cover.dataset.id !== (id || '')) {
    els.cover.dataset.id = id || '';
    els.coverImg.hidden = true;
    els.coverImg.removeAttribute('src');
    els.cover.dataset.empty = 'true';
    if (id) {
      els.coverImg.alt = '';
      els.coverImg.src = `/thumbnail/${id}`;
      els.coverImg.hidden = false;
    }
  }
  els.fav.hidden = !id;
  if (id) els.fav.setAttribute('aria-pressed', String(state.favoriteSet.has(id)));

  els.play.dataset.state = playing ? 'playing' : 'paused';
  // is-busy also suppresses further input: a second play/pause while the
  // first request is in flight would be a double toggle.
  els.play.classList.toggle('is-busy', state.busy.has('play'));
  els.play.setAttribute('aria-busy', String(state.busy.has('play')));
  els.play.setAttribute('aria-label', playing ? 'Pausieren' : 'Abspielen');
  els.play.disabled = !id && !state.queue.length;

  const pct = duration ? Math.min(100, (elapsed / duration) * 100) : 0;
  els.fill.style.width = `${pct}%`;
  els.thumb.style.left = `${pct}%`;
  els.elapsed.textContent = formatTime(elapsed);
  els.total.textContent = formatTime(duration);
  els.track.setAttribute('aria-valuenow', String(Math.round(pct)));
  els.track.setAttribute('aria-valuetext', `${formatTime(elapsed)} von ${formatTime(duration)}`);
  els.track.classList.toggle('is-scrubbing', state.scrubbing);

  const volume = state.volumeDraft ?? s.volume ?? 0;
  els.volumeFill.style.width = `${volume}%`;
  els.volumeThumb.style.left = `${volume}%`;
  els.volume.setAttribute('aria-valuenow', String(Math.round(volume)));
  els.volume.setAttribute('aria-valuetext', `${Math.round(volume)} Prozent`);
  const muted = (s.volume ?? 0) === 0;
  els.mute.setAttribute('aria-pressed', String(muted));

  els.shuffle.setAttribute('aria-pressed', String(!!s.shuffle));
  els.repeat.setAttribute('aria-pressed', String(!!(s.repeat || s.repeat_one)));
  els.repeat.dataset.mode = s.repeat_one ? 'one' : (s.repeat ? 'all' : 'off');

  els.queueCount.textContent = String(state.queue.length || 0);
  els.queueCount.hidden = !state.queue.length;

  els.statState.textContent = STATE_LABEL[s.state] || '–';
}

/** Keeps the progress bar moving smoothly between server updates. */
function startProgressLoop() {
  const tick = () => {
    if (!state.scrubbing && state.status.state === 'playing') paintPlayer();
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function setConnection(mode, text) {
  state.connection = mode;
  els.connection.dataset.state = mode;
  els.connectionText.textContent = text;
  els.connection.setAttribute('title', text);
  els.statMpd.textContent = mode === 'offline' ? 'nicht erreichbar' : 'verbunden';
}

function applyStatus(payload) {
  if (!payload || typeof payload !== 'object') return;
  const previousSong = state.status.current ? trackKey(state.status.current) : null;
  state.status = { ...state.status, ...payload };
  state.elapsedBase = payload.elapsed ?? 0;
  state.statusAt = Date.now() / 1000;
  paintPlayer();
  const song = payload.current ? trackKey(payload.current) : null;
  if (song && song !== previousSong) {
    // A new song means the local history changed; refresh it quietly.
    loadHistory().catch(() => {});
  }
  if (state.view === 'start') renderStart();
  if (state.view === 'queue') renderQueue();
}

function applyQueue(payload) {
  if (!payload) return;
  state.queue = payload;
  paintPlayer();
  if (state.view === 'queue') renderQueue();
  if (state.view === 'start') renderStart();
}

async function refreshStatus() {
  try {
    applyStatus(await api.get('/player/status'));
  } catch (err) {
    if (err.code === 'NETWORK' || err.status === 0) setConnection('offline', 'Server offline');
  }
}

async function refreshQueue() {
  try {
    applyQueue(await api.get('/queue'));
  } catch { /* transient; the next tick or mutation recovers */ }
}

async function loadFavorites() {
  const payload = await api.get('/favorites');
  state.favorites = payload;
  state.favoriteSet = new Set(payload.ids || []);
  paintFavorites();
  paintPlayer();
}

async function loadHistory() {
  const payload = await api.get('/history');
  state.history = payload;
  if (state.view === 'history') renderHistory();
}

function paintFavorites() {
  $$('[data-action="favorite"]').forEach((btn) => {
    const row = btn.closest('.track');
    if (row?.dataset.id) btn.setAttribute('aria-pressed', String(state.favoriteSet.has(row.dataset.id)));
  });
}

/* --- Transport ---------------------------------------------------------- */

async function togglePlay() {
  if (state.busy.has('play')) return;
  state.busy.add('play');
  paintPlayer();
  try {
    const playing = state.status.state === 'playing';
    if (playing) applyStatus(await api.post('/player/pause'));
    else if (state.status.position) applyStatus(await api.post('/player/play', { position: state.status.position }));
    else applyStatus(await api.post('/player/play'));
  } catch (err) {
    toastError(err);
  } finally {
    state.busy.delete('play');
    paintPlayer();
  }
}

async function transport(path, label) {
  try {
    applyStatus(await api.post(path));
  } catch (err) {
    toast(label ? `${label} nicht möglich: ${err.message}` : err.message, { kind: 'error' });
  }
}

function seekTo(seconds) {
  const duration = state.status.duration || 0;
  const target = Math.max(0, Math.min(seconds, duration || seconds));
  state.scrubbing = false;
  return api.post('/player/seek', { seconds: Math.round(target) })
    .then(applyStatus)
    .catch(toastError);
}

/* --- Sliders (pointer + keyboard) --------------------------------------- */

function wireSlider(node, { onScrub, onCommit, formatValue }) {
  const ratioFrom = (event) => {
    const rect = node.getBoundingClientRect();
    if (!rect.width) return 0;
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  };
  node.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    node.setPointerCapture(e.pointerId);
    node.dataset.scrubbing = '1';
    onScrub(ratioFrom(e));
  });
  node.addEventListener('pointermove', (e) => {
    if (node.dataset.scrubbing !== '1') return;
    onScrub(ratioFrom(e));
  });
  const finish = (e) => {
    if (node.dataset.scrubbing !== '1') return;
    delete node.dataset.scrubbing;
    try { node.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    onCommit(ratioFrom(e));
  };
  node.addEventListener('pointerup', finish);
  node.addEventListener('pointercancel', finish);
  node.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 10 : 1;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); onScrub(formatValue(-step)); }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); onScrub(formatValue(step)); }
    else if (e.key === 'Home') { e.preventDefault(); onScrub(0); }
    else if (e.key === 'End') { e.preventDefault(); onScrub(formatValue(1e9)); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onCommit(currentRatio(node)); }
  });
}

let seekRatio = 0;
let volumeRatio = 0.8;

const currentRatio = (node) => (node === els.track ? seekRatio : volumeRatio);

wireSlider(els.track, {
  onScrub: (r) => {
    const duration = state.status.duration || 0;
    seekRatio = r;
    state.scrubbing = true;
    state.scrubValue = duration * r;
    els.fill.style.width = `${r * 100}%`;
    els.thumb.style.left = `${r * 100}%`;
    els.elapsed.textContent = formatTime(state.scrubValue);
  },
  onCommit: (r) => {
    const duration = state.status.duration || 0;
    state.scrubbing = false;
    if (!duration) { paintPlayer(); return; }
    seekTo(duration * r);
  },
  formatValue: (delta) => Math.max(0, Math.min(1, seekRatio + delta / (state.status.duration || 100))),
});

wireSlider(els.volume, {
  onScrub: (r) => {
    volumeRatio = r;
    state.volumeDraft = Math.round(r * 100);
    els.volumeFill.style.width = `${r * 100}%`;
    els.volumeThumb.style.left = `${r * 100}%`;
  },
  onCommit: (r) => {
    const value = Math.round(r * 100);
    state.volumeDraft = null;
    volumeRatio = r;
    api.post('/player/volume', { volume: value }).then(applyStatus).catch((err) => {
      state.volumeDraft = null;
      toastError(err);
    });
  },
  formatValue: (delta) => Math.max(0, Math.min(1, volumeRatio + delta / 100)),
});

els.play.addEventListener('click', togglePlay);
els.prev.addEventListener('click', () => transport('/player/previous', 'Vorheriger Titel'));
els.next.addEventListener('click', () => transport('/player/next', 'Nächster Titel'));
els.mute.addEventListener('click', () => {
  const muted = (state.status.volume ?? 0) === 0;
  api.post('/player/mute', { muted: !muted }).then(applyStatus).catch(toastError);
});
els.shuffle.addEventListener('click', () => {
  api.post('/player/modes', { shuffle: !state.status.shuffle }).then(applyStatus).catch(toastError);
});
els.repeat.addEventListener('click', () => {
  const next = state.status.repeat_one ? 'off' : (state.status.repeat ? 'one' : 'all');
  const body = next === 'off' ? { repeat: false, repeat_one: false } : { repeat: next === 'all', repeat_one: next === 'one' };
  api.post('/player/modes', body).then(applyStatus).catch(toastError);
});
els.fav.addEventListener('click', () => {
  if (state.status.current) toggleFavorite(state.status.current).catch(toastError);
});

/* --- Live updates ------------------------------------------------------- */

let eventSource = null;
let pollTimer = null;

function startPolling() {
  if (pollTimer) return;
  setConnection('polling', 'Live (Polling)');
  pollTimer = setInterval(() => { refreshStatus(); refreshQueue(); }, 2000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function connectLive() {
  if (eventSource || typeof EventSource === 'undefined') { startPolling(); return; }
  setConnection('connecting', 'Verbinde …');
  const source = new EventSource('/events');
  eventSource = source;

  source.addEventListener('open', () => {
    stopPolling();
    setConnection('live', 'Live');
  });
  source.addEventListener('message', (event) => {
    try { applyStatus(JSON.parse(event.data)); } catch { /* malformed frame */ }
  });
  source.addEventListener('error', () => {
    if (eventSource !== source) return;
    // EventSource retries on its own; if the server is really gone the
    // polling fallback keeps the UI truthful instead of silently freezing.
    if (source.readyState === EventSource.CLOSED) {
      eventSource = null;
      source.close();
      setConnection('polling', 'Live (Polling)');
      startPolling();
      setTimeout(connectLive, 15000);
    } else {
      setConnection('connecting', 'Reconnecte …');
    }
  });
  source.addEventListener('reconnect', () => { window.location.reload(); });
}

/* --- Router ------------------------------------------------------------- */

const VIEWS = ['start', 'search', 'queue', 'favorites', 'history'];

function navigate(target) {
  const [view, query] = String(target).replace(/^#\/?/, '').split('?');
  const name = VIEWS.includes(view) ? view : 'start';
  const next = `#/${name}${query ? `?${query}` : ''}`;
  if (window.location.hash === next) { showView(name); return; }
  window.location.hash = next;
}

function showView(name) {
  state.view = name;
  for (const view of VIEWS) {
    const node = $(`#view-${view}`);
    if (!node) continue;
    const active = view === name;
    node.hidden = !active;
    if (active) {
      node.classList.remove('is-entering');
      void node.offsetWidth; // restart the entrance animation
      node.classList.add('is-entering');
    }
  }
  $$('.nav__item').forEach((link) => link.classList.toggle('is-active', link.dataset.view === name));
  closeNav();
  $('#main').scrollTop = 0;

  if (name === 'start') renderStart();
  if (name === 'queue') renderQueue();
  if (name === 'favorites') renderFavorites();
  if (name === 'history') renderHistory();
  if (name === 'search') {
    const params = new URLSearchParams((window.location.hash.split('?')[1] || ''));
    const q = params.get('q') || $('#search-input').value.trim();
    if (q) { $('#search-input').value = q; runSearch(q); }
    else renderSearch();
  }
}

function onHashChange() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [view] = raw.split('?');
  showView(VIEWS.includes(view) ? view : 'start');
}

window.addEventListener('hashchange', onHashChange);

/* --- Navigation drawer (mobile) ----------------------------------------- */

const app = $('#app');

function closeNav() {
  app.classList.remove('nav-open');
  $('#scrim').hidden = true;
  $('#nav-toggle').setAttribute('aria-expanded', 'false');
}

$('#nav-toggle').addEventListener('click', () => {
  const open = !app.classList.contains('nav-open');
  app.classList.toggle('nav-open', open);
  $('#scrim').hidden = !open;
  $('#nav-toggle').setAttribute('aria-expanded', String(open));
});
$('#scrim').addEventListener('click', closeNav);

/* --- Search bar --------------------------------------------------------- */

const searchInput = $('#search-input');

searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const value = searchInput.value.trim();
  if (!value) { $('#search-spinner').hidden = true; return; }
  // Debounced so typing never fires a request per keystroke.
  searchTimer = setTimeout(() => {
    if (state.view !== 'search') navigate(`search?q=${encodeURIComponent(value)}`);
    else runSearch(value);
  }, 380);
});

$('#search-form').addEventListener('submit', (e) => {
  e.preventDefault();
  clearTimeout(searchTimer);
  const value = searchInput.value.trim();
  if (!value) return;
  navigate(`search?q=${encodeURIComponent(value)}`);
  if (state.view === 'search') runSearch(value, true);
  searchInput.blur();
});

/* --- Keyboard shortcuts ------------------------------------------------- */

function isTypingTarget(node) {
  if (!node) return false;
  const tag = node.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || node.isContentEditable;
}

document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
  if (sheet.open) return;

  if (e.key === '/' && !isTypingTarget(e.target)) {
    e.preventDefault();
    navigate('search');
    setTimeout(() => searchInput.focus(), 0);
    return;
  }
  if (isTypingTarget(e.target)) return;

  switch (e.key) {
    case ' ':
      e.preventDefault();
      togglePlay();
      break;
    case 'ArrowRight':
      e.preventDefault();
      if (e.shiftKey) transport('/player/next', 'Nächster Titel');
      else if (state.status.duration) seekTo(interpolatedElapsed() + 5);
      break;
    case 'ArrowLeft':
      e.preventDefault();
      if (e.shiftKey) transport('/player/previous', 'Vorheriger Titel');
      else if (state.status.duration) seekTo(interpolatedElapsed() - 5);
      break;
    case 'n': transport('/player/next', 'Nächster Titel'); break;
    case 'p': transport('/player/previous', 'Vorheriger Titel'); break;
    case 'm': els.mute.click(); break;
    case 'f': if (state.status.current) toggleFavorite(state.status.current).catch(toastError); break;
    case '?': showShortcuts(); break;
    default: break;
  }
});

function showShortcuts() {
  contextMenu($('#search-form'), [
    { label: 'Leertaste — Wiedergabe / Pause', icon: ICON.play, action: () => {} },
    { label: 'Pfeil rechts / links — 5 s springen', icon: ICON.play, action: () => {} },
    { label: 'Shift + Pfeil — nächster / vorheriger Titel', icon: ICON.play, action: () => {} },
    { label: '/ — Suche fokussieren', icon: ICON.search, action: () => {} },
    { label: 'N / P / M / F — nächster, vorheriger, stumm, Favorit', icon: ICON.play, action: () => {} },
  ]);
}

/* --- Boot --------------------------------------------------------------- */

async function boot() {
  state.searchType = 'songs';
  if (!window.location.hash) window.location.hash = '#/start';

  setConnection('connecting', 'Verbinde …');
  try {
    await Promise.all([loadFavorites(), loadHistory()]);
  } catch (err) {
    toast('Einige lokale Daten konnten nicht geladen werden.', { kind: 'error', hint: err.message });
  }
  try {
    applyStatus(await api.get('/player/status'));
    await refreshQueue();
    setConnection('live', 'Verbunden');
  } catch (err) {
    setConnection('offline', 'Server offline');
    toastError(err);
  }
  onHashChange();
  connectLive();
  startProgressLoop();

  // Cheap guard against a stale tab: the MPD state is authoritative.
  window.addEventListener('focus', () => { refreshStatus(); refreshQueue(); });
}

boot();
