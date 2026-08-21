from __future__ import annotations

from pathlib import Path

from app.db import JobRepository
from app import runtime
from app.schemas import JobStatus
from scripts.desktop import (
    DesktopApi,
    ensure_local_service,
    prepare_frozen_multiprocessing,
)


def test_service_check_reuses_running_server(monkeypatch) -> None:
    monkeypatch.setattr("scripts.desktop.service_is_ready", lambda: True)
    assert ensure_local_service() is None


def test_frozen_desktop_prepares_multiprocessing_helpers(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("scripts.desktop.is_frozen", lambda: True)
    monkeypatch.setattr(
        "multiprocessing.freeze_support",
        lambda: calls.append("freeze-support"),
    )

    prepare_frozen_multiprocessing()

    assert calls == ["freeze-support"]


def test_source_desktop_skips_frozen_multiprocessing(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("scripts.desktop.is_frozen", lambda: False)
    monkeypatch.setattr(
        "multiprocessing.freeze_support",
        lambda: calls.append("freeze-support"),
    )

    prepare_frozen_multiprocessing()

    assert calls == []


def test_frozen_worker_disables_numba_jit(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.delenv("NUMBA_DISABLE_JIT", raising=False)
    monkeypatch.setattr(
        runtime.runpy,
        "run_module",
        lambda target, run_name: calls.append((target, run_name)),
    )

    assert runtime.dispatch_worker(["--run-module", "demucs", "--help"])

    assert calls == [("demucs", "__main__")]
    assert runtime.os.environ["NUMBA_DISABLE_JIT"] == "1"


class FakeWindow:
    def __init__(self, destination: Path | None):
        self.destination = destination

    def create_file_dialog(self, *args, **kwargs):
        if self.destination is None:
            return None
        return (str(self.destination),)


def completed_job(settings, source: Path) -> str:
    repository = JobRepository(settings.database_path)
    repository.initialize()
    job_id = "desktop-download"
    record = repository.create(
        job_id=job_id,
        original_name="song.mp3",
        original_path=str(source),
        mime_type="audio/mpeg",
        input_size_bytes=source.stat().st_size,
        duration_seconds=1,
        sample_rate=44_100,
        channels=2,
        sha256="abc",
        cache_key="desktop",
        model_name="htdemucs_ft",
    )
    repository.update(
        record.id,
        status=JobStatus.COMPLETED.value,
        progress=100,
        drums_path=str(source),
    )
    return job_id


def test_native_download_copies_completed_result(settings, tmp_path: Path) -> None:
    source = settings.jobs_dir / "desktop-download" / "outputs" / "drums.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native-download")
    job_id = completed_job(settings, source)
    destination = tmp_path / "saved-drums.mp3"
    api = DesktopApi(settings)
    api.bind_window(FakeWindow(destination))

    result = api.save_job_file(job_id, "drums")

    assert result == {"saved": True, "filename": destination.name}
    assert destination.read_bytes() == b"native-download"


def test_native_download_can_be_cancelled(settings, tmp_path: Path) -> None:
    source = settings.jobs_dir / "desktop-download" / "outputs" / "drums.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"native-download")
    job_id = completed_job(settings, source)
    api = DesktopApi(settings)
    api.bind_window(FakeWindow(None))

    assert api.save_job_file(job_id, "drums") == {
        "saved": False,
        "cancelled": True,
    }
