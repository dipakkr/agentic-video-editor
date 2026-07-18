"""Shot-boundary detection.

Uses PySceneDetect when available; otherwise degrades to a single full-clip shot so
the pipeline never fails for a missing optional dependency.
"""

from __future__ import annotations

from ave.analysis.manifest import Shot


def detect_shots(path: str, duration_s: float, *, enabled: bool = True) -> tuple[list[Shot], bool]:
    """Return (shots, ran) — `ran` is False when we fell back to a single shot."""
    if not enabled or duration_s <= 0:
        return _single(duration_s), False
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except Exception:
        return _single(duration_s), False

    try:
        video = open_video(path)
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=27.0))
        manager.detect_scenes(video, show_progress=False)
        scenes = manager.get_scene_list()
    except Exception:
        return _single(duration_s), False

    if not scenes:
        return _single(duration_s), True

    shots = [
        Shot(start_s=start.get_seconds(), end_s=end.get_seconds())
        for start, end in scenes
    ]
    return shots, True


def _single(duration_s: float) -> list[Shot]:
    return [Shot(start_s=0.0, end_s=max(0.0, duration_s))]
