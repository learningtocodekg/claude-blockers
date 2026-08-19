'use strict';

const POLL_MS = 2000;

const state = {
  status: 'open',
  project: null,
  view: 'recent',          // 'recent' | 'project'
  menuOpen: false,
  clearMenuOpen: false,    // the delete-scope popup above the clear button
  collapsed: new Set(),    // project names collapsed in the grouped view
  selectedId: null,
  knownOpenIds: null,      // null until the first poll, so a cold start is not "all new"
  last: null,              // last /api/state payload, for re-rendering without a fetch
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

/* Minimal inline formatting: `code` and **bold**. Everything else stays literal,
   which is the safe default for text written by another program. */
function fmt(text) {
  return esc(text)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
}

/* --------------------------------------------------------------- palette */

const KIND = {
  blocker:    { short: 'blocker',    label: 'hard blocker',     color: 'var(--rose)' },
  question:   { short: 'question',   label: 'needs a decision', color: 'var(--blue)' },
  review:     { short: 'review',     label: 'needs review',     color: 'var(--green)' },
  idle:       { short: 'waiting',    label: 'waiting on you',   color: 'var(--amber)' },
  permission: { short: 'permission', label: 'needs permission', color: 'var(--purple)' },
};
const kindOf = (k) => KIND[k] || { short: k || 'blocker', label: k || 'blocker', color: 'var(--blue)' };

/* The design hard-codes a colour per project. These are assigned by position in
   the name-sorted project list instead, so any project gets one without being
   listed anywhere.
   Hashing the name was the obvious approach and the wrong one: two projects can
   hash to neighbouring hues and end up near-identical, which is precisely the
   distinction the colour exists to make. */
const HUES = [250, 152, 22, 80, 300, 195, 120, 340];
const projectHue = new Map();

function indexProjects(projects) {
  const names = projects.map((p) => p.project).sort();
  projectHue.clear();
  names.forEach((name, i) => projectHue.set(name, HUES[i % HUES.length]));
}

function projectColor(name) {
  const hue = projectHue.get(name);
  if (hue !== undefined) return `oklch(0.74 0.14 ${hue})`;
  let h = 0;                       // only for a project the server has not listed
  for (const ch of String(name || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `oklch(0.74 0.14 ${HUES[h % HUES.length]})`;
}

/* ------------------------------------------------------------------ time */

function asDate(iso) {
  return new Date(iso.endsWith('Z') ? iso : iso + 'Z');
}

function ago(iso) {
  if (!iso) return '';
  const secs = Math.max(0, (Date.now() - asDate(iso).getTime()) / 1000);
  if (secs < 60) return 'now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

function clockTime(iso) {
  if (!iso) return '';
  const d = asDate(iso);
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const label = dayLabel(iso);
  if (label === 'Today') return `today ${time}`;
  if (label === 'Yesterday') return `yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

function dayLabel(iso) {
  if (!iso) return '';
  const d = asDate(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return 'Today';
  const yest = new Date(today);
  yest.setDate(today.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return 'Yesterday';
  // The year earns its place only once the date is outside this one -- otherwise
  // last August and this August come out sharing a label.
  const opts = { month: 'long', day: 'numeric' };
  if (d.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString([], opts);
}

/* ---------------------------------------------------------------- toasts */

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* -------------------------------------------------------------- confirm */

/* One action here cannot be undone, so it asks first. The browser's own
   confirm() would do the job, but it lands as a bright system box on top of a
   dark board and reads as a browser error rather than a question from the app. */
function confirmDialog(title, message, yesLabel) {
  const wrap = $('confirm-wrap');
  $('confirm-title').textContent = title;
  $('confirm-body').textContent = message;
  $('confirm-yes').textContent = yesLabel;
  wrap.classList.remove('hidden');
  $('confirm-no').focus();

  return new Promise((resolve) => {
    const done = (answer) => {
      wrap.classList.add('hidden');
      $('confirm-yes').onclick = null;
      $('confirm-no').onclick = null;
      wrap.onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(answer);
    };
    const onKey = (ev) => { if (ev.key === 'Escape') done(false); };
    $('confirm-yes').onclick = () => done(true);
    $('confirm-no').onclick = () => done(false);
    wrap.onclick = (ev) => { if (ev.target === wrap) done(false); };
    document.addEventListener('keydown', onKey);
  });
}

/* --------------------------------------------------------- notifications */

const notifyOn = () => 'Notification' in window && Notification.permission === 'granted';

function renderNotifyToggle() {
  $('notify-toggle').setAttribute('aria-checked', notifyOn() ? 'true' : 'false');
}

$('notify-toggle').addEventListener('click', async () => {
  if (!('Notification' in window)) return toast('This browser has no notification support.', 'error');
  if (Notification.permission === 'granted') {
    return toast('Already on. Turn them off in your browser site settings.');
  }
  const result = await Notification.requestPermission();
  renderNotifyToggle();
  toast(result === 'granted' ? 'Desktop notifications enabled.' : 'Permission denied.',
        result === 'granted' ? 'ok' : 'error');
});

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 660;
    osc.type = 'sine';
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.09, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.32);
    osc.start(); osc.stop(ctx.currentTime + 0.34);
    setTimeout(() => ctx.close(), 600);
  } catch (_) { /* audio is a nicety, never a failure */ }
}

function announce(blockers) {
  for (const b of blockers) {
    const body = `${b.project} — ${b.title}`;
    if (notifyOn()) {
      const n = new Notification('Claude needs you', {
        body,
        tag: `blocker-${b.id}`,
        requireInteraction: b.urgency === 'high',
      });
      n.onclick = () => { window.focus(); select(b.id); n.close(); };
    } else {
      toast(body);
    }
  }
  if (blockers.length) beep();
}

/* ------------------------------------------------------------ title bar */

function renderChrome(data) {
  const s = data.stats;
  const pill = $('open-pill');
  pill.textContent = `${s.open} open`;
  pill.classList.toggle('clear', !s.open);
  document.title = s.open ? `(${s.open}) Claude Blockers` : 'Claude Blockers';

  const live = data.sessions ? data.sessions.live : 0;
  $('foot-dot').classList.toggle('down', live === 0);
  $('foot-text').textContent =
    `hook connected · ${live} session${live === 1 ? '' : 's'} live`;
}

/* ---------------------------------------------------------- clear button */

/* Deleting is one button with a scope attached. The button itself does the only
   scope that is safe to hand someone in a single click -- the closed history --
   and the caret beside it opens the rest, ending with the one that takes open
   blockers too. Keeping them in one control means the wider scopes are findable
   without a second destructive button sitting on the board full-time. */
const CLEAR_SCOPES = [
  {
    key: 'resolved', label: 'Resolved only', statuses: ['resolved'],
    title: 'Clear resolved blockers?', yes: 'Delete permanently',
  },
  {
    key: 'dismissed', label: 'Dismissed only', statuses: ['dismissed'],
    title: 'Clear dismissed blockers?', yes: 'Delete permanently',
  },
  {
    key: 'past', label: 'All closed', statuses: ['resolved', 'dismissed'],
    title: 'Clear past blockers?', yes: 'Delete permanently',
  },
  {
    key: 'everything', label: 'Everything, open included', danger: true,
    statuses: ['open', 'resolved', 'dismissed'],
    title: 'Delete every blocker?', yes: 'Delete everything',
  },
];
const scopeByKey = (key) => CLEAR_SCOPES.find((s) => s.key === key);

const pastCount = (stats) => (stats.resolved || 0) + (stats.dismissed || 0);
const countFor = (scope, stats) =>
  scope.statuses.reduce((n, key) => n + (stats[key] || 0), 0);

function joinList(parts) {
  if (parts.length < 2) return parts.join('');
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}

function clearMessage(scope, s) {
  const parts = scope.statuses
    .filter((key) => s[key])
    .map((key) => `${s[key]} ${key}`);
  const total = countFor(scope, s);
  const head = `${joinList(parts)} blocker${total === 1 ? '' : 's'} will be deleted `
             + 'from the database for good.';
  if (!scope.statuses.includes('open')) return `${head} Anything still open is left alone.`;
  if (!s.open) return head;
  return `${head} That includes ${s.open} still open and waiting on you — `
       + 'nothing on the board survives this.';
}

function renderClear(data) {
  const s = data.stats;
  const past = pastCount(s);
  // The panel stays up whenever anything at all is deletable: with only open
  // rows on the board the button has nothing to do, but the menu still does.
  $('side-clear').classList.toggle('hidden', (s.total || 0) === 0);
  $('clear-count').textContent = past ? String(past) : '';
  $('clear-btn').disabled = past === 0;
  renderClearMenu(s);
}

function renderClearMenu(s) {
  const menu = $('clear-menu');
  $('clear-more').setAttribute('aria-expanded', state.clearMenuOpen ? 'true' : 'false');
  menu.classList.toggle('hidden', !state.clearMenuOpen);
  if (!state.clearMenuOpen) return;

  let html = '<div class="menu-label">Delete permanently</div>';
  for (const scope of CLEAR_SCOPES) {
    const n = countFor(scope, s);
    if (scope.key === 'everything') html += '<div class="menu-sep"></div>';
    html += `<button class="menu-item${scope.danger ? ' danger' : ''}"
        data-scope="${scope.key}"${n ? '' : ' disabled'}>${esc(scope.label)}
        <span class="mcount">${n}</span></button>`;
  }
  menu.innerHTML = html;

  menu.querySelectorAll('[data-scope]').forEach((el) => {
    el.onclick = () => {
      state.clearMenuOpen = false;
      renderClearMenu(s);
      runClear(scopeByKey(el.dataset.scope));
    };
  });
}

async function runClear(scope) {
  if (!scope || !state.last) return;
  const s = state.last.stats;
  if (!countFor(scope, s)) return;

  const ok = await confirmDialog(scope.title, clearMessage(scope, s), scope.yes);
  if (!ok) return;

  let removed = 0;
  try {
    const res = await fetch('/api/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ statuses: scope.statuses }),
    });
    if (!res.ok) throw new Error(res.status);
    removed = (await res.json()).removed || 0;
  } catch (_) {
    return toast('Could not clear them — the board server did not answer.', 'error');
  }

  // Whatever was on screen may have just been one of them.
  if (state.selectedId) {
    const still = await fetch(`/api/blocker/${state.selectedId}`).catch(() => null);
    if (!still || !still.ok) closeDetail();
  }
  await refresh(true);
  toast(`Deleted ${removed} blocker${removed === 1 ? '' : 's'}.`, 'ok');
}

$('clear-btn').addEventListener('click', () => runClear(scopeByKey('past')));

$('clear-more').addEventListener('click', (ev) => {
  ev.stopPropagation();
  state.clearMenuOpen = !state.clearMenuOpen;
  if (state.last) renderClearMenu(state.last.stats);
});

/* -------------------------------------------------------------- sidebar */

function renderScopes() {
  const holder = $('scopes');
  const scopes = [
    ['open', 'Open'], ['resolved', 'Resolved'],
    ['dismissed', 'Dismissed'], ['all', 'All'],
  ];
  holder.innerHTML = '';
  for (const [key, label] of scopes) {
    const btn = document.createElement('button');
    btn.className = 'scope' + (state.status === key ? ' on' : '');
    btn.textContent = label;
    btn.onclick = () => { state.status = key; refresh(true); };
    holder.appendChild(btn);
  }
}

/* What the sidebar picker drops down: how the list is ordered, and which project
   it is narrowed to. Both live here so the sidebar keeps its shape. */
function renderMenu(data) {
  const menu = $('menu');
  $('menu-btn').setAttribute('aria-expanded', state.menuOpen ? 'true' : 'false');
  menu.classList.toggle('hidden', !state.menuOpen);
  if (!state.menuOpen) return;

  const views = [['recent', 'Most recent'], ['project', 'By project']];
  let html = '<div class="menu-label">Sidebar view</div>';
  for (const [key, label] of views) {
    html += `<button class="menu-item${state.view === key ? ' on' : ''}" data-view="${key}">
        <span class="tick">${state.view === key ? '&#10003;' : ''}</span>${esc(label)}</button>`;
  }

  html += '<div class="menu-sep"></div><div class="menu-label">Filter by project</div>';
  html += `<button class="menu-item${state.project ? '' : ' on'}" data-project="">
      <span class="tick">${state.project ? '' : '&#10003;'}</span>All projects
      <span class="mcount">${data.stats.open}</span></button>`;
  for (const p of data.projects) {
    const on = state.project === p.project;
    html += `<button class="menu-item${on ? ' on' : ''}" data-project="${esc(p.project)}">
        <span class="tick">${on ? '&#10003;' : ''}</span>
        <span class="swatch" style="background:${projectColor(p.project)}"></span>
        ${esc(p.project)}<span class="mcount">${p.open_count}</span></button>`;
  }
  menu.innerHTML = html;

  menu.querySelectorAll('[data-view]').forEach((el) => {
    el.onclick = () => { state.view = el.dataset.view; state.menuOpen = false; renderAll(); };
  });
  menu.querySelectorAll('[data-project]').forEach((el) => {
    el.onclick = () => {
      state.project = el.dataset.project || null;
      state.menuOpen = false;
      refresh(true);
    };
  });
}

$('menu-btn').addEventListener('click', (ev) => {
  ev.stopPropagation();
  state.menuOpen = !state.menuOpen;
  if (state.last) renderMenu(state.last);
});
document.addEventListener('click', (ev) => {
  if (state.menuOpen && !ev.target.closest('.side-head')) {
    state.menuOpen = false;
    $('menu').classList.add('hidden');
    $('menu-btn').setAttribute('aria-expanded', 'false');
  }
  if (state.clearMenuOpen && !ev.target.closest('.side-clear')) {
    state.clearMenuOpen = false;
    $('clear-menu').classList.add('hidden');
    $('clear-more').setAttribute('aria-expanded', 'false');
  }
});

/* The server hands back one order for everybody: open first, then high urgency,
   then newest. That is the right order for a to-do list and the wrong one for a
   dated list -- urgency cuts the rows into three separately-descending runs, so
   the day dividers came out reading Yesterday, then Today, then Yesterday again,
   with a low-urgency row from 21:49 sitting below a normal one from 21:47.
   So each view sorts for itself and the server's order is only the default. */

const byNewest = (a, b) =>
  asDate(b.created_at) - asDate(a.created_at) || b.id - a.id;

// Timestamps are second-precision, so same-second ties are real; the higher id
// is the later one and keeps the sort stable across polls.
const URGENCY_RANK = { high: 0, normal: 1, low: 2 };
const rank = (u) => (u in URGENCY_RANK ? URGENCY_RANK[u] : 1);

const byPriority = (a, b) =>
  (a.status === 'open' ? 0 : 1) - (b.status === 'open' ? 0 : 1)
  || rank(a.urgency) - rank(b.urgency)
  || byNewest(a, b);

function rowEl(b) {
  const k = kindOf(b.kind);
  const off = b.status !== 'open';
  const el = document.createElement('div');
  el.className = 'row' + (b.id === state.selectedId ? ' sel' : '') + (off ? ' off' : '');
  el.style.setProperty('--k', k.color);
  el.style.setProperty('--p', projectColor(b.project));
  el.onclick = () => select(b.id);
  el.innerHTML = `
    <span class="kdot"></span>
    <div class="rmain">
      <div class="rtitle">${esc(b.title)}</div>
      <div class="rmeta">
        <span class="rid">#${b.id}</span>
        ${b.urgency === 'high' ? '<span class="rhot">high</span>' : ''}
        <span class="psq"></span>
        <span class="pname">${esc(b.project)}</span>
        ${b.branch ? `<span class="slash">/</span><span>${esc(b.branch)}</span>` : ''}
        <span style="flex:1"></span>
        ${(!b.seen && b.status === 'open') ? '<span class="unseen"></span>' : ''}
        <span>${esc(ago(b.created_at))}</span>
      </div>
    </div>`;
  return el;
}

function renderList(data) {
  const list = $('side-list');
  list.innerHTML = '';
  const blockers = data.blockers;

  if (!blockers.length) {
    list.innerHTML = '<div class="side-empty">Nothing is waiting on you.<br>'
      + 'When a session needs you to test, decide, or supply something, it lands here.</div>';
    return;
  }

  if (state.view === 'recent') {
    let lastDay = null;
    // Sorted on a copy: state.last is the server's payload and other readers of
    // it should not find it reordered underneath them.
    for (const b of [...blockers].sort(byNewest)) {
      const day = dayLabel(b.created_at);
      if (day !== lastDay) {
        lastDay = day;
        const div = document.createElement('div');
        div.className = 'day-divider';
        div.textContent = day;
        list.appendChild(div);
      }
      list.appendChild(rowEl(b));
    }
    return;
  }

  // Grouped by project.
  const byProject = new Map();
  for (const b of blockers) {
    if (!byProject.has(b.project)) byProject.set(b.project, []);
    byProject.get(b.project).push(b);
  }
  // Map order would follow the server's, which puts whichever project happens to
  // own the most urgent row on top -- so a group jumps position when a blocker
  // somewhere inside it changes urgency or gets closed. Size then name is stable
  // and matches how the project filter lists them.
  const groups = [...byProject.entries()]
    .sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));

  for (const [name, items] of groups) {
    items.sort(byPriority);
    const grp = document.createElement('div');
    grp.className = 'grp';
    const collapsed = state.collapsed.has(name);
    const head = document.createElement('button');
    head.className = 'grp-head';
    head.style.setProperty('--p', projectColor(name));
    head.innerHTML = `
      <span class="grp-caret">${collapsed ? '&#9656;' : '&#9662;'}</span>
      <span class="grp-sq"></span>
      <span class="grp-name">${esc(name)}</span>
      <span class="grp-count${items.length ? '' : ' zero'}">${items.length}</span>`;
    head.onclick = () => {
      if (collapsed) state.collapsed.delete(name); else state.collapsed.add(name);
      renderAll();
    };
    grp.appendChild(head);
    if (!collapsed) items.forEach((b) => grp.appendChild(rowEl(b)));
    list.appendChild(grp);
  }
}

/* --------------------------------------------------------------- detail */

async function select(id) {
  state.selectedId = id;

  const res = await fetch(`/api/blocker/${id}`);
  if (!res.ok) return toast('Could not load that blocker.', 'error');
  const b = await res.json();

  history.replaceState(null, '', `#${id}`);

  fetch('/api/seen', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: [id] }),
  }).catch(() => {});

  renderDetail(b);
  if (state.last) { renderList(state.last); renderStatusBar(state.last, b); }
  refresh(false);
}

/* What the user pastes into a session that knows nothing about this card. It
   names the tool as well as the number, because a session with the MCP server
   attached but no memory of it otherwise has to be told where to look. */
function handoffPrompt(b) {
  return `Read blocker #${b.id} with the claude-blockers MCP server (read_blocker), `
       + 'work out what to do, then record it with answer_blocker.';
}

function renderTab(b) {
  const k = kindOf(b.kind);
  $('tabbar').innerHTML = `
    <div class="tab" style="--k:${k.color}">
      <span class="kdot"></span>
      <span class="tname">${esc(b.title)}</span>
      <button class="tclose" id="tab-close" title="Close">&#215;</button>
    </div>
    <div class="tabpath">${esc(b.cwd || '')}</div>`;
  $('tab-close').onclick = closeDetail;
}

function closeDetail() {
  state.selectedId = null;
  history.replaceState(null, '', location.pathname);
  $('tabbar').innerHTML = '';
  $('detail').classList.add('hidden');
  $('placeholder').classList.remove('hidden');
  if (state.last) { renderList(state.last); renderStatusBar(state.last, null); }
}

function renderDetail(b) {
  const k = kindOf(b.kind);
  const open = b.status === 'open';
  // The Desktop app has no session to resume and no folder it was working in.
  // Saying "not recorded" there reads as something lost; it was never there.
  const desktop = b.surface === 'claude-desktop';
  const stClass = b.status === 'resolved' ? 'st-resolved' : (open ? 'st-open' : '');

  renderTab(b);
  $('placeholder').classList.add('hidden');
  const host = $('detail');
  host.classList.remove('hidden');
  host.style.setProperty('--k', k.color);
  host.style.setProperty('--p', projectColor(b.project));

  host.innerHTML = `
    <div class="d-head">
      <div class="d-head-main">
        <h1 class="d-title">${esc(b.title)}</h1>
        <div class="d-badges">
          <button class="badge bid" data-copy="${b.id}" title="Copy this id">#${b.id}</button>
          <span class="badge kind"><i></i>${esc(k.label)}</span>
          <span class="badge${b.urgency === 'high' ? ' hot' : ''}">${esc(b.urgency)} urgency</span>
          <span class="badge ${stClass}">${esc(b.status)}</span>
          <span class="d-when">${esc(clockTime(b.created_at))}</span>
          <span class="d-ago">· ${esc(ago(b.created_at))} ago</span>
        </div>
      </div>
      <div class="d-actions">
        ${open ? '<button class="btn done" id="resolve-btn">Mark done</button>' : ''}
        ${open ? '<button class="btn" id="dismiss-btn">Dismiss</button>' : ''}
        ${open ? '' : '<button class="btn" id="reopen-btn">Reopen</button>'}
        ${b.session_id ? `<button class="btn ghost" id="jump-btn">${
            b.live ? 'Open a second copy' : 'Open in a new terminal'}</button>` : ''}
      </div>
    </div>

    <div class="s-card">
      <div class="s-row">
        <span class="s-key">Session</span>
        ${desktop
          ? '<span class="s-branch">Claude Desktop &mdash; no session to jump back to</span>'
          : `<code class="s-val">${esc(b.session_id || 'not recorded')}</code>
             ${b.session_id ? `<button class="copy" data-copy="${esc(b.session_id)}">copy</button>` : ''}
             <span class="s-live ${b.live ? '' : 'dead'}"><i></i>${b.live ? 'session running' : 'not running'}</span>`}
      </div>
      <div class="s-row">
        <span class="s-key">Project</span>
        <span class="s-proj"><span class="psq"></span>${esc(b.project)}</span>
        ${b.branch ? `<span class="s-branch">${esc(b.branch)}</span>` : ''}
        ${b.repo && b.repo !== b.project ? `<span class="s-branch">repo ${esc(b.repo)}</span>` : ''}
      </div>
      ${b.cwd ? `<div class="s-row">
        <span class="s-key">Folder</span>
        <code class="s-val">${esc(b.cwd)}</code>
        <button class="copy" data-copy="${esc(b.cwd)}">copy</button>
      </div>` : ''}
      ${b.host ? `<div class="s-row">
        <span class="s-key">Ran on</span>
        <span class="s-branch">${esc(b.host)}</span>
      </div>` : ''}
    </div>

    <div class="d-cols">
      <div>
        <div class="d-label blocked">What's blocked</div>
        <p class="prose">${b.detail ? fmt(b.detail) : '<span style="color:var(--t-faint)">Nothing recorded.</span>'}</p>
      </div>
      <div>
        <div class="d-label unblock">How to unblock it</div>
        <p class="prose">${b.how_to ? fmt(b.how_to) : '<span style="color:var(--t-faint)">Nothing recorded.</span>'}</p>
      </div>
    </div>

    ${b.resume_command ? `<div class="d-block">
      <div class="d-label">${b.live ? 'Open it yourself' : 'Reopen this session'}</div>
      ${b.live ? '<p class="hint">This session is still running. Switch to its tab in your terminal &mdash; the command below would start a second copy of the conversation.</p>' : ''}
      <div class="cmd">
        <span class="caret">&gt;</span>
        <code>${esc(b.resume_command)}</code>
        <button class="copy" data-copy="${esc(b.resume_command)}">copy</button>
      </div>
    </div>` : ''}

    <div class="d-block">
      <div class="d-label handoff">Hand it to a fresh session</div>
      <p class="hint">Any Claude session can open this card by its number &mdash; it does not
      have to be the one that raised it, or even be in the same folder. Paste this into a new
      session and it will read the blocker and record what it decides back here.</p>
      <div class="cmd wrap">
        <span class="caret">&gt;</span>
        <code>${esc(handoffPrompt(b))}</code>
        <button class="copy" data-copy="${esc(handoffPrompt(b))}">copy</button>
      </div>
    </div>

    ${b.answer ? `<div class="d-block">
      <div class="d-label answered">Decision</div>
      <p class="hint">Recorded by ${esc(b.answered_by || 'another session')}${
        b.answered_at ? ' · ' + esc(clockTime(b.answered_at)) : ''}</p>
      <p class="prose">${fmt(b.answer)}</p>
    </div>` : ''}

    ${b.resolution ? `<div class="d-block">
      <div class="d-label">Resolution</div>
      <p class="prose">${fmt(b.resolution)}</p>
    </div>` : ''}

    <div class="d-block">
      <button class="tx-toggle" id="tx-toggle">
        <span class="tx-caret" id="tx-caret">&#9656;</span>
        <span class="tx-label">Last turns of that session</span>
        <span class="tx-hint" id="tx-hint">show turns</span>
      </button>
      <div id="tx-body"></div>
    </div>`;

  host.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        const was = btn.textContent;
        btn.textContent = 'copied';
        setTimeout(() => (btn.textContent = was), 1400);
      } catch (_) { toast('Clipboard blocked by the browser.', 'error'); }
    };
  });

  const wire = (elId, action) => {
    const el = $(elId);
    if (el) el.onclick = () => act(b.id, action);
  };
  wire('resolve-btn', 'resolve');
  wire('dismiss-btn', 'dismiss');
  wire('reopen-btn', 'reopen');
  wire('jump-btn', 'jump');

  let txOpen = false, txLoaded = false;
  $('tx-toggle').onclick = async () => {
    txOpen = !txOpen;
    $('tx-caret').innerHTML = txOpen ? '&#9662;' : '&#9656;';
    const body = $('tx-body');
    body.innerHTML = txOpen ? '<div class="turn"><div class="turn-who"></div>'
                            + '<p class="turn-note">Loading&hellip;</p></div>' : '';
    if (!txOpen) { $('tx-hint').textContent = 'show turns'; return; }
    if (txLoaded) { body.innerHTML = txLoaded; return; }
    try {
      const r = await fetch(`/api/blocker/${b.id}/context`);
      const { turns, transcript_found: found } = await r.json();
      if (turns.length) {
        txLoaded = turns.map((t) => `
          <div class="turn ${esc(t.role)}">
            <div class="turn-who">
              <span class="turn-role">${t.role === 'assistant' ? 'claude' : 'you'}</span>
            </div>
            <div class="turn-text">${esc(t.text)}</div>
          </div>`).join('');
        $('tx-hint').textContent = `${turns.length} turns`;
      } else {
        const note = found
          ? 'That session has a transcript, but no readable turns in the recent tail.'
          : 'No transcript exists for this session on disk. Claude Code writes one per '
            + 'session, but skips it when the session inherited another session’s '
            + 'identity — which is what happens if it was started from inside another '
            + 'Claude session. Nothing is broken; there is simply nothing to read.';
        txLoaded = `<div class="turn"><div class="turn-who"></div>`
                 + `<p class="turn-note">${esc(note)}</p></div>`;
        $('tx-hint').textContent = 'none';
      }
      body.innerHTML = txLoaded;
    } catch (_) {
      body.innerHTML = '<div class="turn"><div class="turn-who"></div>'
                     + '<p class="turn-note">Could not read the transcript.</p></div>';
    }
  };
}

