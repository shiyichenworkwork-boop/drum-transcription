from __future__ import annotations

import asyncio
import os
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

import mido

from app.config import Settings
from app.midi import (
    CRASH_NOTE,
    FLOOR_TOM_NOTE,
    HIGH_TOM_NOTE,
    HI_HAT_NOTE,
    KICK_NOTE,
    LOW_MID_TOM_NOTE,
    RIDE_NOTE,
    SNARE_NOTE,
    STRUM_DRUM_NOTES,
    TICKS_PER_BEAT,
    DrumEvent,
    DrumTranscriber,
    MidiTranscriptionError,
    MidiTranscriptionResult,
    METER_OPTIONS,
    QuantizationResult,
)
from app.runtime import script_command


ProgressCallback = Callable[[str], Awaitable[None]]

STRUM_SOURCE_COMMIT = "9f420cb6550284d15188e9b69f27614ee62fa731"
STRUM_MODEL_REPOSITORY = "opria123/strum"
STRUM_MODEL_REVISION = "5b9ab23b73291a989407b7e808c7c6261f37b704"

# Only the drum checkpoints are needed. The guitar, vocal, keys and section
# models remain outside the local cache.
STRUM_WEIGHT_FILES = {
    "drums/drums_mc_onset/best.pt": "drums_mc_onset/best.pt",
    "drums/drums_phase3/best.pt": "drums_phase3/best.pt",
    "drums/tom_refinement_demucs/best.pt": "tom_refinement_demucs/best.pt",
    "drums_classifier_ensemble/onset_classifier/best_f1.pt": "onset_classifier/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v4/best_f1.pt": "onset_classifier_v4/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v6/best_f1.pt": "onset_classifier_v6/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v12_clean/best_f1.pt": "onset_classifier_v12_clean/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v15/best_f1.pt": "onset_classifier_v15/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v16/best_f1.pt": "onset_classifier_v16/best_f1.pt",
    "drums_classifier_ensemble/onset_classifier_v17/best_f1.pt": "onset_classifier_v17/best_f1.pt",
}

STRUM_STAGE_MESSAGES = {
    "load": "正在加载 STRUM 高精度鼓模型",
    "infer": "STRUM 正在检测鼓点并分类八类鼓件",
    "convert": "正在整理 STRUM 鼓点与 MIDI 时间轴",
}


@dataclass(frozen=True, slots=True)
class StrumPaths:
    root: Path
    source: Path
    checkpoints: Path


