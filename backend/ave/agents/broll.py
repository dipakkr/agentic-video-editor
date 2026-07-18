"""B-roll Agent — plans muted cutaway overlays over long talking segments.

Classification: clips are split into A-roll (speech density >= 0.5 words/s — the
talking spine of the edit) and B-roll (everything else) by ``ave.broll.classify``.

Placement policy (deterministic, pure):
  * Only timeline segments sourced from A-roll clips and lasting at least
    ``min_talking_s`` on the timeline receive cutaways.
  * The first cutaway starts 2.0s into the segment (let the speaker establish),
    subsequent ones repeat every ``overlay_len_s + gap_s``, and every cutaway must
    fit entirely at least 1.5s before the segment's timeline end (never cover the
    cut point — the outgoing frame should be the speaker).
  * Source material comes from a global pool: B-roll clips visited round-robin in
    clip_id order; within a clip, successive non-overlapping ``overlay_len_s`` slices
    of its usable windows. Windows already consumed by timeline segments of the same
    clip are excluded, and no source window is ever used twice. When the pool runs
    dry, placement stops — repetitive b-roll is worse than none.

The pass is a no-op (same EDL object, no version bump) when the brief disables
b-roll, when there are no B-roll clips, or when nothing could be placed.
"""

from __future__ import annotations

from ave.analysis.manifest import ClipManifest
from ave.broll.classify import classify_clips
from ave.edl.schema import EDL, Overlay

# Delay before the first cutaway within a talking segment (seconds).
_START_DELAY_S = 2.0
# A cutaway must end at least this long before the segment's timeline end.
_TAIL_CLEARANCE_S = 1.5
_EPS = 1e-9


def _free_intervals(
    start: float, end: float, used: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Remove `used` source intervals from [start, end]; return surviving pieces."""
    cuts = sorted((max(start, a), min(end, b)) for a, b in used)
    result: list[tuple[float, float]] = []
    cursor = start
    for a, b in cuts:
        if b <= cursor or a >= end:
            continue
        if a > cursor:
            result.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < end:
        result.append((cursor, end))
    return result


def _slots_for_clip(
    manifest: ClipManifest, used: list[tuple[float, float]], overlay_len_s: float
) -> list[tuple[float, float]]:
    """Successive non-overlapping overlay-length source windows, in time order.

    Draws from ``usable_windows()`` (which itself falls back to [0, duration]),
    minus windows already consumed by the timeline. Pieces shorter than
    ``overlay_len_s`` are skipped.
    """
    slots: list[tuple[float, float]] = []
    for window in manifest.usable_windows():
        for a, b in _free_intervals(window.start_s, window.end_s, used):
            t = a
            while t + overlay_len_s <= b + _EPS:
                slots.append((round(t, 3), round(t + overlay_len_s, 3)))
                t += overlay_len_s
    return slots


class _RoundRobinPool:
    """Hands out source windows round-robin across clips; never reuses a window."""

    def __init__(self, slots_by_clip: dict[str, list[tuple[float, float]]]) -> None:
        self._order = sorted(slots_by_clip)
        self._slots = {cid: list(slots_by_clip[cid]) for cid in self._order}
        self._next = 0

    def take(self) -> tuple[str, tuple[float, float]] | None:
        for _ in range(len(self._order)):
            clip_id = self._order[self._next]
            self._next = (self._next + 1) % len(self._order)
            if self._slots[clip_id]:
                return clip_id, self._slots[clip_id].pop(0)
        return None


def plan_overlays(
    edl: EDL,
    manifests: list[ClipManifest],
    *,
    min_talking_s: float = 6.0,
    overlay_len_s: float = 3.0,
    gap_s: float = 4.0,
) -> EDL:
    """The B-roll Agent pass: place muted cutaways over long talking segments.

    Pure: never mutates ``edl``. Returns a bumped deep copy with overlays set when
    anything was placed, otherwise the input object unchanged.
    """
    if not edl.brief.agent_config.enable_broll:
        return edl

    classes = classify_clips(manifests)
    by_id = {m.clip_id: m for m in manifests}

    # Source windows already consumed by the timeline, per clip — off limits.
    used_by_clip: dict[str, list[tuple[float, float]]] = {}
    for seg in edl.timeline:
        used_by_clip.setdefault(seg.source_clip, []).append((seg.in_, seg.out))

    slots_by_clip = {
        clip_id: _slots_for_clip(by_id[clip_id], used_by_clip.get(clip_id, []), overlay_len_s)
        for clip_id, kind in classes.items()
        if kind == "broll"
    }
    if not slots_by_clip:  # no b-roll clips at all
        return edl

    pool = _RoundRobinPool(slots_by_clip)
    overlays: list[Overlay] = []
    exhausted = False
    for seg in edl.timeline:
        if exhausted:
            break
        if classes.get(seg.source_clip) != "aroll":
            continue
        if seg.timeline_duration_s < min_talking_s:
            continue
        seg_start = edl.timeline_offset_of(seg.id)
        seg_end = seg_start + seg.timeline_duration_s
        t = seg_start + _START_DELAY_S
        while t + overlay_len_s <= seg_end - _TAIL_CLEARANCE_S + _EPS:
            slot = pool.take()
            if slot is None:
                exhausted = True
                break
            clip_id, (src_in, src_out) = slot
            overlays.append(
                Overlay(
                    id=f"ovl_{len(overlays) + 1:02d}",
                    source_clip=clip_id,
                    in_=src_in,
                    out=src_out,
                    timeline_start_s=round(t, 3),
                    mute=True,
                    reason=(
                        f"Cutaway over long talking segment {seg.id} — "
                        f"visual relief from {clip_id}"
                    ),
                )
            )
            t += overlay_len_s + gap_s

    if not overlays:
        return edl
    return edl.model_copy(deep=True, update={"overlays": overlays}).bump(notes="b-roll pass")
