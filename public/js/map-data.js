/* Mapa del campus. Coordenadas = píxeles del dibujo original (1920 x 1080).
 *
 * - nodes / edges : red de carriles de UN solo sentido (de a → b). El ruteo usa esto.
 * - segments      : filas de cajones (las líneas verdes del dibujo) con su cantidad.
 *                   El id de cada cajón es "<segmento>-<número>", p. ej. "S05-12".
 * - sensors       : qué sensor del ESP32 corresponde a qué cajón.
 * - overflow      : zonas de último recurso (sin sensores) cuando todo está lleno.
 * - El resto (surfaces, crosswalks, buildings, nopark, labels) es solo dibujo.
 */
const CAMPUS = (() => {
  const P = (x, y) => ({ x, y });
  const d2r = (d) => (d * Math.PI) / 180;
  // Puntos de una elipse (ángulos en grados, y hacia abajo en pantalla).
  const arc = (cx, cy, rx, ry, a0, a1, step = 6) => {
    const n = Math.max(2, Math.ceil(Math.abs(a1 - a0) / step)), pts = [];
    for (let i = 0; i <= n; i++) {
      const t = d2r(a0 + ((a1 - a0) * i) / n);
      pts.push(P(cx + rx * Math.cos(t), cy + ry * Math.sin(t)));
    }
    return pts;
  };

  /* ---------- rotonda ---------- */
  const ring = { cx: 762, cy: 522, r: 78 };      // sentido contrario a las manecillas del reloj
  const ringPt = (a) => arc(ring.cx, ring.cy, ring.r, ring.r, a, a, 1)[0];
  const ringMid = (a0, a1) => arc(ring.cx, ring.cy, ring.r, ring.r, a0, a1, 8).slice(1, -1);

  // Curva del acceso norte: carril interior (sube hacia las zonas) y exterior (baja a la rotonda).
  // En el camellón entre los dos hay una vuelta en U permitida (t = 240° sobre la curva).
  const arcIn = arc(905, 250, 112, 102, 180, 270);        // 16 puntos, de 180° a 270° cada 6°
  const arcOut = arc(905, 250, 153, 175, 270, 180);       // de 270° a 180°
  const UTURN_AT = 10;                                    // índice en arcIn (180 + 6·10 = 240°); en arcOut es 15 - 10

  /* ---------- nodos ---------- */
  const nodes = {
    E0: P(250, 573),                              // ENTRADA al campus
    X0: P(250, 527),                              // SALIDA del campus
    RW_in: ringPt(146), RS_out: ringPt(93), RS_in: ringPt(64), RE: ringPt(0),
    RN_out: ringPt(-66), RN_in: ringPt(-97), RW_out: ringPt(-176),
    // zona norte / este
    NT_in: P(905, 148), NT_out: P(905, 75), UI: arcIn[UTURN_AT], UO: arcOut[15 - UTURN_AT], M1: P(1290, 146), NEE: P(1762, 135), NE_N: P(1735, 74), UTW: P(1290, 74.5),
    EC: P(1815, 232), W2s: P(1700, 218), J: P(1285, 218), Y3s: P(1340, 310), E3: P(1742, 310),
    EM: P(1775, 313), MAESTROS: P(1787, 352),
    // zona sur
    SP: P(757, 692), Q2: P(670, 695), Q1: P(565, 695), T3d: P(773, 728),
    B1: P(565, 990), B2: P(670, 990), B3: P(773, 990),
    U1: P(592, 990), U2: P(696, 990), U3: P(799, 990),
    P1u: P(592, 712), P2u: P(696, 712), P3u: P(799, 712), TERRAZA: P(842, 702),
  };

  /* ---------- carriles (un sentido) ---------- */
  const edges = [];
  const E = (id, a, b, mid = [], o = {}) => edges.push({ id, a, b, mid, name: o.name || '', ring: !!o.ring, dest: !!o.dest, uturn: !!o.uturn });

  E('w_in', 'E0', 'RW_in', [], { name: 'Acceso' });
  E('w_out', 'RW_out', 'X0', [], { name: 'Salida' });
  const R = { name: 'Rotonda', ring: true };
  E('ring1', 'RW_in', 'RS_out', ringMid(146, 93), R);
  E('ring2', 'RS_out', 'RS_in', ringMid(93, 64), R);
  E('ring3', 'RS_in', 'RE', ringMid(64, 0), R);
  E('ring4', 'RE', 'RN_out', ringMid(0, -66), R);
  E('ring5', 'RN_out', 'RN_in', ringMid(-66, -97), R);
  E('ring6', 'RN_in', 'RW_out', ringMid(-97, -176), R);
  E('ring7', 'RW_out', 'RW_in', ringMid(-176, -214), R);

  // camino al norte: sube por el carril derecho (x≈793) y baja por el izquierdo (x≈752)
  E('n_up1', 'RN_out', 'UI', arcIn.slice(0, UTURN_AT), { name: 'Acceso norte' });
  E('n_up2', 'UI', 'NT_in', arcIn.slice(UTURN_AT + 1, -1), { name: 'Acceso norte' });
  E('n_down1', 'NT_out', 'UO', arcOut.slice(1, 15 - UTURN_AT), { name: 'Acceso norte' });
  E('n_down2', 'UO', 'RN_in', arcOut.slice(15 - UTURN_AT + 1), { name: 'Acceso norte' });
  // vuelta en U permitida en el camellón, en los dos sentidos
  E('uturn_in', 'UI', 'UO', [], { name: 'Vuelta en U', uturn: true });
  E('uturn_out', 'UO', 'UI', [], { name: 'Vuelta en U', uturn: true });

  // zona norte (arriba izquierda) y zona este (arriba derecha): circuito de un solo sentido
  E('tl_east', 'NT_in', 'M1', [], { name: 'Zona Norte' });
  E('east1', 'M1', 'NEE', [P(1335, 135)], { name: 'Zona Este' });
  E('ne_loop', 'NEE', 'NE_N', [P(1785, 118), P(1790, 95), P(1770, 80)], { name: 'Zona Este' });
  E('top_w1', 'NE_N', 'UTW', [], { name: 'Carril superior' });
  E('top_w2', 'UTW', 'NT_out', [], { name: 'Carril superior' });
  // vuelta en U permitida entre el carril superior (hacia el oeste) y el inicio de la zona este (hacia el este)
  E('uturn_e1', 'M1', 'UTW', [], { name: 'Vuelta en U', uturn: true });
  E('uturn_e2', 'UTW', 'M1', [], { name: 'Vuelta en U', uturn: true });
  E('east_down', 'NEE', 'EC', [P(1800, 165), P(1812, 200)], { name: 'Zona Este' });   // supuesto: confirmar
  E('west2_in', 'EC', 'W2s', [P(1790, 214), P(1757, 205)], { name: 'Zona Este' });
  E('west2', 'W2s', 'J', [], { name: 'Zona Este' });
  E('j_up', 'J', 'M1', [P(1288, 190), P(1296, 168)], { name: 'Zona Este' });
  E('j_down', 'J', 'Y3s', [P(1266, 235), P(1268, 262), P(1290, 298)], { name: 'Zona Este' });
  E('south_e', 'Y3s', 'E3', [], { name: 'Zona Este' });
  E('east_up_a', 'E3', 'EM', [P(1760, 312)], { name: 'Zona Este' });
  E('east_up_b', 'EM', 'EC', [P(1815, 305), P(1836, 285), P(1836, 260)], { name: 'Zona Este' });
  E('to_maestros', 'EM', 'MAESTROS', [], { name: 'Zona de maestros', dest: true });

  // zona sur (pasillos L1, L2, L3 de dos sentidos; se cruzan por abajo)
  E('s_entry', 'RS_out', 'SP', [], { name: 'Zona Sur' });
  E('plaza_w1', 'SP', 'Q2', [P(715, 694)], { name: 'Zona Sur' });
  E('plaza_w2', 'Q2', 'Q1', [], { name: 'Zona Sur' });
  E('p_l3', 'SP', 'T3d', [P(773, 705)], { name: 'Zona Sur' });
  E('to_terraza', 'SP', 'TERRAZA', [P(800, 694)], { name: 'Terrazería', dest: true });
  E('l1_down', 'Q1', 'B1', [], { name: 'Zona Sur' });
  E('l2_down', 'Q2', 'B2', [], { name: 'Zona Sur' });
  E('l3_down', 'T3d', 'B3', [], { name: 'Zona Sur' });
  E('x12a', 'B1', 'U2', [P(575, 1012), P(630, 1032), P(682, 1012)], { name: 'Zona Sur' });
  E('x12b', 'B2', 'U1', [P(655, 1016), P(615, 1016), P(600, 1004)], { name: 'Zona Sur' });
  E('x23a', 'B2', 'U3', [P(690, 1012), P(740, 1032), P(787, 1012)], { name: 'Zona Sur' });
  E('x23b', 'B3', 'U2', [P(765, 1016), P(725, 1016), P(708, 1004)], { name: 'Zona Sur' });
  E('l1_up', 'U1', 'P1u', [], { name: 'Zona Sur' });
  E('l2_up', 'U2', 'P2u', [], { name: 'Zona Sur' });
  E('l3_up', 'U3', 'P3u', [], { name: 'Zona Sur' });
  E('plaza_e1', 'P1u', 'P2u', [], { name: 'Zona Sur' });
  E('plaza_e2', 'P2u', 'P3u', [], { name: 'Zona Sur' });
  E('s_return', 'P3u', 'RS_in', [], { name: 'Zona Sur' });

  /* ---------- cajones ---------- */
  const H = (x0, x1, y) => [P(x0, y), P(x1, y)];
  const V = (x, y0, y1) => [P(x, y0), P(x, y1)];
  const segments = [];
  // ang: '/' o '\' = cajones en diagonal; lane: carril que los atiende (si no, el más cercano)
  const S = (id, zone, n, pts, o = {}) => segments.push({ id, zone, n, pts, ...o });

  S('AC1', 'Acceso', 15, H(346, 645, 511));
  S('AC2', 'Acceso', 15, H(346, 645, 589));

  S('AN1', 'Acceso norte', 22, [P(738, 365), P(738, 339), P(738, 298), P(737, 260), P(743, 220), P(755, 179),
    P(765, 155), P(787, 123), P(819, 94), P(850, 75), P(861, 70)]);
  S('AN2', 'Acceso norte', 3, V(812, 332, 371));
  S('AN3', 'Acceso norte', 1, V(813, 400, 425));
  S('AN4', 'Acceso norte', 1, V(736, 401, 426));
  S('DIS', 'Acceso norte', 2, [P(863, 68), P(902, 54)], { type: 'disabled' });

  S('N01', 'Zona Norte', 6, H(913, 1010, 55), { lane: 'top_w2', ang: '\\' });
  S('N02', 'Zona Norte', 10, H(1059, 1150, 54), { lane: 'top_w2', ang: '\\' });
  S('N03', 'Zona Norte', 13, H(1169, 1228, 52), { lane: 'top_w2', ang: '\\' });
  S('N04', 'Zona Norte', 5, H(936, 1012, 112), { lane: 'top_w2', ang: '/' });
  S('N05', 'Zona Norte', 10, H(1059, 1150, 111), { lane: 'top_w2', ang: '/' });
  S('N06', 'Zona Norte', 13, H(1169, 1227, 111), { lane: 'top_w2', ang: '/' });
  S('N07', 'Zona Norte', 5, H(938, 1014, 128), { lane: 'tl_east', ang: '/' });
  S('N08', 'Zona Norte', 10, H(1059, 1147, 129), { lane: 'tl_east', ang: '/' });
  S('N09', 'Zona Norte', 13, H(1169, 1228, 131), { lane: 'tl_east', ang: '/' });
  S('N10', 'Zona Norte', 5, H(934, 1010, 191), { lane: 'tl_east', ang: '\\' });
  S('N11', 'Zona Norte', 10, H(1060, 1148, 193), { lane: 'tl_east', ang: '\\' });

  S('E01', 'Zona Este', 20, H(1256, 1631, 54), { lane: ['top_w1', 'top_w2'], ang: '\\' });
  S('E02', 'Zona Este', 7, H(1332, 1439, 91), { lane: 'top_w1', ang: '/' });
  S('E03', 'Zona Este', 7, H(1334, 1441, 108), { lane: 'east1', ang: '/' });
  S('E04', 'Zona Este', 9, H(1520, 1627, 90), { lane: 'top_w1', ang: '/' });
  S('E05', 'Zona Este', 9, H(1524, 1631, 107), { lane: 'east1', ang: '/' });
  S('E06', 'Zona Este', 4, H(1687, 1726, 92), { lane: 'top_w1', ang: '/' });
  S('E07', 'Zona Este', 4, H(1696, 1722, 105), { lane: 'east1', ang: '/' });
  S('E08', 'Zona Este', 7, H(1332, 1439, 155), { lane: 'east1', ang: '\\' });
  S('E09', 'Zona Este', 7, H(1333, 1440, 173), { lane: 'west2', ang: '\\' });
  S('E10', 'Zona Este', 9, H(1523, 1630, 156), { lane: 'east1', ang: '\\' });
  S('E11', 'Zona Este', 9, H(1523, 1630, 175), { lane: 'west2', ang: '\\' });
  S('E12', 'Zona Este', 5, H(1684, 1751, 157), { lane: 'east1', ang: '\\' });
  S('E13', 'Zona Este', 5, H(1684, 1751, 174), { lane: ['west2', 'west2_in'], ang: '\\' });
  S('E14', 'Zona Este', 7, H(1334, 1442, 262), { lane: 'west2', ang: '/' });
  S('E15', 'Zona Este', 7, H(1334, 1441, 278), { lane: 'south_e', ang: '/' });
  S('E16', 'Zona Este', 9, H(1523, 1630, 264), { lane: 'west2', ang: '/' });
  S('E17', 'Zona Este', 9, H(1520, 1627, 283), { lane: 'south_e', ang: '/' });
  S('E18', 'Zona Este', 17, H(1684, 1791, 261), { lane: ['west2', 'west2_in'], ang: '/' });
  S('E19', 'Zona Este', 17, H(1686, 1793, 281), { lane: ['south_e', 'east_up_a'], ang: '/' });
  S('E20', 'Zona Este', 10, H(1256, 1441, 338), { lane: ['south_e', 'j_down'] });
  S('E21', 'Zona Este', 9, H(1521, 1628, 342), { lane: 'south_e' });
  S('E22', 'Zona Este', 1, H(1683, 1731, 344), { lane: 'south_e', ang: '\\' });
  S('E23', 'Zona Este', 10, [P(1637, 51), P(1660, 50), P(1700, 51), P(1740, 58), P(1781, 77), P(1806, 99)]);
  S('E24', 'Zona Este', 2, [P(1822, 135), P(1834, 170)]);
  S('E25', 'Zona Este', 2, [P(1843, 193), P(1856, 230)]);
  S('E26', 'Zona Este', 6, [P(1851, 238), P(1848, 260), P(1845, 299), P(1841, 332)]);

  S('S01', 'Zona Sur', 8, V(531, 770, 815));
  S('S02', 'Zona Sur', 23, V(528, 844, 990));
  S('S03', 'Zona Sur', 25, V(633, 728, 822), { lane: 'l1_up' });    // línea verde que faltaba en el dibujo (supuesto)
  S('S04', 'Zona Sur', 25, V(632, 846, 990), { lane: 'l1_up' });
  S('S05', 'Zona Sur', 50, V(650, 727, 990), { lane: 'l2_down' });
  S('S06', 'Zona Sur', 50, V(740, 727, 990), { lane: 'l3_down' });
  S('S07', 'Zona Sur', 40, V(843, 727, 948), { lane: 'l3_up' });

  /* ---------- sensores del ESP32 → cajón ---------- */
  // Ajusta esto a donde queden instalados los sensores. La clave es el id que manda el ESP32
  // al servidor (POST /api/sensor) y el valor es el id del cajón en el mapa.
  const sensors = {
    A1: 'S05-20',    // Zona Sur
    A2: 'AN1-11',    // Acceso norte
    A3: 'N05-5',     // Zona Norte
    A4: 'E01-10',    // Zona Este
  };

  /* ---------- último recurso cuando todo está lleno (sin sensores) ---------- */
  const overflow = [
    { id: 'TERRAZERIA', name: 'Terrazería', edge: 'to_terraza' },
    { id: 'MAESTROS', name: 'Zona de maestros', edge: 'to_maestros' },
  ];

  /* ---------- solo dibujo ---------- */
  const surfaces = [
    [P(905, 48), P(1650, 46), P(1740, 56), P(1815, 96), P(1852, 160), P(1865, 236), P(1845, 350),
      P(1245, 350), P(1245, 215), P(928, 214), P(905, 180)],
    [P(527, 990), P(527, 715), P(545, 675), P(600, 655), P(697, 650), P(697, 626), P(806, 626),
      P(806, 650), P(850, 678), P(850, 1000), P(527, 1000)],
  ];
  const crosswalks = [
    // franjas peatonales y camellones (detectados del dibujo)
    [1013,52,43,7],[1013,67,43,7],[1013,83,43,7],[1013,98,43,8],[1013,114,43,17],[1154,114,10,17],[1233,114,11,17],
    [1013,139,43,7],[1013,155,43,7],[1013,170,43,8],[1013,186,43,8],[1013,202,43,8],
    [1448,72,68,10],[1449,96,67,13],[1449,123,67,14],[1449,151,67,14],[1449,178,67,29],[1448,217,68,17],
    [1449,248,67,14],[1449,275,67,14],[1449,303,67,14],[1449,330,67,14],
    [1634,73,72,13],[1634,99,47,14],[1634,154,47,14],[1634,182,47,28],[1634,224,47,14],[1634,252,47,13],
    [1634,279,47,14],[1634,307,49,14],[1634,334,47,14],
    [764,374,8,24],[780,374,8,24],[796,374,8,24],
    [714,617,11,16],[732,618,11,16],[791,618,11,15],
    // paso peatonal del camino de acceso y del pasillo sur
    ...[514, 528, 542, 556, 570, 584].map((y) => [648, y, 13, 9]),
    ...[530, 548, 566, 584, 602, 620].map((x) => [x, 825, 11, 16]),
  ];
  const buildings = [
    { x: 850, y: 678, w: 160, h: 317, label: 'Terrazería' },
    { x: 1745, y: 350, w: 90, h: 320, label: 'Maestros' },
  ];
  const nopark = [   // zonas donde NO se puede estacionar (amarillo)
    [P(1816, 111), P(1825, 133)], [P(1833, 169), P(1842, 191)], [P(1176, 203), P(1220, 204)],
    [P(1256, 223), P(1256, 331)], [P(811, 255), P(811, 326)], [P(1742, 344), P(1831, 344)],
    [P(821, 437), P(847, 456), P(860, 499), P(861, 540), P(845, 583), P(814, 603)],
  ];
  const busbay = [[P(535, 727), P(530, 764)]];                    // solo ruta de la universidad
  const labels = [
    { t: 'ENTRADA', x: 250, y: 612, c: 'ok' }, { t: 'SALIDA', x: 250, y: 507, c: 'bad' },
    { t: 'ACCESO', x: 500, y: 485 }, { t: 'ROTONDA', x: 762, y: 527, c: 'ring' },
    { t: 'ZONA NORTE', x: 1070, y: 30, big: true }, { t: 'ZONA ESTE', x: 1560, y: 30, big: true },
    { t: 'ZONA SUR', x: 690, y: 1050, big: true }, { t: 'ACCESO NORTE', x: 660, y: 300 },
  ];

  // Zonas cercanas a cada una: si no hay lugar en la zona preferida, se busca primero aquí.
  const zoneNear = {
    'Zona Norte': ['Zona Este', 'Acceso norte'],
    'Zona Este': ['Zona Norte', 'Acceso norte'],
    'Acceso norte': ['Zona Norte', 'Zona Este', 'Acceso'],
    'Acceso': ['Acceso norte', 'Zona Sur'],
    'Zona Sur': ['Acceso', 'Acceso norte'],
  };

  const bounds = { x: 190, y: 15, w: 1700, h: 1050 };
  const entry = { edge: 'w_in', s: 0 };

  return {
    id: 'campus', title: 'Estacionamiento Campus', metersPerPx: 0.2, simulate: true, demoSpeed: 20,
    style: { depth: 14, maxW: 14, lane: 42, chevGap: 95, followZoom: 1.7 },
    nodes, edges, segments, sensors, overflow, ring, surfaces, crosswalks, buildings, nopark, busbay, labels, bounds, entry, zoneNear,
  };
})();

