from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from typing import Sequence


WORKER_MODULES = {
    "demucs",
    "mel_band_roformer.inference",
}
WORKER_SCRIPTS = {"strum_worker.py"}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parent.parent


def module_command(module: str, *arguments: object) -> list[str]:
    values = [str(argument) for argument in arguments]
    if is_frozen():
        return [sys.executable, "--run-module", module, *values]
    return [sys.executable, "-m", module, *values]


def script_command(script_name: str, *arguments: object) -> list[str]:
    if script_name not in WORKER_SCRIPTS:
        raise ValueError(f"Unsupported worker script: {script_name}")
    values = [str(argument) for argument in arguments]
    if is_frozen():
        return [sys.executable, "--run-script", script_name, *values]
    return [sys.executable, str(bundle_root() / "scripts" / script_name), *values]


def dispatch_worker(arguments: Sequence[str] | None = None) -> bool:
    argv = list(arguments if arguments is not None else sys.argv[1:])
    if len(argv) < 2 or argv[0] not in {"--run-module", "--run-script"}:
        return False
    if is_frozen():
        # PyInstaller stores Python modules in an archive. Numba cannot locate
        # those source files when third-party libraries request an on-disk JIT
        # cache, so frozen workers use the pure-Python implementation for the
        # small librosa helpers involved here. Model inference still runs in
        # PyTorch and keeps its native acceleration paths.
        os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
    target = argv[1]
    sys.argv = [target, *argv[2:]]
    if argv[0] == "--run-module":
        if target not in WORKER_MODULES:
            raise SystemExit(f"Unsupported worker module: {target}")
        runpy.run_module(target, run_name="__main__")
    else:
        if target not in WORKER_SCRIPTS:
            raise SystemExit(f"Unsupported worker script: {target}")
        runpy.run_path(str(bundle_root() / "scripts" / target), run_name="__main__")
    return True
