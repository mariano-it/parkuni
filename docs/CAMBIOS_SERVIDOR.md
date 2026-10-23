# Registro de cambios en el servidor

Cada cambio queda aquí con **qué es, por qué y cómo deshacerlo** (regla 3 de `MONTAJE_FEDORA.md`).
No se borran ni editan logs, `~/.bash_history` ni el journal.

## Estado actual (2026-10-20)

| Fase | Estado |
|---|---|
| 1. Revisar el entorno | Hecha (solo lectura) |
| 2. Instalar y arrancar el servidor | **Hecha**: `instalar.sh` ya se corrió; servicio activo y habilitado, hostname `parkuni`, avahi, tapa sin suspender, `/opt/parkuni` al día con el repo. Tras cambiar el repo hay que volver a correr `sudo bash server/instalar.sh` para copiarlo a `/opt` |
| 3. Conectar el ESP32 | **Hecha (2026-10-22)**: el firmware nuevo (mDNS `parkuni.local` + prueba de LED) ya se cargó al ESP32 desde el Arduino IDE y los LED se probaron con hardware real. Cada vez que cambie `firmware/parkuni/parkuni.ino` hay que cargarlo de nuevo (esta laptop no tiene `arduino-cli`). Se ve en el Monitor Serie (115200) o en el menú, tecla `s` |
| 4. Internet público | No se hará por ahora |
| 5. Menú de terminal | Hecha y probada (`server/tui/parkuni_tui.py`), con monitor serie y prueba de LED RGB |
| 6. Apagar el escritorio | **Decidido (2026-10-22): sí se quita.** Aún no se ejecuta; lo hace el usuario con la opción ya creada (menú, opción 8 → `sudo systemctl set-default multi-user.target`). Hasta entonces sigue en `graphical.target` |

Ideas descartadas / sin prioridad: botón para fijar la IP del servidor a mano (el ESP32 ya la busca por `parkuni.local`).

## Fase 1: lo que se encontró

Fedora 44 Workstation (kernel 7.2.11), Python 3.14.8, Celeron N4020 (2 núcleos), 3.6 GB de RAM, 114 GB de eMMC.
Solo WiFi (Realtek RTL8821CE con driver `rtw88` del kernel, **no** necesita RPM Fusion), sin puerto de cable.
SELinux `Enforcing`. `firewalld` activo en la zona `FedoraWorkstation`, que ya permite los puertos TCP/UDP 1025-65535:
**el puerto 8000 ya es alcanzable desde la red local, no hace falta regla de firewall.**
(La regla de ejemplo de la guía usa `192.168.0.0/16`; la red de la universidad es `172.20.x.x`, así que no habría servido.)
Batería al 19 % y con solo el 23 % de su capacidad original: dejarla siempre enchufada.
NetworkManager ya tiene guardados `Room-Lab_5G` y dos perfiles `Anahuac` (abiertos). Las claves del sistema se guardan en el
perfil (no en el llavero de la sesión), así que el WiFi debe reconectarse solo sin escritorio.

Decisiones del usuario: ~~la contraseña de emergencia se queda en el código~~ (cambió el 2026-10-22: ahora todas las contraseñas van en `/etc/parkuni.env`, ver abajo).

## Cambios en el repositorio (hechos sin sudo; git lo maneja el usuario)

