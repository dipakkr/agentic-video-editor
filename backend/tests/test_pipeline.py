"""End-to-end M1 pipeline test using synthetic manifests (no ffmpeg / no LLM).

Proves the graceful-degradation contract: with every optional dependency absent, the
orchestrator still produces a valid, versioned EDL and a deterministic render plan.
"""

from __future__ import annotations

from ave.agents import editorial
from ave.agents.render import render, resolve_sources
from ave.analysis.manifest import ClipManifest, Highlight, ProbeInfo, Shot
from ave.config import Settings
from ave.edl.schema import Brief, Platform, Tone
from ave.media import filtergraph
from ave.storage.store import LocalStorage


def _manifest(clip_id: str, dur: float, hook_at: float) -> ClipManifest:
    shots = [Shot(start_s=i, end_s=i + 2.0) for i in range(0, int(dur), 2)]
    return ClipManifest(
        clip_id=clip_id,
        source_path=f"/fake/{clip_id}.mp4",
        proxy_path=f"/fake/{clip_id}_proxy.mp4",
        probe=ProbeInfo(duration_s=dur, width=1920, height=1080, fps=30.0, has_audio=True),
        shots=shots,
        highlights=[Highlight(start_s=hook_at, end_s=hook_at + 2.0, score=0.95, text="the big moment")],
        analysis_features={"probe": True, "scenedetect": False, "whisperx": False},
    )


def _brief() -> Brief:
    return Brief(platform=Platform.youtube, target_duration_s=12.0, tone=Tone.energetic)


def test_deterministic_editor_builds_valid_edl():
    manifests = [_manifest("clip_01", 10.0, hook_at=4.0), _manifest("clip_02", 10.0, hook_at=2.0)]
    edl = editorial.build_edl("proj_test", _brief(), manifests, settings=Settings(anthropic_api_key=""))
    assert edl.timeline, "editor produced no segments"
    assert edl.within_target(), f"duration {edl.total_duration_s}s outside target"
    # Every segment must carry a justification.
    assert all(s.reason for s in edl.timeline)
    # First segment opens on the strongest hook (highest-scoring window).
    assert edl.timeline[0].reason.lower().startswith("strongest hook")


def test_render_plan_is_deterministic(tmp_path):
    manifests = [_manifest("clip_01", 10.0, 4.0), _manifest("clip_02", 10.0, 2.0)]
    edl = editorial.build_edl("proj_test", _brief(), manifests, settings=Settings(anthropic_api_key=""))
    sources = resolve_sources(edl, manifests, use_proxy=True)
    plan_a = filtergraph.build(edl, sources)
    plan_b = filtergraph.build(edl, sources)
    assert plan_a.filtergraph == plan_b.filtergraph
    assert "concat=" in plan_a.filtergraph  # hard cuts by default


def test_render_without_ffmpeg_writes_dry_plan(tmp_path):
    storage = LocalStorage(tmp_path)
    manifests = [_manifest("clip_01", 10.0, 4.0), _manifest("clip_02", 10.0, 2.0)]
    edl = editorial.build_edl("proj_test", _brief(), manifests, settings=Settings(anthropic_api_key=""))
    result = render(edl, manifests, "proj_test", storage)
    # ffmpeg absent in CI => dry render, but a plan artifact must exist.
    assert result["plan_path"]
    assert result["content_hash"] == edl.content_hash()
    plan = storage.read_json("proj_test", result["plan_path"].split("proj_test/")[-1])
    assert plan["argv"], "plan must contain resolved ffmpeg argv"


def test_prompt_log_written_on_llm_path(tmp_path, monkeypatch):
    """The prompt audit log is written for every LLM call (no real API needed)."""
    from ave.llm.client import LLMClient

    settings = Settings(anthropic_api_key="sk-test", ave_data_dir=tmp_path)
    client = LLMClient(settings)

    # Fake the Anthropic client so no network call happens but the prompt is logged.
    class _Block:
        text = '{"timeline": [{"source_clip": "clip_01", "in": 0, "out": 5, "reason": "hook"}]}'

    class _Resp:
        content = [_Block()]

    class _Fake:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

    client._client = _Fake()
    schema = {"type": "object", "required": ["timeline"], "properties": {"timeline": {"type": "array"}}}
    out = client.complete_json(
        project_id="proj_log", agent="editorial", system="sys", user="hello", schema=schema
    )
    assert out["timeline"][0]["source_clip"] == "clip_01"
    log = (tmp_path / "logs" / "prompts_proj_log.txt").read_text()
    assert "agent=editorial" in log and "hello" in log
