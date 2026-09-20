/* hl-traf web UI.
 *
 * Series sharing a base unit share a Y axis; a graph mixing units grows a
 * second, third or fourth axis, each in its own hue, and the lines take
 * that hue so you can tell at a glance which axis a line is read against.
 * Four axes is the cap -- past that nothing is legible.
 *
 * Databases are chosen per client and sent with every query, so browsing
 * an archive here never disturbs anyone else's live view and never stops
 * the recording.
 */
'use strict';

const MAX_AXES = 4;

/* One hue per unit. Members of a unit get shades of it, so "Temps / HBM"
 * lands as six related lines rather than six unrelated colours. */
const UNIT_HUE = {
  'C': 18, 'W': 45, 'V': 140, 'A': 185, '%': 210, 'B/s': 275,
  'MHz': 320, 'GT/s': 300, 'tok/s': 165, 's': 340, 'count': 0,
  'B': 255, 'mJ': 60,
};
const UNIT_SAT = { 'count': 0 };

function shade(unit, i, n) {
  const h = UNIT_HUE[unit] ?? 200;
  const s = UNIT_SAT[unit] ?? 70;
  const l = n <= 1 ? 62 : 44 + (i / Math.max(1, n - 1)) * 34;
  return `hsl(${h} ${s}% ${l.toFixed(0)}%)`;
}
function axisColor(unit) {
  const h = UNIT_HUE[unit] ?? 200;
  return `hsl(${h} ${UNIT_SAT[unit] ?? 70}% 62%)`;
}

const fmt = (v, unit) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  if (unit === 'B' || unit === 'B/s') {
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0, x = Math.abs(v);
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return (v < 0 ? '-' : '') + x.toFixed(x < 10 ? 2 : 1) + ' ' + u[i] +
           (unit === 'B/s' ? '/s' : '');
  }
  const a = Math.abs(v);
  const d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 4;
  return v.toFixed(d) + (unit && unit !== 'count' ? ' ' + unit : '');
};
const fmtBytes = (b) => fmt(b, 'B');
const tsLocal = (t) => new Date(t * 1000).toLocaleString();

/* ------------------------------------------------------------------ */
const api = {
  async get(p) {
    const r = await fetch(p);
    if (!r.ok) throw new Error(`${p}: ${r.status}`);
    return r.json();
  },
  async send(p, body, method = 'POST') {
    const r = await fetch(p, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(data.message || data.error || r.status); e.data = data; e.status = r.status; throw e; }
    return data;
  },
};

/* The Save button is only blue when there is actually something to
 * save -- a permanently blue button tells you nothing. */
function setDirty(v) {
  S.dirty = v;
  const b = document.getElementById('save');
  if (b) {
    b.classList.toggle('primary', !!v);
    b.title = v ? 'Unsaved changes to this profile'
                : 'No changes since the profile was loaded';
  }
}

function toast(msg, isErr) {
  const d = document.createElement('div');
  d.className = 't' + (isErr ? ' err' : '');
  d.textContent = msg;
  document.getElementById('toast').appendChild(d);
  setTimeout(() => d.remove(), isErr ? 9000 : 4000);
}

/* ------------------------------------------------------------------ */
const S = {
  catalog: {}, groups: [], statics: {}, replay: false,
  dbs: [], liveDb: null,
  profile: null, profileName: null, profileVersion: null,
  config: { graphs: [], range: { mode: 'last', seconds: 1800 }, sources: null },
  charts: [],            // one entry per graph
  lastValues: {},
  clientId: 'web-' + Math.random().toString(36).slice(2, 10),
  dirty: false,
  frozen: false,
};

const QUICK = [
  ['5m', 300], ['30m', 1800], ['1h', 3600], ['3h', 10800],
  ['8h', 28800], ['24h', 86400], ['7d', 604800],
];

/* ---------------- sources: [{dbs:[path], shift, label}] ------------- */
function defaultSources() {
  return [{ dbs: S.liveDb ? [S.liveDb] : [], shift: 0, label: 'A' }];
}
function sources() {
  if (!S.config.sources || !S.config.sources.length) S.config.sources = defaultSources();
  return S.config.sources;
}

