#!/bin/zsh
set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
TARGET_ARCH="${MACOS_TARGET_ARCH:-$(uname -m)}"
UV_BIN="${UV_BIN:-$PROJECT_DIR/.runtime/bin/uv}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PROJECT_DIR/.runtime/cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$PROJECT_DIR/.runtime/python}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$PROJECT_DIR/.runtime/pyinstaller}"

if [[ "$TARGET_ARCH" != "arm64" && "$TARGET_ARCH" != "x86_64" ]]; then
  echo "不支持的 macOS 架构：$TARGET_ARCH" >&2
  exit 1
fi
if [[ ! -x "$UV_BIN" ]]; then
  UV_BIN="$(command -v uv || true)"
fi
if [[ -z "$UV_BIN" ]]; then
  echo "未找到 uv，请先运行 run.command 安装项目环境。" >&2
  exit 1
fi

cd "$PROJECT_DIR"
"$UV_BIN" sync --frozen --group packaging --python 3.11

ICON_PATH="$PROJECT_DIR/packaging/macos/AppIcon.png"
"$PROJECT_DIR/.venv/bin/python" \
  "$PROJECT_DIR/packaging/macos/create_icon.py" \
  "$ICON_PATH"

export MACOS_TARGET_ARCH="$TARGET_ARCH"
"$PROJECT_DIR/.venv/bin/pyinstaller" \
  --noconfirm \
  --clean \
  "$PROJECT_DIR/packaging/macos/AI-Audio-Separator.spec"

APP_PATH="$PROJECT_DIR/dist/AI 音轨分离.app"
EXECUTABLE="$APP_PATH/Contents/MacOS/AI Audio Separator"
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "应用打包失败：未生成可执行文件。" >&2
  exit 1
fi
if ! lipo -archs "$EXECUTABLE" | tr ' ' '\n' | grep -qx "$TARGET_ARCH"; then
  echo "应用架构校验失败。" >&2
  exit 1
fi
codesign --verify --deep --strict "$APP_PATH"

DMG_PATH="$PROJECT_DIR/dist/AI-Audio-Separator-macOS-${TARGET_ARCH}.dmg"
rm -f "$DMG_PATH"
hdiutil create \
  -volname "AI 音轨分离" \
  -srcfolder "$APP_PATH" \
  -ov \
  -format UDZO \
  "$DMG_PATH"
(
  cd "$(dirname "$DMG_PATH")"
  shasum -a 256 "$(basename "$DMG_PATH")" > "$(basename "$DMG_PATH").sha256"
)

echo "打包完成：$DMG_PATH"
