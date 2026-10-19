#!/usr/bin/env python3
"""Servidor de ParkUni (solo librería estándar de Python; pensado para Ubuntu Server).

  python3 server/server.py                 # http://0.0.0.0:8000
  python3 server/server.py --port 80
  python3 server/server.py --https         # https autofirmado (para probar el GPS en el celular por LAN)

Hace tres cosas:
  1. Sirve la app web (carpeta public/).
  2. Guarda el estado en SQLite (server/data/parkuni.db): sensores, cajones forzados, simulación,
     límite de velocidad, eventos y el estado de emergencia.
  3. Recibe los datos del ESP32 (POST /api/sensor) y le contesta si hay emergencia activa.

Variables de entorno:
  PARKUNI_EMERGENCY_PASSWORD   contraseña para activar/desactivar la emergencia (por defecto: anahuac2027)
  PARKUNI_DB                   ruta del archivo SQLite
"""
import argparse, hmac, json, os, socket, sqlite3, ssl, subprocess, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "public")                      # lo único que se publica
DB_PATH = os.environ.get("PARKUNI_DB", os.path.join(HERE, "data", "parkuni.db"))
EM_PASSWORD = os.environ.get("PARKUNI_EMERGENCY_PASSWORD", "anahuac2027")
STALE_S = 120            # un sensor sin reportar en este tiempo se marca "sin señal"
DRIVER_TTL = 10          # un conductor sin latido en este tiempo desaparece del panel

LOCK = threading.Lock()
DRIVERS = {}             # id -> {target, x, y, speed, t}   (solo en memoria)
FAILS = []               # intentos fallidos de contraseña (para frenar fuerza bruta)
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
        "drivers": [{"id": k, **d} for k, d in DRIVERS.items()],
        "events": [],
        "now": now,
    }
    if with_events:
        out["events"] = [{"t": t, "msg": m} for t, m in
                         DB.execute("SELECT t, msg FROM events ORDER BY id DESC LIMIT 40")]
    return out


def set_emergency(on, password):
    """Devuelve (ok, mensaje). Exige la contraseña tanto para activar como para desactivar."""
    now = time.time()
    FAILS[:] = [t for t in FAILS if now - t < 60]
    if len(FAILS) >= 5:
        return False, "Demasiados intentos. Espera un minuto."
    if not hmac.compare_digest(str(password or "").encode(), EM_PASSWORD.encode()):
        FAILS.append(now)
        add_event("Intento de emergencia con contraseña incorrecta", "security")
        return False, "Contraseña incorrecta"
    kv_set("emergency", {"on": bool(on), "since": now if on else None})
    add_event("⚠ EMERGENCIA ACTIVADA: LED en amarillo intermitente" if on else "Emergencia desactivada: LED normales", "emergency")
    return True, "ok"


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

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/status":
            with LOCK:
                return self._json(snapshot("events" in self.path))
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
                return self._json({"ok": True, "emergency": emergency_state()["on"]})
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
    if EM_PASSWORD == "anahuac2027":
        print("Aviso     : usando la contraseña de emergencia por defecto (define PARKUNI_EMERGENCY_PASSWORD).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:          # Ctrl+C: cerrar limpio, sin traceback
        print("\nCerrando servidor…")
    finally:
        srv.server_close()
        DB.close()


if __name__ == "__main__":
    main()