function renderSources() {
  const host = document.getElementById('sources');
  host.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'src';
  head.innerHTML = `<span class="srcname"><b>data</b></span>`;
  const add = document.createElement('button');
  add.textContent = '+ overlay source';
  add.onclick = () => {
    const n = String.fromCharCode(65 + sources().length);
    sources().push({ dbs: S.liveDb ? [S.liveDb] : [], shift: 0, label: n });
    setDirty(true); renderSources(); refreshAll();
  };
  head.appendChild(add);
  host.appendChild(head);

  sources().forEach((src, idx) => {
    const row = document.createElement('div');
    row.className = 'src';

    const name = document.createElement('span');
    name.className = 'srcname';
    name.textContent = 'source ' + (src.label || String.fromCharCode(65 + idx));
    row.appendChild(name);

    /* Multi-select = stitching: several archives rendered as one
     * timeline, oldest first, with gaps left as gaps. */
    const sel = document.createElement('select');
    sel.className = 'dbs'; sel.multiple = true;
    sel.size = Math.min(4, Math.max(2, S.dbs.length));
    S.dbs.forEach(db => {
      const o = document.createElement('option');
      o.value = db.path;
      const span = db.start ? `${tsLocal(db.start)} → ${tsLocal(db.end)}` : 'empty';
      o.textContent = `${db.live ? '● live' : '  '} ${db.name} · ${span} · ${fmtBytes(db.bytes)}`;
      o.selected = src.dbs.includes(db.path);
      if (db.error) { o.textContent += ' · UNREADABLE'; o.disabled = true; }
      sel.appendChild(o);
    });
    sel.onchange = () => {
      const picked = [...sel.selectedOptions].map(o => o.value);
      const chosen = S.dbs.filter(d => picked.includes(d.path));
      const overlap = findOverlap(chosen);
      if (overlap) {
        toast(`${overlap[0]} and ${overlap[1]} cover the same time; ` +
              `stitching them would double-plot.`, true);
        [...sel.options].forEach(o => o.selected = src.dbs.includes(o.value));
        return;
      }
      src.dbs = chosen.sort((a, b) => (a.start || 0) - (b.start || 0)).map(d => d.path);
      setDirty(true); refreshAll();
    };
    row.appendChild(sel);

    const sh = document.createElement('div');
    sh.className = 'shift';
    sh.innerHTML = '<span class="note">shift</span>';
    [['-1w', -604800], ['-1d', -86400], ['-1h', -3600],
     ['+1h', 3600], ['+1d', 86400], ['+1w', 604800]].forEach(([lbl, dt]) => {
      const b = document.createElement('button');
      b.textContent = lbl;
      b.onclick = () => { src.shift = (src.shift || 0) + dt; setDirty(true); renderSources(); refreshAll(); };
      sh.appendChild(b);
    });
    const cur = document.createElement('span');
    cur.className = 'note';
    cur.textContent = src.shift ? ` ${(src.shift / 3600).toFixed(2)}h` : ' none';
    sh.appendChild(cur);
    const z = document.createElement('button');
    z.textContent = 'reset'; z.onclick = () => { src.shift = 0; setDirty(true); renderSources(); refreshAll(); };
    sh.appendChild(z);
    row.appendChild(sh);

    if (idx > 0) {
      const rm = document.createElement('button');
      rm.className = 'danger'; rm.textContent = '✕';
      rm.onclick = () => { sources().splice(idx, 1); setDirty(true); renderSources(); refreshAll(); };
      row.appendChild(rm);
    }
    host.appendChild(row);
  });

  const onlyLive = sources().every(s => s.dbs.length === 1 && s.dbs[0] === S.liveDb);
  document.getElementById('liveTail').disabled = !onlyLive || S.replay;
  if (!onlyLive || S.replay) document.getElementById('liveTail').checked = false;
}

function findOverlap(chosen) {
  const withSpan = chosen.filter(d => d.start && d.end)
                         .sort((a, b) => a.start - b.start);
  for (let i = 1; i < withSpan.length; i++) {
    if (withSpan[i].start < withSpan[i - 1].end) {
      return [withSpan[i - 1].name, withSpan[i].name];
    }
  }
  return null;
}

/* ---------------- time range ---------------- */
function rangeWindow() {
  const r = S.config.range || { mode: 'last', seconds: 1800 };
  const now = Date.now() / 1000;
  if (r.mode === 'last') return [now - r.seconds, now];
  return [r.t0, r.t1];
}

function renderQuick() {
  const host = document.getElementById('quick');
  host.innerHTML = '';
  QUICK.forEach(([lbl, secs]) => {
    const b = document.createElement('button');
    b.textContent = 'last ' + lbl;
    const r = S.config.range;
    if (r.mode === 'last' && r.seconds === secs) b.classList.add('on');
    b.onclick = () => {
      S.config.range = { mode: 'last', seconds: secs };
      setDirty(true); renderQuick(); refreshAll();
    };
    host.appendChild(b);
  });
}

/* ---------------- graph rendering ---------------- */
function unitsOf(keys) {
  const seen = [];
  keys.forEach(k => {
    const u = S.catalog[k]?.unit ?? 'count';
    if (!seen.includes(u)) seen.push(u);
  });
  return seen;
}


/* Shared cursor group: every plot joins it, so crosshairs line up across
 * the whole page. */
const CURSOR_SYNC = uPlot.sync('hltraf');

/* ---- drag readout ------------------------------------------------- */
function showDragTip(entry, u) {
  let tip = entry.dragTip;
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'dragtip';
    entry.plotEl.appendChild(tip);
    entry.dragTip = tip;
  }
  const a = u.posToVal(u.select.left, 'x');
  const b = u.posToVal(u.select.left + u.select.width, 'x');
  const span = Math.max(0, b - a);
  tip.textContent = `${tsShort(a)} → ${tsShort(b)}   (${durStr(span)})`;
  tip.style.display = 'block';
  tip.style.left = Math.max(2, u.select.left) + 'px';
  tip.style.top = '2px';
}
function hideDragTip(entry) {
  if (entry.dragTip) entry.dragTip.style.display = 'none';
}
const tsShort = (t) => new Date(t * 1000).toLocaleTimeString();
function durStr(s) {
  if (s < 90) return s.toFixed(1) + 's';
  if (s < 5400) return (s / 60).toFixed(1) + 'm';
  if (s < 172800) return (s / 3600).toFixed(2) + 'h';
  return (s / 86400).toFixed(2) + 'd';
}

