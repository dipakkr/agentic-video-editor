"""A-roll / B-roll classification heuristics.

A clip that is mostly continuous speech is A-roll (the narrative spine); everything
else — ambience, cutaway footage, scenics — is B-roll. The proxy is *speech density*:
transcript word count divided by clip duration. At or above 0.5 words/second a clip is
"aroll", below that "broll". Purely deterministic: same manifests, same answer.
"""

from __future__ import annotations

from ave.analysis.manifest import ClipManifest

# Words-per-second at or above which a clip counts as A-roll (talking footage).
AROLL_DENSITY_THRESHOLD = 0.5


def speech_density(manifest: ClipManifest) -> float:
    """Transcript words per second of clip duration, rounded to 3 decimals.

    Word count per transcript segment is ``len(seg.words)`` when word-level timings
    exist, else a whitespace split of the segment text. Returns 0.0 when there is no
    transcript or the probed duration is zero/unknown (graceful degradation).
    """
    duration = manifest.probe.duration_s
    if not manifest.transcript or duration <= 0:
        return 0.0
    words = 0
    for seg in manifest.transcript:
        words += len(seg.words) if seg.words else len(seg.text.split())
    return round(words / duration, 3)


def classify_clips(manifests: list[ClipManifest]) -> dict[str, str]:
    """Map clip_id -> "aroll" | "broll", iterating deterministically in clip_id order."""
    out: dict[str, str] = {}
    for manifest in sorted(manifests, key=lambda m: m.clip_id):
        density = speech_density(manifest)
        out[manifest.clip_id] = "aroll" if density >= AROLL_DENSITY_THRESHOLD else "broll"
    return out
