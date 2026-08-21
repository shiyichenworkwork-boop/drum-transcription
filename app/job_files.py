from __future__ import annotations

from pathlib import Path

from app.audio import sanitize_display_name
from app.config import Settings
from app.db import JobRecord


FILE_KINDS = {
    "original",
    "drums",
    "no_drums",
    "vocals",
    "instrumental",
    "midi",
}


class JobFileNotFound(FileNotFoundError):
    pass


def audio_media_type(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")


def resolve_job_file(
    settings: Settings,
    record: JobRecord,
    kind: str,
) -> tuple[Path, str, str]:
    if kind not in FILE_KINDS:
        raise JobFileNotFound("文件类型无效。")

    if kind == "original":
        path = Path(record.original_path)
        filename = record.original_name
        media_type = record.mime_type or "application/octet-stream"
    elif kind == "drums" and record.drums_path:
        path = Path(record.drums_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-drums{suffix}"
        media_type = audio_media_type(suffix)
    elif kind == "no_drums" and record.no_drums_path:
        path = Path(record.no_drums_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-no-drums{suffix}"
        media_type = audio_media_type(suffix)
    elif kind == "vocals" and record.vocals_path:
        path = Path(record.vocals_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-vocals{suffix}"
        media_type = audio_media_type(suffix)
    elif kind == "instrumental" and record.instrumental_path:
        path = Path(record.instrumental_path)
        suffix = path.suffix.lower()
        filename = f"{Path(record.original_name).stem}-instrumental{suffix}"
        media_type = audio_media_type(suffix)
    elif kind == "midi" and record.midi_path:
        path = Path(record.midi_path)
        filename = f"{Path(record.original_name).stem}-drums.mid"
        media_type = "audio/midi"
    else:
        raise JobFileNotFound("文件不存在。")

    resolved = path.resolve()
    jobs_root = settings.jobs_dir.resolve()
    if jobs_root not in resolved.parents or not resolved.is_file():
        raise JobFileNotFound("文件不存在。")
    return resolved, sanitize_display_name(filename), media_type
