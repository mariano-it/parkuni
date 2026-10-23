#!/usr/bin/env python3
"""ParkUni · menú de terminal para operar el servidor (solo librería estándar de Python).

  parkuni                                     # acceso directo que crea el instalador
  python3 server/tui/parkuni_tui.py           # abre el menú
  python3 server/tui/parkuni_tui.py --check   # verifica sin abrir la interfaz (OK / AVISO / FALLO)

Variables de entorno (opcionales):
  PARKUNI_URL       URL del servidor            (por defecto http://127.0.0.1:8000)
  PARKUNI_DB        ruta del archivo SQLite     (por defecto /opt/parkuni/server/data/parkuni.db)
  PARKUNI_SERVICE   nombre del servicio systemd (por defecto parkuni)
  PARKUNI_SERIAL    puerto serie del ESP32      (por defecto el primer /dev/ttyUSB* o /dev/ttyACM*)

Reglas: no guarda contraseñas (sudo, WiFi y emergencia se teclean en el momento y pasan directo a
quien las pide), la base se abre siempre en solo lectura y lo que necesita privilegios usa 'sudo'
por acción. Con Ctrl+C siempre se sale.
"""
import collections, contextlib, curses, glob, grp, json, locale, os, re, select, shlex, shutil, signal, socket
import sqlite3, subprocess, sys, termios, threading, time, traceback
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))        # realpath: el acceso directo "parkuni" es un enlace
URL = os.environ.get("PARKUNI_URL", "http://127.0.0.1:8000").rstrip("/")
PORT = urllib.parse.urlparse(URL).port or 80
SERVICE = os.environ.get("PARKUNI_SERVICE", "parkuni")
TUNNEL = "parkuni-tunel"                            # servicio del túnel de Cloudflare (server/parkuni-tunel.service)
TUNNEL_METRICS = os.environ.get("PARKUNI_TUNNEL_METRICS", "127.0.0.1:20241")   # cloudflared publica ahí el link (/quicktunnel)
STALE_S = 120                                    # igual que server.py: sin reportar en este tiempo = "sin señal"
EXPECTED = ["A1", "A2", "A3", "A4"]              # sensores del prototipo
ESP32_NAME = "parkuni"                           # nombre de la laptop que busca el ESP32 (SERVER_HOST del firmware)
NO_MENU = os.path.expanduser("~/.sin-menu")      # si existe, el menú no se abre solo al iniciar sesión
PROFILE = os.path.expanduser("~/.bash_profile")
LOGIND_DROPIN = "/etc/systemd/logind.conf.d/parkuni.conf"
BACKUP_PY = os.path.join(HERE, "..", "respaldo.py")          # el script que hace la copia de la base
BACKUP_DIR = os.path.expanduser("~/respaldos-parkuni")       # en tu carpeta personal: fácil de copiar a una USB

UTF8 = locale.getpreferredencoding(False).lower().replace("-", "") == "utf8" and os.environ.get("TERM") != "linux"
DOT, RING = ("●", "○") if UTF8 else ("*", "o")    # la consola de Linux (TTY) no siempre trae estos símbolos

LOGO = r"""
 ____            _    _   _       _
|  _ \ __ _ _ __| | _| | | |_ __ (_)
| |_) / _` | '__| |/ / | | | '_ \| |
|  __/ (_| | |  |   <| |_| | | | | |
|_|   \__,_|_|  |_|\_\\___/|_| |_|_|""".strip("\n").splitlines()

A = collections.defaultdict(int)                 # atributos de color por nombre (se llenan en init_attrs)


def find_db():
    if os.environ.get("PARKUNI_DB"):
        return os.environ["PARKUNI_DB"]
    for p in ("/opt/parkuni/server/data/parkuni.db", os.path.join(HERE, "..", "data", "parkuni.db")):
        if os.path.exists(p):
            return os.path.abspath(p)
    return "/opt/parkuni/server/data/parkuni.db"


DB_PATH = find_db()


# ----------------------------------------------------------------------------- utilidades
def sh(cmd, timeout=3):
    """Corre un comando y devuelve (código, salida). Nunca lanza. LC_ALL=C para que la salida sea estable."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C"))
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def split_t(line):
    """Separa una línea de 'nmcli -t' (campos con ':' y los ':' del contenido escapados como '\\:')."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


def rd(path):
    with open(path) as f:
        return f.read()


def gateway():
    m = re.search(r"default via (\S+)", sh(["ip", "-4", "route", "show", "default"])[1])
    return m.group(1) if m else None


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None


def ago(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60} min"


def fulltime(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fit(s, w):
    return str(s)[:w].ljust(w)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),     # sin proxy: es el propio equipo
                                      urllib.request.HTTPCookieProcessor())  # guarda la sesión de admin solo en memoria


def api(path, body=None, timeout=1.5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:           # el servidor contesta JSON también en errores (p. ej. 403)
        return json.load(e)


def api_admin(app, path, body, timeout=2):
    """Como api(), pero para las rutas del administrador: si el servidor pide sesión, pide la contraseña del panel
    (no se guarda en ningún lado; solo queda la cookie de sesión en memoria) y repite la llamada una vez."""
    r = api(path, body, timeout)
    if r.get("error") != "auth":
        return r
    pw = app.prompt("Contraseña del panel de administración", secret=True)
    if pw is None:
        return {"ok": False, "error": "Cancelado."}
    r = api("/api/login", {"password": pw}, timeout)
    if not r.get("ok"):
        return r
    return api(path, body, timeout)


def db_ro():
    c = sqlite3.connect(f"file:{urllib.parse.quote(DB_PATH)}?mode=ro", uri=True, timeout=1)
    c.execute("PRAGMA query_only=ON")
    return c


def db_all(sql, args=()):
    with contextlib.closing(db_ro()) as c:
        return c.execute(sql, args).fetchall()


def read_sensors(api_st):
    """Sensores con su último reporte. La base da la hora exacta; si no se puede leer, se usa la API."""
    try:
        rows = db_all("SELECT id, occupied, distance_cm, updated FROM sensors ORDER BY id")
        return [dict(id=i, occ=bool(o), cm=cm, t=t) for i, o, cm, t in rows], None
    except Exception as e:
        err = str(e)
    if api_st:
        return [dict(id=sid, occ=bool(api_st["slots"].get(sid)), cm=api_st["distances"].get(sid), t=None,
                     stale=sid in api_st["stale"]) for sid in sorted(api_st["seen"])], err
    return [], err


def sensor_view(sensors):
    by, now, out = {r["id"]: r for r in sensors}, time.time(), []
    for sid in EXPECTED + sorted(set(by) - set(EXPECTED)):
        r = by.get(sid)
        if r is None:
            out.append(dict(id=sid, state="sin datos", cm=None, ago=None))
            continue
        age = None if r["t"] is None else now - r["t"]
        stale = r.get("stale") or (age is not None and age > STALE_S)
        out.append(dict(id=sid, state="sin señal" if stale else "ocupado" if r["occ"] else "libre", cm=r["cm"], ago=age))
    return out


STATE_ATTR = {"libre": "ok", "ocupado": "bad", "sin señal": "warn", "sin datos": "dim"}


def read_sys(prev_cpu):
    out = {}
    try:
        parts = [int(x) for x in rd("/proc/stat").splitlines()[0].split()[1:]]
        total, idle = sum(parts[:8]), parts[3] + parts[4]
        if prev_cpu and total > prev_cpu[0]:
            out["cpu"] = 100.0 * (1 - (idle - prev_cpu[1]) / (total - prev_cpu[0]))
        out["_cpu"] = (total, idle)
    except (OSError, ValueError, IndexError):
        pass
    try:
        mi = {l.split(":")[0]: int(l.split()[1]) for l in rd("/proc/meminfo").splitlines() if ":" in l}
        out["mem_total"], out["mem_used"] = mi["MemTotal"], mi["MemTotal"] - mi["MemAvailable"]
    except (OSError, ValueError, KeyError):
        pass
    temps = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(rd(p)) / 1000)
        except (OSError, ValueError):
            pass
    if temps:
        out["temp"] = max(temps)
    for b in glob.glob("/sys/class/power_supply/BAT*"):
        try:
            out["bat"] = (int(rd(b + "/capacity")), rd(b + "/status").strip())
            break
        except (OSError, ValueError):
            pass
    return out


