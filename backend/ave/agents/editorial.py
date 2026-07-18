"""Editorial Agent — the brain.

Turns the brief + analysis manifests into a validated EDL. Two paths:

  * **LLM path** (when ANTHROPIC_API_KEY is set): the model does narrative reasoning over
    transcripts and returns structured JSON constrained to `EDITORIAL_SCHEMA`; invalid
    output is rejected and retried, then parsed into the strict Pydantic `EDL`.
  * **Deterministic fallback**: a rule-based planner that greedily selects the best usable
    windows, opens on the strongest hook, drops dead-air/filler, and hits the target
    duration ±tolerance. This guarantees the pipeline runs with zero external services and
    gives the LLM path a reproducible baseline to beat.

Rules enforced by both paths:
  - remove dead air + filler by default
  - open with the strongest hook in the first 3 seconds
  - maintain ordering that respects narrative continuity (source order as the prior)
  - respect target length ±tolerance
"""

from __future__ import annotations

import json

from ave.analysis.manifest import ClipManifest, Shot
from ave.config import Settings, get_settings
from ave.edl.schema import EDL, Brief, Segment
from ave.llm.client import LLMClient

# JSON schema the LLM must satisfy (subset of the EDL — the editor only sets the timeline).
EDITORIAL_SCHEMA: dict = {
    "type": "object",
    "required": ["timeline"],
    "properties": {
        "timeline": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["source_clip", "in", "out", "reason"],
                "properties": {
                    "source_clip": {"type": "string"},
                    "in": {"type": "number", "minimum": 0},
                    "out": {"type": "number", "exclusiveMinimum": 0},
                    "transition_in": {
                        "type": "string",
                        "enum": ["hard", "crossfade", "whip", "fade_from_black", "j_cut", "l_cut"],
                    },
                    "speed": {"type": "number", "exclusiveMinimum": 0, "maximum": 8},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        }
    },
}

_SYSTEM = (
    "You are a world-class video editor. Given a creative brief and per-clip analysis "
    "(shots, transcript, quality flags, ranked highlights), produce an ordered edit "
    "decision list. Rules: (1) remove dead air and filler words, (2) open with the "
    "strongest hook inside the first 3 seconds, (3) keep narrative continuity, (4) hit "
    "the target duration within the stated tolerance. Every segment MUST include a short "
    "`reason` justifying the cut. Reply with JSON only, matching the provided schema."
)


