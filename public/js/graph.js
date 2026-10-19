/* Red de carriles, cajones, ruteo, instrucciones de giro y ocupación.
 * Depende de MAP (map-data.js). Sin acceso al DOM. */

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const angleOf = (dx, dy) => (Math.atan2(dy, dx) * 180) / Math.PI;          // 0° = este, horario (y hacia abajo)
const angleDiff = (a, b) => ((a - b + 540) % 360) - 180;                    // b → a, en (-180, 180]; >0 = derecha

const G = (() => {
  const nodes = MAP.nodes;
  const edges = MAP.edges.map((e) => {
    const pts = [nodes[e.a], ...e.mid, nodes[e.b]];
    const cum = [0];
    for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + dist(pts[i - 1], pts[i]));
    return { ...e, pts, cum, len: cum[cum.length - 1] };
  });
  const byId = Object.fromEntries(edges.map((e) => [e.id, e]));
  const out = {};                                   // nodo → carriles que salen
  for (const e of edges) (out[e.a] ||= []).push(e);

  /* ---- geometría sobre un carril ---- */
  function segIndex(e, s) {
    let i = 1;
    while (i < e.pts.length - 1 && e.cum[i] < s) i++;
    return i;
  }
  function pointAt(e, s) {
    s = clamp(s, 0, e.len);
    const i = segIndex(e, s), a = e.pts[i - 1], b = e.pts[i];
    const L = e.cum[i] - e.cum[i - 1] || 1, t = (s - e.cum[i - 1]) / L;
    return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, a: angleOf(b.x - a.x, b.y - a.y) };
  }
  // puntos del carril entre s0 y s1 (s0 ≤ s1)
  function slice(e, s0, s1) {
    const pts = [pointAt(e, s0)];
    for (let i = 1; i < e.pts.length - 1; i++) if (e.cum[i] > s0 + 0.01 && e.cum[i] < s1 - 0.01) pts.push(e.pts[i]);
    pts.push(pointAt(e, s1));
    return pts;
  }
  // rumbo del carril en s mirando `back` px hacia atrás (para el giro de entrada) o adelante
  const headingAt = (e, s, span, forward) => {
    const a = pointAt(e, forward ? s : Math.max(0, s - span)), b = pointAt(e, forward ? Math.min(e.len, s + span) : s);
    return angleOf(b.x - a.x, b.y - a.y);
  };

  /* ---- punto más cercano a la red ---- */
  function project(x, y, o = {}) {
    let best = null;
    const list = o.edges ? o.edges.map((id) => byId[id]) : edges.filter((e) => !e.dest);
    for (const e of list) {
      for (let i = 1; i < e.pts.length; i++) {
        const a = e.pts[i - 1], b = e.pts[i], dx = b.x - a.x, dy = b.y - a.y, L2 = dx * dx + dy * dy || 1;
        const t = clamp(((x - a.x) * dx + (y - a.y) * dy) / L2, 0, 1);
        const px = a.x + dx * t, py = a.y + dy * t;
        let cost = Math.hypot(x - px, y - py);
        if (o.heading != null && Math.abs(angleDiff(angleOf(dx, dy), o.heading)) > 100) cost += 40;
        if (!best || cost < best.cost) best = { edge: e.id, s: e.cum[i - 1] + t * Math.sqrt(L2), x: px, y: py, cost };
      }
    }
    return best;
  }

  /* ---- caminos más cortos entre nodos (con memoria) ---- */
  const memo = {};
  function distFrom(start) {
    if (memo[start]) return memo[start];
    const dist = { [start]: 0 }, prev = {}, done = new Set();
    for (;;) {
      let u = null;
      for (const k in dist) if (!done.has(k) && (u === null || dist[k] < dist[u])) u = k;
      if (u === null) break;
      done.add(u);
      for (const e of out[u] || []) {
        const nd = dist[u] + e.len;
        if (dist[e.b] === undefined || nd < dist[e.b]) { dist[e.b] = nd; prev[e.b] = e; }
      }
    }
    return (memo[start] = { dist, prev });
  }

  /* ---- ruta de una posición {edge, s} a un acceso {edge, s} ---- */
  function plan(pos, acc) {
    const eA = byId[pos.edge], eT = byId[acc.edge];
    if (pos.edge === acc.edge && acc.s >= pos.s - 0.5) return finish([{ edge: pos.edge, s0: pos.s, s1: acc.s }]);
    const D = distFrom(eA.b);
    if (D.dist[eT.a] === undefined) return null;
    const mid = [];
    for (let n = eT.a; n !== eA.b; ) { const e = D.prev[n]; mid.unshift(e); n = e.a; }
    return finish([
      { edge: pos.edge, s0: pos.s, s1: eA.len },
      ...mid.map((e) => ({ edge: e.id, s0: 0, s1: e.len })),
      { edge: acc.edge, s0: 0, s1: acc.s },
    ]);
  }
  const finish = (pieces) => ({ pieces, len: pieces.reduce((n, p) => n + (p.s1 - p.s0), 0) });

  // costo rápido de ir a muchos accesos desde la misma posición
  function coster(pos) {
    const eA = byId[pos.edge], D = distFrom(eA.b);
    return (acc) => {
      const eT = byId[acc.edge];
      if (pos.edge === acc.edge && acc.s >= pos.s - 0.5) return acc.s - pos.s;
      const d = D.dist[eT.a];
      return d === undefined ? Infinity : eA.len - pos.s + d + acc.s;
    };
  }

  /* ---- nodos de salida de la rotonda (para contar "toma la 2ª salida") ---- */
  const isExitNode = (n) => (out[n] || []).some((e) => !e.ring);

  /* ---- instrucciones de giro ---- */
  const GLYPH = (d) => (Math.abs(d) > 150 ? '↶' : d > 25 ? '→' : d < -25 ? '←' : '↑');
  function maneuvers(route, hop, spotName) {
    const list = [], P = route.pieces;
    let d = 0;
    for (let i = 0; i < P.length; i++) {
      d += P[i].s1 - P[i].s0;
      if (i === P.length - 1) break;
      const cur = byId[P[i].edge], nxt = byId[P[i + 1].edge];
      const delta = angleDiff(headingAt(nxt, 0, 14, true), headingAt(cur, P[i].s1, 14, false));
      const key = `${cur.id}>${nxt.id}`;
      if (cur.ring && nxt.ring) continue;
      if (!cur.ring && nxt.ring) {
        let k = 0, exitName = '';
        for (let j = i + 1; j < P.length && byId[P[j].edge].ring; j++) {
          const e = byId[P[j].edge];
          if (isExitNode(e.b)) k++;
          if (j + 1 < P.length && !byId[P[j + 1].edge].ring) { exitName = byId[P[j + 1].edge].name; break; }
        }
        list.push({ d, key, glyph: '⟲', verb: `Entra a la rotonda y toma la salida ${k || 1}`,
          text: exitName ? `Entra a la rotonda, salida ${k || 1} hacia ${exitName}` : 'Entra a la rotonda' });
        continue;
      }
      if (cur.ring && !nxt.ring) {
        list.push({ d, key, glyph: GLYPH(delta), verb: 'Sal de la rotonda', text: `Sal de la rotonda hacia ${nxt.name}` });
        continue;
      }
      if (Math.abs(delta) < 25) {
        if (cur.name !== nxt.name) list.push({ d, key, glyph: '↑', verb: 'Sigue', text: `Sigue por ${nxt.name}`, minor: true });
        continue;
      }
      const word = Math.abs(delta) > 150 ? 'Da la vuelta' : delta > 0 ? 'Gira a la derecha' : 'Gira a la izquierda';
      list.push({ d, key, glyph: GLYPH(delta), verb: word, text: `${word} hacia ${nxt.name}` });
    }
    // llegada
    let text = `Estaciona: ${spotName}`, glyph = '🅿', verb = 'Estaciona';
    if (hop) {
      const last = byId[P[P.length - 1].edge];
      const delta = angleDiff(angleOf(hop.to.x - hop.from.x, hop.to.y - hop.from.y), headingAt(last, P[P.length - 1].s1, 14, false));
      const side = Math.abs(delta) < 35 ? 'al frente' : delta > 0 ? 'a la derecha' : 'a la izquierda';
      text = `${spotName} está ${side}`; glyph = Math.abs(delta) < 35 ? '↑' : GLYPH(delta); verb = `Tu cajón está ${side}`;
    }
    list.push({ d, key: 'arrive', glyph, verb, text, arrive: true });
    return list;
  }

  return { nodes, edges, byId, pointAt, slice, headingAt, project, distFrom, plan, coster, maneuvers };
})();

