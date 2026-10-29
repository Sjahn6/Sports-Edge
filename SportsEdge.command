#!/bin/bash
# Double click to update everything and open the dashboard.
# The first run sets things up (about a minute) and pulls two seasons of box scores (a few minutes).
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Run: xcode-select --install   (or install from python.org), then double click this again."
  read -r -p "Press Return to close."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First run: setting up..."
  python3 -m venv .venv && ./.venv/bin/pip install -q --upgrade pip && ./.venv/bin/pip install -q -r requirements.txt || {
    echo "Setup failed."; read -r -p "Press Return to close."; exit 1; }
fi

if [ ! -f config.ini ]; then
  cp config.example.ini config.ini
  echo "Created config.ini with the free ESPN setup. Nothing to fill in."
fi

./.venv/bin/python run.py "$@"
echo
read -r -p "Done. Press Return to close."