/* ----------------------------------------------------------- status bar */

function renderStatusBar(data, b) {
  const s = data.stats;
  const bar = $('statusbar');
  if (!b) {
    bar.innerHTML = `
      <span class="sb-item">${s.total} blocker${s.total === 1 ? '' : 's'}</span>
      <span class="grow"></span>
      <span class="sb-item warn">${s.open} open</span>
      <span class="sb-item faint last">Claude Blockers ${esc(data.version || '')}</span>`;
    bar.style.removeProperty('--k');
    return;
  }
  const k = kindOf(b.kind);
  bar.style.setProperty('--k', k.color);
  bar.innerHTML = `
    <span class="sb-kind">${esc(k.short)}</span>
    <span class="sb-item">${esc(b.project)}${b.branch ? ' · ' + esc(b.branch) : ''}</span>
    <span class="sb-item faint">${b.session_id
        ? 'session ' + esc(b.session_id.slice(0, 8))
        : (b.surface === 'claude-desktop' ? 'Claude Desktop' : '')}</span>
    <span class="grow"></span>
    <span class="sb-item warn">${s.open} open</span>
    <span class="sb-item faint">${s.resolved ?? 0} resolved</span>
    <span class="sb-item faint last">Claude Blockers ${esc(data.version || '')}</span>`;
}

