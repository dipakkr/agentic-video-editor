"""Music & Beat Agent — score the edit and cut it to the music.

The pass takes a planned EDL plus the analysis manifests and returns a new EDL revision
with music applied:

  1. **Track selection** — `pick_track` resolves the brief's pin / tone-based auto-pick
     against the on-disk library. No suitable track means *no music*: the EDL is
     returned untouched (same object, no version bump) so the pipeline degrades
     gracefully instead of failing.
  2. **Beat snap** — a `BeatGrid` for the chosen track (real librosa analysis when the
     audio file + librosa exist, synthetic constant-BPM grid otherwise) is fed to
     `snap_edl`, which nudges segment boundaries onto beats within the configured
     tolerance and records the beat<->timeline sync map. `snap_edl` bumps the version.
  3. **Drop alignment** — the strongest manifest highlight that actually made it into
     the timeline is located, its timeline position computed, and the music's start
     offset chosen so the track's energy peak (`drop_time`) lands on that moment.
     ``offset_s`` is clamped to >= 0 (we never pad silence before the track); with no
     usable highlight the track simply starts from the top.
  4. **Mix defaults** — ducking on, schema-default duck level, 0.5 s fade-in,
     1.5 s fade-out.

Deterministic: same EDL + manifests + library => same output. Never raises for missing
optional pieces (no library dir, no audio files, no highlights, no librosa).
"""

from __future__ import annotations

from pathlib import Path

from ave.analysis.manifest import ClipManifest, Highlight
from ave.beat.snap import snap_edl
from ave.config import Settings, get_settings
from ave.edl.schema import EDL
from ave.music.grid import drop_time, grid_for_track
from ave.music.library import load_library, pick_track


def apply_music(
    edl: EDL,
    manifests: list[ClipManifest],
    library_dir: Path,
    settings: Settings | None = None,
) -> EDL:
    """Run the Music & Beat pass. Returns a new EDL revision, or `edl` itself if no
    track is available (graceful no-music path)."""
    settings = settings if settings is not None else get_settings()

    track = pick_track(edl.brief, load_library(library_dir))
    if track is None:
        return edl

    audio = library_dir / track.file if track.file else None
    audio_path = str(audio) if audio is not None and audio.is_file() else None
    grid = grid_for_track(track, audio_path=audio_path)

    snapped = snap_edl(edl, grid, tolerance_ms=settings.ave_beat_snap_tolerance_ms)

    snapped.music.track_id = track.track_id
    snapped.music.offset_s = _drop_offset(snapped, manifests, drop_time(track))
    snapped.music.ducking = True
    snapped.music.fade_in_s = 0.5
    snapped.music.fade_out_s = 1.5
    return snapped


def _drop_offset(edl: EDL, manifests: list[ClipManifest], drop_s: float) -> float:
    """Music start offset that puts the track's drop on the best in-timeline highlight.

    Scans every manifest highlight whose source window overlaps a timeline segment of
    that clip; the highest score wins (first occurrence on ties — determinism). Returns
    ``max(0, drop_s - highlight_timeline_position)``; 0.0 when nothing qualifies.
    """
    best: tuple[float, float] | None = None  # (score, timeline_s)
    for manifest in manifests:
        for hl in manifest.highlights:
            placed = _timeline_position(edl, manifest.clip_id, hl)
            if placed is None:
                continue
            if best is None or hl.score > best[0]:
                best = (hl.score, placed)
    if best is None:
        return 0.0
    return max(0.0, drop_s - best[1])


def _timeline_position(edl: EDL, clip_id: str, hl: Highlight) -> float | None:
    """Timeline time of a highlight's start, or None if no segment contains it.

    Uses the first (timeline-order) segment of `clip_id` whose source window overlaps
    the highlight, mapping the highlight start through the segment's speed and clamping
    into the segment's timeline span.
    """
    for seg in edl.timeline:
        if seg.source_clip != clip_id:
            continue
        if hl.start_s < seg.out and hl.end_s > seg.in_:
            start = edl.timeline_offset_of(seg.id)
            pos = start + (hl.start_s - seg.in_) / seg.speed
            return min(max(pos, start), start + seg.timeline_duration_s)
    return None


__all__ = ["apply_music"]
