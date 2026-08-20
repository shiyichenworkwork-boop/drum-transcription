from __future__ import annotations

from pathlib import Path

import mido
import pytest

from app.midi import (
    CRASH_NOTE,
    FLOOR_TOM_NOTE,
    HIGH_TOM_NOTE,
    HI_HAT_NOTE,
    KICK_NOTE,
    LOW_MID_TOM_NOTE,
    RIDE_NOTE,
    SNARE_NOTE,
)
from app.strum import StrumTranscriber


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


def write_strum_chart(path: Path) -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    sync = mido.MidiTrack()
    sync.append(mido.MetaMessage("track_name", name="TEMPO", time=0))
    sync.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    sync.append(
        mido.MetaMessage(
            "time_signature",
            numerator=4,
            denominator=4,
            time=0,
        )
    )
    midi.tracks.append(sync)

    drums = mido.MidiTrack()
    drums.append(mido.MetaMessage("track_name", name="PART DRUMS", time=0))
    absolute_events = [
        (0, 96),
        (480, 97),
        (960, 110),
        (960, 98),
        (1200, 98),
        (1440, 111),
        (1440, 99),
        (1680, 99),
        (1920, 112),
        (1920, 100),
        (2160, 100),
    ]
    previous_tick = 0
    for tick, note in absolute_events:
        drums.append(
            mido.Message(
                "note_on",
                channel=9,
                note=note,
                velocity=100,
                time=tick - previous_tick,
            )
        )
        previous_tick = tick
    midi.tracks.append(drums)
    midi.save(path)


def test_converts_strum_expert_chart_to_general_midi(settings, tmp_path) -> None:
    source = tmp_path / "strum-chart.mid"
    destination = tmp_path / "drums.mid"
    write_strum_chart(source)

    result = StrumTranscriber(settings)._convert_chart(
        source,
        destination,
        bar_offset_beats=1,
    )

    absolute_tick = 0
    notes: list[tuple[int, int]] = []
    signatures: list[tuple[int, int]] = []
    for message in mido.MidiFile(destination).tracks[0]:
        absolute_tick += message.time
        if message.type == "note_on" and message.velocity > 0:
            notes.append((absolute_tick, message.note))
        elif message.type == "time_signature":
            signatures.append((absolute_tick, message.numerator))

    assert notes == [
        (0, KICK_NOTE),
        (480, SNARE_NOTE),
        (960, HIGH_TOM_NOTE),
        (1200, HI_HAT_NOTE),
        (1440, LOW_MID_TOM_NOTE),
        (1680, RIDE_NOTE),
        (1920, FLOOR_TOM_NOTE),
        (2160, CRASH_NOTE),
    ]
    assert signatures == [(0, 1), (480, 4)]
    assert result.event_count == 8
    assert result.tempo_bpm == 120.0
    assert result.beat_unit == 4
    assert result.bar_offset_beats == 1
    assert "STRUM" in result.engine


def test_manual_six_eight_keeps_strum_playback_timing(settings, tmp_path) -> None:
    source = tmp_path / "strum-chart.mid"
    automatic = tmp_path / "automatic.mid"
    six_eight = tmp_path / "six-eight.mid"
    write_strum_chart(source)
    transcriber = StrumTranscriber(settings)

    transcriber._convert_chart(source, automatic, bar_offset_beats=0)
    result = transcriber._convert_chart(
        source,
        six_eight,
        bar_offset_beats=0,
        meter_override="6/8",
    )

    signatures = [
        message
        for message in mido.MidiFile(six_eight).tracks[0]
        if message.type == "time_signature"
    ]
    assert (result.beats_per_bar, result.beat_unit) == (6, 8)
    assert (signatures[0].numerator, signatures[0].denominator) == (6, 8)
    assert midi_note_times(six_eight) == pytest.approx(
        midi_note_times(automatic),
        abs=1e-5,
    )
