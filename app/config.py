from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    model_name: str = "htdemucs_ft"
    model_version: str = "demucs-4.0.1/htdemucs_ft"
    max_upload_bytes: int = 500 * 1024 * 1024
    max_duration_seconds: float = 15 * 60
    min_free_disk_bytes: int = 2 * 1024 * 1024 * 1024
    sample_rate: int = 44_100
    channels: int = 2
    sample_width: int = 2
    output_format: str = "mp3"
    output_bitrate: str = "256k"
    host: str = "127.0.0.1"
    port: int = 8765
    ffmpeg_path: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        data_dir = Path(
            os.environ.get("DRUM_SEPARATOR_DATA_DIR", project_root / "data")
        ).expanduser().resolve()
        return cls(
            project_root=project_root,
            data_dir=data_dir,
            host=os.environ.get("DRUM_SEPARATOR_HOST", "127.0.0.1"),
            port=int(os.environ.get("DRUM_SEPARATOR_PORT", "8765")),
            ffmpeg_path=os.environ.get("DRUM_SEPARATOR_FFMPEG"),
            output_format=os.environ.get("DRUM_SEPARATOR_OUTPUT_FORMAT", "mp3"),
            output_bitrate=os.environ.get("DRUM_SEPARATOR_OUTPUT_BITRATE", "256k"),
        )

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def static_dir(self) -> Path:
        return self.project_root / "app" / "static"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def resolve_ffmpeg(self) -> str:
        if self.ffmpeg_path:
            candidate = Path(self.ffmpeg_path).expanduser()
            if candidate.is_file():
                return str(candidate.resolve())
            located = shutil.which(self.ffmpeg_path)
            if located:
                return located
            raise RuntimeError(f"找不到指定的 FFmpeg：{self.ffmpeg_path}")

        located = shutil.which("ffmpeg")
        if located:
            return located

        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # pragma: no cover - only reached on broken install
            raise RuntimeError("找不到 FFmpeg，请重新运行项目安装脚本。") from exc