/* =====================================================================================
 * PROTOTIPO: la maqueta con los 4 sensores reales. Un pasillo de un solo sentido con 4
 * cajones, una vuelta en U y la salida. Sin simulación: todo lo que se ve es de los sensores.
 * ===================================================================================== */
const PROTOTYPE = (() => {
  const P = (x, y) => ({ x, y });
  const arc = (cx, cy, rx, ry, a0, a1, n = 12) => Array.from({ length: n + 1 }, (_, i) => {
    const t = ((a0 + ((a1 - a0) * i) / n) * Math.PI) / 180;
    return P(cx + rx * Math.cos(t), cy + ry * Math.sin(t));
  });
  const nodes = {
    E0: P(0, 400), M0: P(130, 400), T1: P(720, 400), T2: P(720, 520), R1: P(250, 520), X0: P(0, 520),
  };
  const edges = [];
  const E = (id, a, b, mid = [], name = '') => edges.push({ id, a, b, mid, name, ring: false, dest: false });
  E('w_in', 'E0', 'M0', [], 'Entrada');
  E('lane', 'M0', 'T1', [], 'Pasillo');
  E('uturn', 'T1', 'T2', arc(720, 460, 70, 60, -90, 90).slice(1, -1), 'Vuelta');
  E('back', 'T2', 'R1', [], 'Pasillo de regreso');
  E('exit_w', 'R1', 'X0', [], 'Salida');
  E('again', 'R1', 'M0', [P(205, 510), P(155, 470), P(135, 432)], 'Pasillo');   // dar otra vuelta

  const segments = [{ id: 'P', zone: 'Prototipo', n: 4, pts: [P(240, 322), P(560, 322)] }];
  const sensors = { A1: 'P-1', A2: 'P-2', A3: 'P-3', A4: 'P-4' };

  return {
    id: 'prototype', title: 'Prototipo · 4 sensores', metersPerPx: 0.035, simulate: false, demoSpeed: 8,
    style: { depth: 70, maxW: 62, lane: 70, chevGap: 150, followZoom: 0.9 },
    nodes, edges, segments, sensors, overflow: [], ring: { cx: -999, cy: -999, r: 0 },
    surfaces: [[P(190, 240), P(780, 240), P(840, 340), P(840, 600), P(190, 600)]],
    crosswalks: [], nopark: [], busbay: [],
    buildings: [{ x: 210, y: 215, w: 530, h: 85, label: 'Edificio' }],
    labels: [
      { t: 'ENTRADA', x: 20, y: 445, c: 'ok' }, { t: 'SALIDA', x: 20, y: 565, c: 'bad' },
      { t: 'PROTOTIPO · 4 SENSORES', x: 420, y: 170, big: true },
    ],
    bounds: { x: -90, y: 130, w: 1000, h: 520 },
    entry: { edge: 'w_in', s: 0 },
  };
})();

/* ---------- mapa activo ---------- */
// ?map=campus | ?map=prototype  (o el último elegido). Por defecto: el prototipo.
const MAPS = { prototype: PROTOTYPE, campus: CAMPUS };
const MAP = (() => {
  let id = 'prototype';
  try {
    const q = new URLSearchParams(location.search).get('map');
    if (q && MAPS[q]) { id = q; localStorage.setItem('parkuni.map', q); }
    else { const l = localStorage.getItem('parkuni.map'); if (l && MAPS[l]) id = l; }
  } catch {}
  return MAPS[id];
})();
CONFIG.metersPerPx = MAP.metersPerPx;
function switchMap(id) {
  try { localStorage.setItem('parkuni.map', id); } catch {}
  const u = new URL(location.href);
  u.searchParams.set('map', id);
  location.href = u.toString();
}

