from __future__ import annotations

import subprocess
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import Settings
from app.db import JobRepository
from app.job_files import JobFileNotFound, resolve_job_file
from app.runtime import dispatch_worker, is_frozen


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_DIR / ".runtime"
SERVICE_URL = "http://127.0.0.1:8765"
HEALTH_URL = f"{SERVICE_URL}/api/health"


def available_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class EmbeddedLocalService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.server: Any | None = None
        self.thread: threading.Thread | None = None

    @property
    def service_url(self) -> str:
        return f"http://{self.settings.host}:{self.settings.port}"

    def start(self, timeout: float = 30.0) -> None:
        import uvicorn

        from app.main import create_app

        config = uvicorn.Config(
            create_app(self.settings),
            host=self.settings.host,
            port=self.settings.port,
            access_log=False,
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run,
            name="audio-separator-local-service",
            daemon=True,
        )
        self.thread.start()
        health_url = f"{self.service_url}/api/health"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if service_is_ready(url=health_url):
                return
            if not self.thread.is_alive():
                raise RuntimeError("本地服务启动失败。")
            time.sleep(0.1)
        raise RuntimeError("本地服务启动超时。")

    def stop(self, timeout: float = 10.0) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=timeout)


class DesktopApi:
    """Native operations that WebKit cannot perform reliably on its own."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.window: Any | None = None

    def bind_window(self, window: Any) -> None:
        self.window = window

    def save_job_file(self, job_id: str, file_kind: str) -> dict[str, object]:
        if self.window is None:
            raise RuntimeError("桌面窗口尚未准备好。")

        repository = JobRepository(self.settings.database_path)
        try:
            record = repository.require(job_id)
            source, filename, _ = resolve_job_file(
                self.settings,
                record,
                file_kind,
            )
        except (KeyError, JobFileNotFound) as exc:
            raise RuntimeError("要保存的文件不存在。") from exc

        import webview

        downloads_dir = Path.home() / "Downloads"
        initial_dir = downloads_dir if downloads_dir.is_dir() else Path.home()
        selected = self.window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(initial_dir),
            save_filename=filename,
        )
        if not selected:
            return {"saved": False, "cancelled": True}

        destination = Path(selected[0] if not isinstance(selected, str) else selected)
        destination = destination.expanduser().resolve()
        if destination == source:
            return {"saved": True, "filename": destination.name}
        if not destination.parent.is_dir():
            raise RuntimeError("保存位置不存在。")

        temporary = destination.with_name(
            f".{destination.name}.part-{uuid4().hex}"
        )
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        except OSError as exc:
            raise RuntimeError(f"无法保存文件：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return {"saved": True, "filename": destination.name}


def service_is_ready(timeout: float = 0.8, *, url: str = HEALTH_URL) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def ensure_local_service(timeout: float = 30.0) -> subprocess.Popen[bytes] | None:
    if service_is_ready():
        return None

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNTIME_DIR / "server.log"
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--no-access-log",
            ],
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_is_ready():
            return process
        if process.poll() is not None:
            raise RuntimeError(f"本地服务启动失败，请查看日志：{log_path}")
        time.sleep(0.2)
    raise RuntimeError(f"本地服务启动超时，请查看日志：{log_path}")


def prepare_frozen_multiprocessing() -> None:
    """Route PyInstaller multiprocessing helpers before desktop startup."""
    if not is_frozen():
        return
    import multiprocessing

    multiprocessing.freeze_support()


def main() -> None:
    try:
        import webview
    except ImportError as exc:  # pragma: no cover - desktop dependency
        raise RuntimeError("桌面窗口组件尚未安装，请重新运行 run.command。") from exc

    settings = Settings.from_env()
    embedded_service: EmbeddedLocalService | None = None
    if is_frozen():
        settings.port = available_local_port()
        embedded_service = EmbeddedLocalService(settings)
        embedded_service.start()
        service_url = embedded_service.service_url
        runtime_dir = settings.data_dir / "runtime"
    else:
        ensure_local_service()
        service_url = SERVICE_URL
        runtime_dir = RUNTIME_DIR

    desktop_api = DesktopApi(settings)
    window = webview.create_window(
        "AI 音轨分离",
        service_url,
        js_api=desktop_api,
        width=1080,
        height=780,
        min_size=(760, 560),
        resizable=True,
        frameless=False,
        shadow=True,
        background_color="#151412",
        text_select=True,
    )
    desktop_api.bind_window(window)
    start_options: dict[str, object] = {
        "private_mode": False,
        "storage_path": str(runtime_dir / "webview"),
    }
    if sys.platform == "darwin":
        start_options["gui"] = "cocoa"
    try:
        webview.start(**start_options)
    finally:
        if embedded_service is not None:
            embedded_service.stop()


if __name__ == "__main__":
    prepare_frozen_multiprocessing()
    if not dispatch_worker():
        main()
