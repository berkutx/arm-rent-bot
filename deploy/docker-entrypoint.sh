#!/usr/bin/env sh
set -eu
umask 077
if [ "${LIVE:-0}" != "1" ] && [ ! -f /app/data/demo.sqlite3 ]; then
    python scripts/seed_demo.py
fi
exec python server.py
