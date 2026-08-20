from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.schemas import JobStatus, OperationState


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class JobRecord:
    id: str
    original_name: str
    original_path: str
    mime_type: str
    input_size_bytes: int
    storage_bytes: int
    duration_seconds: float | None
    sample_rate: int | None
    channels: int | None
    sha256: str
    cache_key: str
    model_name: str
    separation_kind: str
    status: str
    stage: str
    progress: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    warnings_json: str
    drums_path: str | None
    no_drums_path: str | None
    vocals_path: str | None
    instrumental_path: str | None
    midi_path: str | None
    midi_event_count: int
    midi_tempo_bpm: float | None
    midi_engine: str | None
    midi_quantized: int
    midi_warning: str | None
    midi_beats_per_bar: int | None
    midi_beat_unit: int
    midi_bar_offset_beats: int
    midi_model: str
    midi_meter: str
    operation_kind: str | None
    operation_state: str
    operation_stage: str | None
    operation_progress: int
    operation_params_json: str
    operation_error: str | None
    operation_started_at: str | None
    operation_finished_at: str | None
    cancel_requested: int

    @property
    def warnings(self) -> list[str]:
        try:
            value = json.loads(self.warnings_json or "[]")
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []

    @property
    def operation_params(self) -> dict[str, Any]:
        try:
            value = json.loads(self.operation_params_json or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


class JobRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    input_size_bytes INTEGER NOT NULL,
                    storage_bytes INTEGER NOT NULL,
                    duration_seconds REAL,
                    sample_rate INTEGER,
                    channels INTEGER,
                    sha256 TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    separation_kind TEXT NOT NULL DEFAULT 'drums',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    drums_path TEXT,
                    no_drums_path TEXT,
                    vocals_path TEXT,
                    instrumental_path TEXT,
                    midi_path TEXT,
                    midi_event_count INTEGER NOT NULL DEFAULT 0,
                    midi_tempo_bpm REAL,
                    midi_engine TEXT,
                    midi_quantized INTEGER NOT NULL DEFAULT 0,
                    midi_warning TEXT,
                    midi_beats_per_bar INTEGER,
                    midi_beat_unit INTEGER NOT NULL DEFAULT 4,
                    midi_bar_offset_beats INTEGER NOT NULL DEFAULT 0,
                    midi_model TEXT NOT NULL DEFAULT 'adtof',
                    midi_meter TEXT NOT NULL DEFAULT 'auto',
                    operation_kind TEXT,
                    operation_state TEXT NOT NULL DEFAULT 'idle',
                    operation_stage TEXT,
                    operation_progress INTEGER NOT NULL DEFAULT 0,
                    operation_params_json TEXT NOT NULL DEFAULT '{}',
                    operation_error TEXT,
                    operation_started_at TEXT,
                    operation_finished_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_created_at
                    ON jobs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_cache_key
                    ON jobs(cache_key, status);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "midi_path" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN midi_path TEXT")
            if "midi_event_count" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_event_count INTEGER NOT NULL DEFAULT 0"
                )
            if "midi_tempo_bpm" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN midi_tempo_bpm REAL")
            if "midi_engine" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN midi_engine TEXT")
            if "midi_quantized" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_quantized INTEGER NOT NULL DEFAULT 0"
                )
            if "midi_warning" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN midi_warning TEXT")
            if "midi_beats_per_bar" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN midi_beats_per_bar INTEGER")
            if "midi_beat_unit" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_beat_unit INTEGER NOT NULL DEFAULT 4"
                )
            if "midi_bar_offset_beats" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_bar_offset_beats INTEGER NOT NULL DEFAULT 0"
                )
            if "midi_model" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_model TEXT NOT NULL DEFAULT 'adtof'"
                )
            if "midi_meter" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN midi_meter TEXT NOT NULL DEFAULT 'auto'"
                )
            if "separation_kind" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN separation_kind TEXT NOT NULL DEFAULT 'drums'"
                )
            if "vocals_path" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN vocals_path TEXT")
            if "instrumental_path" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN instrumental_path TEXT")
            operation_columns = {
                "operation_kind": "TEXT",
                "operation_state": "TEXT NOT NULL DEFAULT 'idle'",
                "operation_stage": "TEXT",
                "operation_progress": "INTEGER NOT NULL DEFAULT 0",
                "operation_params_json": "TEXT NOT NULL DEFAULT '{}'",
                "operation_error": "TEXT",
                "operation_started_at": "TEXT",
                "operation_finished_at": "TEXT",
            }
            for name, definition in operation_columns.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE jobs ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                UPDATE jobs
                SET operation_kind = 'separation', operation_state = 'queued',
                    operation_stage = stage, operation_progress = progress
                WHERE status = ? AND operation_state = 'idle'
                """,
                (JobStatus.QUEUED.value,),
            )

    def create(
        self,
        *,
        job_id: str,
        original_name: str,
        original_path: str,
        mime_type: str,
        input_size_bytes: int,
        duration_seconds: float,
        sample_rate: int | None,
        channels: int | None,
        sha256: str,
        cache_key: str,
        model_name: str,
        separation_kind: str = "drums",
    ) -> JobRecord:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, original_name, original_path, mime_type,
                    input_size_bytes, storage_bytes, duration_seconds,
                    sample_rate, channels, sha256, cache_key, model_name,
                    separation_kind, status, stage, progress, created_at, updated_at,
                    operation_kind, operation_state, operation_stage, operation_progress
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    original_name,
                    original_path,
                    mime_type,
                    input_size_bytes,
                    input_size_bytes,
                    duration_seconds,
                    sample_rate,
                    channels,
                    sha256,
                    cache_key,
                    model_name,
                    separation_kind,
                    JobStatus.QUEUED.value,
                    "等待处理",
                    0,
                    now,
                    now,
                    "separation",
                    OperationState.QUEUED.value,
                    "等待处理",
                    0,
                ),
            )
        return self.require(job_id)

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return JobRecord(**dict(row)) if row else None

    def require(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if not record:
            raise KeyError(job_id)
        return record

    def list(self, limit: int = 100) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [JobRecord(**dict(row)) for row in rows]

    def update(self, job_id: str, **fields: Any) -> JobRecord:
        allowed = {
            "status",
            "stage",
            "progress",
            "updated_at",
            "started_at",
            "finished_at",
            "error",
            "warnings_json",
            "drums_path",
            "no_drums_path",
            "vocals_path",
            "instrumental_path",
            "midi_path",
            "midi_event_count",
            "midi_tempo_bpm",
            "midi_engine",
            "midi_quantized",
            "midi_warning",
            "midi_beats_per_bar",
            "midi_beat_unit",
            "midi_bar_offset_beats",
            "midi_model",
            "midi_meter",
            "operation_kind",
            "operation_state",
            "operation_stage",
            "operation_progress",
            "operation_params_json",
            "operation_error",
            "operation_started_at",
            "operation_finished_at",
            "storage_bytes",
            "cancel_requested",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"不允许更新字段：{', '.join(sorted(invalid))}")
        if not fields:
            return self.require(job_id)
        fields.setdefault("updated_at", utc_now())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [job_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.require(job_id)

    def delete(self, job_id: str) -> JobRecord:
        record = self.require(job_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return record

    def find_reusable(self, cache_key: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE cache_key = ?
                  AND status IN (?, ?, ?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    cache_key,
                    JobStatus.COMPLETED.value,
                    JobStatus.QUEUED.value,
                    JobStatus.PREPROCESSING.value,
                    JobStatus.SEPARATING.value,
                    JobStatus.POSTPROCESSING.value,
                ),
            ).fetchone()
        return JobRecord(**dict(row)) if row else None

    def prepare_startup_recovery(self) -> list[tuple[str, str]]:
        now = utc_now()
        interrupted = (
            JobStatus.PREPROCESSING.value,
            JobStatus.SEPARATING.value,
            JobStatus.POSTPROCESSING.value,
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE jobs
                SET status = ?, stage = ?, progress = 0,
                    error = ?, finished_at = ?, updated_at = ?,
                    operation_kind = 'separation', operation_state = ?,
                    operation_stage = ?, operation_progress = 0,
                    operation_error = ?, operation_finished_at = ?
                WHERE status IN ({','.join('?' for _ in interrupted)})
                """,
                (
                    JobStatus.FAILED.value,
                    "处理被中断",
                    "应用在任务完成前退出，请点击重新处理。",
                    now,
                    now,
                    OperationState.FAILED.value,
                    "处理被中断",
                    "应用在任务完成前退出。",
                    now,
                    *interrupted,
                ),
            )
            connection.execute(
                """
                UPDATE jobs
                SET operation_state = ?, operation_stage = ?,
                    operation_error = ?, operation_finished_at = ?, updated_at = ?
                WHERE operation_state = ?
                  AND status NOT IN (?, ?, ?)
                """,
                (
                    OperationState.FAILED.value,
                    "操作被中断",
                    "应用在操作完成前退出，请重新执行。",
                    now,
                    now,
                    OperationState.RUNNING.value,
                    *interrupted,
                ),
            )
            rows = connection.execute(
                """
                SELECT id, COALESCE(operation_kind, 'separation') AS operation_kind
                FROM jobs
                WHERE (status = ? AND operation_state = ?)
                   OR (status = ? AND operation_state = ? AND operation_kind IN ('midi', 'compress'))
                ORDER BY created_at
                """,
                (
                    JobStatus.QUEUED.value,
                    OperationState.QUEUED.value,
                    JobStatus.COMPLETED.value,
                    OperationState.QUEUED.value,
                ),
            ).fetchall()
        return [(row["id"], row["operation_kind"]) for row in rows]

    def total_storage_bytes(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(storage_bytes), 0) AS total FROM jobs"
            ).fetchone()
        return int(row["total"])

    def ids_with_statuses(self, statuses: Iterable[JobStatus]) -> list[str]:
        values = [status.value for status in statuses]
        if not values:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id FROM jobs WHERE status IN ({','.join('?' for _ in values)})",
                values,
            ).fetchall()
        return [row["id"] for row in rows]
