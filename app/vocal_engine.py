from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from app.audio import AudioValidationError, assert_decodable, probe_audio
from app.config import Settings
from app.engine import (
    DemucsEngine,
    ProgressCallback,
    SeparationError,
    SeparationResult,
)
from app.runtime import module_command


class MelBandRoformerEngine(DemucsEngine):
    """CPU-first vocal separator with the same cancellable subprocess contract."""

    def __init__(self, settings: Settings):
        super().__init__(settings)

    async def separate(
        self,
        *,
        job_id: str,
        source_path: Path,
        job_dir: Path,
        progress: ProgressCallback,
    ) -> SeparationResult:
        work_dir = job_dir / "work"
        input_dir = work_dir / "input"
        separated_dir = work_dir / "separated"
        normalized_path = input_dir / "input.wav"
        output_dir = job_dir / "outputs"
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        input_dir.mkdir(parents=True, exist_ok=True)
        separated_dir.mkdir(parents=True, exist_ok=True)
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
            source_info = await probe_audio(self.ffmpeg_path, normalized_path)
            await self._raise_if_cancelled(job_id)

            await progress(
                "separating",
                15,
                "正在加载 MelBand RoFormer 人声模型",
            )
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["NO_COLOR"] = "1"
            env["MELBAND_ROFORMER_MODELS_PATH"] = str(
                self.settings.models_dir / "melband-roformer"
            )
            await self._run_process(
                job_id,
                module_command(
                    "mel_band_roformer.inference",
                    "--model",
                    self.settings.vocal_model_name,
                    "--models_dir",
                    str(self.settings.models_dir / "melband-roformer"),
                    "--input_folder",
                    str(input_dir),
                    "--store_dir",
                    str(separated_dir),
                    "--device",
                    "cpu",
                ),
                progress,
                stage="separating",
                env=env,
                progress_message="正在分离人声",
            )
            await self._raise_if_cancelled(job_id)

            await progress("postprocessing", 92, "正在校验人声分轨")
            vocal_candidates = list(separated_dir.rglob("*_vocals.wav"))
            instrumental_candidates = list(separated_dir.rglob("*_instrumental.wav"))
            if len(vocal_candidates) != 1 or len(instrumental_candidates) != 1:
                raise SeparationError("模型没有生成完整的人声分轨结果。")

            await progress("postprocessing", 94, "正在压缩人声轨")
            vocals_final = await self._encode_mp3(
                job_id,
                vocal_candidates[0],
                output_dir / "vocals.mp3",
                progress,
            )
            await progress("postprocessing", 97, "正在压缩无人声伴奏")
            instrumental_final = await self._encode_mp3(
                job_id,
                instrumental_candidates[0],
                output_dir / "instrumental.mp3",
                progress,
            )

            warnings: list[str] = []
            for encoded in (vocals_final, instrumental_final):
                info = await probe_audio(self.ffmpeg_path, encoded)
                if info.sample_rate != self.settings.sample_rate:
                    raise SeparationError(f"{encoded.name} 采样率异常。")
                if info.channels != self.settings.channels:
                    raise SeparationError(f"{encoded.name} 声道数异常。")
                if abs(info.duration_seconds - source_info.duration_seconds) > 0.1:
                    raise SeparationError(f"{encoded.name} 时长与输入不一致。")
                await assert_decodable(self.ffmpeg_path, encoded)

            storage_bytes = vocals_final.stat().st_size + instrumental_final.stat().st_size
            await progress("postprocessing", 99, "正在整理人声分轨")
            successful = True
            return SeparationResult(
                storage_bytes=storage_bytes,
                warnings=warnings,
                vocals_path=vocals_final,
                instrumental_path=instrumental_final,
            )
        except AudioValidationError as exc:
            raise SeparationError(str(exc)) from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if not successful:
                shutil.rmtree(output_dir, ignore_errors=True)
            async with self._lock:
                self._cancelled.discard(job_id)
                self._processes.pop(job_id, None)
