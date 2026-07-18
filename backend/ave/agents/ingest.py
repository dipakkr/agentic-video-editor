"""Ingest & Analysis Agent.

Probes every clip, builds a mezzanine proxy, detects shots, transcribes speech, flags
dead-air/filler, and ranks highlights — emitting one `ClipManifest` per clip. Every
optional pass degrades gracefully and records what actually ran in
`manifest.analysis_features`, so the Editorial Agent can reason about missing signals.
"""

from __future__ import annotations

from pathlib import Path

from ave.analysis import quality, scenes, transcribe
from ave.analysis.manifest import ClipManifest, ProbeInfo
from ave.config import Settings, get_settings
from ave.media import ffmpeg
from ave.storage.store import Storage


def analyze_clip(
    clip_id: str,
    source_path: str,
    project_id: str,
    storage: Storage,
    settings: Settings | None = None,
) -> ClipManifest:
    settings = settings or get_settings()
    features: dict[str, bool] = {}

    # -- probe -------------------------------------------------------------- #
    if ffmpeg.have_ffmpeg():
        try:
            p = ffmpeg.ffprobe(source_path)
            probe = ProbeInfo(
                duration_s=p.duration_s, width=p.width, height=p.height, fps=p.fps,
                video_codec=p.video_codec, audio_codec=p.audio_codec,
                audio_channels=p.audio_channels, has_audio=p.has_audio,
            )
            features["probe"] = True
        except Exception:
            probe, features["probe"] = ProbeInfo(), False
    else:
        probe, features["probe"] = ProbeInfo(), False

    manifest = ClipManifest(clip_id=clip_id, source_path=source_path, probe=probe)

    # -- mezzanine proxy ---------------------------------------------------- #
    if ffmpeg.have_ffmpeg() and probe.duration_s > 0:
        try:
            proxy_dst = storage.path_for(project_id, f"proxies/{clip_id}.mp4")
            ffmpeg.make_proxy(source_path, str(proxy_dst), height=settings.ave_proxy_height)
            manifest.proxy_path = str(proxy_dst)
            features["proxy"] = True
        except Exception:
            features["proxy"] = False
    else:
        features["proxy"] = False

    analysis_target = manifest.proxy_path or source_path

    # -- shots -------------------------------------------------------------- #
    shots, ran = scenes.detect_shots(
        analysis_target, probe.duration_s, enabled=settings.ave_enable_scenedetect
    )
    manifest.shots = shots
    features["scenedetect"] = ran

    # -- transcript --------------------------------------------------------- #
    segs, ran = transcribe.transcribe(
        analysis_target,
        enabled=settings.ave_enable_whisperx,
        model_name=settings.ave_whisper_model,
    )
    manifest.transcript = segs
    features["whisperx"] = ran

    # -- quality flags ------------------------------------------------------ #
    silence = quality.detect_silence(analysis_target) if ffmpeg.have_ffmpeg() else []
    filler = quality.detect_filler(segs)
    manifest.quality_flags = silence + filler
    features["silence"] = bool(ffmpeg.have_ffmpeg())
    features["filler"] = bool(segs)

    # -- highlights --------------------------------------------------------- #
    manifest.highlights = quality.rank_highlights(shots, segs, probe.duration_s)

    manifest.analysis_features = features
    storage.write_json(project_id, f"manifests/{clip_id}.json", manifest.model_dump(mode="json"))
    return manifest


def analyze_all(
    clips: dict[str, str],
    project_id: str,
    storage: Storage,
    settings: Settings | None = None,
) -> list[ClipManifest]:
    """Analyze a mapping of {clip_id: source_path}. Deterministic order by clip_id."""
    return [
        analyze_clip(cid, Path(path).as_posix(), project_id, storage, settings)
        for cid, path in sorted(clips.items())
    ]
