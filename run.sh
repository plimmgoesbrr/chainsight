#!/usr/bin/env bash
cd "$(dirname "$0")"
.venv/bin/python -m chainsight.cli prepare
.venv/bin/python -m chainsight.cli serve
