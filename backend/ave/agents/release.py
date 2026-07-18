"""Release Agent — assembles the publish-ready metadata kit for a finished edit.

IMPORTANT: this module never publishes anywhere; publishing is a separate,
explicitly-confirmed step. It only *prepares* titles, a description, hashtags,
chapters, and thumbnail candidates and persists them to storage for review.

Two paths, mirroring the Editorial Agent:

  * **Deterministic base** (always computed first): pure functions in
    `ave.release.metadata` derive everything from the EDL + manifests.
  * **LLM polish** (when the client is available): a copywriter pass may replace
    titles/description/hashtags, seeded with the deterministic versions. Invalid or
    failing LLM output keeps the deterministic base — the kit is never worse than the
    baseline and the pipeline never fails on an LLM hiccup. Thumbnails and chapters
    are always deterministic (they are timeline math, not copy).
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from ave.analysis.manifest import ClipManifest
from ave.config import Settings, get_settings
from ave.edl.schema import EDL
from ave.llm.client import LLMClient
from ave.release.metadata import (
    gen_chapters,
    gen_description,
    gen_hashtags,
    gen_titles,
    thumbnail_candidates,
)
from ave.storage.store import Storage

_MAX_HASHTAGS = 10


class ReleaseKit(BaseModel):
    """The reviewable, publish-ready metadata bundle for one EDL revision."""

    titles: list[str]
    description: str
    hashtags: list[str]
    thumbnails: list[dict]
    chapters: list[dict]


# JSON schema the LLM must satisfy for the copy-polish pass.
RELEASE_SCHEMA: dict = {
    "type": "object",
    "required": ["titles", "description", "hashtags"],
    "properties": {
        "titles": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
}

_SYSTEM = (
    "You are an honest, platform-savvy copywriter preparing release metadata for a video. "
    "Rules: (1) be truthful — never promise anything the video does not deliver, no "
    "clickbait lies or fake urgency; (2) match the platform's conventions and the brief's "
    "tone; (3) titles must be concise (under 90 characters) and distinct from each other; "
    "(4) hashtags must be relevant, lowercase, and start with '#'. You are given "
    "deterministic seed titles to improve on. Reply with JSON only, matching the provided "
    "schema."
)


def build_release_kit(
    edl: EDL,
    manifests: list[ClipManifest],
    project_id: str,
    storage: Storage,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> ReleaseKit:
    """Build (and persist) the release kit for `edl`.

    The deterministic base is always computed; the LLM may only replace
    titles/description/hashtags, and any failure or invalid reply keeps the base.
    """
    settings = settings or get_settings()
    llm = llm or LLMClient(settings)

    titles = gen_titles(edl, manifests)
    description = gen_description(edl, manifests)
    hashtags = gen_hashtags(edl, manifests, max_tags=_MAX_HASHTAGS)
    chapters = gen_chapters(edl, manifests)
    thumbnails = thumbnail_candidates(edl, manifests)

    if getattr(llm, "available", False):
        try:
            data = llm.complete_json(
                project_id=project_id,
                agent="release",
                system=_SYSTEM,
                user=json.dumps(
                    {
                        "brief": edl.brief.model_dump(mode="json"),
                        "transcript_digest": _transcript_digest(manifests),
                        "seed_titles": titles,
                    },
                    indent=2,
                ),
                schema=RELEASE_SCHEMA,
            )
            llm_titles = [
                t.strip() for t in data.get("titles", []) if isinstance(t, str) and t.strip()
            ]
            if len(llm_titles) == 3:
                titles = llm_titles
            llm_description = data.get("description", "")
            if isinstance(llm_description, str) and llm_description.strip():
                description = llm_description.strip()
            llm_tags = _normalize_hashtags(data.get("hashtags", []))
            if llm_tags:
                hashtags = llm_tags
        except Exception:
            # Never fail release prep on an LLM hiccup — keep the deterministic base.
            pass

    kit = ReleaseKit(
        titles=titles,
        description=description,
        hashtags=hashtags,
        thumbnails=thumbnails,
        chapters=chapters,
    )
    storage.write_json(project_id, f"release/kit_v{edl.version}.json", kit.model_dump())
    return kit


def _transcript_digest(manifests: list[ClipManifest], max_words: int = 300) -> str:
    """First `max_words` words of the combined transcripts (token-frugal LLM context)."""
    words = " ".join(m.transcript_text for m in manifests).split()
    return " ".join(words[:max_words])


def _normalize_hashtags(raw: object) -> list[str]:
    """Normalize LLM hashtags: '#'-prefixed, non-empty, deduped, capped at 10."""
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip().lstrip("#").strip()
        if not tag:
            continue
        tag = "#" + tag
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= _MAX_HASHTAGS:
            break
    return tags