class Poller(threading.Thread):
    """Junta el estado cada segundo en segundo plano (la interfaz solo lo lee)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.s = dict(api=None, api_err=None, svc="?", enabled="?", target="?", gui=False, ip=None, ifname=None,
                      wifi=None, signal=None, gw=None, sensors=[], db_err=None, em=None, sys={})
        self._cpu, self._slow, self.wake = None, 0.0, threading.Event()

    def kick(self):                               # refrescar ya (p. ej. después de cambiar de red)
        self._slow = 0.0
        self.wake.set()

    def run(self):
        while True:
            try:
                self.once()
            except Exception:
                pass
            self.wake.wait(1.0)
            self.wake.clear()

    def once(self):
        s = self.s
        try:
            s["api"], s["api_err"] = api("/api/status"), None
        except Exception as e:
            s["api"], s["api_err"] = None, type(e).__name__
        s["sensors"], s["db_err"] = read_sensors(s["api"])
        if s["api"]:
            s["em"] = s["api"]["emergency"]
        else:
            try:
                s["em"] = json.loads(db_all("SELECT v FROM kv WHERE k='emergency'")[0][0])
            except Exception:
                s["em"] = None
        s["ip"] = lan_ip()
        sy = read_sys(self._cpu)
        self._cpu = sy.pop("_cpu", self._cpu)
        s["sys"] = sy
        if time.monotonic() - self._slow < 3:
            return
        self._slow = time.monotonic()
        s["svc"] = (sh(["systemctl", "is-active", SERVICE])[1].splitlines() or ["?"])[0]
        s["enabled"] = (sh(["systemctl", "is-enabled", SERVICE])[1].splitlines() or ["?"])[0]
        s["target"] = sh(["systemctl", "get-default"])[1]
        s["gui"] = sh(["systemctl", "is-active", "graphical.target"])[1] == "active"
        s["gw"] = gateway()
        wifi = None
        for l in sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])[1].splitlines():
            f = split_t(l)
            if len(f) >= 4 and f[1] == "wifi":
                wifi = f
                break
        s["ifname"], s["wifi"], s["signal"] = (wifi[0] if wifi else None), None, None
        if wifi and wifi[2].startswith("connected"):
            s["wifi"] = wifi[3]
            for l in sh(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list", "--rescan", "no"])[1].splitlines():
                f = split_t(l)
                if len(f) >= 3 and f[0] == "*":
                    s["wifi"], s["signal"] = f[1] or wifi[3], int(f[2] or 0)
                    break


# ----------------------------------------------------------------------------- colores
def init_attrs():
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW      # naranja Anáhuac
        for i, (name, fg) in enumerate([("ok", curses.COLOR_GREEN), ("bad", curses.COLOR_RED), ("warn", orange),
                                        ("head", curses.COLOR_CYAN), ("em", curses.COLOR_MAGENTA)], 1):
            curses.init_pair(i, fg, bg)
            A[name] = curses.color_pair(i) | curses.A_BOLD
        curses.init_pair(10, curses.COLOR_BLACK, orange)
        A["sel"] = curses.color_pair(10) | curses.A_BOLD
        for i, (name, bgc) in enumerate([("red", curses.COLOR_RED), ("green", curses.COLOR_GREEN), ("blue", curses.COLOR_BLUE),
                                         ("yellow", curses.COLOR_YELLOW), ("purple", curses.COLOR_MAGENTA),
                                         ("cyan", curses.COLOR_CYAN), ("white", curses.COLOR_WHITE)], 20):
            curses.init_pair(i, curses.COLOR_BLACK, bgc)             # bloque de color para la prueba de LED
            A["led_" + name] = curses.color_pair(i)
    else:
        A["sel"] = curses.A_REVERSE
        for name in ("ok", "bad", "warn", "head", "em"):
            A[name] = curses.A_BOLD
    for name in ("red", "green", "blue", "yellow", "purple", "cyan", "white"):
        A["led_" + name] = A["led_" + name] if "led_" + name in A else curses.A_REVERSE     # sin color: bloque invertido
    A["dim"] = curses.A_DIM
    A["bold"] = curses.A_BOLD


# ----------------------------------------------------------------------------- aplicación
class Bg:
    """Mixin: refresca datos lentos (subprocesos) en un hilo para no congelar la interfaz."""
    busy = False

    def bg(self, fn):
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                fn()
            except Exception:
                pass
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()


class Screen:
    def tick(self, app): pass
    def draw(self, app, h, w): pass
    def key(self, app, k): return None


class App:
    def __init__(self, scr, poller):
        self.scr, self.p = scr, poller
        self.stack = [MainMenu()]
        self.flash = ("", 0, "ok")
        self.btop = shutil.which("btop")
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        scr.keypad(True)
        init_attrs()

    # ---- dibujo básico
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]           # la última celda de la pantalla no se escribe
        try:
            self.scr.addstr(y, x, text, attr)
        except (curses.error, ValueError):      # ValueError: texto con NUL; mejor perder una línea que cerrar el menú
            pass

    def segs(self, y, x, parts):
        for text, attr in parts:
            self.put(y, x, text, attr)
            x += len(text)

    def hline(self, y, w, left="+", mid="-", right="+"):
        self.put(y, 1, left + mid * (w - 4) + right, A["dim"])

    def say(self, text, kind="ok"):
        self.flash = (text, time.time() + 6, kind)

    def frame(self, title, help_text, h, w):
        self.put(0, 0, f" ParkUni > {title}".ljust(w), A["sel"])
        clock = time.strftime("%H:%M:%S ")
        self.put(0, w - len(clock), clock, A["sel"])
        self.put(h - 1, 0, " " + help_text, A["dim"])
        text, until, kind = self.flash
        if text and time.time() < until:
            self.put(h - 2, 1, text, A[kind])

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 20 or w < 60:
            self.put(0, 0, "Terminal muy pequeña (mínimo 60x20).", A["warn"])
            self.put(1, 0, "Agranda la ventana o reduce la fuente.")
        else:
            self.stack[-1].draw(self, h, w)
        scr.refresh()

    # ---- ventanas emergentes
    def _box(self, lines, attr):
        h, w = self.scr.getmaxyx()
        width = min(w - 2, max(len(l) for l in lines) + 6)
        x0, y0 = (w - width) // 2, max(1, (h - len(lines) - 2) // 2)
        for r in range(y0, y0 + len(lines) + 2):                        # limpia las filas detrás del cuadro
            self.put(r, 0, " " * (w - 1))
        self.put(y0, x0, "+" + "-" * (width - 2) + "+", attr)
        for i, l in enumerate(lines, 1):
            self.put(y0 + i, x0, "|" + (" " + l).ljust(width - 2) + "|", attr)
        self.put(y0 + len(lines) + 1, x0, "+" + "-" * (width - 2) + "+", attr)

    def confirm(self, lines):
        """Pregunta sí/no (por defecto NO). Devuelve True solo con s/y."""
        self.scr.timeout(-1)
        try:
            self.draw()
            self._box(list(lines) + ["", "[s] sí    [N] no"], A["warn"])
            self.scr.refresh()
            try:
                return self.scr.getch() in (ord("s"), ord("S"), ord("y"), ord("Y"))
            except KeyboardInterrupt:
                return False
        finally:
            self.scr.timeout(250)

    def notice(self, lines):
        self.scr.timeout(-1)
        try:
            self.draw()
            self._box(list(lines) + ["", "(cualquier tecla para continuar)"], A["head"])
            self.scr.refresh()
            try:
                self.scr.getch()
            except KeyboardInterrupt:
                pass
        finally:
            self.scr.timeout(250)

    def prompt(self, label, secret=False):
        """Pide un texto (Esc cancela). Con secret=True no se ve lo que se escribe y no se guarda en ningún lado."""
        buf, scr = "", self.scr
        scr.timeout(-1)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                self.draw()
                h, w = scr.getmaxyx()
                text = f"{label}: " + ("*" * len(buf) if secret else buf)
                self.put(h - 3, 0, " " * (w - 1))
                self.put(h - 3, 1, text, A["warn"])
                scr.move(h - 3, min(w - 2, 1 + len(text)))
                scr.refresh()
                try:
                    k = scr.get_wch()
                except KeyboardInterrupt:
                    return None
                if k in ("\n", "\r") or k == curses.KEY_ENTER:
                    return buf
                if k == "\x1b":
                    return None
                if k in ("\x7f", "\b") or k == curses.KEY_BACKSPACE:
                    buf = buf[:-1]
                elif isinstance(k, str) and k.isprintable():
                    buf += k
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            scr.timeout(250)

    # ---- programas externos (sudo, nmcli, btop…): se suspende la interfaz y se devuelve al terminar
    def external(self, *cmds, pause=True):
        """pause: True = siempre espera Enter, 'error' = solo si falla, False = vuelve directo."""
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout.write("\x1b[2J\x1b[H")
        rc = 0
        old = signal.signal(signal.SIGINT, lambda *a: None)     # Ctrl+C va solo al programa externo (se restaura al exec)
        try:
            for cmd in cmds:
                print("$ " + " ".join(shlex.quote(c) for c in cmd), flush=True)
                try:
                    rc = subprocess.call(cmd)
                except OSError as e:
                    print(f"No se pudo ejecutar: {e}")
                    rc = 127
                if rc:
                    break
        finally:
            signal.signal(signal.SIGINT, old)
        if pause is True or (pause == "error" and rc):
            print(f"\n[{'listo' if not rc else f'terminó con código {rc}'}]  Enter para volver al menú… ", end="", flush=True)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        curses.reset_prog_mode()
        self.scr.clear()
        self.scr.refresh()
        self.p.kick()
        return rc

    def sudo(self, *cmd, pause=True):
        return self.external(["sudo", *cmd], pause=pause)

    # ---- acciones compartidas
    def run_btop(self):
        if not self.btop:
            self.say("btop no está instalado. Instálalo con: sudo dnf install btop", "warn")
            return
        cmd = [self.btop]
        if os.environ.get("TERM") == "linux" and "tty_mode" in sh([self.btop, "--help"])[1]:
            cmd.append("--tty_mode")                  # la consola de Linux solo tiene 16 colores
        self.external(cmd, pause="error")

    def menu_autostart(self):
        return not os.path.exists(NO_MENU)

    def toggle_menu(self):
        if self.menu_autostart():
            if self.confirm(["El menú dejará de abrirse solo al iniciar sesión.",
                             "Al entrar caerás directo en la terminal de Fedora.",
                             "Para abrirlo a mano: escribe  parkuni", "¿Desactivarlo?"]):
                try:
                    open(NO_MENU, "a").close()
                    self.say("Menú de inicio DESACTIVADO. Escribe 'parkuni' para abrirlo cuando quieras.", "warn")
                except OSError as e:
                    self.say(f"No se pudo crear {NO_MENU}: {e}", "bad")
        else:
            try:
                os.remove(NO_MENU)
                self.say("Menú de inicio ACTIVADO: se abrirá en el próximo inicio de sesión.")
            except OSError as e:
                self.say(f"No se pudo borrar {NO_MENU}: {e}", "bad")

    def run(self):
        scr = self.scr
        while self.stack:
            scr.timeout(250)
            top = self.stack[-1]
            top.tick(self)
            self.draw()
            try:
                k = scr.getch()
            except KeyboardInterrupt:
                k = 27                                # Ctrl+C = volver / salir (siempre hay salida)
            if k in (-1, curses.KEY_RESIZE):
                continue
            r = top.key(self, k)
            if r == "back":
                self.stack.pop()
            elif r == "exit":
                return
            elif isinstance(r, Screen):
                self.stack.append(r)


# ----------------------------------------------------------------------------- menú principal
class MainMenu(Screen):
    def __init__(self):
        self.sel = 0
        self.items = [                                # (atajo, texto, acción -> Screen | "exit" | None)
            ("1", "Sensores en vivo", lambda app: SensorsScreen()),
            ("2", "Base de datos", lambda app: DbScreen()),
            ("3", "Aplicación (servidor web)", lambda app: AppScreen()),
            ("4", "Red / WiFi", lambda app: WifiScreen(app)),
            ("5", "Logs", lambda app: LogsScreen()),
            ("6", "Emergencia", lambda app: EmergencyScreen()),
            ("x", "Apagado de emergencia informática", lambda app: KillScreen()),
            ("l", "Prueba de LED RGB", lambda app: LedTestScreen()),
            ("b", "Respaldo de la base", lambda app: BackupScreen()),
            ("i", "Internet / cerrar el panel", lambda app: InternetScreen()),
            ("7", "Monitor del equipo (btop)", lambda app: app.run_btop()),
            ("s", "Monitor serie del ESP32", lambda app: SerialScreen()),
            ("8", "Entorno gráfico de Fedora", lambda app: DesktopScreen()),
            ("9", "Menú al iniciar sesión", lambda app: app.toggle_menu()),
            ("q", "Salir a la terminal de Fedora", lambda app: "exit"),
            ("p", "Apagar / reiniciar", lambda app: PowerScreen()),
        ]

    def hint(self, app, key):
        s = app.p.s
        if key == "1":
            v = sensor_view(s["sensors"])
            return f"{sum(r['state'] == 'libre' for r in v)} libres · {sum(r['state'] == 'ocupado' for r in v)} ocupados", "dim"
        if key == "3":
            ok = s["svc"] == "active" or s["api"] is not None
            return (s["svc"] if s["svc"] != "?" else "sin systemd"), ("ok" if ok else "bad")
        if key == "4":
            return (f"{s['wifi']}" if s["wifi"] else "sin conexión"), ("ok" if s["wifi"] else "warn")
        if key == "6":
            on = bool(s["em"] and s["em"].get("on"))
            return ("ACTIVA" if on else "normal"), ("bad" if on else "dim")
        if key == "l":
            n = len((s["api"] or {}).get("ledTest", {}))
            return (f"{n} LED en prueba" if n else "diagnóstico de los LED"), ("warn" if n else "dim")
        if key == "i":
            lock = bool((s["api"] or {}).get("lockdown"))
            on = sh(["systemctl", "is-active", TUNNEL])[1].strip() == "active"
            return (("túnel ACTIVO" if on else "túnel apagado") + ("  ·  panel CERRADO a internet" if lock else "")), ("warn" if on and not lock else "dim")
        if key == "b":
            c = backups()
            return (f"{len(c)} copia(s) · última hace {ago(time.time() - c[0][2])}" if c else "sin copias todavía"), ("dim" if c else "warn")
        if key == "7":
            return ("" if app.btop else "no instalado"), "warn"
        if key == "s":
            return serial_status()
        if key == "8":
            return (("encendido" if s["gui"] else "apagado") + f"  (arranque: {s['target'].replace('.target', '')})"), ("warn" if s["gui"] else "ok")
        if key == "9":
            return ("ACTIVO" if app.menu_autostart() else "DESACTIVADO"), ("ok" if app.menu_autostart() else "warn")
        return "", "dim"

    def draw_status(self, app, y, w):
        s, sy = app.p.s, app.p.s["sys"]
        # servicio · IP · emergencia
        if s["svc"] == "active" and s["api"] is not None:
            svc = (f"{DOT} activo", "ok")
        elif s["svc"] == "active":
            svc = (f"{DOT} activo (sin respuesta)", "warn")
        elif s["api"] is not None:
            svc = (f"{DOT} manual", "ok")                    # corriendo sin systemd (p. ej. python3 server.py)
        else:
            svc = (f"{RING} {s['svc']}", "bad")
        em_on = bool(s["em"] and s["em"].get("on"))
        app.segs(y, 3, [("Servicio: ", 0), (svc[0], A[svc[1]]), ("   IP: ", 0),
                        (f"{s['ip']}:{PORT}" if s["ip"] else "sin red", A["bold"] if s["ip"] else A["warn"]),
                        ("   Emergencia: ", 0), ("ACTIVA" if em_on else "NORMAL", A["em"] if em_on else A["ok"])])
        # sensores
        parts = [("Sensores: ", 0)]
        for r in sensor_view(s["sensors"]):
            at = A[STATE_ATTR[r["state"]]]
            parts += [(f"{r['id']} ", 0), (DOT if r["state"] in ("libre", "ocupado") else RING, at), (f" {r['state']}  ", 0)]
        app.segs(y + 1, 3, parts)
        # equipo y red
        parts = [("Equipo  : ", 0)]
        if "cpu" in sy:
            parts.append((f"CPU {sy['cpu']:.0f}%  ", 0))
        if "mem_total" in sy:
            parts.append((f"RAM {sy['mem_used'] / 1048576:.1f}/{sy['mem_total'] / 1048576:.1f}G  ", 0))
        if "temp" in sy:
            parts.append((f"{sy['temp']:.0f}°C  ", A["bad"] if sy["temp"] > 80 else A["warn"] if sy["temp"] > 70 else 0))
        if "bat" in sy:
            pct, st = sy["bat"]
            parts.append((f"Bat {pct}% {'(en batería!)' if st == 'Discharging' else '(AC)'}  ", A["warn"] if st == "Discharging" else 0))
        parts.append((f"WiFi: {s['wifi']}" + (f" {s['signal']}%" if s["signal"] is not None else "") if s["wifi"] else "WiFi: sin conexión",
                      0 if s["wifi"] else A["warn"]))
        app.segs(y + 2, 3, parts)

    def draw(self, app, h, w):
        big = h >= 24 and w >= 78
        if big:
            for i, line in enumerate(LOGO):
                app.put(i, 1, line, A["warn"])
            app.put(2, 42, "Universidad Anáhuac Cancún", A["dim"])
            app.put(3, 42, "Software server by: Mariano A.", A["dim"])
            app.put(4, 42, "Estacionamiento inteligente", A["head"])
            y = len(LOGO)
        else:
            app.segs(0, 1, [("P A R K U N I", A["warn"]), ("   Estacionamiento inteligente · servidor", A["head"])])
            y = 1
        app.hline(y, w)
        self.draw_status(app, y + 1, w)
        app.hline(y + 4, w)
        y += 5
        for i, (key, text, _) in enumerate(self.items):
            row = y + i
            sel = i == self.sel
            line = f" {'>' if sel else ' '} {key}  {text}"
            app.put(row, 2, line.ljust(w - 5) if sel else line, A["sel"] if sel else 0)
            hint, kind = self.hint(app, key)
            if hint:
                app.put(row, 42, hint, A["sel"] if sel else A[kind])
        text, until, kind = app.flash
        if text and time.time() < until:
            app.put(h - 2, 1, text, A[kind])
        app.put(h - 1, 0, " ↑↓ mover  Enter elegir  1-9 l b i s atajo  q terminal  p apagar  Ctrl+C salir"
                if UTF8 else " Up/Down mover  Enter elegir  1-9 l b i s atajo  q terminal  p apagar  Ctrl+C salir", A["dim"])

    def key(self, app, k):
        n = len(self.items)
        if k in (curses.KEY_UP, ord("k")):
            self.sel = (self.sel - 1) % n
        elif k in (curses.KEY_DOWN, ord("j")):
            self.sel = (self.sel + 1) % n
        elif k == curses.KEY_HOME:
            self.sel = 0
        elif k == curses.KEY_END:
            self.sel = n - 1
        elif k in (10, 13, curses.KEY_ENTER):
            return self.items[self.sel][2](app)
        elif k == 27:
            return "exit"
        elif 0 < k < 256:
            for i, (key, _, act) in enumerate(self.items):
                if chr(k).lower() == key:
                    self.sel = i
                    return act(app)
        return None


# ----------------------------------------------------------------------------- sensores
class SensorsScreen(Screen):
    def draw(self, app, h, w):
        app.frame("Sensores en vivo", "q volver", h, w)
        s = app.p.s
        v = sensor_view(s["sensors"])
        app.put(2, 2, f"{'ID':<6}{'Cajón':<10}{'Estado':<13}{'Distancia':<13}Último reporte", A["head"])
        app.put(3, 2, "-" * (w - 4), A["dim"])
        for i, r in enumerate(v):
            at = A[STATE_ATTR[r["state"]]]
            dot = DOT if r["state"] in ("libre", "ocupado") else RING
            cm = "-" if r["cm"] is None else ("sin eco" if r["cm"] == 0 else f"{r['cm']} cm")
            app.segs(4 + i, 2, [(fit(r["id"], 6), 0), (fit(f"Cajón {r['id'][1:]}" if re.fullmatch(r"A\d+", r["id"]) else "", 10), 0),
                                (dot + " ", at), (fit(r["state"], 11), at), (fit(cm, 13), 0),
                                ("-" if r["ago"] is None else f"hace {ago(r['ago'])}", 0)])
        y = 5 + len(v)
        app.put(y, 2, f"Libres: {sum(r['state'] == 'libre' for r in v)}   Ocupados: {sum(r['state'] == 'ocupado' for r in v)}"
                      f"   Sin señal: {sum(r['state'] == 'sin señal' for r in v)}")
        if s["api"] is not None:
            app.put(y + 1, 2, "ESP32: " + ("enviando datos" if s["api"]["esp"] else "sin señal (revisa alimentación y WiFi)"),
                    A["ok"] if s["api"]["esp"] else A["warn"])
        else:
            app.put(y + 1, 2, f"Servidor sin respuesta ({s['api_err']}): mostrando solo lo guardado en la base.", A["warn"])
        if s["db_err"]:
            app.put(y + 2, 2, f"Base de datos no disponible: {s['db_err']}", A["warn"])
        app.put(y + 4, 2, "Los reportes del ESP32 llegan a:", A["dim"])
        app.put(y + 5, 4, f"POST http://{s['ip'] or '<IP>'}:{PORT}/api/sensor", A["bold"])
        app.put(y + 6, 2, "Más de 120 s sin reportar = sin señal.  'sin eco' = no hay nada enfrente del sensor.", A["dim"])

    def key(self, app, k):
        return "back" if k in (ord("q"), 27) else None


# ----------------------------------------------------------------------------- monitor serie del ESP32
BAUDS = [115200, 9600, 57600, 230400, 460800, 921600]       # el firmware usa 115200


def serial_ports():
    """Puertos USB-serie: el ESP32 aparece como ttyUSB* o ttyACM*. PARKUNI_SERIAL fuerza uno."""
    if os.environ.get("PARKUNI_SERIAL"):
        return [os.environ["PARKUNI_SERIAL"]]
    return sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))


def serial_group(path):
    try:
        return grp.getgrgid(os.stat(path).st_gid).gr_name
    except (OSError, KeyError):
        return "dialout"


def serial_status():
    """(texto, color) para el menú."""
    ports = serial_ports()
    if not ports:
        return "sin puerto (ESP32 por USB)", "dim"
    if not os.access(ports[0], os.R_OK | os.W_OK):
        return f"sin permiso (grupo {serial_group(ports[0])})", "warn"
    return ports[0], "ok"


def serial_attr(text):
    """Color de una línea del firmware."""
    if "emergencia=1" in text:
        return "em"
    m = re.search(r"POST .* -> (-?\d+)", text)
    if m:
        return "ok" if m.group(1) == "200" else "bad"
    if "no respondio" in text:
        return "warn"
    if text.startswith("Servidor:"):
        return "ok"
    if "ROJO" in text:
        return "bad"
    if "VERDE" in text:
        return "ok"
    if text.startswith("---") or "Fuera de rango" in text:
        return "dim"
    return ""


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def serial_text(raw):
    """Bytes del puerto -> texto seguro para curses. Al reiniciarse (o con la velocidad equivocada) el ESP32 suelta
    NUL y escapes; un NUL hace que addstr lance ValueError y tumbaba el menú entero."""
    return _CTRL.sub("", _ANSI.sub("", raw.decode("utf-8", "replace")))


class SerialMon(threading.Thread):
    """Lee el puerto serie del ESP32 en un hilo y guarda las últimas líneas.
    El puerto solo se abre mientras la pantalla está abierta (al abrirlo, algunas placas se reinician)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lines = collections.deque(maxlen=2000)
        self.port, self.want, self.baud = None, None, BAUDS[0]
        self.state, self.err, self.total = "buscando puerto…", "", 0
        self.hold, self.alive, self.reset, self.fd = False, True, False, None

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _configure(self, fd):
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0                       # sin conversiones, sin eco, sin modo canónico
        a[2] = (a[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL)) | termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = getattr(termios, f"B{self.baud}")
        a[6][termios.VMIN], a[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, a)    # HUPCL apagado: al cerrar no se baja DTR (no reinicia la placa)

    def _open(self):
        ports = serial_ports()
        port = self.want if self.want in ports else (ports[0] if ports else None)
        self.port = port
        if not port:
            self.state, self.err = "sin puerto", "Conecta el ESP32 por USB (con un cable de datos, no solo de carga)."
            return
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            self.state, self.err = "sin permiso", f"sudo usermod -aG {serial_group(port)} $USER   y vuelve a iniciar sesión"
            return
        except OSError as e:
            self.state, self.err = "no se pudo abrir", str(e)
            return
        try:
            self._configure(fd)
        except (OSError, termios.error) as e:
            os.close(fd)
            self.state, self.err = "no se pudo configurar", str(e)
            return
        self.fd, self.state, self.err = fd, "conectado", ""

    def run(self):
        buf = b""
        while self.alive:
            if self.reset:                           # el usuario soltó el puerto o cambió de puerto / velocidad
                self._close()
                self.reset, buf = False, b""
            fd = self.fd                             # copia local: solo este hilo abre y cierra el puerto
            if fd is None:
                if not self.hold:
                    self._open()
                time.sleep(1.0)
                continue
            try:
                ready = select.select([fd], [], [], 0.3)[0]
                data = os.read(fd, 4096) if ready else b""
                if ready and not data:
                    raise OSError("el puerto se cerró")
            except (OSError, ValueError):
                self._close()
                self.state, self.err, buf = "desconectado", "Se perdió el USB; reintentando…", b""
                continue
            buf += data
            *done, buf = buf.split(b"\n")
            for raw in done:
                self.lines.append(serial_text(raw))
                self.total += 1
            if len(buf) > 4096:                      # basura sin salto de línea (velocidad equivocada)
                self.lines.append(serial_text(buf))
                self.total, buf = self.total + 1, b""
        self._close()

    def stop(self):
        self.alive = False                           # el hilo cierra el puerto al salir del bucle (no desde aquí)


