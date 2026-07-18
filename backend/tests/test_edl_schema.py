"""EDL schema validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ave.edl.schema import (
    EDL,
    AspectRatio,
    Brief,
    Platform,
    Segment,
    Tone,
    Transition,
    json_schema,
)


def _brief(**kw) -> Brief:
    return Brief(platform=Platform.youtube, target_duration_s=kw.pop("dur", 60.0), tone=Tone.energetic, **kw)


def _seg(id_="seg_01", clip="clip_01", in_=0.0, out=5.0, **kw) -> Segment:
    return Segment(id=id_, source_clip=clip, **{"in": in_}, out=out, reason=kw.pop("reason", "test"), **kw)


def test_segment_in_out_alias_roundtrip():
    seg = _seg(in_=1.5, out=4.5)
    dumped = seg.model_dump(by_alias=True)
    assert dumped["in"] == 1.5 and dumped["out"] == 4.5
    # Reload from aliased dict.
    again = Segment.model_validate(dumped)
    assert again.in_ == 1.5


def test_segment_rejects_out_le_in():
    with pytest.raises(ValidationError):
        _seg(in_=5.0, out=5.0)
    with pytest.raises(ValidationError):
        _seg(in_=5.0, out=4.0)


def test_segment_id_pattern_enforced():
    with pytest.raises(ValidationError):
        Segment(id="bad id!", source_clip="clip_01", **{"in": 0.0}, out=1.0, reason="x")


def test_segment_requires_reason():
    with pytest.raises(ValidationError):
        Segment(id="seg_01", source_clip="clip_01", **{"in": 0.0}, out=1.0, reason="")


def test_duplicate_segment_ids_rejected():
    with pytest.raises(ValidationError):
        EDL(
            project_id="p",
            brief=_brief(),
            timeline=[_seg("seg_01"), _seg("seg_01", in_=5.0, out=9.0)],
        )


def test_duration_math_and_speed():
    edl = EDL(
        project_id="p",
        brief=_brief(dur=20.0),
        timeline=[_seg("seg_01", in_=0, out=10, speed=2.0), _seg("seg_02", clip="clip_02", in_=0, out=5)],
    )
    # seg1: 10s / 2x = 5s timeline; seg2: 5s => 10s total
    assert edl.total_duration_s == 10.0
    assert edl.timeline_offset_of("seg_02") == 5.0


def test_within_target_tolerance():
    brief = _brief(dur=10.0, duration_tolerance_pct=10.0)
    ok = EDL(project_id="p", brief=brief, timeline=[_seg(out=10.5)])   # 10.5 within 11
    bad = EDL(project_id="p", brief=brief, timeline=[_seg(out=12.0)])  # 12 outside 11
    assert ok.within_target()
    assert not bad.within_target()


def test_content_hash_deterministic_and_version_independent():
    a = EDL(project_id="p", brief=_brief(), timeline=[_seg()])
    b = EDL(project_id="p", brief=_brief(), timeline=[_seg()], version=7, notes="different notes")
    assert a.content_hash() == b.content_hash()


def test_content_hash_changes_with_timeline():
    a = EDL(project_id="p", brief=_brief(), timeline=[_seg(out=5.0)])
    b = EDL(project_id="p", brief=_brief(), timeline=[_seg(out=6.0)])
    assert a.content_hash() != b.content_hash()


def test_bump_increments_version_preserves_content_hash():
    a = EDL(project_id="p", brief=_brief(), timeline=[_seg()])
    b = a.bump(notes="feedback")
    assert b.version == a.version + 1
    assert b.content_hash() == a.content_hash()


def test_json_schema_is_wellformed():
    schema = json_schema()
    assert schema["type"] == "object"
    assert "timeline" in schema["properties"]
    # `in` alias must surface in the segment schema, not `in_`.
    seg_schema = schema["$defs"]["Segment"]["properties"]
    assert "in" in seg_schema and "in_" not in seg_schema


def test_timeline_offset_unknown_raises():
    edl = EDL(project_id="p", brief=_brief(), timeline=[_seg()])
    with pytest.raises(KeyError):
        edl.timeline_offset_of("seg_99")


def test_transition_enum_default_hard():
    assert _seg().transition_in == Transition.hard


def test_aspect_ratio_values():
    assert AspectRatio.vertical.value == "9:16"