/* ---- ctrl-hold freeze --------------------------------------------- */
/* Reading a value off a moving graph is a losing game: the point under
 * the pointer is replaced before you can read it. Holding ctrl pins
 * every graph so the value stays put; releasing resumes and jumps to
 * wherever the data has got to. */
function setFrozen(on) {
  if (S.frozen === on) return;
  S.frozen = on;
  S.charts.forEach(e => {
    e.legendEl?.classList.toggle('frozen', on);
    e.plotEl?.closest('.graph')?.classList.toggle('frozen', on);
    renderLegend(e, e.plot?.cursor?.idx ?? null);
  });
  if (!on) S.charts.forEach(e => loadGraph(e));
}

function wireInteractions(entry) {
  const over = entry.plot?.over;
  if (!over) return;
  over.addEventListener('mousedown', (ev) => {
    if (ev.ctrlKey || ev.metaKey) {
      // Capture phase + stopPropagation so uPlot never sees this and
      // cannot start a rubber-band selection underneath the freeze.
      ev.preventDefault();
      ev.stopPropagation();
      setFrozen(true);
    } else {
      entry.dragging = true;
    }
  }, true);
  over.addEventListener('mouseleave', () => hideDragTip(entry));
  /* ctrl+click raises the context menu on some platforms; suppress it so
   * the freeze gesture is not interrupted. */
  over.addEventListener('contextmenu', (ev) => {
    if (ev.ctrlKey || ev.metaKey) ev.preventDefault();
  });
}

window.addEventListener('mouseup', () => {
  S.charts.forEach(e => { e.dragging = false; hideDragTip(e); });
  if (S.frozen) setFrozen(false);
});
/* Releasing ctrl without releasing the button should also resume. */
window.addEventListener('keyup', (ev) => {
  if (S.frozen && (ev.key === 'Control' || ev.key === 'Meta')) setFrozen(false);
});
window.addEventListener('blur', () => { if (S.frozen) setFrozen(false); });

const AXIS_SIZE = 58;            // axis `size` below
const AXIS_LABEL = 16;           // axis `labelSize` below
const AXIS_W = AXIS_SIZE + AXIS_LABEL;   // total width an axis occupies

/* How many Y axes a graph puts on each side. Units alternate left/right
 * in the order they first appear. */
function gutterOf(keys) {
  const n = Math.min(unitsOf(keys).length, MAX_AXES);
  return { left: Math.ceil(n / 2), right: Math.floor(n / 2) };
}

/* The widest layout in the current profile; every graph pads out to it. */
function profileGutter() {
  let left = 1, right = 0;
  (S.config.graphs || []).forEach(g => {
    const u = gutterOf((g.series || []).filter(k => S.catalog[k]));
    left = Math.max(left, u.left);
    right = Math.max(right, u.right);
  });
  return { left, right };
}

function buildChart(entry) {
  const g = entry.cfg;
  const keys = g.series.filter(k => S.catalog[k]);
  const units = unitsOf(keys).slice(0, MAX_AXES);
  const srcs = sources();

  /* One uPlot series per (source, key): overlaying two sources puts the
   * same sensor on the graph twice, distinguished by the source label. */
  const uSeries = [{}];
  const meta = [];
  units.forEach(u => {
    const inUnit = keys.filter(k => (S.catalog[k]?.unit ?? 'count') === u);
    inUnit.forEach((k, i) => {
      srcs.forEach((src, si) => {
        const c = shade(u, i, inUnit.length);
        uSeries.push({
          label: (srcs.length > 1 ? `${src.label}· ` : '') + (S.catalog[k]?.label ?? k),
          stroke: c, width: si === 0 ? 1.6 : 1.2,
          dash: si === 0 ? undefined : [5, 3],
          scale: u, spanGaps: false, points: { show: false },
        });
        meta.push({ key: k, unit: u, color: c, srcIdx: si, srcLabel: src.label });
      });
    });
  });

  const axes = [{
    stroke: '#8b93a5', grid: { stroke: '#2c313d', width: 1 },
    ticks: { stroke: '#2c313d' },
  }];
  units.forEach((u, i) => {
    axes.push({
      scale: u, side: i % 2 === 0 ? 3 : 1, stroke: axisColor(u),
      grid: { show: i === 0, stroke: '#2c313d', width: 1 },
      ticks: { stroke: '#2c313d' },
      size: AXIS_SIZE, label: u, labelSize: AXIS_LABEL,
      values: (self, ticks) => ticks.map(v => fmt(v, u).replace(' ' + u, '')),
    });
  });

  const maxGutter = profileGutter();
  const mine = gutterOf(keys);

  const el = entry.plotEl;
  // destroy() unsubscribes from the cursor sync group and removes its
  // listeners; clearing innerHTML alone leaks the old plot into
  // CURSOR_SYNC.plots, which then drives updates into a dead chart.
  if (entry.plot) {
    try { entry.plot.destroy(); } catch (e) { /* already gone */ }
    entry.plot = null;
  }
  entry.dragTip = null;
  el.innerHTML = '';
  const opts = {
    width: el.clientWidth || 800,
    height: g.height || 220,
    series: uSeries,
    axes,
    // Every graph shows exactly the requested window, so the same
    // instant is the same pixel in all of them and the synced crosshairs
    // actually align.  Without this uPlot auto-fits x per plot.
    scales: { x: { time: true, range: () => rangeWindow() } },
    // Pad out to the profile's widest axis layout so all plotting areas
    // are the same width; otherwise a two-axis graph shifts every point
    // relative to a one-axis graph.
    padding: [8, (maxGutter.right - mine.right) * AXIS_W + 8,
              0, (maxGutter.left - mine.left) * AXIS_W],
    legend: { show: false },
    cursor: {
      drag: { x: true, y: false },
      /* One sync group for every graph, so the crosshair in graph A
       * marks the same instant in B and C -- which is the whole point of
       * having them stacked. */
      sync: { key: CURSOR_SYNC.key, setSeries: false, scales: ['x', null] },
    },
    hooks: {
      setSelect: [(u) => {
        hideDragTip(entry);
        if (S.frozen) { u.setSelect({ width: 0, height: 0 }, false); return; }
        if (u.select.width > 8) {
          const t0 = u.posToVal(u.select.left, 'x');
          const t1 = u.posToVal(u.select.left + u.select.width, 'x');
          S.config.range = { mode: 'abs', t0, t1 };
          document.getElementById('liveTail').checked = false;
          setDirty(true); renderQuick(); syncRangeInputs(); refreshAll();
        }
      }],
      setCursor: [(u) => {
        renderLegend(entry, u.cursor.idx);
        if (u.select && u.select.width > 2 && entry.dragging) {
          showDragTip(entry, u);
        }
      }],
    },
  };
  entry.meta = meta;
  entry.units = units;
  entry.plot = new uPlot(opts, [[]], el);
  wireInteractions(entry);
  renderLegend(entry, null);
}

