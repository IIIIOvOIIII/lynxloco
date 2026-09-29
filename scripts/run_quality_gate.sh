#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec "${PYTHON_BIN:-backend/.venv/bin/python}" scripts/quality_gate.py "$@"
