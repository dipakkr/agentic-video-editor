"""Feedback & revise module tests: EditOp applier purity + Revise Agent paths."""

from __future__ import annotations

import pytest

from ave.agents.revise import OPS_SCHEMA, fallback_ops, revise_edl
from ave.edl.schema import EDL, Brief, CaptionStyle, Segment, Transition
from ave.feedback.ops import EditOp, apply_ops


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
def _brief(dur: float = 15.0, tol: float = 10.0) -> Brief:
    return Brief(target_duration_s=dur, duration_tolerance_pct=tol)


def _seg(i: int, in_: float = 0.0, out: float = 5.0, **kw) -> Segment:
    return Segment(
        id=f"seg_{i:02d}",
        source_clip=kw.pop("clip", "clip_01"),
        **{"in": in_},
        out=out,
        reason=kw.pop("reason", f"beat {i}"),
        **kw,
    )


def _edl(n: int = 3, dur: float = 15.0, tol: float = 10.0) -> EDL:
    """n segments of 5s each (speed 1.0): seg_01..seg_NN."""
    return EDL(
        project_id="proj",
        brief=_brief(dur, tol),
        timeline=[_seg(i + 1) for i in range(n)],
    )


class FakeLLM:
    """Stand-in for LLMClient — no network, canned response or raised exception."""

    def __init__(self, payload: dict | None = None, exc: Exception | None = None,
                 available: bool = True):
        self.available = available
        self._payload = payload
        self._exc = exc
        self.last_kwargs: dict | None = None

    def complete_json(self, **kwargs) -> dict:
        self.last_kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return self._payload or {"ops": []}


# --------------------------------------------------------------------------- #
# apply_ops — per-op happy paths                                               #
# --------------------------------------------------------------------------- #
def test_remove_segment_happy():
    new, skipped = apply_ops(_edl(), [EditOp(op="remove_segment", segment_id="seg_02")], "x")
    assert skipped == []
    assert [s.id for s in new.timeline] == ["seg_01", "seg_03"]
    assert new.version == 2 and new.notes == "feedback: x"


def test_trim_happy_updates_in_out_and_reason():
    op = EditOp(op="trim", segment_id="seg_01", trim_start_s=1.0, trim_end_s=1.0,
                reason="tighten open")
    new, skipped = apply_ops(_edl(), [op])
    assert skipped == []
    seg = new.timeline[0]
    assert seg.in_ == 1.0 and seg.out == 4.0
    assert seg.reason.endswith("[revised: tighten open]")


def test_trim_negative_values_clamped_to_zero():
    op = EditOp(op="trim", segment_id="seg_01", trim_start_s=-3.0, trim_end_s=1.0)
    new, skipped = apply_ops(_edl(), [op])
    assert skipped == []
    assert new.timeline[0].in_ == 0.0 and new.timeline[0].out == 4.0


def test_reorder_happy_moves_segment():
    op = EditOp(op="reorder", segment_id="seg_03", to_index=0)
    new, skipped = apply_ops(_edl(), [op])
    assert skipped == []
    assert [s.id for s in new.timeline] == ["seg_03", "seg_01", "seg_02"]


def test_reorder_clamps_out_of_range_index():
    new, skipped = apply_ops(_edl(), [EditOp(op="reorder", segment_id="seg_01", to_index=99)])
    assert skipped == []
    assert [s.id for s in new.timeline] == ["seg_02", "seg_03", "seg_01"]


def test_retime_happy():
    new, skipped = apply_ops(_edl(), [EditOp(op="retime", segment_id="seg_02", speed=2.0)])
    assert skipped == []
    assert new.timeline[1].speed == 2.0
    assert new.timeline[1].timeline_duration_s == 2.5


def test_retime_speed_clamped_both_ends():
    ops = [
        EditOp(op="retime", segment_id="seg_01", speed=10.0),
        EditOp(op="retime", segment_id="seg_02", speed=0.05),
    ]
    new, skipped = apply_ops(_edl(), ops)
    assert skipped == []
    assert new.timeline[0].speed == 4.0
    assert new.timeline[1].speed == 0.25


