#!/usr/bin/env bash
# Byditors Cut — start on macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  if command -v brew >/dev/null; then echo "Installing Python..."; brew install python@3.12
  else echo "Install Python 3.10+ first: https://www.python.org/downloads/"; exit 1; fi
fi
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  if command -v brew >/dev/null; then echo "Installing ffmpeg..."; brew install ffmpeg
  elif command -v apt-get >/dev/null; then echo "Installing ffmpeg..."; sudo apt-get install -y ffmpeg
  else echo "Install ffmpeg first: https://ffmpeg.org/download.html"; exit 1; fi
fi
if ! cmp -s requirements.txt .venv/installed.ok; then   # first run, or an update changed the packages
  echo "Setting up Byditors Cut (one time, a few minutes)..."
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip --quiet
  .venv/bin/python -m pip install -r requirements.txt
  cp requirements.txt .venv/installed.ok
fi
[ -f .env ] || cp .env.example .env
exec .venv/bin/python -m bcut "$@"
