from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.schemas import JobStatus


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
    midi_path: str | None
    midi_event_count: int
    midi_tempo_bpm: float | None
    cancel_requested: int

    @property
    def warnings(self) -> list[str]:
        try:
            value = json.loads(self.warnings_json or "[]")
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []


class JobRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
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
                    midi_path TEXT,
                    midi_event_count INTEGER NOT NULL DEFAULT 0,
                    midi_tempo_bpm REAL,
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
    ) -> JobRecord:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, original_name, original_path, mime_type,
                    input_size_bytes, storage_bytes, duration_seconds,
                    sample_rate, channels, sha256, cache_key, model_name,
                    status, stage, progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    JobStatus.QUEUED.value,
                    "等待处理",
                    0,
                    now,
                    now,
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
            "midi_path",
            "midi_event_count",
            "midi_tempo_bpm",
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

    def prepare_startup_recovery(self) -> list[str]:
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
                    error = ?, finished_at = ?, updated_at = ?
                WHERE status IN ({','.join('?' for _ in interrupted)})
                """,
                (
                    JobStatus.FAILED.value,
                    "处理被中断",
                    "应用在任务完成前退出，请点击重新处理。",
                    now,
                    now,
                    *interrupted,
                ),
            )
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at",
                (JobStatus.QUEUED.value,),
            ).fetchall()
        return [row["id"] for row in rows]

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
