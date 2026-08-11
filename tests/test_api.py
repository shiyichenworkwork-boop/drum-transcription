from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import _audio_media_type, _parse_range, create_app
from tests.conftest import BlockingFakeEngine, FakeEngine, wav_bytes


def wait_for_status(client: TestClient, job_id: str, expected: set[str], timeout: float = 3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in expected:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"任务未进入预期状态：{expected}")


def test_upload_complete_stream_cache_and_delete(settings) -> None:
    app = create_app(settings, FakeEngine())
    audio = wav_bytes()
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("song.wav", audio, "audio/wav")},
        )
        assert response.status_code == 201
        job_id = response.json()["id"]
        completed = wait_for_status(client, job_id, {"completed"})
        assert completed["files"]["drums"].endswith("/drums")
        assert completed["files"]["no_drums"].endswith("/no_drums")

        partial = client.get(
            completed["files"]["drums"],
            headers={"Range": "bytes=0-9"},
        )
        assert partial.status_code == 206
        assert partial.headers["content-range"].startswith("bytes 0-9/")
        assert len(partial.content) == 10

        cached = client.post(
            "/api/jobs",
            files={"file": ("copy.wav", audio, "audio/wav")},
        )
        assert cached.status_code == 201
        assert cached.json()["id"] == job_id

        compressed = client.post(f"/api/jobs/{job_id}/compress")
        assert compressed.status_code == 200
        assert compressed.json()["output_format"] == "mp3"
        mp3_download = client.get(
            compressed.json()["files"]["drums"] + "?download=true"
        )
        assert mp3_download.status_code == 200
        assert mp3_download.headers["content-type"].startswith("audio/mpeg")
        assert ".mp3" in mp3_download.headers["content-disposition"]

        midi = client.post(f"/api/jobs/{job_id}/midi")
        assert midi.status_code == 200
        assert midi.json()["files"]["midi"].endswith("/midi")
        midi_download = client.get(midi.json()["files"]["midi"] + "?download=true")
        assert midi_download.status_code == 200
        assert midi_download.headers["content-type"].startswith("audio/midi")
        assert midi_download.content.startswith(b"MThd")
        assert ".mid" in midi_download.headers["content-disposition"]

        listing = client.get("/api/jobs").json()
        assert len(listing["jobs"]) == 1
        assert listing["total_storage_bytes"] > len(audio)

        deleted = client.delete(f"/api/jobs/{job_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_rejects_wrong_extension(settings) -> None:
    app = create_app(settings, FakeEngine())
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("song.txt", wav_bytes(), "text/plain")},
        )
    assert response.status_code == 400
    assert "仅支持" in response.json()["detail"]


def test_cancel_running_job(settings) -> None:
    engine = BlockingFakeEngine()
    app = create_app(settings, engine)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("song.wav", wav_bytes(), "audio/wav")},
        )
        job_id = response.json()["id"]
        wait_for_status(client, job_id, {"separating"})
        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        final = wait_for_status(client, job_id, {"cancelled"})
        assert final["status"] == "cancelled"


def test_range_parser() -> None:
    assert _parse_range("bytes=10-19", 100) == (10, 19)
    assert _parse_range("bytes=90-", 100) == (90, 99)
    assert _parse_range("bytes=-10", 100) == (90, 99)


def test_output_media_types_support_new_and_legacy_results() -> None:
    assert _audio_media_type(".mp3") == "audio/mpeg"
    assert _audio_media_type(".wav") == "audio/wav"