class StrumRuntime:
    """Installs the pinned STRUM source and drum-only checkpoints on demand."""

    def __init__(self, settings: Settings):
        root = settings.models_dir / "strum"
        self.paths = StrumPaths(
            root=root,
            source=root / f"source-{STRUM_SOURCE_COMMIT[:8]}",
            checkpoints=root / "checkpoints",
        )

    async def ensure_ready(self, progress: ProgressCallback | None = None) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        await self._ensure_source(progress)
        await self._ensure_weights(progress)

    async def _ensure_source(self, progress: ProgressCallback | None) -> None:
        marker = self.paths.source / ".drum-separator-strum-version"
        if (
            (self.paths.source / "scripts" / "batch_infer_hybrid.py").is_file()
            and marker.is_file()
            and marker.read_text(encoding="utf-8").strip() == STRUM_SOURCE_COMMIT
        ):
            return

        await self._report(progress, "首次使用：正在下载 STRUM 源码")
        staging = self.paths.root / f"source.part-{uuid.uuid4().hex}"
        archive = self.paths.root / f"source-{STRUM_SOURCE_COMMIT[:8]}.zip"
        extract_root = self.paths.root / f"extract-{uuid.uuid4().hex}"
        try:
            url = (
                "https://github.com/opria123/strum/archive/"
                f"{STRUM_SOURCE_COMMIT}.zip"
            )
            try:
                await asyncio.to_thread(self._download_file, url, archive)
            except (OSError, urllib.error.URLError) as exc:
                raise MidiTranscriptionError(f"STRUM 源码下载失败：{exc}") from exc
            extract_root.mkdir(parents=True)
            try:
                with zipfile.ZipFile(archive) as bundle:
                    root = extract_root.resolve()
                    for member in bundle.infolist():
                        resolved = (extract_root / member.filename).resolve()
                        if root != resolved and root not in resolved.parents:
                            raise MidiTranscriptionError("STRUM 源码压缩包路径异常。")
                    bundle.extractall(extract_root)
            except zipfile.BadZipFile as exc:
                raise MidiTranscriptionError("STRUM 源码下载内容损坏。") from exc
            extracted = next(
                (path for path in extract_root.iterdir() if path.is_dir()),
                None,
            )
            if extracted is None:
                raise MidiTranscriptionError("STRUM 源码压缩包内容不完整。")
            os.replace(extracted, staging)
            shutil.rmtree(extract_root, ignore_errors=True)
            marker_path = staging / ".drum-separator-strum-version"
            marker_path.write_text(STRUM_SOURCE_COMMIT, encoding="utf-8")
            if self.paths.source.exists():
                shutil.rmtree(self.paths.source)
            os.replace(staging, self.paths.source)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(extract_root, ignore_errors=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    async def _ensure_weights(self, progress: ProgressCallback | None) -> None:
        for index, (remote_path, local_path) in enumerate(
            STRUM_WEIGHT_FILES.items(),
            start=1,
        ):
            destination = self.paths.checkpoints / local_path
            if self._valid_checkpoint(destination):
                continue
            self._resume_invalid_checkpoint(destination)
            await self._report(
                progress,
                f"首次使用：正在下载 STRUM 鼓模型 {index}/{len(STRUM_WEIGHT_FILES)}",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            url = (
                f"https://huggingface.co/{STRUM_MODEL_REPOSITORY}/resolve/"
                f"{STRUM_MODEL_REVISION}/{remote_path}?download=true"
            )
            try:
                await asyncio.to_thread(
                    self._download_file,
                    url,
                    destination,
                    validate_zip=True,
                )
            except (OSError, urllib.error.URLError) as exc:
                raise MidiTranscriptionError(
                    f"STRUM 鼓模型下载失败（{remote_path}）：{exc}"
                ) from exc

    @staticmethod
    def _valid_checkpoint(path: Path) -> bool:
        return (
            path.is_file()
            and path.stat().st_size > 1_000_000
            and zipfile.is_zipfile(path)
        )

    @staticmethod
    def _resume_invalid_checkpoint(destination: Path) -> None:
        if not destination.is_file():
            return
        part_path = destination.with_suffix(destination.suffix + ".part")
        if not part_path.is_file() or destination.stat().st_size > part_path.stat().st_size:
            os.replace(destination, part_path)
        else:
            destination.unlink()

    @classmethod
    def _download_file(
        cls,
        url: str,
        destination: Path,
        *,
        validate_zip: bool = False,
    ) -> None:
        last_error: Exception | None = None
        for _ in range(10):
            try:
                cls._download_once(url, destination)
                part_path = destination.with_suffix(destination.suffix + ".part")
                if part_path.stat().st_size <= 1_000_000:
                    raise OSError("下载内容不完整")
                if validate_zip and not zipfile.is_zipfile(part_path):
                    raise OSError("模型文件尚未下载完整")
                os.replace(part_path, destination)
                return
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
        raise OSError(f"多次断点续传仍未完成：{last_error}") from last_error

    @staticmethod
    def _download_once(url: str, destination: Path) -> None:
        part_path = destination.with_suffix(destination.suffix + ".part")
        existing = part_path.stat().st_size if part_path.is_file() else 0
        headers = {
            "User-Agent": "local-drum-separator/0.1",
            "Accept": "application/octet-stream",
        }
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            append = existing > 0 and getattr(response, "status", 200) == 206
            mode = "ab" if append else "wb"
            expected = response.headers.get("Content-Length")
            written = 0
            with part_path.open(mode) as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    written += len(chunk)
            if expected is not None and written != int(expected):
                raise OSError(
                    f"连接提前结束（本次收到 {written}，预期 {expected} 字节）"
                )

    @staticmethod
    async def _report(progress: ProgressCallback | None, message: str) -> None:
        if progress is not None:
            await progress(message)


class StrumTranscriber:
    """CPU adapter for STRUM's drum-only high-accuracy pipeline."""

    def __init__(self, settings: Settings, runtime: StrumRuntime | None = None):
        self.settings = settings
        self.runtime = runtime or StrumRuntime(settings)
        self._encoder = DrumTranscriber(settings)
        self._process: asyncio.subprocess.Process | None = None

    async def cancel(self) -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return False
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        finally:
            if self._process is process:
                self._process = None
        return True

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

        await self.runtime.ensure_ready(progress)
        destination.parent.mkdir(parents=True, exist_ok=True)
        work_dir = destination.parent / f".strum-{uuid.uuid4().hex}"
        output_dir = work_dir / "output"
        raw_midi = work_dir / "strum.mid"
        part_path = destination.with_suffix(".mid.part")
        work_dir.mkdir(parents=True, exist_ok=True)
        part_path.unlink(missing_ok=True)
        try:
            command = script_command(
                "strum_worker.py",
                "--source-root",
                str(self.runtime.paths.source),
                "--checkpoints",
                str(self.runtime.paths.checkpoints),
                "--input",
                str(source_path),
                "--beat-source",
                str(beat_source_path or source_path),
                "--output-dir",
                str(output_dir),
                "--result",
                str(raw_midi),
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._process = process
            recent_output: list[str] = []
            assert process.stdout is not None
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                recent_output.append(line)
                recent_output = recent_output[-30:]
                if line.startswith("STRUM_STAGE:") and progress is not None:
                    stage = line.partition(":")[2]
                    await progress(STRUM_STAGE_MESSAGES.get(stage, stage))
            return_code = await process.wait()
            self._process = None
            if return_code != 0 or not raw_midi.is_file():
                detail = "\n".join(recent_output)[-1200:]
                raise MidiTranscriptionError(
                    "STRUM 高精度转录失败。" + (f"\n{detail}" if detail else "")
                )

            if progress is not None:
                await progress(STRUM_STAGE_MESSAGES["convert"])
            result = await asyncio.to_thread(
                self._convert_chart,
                raw_midi,
                part_path,
                bar_offset_beats,
                meter,
            )
            os.replace(part_path, destination)
            return MidiTranscriptionResult(
                path=destination,
                event_count=result.event_count,
                tempo_bpm=result.tempo_bpm,
                note_counts=result.note_counts,
                engine=result.engine,
                quantized=result.quantized,
                beats_per_bar=result.beats_per_bar,
                beat_unit=result.beat_unit,
                bar_offset_beats=result.bar_offset_beats,
                warnings=result.warnings,
            )
        except MidiTranscriptionError:
            raise
        except (EOFError, OSError, ValueError) as exc:
            raise MidiTranscriptionError(f"STRUM 高精度转录失败：{exc}") from exc
        finally:
            await self.cancel()
            part_path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)

    def _convert_chart(
        self,
        source: Path,
        destination: Path,
        bar_offset_beats: int,
        meter_override: str = "auto",
    ) -> MidiTranscriptionResult:
        midi = mido.MidiFile(source)
        tempo_events: list[tuple[int, int]] = []
        time_signatures: list[tuple[int, int, int]] = []
        expert_events: list[tuple[int, int, int]] = []
        markers: set[tuple[int, int]] = set()

        for track in midi.tracks:
            absolute_tick = 0
            track_name = ""
            messages: list[tuple[int, mido.Message | mido.MetaMessage]] = []
            for message in track:
                absolute_tick += message.time
                messages.append((absolute_tick, message))
                if message.type == "track_name":
                    track_name = message.name
                elif message.type == "set_tempo":
                    tempo_events.append((absolute_tick, int(message.tempo)))
                elif message.type == "time_signature":
                    time_signatures.append(
                        (absolute_tick, int(message.numerator), int(message.denominator))
                    )
            if track_name != "PART DRUMS":
                continue
            for tick, message in messages:
                if message.type != "note_on" or message.velocity <= 0:
                    continue
                if message.note in {110, 111, 112}:
                    markers.add((tick, int(message.note)))
                elif message.note in {95, 96, 97, 98, 99, 100}:
                    expert_events.append(
                        (tick, int(message.note), int(message.velocity))
                    )

        if not expert_events:
            raise MidiTranscriptionError("STRUM 没有识别到 Expert 鼓点。")

        events: list[DrumEvent] = []
        for tick, source_note, velocity in expert_events:
            if source_note in {95, 96}:
                note = KICK_NOTE
            elif source_note == 97:
                note = SNARE_NOTE
            elif source_note == 98:
                note = HIGH_TOM_NOTE if (tick, 110) in markers else HI_HAT_NOTE
            elif source_note == 99:
                note = LOW_MID_TOM_NOTE if (tick, 111) in markers else RIDE_NOTE
            else:
                note = FLOOR_TOM_NOTE if (tick, 112) in markers else CRASH_NOTE
            events.append(
                DrumEvent(
                    time_seconds=0.0,
                    note=note,
                    velocity=max(1, min(127, velocity)),
                    tick=max(0, tick),
                )
            )

        tempo_by_tick = {tick: tempo for tick, tempo in sorted(tempo_events)}
        unique_tempos = sorted(tempo_by_tick.items()) or [(0, mido.bpm2tempo(120.0))]
        source_numerator, source_denominator = next(
            (
                (numerator, denominator)
                for _, numerator, denominator in sorted(time_signatures)
                if denominator in {4, 8} and numerator > 0
            ),
            (4, 4),
        )
        if meter_override == "auto":
            target_numerator, target_denominator = (
                source_numerator,
                source_denominator,
            )
        else:
            numerator, denominator = meter_override.split("/", 1)
            target_numerator, target_denominator = int(numerator), int(denominator)
        source_bar_quarters = source_numerator * 4 / source_denominator
        target_bar_quarters = target_numerator * 4 / target_denominator
        meter_scale = target_bar_quarters / source_bar_quarters
        tick_scale = (TICKS_PER_BEAT / midi.ticks_per_beat) * meter_scale
        scaled_events = [replace(event, tick=round(event.tick * tick_scale)) for event in events]
        scaled_tempos = [
            (round(tick * tick_scale), max(1, round(tempo / meter_scale)))
            for tick, tempo in unique_tempos
        ]
        quarter_bpm = mido.tempo2bpm(scaled_tempos[0][1])
        display_bpm = (
            quarter_bpm * 2 / 3
            if target_denominator == 8 and target_numerator % 3 == 0
            else quarter_bpm
        )
        quantized = QuantizationResult(
            events=sorted(scaled_events, key=lambda event: (event.tick, event.note)),
            tempo_bpm=round(display_bpm, 1),
            beats_per_bar=target_numerator,
            beat_unit=target_denominator,
            pulses_per_bar=target_numerator,
            ticks_per_pulse=TICKS_PER_BEAT * 4 / target_denominator,
            bar_offset_beats=bar_offset_beats,
            tempo_events=scaled_tempos,
            triplet_bars=0,
        )
        destination.write_bytes(self._encoder._encode_midi(quantized))
        counts = {
            note: sum(event.note == note for event in events)
            for note in STRUM_DRUM_NOTES
        }
        return MidiTranscriptionResult(
            path=destination,
            event_count=len(events),
            tempo_bpm=quantized.tempo_bpm,
            note_counts=counts,
            engine="STRUM V14 + 鼓件集成模型",
            quantized=True,
            beats_per_bar=target_numerator,
            beat_unit=target_denominator,
            bar_offset_beats=bar_offset_beats,
        )
