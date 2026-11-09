#!/usr/bin/env bash
# HyperCopy launcher — installs deps if needed, then starts the dashboard + bots.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ -f ".env" ]; then
  echo "==> Loading .env"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Starting HyperCopy on http://localhost:${PORT:-8000}"
exec python3 -u server.py
