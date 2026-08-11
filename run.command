#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h}"
exec "$PROJECT_DIR/scripts/run.sh"

