"""QC Agent — gate the draft before the user sees it.

Runs the full deterministic check suite (:mod:`ave.qc.checks`) against the current
EDL, manifests, and (optionally) the render's loudnorm stats, persists the report
next to the EDL revision it judged, and returns it. Failures are tagged with the
responsible agent; routing a failing report back to that agent is the
orchestrator's job (max 2 repair loops before surfacing to the user).
"""

from __future__ import annotations

from typing import Optional

from ave.analysis.manifest import ClipManifest
from ave.config import Settings, get_settings
from ave.edl.schema import EDL
from ave.qc.checks import QCReport, run_all
from ave.storage.store import Storage


def run_qc(
    edl: EDL,
    manifests: list[ClipManifest],
    project_id: str,
    storage: Storage,
    settings: Optional[Settings] = None,
    loudnorm_stats: Optional[dict] = None,
) -> QCReport:
    """Run all QC checks, persist the versioned report, and return it."""
    settings = settings or get_settings()
    report = run_all(edl, manifests, settings, loudnorm_stats=loudnorm_stats)
    storage.write_json(project_id, f"qc/report_v{edl.version}.json", report.model_dump())
    return report
