"""Incremental re-render tests: content-hash render cache + feedback stage re-entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from ave.agents.render import render
from ave.analysis.manifest import ClipManifest, Highlight, ProbeInfo, Shot
from ave.config import Settings
from ave.edl.schema import Brief, Platform, Tone
from ave.llm.client import LLMClient
from ave.orchestrator.graph import Orchestrator, PipelineState, Stage
from ave.storage.store import LocalStorage


def _manifest(clip_id: str, dur: float = 10.0) -> ClipManifest:
    return ClipManifest(
        clip_id=clip_id,
        source_path=f"/fake/{clip_id}.mp4",
        proxy_path=f"/fake/{clip_id}_proxy.mp4",
        probe=ProbeInfo(duration_s=dur, width=1920, height=1080, fps=30.0, has_audio=True),
        shots=[Shot(start_s=i, end_s=i + 2.0) for i in range(0, int(dur), 2)],
        highlights=[Highlight(start_s=2.0, end_s=4.0, score=0.9, text="hook")],
    )


def _orchestrator(tmp_path, events):
    settings = Settings(anthropic_api_key="", ave_data_dir=tmp_path)
    storage = LocalStorage(tmp_path)
    return Orchestrator(
        storage, settings=settings, llm=LLMClient(settings),
        on_progress=lambda s, st, d: events.append((s, st, d)),
    ), storage


def _fresh_state(project="proj_inc"):
    return PipelineState(
        project_id=project,
        brief=Brief(platform=Platform.youtube, target_duration_s=12.0, tone=Tone.energetic),
        clips={},
        manifests=[_manifest("clip_01"), _manifest("clip_02")],
        stage=Stage.editorial,
    )


def test_render_cache_hits_on_same_content_hash(tmp_path):
    storage = LocalStorage(tmp_path)
    manifests = [_manifest("clip_01"), _manifest("clip_02")]
    from ave.agents import editorial

    edl = editorial.build_edl(
        "proj_c", Brief(platform=Platform.youtube, target_duration_s=12.0, tone=Tone.energetic),
        manifests, settings=Settings(anthropic_api_key=""),
    )
    first = render(edl, manifests, "proj_c", storage)
    # Simulate a completed encode for this content hash (dry run leaves no mp4).
    key = edl.content_hash()[:12]
    fake = storage.path_for("proj_c", f"renders/preview_v{edl.version}_{key}.mp4")
    Path(fake).write_bytes(b"video")
    # A structurally identical revision (bumped version) must reuse the file.
    again = render(edl.bump(notes="no-op feedback"), manifests, "proj_c", storage)
    assert again.get("cached") is True
    assert again["output_path"] == str(fake)
    assert first["content_hash"] == again["content_hash"]


def test_feedback_noop_short_circuits(tmp_path):
    pytest.importorskip("ave.agents.revise", reason="revise module (M3) not yet delivered")
    events: list = []
    orch, _ = _orchestrator(tmp_path, events)
    state = orch.run(_fresh_state())
    render_events_before = sum(1 for s, st, _ in events if s == "render" and st == "start")
    # A note the deterministic fallback can't parse -> no ops -> unchanged hash -> no re-run.
    state = orch.apply_feedback(state, "this note matches nothing actionable xyzzy")
    render_events_after = sum(1 for s, st, _ in events if s == "render" and st == "start")
    assert render_events_after == render_events_before
    assert any(st == "revise_noop" for _, st, _ in events)


def test_feedback_change_reenters_at_music_beat_only(tmp_path):
    pytest.importorskip("ave.agents.revise", reason="revise module (M3) not yet delivered")
    events: list = []
    orch, storage = _orchestrator(tmp_path, events)
    state = orch.run(_fresh_state())
    ingest_before = sum(1 for s, _, _ in events if s == "ingest")
    events.clear()
    state = orch.apply_feedback(state, "remove the last segment")
    stages_after = {s for s, _, _ in events}
    # Downstream stages re-ran…
    assert {"music_beat", "captions", "render"} <= stages_after
    # …but ingest never re-ran (that's the incremental guarantee).
    assert sum(1 for s, _, _ in events if s == "ingest") == 0
    assert ingest_before == 0  # editorial-start state never ran ingest either
    assert state.stage == Stage.done
    # Provenance lives in the persisted version chain: some revision carries the
    # feedback note even though later passes (b-roll/graphics/beat) bump further.
    edl_dir = storage.project_dir("proj_inc") / "edl"
    notes = [__import__("json").loads(p.read_text()).get("notes", "")
             for p in edl_dir.glob("v*.json")]
    assert any("feedback" in n for n in notes)


def test_feedback_removes_segment(tmp_path):
    pytest.importorskip("ave.agents.revise", reason="revise module (M3) not yet delivered")
    events: list = []
    orch, _ = _orchestrator(tmp_path, events)
    state = orch.run(_fresh_state())
    before = len(state.edl.timeline)
    state = orch.apply_feedback(state, "remove the last segment")
    assert len(state.edl.timeline) == before - 1


def test_manifest_rehydration_roundtrip(tmp_path):
    """Feedback rounds outlive the original process: manifests reload from storage."""
    events: list = []
    orch, storage = _orchestrator(tmp_path, events)
    manifests = [_manifest("clip_01")]
    for m in manifests:
        storage.write_json("proj_re", f"manifests/{m.clip_id}.json", m.model_dump(mode="json"))
    loaded = orch.load_manifests("proj_re")
    assert len(loaded) == 1
    assert loaded[0].clip_id == "clip_01"
    assert loaded[0].highlights[0].score == 0.9