def test_set_transition_happy():
    op = EditOp(op="set_transition", segment_id="seg_02", transition="crossfade",
                transition_duration_s=0.5)
    new, skipped = apply_ops(_edl(), [op])
    assert skipped == []
    assert new.timeline[1].transition_in == Transition.crossfade
    assert new.timeline[1].transition_duration_s == 0.5


def test_set_transition_duration_clamped_to_schema_max():
    op = EditOp(op="set_transition", segment_id="seg_01", transition="crossfade",
                transition_duration_s=9.0)
    new, skipped = apply_ops(_edl(), [op])
    assert skipped == []
    assert new.timeline[0].transition_duration_s == 3.0


def test_change_caption_style_happy():
    new, skipped = apply_ops(
        _edl(), [EditOp(op="change_caption_style", caption_style="karaoke_bold")]
    )
    assert skipped == []
    assert new.captions.style == CaptionStyle.karaoke_bold


def test_set_target_duration_updates_brief_and_trims_tail():
    # 3 x 5s = 15s; new target 8 with 10% tol => ceiling 8.8:
    # seg_01 kept (5), seg_02 trimmed to 3.8s, seg_03 dropped.
    new, skipped = apply_ops(
        _edl(), [EditOp(op="set_target_duration", target_duration_s=8.0, reason="tighter")]
    )
    assert skipped == []
    assert new.brief.target_duration_s == 8.0
    assert [s.id for s in new.timeline] == ["seg_01", "seg_02"]
    assert new.timeline[1].out == 3.8
    assert new.total_duration_s == pytest.approx(8.8)
    assert new.timeline[1].reason.endswith("[revised: tighter]")


def test_set_target_duration_no_trim_when_under_ceiling():
    new, skipped = apply_ops(
        _edl(), [EditOp(op="set_target_duration", target_duration_s=20.0)]
    )
    assert skipped == []
    assert new.brief.target_duration_s == 20.0
    assert len(new.timeline) == 3 and new.total_duration_s == 15.0
    assert new.version == 2  # still an applied edit => bumped


# --------------------------------------------------------------------------- #
# apply_ops — skip paths and guards                                            #
# --------------------------------------------------------------------------- #
def test_unknown_segment_id_skipped():
    edl = _edl()
    new, skipped = apply_ops(edl, [EditOp(op="remove_segment", segment_id="seg_99")])
    assert len(skipped) == 1 and "seg_99" in skipped[0]
    assert new is edl  # nothing applied => original returned, not bumped


def test_remove_keeps_at_least_one_segment():
    edl = _edl(n=1)
    new, skipped = apply_ops(edl, [EditOp(op="remove_segment", segment_id="seg_01")])
    assert len(skipped) == 1 and "empty" in skipped[0]
    assert len(new.timeline) == 1 and new.version == edl.version


def test_trim_collapse_guard():
    op = EditOp(op="trim", segment_id="seg_01", trim_start_s=2.5, trim_end_s=2.4)
    new, skipped = apply_ops(_edl(), [op])  # 5 - 4.9 = 0.1s < 0.3s
    assert len(skipped) == 1 and "collapse" in skipped[0]
    assert new.timeline[0].in_ == 0.0 and new.timeline[0].out == 5.0


def test_reorder_missing_index_and_unknown_id_skipped():
    ops = [
        EditOp(op="reorder", segment_id="seg_01"),  # no to_index
        EditOp(op="reorder", segment_id="seg_99", to_index=0),
    ]
    edl = _edl()
    new, skipped = apply_ops(edl, ops)
    assert len(skipped) == 2
    assert new is edl


def test_retime_missing_speed_skipped():
    _, skipped = apply_ops(_edl(), [EditOp(op="retime", segment_id="seg_01")])
    assert len(skipped) == 1 and "speed" in skipped[0]


