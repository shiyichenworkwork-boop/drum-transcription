from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from app.config import Settings


ADTOF_NOTE_MAP = {
    35: 36,  # Acoustic Bass Drum -> Bass Drum 1
    38: 38,  # Acoustic Snare
    47: 47,  # Low-Mid Tom
    42: 42,  # Closed Hi-Hat
    49: 49,  # Crash Cymbal 1
}


class MidiModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RawDrumHit:
    time_seconds: float
    note: int
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class BeatGrid:
    beats: np.ndarray
    downbeats: np.ndarray
    engine: str


class DrumModel(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> list[RawDrumHit]: ...


class BeatTracker(Protocol):
    name: str

    def detect(self, audio_path: Path) -> BeatGrid: ...


class AdtofDrumModel:
    """Adapter around the five-class ADTOF PyTorch checkpoint."""

    name = "ADTOF-PyTorch"

    def transcribe(self, audio_path: Path) -> list[RawDrumHit]:
        try:
            import pretty_midi
            from adtof_pytorch import transcribe_to_midi
        except ImportError as exc:  # pragma: no cover - dependency install failure
            raise MidiModelError("ADTOF-PyTorch 未安装。") from exc

        raw_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="adtof-",
                suffix=".mid",
                dir=audio_path.parent,
                delete=False,
            ) as handle:
                raw_path = Path(handle.name)
            raw_path.unlink(missing_ok=True)
            transcribe_to_midi(
                audio_path,
                raw_path,
                device="cpu",
            )
            midi = pretty_midi.PrettyMIDI(str(raw_path))
            hits: list[RawDrumHit] = []
            for instrument in midi.instruments:
                for note in instrument.notes:
                    mapped = ADTOF_NOTE_MAP.get(int(note.pitch))
                    if mapped is None or not np.isfinite(note.start):
                        continue
                    hits.append(
                        RawDrumHit(
                            time_seconds=max(0.0, float(note.start)),
                            note=mapped,
                        )
                    )
            hits.sort(key=lambda hit: (hit.time_seconds, hit.note))
            if not hits:
                raise MidiModelError("ADTOF 没有识别到鼓点。")
            return hits
        except MidiModelError:
            raise
        except Exception as exc:
            raise MidiModelError(f"ADTOF 鼓件识别失败：{exc}") from exc
        finally:
            if raw_path is not None:
                raw_path.unlink(missing_ok=True)


class BeatThisTracker:
    """Lazy-loading Beat This! adapter with a project-local checkpoint cache."""

    name = "Beat This!"

    def __init__(self, settings: Settings, checkpoint: str = "final0"):
        self.settings = settings
        self.checkpoint = checkpoint
        self._tracker = None

    def detect(self, audio_path: Path) -> BeatGrid:
        try:
            import torch
            from beat_this.inference import File2Beats
        except ImportError as exc:  # pragma: no cover - dependency install failure
            raise MidiModelError("Beat This! 未安装。") from exc

        try:
            torch_home = self.settings.models_dir / "torch"
            hub_dir = torch_home / "hub"
            hub_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("TORCH_HOME", str(torch_home))
            torch.hub.set_dir(str(hub_dir))
            if self._tracker is None:
                checkpoint_path = hub_dir / "checkpoints" / f"beat_this-{self.checkpoint}.ckpt"
                selected = str(checkpoint_path) if checkpoint_path.is_file() else self.checkpoint
                self._tracker = File2Beats(
                    checkpoint_path=selected,
                    device="cpu",
                    float16=False,
                    dbn=False,
                )
            beats, downbeats = self._tracker(str(audio_path))
            beat_array = self._clean_times(beats)
            downbeat_array = self._clean_times(downbeats)
            if beat_array.size < 4:
                raise MidiModelError("Beat This! 没有检测到足够拍点。")
            return BeatGrid(
                beats=beat_array,
                downbeats=downbeat_array,
                engine=self.name,
            )
        except MidiModelError:
            raise
        except Exception as exc:
            raise MidiModelError(f"Beat This! 节拍检测失败：{exc}") from exc

    @staticmethod
    def _clean_times(values) -> np.ndarray:
        result = np.asarray(values, dtype=np.float64).reshape(-1)
        result = result[np.isfinite(result) & (result >= 0)]
        return np.unique(np.round(result, 6))
