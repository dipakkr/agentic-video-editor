"""QC checks — automated quality gates run against a draft before the user sees it.

Each check returns a :class:`CheckResult` tagged with the agent responsible for
fixing a failure, so the orchestrator can route a failing report back to the right
agent (editorial, music, captions, or render). All checks are deterministic and
degrade gracefully: anything that cannot be measured (unprobed clips, missing
loudness stats, no captions) is reported as a pass with an explanatory note rather
than an error.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ave.analysis.manifest import ClipManifest
from ave.captions.cues import build_cues, mode_for_style
from ave.config import Settings
from ave.edl.schema import EDL

# Two segments from the same source clip whose windows overlap by more than this
# are considered unintentional reuse.
DUPLICATE_OVERLAP_S = 0.5

# Acceptable deviation from the target integrated loudness.
LOUDNESS_TOLERANCE_LU = 1.5


class CheckResult(BaseModel):
    """Outcome of one QC check, tagged with the agent responsible for fixes."""

    check: str
    passed: bool
    details: str = ""
    responsible_agent: Literal["editorial", "music", "captions", "render"] = "editorial"


class QCReport(BaseModel):
    """Aggregated QC outcome: all results plus a failure->agent routing map."""

    results: list[CheckResult]
    passed: bool
    failures_by_agent: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Individual checks                                                            #
# --------------------------------------------------------------------------- #
def check_duration(edl: EDL) -> CheckResult:
    """Total timeline duration must be within the brief's target ± tolerance."""
    target = edl.brief.target_duration_s
    tol = target * edl.brief.duration_tolerance_pct / 100.0
    return CheckResult(
        check="duration",
        passed=edl.within_target(),
        details=(
            f"total {edl.total_duration_s:.3f}s vs target {target:.3f}s "
            f"± {tol:.3f}s"
        ),
        responsible_agent="editorial",
    )


def check_segment_bounds(edl: EDL, manifests: list[ClipManifest]) -> CheckResult:
    """Every segment's [in, out] must lie within its source clip's probed duration.

    Segments referencing unknown clips fail. Clips whose probe.duration_s is 0 are
    unmeasured and skipped (noted in details) — graceful degradation when ffprobe
    did not run.
    """
    by_id = {m.clip_id: m for m in manifests}
    offenders: list[str] = []
    unmeasured: list[str] = []

    for seg in edl.timeline:
        manifest = by_id.get(seg.source_clip)
        if manifest is None:
            offenders.append(f"{seg.id} (unknown clip {seg.source_clip})")
            continue
        duration = manifest.probe.duration_s
        if duration == 0:
            unmeasured.append(seg.id)
            continue
        if seg.in_ < 0 or seg.out > duration + 1e-6:
            offenders.append(
                f"{seg.id} ([{seg.in_:.3f}, {seg.out:.3f}] outside 0..{duration:.3f})"
            )

    parts: list[str] = []
    if offenders:
        parts.append("out of bounds: " + "; ".join(offenders))
    if unmeasured:
        parts.append("skipped (unmeasured clip): " + ", ".join(unmeasured))
    if not parts:
        parts.append("all segments within source bounds")
    return CheckResult(
        check="segment_bounds",
        passed=not offenders,
        details=" | ".join(parts),
        responsible_agent="editorial",
    )


def check_duplicate_usage(edl: EDL) -> CheckResult:
    """No two segments may reuse the same source window (overlap > 0.5s)."""
    pairs: list[str] = []
    timeline = edl.timeline
    for i, a in enumerate(timeline):
        for b in timeline[i + 1 :]:
            if a.source_clip != b.source_clip:
                continue
            overlap = min(a.out, b.out) - max(a.in_, b.in_)
            if overlap > DUPLICATE_OVERLAP_S:
                pairs.append(f"{a.id}+{b.id} (overlap {overlap:.3f}s in {a.source_clip})")
    details = (
        "overlapping source reuse: " + "; ".join(pairs)
        if pairs
        else "no duplicate source usage"
    )
    return CheckResult(
        check="duplicate_usage",
        passed=not pairs,
        details=details,
        responsible_agent="editorial",
    )


def check_beat_alignment(edl: EDL, tolerance_ms: int) -> CheckResult:
    """Beat-snapped cuts must actually land on their beat, within tolerance."""
    snapped = [
        s for s in edl.timeline if s.cut_snapped_to_beat and s.snapped_beat_s is not None
    ]
    if not snapped:
        return CheckResult(
            check="beat_alignment",
            passed=True,
            details="no snapped cuts",
            responsible_agent="music",
        )

    offenders: list[str] = []
    for seg in snapped:
        offset = edl.timeline_offset_of(seg.id)
        drift_ms = abs(offset - float(seg.snapped_beat_s)) * 1000.0
        if drift_ms > tolerance_ms:
            offenders.append(
                f"{seg.id} (offset {offset:.3f}s vs beat {seg.snapped_beat_s:.3f}s, "
                f"drift {drift_ms:.0f}ms > {tolerance_ms}ms)"
            )
    details = (
        "off-beat cuts: " + "; ".join(offenders)
        if offenders
        else f"{len(snapped)} snapped cut(s) within {tolerance_ms}ms"
    )
    return CheckResult(
        check="beat_alignment",
        passed=not offenders,
        details=details,
        responsible_agent="music",
    )