class SerialScreen(Screen):
    def __init__(self):
        self.mon = SerialMon()
        self.mon.start()
        self.q, self.off, self.follow = "", 0, True

    def draw(self, app, h, w):
        m = self.mon
        app.frame("Monitor serie del ESP32", "↑↓ PgUp/PgDn mover  End seguir  / filtrar  c limpiar  q volver" if UTF8
                  else "Up/Down PgUp/PgDn mover  End seguir  / filtrar  c limpiar  q volver", h, w)
        ok = m.state == "conectado"
        app.segs(1, 2, [("Puerto: ", 0), (m.port or "-", A["bold"]), (f"   {m.baud} baud   ", 0),
                        ((DOT if ok else RING) + " " + m.state, A["ok"] if ok else A["warn"]),
                        (f"   {m.total} líneas", A["dim"]), ("   [siguiendo]" if self.follow else "", A["ok"]),
                        (f"   filtro: '{self.q}'" if self.q else "", A["warn"])])
        if m.err:
            app.put(2, 2, m.err[:w - 4], A["warn"])
        else:
            app.put(2, 2, "b baudios   o otro puerto   x soltar el puerto (para cargar firmware desde esta laptop)", A["dim"])
        q = self.q.lower()
        lines = [l for l in list(m.lines) if q in l.lower()] if q else list(m.lines)
        room = h - 5
        self.off = max(0, len(lines) - room) if self.follow else max(0, min(self.off, len(lines) - room))
        if not lines:
            for i, t in enumerate(["Aquí aparece lo que imprime el ESP32 (Serial a 115200).",
                                   "Conéctalo por USB. Al abrir el puerto algunas placas se reinician,",
                                   "y se ve el arranque completo (WiFi, 'Servidor: parkuni.local -> …', POST).",
                                   "Si no se reinicia, pulsa el botón EN de la placa."]):
                app.put(4 + i, 4, t, A["dim"])
        for i, text in enumerate(lines[self.off:self.off + room]):
            app.put(3 + i, 1, text[:w - 3], A[serial_attr(text)] if serial_attr(text) else 0)

    def key(self, app, k):
        m = self.mon
        if k in (ord("q"), 27):
            m.stop()
            return "back"
        if k == curses.KEY_UP:
            self.follow, self.off = False, self.off - 1
        elif k == curses.KEY_DOWN:
            self.off += 1
        elif k == curses.KEY_PPAGE:
            self.follow, self.off = False, self.off - 15
        elif k == curses.KEY_NPAGE:
            self.off += 15
        elif k in (curses.KEY_END, ord("G")):
            self.follow = True
        elif k == ord("c"):
            m.lines.clear()
            m.total = 0
        elif k == ord("b"):
            m.baud = BAUDS[(BAUDS.index(m.baud) + 1) % len(BAUDS)]
            m.reset = True
            app.say(f"Velocidad: {m.baud} baud (el firmware usa 115200).", "warn" if m.baud != 115200 else "ok")
        elif k == ord("o"):
            ports = serial_ports()
            if len(ports) > 1:
                m.want = ports[(ports.index(m.port) + 1) % len(ports)] if m.port in ports else ports[0]
                m.reset = True
            else:
                app.say("Solo hay un puerto serie disponible.", "warn")
        elif k == ord("x"):
            m.hold = not m.hold
            m.reset = m.hold
            if m.hold:
                m.state, m.err = "puerto liberado", "Puerto libre para cargar el firmware desde esta laptop. Pulsa x para reconectar."
            else:
                m.state, m.err = "reconectando…", ""
        elif k == ord("/"):
            t = app.prompt("Filtrar (vacío = quitar filtro)")
            if t is not None:
                self.q, self.follow = t.strip(), True
        return None