def build_edl(
    project_id: str,
    brief: Brief,
    manifests: list[ClipManifest],
    *,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> EDL:
    settings = settings or get_settings()
    llm = llm or LLMClient(settings)

    if llm.available:
        try:
            return _build_with_llm(project_id, brief, manifests, llm)
        except Exception:
            # Never fail the pipeline on an LLM hiccup — fall back deterministically.
            pass
    return _build_deterministic(project_id, brief, manifests)


# --------------------------------------------------------------------------- #
# LLM path                                                                     #
# --------------------------------------------------------------------------- #
def _build_with_llm(
    project_id: str, brief: Brief, manifests: list[ClipManifest], llm: LLMClient
) -> EDL:
    user = json.dumps(
        {
            "brief": brief.model_dump(mode="json"),
            "clips": [_manifest_digest(m) for m in manifests],
        },
        indent=2,
    )
    data = llm.complete_json(
        project_id=project_id,
        agent="editorial",
        system=_SYSTEM,
        user=user,
        schema=EDITORIAL_SCHEMA,
    )
    segments: list[Segment] = []
    for i, raw in enumerate(data["timeline"], start=1):
        segments.append(
            Segment(
                id=f"seg_{i:02d}",
                source_clip=raw["source_clip"],
                **{"in": float(raw["in"])},
                out=float(raw["out"]),
                speed=float(raw.get("speed", 1.0)),
                transition_in=raw.get("transition_in", "hard"),
                reason=raw["reason"],
            )
        )
    edl = _assemble(project_id, brief, segments, note="editorial (llm)")
    return _enforce_duration(edl, brief)


def _manifest_digest(m: ClipManifest) -> dict:
    """Compact, token-frugal view of a manifest for the LLM (cost control)."""
    return {
        "clip_id": m.clip_id,
        "duration_s": round(m.probe.duration_s, 2),
        "usable_windows": [
            {"start": round(s.start_s, 2), "end": round(s.end_s, 2)}
            for s in m.usable_windows()
        ],
        "highlights": [
            {"start": round(h.start_s, 2), "end": round(h.end_s, 2),
             "score": h.score, "text": h.text}
            for h in m.highlights[:5]
        ],
        "has_transcript": bool(m.transcript),
    }


# --------------------------------------------------------------------------- #
# Deterministic fallback                                                        #
# --------------------------------------------------------------------------- #
def _build_deterministic(project_id: str, brief: Brief, manifests: list[ClipManifest]) -> EDL:
    # Candidate pool: usable windows across all clips, each tagged with its clip + score.
    candidates: list[tuple[ClipManifest, Shot, float]] = []
    for m in manifests:
        highlight_score = {round(h.start_s, 2): h.score for h in m.highlights}
        for win in m.usable_windows():
            score = highlight_score.get(round(win.start_s, 2), 0.1)
            candidates.append((m, win, score))

    if not candidates:
        raise ValueError("no usable material in any clip")

    # Hook: highest-scoring window overall, trimmed to a punchy <=6s open.
    hook_m, hook_win, _ = max(candidates, key=lambda c: c[2])
    hook = Shot(start_s=hook_win.start_s, end_s=min(hook_win.end_s, hook_win.start_s + 6.0))

    ordered = _narrative_order(manifests, hook_m, hook)
    segments = _fill_to_target([(hook_m, hook)] + ordered, brief)
    if not segments:
        raise ValueError(
            "no usable material with positive duration — could not probe clip durations "
            "(is ffmpeg installed?)"
        )

    edl = _assemble(project_id, brief, segments, note="editorial (deterministic)")
    return _enforce_duration(edl, brief)


def _narrative_order(
    manifests: list[ClipManifest], hook_m: ClipManifest, hook: Shot
) -> list[tuple[ClipManifest, Shot]]:
    """Remaining windows in source order (clip order, then time) — a continuity prior."""
    out: list[tuple[ClipManifest, Shot]] = []
    for m in manifests:
        for win in m.usable_windows():
            if m.clip_id == hook_m.clip_id and _overlaps(win, hook):
                continue
            out.append((m, win))
    return out


def _fill_to_target(
    pool: list[tuple[ClipManifest, Shot]], brief: Brief
) -> list[Segment]:
    """Greedily accumulate windows until we reach the target duration."""
    target = brief.target_duration_s
    segments: list[Segment] = []
    total = 0.0
    idx = 0
    for m, win in pool:
        remaining = target - total
        if remaining <= 0.2:
            break
        if win.duration_s <= 0.05:
            continue  # skip empty/unprobeable windows (graceful degradation)
        end = win.end_s
        # Trim the final segment so we don't overshoot the target.
        if win.duration_s > remaining:
            end = win.start_s + remaining
        if end <= win.start_s:
            continue
        idx += 1
        i = idx
        seg = Segment(
            id=f"seg_{i:02d}",
            source_clip=m.clip_id,
            **{"in": round(win.start_s, 3)},
            out=round(end, 3),
            transition_in="fade_from_black" if i == 1 else "hard",
            reason=_reason_for(i, m, win),
        )
        segments.append(seg)
        total += seg.timeline_duration_s
    return segments


def _reason_for(idx: int, m: ClipManifest, win: Shot) -> str:
    if idx == 1:
        text = win.duration_s
        return f"Strongest hook — opens on the highest-energy moment of {m.clip_id}."
    text = next((h.text for h in m.highlights if _overlaps(h, win) and h.text), "")
    if text:
        return f"Continuity beat from {m.clip_id}: '{text[:60]}'."
    return f"Supporting shot from {m.clip_id} ({win.duration_s:.1f}s) to sustain pacing."


# --------------------------------------------------------------------------- #
# Shared helpers                                                                #
# --------------------------------------------------------------------------- #
def _assemble(project_id: str, brief: Brief, segments: list[Segment], note: str) -> EDL:
    from ave.edl.schema import AspectRatio, Captions, CaptionStyle, Music, OutputSpec

    aspect = brief.aspect_ratio
    dims = {
        AspectRatio.wide: (1920, 1080),
        AspectRatio.vertical: (1080, 1920),
        AspectRatio.square: (1080, 1080),
    }[aspect]
    caption_style = (
        CaptionStyle.karaoke_bold
        if aspect in (AspectRatio.vertical, AspectRatio.square)
        else CaptionStyle.clean_subtitle
    )
    return EDL(
        project_id=project_id,
        brief=brief,
        timeline=segments,
        music=Music(track_id=brief.music_track_id, ducking=True),
        captions=Captions(style=caption_style, language="en"),
        output=OutputSpec(aspect_ratio=aspect, width=dims[0], height=dims[1]),
        notes=note,
    )


def _enforce_duration(edl: EDL, brief: Brief) -> EDL:
    """Trim trailing segments if we overshot the +tolerance ceiling."""
    ceiling = brief.target_duration_s * (1 + brief.duration_tolerance_pct / 100.0)
    if edl.total_duration_s <= ceiling:
        return edl
    kept: list[Segment] = []
    total = 0.0
    for seg in edl.timeline:
        if total + seg.timeline_duration_s > ceiling:
            overshoot = ceiling - total
            if overshoot > 0.5:
                trimmed = seg.model_copy()
                trimmed.out = round(seg.in_ + overshoot * seg.speed, 3)
                if trimmed.out > trimmed.in_:
                    kept.append(trimmed)
            break
        kept.append(seg)
        total += seg.timeline_duration_s
    new = edl.model_copy(deep=True)
    new.timeline = kept
    return new


def _overlaps(a, b) -> bool:
    return not (a.end_s <= b.start_s or a.start_s >= b.end_s)
