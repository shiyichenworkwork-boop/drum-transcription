#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def build_classifier(module, config, device):
    model_config = config.model
    return module.OnsetClassifier(
        num_classes=model_config.num_classes,
        branch_channels=list(model_config.branch_channels),
        context_size=model_config.context_size,
        context_hidden=model_config.context_hidden,
        classifier_hidden=model_config.classifier_hidden,
        spectral_dim=model_config.get("spectral_dim", 32),
        dropout=0.0,
        use_freq_attn=model_config.get("use_freq_attn", False),
        use_hpss=model_config.get("use_hpss", False),
        enhanced_spectral=model_config.get("enhanced_spectral", False),
        use_contrastive=False,
        use_aux_head=False,
        use_dual_head=model_config.get("use_dual_head", False),
        tom_head_hidden=model_config.get("tom_head_hidden", 256),
        use_lowfreq_branch=model_config.get("use_lowfreq_branch", False),
        use_lowfreq_spectral=model_config.get("use_lowfreq_spectral", False),
        context_classes=model_config.get("context_classes", None),
        use_crash_flux=model_config.get("use_crash_flux", False),
        crash_flux_dim=model_config.get("crash_flux_dim", 32),
    ).to(device)


def load_ensemble(module, device):
    import torch
    from omegaconf import OmegaConf

    models = []
    for ensemble_index, entry in enumerate(module.ENSEMBLE_MODELS):
        checkpoint_path = Path(entry["checkpoint"])
        if not checkpoint_path.is_file():
            continue
        checkpoint = torch.load(
            checkpoint_path,
            map_location=device,
            weights_only=False,
        )
        raw_config = checkpoint.get("config")
        if not raw_config:
            continue
        config = OmegaConf.create(raw_config)
        model = build_classifier(module, config, device)
        model.load_state_dict(checkpoint["model_state_dict"])
        model.eval()
        model_config = config.model
        models.append(
            {
                "name": entry["name"],
                "model": model,
                "config": config,
                "needs_cqt": bool(
                    model_config.get("use_lowfreq_branch", False)
                    or model_config.get("use_lowfreq_spectral", False)
                ),
                "ensemble_idx": ensemble_index,
            }
        )
    return models


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoints", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--beat-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    os.environ["STRUM_PHASE_ALIGN"] = "0"
    os.environ["STRUM_USE_DRUMSEP"] = "0"
    os.environ["STRUM_TWO_KICK"] = "0"
    os.environ["STRUM_TRACE"] = "0"
    os.environ["STRUM_TEMPO_CHANGE_DETECT"] = "0"
    source_root = args.source_root.resolve()
    checkpoints = args.checkpoints.resolve()
    input_path = args.input.resolve()
    beat_source_path = args.beat_source.resolve()
    output_dir = args.output_dir.resolve()
    result_path = args.result.resolve()
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)

    import torch
    import scripts.batch_infer_hybrid as pipeline

    analyze_audio = pipeline.analyze_audio
    pipeline.analyze_audio = lambda _: analyze_audio(beat_source_path)

    pipeline.MC_ONSET_CHECKPOINT = str(checkpoints / "drums_mc_onset" / "best.pt")
    pipeline.PHASE3_CHECKPOINT = str(checkpoints / "drums_phase3" / "best.pt")
    pipeline.TOM_REFINEMENT_CHECKPOINT = str(
        checkpoints / "tom_refinement_demucs" / "best.pt"
    )
    for entry in pipeline.ENSEMBLE_MODELS:
        entry["checkpoint"] = str(
            checkpoints / Path(entry["checkpoint"]).relative_to("checkpoints")
        )

    # This application only needs notes.mid. Avoid producing a duplicate OGG
    # and song.ini inside STRUM's temporary chart package.
    pipeline.convert_to_ogg = lambda *args, **kwargs: None
    pipeline.create_song_ini = lambda *args, **kwargs: None

    device = torch.device("cpu")
    print("STRUM_STAGE:load", flush=True)
    onset_model = pipeline.load_v14_onset_detector(device)
    ensemble = load_ensemble(pipeline, device)
    if not ensemble:
        raise RuntimeError("STRUM 鼓件分类模型没有成功加载")
    tom_refinement = pipeline.load_tom_refinement(device)
    phase3_model = pipeline.load_phase3_model(device)

    print("STRUM_STAGE:infer", flush=True)
    song_folder = pipeline.process_song(
        onset_model,
        ensemble,
        input_path,
        output_dir,
        device,
        skip_separation=True,
        onset_threshold=0.50,
        two_pass_context=True,
        postprocess=True,
        tomcym=None,
        tom_refinement=tom_refinement,
        phase3_model=phase3_model,
    )
    generated = song_folder / "notes.mid"
    if not generated.is_file():
        raise RuntimeError("STRUM 没有生成 notes.mid")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(generated, result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
