from __future__ import annotations

import asyncio
import math
import os
import struct
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import mido

from app.config import Settings
from app.midi_models import (
    AdtofDrumModel,
    BeatGrid,
    BeatThisTracker,
    BeatTracker,
    DrumModel,
    MidiModelError,
)


KICK_NOTE = 36
SNARE_NOTE = 38
HI_HAT_NOTE = 42
TOM_NOTE = 47
CRASH_NOTE = 49
DRUM_NOTES = (KICK_NOTE, SNARE_NOTE, HI_HAT_NOTE, TOM_NOTE, CRASH_NOTE)
FLOOR_TOM_NOTE = 43
LOW_MID_TOM_NOTE = TOM_NOTE
HIGH_TOM_NOTE = 50
RIDE_NOTE = 51
STRUM_DRUM_NOTES = (
    KICK_NOTE,
    SNARE_NOTE,
    HI_HAT_NOTE,
    FLOOR_TOM_NOTE,
    LOW_MID_TOM_NOTE,
    HIGH_TOM_NOTE,
    CRASH_NOTE,
    RIDE_NOTE,
)
TICKS_PER_BEAT = 480
METER_OPTIONS = {"auto", "2/4", "3/4", "4/4", "6/8", "9/8", "12/8"}

ProgressCallback = Callable[[str], Awaitable[None]]


class MidiTranscriptionError(RuntimeError):
    pass


def rewrite_midi_bar_offset(
    source: Path,
    destination: Path,
    *,
    beats_per_bar: int,
    beat_unit: int = 4,
    bar_offset_beats: int,
) -> Path:
    """Rewrite only MIDI meter markers while preserving musical event timing."""
    if beats_per_bar <= 0:
        raise MidiTranscriptionError("MIDI 小节拍数无效。")
    if beat_unit not in {4, 8}:
        raise MidiTranscriptionError("MIDI 拍号分母无效。")
    if isinstance(bar_offset_beats, bool) or not isinstance(bar_offset_beats, int):
        raise MidiTranscriptionError("小节偏移必须是整数拍。")
    if not -2 <= bar_offset_beats <= 2:
        raise MidiTranscriptionError("小节偏移仅支持前后 2 拍。")
    if not source.is_file():
        raise MidiTranscriptionError("原 MIDI 文件不存在。")

    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_suffix(destination.suffix + ".part")
    part_path.unlink(missing_ok=True)
    try:
        midi = mido.MidiFile(source)
        if not midi.tracks:
            raise MidiTranscriptionError("MIDI 文件没有可编辑的轨道。")

        original_track = midi.tracks[0]
        events: list[tuple[int, int, int, mido.Message | mido.MetaMessage]] = []
        absolute_tick = 0
        final_tick = 0
        for order, message in enumerate(original_track):
            absolute_tick += message.time
            final_tick = max(final_tick, absolute_tick)
            if message.type in {"time_signature", "end_of_track"}:
                continue
            priority = 0 if message.type == "track_name" else 3
            events.append((absolute_tick, priority, order, message.copy(time=0)))

        pickup_beats = bar_offset_beats % beats_per_bar
        unit_ticks = midi.ticks_per_beat * 4 / beat_unit
        signatures = [(0, pickup_beats), (round(pickup_beats * unit_ticks), beats_per_bar)] if pickup_beats else [(0, beats_per_bar)]
        for order, (tick, numerator) in enumerate(signatures):
            events.append(
                (
                    tick,
                    1,
                    order,
                    mido.MetaMessage(
                        "time_signature",
                        numerator=numerator,
                        denominator=beat_unit,
                        clocks_per_click=(36 if beat_unit == 8 and beats_per_bar % 3 == 0 else 24),
                        notated_32nd_notes_per_beat=8,
                        time=0,
                    ),
                )
            )

        events.sort(key=lambda item: (item[0], item[1], item[2]))
        rewritten = mido.MidiTrack()
        previous_tick = 0
        for tick, _, _, message in events:
            rewritten.append(message.copy(time=tick - previous_tick))
            previous_tick = tick
        rewritten.append(
            mido.MetaMessage(
                "end_of_track",
                time=max(0, final_tick - previous_tick),
            )
        )
        midi.tracks[0] = rewritten
        midi.save(part_path)
        os.replace(part_path, destination)
        return destination
    except MidiTranscriptionError:
        raise
    except (OSError, EOFError, ValueError) as exc:
        raise MidiTranscriptionError(f"无法调整 MIDI 小节线：{exc}") from exc
    finally:
        part_path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class DrumEvent:
    time_seconds: float
    note: int
    velocity: int
    tick: int = 0


