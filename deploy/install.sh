#!/usr/bin/env bash
# ProxySQL Web UI installer for RHEL 9 / RHEL 10 (and Rocky, AlmaLinux, Oracle Linux, CentOS Stream).
# Safe to re-run: upgrades the application, keeps /etc/proxysql-webui and /var/lib/proxysql-webui.
#
# Usage: sudo ./deploy/install.sh [options]
#   --port N                 HTTPS port (default 8443)
#   --python PATH            Python interpreter (default /usr/bin/python3; >= 3.9)
#   --wheels DIR             Offline install: pip installs only from wheels in DIR
#   --admin-host H           ProxySQL admin host      (new config only, default 127.0.0.1)
#   --admin-port N           ProxySQL admin port      (new config only, default 6032)
#   --admin-user U           ProxySQL admin user      (new config only, default admin)
#   --admin-password P       ProxySQL admin password  (new config only, default admin)
#   --unit NAME              ProxySQL systemd unit    (default proxysql)
#   --no-firewall            Do not open the port in firewalld
#   --no-start               Install but do not enable/start the service
set -euo pipefail

APP_USER=proxysql-webui
APP_DIR=/opt/proxysql-webui
CONF_DIR=/etc/proxysql-webui
DATA_DIR=/var/lib/proxysql-webui
PORT=8443
PYTHON=/usr/bin/python3
WHEELS=""
A_HOST=127.0.0.1; A_PORT=6032; A_USER=admin; A_PASS=admin
UNIT=proxysql
FIREWALL=1
START=1
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    --python) PYTHON="$2"; shift 2;;
    --wheels) WHEELS="$2"; shift 2;;
    --admin-host) A_HOST="$2"; shift 2;;
    --admin-port) A_PORT="$2"; shift 2;;
    --admin-user) A_USER="$2"; shift 2;;
    --admin-password) A_PASS="$2"; shift 2;;
    --unit) UNIT="$2"; shift 2;;
    --no-firewall) FIREWALL=0; shift;;
    --no-start) START=0; shift;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) echo "Unknown option: $1" >&2; exit 2;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
[[ "$UNIT" =~ ^[A-Za-z0-9@._-]+$ ]] || die "Invalid unit name: $UNIT"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "Invalid port: $PORT"

# ---------------------------------------------------------------- OS check
. /etc/os-release
MAJOR="${VERSION_ID%%.*}"
case "${ID} ${ID_LIKE:-}" in
  *rhel*|*centos*|*rocky*|*almalinux*|*ol*|*fedora*) ;;
  *) warn "Untested distribution: ${PRETTY_NAME}";;
esac
if [[ "$MAJOR" != "9" && "$MAJOR" != "10" ]]; then
  warn "Designed for RHEL 9 / 10, detected ${PRETTY_NAME}. Continuing."
fi
log "Installing ProxySQL Web UI on ${PRETTY_NAME}"

# ---------------------------------------------------------------- packages
PKGS=(python3 python3-pip openssl sudo)
MISSING=()
for p in "${PKGS[@]}"; do rpm -q "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  log "Installing packages: ${MISSING[*]}"
  dnf -y install "${MISSING[@]}"
fi
[[ -x "$PYTHON" ]] || die "Python interpreter $PYTHON not found"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python >= 3.9 is required ($PYTHON is $("$PYTHON" -V 2>&1))"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "$PYTHON lacks venv/ensurepip (install python3-pip / python3.X-pip)"

# ---------------------------------------------------------------- user
if ! id "$APP_USER" >/dev/null 2>&1; then
  log "Creating system user $APP_USER"
  useradd --system --home-dir "$DATA_DIR" --shell /sbin/nologin --comment "ProxySQL Web UI" "$APP_USER"
fi
# read the systemd journal without sudo
getent group systemd-journal >/dev/null && usermod -aG systemd-journal "$APP_USER"
# read proxysql.log directly when the datadir is group-readable
getent group proxysql >/dev/null && usermod -aG proxysql "$APP_USER"