**2026-10-22:** el repo se reconectó a git (rama `main`, remoto `origin` = https://github.com/mariano-it/parkuni.git) con `git fetch` + `git reset origin/main` (mixto: no cambia archivos). Claude solo lo inicializó y conectó; los commits y push los hace el usuario. Se comparó antes contra `origin/main`: todo lo distinto son cambios nuestros. Deshacer: borrar la carpeta `.git`.

| Archivo | Qué | Por qué |
|---|---|---|
| `server/tui/parkuni_tui.py` | Menú de terminal nuevo (solo librería estándar) | Operar el servidor sin escritorio: sensores, base, servicio, WiFi, logs, emergencia, btop, entorno gráfico |
| `server/instalar.sh` | Instalador de la Fase 2 (idempotente) | Un solo comando con `sudo` en vez de ocho |
| `server/parkuni.service` | Se quitó `Environment=PARKUNI_EMERGENCY_PASSWORD=cambia-esta-contrasena`; se agregó `EnvironmentFile=-/etc/parkuni.env` (opcional) | El placeholder habría reemplazado la contraseña por defecto del código |
| `firmware/parkuni/parkuni.ino` | El servidor se busca por nombre (mDNS `parkuni.local`) en vez de la IP fija `172.20.5.57`; si no responde usa la última IP buena (guardada en la memoria del ESP32) y luego `SERVER_IP_RESPALDO`; busca de nuevo tras 3 fallos seguidos; `WiFi.setSleep(false)` | La IP cambia en cada red (casa, universidad). **Hay que volver a cargarlo en el ESP32** |
| `docs/CAMBIOS_SERVIDOR.md` | Este registro | Regla 3 |
| `server/tui/parkuni_tui.py` (2026-10-20) | Pantalla «Prueba de LED RGB» (tecla `l`) | Lo mismo que la tarjeta del panel, desde la terminal |
| `server/tui/parkuni_tui.py` (2026-10-20) | Monitor serie: `serial_text()` quita NUL/escapes de lo que llega del ESP32; `App.put` también atrapa `ValueError`; `SerialMon.stop()` ya no cierra el puerto desde otro hilo (lo cierra el propio hilo lector) | Al abrir el puerto el ESP32 se reinicia y suelta basura con NUL: `addstr` lanzaba `ValueError` y el menú se cerraba ("El menú falló"). Además `stop()` hacía morir al hilo lector con `TypeError` en cada salida. Hay que copiarlo a `/opt/parkuni` (`sudo bash server/instalar.sh`) |
| `server/server.py`, `public/admin.html`, `public/js/admin.js`, `public/css/base.css`, `firmware/parkuni/parkuni.ino` (2026-10-20) | Prueba de LED RGB: tarjeta en el panel para fijar un color por LED (`POST /api/ledtest`); el ESP32 lo recibe en la respuesta de `/api/sensor` (`"test"`) y la prueba manda sobre libre/ocupado/emergencia. Se apaga sola a los 3 min o tras 3 fallos de red | Diagnosticar un LED RGB dañado sin reprogramar. También está en el menú de terminal (`l`, con modo demo `d`). Hay que correr `sudo bash server/instalar.sh` y volver a cargar el firmware |

| `server/server.py`, `public/login.html`, `public/admin.html`, `public/js/admin.js`, `public/js/config.js`, `server/tui/parkuni_tui.py` (2026-10-22) | Login del panel: `POST /api/login` (cookie `HttpOnly`, 8 h, en memoria), `/api/logout`, `/api/me`. `admin.html` y las rutas de escritura del panel exigen sesión; `/api/status?events` solo con sesión. 5 fallos/min por IP bloquean el login. Se **quitó del código** la contraseña de emergencia por defecto: ahora `PARKUNI_ADMIN_PASSWORD` y `PARKUNI_EMERGENCY_PASSWORD` salen del entorno y, si faltan, el panel / la emergencia quedan bloqueados. Opcional `PARKUNI_ADMIN_LOCAL_ONLY=1`. El menú pide la contraseña del panel al usar LED o emergencia | Proteger el panel antes de publicar y sacar las contraseñas del repo. Probado en un servidor aparte (puerto 8123+) |
| `server/respaldo.py`, `server/tui/parkuni_tui.py` (2026-10-22) | Respaldo **manual** de la base (API de respaldo de SQLite, verifica integridad, conserva 14): pantalla `b` del menú con confirmación; copias en `~/respaldos-parkuni/`. No hay temporizador a propósito: la laptop no aguanta un respaldo automático | Tener copia de `parkuni.db` sin detener el servicio |
| `server/instalar.sh`, `server/parkuni.service` (2026-10-22) | `instalar.sh` crea `/etc/parkuni.env` vacío (600, root) si no existe; el comentario del `.service` ya no habla de la contraseña por defecto | Ver arriba |

**Requiere correr** `sudo bash server/instalar.sh` y escribir las contraseñas en `/etc/parkuni.env` (`sudo nano /etc/parkuni.env`, luego `sudo systemctl restart parkuni`). Hasta entonces la copia de `/opt/parkuni` sigue con el código viejo (contraseña por defecto, panel abierto).

Deshacer: los archivos modificados (`parkuni.service`, `parkuni.ino`) se recuperan de GitHub (mariano-it/parkuni); los nuevos se borran con `rm -r server/tui server/instalar.sh docs/CAMBIOS_SERVIDOR.md`.

## Cambios fuera del repositorio

### Ya hechos (sin sudo)

| Qué | Por qué | Cómo deshacerlo |
|---|---|---|
| Bloque `# >>> parkuni-tui >>>` al final de `~/.bash_profile` (copia previa en `~/.bash_profile.bak-20261019`) | Abrir el menú al iniciar sesión en una terminal de texto local | Borrar el bloque, o `cp ~/.bash_profile.bak-20261019 ~/.bash_profile` |

El bloque **no** se activa en SSH, dentro del escritorio, con `PARKUNI_TUI=0` ni si existe `~/.sin-menu`, y nunca usa `exec`:
si el menú falla o sales de él, queda la shell normal. Si el archivo de `/opt/parkuni` no existe, no hace nada.

### Los hace `sudo bash server/instalar.sh` (pendiente de correr)

| # | Qué | Cómo deshacerlo |
|---|---|---|
| 1 | Paquetes `btop`, `sqlite`, `rsync` (`dnf install`) | `sudo dnf remove btop sqlite` |
| 2 | Usuario de sistema `parkuni` (sin login, home `/opt/parkuni`) | `sudo userdel parkuni` |
| 3 | Copia del proyecto en `/opt/parkuni` (propiedad de root) y `/opt/parkuni/server/data` (propiedad de `parkuni`, ahí vive `parkuni.db`) | `sudo rm -rf /opt/parkuni` (**respalda antes `parkuni.db` si importa**) |
| 4 | `/etc/systemd/system/parkuni.service`, habilitado al arranque (si ya existía se guarda `.bak-AAAAMMDD`) | `sudo systemctl disable --now parkuni && sudo rm /etc/systemd/system/parkuni.service && sudo systemctl daemon-reload` |
| 5 | Enlace `/usr/local/bin/parkuni` -> el menú | `sudo rm /usr/local/bin/parkuni` |
| 6 | `/etc/systemd/logind.conf.d/parkuni.conf`: cerrar la tapa no suspende la laptop | `sudo rm /etc/systemd/logind.conf.d/parkuni.conf` y reiniciar |
| 7 | Nombre de la laptop: `parkuni` (antes `fedora`, que era el nombre dinámico; copia en `/etc/hostname.bak-AAAAMMDD` si existía) y `avahi-daemon` activo, para que responda como `parkuni.local` | `sudo hostnamectl set-hostname fedora && sudo systemctl restart avahi-daemon` |
| 9 | (2026-10-22) `/etc/parkuni.env` (plantilla vacía, 600 root): contraseñas del panel y de la emergencia | `sudo rm /etc/parkuni.env` (el panel y la emergencia quedan bloqueados) |
| 8 | Tu usuario se agrega al grupo `dialout` (`usermod -aG`), para leer el puerto USB-serie del ESP32 desde el monitor serie del menú | `sudo gpasswd -d TU_USUARIO dialout` |

No toca: SELinux (sigue `Enforcing`), el firewall, la red ni el entorno gráfico.

### Pendiente de decidir por el usuario (desde el menú, opción 8)

- `sudo systemctl set-default multi-user.target`: quita el escritorio en el próximo arranque.
  Volver: opción 1 del mismo menú, o `sudo systemctl set-default graphical.target && sudo reboot`.
  La pantalla revisa antes que el servicio arranque solo, que el WiFi se reconecte, que el menú esté en `~/.bash_profile`
  y que la tapa no suspenda.

## Notas operativas

- **Cambiar de red (casa <-> universidad):** menú, opción 4. La IP cambia con cada red, pero el ESP32 ya no depende de ella:
  busca a la laptop por nombre (`parkuni.local`, mDNS). Mira el Monitor Serie (115200): `Servidor: parkuni.local -> <IP>`.
  Si dice `no respondio, uso <IP>`, esa red no deja pasar mDNS: se usa la última IP buena guardada en el ESP32.
- **Respaldo de la base:** copiar `/opt/parkuni/server/data/parkuni.db` (con el servicio detenido, o `sqlite3 ... ".backup ..."`).
- **Si el menú falla al iniciar sesión:** cae a la shell normal. Para que no se abra: `touch ~/.sin-menu` (o menú, opción 9).
  Para abrirlo a mano: `parkuni`.
- **Si algo sale mal sin escritorio:** `Ctrl+Alt+F3` abre otra terminal; en GRUB, `e` sobre la entrada de Fedora y
  `systemd.unit=graphical.target` al final de la línea `linux`, `Ctrl+X` (solo ese arranque).