@dataclass(frozen=True, slots=True)
class QuantizationResult:
    events: list[DrumEvent]
    tempo_bpm: float
    beats_per_bar: int
    beat_unit: int
    pulses_per_bar: int
    ticks_per_pulse: float
    bar_offset_beats: int
    tempo_events: list[tuple[int, int]]
    triplet_bars: int


@dataclass(frozen=True, slots=True)
class MidiTranscriptionResult:
    path: Path
    event_count: int
    tempo_bpm: float
    note_counts: dict[int, int]
    engine: str
    quantized: bool
    beats_per_bar: int
    beat_unit: int
    bar_offset_beats: int
    warnings: tuple[str, ...] = ()


class DrumTranscriber:
    """Model-based drum transcription followed by beat-aware MIDI cleanup."""

    analysis_sample_rate = 22_050
    frame_size = 1_024
    hop_size = 256

    def __init__(
        self,
        settings: Settings,
        *,
        drum_model: DrumModel | None = None,
        beat_tracker: BeatTracker | None = None,
        allow_fallback: bool = True,
    ):
        self.settings = settings
        self.ffmpeg_path = settings.resolve_ffmpeg()
        self.drum_model = drum_model or AdtofDrumModel()
        self.beat_tracker = beat_tracker or BeatThisTracker(settings)
        self.allow_fallback = allow_fallback

    async def transcribe(
        self,
        source_path: Path,
        destination: Path,
        *,
        beat_source_path: Path | None = None,
        bar_offset_beats: int = 0,
        meter: str = "auto",
        progress: ProgressCallback | None = None,
    ) -> MidiTranscriptionResult:
        if isinstance(bar_offset_beats, bool) or not isinstance(bar_offset_beats, int):
            raise MidiTranscriptionError("小节偏移必须是整数拍。")
        if not -2 <= bar_offset_beats <= 2:
            raise MidiTranscriptionError("小节偏移仅支持前后 2 拍。")
        if meter not in METER_OPTIONS:
            raise MidiTranscriptionError("不支持的拍号设置。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        decoded_path = destination.with_suffix(".drums.analysis.wav")
        beat_path = destination.with_suffix(".beat.analysis.wav")
        part_path = destination.with_suffix(".mid.part")
        for path in (decoded_path, beat_path, part_path):
            path.unlink(missing_ok=True)
        warnings: list[str] = []

        try:
            await self._report(progress, "正在准备鼓轨分析")
            await self._decode_audio(source_path, decoded_path)
            selected_beat_source = beat_source_path or source_path
            if selected_beat_source.resolve() == source_path.resolve():
                beat_path = decoded_path
            else:
                await self._decode_audio(selected_beat_source, beat_path)

            samples = await asyncio.to_thread(self._read_wave, decoded_path)
            fallback_events: list[DrumEvent] | None = None
            fallback_tempo: float | None = None

            await self._report(progress, "正在用 ADTOF 识别五类鼓件")
            try:
                raw_hits = await asyncio.to_thread(
                    self.drum_model.transcribe,
                    decoded_path,
                )
                events = [
                    DrumEvent(hit.time_seconds, hit.note, 100)
                    for hit in raw_hits
                    if hit.note in DRUM_NOTES
                ]
                if not events:
                    raise MidiModelError("鼓件模型没有返回可用事件。")
                drum_engine = self.drum_model.name
            except Exception as exc:
                if not self.allow_fallback:
                    raise MidiTranscriptionError(str(exc)) from exc
                fallback_events, fallback_tempo = await asyncio.to_thread(
                    self._analyze_heuristic,
                    samples,
                )
                events = fallback_events
                drum_engine = "频谱识别回退"
                warnings.append(f"ADTOF 不可用：{exc}")

            events = self._apply_velocities(events, samples)
            duration_seconds = samples.size / self.analysis_sample_rate

            await self._report(progress, "正在检测拍点与小节")
            try:
                beat_grid = await asyncio.to_thread(
                    self.beat_tracker.detect,
                    beat_path,
                )
                beat_engine = self.beat_tracker.name
            except Exception as exc:
                if fallback_tempo is None:
                    if fallback_events is None:
                        fallback_events, fallback_tempo = await asyncio.to_thread(
                            self._analyze_heuristic,
                            samples,
                        )
                    else:
                        fallback_tempo = 120.0
                beat_grid = self._regular_beat_grid(
                    duration_seconds,
                    fallback_tempo or 120.0,
                )
                beat_engine = beat_grid.engine
                warnings.append(f"Beat This! 不可用：{exc}")

            await self._report(progress, "正在量化节奏并清理重复鼓点")
            quantized = self._quantize(
                events,
                beat_grid,
                duration_seconds,
                bar_offset_beats=bar_offset_beats,
                meter=meter,
            )
            if not quantized.events:
                raise MidiTranscriptionError("量化后没有可写入的鼓点。")
            midi_bytes = self._encode_midi(quantized)
            await asyncio.to_thread(part_path.write_bytes, midi_bytes)
            os.replace(part_path, destination)

            counts = {
                note: sum(event.note == note for event in quantized.events)
                for note in DRUM_NOTES
            }
            return MidiTranscriptionResult(
                path=destination,
                event_count=len(quantized.events),
                tempo_bpm=quantized.tempo_bpm,
                note_counts=counts,
                engine=f"{drum_engine} + {beat_engine}",
                quantized=True,
                beats_per_bar=quantized.beats_per_bar,
                beat_unit=quantized.beat_unit,
                bar_offset_beats=quantized.bar_offset_beats,
                warnings=tuple(warnings),
            )
        except MidiTranscriptionError:
            raise
        except (wave.Error, EOFError, OSError) as exc:
            raise MidiTranscriptionError(f"无法分析鼓轨：{exc}") from exc
        finally:
            decoded_path.unlink(missing_ok=True)
            if beat_path != decoded_path:
                beat_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)

    async def _report(
        self,
        progress: ProgressCallback | None,
        message: str,
    ) -> None:
        if progress is not None:
            await progress(message)

    async def _decode_audio(self, source: Path, destination: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            self.ffmpeg_path,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(self.analysis_sample_rate),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise MidiTranscriptionError(
                f"鼓轨解码失败：{detail[-300:] if detail else '未知错误'}"
            )

    def _read_wave(self, path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise MidiTranscriptionError("鼓轨分析格式异常。")
            frames = audio.readframes(audio.getnframes())
        if not frames:
            raise MidiTranscriptionError("鼓轨为空。")
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32_768.0

    def _apply_velocities(
        self,
        events: list[DrumEvent],
        samples: np.ndarray,
    ) -> list[DrumEvent]:
        if not events:
            return []
        strengths: list[float] = []
        before = round(0.008 * self.analysis_sample_rate)
        after = round(0.075 * self.analysis_sample_rate)
        for event in events:
            center = round(event.time_seconds * self.analysis_sample_rate)
            segment = samples[max(0, center - before) : min(samples.size, center + after)]
            if segment.size:
                peak = float(np.percentile(np.abs(segment), 96))
                rms = float(np.sqrt(np.mean(np.square(segment), dtype=np.float64)))
                strengths.append(0.7 * peak + 0.3 * rms)
            else:
                strengths.append(0.0)
        low = float(np.percentile(strengths, 10))
        high = float(np.percentile(strengths, 95))
        scale = max(1e-6, high - low)
        result: list[DrumEvent] = []
        for event, strength in zip(events, strengths, strict=True):
            normalized = max(0.0, min(1.0, (strength - low) / scale))
            velocity = round(46 + 77 * math.sqrt(normalized))
            result.append(replace(event, velocity=max(1, min(127, velocity))))
        return result

    def _regular_beat_grid(self, duration_seconds: float, tempo_bpm: float) -> BeatGrid:
        tempo_bpm = max(60.0, min(180.0, float(tempo_bpm)))
        interval = 60.0 / tempo_bpm
        beats = np.arange(0, duration_seconds + interval * 1.5, interval)
        return BeatGrid(
            beats=beats,
            downbeats=beats[::4],
            engine="固定节拍回退",
        )

    def _quantize(
        self,
        events: list[DrumEvent],
        beat_grid: BeatGrid,
        duration_seconds: float,
        *,
        bar_offset_beats: int = 0,
        meter: str = "auto",
    ) -> QuantizationResult:
        beats = self._prepare_beats(beat_grid.beats, duration_seconds)
        downbeats = np.asarray(beat_grid.downbeats, dtype=np.float64)
        median_interval = float(np.median(np.diff(beats)))
        detected_pulses, anchor = self._infer_meter(beats, downbeats)

        raw_placements: list[tuple[DrumEvent, int, float]] = []
        for event in events:
            beat_index = int(np.searchsorted(beats, event.time_seconds, side="right") - 1)
            beat_index = max(0, min(beat_index, beats.size - 2))
            interval = max(1e-6, float(beats[beat_index + 1] - beats[beat_index]))
            phase = max(0.0, min(1.0, (event.time_seconds - beats[beat_index]) / interval))
            raw_placements.append((event, beat_index, phase))

        offbeat_phases = [
            phase
            for _, _, phase in raw_placements
            if min(phase, 1 - phase) > 0.06
        ]
        compound = self._has_compound_subdivision(offbeat_phases)
        beats_per_bar, beat_unit = self._resolve_meter(
            detected_pulses,
            compound=compound,
            override=meter,
        )
        bar_ticks = beats_per_bar * TICKS_PER_BEAT * 4 / beat_unit
        ticks_per_pulse = bar_ticks / detected_pulses
        tick_offset = (-anchor % detected_pulses) * ticks_per_pulse
        if beat_unit == 8 and beats_per_bar % 3 == 0:
            main_beats_per_bar = beats_per_bar / 3
            tempo_bpm = round(
                (60.0 / median_interval) * main_beats_per_bar / detected_pulses,
                1,
            )
        else:
            tempo_bpm = round(
                (60.0 / median_interval) * beats_per_bar / detected_pulses,
                1,
            )

        placements: list[tuple[DrumEvent, int, float, int]] = [
            (
                event,
                beat_index,
                phase,
                math.floor((beat_index - anchor) / detected_pulses),
            )
            for event, beat_index, phase in raw_placements
        ]

        straight = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        triplet = np.array([0.0, 1 / 3, 2 / 3, 1.0])
        triplet_bars: set[int] = set()
        for bar_index in {item[3] for item in placements}:
            phases = [item[2] for item in placements if item[3] == bar_index]
            offbeats = [phase for phase in phases if min(phase, 1 - phase) > 0.06]
            if len(offbeats) < 2:
                continue
            straight_error = sum(float(np.min(np.abs(straight - phase))) for phase in offbeats)
            triplet_error = sum(float(np.min(np.abs(triplet - phase))) for phase in offbeats)
            if triplet_error < straight_error * 0.72:
                triplet_bars.add(bar_index)

        quantized_events: list[DrumEvent] = []
        for event, beat_index, phase, bar_index in placements:
            family = triplet if bar_index in triplet_bars else straight
            fraction = float(family[int(np.argmin(np.abs(family - phase)))])
            if fraction >= 1.0:
                beat_index += 1
                fraction = 0.0
            beat_index = min(beat_index, beats.size - 2)
            start = float(beats[beat_index])
            interval = float(beats[beat_index + 1] - start)
            tick = round(
                tick_offset
                + beat_index * ticks_per_pulse
                + fraction * ticks_per_pulse
            )
            quantized_events.append(
                replace(
                    event,
                    time_seconds=start + fraction * interval,
                    tick=max(0, tick),
                )
            )

        deduplicated: dict[tuple[int, int], DrumEvent] = {}
        for event in quantized_events:
            key = (event.tick, event.note)
            existing = deduplicated.get(key)
            if existing is None or event.velocity > existing.velocity:
                deduplicated[key] = event
        cleaned = sorted(deduplicated.values(), key=lambda event: (event.tick, event.note))
        tempo_events = self._build_tempo_map(
            beats,
            detected_pulses,
            anchor,
            tick_offset,
            ticks_per_pulse,
        )
        return QuantizationResult(
            events=cleaned,
            tempo_bpm=tempo_bpm,
            beats_per_bar=beats_per_bar,
            beat_unit=beat_unit,
            pulses_per_bar=detected_pulses,
            ticks_per_pulse=ticks_per_pulse,
            bar_offset_beats=bar_offset_beats,
            tempo_events=tempo_events,
            triplet_bars=len(triplet_bars),
        )

    def _prepare_beats(self, values: np.ndarray, duration_seconds: float) -> np.ndarray:
        beats = np.asarray(values, dtype=np.float64).reshape(-1)
        beats = np.unique(beats[np.isfinite(beats) & (beats >= 0)])
        if beats.size < 2:
            return self._regular_beat_grid(duration_seconds, 120).beats
        intervals = np.diff(beats)
        valid = intervals[(intervals >= 0.2) & (intervals <= 1.5)]
        period = float(np.median(valid if valid.size else intervals))
        while beats[0] > period * 0.25:
            beats = np.insert(beats, 0, beats[0] - period)
        while beats[-1] < duration_seconds + period:
            beats = np.append(beats, beats[-1] + period)
        return beats

    def _infer_meter(self, beats: np.ndarray, downbeats: np.ndarray) -> tuple[int, int]:
        if downbeats.size < 2:
            return 4, 0
        period = float(np.median(np.diff(beats)))
        indices: list[int] = []
        for downbeat in downbeats:
            index = int(np.argmin(np.abs(beats - downbeat)))
            if abs(float(beats[index] - downbeat)) <= period * 0.35:
                indices.append(index)
        indices = sorted(set(indices))
        gaps = [right - left for left, right in zip(indices, indices[1:]) if 2 <= right - left <= 12]
        meter = int(round(float(np.median(gaps)))) if gaps else 4
        if meter not in {2, 3, 4, 5, 6, 7, 8, 9, 12}:
            meter = 4
        return meter, indices[0] if indices else 0

    @staticmethod
    def _has_compound_subdivision(phases: list[float]) -> bool:
        if len(phases) < 6:
            return False
        straight = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        triplet = np.array([0.0, 1 / 3, 2 / 3, 1.0])
        straight_error = sum(
            float(np.min(np.abs(straight - phase))) for phase in phases
        )
        triplet_error = sum(
            float(np.min(np.abs(triplet - phase))) for phase in phases
        )
        return triplet_error < straight_error * 0.62

    @staticmethod
    def _resolve_meter(
        detected_pulses: int,
        *,
        compound: bool,
        override: str,
    ) -> tuple[int, int]:
        if override != "auto":
            numerator, denominator = override.split("/", 1)
            return int(numerator), int(denominator)
        if detected_pulses == 2 and compound:
            return 6, 8
        if detected_pulses == 6:
            return 6, 8
        if detected_pulses == 9:
            return 9, 8
        if detected_pulses == 12:
            return 12, 8
        if detected_pulses == 8:
            return 4, 4
        return detected_pulses, 4

    def _build_tempo_map(
        self,
        beats: np.ndarray,
        pulses_per_bar: int,
        anchor: int,
        tick_offset: float,
        ticks_per_pulse: float,
    ) -> list[tuple[int, int]]:
        global_interval = float(np.median(np.diff(beats)))
        tempo_scale = TICKS_PER_BEAT / ticks_per_pulse
        events: list[tuple[int, int]] = [
            (0, round(global_interval * tempo_scale * 1_000_000))
        ]
        first_bar = anchor
        while first_bar > 0:
            first_bar -= pulses_per_bar
        previous = events[0][1]
        for index in range(max(0, first_bar), beats.size - 1, pulses_per_bar):
            intervals = np.diff(beats[index : min(beats.size, index + pulses_per_bar + 1)])
            valid = intervals[(intervals >= 0.2) & (intervals <= 1.5)]
            if valid.size == 0:
                continue
            microseconds = round(float(np.median(valid)) * tempo_scale * 1_000_000)
            tick = max(0, round(tick_offset + index * ticks_per_pulse))
            if tick == 0:
                events[0] = (0, microseconds)
                previous = microseconds
            elif abs(microseconds - previous) / max(1, previous) >= 0.015:
                events.append((tick, microseconds))
                previous = microseconds
        return events

    def _analyze_heuristic(self, samples: np.ndarray) -> tuple[list[DrumEvent], float]:
        if samples.size < self.frame_size:
            samples = np.pad(samples, (0, self.frame_size - samples.size))
        frame_count = 1 + math.ceil((samples.size - self.frame_size) / self.hop_size)
        padded_size = (frame_count - 1) * self.hop_size + self.frame_size
        if samples.size < padded_size:
            samples = np.pad(samples, (0, padded_size - samples.size))

        window = np.hanning(self.frame_size).astype(np.float32)
        frequencies = np.fft.rfftfreq(self.frame_size, 1 / self.analysis_sample_rate)
        band_masks = (
            (frequencies >= 35) & (frequencies < 180),
            (frequencies >= 180) & (frequencies < 3_500),
            (frequencies >= 3_500) & (frequencies <= 11_000),
        )
        band_flux = np.zeros((3, frame_count), dtype=np.float32)
        total_flux = np.zeros(frame_count, dtype=np.float32)
        previous = np.zeros(self.frame_size // 2 + 1, dtype=np.float32)

        for index in range(frame_count):
            start = index * self.hop_size
            frame = samples[start : start + self.frame_size]
            magnitude = np.log1p(np.abs(np.fft.rfft(frame * window)) * 8).astype(np.float32)
            positive = np.maximum(magnitude - previous, 0)
            total_flux[index] = float(positive.mean())
            for band_index, mask in enumerate(band_masks):
                band_flux[band_index, index] = float(positive[mask].mean())
            previous = magnitude

        normalized_total = self._normalize_envelope(total_flux)
        normalized_bands = np.vstack(
            [self._normalize_envelope(envelope) for envelope in band_flux]
        )
        combined = (
            normalized_total
            + 0.35 * normalized_bands[0]
            + 0.25 * normalized_bands[1]
            + 0.35 * normalized_bands[2]
        )
        peak_sets = [
            self._pick_peaks(combined, threshold=0.16, min_interval_seconds=0.055),
            self._pick_peaks(normalized_bands[2], threshold=0.18, min_interval_seconds=0.045),
        ]
        candidates = self._merge_peaks(
            [index for peak_set in peak_sets for index in peak_set],
            combined,
        )
        events: list[DrumEvent] = []
        for index in candidates:
            start = index * self.hop_size
            analysis = samples[start : start + self.frame_size]
            if analysis.size < self.frame_size:
                analysis = np.pad(analysis, (0, self.frame_size - analysis.size))
            spectrum = np.abs(np.fft.rfft(analysis * window)) ** 2
            energies = np.array(
                [float(spectrum[mask].sum()) for mask in band_masks],
                dtype=np.float64,
            )
            total_energy = float(energies.sum())
            if total_energy <= 1e-10:
                continue
            ratios = energies / total_energy
            if ratios[2] >= 0.62 and ratios[1] < 0.16:
                note = HI_HAT_NOTE
            elif ratios[0] >= 0.55:
                note = KICK_NOTE
            elif ratios[1] >= 0.09:
                note = SNARE_NOTE
            else:
                note = HI_HAT_NOTE
            time_seconds = (start + self.frame_size / 2) / self.analysis_sample_rate
            events.append(DrumEvent(time_seconds, note, 100))

        tempo_bpm = self._estimate_tempo(combined)
        return self._deduplicate_by_time(events), tempo_bpm

    def _normalize_envelope(self, envelope: np.ndarray) -> np.ndarray:
        if envelope.size == 0:
            return envelope.copy()
        smoothed = np.convolve(envelope, np.array([0.2, 0.6, 0.2]), mode="same")
        floor = float(np.percentile(smoothed, 45))
        ceiling = float(np.percentile(smoothed, 98))
        scale = ceiling - floor
        if scale <= 1e-9:
            return np.zeros_like(smoothed, dtype=np.float32)
        return np.clip((smoothed - floor) / scale, 0, 1.5).astype(np.float32)

    def _pick_peaks(
        self,
        envelope: np.ndarray,
        *,
        threshold: float,
        min_interval_seconds: float,
    ) -> list[int]:
        if envelope.size < 3 or float(envelope.max(initial=0)) < threshold:
            return []
        candidates = [
            index
            for index in range(1, envelope.size - 1)
            if envelope[index] >= threshold
            and envelope[index] >= envelope[index - 1]
            and envelope[index] > envelope[index + 1]
        ]
        min_frames = max(
            1,
            round(min_interval_seconds * self.analysis_sample_rate / self.hop_size),
        )
        selected: list[int] = []
        for candidate in sorted(candidates, key=lambda item: envelope[item], reverse=True):
            if all(abs(candidate - existing) >= min_frames for existing in selected):
                selected.append(candidate)
        return sorted(selected)

    def _merge_peaks(self, candidates: list[int], envelope: np.ndarray) -> list[int]:
        if not candidates:
            return []
        merge_frames = max(1, round(0.03 * self.analysis_sample_rate / self.hop_size))
        merged: list[int] = []
        for candidate in sorted(set(candidates)):
            if not merged or candidate - merged[-1] > merge_frames:
                merged.append(candidate)
            elif envelope[candidate] > envelope[merged[-1]]:
                merged[-1] = candidate
        return merged

    def _deduplicate_by_time(self, events: list[DrumEvent]) -> list[DrumEvent]:
        ordered = sorted(events, key=lambda event: (event.time_seconds, event.note))
        result: list[DrumEvent] = []
        last_by_note: dict[int, float] = {}
        for event in ordered:
            last_time = last_by_note.get(event.note, -1.0)
            if event.time_seconds - last_time < 0.06:
                continue
            result.append(event)
            last_by_note[event.note] = event.time_seconds
        return result

    def _estimate_tempo(self, envelope: np.ndarray) -> float:
        if envelope.size < 8 or float(envelope.max(initial=0)) <= 0:
            return 120.0
        centered = envelope.astype(np.float64) - float(envelope.mean())
        frames_per_second = self.analysis_sample_rate / self.hop_size
        min_lag = max(1, round(frames_per_second * 60 / 180))
        max_lag = min(envelope.size - 1, round(frames_per_second * 60 / 60))
        if max_lag <= min_lag:
            return 120.0
        correlations = np.array(
            [
                float(np.dot(centered[:-lag], centered[lag:])) / (envelope.size - lag)
                for lag in range(min_lag, max_lag + 1)
            ]
        )
        best_lag = min_lag + int(np.argmax(correlations))
        bpm = 60 * frames_per_second / best_lag
        if bpm < 80:
            bpm *= 2
        return round(float(max(60.0, min(180.0, bpm))), 1)

    def _encode_midi(self, result: QuantizationResult) -> bytes:
        track_name = b"Quantized Drum Transcription"
        track_events: list[tuple[int, int, bytes]] = [
            (0, 0, b"\xff\x03" + self._variable_length(len(track_name)) + track_name),
        ]
        # MIDI does not have a standalone "bar phase" event. Encode the requested
        # phase as a pickup measure, then start the detected meter on the shifted
        # beat. Notes and tempo events keep their original absolute ticks.
        pickup_beats = result.bar_offset_beats % result.beats_per_bar
        unit_ticks = TICKS_PER_BEAT * 4 / result.beat_unit
        clocks_per_click = (
            36
            if result.beat_unit == 8 and result.beats_per_bar % 3 == 0
            else 24
        )
        if pickup_beats:
            track_events.append(
                (
                    0,
                    1,
                    bytes(
                        (
                            0xFF,
                            0x58,
                            0x04,
                            pickup_beats,
                            int(math.log2(result.beat_unit)),
                            clocks_per_click,
                            0x08,
                        )
                    ),
                )
            )
            track_events.append(
                (
                    round(pickup_beats * unit_ticks),
                    1,
                    bytes(
                        (
                            0xFF,
                            0x58,
                            0x04,
                            result.beats_per_bar,
                            int(math.log2(result.beat_unit)),
                            clocks_per_click,
                            0x08,
                        )
                    ),
                )
            )
        else:
            track_events.append(
                (
                    0,
                    1,
                    bytes(
                        (
                            0xFF,
                            0x58,
                            0x04,
                            result.beats_per_bar,
                            int(math.log2(result.beat_unit)),
                            clocks_per_click,
                            0x08,
                        )
                    ),
                )
            )
        for tick, microseconds in result.tempo_events:
            microseconds = max(1, min(0xFFFFFF, microseconds))
            track_events.append(
                (tick, 2, b"\xff\x51\x03" + microseconds.to_bytes(3, "big"))
            )
        for event in result.events:
            duration_ticks = TICKS_PER_BEAT // 16 if event.note == HI_HAT_NOTE else TICKS_PER_BEAT // 8
            track_events.append((event.tick, 4, bytes((0x99, event.note, event.velocity))))
            track_events.append(
                (event.tick + max(1, duration_ticks), 3, bytes((0x89, event.note, 0)))
            )

        track_events.sort(key=lambda item: (item[0], item[1]))
        track = bytearray()
        previous_tick = 0
        for tick, _, payload in track_events:
            track.extend(self._variable_length(tick - previous_tick))
            track.extend(payload)
            previous_tick = tick
        track.extend(b"\x00\xff\x2f\x00")
        header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TICKS_PER_BEAT)
        return header + b"MTrk" + struct.pack(">I", len(track)) + bytes(track)

    @staticmethod
    def _variable_length(value: int) -> bytes:
        buffer = value & 0x7F
        result = bytearray()
        while value := value >> 7:
            buffer <<= 8
            buffer |= (value & 0x7F) | 0x80
        while True:
            result.append(buffer & 0xFF)
            if buffer & 0x80:
                buffer >>= 8
            else:
                break
        return bytes(result)
