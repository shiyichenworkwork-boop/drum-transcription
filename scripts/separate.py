from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import filetype

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.audio import ALLOWED_FORMATS, assert_decodable, probe_audio
from app.config import Settings
from app.engine import DemucsEngine


async def run(source: Path, output: Path) -> None:
    settings = Settings.from_env()
    settings.ensure_directories()
    if not source.is_file():
        raise SystemExit(f"文件不存在：{source}")
    if source.stat().st_size > settings.max_upload_bytes:
        raise SystemExit("文件超过 500MB 限制。")
    kind = filetype.guess(str(source))
    if not kind or kind.extension.lower() not in ALLOWED_FORMATS:
        raise SystemExit("仅支持 WAV、MP3、FLAC、M4A 和 OGG。")

    ffmpeg = settings.resolve_ffmpeg()
    info = await probe_audio(ffmpeg, source)
    if info.duration_seconds > settings.max_duration_seconds:
        raise SystemExit("音频超过 15 分钟限制。")
    await assert_decodable(ffmpeg, source)

    temporary_job = Path(tempfile.mkdtemp(prefix="cli-", dir=settings.jobs_dir))
    job_id = uuid4().hex

    async def progress(stage: str, value: int, message: str | None) -> None:
        print(f"[{value:>3}%] {message or stage}", flush=True)

    try:
        result = await DemucsEngine(settings).separate(
            job_id=job_id,
            source_path=source.resolve(),
            job_dir=temporary_job,
            progress=progress,
        )
        output.mkdir(parents=True, exist_ok=True)
        drums = output / f"{source.stem}-drums{result.drums_path.suffix}"
        no_drums = output / f"{source.stem}-no-drums{result.no_drums_path.suffix}"
        shutil.copy2(result.drums_path, drums)
        shutil.copy2(result.no_drums_path, no_drums)
        print(f"鼓轨：{drums.resolve()}")
        print(f"去鼓伴奏：{no_drums.resolve()}")
        for warning in result.warnings:
            print(f"提醒：{warning}")
    finally:
        shutil.rmtree(temporary_job, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 HTDemucs FT 分离鼓轨")
    parser.add_argument("input", type=Path, help="输入音频文件")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="结果目录，默认为 ./output",
    )
    args = parser.parse_args()
    asyncio.run(run(args.input.expanduser(), args.output.expanduser()))


if __name__ == "__main__":
    main()