def _norm_words(text: str) -> list[str]:
    """Lowercased words with punctuation stripped (apostrophes kept)."""
    return re.findall(r"[a-z0-9']+", text.lower())


def check_caption_alignment(
    edl: EDL, manifests: list[ClipManifest], sample_n: int = 5
) -> CheckResult:
    """Sampled caption cues must be traceable back to a source transcript.

    Cues are rebuilt from the EDL + manifests, then up to ``sample_n`` cues are
    sampled with an even deterministic stride. A cue passes if its (normalised)
    word set is contained in the word set of some manifest's transcript.
    """
    cues = build_cues(edl, manifests, mode=mode_for_style(edl.captions.style))
    if not cues:
        return CheckResult(
            check="caption_alignment",
            passed=True,
            details="no captions to check",
            responsible_agent="captions",
        )

    if len(cues) <= sample_n:
        sampled = list(enumerate(cues))
    else:
        # Even deterministic stride across the cue list, first cue included.
        indices = sorted({(i * (len(cues) - 1)) // (sample_n - 1) for i in range(sample_n)})
        sampled = [(i, cues[i]) for i in indices]

    transcript_sets = [set(_norm_words(m.transcript_text)) for m in manifests]
    offenders: list[str] = []
    for idx, cue in sampled:
        cue_words = set(_norm_words(cue.text))
        if not cue_words:
            continue
        if not any(cue_words <= ts for ts in transcript_sets):
            offenders.append(f"cue[{idx}] @{cue.start_s:.3f}s ({cue.text!r})")

    details = (
        "cues not found in any transcript: " + "; ".join(offenders)
        if offenders
        else f"{len(sampled)}/{len(cues)} cues sampled, all match a source transcript"
    )
    return CheckResult(
        check="caption_alignment",
        passed=not offenders,
        details=details,
        responsible_agent="captions",
    )


def check_loudness(loudnorm_stats: Optional[dict], target_lufs: float) -> CheckResult:
    """Rendered integrated loudness must be within ±1.5 LU of the target.

    ``loudnorm_stats`` is ffmpeg's loudnorm JSON (values are printed as strings).
    ``None`` means the measurement never ran (no ffmpeg) — pass, noted.
    """
    if loudnorm_stats is None:
        return CheckResult(
            check="loudness",
            passed=True,
            details="not measured (ffmpeg unavailable)",
            responsible_agent="render",
        )

    raw = loudnorm_stats.get("output_i", loudnorm_stats.get("input_i"))
    if raw is None:
        return CheckResult(
            check="loudness",
            passed=False,
            details=f"stats missing output_i/input_i (keys: {sorted(loudnorm_stats)})",
            responsible_agent="render",
        )
    try:
        measured = float(raw)
    except (TypeError, ValueError):
        return CheckResult(
            check="loudness",
            passed=False,
            details=f"unparseable loudness value: {raw!r}",
            responsible_agent="render",
        )

    delta = abs(measured - target_lufs)
    return CheckResult(
        check="loudness",
        passed=delta <= LOUDNESS_TOLERANCE_LU,
        details=(
            f"measured {measured:.2f} LUFS vs target {target_lufs:.2f} "
            f"(Δ {delta:.2f} LU, tolerance {LOUDNESS_TOLERANCE_LU} LU)"
        ),
        responsible_agent="render",
    )


# --------------------------------------------------------------------------- #
# Aggregation                                                                  #
# --------------------------------------------------------------------------- #
def run_all(
    edl: EDL,
    manifests: list[ClipManifest],
    settings: Settings,
    loudnorm_stats: Optional[dict] = None,
) -> QCReport:
    """Run every QC check and aggregate failures per responsible agent."""
    results = [
        check_duration(edl),
        check_segment_bounds(edl, manifests),
        check_duplicate_usage(edl),
        check_beat_alignment(edl, settings.ave_beat_snap_tolerance_ms),
        check_caption_alignment(edl, manifests),
        check_loudness(loudnorm_stats, settings.ave_target_lufs),
    ]

    failures_by_agent: dict[str, list[str]] = {}
    for r in results:
        if not r.passed:
            failures_by_agent.setdefault(r.responsible_agent, []).append(r.check)

    return QCReport(
        results=results,
        passed=all(r.passed for r in results),
        failures_by_agent=failures_by_agent,
    )
