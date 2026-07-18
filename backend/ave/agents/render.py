"""Render Agent — compile the EDL into a video file via ffmpeg.

Backend choice: **ffmpeg filtergraph** over Remotion for M1. Rationale — the render is a
pure function of the EDL and ffmpeg gives us deterministic, fast, dependency-light
concat/xfade with frame-accurate trims; captions go in later as burned-in libass/ASS
(M2), which keeps the render self-contained with no Node/Chromium in the hot path.
Remotion remains a viable alternate backend for heavily animated caption/graphic styles.

The agent resolves each segment's source to a proxy (preview) or full-res file (final),
builds the plan, and either executes ffmpeg or — when ffmpeg is unavailable — writes the
resolved plan to disk (a "dry render") so the pipeline still produces an auditable
artifact end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

from ave.analysis.manifest import ClipManifest
from ave.edl.schema import EDL
from ave.media import ffmpeg, filtergraph
from ave.storage.store import Storage


def resolve_sources(
    edl: EDL, manifests: list[ClipManifest], *, use_proxy: bool
) -> dict[str, str]:
    """Map each source_clip referenced by the EDL to a concrete file path."""
    by_id = {m.clip_id: m for m in manifests}
    sources: dict[str, str] = {}
    for seg in edl.timeline:
        m = by_id.get(seg.source_clip)
        if m is None:
            raise KeyError(f"segment {seg.id} references unknown clip {seg.source_clip}")
        path = (m.proxy_path if use_proxy and m.proxy_path else m.source_path)
        sources[seg.source_clip] = path
    return sources


def render(
    edl: EDL,
    manifests: list[ClipManifest],
    project_id: str,
    storage: Storage,
    *,
    use_proxy: bool | None = None,
    music_path: str | None = None,
    ass_path: str | None = None,
) -> dict:
    """Render the EDL. Returns {output_path|plan_path, executed, content_hash}.

    `music_path` / `ass_path` (M2) layer the music bed + ducking + burned-in captions;
    b-roll overlays + graphics (M5) come straight from the EDL. Omit everything and you
    get the plain M1 rough cut. Layer order: base cut → overlays/graphics → music/
    captions, so burned-in captions always sit on top.
    """
    use_proxy = edl.output.use_proxy if use_proxy is None else use_proxy
    sources = resolve_sources(edl, manifests, use_proxy=use_proxy)
    plan = filtergraph.build(edl, sources)
    if edl.overlays or edl.graphics.title_card or edl.graphics.lower_thirds:
        plan = filtergraph.augment_with_overlays_and_graphics(
            plan, edl,
            overlay_sources=resolve_overlay_sources(edl, manifests, use_proxy=use_proxy),
        )
    if music_path is not None or ass_path is not None:
        plan = filtergraph.augment_with_music_and_captions(
            plan, edl, music_path=music_path, ass_path=ass_path
        )

    kind = "preview" if use_proxy else "final"
    content_key = edl.content_hash()[:12]
    stem = f"renders/{kind}_v{edl.version}_{content_key}"

    # Incremental re-render: rendering is a pure function of the EDL, so an existing
    # output for the same content hash IS this render. Version bumps that don't change
    # content (e.g. a no-op feedback round) hit this cache instead of re-encoding.
    cached = _find_cached(storage, project_id, kind, content_key)
    if cached is not None:
        return {
            "plan_path": None,
            "output_path": cached,
            "executed": True,
            "cached": True,
            "content_hash": edl.content_hash(),
        }

    # Always persist the resolved plan — it's the deterministic record of this render.
    plan_path = storage.write_json(
        project_id,
        f"{stem}.plan.json",
        {
            "content_hash": edl.content_hash(),
            "version": edl.version,
            "inputs": plan.inputs,
            "filtergraph": plan.filtergraph,
            "maps": plan.maps,
            "argv": plan.args,
        },
    )

    if not ffmpeg.have_ffmpeg():
        return {
            "plan_path": plan_path,
            "output_path": None,
            "executed": False,
            "content_hash": edl.content_hash(),
            "note": "ffmpeg not available — wrote dry render plan only.",
        }

    out_path = storage.path_for(project_id, f"{stem}.mp4")
    ffmpeg.run_ffmpeg(plan.args + [str(out_path)])
    return {
        "plan_path": plan_path,
        "output_path": str(out_path),
        "executed": True,
        "content_hash": edl.content_hash(),
    }


def resolve_overlay_sources(
    edl: EDL, manifests: list[ClipManifest], *, use_proxy: bool
) -> dict[str, str]:
    """Map overlay ids to concrete source files (proxy or full-res).

    Unresolvable clips are simply omitted — the filtergraph skips those cutaways rather
    than failing the render (graceful degradation).
    """
    by_id = {m.clip_id: m for m in manifests}
    out: dict[str, str] = {}
    for ovl in edl.overlays:
        m = by_id.get(ovl.source_clip)
        if m is None:
            continue
        out[ovl.id] = m.proxy_path if use_proxy and m.proxy_path else m.source_path
    return out


def _find_cached(storage: Storage, project_id: str, kind: str, content_key: str) -> str | None:
    """Locate an existing rendered file for this content hash (any version number)."""
    renders_dir = storage.project_dir(project_id) / "renders"
    if not renders_dir.exists():
        return None
    matches = sorted(renders_dir.glob(f"{kind}_v*_{content_key}.mp4"))
    return str(matches[0]) if matches else None


def load_plan(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