/* --------------------------------------------------------------- actions */

async function act(id, action) {
  const res = await fetch(`/api/blocker/${id}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const data = await res.json().catch(() => ({}));

  if (action === 'jump') {
    toast(data.message || 'Opened.', data.ok ? 'ok' : 'error');
    return;
  }
  await refresh(true);
  if (action === 'reopen') return select(id);

  // Move on to whatever is next rather than leaving a closed item selected.
  const next = document.querySelector('.row');
  if (next) next.click();
  else closeDetail();
}

/* ------------------------------------------------------------------ poll */

function renderAll() {
  if (!state.last) return;
  // The picker holds the project filter too, so its label has to say when one is
  // on -- otherwise a filtered board just looks like an empty one.
  const view = state.view === 'recent' ? 'Most recent' : 'By project';
  $('view-label').textContent = state.project ? `${view} · ${state.project}` : view;
  renderScopes();
  renderMenu(state.last);
  renderChrome(state.last);
  renderClear(state.last);
  renderList(state.last);
}

async function refresh(force) {
  const params = new URLSearchParams({ status: state.status });
  if (state.project) params.set('project', state.project);

  let data;
  try {
    const res = await fetch(`/api/state?${params}`);
    data = await res.json();
    $('pulse').classList.remove('stale');
    $('watching').textContent = 'watching';
  } catch (_) {
    $('pulse').classList.add('stale');
    $('watching').textContent = 'offline';
    return;
  }
  state.last = data;
  indexProjects(data.projects);

  // New-blocker detection runs against the open rows the server returned, so a
  // blocker in a project you have filtered out still pings you.
  const openNow = data.blockers.filter((b) => b.status === 'open');
  if (state.knownOpenIds === null) {
    state.knownOpenIds = new Set(openNow.map((b) => b.id));
  } else {
    const fresh = openNow.filter((b) => !state.knownOpenIds.has(b.id));
    if (fresh.length) {
      announce(fresh);
      fresh.forEach((b) => state.knownOpenIds.add(b.id));
    }
    const liveIds = new Set(openNow.map((b) => b.id));
    state.knownOpenIds.forEach((id) => { if (!liveIds.has(id)) state.knownOpenIds.delete(id); });
  }

  renderAll();
  if (!state.selectedId) renderStatusBar(data, null);
}

/* Deep links, in the same spirit as #<id>: a filtered board can be bookmarked. */
const startup = new URLSearchParams(location.search);
if (['recent', 'project'].includes(startup.get('view'))) state.view = startup.get('view');
if (['open', 'resolved', 'dismissed', 'all'].includes(startup.get('status'))) {
  state.status = startup.get('status');
}
if (startup.get('project')) state.project = startup.get('project');

renderNotifyToggle();
refresh(true).then(() => {
  const deepLinked = parseInt(location.hash.slice(1), 10);
  if (Number.isInteger(deepLinked) && deepLinked > 0) select(deepLinked);
});
setInterval(refresh, POLL_MS);
