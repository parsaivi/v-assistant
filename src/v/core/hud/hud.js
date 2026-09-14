/* hud.js — the HUD, driven by two channels.
 *
 * `/api/events` is a Server-Sent Events stream of the things that must keep
 * moving while a turn is in flight: the aperture, the route strip, the tool
 * ticker, the transcript. It comes from `Session`, which is in front of the
 * engine thread, so it survives a slow answer.
 *
 * `/api/state` is a poll of the panels — agenda, tasks, deadlines, mail,
 * memory, nudges. It goes *through* the engine thread and may therefore be
 * late; when it is, the server sends the previous snapshot marked `stale` and
 * the panels dim rather than blank. That is `glance()`'s contract for the
 * phone widget, applied to a screen for the same reason: a picture with a
 * timestamp beats a spinner.
 *
 * One rule runs through every render function here: **vault text is set with
 * `textContent`, never `innerHTML`.** A note title in this vault can contain
 * anything Parsa typed, plus anything an email or a scraped Quera page put
 * there, and a dashboard that renders it as markup is a dashboard that
 * executes it. `el()` is the only DOM builder and it has no HTML path at all.
 */

'use strict';

/* ---- the six states, and what each one looks like ---------------------- */
const C = { cyan: '#6FE3FF', amber: '#FFC061', green: '#48E39B',
            red: '#FF6A5A', violet: '#9C86FF', dim: '#4E7482' };

const STATES = {
  idle:      { word: 'IDLE',      abbr: 'V',   color: C.dim,    code: 'STANDBY', hz: '0.2Hz' },
  listening: { word: 'LISTENING', abbr: '◉',   color: C.cyan,   code: 'CAPTURE', hz: '6Hz'   },
  thinking:  { word: 'THINKING',  abbr: '···', color: C.violet, code: 'REASON',  hz: '12Hz'  },
  speaking:  { word: 'SPEAKING',  abbr: '◈',   color: C.green,  code: 'OUTPUT',  hz: '8Hz'   },
  writing:   { word: 'WRITING',   abbr: '✎',   color: C.amber,  code: 'COMMIT',  hz: '3Hz'   },
  error:     { word: 'FAULT',     abbr: '!',   color: C.red,    code: 'FAULT',   hz: '1Hz'   },
};
const STATE_ORDER = ['idle', 'listening', 'thinking', 'speaking', 'writing', 'error'];

/* The four arcs of the aperture: which tools they stand for, where they sit
 * on the ring, and what colour they light. Angles are the design's. */
const ARCS = {
  vault: { from: 281, tone: C.cyan,   label: 'VLT' },
  cal:   { from: 11,  tone: C.amber,  label: 'CAL' },
  msg:   { from: 101, tone: C.green,  label: 'MSG' },
  mem:   { from: 191, tone: C.violet, label: 'MEM' },
};

const VIEWS = [
  { id: 'console', idx: '01', label: 'CONSOLE', title: 'CONVERSATION' },
  { id: 'tasks',   idx: '02', label: 'TASKS',   title: 'TASK LIST' },
  { id: 'agenda',  idx: '03', label: 'AGENDA',  title: 'DAY' },
  { id: 'week',    idx: '04', label: 'WEEK',    title: 'WEEK' },
  { id: 'memory',  idx: '05', label: 'MEMORY',  title: 'MEMORY STORE' },
  { id: 'vault',   idx: '06', label: 'VAULT',   title: 'VAULT SEARCH' },
];

/* One colour per block kind, and the vocabulary is closed on the server
 * (`weekplan.KINDS`) so this map can be exhaustive rather than defensive.
 *
 * Mostly the HUD's own six tones. `paper` gets the one hue that appears
 * nowhere else on the page, because it is the thing the week is *for* — the
 * hours that get eaten first are the ones with nothing loud defending them,
 * and a colour that reads as "not like the others" is doing real work. */
const KINDS = {
  class:    '#9C86FF', work:  '#6FE3FF', commute: '#4E7482', gym: '#48E39B',
  sleep:    '#2F4A57', study: '#FFC061', paper:   '#FF9BC7',
  personal: '#7BD1C4', event: '#FFA05A',
};

/* The task view's filters, and the honest name for each one.
 *
 * `STALE` is the one that is not a re-cut of the others: open, undated, and
 * older than two months. `backlog_shape` has counted those as `rotting` since
 * the task layer was written and nothing could ever act on them, which is
 * exactly the pile this view exists to empty. */
const TASK_FILTERS = [
  { id: 'open',    label: 'OPEN',    empty: 'NOTHING OPEN.' },
  { id: 'overdue', label: 'OVERDUE', empty: 'NOTHING PAST ITS DATE.' },
  { id: 'stale',   label: 'STALE',   empty: 'NOTHING ROTTING. THE BACKLOG IS LIVE.' },
  { id: 'ignored', label: 'IGNORED', empty: 'NOTHING SILENCED. V IS TRACKING EVERYTHING.' },
  { id: 'done',    label: 'DONE',    empty: 'NOTHING CLOSED IN THE LAST FORTNIGHT.' },
];

/* What each row button does, in the row's own words. The server holds the
 * same table in `hud.TASK_ACTIONS`; these are the labels for it.
 *
 * The three that matter are the three answers a task can get. `done` and
 * `drop` both write to the note — a tick and a strike-through — and are
 * undoable with `v undo`. `ignore` writes to nothing: the checkbox stays
 * exactly as it was typed and V stops counting it. That is the one this view
 * was built for, and it is why the button says what it does not touch. */
const TASK_BUTTONS = {
  done:      { glyph: '✓', label: 'DONE',    why: 'ticks the box in the note · undoable with v undo' },
  open:      { glyph: '↺', label: 'REOPEN',  why: 'unticks it in the note' },
  dropped:   { glyph: '⊘', label: 'DROP',    why: 'strikes it out in the note, so the decision survives · undoable' },
  ignored:   { glyph: '⊗', label: 'IGNORE',  why: 'leaves the note untouched — V stops bringing it up anywhere' },
  unignored: { glyph: '↺', label: 'RESTORE', why: 'back into every list V builds' },
};

/* The quick row is a shortcut to the palette's own entries, so every chip
 * here is a command that really exists. It used to hold `/memory` and
 * `/search`, which are not commands at all — they were nav buttons wearing a
 * slash, and `/undo` next to them sent the literal string to the model. */
const QUICK = ['/why', '/brief', '/status', '/nudges', '/cost', '/new'];

/* Handled in the page rather than at the server: the trace is already on
 * screen, the palette is a keystroke, and the clipboard belongs to the
 * browser. Everything else in the table is an engine call. */
const CLIENT_ONLY = new Set(['why', 'help', 'copy']);

/* Where a picture came from, in the viewport's own words.
 *
 * Provenance is the one thing a frame must always say. A chart V drew from
 * vault numbers, a screenshot Parsa pasted into a bug report two months ago
 * and a map tile fetched from Neshan are three completely different claims,
 * and once they are all PNGs on a black panel they look identical. `label`
 * is what goes under the picture; `abbr` is what fits on a 42px thumbnail. */
const SOURCES = {
  note:   { label: 'IN NOTE',  abbr: 'NOTE', tone: C.cyan },
  render: { label: 'V DREW',   abbr: 'DREW', tone: C.green },
  place:  { label: 'MAP OF',   abbr: 'MAP',  tone: C.amber },
};

/* ---- tiny DOM builder --------------------------------------------------
 * `el('div.card', {title: x}, [child, 'text'])`. Strings become text nodes,
 * which is the whole security argument: there is no path from data to markup
 * because this function cannot produce markup. */
