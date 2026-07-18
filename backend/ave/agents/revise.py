"""Revise Agent — turns a natural-language feedback note into EDL edit operations.

Two paths, mirroring the Editorial Agent:

  * **LLM path** (when the client is available): the model reads the note plus a compact
    digest of the current timeline and replies with a minimal list of edit operations
    constrained to `OPS_SCHEMA`; entries that fail `EditOp` validation are ignored.
  * **Deterministic fallback**: keyword parsing of the note into ops — guarantees the
    feedback loop works with zero external services and never raises on an LLM hiccup.

Either way the ops are executed by the pure `ave.feedback.ops.apply_ops`, so revisions
are versioned, explainable, and the original EDL is never mutated.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from ave.analysis.manifest import ClipManifest
from ave.config import Settings, get_settings
from ave.edl.schema import EDL
from ave.feedback.ops import EditOp, apply_ops
from ave.llm.client import LLMClient

_OP_NAMES = [
    "remove_segment",
    "trim",
    "reorder",
    "retime",
    "set_transition",
    "change_caption_style",
    "set_target_duration",
]

# JSON schema the LLM must satisfy: {"ops": [...]} matching EditOp fields. Deliberately
# permissive (extra properties allowed) — strict parsing happens via EditOp afterwards.
OPS_SCHEMA: dict = {
    "type": "object",
    "required": ["ops"],
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["op"],
                "properties": {
                    "op": {"type": "string", "enum": _OP_NAMES},
                    "segment_id": {"type": ["string", "null"]},
                    "trim_start_s": {"type": "number", "minimum": 0},
                    "trim_end_s": {"type": "number", "minimum": 0},
                    "to_index": {"type": ["integer", "null"], "minimum": 0},
                    "speed": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "transition": {"type": ["string", "null"]},
                    "transition_duration_s": {"type": "number", "minimum": 0},
                    "caption_style": {"type": ["string", "null"]},
                    "target_duration_s": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

_SYSTEM = (
    "You are a professional video editor revising an existing edit from a user's note. "
    "Interpret the user's revision request as a minimal list of edit operations; never "
    "invent segment ids; prefer the smallest change that satisfies the note. Each op must "
    "carry a short `reason` tying it to the note. Reply with JSON only, matching the "
    "provided schema."
)


def revise_edl(
    edl: EDL,
    manifests: list[ClipManifest],
    note: str,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> tuple[EDL, list[str]]:
    """Revise `edl` according to the natural-language `note`.

    Returns `(new_edl, skipped)` from `apply_ops`. Uses the LLM when available; on any
    LLM failure falls through to deterministic keyword parsing — never raises.
    `manifests` is part of the agent contract for future source-aware revisions.
    """
    if llm is None:
        llm = LLMClient(settings or get_settings())
    if getattr(llm, "available", False):
        try:
            return _finish(edl, _ops_from_llm(edl, manifests, note, llm), note)
        except Exception:
            # Never fail the feedback loop on an LLM hiccup — fall back deterministically.
            pass
    return _finish(edl, fallback_ops(edl, note), note)


def _finish(edl: EDL, ops: list[EditOp], note: str) -> tuple[EDL, list[str]]:
    if not ops:
        return edl, ["no actionable op parsed"]
    return apply_ops(edl, ops, note)


# --------------------------------------------------------------------------- #
# LLM path                                                                     #
# --------------------------------------------------------------------------- #
def _ops_from_llm(
    edl: EDL, manifests: list[ClipManifest], note: str, llm: LLMClient
) -> list[EditOp]:
    user = json.dumps(
        {
            "note": note,
            "target_duration_s": edl.brief.target_duration_s,
            "duration_tolerance_pct": edl.brief.duration_tolerance_pct,
            "total_duration_s": edl.total_duration_s,
            "segments": [
                {
                    "id": s.id,
                    "in": s.in_,
                    "out": s.out,
                    "timeline_duration_s": round(s.timeline_duration_s, 3),
                    "transition": s.transition_in.value,
                    "reason": s.reason,
                }
                for s in edl.timeline
            ],
            "op_vocabulary": _OP_NAMES,
        },
        indent=2,
    )
    data = llm.complete_json(
        project_id=edl.project_id,
        agent="revise",
        system=_SYSTEM,
        user=user,
        schema=OPS_SCHEMA,
    )
    ops: list[EditOp] = []
    for raw in data.get("ops", []):
        try:
            ops.append(EditOp.model_validate(raw))
        except ValidationError:
            continue  # ignore malformed entries; the rest still apply
    return ops


# --------------------------------------------------------------------------- #
# Deterministic fallback                                                       #
# --------------------------------------------------------------------------- #
def fallback_ops(edl: EDL, note: str) -> list[EditOp]:
    """Keyword-parse `note` into ops. First matching rule (in priority order) wins."""
    text = note.lower()
    rules = (
        _rule_remove,
        _rule_target_number,
        _rule_shorter_longer,
        _rule_punchier,
        _rule_speed,
        _rule_transitions,
        _rule_captions,
    )
    for rule in rules:
        ops = rule(edl, text)
        if ops:
            return ops
    return []


_REMOVE_RE = re.compile(
    r"\b(?:remove|cut|delete)\b[^.,;!?]*?"
    r"\b(seg_[0-9a-zA-Z_]+|segment\s+(\d+)|first|second|third|last)\b"
)


def _rule_remove(edl: EDL, text: str) -> list[EditOp]:
    m = _REMOVE_RE.search(text)
    if not m or not edl.timeline:
        return []
    token, number = m.group(1), m.group(2)
    segment_id: str | None = None
    if token.startswith("seg_"):
        segment_id = token
    elif number is not None:
        n = int(number)
        # In-range numbers resolve positionally; out-of-range ones pass through so
        # apply_ops reports them as unknown instead of silently guessing.
        segment_id = edl.timeline[n - 1].id if 1 <= n <= len(edl.timeline) else f"seg_{n:02d}"
    elif token == "last":
        segment_id = edl.timeline[-1].id
    else:
        idx = {"first": 0, "second": 1, "third": 2}[token]
        if idx < len(edl.timeline):
            segment_id = edl.timeline[idx].id
    if segment_id is None:
        return []
    return [EditOp(op="remove_segment", segment_id=segment_id, reason=m.group(0))]


_DUR_RE = re.compile(
    r"\b(?:make it|shorten to|target)\s+(\d+(?:\.\d+)?)\s*s?(?:ec(?:ond)?s?)?\b"
)


def _rule_target_number(edl: EDL, text: str) -> list[EditOp]:
    m = _DUR_RE.search(text)
    if not m:
        return []
    return [
        EditOp(op="set_target_duration", target_duration_s=float(m.group(1)), reason=m.group(0))
    ]


def _rule_shorter_longer(edl: EDL, text: str) -> list[EditOp]:
    if re.search(r"\bshorter\b", text):
        factor, phrase = 0.8, "shorter"
    elif re.search(r"\blonger\b", text):
        factor, phrase = 1.2, "longer"
    else:
        return []
    target = round(edl.brief.target_duration_s * factor, 3)
    return [EditOp(op="set_target_duration", target_duration_s=target, reason=phrase)]


_PUNCH_RE = re.compile(
    r"\b(?:punchier|snappier)\b[^.,;!?]*?\b(?:intro|opening|hook)\b"
    r"|\b(?:intro|opening|hook)\b[^.,;!?]*?\b(?:punchier|snappier)\b"
)
_PUNCHY_MAX_S = 3.0


def _rule_punchier(edl: EDL, text: str) -> list[EditOp]:
    m = _PUNCH_RE.search(text)
    if not m or not edl.timeline:
        return []
    first = edl.timeline[0]
    if first.timeline_duration_s <= _PUNCHY_MAX_S:
        return []  # already punchy enough
    trim_end = round(first.source_duration_s - _PUNCHY_MAX_S * first.speed, 3)
    return [
        EditOp(op="trim", segment_id=first.id, trim_end_s=trim_end, reason=m.group(0))
    ]


def _rule_speed(edl: EDL, text: str) -> list[EditOp]:
    if re.search(r"\bfaster\b", text):
        speed, phrase = 1.25, "faster"
    elif re.search(r"\bslower\b", text):
        speed, phrase = 0.9, "slower"
    else:
        return []
    return [
        EditOp(op="retime", segment_id=s.id, speed=speed, reason=phrase) for s in edl.timeline
    ]


_TRANSITION_RE = re.compile(r"\bcrossfade\b|\bsmoother\b[^.,;!?]*?\btransitions?\b")


def _rule_transitions(edl: EDL, text: str) -> list[EditOp]:
    m = _TRANSITION_RE.search(text)
    if not m:
        return []
    return [
        EditOp(
            op="set_transition",
            segment_id=s.id,
            transition="crossfade",
            transition_duration_s=0.5,
            reason=m.group(0),
        )
        for s in edl.timeline[1:]
    ]


_NO_CAPTIONS_RE = re.compile(r"\bno\s+(?:captions?|subtitles?)\b")
_KARAOKE_RE = re.compile(r"\bkaraoke\b|\bword[\s-]by[\s-]word\b")
_CLEAN_RE = re.compile(r"\b(?:clean|simple)\b[^.,;!?]*?\b(?:subtitles?|captions?)\b")


def _rule_captions(edl: EDL, text: str) -> list[EditOp]:
    for pattern, style in (
        (_NO_CAPTIONS_RE, "none"),
        (_KARAOKE_RE, "karaoke_bold"),
        (_CLEAN_RE, "clean_subtitle"),
    ):
        m = pattern.search(text)
        if m:
            return [EditOp(op="change_caption_style", caption_style=style, reason=m.group(0))]
    return []
