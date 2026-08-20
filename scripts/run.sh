#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
UV_BIN="$RUNTIME_DIR/bin/uv"

mkdir -p "$RUNTIME_DIR/bin" "$RUNTIME_DIR/cache" "$RUNTIME_DIR/python"

if [[ ! -x "$UV_BIN" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  else
    echo "首次启动：正在下载项目级 uv 运行工具…"
    INSTALLER="$RUNTIME_DIR/uv-installer.sh"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$INSTALLER"; then
      echo "uv 下载失败，请检查网络后重新运行。" >&2
      exit 1
    fi
    UV_INSTALL_DIR="$RUNTIME_DIR/bin" sh "$INSTALLER"
  fi
fi

export UV_CACHE_DIR="$RUNTIME_DIR/cache"
export UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python"

DEPENDENCY_MARKER="$RUNTIME_DIR/dependencies.sha256"
DEPENDENCY_FINGERPRINT="$({ shasum -a 256 "$PROJECT_DIR/pyproject.toml" "$PROJECT_DIR/uv.lock"; } | shasum -a 256 | awk '{print $1}')"
INSTALLED_FINGERPRINT=""
if [[ -f "$DEPENDENCY_MARKER" ]]; then
  IFS= read -r INSTALLED_FINGERPRINT < "$DEPENDENCY_MARKER"
fi

if [[ -z "$INSTALLED_FINGERPRINT" && -x "$PROJECT_DIR/.venv/bin/python" && -x "$PROJECT_DIR/.venv/bin/uvicorn" ]]; then
  if "$UV_BIN" sync --check --offline --python "$PROJECT_DIR/.venv/bin/python" >/dev/null 2>&1; then
    print -r -- "$DEPENDENCY_FINGERPRINT" > "$DEPENDENCY_MARKER"
    INSTALLED_FINGERPRINT="$DEPENDENCY_FINGERPRINT"
  fi
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" || ! -x "$PROJECT_DIR/.venv/bin/uvicorn" || "$INSTALLED_FINGERPRINT" != "$DEPENDENCY_FINGERPRINT" ]]; then
  echo "正在检查 Python 3.11 与项目依赖…"
  "$UV_BIN" python install 3.11
  "$UV_BIN" sync --python 3.11
  print -r -- "$DEPENDENCY_FINGERPRINT" > "$DEPENDENCY_MARKER"
else
  echo "项目依赖没有变化，跳过安装检查。"
fi

if [[ "${1:-}" == "--setup-only" ]]; then
  echo "项目运行环境安装完成。"
  exit 0
fi

echo ""
echo "鼓点拆解室已启动：http://127.0.0.1:8765"
echo "按 Control-C 可以停止服务。"
echo ""
cd "$PROJECT_DIR"

if [[ "${DRUM_SEPARATOR_NO_OPEN:-0}" != "1" ]]; then
  (
    for _ in {1..120}; do
      if curl -fsS "http://127.0.0.1:8765/api/health" >/dev/null 2>&1; then
        open "http://127.0.0.1:8765"
        exit 0
      fi
      sleep 0.25
    done
  ) >/dev/null 2>&1 &
fi

exec "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8765 --no-access-log