function el(spec, attrs, kids) {
  const [tag, ...classes] = String(spec).split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');
  for (const k in (attrs || {})) {
    const v = attrs[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === 'style') node.setAttribute('style', v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'text') node.textContent = String(v);
    else node.setAttribute(k, String(v));
  }
  for (const kid of (kids || [])) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === 'object' ? kid : document.createTextNode(String(kid)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);
function fill(node, kids) { node.replaceChildren(...kids.filter(Boolean)); }

/* ---- render only what changed ------------------------------------------
 * The panels poll every four seconds and the live stream fires on every tool
 * call. Re-running `fill()` on that cadence rebuilt the whole transcript from
 * scratch: the arrival animation replayed on every turn, the scroll position
 * snapped, an open probe lost the node it was pointing at, and text you were
 * halfway through selecting was replaced under the cursor.
 *
 * So every render is gated on a signature of the data behind it. The cost is
 * a `JSON.stringify` of a few small arrays; what it buys is a page that only
 * moves when something actually happened, which is the whole point of a
 * surface you leave open on a second monitor. */
const memo = {};

function changed(key, sig) {
  if (memo[key] === sig) return false;
  memo[key] = sig;
  return true;
}

function rgba(hex, a) {
  const n = parseInt(hex.replace('#', ''), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
/** `claude-haiku-4-5-20251001` → `haiku-4-5`. The vendor prefix and the
 *  release date are the two parts of a model id that never change the answer
 *  to "was this worth it". `cli._short_model` does the same for the terminal. */
const shortModel = (n) => String(n || '').replace(/^claude-/, '').replace(/-\d{8}$/, '');
const money = (n) => '$' + (Number(n) || 0).toFixed(Number(n) >= 1 ? 2 : 4).replace(/0+$/, '').replace(/\.$/, '.00');
const pct = (x) => Math.max(0, Math.min(100, x * 100)).toFixed(1) + '%';

/* ---- markdown ----------------------------------------------------------
 * `Response.text` is markdown. `ui.py` renders it to ANSI for the terminal —
 * its own docstring notes the REPL that came before printed the asterisks —
 * and this is the same grammar rendered to DOM instead, so the browser does
 * not repeat the bug the terminal already fixed.
 *
 * Same subset `ui._inline` handles, deliberately: `**bold**`, `*italic*`,
 * `_italic_`, `` `code` ``, `[[wikilinks]]`, `#` headings, `-·•` bullets and
 * fenced blocks. Nothing more — V writes this text and that is what V writes.
 *
 * Every branch ends at `document.createTextNode` or `el(..., {text})`. There
 * is no innerHTML path, which is what makes it safe to render text that came
 * out of a note, an email subject or a scraped page. */

const MD_FENCE = /^\s*```(\w*)\s*$/;
const MD_HEAD = /^(#{1,6})\s+(.*)$/;
const MD_BULLET = /^(\s*)([-*+·•]|\d+\.)\s+(.*)$/;
/* Ordered: code first, so `**` inside a code span is left alone. */
const MD_INLINE = [
  [/`([^`\n]+)`/, (t) => el('code', { text: t, style: 'font-family:var(--mono);font-size:.92em;color:#9E9EAE' })],
  [/\*\*([^*\n]+)\*\*/, (t) => el('b', { text: t, style: 'font-weight:600;color:var(--paper)' })],
  [/(?<![\w*])\*([^*\n]+)\*(?![\w*])/, (t) => el('i', { text: t })],
  [/(?<![\w_])_([^_\n]+)_(?![\w_])/, (t) => el('i', { text: t })],
  [/\[\[([^\]\n]+)\]\]/, (t) => el('span', {
    text: t.split('|').pop(), style: 'color:#A793FF' })],
];

function mdInline(text) {
  let best = null;
  for (const [re, make] of MD_INLINE) {
    const m = re.exec(text);
    if (m && (best === null || m.index < best.m.index)) best = { m, make };
  }
  if (!best) return [document.createTextNode(text)];
  const { m, make } = best;
  return [
    ...(m.index ? mdInline(text.slice(0, m.index)) : []),
    make(m[1]),
    ...mdInline(text.slice(m.index + m[0].length)),
  ];
}

/** Markdown to a list of block elements. */
function markdown(text) {
  const out = [];
  let fence = null, buf = [];
  for (const raw of String(text).split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const f = MD_FENCE.exec(line);
    if (f) {
      if (fence === null) { fence = f[1] || ''; buf = []; }
      else {
        out.push(el('pre', { style: 'margin:8px 0;padding:9px 11px;overflow-x:auto;' +
          'background:rgba(4,12,18,.7);border-left:2px solid rgba(111,227,255,.3);' +
          'font-family:var(--mono);font-size:12px;line-height:1.5;color:#9FC3D0' },
          [el('code', { text: buf.join('\n') })]));
        fence = null;
      }
      continue;
    }
    if (fence !== null) { buf.push(raw); continue; }

    if (!line.trim()) { out.push(el('div', { style: 'height:.62em' })); continue; }

    const h = MD_HEAD.exec(line);
    if (h) {
      out.push(el('div', { style: `font-weight:600;color:var(--paper);` +
        `font-size:${[15, 14.5, 14, 13.5, 13.5, 13.5][h[1].length - 1]}px;` +
        'padding:6px 0 2px' }, mdInline(h[2])));
      continue;
    }

    const b = MD_BULLET.exec(line);
    if (b) {
      out.push(el('div', { style: 'display:flex;gap:8px;padding:1px 0' }, [
        el('span', { text: /\d/.test(b[2]) ? b[2] : '·',
          style: 'flex:none;color:var(--cyan);min-width:1ch' }),
        el('span', { style: 'min-width:0' }, mdInline(b[3])),
      ]));
      continue;
    }
    out.push(el('div', {}, mdInline(line)));
  }
  /* An unterminated fence is a truncated answer, not a reason to lose it. */
  if (fence !== null && buf.length) {
    out.push(el('pre', { style: 'margin:8px 0;font-family:var(--mono);font-size:12px' },
      [el('code', { text: buf.join('\n') })]));
  }
  return out;
}

/* ---- app state --------------------------------------------------------- */
const app = {
  view: 'console',
  snap: null,           // the last /api/state
  live: null,           // the last SSE frame
  hits: null,           // the last vault search
  tasks: null,          // the last /api/tasks
  taskView: 'open',     // which filter the task view is showing
  taskBusy: '',         // the key of the row with an action in flight
  taskError: '',        // the last failed action, shown until dismissed
  nudgeBusy: '',        // the key of the nudge with a silence/restore in flight
  nudgeError: '',       // the last failed silence/restore
  weekZoom: 1,          // index into WEEK_ZOOM — how many pixels a minute gets
  weekKinds: null,      // the block vocabulary, as the server last sent it
  blockEdit: null,      // the block being added or edited, or null
  blockErr: '',         // what the server said no to, shown in the form
  blockBusy: false,     // a write is in flight — don't send it twice
  query: '',
  frame: '',           // which frame the viewport is showing, by id
  seenFrames: null,    // ids already on screen — what makes an arrival an arrival
  lightbox: '',        // which frame is blown up, or '' for none
  trace: false,
  busy: false,
  connected: false,
};

/* ---- the probe ---------------------------------------------------------
 * The design's "interrogate" callout: hover anything and a line is drawn from
 * it to a card of where the number came from. Positioned in the viewport, and
 * flipped to the other side when there is no room on the left. */
const probe = $('probe');
function showProbe(target, kind, title, rows) {
  const r = target.getBoundingClientRect();
  const gap = 46, w = 250;
  const y = Math.max(70, Math.min(window.innerHeight - 130, r.top + r.height / 2));
  let boxLeft = r.left - gap - w, wireLeft = r.left - gap, wireW = gap - 8, dotLeft = r.left - 7;
  if (boxLeft < 12) {
    boxLeft = Math.min(window.innerWidth - w - 12, r.right + gap);
    wireLeft = r.right + 8; dotLeft = r.right + 1;
  }
  probe.querySelector('.dot').style.cssText = `left:${dotLeft}px;top:${y - 3}px`;
  probe.querySelector('.wire').style.cssText = `left:${wireLeft}px;top:${y}px;width:${wireW}px`;
  probe.querySelector('.box').style.cssText = `left:${boxLeft}px;top:${y - 42}px`;
  probe.querySelector('.kind').textContent = kind;
  probe.querySelector('.title').textContent = title;
  fill(probe.querySelector('.rows'), rows.filter(Boolean).map(([k, v]) =>
    el('div.row', {}, [el('span.k', { text: k }), el('span.v', { text: v })])));
  probe.hidden = false;
}
function hideProbe() { probe.hidden = true; }

/** Attach a probe to a node. Rows are `[label, value]` pairs. */
function probed(node, kind, title, rows) {
  node.addEventListener('mouseenter', () => showProbe(node, kind, title, rows));
  node.addEventListener('mouseleave', hideProbe);
  return node;
}

/* ---- static chrome ----------------------------------------------------- */
function buildNav() {
  fill($('nav'), VIEWS.map((v) => {
    const b = el('button', { type: 'button', onclick: () => go(v.id) }, [
      el('span.idx', { text: v.idx }),
      el('span.label', { text: v.label }),
      el('span.bar'),
    ]);
    b.dataset.view = v.id;
    return b;
  }));
}

function buildStates() {
  fill($('states'), STATE_ORDER.map((k) => {
    const s = STATES[k];
    const row = el('div.state-row', {}, [
      el('span.dot', { style: `background:${s.color}` }),
      el('span.label', { text: s.word }),
      el('span.hz', { text: s.hz }),
    ]);
    row.dataset.state = k;
    /* Not a control. There is no microphone on this surface, so every state
     * is derived from something that really happened — see hud.py. */
    return probed(row, 'AMBIENT STATE', s.word, [
      ['DERIVED FROM', {
        idle: 'nothing running',
        listening: 'keystrokes in the prompt',
        thinking: 'a turn in flight',
        speaking: 'the seconds after an answer',
        writing: 'a turn that committed to the vault',
        error: 'a turn that raised',
      }[k]],
      ['SAMPLE', s.hz],
      ['CONTROL', 'read-only — no mic on this surface'],
    ]);
  }));
}

function buildWave() {
  /* 30 bars. With no microphone they cannot be RMS, so they are not pretending
   * to be: at rest they drift, and they only quicken while something is
   * actually running. The brief's rule that a waveform must be driven by real
   * audio is met by not claiming this one is audio. */
  fill($('wave'), Array.from({ length: 30 }, (_, i) => {
    const b = el('i');
    b.dataset.i = i;
    b.style.animationDelay = (i * 0.045).toFixed(2) + 's';
    return b;
  }));
}

function buildArcs() {
  for (const [key, a] of Object.entries(ARCS)) {
    const node = document.querySelector(`.arc[data-arc="${key}"]`);
    if (node) node.style.background =
      `conic-gradient(from ${a.from}deg, ${rgba(C.cyan, .35)} 0 68deg, transparent 68deg)`;
  }
}

function buildQuick() {
  fill($('quick'), QUICK.map((cmd) =>
    el('button', { type: 'button', text: cmd, onclick: () => run(cmd) })));
}

/* ---- the slash palette ------------------------------------------------- */
let COMMANDS = [];          // filled once at boot from /api/commands
let picked = 0;             // which row Enter would run

function paletteRows() {
  const v = $('input').value.trim();
  if (!v.startsWith('/')) return null;
  const q = v.split(' ')[0].toLowerCase();
  return COMMANDS.filter((c) => c.cmd.startsWith(q));
}

function drawPalette() {
  const node = $('palette');
  const rows = paletteRows();
  if (rows === null) { node.hidden = true; picked = 0; return; }
  node.hidden = false;
  if (picked >= rows.length) picked = 0;
  if (!rows.length) {
    fill(node, [el('div.none', {
      text: 'no command matches — press enter to send as plain language' })]);
    return;
  }
  fill(node, rows.map((c, i) => {
    const row = el('div.row', { role: 'option', onclick: () => run(c.cmd) }, [
      el('span.cmd', { text: c.cmd }),
      el('span.what', { text: c.what }),
      el('span.said', { text: c.said ? `“${c.said}”` : '' }),
    ]);
    row.setAttribute('aria-selected', String(i === picked));
    return row;
  }));
}

function hidePalette() { $('palette').hidden = true; picked = 0; }

/* ---- ambient state ----------------------------------------------------- */
function paintState(name, detail, tools) {
  const s = STATES[name] || STATES.idle;
  const hud = $('hud');
  hud.style.setProperty('--st', s.color);
  hud.style.setProperty('--st-line', rgba(s.color, .34));
  hud.style.setProperty('--st-line2', rgba(s.color, .3));
  hud.style.setProperty('--st-wash', rgba(s.color, .08));
  hud.style.setProperty('--st-field', rgba(s.color, .16));
  hud.style.setProperty('--st-halo', rgba(s.color, .5));
  hud.style.setProperty('--st-soft', rgba(s.color, .55));
  hud.style.setProperty('--st-glow', rgba(s.color, .7));
  hud.style.setProperty('--st-glow2', rgba(s.color, .35));
  $('stword').textContent = s.word;
  $('stdetail').textContent = detail || '';
  $('stabbr').textContent = s.abbr;
  $('stcode').textContent = s.code;

  for (const row of $('states').children) {
    row.dataset.on = row.dataset.state === name ? '1' : '0';
  }

  const active = name === 'thinking' || name === 'listening' || name === 'speaking';
  for (const bar of $('wave').children) {
    const i = Number(bar.dataset.i);
    bar.style.background = active
      ? rgba(s.color, 0.28 + 0.5 * Math.abs(Math.sin(i * 1.1)))
      : rgba(C.dim, 0.28);
    bar.style.animationDuration = active
      ? (0.5 + (i % 5) * 0.13).toFixed(2) + 's'
      : (2.2 + (i % 4) * 0.5).toFixed(2) + 's';
  }

  const lit = new Set(tools || []);
  for (const [key, a] of Object.entries(ARCS)) {
    const node = document.querySelector(`.arc[data-arc="${key}"]`);
    const label = document.querySelector(`.arclabel[data-label="${key}"]`);
    const on = lit.has(key);
    if (node) {
      node.dataset.on = on ? '1' : '0';
      node.style.background = `conic-gradient(from ${a.from}deg, ` +
        `${on ? a.tone : rgba(C.cyan, .35)} 0 68deg, transparent 68deg)`;
    }
    if (label) label.style.color = on ? a.tone : '#2E4A57';
  }
}

/* ---- header, gauges, footer -------------------------------------------- */
/* The day currently under the clock, so the probe wired up once in `boot()`
 * can read the live one. `paintHeader` runs every poll and `#jdate` is a
 * static node — attaching the listener there instead would add a fresh pair
 * every four seconds for as long as the HUD is open. */
let jday = null;

function jalaliRows(j) {
  const holiday = j.holiday === null
    ? (j.friday ? 'Friday — off, and that much is known offline'
                : 'unknown — the calendar service was not reached')
    : (j.holiday ? 'تعطیل — a day off' : 'a working day');
  return [
    ['JALALI', j.iso],
    ['GREGORIAN', j.gregorian],
    ['WEEKDAY', j.weekday],
    ['STATUS', holiday],
    ['MONTH KEY', j.month_key],
    ...(j.events || []).slice(0, 3).map((e) => ['MARKED', e]),
  ];
}

function paintJalali(snap) {
  const j = snap.jalali;
  if (!j) return;
  jday = j;
  if (!changed('jdate', JSON.stringify(j))) return;
  const box = $('jdate');
  box.querySelector('.d').textContent = j.short;
  /* Three states, not two — `hud._jalali` leaves `holiday` null when the
   * service was unreachable, and "I don't know" must not be drawn as "no". A
   * Friday is the one case where null is still an answer. */
  box.dataset.off = (j.holiday === null && !j.friday) ? '?' : (j.off ? '1' : '0');
  box.title = j.label;
}

function paintHeader(snap, live) {
  const s = live.session;
  paintJalali(snap);
  $('c-model').textContent = snap.header.model || 'local';
  $('c-lat').textContent = s.latency_ms ? s.latency_ms + 'ms' : '—';
  $('c-turns').textContent = `${s.local}/${s.turns}`;
  $('c-spend').textContent = money(s.spent);
  $('c-spend').style.color = s.spent > 0 ? C.amber : C.green;
  $('c-mem').textContent = String(snap.header.memories);

  const alarm = snap.alarm || {};
  $('alarm').hidden = !alarm.on;
  $('alarmchip').hidden = !alarm.on;
  $('alarmtxt').textContent = alarm.label || '';
  $('card-dl').dataset.alarm = alarm.on ? '1' : '0';
}

function gauge(k, value, frac, tone, kind, title, rows) {
  const g = el('div.gauge', {}, [
    el('div.top', {}, [el('span.k', { text: k }), el('span.v', { text: value, style: `color:${tone}` })]),
    el('div.track', {}, [el('i.fill', { style: `width:${pct(frac)};background:${tone};box-shadow:0 0 8px ${tone}` })]),
  ]);
  return probed(g, kind, title, rows);
}

function paintGauges(snap, live) {
  const g = snap.gauges, s = live.session;
  if (!changed('gauges', JSON.stringify([g, s.spent, s.turns, s.local,
                                         s.latency_ms, snap.header.model]))) return;
  const cap = g.budget.cap || 2;
  /* The day's, off the run log every surface writes — not this tab's running
   * total. The bar is drawn against a *daily* cap, so it has to be filled by
   * a daily number: the old one zeroed on /new and on every restart, and
   * never saw a penny spent over Telegram. `spent` is still here, as the
   * second row, because "what has this conversation cost" is a real question
   * — just not the one a cap answers. */
  const used = g.budget.used;
  const drift = g.drift.files;
  fill($('gauges'), [
    gauge('BUDGET', `${money(used)} / ${money(cap)}`, used / cap,
      used / cap > 0.8 ? C.amber : C.green, 'GAUGE', `Spent today ${money(used)}`, [
        ['TODAY', `${money(used)} of ${money(cap)} cap · ${g.budget.turns} turns`],
        ['SESSION', `${money(s.spent)} · ${s.turns} turns (${s.local} free)`],
        ['MODEL', snap.header.model || 'local'],
        ['SCOPE', 'today is every surface; session resets on /new'],
      ]),
    gauge('LATENCY', s.latency_ms ? s.latency_ms + 'ms' : '—',
      Math.min(1, (s.latency_ms || 0) / 4000), C.cyan, 'GAUGE',
      s.latency_ms ? `Last turn ${s.latency_ms}ms` : 'No turn yet', [
        ['SCALE', 'full bar at 4s'],
        ['ROUTE', (live.route || []).map((h) => h.arc || '·').join(' → ') || 'none'],
      ]),
    gauge('LOCAL', `${s.local} / ${s.turns || 0}`,
      s.turns ? s.local / s.turns : 1, C.green, 'GAUGE',
      'Turns that never left the machine', [
        ['FREE PATH', 'SQL · FTS · RAG · capture'],
        ['METERED', 'escalation to a shell, last'],
      ]),
    gauge('INDEX DRIFT', drift ? `${drift} file${drift === 1 ? '' : 's'}` : 'fresh',
      Math.min(1, drift / 20), drift ? C.amber : C.green, 'GAUGE',
      drift ? `${drift} notes a rebuild would touch` : 'Index matches the vault', [
        ['SAMPLE', (g.drift.sample || []).join(', ') || '—'],
        ['NOTES', String(g.drift.notes)],
        ['FIX', 'v index'],
      ]),
  ]);
}

function paintFooter(snap, live) {
  const v = snap.vault, s = live.session;
  $('f-vault').textContent = `${v.notes.toLocaleString()} notes · ${v.types} types`;
  $('f-index').textContent = v.fresh ? 'fresh' : `${snap.gauges.drift.files} drifted`;
  $('f-index').style.color = v.fresh ? C.green : C.amber;
  $('f-net').textContent = v.network;
  const mins = Math.floor(s.up / 60);
  $('f-session').textContent = `${s.id} · ${mins}m`;
  /* The tile has said SPEND TODAY since it was built. It now says it truly. */
  $('t-spend').textContent = money(snap.gauges.budget.used);
  $('t-cap').textContent = 'of ' + money(snap.gauges.budget.cap) + ' cap · '
    + money(s.spent) + ' this session';
  $('t-mem').textContent = snap.header.memories.toLocaleString();
  $('t-memsub').textContent = 'active memories';
}

/* ---- route + trace ------------------------------------------------------ */
function paintRoute(live) {
  const hops = live.route || [];
  fill($('hops'), hops.length ? hops.map((h, i) => {
    const tone = h.arc && ARCS[h.arc] ? ARCS[h.arc].tone : C.cyan;
    const box = probed(el('div.box', {
      style: `border:1px solid ${rgba(tone, .3)};background:${rgba(tone, .07)}`,
    }, [
      el('span.led', { style: `background:${tone};box-shadow:0 0 7px ${tone}` }),
      el('span.name', { text: h.arc ? h.arc.toUpperCase()
        : (h.call.split('(')[0].slice(0, 14) || 'TOOL') }),
      el('span.ms', { text: h.ms ? h.ms + 'ms' : '…' }),
    ]), 'ROUTE HOP', h.call, [
      ['STATUS', h.status], ['AT', h.at],
      ['LATENCY', h.ms ? h.ms + 'ms' : 'in flight'],
      ['ARC', h.arc || 'unmapped'],
    ]);
    return el('div.hop', {}, [
      box,
      i < hops.length - 1 ? el('span.wire', {}, [
        el('i', { style: `background:linear-gradient(90deg,transparent,${tone})` }),
      ]) : null,
    ]);
  }) : [el('span', { style: 'font-size:9px;letter-spacing:.14em;color:#3F6472', text: 'IDLE · NO CALLS' })]);

  if (app.trace) paintTrace(live);
}

function paintTrace(live) {
  const rows = (live.ticker || []).slice(0, 8);
  fill($('trace'), rows.length ? rows.map((h) => {
    const tone = h.arc && ARCS[h.arc] ? ARCS[h.arc].tone : C.cyan;
    return el('div.row', { style: `--t:${tone}` }, [
      el('span.n', { text: h.at }),
      el('span.c', { text: h.call }),
      el('span.m', { text: h.ms ? h.ms + 'ms' : '…' }),
      el('span.s', { text: h.status }),
    ]);
  }) : [el('div.empty', {
    text: 'No tool calls yet. Ask something that has to read the vault.',
  })]);
}

function toggleTrace() {
  app.trace = !app.trace;
  $('why').setAttribute('aria-pressed', String(app.trace));
  $('trace').hidden = !app.trace;
  if (app.trace && app.live) paintTrace(app.live);
}

/* ---- tool bus ----------------------------------------------------------- */
function paintBus(live) {
  const items = live.ticker || [];
  const one = (h) => {
    const tone = h.arc && ARCS[h.arc] ? ARCS[h.arc].tone : C.cyan;
    return el('span.item', {}, [
      el('span.at', { text: h.at }),
      el('span', { text: h.call, style: `color:${tone}` }),
      el('span.ms', { text: h.ms ? h.ms + 'ms' : '…' }),
      el('span', { text: h.status, style: `color:${tone}` }),
    ]);
  };
  /* Doubled so the marquee wraps seamlessly at -50%. */
  fill($('tape'), items.length ? [...items.map(one), ...items.map(one)]
    : [el('span.item', {}, [el('span.at', { text: 'idle · no calls this session' })])]);
  $('tape').style.animationPlayState = items.length ? 'running' : 'paused';

  const spark = items.slice(0, 26).reverse();
  fill($('spark'), spark.length ? spark.map((h) => {
    const ms = h.ms || 1;
    const height = Math.max(3, Math.min(14, Math.round(Math.log10(ms + 1) * 6)));
    return el('i', { style: `height:${height}px;background:${ms > 400 ? rgba(C.amber, .9) : rgba(C.cyan, .55)}` });
  }) : [el('i', { style: `height:3px;background:${rgba(C.cyan, .3)}` })]);

  const s = live.session;
  $('bus-tail').textContent = (s.latency_ms ? `LAT ${s.latency_ms}ms · ` : '') + money(s.spent);
  $('bus-led').style.background = app.connected ? C.green : C.red;
  $('bus-led').style.boxShadow = `0 0 8px ${app.connected ? C.green : C.red}`;
}

/* ---- the pane ----------------------------------------------------------- */
function paintPaneHead(snap) {
  const v = VIEWS.find((x) => x.id === app.view);
  const s = snap && snap.vault;
  /* `snap.day` is already V's day. Parsed as a bare date it would be read as
   * UTC midnight and rendered back in the browser's zone, which is how a
   * heading one day off gets made out of a correct string. */
  $('pane-t').textContent = app.view === 'agenda' && snap
    ? `DAY / ${new Date(snap.day + 'T12:00:00').toDateString().slice(4, 10).toUpperCase()}`
    : v.title;
  const meta = {
    console: () => `session ${app.live ? app.live.session.id : '—'} · ${app.live ? app.live.session.turns : 0} turns`,
    /* The counts, said in full, because the whole complaint this view answers
     * is that the open number was meaningless. `carried` and `ignored` are
     * the two that explain it: one is the same task written five times, the
     * other is what has been deliberately silenced. */
    tasks: () => {
      const c = taskCounts();
      if (!c) return 'checkbox commitments, collapsed across notes';
      return `${c.open} open · ${c.overdue} past date · ${c.stale} rotting · ${c.ignored} silenced`;
    },
    agenda: () => 'vault calendar + reminders',
    /* What the week costs, said as hours rather than as blocks: "31 blocks"
     * is a number about the note, and "40h spoken for" is a number about his
     * life. The second one is the question the view answers. */
    /* Hours committed and what is on right now. Not how many hours are
     * free — that was a tally under every day header, and a calendar that
     * counts the time you have not committed is nagging rather than
     * reporting. `v week --free` answers it when it is actually asked. */
    week: () => {
      const w = snap && snap.week;
      if (!w) return 'panel missing — restart v hud';
      if (!w.blocks) return `${w.rel} · nothing scheduled`;
      return `${w.blocks} blocks · ${hrs(w.claimed)} committed · `
        + (w.now ? `now: ${w.now.title} until ${w.now.until}` : 'nothing on right now');
    },
    memory: () => snap ? `${snap.header.memories} active · decay on read` : '',
    vault: () => app.hits ? `${app.hits.backend}${app.hits.ms ? ' · ' + app.hits.ms + 'ms' : ''}` : 'semantic when embeddings are built',
  }[app.view];
  $('pane-m').textContent = meta ? meta() : '';
  const right = {
    console: () => app.live && app.live.session.spent ? money(app.live.session.spent) + ' THIS SESSION' : 'FREE SO FAR',
    tasks: () => app.tasks ? `${app.tasks.total} SHOWN · WRITES TO THE NOTE` : '',
    agenda: () => snap ? `${snap.day_events.length} ITEMS` : '',
    week: () => {
      const w = snap && snap.week;
      if (!w) return '';
      if (w.next) {
        const when = w.next.today ? 'TODAY' : w.next.day;
        return `NEXT · ${when} ${w.next.at} · ${w.next.title.toUpperCase()}`;
      }
      return 'NOTHING LEFT THIS WEEK';
    },
    memory: () => 'SORTED BY SALIENCE',
    vault: () => s ? `${s.notes.toLocaleString()} NOTES INDEXED` : '',
  }[app.view];
  $('pane-r').textContent = right ? right() : '';
}

/** Render the pane, but only if its own data moved. `key` is the signature.
 *
 * The signature is recorded *after* the draw, not before, and that ordering
 * is load-bearing: `changed()` used to mark the frame as drawn and then hand
 * over to a function that could throw. One row referring to a button that did
 * not exist was enough — the DOM kept the previous view's rows, the memo
 * claimed the new ones were on screen, and every later frame returned early.
 * The filter looked broken rather than the row, and nothing in the pane ever
 * recovered. Advancing the memo last means a failed draw is simply retried on
 * the next frame. */
function renderPane(key, draw) {
  const node = $('pane');
  node.dataset.stale = app.snap && app.snap.stale ? '1' : '0';
  /* Which view owns the pane, so a view that scrolls itself can say so in
   * CSS. Set here rather than in the view: setting it in `paintWeekInto`
   * left `week` on the pane after switching to TASKS, which inherited the
   * week's "don't scroll, no padding" and broke the task list. */
  node.dataset.pane = app.view;
  const sig = app.view + '|' + key;
  if (memo.pane === sig) return;
  hideProbe();
  const top = node.scrollTop;
  draw(node);
  memo.pane = sig;
  node.scrollTop = top;
}

function paintPane() {
  if (app.view === 'console') return paintConsole();
  if (app.view === 'tasks') return paintTasks();
  if (app.view === 'agenda') return paintAgenda();
  if (app.view === 'week') return paintWeek();
  if (app.view === 'memory') return paintMemory();
  if (app.view === 'vault') return paintVault();
}

let lastTurns = 0;

function paintConsole() {
  const turns = (app.live && app.live.turns) || [];
  const node = $('pane');
  node.dataset.stale = app.snap && app.snap.stale ? '1' : '0';
  node.dataset.pane = app.view;
  /* A landed turn is immutable, so its count and the length of the last one
   * are enough to say whether the log moved. */
  const sig = turns.length + ':' + (turns.length ? turns[turns.length - 1].text.length : 0);
  if (!changed('pane', 'console|' + sig)) return;
  /* Follow the log only when you are already at the end of it. Reading back
   * through an answer while V is still working should not be interrupted by
   * the next frame — the same rule a terminal follows. */
  const atEnd = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
  const grew = turns.length > lastTurns;
  const top = node.scrollTop;
  lastTurns = turns.length;
  hideProbe();
  paintTurns(node, turns);
  node.scrollTop = (grew || atEnd) ? node.scrollHeight : top;
}

function paintTurns(pane, turns) {
  if (!turns.length) {
    /* The brief's `empty` state: no greeting, no tour, no sample questions.
     * V does not introduce himself to the person who wrote him. */
    fill(pane, [el('div.empty', { text: 'READY.' })]);
    return;
  }
  fill(pane, [el('div.turns', {}, turns.map((t) => {
    const isV = t.role === 'v';
    const who = isV ? ({ refused: C.red, unsure: C.amber, degraded: C.dim }[t.state] || C.cyan) : '#5E8C9C';
    const bits = [];
    if (isV) {
      bits.push(el('div.cost', {}, [
        '◈ ', el('b', { text: shortModel(t.model) || t.route || 'local' }),
        t.cites.length ? ' · ⟡ ' : '', t.cites.length ? el('b', { text: t.cites.length + ' notes' }) : '',
        ' · ', el('b', { text: (t.ms / 1000).toFixed(2) + 's' }),
        ' · ', el('b', { text: t.cost ? money(t.cost) : '$0.0000' }),
        t.confidence !== null && t.confidence !== undefined ? ' · ◉ ' : '',
        t.confidence !== null && t.confidence !== undefined
          ? el('b', { text: Number(t.confidence).toFixed(2),
                      style: `color:${t.confidence >= 0.8 ? C.green : C.amber}` }) : '',
      ]));
    }
    return el('div.turn', {
      style: `--w:${who};--bl:${rgba(isV ? C.cyan : C.cyan, isV ? .18 : .08)};` +
             `--bw:${isV ? 'rgba(10,26,36,.6)' : 'rgba(8,14,18,.4)'};` +
             `--bf:${isV ? '#D8F2FB' : '#9FC3D0'}`,
    }, [
      el('div.meta', {}, [
        el('span.who', { text: isV ? 'V' : 'OPERATOR' }),
        el('span.time', { text: t.time }),
        el('span.fill'),
        el('span.route', { text: t.route || '—' }),
      ]),
      el('div.bubble', {}, isV ? markdown(t.text) : [t.text]),
      ...bits,
      t.cites.length ? el('div.cites', {}, t.cites.map((c) =>
        probed(el('span.cite', { text: c.replace(/^\[\[|\]\]$/g, '').split('/').pop() }),
          'CITATION', c, [['SOURCE', 'vault note'], ['USED FOR', 'this answer'],
                          ['OPEN', 'obsidian://open?file=' + encodeURIComponent(c)]]))) : null,
    ]);
  }))]);
}

/* ---- the task view ------------------------------------------------------
 * The right rail's TASKS card answers "what should I do next" in six rows.
 * This answers the other question, which had no surface at all: "what is V
 * still counting that I am never going to do?"
 *
 * Which is why the row is a row of *verbs* rather than a checkbox. A vault
 * that has been written in for a year accumulates checkboxes that were never
 * decisions, and until now the only thing V could be told about one of them
 * was that it was finished — so they stayed open, stayed ranked, and stayed
 * in every count of how far behind he was. `IGNORE` is the missing third
 * answer, and it deliberately writes nothing to the note: the line stays
 * exactly as it was typed in July, and V stops bringing it up. */

function taskCounts() {
  if (app.tasks && app.tasks.counts) return app.tasks.counts;
  return (app.snap && app.snap.task_counts) || null;
}

/* Only the newest request is allowed to be the answer.
 *
 * The four-second poll refetches this list while it is on screen, so pressing
 * a filter chip reliably puts two requests in flight at once — and the older
 * one can land last. It did: clicking DONE drew 62 in the header and left the
 * IGNORED list's empty state in the body, which reads as a broken filter
 * rather than as a race. A sequence number is the whole fix; an AbortController
 * would cancel the poll instead, which is the same outcome with a spurious
 * error to swallow on every click. */
let taskSeq = 0;

async function loadTasks(view) {
  if (view) app.taskView = view;
  const seq = ++taskSeq;
  const want = app.taskView;
  let next;
  try {
    const r = await fetch('/api/tasks?view=' + encodeURIComponent(want));
    if (!r.ok) throw new Error('tasks ' + r.status);
    next = await r.json();
  } catch (err) {
    next = { view: want, rows: [], total: 0, counts: null,
             error: String(err.message || err) };
  }
  if (seq !== taskSeq) return;
  app.tasks = next;
  if (app.view === 'tasks') { memo.pane = null; paintPane(); paintPaneHead(app.snap); }
}

function paintTasks() {
  renderPane([app.taskView, app.taskBusy, app.taskError,
              JSON.stringify(app.tasks)].join('|'),
             (node) => paintTasksInto(node));
}

function paintTasksInto(pane) {
  const data = app.tasks;
  const counts = taskCounts() || {};
  const filters = el('div.tfilter', {}, TASK_FILTERS.map((f) => {
    const n = counts[f.id];
    const b = el('button', { type: 'button', onclick: () => loadTasks(f.id) }, [
      el('span.l', { text: f.label }),
      n === undefined || n === null ? null : el('span.n', { text: String(n) }),
    ]);
    b.setAttribute('aria-pressed', String(app.taskView === f.id));
    return b;
  }));

  const err = app.taskError
    ? el('div.terr', { text: app.taskError, onclick: () => { app.taskError = ''; memo.pane = null; paintPane(); } })
    : null;

  if (!data) { fill(pane, [filters, err, el('div.empty', { text: 'READING THE VAULT…' })]); return; }
  if (data.error) { fill(pane, [filters, err, el('div.empty', { text: 'TASK LIST UNREACHABLE · ' + data.error })]); return; }

  const spec = TASK_FILTERS.find((f) => f.id === data.view) || TASK_FILTERS[0];
  if (!data.rows.length) { fill(pane, [filters, err, el('div.empty', { text: spec.empty })]); return; }

  fill(pane, [filters, err,
              el('div.tlist', {}, data.rows.map((t) => taskRow(t, data.view)))]);
}

/** One task, with everything that can be done to it. */
function taskRow(t, view) {
  const tone = t.ignored ? C.dim
    : (t.rank === 'P0' ? C.red : (t.rank === 'P1' ? C.amber : (t.rotting ? C.violet : '#3F6472')));
  /* The meta line is the row's argument for being on screen. `12d old` and
   * `carried 5×` are the two facts that decide whether a task is live work or
   * something to get rid of, and neither of them is in the text of the
   * checkbox — which is why a vault full of them is hard to prune by hand. */
  const meta = [t.where, t.why];
  if (view === 'ignored') meta.push(t.carried ? `hiding ${t.carried} open` : 'no live copy left');
  else {
    if (t.age) meta.push(t.age + 'd old');
    if (t.carried > 1) meta.push('carried ' + t.carried + '×');
    if (t.rotting) meta.push('rotting');
  }

  const verbs = view === 'ignored' ? ['unignored']
    : (t.done ? ['open', 'ignored'] : ['done', 'dropped', 'ignored']);

  const row = el('div.trow', { style: `--t:${tone}` }, [
    el('button.tbox', {
      type: 'button',
      'aria-label': t.done ? 'reopen' : 'tick',
      disabled: view === 'ignored' || app.taskBusy === t.key,
      onclick: () => taskAction(t, t.done ? 'open' : 'done'),
    }, [el('span', { text: t.done ? '✓' : '' })]),
    el('div.tmid', {}, [
      el('div.n', { text: t.title }),
      el('div.m', { text: meta.filter(Boolean).join(' · ') }),
    ]),
    el('span.trank', { text: t.rank }),
    el('div.tacts', {}, verbs.map((v) => {
      const b = TASK_BUTTONS[v];
      const btn = el('button', {
        type: 'button', title: b.label + ' — ' + b.why,
        disabled: app.taskBusy === t.key,
        onclick: (ev) => { ev.stopPropagation(); taskAction(t, v); },
      }, [el('span.g', { text: b.glyph }), el('span.l', { text: b.label })]);
      btn.dataset.verb = v;
      return btn;
    })),
  ]);
  row.dataset.done = t.done ? '1' : '0';
  row.dataset.ignored = t.ignored ? '1' : '0';

  return probed(row, t.ignored ? 'IGNORED TASK' : 'TASK · ' + t.rank, t.title, [
    ['WHY RANKED', t.why],
    ['WHERE', t.where + (t.area ? ' · ' + t.area : '')],
    t.rel ? ['NOTE', t.rel] : null,
    t.carried > 1 && view !== 'ignored' ? ['CARRIED', t.carried + ' notes hold this same line'] : null,
    ...verbs.map((v) => [TASK_BUTTONS[v].label, TASK_BUTTONS[v].why]),
  ]);
}

/** Run one verb against one task, then reload the list and the panels. */
async function taskAction(t, verb) {
  if (app.taskBusy) return;
  app.taskBusy = t.key;
  memo.pane = null;
  paintPane();
  try {
    /* The key travels with every call, not just the ignore ones: it is how an
     * un-ignore finds an entry whose checkbox has since moved or whose note
     * was deleted, and the server ignores it when it has an id to write to. */
    await post('/api/task', { id: t.id || 0, key: t.key || '', state: verb });
    app.taskError = '';
  } catch (err) {
    app.taskError = String(err.message || err);
  } finally {
    app.taskBusy = '';
  }
  await loadTasks();
  refresh(true);
}

function paintAgenda() {
  const items = (app.snap && app.snap.day_events) || [];
  renderPane(JSON.stringify(items), (node) => paintAgendaInto(node, items));
}

function paintAgendaInto(pane, items) {
  if (!items.length) { fill(pane, [el('div.empty', { text: 'NOTHING ON THE CALENDAR TODAY.' })]); return; }
  fill(pane, [el('div.day', {}, items.map((d) => {
    const tone = d.kind === 'v' ? C.violet : (d.past ? C.dim : C.cyan);
    const row = el('div.row', { style: `--t:${tone}` }, [
      el('span.time', { text: d.time || '—' }),
      el('span.dot'),
      el('span.title', { text: d.title }),
      el('span.src', { text: d.src }),
    ]);
    row.dataset.past = d.past ? '1' : '0';
    return probed(row, 'EVENT', d.title, [
      ['SOURCE', d.src === 'REM' ? 'v reminders' : 'vault calendar'],
      ['WHEN', d.at || d.time || 'all day'],
      d.where ? ['WHERE', d.where] : null,
      ['STATUS', d.past ? 'past' : 'ahead'],
    ]);
  }))]);
}

/* ---- the week ----------------------------------------------------------
 *
 * A grid, not a list, and that is the whole point of putting it here: `v week`
 * already prints the rows, and the one thing a terminal cannot show is the
 * *shape* — where the solid walls of the week are, and what is beside what.
 *
 * Three things this gets right that the first cut did not:
 *
 * **The canvas is the whole day.** 00:00 to 24:00, always, scrolled rather
 * than clipped. Sizing the grid to whatever hours happened to have blocks
 * meant the rows moved every time anything changed, and 03:00 was not
 * somewhere you could scroll to — it did not exist.
 *
 * **The header is inside the same grid.** It is row one, `position: sticky`,
 * sharing the column template with the columns underneath it. Two grids side
 * by side cannot be kept aligned — one of them eventually gets a scrollbar.
 *
 * **Overlaps split the width.** Two events at the same hour were drawn at the
 * same coordinates, one on top of the other. The server assigns lanes; this
 * lays them out.
 */
const WEEK_ZOOM = [0.55, 0.8, 1.15, 1.7];   /* pixels per minute */
const SNAP = 15;                             /* minutes, for click-to-add */

function paintWeek() {
  const w = app.snap && app.snap.week;
  /* Zoom is view state, not snapshot state, so it goes in the signature —
   * otherwise zooming does nothing until the next poll happens to change
   * something else.
   *
   * The editor is deliberately NOT in here. It used to be, and that was the
   * bug that made it feel broken: every keystroke mutated `app.blockEdit`,
   * which changed the signature, which rebuilt the whole pane on the next
   * four-second poll — destroying the input you were typing into, along with
   * its focus and cursor. It lives outside the pane now and nothing on this
   * path can touch it. */
  renderPane(JSON.stringify(w) + '|' + app.weekZoom,
    (node) => paintWeekInto(node, w));
}

function hrs(mins) {
  if (!mins) return '0h';
  const h = Math.floor(mins / 60), m = mins % 60;
  return h && m ? `${h}h${String(m).padStart(2, '0')}` : (h ? `${h}h` : `${m}m`);
}

function dayName(iso) {
  return new Date(iso + 'T12:00:00').toDateString().slice(0, 3).toUpperCase();
}

/* Not `clock` — that name is already the wall clock in the header, and a
 * second `function clock` simply replaces it at parse time. The gutter
 * labels came out blank and the header kept working, which is the confusing
 * way round for the two of them to fail. */
function hhmm(mins) {
  const m = Math.max(0, Math.min(24 * 60 - 1, Math.round(mins)));
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

function minutesOf(hhmmStr) {
  const [h, m] = String(hhmmStr).split(':').map(Number);
  return h * 60 + m;
}

function paintWeekInto(pane, w) {
  /* `hud.js` is served off disk on every load; `snapshot()` is Python held in
   * a process. So a HUD left running across an upgrade serves a new page from
   * an old server, the nav grows a WEEK button, and the panel behind it is
   * simply absent — which is not the same nothing as an empty week, and used
   * to print the same message. */
  if (!w || !w.days) {
    fill(pane, [el('div.empty', {
      text: 'THIS SNAPSHOT HAS NO WEEK PANEL — THE SERVER PREDATES IT. RESTART v hud.',
    })]);
    return;
  }
  if (!w.exists) {
    fill(pane, [el('div.empty', { text: `NO WEEK NOTE AT ${w.rel} — RUN v week --ensure` })]);
    return;
  }

  const pxm = WEEK_ZOOM[app.weekZoom];
  const height = (w.hi - w.lo) * pxm;
  const at = (m) => (m - w.lo) * pxm;

  /* Hour labels, thinned as the grid tightens so they never collide. */
  const every = pxm < 0.7 ? 180 : (pxm < 1.1 ? 120 : 60);
  const gutter = [];
  for (let m = w.lo; m <= w.hi; m += 60) {
    const major = m % every === 0;
    gutter.push(el('i.line', { style: `top:${at(m)}px`, 'data-major': major ? '1' : '0' }));
    if (major) gutter.push(el('span.h', { text: hhmm(m), style: `top:${at(m)}px` }));
  }

  const cells = [el('div.hcell.hgut', {}, [
    el('button.zoom', {
      type: 'button', title: 'tighter',
      onclick: () => { app.weekZoom = Math.max(0, app.weekZoom - 1); paintWeek(); },
      text: '−',
    }),
    el('button.zoom', {
      type: 'button', title: 'taller',
      onclick: () => { app.weekZoom = Math.min(WEEK_ZOOM.length - 1, app.weekZoom + 1); paintWeek(); },
      text: '+',
    }),
  ])];
  for (const d of w.days) {
    const h = el('div.hcell', {}, [el('b', { text: d.label })]);
    h.dataset.today = d.today ? '1' : '0';
    cells.push(h);
  }

  const gutCol = el('div.gutcol', { style: `height:${height}px` }, gutter);
  const columns = w.days.map((d) => {
    const kids = [];
    for (let m = w.lo + 60; m < w.hi; m += 60) {
      kids.push(el('i.line', { style: `top:${at(m)}px`,
                               'data-major': m % every === 0 ? '1' : '0' }));
    }
    for (const b of d.blocks) {
      const tone = KINDS[b.kind] || C.dim;
      const top = at(b.s);
      const tall = Math.max(3, (b.e - b.s) * pxm);
      /* `depth` insets a block that sits wholly inside a longer one; `lane`
       * splits the remaining width between things that genuinely overlap
       * without containing each other. Drawing order follows depth, so the
       * smaller thing is on top — which is also what the pointer hits, which
       * is the whole reason to do it this way. */
      const indent = Math.min(40, b.depth * 11);
      const width = (100 - indent) / b.lanes;
      const box = el('div.blk', {
        style: `--t:${tone};top:${top}px;height:${tall}px;`
             + `left:${indent + b.lane * width}%;`
             + `width:calc(${width}% - 2px);z-index:${2 + b.depth}`,
        onclick: (e) => { e.stopPropagation(); openBlockEditor(d, b); },
      }, [
        el('span.k', { text: b.kind }),
        b.label ? el('span.n', { text: b.label }) : null,
      ].filter(Boolean));
      box.dataset.past = b.past ? '1' : '0';
      box.dataset.now = b.now ? '1' : '0';
      box.dataset.nested = b.depth ? '1' : '0';
      /* Under ~26px there is room for one line, not two, and forcing two is
       * what clipped a 30-minute standup's title through the middle of its
       * own glyphs. */
      box.dataset.tight = tall < 26 ? '1' : '0';
      /* The same probe every other row on this page has. A block is often
       * too short to show its own title, and hovering is how you read it —
       * `title=` was a browser tooltip a second and a half late, which is not
       * the same thing as the HUD's own. */
      kids.push(probed(box, b.kind.toUpperCase(), b.label || b.kind, [
        ['WHEN', `${d.label} · ${b.time}`],
        ['FOR', hrs(b.mins)],
        b.where ? ['WHERE', b.where] : null,
        b.depth ? ['INSIDE', 'sits within a longer block'] : null,
        ['STATUS', b.now ? 'happening now' : (b.past ? 'past' : 'ahead')],
        ['', 'click to edit or delete'],
      ]));
    }
    if (d.today && w.now_min !== null && w.now_min >= w.lo && w.now_min <= w.hi) {
      kids.push(el('i.nowline', { style: `top:${at(w.now_min)}px` }));
    }
    const col = el('div.col', {
      style: `height:${height}px`,
      /* Click the empty canvas to add something there. The minute under the
       * pointer is snapped to the quarter hour — a calendar that opens a form
       * at 14:07 is a calendar nobody uses twice. */
      onclick: (e) => {
        const box = col.getBoundingClientRect();
        const m = Math.round(((e.clientY - box.top) / pxm + w.lo) / SNAP) * SNAP;
        openBlockEditor(d, null, Math.max(0, Math.min(24 * 60 - 60, m)));
      },
    }, kids);
    col.dataset.today = d.today ? '1' : '0';
    return col;
  });

  const grid = el('div.wk-grid', {}, cells.concat([gutCol], columns));
  const scroll = el('div.wk-scroll', {}, [grid]);

  const foot = w.blocks ? el('div.wk-foot', {}, [
    el('div.wk-load', {}, w.load.map((k) => probed(
      el('i', { style: `--t:${KINDS[k.kind] || C.dim};flex:${k.mins}` }),
      'LOAD', k.kind, [['HOURS', hrs(k.mins)],
                       ['SHARE', Math.round(k.share * 100) + '% of claimed time']]))),
    el('div.legend', {}, w.load.map((k) => el('span', {}, [
      el('i', { style: `background:${KINDS[k.kind] || C.dim}` }),
      el('b', { text: k.kind }), ' ', hrs(k.mins),
    ])).concat([el('span.src', { text: w.rel })])),
  ]) : el('div.wk-foot', {}, [
    el('div.legend', {}, [el('span.src', {
      text: `${w.rel} — nothing under ## Schedule yet. Click anywhere above to add a block.`,
    })]),
  ]);

  fill(pane, [el('div.week', {}, [scroll, foot])]);
  app.weekKinds = w.kinds || Object.keys(KINDS);

  /* Open where the week actually starts. Only on the first paint of a given
   * week — re-centring on every four-second poll would yank the view out from
   * under anyone reading it. */
  if (memo.weekScroll !== w.start + '|' + app.weekZoom) {
    memo.weekScroll = w.start + '|' + app.weekZoom;
    requestAnimationFrame(() => { scroll.scrollTop = at(w.focus); });
  }
}

/* ---- editing a block ---------------------------------------------------
 *
 * The TASKS view has written to the vault since it was built; this is the
 * same contract for time. Every verb here is one `[bot]` commit on one note,
 * reverted by `v undo`, and the server refuses a line it cannot find exactly
 * once — so a tab left open on a week somebody has since changed fails loudly
 * rather than clobbering a neighbour.
 *
 * It renders into `#blockedit`, which lives outside `#pane` on purpose. When
 * it was part of the pane, the four-second poll rebuilt it: every keystroke
 * changed the render signature, the DOM was replaced under the cursor, and
 * the field you were typing into lost its focus and its selection. Nothing on
 * the polling path reaches this node now — it is redrawn only when something
 * actually happens to it.
 */
function openBlockEditor(day, block, minute) {
  app.blockEdit = block
    ? { mode: 'edit', date: day.date, line: block.line, kind: block.kind,
        /* One field for label and location, because that is one line in the
         * note. Showing only the label meant an `@location` you could not
         * see survived every rename you made. */
        label: (block.label || '') + (block.where ? ' @' + block.where : ''),
        from: block.from, to: block.to }
    : { mode: 'add', date: day.date, line: '', kind: 'personal', label: '',
        from: hhmm(minute), to: hhmm(Math.min(24 * 60, minute + 60)) };
  app.blockErr = '';
  app.blockBusy = false;
  drawBlockEditor();
}

function closeBlockEditor() {
  app.blockEdit = null;
  app.blockErr = '';
  app.blockBusy = false;
  drawBlockEditor();
}

/* Redraw only the parts that cannot be edited in place. The inputs are built
 * once per open and then left alone — rebuilding them is what ate keystrokes,
 * so the kind picker and the error line repaint themselves and nothing else
 * is touched. */
function drawBlockEditor() {
  const host = $('blockedit');
  const e = app.blockEdit;
  if (!e) { host.hidden = true; fill(host, []); return; }
  host.hidden = false;

  const adding = e.mode === 'add';
  const bind = (k, node) => {
    node.addEventListener('input', () => { e[k] = node.value; });
    return node;
  };
  const field = (label, node) => el('label.f', {}, [el('span', { text: label }), node]);

  const kinds = el('div.kinds', {}, (app.weekKinds || Object.keys(KINDS)).map((k) => {
    const b = el('button', {
      type: 'button', text: k, style: `--t:${KINDS[k] || C.dim}`,
      onclick: () => {
        e.kind = k;
        for (const other of kinds.children) other.dataset.on = '0';
        b.dataset.on = '1';
      },
    });
    b.dataset.on = e.kind === k ? '1' : '0';
    return b;
  }));

  const err = el('div.err', { text: app.blockErr });
  err.hidden = !app.blockErr;

  const submit = () => (adding
    ? sendBlock('add', { date: e.date, kind: e.kind, from: e.from, to: e.to,
                         label: e.label })
    : sendBlock('move', { date: e.date, line: e.line, to_date: e.date,
                          kind: e.kind, from: e.from, to: e.to, label: e.label }));

  const form = el('div.blk-form', {
    /* Enter saves, Escape closes — the two keys anyone tries. */
    onkeydown: (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); submit(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); closeBlockEditor(); }
    },
  }, [
    el('div.top', {}, [
      el('span.t', { text: adding ? 'NEW BLOCK' : 'EDIT BLOCK' }),
      el('span.fill'),
      el('button.x', { type: 'button', text: '✕', onclick: closeBlockEditor,
                       'aria-label': 'close' }),
    ]),
    kinds,
    el('div.row', {}, [
      field('DATE', bind('date', el('input', { type: 'date', value: e.date }))),
      field('FROM', bind('from', el('input', { type: 'time', value: e.from, step: '300' }))),
      field('TO', bind('to', el('input', { type: 'time', value: e.to, step: '300' }))),
    ]),
    field('LABEL', bind('label', el('input', {
      type: 'text', value: e.label, placeholder: 'Compiler Design @204',
    }))),
    err,
    el('div.acts', {}, [
      adding ? null : el('button.drop', {
        type: 'button', text: 'DELETE',
        title: 'removes the line from the week note · undoable with v undo',
        onclick: () => sendBlock('drop', { date: e.date, line: e.line }),
      }),
      el('span.fill'),
      el('button.ghost', { type: 'button', text: 'CANCEL', onclick: closeBlockEditor }),
      el('button.go', { type: 'button', text: adding ? 'ADD' : 'SAVE',
                        onclick: submit }),
    ].filter(Boolean)),
  ]);

  /* The scrim closes, the sheet does not — a mis-click at the edge of a form
   * should not throw away what was typed into it. */
  fill(host, [el('div.blk-sheet', { onclick: closeBlockEditor }, [
    el('div.inner', { onclick: (ev) => ev.stopPropagation() }, [form]),
  ])]);

  const first = form.querySelector('input');
  if (first) requestAnimationFrame(() => first.focus());
}

function blockError(message) {
  app.blockErr = String(message || '').toUpperCase();
  const host = $('blockedit');
  const line = host.querySelector('.err');
  if (!line) return drawBlockEditor();
  line.textContent = app.blockErr;
  line.hidden = !app.blockErr;
}

async function sendBlock(action, body) {
  if (app.blockBusy) return;
  /* The date the block currently lives on is what names its week note, so a
   * move to another day sends both: `date` to find it, `to_date` to put it. */
  if (action !== 'add' && app.blockEdit && app.blockEdit.line) {
    const was = app.blockEdit.line.match(/^\s*[-*+]\s+(\d{4}-\d{2}-\d{2})/);
    if (was) body.date = was[1];
  }
  app.blockBusy = true;
  blockError('');
  try {
    await post('/api/block', Object.assign({ action }, body));
    app.blockEdit = null;
    app.blockBusy = false;
    drawBlockEditor();
    memo.pane = '';
    await refresh(true);
  } catch (err) {
    app.blockBusy = false;
    blockError(err.message || err);
  }
}

function paintMemory() {
  const mems = (app.snap && app.snap.mems) || [];
  renderPane(JSON.stringify(mems), (node) => paintMemoryInto(node, mems));
}

function paintMemoryInto(pane, mems) {
  if (!mems.length) { fill(pane, [el('div.empty', { text: 'NOTHING LEARNED YET.' })]); return; }
  fill(pane, [el('div.mems', {}, mems.map((m) => {
    const tone = m.score >= 0.7 ? C.green : (m.score >= 0.35 ? C.cyan : (m.score >= 0.2 ? C.amber : C.red));
    const card = el('div.mem', { style: `--t:${tone}` }, [
      el('div.top', {}, [
        el('span.kind', { text: m.kind }),
        el('span.fill'),
        el('span.seen', { text: m.seen }),
      ]),
      el('div.text', { text: m.text }),
      el('div.decay', {}, [
        el('span', { text: 'DECAY' }),
        el('span.track', {}, [el('i', { style: `width:${pct(m.score)}` })]),
        el('span.score', { text: m.score.toFixed(2) }),
      ]),
    ]);
    return probed(card, 'MEMORY #' + m.id, m.kind.toLowerCase() + ' · ' + m.agent, [
      ['STRENGTH', m.score.toFixed(2) + ' after decay'],
      ['CONFIDENCE', String(m.confidence)],
      ['SOURCE', m.source],
      m.why ? ['WHY', m.why] : null,
      ['SCOPE', m.agent],
    ]);
  }))]);
}

function paintVault() {
  renderPane(app.query + '|' + JSON.stringify(app.hits), (node) => paintVaultInto(node));
}

function paintVaultInto(pane) {
  if (!app.query) {
    fill(pane, [el('div.empty', { text: 'TYPE BELOW TO SEARCH THE VAULT. ENTER RUNS IT.' })]);
    return;
  }
  const hits = (app.hits && app.hits.hits) || [];
  if (!hits.length) {
    fill(pane, [el('div.empty', { text: `NO MATCH FOR “${app.query}”.` })]);
    return;
  }
  /* The bar is relative to the best hit, not absolute. A dense-embedding
   * score and a tf-idf score are both "relevance" and are nowhere near the
   * same scale — tf-idf lands around 0.03 on this vault, which as an absolute
   * bar is an empty track under every row. The number next to it stays raw,
   * because that is the value the backend actually reported. */
  const top = hits.reduce((m, h) => Math.max(m, h.score || 0), 0) || 1;
  fill(pane, [el('div.hits', {}, hits.map((h) => {
    const score = h.score === null ? null : h.score;
    const rel = score === null ? 1 : score / top;
    const tone = rel >= 0.7 ? C.cyan : (rel >= 0.4 ? C.green : C.amber);
    const row = el('div.hit', { style: `--t:${tone}` }, [
      el('span.score', { text: score === null ? '·'
        : (score >= 0.1 ? score.toFixed(2) : score.toFixed(3)) }),
      el('span', {}, [
        el('span.title', { text: h.title }),
        el('span.path', { text: h.path }),
      ]),
      el('span.track', {}, [el('i', { style: `width:${pct(score === null ? 0.5 : score / top)}` })]),
    ]);
    return probed(row, 'VAULT HIT', h.title, [
      ['MATCH', h.why + (score === null ? '' : ' ' + score.toFixed(2))],
      h.heading ? ['SECTION', h.heading] : null,
      ['PATH', h.path],
      h.snippet ? ['SNIPPET', h.snippet] : null,
    ]);
  }))]);
}

/* ---- right rail --------------------------------------------------------- */
function paintPanels(snap, live) {
  if (!changed('panels', JSON.stringify([snap.day_events, snap.tasks,
      snap.deadlines, snap.mail, snap.nudges, snap.nudges_off, live.pending,
      snap.task_counts, app.nudgeBusy, app.nudgeError]))) return;
  hideProbe();
  /* TODAY */
  const events = snap.day_events.slice(0, 4);
  $('tag-today').textContent = `M-01 · ${snap.day_events.length} EV`;
  fill($('p-today'), events.length ? events.map((d) => {
    const tone = d.kind === 'v' ? C.violet : (d.past ? C.dim : C.cyan);
    return probed(el('div.ev', { style: `--t:${tone}`, onclick: () => go('agenda') }, [
      /* The start only. `13:00-15:30` needs twice this column and wrapped to
       * two lines; the full label is on the AGENDA row and in the probe. */
      el('span.t', { text: (d.time || '—').split('-')[0] }),
      el('span.bar'),
      el('span.n', { text: d.title }),
      el('span.s', { text: d.src }),
    ]), 'EVENT', d.title, [['WHEN', d.at || d.time || 'all day'],
                            d.where ? ['WHERE', d.where] : null,
                            ['SOURCE', d.src === 'REM' ? 'v reminders' : 'vault calendar']]);
  }) : [el('div.empty', { style: 'padding:6px 2px', text: 'nothing today' })]);

  /* TASKS */
  /* `vault.tasks_open` counted every unticked box in the vault — the MR review
   * checklists, the lesson rules, `- [ ] Trained today` in every daily note —
   * and put the total on this card as "OPEN". It read as 800 things owed. The
   * count here is commitments only, collapsed across the notes they were
   * carried through, minus what has been silenced, which is the number the
   * task view is built to bring down. */
  const tc = snap.task_counts || { open: 0, ignored: 0 };
  $('tag-tasks').textContent = `M-02 · ${tc.open} OPEN`
    + (tc.ignored ? ` · ${tc.ignored} OFF` : '');
  fill($('p-tasks'), snap.tasks.length ? snap.tasks.map((t) => {
    const tone = t.rank === 'P0' ? C.red : (t.rank === 'P1' ? C.amber : '#3F6472');
    const b = el('button.task', { type: 'button', style: `--t:${tone}`,
      onclick: () => toggleTask(t, b) }, [
      el('span.box', { text: t.done ? '✓' : '' }),
      el('span.n', { text: t.title }),
      el('span.rank', { text: t.rank }),
    ]);
    b.dataset.done = t.done ? '1' : '0';
    return probed(b, 'TASK · ' + t.rank, t.title, [
      ['WHY RANKED', t.why], ['WHERE', t.where], ['NOTE', t.rel],
      ['CLICK', t.done ? 'reopens it in the note' : 'ticks it in the note'],
      ['DROP OR IGNORE', 'in the TASKS view — 02 in the left rail'],
    ]);
  }) : [el('div.empty', { style: 'padding:6px 2px', text: 'nothing ranked' })]);

  /* DEADLINES */
  $('tag-dl').textContent = `M-03 · ${snap.deadlines.length ? 'QUERA' : 'CLEAR'}`;
  fill($('p-dl'), snap.deadlines.length ? snap.deadlines.map((d) => {
    const tone = d.days <= 1 ? C.red : (d.days <= 3 ? C.amber : '#7FB6C7');
    return probed(el('div.dl', { style: `--t:${tone};--tg:${rgba(tone, .6)}` }, [
      el('span.n', { text: d.title }),
      el('span.left', { text: d.left }),
    ]), d.days <= 1 ? 'DEADLINE · CRITICAL' : 'DEADLINE', d.title, [
      ['COURSE', d.course || '—'], ['T-MINUS', d.left],
      ['STATUS', d.status || 'open'], ['NOTE', d.rel],
    ]);
  }) : [el('div.empty', { style: 'padding:6px 2px', text: 'nothing due in 30 days' })]);

  /* INBOX */
  const mail = snap.mail || { state: 'off', items: [] };
  const mailLabel = { on: `${mail.items.length} SHOWN`, unconfigured: 'NOT SET UP',
                      unauthorised: 'NOT SIGNED IN', error: 'UNREACHABLE', off: 'OFF' }[mail.state];
  $('tag-mail').textContent = 'M-04 · ' + mailLabel;
  fill($('p-mail'), mail.items.length ? mail.items.map((m) =>
    probed(el('div.msg', { style: `--t:${C.cyan}` }, [
      el('span.dot'),
      el('span.from', { text: m.from }),
      el('span.subj', { text: m.subject }),
    ]), 'MESSAGE', m.subject, [['FROM', m.from], ['WHEN', m.when || '—'],
                               ['SOURCE', 'gmail · in:inbox unread'],
                               ['CACHE', 'refreshed at most every 5m']])
  ) : [el('div.empty', { style: 'padding:6px 2px', text:
      mail.state === 'unconfigured' ? 'gmail not connected — v gmail'
      : mail.state === 'unauthorised' ? 'gmail needs signing in — v gmail'
      : mail.state === 'error' ? 'gmail unreachable' : 'inbox clear' })]);

  /* WRITES — this session's, with the confidence each landed at. */
  const writes = live.pending || [];
  $('tag-writes').textContent = `M-05 · ${writes.length} THIS SESSION`;
  fill($('p-writes'), writes.length ? writes.map((w) =>
    probed(el('div.write', {}, [
      el('div.text', { text: w.path }),
      el('div.bottom', {}, [
        el('span.k', { text: 'CONF' }),
        el('span.track', {}, [el('i', { style: `width:${pct(w.confidence)}` })]),
        el('span.pct', { text: w.confidence.toFixed(2) }),
        el('span.state', { text: w.status.toUpperCase() }),
      ]),
    ]), 'VAULT WRITE', w.path, [
      ['STATE', w.status], ['CONFIDENCE', w.confidence.toFixed(2)],
      ['REVERSIBLE', 'yes — v undo'],
      ['NOTE', 'appended and committed as [bot]'],
    ])
  ) : [el('div.empty', { style: 'padding:6px 2px', text: 'nothing written this session' })]);

  /* NUDGES */
  const off = snap.nudges_off || [];
  $('tag-nudges').textContent = `M-06 · ${snap.nudges.length} FIRED`
    + (off.length ? ` · ${off.length} OFF` : '');
  const rows = snap.nudges.map((n) => {
    const tone = n.urgency >= 3 ? C.red : (n.urgency === 2 ? C.amber : (n.urgency === 1 ? C.cyan : '#5E8C9C'));
    return probed(el('div.nudge', { style: `--t:${tone}` }, [
      el('span.bar'),
      el('span', {}, [el('span.k', { text: n.kind }), el('span.n', { text: n.text })]),
      /* Only on a nudge that has a key. A check that *crashed* arrives
       * without one — the server refuses to silence those, and a button that
       * is going to be refused should not be drawn. */
      n.key ? el('button.nx', {
        type: 'button', title: 'silence — stop raising this until I restore it',
        disabled: app.nudgeBusy === n.key,
        onclick: (ev) => { ev.stopPropagation(); nudgeAction('silence', n.key); },
      }, [el('span', { text: '⊗' })]) : null,
    ].filter(Boolean)), 'NUDGE', n.kind.toLowerCase(), [
      ['URGENCY', ['fyi', 'worth a look', 'today', 'act now'][n.urgency] || '—'],
      n.detail ? ['DETAIL', n.detail] : null,
      ['CHECK', 'free · SQL over the vault'],
      n.key ? ['SILENCE', '⊗ hides `' + n.key + '` everywhere until restored'] : null,
    ]);
  });
  if (!rows.length) {
    rows.push(el('div.empty', { style: 'padding:6px 2px', text: 'nothing to flag' }));
  }
  /* Under the panel it censors, never behind a menu. A silence list you have
   * to go looking for is one you forget you made, and then "nothing needs
   * you" has quietly stopped being true. */
  if (off.length) {
    rows.push(el('div.nudge-off', {}, [
      el('div.h', { text: 'SILENCED' }),
      ...off.map((s) => probed(el('div.r', {}, [
        el('span.n', { text: s.text || s.key }),
        el('button.nx', {
          type: 'button', title: 'restore — back in every list V builds',
          disabled: app.nudgeBusy === s.key,
          onclick: (ev) => { ev.stopPropagation(); nudgeAction('restore', s.key); },
        }, [el('span', { text: '↺' })]),
      ]), 'SILENCED CHECK', s.key, [
        ['SINCE', s.at],
        s.reason ? ['WHY', s.reason] : null,
        ['RESTORE', '↺ puts it back in the brief, the digest and this panel'],
      ])),
    ]));
  }
  if (app.nudgeError) {
    rows.push(el('div.empty', { style: `padding:6px 2px;color:${C.red}`,
                                text: app.nudgeError }));
  }
  fill($('p-nudges'), rows);
}

/** Silence or restore one check, then repaint from a forced snapshot. */
async function nudgeAction(action, key) {
  /* Set before the await, so a double-click is dropped by the guard rather
   * than by a disabled attribute that has not been painted yet. */
  if (app.nudgeBusy) return;
  app.nudgeBusy = key;
  try {
    await post('/api/nudge', { action, key });
    app.nudgeError = '';
  } catch (err) {
    app.nudgeError = String(err.message || err);
  } finally {
    app.nudgeBusy = '';
  }
  /* Forced: the panel is drawn from the cached snapshot, and the whole point
   * of the click was to change what that snapshot says. */
  await refresh(true);
}

/* ---- actions ------------------------------------------------------------ */
function go(view) {
  app.view = view;
  /* The pane holds whichever view drew last, so a signature that happens to
   * match is not evidence the right thing is on screen. */
  memo.pane = null;
  for (const b of $('nav').children) {
    b.setAttribute('aria-current', String(b.dataset.view === view));
  }
  $('input').placeholder = view === 'vault'
    ? 'search the vault — enter runs it'
    : 'ask V anything · / for commands';
  /* Fetched on entry rather than ridden along on the four-second poll: this
   * is 120 rows to render six of them everywhere else on the page, and the
   * card in the right rail already covers the glance. */
  if (view === 'tasks') loadTasks();
  if (app.snap) paintPaneHead(app.snap);
  paintPane();
  $('input').focus();
}

async function post(path, body) {
  const r = await fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.error || r.statusText);
  return out;
}

