"""The Edit Decision List (EDL) — the single source of truth for a project.

Every agent reads and mutates the EDL; rendering is a *pure function* of the EDL
(same EDL + same assets => byte-identical output). Models are versioned and every
revision is persisted, which is what makes feedback-driven, incremental re-rendering
tractable: a diff between EDL versions tells the orchestrator exactly which downstream
steps must re-run.

The models below are the canonical definition. `edl.schema.json` is generated from
these via `python -m ave.edl.schema`.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# Enums                                                                        #
# --------------------------------------------------------------------------- #
class Platform(str, Enum):
    youtube = "youtube"
    reels = "reels"
    shorts = "shorts"
    tiktok = "tiktok"


class Tone(str, Enum):
    energetic = "energetic"
    cinematic = "cinematic"
    tutorial = "tutorial"
    vlog = "vlog"


class AspectRatio(str, Enum):
    wide = "16:9"
    vertical = "9:16"
    square = "1:1"


class Transition(str, Enum):
    """Cut/transition applied at the *start* of a segment.

    Hard cut is the default. J-cut / L-cut carry audio across the boundary
    (audio leads / trails the picture respectively).
    """

    hard = "hard"
    crossfade = "crossfade"
    whip = "whip"
    fade_from_black = "fade_from_black"
    j_cut = "j_cut"
    l_cut = "l_cut"


class CaptionStyle(str, Enum):
    karaoke_bold = "karaoke_bold"        # word-by-word highlight (Shorts/Reels/TikTok)
    phrase_pop = "phrase_pop"            # phrase-level pop-on
    clean_subtitle = "clean_subtitle"    # sentence-level lower-third (YouTube)
    none = "none"


# --------------------------------------------------------------------------- #
# Sub-documents                                                                #
# --------------------------------------------------------------------------- #
class Brief(BaseModel):
    """The user's request. Drives every downstream decision."""

    platform: Platform = Platform.youtube
    target_duration_s: float = Field(..., gt=0, le=3600)
    tone: Tone = Tone.energetic
    aspect_ratio: AspectRatio = AspectRatio.wide
    music_track_id: Optional[str] = Field(
        None, description="Explicit track id, or None for auto-pick / no music."
    )
    auto_pick_music: bool = True
    # ±tolerance the Editorial Agent must respect for total duration.
    duration_tolerance_pct: float = Field(10.0, ge=0, le=50)


class Segment(BaseModel):
    """One clip on the timeline, referencing a source clip's in/out points.

    `reason` is non-negotiable: the Editorial Agent must justify every cut. This
    makes the edit explainable and makes revision-by-feedback tractable (the LLM
    can locate the segment a user is describing from its reason).
    """

    id: str = Field(..., pattern=r"^seg_[0-9a-zA-Z_]+$")
    source_clip: str = Field(..., description="Id of the source clip in the manifest.")
    in_: float = Field(..., ge=0, alias="in", description="Source in-point (seconds).")
    out: float = Field(..., gt=0, description="Source out-point (seconds).")
    speed: float = Field(1.0, gt=0, le=8.0)
    transition_in: Transition = Transition.hard
    transition_duration_s: float = Field(0.0, ge=0, le=3.0)
    cut_snapped_to_beat: bool = False
    # Populated by the beat agent: the beat time this segment's start was snapped to.
    snapped_beat_s: Optional[float] = None
    reason: str = Field(..., min_length=1, description="Why the editor chose this segment.")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_bounds(self) -> "Segment":
        if self.out <= self.in_:
            raise ValueError(f"segment {self.id}: out ({self.out}) must be > in ({self.in_})")
        return self

    @property
    def source_duration_s(self) -> float:
        """Duration consumed from the source clip."""
        return self.out - self.in_

    @property
    def timeline_duration_s(self) -> float:
        """Duration this segment occupies on the timeline after speed change."""
        return self.source_duration_s / self.speed


class SyncPoint(BaseModel):
    """Maps a musical beat to a timeline position (beat-sync alignment)."""

    beat_s: float = Field(..., ge=0)
    timeline_s: float = Field(..., ge=0)
    is_downbeat: bool = False


