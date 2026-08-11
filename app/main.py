from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Literal
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
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
from app.manager import JobConflictError, JobManager
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
        output_format=(
            Path(record.drums_path).suffix.lower().lstrip(".")
            if record.drums_path
            else None
        ),
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
        ),
    )


def _safe_job_file(settings: Settings, record: JobRecord, kind: str) -> tuple[Path, str, str]:
    if kind == "original":
        path = Path(record.original_path)
        filename = record.original_name
        media_type = record.mime_type or "application/octet-stream"
    elif kind == "drums" and record.drums_path:
        path = Path(record.drums_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-drums{suffix}"
        media_type = _audio_media_type(suffix)
    elif kind == "no_drums" and record.no_drums_path:
        path = Path(record.no_drums_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-no-drums{suffix}"
        media_type = _audio_media_type(suffix)
    else:
        raise HTTPException(status_code=404, detail="文件不存在。")

    resolved = path.resolve()
    jobs_root = settings.jobs_dir.resolve()
    if jobs_root not in resolved.parents or not resolved.is_file():
        raise HTTPException(status_code=404, detail="文件不存在。")
    return resolved, sanitize_display_name(filename), media_type


def _audio_media_type(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")


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
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    repository = JobRepository(settings.database_path)
    repository.initialize()
    selected_engine = engine or DemucsEngine(settings)
    manager = JobManager(settings, repository, selected_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="鼓点拆解室",
        version="0.1.0",
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
        return {"status": "ok", "model": settings.model_name}

    @app.post("/api/jobs", response_model=JobResponse, status_code=201)
    async def create_job(file: UploadFile = File(...)) -> JobResponse:
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
            cache_source = (
                f"{saved.sha256}:{settings.model_version}:two-stems=drums:"
                f"overlap=0.25:pcm16:{settings.sample_rate}:"
                f"output={settings.output_format}:{settings.output_bitrate}"
            )
            cache_key = hashlib.sha256(cache_source.encode()).hexdigest()
            reusable = repository.find_reusable(cache_key)
            if reusable:
                if reusable.status != JobStatus.COMPLETED.value or (
                    reusable.drums_path
                    and reusable.no_drums_path
                    and Path(reusable.drums_path).is_file()
                    and Path(reusable.no_drums_path).is_file()
                ):
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
                model_name=settings.model_name,
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
        file_kind: Literal["original", "drums", "no_drums"],
        download: bool = Query(False),
    ) -> Response:
        record = repository.require(job_id)
        path, filename, media_type = _safe_job_file(settings, record, file_kind)
        return ranged_file_response(request, path, filename, media_type, download)

    app.mount("/assets", StaticFiles(directory=settings.static_dir), name="assets")

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(settings.static_dir / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def javascript() -> FileResponse:
        return FileResponse(settings.static_dir / "app.js", media_type="text/javascript")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(settings.static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html")

    return app


app = create_app()
