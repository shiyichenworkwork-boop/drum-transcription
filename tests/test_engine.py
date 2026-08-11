from __future__ import annotations

from app.audio import probe_audio
from app.engine import DemucsEngine
from tests.conftest import wav_bytes


async def test_mp3_export_is_playable_and_smaller(settings, tmp_path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "drums.mp3"
    source.write_bytes(wav_bytes(2.0))
    engine = DemucsEngine(settings)

    async def progress(stage: str, value: int, message: str | None) -> None:
        return None

    result = await engine._encode_mp3(
        "encode-test",
        source,
        destination,
        progress,
    )

    assert result == destination
    assert destination.is_file()
    assert destination.stat().st_size < source.stat().st_size / 4
    info = await probe_audio(settings.resolve_ffmpeg(), destination)
    assert info.sample_rate == 44_100
    assert info.channels == 2
    assert abs(info.duration_seconds - 2.0) < 0.1
