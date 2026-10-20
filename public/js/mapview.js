/* Dibujo del mapa (SVG) a partir de MAP / G / SPOTS. Lo usan la app del conductor y el panel. */

// only: dibuja solo los cajones de esa zona (para las vistas ampliadas del panel)
function drawMap(svg, { titles = false, only = null } = {}) {
  const ST = MAP.style, SPOT_DEPTH = ST.depth;
  const world = el('g', {}, svg);
  const pl = (pts) => pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');

  el('rect', { class: 'ground', x: -4000, y: -4000, width: 9000, height: 9000 }, world);
  for (const poly of MAP.surfaces) el('polygon', { class: 'lot', points: pl(poly) }, world);

  // carriles: la unión de los carriles dibuja las calles
  for (const e of G.edges) el('polyline', { class: 'road', points: pl(e.pts), 'stroke-width': e.dest ? ST.lane * 0.72 : ST.lane }, world);
  if (MAP.ring.r) el('circle', { class: 'island', cx: MAP.ring.cx, cy: MAP.ring.cy, r: 56 }, world);

  for (const b of MAP.buildings) el('rect', { class: 'building', x: b.x, y: b.y, width: b.w, height: b.h, rx: 6 }, world);
  for (const [x, y, w, h] of MAP.crosswalks) el('rect', { class: 'cross', x, y, width: w, height: h, rx: 1.5 }, world);
  for (const pts of MAP.nopark) el('polyline', { class: 'nopark', points: pl(pts) }, world);
  for (const pts of MAP.busbay) el('polyline', { class: 'busbay', points: pl(pts) }, world);

  // flechas de sentido sobre los carriles
  const chev = el('g', { class: 'chevrons' }, world);
  for (const e of G.edges) {
    if (e.len < 50) continue;
    const n = Math.max(1, Math.floor(e.len / ST.chevGap));
    for (let i = 0; i < n; i++) {
      const p = G.pointAt(e, ((i + 0.5) / n) * e.len);
      el('path', { d: 'M-5 -5 L4 0 L-5 5', transform: `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)}) rotate(${p.a.toFixed(0)}) scale(${(ST.lane / 42).toFixed(2)})` }, chev);
    }
  }

  // cajones
  const spotEls = {}, spotLayer = el('g', { class: 'spots' }, world), leds = el('g', {}, world);
  for (const sp of SPOTS) {
    if (only && sp.zone !== only) continue;
    const w = Math.min(sp.w * 0.8, ST.maxW), cx = sp.x + sp.nx * (SPOT_DEPTH / 2), cy = sp.y + sp.ny * (SPOT_DEPTH / 2);
    let k = 0;
    if (sp.ang) {
      const want = sp.ang === '/' ? -1 : 1;                              // signo de x·y de la raya
      const t = (kk) => (sp.nx + kk * sp.tx) * (sp.ny + kk * sp.ty);
      k = Math.sign(t(0.6)) === want ? 0.6 : -0.6;
    }
    const m = [sp.tx, sp.ty, sp.nx + k * sp.tx, sp.ny + k * sp.ty, cx, cy].map((v) => v.toFixed(3)).join(' ');
    const r = el('rect', { class: 'spot none' + (sp.type === 'disabled' ? ' dis' : ''), x: -w / 2, y: -SPOT_DEPTH / 2,
      width: w, height: SPOT_DEPTH, rx: 1.5, transform: `matrix(${m})` }, spotLayer);
    if (titles) el('title', {}, r).textContent = `${sp.id} · ${sp.zone}${sp.sensor ? ' · sensor ' + sp.sensor : ''}`;
    r.dataset.id = sp.id;
    spotEls[sp.id] = { el: r, cls: '' };
    if (sp.sensor) el('circle', { class: 'sensor-dot', cx, cy, r: 3.5 * ST.lane / 42 }, leds);
  }

  // vista lejana: una línea por fila, coloreada por disponibilidad
  const aggLayer = el('g', { class: 'aggs' }, world), aggEls = {};
  const labels = [];
  for (const seg of MAP.segments) {
    if (only && seg.zone !== only) continue;
    const line = el('polyline', { class: 'agg n', points: pl(seg.pts) }, aggLayer);
    aggEls[seg.id] = line;
  }
  const segTotals = {};
  for (const sp of SPOTS) (segTotals[sp.seg] ||= []).push(sp.id);

  // textos del mapa (se contra-rotan en la vista del conductor)
  const lay = el('g', { class: 'labels' }, world);
  for (const b of MAP.buildings) {
    const t = el('text', { class: 'blabel', x: b.x + b.w / 2, y: b.y + b.h / 2 }, lay);
    t.textContent = b.label;
    labels.push({ node: t, x: b.x + b.w / 2, y: b.y + b.h / 2 });
  }
  for (const l of MAP.labels) {
    if (only && /^(ZONA|ACCESO)/.test(l.t) && l.t !== only.toUpperCase()) continue;   // solo el rótulo de su zona
    const t = el('text', { class: 'mlabel ' + (l.c || '') + (l.big ? ' big' : ''), x: l.x, y: l.y }, lay);
    t.textContent = l.t;
    labels.push({ node: t, x: l.x, y: l.y });
  }

  // marcador del cajón asignado
  const pin = el('g', { class: 'pin', style: 'display:none' }, world);
  el('circle', { class: 'pin-ring', r: 18 }, pin);
  el('circle', { class: 'pin-dot', r: 6 }, pin);

  return {
    world, spotEls, labels, pin,
    far(on) { svg.classList.toggle('far', on); },
    emergency(on) { svg.classList.toggle('emergency', !!on); },
    // map: Occ.map  (id → { occ, src })
    setOcc(map) {
      this.lastMap = map;
      for (const sp of SPOTS) {
        const o = map.get(sp.id) || { occ: null, src: 'none' };
        const cls = 'spot ' + (o.occ === null ? 'none' : o.occ ? 'busy' : 'free') + (o.src === 'sim' ? ' sim' : '')
          + (sp.type === 'disabled' ? ' dis' : '') + (sp.sensor ? ' hw' : '') + (this._resv.has(sp.id) ? ' resv' : '') + (sp.id === this._target ? ' target' : '');
        const s = spotEls[sp.id];
        if (!s) continue;
        if (s.cls !== cls) { s.el.setAttribute('class', cls); s.cls = cls; }
      }
      for (const seg of MAP.segments) {
        if (!aggEls[seg.id]) continue;
        let free = 0, total = 0;
        for (const id of segTotals[seg.id]) {
          const o = map.get(id);
          if (!o || o.occ === null) continue;
          total++; if (!o.occ) free++;
        }
        const r = total ? free / total : -1;
        aggEls[seg.id].setAttribute('class', 'agg ' + (r < 0 ? 'n' : r >= 0.35 ? 'g' : r >= 0.1 ? 'y' : 'r'));
      }
    },
    setTarget(spot) {
      this._target = spot ? spot.id : null;
      pin.style.display = spot ? '' : 'none';
      if (spot) pin.setAttribute('transform', `translate(${spot.x + spot.nx * ST.depth / 2} ${spot.y + spot.ny * ST.depth / 2}) scale(${ST.lane / 42})`);
    },
    // cajones que otros conductores ya van a ocupar (se dibujan con borde punteado)
    setReserved(set) { this._resv = set; if (this.lastMap) this.setOcc(this.lastMap); },
    _resv: new Set(),
    _target: null,
  };
}
