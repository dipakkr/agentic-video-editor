"""M5 manager-feature tests: overlay/graphics rendering, graphics planner, agent config."""

from __future__ import annotations

from ave.agents.graphics import plan_graphics
from ave.analysis.manifest import ClipManifest, Highlight, ProbeInfo, Shot
from ave.config import Settings
from ave.edl.schema import (
    EDL,
    AgentConfig,
    Brief,
    LowerThird,
    Overlay,
    Platform,
    Segment,
    TitleCard,
    Tone,
)
from ave.llm.client import LLMClient
from ave.media.filtergraph import (
    _escape_drawtext,
    augment_with_overlays_and_graphics,
    build,
)
from ave.orchestrator.graph import Orchestrator, PipelineState, Stage
from ave.storage.store import LocalStorage


def _edl(**brief_kw) -> EDL:
    brief = Brief(platform=Platform.youtube, target_duration_s=12.0, tone=Tone.energetic,
                  **brief_kw)
    return EDL(
        project_id="p", brief=brief,
        timeline=[
            Segment(id="seg_01", source_clip="c1", **{"in": 0.0}, out=8.0, reason="talk"),
            Segment(id="seg_02", source_clip="c2", **{"in": 0.0}, out=4.0, reason="more"),
        ],
    )


SOURCES = {"c1": "/f/c1.mp4", "c2": "/f/c2.mp4"}


# ---- filtergraph overlays + graphics ---------------------------------------- #
def test_overlay_chain_delays_and_gates():
    edl = _edl()
    edl.overlays = [Overlay(id="ovl_01", source_clip="c2", **{"in": 1.0}, out=4.0,
                            timeline_start_s=3.0, reason="cutaway")]
    plan = augment_with_overlays_and_graphics(
        build(edl, SOURCES), edl, overlay_sources={"ovl_01": "/f/c2.mp4"}
    )
    assert plan.inputs[-1] == "/f/c2.mp4"
    assert "setpts=PTS+3.0/TB" in plan.filtergraph
    assert "overlay=eof_action=pass:enable='between(t,3.0,6.0)'" in plan.filtergraph


def test_unresolvable_overlay_skipped():
    edl = _edl()
    edl.overlays = [Overlay(id="ovl_01", source_clip="ghost", **{"in": 0.0}, out=2.0,
                            timeline_start_s=1.0, reason="x")]
    base = build(edl, SOURCES)
    plan = augment_with_overlays_and_graphics(base, edl, overlay_sources={})
    assert plan.filtergraph == base.filtergraph  # untouched


def test_title_card_drawtext():
    edl = _edl()
    edl.graphics.title_card = TitleCard(text="We almost lost it", start_s=0.0,
                                        duration_s=2.5)
    plan = augment_with_overlays_and_graphics(build(edl, SOURCES), edl)
    assert "drawtext=text='We almost lost it'" in plan.filtergraph
    assert "enable='between(t,0.0,2.5)'" in plan.filtergraph
    assert "fontsize=108" in plan.filtergraph  # 1080 // 10


def test_lower_third_drawtext_box():
    edl = _edl()
    edl.graphics.lower_thirds = [LowerThird(text="Deepak — Builder", start_s=2.0)]
    plan = augment_with_overlays_and_graphics(build(edl, SOURCES), edl)
    assert "box=1:boxcolor=black@0.55" in plan.filtergraph
    assert "y=h*0.78" in plan.filtergraph


def test_escape_drawtext():
    assert _escape_drawtext("a:b") == "a\\:b"
    assert _escape_drawtext("100%") == "100\\%"
    assert "\\\\" in _escape_drawtext("a\\b")


def test_captions_stay_on_top_of_graphics():
    """Layer order: overlays/graphics first, ass burn-in last."""
    from ave.media.filtergraph import augment_with_music_and_captions

    edl = _edl()
    edl.graphics.title_card = TitleCard(text="T", start_s=0.0)
    plan = augment_with_overlays_and_graphics(build(edl, SOURCES), edl)
    plan = augment_with_music_and_captions(plan, edl, ass_path="/s.ass")
    graph = plan.filtergraph
    assert graph.index("drawtext") < graph.index("ass=filename")


# ---- graphics planner -------------------------------------------------------- #
def _manifest_with_highlight(text: str, score: float = 0.9) -> ClipManifest:
    return ClipManifest(
        clip_id="c1", source_path="/f.mp4", probe=ProbeInfo(duration_s=10),
        shots=[Shot(start_s=0, end_s=10)],
        highlights=[Highlight(start_s=1, end_s=3, score=score, text=text)],
    )


