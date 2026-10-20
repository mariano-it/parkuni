# Montaje de ParkUni en Fedora (instrucciones para Claude Code)

> **Cómo usar este archivo:** copia la carpeta del proyecto a la máquina con **Fedora 44**, abre Claude Code dentro de la carpeta y dile:
> *"Lee `docs/MONTAJE_FEDORA.md` y empecemos por la Fase 1."*
> Claude trabaja **por fases**, se detiene al final de cada una y espera tu OK.
>
> Nota: esta guía se escribió sin acceso a una máquina con Fedora 44. Si un comando, paquete o ruta cambió (por ejemplo la sintaxis de `dnf5`), **verifícalo y avisa al usuario** en lugar de asumir.

---

## 0. Reglas de trabajo (obligatorias)

1. **Confirma antes de cambiar el sistema.** Instalar paquetes, tocar `systemd`, red, firewall, SELinux o el escritorio se explica primero (qué hace, cómo se deshace) y se ejecuta solo con el OK del usuario.
2. **Contraseñas: nunca.** No pidas, leas, guardes ni escribas contraseñas (ni la de `sudo`, ni la de WiFi, ni la de emergencia). Cuando haga falta `sudo`, dile al usuario el comando exacto y que lo corra él en su terminal. Si un archivo necesita una contraseña (p. ej. `/etc/parkuni.env`), crea la plantilla vacía y que el usuario escriba el valor.
3. **No ocultes nada.** No borres ni edites logs, `~/.bash_history`, el journal ni los registros de auditoría. Todo lo que hagas queda registrado en `docs/CAMBIOS_SERVIDOR.md` (fecha, qué, por qué, cómo deshacerlo).
4. **Todo reversible.** Antes de sobrescribir una configuración del sistema, guarda copia (`archivo.bak-AAAAMMDD`).
5. **SELinux se queda activo.** No lo pongas en `permissive` ni lo desactives para "que funcione": si algo se bloquea, diagnostica (Fase 2, paso 6) y arregla la etiqueta o la política concreta.
6. **El escritorio se apaga al final**, solo cuando todo lo demás funcione y el usuario lo confirme (Fase 6).
7. **No hagas `git commit` ni `git push`.** Eso lo hace el usuario.
8. **Limpieza:** al terminar, borra solo lo que tú creaste temporalmente (scripts de prueba, archivos en `/tmp`). No toques nada más.
9. Si algo no coincide con este documento (versión de Python, red, hardware), **detente y pregunta**.

---

## 1. Qué es ParkUni

Estacionamiento inteligente para la Universidad Anáhuac Cancún: sensores ultrasónicos + LED RGB (ESP32) que le dicen a una app web qué cajones están libres. La app (estilo Waze) pregunta la zona preferida del conductor, asigna el cajón más cercano por ruta real **reservándolo** para que no se lo den a otro, guía con voz, redirige si lo ocupan y vigila el límite de velocidad con GPS. Hay un botón de **emergencia** (contraseña) que pone los LED en morado intermitente.

```
ESP32 (4 sensores + 4 LED) ──POST /api/sensor──▶  server/server.py + SQLite  ◀── App del conductor / Panel admin
                          ◀── {"emergency": bool} ──┘
```

Lee primero: `README.md` (visión general), `server/server.py` (API, reservas y base de datos), `public/js/map-data.js` (los dos mapas y `MAP.sensors`), `firmware/parkuni/parkuni.ino` y `docs/Ficha_Tecnica_Estacionamiento.docx` (cableado).

Solo usa **la librería estándar de Python** (no hay `pip install`). La base es `server/data/parkuni.db` (SQLite).

**Objetivo de este montaje:** dejar el servidor corriendo solo (al encender la máquina), con una **herramienta de terminal** (menú ASCII) para operarlo, y poder apagar el escritorio para que parezca un servidor.

---

## 2. Fase 1 — Revisar el entorno (solo lectura)

Corre y resume en una tabla, sin cambiar nada:

