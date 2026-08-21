#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
"$PROJECT_DIR/scripts/run.sh" --setup-only
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/desktop.py"