# ----------------------------------------------------------------------------- base de datos (solo lectura)
class DbScreen(Screen):
    def __init__(self):
        self.mode, self.sel, self.tables, self.err = "tables", 0, [], None
        self.table, self.cols, self.rows, self.total, self.q, self.off, self.last = None, [], [], 0, "", 0, 0
        self.load_tables()

    def load_tables(self):
        try:
            with contextlib.closing(db_ro()) as c:
                names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                self.tables = [(n, c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names]
            self.err = None
        except Exception as e:
            self.tables, self.err = [], str(e)

    @staticmethod
    def cell(col, v):
        if v is None:
            return "NULL"
        if col in ("t", "updated") and isinstance(v, (int, float)):
            return fulltime(v)
        return str(v).replace("\n", " ")

    def load_rows(self):
        try:
            with contextlib.closing(db_ro()) as c:
                cur = c.execute(f'SELECT * FROM "{self.table}" ORDER BY rowid DESC LIMIT 1000')
                self.cols = [d[0] for d in cur.description]
                raw = cur.fetchall()
                self.total = c.execute(f'SELECT COUNT(*) FROM "{self.table}"').fetchone()[0]
            self.rows = [[self.cell(c, v) for c, v in zip(self.cols, r)] for r in raw]
            self.err = None
        except Exception as e:
            self.err = str(e)
        self.last = time.time()

    def shown(self):
        q = self.q.lower()
        return [r for r in self.rows if q in " ".join(r).lower()] if q else self.rows

    def tick(self, app):
        if time.time() - self.last > 2:
            if self.mode == "rows":
                self.load_rows()
            else:
                self.load_tables()
                self.last = time.time()

    def draw(self, app, h, w):
        if self.mode == "tables":
            app.frame("Base de datos (solo lectura)", "↑↓ mover   Enter abrir tabla   q volver" if UTF8 else "Up/Down mover   Enter abrir   q volver", h, w)
            try:
                size = f"{os.path.getsize(DB_PATH) / 1024:.0f} KB"
            except OSError:
                size = "no existe"
            app.put(2, 2, f"Archivo: {DB_PATH}  ({size})", A["dim"])
            if self.err:
                app.put(4, 2, f"No se pudo abrir la base: {self.err}", A["bad"])
                app.put(5, 2, "¿El servicio ya se inició alguna vez? El servidor crea la base al arrancar.", A["dim"])
                return
            app.put(4, 2, f"{'Tabla':<16}{'Filas':>8}   Contenido", A["head"])
            about = {"sensors": "último estado de cada sensor", "overrides": "cajones forzados desde el panel",
                     "kv": "ajustes (límite, simulación, emergencia…)", "events": "bitácora (últimos 500)"}
            for i, (n, c) in enumerate(self.tables):
                sel = i == self.sel
                app.put(5 + i, 2, f"{'>' if sel else ' '} {n:<14}{c:>8}   {about.get(n, '')}".ljust(w - 5), A["sel"] if sel else 0)
        else:
            shown = self.shown()
            app.frame(f"Base de datos > {self.table}", "↑↓ PgUp/PgDn mover   / buscar   r recargar   q volver" if UTF8 else "Up/Down PgUp/PgDn mover   / buscar   r recargar   q volver", h, w)
            app.put(1, 2, f"{self.total} filas (últimas {len(self.rows)})" + (f"   filtro: '{self.q}' → {len(shown)}" if self.q else ""), A["dim"])
            if self.err:
                app.put(3, 2, f"Error: {self.err}", A["bad"])
                return
            widths = [min(34, max(len(c), *(len(r[i]) for r in self.rows[:80]), 1)) for i, c in enumerate(self.cols)]
            app.put(2, 2, "  ".join(fit(c, wd) for c, wd in zip(self.cols, widths)), A["head"])
            room = h - 5
            self.off = max(0, min(self.off, max(0, len(shown) - room)))
            for i, r in enumerate(shown[self.off:self.off + room]):
                app.put(3 + i, 2, "  ".join(fit(v, wd) for v, wd in zip(r, widths)))

    def key(self, app, k):
        if self.mode == "tables":
            if k in (ord("q"), 27):
                return "back"
            if k in (curses.KEY_UP, ord("k")) and self.tables:
                self.sel = (self.sel - 1) % len(self.tables)
            elif k in (curses.KEY_DOWN, ord("j")) and self.tables:
                self.sel = (self.sel + 1) % len(self.tables)
            elif k in (10, 13, curses.KEY_ENTER) and self.tables:
                self.table, self.mode, self.off, self.q = self.tables[self.sel][0], "rows", 0, ""
                self.load_rows()
            return None
        if k in (ord("q"), 27):
            self.mode, self.last = "tables", 0
        elif k == curses.KEY_UP:
            self.off -= 1
        elif k == curses.KEY_DOWN:
            self.off += 1
        elif k == curses.KEY_PPAGE:
            self.off -= 15
        elif k == curses.KEY_NPAGE:
            self.off += 15
        elif k == curses.KEY_HOME:
            self.off = 0
        elif k == curses.KEY_END:
            self.off = 10 ** 6
        elif k == ord("r"):
            self.load_rows()
        elif k == ord("/"):
            t = app.prompt("Buscar (vacío = quitar filtro)")
            if t is not None:
                self.q, self.off = t.strip(), 0
        return None


# ----------------------------------------------------------------------------- aplicación (servicio)
class AppScreen(Screen, Bg):
    def __init__(self):
        self.info, self.fw, self.last, self.name = {}, "", 0, ("?", None)

    def refresh(self):
        rc, out = sh(["systemctl", "show", SERVICE, "-p", "LoadState,ActiveState,SubState,UnitFileState,ActiveEnterTimestamp,MainPID"])
        self.info = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        rc, out = sh(["firewall-cmd", f"--query-port={PORT}/tcp"])
        zone = sh(["firewall-cmd", "--get-default-zone"])[1].splitlines()[0] if rc in (0, 1) else "?"
        self.fw = (f"ABIERTO en la zona {zone}" if out.startswith("yes") else
                   f"CERRADO en la zona {zone}  (sudo firewall-cmd --permanent --add-port={PORT}/tcp && sudo firewall-cmd --reload)"
                   if out.startswith("no") else f"no se pudo consultar ({out[:50]})")
        rc, out = sh(["getent", "ahostsv4", f"{ESP32_NAME}.local"], timeout=8)
        self.name = (socket.gethostname(), out.split()[0] if rc == 0 and out else None)
        self.last = time.time()

    def tick(self, app):
        if time.time() - self.last > 4:
            self.bg(self.refresh)

    def draw(self, app, h, w):
        app.frame("Aplicación (servidor web)", "i iniciar   d detener   r reiniciar   a arranque automático   q volver", h, w)
        s, i = app.p.s, self.info
        ip = s["ip"] or "<IP>"
        active = i.get("ActiveState", "?")
        at = A["ok"] if active == "active" else A["bad"] if active in ("failed", "inactive") else A["warn"]
        if i.get("LoadState") == "not-found":
            app.segs(2, 2, [("Servicio ", 0), (SERVICE, A["bold"]), (": ", 0), ("NO INSTALADO", A["warn"]),
                            ("   (corre  sudo bash server/instalar.sh)", A["dim"])])
        else:
            app.segs(2, 2, [("Servicio ", 0), (SERVICE, A["bold"]), (": ", 0), (f"{active}/{i.get('SubState', '?')}", at),
                            (f"   arranque automático: {i.get('UnitFileState') or '?'}", 0)])
        if i.get("ActiveEnterTimestamp"):
            app.put(3, 2, f"Activo desde: {i['ActiveEnterTimestamp']}   PID {i.get('MainPID', '?')}", A["dim"])
        api_ok = s["api"] is not None
        app.segs(4, 2, [("Servidor web : ", 0), ("responde" if api_ok else f"SIN RESPUESTA ({s['api_err']})", A["ok"] if api_ok else A["bad"])])
        app.put(5, 2, f"Firewall     : puerto {PORT}/tcp {self.fw}")
        app.put(7, 2, "Direcciones (desde cualquier equipo de la misma red):", A["head"])
        app.put(8, 4, f"Conductor : http://{ip}:{PORT}/")
        app.put(9, 4, f"Panel     : http://{ip}:{PORT}/admin.html")
        app.segs(10, 4, [("ESP32     : ", 0), (f"{ESP32_NAME}.local", A["warn"]), (f"  (lo busca por nombre; no depende de la IP)", A["dim"])])
        host, found = self.name
        if host == "?":
            app.put(11, 17, "comprobando el nombre en la red…", A["dim"])
        elif found:
            app.segs(11, 17, [("[OK] ", A["ok"]), (f"{ESP32_NAME}.local -> {found}", 0)])
        elif host != ESP32_NAME:
            app.put(11, 17, f"[!!] esta laptop se llama '{host}': el ESP32 no la encuentra.", A["warn"])
            app.put(12, 4, f"sudo hostnamectl set-hostname {ESP32_NAME}; sudo systemctl restart avahi-daemon", A["dim"])
        else:
            app.put(11, 17, f"[!!] {ESP32_NAME}.local no resuelve: sudo systemctl restart avahi-daemon", A["warn"])
        app.put(13, 2, f"Base de datos: {DB_PATH}", A["dim"])
        if api_ok:
            st = s["api"]
            reserved = sorted({d["target"] for d in st["drivers"] if d.get("target")})
            app.put(15, 2, "Estado en vivo:", A["head"])
            app.put(16, 4, f"Conductores en camino: {len(st['drivers'])}    Cajones reservados: {', '.join(reserved) or '-'}")
            app.put(17, 4, f"Límite de velocidad: {st['speedLimit']} km/h    Excesos registrados: {st['speedingCount']}")
            app.put(18, 4, f"Cajones forzados desde el panel: {len(st['overrides'])}    Simulación: {'sí' if st['sim']['on'] else 'no'}")

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        status = ["systemctl", "--no-pager", "-n", "6", "status", SERVICE]
        if k == ord("i"):
            app.external(["sudo", "systemctl", "start", SERVICE], status)
        elif k == ord("r"):
            app.external(["sudo", "systemctl", "restart", SERVICE], status)
        elif k == ord("d") and app.confirm([f"Se detendrá {SERVICE}: la app y los sensores dejarán de funcionar.", "¿Detener el servidor?"]):
            app.external(["sudo", "systemctl", "stop", SERVICE], status)
        elif k == ord("a"):
            on = self.info.get("UnitFileState") == "enabled"
            if not on or app.confirm(["El servidor ya no arrancará solo al encender la laptop.", "¿Quitar el arranque automático?"]):
                app.external(["sudo", "systemctl", "disable" if on else "enable", SERVICE], pause="error")
        else:
            return None
        self.last = 0
        return None


# ----------------------------------------------------------------------------- red / WiFi
class WifiScreen(Screen):
    LIST = ["nmcli", "-t", "-f", "IN-USE,SSID,FREQ,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan"]

    def __init__(self, app):
        self.sel, self.nets, self.err = 0, [], ""
        self.scanning, self._pending = False, None
        self.show_cached()                            # al instante: la red actual y las guardadas
        self.scan()                                   # el escaneo completo tarda ~10 s: va en segundo plano

    def show_cached(self):
        self.apply(sh(self.LIST + ["no"]))

    def scan(self):
        if self.scanning:
            return
        self.scanning = True

        def work():
            self._pending = sh(self.LIST + ["yes"], timeout=40)
        threading.Thread(target=work, daemon=True).start()

    def tick(self, app):
        if self._pending is not None:
            res, self._pending, self.scanning = self._pending, None, False
            self.apply(res)
            app.say(f"{sum(n['visible'] for n in self.nets)} redes encontradas.")

    def profiles(self):
        """SSID -> [(nombre, uuid)]  (puede haber varios perfiles con el mismo nombre, p. ej. 'Anahuac')."""
        res = {}
        for l in sh(["nmcli", "-t", "-f", "NAME,UUID,TYPE", "connection", "show"])[1].splitlines():
            f = split_t(l)
            if len(f) >= 3 and f[2] == "802-11-wireless":
                ssid = sh(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "uuid", f[1]])[1].strip() or f[0]
                res.setdefault(ssid, []).append((f[0], f[1]))
        return res

    def apply(self, res):
        keep = self.nets[self.sel]["ssid"] if self.nets else None
        prof = self.profiles()
        rc, out = res
        self.err = out if rc else ""
        best = {}
        for l in out.splitlines() if not rc else []:
            f = split_t(l)
            if len(f) < 5 or not f[1]:
                continue
            n = dict(ssid=f[1], inuse=f[0] == "*", band="2.4G" if int(re.sub(r"\D", "", f[2]) or 0) < 3000 else "5G",
                     signal=int(f[3] or 0), sec="" if f[4] in ("", "--") else f[4], visible=True)
            old = best.get(n["ssid"])
            if old:
                n["inuse"] = n["inuse"] or old["inuse"]
                if old["signal"] > n["signal"]:
                    n["signal"], n["band"] = old["signal"], old["band"]
            best[n["ssid"]] = n
        for ssid in prof:
            best.setdefault(ssid, dict(ssid=ssid, inuse=False, band="", signal=0, sec="?", visible=False))
        for n in best.values():
            n["profiles"] = prof.get(n["ssid"], [])
        self.nets = sorted(best.values(), key=lambda n: (not n["inuse"], not n["visible"], not n["profiles"], -n["signal"]))
        self.sel = next((i for i, n in enumerate(self.nets) if n["ssid"] == keep), 0)

    def draw(self, app, h, w):
        app.frame("Red / WiFi", "↑↓ mover  Enter conectar  r buscar  d olvidar  x desconectar  p ping  q volver" if UTF8
                  else "Up/Down mover  Enter conectar  r buscar  d olvidar  x desconectar  p ping  q volver", h, w)
        s = app.p.s
        gw = s["gw"]
        app.segs(2, 2, [("Conectado a: ", 0), (s["wifi"] or "nada", A["ok"] if s["wifi"] else A["warn"]),
                        (f"   IP: {s['ip'] or '-'}   Puerta de enlace: {gw or '-'}   Interfaz: {s['ifname'] or '-'}", 0)])
        app.segs(3, 2, [("El ESP32 busca al servidor como  ", 0), (f"{ESP32_NAME}.local", A["warn"]), (f"   (IP ahora: {s['ip'] or '-'})", A["dim"])])
        app.put(5, 2, f"  {'Red (SSID)':<28}{'Banda':<7}{'Señal':<9}{'Seguridad':<13}Perfil", A["head"])
        if self.scanning:
            app.put(4, 2, "Buscando redes… (~10 s)", A["warn"])
        if self.err:
            app.put(6, 2, f"nmcli: {self.err[:w - 12]}", A["bad"])
        room = h - 9
        top = max(0, min(self.sel - room + 1, len(self.nets) - room)) if self.sel >= room else 0
        for i, n in enumerate(self.nets[top:top + room]):
            idx, sel = top + i, top + i == self.sel
            bars = f"[{'#' * round(n['signal'] / 20):<5}]" if n["visible"] else "  --   "
            sec = ("abierta" if not n["sec"] else n["sec"].replace("WPA1 WPA2", "WPA1/2")) if n["visible"] else "-"
            prof = ("guardada" + (f" (x{len(n['profiles'])})" if len(n["profiles"]) > 1 else "")) if n["profiles"] else ""
            prof += "" if n["visible"] else (" · " if prof else "") + "fuera de alcance"
            line = f"{'*' if n['inuse'] else ' '} {fit(n['ssid'], 28)}{fit(n['band'], 7)}{fit(bars, 9)}{fit(sec, 13)}{prof}"
            at = A["sel"] if sel else A["ok"] if n["inuse"] else A["dim"] if not n["visible"] else 0
            app.put(6 + i, 1, (">" if sel else " ") + line.ljust(w - 5) if sel else " " + line, at)
        app.put(h - 3, 2, "Las claves nuevas las pide nmcli directamente (no se guardan en este programa).", A["dim"])

    def key(self, app, k):
        n = self.nets[self.sel] if self.nets else None
        if k in (ord("q"), 27):
            return "back"
        if k in (curses.KEY_UP, ord("k")) and self.nets:
            self.sel = (self.sel - 1) % len(self.nets)
        elif k in (curses.KEY_DOWN, ord("j")) and self.nets:
            self.sel = (self.sel + 1) % len(self.nets)
        elif k == ord("r"):
            self.scan()
        elif k == ord("p"):
            gw = app.p.s["gw"]
            if gw:
                app.external(["ping", "-c", "4", "-W", "2", gw])
            else:
                app.say("No hay puerta de enlace: no estás conectado a ninguna red.", "warn")
        elif k == ord("x") and app.p.s["ifname"] and app.confirm(["Te desconectarás del WiFi y perderás la red hasta elegir otra.", "¿Desconectar?"]):
            app.external(["nmcli", "device", "disconnect", app.p.s["ifname"]], pause="error")
            self.show_cached()
        elif k == ord("d") and n and n["profiles"]:
            if app.confirm([f"Se olvidará la red '{n['ssid']}' ({len(n['profiles'])} perfil/es guardado/s).",
                            "Tendrás que volver a teclear su clave para reconectar." + (" Estás conectado a ella ahora." if n["inuse"] else ""),
                            "¿Olvidarla?"]):
                app.external(*[["nmcli", "connection", "delete", "uuid", u] for _, u in n["profiles"]], pause="error")
                self.show_cached()
                self.scan()
        elif k in (10, 13, curses.KEY_ENTER) and n:
            self.connect(app, n)
        return None

    def connect(self, app, n):
        if n["inuse"]:
            app.say("Ya estás conectado a esta red.", "warn")
            return
        if not n["visible"]:
            app.say("Esa red no está al alcance ahora (r para buscar de nuevo).", "warn")
            return
        ifc = app.p.s["ifname"]
        extra = ["ifname", ifc] if ifc else []
        if n["profiles"]:                             # ya guardada: no pide clave
            cmd = ["nmcli", "connection", "up", "uuid", n["profiles"][0][1]] + extra
        elif not n["sec"]:
            cmd = ["nmcli", "device", "wifi", "connect", n["ssid"]] + extra
        else:                                         # --ask: nmcli pide la clave en la terminal, aquí nunca pasa por el código
            cmd = ["nmcli", "--ask", "device", "wifi", "connect", n["ssid"]] + extra
        old_ip = lan_ip()
        rc = app.external(cmd, pause="error")
        if rc and app.confirm(["No se pudo conectar. Si el error habla de permisos,", "se puede reintentar con sudo.", "¿Reintentar con sudo?"]):
            rc = app.external(["sudo"] + cmd, pause="error")
        app.p.kick()
        time.sleep(1.0)
        self.show_cached()
        self.scan()
        if not rc:
            new_ip = lan_ip()
            lines = [f"Conectado a '{n['ssid']}'.", f"IP del servidor: {new_ip or '(sin IP todavía)'}"]
            if new_ip != old_ip:
                lines += ["", f"La IP cambió (antes {old_ip or '-'}).", f"El ESP32 la encuentra solo: busca {ESP32_NAME}.local."]
            app.notice(lines)