function renderLegend(entry, idx) {
  const host = entry.legendEl;
  host.innerHTML = '';
  entry.meta.forEach((m, i) => {
    const item = document.createElement('div');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'sw'; sw.style.background = m.color;
    if (m.srcIdx > 0) sw.style.background =
      `repeating-linear-gradient(90deg, ${m.color} 0 4px, transparent 4px 7px)`;
    item.appendChild(sw);

    const lbl = document.createElement('span');
    const s = S.catalog[m.key];
    lbl.textContent = (entry.meta.some(o => o.srcIdx > 0) ? m.srcLabel + '· ' : '') +
                      (s?.label ?? m.key);
    if (s?.note) lbl.title = s.note;
    item.appendChild(lbl);

    const val = document.createElement('span');
    val.className = 'val';
    const data = entry.plot?.data?.[i + 1];
    let v = null;
    if (data && data.length) {
      v = idx != null && idx < data.length ? data[idx] : data[data.length - 1];
    }
    val.textContent = fmt(v, m.unit);
    item.appendChild(val);

    /* No separate axis tag: fmt() already prints the unit, and showing
     * it twice read as "193.1 W W". The swatch colour is what ties a
     * line to its axis. */

    if (m.srcIdx === 0) {
      const x = document.createElement('span');
      x.className = 'x'; x.textContent = '✕'; x.title = 'remove from graph';
      x.onclick = () => {
        entry.cfg.series = entry.cfg.series.filter(k => k !== m.key);
        setDirty(true); rebuildGraph(entry);
      };
      item.appendChild(x);
    }
    host.appendChild(item);
  });
  if (!entry.meta.length) {
    host.innerHTML = '<span class="note">no series — use ADD or ADD GROUP</span>';
  }
  if (S.frozen) {
    const tag = document.createElement('span');
    tag.className = 'frozen-tag';
    tag.textContent = 'HELD — release ctrl to resume';
    host.appendChild(tag);
  }
}

async function loadGraph(entry) {
  if (S.frozen) return;
  const g = entry.cfg;
  const keys = g.series.filter(k => S.catalog[k]);
  if (!keys.length) { entry.plot?.setData([[]]); renderLegend(entry, null); return; }
  const [t0, t1] = rangeWindow();
  const srcs = sources().filter(s => s.dbs.length);
  if (!srcs.length) return;

  let res;
  try {
    res = await api.send('/api/query', {
      keys, t0, t1, max_points: Math.max(400, entry.plotEl.clientWidth || 800),
      sources: srcs,
    });
  } catch (e) { toast('query failed: ' + e.message, true); return; }

  /* Merge every (source, key) onto one shared x axis. */
  const xs = new Set();
  res.sources.forEach(sr => Object.values(sr.data).forEach(
    pts => pts.forEach(p => xs.add(p[0]))));
  const x = [...xs].sort((a, b) => a - b);
  const index = new Map(x.map((t, i) => [t, i]));
  const cols = [x];
  entry.meta.forEach(m => {
    const col = new Array(x.length).fill(null);
    const pts = res.sources[m.srcIdx]?.data?.[m.key] || [];
    pts.forEach(([t, v]) => {
      const i = index.get(t);
      if (i !== undefined) col[i] = v;   // null stays null: a gap, not a line
    });
    cols.push(col);
  });
  entry.plot?.setData(cols);
  renderLegend(entry, null);
}

function rebuildGraph(entry) {
  buildChart(entry);
  loadGraph(entry);
  pushSubscription();
}

