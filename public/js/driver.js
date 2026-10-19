/* App del conductor (estilo Waze): asigna cajón, guía por carriles reales del campus, redirige
 * y vigila la velocidad.
 * Modo demo: el auto avanza solo a la velocidad del control deslizante.
 * Modo GPS: posición y velocidad vienen del celular (requiere https o localhost). */

CONFIG.entrance = MAP.nodes.E0;

const S = {
  st: { slots: {}, seen: [], overrides: {}, sim: CONFIG.sim },   // último /api/status
  limit: CONFIG.defaultLimitKmh,
  online: false,
  mode: 'demo',                    // 'demo' | 'gps'
  nav: null,                       // { target: {kind, id}, arrived, said }
  route: null,
  pos: { ...MAP.entry },           // posición sobre la red de carriles { edge, s }
  car: { x: 0, y: 0 },
  heading: 0,                      // ° en el mapa, 0 = este, horario
  hopT: 0,                         // avance del último tramo (del carril al cajón)
  speed: 0,                        // km/h
  demoSpeed: MAP.demoSpeed ?? 20,
  view: 'follow',                  // 'follow' | 'overview'
  voice: true,
  emergency: false,
  id: Math.random().toString(36).slice(2, 8),
};
S.car = G.pointAt(G.byId[S.pos.edge], S.pos.s);
S.heading = S.car.a;

const svg = $('map');
const lot = drawMap(svg);
const routeLine = el('polyline', { id: 'route', points: '' });
lot.world.insertBefore(routeLine, lot.world.querySelector('.spots'));
const carMark = el('g', {}, lot.world);
const carScale = MAP.style.lane / 42;
el('circle', { r: 22, fill: 'var(--target)', opacity: 0.22 }, carMark);
el('path', { d: 'M18 0 L-12 -12 L-5 0 L-12 12 Z', fill: 'var(--target)', stroke: '#fff', 'stroke-width': 3, 'stroke-linejoin': 'round' }, carMark);

const setText = (id, v) => { const n = $(id); if (n.textContent !== String(v)) n.textContent = v; };

/* ---------- cámara ---------- */
const cam = { fx: S.car.x, fy: S.car.y, R: -90 - S.heading, z: 1, ax: 0, ay: 0 };
const view = { w: 0, h: 0 };
function resize() { view.w = svg.clientWidth; view.h = svg.clientHeight; }
addEventListener('resize', resize);
resize();

function camTarget() {
  if (S.view === 'overview' || !S.nav) {
    const b = MAP.bounds;
    const z = Math.min(view.w / b.w, (view.h * 0.55) / b.h) * 0.95;
    return { fx: b.x + b.w / 2, fy: b.y + b.h / 2, R: 0, z, ax: view.w / 2, ay: view.h * 0.3 };
  }
  return { fx: S.car.x, fy: S.car.y, R: -90 - S.heading, z: MAP.style.followZoom, ax: view.w / 2, ay: view.h * 0.62 };
}
function stepCamera(dt, snap) {
  const t = camTarget(), k = snap ? 1 : 1 - Math.exp(-dt * 5);
  cam.R += angleDiff(t.R, cam.R) * k;
  for (const p of ['fx', 'fy', 'z', 'ax', 'ay']) cam[p] += (t[p] - cam[p]) * k;
  lot.world.setAttribute('transform',
    `translate(${cam.ax} ${cam.ay}) rotate(${cam.R}) scale(${cam.z}) translate(${-cam.fx} ${-cam.fy})`);
  for (const l of lot.labels) l.node.setAttribute('transform', `rotate(${-cam.R} ${l.x} ${l.y})`);
  lot.far(cam.z * MAP.style.depth < 9);       // muy lejos: una línea de color por fila
  carMark.setAttribute('transform', `translate(${S.car.x} ${S.car.y}) rotate(${S.heading}) scale(${carScale})`);
}

/* ---------- avisos: voz, sonido, toast ---------- */
let audio;
function beep() {
  try {
    audio ||= new (window.AudioContext || window.webkitAudioContext)();
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = 880; g.gain.value = 0.12;
    o.connect(g); g.connect(audio.destination);
    o.start(); o.stop(audio.currentTime + 0.18);
  } catch {}
}