```bash
cat /etc/os-release | head -4 ; uname -r
python3 --version; python3 -c "import sqlite3, curses; print('sqlite', sqlite3.sqlite_version, '/ curses ok')"
ip -br addr; nmcli device status; nmcli -t -f ACTIVE,SSID dev wifi | head
systemctl get-default; systemctl is-active NetworkManager firewalld
getenforce                                   # SELinux: debería decir Enforcing
sudo -n true 2>&1 | head -1                  # ¿hay sudo sin contraseña? (normalmente no)
df -h / ; free -h
rfkill list; lspci -nnk | grep -iA3 'network\|ethernet'    # hardware de red y su driver
```

Reporta: versión de Python (el proyecto se probó con 3.14; cualquiera ≥ 3.9 debería servir), si hay red por cable y/o WiFi, estado de SELinux y de `firewalld`, y la **IP actual**. Pregunta al usuario cuál interfaz usará el ESP32 para llegar al servidor (misma red/router; ojo con redes abiertas que aíslan clientes entre sí).

Si el WiFi no funciona: revisa `rfkill`, `journalctl -k | grep -i firmware` y el chip (`lspci -nnk`). Algunos chips (p. ej. Broadcom) necesitan firmware o drivers de **RPM Fusion**; para instalarlos hace falta internet, así que conviene hacerlo **por cable** y **antes** de quitar el escritorio. Explícaselo al usuario y espera su OK.

**Detente y espera el OK.**

---

## 3. Fase 2 — Instalar y arrancar el servidor

1. Paquetes que pueden faltar (sin duplicar los que ya estén): `sudo dnf install -y rsync python3 sqlite`. (Son comandos con `sudo`: se los das al usuario para que los corra él.)
2. Elegir ruta de instalación (propuesta: `/opt/parkuni`) y usuario de servicio sin login:
   ```bash
   sudo useradd --system --home-dir /opt/parkuni --shell /usr/sbin/nologin parkuni
   sudo mkdir -p /opt/parkuni && sudo rsync -a --exclude 'server/data' --exclude 'server/certs' --exclude '.git' ./ /opt/parkuni/
   sudo mkdir -p /opt/parkuni/server/data && sudo chown -R parkuni:parkuni /opt/parkuni/server/data
   ```
3. **Contraseña de emergencia** en archivo de entorno, nunca en el código ni en el repositorio:
   ```bash
   sudo install -m 600 -o root -g root /dev/null /etc/parkuni.env
   # El USUARIO edita el archivo y escribe una sola línea:  PARKUNI_EMERGENCY_PASSWORD=<la contraseña>
   ```
4. Servicio systemd a partir de `server/parkuni.service`: ajusta `WorkingDirectory`, `ExecStart` y reemplaza la línea `Environment=PARKUNI_EMERGENCY_PASSWORD=...` por `EnvironmentFile=/etc/parkuni.env`. Instálalo en `/etc/systemd/system/`, `sudo systemctl daemon-reload`, `sudo systemctl enable --now parkuni`.
5. **Firewall (`firewalld`)**: en Fedora el firewall viene activo y bloquea el puerto 8000, así que el ESP32 y los celulares no llegarían. Abrirlo **solo para la red local** (ajustando la subred real que reportó la Fase 1):
   ```bash
   sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.0.0/16" port port="8000" protocol="tcp" accept'
   sudo firewall-cmd --reload
   sudo firewall-cmd --list-rich-rules
   ```
   No abras el puerto a todo internet (`--add-port=8000/tcp` sin origen) sin preguntar.
6. **SELinux**: debe seguir en `Enforcing`. Un servicio propio ejecutado desde `/opt` normalmente corre sin problemas; si el servicio no arranca o no puede escribir la base, mira los bloqueos con `sudo ausearch -m avc -ts recent` (y `journalctl -u parkuni`) y corrige **la etiqueta** (`sudo restorecon -Rv /opt/parkuni`, o `semanage fcontext` para `server/data`) en vez de desactivar SELinux. Anota la causa y la solución en `docs/CAMBIOS_SERVIDOR.md`.
7. Verificar:
   ```bash
   systemctl status parkuni --no-pager
   curl -s localhost:8000/api/status | head -c 300
   curl -sI localhost:8000/ | head -1 ; curl -sI localhost:8000/admin.html | head -1
   curl -s -X POST localhost:8000/api/sensor -d '{"sensors":{"A1":{"occupied":true,"distance_cm":9}}}'
   curl -s -X POST localhost:8000/api/claim -d '{"id":"prueba","spot":"N05-5"}'
   ```
   Y desde **otro dispositivo de la red** (celular): abrir `http://<IP>:8000/admin.html`.