function renderGraphs() {
  const host = document.getElementById('graphs');
  // Tear the previous set down first: their intervals would otherwise
  // keep firing against detached elements for the life of the page.
  S.charts.forEach(e => {
    clearInterval(e.timer);
    if (e.plot) {
      try { e.plot.destroy(); } catch (err) { /* already gone */ }
      e.plot = null;
    }
  });
  host.innerHTML = '';
  S.charts = [];
  if (!S.config.graphs.length) {
    host.innerHTML = '<div class="empty">No graphs in this profile. ' +
                     'Use <b>+ Graph</b> above.</div>';
    return;
  }
  S.config.graphs.forEach((g, gi) => {
    if (!g.series) g.series = [];
    const box = document.createElement('div');
    box.className = 'graph';

    const head = document.createElement('div');
    head.className = 'head';
    const t = document.createElement('input');
    t.className = 'title'; t.value = g.title || 'Graph ' + (gi + 1);
    t.onchange = () => { g.title = t.value; setDirty(true); };
    head.appendChild(t);

    const mkBtn = (label, fn, cls) => {
      const b = document.createElement('button');
      b.textContent = label; b.onclick = fn;
      if (cls) b.className = cls;
      head.appendChild(b); return b;
    };
    const entry = { cfg: g };
    mkBtn('ADD', () => openAdd(entry));
    mkBtn('ADD GROUP', () => openAddGroup(entry));

    const spd = document.createElement('select');
    [[500, '0.5s'], [1000, '1s'], [2000, '2s'], [5000, '5s'],
     [15000, '15s'], [60000, '1m']].forEach(([ms, lbl]) => {
      const o = document.createElement('option');
      o.value = ms; o.textContent = 'redraw ' + lbl;
      o.selected = (g.update_ms || 1000) === ms;
      spd.appendChild(o);
    });
    spd.title = 'How often this graph redraws. Sample rate is set in Settings.';
    spd.onchange = () => { g.update_ms = +spd.value; setDirty(true); restartTimer(entry); };
    head.appendChild(spd);

    mkBtn('Delete graph', async () => {
      if (!confirm(`Delete the graph "${g.title || gi + 1}" from this profile?\n\n` +
                   `This removes the graph's settings only. No recorded ` +
                   `sample data is deleted.`)) return;
      if (!confirm('Are you sure? This cannot be undone without re-adding the ' +
                   'series by hand.')) return;
      S.config.graphs.splice(gi, 1);
      setDirty(true); renderGraphs(); pushSubscription();
    }, 'danger');

    box.appendChild(head);
    const body = document.createElement('div');
    body.className = 'body';
    box.appendChild(body);
    const legend = document.createElement('div');
    legend.className = 'legend';
    box.appendChild(legend);
    host.appendChild(box);

    entry.plotEl = body;
    entry.legendEl = legend;
    S.charts.push(entry);
    buildChart(entry);
    loadGraph(entry);
    restartTimer(entry);
  });
  pushSubscription();
}

function restartTimer(entry) {
  clearInterval(entry.timer);
  const ms = entry.cfg.update_ms || 1000;
  entry.timer = setInterval(() => {
    if (S.frozen) return;          // ctrl is held: leave the graph alone
    const live = document.getElementById('liveTail').checked;
    if (live || S.config.range.mode === 'last') loadGraph(entry);
  }, ms);
}

function refreshAll() {
  syncRangeInputs();
  S.charts.forEach(e => { buildChart(e); loadGraph(e); });
  pushSubscription();
}

/* Tell the engine what we are looking at, so unwatched sensors cost
 * nothing.  The union across all clients is what gets polled. */
async function pushSubscription() {
  const keys = new Set();
  S.config.graphs.forEach(g => (g.series || []).forEach(k => keys.add(k)));
  try {
    await api.send('/api/subscribe', { client: S.clientId, keys: [...keys] });
  } catch (e) { /* replay mode has no engine */ }
}

/* ---------------- ADD / ADD GROUP ---------------- */
function modal(html) {
  const sheet = document.getElementById('sheet');
  sheet.innerHTML = html;
  document.getElementById('modal').classList.add('on');
  return sheet;
}
function closeModal() { document.getElementById('modal').classList.remove('on'); }
document.getElementById('modal').onclick = (e) => {
  if (e.target.id === 'modal') closeModal();
};

function groupsWithSeries() {
  return S.groups.filter(g => g.keys.length);
}

