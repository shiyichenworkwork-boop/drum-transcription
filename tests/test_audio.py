from __future__ import annotations

from pathlib import Path

import pytest

from app.audio import remove_job_directory, sanitize_display_name, validate_wav
from tests.conftest import wav_bytes


def test_sanitize_display_name_removes_paths() -> None:
    assert sanitize_display_name("../../music/song.wav") == "song.wav"
    assert sanitize_display_name(r"C:\music\song.wav") == "song.wav"


def test_validate_wav_checks_format_and_frames(tmp_path: Path) -> None:
    path = tmp_path / "valid.wav"
    path.write_bytes(wav_bytes(0.1))
    result = validate_wav(
        path,
        expected_sample_rate=44_100,
        expected_channels=2,
        expected_sample_width=2,
        expected_frames=4_410,
    )
    assert result.frames == 4_410
    assert result.peak == 1200
    assert result.warnings == []


def test_remove_job_directory_rejects_outside_path(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError):
        remove_job_directory(outside, jobs)


def test_remove_job_directory_removes_only_selected_job(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    target = jobs / "job-1"
    target.mkdir(parents=True)
    (target / "file.txt").write_text("ok")
    remove_job_directory(target, jobs)
    assert not target.exists()
    assert jobs.exists()

