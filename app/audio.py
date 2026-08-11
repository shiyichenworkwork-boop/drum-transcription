from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import filetype
import numpy as np
from fastapi import UploadFile

from app.schemas import AudioInfo


ALLOWED_FORMATS = {"wav", "mp3", "flac", "m4a", "ogg"}
ALLOWED_SUFFIXES = {f".{extension}" for extension in ALLOWED_FORMATS}
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
_SAMPLE_RATE_RE = re.compile(r"(\d+)\s*Hz")


class AudioValidationError(ValueError):
    pass


@dataclass(slots=True)
class SavedUpload:
    path: Path
    size_bytes: int
    sha256: str
    format_name: str
    mime_type: str


@dataclass(slots=True)
class WavValidation:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    peak: int
    warnings: list[str]


def mp3_encode_args(
    ffmpeg_path: str,
    source_path: Path,
    destination: Path,
    *,
    sample_rate: int,
    channels: int,
    bitrate: str,
) -> list[str]:
    return [
        ffmpeg_path,
        "-y",
        "-v",
        "error",
        "-i",
        str(source_path),
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-write_xing",
        "1",
        "-f",
        "mp3",
        str(destination),
    ]


def sanitize_display_name(name: str | None) -> str:
    if not name:
        return "未命名音频"
    return Path(name.replace("\\", "/")).name[:240] or "未命名音频"


async def save_upload(
    upload: UploadFile,
    destination: Path,
    max_bytes: int,
) -> SavedUpload:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise AudioValidationError("文件超过 500MB 限制。")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        destination.unlink(missing_ok=True)
        raise AudioValidationError("上传的文件为空。")

    kind = filetype.guess(str(destination))
    if not kind or kind.extension.lower() not in ALLOWED_FORMATS:
        destination.unlink(missing_ok=True)
        raise AudioValidationError("文件内容不是受支持的音频格式。")

    return SavedUpload(
        path=destination,
        size_bytes=size,
        sha256=digest.hexdigest(),
        format_name=kind.extension.lower(),
        mime_type=kind.mime,
    )


async def probe_audio(ffmpeg_path: str, path: Path) -> AudioInfo:
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner",
        "-i",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    output = stderr.decode("utf-8", errors="replace")
    duration_match = _DURATION_RE.search(output)
    audio_line = next((line for line in output.splitlines() if "Audio:" in line), "")
    if not duration_match or not audio_line:
        raise AudioValidationError("无法读取音频信息，文件可能已损坏。")

    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise AudioValidationError("音频时长必须大于 0 秒。")

    sample_match = _SAMPLE_RATE_RE.search(audio_line)
    sample_rate = int(sample_match.group(1)) if sample_match else None
    line_lower = audio_line.lower()
    channels = None
    if "mono" in line_lower:
        channels = 1
    elif "stereo" in line_lower:
        channels = 2
    elif "5.1" in line_lower:
        channels = 6
    elif "7.1" in line_lower:
        channels = 8

    return AudioInfo(
        duration_seconds=duration,
        sample_rate=sample_rate,
        channels=channels,
        format_name=path.suffix.lower().lstrip("."),
    )


async def assert_decodable(ffmpeg_path: str, path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-v",
        "error",
        "-t",
        "1",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise AudioValidationError(
            f"音频解码失败：{detail[-300:] if detail else '未知错误'}"
        )


def validate_wav(
    path: Path,
    *,
    expected_sample_rate: int,
    expected_channels: int,
    expected_sample_width: int,
    expected_frames: int | None = None,
) -> WavValidation:
    if not path.is_file() or path.stat().st_size <= 44:
        raise AudioValidationError(f"输出文件不存在或为空：{path.name}")

    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
            if channels != expected_channels:
                raise AudioValidationError(f"{path.name} 声道数异常：{channels}")
            if sample_width != expected_sample_width:
                raise AudioValidationError(f"{path.name} 位深异常：{sample_width * 8} bit")
            if sample_rate != expected_sample_rate:
                raise AudioValidationError(f"{path.name} 采样率异常：{sample_rate}")
            if expected_frames is not None and abs(frames - expected_frames) > 1:
                raise AudioValidationError(
                    f"{path.name} 时长与输入不一致，相差 {abs(frames - expected_frames)} 帧。"
                )

            peak = 0
            while data := audio.readframes(65_536):
                samples = np.frombuffer(data, dtype="<i2")
                if sys.byteorder == "little":
                    block_peak = int(np.abs(samples.astype(np.int32)).max(initial=0))
                else:  # pragma: no cover - WAV tests run on little-endian platforms
                    block_peak = max(
                        (abs(int.from_bytes(data[i : i + 2], "little", signed=True)) for i in range(0, len(data), 2)),
                        default=0,
                    )
                peak = max(peak, block_peak)
    except (wave.Error, EOFError) as exc:
        raise AudioValidationError(f"输出 WAV 文件损坏：{path.name}") from exc

    warnings: list[str] = []
    if peak < 16:
        warnings.append(f"{path.name} 接近静音；原曲可能没有可识别的鼓声。")
    return WavValidation(
        duration_seconds=frames / sample_rate,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
        peak=peak,
        warnings=warnings,
    )


def ensure_disk_space(path: Path, required_bytes: int) -> None:
    usage = shutil.disk_usage(path)
    if usage.free < required_bytes:
        required_gb = required_bytes / (1024**3)
        free_gb = usage.free / (1024**3)
        raise AudioValidationError(
            f"磁盘空间不足，需要约 {required_gb:.1f}GB，当前可用 {free_gb:.1f}GB。"
        )


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def remove_job_directory(path: Path, jobs_root: Path) -> None:
    resolved = path.resolve()
    root = jobs_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("拒绝删除任务目录之外的路径。")
    if resolved.exists():
        shutil.rmtree(resolved)
