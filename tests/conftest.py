from __future__ import annotations

import io
import shutil
import wave
from pathlib import Path

import imageio_ffmpeg
import pytest

from app.config import Settings
from app.engine import SeparationCancelled, SeparationEngine, SeparationResult


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

