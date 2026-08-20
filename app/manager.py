from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.audio import (
    AudioValidationError,
    assert_decodable,
    directory_size,
    ensure_disk_space,
    mp3_encode_args,
    probe_audio,
)
from app.config import Settings
from app.db import JobRecord, JobRepository, utc_now
from app.engine import (
    SeparationCancelled,
    SeparationEngine,
    SeparationError,
)
from app.midi import (
    DrumTranscriber,
    MidiTranscriptionError,
    rewrite_midi_bar_offset,
)
from app.schemas import (
    ACTIVE_OPERATION_STATES,
    ACTIVE_STATUSES,
    JobStatus,
    OperationState,
)
from app.strum import StrumTranscriber


class JobConflictError(RuntimeError):
    pass


class OperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ComputeRequest:
    job_id: str
    kind: str


class JobManager:
    def __init__(
        self,
        settings: Settings,
        repository: JobRepository,
        engine: SeparationEngine,
        *,
        vocal_engine: SeparationEngine | None = None,
        transcriber: DrumTranscriber | None = None,
        strum_transcriber: StrumTranscriber | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.engine = engine
        self.vocal_engine = vocal_engine or engine
        self.queue: asyncio.Queue[ComputeRequest] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._current_request: ComputeRequest | None = None
        self._current_process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self.transcriber = transcriber or DrumTranscriber(settings)
        self.strum_transcriber = strum_transcriber or StrumTranscriber(settings)

    async def start(self) -> None:
        self._stopping = False
        for job_id, kind in self.repository.prepare_startup_recovery():
            self.queue.put_nowait(ComputeRequest(job_id, kind))
        self._worker_task = asyncio.create_task(self._worker(), name="drum-separator-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._current_request:
            record = self.repository.get(self._current_request.job_id)
            if record and self._current_request.kind == "separation":
                await self._engine_for(record).cancel(self._current_request.job_id)
        await self._terminate_current_process()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(self, job_id: str, kind: str = "separation") -> None:
        await self.queue.put(ComputeRequest(job_id, kind))

    async def cancel(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        status = JobStatus(record.status)
        operation_state = OperationState(record.operation_state)
        if status == JobStatus.QUEUED:
            return self.repository.update(
                job_id,
                status=JobStatus.CANCELLED.value,
                stage="已取消",
                finished_at=utc_now(),
                cancel_requested=1,
                operation_state=OperationState.CANCELLED.value,
                operation_stage="已取消",
                operation_progress=0,
                operation_finished_at=utc_now(),
            )
        if status in ACTIVE_STATUSES:
            self.repository.update(
                job_id,
                stage="正在取消",
                cancel_requested=1,
            )
            await self._engine_for(record).cancel(job_id)
            return self.repository.require(job_id)
        if operation_state in ACTIVE_OPERATION_STATES:
            updated = self.repository.update(
                job_id,
                operation_state=OperationState.CANCELLED.value,
                operation_stage="已取消" if operation_state == OperationState.QUEUED else "正在取消",
                operation_progress=0,
                operation_finished_at=utc_now(),
                cancel_requested=1,
            )
            if operation_state == OperationState.RUNNING:
                if record.operation_kind == "midi":
                    transcriber = (
                        self.strum_transcriber
                        if record.operation_params.get("midi_model") == "strum"
                        else self.transcriber
                    )
                    cancel = getattr(transcriber, "cancel", None)
                    if cancel is not None:
                        await cancel()
                await self._terminate_current_process()
            return updated
        raise JobConflictError("当前任务状态无法取消。")

    async def retry(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        status = JobStatus(record.status)
        if status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            raise JobConflictError("只有失败或已取消的任务可以重新处理。")
        job_dir = self.settings.jobs_dir / job_id
        shutil.rmtree(job_dir / "work", ignore_errors=True)
        shutil.rmtree(job_dir / "outputs", ignore_errors=True)
        updated = self.repository.update(
            job_id,
            status=JobStatus.QUEUED.value,
            stage="等待处理",
            progress=0,
            started_at=None,
            finished_at=None,
            error=None,
            warnings_json="[]",
            drums_path=None,
            no_drums_path=None,
            vocals_path=None,
            instrumental_path=None,
            midi_path=None,
            midi_event_count=0,
            midi_tempo_bpm=None,
            midi_engine=None,
            midi_quantized=0,
            midi_warning=None,
            midi_beats_per_bar=None,
            midi_beat_unit=4,
            midi_bar_offset_beats=0,
            midi_model="adtof",
            midi_meter="auto",
            operation_kind="separation",
            operation_state=OperationState.QUEUED.value,
            operation_stage="等待处理",
            operation_progress=0,
            operation_params_json="{}",
            operation_error=None,
            operation_started_at=None,
            operation_finished_at=None,
            storage_bytes=record.input_size_bytes,
            cancel_requested=0,
        )
        await self.enqueue(job_id)
        return updated

    async def compress(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        if JobStatus(record.status) != JobStatus.COMPLETED:
            raise JobConflictError("只有已完成的任务可以压缩。")
        if OperationState(record.operation_state) in ACTIVE_OPERATION_STATES:
            raise JobConflictError("当前任务已有操作在排队或运行。")
        raw_paths = self._result_paths(record)
        if not all(raw_paths):
            raise JobConflictError("任务结果文件不完整。")
        sources = [Path(path) for path in raw_paths if path]
        if all(path.suffix.lower() == ".mp3" for path in sources):
            return record
        if any(path.suffix.lower() != ".wav" or not path.is_file() for path in sources):
            raise JobConflictError("当前结果无法转换为轻量 MP3。")
        updated = self.repository.update(
            job_id,
            operation_kind="compress",
            operation_state=OperationState.QUEUED.value,
            operation_stage="等待压缩",
            operation_progress=0,
            operation_params_json="{}",
            operation_error=None,
            operation_started_at=None,
            operation_finished_at=None,
            cancel_requested=0,
        )
        await self.enqueue(job_id, "compress")
        return updated

    async def generate_midi(
        self,
        job_id: str,
        *,
        force: bool = False,
        bar_offset_beats: int = 0,
        midi_model: str = "adtof",
        meter: str = "auto",
    ) -> JobRecord:
        if midi_model not in {"adtof", "strum"}:
            raise JobConflictError("不支持的 MIDI 识别模型。")
        if meter not in {"auto", "2/4", "3/4", "4/4", "6/8", "9/8", "12/8"}:
            raise JobConflictError("不支持的拍号设置。")
        record = self.repository.require(job_id)
        if JobStatus(record.status) != JobStatus.COMPLETED:
            raise JobConflictError("只有已完成分轨的任务可以生成 MIDI。")
        if record.separation_kind != "drums":
            raise JobConflictError("人声分轨任务不生成鼓点 MIDI。")
        if OperationState(record.operation_state) in ACTIVE_OPERATION_STATES:
            raise JobConflictError("当前任务已有操作在排队或运行。")
        midi_exists = bool(record.midi_path and Path(record.midi_path).is_file())
        if (
            not force
            and record.midi_model == midi_model
            and record.midi_bar_offset_beats == bar_offset_beats
            and record.midi_meter == meter
            and midi_exists
        ):
            return record
        if not record.drums_path or not Path(record.drums_path).is_file():
            raise JobConflictError("鼓轨文件不存在。")

        mode = (
            "rebar"
            if midi_exists
            and record.midi_model == midi_model
            and record.midi_meter == meter
            and record.midi_beats_per_bar
            and record.midi_bar_offset_beats != bar_offset_beats
            else "transcribe"
        )
        params = {
            "mode": mode,
            "force": force,
            "bar_offset_beats": bar_offset_beats,
            "midi_model": midi_model,
            "meter": meter,
        }
        updated = self.repository.update(
            job_id,
            operation_kind="midi",
            operation_state=OperationState.QUEUED.value,
            operation_stage=("等待调整小节线" if mode == "rebar" else "等待生成 MIDI"),
            operation_progress=0,
            operation_params_json=json.dumps(params, ensure_ascii=False),
            operation_error=None,
            operation_started_at=None,
            operation_finished_at=None,
            cancel_requested=0,
        )
        await self.enqueue(job_id, "midi")
        return updated

    async def _run_compress(self, job_id: str) -> None:
        record = self.repository.require(job_id)
        raw_paths = self._result_paths(record)
        if not all(raw_paths):
            raise JobConflictError("任务结果文件不完整。")
        sources = [Path(path) for path in raw_paths if path]
        output_names = (
            ("vocals.mp3", "instrumental.mp3")
            if record.separation_kind == "vocals"
            else ("drums.mp3", "no_drums.mp3")
        )
        update_fields = (
            ("vocals_path", "instrumental_path")
            if record.separation_kind == "vocals"
            else ("drums_path", "no_drums_path")
        )
        output_dir = (self.settings.jobs_dir / job_id / "outputs").resolve()
        if any(output_dir not in path.resolve().parents for path in sources):
            raise JobConflictError("结果文件路径异常。")
        ensure_disk_space(self.settings.data_dir, sum(path.stat().st_size for path in sources))
        destinations = [output_dir / name for name in output_names]
        temporary = [path.with_suffix(".mp3.part") for path in destinations]
        ffmpeg = self.settings.resolve_ffmpeg()
        try:
            for index, (source, part_path) in enumerate(
                zip(sources, temporary, strict=True), start=1
            ):
                self._ensure_operation_not_cancelled(job_id)
                self.repository.update(
                    job_id,
                    operation_stage=f"正在压缩第 {index}/2 条音轨",
                    operation_progress=10 + index * 30,
                )
                part_path.unlink(missing_ok=True)
                self._current_process = await asyncio.create_subprocess_exec(
                    *mp3_encode_args(
                        ffmpeg,
                        source,
                        part_path,
                        sample_rate=self.settings.sample_rate,
                        channels=self.settings.channels,
                        bitrate=self.settings.output_bitrate,
                    ),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await self._current_process.communicate()
                return_code = self._current_process.returncode
                self._current_process = None
                self._ensure_operation_not_cancelled(job_id)
                if return_code != 0:
                    detail = stderr.decode("utf-8", errors="replace").strip()
                    raise JobConflictError(
                        f"压缩失败：{detail[-300:] if detail else '未知错误'}"
                    )
                info = await probe_audio(ffmpeg, part_path)
                if info.sample_rate != self.settings.sample_rate or info.channels != self.settings.channels:
                    raise JobConflictError("压缩后的音频格式异常。")
                if record.duration_seconds and abs(info.duration_seconds - record.duration_seconds) > 0.1:
                    raise JobConflictError("压缩后的音频时长异常。")
                await assert_decodable(ffmpeg, part_path)

            self._ensure_operation_not_cancelled(job_id)
            for part_path, destination in zip(temporary, destinations, strict=True):
                os.replace(part_path, destination)
            for source in sources:
                source.unlink(missing_ok=True)
            self.repository.update(
                job_id,
                **{
                    update_fields[0]: str(destinations[0]),
                    update_fields[1]: str(destinations[1]),
                    "storage_bytes": directory_size(self.settings.jobs_dir / job_id),
                    "stage": "已压缩为轻量 MP3",
                    "operation_kind": None,
                    "operation_state": OperationState.IDLE.value,
                    "operation_stage": None,
                    "operation_progress": 100,
                    "operation_error": None,
                    "operation_finished_at": utc_now(),
                    "cancel_requested": 0,
                },
            )
        except AudioValidationError as exc:
            raise JobConflictError(str(exc)) from exc
        finally:
            self._current_process = None
            for part_path in temporary:
                part_path.unlink(missing_ok=True)

    async def _run_midi(self, job_id: str) -> None:
        record = self.repository.require(job_id)
        params = record.operation_params
        bar_offset_beats = int(params.get("bar_offset_beats", 0))
        midi_model = str(params.get("midi_model", "adtof"))
        meter = str(params.get("meter", "auto"))
        mode = str(params.get("mode", "transcribe"))
        job_dir = self.settings.jobs_dir / job_id
        destination = job_dir / "outputs" / "drums.mid"
        work_dir = job_dir / "work" / "midi"
        candidate = work_dir / "drums.mid"
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._ensure_operation_not_cancelled(job_id)
            if mode == "rebar":
                if not record.midi_path or not record.midi_beats_per_bar:
                    raise JobConflictError("缺少可调整小节线的 MIDI 文件。")
                self.repository.update(
                    job_id,
                    operation_stage="正在调整 MIDI 小节线",
                    operation_progress=50,
                )
                await asyncio.to_thread(
                    rewrite_midi_bar_offset,
                    Path(record.midi_path),
                    candidate,
                    beats_per_bar=record.midi_beats_per_bar,
                    beat_unit=record.midi_beat_unit,
                    bar_offset_beats=bar_offset_beats,
                )
                self._ensure_operation_not_cancelled(job_id)
                os.replace(candidate, destination)
                self.repository.update(
                    job_id,
                    stage="MIDI 小节线已调整",
                    midi_path=str(destination.resolve()),
                    midi_bar_offset_beats=bar_offset_beats,
                    storage_bytes=directory_size(job_dir),
                    operation_kind=None,
                    operation_state=OperationState.IDLE.value,
                    operation_stage=None,
                    operation_progress=100,
                    operation_error=None,
                    operation_finished_at=utc_now(),
                    cancel_requested=0,
                )
                return

            selected_transcriber = (
                self.strum_transcriber if midi_model == "strum" else self.transcriber
            )

            async def report(stage: str) -> None:
                current = self.repository.require(job_id)
                self._ensure_operation_not_cancelled(job_id)
                self.repository.update(
                    job_id,
                    operation_stage=stage,
                    operation_progress=min(90, max(10, current.operation_progress + 8)),
                )

            result = await selected_transcriber.transcribe(
                Path(record.drums_path),
                candidate,
                beat_source_path=Path(record.original_path),
                bar_offset_beats=bar_offset_beats,
                meter=meter,
                progress=report,
            )
            self._ensure_operation_not_cancelled(job_id)
            os.replace(candidate, destination)
            self.repository.update(
                job_id,
                stage=f"MIDI 已生成 · {result.engine} · {result.event_count} 个鼓点",
                midi_path=str(destination.resolve()),
                midi_event_count=result.event_count,
                midi_tempo_bpm=result.tempo_bpm,
                midi_engine=result.engine,
                midi_quantized=int(result.quantized),
                midi_warning="；".join(result.warnings) if result.warnings else None,
                midi_beats_per_bar=result.beats_per_bar,
                midi_beat_unit=result.beat_unit,
                midi_bar_offset_beats=result.bar_offset_beats,
                midi_model=midi_model,
                midi_meter=meter,
                storage_bytes=directory_size(job_dir),
                operation_kind=None,
                operation_state=OperationState.IDLE.value,
                operation_stage=None,
                operation_progress=100,
                operation_error=None,
                operation_finished_at=utc_now(),
                cancel_requested=0,
            )
        except MidiTranscriptionError as exc:
            raise JobConflictError(str(exc)) from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def delete(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        if (
            JobStatus(record.status) in ACTIVE_STATUSES
            or OperationState(record.operation_state) in ACTIVE_OPERATION_STATES
        ):
            raise JobConflictError("请先取消任务，再删除文件。")
        deleted = self.repository.delete(job_id)
        job_dir = self.settings.jobs_dir / job_id
        from app.audio import remove_job_directory

        remove_job_directory(job_dir, self.settings.jobs_dir)
        return deleted

    async def _worker(self) -> None:
        while True:
            request = await self.queue.get()
            self._current_request = request
            try:
                if request.kind == "separation":
                    await self._process(request.job_id)
                else:
                    await self._process_operation(request)
            finally:
                self._current_request = None
                self.queue.task_done()

    async def _process_operation(self, request: ComputeRequest) -> None:
        record = self.repository.get(request.job_id)
        if (
            not record
            or record.status != JobStatus.COMPLETED.value
            or record.operation_kind != request.kind
            or record.operation_state != OperationState.QUEUED.value
        ):
            return
        self.repository.update(
            request.job_id,
            operation_state=OperationState.RUNNING.value,
            operation_stage=(
                "正在生成 MIDI" if request.kind == "midi" else "正在准备压缩"
            ),
            operation_progress=5,
            operation_started_at=utc_now(),
            operation_finished_at=None,
            operation_error=None,
        )
        try:
            if request.kind == "midi":
                await self._run_midi(request.job_id)
            elif request.kind == "compress":
                await self._run_compress(request.job_id)
            else:
                raise JobConflictError("未知的计算操作。")
        except OperationCancelled:
            self.repository.update(
                request.job_id,
                operation_state=OperationState.CANCELLED.value,
                operation_stage="已取消",
                operation_progress=0,
                operation_error=None,
                operation_finished_at=utc_now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.repository.get(request.job_id)
            if current and (
                current.cancel_requested
                or current.operation_state == OperationState.CANCELLED.value
            ):
                self.repository.update(
                    request.job_id,
                    operation_state=OperationState.CANCELLED.value,
                    operation_stage="已取消",
                    operation_progress=0,
                    operation_error=None,
                    operation_finished_at=utc_now(),
                )
            else:
                self.repository.update(
                    request.job_id,
                    operation_state=OperationState.FAILED.value,
                    operation_stage="操作失败",
                    operation_progress=0,
                    operation_error=str(exc),
                    operation_finished_at=utc_now(),
                    cancel_requested=0,
                )

    async def _process(self, job_id: str) -> None:
        record = self.repository.get(job_id)
        if not record or record.status != JobStatus.QUEUED.value:
            return
        if record.cancel_requested:
            self.repository.update(
                job_id,
                status=JobStatus.CANCELLED.value,
                stage="已取消",
                finished_at=utc_now(),
                operation_state=OperationState.CANCELLED.value,
                operation_stage="已取消",
                operation_finished_at=utc_now(),
            )
            return

        job_dir = self.settings.jobs_dir / job_id
        selected_engine = self._engine_for(record)
        try:
            required_space = max(
                self.settings.min_free_disk_bytes,
                record.input_size_bytes * 4,
            )
            ensure_disk_space(self.settings.data_dir, required_space)
            self.repository.update(
                job_id,
                status=JobStatus.PREPROCESSING.value,
                stage="准备音频",
                progress=2,
                started_at=utc_now(),
                finished_at=None,
                error=None,
                operation_kind="separation",
                operation_state=OperationState.RUNNING.value,
                operation_stage="准备音频",
                operation_progress=2,
                operation_started_at=utc_now(),
                operation_finished_at=None,
                operation_error=None,
            )

            async def report(stage: str, value: int, message: str | None) -> None:
                current = self.repository.require(job_id)
                if current.cancel_requested:
                    await selected_engine.cancel(job_id)
                status = JobStatus(stage)
                self.repository.update(
                    job_id,
                    status=status.value,
                    stage=message or current.stage,
                    progress=max(0, min(value, 99)),
                    operation_stage=message or current.operation_stage,
                    operation_progress=max(0, min(value, 99)),
                )

            result = await selected_engine.separate(
                job_id=job_id,
                source_path=Path(record.original_path),
                job_dir=job_dir,
                progress=report,
            )
            total_size = record.input_size_bytes + result.storage_bytes
            result_paths = {
                "drums_path": str(result.drums_path.resolve()) if result.drums_path else None,
                "no_drums_path": str(result.no_drums_path.resolve()) if result.no_drums_path else None,
                "vocals_path": str(result.vocals_path.resolve()) if result.vocals_path else None,
                "instrumental_path": (
                    str(result.instrumental_path.resolve())
                    if result.instrumental_path
                    else None
                ),
            }
            self.repository.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="处理完成",
                progress=100,
                finished_at=utc_now(),
                error=None,
                warnings_json=json.dumps(result.warnings, ensure_ascii=False),
                **result_paths,
                storage_bytes=total_size,
                cancel_requested=0,
                operation_kind=None,
                operation_state=OperationState.IDLE.value,
                operation_stage=None,
                operation_progress=100,
                operation_error=None,
                operation_finished_at=utc_now(),
            )
        except SeparationCancelled:
            self.repository.update(
                job_id,
                status=JobStatus.CANCELLED.value,
                stage="已取消",
                progress=0,
                finished_at=utc_now(),
                error=None,
                storage_bytes=directory_size(job_dir),
                operation_state=OperationState.CANCELLED.value,
                operation_stage="已取消",
                operation_progress=0,
                operation_finished_at=utc_now(),
            )
        except asyncio.CancelledError:
            raise
        except (SeparationError, AudioValidationError) as exc:
            self.repository.update(
                job_id,
                status=JobStatus.FAILED.value,
                stage="处理失败",
                progress=0,
                finished_at=utc_now(),
                error=str(exc),
                storage_bytes=directory_size(job_dir),
                operation_state=OperationState.FAILED.value,
                operation_stage="处理失败",
                operation_progress=0,
                operation_error=str(exc),
                operation_finished_at=utc_now(),
            )
        except Exception as exc:  # pragma: no cover - final safety net
            self.repository.update(
                job_id,
                status=JobStatus.FAILED.value,
                stage="处理失败",
                progress=0,
                finished_at=utc_now(),
                error=f"未预期错误：{exc}",
                storage_bytes=directory_size(job_dir),
                operation_state=OperationState.FAILED.value,
                operation_stage="处理失败",
                operation_progress=0,
                operation_error=f"未预期错误：{exc}",
                operation_finished_at=utc_now(),
            )

    def _result_paths(self, record: JobRecord) -> tuple[str | None, str | None]:
        if record.separation_kind == "vocals":
            return record.vocals_path, record.instrumental_path
        return record.drums_path, record.no_drums_path

    def _ensure_operation_not_cancelled(self, job_id: str) -> None:
        record = self.repository.require(job_id)
        if (
            record.cancel_requested
            or record.operation_state == OperationState.CANCELLED.value
        ):
            raise OperationCancelled("操作已取消。")

    async def _terminate_current_process(self) -> None:
        process = self._current_process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        finally:
            if self._current_process is process:
                self._current_process = None

    def _engine_for(self, record: JobRecord) -> SeparationEngine:
        return self.vocal_engine if record.separation_kind == "vocals" else self.engine
