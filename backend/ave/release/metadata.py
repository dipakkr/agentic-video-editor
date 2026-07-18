"""Deterministic release-metadata generators: titles, chapters, description, hashtags,
and thumbnail candidates.

Everything here is a pure function of (EDL, manifests) — no LLM, no randomness, no I/O —
so the release kit always has a reproducible baseline even with zero external services.
The LLM-polished path lives in `ave.agents.release` and treats these outputs as seeds.
"""

from __future__ import annotations

import re
from collections import Counter

from ave.analysis.manifest import ClipManifest, Highlight
from ave.edl.schema import EDL, Segment

_TITLE_MAX_CHARS = 90

# Honest, platform-flavored bracket suffixes for the third title variant.
_PLATFORM_BENEFIT: dict[str, str] = {
    "youtube": "Full Breakdown",
    "shorts": "Quick Watch",
    "reels": "In Under a Minute",
    "tiktok": "Watch to the End",
}

# Small built-in stopword set for hashtag keyword extraction (length >= 4 words only,
# so shorter function words never reach the filter).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "this", "that", "with", "have", "from", "your", "about", "just", "like",
        "they", "them", "then", "than", "what", "when", "where", "which", "will",
        "would", "could", "should", "there", "their", "these", "those", "really",
        "going", "because", "been", "were", "here", "some", "more", "very", "into",
        "over", "only", "also", "well", "want", "make", "made", "know", "think",
        "thing", "things", "gonna", "yeah", "okay", "right", "actually", "little",
        "much", "many", "every", "other", "being", "doing", "does", "dont", "youre",
        "thats", "its", "weve", "were", "lets",
    }
)

_PLATFORM_TAGS: dict[str, list[str]] = {
    "youtube": ["youtube"],
    "shorts": ["shorts", "youtubeshorts"],
    "reels": ["reels", "instagram"],
    "tiktok": ["tiktok", "fyp"],
}


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #
def _manifest_by_clip(manifests: list[ClipManifest]) -> dict[str, ClipManifest]:
    return {m.clip_id: m for m in manifests}


def _best_highlight_text(manifests: list[ClipManifest]) -> str:
    """Text of the strongest highlight across all manifests (deterministic tie-break)."""
    best: tuple[float, str, float, str] | None = None
    for m in manifests:
        for h in m.highlights:
            if not h.text.strip():
                continue
            key = (-h.score, m.clip_id, h.start_s, h.text)
            if best is None or key < best:
                best = key
    return best[3].strip() if best else ""


def _first_words(text: str, n: int) -> str:
    return " ".join(text.split()[:n]).strip()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clean_sentence(text: str) -> str:
    """Collapse whitespace, capitalize the first letter, drop a trailing period."""
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:1].upper() + text[1:] if text else text


def _mmss(t: float) -> str:
    total = int(t)
    return f"{total // 60:02d}:{total % 60:02d}"


def _segment_transcript_slice(seg: Segment, manifest: ClipManifest | None) -> str:
    """Transcript text of the source clip overlapping this segment's [in, out]."""
    if manifest is None:
        return ""
    parts = [
        ts.text
        for ts in manifest.transcript
        if ts.end_s > seg.in_ and ts.start_s < seg.out and ts.text.strip()
    ]
    return " ".join(parts).strip()


# --------------------------------------------------------------------------- #
# Titles                                                                       #
# --------------------------------------------------------------------------- #
def gen_titles(edl: EDL, manifests: list[ClipManifest]) -> list[str]:
    """Exactly 3 distinct titles (<= 90 chars each), derived from the strongest
    highlight text with platform-flavored patterns; tone-based generics otherwise."""
    platform = edl.brief.platform.value
    tone = edl.brief.tone.value
    benefit = _PLATFORM_BENEFIT.get(platform, "Highlights")
    hook = _best_highlight_text(manifests)
    if hook:
        base = _clean_sentence(hook)
        how = "How " + (base[:1].lower() + base[1:])
        candidates = [base, how, f"{base} [{benefit}]"]
    else:
        candidates = [
            f"A {tone} edit, cut for {platform}",
            f"How this {tone} edit came together",
            f"The {tone} cut [{benefit}]",
        ]

    titles: list[str] = []
    for i, cand in enumerate(candidates, start=1):
        title = _truncate(cand, _TITLE_MAX_CHARS)
        if title in titles:  # can only collide via truncation; re-mint deterministically
            title = _truncate(cand, _TITLE_MAX_CHARS - 4) + f" ({i})"
        titles.append(title)
    return titles