async function toggleTask(t, button) {
  button.disabled = true;
  try {
    await post('/api/task', { id: t.id, state: t.done ? 'open' : 'done' });
    t.done = !t.done;
    button.dataset.done = t.done ? '1' : '0';
    button.querySelector('.box').textContent = t.done ? '✓' : '';
    refresh(true);
  } catch (err) {
    button.querySelector('.rank').textContent = 'ERR';
  } finally {
    button.disabled = false;
  }
}

/** Run a slash command. Client-side ones never leave the page. */
async function run(line) {
  const parsed = /^\/(\w*)\s*(.*)$/.exec(line.trim());
  if (!parsed) return;
  const [, cmd, rest] = parsed;
  hidePalette();
  $('input').value = '';
  $('blink').hidden = false;

  if (cmd === '' || cmd === 'help') { $('input').value = '/'; drawPalette(); return; }
  if (cmd === 'why') return toggleTrace();
  if (cmd === 'copy') {
    const turns = (app.live && app.live.turns) || [];
    const last = [...turns].reverse().find((t) => t.role === 'v');
    if (!last) return;
    try { await navigator.clipboard.writeText(last.text); } catch (e) { /* denied */ }
    return;
  }

  app.busy = true;
  $('send').disabled = true;
  try {
    await post('/api/command', { text: '/' + cmd + (rest ? ' ' + rest : '') });
    refresh(true);
  } catch (err) {
    if (app.live) {
      app.live.turns.push({ role: 'v', text: String(err.message || err), time: '',
        route: 'error', cites: [], cost: 0, ms: 0, model: '', confidence: null,
        state: 'refused' });
      memo.pane = null;
      paintPane();
    }
  } finally {
    app.busy = false;
    $('send').disabled = false;
    $('input').focus();
  }
}

