"""Music library — track metadata loading and tone-driven track selection.

Tracks live as sidecar ``*.json`` files next to the audio in ``assets/music``. The
library layer is deliberately dumb and deterministic: metadata in, one track out.

Selection policy (`pick_track`):

  1. An explicit ``brief.music_track_id`` pin always wins; if the pinned id is absent
     from the library we return ``None`` rather than silently substituting a track.
  2. Otherwise, when ``brief.auto_pick_music`` is on, the brief's tone maps to a
     preferred genre (energetic->electronic, cinematic->cinematic, tutorial->ambient,
     vlog->pop). An exact genre match is taken first; failing that, the track whose BPM
     is closest to the tone's default tempo wins. All ties break on ``track_id`` so the
     same brief + library always yields the same track.
  3. No auto-pick and no pin -> no music (``None``). Graceful degradation everywhere:
     a missing library directory simply yields an empty library.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from ave.edl.schema import Brief, Tone

# Tone -> preferred genre for auto-pick.
TONE_GENRE: dict[Tone, str] = {
    Tone.energetic: "electronic",
    Tone.cinematic: "cinematic",
    Tone.tutorial: "ambient",
    Tone.vlog: "pop",
}

# Tone -> default tempo used for the BPM-closest fallback.
TONE_BPM: dict[Tone, float] = {
    Tone.energetic: 128.0,
    Tone.cinematic: 90.0,
    Tone.tutorial: 100.0,
    Tone.vlog: 110.0,
}


class TrackMeta(BaseModel):
    """Metadata for one library track (mirrors the sidecar JSON files)."""

    track_id: str
    title: str = ""
    artist: str = ""
    genre: str = ""
    bpm: float
    duration_s: float
    energy_curve: list[float] = Field(default_factory=list)
    downbeat_offset_s: float = 0.0
    license: str = ""
    source: str = ""
    file: str = ""


def load_library(dir: Path) -> list[TrackMeta]:
    """Load all track metadata files from ``dir``.

    Only ``*.json`` files containing both ``track_id`` and ``bpm`` keys are treated as
    track metadata (README/config JSON is skipped). Unreadable files are skipped rather
    than raised. Result is sorted by ``track_id`` for determinism. Missing dir -> [].
    """
    if not dir.is_dir():
        return []
    tracks: list[TrackMeta] = []
    for path in sorted(dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "track_id" not in data or "bpm" not in data:
            continue
        try:
            tracks.append(TrackMeta.model_validate(data))
        except Exception:
            continue
    tracks.sort(key=lambda t: t.track_id)
    return tracks


def pick_track(brief: Brief, library: list[TrackMeta]) -> TrackMeta | None:
    """Choose the track for a brief. See the module docstring for the policy."""
    if brief.music_track_id:
        for track in library:
            if track.track_id == brief.music_track_id:
                return track
        return None  # pinned track absent: no silent substitution

    if not brief.auto_pick_music or not library:
        return None

    preferred_genre = TONE_GENRE.get(brief.tone, "")
    genre_matches = sorted(
        (t for t in library if t.genre == preferred_genre), key=lambda t: t.track_id
    )
    if genre_matches:
        return genre_matches[0]

    target_bpm = TONE_BPM.get(brief.tone, 110.0)
    return min(library, key=lambda t: (abs(t.bpm - target_bpm), t.track_id))
