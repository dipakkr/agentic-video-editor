"""Orchestrator — runs the agents as a checkpointed state machine.

This is the LangGraph-equivalent for M1: an explicit, resumable graph over the pipeline
stages. State is checkpointed to storage after every stage, so a crash resumes from the
last completed node rather than re-running expensive analysis/renders. Progress events
are emitted through a callback that the API layer (M3) forwards over SSE/WebSocket.

Graph (M1):  ingest → editorial → render → done
M2 inserts music+beat-sync and captions between editorial and render; M4 adds qc/release.
The feedback loop (M3) re-enters at `editorial` with the user's note and only re-runs the
affected downstream nodes (incremental re-render keyed on EDL content hash).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ave.agents import editorial, ingest, render
from ave.analysis.manifest import ClipManifest
from ave.config import Settings, get_settings
from ave.edl.schema import EDL, Brief
from ave.llm.client import LLMClient
from ave.storage.store import Storage


class Stage(str, Enum):
    ingest = "ingest"
    editorial = "editorial"
    music_beat = "music_beat"
    captions = "captions"
    render = "render"
    qc = "qc"
    release = "release"
    done = "done"


# QC failure routing: which graph stage re-runs for each responsible agent.
# Editorial failures are surfaced to the user rather than auto-looped (an automatic
# re-plan could oscillate; a human note through the feedback loop converges faster).
_QC_RETRY_STAGE = {
    "music": Stage.music_beat,
    "captions": Stage.captions,
    "render": Stage.render,
}
_MAX_QC_RETRIES = 2


@dataclass
class PipelineState:
    project_id: str
    brief: Brief
    clips: dict[str, str]                       # clip_id -> source path
    stage: Stage = Stage.ingest
    manifests: list[ClipManifest] = field(default_factory=list)
    edl: EDL | None = None
    render_result: dict | None = None
    # M2 artifacts flowing between stages.
    music_path: str | None = None
    captions_result: dict | None = None
    # User overrides (CLI/API): disable music, force a caption style.
    no_music: bool = False
    caption_style: str | None = None
    # M4 artifacts.
    qc_report: dict | None = None
    release_kit: dict | None = None
    qc_retries: int = 0

    def checkpoint(self, storage: Storage) -> None:
        storage.write_json(
            self.project_id,
            "state.json",
            {
                "project_id": self.project_id,
                "stage": self.stage.value,
                "brief": self.brief.model_dump(mode="json"),
                "clips": self.clips,
                "edl_version": self.edl.version if self.edl else None,
                "render": self.render_result,
            },
        )


ProgressFn = Callable[[str, str, dict], None]


def _noop(stage: str, status: str, data: dict) -> None:  # pragma: no cover
    pass


class Orchestrator:
    def __init__(
        self,
        storage: Storage,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        on_progress: ProgressFn | None = None,
    ):
        self.storage = storage
        self.settings = settings or get_settings()
        self.llm = llm or LLMClient(self.settings)
        self.on_progress = on_progress or _noop

    def _emit(self, stage: Stage, status: str, **data) -> None:
        self.on_progress(stage.value, status, data)

    def run(self, state: PipelineState) -> PipelineState:
        """Drive the graph to completion from the current stage (resumable)."""
        while state.stage != Stage.done:
            if state.stage == Stage.ingest:
                self._run_ingest(state)
            elif state.stage == Stage.editorial:
                self._run_editorial(state)
            elif state.stage == Stage.music_beat:
                self._run_music_beat(state)
            elif state.stage == Stage.captions:
                self._run_captions(state)
            elif state.stage == Stage.render:
                self._run_render(state)
            elif state.stage == Stage.qc:
                self._run_qc(state)
            elif state.stage == Stage.release:
                self._run_release(state)
            state.checkpoint(self.storage)
        self._emit(Stage.done, "complete")
        return state

    # -- nodes -------------------------------------------------------------- #
    def _run_ingest(self, state: PipelineState) -> None:
        self._emit(Stage.ingest, "start", clips=len(state.clips))
        state.manifests = ingest.analyze_all(
            state.clips, state.project_id, self.storage, self.settings
        )
        self._emit(
            Stage.ingest, "done",
            clips=len(state.manifests),
            features={m.clip_id: m.analysis_features for m in state.manifests},
        )
        state.stage = Stage.editorial

    def _run_editorial(self, state: PipelineState) -> None:
        self._emit(Stage.editorial, "start", planner="llm" if self.llm.available else "deterministic")
        state.edl = editorial.build_edl(
            state.project_id, state.brief, state.manifests, llm=self.llm, settings=self.settings
        )
        if state.caption_style:
            from ave.edl.schema import CaptionStyle

            state.edl = state.edl.model_copy(deep=True)
            state.edl.captions.style = CaptionStyle(state.caption_style)
        self._save_edl(state.edl)
        self._emit(
            Stage.editorial, "done",
            version=state.edl.version,
            segments=len(state.edl.timeline),
            duration_s=state.edl.total_duration_s,
            within_target=state.edl.within_target(),
        )
        state.stage = Stage.music_beat

    def _run_music_beat(self, state: PipelineState) -> None:
        """Music & Beat Agent (M2): pick a track, snap cuts to the beat, align the drop.

        Graceful: with --no-music, an empty library, or the module unavailable, the EDL
        passes through untouched and the pipeline continues.
        """
        assert state.edl is not None
        self._emit(Stage.music_beat, "start")
        if state.no_music:
            self._emit(Stage.music_beat, "done", skipped=True, reason="disabled by user")
            state.stage = Stage.captions
            return
        try:
            from ave.agents.music import apply_music  # lazy: module lands in M2

            before = state.edl.version
            state.edl = apply_music(
                state.edl, state.manifests, self.settings.ave_music_dir, self.settings
            )
            if state.edl.music.track_id:
                # apply_music stores track_id; resolve the audio file via the library.
                from ave.music.library import load_library

                lib = {t.track_id: t for t in load_library(self.settings.ave_music_dir)}
                meta = lib.get(state.edl.music.track_id)
                if meta and meta.file:
                    candidate = self.settings.ave_music_dir / meta.file
                    state.music_path = str(candidate) if candidate.exists() else None
            if state.edl.version != before:
                self._save_edl(state.edl)
            self._emit(
                Stage.music_beat, "done",
                track=state.edl.music.track_id,
                snapped=sum(1 for s in state.edl.timeline if s.cut_snapped_to_beat),
                sync_points=len(state.edl.music.sync_map),
                music_file=bool(state.music_path),
            )
        except Exception as exc:  # noqa: BLE001 — optional feature, never fail the run
            self._emit(Stage.music_beat, "degraded", error=str(exc))
        state.stage = Stage.captions

    def _run_captions(self, state: PipelineState) -> None:
        """Caption Agent (M2): styled cues -> sidecar .srt/.vtt + .ass for burn-in."""
        assert state.edl is not None
        self._emit(Stage.captions, "start", style=state.edl.captions.style.value)
        try:
            from ave.agents.captions import generate_captions  # lazy: module lands in M2

            state.captions_result = generate_captions(
                state.edl, state.manifests, state.project_id, self.storage
            )
            self._emit(Stage.captions, "done", **{
                k: v for k, v in (state.captions_result or {}).items() if k != "words"
            })
        except Exception as exc:  # noqa: BLE001 — optional feature, never fail the run
            state.captions_result = None
            self._emit(Stage.captions, "degraded", error=str(exc))
        state.stage = Stage.render

    def _run_render(self, state: PipelineState) -> None:
        assert state.edl is not None
        self._emit(Stage.render, "start", version=state.edl.version)
        ass_path = (state.captions_result or {}).get("ass")
        state.render_result = render.render(
            state.edl, state.manifests, state.project_id, self.storage,
            music_path=state.music_path, ass_path=ass_path,
        )
        self._emit(Stage.render, "done", **state.render_result)
        state.stage = Stage.qc

    def _run_qc(self, state: PipelineState) -> None:
        """QC Agent (M4): gate the draft; route failures back to the responsible agent.

        Failing checks whose responsible agent has a retry stage re-enter the graph
        there (max _MAX_QC_RETRIES loops); editorial failures — and anything still
        failing after the retry budget — surface in the report for the user.
        """
        assert state.edl is not None
        self._emit(Stage.qc, "start", attempt=state.qc_retries + 1)
        try:
            from ave.agents.qc import run_qc  # lazy: module lands in M4

            report = run_qc(
                state.edl, state.manifests, state.project_id, self.storage,
                settings=self.settings,
            )
            state.qc_report = report.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — QC unavailable must not kill the run
            state.qc_report = None
            self._emit(Stage.qc, "degraded", error=str(exc))
            state.stage = Stage.release
            return

        if report.passed:
            self._emit(Stage.qc, "done", passed=True)
            state.stage = Stage.release
            return

        retryable = [a for a in report.failures_by_agent if a in _QC_RETRY_STAGE]
        if retryable and state.qc_retries < _MAX_QC_RETRIES:
            state.qc_retries += 1
            target = min(
                (_QC_RETRY_STAGE[a] for a in retryable),
                key=lambda s: list(Stage).index(s),
            )
            self._emit(
                Stage.qc, "retry",
                attempt=state.qc_retries, reentry=target.value,
                failures=report.failures_by_agent,
            )
            state.stage = target
            return

        # Out of retries (or editorial-only failures): surface and continue to release —
        # the user sees the draft WITH the failure report attached, never a dead end.
        self._emit(Stage.qc, "done", passed=False, failures=report.failures_by_agent)
        state.stage = Stage.release

    def _run_release(self, state: PipelineState) -> None:
        """Release Agent (M4): titles, chaptered description, hashtags, thumbnails."""
        assert state.edl is not None
        self._emit(Stage.release, "start")
        try:
            from ave.agents.release import build_release_kit  # lazy: module lands in M4

            kit = build_release_kit(
                state.edl, state.manifests, state.project_id, self.storage,
                llm=self.llm, settings=self.settings,
            )
            state.release_kit = kit.model_dump(mode="json")
            self._emit(
                Stage.release, "done",
                titles=len(kit.titles), hashtags=len(kit.hashtags),
                thumbnails=len(kit.thumbnails),
            )
        except Exception as exc:  # noqa: BLE001 — optional layer, never fail the run
            state.release_kit = None
            self._emit(Stage.release, "degraded", error=str(exc))
        state.stage = Stage.done

    # -- feedback loop (M3) ------------------------------------------------- #
    def apply_feedback(self, state: PipelineState, note: str) -> PipelineState:
        """Natural-language revision → incremental re-run of only the affected stages.

        The Revise Agent (LLM ops or deterministic keyword fallback) rewrites the EDL.
        If the content hash is unchanged the whole pipeline short-circuits (rendering is
        a pure function of the EDL). Otherwise we re-enter at `music_beat` — cuts moved,
        so beats re-snap, captions re-map, and render re-runs — while ingest and the
        original editorial pass are never repeated. Render itself is content-hash cached.
        """
        assert state.edl is not None, "no EDL to revise"
        if not state.manifests:
            state.manifests = self.load_manifests(state.project_id)
        before = state.edl.content_hash()
        self._emit(Stage.editorial, "revising", note=note)

        try:
            from ave.agents.revise import revise_edl  # lazy: module lands in M3

            revised, skipped = revise_edl(
                state.edl, state.manifests, note, llm=self.llm, settings=self.settings
            )
        except Exception as exc:  # noqa: BLE001 — feedback must never crash a project
            self._emit(Stage.editorial, "revise_failed", error=str(exc))
            state.checkpoint(self.storage)
            return state

        if skipped:
            self._emit(Stage.editorial, "revise_skipped_ops", skipped=skipped)

        if revised.content_hash() == before:
            self._emit(Stage.editorial, "revise_noop", note=note)
            state.stage = Stage.done
            state.checkpoint(self.storage)
            return state

        state.edl = revised
        self._save_edl(revised)
        self._emit(
            Stage.editorial, "revised",
            version=revised.version, segments=len(revised.timeline),
            duration_s=revised.total_duration_s,
        )
        # Re-run only the downstream stages affected by a timeline change.
        state.stage = Stage.music_beat
        return self.run(state)

    def load_manifests(self, project_id: str) -> list[ClipManifest]:
        """Rehydrate persisted clip manifests (feedback rounds outlive the run process)."""
        manifests_dir = self.storage.project_dir(project_id) / "manifests"
        if not manifests_dir.exists():
            return []
        out: list[ClipManifest] = []
        for path in sorted(manifests_dir.glob("*.json")):
            try:
                out.append(ClipManifest.model_validate_json(path.read_text()))
            except Exception:  # noqa: BLE001 — skip corrupt manifests, keep the rest
                continue
        return out

    def _save_edl(self, edl: EDL) -> None:
        # Versioned: every revision is persisted (never overwritten).
        self.storage.write_json(
            edl.project_id, f"edl/v{edl.version}.json", edl.model_dump(mode="json", by_alias=True)
        )
        self.storage.write_json(
            edl.project_id, "edl/latest.json", edl.model_dump(mode="json", by_alias=True)
        )