async function send(text) {
  const value = (text === undefined ? $('input').value : text).trim();
  if (!value || app.busy) return;
  /* A command is a command in every view. Searching for "/new" is not what
   * anyone meant by typing it. */
  if (value.startsWith('/')) {
    const rows = paletteRows();
    /* An unknown slash still goes to the server, which answers "unknown
     * command" for free rather than paying a model to guess. */
    if (rows && rows.length && !COMMANDS.some((c) => c.cmd === value.split(' ')[0])) {
      return run(rows[picked].cmd + (value.includes(' ')
        ? ' ' + value.split(' ').slice(1).join(' ') : ''));
    }
    return run(value);
  }
  if (app.view === 'vault') return runSearch(value);
  app.busy = true;
  $('send').disabled = true;
  $('input').value = '';
  try {
    await post('/api/chat', { text: value });
    refresh(true);
  } catch (err) {
    /* The failure is already in the session as a fault state; this only makes
     * sure the pane says so even if the SSE frame was missed. */
    if (app.live) { app.live.turns.push({ role: 'v', text: String(err.message || err),
      time: '', route: 'error', cites: [], cost: 0, ms: 0, model: '',
      confidence: null, state: 'refused' }); paintPane(); }
  } finally {
    app.busy = false;
    $('send').disabled = false;
    $('input').focus();
  }
}