# ----------------------------------------------------------------------------- logs
class LogsScreen(Screen, Bg):
    def __init__(self):
        self.tab, self.lines, self.q, self.off, self.follow, self.last, self.err = "journal", [], "", 0, True, 0, None

    def load(self):
        tab = self.tab
        if tab == "journal":
            rc, out = sh(["journalctl", "-u", SERVICE, "-n", "300", "--no-pager", "-o", "short"], timeout=5)
            err = out if rc else None
            lines = [(re.sub(r"^(\w{3} +\d+ [\d:]+) \S+ ", r"\1 ", l), "") for l in out.splitlines()] if not rc else []
        else:
            try:
                rows = db_all("SELECT t, type, msg FROM events ORDER BY id DESC LIMIT 300")[::-1]
                lines = [(f"{time.strftime('%H:%M:%S', time.localtime(t))} [{ty}] {m}", ty) for t, ty, m in rows]
                err = None
            except Exception as e:
                lines, err = [], str(e)
        if tab == self.tab:                           # si cambió de pestaña mientras cargaba, se descarta
            self.lines, self.err = lines, err
        self.last = time.time()

    def tick(self, app):
        if time.time() - self.last > 2:
            self.bg(self.load)

    def draw(self, app, h, w):
        app.frame("Logs", "Tab cambiar   ↑↓ PgUp/PgDn mover   End seguir   / filtrar   q volver" if UTF8 else "Tab cambiar   Up/Down PgUp/PgDn mover   End seguir   / filtrar   q volver", h, w)
        app.segs(1, 2, [(" journal del servicio ", A["sel"] if self.tab == "journal" else A["dim"]), ("  ", 0),
                        (" eventos de la base ", A["sel"] if self.tab == "events" else A["dim"]),
                        (f"   filtro: '{self.q}'" if self.q else "", A["warn"]),
                        ("   [siguiendo]" if self.follow else "", A["ok"])])
        if self.err:
            app.put(3, 2, self.err[:w - 4], A["bad"])
            if self.tab == "journal":
                app.put(4, 2, "Si dice que no hay permisos, tu usuario debe estar en el grupo wheel o systemd-journal.", A["dim"])
            return
        q = self.q.lower()
        lines = [l for l in self.lines if q in l[0].lower()] if q else self.lines
        room = h - 5
        self.off = max(0, len(lines) - room) if self.follow else max(0, min(self.off, len(lines) - room))
        if not lines:
            app.put(3, 2, "(sin registros)", A["dim"])
        for i, (text, kind) in enumerate(lines[self.off:self.off + room]):
            at = A["em"] if kind == "emergency" else A["warn"] if kind in ("security", "config") else A["ok"] if kind == "sensor" else 0
            app.put(3 + i, 1, text[:w - 3], at)

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k == 9:
            self.tab, self.follow, self.lines, self.err, self.last = ("events" if self.tab == "journal" else "journal"), True, [], None, 0
        elif k == curses.KEY_UP:
            self.follow, self.off = False, self.off - 1
        elif k == curses.KEY_DOWN:
            self.off += 1
        elif k == curses.KEY_PPAGE:
            self.follow, self.off = False, self.off - 15
        elif k == curses.KEY_NPAGE:
            self.off += 15
        elif k in (curses.KEY_END, ord("G")):
            self.follow = True
        elif k == ord("/"):
            t = app.prompt("Filtrar (vacío = quitar filtro)")
            if t is not None:
                self.q, self.follow = t.strip(), True
        return None