/* Voz con prioridades: emergency (4) > speed (3) > reroute (2) > guide (1).
 * - speed interrumpe todo; reroute espera a que termine speed.
 * - guide nunca hace cola: se descarta si hay exceso de velocidad, si hay un aviso de mayor
 *   prioridad hablando/pendiente, o durante GUIDE_HOLD_MS tras un "redirigiendo", para que
 *   el conductor pueda orillarse sin que le sigan dando indicaciones. */
const VOICE_PRI = { guide: 1, reroute: 2, speed: 3, emergency: 4 };
const GUIDE_HOLD_MS = 10000;
const voice = { cur: null, queue: [], holdUntil: 0, speeding: false, token: 0 };

function voicePlay(item) {
  try { speechSynthesis.cancel(); } catch {}
  const token = ++voice.token;
  voice.cur = { kind: item.kind, pri: VOICE_PRI[item.kind], token };
  const done = () => {
    if (!voice.cur || voice.cur.token !== token) return;   // utterance ya reemplazada
    voice.cur = null;
    if (item.kind === 'reroute') voice.holdUntil = Date.now() + GUIDE_HOLD_MS;
    voiceNext();
  };
  const u = new SpeechSynthesisUtterance(item.msg);
  u.lang = 'es-MX';
  u.onend = done; u.onerror = done;
  setTimeout(done, 9000);                                   // por si el navegador no dispara onend
  try { speechSynthesis.speak(u); } catch { done(); }
}
function voiceNext() {
  if (!voice.queue.length) return;
  voice.queue.sort((a, b) => VOICE_PRI[b.kind] - VOICE_PRI[a.kind]);
  voicePlay(voice.queue.shift());
}
function voiceStop() {
  voice.queue = []; voice.cur = null; voice.token++;
  try { speechSynthesis.cancel(); } catch {}
}
// Devuelve true si el aviso se aceptó (habla ahora o queda en cola).
function speak(msg, kind = 'guide') {
  if (!S.voice || !('speechSynthesis' in window)) return false;
  const pri = VOICE_PRI[kind], cur = voice.cur;
  if (kind === 'guide') {
    if (voice.speeding || Date.now() < voice.holdUntil || voice.queue.length || (cur && cur.pri > pri)) return false;
    voicePlay({ msg, kind }); return true;
  }
  if (kind === 'reroute') {
    voice.holdUntil = Date.now() + GUIDE_HOLD_MS;           // calla la guía desde ya
    if (cur && cur.pri < pri) { voicePlay({ msg, kind }); return true; }
  }
  if (kind === 'speed' || kind === 'emergency') {
    if (cur && cur.kind === kind) return false;             // ya está advirtiendo
    voice.queue = voice.queue.filter((q) => q.kind !== kind);
    voicePlay({ msg, kind }); return true;                  // interrumpe lo que sea
  }
  if (cur) voice.queue.push({ msg, kind }); else voicePlay({ msg, kind });
  return true;
}

let toastT;
function toast(msg, bad) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('bad', !!bad);
  t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; }, 4500);
}
function sendEvent(type, msg) { api('/api/event', { type, msg }).catch(() => {}); }

/* ---------- destinos ---------- */
const targetSpot = (t) => (t.kind === 'spot' ? SPOT_BY_ID[t.id] : null);
const targetLabel = (t) => (t.kind === 'spot' ? spotName(SPOT_BY_ID[t.id]) : OVERFLOW.find((o) => o.id === t.id).name);
const targetSay = (t) => (t.kind === 'spot' ? spotSay(SPOT_BY_ID[t.id]) : OVERFLOW.find((o) => o.id === t.id).name);

function setTarget(t) {
  S.nav = { target: t, arrived: false, said: {} };
  S.hopT = 0;
  lot.setTarget(targetSpot(t));
  lot.setOcc(Occ.map);
  renderStatic();
}

