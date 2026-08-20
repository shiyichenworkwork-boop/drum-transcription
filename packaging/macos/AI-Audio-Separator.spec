# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import os
import platform
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata


PROJECT_ROOT = Path(SPECPATH).parents[1]
TARGET_ARCH = os.environ.get("MACOS_TARGET_ARCH", platform.machine())
if TARGET_ARCH not in {"arm64", "x86_64"}:
    raise SystemExit(f"Unsupported macOS architecture: {TARGET_ARCH}")

datas = [
    (str(PROJECT_ROOT / "app" / "static"), "app/static"),
    (str(PROJECT_ROOT / "scripts" / "strum_worker.py"), "scripts"),
]
binaries = []
hiddenimports = [
    "app.main",
    "demucs.__main__",
    "mel_band_roformer.inference",
    "beat_this.inference",
    "adtof_pytorch",
    "pretty_midi",
    "torch",
    "torchaudio",
    "librosa",
    "soundfile",
    "scipy.signal",
    "sklearn",
    "omegaconf",
    "mido",
    "webview.platforms.cocoa",
]

for package in (
    "webview",
    "imageio_ffmpeg",
    "demucs",
    "beat_this",
    "adtof_pytorch",
    "mel_band_roformer",
):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("uvicorn")
for distribution in (
    "local-drum-separator",
    "demucs",
    "beat-this",
    "adtof-pytorch",
    "melband-roformer-infer",
    "pywebview",
):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "pytest",
        "pytest_asyncio",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI Audio Separator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=TARGET_ARCH,
    codesign_identity=os.environ.get("MACOS_CODESIGN_IDENTITY") or None,
    entitlements_file=str(PROJECT_ROOT / "packaging" / "macos" / "entitlements.plist"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AI Audio Separator",
)
app = BUNDLE(
    coll,
    name="AI 音轨分离.app",
    icon=str(PROJECT_ROOT / "packaging" / "macos" / "AppIcon.png"),
    bundle_identifier="com.shiyichen.aiaudioseparator",
    version="0.2.0",
    info_plist={
        "CFBundleDisplayName": "AI 音轨分离",
        "CFBundleName": "AI 音轨分离",
        "LSApplicationCategoryType": "public.app-category.music",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
