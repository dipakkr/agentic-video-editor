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
    done = "done"


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
        state.stage = Stage.done

    # -- feedback loop (M3 seam) ------------------------------------------- #
    def apply_feedback(self, state: PipelineState, note: str) -> PipelineState:
        """Revise the EDL from a natural-language note and re-render incrementally.

        Only render re-runs when the EDL content hash changes; identical hashes short-
        circuit (the determinism guarantee makes this safe).
        """
        assert state.edl is not None, "no EDL to revise"
        before = state.edl.content_hash()
        # M3 wires the note into the Editorial Agent; for now re-plan with the note as a
        # provenance annotation so the seam is exercised and versioning advances.
        revised = state.edl.bump(notes=f"feedback: {note}")
        state.edl = revised
        self._save_edl(revised)
        if revised.content_hash() != before:
            state.stage = Stage.render
            self._run_render(state)
        state.stage = Stage.done
        state.checkpoint(self.storage)
        return state

    def _save_edl(self, edl: EDL) -> None:
        # Versioned: every revision is persisted (never overwritten).
        self.storage.write_json(
            edl.project_id, f"edl/v{edl.version}.json", edl.model_dump(mode="json", by_alias=True)
        )
        self.storage.write_json(
            edl.project_id, "edl/latest.json", edl.model_dump(mode="json", by_alias=True)
        )
