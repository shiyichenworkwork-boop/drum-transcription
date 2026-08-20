from __future__ import annotations

import io
import time
import asyncio

import mido
from fastapi.testclient import TestClient

from app.main import _audio_media_type, _parse_range, create_app
from app.engine import SeparationCancelled
from tests.conftest import BlockingFakeEngine, FakeEngine, FakeVocalEngine, wav_bytes


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


def wait_for_operation(
    client: TestClient,
    job_id: str,
    expected: set[str] = {"idle"},
    timeout: float = 5,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["operation_state"] in expected:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"操作未进入预期状态：{expected}")


def test_upload_complete_stream_cache_and_delete(settings, fake_transcriber) -> None:
    app = create_app(
        settings,
        FakeEngine(),
        transcriber=fake_transcriber,
        strum_transcriber=fake_transcriber,
    )
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
        assert compressed.json()["operation_state"] in {"queued", "running", "idle"}
        compressed_payload = wait_for_operation(client, job_id)
        assert compressed_payload["output_format"] == "mp3"
        mp3_download = client.get(
            compressed_payload["files"]["drums"] + "?download=true"
        )
        assert mp3_download.status_code == 200
        assert mp3_download.headers["content-type"].startswith("audio/mpeg")
        assert ".mp3" in mp3_download.headers["content-disposition"]

        midi = client.post(f"/api/jobs/{job_id}/midi")
        assert midi.status_code == 200
        midi_payload = wait_for_operation(client, job_id)
        assert midi_payload["files"]["midi"].endswith("/midi")
        assert midi_payload["midi_quantized"] is True
        assert "ADTOF" in midi_payload["midi_engine"]
        midi_download = client.get(midi_payload["files"]["midi"] + "?download=true")
        assert midi_download.status_code == 200
        assert midi_download.headers["content-type"].startswith("audio/midi")
        assert midi_download.content.startswith(b"MThd")
        assert ".mid" in midi_download.headers["content-disposition"]

        preview = client.get(f"/api/jobs/{job_id}/midi/preview?bars=4")
        assert preview.status_code == 200
        assert preview.json()["bar_count"] == 4
        assert preview.json()["beat_unit"] == 4
        assert preview.json()["min_start_bar"] == 1
        assert preview.json()["max_start_bar"] >= preview.json()["min_start_bar"]
        assert preview.json()["notes"]
        assert {lane["id"] for lane in preview.json()["lanes"]} >= {
            "kick",
            "snare",
            "hihat",
        }
        clamped_preview = client.get(
            f"/api/jobs/{job_id}/midi/preview?bars=4&start_bar=999"
        )
        assert clamped_preview.status_code == 200
        assert (
            clamped_preview.json()["start_bar"]
            == clamped_preview.json()["max_start_bar"]
        )

        regenerated = client.post(
            f"/api/jobs/{job_id}/midi?force=true&bar_offset_beats=1"
        )
        assert regenerated.status_code == 200
        regenerated_payload = wait_for_operation(client, job_id)
        assert regenerated_payload["midi_event_count"] == 3
        assert regenerated_payload["midi_beats_per_bar"] == 4
        assert regenerated_payload["midi_bar_offset_beats"] == 1
        assert fake_transcriber.drum_model.call_count == 1
        shifted_download = client.get(
            regenerated_payload["files"]["midi"] + "?download=true"
        )
        midi_file = mido.MidiFile(file=io.BytesIO(shifted_download.content))
        signatures = []
        absolute_tick = 0
        for message in midi_file.tracks[0]:
            absolute_tick += message.time
            if message.type == "time_signature":
                signatures.append((absolute_tick, message.numerator))
        assert signatures == [(0, 1), (480, 4)]

        compound = client.post(
            f"/api/jobs/{job_id}/midi?force=true&meter=6%2F8"
        )
        assert compound.status_code == 200
        compound_payload = wait_for_operation(client, job_id)
        assert compound_payload["midi_beats_per_bar"] == 6
        assert compound_payload["midi_beat_unit"] == 8
        assert compound_payload["midi_meter"] == "6/8"
        compound_preview = client.get(f"/api/jobs/{job_id}/midi/preview?bars=4")
        assert compound_preview.status_code == 200
        assert compound_preview.json()["beats_per_bar"] == 6
        assert compound_preview.json()["beat_unit"] == 8

        strum = client.post(
            f"/api/jobs/{job_id}/midi?force=true&midi_model=strum&bar_offset_beats=-1"
        )
        assert strum.status_code == 200
        strum_payload = wait_for_operation(client, job_id)
        assert strum_payload["midi_model"] == "strum"
        assert strum_payload["midi_bar_offset_beats"] == -1

        invalid_offset = client.post(
            f"/api/jobs/{job_id}/midi?force=true&bar_offset_beats=3"
        )
        assert invalid_offset.status_code == 422
        invalid_model = client.post(
            f"/api/jobs/{job_id}/midi?force=true&midi_model=unknown"
        )
        assert invalid_model.status_code == 422
        invalid_meter = client.post(
            f"/api/jobs/{job_id}/midi?force=true&meter=5%2F8"
        )
        assert invalid_meter.status_code == 422

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


