"""Beat grid, downbeat detection, and the beat-snap pass.

This is the product's differentiator, so the snapping *policy* lives in pure, tested
Python independent of any audio library:

  * `BeatGrid` holds beat times + which beats are downbeats + a per-beat energy envelope.
  * `snap_time` snaps a single cut to the nearest beat **within tolerance**, preferring
    downbeats when one is comparably close (major transitions should land on downbeats).
  * `snap_edl` rewrites an EDL's segment boundaries onto the grid and records a sync map.

librosa/madmom populate the grid (`grid_from_librosa`); when they're absent the caller
supplies a synthetic constant-BPM grid, so snapping is always exercisable in tests.
A cut that lands 200 ms off-beat feels wrong — the tolerance default is 180 ms.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field

from ave.edl.schema import EDL, SyncPoint


@dataclass
class BeatGrid:
    beats: list[float]                       # ascending beat onset times (s)
    downbeats: list[float] = field(default_factory=list)
    energy: list[float] = field(default_factory=list)  # per-beat energy, aligned to `beats`
    bpm: float = 0.0

    def __post_init__(self) -> None:
        self.beats = sorted(self.beats)
        self._downbeat_set = set(round(d, 4) for d in self.downbeats)

    def is_downbeat(self, t: float) -> bool:
        return round(t, 4) in self._downbeat_set


@dataclass
class SnapResult:
    original_s: float
    snapped_s: float
    snapped: bool
    to_downbeat: bool

    @property
    def delta_ms(self) -> float:
        return round((self.snapped_s - self.original_s) * 1000.0, 2)


def _nearest(sorted_vals: list[float], t: float) -> float | None:
    """Nearest value in a sorted list, or None if empty."""
    if not sorted_vals:
        return None
    i = bisect_left(sorted_vals, t)
    if i == 0:
        return sorted_vals[0]
    if i == len(sorted_vals):
        return sorted_vals[-1]
    before, after = sorted_vals[i - 1], sorted_vals[i]
    return before if (t - before) <= (after - t) else after


def snap_time(
    t: float,
    grid: BeatGrid,
    *,
    tolerance_ms: int = 180,
    prefer_downbeat: bool = False,
    downbeat_bias_ms: int = 90,
) -> SnapResult:
    """Snap `t` to the nearest beat within tolerance.

    When `prefer_downbeat` (major transitions), a downbeat is chosen over a closer
    regular beat as long as it's within `tolerance_ms` and no more than
    `downbeat_bias_ms` further away than the nearest regular beat. Outside tolerance we
    leave the cut untouched rather than yank it audibly off its content.
    """
    tol = tolerance_ms / 1000.0
    nearest = _nearest(grid.beats, t)
    if nearest is None:
        return SnapResult(t, t, snapped=False, to_downbeat=False)

    best, is_down = nearest, grid.is_downbeat(nearest)

    if prefer_downbeat and grid.downbeats:
        nd = _nearest(grid.downbeats, t)
        if nd is not None and abs(nd - t) <= tol:
            bias = downbeat_bias_ms / 1000.0
            if abs(nd - t) <= abs(nearest - t) + bias:
                best, is_down = nd, True

    if abs(best - t) <= tol:
        return SnapResult(t, round(best, 4), snapped=True, to_downbeat=is_down)
    return SnapResult(t, t, snapped=False, to_downbeat=False)


def snap_edl(edl: EDL, grid: BeatGrid, *, tolerance_ms: int | None = None) -> EDL:
    """Return a new EDL with cut points snapped to the beat grid + a populated sync map.

    Timeline boundaries (not source in/out points) are what the ear judges, so we snap
    each segment's *timeline start* and absorb the delta by trimming its source `in`.
    The first segment of a major transition (non-crossfade, or first segment) prefers a
    downbeat. Determinism preserved: same EDL + same grid => same output.
    """
    tol = tolerance_ms if tolerance_ms is not None else 180
    segments = [s.model_copy(deep=True) for s in edl.timeline]
    sync: list[SyncPoint] = []

    timeline_cursor = 0.0
    for idx, seg in enumerate(segments):
        prefer_down = idx == 0 or seg.transition_in.value in ("hard", "whip", "fade_from_black")
        res = snap_time(
            timeline_cursor, grid, tolerance_ms=tol, prefer_downbeat=prefer_down
        )
        if res.snapped and idx > 0:
            # Shift the timeline boundary by trimming the source in-point, keeping the
            # out-point fixed so we never read past the clip. Guard against inversion.
            shift = res.snapped_s - timeline_cursor
            new_in = max(0.0, seg.in_ + shift * seg.speed)
            if new_in < seg.out:
                seg.in_ = round(new_in, 4)
            seg.cut_snapped_to_beat = True
            seg.snapped_beat_s = res.snapped_s
            timeline_cursor = res.snapped_s
        elif res.snapped:
            seg.cut_snapped_to_beat = True
            seg.snapped_beat_s = res.snapped_s

        if res.snapped:
            sync.append(
                SyncPoint(
                    beat_s=res.snapped_s,
                    timeline_s=round(timeline_cursor, 4),
                    is_downbeat=res.to_downbeat,
                )
            )
        timeline_cursor += seg.timeline_duration_s

    new = edl.model_copy(deep=True)
    new.timeline = segments
    new.music.sync_map = sync
    return new.bump(notes="beat-sync pass")


def grid_from_librosa(audio_path: str) -> BeatGrid | None:
    """Build a BeatGrid from an audio file using librosa. None if librosa is absent."""
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None
    try:
        y, sr = librosa.load(audio_path, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        # Downbeats: approximate as every 4th beat (4/4). madmom would do this precisely.
        downbeats = beats[::4]
        rms = librosa.feature.rms(y=y)[0]
        energy = [float(rms[min(f, len(rms) - 1)]) for f in beat_frames]
        peak = max(energy) if energy else 1.0
        energy = [e / peak for e in energy] if peak else energy
        return BeatGrid(beats=beats, downbeats=downbeats, energy=energy, bpm=float(tempo))
    except Exception:
        return None


def synthetic_grid(bpm: float, duration_s: float, *, beats_per_bar: int = 4) -> BeatGrid:
    """A constant-tempo grid — deterministic, dependency-free, used for tests/fallback."""
    interval = 60.0 / bpm
    n = int(duration_s / interval) + 1
    beats = [round(i * interval, 4) for i in range(n)]
    downbeats = beats[::beats_per_bar]
    return BeatGrid(beats=beats, downbeats=downbeats, energy=[1.0] * len(beats), bpm=bpm)
