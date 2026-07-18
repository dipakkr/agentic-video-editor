"""Tests for M4 manager features: export presets, reframe, publish gate, QC routing."""

from __future__ import annotations

import pytest

from ave.agents.publish import PublishNotConfigured, PublishNotConfirmed, publish_youtube
from ave.edl.schema import EDL, AspectRatio, Brief, Platform, Segment, Tone
from ave.media.exports import EXPORT_PRESETS, export_all, variant_edl
from ave.media.filtergraph import build


def _edl(n_segments: int = 3, seg_len: float = 40.0) -> EDL:
    return EDL(
        project_id="p",
        brief=Brief(platform=Platform.youtube, target_duration_s=n_segments * seg_len,
                    tone=Tone.energetic),
        timeline=[
            Segment(id=f"seg_{i:02d}", source_clip=f"c{i}", **{"in": 0.0}, out=seg_len,
                    reason=f"segment {i}")
            for i in range(1, n_segments + 1)
        ],
    )


def test_variant_sets_output_spec():
    v = variant_edl(_edl(), "shorts")
    assert v.output.aspect_ratio == AspectRatio.vertical
    assert (v.output.width, v.output.height) == (1080, 1920)
    assert v.output.reframe == "center_crop"
    assert v.output.use_proxy is False
    assert v.version == _edl().version + 1
    assert "export variant: shorts" in v.notes


def test_shorts_family_trims_to_90s():
    edl = _edl(n_segments=3, seg_len=40.0)  # 120s total
    v = variant_edl(edl, "reels")
    assert v.total_duration_s <= 90.0 + 1e-6
    # Boundary segment partially trimmed, whole segments kept.
    assert len(v.timeline) == 3
    assert v.timeline[2].out == pytest.approx(10.0, abs=0.01)


def test_youtube_preset_keeps_full_duration():
    edl = _edl(n_segments=3, seg_len=40.0)
    v = variant_edl(edl, "youtube")
    assert v.total_duration_s == edl.total_duration_s
    assert v.output.reframe == "pad"


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        variant_edl(_edl(), "myspace")


def test_center_crop_in_filtergraph():
    v = variant_edl(_edl(1), "shorts")
    plan = build(v, {"c1": "/fake/c1.mp4"})
    assert "force_original_aspect_ratio=increase,crop=1080:1920" in plan.filtergraph
    assert "pad=" not in plan.filtergraph


def test_pad_in_filtergraph_for_wide():
    v = variant_edl(_edl(1), "youtube")
    plan = build(v, {"c1": "/fake/c1.mp4"})
    assert "pad=1920:1080" in plan.filtergraph
    assert "crop=" not in plan.filtergraph


def test_export_all_defaults_to_brief_platform(tmp_path):
    from ave.analysis.manifest import ClipManifest, ProbeInfo
    from ave.storage.store import LocalStorage

    edl = _edl(1, 10.0)
    manifests = [ClipManifest(clip_id="c1", source_path="/fake/c1.mp4",
                              probe=ProbeInfo(duration_s=40.0))]
    results = export_all(edl, manifests, "p", LocalStorage(tmp_path))
    assert set(results) == {"youtube"}
    assert results["youtube"]["preset"] == "youtube"


def test_every_preset_is_well_formed():
    for name, preset in EXPORT_PRESETS.items():
        assert preset["width"] > 0 and preset["height"] > 0
        v = variant_edl(_edl(1, 10.0), name)
        assert v.output.width == preset["width"]


# ---- publish gate ---------------------------------------------------------- #
def test_publish_requires_confirm(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(PublishNotConfirmed):
        publish_youtube(str(video), title="t", description="d", confirm=False)


def test_publish_confirm_default_is_false(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(PublishNotConfirmed):
        publish_youtube(str(video), title="t", description="d")


def test_publish_requires_credentials(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(PublishNotConfigured):
        publish_youtube(str(video), title="t", description="d", confirm=True)


def test_publish_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish_youtube(str(tmp_path / "missing.mp4"), title="t", description="d",
                        confirm=True)


# ---- orchestrator QC routing ------------------------------------------------ #
def test_graph_reaches_done_through_qc_release(tmp_path):
    """With or without the QC/Release modules, the graph must reach done."""
    from ave.analysis.manifest import ClipManifest, Highlight, ProbeInfo, Shot
    from ave.config import Settings
    from ave.llm.client import LLMClient
    from ave.orchestrator.graph import Orchestrator, PipelineState, Stage
    from ave.storage.store import LocalStorage

    settings = Settings(anthropic_api_key="", ave_data_dir=tmp_path)
    events: list = []
    orch = Orchestrator(LocalStorage(tmp_path), settings=settings, llm=LLMClient(settings),
                        on_progress=lambda s, st, d: events.append((s, st)))
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
    stages = [s for s, _ in events]
    assert "qc" in stages and "release" in stages
    assert stages.index("qc") > stages.index("render")
    assert stages.index("release") > stages.index("qc")
    assert state.stage == Stage.done
