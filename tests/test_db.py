from __future__ import annotations

from pathlib import Path

from app.db import JobRepository
from app.schemas import JobStatus


def create_record(repository: JobRepository, job_id: str, cache_key: str = "cache"):
    return repository.create(
        job_id=job_id,
        original_name="song.wav",
        original_path="/tmp/song.wav",
        mime_type="audio/wav",
        input_size_bytes=100,
        duration_seconds=1.0,
        sample_rate=44_100,
        channels=2,
        sha256="abc",
        cache_key=cache_key,
        model_name="htdemucs_ft",
    )


def test_repository_crud_and_storage(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    create_record(repository, "job-1")
    repository.update("job-1", storage_bytes=300, progress=20)
    assert repository.require("job-1").progress == 20
    assert repository.total_storage_bytes() == 300
    repository.delete("job-1")
    assert repository.get("job-1") is None


def test_startup_recovery_marks_running_failed_and_returns_queue(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    create_record(repository, "queued", "one")
    create_record(repository, "running", "two")
    repository.update(
        "running",
        status=JobStatus.SEPARATING.value,
        stage="正在分离",
        progress=55,
    )

    queued = repository.prepare_startup_recovery()
    assert queued == [("queued", "separation")]
    interrupted = repository.require("running")
    assert interrupted.status == JobStatus.FAILED.value
    assert "重新处理" in (interrupted.error or "")


def test_startup_recovery_restores_queued_operation_and_fails_running_operation(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    for job_id in ("midi-queued", "compress-running"):
        create_record(repository, job_id, job_id)
        repository.update(
            job_id,
            status=JobStatus.COMPLETED.value,
            progress=100,
            operation_kind="midi" if job_id == "midi-queued" else "compress",
            operation_state="queued" if job_id == "midi-queued" else "running",
        )

    queued = repository.prepare_startup_recovery()

    assert queued == [("midi-queued", "midi")]
    interrupted = repository.require("compress-running")
    assert interrupted.operation_state == "failed"
    assert "退出" in (interrupted.operation_error or "")


def test_cache_finds_active_or_completed_job(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    create_record(repository, "job-1", "same")
    assert repository.find_reusable("same").id == "job-1"