/* =================== cajones =================== */
const SPOTS = [];
const SPOT_BY_ID = {};
const OVERFLOW = MAP.overflow.map((o) => ({ ...o, access: { edge: o.edge, s: G.byId[o.edge].len } }));
const ZONES = [...new Set(MAP.segments.map((s) => s.zone))];
const spotSensor = Object.fromEntries(Object.entries(MAP.sensors).map(([sid, spot]) => [spot, sid]));

for (const seg of MAP.segments) {
  const pts = seg.pts, cum = [0];
  for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + dist(pts[i - 1], pts[i]));
  const len = cum[cum.length - 1], gap = len / seg.n;
  for (let k = 0; k < seg.n; k++) {
    const s = (k + 0.5) * gap;
    let i = 1;
    while (i < pts.length - 1 && cum[i] < s) i++;
    const a = pts[i - 1], b = pts[i], L = cum[i] - cum[i - 1] || 1, t = (s - cum[i - 1]) / L;
    const x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t;
    let tx = (b.x - a.x) / L, ty = (b.y - a.y) / L;
    const access = G.project(x, y, seg.lane ? { edges: [seg.lane] } : {});
    // la normal apunta hacia el carril que atiende al cajón
    let nx = -ty, ny = tx;
    if (nx * (access.x - x) + ny * (access.y - y) < 0) { nx = -nx; ny = -ny; }
    const id = `${seg.id}-${k + 1}`;
    const spot = { id, seg: seg.id, zone: seg.zone, idx: k + 1, x, y, tx, ty, nx, ny, w: gap,
      ang: seg.ang || '', type: seg.type || 'normal', sensor: spotSensor[id] || null, access };
    SPOTS.push(spot);
    SPOT_BY_ID[id] = spot;
  }
}
const SINGLE_ZONE = ZONES.length === 1;                       // el prototipo: un solo pasillo, sin filas
const spotName = (sp) => (SINGLE_ZONE ? `Cajón ${sp.idx}` : `${sp.zone}, fila ${sp.seg}, cajón ${sp.idx}`);
const spotSay = (sp) => (SINGLE_ZONE ? `el cajón ${sp.idx}` : `${sp.zone}, fila ${sp.seg}, cajón ${sp.idx}`);
const spotShort = (sp) => (SINGLE_ZONE ? `${sp.idx}` : `${sp.seg} · ${sp.idx}`);

