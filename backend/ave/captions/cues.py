"""Cue building — remap transcript words from source-clip time to timeline time.

The transcript in each :class:`~ave.analysis.manifest.ClipManifest` is expressed in
*source-clip* seconds. The timeline is a concatenation of speed-adjusted segment
slices, so every word that survives a cut must be shifted and rescaled into timeline
time before it can become a caption cue. This module owns that remap plus the two
grouping strategies (sentence-level and phrase-level) that the caption styles need.

All times on :class:`Cue` / :class:`CueWord` are TIMELINE times, never source times.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ave.analysis.manifest import ClipManifest, Word
from ave.edl.schema import EDL, CaptionStyle, Segment

# Phrase-mode grouping limits: a phrase cue never exceeds this many words or span.
PHRASE_MAX_WORDS = 5
PHRASE_MAX_SPAN_S = 2.2


class CueWord(BaseModel):
    """One word of a cue, in timeline time (for karaoke-style per-word timing)."""

    word: str
    start_s: float
    end_s: float


class Cue(BaseModel):
    """One caption cue on the timeline. All times are timeline times."""

    start_s: float
    end_s: float
    text: str
    words: list[CueWord] = Field(default_factory=list)


def mode_for_style(style: CaptionStyle) -> str:
    """Grouping mode a caption style needs: word-timed styles want phrases."""
    if style in (CaptionStyle.karaoke_bold, CaptionStyle.phrase_pop):
        return "phrase"
    return "sentence"


def _remap_word(word: Word, seg: Segment, offset: float) -> CueWord:
    """Shift/rescale one source-time word into timeline time, clamped to the segment."""
    end_of_segment = offset + seg.timeline_duration_s

    def remap(t: float) -> float:
        mapped = offset + (t - seg.in_) / seg.speed
        return round(min(max(mapped, offset), end_of_segment), 3)

    start = remap(word.start_s)
    end = max(remap(word.end_s), start)
    return CueWord(word=word.word, start_s=start, end_s=end)


def _cue_from_words(words: list[CueWord]) -> Cue:
    return Cue(
        start_s=words[0].start_s,
        end_s=words[-1].end_s,
        text=" ".join(w.word for w in words),
        words=list(words),
    )


def _phrase_groups(words: list[CueWord]) -> list[list[CueWord]]:
    """Greedy grouping: <= PHRASE_MAX_WORDS words and <= PHRASE_MAX_SPAN_S span."""
    groups: list[list[CueWord]] = []
    current: list[CueWord] = []
    for w in words:
        if current and (
            len(current) >= PHRASE_MAX_WORDS
            or w.end_s - current[0].start_s > PHRASE_MAX_SPAN_S
        ):
            groups.append(current)
            current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def build_cues(edl: EDL, manifests: list[ClipManifest], mode: str = "sentence") -> list[Cue]:
    """Build timeline-time caption cues for every segment of the EDL.

    For each timeline segment, transcript words whose source midpoint falls within
    the segment's [in, out] slice are remapped into timeline time (offset by the
    segment's timeline start, rescaled by 1/speed, clamped to the segment bounds).
    Filler words are dropped. Segments with no manifest or no transcript contribute
    nothing (graceful degradation). Returns cues sorted by start time.
    """
    by_id = {m.clip_id: m for m in manifests}
    cues: list[Cue] = []

    for seg in edl.timeline:
        manifest = by_id.get(seg.source_clip)
        if manifest is None or not manifest.transcript:
            continue
        offset = edl.timeline_offset_of(seg.id)

        kept_per_sentence: list[list[CueWord]] = []
        for tseg in manifest.transcript:
            kept: list[CueWord] = []
            for word in tseg.words:
                if word.is_filler:
                    continue
                mid = (word.start_s + word.end_s) / 2.0
                if not (seg.in_ <= mid <= seg.out):
                    continue
                kept.append(_remap_word(word, seg, offset))
            if kept:
                kept_per_sentence.append(kept)

        if mode == "phrase":
            flat = [w for sentence in kept_per_sentence for w in sentence]
            cues.extend(_cue_from_words(g) for g in _phrase_groups(flat))
        else:
            cues.extend(_cue_from_words(sentence) for sentence in kept_per_sentence)

    cues.sort(key=lambda c: c.start_s)
    return cues