def test_set_transition_invalid_enum_skipped():
    op = EditOp(op="set_transition", segment_id="seg_01", transition="wipe")
    new, skipped = apply_ops(_edl(), [op])
    assert len(skipped) == 1 and "wipe" in skipped[0]
    assert new.timeline[0].transition_in == Transition.hard


def test_change_caption_style_invalid_skipped():
    _, skipped = apply_ops(
        _edl(), [EditOp(op="change_caption_style", caption_style="comic_sans")]
    )
    assert len(skipped) == 1 and "comic_sans" in skipped[0]


def test_set_target_duration_invalid_values_skipped():
    edl = _edl()
    ops = [
        EditOp(op="set_target_duration"),                          # None
        EditOp(op="set_target_duration", target_duration_s=-5.0),  # negative
        EditOp(op="set_target_duration", target_duration_s=4000.0),  # > 3600
    ]
    new, skipped = apply_ops(edl, ops)
    assert len(skipped) == 3
    assert new is edl and new.brief.target_duration_s == 15.0


def test_set_target_duration_too_small_keeps_timeline_and_brief():
    # ceiling = 0.4 * 1.1 = 0.44 < 0.5 keep-threshold => no segment survives => skip whole op.
    edl = _edl()
    new, skipped = apply_ops(
        edl, [EditOp(op="set_target_duration", target_duration_s=0.4)]
    )
    assert len(skipped) == 1
    assert new is edl and new.brief.target_duration_s == 15.0


# --------------------------------------------------------------------------- #
# apply_ops — purity, ordering, bump semantics                                 #
# --------------------------------------------------------------------------- #
def test_apply_ops_is_pure_input_untouched():
    edl = _edl()
    before = edl.model_dump(mode="json", by_alias=True)
    ops = [
        EditOp(op="remove_segment", segment_id="seg_03"),
        EditOp(op="retime", segment_id="seg_01", speed=2.0),
        EditOp(op="set_target_duration", target_duration_s=8.0),
        EditOp(op="change_caption_style", caption_style="none"),
    ]
    new, _ = apply_ops(edl, ops, "big revision")
    assert edl.model_dump(mode="json", by_alias=True) == before
    assert new is not edl and new.version == 2


def test_no_applied_ops_means_no_bump():
    edl = _edl()
    new, skipped = apply_ops(edl, [EditOp(op="remove_segment", segment_id="seg_99")], "n")
    assert new is edl
    assert new.version == 1 and new.notes != "feedback: n"
    assert skipped


def test_empty_ops_list_is_a_noop():
    edl = _edl()
    new, skipped = apply_ops(edl, [], "nothing")
    assert new is edl and skipped == []


def test_partial_apply_still_bumps_once():
    ops = [
        EditOp(op="remove_segment", segment_id="seg_99"),  # skipped
        EditOp(op="retime", segment_id="seg_01", speed=1.5),  # applies
    ]
    new, skipped = apply_ops(_edl(), ops, "mixed")
    assert len(skipped) == 1
    assert new.version == 2 and new.notes == "feedback: mixed"


def test_ops_apply_in_order_later_op_sees_earlier_edit():
    ops = [
        EditOp(op="remove_segment", segment_id="seg_02"),
        EditOp(op="trim", segment_id="seg_02", trim_end_s=1.0),  # now unknown
    ]
    new, skipped = apply_ops(_edl(), ops)
    assert [s.id for s in new.timeline] == ["seg_01", "seg_03"]
    assert len(skipped) == 1 and "seg_02" in skipped[0]


def test_reason_annotation_falls_back_to_op_name():
    new, _ = apply_ops(_edl(), [EditOp(op="retime", segment_id="seg_01", speed=1.5)])
    assert new.timeline[0].reason.endswith("[revised: retime]")


def test_bump_preserves_content_hash_semantics():
    edl = _edl()
    new, _ = apply_ops(edl, [EditOp(op="retime", segment_id="seg_01", speed=2.0)], "n")
    assert new.content_hash() != edl.content_hash()  # render-affecting change


