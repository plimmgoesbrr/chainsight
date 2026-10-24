#!/usr/bin/env bash
# ChainSight setup for macOS / Linux (one time, needs internet)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m chainsight.cli prepare
echo "Setup complete. Start with ./run.sh"
