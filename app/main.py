from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Literal
from urllib.parse import quote
from uuid import uuid4

import mido
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.audio import (
    ALLOWED_SUFFIXES,
    AudioValidationError,
    assert_decodable,
    ensure_disk_space,
    probe_audio,
    remove_job_directory,
    sanitize_display_name,
    save_upload,
)
from app.config import Settings
from app.db import JobRecord, JobRepository
from app.engine import DemucsEngine, SeparationEngine
from app.job_files import (
    JobFileNotFound,
    audio_media_type as _audio_media_type,
    resolve_job_file,
)
from app.manager import JobConflictError, JobManager
from app.midi import DrumTranscriber
from app.strum import StrumTranscriber
from app.vocal_engine import MelBandRoformerEngine
from app.schemas import (
    ActionResponse,
    JobFiles,
    JobListResponse,
    JobResponse,
    JobStatus,
)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _elapsed(record: JobRecord) -> float:
    started = _parse_time(record.started_at)
    if not started:
        return 0.0
    finished = _parse_time(record.finished_at) or datetime.now(UTC)
    return max(0.0, (finished - started).total_seconds())


def _file_url(job_id: str, kind: str, exists: bool) -> str | None:
    return f"/api/jobs/{job_id}/files/{kind}" if exists else None


def job_response(record: JobRecord) -> JobResponse:
    original_exists = Path(record.original_path).is_file()
    drums_exists = bool(record.drums_path and Path(record.drums_path).is_file())
    no_drums_exists = bool(record.no_drums_path and Path(record.no_drums_path).is_file())
    vocals_exists = bool(record.vocals_path and Path(record.vocals_path).is_file())
    instrumental_exists = bool(
        record.instrumental_path and Path(record.instrumental_path).is_file()
    )
    midi_exists = bool(record.midi_path and Path(record.midi_path).is_file())
    return JobResponse(
        id=record.id,
        original_name=record.original_name,
        status=JobStatus(record.status),
        stage=record.stage,
        progress=record.progress,
        duration_seconds=record.duration_seconds,
        sample_rate=record.sample_rate,
        channels=record.channels,
        input_size_bytes=record.input_size_bytes,
        storage_bytes=record.storage_bytes,
        model_name=record.model_name,
        separation_kind=record.separation_kind,
        output_format=(
            Path(
                record.drums_path
                or record.vocals_path
                or record.no_drums_path
                or record.instrumental_path
            ).suffix.lower().lstrip(".")
            if (
                record.drums_path
                or record.vocals_path
                or record.no_drums_path
                or record.instrumental_path
            )
            else None
        ),
        midi_event_count=record.midi_event_count,
        midi_tempo_bpm=record.midi_tempo_bpm,
        midi_engine=record.midi_engine,
        midi_quantized=bool(record.midi_quantized),
        midi_warning=record.midi_warning,
        midi_beats_per_bar=record.midi_beats_per_bar,
        midi_beat_unit=record.midi_beat_unit,
        midi_bar_offset_beats=record.midi_bar_offset_beats,
        midi_model=record.midi_model,
        midi_meter=record.midi_meter,
        operation_kind=record.operation_kind,
        operation_state=record.operation_state,
        operation_stage=record.operation_stage,
        operation_progress=record.operation_progress,
        operation_error=record.operation_error,
        operation_started_at=record.operation_started_at,
        operation_finished_at=record.operation_finished_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        elapsed_seconds=_elapsed(record),
        error=record.error,
        warnings=record.warnings,
        files=JobFiles(
            original=_file_url(record.id, "original", original_exists),
            drums=_file_url(record.id, "drums", drums_exists),
            no_drums=_file_url(record.id, "no_drums", no_drums_exists),
            vocals=_file_url(record.id, "vocals", vocals_exists),
            instrumental=_file_url(
                record.id, "instrumental", instrumental_exists
            ),
            midi=_file_url(record.id, "midi", midi_exists),
        ),
    )