function openAdd(entry) {
  const sheet = modal(`
    <h3>Add series to "${entry.cfg.title || 'graph'}"</h3>
    <div class="content">
      <input id="flt" placeholder="filter…" style="width:100%;margin-bottom:8px">
      <div id="list"></div>
    </div>
    <div class="foot"><span class="count" id="cnt"></span>
      <button id="cancel">Cancel</button>
      <button id="ok" class="primary">ADD</button></div>`);
  const chosen = new Set();
  const list = sheet.querySelector('#list');

  function draw(filter) {
    list.innerHTML = '';
    const f = filter.toLowerCase();
    groupsWithSeries().forEach(g => {
      const hits = g.keys.filter(k => {
        const s = S.catalog[k];
        return !f || (s.label + ' ' + k + ' ' + g.label).toLowerCase().includes(f);
      });
      if (!hits.length) return;
      const d = document.createElement('details');
      d.className = 'grp'; d.open = !!f;
      d.innerHTML = `<summary>${g.label} <span class="unit">· ${hits.length}` +
                    `${g.unit ? ' · ' + g.unit : ' · mixed units'}</span></summary>`;
      const items = document.createElement('div');
      items.className = 'items';
      hits.forEach(k => {
        const s = S.catalog[k];
        const lab = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = chosen.has(k) || entry.cfg.series.includes(k);
        cb.disabled = entry.cfg.series.includes(k);
        cb.onchange = () => {
          cb.checked ? chosen.add(k) : chosen.delete(k);
          sheet.querySelector('#cnt').textContent = `${chosen.size} selected`;
        };
        lab.appendChild(cb);
        const txt = document.createElement('span');
        txt.textContent = s.label + (s.note ? ' *' : '');
        txt.title = k + (s.note ? '\n' + s.note : '');
        lab.appendChild(txt);
        items.appendChild(lab);
      });
      d.appendChild(items);
      list.appendChild(d);
    });
  }
  draw('');
  sheet.querySelector('#flt').oninput = (e) => draw(e.target.value);
  sheet.querySelector('#cancel').onclick = closeModal;
  sheet.querySelector('#ok').onclick = () => {
    applyAdd(entry, [...chosen]);
    closeModal();
  };
}

/* ADD GROUP: pick a group, everything in it arrives pre-selected, you
 * unselect what you don't want, then ADD or Cancel. */
function openAddGroup(entry) {
  const sheet = modal(`
    <h3>Add a group to "${entry.cfg.title || 'graph'}"</h3>
    <div class="content" id="content"></div>
    <div class="foot"><span class="count" id="cnt"></span>
      <button id="cancel">Cancel</button>
      <button id="ok" class="primary" disabled>ADD</button></div>`);
  const content = sheet.querySelector('#content');
  const ok = sheet.querySelector('#ok');
  const cnt = sheet.querySelector('#cnt');

  groupsWithSeries().forEach(g => {
    const b = document.createElement('button');
    b.style.margin = '3px';
    b.textContent = `${g.label} (${g.keys.length}${g.unit ? ', ' + g.unit : ''})`;
    b.onclick = () => pick(g);
    content.appendChild(b);
  });

  function pick(g) {
    const chosen = new Set(g.keys.filter(k => !entry.cfg.series.includes(k)));
    content.innerHTML = '';
    const h = document.createElement('div');
    h.innerHTML = `<b>${g.label}</b> <span class="note">` +
      `${g.unit ? 'all ' + g.unit + ' — one shared axis' : 'mixed units'}` +
      `</span>`;
    content.appendChild(h);

    const btns = document.createElement('div');
    btns.className = 'rowbtns';
    const all = document.createElement('button'); all.textContent = 'select all';
    const none = document.createElement('button'); none.textContent = 'select none';
    const back = document.createElement('button'); back.textContent = '← groups';
    btns.append(all, none, back);
    content.appendChild(btns);

    const items = document.createElement('div');
    items.className = 'items';
    items.style.display = 'grid';
    items.style.gridTemplateColumns = 'repeat(auto-fill,minmax(250px,1fr))';
    content.appendChild(items);

    function draw() {
      items.innerHTML = '';
      g.keys.forEach(k => {
        const s = S.catalog[k];
        const already = entry.cfg.series.includes(k);
        const lab = document.createElement('label');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = chosen.has(k); cb.disabled = already;
        cb.onchange = () => { cb.checked ? chosen.add(k) : chosen.delete(k); upd(); };
        lab.appendChild(cb);
        const t = document.createElement('span');
        t.textContent = s.label + (already ? ' (already on graph)' : '');
        t.title = k + (s.note ? '\n' + s.note : '');
        if (s.note) t.style.color = 'var(--dim)';
        lab.appendChild(t);
        items.appendChild(lab);
      });
      upd();
    }
    function upd() {
      cnt.textContent = `${chosen.size} of ${g.keys.length} selected`;
      ok.disabled = chosen.size === 0;
    }
    all.onclick = () => { g.keys.forEach(k => { if (!entry.cfg.series.includes(k)) chosen.add(k); }); draw(); };
    none.onclick = () => { chosen.clear(); draw(); };
    back.onclick = () => openAddGroup(entry);
    ok.onclick = () => { applyAdd(entry, [...chosen]); closeModal(); };
    draw();
  }
}

function applyAdd(entry, keys) {
  if (!keys.length) return;
  const merged = [...entry.cfg.series, ...keys.filter(k => !entry.cfg.series.includes(k))];
  const units = unitsOf(merged);
  if (units.length > MAX_AXES) {
    toast(`That would need ${units.length} Y axes (${units.join(', ')}). ` +
          `The limit is ${MAX_AXES} — past that nothing is readable. ` +
          `Put the extra units on another graph.`, true);
    return;
  }
  entry.cfg.series = merged;
  setDirty(true);
  rebuildGraph(entry);
}

