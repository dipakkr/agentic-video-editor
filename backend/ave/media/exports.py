"""Platform export presets — every deliverable derives from ONE EDL.

A preset stamps a new `OutputSpec` (canvas, fps, reframe mode) onto a copy of the EDL
and, for the short-form family, trims the timeline to the platform ceiling (≤90s). The
render agent then does the rest; because rendering is a pure function of the EDL, each
variant is independently cacheable by content hash.
"""

from __future__ import annotations

from ave.analysis.manifest import ClipManifest
from ave.edl.schema import EDL, AspectRatio, OutputSpec, Segment
from ave.storage.store import Storage

EXPORT_PRESETS: dict[str, dict] = {
    "youtube": {
        "aspect_ratio": AspectRatio.wide, "width": 1920, "height": 1080,
        "fps": 30.0, "reframe": "pad", "max_duration_s": None,
    },
    "shorts": {
        "aspect_ratio": AspectRatio.vertical, "width": 1080, "height": 1920,
        "fps": 30.0, "reframe": "center_crop", "max_duration_s": 90.0,
    },
    "reels": {
        "aspect_ratio": AspectRatio.vertical, "width": 1080, "height": 1920,
        "fps": 30.0, "reframe": "center_crop", "max_duration_s": 90.0,
    },
    "tiktok": {
        "aspect_ratio": AspectRatio.vertical, "width": 1080, "height": 1920,
        "fps": 30.0, "reframe": "center_crop", "max_duration_s": 90.0,
    },
    "square": {
        "aspect_ratio": AspectRatio.square, "width": 1080, "height": 1080,
        "fps": 30.0, "reframe": "center_crop", "max_duration_s": None,
    },
}


def variant_edl(edl: EDL, preset_name: str) -> EDL:
    """Derive a platform variant EDL: new output spec + optional duration ceiling."""
    preset = EXPORT_PRESETS.get(preset_name)
    if preset is None:
        raise KeyError(f"unknown export preset: {preset_name!r} "
                       f"(available: {sorted(EXPORT_PRESETS)})")
    variant = edl.model_copy(deep=True)
    variant.output = OutputSpec(
        aspect_ratio=preset["aspect_ratio"],
        width=preset["width"],
        height=preset["height"],
        fps=preset["fps"],
        target_lufs=edl.output.target_lufs,
        use_proxy=False,  # exports are always full-res
        reframe=preset["reframe"],
    )
    ceiling = preset["max_duration_s"]
    if ceiling is not None and variant.total_duration_s > ceiling:
        variant.timeline = _trim_to(variant.timeline, ceiling)
    return variant.bump(notes=f"export variant: {preset_name}")


def _trim_to(timeline: list[Segment], ceiling_s: float) -> list[Segment]:
    """Keep whole segments up to the ceiling; partially trim the boundary segment."""
    kept: list[Segment] = []
    total = 0.0
    for seg in timeline:
        remaining = ceiling_s - total
        if remaining <= 0.2:
            break
        if seg.timeline_duration_s <= remaining:
            kept.append(seg)
            total += seg.timeline_duration_s
            continue
        trimmed = seg.model_copy(deep=True)
        trimmed.out = round(seg.in_ + remaining * seg.speed, 3)
        if trimmed.out > trimmed.in_ + 0.2:
            kept.append(trimmed)
        break
    return kept or timeline[:1]


def export_all(
    edl: EDL,
    manifests: list[ClipManifest],
    project_id: str,
    storage: Storage,
    presets: list[str] | None = None,
) -> dict[str, dict]:
    """Render every requested preset from the one EDL. Returns preset -> render result."""
    from ave.agents.render import render  # local import to avoid a cycle

    chosen = presets or [edl.brief.platform.value]
    results: dict[str, dict] = {}
    for name in chosen:
        variant = variant_edl(edl, name)
        results[name] = render(
            variant, manifests, project_id, storage, use_proxy=False
        )
        results[name]["preset"] = name
        results[name]["duration_s"] = variant.total_duration_s
    return results