/* =================== ocupación =================== */
// Prioridad: sensor real > manual del administrador > simulación determinista por hora.
// La simulación es la misma en todos los dispositivos sin necesidad de un servidor.
function hash01(str) {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0) / 4294967296;
}
function simOcc(id, now, level) {
  const period = 240000 * (0.6 + hash01(id + 'p') * 0.8);
  const bucket = Math.floor((now + hash01(id + 'o') * period) / period);
  return hash01(id + ':' + bucket) < level;
}

const Occ = {
  map: new Map(),                       // id → { occ: true|false|null, src }
  counts: {},                           // zona → { free, total }
  total: { free: 0, total: 0 },
  compute(st, now = Date.now()) {
    const seen = new Set(st.seen || []);
    this.map.clear();
    this.counts = Object.fromEntries(ZONES.map((z) => [z, { free: 0, total: 0 }]));
    this.total = { free: 0, total: 0 };
    for (const sp of SPOTS) {
      let r;
      if (sp.sensor && seen.has(sp.sensor)) r = { occ: !!st.slots[sp.sensor], src: 'sensor' };
      else if (st.overrides && sp.id in st.overrides) r = { occ: !!st.overrides[sp.id], src: 'manual' };
      else if (MAP.simulate && st.sim && st.sim.on) r = { occ: simOcc(sp.id, now, st.sim.level), src: 'sim' };
      else r = { occ: null, src: 'none' };
      this.map.set(sp.id, r);
      if (sp.type === 'disabled' || r.occ === null) continue;
      const c = this.counts[sp.zone];
      c.total++; this.total.total++;
      if (!r.occ) { c.free++; this.total.free++; }
    }
  },
  get(id) { return this.map.get(id) || { occ: null, src: 'none' }; },
  isFree(id) { return this.get(id).occ === false; },
};

/* =================== elección de destino =================== */
// Mejor cajón libre desde `pos` (el de menor recorrido). Si no hay, la zona de último recurso más cercana.
function bestTarget(pos, excludeId) {
  const cost = G.coster(pos);
  let best = null, bestC = Infinity;
  for (const sp of SPOTS) {
    if (sp.type === 'disabled' || sp.id === excludeId || !Occ.isFree(sp.id)) continue;
    const c = cost(sp.access);
    if (c < bestC) { best = sp; bestC = c; }
  }
  if (best) return { kind: 'spot', id: best.id };
  let o = null, oc = Infinity;
  for (const z of OVERFLOW) { const c = cost(z.access); if (c < oc) { o = z; oc = c; } }
  return o ? { kind: 'overflow', id: o.id } : null;
}

// Ruta completa hacia un destino: carriles + último tramo hasta el cajón + instrucciones.
function planTarget(pos, target) {
  let acc, hop = null, name, goal;
  if (target.kind === 'spot') {
    const sp = SPOT_BY_ID[target.id];
    acc = sp.access; goal = { x: sp.x, y: sp.y }; name = spotName(sp);
    const a = G.pointAt(G.byId[acc.edge], acc.s);
    if (dist(a, goal) > 1) hop = { from: { x: a.x, y: a.y }, to: goal };
  } else {
    const z = OVERFLOW.find((o) => o.id === target.id);
    acc = z.access; name = z.name;
    const a = G.pointAt(G.byId[acc.edge], acc.s);
    goal = { x: a.x, y: a.y };
  }
  const r = G.plan(pos, acc);
  if (!r) return null;
  const pts = [];
  for (const p of r.pieces) {
    const seg = G.slice(G.byId[p.edge], p.s0, p.s1);
    for (const q of seg) if (!pts.length || dist(pts[pts.length - 1], q) > 0.3) pts.push({ x: q.x, y: q.y });
  }
  if (hop) pts.push(hop.to);
  const hopLen = hop ? dist(hop.from, hop.to) : 0;
  return { pieces: r.pieces, hop, hopLen, pts, len: r.len + hopLen, goal, name, maneuvers: G.maneuvers(r, hop, name) };
}