def test_vocal_module_is_independent_from_drum_module(settings, fake_transcriber) -> None:
    app = create_app(
        settings,
        FakeEngine(),
        vocal_engine=FakeVocalEngine(),
        transcriber=fake_transcriber,
    )
    audio = wav_bytes()
    with TestClient(app) as client:
        drum_response = client.post(
            "/api/jobs",
            files={"file": ("song.wav", audio, "audio/wav")},
            data={"separation_kind": "drums"},
        )
        vocal_response = client.post(
            "/api/jobs",
            files={"file": ("song.wav", audio, "audio/wav")},
            data={"separation_kind": "vocals"},
        )
        assert drum_response.status_code == 201
        assert vocal_response.status_code == 201
        assert drum_response.json()["id"] != vocal_response.json()["id"]

        drum_job = wait_for_status(client, drum_response.json()["id"], {"completed"})
        vocal_job = wait_for_status(client, vocal_response.json()["id"], {"completed"})
        assert drum_job["separation_kind"] == "drums"
        assert drum_job["files"]["drums"]
        assert drum_job["files"]["vocals"] is None
        assert vocal_job["separation_kind"] == "vocals"
        assert vocal_job["model_name"] == settings.vocal_model_name
        assert vocal_job["files"]["vocals"].endswith("/vocals")
        assert vocal_job["files"]["instrumental"].endswith("/instrumental")
        assert vocal_job["files"]["drums"] is None

        vocals_range = client.get(
            vocal_job["files"]["vocals"],
            headers={"Range": "bytes=0-9"},
        )
        assert vocals_range.status_code == 206
        rejected_midi = client.post(f"/api/jobs/{vocal_job['id']}/midi")
        assert rejected_midi.status_code == 409
        rejected_preview = client.get(f"/api/jobs/{vocal_job['id']}/midi/preview")
        assert rejected_preview.status_code == 409


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


def test_heavy_operations_share_one_queue_and_block_delete(
    settings, fake_transcriber
) -> None:
    class SecondJobBlockingEngine(FakeEngine):
        def __init__(self):
            self.calls = 0
            self.release = asyncio.Event()
            self.cancelled: set[str] = set()

        async def separate(self, *, job_id, source_path, job_dir, progress):
            self.calls += 1
            if self.calls == 1:
                return await super().separate(
                    job_id=job_id,
                    source_path=source_path,
                    job_dir=job_dir,
                    progress=progress,
                )
            await progress("separating", 25, "占用统一计算队列")
            await self.release.wait()
            if job_id in self.cancelled:
                raise SeparationCancelled("任务已取消。")
            raise AssertionError("第二个任务应通过取消退出")

        async def cancel(self, job_id: str) -> bool:
            self.cancelled.add(job_id)
            self.release.set()
            return True

    engine = SecondJobBlockingEngine()
    app = create_app(
        settings,
        engine,
        transcriber=fake_transcriber,
        strum_transcriber=fake_transcriber,
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/jobs",
            files={"file": ("first.wav", wav_bytes(0.25), "audio/wav")},
        ).json()
        wait_for_status(client, first["id"], {"completed"})
        second = client.post(
            "/api/jobs",
            files={"file": ("second.wav", wav_bytes(0.3), "audio/wav")},
        ).json()
        wait_for_status(client, second["id"], {"separating"})

        queued_midi = client.post(f"/api/jobs/{first['id']}/midi")
        assert queued_midi.status_code == 200
        assert queued_midi.json()["operation_state"] == "queued"
        time.sleep(0.05)
        assert fake_transcriber.drum_model.call_count == 0
        assert client.delete(f"/api/jobs/{first['id']}").status_code == 409

        assert client.post(f"/api/jobs/{second['id']}/cancel").status_code == 200
        wait_for_status(client, second["id"], {"cancelled"})
        completed_midi = wait_for_operation(client, first["id"])
        assert completed_midi["files"]["midi"]
        assert fake_transcriber.drum_model.call_count == 1


def test_range_parser() -> None:
    assert _parse_range("bytes=10-19", 100) == (10, 19)
    assert _parse_range("bytes=90-", 100) == (90, 99)
    assert _parse_range("bytes=-10", 100) == (90, 99)


def test_output_media_types_support_new_and_legacy_results() -> None:
    assert _audio_media_type(".mp3") == "audio/mpeg"
    assert _audio_media_type(".wav") == "audio/wav"


def test_static_cache_headers(settings) -> None:
    app = create_app(settings, FakeEngine())
    with TestClient(app) as client:
        index = client.get("/")
        javascript = client.get("/app.js?v=test")
    assert "no-cache" in index.headers["cache-control"]
    assert "immutable" in javascript.headers["cache-control"]