async function runSearch(q) {
  app.query = q;
  $('input').value = q;
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    app.hits = await r.json();
  } catch (err) {
    app.hits = { backend: 'unreachable', hits: [] };
  }
  if (app.snap) paintPaneHead(app.snap);
  paintPane();
}

/* ---- the viewport -------------------------------------------------------
 * The pictures a turn is about, on the surface that has room for one.
 *
 * `hud.py` finds them; this draws them. Three rules it follows, each of them
 * a thing that went wrong first:
 *
 * **The picture is never markup.** `/api/media?id=…` takes a hash the server
 * minted for a file it had already decided to show. There is no filename in
 * the URL, so nothing a note or an email subject contains can steer it — the
 * same argument `el()` makes for text, made for bytes.
 *
 * **It redraws only on a real change.** The live stream fires on every tool
 * call. Rebuilding the <img> on that cadence restarted the download each
 * time, which on a 4 MB screenshot is a picture that never finishes arriving.
 *
 * **An arrival is visible; a redraw is not.** The scan line runs once, on a
 * frame that was not on screen a moment ago. Everything else — clicking the
 * filmstrip, a poll landing — is silent, because a panel that flashes every
 * four seconds is one you turn off.
 */
const mediaSrc = (id) => '/api/media?id=' + encodeURIComponent(id);