# --------------------------------------------------------------------------- #
# fallback_ops — keyword parsing, every rule                                   #
# --------------------------------------------------------------------------- #
def test_fallback_remove_by_number():
    ops = fallback_ops(_edl(), "please remove segment 2, it drags")
    assert [(o.op, o.segment_id) for o in ops] == [("remove_segment", "seg_02")]
    assert "remove segment 2" in ops[0].reason


@pytest.mark.parametrize(
    "note, expected",
    [
        ("delete the first segment", "seg_01"),
        ("remove the second segment", "seg_02"),
        ("cut the third segment", "seg_03"),
        ("cut the last segment", "seg_03"),
    ],
)
def test_fallback_remove_ordinals(note, expected):
    ops = fallback_ops(_edl(), note)
    assert len(ops) == 1 and ops[0].op == "remove_segment"
    assert ops[0].segment_id == expected


def test_fallback_remove_direct_seg_id():
    ops = fallback_ops(_edl(), "remove seg_02 entirely")
    assert ops[0].segment_id == "seg_02"


def test_fallback_remove_out_of_range_number_passes_through():
    ops = fallback_ops(_edl(), "remove segment 9")
    assert ops[0].segment_id == "seg_09"
    _, skipped = apply_ops(_edl(), ops)  # then reported as unknown, not applied
    assert len(skipped) == 1


@pytest.mark.parametrize(
    "note, expected",
    [
        ("make it 20 seconds", 20.0),
        ("shorten to 12", 12.0),
        ("target 30 s", 30.0),
        ("make it 7.5s", 7.5),
    ],
)
def test_fallback_explicit_target_duration(note, expected):
    ops = fallback_ops(_edl(), note)
    assert len(ops) == 1 and ops[0].op == "set_target_duration"
    assert ops[0].target_duration_s == expected


def test_fallback_shorter_scales_down_20pct():
    ops = fallback_ops(_edl(dur=15.0), "can you make it shorter?")
    assert ops[0].op == "set_target_duration" and ops[0].target_duration_s == 12.0
    assert ops[0].reason == "shorter"


def test_fallback_longer_scales_up_20pct():
    ops = fallback_ops(_edl(dur=15.0), "a bit longer please")
    assert ops[0].op == "set_target_duration" and ops[0].target_duration_s == 18.0


def test_fallback_punchier_intro_trims_first_segment():
    ops = fallback_ops(_edl(), "make the opening punchier")  # or "punchier intro"
    assert len(ops) == 1
    op = ops[0]
    assert op.op == "trim" and op.segment_id == "seg_01"
    assert op.trim_end_s == 2.0  # 5s -> 3s timeline
    new, skipped = apply_ops(_edl(), ops)
    assert skipped == [] and new.timeline[0].timeline_duration_s == 3.0


def test_fallback_punchier_skipped_when_already_short():
    edl = EDL(project_id="p", brief=_brief(),
              timeline=[_seg(1, out=2.5), _seg(2, out=5.0)])
    assert fallback_ops(edl, "snappier hook") == []


def test_fallback_faster_retimes_all_segments():
    ops = fallback_ops(_edl(), "this feels slow, make it faster")
    assert [o.op for o in ops] == ["retime"] * 3
    assert all(o.speed == 1.25 and o.reason == "faster" for o in ops)
    assert [o.segment_id for o in ops] == ["seg_01", "seg_02", "seg_03"]


def test_fallback_slower_retimes_all_segments():
    ops = fallback_ops(_edl(), "slower please")
    assert all(o.op == "retime" and o.speed == 0.9 for o in ops) and len(ops) == 3


def test_fallback_crossfade_all_but_first():
    for note in ("use crossfade cuts", "smoother transitions please"):
        ops = fallback_ops(_edl(), note)
        assert [o.segment_id for o in ops] == ["seg_02", "seg_03"]
        assert all(
            o.op == "set_transition"
            and o.transition == "crossfade"
            and o.transition_duration_s == 0.5
            for o in ops
        )