/* ---------------- settings ---------------- */
async function openSettings() {
  let cur;
  try { cur = await api.get('/api/settings'); }
  catch (e) { toast('cannot read settings: ' + e.message, true); return; }
  const s = cur.settings || {};
  const sheet = modal(`
    <h3>Engine settings</h3>
    <div class="content">
      <p class="note">These control what the <b>engine records</b>, for
        everyone. Each hwmon channel is a ~3.6&nbsp;ms firmware round trip and
        the driver serialises them, so recording everything at 1&nbsp;Hz would
        consume roughly a third of the firmware mailbox continuously.
        A graph's own redraw rate is set on the graph.</p>
      <div class="setrow"><label>Recording interval (seconds)</label>
        <input id="ri" type="number" min="0.5" max="3600" step="0.5"
               value="${s.record_interval ?? 5}"></div>
      <div class="setrow"><label>Record exotic rails</label>
        <input id="ex" type="checkbox" ${s.record_exotic ? 'checked' : ''}>
        <span class="note">the ~817&nbsp;mV core rails, housekeeping rails and
          firmware peak values. Off by default; they stay graphable live
          either way.</span></div>
      <div class="setrow"><label>Record NIC rates</label>
        <input id="nr" type="checkbox" ${s.record_nic_rates ? 'checked' : ''}>
        <span class="note">per-port rx/tx</span></div>
      <div class="setrow"><label>Record NIC error counters</label>
        <input id="ne" type="checkbox" ${s.record_nic_errors ? 'checked' : ''}></div>
      <div class="setrow"><label>Stop recording below (GiB free)</label>
        <input id="mf" type="number" min="0.1" step="0.1"
               value="${((s.min_free_bytes ?? 2147483648) / 1073741824).toFixed(1)}"></div>
      <p class="note">Archives are never deleted automatically. Rotate with
        <code>hl-traf serve-reset</code>; delete old files from the command
        line.</p>
    </div>
    <div class="foot"><span class="count"></span>
      <button id="cancel">Cancel</button>
      <button id="ok" class="primary">Save settings</button></div>`);
  sheet.querySelector('#cancel').onclick = closeModal;
  sheet.querySelector('#ok').onclick = async () => {
    try {
      const r = await api.send('/api/settings', {
        record_interval: +sheet.querySelector('#ri').value,
        record_exotic: sheet.querySelector('#ex').checked,
        record_nic_rates: sheet.querySelector('#nr').checked,
        record_nic_errors: sheet.querySelector('#ne').checked,
        min_free_bytes: Math.round(+sheet.querySelector('#mf').value * 1073741824),
      });
      toast(`settings saved — recording ${r.recorded_series} series`);
      closeModal();
    } catch (e) { toast('save failed: ' + e.message, true); }
  };
}

/* ---------------- profiles ---------------- */
async function loadProfiles(select) {
  const list = await api.get('/api/profiles');
  const sel = document.getElementById('profile');
  sel.innerHTML = '';
  list.forEach(p => {
    const o = document.createElement('option');
    o.value = p.name; o.textContent = p.name;
    sel.appendChild(o);
  });
  const want = select || S.profileName || (list[0] && list[0].name);
  if (want) { sel.value = want; await openProfile(want); }
}

async function openProfile(name) {
  const p = await api.get('/api/profiles/' + encodeURIComponent(name));
  S.profileName = p.name;
  S.profileVersion = p.version;
  S.config = Object.assign({ graphs: [], range: { mode: 'last', seconds: 1800 },
                             sources: null }, p.config || {});
  if (!S.config.sources) S.config.sources = defaultSources();
  S.config.sources.forEach(s => {
    s.dbs = (s.dbs || []).filter(path => S.dbs.some(d => d.path === path));
    if (!s.dbs.length && S.liveDb) s.dbs = [S.liveDb];
  });
  setDirty(false);
  renderQuick(); syncRangeInputs(); renderSources(); renderGraphs();
}

async function saveProfile(asNew) {
  let name = S.profileName;
  if (asNew) {
    name = prompt('Save this layout as a new profile named:', '');
    if (!name) return;
  }
  try {
    const r = await api.send('/api/profiles/' + encodeURIComponent(name), {
      version: asNew ? null : S.profileVersion,
      config: S.config,
      who: S.clientId,
    });
    S.profileName = r.name; S.profileVersion = r.version; setDirty(false);
    toast(`saved "${r.name}" (v${r.version})`);
    await loadProfiles(r.name);
  } catch (e) {
    if (e.status === 409) {
      /* Someone else saved while this tab was editing. */
      toast(`${e.data.name} was updated by someone else — can't change. ` +
            `Reload it and try again.`, true);
    } else if (e.status === 400 || /exists/i.test(e.message)) {
      toast('save failed: ' + e.message, true);
    } else {
      toast('save failed: ' + e.message, true);
    }
  }
}

