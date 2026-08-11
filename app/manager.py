from __future__ import annotations

import asyncio
import json
import os
import shutil
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
from app.midi import DrumTranscriber, MidiTranscriptionError
from app.schemas import ACTIVE_STATUSES, JobStatus


class JobConflictError(RuntimeError):
    pass


class JobManager:
    def __init__(
        self,
        settings: Settings,
        repository: JobRepository,
        engine: SeparationEngine,
    ):
        self.settings = settings
        self.repository = repository
        self.engine = engine
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._current_job_id: str | None = None
        self._stopping = False
        self._compression_lock = asyncio.Lock()
        self._midi_lock = asyncio.Lock()
        self.transcriber = DrumTranscriber(settings)

    async def start(self) -> None:
        self._stopping = False
        for job_id in self.repository.prepare_startup_recovery():
            self.queue.put_nowait(job_id)
        self._worker_task = asyncio.create_task(self._worker(), name="drum-separator-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._current_job_id:
            await self.engine.cancel(self._current_job_id)
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def cancel(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        status = JobStatus(record.status)
        if status == JobStatus.QUEUED:
            return self.repository.update(
                job_id,
                status=JobStatus.CANCELLED.value,
                stage="已取消",
                finished_at=utc_now(),
                cancel_requested=1,
            )
        if status in ACTIVE_STATUSES:
            self.repository.update(
                job_id,
                stage="正在取消",
                cancel_requested=1,
            )
            await self.engine.cancel(job_id)
            return self.repository.require(job_id)
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
            midi_path=None,
            midi_event_count=0,
            midi_tempo_bpm=None,
            storage_bytes=record.input_size_bytes,
            cancel_requested=0,
        )
        await self.enqueue(job_id)
        return updated

    async def compress(self, job_id: str) -> JobRecord:
        async with self._compression_lock:
            record = self.repository.require(job_id)
            if JobStatus(record.status) != JobStatus.COMPLETED:
                raise JobConflictError("只有已完成的任务可以压缩。")
            if not record.drums_path or not record.no_drums_path:
                raise JobConflictError("任务结果文件不完整。")

            sources = [Path(record.drums_path), Path(record.no_drums_path)]
            if all(path.suffix.lower() == ".mp3" for path in sources):
                return record
            if any(path.suffix.lower() != ".wav" or not path.is_file() for path in sources):
                raise JobConflictError("当前结果无法转换为轻量 MP3。")

            output_dir = (self.settings.jobs_dir / job_id / "outputs").resolve()
            if any(output_dir not in path.resolve().parents for path in sources):
                raise JobConflictError("结果文件路径异常。")

            ensure_disk_space(self.settings.data_dir, sum(path.stat().st_size for path in sources))
            destinations = [output_dir / "drums.mp3", output_dir / "no_drums.mp3"]
            temporary = [path.with_suffix(".mp3.part") for path in destinations]
            ffmpeg = self.settings.resolve_ffmpeg()
            try:
                for source, part_path in zip(sources, temporary, strict=True):
                    part_path.unlink(missing_ok=True)
                    process = await asyncio.create_subprocess_exec(
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
                    _, stderr = await process.communicate()
                    if process.returncode != 0:
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

                for part_path, destination in zip(temporary, destinations, strict=True):
                    os.replace(part_path, destination)
                total_size = record.input_size_bytes + sum(
                    path.stat().st_size for path in destinations
                )
                if record.midi_path and Path(record.midi_path).is_file():
                    total_size += Path(record.midi_path).stat().st_size
                updated = self.repository.update(
                    job_id,
                    drums_path=str(destinations[0]),
                    no_drums_path=str(destinations[1]),
                    storage_bytes=total_size,
                    stage="已压缩为轻量 MP3",
                )
                for source in sources:
                    source.unlink(missing_ok=True)
                return updated
            except AudioValidationError as exc:
                raise JobConflictError(str(exc)) from exc
            finally:
                for part_path in temporary:
                    part_path.unlink(missing_ok=True)

    async def generate_midi(self, job_id: str) -> JobRecord:
        async with self._midi_lock:
            record = self.repository.require(job_id)
            if JobStatus(record.status) != JobStatus.COMPLETED:
                raise JobConflictError("只有已完成分轨的任务可以生成 MIDI。")
            if record.midi_path and Path(record.midi_path).is_file():
                return record
            if not record.drums_path or not Path(record.drums_path).is_file():
                raise JobConflictError("鼓轨文件不存在。")

            job_dir = self.settings.jobs_dir / job_id
            destination = job_dir / "outputs" / "drums.mid"
            try:
                result = await self.transcriber.transcribe(
                    Path(record.drums_path), destination
                )
            except MidiTranscriptionError as exc:
                raise JobConflictError(str(exc)) from exc
            return self.repository.update(
                job_id,
                stage=f"MIDI 已生成 · {result.event_count} 个鼓点",
                midi_path=str(result.path.resolve()),
                midi_event_count=result.event_count,
                midi_tempo_bpm=result.tempo_bpm,
                storage_bytes=directory_size(job_dir),
            )

    async def delete(self, job_id: str) -> JobRecord:
        record = self.repository.require(job_id)
        if JobStatus(record.status) in ACTIVE_STATUSES:
            raise JobConflictError("请先取消任务，再删除文件。")
        deleted = self.repository.delete(job_id)
        job_dir = self.settings.jobs_dir / job_id
        from app.audio import remove_job_directory

        remove_job_directory(job_dir, self.settings.jobs_dir)
        return deleted

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id)
            finally:
                self.queue.task_done()

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
            )
            return

        self._current_job_id = job_id
        job_dir = self.settings.jobs_dir / job_id
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
            )

            async def report(stage: str, value: int, message: str | None) -> None:
                current = self.repository.require(job_id)
                if current.cancel_requested:
                    await self.engine.cancel(job_id)
                status = JobStatus(stage)
                self.repository.update(
                    job_id,
                    status=status.value,
                    stage=message or current.stage,
                    progress=max(0, min(value, 99)),
                )

            result = await self.engine.separate(
                job_id=job_id,
                source_path=Path(record.original_path),
                job_dir=job_dir,
                progress=report,
            )
            total_size = record.input_size_bytes + result.storage_bytes
            self.repository.update(
                job_id,
                status=JobStatus.COMPLETED.value,
                stage="处理完成",
                progress=100,
                finished_at=utc_now(),
                error=None,
                warnings_json=json.dumps(result.warnings, ensure_ascii=False),
                drums_path=str(result.drums_path.resolve()),
                no_drums_path=str(result.no_drums_path.resolve()),
                storage_bytes=total_size,
                cancel_requested=0,
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
            )
        finally:
            self._current_job_id = None
