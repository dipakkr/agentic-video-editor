"""Revision operation vocabulary and the pure applier that executes it on an EDL.

Natural-language feedback ("make the intro punchier") is translated — by the Revise
Agent's LLM path or its deterministic fallback — into a small list of `EditOp` values.
`apply_ops` then executes those ops *purely*: the input EDL is never mutated, invalid
ops are collected as human-readable skip messages instead of raising, and the result
is a `bump()`ed new revision only when at least one op actually applied. This keeps the
feedback loop deterministic, explainable, and safe to retry.
"""

from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel

from ave.edl.schema import EDL, CaptionStyle, Segment, Transition

# A trim (or any edit) may never leave a segment with less source material than this.
MIN_SEGMENT_SOURCE_S = 0.3
# Retime clamp range.
SPEED_MIN = 0.25
SPEED_MAX = 4.0
# Segments removed entirely by a duration trim need at least this much room to survive
# as a partial segment (mirrors editorial's duration enforcement).
_MIN_TAIL_KEEP_S = 0.5


class EditOp(BaseModel):
    """One atomic revision to an EDL, derived from a user's feedback note."""

    op: Literal[
        "remove_segment",
        "trim",
        "reorder",
        "retime",
        "set_transition",
        "change_caption_style",
        "set_target_duration",
    ]
    segment_id: str | None = None
    trim_start_s: float = 0.0        # trim: seconds removed from segment head
    trim_end_s: float = 0.0          # trim: seconds removed from segment tail
    to_index: int | None = None      # reorder: new position (0-based, clamped)
    speed: float | None = None       # retime (clamped to 0.25..4.0)
    transition: str | None = None    # set_transition: Transition value
    transition_duration_s: float = 0.4
    caption_style: str | None = None  # change_caption_style: CaptionStyle value
    target_duration_s: float | None = None  # set_target_duration
    reason: str = ""                 # why this op (from the user's note)


def apply_ops(edl: EDL, ops: list[EditOp], note: str = "") -> tuple[EDL, list[str]]:
    """Apply `ops` in order to a deep copy of `edl` (pure — the input is never mutated).

    Returns `(new_edl, skipped)`. Ops that cannot apply (unknown segment id, a trim that
    would collapse a segment, invalid enum values, ...) are skipped with a human-readable
    message. If at least one op applied, the result is `edl.bump(notes=f"feedback: {note}")`
    of the edited copy; if *every* op was skipped the original `edl` is returned unchanged
    (and not bumped).
    """
    work = edl.model_copy(deep=True)
    skipped: list[str] = []
    applied = 0
    for op in ops:
        err = _APPLIERS[op.op](work, op)
        if err is None:
            applied += 1
        else:
            skipped.append(err)
    if applied == 0:
        return edl, skipped
    return work.bump(notes=f"feedback: {note}"), skipped


# --------------------------------------------------------------------------- #
# Per-op appliers (mutate the working copy; return an error string to skip)    #
# --------------------------------------------------------------------------- #
def _find(work: EDL, segment_id: str | None) -> int:
    for i, seg in enumerate(work.timeline):
        if seg.id == segment_id:
            return i
    return -1


def _touch(seg: Segment, op: EditOp) -> None:
    """Record on the segment why it was revised (keeps the edit explainable)."""
    seg.reason = f"{seg.reason} [revised: {op.reason or op.op}]"


def _apply_remove(work: EDL, op: EditOp) -> str | None:
    i = _find(work, op.segment_id)
    if i < 0:
        return f"remove_segment: unknown segment_id {op.segment_id!r}"
    if len(work.timeline) <= 1:
        return f"remove_segment: removing {op.segment_id!r} would empty the timeline"
    del work.timeline[i]
    return None