function startNav() {
  beep();                                                 // desbloquea el audio con el gesto del usuario
  if (S.mode === 'demo' && !S.nav) resetCar();
  const t = bestTarget(S.pos);
  if (!t) { toast('Estacionamiento lleno por ahora', true); speak('El estacionamiento está lleno', 'reroute'); return; }
  setTarget(t);
  S.view = 'follow';
  if (t.kind === 'spot') speak(`Te asignamos ${targetSay(t)}`);
  else { toast('No hay cajones libres: te enviamos a ' + targetLabel(t), true); speak(`No hay cajones libres. Te enviamos a ${targetLabel(t)}`, 'reroute'); }
  sendEvent('nav', `Conductor ${S.id} → ${t.id}`);
}

function reroute(lostId) {
  const t = bestTarget(S.pos, lostId);
  if (!t) {
    endNav();
    toast(`Tu cajón ${lostId} fue ocupado y ya no quedan lugares`, true);
    speak('Tu cajón fue ocupado y ya no quedan lugares libres', 'reroute');
    return;
  }
  setTarget(t);
  if (t.kind === 'spot') {
    toast(`Tu cajón ${lostId} fue ocupado. Nuevo destino: ${t.id}`, true);
    speak(`Tu cajón fue ocupado. Redirigiendo a ${targetSay(t)}`, 'reroute');
  } else {
    toast(`Tu cajón ${lostId} fue ocupado y no quedan lugares. Te enviamos a ${targetLabel(t)}`, true);
    speak(`Tu cajón fue ocupado y no quedan lugares libres. Te enviamos a ${targetLabel(t)}`, 'reroute');
  }
  sendEvent('reroute', `Conductor ${S.id}: ${lostId} ocupado → ${t.id}`);
}

function endNav() {
  S.nav = null; S.route = null;
  voiceStop();
  lot.setTarget(null);
  lot.setOcc(Occ.map);
  api('/api/driver/leave', { id: S.id }).catch(() => {});
  renderStatic();
}

function resetCar() {
  S.pos = { ...MAP.entry };
  S.car = G.pointAt(G.byId[S.pos.edge], S.pos.s);
  S.heading = S.car.a;
  S.hopT = 0;
}

