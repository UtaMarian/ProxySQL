#!/usr/bin/env bash
# Remove ProxySQL Web UI. Pass --purge to also delete configuration, data and the system user.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
PURGE=0; [[ "${1:-}" == "--purge" ]] && PURGE=1

PORT="$(sed -n 's/^bind = ".*:\([0-9]*\)".*/\1/p' /etc/proxysql-webui/gunicorn.conf.py 2>/dev/null | head -1 || true)"
systemctl disable --now proxysql-webui 2>/dev/null || true
rm -f /etc/systemd/system/proxysql-webui.service /etc/sudoers.d/proxysql-webui /usr/local/bin/proxysql-webui-admin
systemctl daemon-reload
rm -rf /opt/proxysql-webui
if [[ -n "$PORT" ]] && systemctl is-active --quiet firewalld; then
  firewall-cmd --quiet --permanent --remove-port="${PORT}/tcp" || true
  firewall-cmd --quiet --reload || true
fi
if [[ $PURGE -eq 1 ]]; then
  rm -rf /etc/proxysql-webui /var/lib/proxysql-webui
  userdel proxysql-webui 2>/dev/null || true
  echo "ProxySQL Web UI removed (including configuration and data)."
else
  echo "ProxySQL Web UI removed. Kept /etc/proxysql-webui and /var/lib/proxysql-webui (use --purge to delete)."
fi
