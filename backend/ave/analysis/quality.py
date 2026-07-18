"""Quality heuristics: silence/dead-air, filler words, and highlight ranking.

Silence detection uses ffmpeg's `silencedetect` (no extra deps). Filler detection uses
the transcript. Highlight ranking combines audio energy and information density; when no
transcript exists we rank on shot duration/position as a coarse proxy. Blur/shake scoring
(Laplacian variance / optical-flow) is stubbed behind the same graceful-degradation
contract and only runs when opencv is present.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from ave.analysis.manifest import (
    Highlight,
    QualityWindow,
    Shot,
    TranscriptSegment,
)

_SILENCE_RE = re.compile(r"silence_(start|end):\s*([0-9.]+)")


def detect_silence(path: str, noise_db: float = -30.0, min_dur_s: float = 0.6) -> list[QualityWindow]:
    """Detect dead-air windows via ffmpeg silencedetect. Empty list if ffmpeg missing."""
    if not shutil.which("ffmpeg"):
        return []
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur_s}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception:
        return []

    windows: list[QualityWindow] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(proc.stderr):
        v = float(value)
        if kind == "start":
            start = v
        elif kind == "end" and start is not None:
            windows.append(QualityWindow(start_s=start, end_s=v, kind="silence", score=1.0))
            start = None
    return windows


def detect_filler(transcript: list[TranscriptSegment]) -> list[QualityWindow]:
    """Flag filler-word spans from the transcript."""
    out: list[QualityWindow] = []
    for seg in transcript:
        for w in seg.words:
            if w.is_filler:
                out.append(
                    QualityWindow(start_s=w.start_s, end_s=w.end_s, kind="filler", score=1.0)
                )
    return out


def rank_highlights(
    shots: list[Shot],
    transcript: list[TranscriptSegment],
    duration_s: float,
    *,
    top_k: int = 8,
) -> list[Highlight]:
    """Score candidate moments so the editor can open on the strongest hook.

    Score = 0.5*info_density + 0.3*energy + 0.2*recency-of-position bonus for early,
    information-dense content. With no transcript, info_density is 0 and ranking leans
    on shot length + position (a reasonable proxy for "a real moment").
    """
    highlights: list[Highlight] = []
    for shot in shots:
        text, words = _text_in(shot, transcript)
        dur = max(shot.duration_s, 1e-3)
        info_density = min(1.0, words / dur / 3.0)  # ~3 wps saturates
        # Longer, contentful shots read as more "energetic" without motion analysis.
        energy = min(1.0, dur / 6.0)
        # Slight preference for earlier material as hook candidates.
        pos_bonus = 1.0 - (shot.start_s / duration_s if duration_s else 0.0)
        score = 0.5 * info_density + 0.3 * energy + 0.2 * pos_bonus
        highlights.append(
            Highlight(
                start_s=shot.start_s,
                end_s=shot.end_s,
                energy=round(energy, 3),
                info_density=round(info_density, 3),
                text=text[:200],
                score=round(score, 4),
            )
        )
    highlights.sort(key=lambda h: h.score, reverse=True)
    return highlights[:top_k]


def _text_in(shot: Shot, transcript: list[TranscriptSegment]) -> tuple[str, int]:
    parts: list[str] = []
    words = 0
    for seg in transcript:
        if seg.end_s < shot.start_s or seg.start_s > shot.end_s:
            continue
        parts.append(seg.text)
        words += len(seg.words) or len(seg.text.split())
    return " ".join(parts).strip(), words
