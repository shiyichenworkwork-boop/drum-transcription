from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path

import imageio_ffmpeg
import pytest

from app.config import Settings
from app.engine import SeparationCancelled, SeparationEngine, SeparationResult
from app.midi import HI_HAT_NOTE, KICK_NOTE, SNARE_NOTE, DrumTranscriber
from app.midi_models import BeatGrid, RawDrumHit


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def wav_bytes(duration_seconds: float = 0.25, sample_rate: int = 44_100) -> bytes:
    buffer = io.BytesIO()
    frames = max(1, round(duration_seconds * sample_rate))
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        pulse = (1200).to_bytes(2, "little", signed=True) + (-1200).to_bytes(2, "little", signed=True)
        audio.writeframes(pulse * frames)
    return buffer.getvalue()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=tmp_path / "data",
        max_upload_bytes=2 * 1024 * 1024,
        max_duration_seconds=2,
        min_free_disk_bytes=1,
        ffmpeg_path=imageio_ffmpeg.get_ffmpeg_exe(),
    )


class FakeEngine(SeparationEngine):
    async def separate(self, *, job_id, source_path, job_dir, progress):
        await progress("preprocessing", 8, "测试预处理")
        await progress("separating", 60, "测试分离")
        output_dir = job_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        drums = output_dir / "drums.wav"
        no_drums = output_dir / "no_drums.wav"
        shutil.copy2(source_path, drums)
        shutil.copy2(source_path, no_drums)
        await progress("postprocessing", 95, "测试输出")
        return SeparationResult(
            drums_path=drums,
            no_drums_path=no_drums,
            storage_bytes=drums.stat().st_size + no_drums.stat().st_size,
            warnings=[],
        )

    async def cancel(self, job_id: str) -> bool:
        return False


class FakeVocalEngine(SeparationEngine):
    async def separate(self, *, job_id, source_path, job_dir, progress):
        await progress("preprocessing", 8, "测试人声预处理")
        await progress("separating", 60, "测试人声分离")
        output_dir = job_dir / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        vocals = output_dir / "vocals.wav"
        instrumental = output_dir / "instrumental.wav"
        shutil.copy2(source_path, vocals)
        shutil.copy2(source_path, instrumental)
        await progress("postprocessing", 95, "测试人声输出")
        return SeparationResult(
            vocals_path=vocals,
            instrumental_path=instrumental,
            storage_bytes=vocals.stat().st_size + instrumental.stat().st_size,
            warnings=[],
        )

    async def cancel(self, job_id: str) -> bool:
        return False


class BlockingFakeEngine(SeparationEngine):
    def __init__(self):
        import asyncio

        self.release = asyncio.Event()
        self.cancelled: set[str] = set()

    async def separate(self, *, job_id, source_path, job_dir, progress):
        await progress("separating", 20, "等待取消")
        await self.release.wait()
        if job_id in self.cancelled:
            raise SeparationCancelled("任务已取消。")
        raise AssertionError("测试任务只能通过取消结束")

    async def cancel(self, job_id: str) -> bool:
        self.cancelled.add(job_id)
        self.release.set()
        return True


class FakeDrumModel:
    name = "ADTOF 测试模型"

    def __init__(self):
        self.call_count = 0

    def transcribe(self, audio_path: Path) -> list[RawDrumHit]:
        self.call_count += 1
        return [
            RawDrumHit(0.02, KICK_NOTE),
            RawDrumHit(0.12, HI_HAT_NOTE),
            RawDrumHit(0.22, SNARE_NOTE),
        ]


class FakeBeatTracker:
    name = "Beat This! 测试网格"

    def detect(self, audio_path: Path) -> BeatGrid:
        beats = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
        return BeatGrid(beats=beats, downbeats=beats[::4], engine=self.name)


@pytest.fixture
def fake_transcriber(settings: Settings) -> DrumTranscriber:
    return DrumTranscriber(
        settings,
        drum_model=FakeDrumModel(),
        beat_tracker=FakeBeatTracker(),
        allow_fallback=False,
    )
