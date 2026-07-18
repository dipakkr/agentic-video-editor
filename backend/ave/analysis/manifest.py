"""Per-clip analysis manifest — the Ingest & Analysis Agent's structured output.

The Editorial Agent reads *only* manifests (never raw media), so this is the
contract between analysis and planning. Everything here is JSON-serialisable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProbeInfo(BaseModel):
    """Container/stream facts from ffprobe."""

    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    audio_channels: int = 0
    has_audio: bool = False


class Shot(BaseModel):
    """A shot boundary from scene detection."""

    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class Word(BaseModel):
    """Word-level transcript token (WhisperX)."""

    word: str
    start_s: float
    end_s: float
    speaker: str | None = None
    is_filler: bool = False


class TranscriptSegment(BaseModel):
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None
    words: list[Word] = Field(default_factory=list)


class QualityWindow(BaseModel):
    """A time window flagged by quality heuristics."""

    start_s: float
    end_s: float
    kind: str  # "silence" | "filler" | "shaky" | "blurry"
    score: float = 0.0


class Highlight(BaseModel):
    """A candidate strong moment (potential hook / key beat)."""

    start_s: float
    end_s: float
    energy: float = 0.0            # audio/motion energy, 0..1
    info_density: float = 0.0      # words-per-second proxy, 0..1
    text: str = ""
    score: float = 0.0             # combined rank used by the editor

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class ClipManifest(BaseModel):
    """Everything analysis knows about one source clip."""

    clip_id: str
    source_path: str
    proxy_path: str | None = None
    probe: ProbeInfo = Field(default_factory=ProbeInfo)
    shots: list[Shot] = Field(default_factory=list)
    transcript: list[TranscriptSegment] = Field(default_factory=list)
    quality_flags: list[QualityWindow] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    # Which optional passes actually ran (provenance for graceful degradation).
    analysis_features: dict[str, bool] = Field(default_factory=dict)

    @property
    def transcript_text(self) -> str:
        return " ".join(seg.text for seg in self.transcript).strip()

    def usable_windows(self) -> list[Shot]:
        """Shots minus flagged silence/filler regions — the editor's candidate pool.

        Falls back to the whole clip if scene detection did not run.
        """
        base = self.shots or [Shot(start_s=0.0, end_s=self.probe.duration_s or 0.0)]
        bad = [f for f in self.quality_flags if f.kind in ("silence", "filler")]
        if not bad:
            return base
        out: list[Shot] = []
        for shot in base:
            windows = _subtract(shot.start_s, shot.end_s, bad)
            out.extend(Shot(start_s=a, end_s=b) for a, b in windows if b - a > 0.4)
        return out or base


def _subtract(start: float, end: float, flags: list[QualityWindow]) -> list[tuple[float, float]]:
    """Remove flagged intervals from [start, end], returning surviving sub-intervals."""
    cuts = sorted((max(start, f.start_s), min(end, f.end_s)) for f in flags)
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
