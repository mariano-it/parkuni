/* Panel de administración: lee el estado compartido cada segundo. */
CONFIG.entrance = MAP.nodes.E0;

const S = { st: null, dragging: false };
const svg = $('map');
const b = MAP.bounds;
const FULL = [b.x, b.y, b.w, b.h];
svg.setAttribute('viewBox', FULL.join(' '));
svg.style.aspectRatio = `${b.w} / ${b.h}`;
$('mainTitle').textContent = MAP.title;
const MULTI = ZONES.length > 1;               // el campus tiene zonas ampliables; el prototipo no
const main = { svg, lot: drawMap(svg, { titles: true }) };
const maps = [main];                                    // el mapa central + una vista ampliada por zona

/* ---------- vistas ampliadas por zona ---------- */
function zoneBox(zone) {
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const sp of SPOTS) if (sp.zone === zone) {
    x0 = Math.min(x0, sp.x); y0 = Math.min(y0, sp.y); x1 = Math.max(x1, sp.x); y1 = Math.max(y1, sp.y);
  }
  const pad = 55;
  return [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
}
const zonePanels = {};
const LEFT = ['Zona Norte', 'Acceso norte', 'Acceso'], RIGHT = ['Zona Este', 'Zona Sur'];
if (!MULTI) { $('zoomgrid').classList.add('single'); $('zLeft').remove(); $('zRight').remove(); }
for (const [host, list] of MULTI ? [[$('zLeft'), LEFT], [$('zRight'), RIGHT]] : []) {
  for (const zone of list) {
    const card = document.createElement('div');
    card.className = 'card zone-card';
    card.innerHTML = `<div class="zhead"><h3>${zone}</h3><span class="zbadge" data-badge></span></div>
      <svg role="img" aria-label="${zone}"></svg><div class="segchips" data-chips></div>`;
    host.appendChild(card);
    const zsvg = card.querySelector('svg'), box = zoneBox(zone);
    zsvg.setAttribute('viewBox', box.join(' '));
    zsvg.style.aspectRatio = `${box[2]} / ${box[3]}`;
    const m = { svg: zsvg, lot: drawMap(zsvg, { titles: true, only: zone }) };
    maps.push(m);
    zonePanels[zone] = { card, box, badge: card.querySelector('[data-badge]'), chips: card.querySelector('[data-chips]') };
    card.querySelector('.zhead').onclick = () => zoomMain(zone);
  }
}
for (const m of maps) m.drv = el('g', {}, m.lot.world);

/* ---------- zoom animado del mapa central ---------- */
let viewAnim = 0;
function setMainView(box) {
  cancelAnimationFrame(viewAnim);
  const from = svg.getAttribute('viewBox').split(' ').map(Number), t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 350), e = 1 - Math.pow(1 - k, 3);
    svg.setAttribute('viewBox', from.map((v, i) => v + (box[i] - v) * e).join(' '));
    if (k < 1) viewAnim = requestAnimationFrame(step);
  };
  viewAnim = requestAnimationFrame(step);
  clearTimeout(setMainView.t);
  setMainView.t = setTimeout(() => svg.setAttribute('viewBox', box.join(' ')), 450);   // por si la animación se pausa
}
function zoomMain(zone) {
  $('mainTitle').textContent = zone;
  $('btnFull').hidden = false;
  setMainView(zonePanels[zone].box);
  for (const [z, p] of Object.entries(zonePanels)) p.card.classList.toggle('active', z === zone);
}
$('btnFull').onclick = () => {
  $('mainTitle').textContent = MAP.title;
  $('btnFull').hidden = true;
  setMainView(FULL);
  for (const p of Object.values(zonePanels)) p.card.classList.remove('active');
};

/* ---------- tocar un cajón = forzar su estado (en cualquier mapa) ---------- */
for (const m of maps) m.svg.addEventListener('click', (e) => {
  const id = e.target.dataset && e.target.dataset.id;
  if (!id || !S.st) return;
  const cur = S.st.overrides[id];                     // undefined → ocupado → libre → automático
  const next = cur === undefined ? true : cur === true ? false : null;
  if (next === null) delete S.st.overrides[id]; else S.st.overrides[id] = next;
  render(S.st);
  api('/api/override', { id, occupied: next }).then(poll);
});