# --------------------------------------------------------------------------- #
# Chapters                                                                     #
# --------------------------------------------------------------------------- #
def gen_chapters(edl: EDL, manifests: list[ClipManifest]) -> list[dict]:
    """One chapter per timeline segment at its timeline offset.

    Labels come from the first ~6 words of the segment's transcript slice, else the
    segment reason prefix. Always starts with a 0.0 chapter; consecutive duplicate
    labels are merged (first occurrence wins).
    """
    by_clip = _manifest_by_clip(manifests)
    chapters: list[dict] = []
    for seg in edl.timeline:
        slice_text = _segment_transcript_slice(seg, by_clip.get(seg.source_clip))
        label = _first_words(slice_text, 6) or _first_words(seg.reason, 6) or seg.id
        chapters.append({"time_s": round(edl.timeline_offset_of(seg.id), 3), "label": label})

    if not chapters or chapters[0]["time_s"] != 0.0:
        chapters.insert(0, {"time_s": 0.0, "label": "Intro"})

    merged: list[dict] = []
    for ch in chapters:
        if merged and merged[-1]["label"] == ch["label"]:
            continue
        merged.append(ch)
    return merged


# --------------------------------------------------------------------------- #
# Description                                                                  #
# --------------------------------------------------------------------------- #
def gen_description(edl: EDL, manifests: list[ClipManifest]) -> str:
    """1-2 sentence summary, a blank line, then a mm:ss chapter list."""
    platform = edl.brief.platform.value
    tone = edl.brief.tone.value
    hook = _best_highlight_text(manifests)
    if hook:
        summary = (
            f'"{_truncate(_clean_sentence(hook), 120)}" — the moment this edit is built '
            f"around. A {tone} cut prepared for {platform}."
        )
    else:
        summary = f"A {tone} edit prepared for {platform}."

    lines = [f"{_mmss(ch['time_s'])} {ch['label']}" for ch in gen_chapters(edl, manifests)]
    return summary + "\n\nChapters:\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# Hashtags                                                                     #
# --------------------------------------------------------------------------- #
def gen_hashtags(edl: EDL, manifests: list[ClipManifest], max_tags: int = 10) -> list[str]:
    """Platform+tone base tags plus top transcript keywords by frequency.

    Keywords are lowercase alnum tokens, minus stopwords, length >= 4; ties break
    alphabetically. Every tag is "#"-prefixed, deduped, capped at `max_tags`.
    """
    platform = edl.brief.platform.value
    tone = edl.brief.tone.value
    base = _PLATFORM_TAGS.get(platform, [platform]) + [tone]

    text = " ".join(m.transcript_text for m in manifests).lower()
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", text) if len(t) >= 4 and t not in _STOPWORDS
    ]
    counts = Counter(tokens)
    keywords = sorted(counts, key=lambda w: (-counts[w], w))

    tags: list[str] = []
    for word in base + keywords:
        tag = "#" + word
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= max_tags:
            break
    return tags


# --------------------------------------------------------------------------- #
# Thumbnails                                                                   #
# --------------------------------------------------------------------------- #
def thumbnail_candidates(edl: EDL, manifests: list[ClipManifest], k: int = 3) -> list[dict]:
    """Top-k highlight moments (by score, then clip_id) whose midpoint lies inside a
    timeline segment of the same clip, remapped to timeline time. Falls back to segment
    midpoints when no highlight qualifies."""
    candidates: list[tuple[float, str, float, dict]] = []
    for m in manifests:
        for h in m.highlights:
            placed = _place_on_timeline(edl, m.clip_id, h)
            if placed is not None:
                candidates.append((-h.score, m.clip_id, placed["source_time_s"], placed))
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    if candidates:
        return [c[3] for c in candidates[:k]]

    # No in-timeline highlight anywhere: fall back to segment midpoints.
    out: list[dict] = []
    for seg in edl.timeline[: min(k, len(edl.timeline))]:
        mid = (seg.in_ + seg.out) / 2.0
        out.append(
            {
                "clip_id": seg.source_clip,
                "source_time_s": round(mid, 3),
                "timeline_s": round(
                    edl.timeline_offset_of(seg.id) + (mid - seg.in_) / seg.speed, 3
                ),
                "score": 0.0,
                "reason": "segment midpoint fallback",
            }
        )
    return out


def _place_on_timeline(edl: EDL, clip_id: str, h: Highlight) -> dict | None:
    """Remap a highlight midpoint onto the timeline if a segment of `clip_id` covers it."""
    mid = (h.start_s + h.end_s) / 2.0
    for seg in edl.timeline:
        if seg.source_clip == clip_id and seg.in_ <= mid <= seg.out:
            timeline_s = edl.timeline_offset_of(seg.id) + (mid - seg.in_) / seg.speed
            return {
                "clip_id": clip_id,
                "source_time_s": round(mid, 3),
                "timeline_s": round(timeline_s, 3),
                "score": h.score,
                "reason": _truncate(h.text.strip(), 60) if h.text.strip()
                else "high-energy moment",
            }
    return None
