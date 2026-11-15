#!/usr/bin/env bash
# Byditors Cut — start on macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting things up, this takes a few minutes..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m bcut "$@"