/* ---------- navegación (cada cuadro) ---------- */
function updateNav() {
  const n = S.nav;
  if (!n) return;
  const route = (S.route = planTarget(S.pos, n.target));
  if (!route) { endNav(); toast('Perdí la ruta, vuelve a buscar', true); return; }

  if (!n.arrived && S.mode === 'gps' && route.len <= CONFIG.reachPx) n.arrived = true;
  if (n.arrived && !n.announced) {
    n.announced = true;
    toast(`Llegaste: ${targetLabel(n.target)}`);
    speak(`Llegaste. Estaciona en ${targetSay(n.target)}`);
    renderStatic();
  }

  const mpp = CONFIG.metersPerPx;
  const ins = route.maneuvers[0];
  const m = Math.round(ins.d * mpp);
  if (n.arrived) {
    $('banner').hidden = false;
    setText('bArrow', '🅿'); setText('bDist', 'Llegaste'); setText('bText', targetLabel(n.target));
    routeLine.setAttribute('points', '');
  } else {
    $('banner').hidden = false;
    setText('bArrow', ins.glyph);
    setText('bDist', ins.arrive && m < 3 ? 'Aquí' : `${m < 100 ? m : Math.round(m / 5) * 5} m`);
    setText('bText', ins.text);
    routeLine.setAttribute('points', [S.car, ...route.pts].map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '));
    // voz de guía: aviso anticipado y aviso inmediato (la prioridad la decide speak())
    const say = (suffix, msg) => { if (!n.said[ins.key + suffix] && speak(msg)) n.said[ins.key + suffix] = true; };
    if (!ins.minor) {
      if (ins.arrive) { if (m <= 30) say(':a', ins.verb); }
      else {
        if (m <= 60) say(':a', `En ${m < 30 ? Math.max(5, Math.round(m / 5) * 5) : Math.round(m / 10) * 10} metros, ${ins.verb.toLowerCase()}`);
        if (m <= 15) say(':b', ins.verb);
      }
    }
  }
  const len = route.len * mpp;
  setText('nMin', Math.round(len));
  const v = Math.max(S.speed, S.limit * 0.6) / 3.6;      // ETA con velocidad mínima razonable
  setText('nEta', n.arrived ? 0 : Math.round(len / v));
}

/* ---------- velocidad ---------- */
let lastOverAt = 0, lastReport = 0;
function updateSpeed() {
  const kmh = Math.round(S.speed);
  const over = S.speed > S.limit + 1;
  voice.speeding = over && !!S.nav;
  setText('spd', kmh);
  setText('lim', S.limit);
  $('speedo').classList.toggle('over', over);
  $('speedAlert').hidden = !over;
  setText('aLimit', S.limit);
  $('banner').classList.toggle('warn', over && !!S.nav);
  const now = Date.now();
  if (over && S.nav && now - lastOverAt > 8000) {
    lastOverAt = now;
    beep();
    try { navigator.vibrate?.(200); } catch {}
    speak(`Reduce la velocidad. El límite es ${S.limit}`, 'speed');
    if (now - lastReport > 20000) {
      lastReport = now;
      sendEvent('speeding', `Exceso: ${kmh} km/h (límite ${S.limit}) · conductor ${S.id}`);
    }
  }
}

/* ---------- render de estado ---------- */
function renderStatic() {
  setText('idleTitle', MAP.title);
  setText('freeN', Occ.total.free);
  $('freeN').parentElement.classList.toggle('full', Occ.total.free === 0);
  $('areas').innerHTML = ZONES.map((z) => {
    const c = Occ.counts[z];
    return `<span class="chip ${c.free ? '' : 'zero'}">${z.replace('Zona ', '')}: <b>${c.free}</b>/${c.total}</span>`;
  }).join('');

  $('sheetIdle').hidden = !!S.nav;
  $('sheetNav').hidden = !S.nav;
  $('btnView').hidden = !S.nav;
  $('demoSpeed').hidden = S.mode !== 'demo';
  if (!S.nav) { $('banner').hidden = true; routeLine.setAttribute('points', ''); $('speedAlert').hidden = true; }
  else {
    const t = S.nav.target, sp = targetSpot(t);
    setText('nArea', sp ? sp.zone : 'Último recurso');
    setText('nSlot', sp ? spotShort(sp) : targetLabel(t));
    setText('nNote', !sp ? 'Sin sensores en esta zona: confirma al llegar.'
      : Occ.get(sp.id).src === 'sensor' ? '● Cajón con sensor en vivo' : 'Disponibilidad simulada (demo)');
    $('btnPark').hidden = !S.nav.arrived;
  }
  $('modeHint').textContent = S.mode === 'demo'
    ? 'Modo demo: el auto avanza solo. Ajusta la velocidad con el control.'
    : geo.calibrated() ? 'Modo GPS activo.' : 'Modo GPS: velocidad real; calibra la entrada en ajustes para ver tu posición.';
  $('btnVoice').textContent = S.voice ? '🔊 Voz' : '🔇 Voz';
}

/* ---------- movimiento en demo ---------- */
function moveDemo(dt) {
  const n = S.nav, route = S.route;
  if (!n || n.arrived || !route) { S.speed = 0; return; }
  S.speed = S.demoSpeed;
  let step = (S.speed / 3.6 / CONFIG.metersPerPx) * dt;
  const prev = { x: S.car.x, y: S.car.y };
  const pieces = route.pieces.map((p) => ({ ...p }));
  while (step > 0 && pieces.length) {
    const p = pieces[0], L = p.s1 - p.s0;
    if (L <= step) { step -= L; S.pos = { edge: p.edge, s: p.s1 }; pieces.shift(); if (L > 0.01) S.hopT = 0; }
    else { S.pos = { edge: p.edge, s: p.s0 + step }; step = 0; }
  }
  const onHop = !pieces.length && route.hop;
  if (onHop) {
    S.hopT = Math.min(route.hopLen, S.hopT + step);
    const t = route.hopLen ? S.hopT / route.hopLen : 1;
    S.car = { x: route.hop.from.x + (route.hop.to.x - route.hop.from.x) * t, y: route.hop.from.y + (route.hop.to.y - route.hop.from.y) * t };
  } else {
    const p = G.pointAt(G.byId[S.pos.edge], S.pos.s);
    S.car = { x: p.x, y: p.y };
  }
  if (!pieces.length && (!route.hop || S.hopT >= route.hopLen - 0.1)) n.arrived = true;
  const dx = S.car.x - prev.x, dy = S.car.y - prev.y;
  if (Math.hypot(dx, dy) > 0.15) S.heading += angleDiff(angleOf(dx, dy), S.heading) * 0.4;
}

/* ---------- modo GPS ---------- */
let watchId = null, prevFix = null;
function startGps() {
  if (!('geolocation' in navigator)) return gpsMsg('Este dispositivo no tiene geolocalización.');
  if (!window.isSecureContext) return gpsMsg('El GPS requiere https (en Cloudflare ya lo es; en local: python3 tools/server.py --https).');
  stopGps();
  watchId = navigator.geolocation.watchPosition(onFix, (e) => gpsMsg('GPS: ' + e.message), {
    enableHighAccuracy: true, maximumAge: 500, timeout: 10000,
  });
  gpsMsg('Buscando señal GPS…');
}
function stopGps() {
  if (watchId != null) navigator.geolocation.clearWatch(watchId);
  watchId = null; prevFix = null;
}
function gpsMsg(m) { $('gpsStatus').textContent = m; }

function onFix(pos) {
  const c = pos.coords;
  let kmh = c.speed != null && !Number.isNaN(c.speed) ? c.speed * 3.6 : null;
  if (kmh == null && prevFix) {                       // velocidad por diferencia de posiciones
    const dtS = (pos.timestamp - prevFix.timestamp) / 1000;
    if (dtS > 0) {
      const a = geo.project(c.latitude, c.longitude);
      const b = geo.project(prevFix.coords.latitude, prevFix.coords.longitude);
      kmh = (dist(a, b) * CONFIG.metersPerPx / dtS) * 3.6;
    }
  }
  S.speed = Math.max(0, kmh ?? 0);
  prevFix = pos;
  gpsMsg(`GPS ±${Math.round(c.accuracy)} m · ${Math.round(S.speed)} km/h`);
  if (!geo.calibrated()) return;
  const raw = geo.project(c.latitude, c.longitude);
  const sn = G.project(raw.x, raw.y, { heading: S.heading });
  const next = sn.cost < 80 ? { x: sn.x, y: sn.y } : raw;      // se "pega" a la calle si está cerca
  if (dist(next, S.car) > 3) S.heading += angleDiff(angleOf(next.x - S.car.x, next.y - S.car.y), S.heading) * 0.6;
  S.car = next;
  S.pos = { edge: sn.edge, s: sn.s };
}

function setMode(m) {
  S.mode = m;
  $('mDemo').classList.toggle('on', m === 'demo');
  $('mGps').classList.toggle('on', m === 'gps');
  $('gpsBox').hidden = m !== 'gps';
  if (m === 'gps') startGps(); else { stopGps(); S.speed = 0; }
  if (S.nav) S.nav.arrived = false;
  renderStatic();
}

/* ---------- datos compartidos ---------- */
function applyEmergency(em) {
  const on = !!(em && em.on);
  if (on === S.emergency) return;
  S.emergency = on;
  $('emBanner').hidden = !on;
  lot.emergency(on);
  if (on) { beep(); try { navigator.vibrate?.([300, 150, 300]); } catch {} speak('Emergencia. Precaución. Sigue las indicaciones del personal.', 'emergency'); }
}
function applyStatus(st) {
  S.st = st;
  applyEmergency(st.emergency);
  S.limit = st.speedLimit ?? S.limit;
  Occ.compute(st);
  lot.setOcc(Occ.map);
  const n = S.nav;
  if (n && !n.arrived) {
    if (n.target.kind === 'spot' && !Occ.isFree(n.target.id)) { reroute(n.target.id); return; }
    if (n.target.kind === 'overflow') {                 // ¿se liberó algo mientras voy a la zona de último recurso?
      const b = bestTarget(S.pos);
      if (b && b.kind === 'spot') {
        setTarget(b);
        toast(`Se liberó un cajón: ${targetLabel(b)}`);
        speak(`Se liberó un cajón. Redirigiendo a ${targetSay(b)}`, 'reroute');
        return;
      }
    }
  }
  renderStatic();
}
async function poll() {
  try { S.online = true; applyStatus(await api('/api/status')); } catch { S.online = false; }
}
setInterval(poll, 1000);
setInterval(() => {
  if (S.nav) api('/api/driver', { id: S.id, target: S.nav.target.id, x: S.car.x, y: S.car.y, speed: S.speed }).catch(() => {});
}, 1000);
addEventListener('pagehide', () => { api('/api/driver/leave', { id: S.id }).catch(() => {}); });

/* ---------- eventos de UI ---------- */
$('btnGo').onclick = startNav;
$('btnCancel').onclick = endNav;
$('btnPark').onclick = () => {
  const t = S.nav.target;
  sendEvent('park', `Conductor ${S.id} estacionó en ${t.id}`);
  toast(`¡Listo! ${targetLabel(t)}`);
  endNav();
};
$('btnVoice').onclick = () => { S.voice = !S.voice; if (!S.voice) voiceStop(); renderStatic(); };
$('btnView').onclick = () => { S.view = S.view === 'follow' ? 'overview' : 'follow'; };
$('demoSpeed').oninput = (e) => { S.demoSpeed = +e.target.value; if (!S.nav) S.speed = 0; };
$('btnGear').onclick = () => { $('settings').hidden = false; };
$('btnCloseSet').onclick = () => { $('settings').hidden = true; };
const paintTheme = () => ['Auto', 'Light', 'Dark'].forEach((n) =>
  $('th' + n).classList.toggle('on', theme.pref() === n.toLowerCase()));
for (const n of ['Auto', 'Light', 'Dark']) $('th' + n).onclick = () => { theme.set(n.toLowerCase()); paintTheme(); };
paintTheme();
for (const [btn, id] of [['mapProto', 'prototype'], ['mapCampus', 'campus']]) {
  $(btn).classList.toggle('on', MAP.id === id);
  $(btn).onclick = () => { if (MAP.id !== id) switchMap(id); };
}
$('mDemo').onclick = () => setMode('demo');
$('mGps').onclick = () => setMode('gps');
$('bearing').value = geo.get().bearing ?? 90;
$('bearing').onchange = (e) => geo.set({ bearing: +e.target.value });
$('btnCalib').onclick = () => {
  if (!window.isSecureContext) return gpsMsg('Calibrar requiere https.');
  navigator.geolocation.getCurrentPosition((p) => {
    geo.set({ lat: p.coords.latitude, lon: p.coords.longitude, bearing: +$('bearing').value });
    resetCar();
    gpsMsg('Entrada fijada en tu ubicación actual.');
    renderStatic();
  }, (e) => gpsMsg('GPS: ' + e.message), { enableHighAccuracy: true });
};
$('btnClearCalib').onclick = () => {
  try { localStorage.removeItem('parkuni.geo'); } catch {}
  $('bearing').value = 90;
  gpsMsg('Calibración borrada.');
  renderStatic();
};

/* ---------- bucle principal ---------- */
let last = performance.now();
function frame(ts) {
  const dt = Math.min((ts - last) / 1000, 0.1);
  last = ts;
  if (S.mode === 'demo') moveDemo(dt);
  updateNav();
  updateSpeed();
  stepCamera(dt);
  requestAnimationFrame(frame);
}
Occ.compute(S.st);
lot.setOcc(Occ.map);
$('demoSpeed').value = S.demoSpeed;
renderStatic();
stepCamera(0, true);
poll();
requestAnimationFrame(frame);
