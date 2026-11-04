#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating a Python environment (first run only)..."
  python3 -m venv .venv && source .venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt
else
  source .venv/bin/activate
fi
python run_local.py "$@"
