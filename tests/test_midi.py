from __future__ import annotations

import wave
from pathlib import Path

import mido
import numpy as np
import pytest

from app.midi import (
    CRASH_NOTE,
    HI_HAT_NOTE,
    KICK_NOTE,
    SNARE_NOTE,
    TOM_NOTE,
    DrumEvent,
    DrumTranscriber,
    rewrite_midi_bar_offset,
)
from app.midi_models import BeatGrid, RawDrumHit


class StubDrumModel:
    name = "ADTOF 测试模型"

    def transcribe(self, audio_path: Path) -> list[RawDrumHit]:
        hits: list[RawDrumHit] = []
        for time_seconds in np.arange(0.25, 3.76, 0.25):
            hits.append(RawDrumHit(float(time_seconds), HI_HAT_NOTE))
        for time_seconds in np.arange(0.25, 3.76, 0.5):
            hits.append(RawDrumHit(float(time_seconds), KICK_NOTE))
        for time_seconds in np.arange(0.75, 3.76, 1.0):
            hits.append(RawDrumHit(float(time_seconds), SNARE_NOTE))
        hits.extend(
            [
                RawDrumHit(1.26, TOM_NOTE),
                RawDrumHit(2.24, CRASH_NOTE),
            ]
        )
        return hits


class StubBeatTracker:
    name = "Beat This! 测试网格"

    def detect(self, audio_path: Path) -> BeatGrid:
        beats = np.arange(0.25, 4.76, 0.5)
        return BeatGrid(beats=beats, downbeats=beats[::4], engine=self.name)


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


def write_stereo_wave(path: Path, samples: np.ndarray, sample_rate: int = 44_100) -> None:
    pcm = (samples * 32_767).astype("<i2")
    stereo = np.column_stack((pcm, pcm)).ravel()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(stereo.tobytes())


def midi_note_times(path: Path) -> list[float]:
    midi = mido.MidiFile(path)
    tempo = mido.bpm2tempo(120)
    elapsed = 0.0
    note_times: list[float] = []
    for message in mido.merge_tracks(midi.tracks):
        elapsed += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if message.type == "set_tempo":
            tempo = message.tempo
        elif message.type == "note_on" and message.velocity > 0:
            note_times.append(elapsed)
    return note_times


async def test_transcribes_and_quantizes_five_drum_classes(settings, tmp_path) -> None:
    source = tmp_path / "drums.wav"
    destination = tmp_path / "drums.mid"
    write_stereo_wave(source, synthetic_drum_loop())
    transcriber = DrumTranscriber(
        settings,
        drum_model=StubDrumModel(),
        beat_tracker=StubBeatTracker(),
        allow_fallback=False,
    )

    result = await transcriber.transcribe(source, destination)

    payload = destination.read_bytes()
    assert payload.startswith(b"MThd\x00\x00\x00\x06")
    assert b"MTrk" in payload
    assert result.engine == "ADTOF 测试模型 + Beat This! 测试网格"
    assert result.quantized is True
    assert result.event_count >= 20
    for note in (KICK_NOTE, SNARE_NOTE, HI_HAT_NOTE, TOM_NOTE, CRASH_NOTE):
        assert result.note_counts[note] > 0
    assert result.tempo_bpm == 120.0
    assert result.beats_per_bar == 4
    assert result.beat_unit == 4
    assert result.bar_offset_beats == 0

    midi = mido.MidiFile(destination)
    absolute_tick = 0
    notes: list[tuple[int, int, int]] = []
    time_signatures = []
    tempo_changes = []
    for message in midi.tracks[0]:
        absolute_tick += message.time
        if message.type == "note_on" and message.velocity > 0:
            notes.append((absolute_tick, message.note, message.velocity))
        elif message.type == "time_signature":
            time_signatures.append(message)
        elif message.type == "set_tempo":
            tempo_changes.append(message)
    assert {note for _, note, _ in notes} == set(result.note_counts)
    assert all(tick % 120 == 0 for tick, _, _ in notes)
    assert len({velocity for _, _, velocity in notes}) > 1
    assert time_signatures[0].numerator == 4
    assert time_signatures[0].denominator == 4
    assert tempo_changes


async def test_bar_offset_moves_only_bar_lines(settings, tmp_path) -> None:
    source = tmp_path / "drums.wav"
    automatic_path = tmp_path / "automatic.mid"
    shifted_path = tmp_path / "shifted.mid"
    write_stereo_wave(source, synthetic_drum_loop())
    transcriber = DrumTranscriber(
        settings,
        drum_model=StubDrumModel(),
        beat_tracker=StubBeatTracker(),
        allow_fallback=False,
    )

    await transcriber.transcribe(source, automatic_path)
    shifted = await transcriber.transcribe(
        source,
        shifted_path,
        bar_offset_beats=1,
    )

    def timeline(path: Path):
        absolute_tick = 0
        notes = []
        tempos = []
        signatures = []
        for message in mido.MidiFile(path).tracks[0]:
            absolute_tick += message.time
            if message.type in {"note_on", "note_off"}:
                notes.append((absolute_tick, message.type, message.note, message.velocity))
            elif message.type == "set_tempo":
                tempos.append((absolute_tick, message.tempo))
            elif message.type == "time_signature":
                signatures.append(
                    (absolute_tick, message.numerator, message.denominator)
                )
        return notes, tempos, signatures

    automatic_notes, automatic_tempos, automatic_signatures = timeline(automatic_path)
    shifted_notes, shifted_tempos, shifted_signatures = timeline(shifted_path)
    assert shifted.bar_offset_beats == 1
    assert shifted_notes == automatic_notes
    assert shifted_tempos == automatic_tempos
    assert automatic_signatures == [(0, 4, 4)]
    assert shifted_signatures == [(0, 1, 4), (480, 4, 4)]


