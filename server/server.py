#!/usr/bin/env python3
"""Servidor de ParkUni (solo librería estándar de Python; pensado para un servidor Linux, p. ej. Fedora).

  python3 server/server.py                 # http://0.0.0.0:8000
  python3 server/server.py --port 80
  python3 server/server.py --https         # https autofirmado (para probar el GPS en el celular por LAN)

Hace tres cosas:
  1. Sirve la app web (carpeta public/).
  2. Guarda el estado en SQLite (server/data/parkuni.db): sensores, cajones forzados, simulación,
     límite de velocidad, eventos y el estado de emergencia.
  3. Recibe los datos del ESP32 (POST /api/sensor) y le contesta si hay emergencia activa.
  4. Prueba de LED RGB desde el panel (POST /api/ledtest): el ESP32 recibe el color en la respuesta de /api/sensor.
  5. Reserva cajones para los conductores (POST /api/claim) para no mandar a dos al mismo.

Variables de entorno (en el servidor viven en /etc/parkuni.env, nunca en el código):
  PARKUNI_ADMIN_PASSWORD       contraseña del panel de administración (sin ella el panel queda bloqueado)
  PARKUNI_EMERGENCY_PASSWORD   contraseña para activar/desactivar la emergencia (sin ella la emergencia queda bloqueada)
  PARKUNI_ADMIN_LOCAL_ONLY     "1": el panel y sus rutas solo se aceptan desde esta misma máquina (127.0.0.1)
  PARKUNI_DB                   ruta del archivo SQLite
"""
import argparse, hmac, json, os, secrets, socket, sqlite3, ssl, subprocess, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "public")                      # lo único que se publica
DB_PATH = os.environ.get("PARKUNI_DB", os.path.join(HERE, "data", "parkuni.db"))
EM_PASSWORD = os.environ.get("PARKUNI_EMERGENCY_PASSWORD", "")
ADMIN_PASSWORD = os.environ.get("PARKUNI_ADMIN_PASSWORD", "")
ADMIN_LOCAL_ONLY = os.environ.get("PARKUNI_ADMIN_LOCAL_ONLY") == "1"
SESSION_TTL = 8 * 3600   # la sesión del panel dura 8 horas
COOKIE = "parkuni_session"
# rutas de escritura que solo puede usar el administrador (el ESP32 y la app del conductor usan las demás)
ADMIN_POST = {"/api/ledtest", "/api/emergency", "/api/override", "/api/overrides/clear", "/api/sim", "/api/config", "/api/reset"}
ADMIN_PAGES = {"/admin.html", "/js/admin.js"}
LOOPBACK = ("127.0.0.1", "::1")
STALE_S = 120            # un sensor sin reportar en este tiempo se marca "sin señal"
DRIVER_TTL = 10          # un conductor sin latido en este tiempo desaparece del panel

LOCK = threading.Lock()
DRIVERS = {}             # id -> {target, x, y, speed, t}   (solo en memoria)
FAILS = []               # intentos fallidos de contraseña de emergencia (para frenar fuerza bruta)
LOGIN_FAILS = {}         # ip -> intentos fallidos de inicio de sesión del último minuto
SESSIONS = {}            # token -> vencimiento (solo en memoria: reiniciar el servidor cierra las sesiones)
LED_TEST = {}            # id de sensor -> {color, t}   (prueba de LED RGB desde el panel; solo en memoria)
LED_TEST_TTL = 180       # la prueba se apaga sola: si se olvida, los LED vuelven a indicar libre / ocupado
LED_SENSORS = ["A1", "A2", "A3", "A4"]
LED_COLORS = {"off": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "purple": 5, "cyan": 6, "white": 7}   # máscara R=1 G=2 B=4
TLS_CTX = None