class Music(BaseModel):
    track_id: Optional[str] = None
    offset_s: float = Field(0.0, ge=0, description="Where in the track playback starts.")
    ducking: bool = True
    duck_db: float = Field(-14.0, le=0, description="Music gain under dialogue (dB).")
    fade_in_s: float = Field(0.5, ge=0)
    fade_out_s: float = Field(1.5, ge=0)
    sync_map: list[SyncPoint] = Field(default_factory=list)


class Captions(BaseModel):
    style: CaptionStyle = CaptionStyle.clean_subtitle
    language: str = "en"
    font: str = "Inter"
    font_size: int = Field(48, gt=0)
    primary_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: int = Field(3, ge=0)
    # Normalized vertical position of the caption baseline (0=top, 1=bottom).
    position_y: float = Field(0.82, ge=0, le=1)
    emphasize_keywords: bool = True


class OutputSpec(BaseModel):
    """Render target derived from the brief; one EDL can produce several."""

    aspect_ratio: AspectRatio = AspectRatio.wide
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    target_lufs: float = -14.0
    # Preview renders use the low-res proxy; full-res only on final render.
    use_proxy: bool = True
    # How sources that don't match the canvas aspect are fitted: letterbox pad (safe
    # default) or center-crop (fills the frame — used for 9:16/1:1 variants; face/subject
    # tracking upgrades the crop anchor in M5's reframe work).
    reframe: Literal["pad", "center_crop"] = "pad"


# --------------------------------------------------------------------------- #
# Root document                                                                #
# --------------------------------------------------------------------------- #
class EDL(BaseModel):
    """The versioned Edit Decision List."""

    schema_version: str = SCHEMA_VERSION
    project_id: str
    version: int = Field(1, ge=1, description="Monotonic revision number.")
    brief: Brief
    timeline: list[Segment] = Field(default_factory=list)
    music: Music = Field(default_factory=Music)
    captions: Captions = Field(default_factory=Captions)
    output: OutputSpec = Field(default_factory=OutputSpec)
    # Free-form provenance: which agent produced this revision and why.
    notes: str = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _unique_segment_ids(self) -> "EDL":
        ids = [s.id for s in self.timeline]
        if len(ids) != len(set(ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"duplicate segment ids: {sorted(dupes)}")
        return self

    # -- derived properties ------------------------------------------------- #
    @property
    def total_duration_s(self) -> float:
        return round(sum(s.timeline_duration_s for s in self.timeline), 3)

    def within_target(self) -> bool:
        tol = self.brief.target_duration_s * self.brief.duration_tolerance_pct / 100.0
        return abs(self.total_duration_s - self.brief.target_duration_s) <= tol

    def timeline_offset_of(self, segment_id: str) -> float:
        """Timeline start time of a segment (sum of preceding durations)."""
        offset = 0.0
        for seg in self.timeline:
            if seg.id == segment_id:
                return round(offset, 3)
            offset += seg.timeline_duration_s
        raise KeyError(segment_id)

    def content_hash(self) -> str:
        """Deterministic hash of render-affecting content.

        Excludes `version`/`notes` so two structurally identical EDLs render to the
        same output and can be de-duplicated. Backs the determinism guarantee.
        """
        payload = self.model_dump(mode="json", by_alias=True, exclude={"version", "notes"})
        blob = _canonical_json(payload)
        return hashlib.sha256(blob.encode()).hexdigest()

    def bump(self, notes: str = "") -> "EDL":
        """Return a new revision (version + 1). EDLs are treated as immutable."""
        return self.model_copy(update={"version": self.version + 1, "notes": notes})


def _canonical_json(obj: object) -> str:
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def json_schema() -> dict:
    """JSON Schema for the root EDL document (for external validators / the UI)."""
    return EDL.model_json_schema(by_alias=True)


if __name__ == "__main__":
    import json
    import pathlib

    out = pathlib.Path(__file__).with_name("edl.schema.json")
    out.write_text(json.dumps(json_schema(), indent=2))
    print(f"wrote {out}")