8. **Borrar los datos de prueba** de esa verificación antes de producción: parar el servicio, borrar `server/data/parkuni.db` y volver a arrancar (el servidor la recrea vacía).
9. Dejar la **IP fija**: sugerir al usuario una reserva DHCP en el router (la más sencilla) y anotar la IP. El ESP32 la necesita.

Anota todo en `docs/CAMBIOS_SERVIDOR.md`. **Detente y espera el OK.**

---

## 4. Fase 3 — Conectar el ESP32

Claude **no puede cargar el firmware** (se hace desde Arduino IDE en la compu del usuario); sí prepara todo:

1. En `firmware/parkuni/parkuni.ino` cambiar `SERVER_URL` a `http://<IP-del-servidor>:8000/api/sensor` (y `WIFI_SSID` si cambia la red).
2. El usuario carga el sketch. Pedirle que abra el Monitor Serie (115200): debe verse `POST -> 200`. Si sale `POST -> -1`, el ESP32 no llega al servidor: revisar IP, que estén en la misma red y el firewall (paso 5 de la Fase 2).
3. Verificar desde el servidor: `curl -s localhost:8000/api/status` debe mostrar `seen: ["A1","A2","A3","A4"]` y `esp: true`; abrir `http://<IP>:8000/admin.html` y ver los 4 sensores. Pedirle al usuario que tape un sensor con la mano y comprobar que el cajón cambia.
4. Probar la **emergencia** desde el panel web (la contraseña la escribe el usuario): los 4 LED deben parpadear en morado en ~2 s y volver al desactivarla.

**Plan B (solo si el ESP32 no alcanza al servidor por WiFi):** puente por cable. ESP32 conectado por USB a esta máquina y un script `server/serial_bridge.py` que lea el puerto serie y haga `POST /api/sensor` a `localhost`. Requeriría `pyserial` (`sudo dnf install python3-pyserial`), que el usuario esté en el grupo `dialout` para leer `/dev/ttyUSB0` y que el firmware imprima líneas parseables. No lo construyas sin que el usuario lo pida.

**Detente y espera el OK.**

---

## 5. Fase 4 — Internet público (opcional)

Para que cualquiera abra la app desde su celular (y el GPS funcione, que exige https):

1. Instalar `cloudflared` desde el **repositorio RPM oficial de Cloudflare** para Fedora/RHEL (consulta su documentación vigente y muéstrale al usuario los comandos antes de ejecutarlos; no ejecutes `curl ... | sh` a ciegas). El **login lo hace el usuario en el navegador** (`cloudflared tunnel login`); tú no manejas credenciales.
2. Crear túnel hacia `http://localhost:8000` y dejarlo como servicio systemd.
3. ⚠️ **Antes de publicar:** el panel `admin.html` y las rutas de escritura de la API no tienen inicio de sesión. Proponer proteger `admin.html` y `/api/*` (excepto `/api/status`, `/api/driver*`, `/api/claim` y `/api/event`) con **Cloudflare Access**, o separar la ruta del panel. No publiques sin resolver esto.

---

## 6. Fase 5 — Herramienta de terminal `parkuni-tui`

Menú interactivo en terminal, con diseño ASCII, para operar el servidor sin recordar comandos. Y que aparezca **automáticamente al iniciar sesión** cuando el escritorio esté apagado.

### Requisitos
- Python **estándar** (`curses`, `sqlite3`, `subprocess`, `urllib`): **sin dependencias externas** (la máquina puede no tener internet).
- Archivo: `server/tui/parkuni_tui.py` (un solo archivo o paquete pequeño), ejecutable con `python3 server/tui/parkuni_tui.py`. Funciona en una terminal de 80×24 y se adapta si es más grande.
- Colores con `curses` (verde = libre/OK, rojo = ocupado/error, naranja/amarillo = aviso). Si la terminal no soporta color, debe seguir siendo legible.
- Se maneja con flechas, Enter, `q`/Esc para volver. Mostrar siempre una barra de ayuda con las teclas.
- Debe tener `--check` (verifica sin abrir la interfaz: lee la base, consulta la API, imprime OK/FALLO) para poder probarlo.

