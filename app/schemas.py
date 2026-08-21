from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    PREPROCESSING = "preprocessing"
    SEPARATING = "separating"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_OPERATION_STATES = {
    OperationState.QUEUED,
    OperationState.RUNNING,
}


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.PREPROCESSING,
    JobStatus.SEPARATING,
    JobStatus.POSTPROCESSING,
}


class AudioInfo(BaseModel):
    duration_seconds: float
    sample_rate: int | None = None
    channels: int | None = None
    format_name: str


class JobFiles(BaseModel):
    original: str | None = None
    drums: str | None = None
    no_drums: str | None = None
    vocals: str | None = None
    instrumental: str | None = None
    midi: str | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_name: str
    status: JobStatus
    stage: str
    progress: int
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    input_size_bytes: int
    storage_bytes: int
    model_name: str
    separation_kind: str = "drums"
    output_format: str | None = None
    midi_event_count: int = 0
    midi_tempo_bpm: float | None = None
    midi_engine: str | None = None
    midi_quantized: bool = False
    midi_warning: str | None = None
    midi_beats_per_bar: int | None = None
    midi_beat_unit: int = 4
    midi_bar_offset_beats: int = 0
    midi_model: str = "adtof"
    midi_meter: str = "auto"
    operation_kind: str | None = None
    operation_state: OperationState = OperationState.IDLE
    operation_stage: str | None = None
    operation_progress: int = 0
    operation_error: str | None = None
    operation_started_at: str | None = None
    operation_finished_at: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    files: JobFiles


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total_storage_bytes: int


class ActionResponse(BaseModel):
    ok: bool = True
    message: str