function fmtBytes(n) {
  if (!n) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function frameById(id) {
  return ((app.live && app.live.frames) || []).find((f) => f.id === id) || null;
}

/** The picture itself: an <img> for an image, a sandboxed frame for a page.
 *  Sized entirely in CSS, so the same node serves the panel and the lightbox. */
function frameMedia(f) {
  if (f.kind === 'page') {
    /* A rendered chart or map is a whole document, so it is framed rather
     * than drawn. `sandbox` without `allow-same-origin` puts it in an opaque
     * origin: it can draw itself and reach Neshan for tiles, and it can read
     * nothing of this page — not the DOM around it, not storage, not the
     * conversation. The server sends it `ARTIFACT_CSP` to match. */
    return el('iframe', { src: mediaSrc(f.id), sandbox: 'allow-scripts',
                          loading: 'lazy', title: f.title || 'rendered artifact' });
  }
  return el('img', { src: mediaSrc(f.id), alt: f.title || '',
                     loading: 'lazy', decoding: 'async' });
}

/** Fit a whole rendered page inside the panel's glass.
 *
 * `render._shell` authors at 720–900px wide. Dropped into a 320px frame it
 * lays out *at* 320px and scrolls, which puts the middle of a bar chart on
 * screen and none of its labels — a picture of a chart that cannot be read
 * is worse than no picture, because it looks like the surface is broken.
 * So the document is laid out at its real width and the whole thing scaled
 * down: a thumbnail of the page rather than a crop of it. Click it and the
 * lightbox shows it at a size meant for reading.
 */
function fitPage(glass, frame) {
  /* Wider than tall on purpose. A chart page is mostly white below its last
   * bar, so fitting to the page's full height would shrink it to a stamp in
   * the middle of the glass to make room for nothing; the height here is the
   * part worth showing, and what falls off the bottom is the margin. */
  const W = 780, H = 460;
  const w = glass.clientWidth, h = glass.clientHeight;
  if (!w || !h) return;
  const k = Math.min(w / W, h / H);
  frame.style.cssText = `position:absolute;top:0;left:${((w - W * k) / 2).toFixed(1)}px;`
    + `width:${W}px;height:${H}px;transform:scale(${k.toFixed(4)});transform-origin:0 0`;
}

function paintFrames(live) {
  const frames = (live && live.frames) || [];
  const vp = $('viewport');
  /* `null` means no payload has landed yet — a reload, not a clear. The two
   * have to be told apart: on a reload whatever is already there is history
   * and must not scan in, while the first picture after a clear is the most
   * important arrival there is. */
  const booted = app.seenFrames !== null;
  if (!frames.length) {
    vp.hidden = true;
    app.frame = '';
    app.seenFrames = new Set();
    if (app.lightbox) closeLightbox();
    memo.frames = null;
    return;
  }
  const known = booted ? app.seenFrames : new Set(frames.map((f) => f.id));
  const fresh = frames.filter((f) => !known.has(f.id)).map((f) => f.id);
  app.seenFrames = new Set(frames.map((f) => f.id));

  /* A new picture takes the glass. Otherwise whatever was being looked at
   * keeps it — a poll landing must not yank the view off the frame someone
   * clicked to two seconds ago. */
  if (fresh.length) app.frame = frames[0].id;
  if (!frames.some((f) => f.id === app.frame)) app.frame = frames[0].id;

  const sig = JSON.stringify([frames.map((f) => f.id), app.frame]);
  if (!changed('frames', sig)) return;
  vp.hidden = false;

  const f = frameById(app.frame) || frames[0];
  const src = SOURCES[f.source] || SOURCES.note;
  $('vp-count').textContent = frames.length > 1
    ? `${frames.indexOf(f) + 1}/${frames.length}` : src.abbr;

  const glass = $('vp-glass');
  glass.dataset.kind = f.kind;
  /* The decoration is kept and the media swapped, rather than rebuilding the
   * box: the corner brackets and the scan line are the frame, not content. */
  for (const node of [...glass.children]) {
    if (!node.classList.contains('scan') && !node.classList.contains('corner')) node.remove();
  }
  const media = frameMedia(f);
  glass.insertBefore(media, glass.firstChild);
  if (f.kind === 'page') fitPage(glass, media);
  glass.dataset.fresh = fresh.includes(f.id) ? '1' : '0';

  $('vp-title').textContent = f.title || '(untitled)';
  fill($('vp-sub'), [
    el('b', { text: src.label }), ' · ',
    f.caption || f.rel.split('/').pop().replace(/\.md$/, '') || f.kind,
    f.bytes ? ' · ' + fmtBytes(f.bytes) : '', ' · ' + f.time,
  ]);

  fill($('vp-strip'), frames.length < 2 ? [] : frames.map((g) => {
    const s = SOURCES[g.source] || SOURCES.note;
    const b = el('button', { type: 'button', title: g.title,
      'aria-current': String(g.id === f.id),
      onclick: () => { app.frame = g.id; memo.frames = null; paintFrames(app.live); } },
      [g.kind === 'page' ? el('span', { text: s.abbr })
                         : el('img', { src: mediaSrc(g.id), alt: '', loading: 'lazy' })]);
    return probed(b, 'FRAME · ' + s.label, g.title, [
      ['SOURCE', s.label], ['FROM', g.rel || g.caption || '—'],
      ['KIND', g.kind], ['SIZE', fmtBytes(g.bytes) || '—'], ['AT', g.time],
    ]);
  }));
}

/** Full size, over everything. A page too: a chart shrunk into the panel is
 *  the one frame that most needs somewhere bigger to be read. */
function openLightbox(id) {
  const f = frameById(id);
  if (!f) return;
  app.lightbox = id;
  const src = SOURCES[f.source] || SOURCES.note;
  $('lb-kind').textContent = src.label;
  $('lb-title').textContent = f.title || '(untitled)';
  $('lb-sub').textContent = [f.rel || f.caption, fmtBytes(f.bytes)]
    .filter(Boolean).join(' · ');
  fill($('lb-stage'), [frameMedia(f)]);
  $('lightbox').hidden = false;
}

function closeLightbox() {
  app.lightbox = '';
  $('lightbox').hidden = true;
  fill($('lb-stage'), []);
}

/** Step through the strip. Only bound inside the lightbox, where the arrow
 *  keys are not already the palette's. */
function stepLightbox(by) {
  const frames = (app.live && app.live.frames) || [];
  const i = frames.findIndex((f) => f.id === app.lightbox);
  const n = frames.length;
  if (i < 0 || n < 2) return;
  const next = frames[(((i + by) % n) + n) % n];   // wraps both ways
  app.frame = next.id;
  memo.frames = null;
  paintFrames(app.live);
  openLightbox(next.id);
}

/** The close button. The files stay exactly where they are — this is the
 *  screen being cleared, which is why it is the same verb as `/clear`. */
async function clearFrames() {
  try {
    const out = await post('/api/frames', { action: 'clear' });
    if (out.live) applyLive(out.live);
  } catch (err) {
    $('viewport').hidden = true;      // the server said no; the panel still goes
  }
}

/* ---- the two channels --------------------------------------------------- */
function applyLive(live) {
  app.live = live;
  paintState(live.state.name, live.state.detail, live.state.tools);
  paintRoute(live);
  paintBus(live);
  paintFrames(live);
  if (app.snap) {
    paintHeader(app.snap, live);
    paintGauges(app.snap, live);
    paintFooter(app.snap, live);
    paintPanels(app.snap, live);
    paintPaneHead(app.snap);
  }
  paintPane();
}

async function refresh(force) {
  try {
    const r = await fetch('/api/state' + (force ? '?force=1' : ''));
    if (!r.ok) throw new Error('state ' + r.status);
    const snap = await r.json();
    app.snap = snap;
    syncClock(snap.at);
    setOffline(false);
    $('hud').dataset.agent = snap.agent === 'v' ? 'v' : snap.agent;
    if (snap.live) applyLive(snap.live);
    else if (app.live) applyLive(app.live);
    /* Only while it is the visible view. A task ticked in Obsidian should
     * show up here without a reload; a task list nobody is looking at should
     * not be queried every four seconds. */
    if (app.view === 'tasks' && !app.taskBusy) loadTasks();
  } catch (err) {
    setOffline(true);
  }
}

function setOffline(down) {
  app.connected = !down;
  $('offline').hidden = !down;
}

function stream() {
  const src = new EventSource('/api/events');
  src.onmessage = (ev) => {
    setOffline(false);
    try { applyLive(JSON.parse(ev.data)); } catch (e) { /* a partial frame */ }
  };
  src.onerror = () => {
    setOffline(true);
    src.close();
    /* The server is on loopback; if it is gone it is because it was stopped,
     * and the tab should reconnect the moment it comes back rather than make
     * anyone reload. */
    setTimeout(stream, 2000);
  };
}

/* ---- boot --------------------------------------------------------------- */

/* The header clock runs on **V's** clock, not the browser's.
 *
 * `config.py` pins the process to `TZ=Asia/Tehran`, which is routinely hours
 * off the machine's ambient zone — this box runs several hours behind it. Left
 * on `new Date()` the header read 04:14 while the tool bus underneath it
 * timestamped the same calls 23:40 and the agenda was headed with yesterday's
 * date. All three are on the page at once, so they have to be one clock, and
 * the only one that matters is the one V files notes and fires reminders on.
 *
 * The offset is measured from the snapshot's `at` and the seconds are ticked
 * locally, so the clock stays smooth between four-second polls. */
let clockSkew = 0;

function clock() {
  const d = new Date(Date.now() + clockSkew), p = (n) => String(n).padStart(2, '0');
  $('clock').textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function syncClock(at) {
  const server = Date.parse(at);
  if (!Number.isNaN(server)) {
    clockSkew = server - Date.now();
    clock();
  }
}

function parallax() {
  /* The design's two drifting grids. Off entirely under reduced motion — it is
   * the one effect here that moves with the pointer, which is exactly what
   * that setting is asking to stop. */
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  window.addEventListener('mousemove', (e) => {
    const x = e.clientX / window.innerWidth - 0.5;
    const y = e.clientY / window.innerHeight - 0.5;
    $('bg1').style.transform = `translate(${(-x * 14).toFixed(1)}px,${(-y * 14).toFixed(1)}px)`;
    $('bg2').style.transform = `translate(${(x * 26).toFixed(1)}px,${(y * 26).toFixed(1)}px)`;
  }, { passive: true });
}

let typingSent = 0;
function boot() {
  buildNav(); buildStates(); buildWave(); buildArcs(); buildQuick();
  paintState('idle', 'connecting…', []);
  go('console');
  clock(); setInterval(clock, 1000);
  $('jdate').addEventListener('mouseenter', () => {
    if (jday) showProbe($('jdate'), 'DATE', jday.label, jalaliRows(jday));
  });
  $('jdate').addEventListener('mouseleave', hideProbe);
  parallax();

  $('form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('why').addEventListener('click', toggleTrace);

  /* The viewport. Clicking the glass blows the picture up; clicking ✕ says
   * stop showing me these, which is the screen being cleared and not a file
   * being deleted — see `/api/frames`. */
  $('vp-glass').addEventListener('click', () => openLightbox(app.frame));
  $('vp-close').addEventListener('click', clearFrames);
  $('lb-close').addEventListener('click', closeLightbox);
  $('lightbox').addEventListener('click', (e) => {
    if (e.target === $('lightbox')) closeLightbox();   // the backdrop, not the sheet
  });
  /* Escape reaches here from anywhere, including the prompt — a picture over
   * the whole screen is the one thing on this page that has to be dismissable
   * without first working out what has focus. The palette's own Escape is on
   * the input and runs first, which is the right order: close the small thing
   * before the big one. */
  window.addEventListener('keydown', (e) => {
    if (!app.lightbox) return;
    if (e.key === 'Escape') { e.preventDefault(); closeLightbox(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); stepLightbox(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); stepLightbox(-1); }
  });

  fetch('/api/commands').then((r) => r.json())
    .then((d) => { COMMANDS = d.commands || []; })
    .catch(() => { COMMANDS = []; });

  $('input').addEventListener('keydown', (e) => {
    const rows = paletteRows();
    if (!rows || !rows.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); picked = (picked + 1) % rows.length; drawPalette(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); picked = (picked - 1 + rows.length) % rows.length; drawPalette(); }
    else if (e.key === 'Tab') { e.preventDefault(); $('input').value = rows[picked].cmd + ' '; drawPalette(); }
    else if (e.key === 'Escape') { hidePalette(); }
  });
  $('input').addEventListener('blur', () => setTimeout(hidePalette, 140));

  $('input').addEventListener('input', () => {
    $('blink').hidden = $('input').value.length > 0;
    drawPalette();
    /* Throttled hard: this is the `listening` state's only evidence, and it is
     * a timestamp — never the text. Nothing typed leaves the tab until Enter. */
    const now = Date.now();
    if ($('input').value && now - typingSent > 1200) {
      typingSent = now; post('/api/typing').catch(() => {});
    }
  });
  window.addEventListener('scroll', hideProbe, true);
  /* A scaled page is fitted to a measured box, so a resized window leaves it
   * at the wrong scale. Refitted rather than repainted: rebuilding the node
   * would restart the download on every pixel of a drag. */
  window.addEventListener('resize', () => {
    const glass = $('vp-glass'), page = glass.querySelector('iframe');
    if (page) fitPage(glass, page);
  }, { passive: true });

  stream();
  refresh(true);
  setInterval(() => { if (!app.busy) refresh(false); }, 4000);
}

document.addEventListener('DOMContentLoaded', boot);
