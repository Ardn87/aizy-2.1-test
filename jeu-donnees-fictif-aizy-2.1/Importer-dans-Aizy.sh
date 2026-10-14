#!/usr/bin/env bash
set -euo pipefail
TASK_PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASK_APP_DIR="${1:-$(cd "$TASK_PACKAGE_DIR/.." && pwd)}"
cd "$TASK_APP_DIR"
if [ -f .local/env ]; then
  set -a
  source .local/env
  set +a
fi
if [ -x .venv/bin/python ]; then
  TASK_PYTHON=.venv/bin/python
else
  TASK_PYTHON=python
fi
"$TASK_PYTHON" "$TASK_PACKAGE_DIR/importer.py" --app "$TASK_APP_DIR"