def test_plan_graphics_uses_strongest_hook():
    edl = plan_graphics(_edl(), [_manifest_with_highlight("The big reveal moment")])
    assert edl.graphics.title_card is not None
    assert edl.graphics.title_card.text == "The big reveal moment"
    assert "graphics pass" in edl.notes


def test_plan_graphics_truncates_long_titles():
    long = "This is an extremely long highlight sentence that keeps going on"
    edl = plan_graphics(_edl(), [_manifest_with_highlight(long)])
    assert len(edl.graphics.title_card.text) <= 41  # 40 + ellipsis char
    assert edl.graphics.title_card.text.endswith("…")


def test_plan_graphics_disabled_passthrough():
    base = _edl(agent_config=AgentConfig(enable_graphics=False))
    out = plan_graphics(base, [_manifest_with_highlight("x")])
    assert out is base and out.graphics.title_card is None


def test_plan_graphics_idempotent():
    edl = plan_graphics(_edl(), [_manifest_with_highlight("hook")])
    again = plan_graphics(edl, [_manifest_with_highlight("other")])
    assert again is edl  # already planned — unchanged


def test_plan_graphics_tone_fallback_without_transcript():
    m = ClipManifest(clip_id="c1", source_path="/f.mp4", probe=ProbeInfo(duration_s=10))
    edl = plan_graphics(_edl(), [m])
    assert edl.graphics.title_card is not None
    assert "Energetic" in edl.graphics.title_card.text


# ---- agent config through the orchestrator ---------------------------------- #
def _run_pipeline(tmp_path, agent_config: AgentConfig):
    settings = Settings(anthropic_api_key="", ave_data_dir=tmp_path)
    orch = Orchestrator(LocalStorage(tmp_path), settings=settings,
                        llm=LLMClient(settings), on_progress=lambda *a: None)
    m = ClipManifest(
        clip_id="clip_01", source_path="/f.mp4", proxy_path="/p.mp4",
        probe=ProbeInfo(duration_s=10, width=1920, height=1080, fps=30, has_audio=True),
        shots=[Shot(start_s=i, end_s=i + 2.0) for i in range(0, 10, 2)],
        highlights=[Highlight(start_s=2, end_s=4, score=0.9, text="hook")],
    )
    state = PipelineState(
        project_id="p",
        brief=Brief(platform=Platform.youtube, target_duration_s=8.0,
                    tone=Tone.energetic, agent_config=agent_config),
        clips={}, manifests=[m], stage=Stage.editorial,
    )
    return orch.run(state)


def test_config_transition_style_forced(tmp_path):
    state = _run_pipeline(tmp_path, AgentConfig(transition_style="crossfade"))
    assert all(s.transition_in.value == "crossfade" for s in state.edl.timeline[1:])


def test_config_caption_style_and_lufs(tmp_path):
    state = _run_pipeline(
        tmp_path, AgentConfig(caption_style="karaoke_bold", target_lufs=-16.0)
    )
    assert state.edl.captions.style.value == "karaoke_bold"
    assert state.edl.output.target_lufs == -16.0


def test_config_duck_db_applied(tmp_path):
    state = _run_pipeline(tmp_path, AgentConfig(duck_db=-20.0))
    assert state.edl.music.duck_db == -20.0


def test_config_invalid_values_ignored(tmp_path):
    state = _run_pipeline(
        tmp_path, AgentConfig(transition_style="teleport", caption_style="comic_sans")
    )
    # Unknown values fall back to defaults rather than failing the pipeline.
    assert state.stage == Stage.done


def test_graph_traverses_broll_and_graphics(tmp_path):
    settings = Settings(anthropic_api_key="", ave_data_dir=tmp_path)
    events: list = []
    orch = Orchestrator(LocalStorage(tmp_path), settings=settings, llm=LLMClient(settings),
                        on_progress=lambda s, st, d: events.append(s))
    m = ClipManifest(
        clip_id="clip_01", source_path="/f.mp4", proxy_path="/p.mp4",
        probe=ProbeInfo(duration_s=10, width=1920, height=1080, fps=30, has_audio=True),
        shots=[Shot(start_s=0, end_s=5), Shot(start_s=5, end_s=10)],
        highlights=[Highlight(start_s=1, end_s=3, score=0.9, text="x")],
    )
    state = PipelineState(
        project_id="p",
        brief=Brief(platform=Platform.youtube, target_duration_s=8.0, tone=Tone.energetic),
        clips={}, manifests=[m], stage=Stage.editorial,
    )
    state = orch.run(state)
    assert "broll" in events and "graphics" in events
    assert events.index("broll") < events.index("graphics") < events.index("music_beat")
    assert state.stage == Stage.done