function render(st) {
  Occ.compute(st);
  const reserved = new Set(st.drivers.map((dr) => dr.target));
  for (const m of maps) { m.lot.setReserved(reserved); m.lot.setOcc(Occ.map); }
  const t = Occ.total, busy = t.total - t.free;
  $('kFree').textContent = t.free;
  $('kBusy').textContent = busy;
  $('kPct').textContent = (t.total ? Math.round((busy / t.total) * 100) : 0) + '%';
  $('kSpeed').textContent = st.speedingCount;
  $('limit').textContent = st.speedLimit;
  renderEmergency(st.emergency);

  $('zones').closest('.card').hidden = !MULTI;
  $('simCard').hidden = !MAP.simulate;
  $('zones').innerHTML = ZONES.map((z) => {
    const c = Occ.counts[z], pct = c.total ? Math.round(((c.total - c.free) / c.total) * 100) : 0;
    return `<div class="zone"><div><b>${z}</b><span>${c.free} libres de ${c.total}</span></div>
      <div class="bar"><i style="width:${pct}%"></i></div></div>`;
  }).join('');

  for (const [zone, p] of Object.entries(zonePanels)) {
    const c = Occ.counts[zone];
    p.badge.textContent = `${c.free} libres / ${c.total}`;
    p.badge.className = 'zbadge ' + (c.free ? '' : 'zero');
    p.chips.innerHTML = MAP.segments.filter((s) => s.zone === zone && s.type !== 'disabled').map((s) => {
      let f = 0;
      for (let i = 1; i <= s.n; i++) if (Occ.isFree(`${s.id}-${i}`)) f++;
      return `<span class="chip ${f ? '' : 'zero'}" title="${s.n - f} ocupados de ${s.n}"><b>${s.id}</b> ${f}/${s.n}</span>`;
    }).join('');
  }

  if (!S.dragging) {
    $('simOn').checked = st.sim.on;
    $('simLevel').value = Math.round(st.sim.level * 100);
    $('simLevelTxt').textContent = Math.round(st.sim.level * 100) + '%';
  }
  $('simLevel').disabled = !st.sim.on;

  $('sensors').innerHTML = Object.entries(MAP.sensors).map(([sid, spotId]) => {
    const seen = st.seen.includes(sid), stale = st.stale.includes(sid);
    const cls = !seen ? 'warn' : stale ? 'warn' : st.slots[sid] ? 'busy' : 'free';
    const txt = !seen ? 'sin datos' : stale ? 'sin señal' : st.slots[sid] ? 'ocupado' : 'libre';
    const cm = seen && st.distances[sid] != null ? ` · ${st.distances[sid]} cm` : '';
    return `<div class="led ${cls}"><i></i><b>${sid}</b><span>${spotId}</span><small>${txt}${cm}</small></div>`;
  }).join('');
  $('espNote').textContent = st.seen.length === 0
    ? 'Ningún sensor ha reportado todavía.'
    : st.esp ? 'ESP32 enviando datos.' : 'ESP32 sin señal: revisa alimentación y WiFi.';

  for (const m of maps) {
    m.drv.innerHTML = '';
    for (const dr of st.drivers) {
      if (dr.x == null) continue;
      const k = MAP.style.lane / 42;
      el('circle', { class: 'driver-ring', cx: dr.x, cy: dr.y, r: 22 * k }, m.drv);
      el('circle', { class: 'driver-dot', cx: dr.x, cy: dr.y, r: 9 * k }, m.drv);
    }
  }
  $('nDrivers').textContent = st.drivers.length;
  $('drivers').innerHTML = st.drivers.length ? st.drivers.map((dr) => {
    const over = dr.speed > st.speedLimit;
    return `<li>Hacia <b>${dr.target}</b> · <span class="${over ? 'over' : ''}">${Math.round(dr.speed || 0)} km/h${over ? ' ⚠' : ''}</span></li>`;
  }).join('') : '<li><small>Ninguno</small></li>';

  $('log').innerHTML = st.events.map((e) => `<li><time>${fmtTime(e.t)}</time>${e.msg}</li>`).join('');
}

