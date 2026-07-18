"""Graphics Agent (M5) — plans the EDL's graphics spec.

Deterministic v1: an opening title card drawn from the strongest hook (the same signal
the editorial agent opens on), gated by `agent_config.enable_graphics`. Lower thirds are
schema-ready but only planned when diarization can name a speaker — inventing straps
with no real name would hurt the edit, so absent that signal we plan none (graceful
minimalism, not a failure). Rendering happens in the filtergraph via drawtext.
"""

from __future__ import annotations

from ave.analysis.manifest import ClipManifest
from ave.edl.schema import EDL, TitleCard

_MAX_TITLE_CHARS = 40


def plan_graphics(edl: EDL, manifests: list[ClipManifest]) -> EDL:
    """Fill edl.graphics. Returns the input unchanged when disabled or nothing to add."""
    if not edl.brief.agent_config.enable_graphics:
        return edl
    if edl.graphics.title_card is not None:
        return edl  # already planned (idempotent across feedback re-runs)

    text = _title_text(edl, manifests)
    if not text:
        return edl

    new = edl.model_copy(deep=True)
    new.graphics.title_card = TitleCard(text=text, start_s=0.0, duration_s=2.5)
    return new.bump(notes="graphics pass")


def _title_text(edl: EDL, manifests: list[ClipManifest]) -> str:
    best_text, best_score = "", -1.0
    for m in manifests:
        for hl in m.highlights:
            if hl.text and hl.score > best_score:
                best_text, best_score = hl.text, hl.score
    if best_text:
        return _truncate(best_text)
    # No transcript signal: a tone-flavored generic beats an empty screen.
    return f"A {edl.brief.tone.value} cut".title()


def _truncate(text: str) -> str:
    text = text.strip().rstrip(".")
    if len(text) <= _MAX_TITLE_CHARS:
        return text
    cut = text[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0]
    return f"{cut}…"
