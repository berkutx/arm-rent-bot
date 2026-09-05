#!/usr/bin/env sh
set -eu
umask 077
exec python server.py