def test_rewrite_bar_offset_preserves_notes_and_tempo(tmp_path: Path) -> None:
    source = tmp_path / "source.mid"
    shifted = tmp_path / "shifted.mid"
    midi = mido.MidiFile(type=0, ticks_per_beat=480)
    track = mido.MidiTrack()
    track.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    track.append(mido.Message("note_on", channel=9, note=36, velocity=110, time=240))
    track.append(mido.Message("note_off", channel=9, note=36, velocity=0, time=60))
    midi.tracks.append(track)
    midi.save(source)

    rewrite_midi_bar_offset(
        source,
        shifted,
        beats_per_bar=4,
        bar_offset_beats=-1,
    )

    absolute_tick = 0
    musical_events = []
    signatures = []
    for message in mido.MidiFile(shifted).tracks[0]:
        absolute_tick += message.time
        if message.type in {"note_on", "note_off", "set_tempo"}:
            musical_events.append((absolute_tick, message.type, message.dict()))
        elif message.type == "time_signature":
            signatures.append((absolute_tick, message.numerator))
    assert [(tick, kind) for tick, kind, _ in musical_events] == [
        (0, "set_tempo"),
        (240, "note_on"),
        (300, "note_off"),
    ]
    assert signatures == [(0, 3), (1440, 4)]


def test_triplet_events_choose_triplet_grid(settings) -> None:
    transcriber = DrumTranscriber(
        settings,
        drum_model=StubDrumModel(),
        beat_tracker=StubBeatTracker(),
    )
    events = [
        DrumEvent(beat + fraction, HI_HAT_NOTE, 90)
        for beat in (0.0, 1.0, 2.0, 3.0)
        for fraction in (0.0, 1 / 3, 2 / 3)
    ]
    grid = BeatGrid(
        beats=np.arange(0.0, 6.0, 1.0),
        downbeats=np.array([0.0, 4.0]),
        engine="test",
    )

    result = transcriber._quantize(events, grid, 4.0)

    assert result.triplet_bars == 1
    assert any(event.tick % 480 in {160, 320} for event in result.events)


def test_detects_three_four_from_downbeat_spacing(settings) -> None:
    transcriber = DrumTranscriber(settings)
    grid = BeatGrid(
        beats=np.arange(0.0, 5.0, 0.5),
        downbeats=np.arange(0.0, 5.0, 1.5),
        engine="test",
    )
    events = [
        DrumEvent(float(time_seconds), KICK_NOTE, 100)
        for time_seconds in np.arange(0.0, 4.5, 0.5)
    ]

    result = transcriber._quantize(events, grid, 4.5)

    assert (result.beats_per_bar, result.beat_unit) == (3, 4)
    destination = settings.data_dir / "three-four.mid"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(transcriber._encode_midi(result))
    signatures = [
        message
        for message in mido.MidiFile(destination).tracks[0]
        if message.type == "time_signature"
    ]
    assert (signatures[0].numerator, signatures[0].denominator) == (3, 4)


def test_detects_six_eight_and_groups_dotted_quarter_pulses(settings) -> None:
    transcriber = DrumTranscriber(settings)
    grid = BeatGrid(
        beats=np.arange(0.0, 6.0, 0.75),
        downbeats=np.arange(0.0, 6.0, 1.5),
        engine="test",
    )
    events = [
        DrumEvent(float(time_seconds), HI_HAT_NOTE, 92)
        for time_seconds in np.arange(0.0, 5.5, 0.25)
    ]

    result = transcriber._quantize(events, grid, 5.5)

    assert (result.beats_per_bar, result.beat_unit) == (6, 8)
    assert result.ticks_per_pulse == 720
    assert result.tempo_bpm == 80.0


def test_manual_six_eight_preserves_note_playback_times(settings, tmp_path) -> None:
    transcriber = DrumTranscriber(settings)
    grid = BeatGrid(
        beats=np.arange(0.0, 6.0, 0.5),
        downbeats=np.arange(0.0, 6.0, 2.0),
        engine="test",
    )
    events = [
        DrumEvent(float(time_seconds), KICK_NOTE, 105)
        for time_seconds in np.arange(0.0, 5.0, 0.5)
    ]
    automatic = transcriber._quantize(events, grid, 5.0)
    six_eight = transcriber._quantize(events, grid, 5.0, meter="6/8")
    automatic_path = tmp_path / "automatic-4-4.mid"
    six_eight_path = tmp_path / "manual-6-8.mid"
    automatic_path.write_bytes(transcriber._encode_midi(automatic))
    six_eight_path.write_bytes(transcriber._encode_midi(six_eight))

    assert (six_eight.beats_per_bar, six_eight.beat_unit) == (6, 8)
    assert midi_note_times(six_eight_path) == pytest.approx(
        midi_note_times(automatic_path),
        abs=1e-5,
    )
