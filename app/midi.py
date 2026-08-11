from __future__ import annotations

import asyncio
import math
import os
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import Settings


KICK_NOTE = 36
SNARE_NOTE = 38
HI_HAT_NOTE = 42
TICKS_PER_BEAT = 480


class MidiTranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DrumEvent:
    time_seconds: float
    note: int
    velocity: int


@dataclass(frozen=True, slots=True)
class MidiTranscriptionResult:
    path: Path
    event_count: int
    tempo_bpm: float
    note_counts: dict[int, int]


class DrumTranscriber:
    """Lightweight onset-based transcription for an already isolated drum stem."""

    analysis_sample_rate = 22_050
    frame_size = 1_024
    hop_size = 256

    def __init__(self, settings: Settings):
        self.settings = settings
        self.ffmpeg_path = settings.resolve_ffmpeg()

    async def transcribe(
        self,
        source_path: Path,
        destination: Path,
    ) -> MidiTranscriptionResult:
        destination.parent.mkdir(parents=True, exist_ok=True)
        decoded_path = destination.with_suffix(".analysis.wav")
        part_path = destination.with_suffix(".mid.part")
        decoded_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)
        try:
            process = await asyncio.create_subprocess_exec(
                self.ffmpeg_path,
                "-y",
                "-v",
                "error",
                "-i",
                str(source_path),
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
                str(decoded_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise MidiTranscriptionError(
                    f"鼓轨解码失败：{detail[-300:] if detail else '未知错误'}"
                )

            samples = await asyncio.to_thread(self._read_wave, decoded_path)
            events, tempo_bpm = await asyncio.to_thread(self._analyze, samples)
            midi_bytes = self._encode_midi(events, tempo_bpm)
            await asyncio.to_thread(part_path.write_bytes, midi_bytes)
            os.replace(part_path, destination)
            counts = {
                note: sum(event.note == note for event in events)
                for note in (KICK_NOTE, SNARE_NOTE, HI_HAT_NOTE)
            }
            return MidiTranscriptionResult(
                path=destination,
                event_count=len(events),
                tempo_bpm=tempo_bpm,
                note_counts=counts,
            )
        except (wave.Error, EOFError, OSError) as exc:
            raise MidiTranscriptionError(f"无法分析鼓轨：{exc}") from exc
        finally:
            decoded_path.unlink(missing_ok=True)
            part_path.unlink(missing_ok=True)

    def _read_wave(self, path: Path) -> np.ndarray:
        with wave.open(str(path), "rb") as audio:
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                raise MidiTranscriptionError("鼓轨分析格式异常。")
            frames = audio.readframes(audio.getnframes())
        if not frames:
            raise MidiTranscriptionError("鼓轨为空。")
        return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32_768.0

    def _analyze(self, samples: np.ndarray) -> tuple[list[DrumEvent], float]:
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
            magnitude = np.log1p(np.abs(np.fft.rfft(frame * window)) * 8).astype(
                np.float32
            )
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
            self._pick_peaks(
                normalized_bands[2], threshold=0.18, min_interval_seconds=0.045
            ),
        ]
        candidates = self._merge_peaks(
            [index for peak_set in peak_sets for index in peak_set], combined
        )
        events: list[DrumEvent] = []
        for index in candidates:
            start = index * self.hop_size
            analysis = samples[start : start + self.frame_size]
            if analysis.size < self.frame_size:
                analysis = np.pad(analysis, (0, self.frame_size - analysis.size))
            spectrum = np.abs(np.fft.rfft(analysis * window)) ** 2
            energies = np.array(
                [float(spectrum[mask].sum()) for mask in band_masks], dtype=np.float64
            )
            total_energy = float(energies.sum())
            if total_energy <= 1e-10:
                continue
            ratios = energies / total_energy
            strength = float(min(1.0, max(combined[index], normalized_total[index])))
            velocity = int(round(38 + 89 * math.sqrt(max(0.0, strength))))
            velocity = max(1, min(127, velocity))
            band_scores = normalized_bands[:, index]
            if ratios[2] >= 0.62 and ratios[1] < 0.16:
                primary_note = HI_HAT_NOTE
            elif ratios[0] >= 0.55:
                primary_note = KICK_NOTE
            elif ratios[1] >= 0.09:
                primary_note = SNARE_NOTE
            else:
                primary_note = HI_HAT_NOTE
            notes = [primary_note]
            if (
                primary_note == KICK_NOTE
                and ratios[1] >= 0.055
                and band_scores[1] >= 0.4
            ):
                notes.append(SNARE_NOTE)
            if (
                primary_note != HI_HAT_NOTE
                and ratios[2] >= 0.14
                and band_scores[2] >= 0.36
            ):
                notes.append(HI_HAT_NOTE)
            if (
                primary_note == SNARE_NOTE
                and ratios[0] >= 0.34
                and band_scores[0] >= 0.42
            ):
                notes.append(KICK_NOTE)
            time_seconds = (start + self.frame_size / 2) / self.analysis_sample_rate
            events.extend(
                DrumEvent(time_seconds=time_seconds, note=note, velocity=velocity)
                for note in notes
            )

        events = self._deduplicate_events(events)
        tempo_bpm = self._estimate_tempo(combined)
        return events, tempo_bpm

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
            round(
                min_interval_seconds
                * self.analysis_sample_rate
                / self.hop_size
            ),
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

    def _deduplicate_events(self, events: list[DrumEvent]) -> list[DrumEvent]:
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

    def _encode_midi(self, events: list[DrumEvent], tempo_bpm: float) -> bytes:
        microseconds_per_beat = round(60_000_000 / tempo_bpm)
        track_events: list[tuple[int, int, bytes]] = [
            (0, 0, b"\xff\x03\x12Drum Transcription"),
            (
                0,
                1,
                b"\xff\x51\x03" + microseconds_per_beat.to_bytes(3, "big"),
            ),
            (0, 2, b"\xff\x58\x04\x04\x02\x18\x08"),
        ]
        ticks_per_second = TICKS_PER_BEAT * tempo_bpm / 60
        for event in events:
            start_tick = max(0, round(event.time_seconds * ticks_per_second))
            duration_seconds = 0.035 if event.note == HI_HAT_NOTE else 0.065
            end_tick = start_tick + max(1, round(duration_seconds * ticks_per_second))
            track_events.append(
                (start_tick, 4, bytes((0x99, event.note, event.velocity)))
            )
            track_events.append((end_tick, 3, bytes((0x89, event.note, 0))))

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