def _safe_job_file(settings: Settings, record: JobRecord, kind: str) -> tuple[Path, str, str]:
    try:
        return resolve_job_file(settings, record, kind)
    except JobFileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _parse_range(range_header: str, size: int) -> tuple[int, int]:
    if not range_header.startswith("bytes=") or "," in range_header:
        raise ValueError
    start_text, end_text = range_header[6:].split("-", 1)
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError
        return max(0, size - suffix), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError
    return start, min(end, size - 1)


def ranged_file_response(
    request: Request,
    path: Path,
    filename: str,
    media_type: str,
    download: bool,
) -> Response:
    size = path.stat().st_size
    disposition = "attachment" if download else "inline"
    encoded_name = quote(filename)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
    }
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, filename=filename if download else None, headers=headers)

    try:
        start, end = _parse_range(range_header, size)
    except (ValueError, TypeError):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    length = end - start + 1
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        }
    )

    async def stream() -> AsyncIterator[bytes]:
        remaining = length
        with path.open("rb") as source:
            source.seek(start)
            while remaining > 0:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=206, media_type=media_type, headers=headers)


def create_app(
    settings: Settings | None = None,
    engine: SeparationEngine | None = None,
    vocal_engine: SeparationEngine | None = None,
    transcriber: DrumTranscriber | None = None,
    strum_transcriber: StrumTranscriber | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    repository = JobRepository(settings.database_path)
    repository.initialize()
    selected_engine = engine or DemucsEngine(settings)
    selected_vocal_engine = vocal_engine or (
        selected_engine if engine is not None else MelBandRoformerEngine(settings)
    )
    manager = JobManager(
        settings,
        repository,
        selected_engine,
        vocal_engine=selected_vocal_engine,
        transcriber=transcriber,
        strum_transcriber=strum_transcriber,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="鼓点拆解室",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.manager = manager

    @app.exception_handler(KeyError)
    async def handle_missing_job(_: Request, __: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "任务不存在。"})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "drum_model": settings.model_name,
            "vocal_model": settings.vocal_model_name,
        }

    @app.post("/api/jobs", response_model=JobResponse, status_code=201)
    async def create_job(
        file: UploadFile = File(...),
        separation_kind: Literal["drums", "vocals"] = Form("drums"),
    ) -> JobResponse:
        display_name = sanitize_display_name(file.filename)
        suffix = Path(display_name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail="仅支持 WAV、MP3、FLAC、M4A 和 OGG。",
            )
        ensure_disk_space(settings.data_dir, settings.min_free_disk_bytes)
        job_id = uuid4().hex
        job_dir = settings.jobs_dir / job_id
        upload_temp = job_dir / "input" / "upload.part"
        try:
            saved = await save_upload(file, upload_temp, settings.max_upload_bytes)
            original_path = job_dir / "input" / f"original.{saved.format_name}"
            os.replace(upload_temp, original_path)
            info = await probe_audio(settings.resolve_ffmpeg(), original_path)
            if info.duration_seconds > settings.max_duration_seconds:
                raise AudioValidationError("音频超过 15 分钟限制。")
            await assert_decodable(settings.resolve_ffmpeg(), original_path)
            if separation_kind == "vocals":
                model_name = settings.vocal_model_name
                engine_version = settings.vocal_model_version
                engine_parameters = "two-stems=vocals+instrumental:cpu"
            else:
                model_name = settings.model_name
                engine_version = settings.model_version
                engine_parameters = "two-stems=drums:overlap=0.25:cpu"
            cache_source = (
                f"{saved.sha256}:{separation_kind}:{engine_version}:"
                f"{engine_parameters}:pcm16:{settings.sample_rate}:"
                f"output={settings.output_format}:{settings.output_bitrate}"
            )
            cache_key = hashlib.sha256(cache_source.encode()).hexdigest()
            reusable = repository.find_reusable(cache_key)
            if reusable:
                if separation_kind == "vocals":
                    complete_files = (
                        reusable.vocals_path
                        and reusable.instrumental_path
                        and Path(reusable.vocals_path).is_file()
                        and Path(reusable.instrumental_path).is_file()
                    )
                else:
                    complete_files = (
                        reusable.drums_path
                        and reusable.no_drums_path
                        and Path(reusable.drums_path).is_file()
                        and Path(reusable.no_drums_path).is_file()
                    )
                if reusable.status != JobStatus.COMPLETED.value or complete_files:
                    remove_job_directory(job_dir, settings.jobs_dir)
                    return job_response(reusable)

            record = repository.create(
                job_id=job_id,
                original_name=display_name,
                original_path=str(original_path.resolve()),
                mime_type=saved.mime_type,
                input_size_bytes=saved.size_bytes,
                duration_seconds=info.duration_seconds,
                sample_rate=info.sample_rate,
                channels=info.channels,
                sha256=saved.sha256,
                cache_key=cache_key,
                model_name=model_name,
                separation_kind=separation_kind,
            )
            await manager.enqueue(job_id)
            return job_response(record)
        except AudioValidationError as exc:
            if job_dir.exists():
                remove_job_directory(job_dir, settings.jobs_dir)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            if job_dir.exists() and not repository.get(job_id):
                remove_job_directory(job_dir, settings.jobs_dir)
            raise

    @app.get("/api/jobs", response_model=JobListResponse)
    async def list_jobs() -> JobListResponse:
        records = repository.list()
        return JobListResponse(
            jobs=[job_response(record) for record in records],
            total_storage_bytes=repository.total_storage_bytes(),
        )

    @app.get("/api/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> JobResponse:
        return job_response(repository.require(job_id))

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(job_id: str) -> JobResponse:
        try:
            return job_response(await manager.cancel(job_id))
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/retry", response_model=JobResponse)
    async def retry_job(job_id: str) -> JobResponse:
        try:
            return job_response(await manager.retry(job_id))
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/compress", response_model=JobResponse)
    async def compress_job(job_id: str) -> JobResponse:
        try:
            return job_response(await manager.compress(job_id))
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/midi", response_model=JobResponse)
    async def generate_midi(
        job_id: str,
        force: bool = Query(False),
        bar_offset_beats: int = Query(0, ge=-2, le=2),
        midi_model: Literal["adtof", "strum"] = Query("adtof"),
        meter: Literal[
            "auto", "2/4", "3/4", "4/4", "6/8", "9/8", "12/8"
        ] = Query("auto"),
    ) -> JobResponse:
        try:
            return job_response(
                await manager.generate_midi(
                    job_id,
                    force=force,
                    bar_offset_beats=bar_offset_beats,
                    midi_model=midi_model,
                    meter=meter,
                )
            )
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/midi/preview")
    async def preview_midi(
        job_id: str,
        bars: int = Query(4, ge=1, le=8),
        start_bar: int | None = Query(None, ge=0),
    ) -> dict[str, object]:
        record = repository.require(job_id)
        if record.separation_kind != "drums":
            raise HTTPException(status_code=409, detail="人声分轨任务没有鼓点 MIDI。")
        if not record.midi_path or not Path(record.midi_path).is_file():
            raise HTTPException(status_code=404, detail="请先生成鼓点 MIDI。")
        return build_midi_preview(
            Path(record.midi_path),
            record,
            bars=bars,
            requested_start_bar=start_bar,
        )

    @app.delete("/api/jobs/{job_id}", response_model=ActionResponse)
    async def delete_job(job_id: str) -> ActionResponse:
        try:
            await manager.delete(job_id)
            return ActionResponse(message="任务和本地文件已删除。")
        except JobConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/files/{file_kind}")
    async def get_job_file(
        request: Request,
        job_id: str,
        file_kind: Literal[
            "original", "drums", "no_drums", "vocals", "instrumental", "midi"
        ],
        download: bool = Query(False),
    ) -> Response:
        record = repository.require(job_id)
        path, filename, media_type = _safe_job_file(settings, record, file_kind)
        return ranged_file_response(request, path, filename, media_type, download)

    app.mount("/assets", StaticFiles(directory=settings.static_dir), name="assets")

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(
            settings.static_dir / "styles.css",
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/app.js", include_in_schema=False)
    async def javascript() -> FileResponse:
        return FileResponse(
            settings.static_dir / "app.js",
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(
            settings.static_dir / "favicon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            settings.static_dir / "index.html",
            headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
        )

    return app


_DRUM_LANES = (
    ("crash", "吊镲", {49, 52, 55, 57}),
    ("ride", "叮叮镲", {51, 53, 59}),
    ("hihat", "踩镲", {42, 44, 46}),
    ("high_tom", "高通鼓", {48, 50}),
    ("mid_tom", "中通鼓", {45, 47}),
    ("snare", "军鼓", {37, 38, 39, 40}),
    ("floor_tom", "落地通鼓", {41, 43}),
    ("kick", "底鼓", {35, 36}),
)


def build_midi_preview(
    path: Path,
    record: JobRecord,
    *,
    bars: int = 4,
    requested_start_bar: int | None = None,
) -> dict[str, object]:
    midi = mido.MidiFile(path)
    ticks_per_beat = midi.ticks_per_beat or 480
    notes: list[tuple[int, int, int]] = []
    for track in midi.tracks:
        absolute_tick = 0
        for message in track:
            absolute_tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                notes.append((absolute_tick, message.note, message.velocity))
    notes.sort()
    if not notes:
        return {
            "ticks_per_beat": ticks_per_beat,
            "beats_per_bar": record.midi_beats_per_bar or 4,
            "beat_unit": record.midi_beat_unit or 4,
            "start_bar": 1,
            "min_start_bar": 1,
            "max_start_bar": 1,
            "total_bars": 1,
            "bar_count": bars,
            "notes": [],
            "lanes": [
                {"id": lane_id, "label": label}
                for lane_id, label, _ in _DRUM_LANES
            ],
        }

    beats_per_bar = record.midi_beats_per_bar or 4
    beat_unit = record.midi_beat_unit or 4
    unit_ticks = ticks_per_beat * 4 / beat_unit
    bar_ticks = round(beats_per_bar * unit_ticks)
    pickup_ticks = (
        record.midi_bar_offset_beats % beats_per_bar
    ) * unit_ticks
    pickup_ticks = round(pickup_ticks)
    first_tick = notes[0][0]
    last_tick = notes[-1][0]
    min_start_bar = 0 if pickup_ticks else 1
    last_bar = (
        0
        if pickup_ticks and last_tick < pickup_ticks
        else max(1, ((last_tick - pickup_ticks) // bar_ticks) + 1)
    )
    total_bars = max(1, last_bar + (1 if pickup_ticks else 0))
    max_start_bar = max(min_start_bar, last_bar - bars + 1)

    if requested_start_bar is not None:
        start_bar = max(min_start_bar, min(requested_start_bar, max_start_bar))
        start_tick = (
            0
            if start_bar == 0
            else pickup_ticks + (start_bar - 1) * bar_ticks
        )
    elif pickup_ticks and first_tick < pickup_ticks:
        start_tick = 0
        start_bar = 0
    else:
        origin = pickup_ticks
        bar_index = max(0, (first_tick - origin) // bar_ticks)
        start_tick = origin + bar_index * bar_ticks
        start_bar = bar_index + 1
    end_tick = start_tick + bars * bar_ticks

    def lane_for(note: int) -> str:
        for lane_id, _, note_numbers in _DRUM_LANES:
            if note in note_numbers:
                return lane_id
        return "snare"

    visible_notes = [
        {
            "tick": tick - start_tick,
            "note": note,
            "velocity": velocity,
            "lane": lane_for(note),
        }
        for tick, note, velocity in notes
        if start_tick <= tick < end_tick
    ]
    return {
        "ticks_per_beat": ticks_per_beat,
        "beats_per_bar": beats_per_bar,
        "beat_unit": beat_unit,
        "start_bar": start_bar,
        "min_start_bar": min_start_bar,
        "max_start_bar": max_start_bar,
        "total_bars": total_bars,
        "bar_count": bars,
        "notes": visible_notes,
        "lanes": [
            {"id": lane_id, "label": label}
            for lane_id, label, _ in _DRUM_LANES
        ],
    }


app = create_app()
