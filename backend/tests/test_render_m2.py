"""Tests for the M2 render layers: music bed, ducking, loudness, caption burn-in."""

from __future__ import annotations

from ave.edl.schema import EDL, Brief, Music, Platform, Segment, Tone
from ave.media.filtergraph import (
    augment_with_music_and_captions,
    build,
    _escape_filter_path,
)


def _edl(*, ducking: bool = True, offset: float = 0.0) -> EDL:
    return EDL(
        project_id="p",
        brief=Brief(platform=Platform.youtube, target_duration_s=10.0, tone=Tone.energetic),
        timeline=[
            Segment(id="seg_01", source_clip="c1", **{"in": 0.0}, out=4.0, reason="a"),
            Segment(id="seg_02", source_clip="c2", **{"in": 1.0}, out=7.0, reason="b"),
        ],
        music=Music(track_id="t", offset_s=offset, ducking=ducking),
    )


SOURCES = {"c1": "/fake/c1.mp4", "c2": "/fake/c2.mp4"}


def test_music_layer_adds_input_and_chain():
    edl = _edl(offset=2.5)
    plan = augment_with_music_and_captions(
        build(edl, SOURCES), edl, music_path="/fake/track.wav"
    )
    assert plan.inputs[-1] == "/fake/track.wav"
    # atrim honours music offset over the timeline window (10s total).
    assert "atrim=start=2.5:end=12.5" in plan.filtergraph
    assert "afade=t=in:st=0:d=0.5" in plan.filtergraph
    assert "afade=t=out:st=8.5:d=1.5" in plan.filtergraph
    assert plan.maps == ["[vfinal]", "[afinal]"]


def test_ducking_uses_sidechain_compress():
    edl = _edl(ducking=True)
    plan = augment_with_music_and_captions(build(edl, SOURCES), edl, music_path="/m.wav")
    assert "asplit=2[dlg][sc]" in plan.filtergraph
    assert "sidechaincompress=" in plan.filtergraph
    assert "amix=inputs=2:duration=first" in plan.filtergraph


def test_no_ducking_plain_mix():
    edl = _edl(ducking=False)
    plan = augment_with_music_and_captions(build(edl, SOURCES), edl, music_path="/m.wav")
    assert "sidechaincompress=" not in plan.filtergraph
    assert "amix=inputs=2:duration=first" in plan.filtergraph


def test_loudnorm_targets_output_lufs():
    edl = _edl()
    plan = augment_with_music_and_captions(build(edl, SOURCES), edl, music_path="/m.wav")
    assert "loudnorm=I=-14.0:TP=-1.5:LRA=11" in plan.filtergraph


def test_caption_burn_in_and_path_escaping():
    edl = _edl()
    plan = augment_with_music_and_captions(
        build(edl, SOURCES), edl, ass_path="/tmp/a b/subs.ass"
    )
    assert "ass=filename='/tmp/a b/subs.ass'" in plan.filtergraph
    # No music requested: still mastered to LUFS, no music input added.
    assert len(plan.inputs) == 2
    assert "loudnorm=" in plan.filtergraph


def test_escape_filter_path():
    assert _escape_filter_path("C:\\x\\y.ass") == "C\\:/x/y.ass"
    assert _escape_filter_path("/a'b.ass") == "/a\\'b.ass"


def test_augment_is_deterministic():
    edl = _edl()
    a = augment_with_music_and_captions(build(edl, SOURCES), edl, music_path="/m.wav",
                                        ass_path="/s.ass")
    b = augment_with_music_and_captions(build(edl, SOURCES), edl, music_path="/m.wav",
                                        ass_path="/s.ass")
    assert a.args == b.args
