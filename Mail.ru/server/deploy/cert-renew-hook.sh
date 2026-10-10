#!/usr/bin/env bash
set -euo pipefail
# Future deploy hook: install under /etc/letsencrypt/renewal-hooks/deploy/ only
# if equivalent nginx test/reload hooks are not already present.
nginx -t
systemctl reload nginx
