from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from app.audio import (
    AudioValidationError,
    assert_decodable,
    mp3_encode_args,
    probe_audio,
    validate_wav,
)
from app.config import Settings


ProgressCallback = Callable[[str, int, str | None], Awaitable[None]]
_PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})%")


class SeparationError(RuntimeError):
    pass


class SeparationCancelled(SeparationError):
    pass


@dataclass(slots=True)
class SeparationResult:
    drums_path: Path
    no_drums_path: Path
    storage_bytes: int
    warnings: list[str]


class SeparationEngine(ABC):
    @abstractmethod
    async def separate(
        self,
        *,
        job_id: str,
        source_path: Path,
        job_dir: Path,
        progress: ProgressCallback,
    ) -> SeparationResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, job_id: str) -> bool:
        raise NotImplementedError


class DemucsEngine(SeparationEngine):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ffmpeg_path = settings.resolve_ffmpeg()
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    async def separate(
        self,
        *,
        job_id: str,
        source_path: Path,
        job_dir: Path,
        progress: ProgressCallback,
    ) -> SeparationResult:
        work_dir = job_dir / "work"
        normalized_path = work_dir / "input.wav"
        separated_dir = work_dir / "separated"
        output_dir = job_dir / "outputs"
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        successful = False

        try:
            await progress("preprocessing", 5, "正在统一音频格式")
            await self._run_process(
                job_id,
                [
                    self.ffmpeg_path,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source_path),
                    "-map_metadata",
                    "-1",
                    "-vn",
                    "-ac",
                    str(self.settings.channels),
                    "-ar",
                    str(self.settings.sample_rate),
                    "-c:a",
                    "pcm_s16le",
                    str(normalized_path),
                ],
                progress,
                stage="preprocessing",
            )
            normalized = await asyncio.to_thread(
                validate_wav,
                normalized_path,
                expected_sample_rate=self.settings.sample_rate,
                expected_channels=self.settings.channels,
                expected_sample_width=self.settings.sample_width,
            )
            await self._raise_if_cancelled(job_id)

            await progress("separating", 15, "正在加载高质量模型")
            env = os.environ.copy()
            env["TORCH_HOME"] = str(self.settings.models_dir / "torch")
            env["PYTHONUNBUFFERED"] = "1"
            env["NO_COLOR"] = "1"
            ffmpeg_dir = str(Path(self.ffmpeg_path).resolve().parent)
            env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
            await self._run_process(
                job_id,
                [
                    sys.executable,
                    "-m",
                    "demucs",
                    "-n",
                    self.settings.model_name,
                    "--two-stems",
                    "drums",
                    "-d",
                    "cpu",
                    "--overlap",
                    "0.25",
                    "--out",
                    str(separated_dir),
                    str(normalized_path),
                ],
                progress,
                stage="separating",
                env=env,
            )
            await self._raise_if_cancelled(job_id)

            await progress("postprocessing", 92, "正在校验输出文件")
            drum_candidates = list(separated_dir.rglob("drums.wav"))
            no_drum_candidates = list(separated_dir.rglob("no_drums.wav"))
            if len(drum_candidates) != 1 or len(no_drum_candidates) != 1:
                raise SeparationError("模型没有生成完整的鼓轨结果。")

            drum_validation = await asyncio.to_thread(
                validate_wav,
                drum_candidates[0],
                expected_sample_rate=self.settings.sample_rate,
                expected_channels=self.settings.channels,
                expected_sample_width=self.settings.sample_width,
                expected_frames=normalized.frames,
            )
            no_drum_validation = await asyncio.to_thread(
                validate_wav,
                no_drum_candidates[0],
                expected_sample_rate=self.settings.sample_rate,
                expected_channels=self.settings.channels,
                expected_sample_width=self.settings.sample_width,
                expected_frames=normalized.frames,
            )

            if self.settings.output_format != "mp3":
                raise SeparationError(
                    f"当前版本不支持输出格式：{self.settings.output_format}"
                )

            await progress("postprocessing", 94, "正在压缩鼓轨")
            drums_final = await self._encode_mp3(
                job_id,
                drum_candidates[0],
                output_dir / "drums.mp3",
                progress,
            )
            await progress("postprocessing", 97, "正在压缩去鼓伴奏")
            no_drums_final = await self._encode_mp3(
                job_id,
                no_drum_candidates[0],
                output_dir / "no_drums.mp3",
                progress,
            )

            for encoded in (drums_final, no_drums_final):
                info = await probe_audio(self.ffmpeg_path, encoded)
                if info.sample_rate != self.settings.sample_rate:
                    raise SeparationError(f"{encoded.name} 采样率异常。")
                if info.channels != self.settings.channels:
                    raise SeparationError(f"{encoded.name} 声道数异常。")
                if abs(info.duration_seconds - normalized.duration_seconds) > 0.1:
                    raise SeparationError(f"{encoded.name} 时长与输入不一致。")
                await assert_decodable(self.ffmpeg_path, encoded)

            warnings = drum_validation.warnings + no_drum_validation.warnings
            storage_bytes = drums_final.stat().st_size + no_drums_final.stat().st_size
            await progress("postprocessing", 99, "正在整理结果")
            result = SeparationResult(
                drums_path=drums_final,
                no_drums_path=no_drums_final,
                storage_bytes=storage_bytes,
                warnings=warnings,
            )
            successful = True
            return result
        except AudioValidationError as exc:
            raise SeparationError(str(exc)) from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if not successful:
                shutil.rmtree(output_dir, ignore_errors=True)
            async with self._lock:
                self._cancelled.discard(job_id)
                self._processes.pop(job_id, None)

    async def _encode_mp3(
        self,
        job_id: str,
        source_path: Path,
        destination: Path,
        progress: ProgressCallback,
    ) -> Path:
        part_path = destination.with_suffix(destination.suffix + ".part")
        part_path.unlink(missing_ok=True)
        try:
            await self._run_process(
                job_id,
                mp3_encode_args(
                    self.ffmpeg_path,
                    source_path,
                    part_path,
                    sample_rate=self.settings.sample_rate,
                    channels=self.settings.channels,
                    bitrate=self.settings.output_bitrate,
                ),
                progress,
                stage="postprocessing",
            )
            if not part_path.is_file() or part_path.stat().st_size == 0:
                raise SeparationError(f"{destination.name} 压缩失败。")
            os.replace(part_path, destination)
            return destination
        finally:
            part_path.unlink(missing_ok=True)

    async def cancel(self, job_id: str) -> bool:
        async with self._lock:
            self._cancelled.add(job_id)
            process = self._processes.get(job_id)
        if not process or process.returncode is not None:
            return False
        await self._terminate_process(process)
        return True

    async def _raise_if_cancelled(self, job_id: str) -> None:
        async with self._lock:
            if job_id in self._cancelled:
                raise SeparationCancelled("任务已取消。")

    async def _run_process(
        self,
        job_id: str,
        args: list[str],
        progress: ProgressCallback,
        *,
        stage: str,
        env: dict[str, str] | None = None,
    ) -> str:
        await self._raise_if_cancelled(job_id)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=os.name != "nt",
        )
        async with self._lock:
            self._processes[job_id] = process

        lines: list[str] = []
        assert process.stdout is not None
        try:
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)
                    lines = lines[-80:]
                if stage == "separating":
                    matches = _PERCENT_RE.findall(line)
                    if matches:
                        raw_percent = min(int(matches[-1]), 100)
                        await progress(
                            "separating",
                            15 + round(raw_percent * 0.75),
                            "正在分离鼓轨",
                        )
            return_code = await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        finally:
            async with self._lock:
                if self._processes.get(job_id) is process:
                    self._processes.pop(job_id, None)

        async with self._lock:
            cancelled = job_id in self._cancelled
        if cancelled:
            raise SeparationCancelled("任务已取消。")
        if return_code != 0:
            detail = "\n".join(lines[-12:]).strip()
            if "download" in detail.lower() or "urlopen" in detail.lower():
                message = "模型下载失败，请检查网络后重新处理。"
            else:
                message = f"音频处理进程异常退出（代码 {return_code}）。"
            if detail:
                message += f"\n{detail[-1200:]}"
            raise SeparationError(message)
        return "\n".join(lines)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - current target is macOS
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except ProcessLookupError:
            return
        except TimeoutError:
            if process.returncode is None:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - current target is macOS
                    process.kill()
                await process.wait()
