/* health-monitor web UI.
 *
 * Series are addressed by ref: "<monitor>/<gpu_id>/<key>".  Series sharing
 * a base unit share a Y axis; a graph mixing units grows a second, third
 * or fourth axis in its own hue.  Four is the cap.
 *
 * Everything comes through the web server, which holds the session and
 * proxies to the manager; this page never talks to a monitor directly.
 */
'use strict';

const MAX_AXES = 4;
const AXIS_SIZE = 58, AXIS_LABEL = 16, AXIS_W = AXIS_SIZE + AXIS_LABEL;
const UNIT_HUE = { 'C': 18, 'W': 45, 'V': 140, 'A': 185, '%': 210, 'B/s': 275,
  'MHz': 320, 'GT/s': 300, 'tok/s': 165, 's': 340, 'count': 0, 'B': 255, 'mJ': 60 };
const UNIT_SAT = { 'count': 0 };
function shade(unit, i, n) {
  const h = UNIT_HUE[unit] ?? 200, s = UNIT_SAT[unit] ?? 70;
  const l = n <= 1 ? 62 : 44 + (i / Math.max(1, n - 1)) * 34;
  return `hsl(${h} ${s}% ${l.toFixed(0)}%)`;
}
const axisColor = (u) => `hsl(${UNIT_HUE[u] ?? 200} ${UNIT_SAT[u] ?? 70}% 62%)`;

const fmt = (v, unit) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  if (unit === 'B' || unit === 'B/s') {
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0, x = Math.abs(v);
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
    return (v < 0 ? '-' : '') + x.toFixed(x < 10 ? 2 : 1) + ' ' + u[i] + (unit === 'B/s' ? '/s' : '');
  }
  const a = Math.abs(v), d = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 1 ? 2 : 4;
  return v.toFixed(d) + (unit && unit !== 'count' ? ' ' + unit : '');
};
const fmtBytes = (b) => fmt(b, 'B');
const tsLocal = (t) => new Date(t * 1000).toLocaleString();
const tsShort = (t) => new Date(t * 1000).toLocaleTimeString();
const ago = (t) => { const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? s.toFixed(0) + 's' : s < 5400 ? (s / 60).toFixed(0) + 'm' : (s / 3600).toFixed(1) + 'h'; };
function durStr(s) {
  if (s < 90) return s.toFixed(1) + 's'; if (s < 5400) return (s / 60).toFixed(1) + 'm';
  if (s < 172800) return (s / 3600).toFixed(2) + 'h'; return (s / 86400).toFixed(2) + 'd';
}