# ----------------------------------------------------------------------------- base de datos
def db_open():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sensors(id TEXT PRIMARY KEY, occupied INTEGER, distance_cm INTEGER, updated REAL);
        CREATE TABLE IF NOT EXISTS overrides(spot TEXT PRIMARY KEY, occupied INTEGER);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, type TEXT, msg TEXT);
    """)
    conn.commit()
    return conn


DB = db_open()


def kv_get(k, default):
    row = DB.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(k, v):
    DB.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
    DB.commit()


def add_event(msg, typ="info"):
    DB.execute("INSERT INTO events(t,type,msg) VALUES(?,?,?)", (time.time(), typ, msg))
    DB.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - 500")   # solo las últimas 500
    DB.commit()


def emergency_state():
    return kv_get("emergency", {"on": False, "since": None})


def snapshot(with_events):
    now = time.time()
    slots, seen, stale, dist = {}, [], [], {}
    for sid, occ, cm, upd in DB.execute("SELECT id, occupied, distance_cm, updated FROM sensors"):
        slots[sid] = bool(occ)
        dist[sid] = cm
        seen.append(sid)
        if now - upd > STALE_S:
            stale.append(sid)
    for k in [k for k, d in DRIVERS.items() if now - d["t"] > DRIVER_TTL]:
        del DRIVERS[k]
    out = {
        "slots": slots, "seen": seen, "stale": stale, "distances": dist,
        "overrides": {s: bool(o) for s, o in DB.execute("SELECT spot, occupied FROM overrides")},
        "sim": kv_get("sim", {"on": True, "level": 0.7}),
        "speedLimit": kv_get("speedLimit", 20),
        "speedingCount": kv_get("speeding", 0),
        "esp": bool(seen) and len(stale) < len(seen),
        "emergency": emergency_state(),
        "ledTest": led_test_active(),
        "drivers": [{"id": k, **d} for k, d in DRIVERS.items()],
        "events": [],
        "now": now,
    }
    if with_events:
        out["events"] = [{"t": t, "msg": m} for t, m in
                         DB.execute("SELECT t, msg FROM events ORDER BY id DESC LIMIT 40")]
    return out


def led_test_active():
    """Pruebas de LED vigentes {id: color}; descarta las vencidas."""
    now = time.time()
    for k in [k for k, v in LED_TEST.items() if now - v["t"] > LED_TEST_TTL]:
        del LED_TEST[k]
    return {k: v["color"] for k, v in LED_TEST.items()}


def led_test_set(target, color):
    """target: id de sensor o 'all'. color: nombre de LED_COLORS, 'auto' o None para volver a lo normal."""
    ids = LED_SENSORS if target == "all" else [target] if target in LED_SENSORS else []
    if not ids or (color not in LED_COLORS and color not in (None, "auto")):
        return False
    for sid in ids:
        if color in LED_COLORS:
            LED_TEST[sid] = {"color": color, "t": time.time()}
        else:
            LED_TEST.pop(sid, None)
    return True


def set_emergency(on, password):
    """Devuelve (ok, mensaje). Exige la contraseña tanto para activar como para desactivar."""
    now = time.time()
    FAILS[:] = [t for t in FAILS if now - t < 60]
    if len(FAILS) >= 5:
        return False, "Demasiados intentos. Espera un minuto."
    if not EM_PASSWORD:
        return False, "La contraseña de emergencia no está configurada en el servidor"
    if not hmac.compare_digest(str(password or "").encode(), EM_PASSWORD.encode()):
        FAILS.append(now)
        add_event("Intento de emergencia con contraseña incorrecta", "security")
        return False, "Contraseña incorrecta"
    kv_set("emergency", {"on": bool(on), "since": now if on else None})
    add_event("⚠ EMERGENCIA ACTIVADA: LED en morado intermitente" if on else "Emergencia desactivada: LED normales", "emergency")
    return True, "ok"


def login_try(ip, password):
    """Devuelve (token, error). Frena la fuerza bruta: 5 fallos por minuto desde la misma IP."""
    now = time.time()
    fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 60]
    LOGIN_FAILS[ip] = fails
    if len(fails) >= 5:
        return None, "Demasiados intentos. Espera un minuto."
    if not ADMIN_PASSWORD:
        return None, "El panel está bloqueado: falta PARKUNI_ADMIN_PASSWORD en el servidor"
    if not hmac.compare_digest(str(password or "").encode(), ADMIN_PASSWORD.encode()):
        fails.append(now)
        add_event(f"Intento de inicio de sesión del panel con contraseña incorrecta ({ip})", "security")
        return None, "Contraseña incorrecta"
    for k in [k for k, exp in SESSIONS.items() if exp < now]:
        del SESSIONS[k]
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = now + SESSION_TTL
    add_event(f"Inicio de sesión en el panel ({ip})", "security")
    return token, None


# ----------------------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def setup(self):
        if TLS_CTX:   # el handshake va en el hilo de la conexión, no en el accept
            self.request = TLS_CTX.wrap_socket(self.request, server_side=True)
        super().setup()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/" not in self.path:
            super().log_message(fmt, *args)

    def _json(self, obj, code=200, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _admin(self):
        """True si la petición trae una sesión de administrador vigente (y, si se pidió, viene de esta máquina)."""
        if ADMIN_LOCAL_ONLY and self.client_address[0] not in LOOPBACK:
            return False
        exp = SESSIONS.get(self._token())
        return bool(exp and exp > time.time())

    def _cookie(self, value, max_age):
        return [("Set-Cookie", f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
                               + ("; Secure" if TLS_CTX else ""))]

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ADMIN_PAGES and not self._admin():
            if path == "/js/admin.js":
                return self._json({"ok": False, "error": "auth"}, 401)
            self.send_response(302)
            self.send_header("Location", "/login.html")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if path == "/api/me":
            return self._json({"admin": self._admin(), "configured": bool(ADMIN_PASSWORD), "localOnly": ADMIN_LOCAL_ONLY})
        if path == "/api/status":
            with LOCK:
                return self._json(snapshot("events" in self.path and self._admin()))
        if path == "/api/hardware":                      # ligero: solo lo que necesita el ESP32
            with LOCK:
                return self._json({"emergency": emergency_state()["on"]})
        super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "json"}, 400)
        p = self.path.split("?")[0]
        if p == "/api/login":
            ip = self.client_address[0]
            if ADMIN_LOCAL_ONLY and ip not in LOOPBACK:
                return self._json({"ok": False, "error": "El panel solo se puede abrir desde la laptop del servidor"}, 403)
            with LOCK:
                token, err = login_try(ip, d.get("password"))
            if err:
                return self._json({"ok": False, "error": err}, 429 if err.startswith("Demasiados") else 403)
            return self._json({"ok": True}, headers=self._cookie(token, SESSION_TTL))
        if p == "/api/logout":
            SESSIONS.pop(self._token(), None)
            return self._json({"ok": True}, headers=self._cookie("", 0))
        if p in ADMIN_POST and not self._admin():
            return self._json({"ok": False, "error": "auth"}, 401)
        with LOCK:
            if p == "/api/sensor":
                # ESP32: {"sensors": {"A1": {"occupied": true, "distance_cm": 14}, ...}}  (o un solo {"id":..})
                items = d.get("sensors") or ({d["id"]: d} if d.get("id") else {})
                now = time.time()
                for sid, v in items.items():
                    sid = str(sid)[:16]
                    occ, cm = int(bool(v.get("occupied"))), int(v.get("distance_cm") or 0)
                    old = DB.execute("SELECT occupied FROM sensors WHERE id=?", (sid,)).fetchone()
                    DB.execute("INSERT INTO sensors(id,occupied,distance_cm,updated) VALUES(?,?,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET occupied=excluded.occupied, "
                               "distance_cm=excluded.distance_cm, updated=excluded.updated", (sid, occ, cm, now))
                    if old is not None and old[0] != occ:
                        add_event(f"Sensor {sid}: {'ocupado' if occ else 'liberado'}", "sensor")
                DB.commit()
                # "test": máscaras de color (R=1 G=2 B=4) por sensor, solo de los LED en prueba desde el panel
                test = {k: LED_COLORS[c] for k, c in led_test_active().items()}
                return self._json({"ok": True, "emergency": emergency_state()["on"], "test": test})
            if p == "/api/ledtest":
                color = d.get("color")
                if not led_test_set(str(d.get("id")), color):
                    return self._json({"ok": False, "error": "id o color no válido"}, 400)
                add_event(f"Prueba de LED {d.get('id')}: {color or 'auto'}", "config")
                return self._json({"ok": True, "ledTest": led_test_active()})
            if p == "/api/claim":
                # Reserva atómica de un cajón: dos conductores nunca reciben el mismo.
                me, spot, now = str(d.get("id"))[:20], str(d.get("spot") or "")[:32], time.time()
                for k, dr in DRIVERS.items():
                    if k != me and dr.get("target") == spot and now - dr["t"] <= DRIVER_TTL:
                        return self._json({"ok": False, "taken_by": k})
                cur = DRIVERS.get(me, {"x": None, "y": None, "speed": None})
                DRIVERS[me] = {**cur, "target": spot, "t": now}
                return self._json({"ok": True})
            if p == "/api/emergency":
                ok, msg = set_emergency(bool(d.get("on")), d.get("password"))
                return self._json({"ok": ok, "error": None if ok else msg, "emergency": emergency_state()},
                                  200 if ok else 403)
            if p == "/api/override" and d.get("id"):
                if d.get("occupied") is None:
                    DB.execute("DELETE FROM overrides WHERE spot=?", (d["id"],))
                else:
                    DB.execute("INSERT INTO overrides(spot,occupied) VALUES(?,?) "
                               "ON CONFLICT(spot) DO UPDATE SET occupied=excluded.occupied", (d["id"], int(bool(d["occupied"]))))
                DB.commit()
            elif p == "/api/overrides/clear":
                DB.execute("DELETE FROM overrides")
                DB.commit()
            elif p == "/api/sim":
                kv_set("sim", {"on": bool(d.get("on")), "level": max(0.0, min(1.0, float(d.get("level", 0.7))))})
            elif p == "/api/config" and "speedLimit" in d:
                v = max(5, min(80, int(d["speedLimit"])))
                kv_set("speedLimit", v)
                add_event(f"Límite de velocidad: {v} km/h", "config")
            elif p == "/api/driver":
                DRIVERS[str(d.get("id"))[:20]] = {"target": d.get("target"), "x": d.get("x"), "y": d.get("y"),
                                                  "speed": d.get("speed"), "t": time.time()}
            elif p == "/api/driver/leave":
                DRIVERS.pop(str(d.get("id")), None)
            elif p == "/api/event":
                if d.get("type") == "speeding":
                    kv_set("speeding", kv_get("speeding", 0) + 1)
                add_event(str(d.get("msg", ""))[:120], str(d.get("type", "info"))[:20])
            elif p == "/api/reset":
                DB.execute("DELETE FROM overrides")
                DB.commit()
                kv_set("speeding", 0)
                add_event("Estado reiniciado", "config")
            else:
                return self._json({"error": "ruta"}, 404)
            return self._json({"ok": True})


# ----------------------------------------------------------------------------- arranque
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def make_cert(ip):
    d = os.path.join(HERE, "certs")
    os.makedirs(d, exist_ok=True)
    crt, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    if not os.path.exists(crt):
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
            "-keyout", key, "-out", crt, "-subj", "/CN=parkuni",
            "-addext", f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{ip}",
        ], check=True, capture_output=True)
    return crt, key


def main():
    global TLS_CTX
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--https", action="store_true")
    a = ap.parse_args()
    ip = lan_ip()
    scheme = "http"
    if a.https:
        crt, key = make_cert(ip)
        TLS_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        TLS_CTX.load_cert_chain(crt, key)
        scheme = "https"
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Conductor : {scheme}://localhost:{a.port}/        (red local: {scheme}://{ip}:{a.port}/)")
    print(f"Admin     : {scheme}://localhost:{a.port}/admin.html")
    print(f"ESP32     : POST {scheme}://{ip}:{a.port}/api/sensor")
    print(f"Base      : {os.path.abspath(DB_PATH)}")
    if not ADMIN_PASSWORD:
        print("Aviso     : falta PARKUNI_ADMIN_PASSWORD: el panel de administración queda bloqueado.")
    if not EM_PASSWORD:
        print("Aviso     : falta PARKUNI_EMERGENCY_PASSWORD: la emergencia queda bloqueada.")
    if ADMIN_LOCAL_ONLY:
        print("Panel     : solo se acepta desde esta máquina (PARKUNI_ADMIN_LOCAL_ONLY=1).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:          # Ctrl+C: cerrar limpio, sin traceback
        print("\nCerrando servidor…")
    finally:
        srv.server_close()
        DB.close()


if __name__ == "__main__":
    main()
