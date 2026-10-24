#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/mercado"
exec python3 app.py