# ----------------------------------------------------------------------------- prueba de LED RGB
LED_IDS = ["A1", "A2", "A3", "A4"]
LED_KEYS = [("r", "red", "Rojo"), ("g", "green", "Verde"), ("b", "blue", "Azul"), ("y", "yellow", "Amarillo"),
            ("m", "purple", "Morado"), ("c", "cyan", "Cian"), ("w", "white", "Blanco"), ("o", "off", "Apagado"),
            ("a", "auto", "Auto")]
LED_NAME = {c: n for _, c, n in LED_KEYS}
LED_CYCLE = ["red", "green", "blue", "yellow", "purple", "cyan", "white"]       # colores del modo demo


class LedTestScreen(Screen):
    """Fija el color de cada LED RGB del prototipo (POST /api/ledtest). El ESP32 lo recibe en su siguiente reporte (~1.5 s)."""

    def __init__(self):
        self.sel, self.demo, self.step, self.t_demo, self.t_keep, self.test = 0, False, 0, 0.0, time.time(), {}

    def send(self, app, target, color):
        try:
            r = api_admin(app, "/api/ledtest", {"id": target, "color": color})
            if not r.get("ok"):
                app.say(r.get("error") or "No se pudo cambiar el LED.", "bad")
            else:
                self.test = r.get("ledTest", {})
            return bool(r.get("ok"))
        except Exception as e:
            app.say(f"Servidor sin respuesta: {type(e).__name__}", "bad")
            return False

    def tick(self, app):
        now = time.time()
        api_st = app.p.s["api"]
        if api_st is not None:
            self.test = api_st.get("ledTest", {})
        if self.demo and now - self.t_demo >= 2.0:                  # cada color dura 2 s (el ESP32 reporta cada 1.5 s)
            self.t_demo, self.step = now, self.step + 1
            for i, sid in enumerate(LED_IDS):
                if not self.send(app, sid, LED_CYCLE[(self.step + i) % len(LED_CYCLE)]):
                    self.demo = False
                    break
        elif not self.demo and self.test and now - self.t_keep >= 60:   # la prueba vence a los 3 min: mientras esta pantalla esté abierta se renueva
            self.t_keep = now
            for sid, c in list(self.test.items()):
                self.send(app, sid, c)

    def draw(self, app, h, w):
        app.frame("Prueba de LED RGB", "↑↓ elegir LED  r g b y m c w o a color  d demo  x todo a Auto  q volver" if UTF8
                  else "Up/Down LED  r g b y m c w o a color  d demo  x todo a Auto  q volver", h, w)
        s = app.p.s
        if s["api"] is None:
            app.put(2, 2, "El servidor no responde: no se puede probar ahora.", A["bad"])
            return
        app.put(2, 2, "El color tarda ~1.5 s en llegar al ESP32. La prueba se apaga sola a los 3 min y manda sobre libre / ocupado / emergencia.", A["dim"])
        app.put(4, 2, f"  {'LED':<10}{'Cajón':<8}{'Color en prueba':<20}Sensor", A["head"])
        rows = [("all", "Todos", "")] + [(sid, sid, f"Cajón {sid[1:]}") for sid in LED_IDS]
        v = {r["id"]: r for r in sensor_view(s["sensors"])}
        for i, (sid, name, cajon) in enumerate(rows):
            sel = i == self.sel
            c = self.test.get(sid) if sid != "all" else None
            if sid == "all":
                cols = set(self.test.get(x) for x in LED_IDS)
                c = cols.pop() if len(cols) == 1 else None
            label = LED_NAME.get(c, "Auto (libre/ocupado)") if c or sid != "all" else "—"
            y = 5 + i
            app.put(y, 2, (">" if sel else " ") + f" {name:<10}{cajon:<8}".ljust(w - 5), A["sel"] if sel else 0)
            app.put(y, 22, "      " if c in (None, "off") else "      ", A["led_" + c] if c in LED_CYCLE else (A["dim"] if c == "off" else 0))
            app.put(y, 29, label if sid != "all" or c else "(distintos o Auto)", A["sel"] if sel else 0)
            if sid in v:
                st = v[sid]
                app.put(y, 52, f"{st['state']}" + ("" if st["cm"] is None else f" · {st['cm']} cm"), A["sel"] if sel else A[STATE_ATTR[st["state"]]])
        y = 5 + len(rows) + 1
        app.put(y, 2, "Colores:", A["head"])
        x = 2
        for k, c, n in LED_KEYS:
            if x + 14 > w - 2:                           # no cabe: siguiente renglón
                x, y = 2, y + 1
            app.put(y + 1, x, f" {k} ", A["bold"])
            if c in LED_CYCLE:
                app.put(y + 1, x + 3, "  ", A["led_" + c])
            app.put(y + 1, x + 6 if c in LED_CYCLE else x + 3, n, 0)
            x += 14
        if self.demo:
            app.put(y + 3, 2, "DEMO en marcha: los 4 LED recorren los colores en cadena. Cualquier otra tecla la detiene.", A["warn"])
        else:
            app.put(y + 3, 2, "d  demo: los 4 LED recorren todos los colores en cadena (para lucirlo).", A["dim"])
        app.put(y + 4, 2, "Si un color no se enciende, ese canal del LED (o su resistencia o su cable) está dañado.", A["dim"])

    def key(self, app, k):
        n = 5
        if k in (ord("q"), 27):
            if self.demo:
                self.demo = False
                self.send(app, "all", "auto")
            elif self.test:
                app.say("Los LED seguirán en prueba hasta 3 min. Para quitarla ya: x en esta pantalla.", "warn")
            return "back"
        if k == ord("d"):
            if self.demo:
                self.demo = False
                self.send(app, "all", "auto")
            else:
                self.demo, self.t_demo, self.step = True, 0.0, 0
            return None
        if self.demo:                                    # cualquier otra tecla detiene el demo
            self.demo = False
            self.send(app, "all", "auto")
            return None
        if k in (curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")):
            self.sel = (self.sel + (-1 if k in (curses.KEY_UP, ord("k")) else 1)) % n
        elif k == ord("x"):
            self.send(app, "all", "auto")
        elif 0 < k < 256:
            for key, color, _ in LED_KEYS:
                if chr(k).lower() == key:
                    self.send(app, "all" if self.sel == 0 else LED_IDS[self.sel - 1], color)
                    self.t_keep = time.time()
                    break
        return None


# ----------------------------------------------------------------------------- emergencia
class EmergencyScreen(Screen):
    def draw(self, app, h, w):
        app.frame("Emergencia / simulacro", "Enter activar o desactivar   q volver", h, w)
        s = app.p.s
        em = s["em"]
        on = bool(em and em.get("on"))
        app.segs(2, 2, [("Estado: ", 0), ("EMERGENCIA ACTIVA" if on else "normal", A["em"] if on else A["ok"])])
        if on and em.get("since"):
            app.put(3, 2, f"Activa desde las {time.strftime('%H:%M:%S', time.localtime(em['since']))}: los LED de los sensores parpadean en morado.", A["em"])
        else:
            app.put(3, 2, "Los LED indican libre (verde) / ocupado (rojo).", A["dim"])
        app.put(5, 2, "Al activarla, los LED parpadean en morado y las apps muestran un aviso con voz.")
        app.put(6, 2, "El ESP32 se entera en ~1.5 s. Pide la contraseña en cada cambio y no la guarda.", A["dim"])
        if s["api"] is None:
            app.put(8, 2, "El servidor no responde: no se puede cambiar la emergencia ahora.", A["bad"])
        else:
            app.put(8, 2, f"> Enter: {'DESACTIVAR' if on else 'ACTIVAR'} la emergencia", A["warn"])

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k in (10, 13, curses.KEY_ENTER):
            if app.p.s["api"] is None:
                app.say("El servidor no responde.", "bad")
                return None
            on = bool(app.p.s["em"] and app.p.s["em"].get("on"))
            pw = app.prompt("Contraseña de emergencia", secret=True)
            if pw is None:
                return None
            try:
                r = api_admin(app, "/api/emergency", {"on": not on, "password": pw}, timeout=3)
                app.say(("Emergencia ACTIVADA." if not on else "Emergencia desactivada.") if r.get("ok") else (r.get("error") or "No se pudo cambiar."),
                        ("em" if not on else "ok") if r.get("ok") else "bad")
            except Exception as e:
                app.say(f"Error al hablar con el servidor: {e}", "bad")
            app.p.kick()
        return None


# ----------------------------------------------------------------------------- apagado de emergencia informática
class KillScreen(Screen):
    def draw(self, app, h, w):
        app.frame("Apagado de emergencia informática", "Enter apagar el servidor   q volver", h, w)
        app.put(2, 2, "Detiene ParkUni por completo (app, panel, sensores y túnel) ante un incidente informático.")
        app.put(3, 2, "Solo se ordena desde esta laptop, con su contraseña propia (no la del panel ni la de emergencia).", A["dim"])
        app.put(4, 2, "No vuelve a arrancar solo, ni siquiera al reiniciar la laptop.", A["dim"])
        app.put(6, 2, "Para volver a encenderlo:", A["warn"])
        app.put(7, 4, "sudo rm /opt/parkuni/server/data/APAGADO_DE_EMERGENCIA && sudo systemctl start parkuni")
        if app.p.s["api"] is None:
            app.put(9, 2, "El servidor no responde: ya está apagado o detenido.", A["bad"])
        else:
            app.put(9, 2, "> Enter: APAGAR EL SERVIDOR", A["warn"])

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k in (10, 13, curses.KEY_ENTER):
            if app.p.s["api"] is None:
                app.say("El servidor no responde.", "bad")
                return None
            if not app.confirm(["¡EMERGENCIA! Se apagará el servidor ParkUni.", "La app y los sensores quedarán sin servicio."]):
                app.say("Cancelado.", "warn")
                return None
            pw = app.prompt("Contraseña de apagado de emergencia", secret=True)
            if pw is None:
                return None
            try:
                r = api("/api/kill", {"password": pw}, timeout=3)
                app.say("Servidor APAGADO. Para encenderlo, mira las instrucciones de esta pantalla." if r.get("ok")
                        else (r.get("error") or "No se pudo apagar."), "em" if r.get("ok") else "bad")
            except Exception as e:
                app.say(f"Error al hablar con el servidor: {e}", "bad")
            app.p.kick()
        return None


# ----------------------------------------------------------------------------- entorno gráfico
class DesktopScreen(Screen, Bg):
    def __init__(self):
        self.checks, self.last = [], 0

    def run_checks(self, app):
        c = []
        en = sh(["systemctl", "is-enabled", SERVICE])[1].splitlines()[:1]
        c.append((en == ["enabled"], f"{SERVICE} arranca solo al encender", f"sudo systemctl enable {SERVICE}   (o tecla 'a' en Aplicación)"))
        act = [l for l in sh(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show", "--active"])[1].splitlines() if l.endswith(":802-11-wireless")]
        if act:
            uuid = split_t(act[0])[0]
            _, o = sh(["nmcli", "-t", "-f", "connection.autoconnect,connection.permissions,802-11-wireless-security.psk-flags", "connection", "show", "uuid", uuid])
            f = dict(l.split(":", 1) for l in o.splitlines() if ":" in l)
            ok = f.get("connection.autoconnect") == "yes" and not f.get("connection.permissions") and f.get("802-11-wireless-security.psk-flags", "0") in ("", "0")
            c.append((ok, "WiFi actual se reconecta sin escritorio (clave del sistema)", "vuelve a conectarte desde 'Red / WiFi' o revisa el perfil con nmcli"))
        else:
            c.append((False, "WiFi: hoy no hay una red activa", "conéctate desde 'Red / WiFi'"))
        try:
            hook = "parkuni-tui" in rd(PROFILE)
        except OSError:
            hook = False
        c.append((hook, "El menú se abre al iniciar sesión (~/.bash_profile)", "falta el bloque parkuni-tui en ~/.bash_profile"))
        try:
            lid = "HandleLidSwitch=ignore" in rd(LOGIND_DROPIN)
        except OSError:
            lid = False
        c.append((lid, "Cerrar la tapa no suspende la laptop", f"falta {LOGIND_DROPIN} (lo crea instalar.sh)"))
        c.append((sh(["getenforce"])[1] == "Enforcing", "SELinux sigue activo (Enforcing)", "no lo desactives: diagnostica con ausearch"))
        self.checks, self.last = c, time.time()

    def tick(self, app):
        if not self.last:
            self.bg(lambda: self.run_checks(app))

    def draw(self, app, h, w):
        app.frame("Entorno gráfico", "1 activar   2 desactivar (modo servidor)   r revisar   q volver", h, w)
        s = app.p.s
        app.segs(2, 2, [("Arranque por defecto: ", 0), (s["target"], A["bold"]),
                        ("   (multi-user = modo servidor)", A["dim"])])
        app.segs(3, 2, [("Escritorio ahora    : ", 0), ("ENCENDIDO" if s["gui"] else "apagado", A["warn"] if s["gui"] else A["ok"])])
        app.put(5, 2, "1  ACTIVAR el entorno gráfico (y abrirlo ahora, si quieres)", A["warn"])
        app.put(6, 2, "2  DESACTIVARLO en el próximo arranque (modo servidor, solo terminal)", A["warn"])
        app.put(8, 2, "Revisión antes de quitar el escritorio:", A["head"])
        for i, (ok, text, fix) in enumerate(self.checks):
            app.segs(9 + i, 4, [("[OK] " if ok else "[!!] ", A["ok"] if ok else A["warn"]), (text, 0 if ok else A["warn"])])
        bad = [fix for ok, _, fix in self.checks if not ok]
        if bad:
            app.put(10 + len(self.checks), 4, "Pendiente: " + bad[0], A["dim"])
        app.put(12 + len(self.checks), 2, "Si algo sale mal: Ctrl+Alt+F3 abre otra terminal, o aquí la opción 1.", A["dim"])

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k == ord("r"):
            self.last = 0
        elif k == ord("1"):
            if app.sudo("systemctl", "set-default", "graphical.target") == 0:
                if app.confirm(["Listo: el escritorio arrancará en cada inicio.", "¿Abrirlo ahora mismo? La pantalla cambiará al login gráfico."]):
                    app.sudo("systemctl", "isolate", "graphical.target", pause="error")
        elif k == ord("2"):
            self.run_checks(app)
            pend = [t for ok, t, _ in self.checks if not ok]
            msg = ["Se quitará el escritorio en el PRÓXIMO arranque (ahora no se cierra nada)."]
            if pend:
                msg += ["", "Hay pendientes sin resolver:"] + [f"  - {t}" for t in pend] + ["", "¿Quitarlo de todos modos?"]
            else:
                msg += ["Todas las revisiones están en orden.", "¿Quitarlo?"]
            if app.confirm(msg) and app.sudo("systemctl", "set-default", "multi-user.target") == 0:
                app.say("Listo. Surtirá efecto al reiniciar (menú Apagar / reiniciar). Para volver: opción 1.", "warn")
        return None


# ----------------------------------------------------------------------------- apagar / reiniciar
class PowerScreen(Screen):
    def __init__(self):
        self.sel = 0
        self.items = [("Reiniciar la laptop", "reboot"), ("Apagar la laptop", "poweroff")]

    def draw(self, app, h, w):
        app.frame("Apagar / reiniciar", "↑↓ mover   Enter elegir   q volver" if UTF8 else "Up/Down mover   Enter elegir   q volver", h, w)
        for i, (t, _) in enumerate(self.items):
            app.put(3 + i, 4, f"{'>' if i == self.sel else ' '} {t}".ljust(40), A["sel"] if i == self.sel else 0)
        app.put(7, 4, "Pide confirmación doble. El servidor ParkUni se detiene y los sensores dejan de reportar.", A["dim"])

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k in (curses.KEY_UP, curses.KEY_DOWN, ord("k"), ord("j")):
            self.sel = 1 - self.sel
        elif k in (10, 13, curses.KEY_ENTER):
            text, verb = self.items[self.sel]
            if app.confirm([f"¿{text}?", "El servidor y los sensores quedarán sin servicio."]):
                if (app.prompt("Escribe SI para confirmar") or "").strip().upper() == "SI":
                    app.sudo("systemctl", verb, pause="error")
                else:
                    app.say("Cancelado.", "warn")
        return None


# ----------------------------------------------------------------------------- respaldo
def backups():
    """Copias hechas con respaldo.py: [(ruta, bytes, fecha)], la más nueva primero."""
    out = []
    for f in glob.glob(os.path.join(BACKUP_DIR, "parkuni-*.db")):
        try:
            st = os.stat(f)
            out.append((f, st.st_size, st.st_mtime))
        except OSError:
            pass
    return sorted(out, key=lambda r: r[2], reverse=True)


class BackupScreen(Screen):
    """Copia la base con server/respaldo.py (solo a mano: la laptop no aguanta un respaldo automático)."""

    def draw(self, app, h, w):
        app.frame("Respaldo de la base", "Enter / r hacer un respaldo   q volver" if UTF8 else "Enter / r respaldar   q volver", h, w)
        app.put(2, 2, f"Base    : {DB_PATH}")
        app.put(3, 2, f"Copias  : {BACKUP_DIR}")
        app.put(4, 2, "Cada copia la hace  respaldo.py  con la API de respaldo de SQLite (segura con el servidor encendido) y se conservan las últimas 14.", A["dim"])
        app.put(6, 2, "> Enter: hacer un respaldo ahora", A["warn"])
        c = backups()
        app.put(8, 2, f"Copias guardadas: {len(c)}" if c else "Todavía no hay copias.", A["head"] if c else A["warn"])
        for i, (f, size, t) in enumerate(c[:max(0, h - 12)]):
            app.put(9 + i, 4, f"{os.path.basename(f)}   {size / 1024:.0f} KB   {fulltime(t)}   (hace {ago(time.time() - t)})")

    def key(self, app, k):
        if k in (ord("q"), 27):
            return "back"
        if k in (10, 13, curses.KEY_ENTER, ord("r")):
            if not os.path.exists(DB_PATH):
                app.say(f"No existe la base: {DB_PATH}", "bad")
            elif not os.path.exists(BACKUP_PY):
                app.say(f"Falta el script {os.path.abspath(BACKUP_PY)} (corre  sudo bash server/instalar.sh).", "bad")
            elif app.confirm(["Se copiará la base de datos a:", BACKUP_DIR, "¿Hacer el respaldo ahora?"]):
                rc = app.external(["python3", os.path.abspath(BACKUP_PY), "--db", DB_PATH, "--dir", BACKUP_DIR])
                app.say("Respaldo hecho." if rc == 0 else "El respaldo falló (ver mensaje arriba).", "ok" if rc == 0 else "bad")
        return None


# ----------------------------------------------------------------------------- internet
def tunnel_state():
    """(estado del servicio del túnel, link público o None). El link se pide a las métricas locales de cloudflared
    (/quicktunnel); si no responden, se busca en el registro del servicio."""
    st = sh(["systemctl", "is-active", TUNNEL])[1].strip() or "?"
    url = None
    if st == "active":
        try:
            with _OPENER.open(f"http://{TUNNEL_METRICS}/quicktunnel", timeout=1.5) as r:
                host = str(json.load(r).get("hostname") or "")
            if host.endswith(".trycloudflare.com"):
                url = f"https://{host}/"
        except Exception:
            pass
        if not url:
            found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", sh(["journalctl", "-u", TUNNEL, "-n", "300", "--no-pager", "-o", "cat"], timeout=5)[1])
            url = found[-1] + "/" if found else None
    return st, url


def qr_matrix(text):
    """Matriz del QR [[bool]] (True = módulo oscuro) con la herramienta qrencode, o None si no está instalada."""
    if not shutil.which("qrencode"):
        return None
    rc, out = sh(["qrencode", "-t", "ASCII", "-m", "2", "-l", "M", "-o", "-", text])
    if rc or not out:
        return None
    rows = out.split("\n")
    return [[line[i] == "#" for i in range(0, len(line) - 1, 2)] for line in rows]


class InternetScreen(Screen, Bg):
    """Acceso desde internet: estado del túnel y el interruptor para cerrar el panel de golpe (lockdown)."""

    def __init__(self):
        self.st, self.url, self.t = "?", None, 0.0
        self.qr, self.qr_url, self.show_qr = None, None, False

    def draw_qr(self, app, h, w):
        """El QR se dibuja con medios bloques (2 filas de módulos por línea de pantalla): sale de ~18 líneas.
        Módulo claro = celda en vídeo inverso; la zona de silencio también, así se lee en terminal oscura."""
        app.frame("QR de la app del conductor", "r volver   q volver al menú" if UTF8 else "r volver   q menu", h, w)
        if not self.url:
            app.put(3, 2, "El túnel no está activo o aún no hay link. Enciéndelo con t.", A["warn"])
            return
        if self.qr_url != self.url:
            self.qr, self.qr_url = qr_matrix(self.url), self.url
        if not self.qr:
            app.put(3, 2, "Falta qrencode:  sudo dnf install qrencode   (el QR también sale en el panel web)", A["warn"])
            app.put(5, 2, self.url, A["bold"])
            return
        m = self.qr
        top = 2
        if len(m) // 2 + 6 > h or len(m[0]) + 2 > w:
            app.put(3, 2, f"La terminal es chica para el QR ({len(m[0])}x{len(m) // 2 + 1} necesarios). Agrándala o usa el panel web.", A["warn"])
            app.put(5, 2, self.url, A["bold"])
            return
        x0 = max(0, (w - len(m[0])) // 2)
        for r in range(0, len(m), 2):
            for c, dark_t in enumerate(m[r]):
                dark_b = m[r + 1][c] if r + 1 < len(m) else False
                ch, at = ((" ", 0) if dark_t and dark_b else ("▄", 0) if dark_t else ("▀", 0) if dark_b else (" ", curses.A_REVERSE))
                app.put(top + r // 2, x0 + c, ch, at)
        app.put(top + len(m) // 2 + 1, 2, self.url, A["bold"])

    def refresh(self):
        self.t = time.time()
        self.st, self.url = tunnel_state()

    def tick(self, app):
        if time.time() - self.t > 5:
            self.bg(self.refresh)

    def draw(self, app, h, w):
        if self.show_qr:
            return self.draw_qr(app, h, w)
        app.frame("Internet / cerrar el panel", "c cerrar/abrir panel   t túnel iniciar/detener   r QR del link   q volver" if UTF8
                  else "c cerrar/abrir panel   t tunel   r QR   q volver", h, w)
        api_st = app.p.s["api"]
        lock = bool((api_st or {}).get("lockdown"))
        app.segs(2, 2, [("Túnel de Cloudflare : ", 0), (self.st, A["warn"] if self.st == "active" else A["dim"]),
                        ("   (servicio " + TUNNEL + ")", A["dim"])])
        if self.st == "active":
            app.segs(3, 2, [("Link público         : ", 0), (self.url or "buscando en el registro…", A["bold"])])
        elif self.st in ("inactive", "failed"):
            app.put(3, 2, "Sin túnel: la app solo se ve en la red local.", A["dim"])
        else:
            app.put(3, 2, f"El servicio {TUNNEL} no está instalado (ver README: 'Subir a internet').", A["warn"])
        if api_st is None:
            app.put(5, 2, "El servidor no responde.", A["bad"])
        else:
            app.segs(5, 2, [("Panel de administración: ", 0), ("CERRADO a internet" if lock else "abierto a internet (con contraseña)", A["ok"] if lock else A["warn"])])
        app.put(7, 2, "Cerrar el panel = por internet ya no se puede entrar ni usar el login; la app del conductor sigue.", A["dim"])
        app.put(8, 2, "Desde la red local el panel sigue funcionando. Úsalo si ves intentos raros en los logs (eventos 'security').", A["dim"])
        app.put(9, 2, "Si hay que cortar todo: tecla t (detiene el túnel) o menú 3 (detiene el servidor).", A["dim"])

    def key(self, app, k):
        if k in (ord("q"), 27):
            if self.show_qr:
                self.show_qr = False
                return None
            return "back"
        api_st = app.p.s["api"]
        if k == ord("r"):
            self.show_qr = not self.show_qr
            if self.show_qr and not self.url:
                self.refresh()
        elif k == ord("c"):
            if api_st is None:
                app.say("El servidor no responde.", "bad")
                return None
            lock = bool(api_st.get("lockdown"))
            if app.confirm(["Se abrirá el panel a internet (con contraseña)." if lock else "Se cerrará el panel a internet: caen las sesiones abiertas desde internet.",
                            "¿Continuar?"]):
                try:
                    r = api_admin(app, "/api/lockdown", {"on": not lock})
                    app.say(("Panel cerrado a internet." if r.get("lockdown") else "Panel abierto a internet.") if r.get("ok") else (r.get("error") or "No se pudo cambiar."),
                            ("ok" if r.get("lockdown") else "warn") if r.get("ok") else "bad")
                except Exception as e:
                    app.say(f"Servidor sin respuesta: {type(e).__name__}", "bad")
                app.p.kick()
        elif k == ord("t"):
            if self.st in ("?", "unknown") or self.st not in ("active", "inactive", "failed"):
                app.say(f"Falta instalar el servicio {TUNNEL} (README: 'Subir a internet').", "warn")
            else:
                verb = "stop" if self.st == "active" else "start"
                if verb == "start" or app.confirm(["Se detendrá el túnel: la app dejará de verse desde internet.", "¿Detener?"]):
                    app.external(["sudo", "systemctl", verb, TUNNEL], pause="error")
                    time.sleep(3 if verb == "start" else 0)
                    self.refresh()
        return None


# ----------------------------------------------------------------------------- --check
def check():
    bad = 0

    def line(level, name, detail=""):
        nonlocal bad
        bad += level == "FALLO"
        print(f"[{level:^5}] {name}" + (f": {detail}" if detail else ""))

    line("OK" if sys.version_info >= (3, 9) else "FALLO", "Python", sys.version.split()[0])
    line("OK", "curses y sqlite3", f"sqlite {sqlite3.sqlite_version}")
    try:
        tabs = db_all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        counts = {t[0]: db_all(f'SELECT COUNT(*) FROM "{t[0]}"')[0][0] for t in tabs}
        line("OK", "Base de datos (solo lectura)", f"{DB_PATH} · " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    except Exception as e:
        line("FALLO", "Base de datos", f"{DB_PATH}: {e}")
    try:
        st = api("/api/status", timeout=3)
        line("OK", "API del servidor", f"{URL} · sensores vistos: {len(st['seen'])} · esp={st['esp']} · emergencia={st['emergency']['on']}")
    except Exception as e:
        line("FALLO", "API del servidor", f"{URL}: {type(e).__name__}: {e}")
    rc, out = sh(["systemctl", "is-active", SERVICE])
    line("OK" if out == "active" else "AVISO", f"Servicio {SERVICE}", out.splitlines()[0] if out else "?")
    sp = serial_ports()
    if not sp:
        line("AVISO", "Puerto serie del ESP32", "ningún /dev/ttyUSB* ni ttyACM* (conecta el ESP32 por USB; es opcional)")
    elif not os.access(sp[0], os.R_OK | os.W_OK):
        line("AVISO", "Puerto serie del ESP32", f"{sp[0]} sin permiso: sudo usermod -aG {serial_group(sp[0])} $USER y reinicia sesión")
    else:
        line("OK", "Puerto serie del ESP32", sp[0])
    rc, out = sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    line("OK" if not rc else "AVISO", "NetworkManager (nmcli)", out.replace("\n", " | ") if not rc else out)
    line("OK" if shutil.which("btop") else "AVISO", "btop", shutil.which("btop") or "no instalado (sudo dnf install btop)")
    line("OK" if shutil.which("sudo") else "AVISO", "sudo", shutil.which("sudo") or "no está")
    line("OK", "Arranque por defecto", sh(["systemctl", "get-default"])[1])
    line("OK" if os.access(os.path.dirname(NO_MENU), os.W_OK) else "AVISO", "Menú de inicio", "activo" if not os.path.exists(NO_MENU) else "desactivado (~/.sin-menu)")
    print("\nResultado:", "TODO BIEN" if not bad else f"{bad} FALLO(S)")
    return 1 if bad else 0


def main():
    if "--check" in sys.argv:
        sys.exit(check())
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("parkuni_tui necesita una terminal interactiva (usa --check para probar sin interfaz).")
        sys.exit(1)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    poller = Poller()
    poller.once()                                 # primer estado antes de dibujar
    poller.start()
    try:
        curses.wrapper(lambda scr: App(scr, poller).run())
    except Exception:
        traceback.print_exc()
        print("\nEl menú falló; sigues en la terminal normal. Escribe 'parkuni' para reintentarlo.")
        sys.exit(1)


if __name__ == "__main__":
    main()