### Pantalla principal (referencia visual)
```
 ____            _    _   _       _
|  _ \ __ _ _ __| | _| | | |_ __ (_)
| |_) / _` | '__| |/ / | | | '_ \| |
|  __/ (_| | |  |   <| |_| | | | | |
|_|   \__,_|_|  |_|\_\\___/|_| |_|_|     Estacionamiento inteligente

  Servicio: ● activo     IP: 192.168.1.50:8000     Emergencia: NORMAL
  Sensores: A1 ● libre  A2 ● ocupado  A3 ● libre  A4 ○ sin señal

  ▶ 1  Sensores en vivo
    2  Base de datos
    3  Aplicación (web)
    4  Red / WiFi
    5  Logs
    6  Emergencia
    7  Escritorio (activar / desactivar)
    8  Salir a la shell
    9  Apagar / reiniciar

  ↑↓ mover   Enter elegir   q salir
```

### Secciones
1. **Sensores en vivo** — tabla que se refresca cada segundo: id (A1–A4), cajón, estado (libre/ocupado), distancia en cm, hace cuánto reportó y "sin señal" si pasaron > 120 s. Lee la API local (`/api/status`) o, si el servicio está caído, la base directamente.
2. **Base de datos** — visor **de solo lectura** (abrir SQLite con `file:...?mode=ro` y `uri=True`) de las tablas `sensors`, `overrides`, `kv` y `events`: lista de tablas, filas con scroll, conteo y búsqueda simple. Nada de editar ni borrar desde aquí.
3. **Aplicación** — estado de `parkuni` (`systemctl is-active`), URLs con la IP real (`/`, `/admin.html`, y la de `POST /api/sensor` para el ESP32), conductores activos y cajones reservados (de `/api/status`), y acciones **iniciar / detener / reiniciar** (cada una llama `sudo systemctl …` y avisa si pide contraseña: la escribe el usuario en esa terminal).
4. **Red / WiFi** — con `nmcli`: estado de interfaces, IP, listar redes WiFi, conectar a una (SSID elegido de la lista; si la red tiene clave, **la teclea el usuario** y se pasa directo a `nmcli`, sin guardarla en ningún archivo ni log propio), desconectar y probar conexión (ping al router). Mostrar también el estado de `firewalld` y si el puerto 8000 está abierto a la red local.
5. **Logs** — `journalctl -u parkuni` en vivo (`-f`) con scroll, y la bitácora de eventos de la base (`events`). Filtro por texto.
6. **Emergencia** — ver estado y activar/desactivar llamando a `POST /api/emergency`; **pide la contraseña al usuario** en el momento (no se guarda).
7. **Escritorio** — muestra `systemctl get-default`.
   - *Desactivar:* `sudo systemctl set-default multi-user.target` (con confirmación; avisa que surte efecto al reiniciar).
   - *Activar:* `sudo systemctl set-default graphical.target` y ofrecer `sudo systemctl isolate graphical.target` para verlo al instante.
8. **Salir a la shell** — cierra curses limpiamente y devuelve al usuario a su shell normal.
9. **Apagar / reiniciar** — con confirmación doble (`systemctl poweroff` / `reboot`).

### Que aparezca al iniciar sesión (sin atrapar al usuario)
- **El login de Fedora no se reemplaza**: sigue pidiendo usuario y contraseña (getty/login/PAM). Nada de autenticación propia ni autologin sin contraseña.
- Lanzar la herramienta justo **después** de iniciar sesión desde `~/.bash_profile` (o `/etc/profile.d/parkuni-tui.sh`), con estas guardas:
  ```sh
  # solo en terminal local interactiva (no SSH, no scripts), y si no se pidió saltarla
  if [ -t 0 ] && [ -t 1 ] && [ -z "$SSH_CONNECTION" ] && [ -z "$PARKUNI_TUI_ACTIVE" ] \
     && [ "${PARKUNI_TUI:-1}" != "0" ] && [ ! -e "$HOME/.sin-menu" ]; then
    export PARKUNI_TUI_ACTIVE=1
    python3 /opt/parkuni/server/tui/parkuni_tui.py || true     # NUNCA 'exec': si falla, caes a la shell
  fi
  ```
- **Siempre hay salida:** opción 8 del menú, y si el programa falla o termina, queda la shell normal. Saltarlo a mano: `touch ~/.sin-menu` o `PARKUNI_TUI=0 bash -l`.
- No ejecutar la herramienta como root. Lo que necesite privilegios usa `sudo` por acción.
- Con SELinux activo, el script corre como el usuario normal sin problema; si algo se bloquea, diagnostícalo como en la Fase 2, paso 6.

### Pruebas antes de darla por buena
- `python3 server/tui/parkuni_tui.py --check` → todo OK.
- Abrirla en una terminal de 80×24 y en una grande; navegar todas las secciones; salir con `q` y con "Salir a la shell".
- Simular un fallo (renombrar temporalmente el script) y comprobar que el inicio de sesión sigue llegando a la shell.

**Detente y espera el OK.**

---

## 7. Fase 6 — Apagar el escritorio (modo servidor)

Fedora Workstation arranca el escritorio (GNOME con GDM) por el objetivo `graphical.target`. **Solo si el usuario lo confirma**, y después de comprobar que:
- el servicio `parkuni` arranca solo tras un reinicio,
- la red (cable o WiFi con `nmcli`) se conecta sin el escritorio (probar `nmcli device status` desde una TTY: `Ctrl+Alt+F3`),
- el firewall sigue permitiendo el puerto 8000 desde la red local (reglas **permanentes**, no solo en runtime),
- la herramienta `parkuni-tui` funciona en la TTY.

Pasos:
```bash
sudo systemctl set-default multi-user.target     # no arranca GDM / el entorno gráfico
sudo reboot
```
Tras reiniciar debe aparecer el login de texto; al entrar, se abre el menú. **Volver al escritorio:** menú → *Escritorio → Activar*, o `sudo systemctl set-default graphical.target && sudo reboot`.

**Si algo sale mal:** `Ctrl+Alt+F3` abre una TTY; o en el menú de GRUB pulsa `e` sobre la entrada de Fedora y añade `systemd.unit=graphical.target` (o `multi-user.target`) al final de la línea que empieza con `linux`, y arranca con `Ctrl+X` (solo vale para ese arranque).

Importante: **NetworkManager se queda** como gestor de red para no romper el WiFi. No cambies a `systemd-networkd`.

---

## 8. Cierre

Entregables al terminar:
- `docs/CAMBIOS_SERVIDOR.md` con cada paquete instalado, archivo creado, regla de firewall o servicio modificado, y cómo deshacerlo.
- Resumen corto de URLs, IP del servidor, rutas importantes y comandos de uso diario (`systemctl status parkuni`, `journalctl -u parkuni -f`, respaldo: copiar `server/data/parkuni.db`).
- Limpieza de lo temporal que creaste tú. **No se tocan logs ni historial.**

### Cómo desinstalar todo
```bash
sudo systemctl disable --now parkuni && sudo rm /etc/systemd/system/parkuni.service /etc/parkuni.env
sudo firewall-cmd --permanent --remove-rich-rule='rule family="ipv4" source address="192.168.0.0/16" port port="8000" protocol="tcp" accept' && sudo firewall-cmd --reload
sudo rm -rf /opt/parkuni && sudo userdel parkuni
# quitar el bloque de parkuni-tui de ~/.bash_profile (o borrar /etc/profile.d/parkuni-tui.sh)
sudo systemctl set-default graphical.target       # si se había apagado el escritorio
```
(Usa en la regla de firewall la misma subred que se configuró.)

### Decisiones pendientes del usuario (pregúntalas en su momento)
1. ¿Se quita del código la contraseña de emergencia por defecto antes de subir a GitHub? (En el servidor ya se usa `/etc/parkuni.env`.)
2. ¿IP fija por reserva en el router o por configuración en la máquina?
3. ¿Se publica con Cloudflare Tunnel? Si sí, ¿con qué dominio y cómo se protege el panel (Cloudflare Access)?
4. ¿Hace falta el puente serie USB (Plan B) o el WiFi del ESP32 llega bien?