# ---------------------------------------------------------------- application
log "Installing application to $APP_DIR"
install -d -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/app"
cp -r "$SRC_DIR/app" "$APP_DIR/app"
install -m 0644 "$SRC_DIR/wsgi.py" "$SRC_DIR/requirements.txt" "$APP_DIR/"
[[ -f "$SRC_DIR/README.md" ]] && install -m 0644 "$SRC_DIR/README.md" "$APP_DIR/"
find "$APP_DIR/app" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR"
chmod -R u=rwX,go=rX "$APP_DIR"

if [[ ! -x "$APP_DIR/venv/bin/python" ]] || ! "$APP_DIR/venv/bin/python" -c 'import sys' 2>/dev/null; then
  log "Creating virtualenv with $PYTHON"
  rm -rf "$APP_DIR/venv"
  "$PYTHON" -m venv "$APP_DIR/venv"
fi
log "Installing Python dependencies"
if [[ -n "$WHEELS" ]]; then
  "$APP_DIR/venv/bin/pip" install --no-index --find-links "$WHEELS" -r "$APP_DIR/requirements.txt"
else
  "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
fi

# ---------------------------------------------------------------- configuration
install -d -m 0750 -o root -g "$APP_USER" "$CONF_DIR" "$CONF_DIR/tls"
install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

if [[ ! -f "$CONF_DIR/config.ini" ]]; then
  log "Writing $CONF_DIR/config.ini"
  SECRET="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
  # escape for sed replacement
  esc() { printf '%s' "$1" | sed -e 's/[\/&|\\]/\\&/g'; }
  sed -e "s|^secret_key = .*|secret_key = $SECRET|" \
      -e "s|^unit = .*|unit = $(esc "$UNIT")|" \
      -e "0,/^host = .*/s||host = $(esc "$A_HOST")|" \
      -e "0,/^port = .*/s||port = $(esc "$A_PORT")|" \
      -e "0,/^user = .*/s||user = $(esc "$A_USER")|" \
      -e "0,/^password = .*/s||password = $(esc "$A_PASS")|" \
      "$SRC_DIR/deploy/config.ini.example" > "$CONF_DIR/config.ini"
else
  log "Keeping existing $CONF_DIR/config.ini"
fi
chown root:"$APP_USER" "$CONF_DIR/config.ini"; chmod 0640 "$CONF_DIR/config.ini"

if [[ ! -f "$CONF_DIR/gunicorn.conf.py" ]]; then
  sed -e "s|^bind = .*|bind = \"0.0.0.0:$PORT\"|" "$SRC_DIR/deploy/gunicorn.conf.py" > "$CONF_DIR/gunicorn.conf.py"
else
  log "Keeping existing $CONF_DIR/gunicorn.conf.py"
  PORT="$(sed -n 's/^bind = ".*:\([0-9]*\)".*/\1/p' "$CONF_DIR/gunicorn.conf.py" | head -1)"; PORT="${PORT:-8443}"
fi
chown root:"$APP_USER" "$CONF_DIR/gunicorn.conf.py"; chmod 0640 "$CONF_DIR/gunicorn.conf.py"

if [[ ! -f "$CONF_DIR/tls/server.crt" ]]; then
  log "Generating self-signed TLS certificate (replace $CONF_DIR/tls/server.{crt,key} with your own)"
  FQDN="$(hostname -f 2>/dev/null || hostname)"
  openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
    -keyout "$CONF_DIR/tls/server.key" -out "$CONF_DIR/tls/server.crt" \
    -subj "/CN=$FQDN" -addext "subjectAltName=DNS:$FQDN,DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
fi
chown root:"$APP_USER" "$CONF_DIR/tls/server.key" "$CONF_DIR/tls/server.crt"
chmod 0640 "$CONF_DIR/tls/server.key"; chmod 0644 "$CONF_DIR/tls/server.crt"

