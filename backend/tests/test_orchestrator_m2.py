"""Orchestrator M2 graph tests: music_beat + captions stages in the pipeline."""

from __future__ import annotations

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


def _run(tmp_path, **state_kw):
    settings = Settings(anthropic_api_key="", ave_data_dir=tmp_path)
    storage = LocalStorage(tmp_path)
    events: list[tuple[str, str, dict]] = []
    orch = Orchestrator(
        storage, settings=settings, llm=LLMClient(settings),
        on_progress=lambda s, st, d: events.append((s, st, d)),
    )
    state = PipelineState(
        project_id="proj_m2",
        brief=Brief(platform=Platform.youtube, target_duration_s=12.0, tone=Tone.energetic),
        clips={},
        manifests=[_manifest("clip_01"), _manifest("clip_02")],
        stage=Stage.editorial,
        **state_kw,
    )
    return orch.run(state), events


def test_graph_traverses_m2_stages(tmp_path):
    state, events = _run(tmp_path)
    stages = [s for s, _, _ in events]
    assert "music_beat" in stages and "captions" in stages
    # music_beat and captions run between editorial and render.
    assert stages.index("music_beat") > stages.index("editorial")
    assert stages.index("render") > stages.index("captions")
    assert state.stage == Stage.done
    assert state.render_result is not None


def test_no_music_flag_skips_music_stage(tmp_path):
    state, events = _run(tmp_path, no_music=True)
    music_events = [(st, d) for s, st, d in events if s == "music_beat"]
    assert any(d.get("skipped") for _, d in music_events)
    assert state.music_path is None
    assert state.stage == Stage.done


def test_caption_style_override_applied(tmp_path):
    state, _ = _run(tmp_path, caption_style="karaoke_bold")
    assert state.edl is not None
    assert state.edl.captions.style.value == "karaoke_bold"


def test_m2_stages_never_fail_the_run(tmp_path):
    """Music/captions are optional layers — the graph must reach done regardless."""
    state, events = _run(tmp_path)
    for s, st, _ in events:
        if s in ("music_beat", "captions"):
            assert st in ("start", "done", "degraded")
    assert state.stage == Stage.done