@pytest.mark.parametrize(
    "note, style",
    [
        ("karaoke captions please", "karaoke_bold"),
        ("word-by-word captions", "karaoke_bold"),
        ("clean subtitles", "clean_subtitle"),
        ("just simple subtitles", "clean_subtitle"),
        ("no captions at all", "none"),
    ],
)
def test_fallback_caption_styles(note, style):
    ops = fallback_ops(_edl(), note)
    assert len(ops) == 1
    assert ops[0].op == "change_caption_style" and ops[0].caption_style == style


def test_fallback_no_match_returns_empty():
    assert fallback_ops(_edl(), "I love it, great job!") == []


def test_fallback_priority_remove_beats_speed():
    ops = fallback_ops(_edl(), "remove segment 2 and make everything faster")
    assert len(ops) == 1 and ops[0].op == "remove_segment"


# --------------------------------------------------------------------------- #
# revise_edl — end to end                                                      #
# --------------------------------------------------------------------------- #
def test_revise_unavailable_llm_uses_fallback():
    edl = _edl()
    new, skipped = revise_edl(edl, [], "make it faster", llm=FakeLLM(available=False))
    assert skipped == []
    assert all(s.speed == 1.25 for s in new.timeline)
    assert new.version == 2 and new.notes == "feedback: make it faster"


def test_revise_no_actionable_note_reports_and_leaves_edl():
    edl = _edl()
    new, skipped = revise_edl(edl, [], "looks great!", llm=FakeLLM(available=False))
    assert new is edl and skipped == ["no actionable op parsed"]


def test_revise_llm_path_applies_canned_ops_and_ignores_invalid():
    payload = {
        "ops": [
            {"op": "retime", "segment_id": "seg_01", "speed": 2.0, "reason": "speed up"},
            {"op": "explode_timeline"},  # invalid op name -> ignored at parse
            {"op": "remove_segment", "segment_id": "seg_03", "reason": "drop outro"},
        ]
    }
    fake = FakeLLM(payload=payload)
    edl = _edl()
    new, skipped = revise_edl(edl, [], "speed up the open, drop the outro", llm=fake)
    assert skipped == []
    assert new.timeline[0].speed == 2.0
    assert [s.id for s in new.timeline] == ["seg_01", "seg_02"]
    assert new.version == 2
    # LLM was called with the revise agent contract.
    assert fake.last_kwargs is not None
    assert fake.last_kwargs["agent"] == "revise"
    assert fake.last_kwargs["schema"] is OPS_SCHEMA
    assert fake.last_kwargs["project_id"] == "proj"
    assert "seg_01" in fake.last_kwargs["user"]  # segment digest present


def test_revise_llm_exception_falls_back_deterministically():
    fake = FakeLLM(exc=RuntimeError("api down"))
    edl = _edl()
    new, skipped = revise_edl(edl, [], "slower", llm=fake)
    assert skipped == []
    assert all(s.speed == 0.9 for s in new.timeline)
    assert fake.last_kwargs is not None  # LLM path was attempted first


def test_revise_llm_empty_ops_reports_no_actionable():
    fake = FakeLLM(payload={"ops": []})
    edl = _edl()
    new, skipped = revise_edl(edl, [], "hmm", llm=fake)
    assert new is edl and skipped == ["no actionable op parsed"]


def test_revise_never_mutates_input():
    edl = _edl()
    before = edl.model_dump(mode="json", by_alias=True)
    revise_edl(edl, [], "make it faster and remove segment 2", llm=FakeLLM(available=False))
    assert edl.model_dump(mode="json", by_alias=True) == before


def test_ops_schema_shape():
    assert OPS_SCHEMA["required"] == ["ops"]
    item = OPS_SCHEMA["properties"]["ops"]["items"]
    assert item["required"] == ["op"]
    assert "remove_segment" in item["properties"]["op"]["enum"]
