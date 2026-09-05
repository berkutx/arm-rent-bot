#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
export LIVE=0 PYTHONUTF8=1
python scripts/seed_demo.py
python server.py