/* ---------------- status ---------------- */
async function pollStatus() {
  let st;
  try { st = await api.get('/api/status'); }
  catch { document.getElementById('status').innerHTML =
    '<span class="pill stopped">SERVER UNREACHABLE</span>'; return; }

  const host = document.getElementById('status');
  const bits = [];
  if (st.replay) {
    bits.push('<span class="pill replay">REPLAY — no live data</span>');
  }
  const e = st.engine;
  if (e) {
    const r = e.recorder;
    bits.push(r.recording
      ? `<span class="pill rec">● recording</span>`
      : `<span class="pill stopped">RECORDING STOPPED — ${r.reason || 'ERROR'}</span>`);
    bits.push(`<span class="pill">${e.sweep_ms} ms/sweep</span>`);
    bits.push(`<span class="pill">${e.active_series} read / ` +
              `${e.recorded_series} recorded / ${e.catalog_series} known</span>`);
    bits.push(`<span class="pill">${fmtBytes(r.free_bytes)} free</span>`);
    const vl = Object.entries(e.vllm || {});
    if (vl.length) {
      bits.push(vl.map(([k, up]) =>
        `<span class="pill" style="color:${up ? 'var(--good)' : 'var(--bad)'}">` +
        `vLLM ${up ? 'up' : 'down'}</span>`).join(''));
    }
  }
  if (S.dirty) bits.push('<span class="pill" style="color:var(--warn)">unsaved changes</span>');
  host.innerHTML = bits.join('');
}

/* ---------------- range inputs ---------------- */
function syncRangeInputs() {
  const [t0, t1] = rangeWindow();
  const iso = t => new Date((t - new Date().getTimezoneOffset() * 60) * 1000)
    .toISOString().slice(0, 19);
  document.getElementById('from').value = iso(t0);
  document.getElementById('to').value = iso(t1);
}

/* ---------------- boot ---------------- */
async function boot() {
  const cat = await api.get('/api/catalog');
  S.replay = cat.replay;
  S.statics = cat.statics || {};
  S.groups = cat.groups;
  cat.series.forEach(s => { S.catalog[s.key] = s; });

  const st = await api.get('/api/status');
  S.liveDb = st.live_db;
  S.dbs = await api.get('/api/databases');
  if (!S.liveDb && S.dbs.length) S.liveDb = S.dbs[0].path;

  await loadProfiles();
  renderSources();

  document.getElementById('profile').onchange = async (e) => {
    if (S.dirty && !confirm('Discard unsaved changes to this profile?')) {
      e.target.value = S.profileName; return;
    }
    await openProfile(e.target.value);
  };
  document.getElementById('save').onclick = () => saveProfile(false);
  document.getElementById('saveAs').onclick = () => saveProfile(true);
  document.getElementById('delProfile').onclick = async () => {
    const n = S.profileName;
    if (!confirm(`Delete the profile "${n}"?\n\nThis removes its graph ` +
                 `settings only. No recorded sample data is deleted.`)) return;
    if (!confirm(`Are you sure you want to delete "${n}"?`)) return;
    await api.send('/api/profiles/' + encodeURIComponent(n), undefined, 'DELETE');
    S.profileName = null;
    await loadProfiles();
  };
  document.getElementById('addGraph').onclick = () => {
    S.config.graphs.push({ title: 'Graph ' + (S.config.graphs.length + 1),
                           series: [], update_ms: 1000 });
    setDirty(true); renderGraphs();
  };
  document.getElementById('settingsBtn').onclick = openSettings;
  document.getElementById('applyRange').onclick = () => {
    const a = document.getElementById('from').value;
    const b = document.getElementById('to').value;
    if (!a || !b) return;
    const t0 = new Date(a).getTime() / 1000, t1 = new Date(b).getTime() / 1000;
    if (t1 <= t0) { toast('"to" must be after "from"', true); return; }
    S.config.range = { mode: 'abs', t0, t1 };
    document.getElementById('liveTail').checked = false;
    setDirty(true); renderQuick(); refreshAll();
  };
  document.getElementById('liveTail').onchange = (e) => {
    if (e.target.checked && S.config.range.mode === 'abs') {
      S.config.range = { mode: 'last', seconds: 1800 };
      renderQuick(); refreshAll();
    }
  };

  /* Live push: samples keep "follow live" moving, and a profile saved by
   * someone else is announced so the second editor knows before they hit
   * save. */
  const es = new EventSource('/api/stream');
  es.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'sample') {
      S.lastValues = msg.values;
    } else if (msg.type === 'profile' && msg.name === S.profileName) {
      if (msg.by !== S.clientId) {
        S.profileVersion = msg.version;
        toast(`"${msg.name}" was just updated by someone else (now v${msg.version}).` +
              (S.dirty ? ' Your unsaved changes will be refused — reload first.' : ''),
              S.dirty);
        if (!S.dirty) openProfile(msg.name);
      }
    } else if (msg.type === 'profile_deleted' && msg.name === S.profileName) {
      toast(`"${msg.name}" was deleted by someone else.`, true);
      loadProfiles();
    } else if (msg.type === 'settings') {
      toast('engine settings were changed');
    }
  };

  setInterval(pollStatus, 2000);
  pollStatus();
  window.addEventListener('resize', () => {
    S.charts.forEach(e => e.plot?.setSize({
      width: e.plotEl.clientWidth, height: e.cfg.height || 220 }));
  });
  /* Browsers show their own wording ("Reload site? Changes you made may
   * not be saved") and ignore any custom string, but returnValue must be
   * set for the prompt to appear at all. */
  window.addEventListener('beforeunload', (e) => {
    if (!S.dirty) return;
    e.preventDefault();
    e.returnValue = 'You have unsaved changes to this profile.';
    return e.returnValue;
  });
}

window.__hltraf = S;   // for automated UI tests

boot().catch(e => {
  document.body.innerHTML =
    `<div class="empty">failed to start: ${e.message}</div>`;
});