/* ------------------------------------------------------------------ */
const api = {
  async get(p) {
    const r = await fetch(p);
    if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
    if (!r.ok) throw new Error(`${p}: ${r.status}`);
    return r.json();
  },
  async send(p, body, method = 'POST') {
    const r = await fetch(p, { method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    if (r.status === 401 && !p.startsWith('/api/login')) { location.href = '/login'; throw new Error('login'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(data.message || data.error || r.status); e.data = data; e.status = r.status; throw e; }
    return data;
  },
};
function toast(msg, isErr) {
  const d = document.createElement('div');
  d.className = 't' + (isErr ? ' err' : ''); d.textContent = msg;
  document.getElementById('toast').appendChild(d);
  setTimeout(() => d.remove(), isErr ? 9000 : 4000);
}

/* ------------------------------------------------------------------ */
const S = {
  me: null, build: {}, catalog: {}, groups: [], statics: {}, monitors: [],
  dbs: [], liveDb: null,
  profileName: null, profileVersion: null,
  config: { graphs: [], range: { mode: 'last', seconds: 1800 }, sources: null },
  charts: [], lastValues: {}, dirty: false, frozen: false,
  clientId: 'web-' + Math.random().toString(36).slice(2, 10),
};
const QUICK = [['5m', 300], ['30m', 1800], ['1h', 3600], ['3h', 10800], ['8h', 28800], ['24h', 86400], ['7d', 604800]];

function setDirty(v) {
  S.dirty = v;
  const b = document.getElementById('save');
  if (b) { b.classList.toggle('primary', !!v); b.title = v ? 'Unsaved changes' : 'No changes since load'; }
}

/* ---- monitor/gpu naming helpers ----------------------------------- */
function monInfo(id) { return S.monitors.find(m => m.monitor === id) || { monitor: id, nickname: '', gpus: [] }; }
function monLabel(id) { const m = monInfo(id); return m.nickname ? `${m.nickname}` : id; }
function gpuLabel(monId, gpuId) {
  if (gpuId === 'host') return 'host';
  const g = (monInfo(monId).gpus || []).find(x => x.gpu_id === gpuId);
  return g ? g.name : gpuId;
}
function refParts(ref) { const i = ref.indexOf('/'), j = ref.indexOf('/', i + 1);
  return [ref.slice(0, i), ref.slice(i + 1, j), ref.slice(j + 1)]; }
function seriesLabel(ref, withMon = true) {
  const s = S.catalog[ref]; const [m, g] = refParts(ref);
  const base = s ? s.label : ref;
  return withMon ? `${monLabel(m)}/${gpuLabel(m, g)} · ${base}` : `${gpuLabel(m, g)} · ${base}`;
}

/* ---- sources ------------------------------------------------------ */
function defaultSources() { return [{ dbs: S.liveDb ? [S.liveDb] : [], shift: 0, label: 'A' }]; }
function sources() {
  if (!S.config.sources || !S.config.sources.length) S.config.sources = defaultSources();
  return S.config.sources;
}
function renderSources() {
  const host = document.getElementById('sources'); host.innerHTML = '';
  const head = document.createElement('div'); head.className = 'src';
  head.innerHTML = `<span class="srcname"><b>data</b></span>`;
  const add = document.createElement('button'); add.textContent = '+ overlay source';
  add.onclick = () => { sources().push({ dbs: S.liveDb ? [S.liveDb] : [], shift: 0,
    label: String.fromCharCode(65 + sources().length) }); setDirty(true); renderSources(); refreshAll(); };
  head.appendChild(add); host.appendChild(head);
  sources().forEach((src, idx) => {
    const row = document.createElement('div'); row.className = 'src';
    const name = document.createElement('span'); name.className = 'srcname';
    name.textContent = 'source ' + (src.label || String.fromCharCode(65 + idx)); row.appendChild(name);
    const sel = document.createElement('select'); sel.className = 'dbs'; sel.multiple = true;
    sel.size = Math.min(4, Math.max(2, S.dbs.length));
    S.dbs.forEach(db => {
      const o = document.createElement('option'); o.value = db.path;
      const span = db.start ? `${tsLocal(db.start)} → ${tsLocal(db.end)}` : 'empty';
      o.textContent = `${db.live ? '● live' : '  '} ${db.name} · ${span} · ${fmtBytes(db.bytes)}`;
      o.selected = src.dbs.includes(db.path);
      if (db.error) { o.textContent += ' · UNREADABLE'; o.disabled = true; }
      sel.appendChild(o);
    });
    sel.onchange = () => {
      const picked = [...sel.selectedOptions].map(o => o.value);
      const chosen = S.dbs.filter(d => picked.includes(d.path));
      const ov = findOverlap(chosen);
      if (ov) { toast(`${ov[0]} and ${ov[1]} overlap in time; stitching would double-plot.`, true);
        [...sel.options].forEach(o => o.selected = src.dbs.includes(o.value)); return; }
      src.dbs = chosen.sort((a, b) => (a.start || 0) - (b.start || 0)).map(d => d.path);
      setDirty(true); refreshAll();
    };
    row.appendChild(sel);
    const sh = document.createElement('div'); sh.className = 'shift'; sh.innerHTML = '<span class="note">shift</span>';
    [['-1w', -604800], ['-1d', -86400], ['-1h', -3600], ['+1h', 3600], ['+1d', 86400], ['+1w', 604800]].forEach(([l, dt]) => {
      const b = document.createElement('button'); b.textContent = l;
      b.onclick = () => { src.shift = (src.shift || 0) + dt; setDirty(true); renderSources(); refreshAll(); };
      sh.appendChild(b);
    });
    const cur = document.createElement('span'); cur.className = 'note';
    cur.textContent = src.shift ? ` ${(src.shift / 3600).toFixed(2)}h` : ' none'; sh.appendChild(cur);
    const z = document.createElement('button'); z.textContent = 'reset';
    z.onclick = () => { src.shift = 0; setDirty(true); renderSources(); refreshAll(); }; sh.appendChild(z);
    row.appendChild(sh);
    if (idx > 0) { const rm = document.createElement('button'); rm.className = 'danger'; rm.textContent = '✕';
      rm.onclick = () => { sources().splice(idx, 1); setDirty(true); renderSources(); refreshAll(); }; row.appendChild(rm); }
    host.appendChild(row);
  });
  const onlyLive = sources().every(s => s.dbs.length === 1 && s.dbs[0] === S.liveDb);
  document.getElementById('liveTail').disabled = !onlyLive;
  if (!onlyLive) document.getElementById('liveTail').checked = false;
}
function findOverlap(chosen) {
  const w = chosen.filter(d => d.start && d.end).sort((a, b) => a.start - b.start);
  for (let i = 1; i < w.length; i++) if (w[i].start < w[i - 1].end) return [w[i - 1].name, w[i].name];
  return null;
}

/* ---- range ------------------------------------------------------- */
function rangeWindow() {
  const r = S.config.range || { mode: 'last', seconds: 1800 }; const now = Date.now() / 1000;
  return r.mode === 'last' ? [now - r.seconds, now] : [r.t0, r.t1];
}
function renderQuick() {
  const host = document.getElementById('quick'); host.innerHTML = '';
  QUICK.forEach(([lbl, secs]) => {
    const b = document.createElement('button'); b.textContent = 'last ' + lbl;
    const r = S.config.range; if (r.mode === 'last' && r.seconds === secs) b.classList.add('on');
    b.onclick = () => { S.config.range = { mode: 'last', seconds: secs }; setDirty(true); renderQuick(); refreshAll(); };
    host.appendChild(b);
  });
}
function syncRangeInputs() {
  const [t0, t1] = rangeWindow();
  const iso = t => new Date((t - new Date().getTimezoneOffset() * 60) * 1000).toISOString().slice(0, 19);
  document.getElementById('from').value = iso(t0); document.getElementById('to').value = iso(t1);
}

/* ---- charts ------------------------------------------------------ */
const CURSOR_SYNC = uPlot.sync('hm');
function unitsOf(refs) { const seen = []; refs.forEach(r => { const u = S.catalog[r]?.unit ?? 'count'; if (!seen.includes(u)) seen.push(u); }); return seen; }
function gutterOf(refs) { const n = Math.min(unitsOf(refs).length, MAX_AXES); return { left: Math.ceil(n / 2), right: Math.floor(n / 2) }; }
function profileGutter() {
  let left = 1, right = 0;
  (S.config.graphs || []).forEach(g => { const u = gutterOf((g.series || []).filter(r => S.catalog[r]));
    left = Math.max(left, u.left); right = Math.max(right, u.right); });
  return { left, right };
}
function showDragTip(entry, u) {
  let tip = entry.dragTip;
  if (!tip) { tip = document.createElement('div'); tip.className = 'dragtip'; entry.plotEl.appendChild(tip); entry.dragTip = tip; }
  const a = u.posToVal(u.select.left, 'x'), b = u.posToVal(u.select.left + u.select.width, 'x');
  tip.textContent = `${tsShort(a)} → ${tsShort(b)}   (${durStr(Math.max(0, b - a))})`;
  tip.style.display = 'block'; tip.style.left = Math.max(2, u.select.left) + 'px'; tip.style.top = '2px';
}
function hideDragTip(entry) { if (entry.dragTip) entry.dragTip.style.display = 'none'; }
function setFrozen(on) {
  if (S.frozen === on) return; S.frozen = on;
  S.charts.forEach(e => { e.legendEl?.classList.toggle('frozen', on);
    e.plotEl?.closest('.graph')?.classList.toggle('frozen', on); renderLegend(e, e.plot?.cursor?.idx ?? null); });
  if (!on) S.charts.forEach(e => loadGraph(e));
}
function wireInteractions(entry) {
  const over = entry.plot?.over; if (!over) return;
  over.addEventListener('mousedown', (ev) => {
    if (ev.ctrlKey || ev.metaKey) { ev.preventDefault(); ev.stopPropagation(); setFrozen(true); }
    else entry.dragging = true;
  }, true);
  over.addEventListener('mouseleave', () => hideDragTip(entry));
  over.addEventListener('contextmenu', (ev) => { if (ev.ctrlKey || ev.metaKey) ev.preventDefault(); });
}
window.addEventListener('mouseup', () => { S.charts.forEach(e => { e.dragging = false; hideDragTip(e); }); if (S.frozen) setFrozen(false); });
window.addEventListener('keyup', (ev) => { if (S.frozen && (ev.key === 'Control' || ev.key === 'Meta')) setFrozen(false); });
window.addEventListener('blur', () => { if (S.frozen) setFrozen(false); });

function buildChart(entry) {
  const g = entry.cfg;
  const refs = g.series.filter(r => S.catalog[r]);
  const units = unitsOf(refs).slice(0, MAX_AXES);
  const srcs = sources();
  const maxGutter = profileGutter(), mine = gutterOf(refs);
  const multiMon = new Set(refs.map(r => refParts(r)[0])).size > 1;
  const uSeries = [{}], meta = [];
  units.forEach(u => {
    const inUnit = refs.filter(r => (S.catalog[r]?.unit ?? 'count') === u);
    inUnit.forEach((r, i) => srcs.forEach((src, si) => {
      const c = shade(u, i, inUnit.length);
      uSeries.push({ label: seriesLabel(r, multiMon), stroke: c, width: si === 0 ? 1.6 : 1.2,
        dash: si === 0 ? undefined : [5, 3], scale: u, spanGaps: false, points: { show: false } });
      meta.push({ ref: r, unit: u, color: c, srcIdx: si, srcLabel: src.label });
    }));
  });
  const axes = [{ stroke: '#8b93a5', grid: { stroke: '#2c313d', width: 1 }, ticks: { stroke: '#2c313d' } }];
  units.forEach((u, i) => axes.push({ scale: u, side: i % 2 === 0 ? 3 : 1, stroke: axisColor(u),
    grid: { show: i === 0, stroke: '#2c313d', width: 1 }, ticks: { stroke: '#2c313d' },
    size: AXIS_SIZE, label: u, labelSize: AXIS_LABEL,
    values: (self, ticks) => ticks.map(v => fmt(v, u).replace(' ' + u, '')) }));
  const el = entry.plotEl;
  if (entry.plot) { try { entry.plot.destroy(); } catch (e) { /* gone */ } entry.plot = null; }
  entry.dragTip = null; el.innerHTML = '';
  const opts = {
    width: el.clientWidth || 800, height: g.height || 220, series: uSeries, axes,
    scales: { x: { time: true, range: () => rangeWindow() } },
    padding: [8, (maxGutter.right - mine.right) * AXIS_W + 8, 0, (maxGutter.left - mine.left) * AXIS_W],
    legend: { show: false },
    cursor: { drag: { x: true, y: false }, sync: { key: CURSOR_SYNC.key, setSeries: false, scales: ['x', null] } },
    hooks: {
      setSelect: [(u) => { hideDragTip(entry);
        if (S.frozen) { u.setSelect({ width: 0, height: 0 }, false); return; }
        if (u.select.width > 8) { const t0 = u.posToVal(u.select.left, 'x'), t1 = u.posToVal(u.select.left + u.select.width, 'x');
          S.config.range = { mode: 'abs', t0, t1 }; document.getElementById('liveTail').checked = false;
          setDirty(true); renderQuick(); syncRangeInputs(); refreshAll(); } }],
      setCursor: [(u) => { renderLegend(entry, u.cursor.idx); if (u.select && u.select.width > 2 && entry.dragging) showDragTip(entry, u); }],
    },
  };
  entry.meta = meta; entry.units = units; entry.multiMon = multiMon;
  entry.plot = new uPlot(opts, [[]], el);
  wireInteractions(entry); renderLegend(entry, null);
}
function renderLegend(entry, idx) {
  const host = entry.legendEl; host.innerHTML = '';
  entry.meta.forEach((m, i) => {
    const item = document.createElement('div'); item.className = 'item';
    const sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = m.color;
    if (m.srcIdx > 0) sw.style.background = `repeating-linear-gradient(90deg, ${m.color} 0 4px, transparent 4px 7px)`;
    item.appendChild(sw);
    const [mon, gpu] = refParts(m.ref);
    const tag = document.createElement('span'); tag.className = 'pill mon'; tag.textContent = `${monLabel(mon)}/${gpuLabel(mon, gpu)}`; item.appendChild(tag);
    const lbl = document.createElement('span');
    lbl.textContent = (entry.meta.some(o => o.srcIdx > 0) ? m.srcLabel + '· ' : '') + (S.catalog[m.ref]?.label ?? m.ref);
    if (S.catalog[m.ref]?.note) lbl.title = S.catalog[m.ref].note; item.appendChild(lbl);
    const val = document.createElement('span'); val.className = 'val';
    const data = entry.plot?.data?.[i + 1]; let v = null;
    if (data && data.length) v = idx != null && idx < data.length ? data[idx] : data[data.length - 1];
    if (v == null && idx == null && m.srcIdx === 0 && S.lastValues[m.ref] != null) v = S.lastValues[m.ref];
    val.textContent = fmt(v, m.unit); item.appendChild(val);
    if (m.srcIdx === 0) { const x = document.createElement('span'); x.className = 'x'; x.textContent = '✕'; x.title = 'remove';
      x.onclick = () => { entry.cfg.series = entry.cfg.series.filter(r => r !== m.ref); setDirty(true); rebuildGraph(entry); }; item.appendChild(x); }
    host.appendChild(item);
  });
  if (!entry.meta.length) host.innerHTML = '<span class="note">no series — use ADD or ADD GROUP</span>';
  if (S.frozen) { const t = document.createElement('span'); t.className = 'frozen-tag'; t.textContent = 'HELD — release ctrl to resume'; host.appendChild(t); }
}
async function loadGraph(entry) {
  if (S.frozen) return;
  const refs = entry.cfg.series.filter(r => S.catalog[r]);
  if (!refs.length) { entry.plot?.setData([[]]); renderLegend(entry, null); return; }
  const [t0, t1] = rangeWindow(); const srcs = sources().filter(s => s.dbs.length); if (!srcs.length) return;
  let res;
  try { res = await api.send('/api/query', { refs, t0, t1, max_points: Math.max(400, entry.plotEl.clientWidth || 800), sources: srcs, interval: 5 }); }
  catch (e) { if (e.message !== 'login') toast('query failed: ' + e.message, true); return; }
  const xs = new Set(); res.sources.forEach(sr => Object.values(sr.data).forEach(pts => pts.forEach(p => xs.add(p[0]))));
  const x = [...xs].sort((a, b) => a - b); const index = new Map(x.map((t, i) => [t, i])); const cols = [x];
  entry.meta.forEach(m => { const col = new Array(x.length).fill(null);
    (res.sources[m.srcIdx]?.data?.[m.ref] || []).forEach(([t, v]) => { const i = index.get(t); if (i !== undefined) col[i] = v; });
    cols.push(col); });
  entry.plot?.setData(cols); renderLegend(entry, null);
}
function rebuildGraph(entry) { buildChart(entry); loadGraph(entry); pushSubscription(); }
function restartTimer(entry) {
  clearInterval(entry.timer);
  entry.timer = setInterval(() => { if (S.frozen) return;
    if (document.getElementById('liveTail').checked || S.config.range.mode === 'last') loadGraph(entry); }, entry.cfg.update_ms || 1000);
}
function renderGraphs() {
  const host = document.getElementById('graphs');
  S.charts.forEach(e => { clearInterval(e.timer); if (e.plot) { try { e.plot.destroy(); } catch (err) {} e.plot = null; } });
  host.innerHTML = ''; S.charts = [];
  if (!S.config.graphs.length) { host.innerHTML = '<div class="empty">No graphs in this profile. Use <b>+ Graph</b>.</div>'; return; }
  S.config.graphs.forEach((g, gi) => {
    if (!g.series) g.series = [];
    const box = document.createElement('div'); box.className = 'graph';
    const head = document.createElement('div'); head.className = 'head';
    const t = document.createElement('input'); t.className = 'title'; t.value = g.title || 'Graph ' + (gi + 1);
    t.onchange = () => { g.title = t.value; setDirty(true); }; head.appendChild(t);
    const entry = { cfg: g };
    const mk = (label, fn, cls) => { const b = document.createElement('button'); b.textContent = label; b.onclick = fn; if (cls) b.className = cls; head.appendChild(b); };
    mk('ADD', () => openAdd(entry)); mk('ADD GROUP', () => openAddGroup(entry));
    const spd = document.createElement('select');
    [[500, '0.5s'], [1000, '1s'], [2000, '2s'], [5000, '5s'], [15000, '15s'], [60000, '1m']].forEach(([ms, lbl]) => {
      const o = document.createElement('option'); o.value = ms; o.textContent = 'redraw ' + lbl; o.selected = (g.update_ms || 1000) === ms; spd.appendChild(o); });
    spd.title = 'Redraw rate for this graph. Sample rates are per monitor (Settings).';
    spd.onchange = () => { g.update_ms = +spd.value; setDirty(true); restartTimer(entry); }; head.appendChild(spd);
    mk('Delete graph', () => {
      if (!confirm(`Delete "${g.title || gi + 1}" from this profile?\n\nGraph settings only — no recorded data is deleted.`)) return;
      if (!confirm('Are you sure?')) return;
      S.config.graphs.splice(gi, 1); setDirty(true); renderGraphs(); pushSubscription(); }, 'danger');
    box.appendChild(head);
    const body = document.createElement('div'); body.className = 'body'; box.appendChild(body);
    const legend = document.createElement('div'); legend.className = 'legend'; box.appendChild(legend);
    host.appendChild(box);
    entry.plotEl = body; entry.legendEl = legend; S.charts.push(entry);
    buildChart(entry); loadGraph(entry); restartTimer(entry);
  });
  pushSubscription();
}
function refreshAll() { syncRangeInputs(); S.charts.forEach(e => { buildChart(e); loadGraph(e); }); pushSubscription(); }
async function pushSubscription() {
  const refs = new Set(); S.config.graphs.forEach(g => (g.series || []).forEach(r => refs.add(r)));
  try { await api.send('/api/subscribe', { client: S.clientId, refs: [...refs] }); } catch (e) { /* offline */ }
}

/* ---- ADD / ADD GROUP -------------------------------------------- */
function modal(html) { const sheet = document.getElementById('sheet'); sheet.innerHTML = html; document.getElementById('modal').classList.add('on'); return sheet; }
function closeModal() { document.getElementById('modal').classList.remove('on'); }
document.getElementById('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(); };

/* Groups arrive per monitor from the manager; the picker filters by
 * monitor first so a fleet of eight boxes does not become one wall. */
function monitorPicker(container, onPick) {
  const wrap = document.createElement('div'); wrap.className = 'monpick';
  const ids = [...new Set(S.groups.map(g => g.monitor))].sort();
  let cur = ids[0] || null;
  const btns = [];
  const all = document.createElement('button'); all.textContent = 'all monitors'; wrap.appendChild(all);
  const set = (id) => { cur = id; btns.forEach(([b, i]) => b.classList.toggle('on', i === id)); all.classList.toggle('on', id === null); onPick(id); };
  all.onclick = () => set(null);
  ids.forEach(id => { const b = document.createElement('button'); const m = monInfo(id);
    b.textContent = `${monLabel(id)} (${m.state || '?'})`; b.onclick = () => set(id); wrap.appendChild(b); btns.push([b, id]); });
  container.appendChild(wrap);
  set(cur);
}
function groupsFor(monId) { return S.groups.filter(g => g.keys.length && (monId === null || g.monitor === monId)); }
function groupTitle(g) { return `${monLabel(g.monitor)} · ${g.label}`; }

function openAdd(entry) {
  const sheet = modal(`<h3>Add series to "${entry.cfg.title || 'graph'}"</h3>
    <div class="content"><div id="mp"></div><input id="flt" placeholder="filter…" style="width:100%;margin-bottom:8px"><div id="list"></div></div>
    <div class="foot"><span class="count" id="cnt"></span><button id="cancel">Cancel</button><button id="ok" class="primary">ADD</button></div>`);
  const chosen = new Set(); const list = sheet.querySelector('#list'); let monId = null;
  function draw(filter) {
    list.innerHTML = ''; const f = (filter || '').toLowerCase();
    groupsFor(monId).forEach(g => {
      const hits = g.keys.filter(r => { const s = S.catalog[r]; return s && (!f || (seriesLabel(r) + ' ' + r + ' ' + g.label).toLowerCase().includes(f)); });
      if (!hits.length) return;
      const d = document.createElement('details'); d.className = 'grp'; d.open = !!f;
      d.innerHTML = `<summary>${groupTitle(g)} <span class="unit">· ${hits.length}${g.unit ? ' · ' + g.unit : ' · mixed'}</span></summary>`;
      const items = document.createElement('div'); items.className = 'items';
      hits.forEach(r => { const s = S.catalog[r]; const lab = document.createElement('label'); const cb = document.createElement('input');
        cb.type = 'checkbox'; cb.checked = chosen.has(r) || entry.cfg.series.includes(r); cb.disabled = entry.cfg.series.includes(r);
        cb.onchange = () => { cb.checked ? chosen.add(r) : chosen.delete(r); sheet.querySelector('#cnt').textContent = `${chosen.size} selected`; };
        lab.appendChild(cb); const txt = document.createElement('span'); txt.textContent = s.label + (s.note ? ' *' : ''); txt.title = r + (s.note ? '\n' + s.note : '');
        lab.appendChild(txt); items.appendChild(lab); });
      d.appendChild(items); list.appendChild(d);
    });
  }
  monitorPicker(sheet.querySelector('#mp'), (id) => { monId = id; draw(sheet.querySelector('#flt').value); });
  sheet.querySelector('#flt').oninput = (e) => draw(e.target.value);
  sheet.querySelector('#cancel').onclick = closeModal;
  sheet.querySelector('#ok').onclick = () => { applyAdd(entry, [...chosen]); closeModal(); };
}
function openAddGroup(entry) {
  const sheet = modal(`<h3>Add a group to "${entry.cfg.title || 'graph'}"</h3><div class="content" id="content"></div>
    <div class="foot"><span class="count" id="cnt"></span><button id="cancel">Cancel</button><button id="ok" class="primary" disabled>ADD</button></div>`);
  const content = sheet.querySelector('#content'), ok = sheet.querySelector('#ok'), cnt = sheet.querySelector('#cnt');
  const listBox = document.createElement('div');
  function drawList(monId) {
    listBox.innerHTML = '';
    groupsFor(monId).forEach(g => { const b = document.createElement('button'); b.style.margin = '3px';
      b.textContent = `${groupTitle(g)} (${g.keys.length}${g.unit ? ', ' + g.unit : ''})`; b.onclick = () => pick(g); listBox.appendChild(b); });
  }
  monitorPicker(content, drawList); content.appendChild(listBox);
  function pick(g) {
    const chosen = new Set(g.keys.filter(r => !entry.cfg.series.includes(r)));
    content.innerHTML = '';
    const h = document.createElement('div'); h.innerHTML = `<b>${groupTitle(g)}</b> <span class="note">${g.unit ? 'all ' + g.unit + ' — one shared axis' : 'mixed units'}</span>`; content.appendChild(h);
    const btns = document.createElement('div'); btns.className = 'rowbtns';
    const all = document.createElement('button'); all.textContent = 'select all'; const none = document.createElement('button'); none.textContent = 'select none';
    const back = document.createElement('button'); back.textContent = '← groups'; btns.append(all, none, back); content.appendChild(btns);
    const items = document.createElement('div'); items.className = 'items'; items.style.display = 'grid'; items.style.gridTemplateColumns = 'repeat(auto-fill,minmax(250px,1fr))'; content.appendChild(items);
    function draw() { items.innerHTML = '';
      g.keys.forEach(r => { const s = S.catalog[r]; if (!s) return; const already = entry.cfg.series.includes(r);
        const lab = document.createElement('label'); const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = chosen.has(r); cb.disabled = already;
        cb.onchange = () => { cb.checked ? chosen.add(r) : chosen.delete(r); upd(); }; lab.appendChild(cb);
        const t = document.createElement('span'); t.textContent = s.label + (already ? ' (already on graph)' : ''); t.title = r; if (s.note) t.style.color = 'var(--dim)';
        lab.appendChild(t); items.appendChild(lab); });
      upd(); }
    function upd() { cnt.textContent = `${chosen.size} of ${g.keys.length} selected`; ok.disabled = chosen.size === 0; }
    all.onclick = () => { g.keys.forEach(r => { if (!entry.cfg.series.includes(r)) chosen.add(r); }); draw(); };
    none.onclick = () => { chosen.clear(); draw(); };
    back.onclick = () => openAddGroup(entry);
    ok.onclick = () => { applyAdd(entry, [...chosen]); closeModal(); };
    draw();
  }
}
function applyAdd(entry, refs) {
  if (!refs.length) return;
  const merged = [...entry.cfg.series, ...refs.filter(r => !entry.cfg.series.includes(r))];
  const units = unitsOf(merged);
  if (units.length > MAX_AXES) { toast(`That would need ${units.length} Y axes (${units.join(', ')}); the limit is ${MAX_AXES}.`, true); return; }
  entry.cfg.series = merged; setDirty(true); rebuildGraph(entry);
}

/* ---- monitors strip ---------------------------------------------- */
function renderMonitors() {
  const host = document.getElementById('monitors'); host.innerHTML = '';
  if (!S.monitors.length) { host.innerHTML = '<span class="note">no monitors have connected to the manager yet</span>'; return; }
  S.monitors.forEach(m => {
    const d = document.createElement('div'); d.className = 'mon ' + m.state;
    const gpus = (m.gpus || []).map(g => `<span class="g"><b>${g.name}</b> ${g.model.split('[')[0].slice(0, 18)}${g.state !== 'active' ? ' (' + g.state + ')' : ''}</span>`).join(' ');
    const skew = Math.abs(m.skew || 0) > 5 ? ` <span class="pill warn">skew ${m.skew.toFixed(1)}s</span>` : '';
    const ob = m.outbox && (m.outbox.rows_dropped > 0 || !m.outbox.recording) ? ` <span class="pill stopped">${m.outbox.recording ? 'DATA DROPPED' : 'RECORDING STOPPED'}</span>` : '';
    const bl = m.outbox && m.outbox.rows_pending > 5000 ? ` <span class="pill warn">backlog ${m.outbox.rows_pending}</span>` : '';
    d.innerHTML = `<span class="dot"></span><b title="${m.monitor}">${monLabel(m.monitor)}</b>` +
      `<span class="note">${m.state}${m.state !== 'online' ? ' · ' + ago(m.last_seen) + ' ago' : ''}${m.engine ? ' · ' + m.engine.sweep_ms + 'ms' : ''}</span> ${gpus}${skew}${ob}${bl}`;
    if (S.me?.role === 'admin') { d.style.cursor = 'pointer'; d.title = 'click to manage'; d.onclick = () => openMonitor(m.monitor); }
    host.appendChild(d);
  });
}
async function loadMonitors() {
  try { S.monitors = await api.get('/api/monitors'); } catch (e) { return; }
  renderMonitors();
}
async function loadCatalog() {
  const cat = await api.get('/api/catalog');
  S.catalog = {}; cat.series.forEach(s => { S.catalog[s.ref] = s; });
  S.groups = cat.groups || []; S.statics = cat.statics || {};
}

/* ---- admin: monitor page ----------------------------------------- */
async function openMonitor(id) {
  const m = monInfo(id);
  const sheet = modal(`<h3>${monLabel(id)} <span class="note">(${id})</span></h3><div class="content" id="c"></div>
    <div class="foot"><span class="count"></span><button id="close">Close</button></div>`);
  const c = sheet.querySelector('#c');
  c.innerHTML = `
    <div class="setrow"><label>Nickname</label><input id="nick" value="${m.nickname || ''}" placeholder="${id}"><button id="nickSave">Save</button></div>
    <h4>GPUs</h4><table class="tbl"><thead><tr><th>id</th><th>name</th><th>model</th><th>serial</th><th>pci</th><th>state</th><th></th></tr></thead><tbody id="gt"></tbody></table>
    <h4>Engine settings <span class="note">(this monitor only)</span></h4>
    <div class="setrow"><label>Recording interval (s)</label><input id="ri" type="number" min="0.5" step="0.5" value="${m.settings?.record_interval ?? 5}"></div>
    <div class="setrow"><label>Record exotic rails</label><input id="ex" type="checkbox" ${m.settings?.record_exotic ? 'checked' : ''}></div>
    <div class="setrow"><label>Record NIC rates</label><input id="nr" type="checkbox" ${m.settings?.record_nic_rates ? 'checked' : ''}></div>
    <div class="setrow"><label>Record NIC errors</label><input id="ne" type="checkbox" ${m.settings?.record_nic_errors ? 'checked' : ''}></div>
    <button id="setSave" class="primary">Apply settings</button>
    <p class="note">state: ${m.state} · version ${m.version || '?'} · skew ${(m.skew || 0).toFixed(3)}s · rows this session ${m.rows_received ?? '-'}
    ${m.outbox ? `· outbox pending ${m.outbox.rows_pending} / dropped ${m.outbox.rows_dropped}` : ''}</p>`;
  const gt = c.querySelector('#gt');
  (m.gpus || []).forEach(g => { const tr = document.createElement('tr');
    tr.innerHTML = `<td>${g.gpu_id}</td><td><input value="${g.name}" size="12"></td><td>${g.model.slice(0, 30)}</td><td>${g.serial || '—'}</td><td>${g.pci}</td><td>${g.state}</td><td><button>rename</button></td>`;
    tr.querySelector('button').onclick = async () => { try { await api.send('/api/gpu/rename', { monitor: id, gpu_id: g.gpu_id, name: tr.querySelector('input').value }); toast('rename sent'); setTimeout(loadMonitors, 1500); } catch (e) { toast(e.message, true); } };
    gt.appendChild(tr); });
  c.querySelector('#nickSave').onclick = async () => { try { await api.send('/api/nickname', { monitor: id, nickname: c.querySelector('#nick').value }); toast('nickname saved'); await loadMonitors(); renderGraphs(); } catch (e) { toast(e.message, true); } };
  c.querySelector('#setSave').onclick = async () => { try { await api.send('/api/settings', { monitor: id, settings: {
      record_interval: +c.querySelector('#ri').value, record_exotic: c.querySelector('#ex').checked,
      record_nic_rates: c.querySelector('#nr').checked, record_nic_errors: c.querySelector('#ne').checked } });
    toast('settings sent to ' + monLabel(id)); } catch (e) { toast(e.message, true); } };
  sheet.querySelector('#close').onclick = closeModal;
}

/* ---- burger menu -------------------------------------------------- */
function renderMenu() {
  const m = document.getElementById('menu'); m.innerHTML = '';
  const item = (label, fn) => { const b = document.createElement('button'); b.className = 'item'; b.textContent = label; b.onclick = () => { m.classList.remove('on'); fn(); }; m.appendChild(b); };
  item('Change my password', openPassword);
  item('API token for the TUI', openToken);
  if (S.me?.role === 'admin') item('Users', openUsers);
  item('Sign out', async () => { await api.send('/api/logout', {}); location.href = '/login'; });
  const info = document.createElement('div'); info.className = 'info';
  info.innerHTML = `signed in as <b>${S.me?.name}</b> (${S.me?.role})<br>version <b>${S.version || '?'}</b><br>build <b>${S.build.hash || '?'}</b><br><span>${S.build.date || ''}</span>`;
  m.appendChild(info);
}
document.getElementById('burgerBtn').onclick = (e) => { e.stopPropagation(); document.getElementById('menu').classList.toggle('on'); };
document.addEventListener('click', (e) => { if (!e.target.closest('#burger')) document.getElementById('menu').classList.remove('on'); });

function openPassword() {
  const sheet = modal(`<h3>Change password</h3><div class="content">
    <div class="setrow"><label>Current</label><input id="cur" type="password"></div>
    <div class="setrow"><label>New (8+)</label><input id="n1" type="password"></div>
    <div class="setrow"><label>Repeat</label><input id="n2" type="password"></div><div id="m"></div></div>
    <div class="foot"><span class="count"></span><button id="cancel">Cancel</button><button id="ok" class="primary">Change</button></div>`);
  sheet.querySelector('#cancel').onclick = closeModal;
  sheet.querySelector('#ok').onclick = async () => {
    const n1 = sheet.querySelector('#n1').value, n2 = sheet.querySelector('#n2').value;
    if (n1 !== n2) { sheet.querySelector('#m').innerHTML = '<div class="msg err">passwords do not match</div>'; return; }
    try { await api.send('/api/password', { current: sheet.querySelector('#cur').value, new: n1 }); toast('password changed'); closeModal(); }
    catch (e) { sheet.querySelector('#m').innerHTML = `<div class="msg err">${e.message}</div>`; } };
}
function openToken() {
  const sheet = modal(`<h3>API token</h3><div class="content"><p class="note">For <code>health-monitor tui --token …</code>. Shown once.</p>
    <div class="setrow"><label>Label</label><input id="lbl" value="tui"></div><div id="out"></div></div>
    <div class="foot"><span class="count"></span><button id="cancel">Close</button><button id="ok" class="primary">Create token</button></div>`);
  sheet.querySelector('#cancel').onclick = closeModal;
  sheet.querySelector('#ok').onclick = async () => { try { const r = await api.send('/api/tokens', { label: sheet.querySelector('#lbl').value });
    sheet.querySelector('#out').innerHTML = `<div class="msg ok" style="word-break:break-all">${r.token}</div>`; } catch (e) { toast(e.message, true); } };
}
async function openUsers() {
  const sheet = modal(`<h3>Users</h3><div class="content" id="c"></div><div class="foot"><span class="count"></span><button id="close">Close</button></div>`);
  sheet.querySelector('#close').onclick = closeModal;
  const c = sheet.querySelector('#c');
  async function draw() {
    const users = await api.get('/api/users');
    c.innerHTML = `<table class="tbl"><thead><tr><th>name</th><th>role</th><th>status</th><th>created</th><th></th></tr></thead><tbody id="ut"></tbody></table>
      <h4>Add user</h4><div class="setrow"><input id="un" placeholder="name"><input id="up" type="password" placeholder="password (8+)">
      <select id="ur"><option value="user">user</option><option value="admin">admin</option></select><button id="ua" class="primary">Add</button></div><div id="m"></div>`;
    const tb = c.querySelector('#ut');
    users.forEach(u => { const tr = document.createElement('tr');
      tr.innerHTML = `<td>${u.name}</td><td>${u.role}</td><td>${u.disabled ? 'disabled' : 'active'}</td><td>${u.created.slice(0, 10)}</td>
        <td><button data-a="pw">password</button> <button data-a="role">${u.role === 'admin' ? 'make user' : 'make admin'}</button>
        <button data-a="dis">${u.disabled ? 'enable' : 'disable'}</button> <button data-a="del" class="danger">delete</button></td>`;
      tr.querySelectorAll('button').forEach(b => b.onclick = async () => { try {
        const a = b.dataset.a;
        if (a === 'pw') { const p = prompt(`New password for ${u.name} (8+ chars):`); if (p) await api.send(`/api/users/${encodeURIComponent(u.name)}/password`, { password: p }); }
        else if (a === 'role') await api.send(`/api/users/${encodeURIComponent(u.name)}/role`, { role: u.role === 'admin' ? 'user' : 'admin' });
        else if (a === 'dis') await api.send(`/api/users/${encodeURIComponent(u.name)}/disable`, { disabled: !u.disabled });
        else if (a === 'del') { if (!confirm(`Delete ${u.name} and all their profiles?`)) return; if (!confirm('Are you sure?')) return;
          await api.send(`/api/users/${encodeURIComponent(u.name)}`, undefined, 'DELETE'); }
        await draw(); } catch (e) { toast(e.message, true); } });
      tb.appendChild(tr); });
    c.querySelector('#ua').onclick = async () => { try { await api.send('/api/users', { name: c.querySelector('#un').value, password: c.querySelector('#up').value, role: c.querySelector('#ur').value }); await draw(); }
      catch (e) { c.querySelector('#m').innerHTML = `<div class="msg err">${e.message}</div>`; } };
  }
  await draw();
}

/* ---- profiles ----------------------------------------------------- */
async function loadProfiles(select) {
  const list = await api.get('/api/profiles');
  const sel = document.getElementById('profile'); sel.innerHTML = '';
  list.forEach(p => { const o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.appendChild(o); });
  const want = select || S.profileName || (list[0] && list[0].name);
  if (want) { sel.value = want; await openProfile(want); }
}
async function openProfile(name) {
  const p = await api.get('/api/profiles/' + encodeURIComponent(name));
  S.profileName = p.name; S.profileVersion = p.version;
  S.config = Object.assign({ graphs: [], range: { mode: 'last', seconds: 1800 }, sources: null }, p.config || {});
  if (!S.config.sources) S.config.sources = defaultSources();
  S.config.sources.forEach(s => { s.dbs = (s.dbs || []).filter(path => S.dbs.some(d => d.path === path)); if (!s.dbs.length && S.liveDb) s.dbs = [S.liveDb]; });
  setDirty(false); renderQuick(); syncRangeInputs(); renderSources(); renderGraphs();
}
async function saveProfile(asNew) {
  let name = S.profileName;
  if (asNew) { name = prompt('Save this layout as a new profile named:', ''); if (!name) return; }
  try { const r = await api.send('/api/profiles/' + encodeURIComponent(name), { version: asNew ? null : S.profileVersion, config: S.config });
    S.profileName = r.name; S.profileVersion = r.version; setDirty(false); toast(`saved "${r.name}" (v${r.version})`); await loadProfiles(r.name); }
  catch (e) { toast(e.status === 409 ? `${e.data.name} was updated elsewhere (another tab?) — reload it and try again.` : 'save failed: ' + e.message, true); }
}

/* ---- status ------------------------------------------------------- */
async function pollStatus() {
  let st; try { st = await api.get('/api/status'); } catch { document.getElementById('status').innerHTML = '<span class="pill stopped">WEB SERVER UNREACHABLE</span>'; return; }
  const bits = [];
  if (!st.manager) bits.push(`<span class="pill stopped">MANAGER UNREACHABLE — ${st.error || ''}</span>`);
  else { const s = st.manager.store;
    bits.push(s.recording ? `<span class="pill rec">● recording</span>` : `<span class="pill stopped">RECORDING STOPPED — ${s.reason}</span>`);
    bits.push(`<span class="pill">${st.manager.monitors_online} monitors online</span>`);
    bits.push(`<span class="pill">${fmtBytes(s.free_bytes)} free</span>`); }
  if (!st.web.live_link) bits.push('<span class="pill warn">live link down</span>');
  if (S.dirty) bits.push('<span class="pill warn">unsaved changes</span>');
  document.getElementById('status').innerHTML = bits.join('');
}

/* ---- boot --------------------------------------------------------- */
async function boot() {
  const me = await api.get('/api/me'); S.me = me.user; S.build = me.build || {}; S.version = me.version;
  renderMenu();
  await loadMonitors();
  await loadCatalog();
  const st = await api.get('/api/status'); S.liveDb = st.manager?.live_db || null;
  S.dbs = await api.get('/api/databases'); if (!S.liveDb && S.dbs.length) S.liveDb = S.dbs[0].path;
  await loadProfiles(); renderSources();

  document.getElementById('profile').onchange = async (e) => { if (S.dirty && !confirm('Discard unsaved changes?')) { e.target.value = S.profileName; return; } await openProfile(e.target.value); };
  document.getElementById('save').onclick = () => saveProfile(false);
  document.getElementById('saveAs').onclick = () => saveProfile(true);
  document.getElementById('delProfile').onclick = async () => { const n = S.profileName;
    if (!confirm(`Delete profile "${n}"?\n\nGraph settings only — no recorded data is deleted.`)) return; if (!confirm('Are you sure?')) return;
    await api.send('/api/profiles/' + encodeURIComponent(n), undefined, 'DELETE'); S.profileName = null; await loadProfiles(); };
  document.getElementById('addGraph').onclick = () => { S.config.graphs.push({ title: 'Graph ' + (S.config.graphs.length + 1), series: [], update_ms: 1000 }); setDirty(true); renderGraphs(); };
  document.getElementById('applyRange').onclick = () => { const a = document.getElementById('from').value, b = document.getElementById('to').value; if (!a || !b) return;
    const t0 = new Date(a).getTime() / 1000, t1 = new Date(b).getTime() / 1000; if (t1 <= t0) { toast('"to" must be after "from"', true); return; }
    S.config.range = { mode: 'abs', t0, t1 }; document.getElementById('liveTail').checked = false; setDirty(true); renderQuick(); refreshAll(); };
  document.getElementById('liveTail').onchange = (e) => { if (e.target.checked && S.config.range.mode === 'abs') { S.config.range = { mode: 'last', seconds: 1800 }; renderQuick(); refreshAll(); } };

  const es = new EventSource('/api/stream?client=' + S.clientId);
  es.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === 'sample') Object.assign(S.lastValues, m.values);
    else if (m.type === 'monitor') { const i = S.monitors.findIndex(x => x.monitor === m.monitor); if (i >= 0) S.monitors[i] = m; else S.monitors.push(m);
      renderMonitors(); if (m.state === 'online') loadCatalog().then(() => S.charts.forEach(e => buildChart(e))); }
    else if (m.type === 'manager') toast('live link to the manager dropped; reconnecting', true); };

  setInterval(pollStatus, 3000); pollStatus();
  setInterval(loadMonitors, 15000);
  window.addEventListener('resize', () => S.charts.forEach(e => e.plot?.setSize({ width: e.plotEl.clientWidth, height: e.cfg.height || 220 })));
  window.addEventListener('beforeunload', (e) => { if (!S.dirty) return; e.preventDefault(); e.returnValue = 'You have unsaved changes.'; return e.returnValue; });
}
window.__hm = S;
boot().catch(e => { if (e.message !== 'login') document.body.innerHTML = `<div class="empty">failed to start: ${e.message}</div>`; });