# ---------------------------------------------------------------- sudoers (exact commands only)
LOGFILE="$(sed -n 's/^log_file *= *//p' "$CONF_DIR/config.ini" | head -1)"; LOGFILE="${LOGFILE:-/var/lib/proxysql/proxysql.log}"
CFG_UNIT="$(sed -n 's/^unit *= *//p' "$CONF_DIR/config.ini" | head -1)"; CFG_UNIT="${CFG_UNIT:-$UNIT}"
[[ "$CFG_UNIT" =~ ^[A-Za-z0-9@._-]+$ ]] || die "Invalid unit in config.ini: $CFG_UNIT"
[[ "$LOGFILE" =~ ^/[A-Za-z0-9/._-]+$ ]] || die "Invalid log_file in config.ini: $LOGFILE"
TMP_SUDO="$(mktemp)"
cat > "$TMP_SUDO" <<EOF
# Generated by proxysql-webui installer - exact command lines the web UI may run as root.
Defaults:$APP_USER !requiretty
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start $CFG_UNIT, /usr/bin/systemctl stop $CFG_UNIT, /usr/bin/systemctl restart $CFG_UNIT, /usr/bin/journalctl -u $CFG_UNIT --no-pager -n 500 -o short-iso, /usr/bin/tail -n 500 $LOGFILE
EOF
visudo -cf "$TMP_SUDO" >/dev/null || { rm -f "$TMP_SUDO"; die "Generated sudoers file is invalid"; }
install -m 0440 -o root -g root "$TMP_SUDO" /etc/sudoers.d/proxysql-webui
rm -f "$TMP_SUDO"

# ---------------------------------------------------------------- CLI wrapper
cat > /usr/local/bin/proxysql-webui-admin <<'EOF'
#!/bin/sh
# Manage ProxySQL Web UI accounts: create-user, set-password, set-role, list-users, delete-user, unlock
[ "$(id -u)" -eq 0 ] || { echo "Run as root" >&2; exit 1; }
cd /opt/proxysql-webui || exit 1
exec runuser -u proxysql-webui -- env PROXYSQL_WEBUI_CONFIG=/etc/proxysql-webui/config.ini \
  /opt/proxysql-webui/venv/bin/python -m app.cli "$@"
EOF
chmod 0755 /usr/local/bin/proxysql-webui-admin

# ---------------------------------------------------------------- systemd
install -m 0644 "$SRC_DIR/deploy/proxysql-webui.service" /etc/systemd/system/proxysql-webui.service
systemctl daemon-reload 2>/dev/null || warn "systemd is not running (container?) - skipped daemon-reload"

# ---------------------------------------------------------------- SELinux
if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" != "Disabled" ]]; then
  log "Restoring SELinux contexts"
  restorecon -R "$APP_DIR" "$CONF_DIR" "$DATA_DIR" /etc/systemd/system/proxysql-webui.service /usr/local/bin/proxysql-webui-admin 2>/dev/null || true
fi

# ---------------------------------------------------------------- firewalld
if [[ $FIREWALL -eq 1 ]] && systemctl is-active --quiet firewalld; then
  log "Opening ${PORT}/tcp in firewalld"
  firewall-cmd --quiet --permanent --add-port="${PORT}/tcp"
  firewall-cmd --quiet --reload
fi

# ---------------------------------------------------------------- initial web admin
INITIAL_PW=""
USERS="$(/usr/local/bin/proxysql-webui-admin list-users 2>/dev/null || true)"
if [[ -z "$USERS" ]]; then
  INITIAL_PW="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(14))')"
  /usr/local/bin/proxysql-webui-admin create-user admin --role admin --password "$INITIAL_PW" >/dev/null
fi

if [[ $START -eq 1 ]]; then
  systemctl enable --quiet proxysql-webui
  systemctl restart proxysql-webui
  sleep 2
  systemctl is-active --quiet proxysql-webui || { journalctl -u proxysql-webui -n 30 --no-pager; die "Service failed to start"; }
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
log "ProxySQL Web UI installed."
echo "    URL:     https://${IP:-<this-host>}:${PORT}/"
if [[ -n "$INITIAL_PW" ]]; then
  echo "    Login:   admin / ${INITIAL_PW}    (change it under 'My Account')"
fi
echo "    Config:  $CONF_DIR/config.ini   (ProxySQL admin credentials, instances)"
echo "    Accounts: proxysql-webui-admin --help"
echo "    Logs:    journalctl -u proxysql-webui -f"