async function poll() {
  try {
    const st = await api('/api/status?events');
    S.st = st;
    $('conn').classList.remove('bad');
    $('connText').textContent = 'Servidor conectado';
    render(st);
  } catch {
    $('conn').classList.add('bad');
    $('connText').textContent = 'Servidor no disponible';
  }
}

/* ---------- emergencia ---------- */
function renderEmergency(em) {
  const on = !!(em && em.on);
  S.emOn = on;
  $('emCard').classList.toggle('on', on);
  $('emState').textContent = on
    ? `ACTIVA desde las ${fmtTime(em.since)} · los LED parpadean en morado`
    : 'Normal · los LED indican libre / ocupado';
  $('btnEm').textContent = on ? 'DESACTIVAR EMERGENCIA' : 'ACTIVAR EMERGENCIA';
  for (const m of maps) m.lot.emergency(on);
}
$('emForm').onsubmit = async (e) => {
  e.preventDefault();
  const msg = $('emMsg');
  msg.textContent = 'Verificando…';
  try {
    const r = await api('/api/emergency', { on: !S.emOn, password: $('emPass').value });
    msg.textContent = r.ok ? '' : (r.error || 'No se pudo cambiar el estado');
    if (r.ok) renderEmergency(r.emergency);
  } catch { msg.textContent = 'Servidor no disponible'; }
  $('emPass').value = '';
  poll();
};

/* ---------- selector de mapa ---------- */
for (const [btn, id] of [['mapProto', 'prototype'], ['mapCampus', 'campus']]) {
  $(btn).classList.toggle('on', MAP.id === id);
  $(btn).onclick = () => { if (MAP.id !== id) switchMap(id); };
}

/* ---------- controles ---------- */
$('limUp').onclick = () => api('/api/config', { speedLimit: S.st.speedLimit + 5 }).then(poll);
$('limDown').onclick = () => api('/api/config', { speedLimit: S.st.speedLimit - 5 }).then(poll);
$('simOn').onchange = (e) => api('/api/sim', { on: e.target.checked, level: S.st.sim.level }).then(poll);
$('simLevel').oninput = (e) => { S.dragging = true; $('simLevelTxt').textContent = e.target.value + '%'; };
$('simLevel').onchange = (e) => {
  api('/api/sim', { on: S.st.sim.on, level: e.target.value / 100 }).then(() => { S.dragging = false; poll(); });
};
$('btnTake').onclick = () => {
  const ids = S.st.drivers.map((d) => d.target).filter((id) => SPOT_BY_ID[id]);
  Promise.all(ids.map((id) => api('/api/override', { id, occupied: true }))).then(poll);
};
$('btnClear').onclick = () => api('/api/overrides/clear', {}).then(poll);
$('btnReset').onclick = () => api('/api/reset', {}).then(poll);

/* Botón de pruebas: forzar día / noche solo en este panel (no toca la app del conductor). */
const autoResolve = theme.resolve;
let themeMode = 'auto';                 // 'auto' | 'light' | 'dark'
theme.resolve = () => (themeMode === 'auto' ? autoResolve() : themeMode);
$('btnTheme').onclick = () => {
  themeMode = { auto: 'light', light: 'dark', dark: 'auto' }[themeMode];
  theme.apply();
  $('btnTheme').textContent = 'Tema: ' + { auto: 'Auto', light: '☀ Día', dark: '☾ Noche' }[themeMode];
};

Occ.compute({ slots: {}, seen: [], overrides: {}, sim: CONFIG.sim });
for (const m of maps) m.lot.setOcc(Occ.map);
poll();
setInterval(poll, 1000);
