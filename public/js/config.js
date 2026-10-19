/* Código compartido por la app del conductor (index.html) y el panel (admin.html). */

const CONFIG = {
  metersPerPx: 0.2,                // escala del dibujo (≈ 0.2 m por píxel); se afina al calibrar con GPS
  reachPx: 40,                     // en modo GPS: a menos de esto del cajón (≈ 8 m) se da por llegado
  defaultLimitKmh: 20,
  // Los cajones sin sensor se simulan (misma ocupación en todos los dispositivos).
  // El administrador puede apagarla o cambiar el nivel desde el panel.
  sim: { on: true, level: 0.7 },
  entrance: { x: 250, y: 573 },    // se ajusta a MAP.nodes.E0 en graph.js
};

/* ---------- tema día / noche ---------- */
// Claro de día, oscuro de noche según la hora local. Se puede forzar desde ajustes.
const theme = {
  dayStart: 6.5, dayEnd: 18.5,     // horas locales (6:30 – 18:30)
  pref() { try { return localStorage.getItem('parkuni.theme') || 'auto'; } catch { return 'auto'; } },
  set(p) { try { localStorage.setItem('parkuni.theme', p); } catch {} theme.apply(); },
  resolve() {
    const p = theme.pref();
    if (p !== 'auto') return p;
    const d = new Date(), h = d.getHours() + d.getMinutes() / 60;
    return h >= theme.dayStart && h < theme.dayEnd ? 'light' : 'dark';
  },
  apply() {
    const t = theme.resolve();
    document.documentElement.dataset.theme = t;
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', t === 'dark' ? '#0b0b0b' : '#f4f4f4');
  },
};
theme.apply();
setInterval(theme.apply, 60000);

const $ = (id) => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
}
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

/* ---------- GPS -> mapa esquemático ---------- */
const geo = {
  get() {
    try { return JSON.parse(localStorage.getItem('parkuni.geo')) || {}; } catch { return {}; }
  },
  set(o) {
    try { localStorage.setItem('parkuni.geo', JSON.stringify({ ...geo.get(), ...o })); } catch {}
  },
  calibrated() { const g = geo.get(); return g.lat != null && g.lon != null; },
  // bearing = rumbo (° desde el norte, horario) hacia el que apunta el eje +x del mapa
  project(lat, lon) {
    const g = geo.get(), th = ((g.bearing ?? 90) * Math.PI) / 180;
    const e = (lon - g.lon) * 111320 * Math.cos((g.lat * Math.PI) / 180);
    const n = (lat - g.lat) * 110540;
    const xm = e * Math.sin(th) + n * Math.cos(th);
    const ym = e * Math.cos(th) - n * Math.sin(th);
    return { x: CONFIG.entrance.x + xm / CONFIG.metersPerPx, y: CONFIG.entrance.y + ym / CONFIG.metersPerPx };
  },
};

/* ---------- datos compartidos ---------- */
// Todo el estado (sensores, cajones forzados, simulación, límite, conductores, eventos, emergencia)
// vive en el servidor de ParkUni (server/server.py, base de datos SQLite). La app lo consulta en
// el mismo origen: api('/api/status'), api('/api/override', {...}), ...
async function api(path, body) {
  const r = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true,
  });
  return r.json();
}

function fmtTime(t) {
  return new Date(t * 1000).toLocaleTimeString('es-MX', { hour12: false });
}
