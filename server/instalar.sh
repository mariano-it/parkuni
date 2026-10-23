#!/usr/bin/env bash
# Instala ParkUni en este Fedora (Fase 2 de docs/MONTAJE_FEDORA.md) y deja la laptop lista como servidor.
#
#   sudo bash server/instalar.sh
#
# Se puede correr varias veces (vuelve a copiar el proyecto y reinicia el servicio; la base de datos no se toca).
# Todo es reversible: ver "Cómo deshacerlo" en docs/CAMBIOS_SERVIDOR.md.
# NO apaga el escritorio ni toca SELinux, el firewall o la red: eso se hace desde el menú (parkuni).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Corre este script con sudo:  sudo bash $0"; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/parkuni
DIA="$(date +%Y%m%d)"

paso() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

paso "1/10  Paquetes: btop (monitor del equipo), sqlite (consola de la base), rsync, avahi y nss-mdns (nombre en la red)"
dnf install -y btop sqlite rsync avahi nss-mdns

paso "2/10  Usuario del servicio (sin login)"
id parkuni >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin parkuni
id parkuni

paso "3/10  Copiando el proyecto a $DEST (la carpeta server/data no se toca)"
mkdir -p "$DEST"
rsync -a --chown=root:root --exclude 'server/data' --exclude 'server/certs' --exclude '.git' --exclude '__pycache__' "$SRC"/ "$DEST"/
mkdir -p "$DEST/server/data"
chown -R parkuni:parkuni "$DEST/server/data"
chmod +x "$DEST/server/tui/parkuni_tui.py"
restorecon -R "$DEST"

paso "4/10  Contraseñas del servidor: /etc/parkuni.env (plantilla vacía; las escribes tú, nunca van en el código)"
if [ ! -e /etc/parkuni.env ]; then
  install -m 600 -o root -g root /dev/null /etc/parkuni.env
  cat > /etc/parkuni.env <<'EOF'
# ParkUni: contraseñas del servidor. Escribe el valor después del "=" y quita el "#". Sin comillas ni espacios.
# Edita con:  sudo nano /etc/parkuni.env     y aplica con:  sudo systemctl restart parkuni
#
# Contraseña para entrar al panel de administración (admin.html). Sin ella el panel queda bloqueado.
#PARKUNI_ADMIN_PASSWORD=
# Contraseña para activar / desactivar la emergencia. Sin ella la emergencia queda bloqueada.
#PARKUNI_EMERGENCY_PASSWORD=
# Contraseña ÚNICA del apagado de emergencia informática (detiene el servidor y no deja que vuelva a arrancar solo).
# Distinta de las dos anteriores. Sin ella el apagado no está disponible.
#PARKUNI_KILL_PASSWORD=
# Opcional: 1 = el panel solo se puede abrir desde esta misma laptop (127.0.0.1), no desde la red.
#PARKUNI_ADMIN_LOCAL_ONLY=1
EOF
  echo "creado /etc/parkuni.env (vacío): ponle las contraseñas con  sudo nano /etc/parkuni.env"
else
  echo "/etc/parkuni.env ya existe: no se toca"
fi

paso "5/10  Servicio systemd (arranca solo al encender la laptop)"
if [ -f /etc/systemd/system/parkuni.service ]; then
  cp -a /etc/systemd/system/parkuni.service "/etc/systemd/system/parkuni.service.bak-$DIA"
fi
install -m 644 "$DEST/server/parkuni.service" /etc/systemd/system/parkuni.service
restorecon /etc/systemd/system/parkuni.service
install -m 644 "$DEST/server/parkuni-tunel.service" /etc/systemd/system/parkuni-tunel.service   # solo se copia; no se activa (necesita cloudflared)
restorecon /etc/systemd/system/parkuni-tunel.service
systemctl daemon-reload
systemctl enable parkuni
systemctl restart parkuni

paso "6/10  Acceso directo: escribe  parkuni  para abrir el menú"
ln -sf "$DEST/server/tui/parkuni_tui.py" /usr/local/bin/parkuni

paso "7/10  Cerrar la tapa de la laptop no debe suspender el servidor"
install -d /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/parkuni.conf <<'EOF'
# ParkUni: la laptop funciona como servidor; cerrar la tapa no la suspende.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl kill -s HUP systemd-logind.service || echo "(se aplicará en el próximo reinicio)"

paso "8/10  Nombre de la laptop en la red: parkuni (el ESP32 la busca como parkuni.local)"
if [ "$(hostnamectl --static)" != "parkuni" ]; then
  [ -f /etc/hostname ] && cp -a /etc/hostname "/etc/hostname.bak-$DIA"
  hostnamectl set-hostname parkuni
fi
systemctl enable --now avahi-daemon
systemctl restart avahi-daemon
sleep 2
echo "parkuni.local -> $(getent ahostsv4 parkuni.local | awk 'NR==1{print $1}')"

paso "9/10  Permiso para el puerto USB-serie (monitor serie del menú)"
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
  usermod -aG dialout "$SUDO_USER"
  echo "$SUDO_USER añadido al grupo dialout (se aplica al volver a iniciar sesión)"
else
  echo "(sin usuario normal detectado: agrega el tuyo con  sudo usermod -aG dialout USUARIO)"
fi

paso "10/10  Verificación"
sleep 2
echo "servicio : $(systemctl is-active parkuni)  ·  arranque automático: $(systemctl is-enabled parkuni)"
curl -s -o /dev/null -w "GET /            -> HTTP %{http_code}\n" http://localhost:8000/
curl -s -o /dev/null -w "GET /admin.html  -> HTTP %{http_code}\n" http://localhost:8000/admin.html
echo "GET /api/status  -> $(curl -s http://localhost:8000/api/status | head -c 160)…"
echo "base de datos    -> $(ls -l "$DEST/server/data/parkuni.db" 2>&1 | awk '{print $1, $3, $5" bytes", $9}')"
echo "SELinux          -> $(getenforce)"
IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')"

cat <<EOF

Listo. Direcciones desde cualquier equipo de la misma red:
  App del conductor : http://${IP:-<IP>}:8000/
  Panel de admin    : http://${IP:-<IP>}:8000/admin.html
  Nombre en la red  : http://parkuni.local:8000/   (el ESP32 la busca por este nombre, sin importar la IP)
Abre el menú con:  parkuni
EOF