def _apply_trim(work: EDL, op: EditOp) -> str | None:
    i = _find(work, op.segment_id)
    if i < 0:
        return f"trim: unknown segment_id {op.segment_id!r}"
    seg = work.timeline[i]
    head = max(op.trim_start_s, 0.0)
    tail = max(op.trim_end_s, 0.0)
    new_in = seg.in_ + head
    new_out = seg.out - tail
    if new_out - new_in < MIN_SEGMENT_SOURCE_S:
        return (
            f"trim: would collapse {seg.id} below {MIN_SEGMENT_SOURCE_S}s source duration"
        )
    seg.in_ = round(new_in, 3)
    seg.out = round(new_out, 3)
    _touch(seg, op)
    return None


def _apply_reorder(work: EDL, op: EditOp) -> str | None:
    if op.to_index is None:
        return f"reorder: missing to_index for {op.segment_id!r}"
    i = _find(work, op.segment_id)
    if i < 0:
        return f"reorder: unknown segment_id {op.segment_id!r}"
    seg = work.timeline.pop(i)
    new_idx = max(0, min(op.to_index, len(work.timeline)))
    work.timeline.insert(new_idx, seg)
    _touch(seg, op)
    return None


def _apply_retime(work: EDL, op: EditOp) -> str | None:
    if op.speed is None:
        return f"retime: missing speed for {op.segment_id!r}"
    i = _find(work, op.segment_id)
    if i < 0:
        return f"retime: unknown segment_id {op.segment_id!r}"
    seg = work.timeline[i]
    seg.speed = max(SPEED_MIN, min(op.speed, SPEED_MAX))
    _touch(seg, op)
    return None


def _apply_set_transition(work: EDL, op: EditOp) -> str | None:
    i = _find(work, op.segment_id)
    if i < 0:
        return f"set_transition: unknown segment_id {op.segment_id!r}"
    try:
        transition = Transition(op.transition)
    except ValueError:
        return f"set_transition: invalid transition {op.transition!r}"
    seg = work.timeline[i]
    seg.transition_in = transition
    seg.transition_duration_s = max(0.0, min(op.transition_duration_s, 3.0))
    _touch(seg, op)
    return None


def _apply_change_caption_style(work: EDL, op: EditOp) -> str | None:
    try:
        style = CaptionStyle(op.caption_style)
    except ValueError:
        return f"change_caption_style: invalid caption_style {op.caption_style!r}"
    work.captions.style = style
    return None


def _apply_set_target_duration(work: EDL, op: EditOp) -> str | None:
    target = op.target_duration_s
    if target is None or target <= 0 or target > 3600:
        return f"set_target_duration: invalid target_duration_s {target!r}"
    ceiling = target * (1 + work.brief.duration_tolerance_pct / 100.0)
    if work.total_duration_s > ceiling:
        kept = _trim_to_ceiling(work.timeline, ceiling, op)
        if not kept:
            return (
                f"set_target_duration: target {target}s too small to keep any segment"
            )
        work.timeline = kept
    work.brief.target_duration_s = float(target)
    return None


def _trim_to_ceiling(timeline: list[Segment], ceiling: float, op: EditOp) -> list[Segment]:
    """Trim trailing segments so total duration fits under `ceiling`.

    Mirrors the Editorial Agent's duration enforcement (implemented locally by design —
    that helper is private to editorial).
    """
    kept: list[Segment] = []
    total = 0.0
    for seg in timeline:
        if total + seg.timeline_duration_s > ceiling:
            overshoot = ceiling - total
            if overshoot > _MIN_TAIL_KEEP_S:
                trimmed = seg.model_copy()
                trimmed.out = round(seg.in_ + overshoot * seg.speed, 3)
                if trimmed.out > trimmed.in_:
                    _touch(trimmed, op)
                    kept.append(trimmed)
            break
        kept.append(seg)
        total += seg.timeline_duration_s
    return kept


_APPLIERS: dict[str, Callable[[EDL, EditOp], str | None]] = {
    "remove_segment": _apply_remove,
    "trim": _apply_trim,
    "reorder": _apply_reorder,
    "retime": _apply_retime,
    "set_transition": _apply_set_transition,
    "change_caption_style": _apply_change_caption_style,
    "set_target_duration": _apply_set_target_duration,
}
