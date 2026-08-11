from __future__ import annotations

import wave

import numpy as np

from app.midi import HI_HAT_NOTE, KICK_NOTE, SNARE_NOTE, DrumTranscriber


def synthetic_drum_loop(sample_rate: int = 44_100, duration: float = 4.0) -> np.ndarray:
    frame_count = round(sample_rate * duration)
    audio = np.zeros(frame_count, dtype=np.float32)
    rng = np.random.default_rng(2026)

    def add_tone(time_seconds: float, frequency: float, length: float, level: float) -> None:
        start = round(time_seconds * sample_rate)
        count = min(round(length * sample_rate), frame_count - start)
        if count <= 0:
            return
        time = np.arange(count) / sample_rate
        envelope = np.exp(-time * 28)
        audio[start : start + count] += level * np.sin(2 * np.pi * frequency * time) * envelope

    def add_noise(time_seconds: float, length: float, level: float) -> None:
        start = round(time_seconds * sample_rate)
        count = min(round(length * sample_rate), frame_count - start)
        if count <= 0:
            return
        time = np.arange(count) / sample_rate
        envelope = np.exp(-time * 42)
        noise = rng.standard_normal(count).astype(np.float32)
        audio[start : start + count] += level * noise * envelope

    for beat in np.arange(0.25, duration, 0.5):
        add_tone(float(beat), 62, 0.16, 0.9)
    for beat in np.arange(0.75, duration, 1.0):
        add_noise(float(beat), 0.11, 0.42)
    for beat in np.arange(0.25, duration, 0.25):
        add_tone(float(beat), 7_500, 0.035, 0.28)
    return np.clip(audio, -1, 1)


async def test_transcribes_synthetic_drum_loop_to_standard_midi(settings, tmp_path) -> None:
    source = tmp_path / "drums.wav"
    destination = tmp_path / "drums.mid"
    samples = synthetic_drum_loop()
    pcm = (samples * 32_767).astype("<i2")
    stereo = np.column_stack((pcm, pcm)).ravel()
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44_100)
        audio.writeframes(stereo.tobytes())

    result = await DrumTranscriber(settings).transcribe(source, destination)

    payload = destination.read_bytes()
    assert payload.startswith(b"MThd\x00\x00\x00\x06")
    assert b"MTrk" in payload
    assert result.event_count >= 8
    assert result.note_counts[KICK_NOTE] > 0
    assert result.note_counts[SNARE_NOTE] > 0
    assert result.note_counts[HI_HAT_NOTE] > 0
    assert 60 <= result.tempo_bpm <= 180
