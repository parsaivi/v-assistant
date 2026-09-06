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
  { id: 'agenda',  idx: '02', label: 'AGENDA',  title: 'DAY' },
  { id: 'memory',  idx: '03', label: 'MEMORY',  title: 'MEMORY STORE' },
  { id: 'vault',   idx: '04', label: 'VAULT',   title: 'VAULT SEARCH' },
];

/* Every one of these is a real thing V already understands in plain language;
 * the palette is a shortcut, never a second language. (Design brief §03.) */
const QUICK = [
  { label: '/why',    act: () => toggleTrace() },
  { label: '/brief',  act: () => send('brief me') },
  { label: '/tasks',  act: () => go('agenda') },
  { label: '/memory', act: () => go('memory') },
  { label: '/search', act: () => go('vault') },
  { label: '/undo',   act: () => send('/undo') },
];

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
  query: '',
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
  fill($('quick'), QUICK.map((q) =>
    el('button', { type: 'button', text: q.label, onclick: q.act })));
}

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
function paintHeader(snap, live) {
  const s = live.session;
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
  const used = s.spent;
  const drift = g.drift.files;
  fill($('gauges'), [
    gauge('BUDGET', `${money(used)} / ${money(cap)}`, used / cap,
      used / cap > 0.8 ? C.amber : C.green, 'GAUGE', `Session spend ${money(used)}`, [
        ['CAP', money(cap) + ' daily'],
        ['TURNS', `${s.turns} (${s.local} free)`],
        ['MODEL', snap.header.model || 'local'],
        ['NOTE', 'session total, not the day\'s'],
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
  $('t-spend').textContent = money(s.spent);
  $('t-cap').textContent = 'of ' + money(snap.gauges.budget.cap) + ' cap';
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
    agenda: () => 'vault calendar + reminders',
    memory: () => snap ? `${snap.header.memories} active · decay on read` : '',
    vault: () => app.hits ? `${app.hits.backend}${app.hits.ms ? ' · ' + app.hits.ms + 'ms' : ''}` : 'semantic when embeddings are built',
  }[app.view];
  $('pane-m').textContent = meta ? meta() : '';
  const right = {
    console: () => app.live && app.live.session.spent ? money(app.live.session.spent) + ' THIS SESSION' : 'FREE SO FAR',
    agenda: () => snap ? `${snap.day_events.length} ITEMS` : '',
    memory: () => 'SORTED BY SALIENCE',
    vault: () => s ? `${s.notes.toLocaleString()} NOTES INDEXED` : '',
  }[app.view];
  $('pane-r').textContent = right ? right() : '';
}

/** Render the pane, but only if its own data moved. `key` is the signature. */
function renderPane(key, draw) {
  const node = $('pane');
  node.dataset.stale = app.snap && app.snap.stale ? '1' : '0';
  if (!changed('pane', app.view + '|' + key)) return;
  hideProbe();
  const top = node.scrollTop;
  draw(node);
  node.scrollTop = top;
}

function paintPane() {
  if (app.view === 'console') return paintConsole();
  if (app.view === 'agenda') return paintAgenda();
  if (app.view === 'memory') return paintMemory();
  if (app.view === 'vault') return paintVault();
}

let lastTurns = 0;

function paintConsole() {
  const turns = (app.live && app.live.turns) || [];
  const node = $('pane');
  node.dataset.stale = app.snap && app.snap.stale ? '1' : '0';
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
      snap.deadlines, snap.mail, snap.nudges, live.pending,
      snap.vault.tasks_open]))) return;
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
  $('tag-tasks').textContent = `M-02 · ${snap.vault.tasks_open} OPEN`;
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
  $('tag-nudges').textContent = `M-06 · ${snap.nudges.length} FIRED`;
  fill($('p-nudges'), snap.nudges.length ? snap.nudges.map((n) => {
    const tone = n.urgency >= 3 ? C.red : (n.urgency === 2 ? C.amber : (n.urgency === 1 ? C.cyan : '#5E8C9C'));
    return probed(el('div.nudge', { style: `--t:${tone}` }, [
      el('span.bar'),
      el('span', {}, [el('span.k', { text: n.kind }), el('span.n', { text: n.text })]),
    ]), 'NUDGE', n.kind.toLowerCase(), [
      ['URGENCY', ['fyi', 'worth a look', 'today', 'act now'][n.urgency] || '—'],
      n.detail ? ['DETAIL', n.detail] : null,
      ['CHECK', 'free · SQL over the vault'],
    ]);
  }) : [el('div.empty', { style: 'padding:6px 2px', text: 'nothing to flag' })]);
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

async function send(text) {
  const value = (text === undefined ? $('input').value : text).trim();
  if (!value || app.busy) return;
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

/* ---- the two channels --------------------------------------------------- */
function applyLive(live) {
  app.live = live;
  paintState(live.state.name, live.state.detail, live.state.tools);
  paintRoute(live);
  paintBus(live);
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
  parallax();

  $('form').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('why').addEventListener('click', toggleTrace);
  $('input').addEventListener('input', () => {
    $('blink').hidden = $('input').value.length > 0;
    /* Throttled hard: this is the `listening` state's only evidence, and it is
     * a timestamp — never the text. Nothing typed leaves the tab until Enter. */
    const now = Date.now();
    if (now - typingSent > 1200) { typingSent = now; post('/api/typing').catch(() => {}); }
  });
  window.addEventListener('scroll', hideProbe, true);

  stream();
  refresh(true);
  setInterval(() => { if (!app.busy) refresh(false); }, 4000);
}

document.addEventListener('DOMContentLoaded', boot);
