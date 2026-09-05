#!/usr/bin/env sh
# Install as an executable deploy hook in /etc/letsencrypt/renewal-hooks/deploy/.
set -eu
nginx -t
systemctl reload nginx
