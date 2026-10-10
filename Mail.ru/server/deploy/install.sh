#!/usr/bin/env bash
set -euo pipefail
# Run as root on an Ubuntu/Debian VPS after copying the project to /tmp/mailru-mcp.
# This script does NOT touch DNS, SSH, WireGuard, nginx, Caddy, or existing @Yandex service.
if [ "$(id -u)" -ne 0 ]; then echo 'Run as root'; exit 1; fi
if [ ! -d /tmp/mailru-mcp/mailru_mcp ]; then echo 'Copy project to /tmp/mailru-mcp first'; exit 1; fi
if [ -e /opt/mailru-mcp/mailru_mcp/server.py ]; then echo 'Existing installation detected. Back it up and follow the update procedure; installer refuses to overwrite.'; exit 1; fi
if ss -ltnH 'sport = :8765' | grep -q .; then echo 'Port 8765 already in use'; exit 1; fi
id -u mailrumcp >/dev/null 2>&1 || useradd --system --home-dir /var/lib/mailru-mcp --shell /usr/sbin/nologin mailrumcp
install -d -o root -g root -m 0755 /opt/mailru-mcp
cp -r /tmp/mailru-mcp/mailru_mcp /opt/mailru-mcp/
chown -R root:root /opt/mailru-mcp
find /opt/mailru-mcp -type d -exec chmod 755 {} +
find /opt/mailru-mcp -type f -exec chmod 644 {} +
install -d -o mailrumcp -g mailrumcp -m 0700 /var/lib/mailru-mcp
if [ ! -e /etc/mailru-mcp.env ]; then
  install -o root -g root -m 0600 /dev/null /etc/mailru-mcp.env
  echo 'Created /etc/mailru-mcp.env: populate it before starting service.'
fi
install -o root -g root -m 0644 /tmp/mailru-mcp/deploy/mailru-mcp.service /etc/systemd/system/mailru-mcp.service
systemctl daemon-reload
# Do not enable or start until credentials and the HTTPS proxy are configured.
printf '\nInstalled but disabled. Set /etc/mailru-mcp.env, configure HTTPS, then: systemctl enable --now mailru-mcp\n'
