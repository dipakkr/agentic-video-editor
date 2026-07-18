"""Beat grids and drop-point math for library tracks.

Bridges track metadata (`TrackMeta`) to the beat-snap machinery (`ave.beat.snap`):

  * `grid_for_track` prefers a real librosa analysis of the audio file when both the
    file and librosa are available; otherwise it degrades to a deterministic synthetic
    constant-BPM grid built from the metadata's BPM/duration, shifted by the track's
    known ``downbeat_offset_s`` so bar boundaries still line up with the recording.
  * `drop_time` locates the "drop" — the energy-curve maximum — which the Music Agent
    aligns with the edit's strongest highlight.

Everything here is dependency-free and deterministic when librosa is absent.
"""

from __future__ import annotations

from ave.beat.snap import BeatGrid, grid_from_librosa, synthetic_grid
from ave.music.library import TrackMeta


def grid_for_track(meta: TrackMeta, audio_path: str | None = None) -> BeatGrid:
    """Build a BeatGrid for a track, preferring real audio analysis.

    Falls back to a synthetic grid at ``meta.bpm`` shifted by ``meta.downbeat_offset_s``
    (dropping any beats shifted below t=0) whenever librosa is missing, the file is
    unreadable, or no ``audio_path`` was given. Never raises.
    """
    if audio_path:
        try:
            grid = grid_from_librosa(audio_path)
        except Exception:
            grid = None
        if grid is not None:
            return grid

    base = synthetic_grid(meta.bpm, meta.duration_s)
    offset = meta.downbeat_offset_s
    if not offset:
        return base
    beats = [round(b + offset, 4) for b in base.beats if b + offset >= 0.0]
    downbeats = [round(d + offset, 4) for d in base.downbeats if d + offset >= 0.0]
    return BeatGrid(beats=beats, downbeats=downbeats, energy=[1.0] * len(beats), bpm=meta.bpm)


def drop_time(meta: TrackMeta) -> float:
    """Time (s) of the track's energy peak: ``argmax(curve)/len(curve) * duration``.

    Empty curve -> 0.0. On tied maxima the first occurrence wins (determinism).
    """
    curve = meta.energy_curve
    if not curve:
        return 0.0
    idx = curve.index(max(curve))
    return idx / len(curve) * meta.duration_s
