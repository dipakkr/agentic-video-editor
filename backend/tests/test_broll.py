"""B-roll module tests: classification math, cutaway placement, pool discipline, purity."""

from __future__ import annotations

from ave.agents.broll import plan_overlays
from ave.analysis.manifest import ClipManifest, ProbeInfo, TranscriptSegment, Word
from ave.broll.classify import classify_clips, speech_density
from ave.edl.schema import EDL, AgentConfig, Brief, Segment


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
def _words(n: int) -> list[Word]:
    return [Word(word=f"w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.3) for i in range(n)]


def _talking_manifest(clip_id: str = "a1", duration: float = 60.0, n_words: int = 60) -> ClipManifest:
    words = _words(n_words)
    seg = TranscriptSegment(
        start_s=0.0, end_s=duration, text=" ".join(w.word for w in words), words=words
    )
    return ClipManifest(
        clip_id=clip_id,
        source_path=f"/{clip_id}.mp4",
        probe=ProbeInfo(duration_s=duration),
        transcript=[seg],
    )


def _silent_manifest(clip_id: str, duration: float = 20.0) -> ClipManifest:
    return ClipManifest(
        clip_id=clip_id, source_path=f"/{clip_id}.mp4", probe=ProbeInfo(duration_s=duration)
    )


def _edl(segments: list[Segment] | None = None, enable_broll: bool = True) -> EDL:
    if segments is None:
        segments = [Segment(id="seg_01", source_clip="a1", in_=0.0, out=30.0, reason="talking")]
    brief = Brief(target_duration_s=30.0, agent_config=AgentConfig(enable_broll=enable_broll))
    return EDL(project_id="proj", brief=brief, timeline=segments)


# --------------------------------------------------------------------------- #
# speech_density                                                               #
# --------------------------------------------------------------------------- #
def test_speech_density_from_word_lists():
    # 30 words over 60s -> exactly 0.5 words/s.
    m = _talking_manifest(duration=60.0, n_words=30)
    assert speech_density(m) == 0.5


def test_speech_density_text_split_fallback():
    seg = TranscriptSegment(start_s=0.0, end_s=10.0, text="one two three four five")
    m = ClipManifest(
        clip_id="c1", source_path="/c1.mp4", probe=ProbeInfo(duration_s=10.0), transcript=[seg]
    )
    assert speech_density(m) == 0.5


def test_speech_density_mixed_segments():
    with_words = TranscriptSegment(start_s=0.0, end_s=5.0, text="a b c", words=_words(4))
    text_only = TranscriptSegment(start_s=5.0, end_s=10.0, text="d e f")
    m = ClipManifest(
        clip_id="c1",
        source_path="/c1.mp4",
        probe=ProbeInfo(duration_s=10.0),
        transcript=[with_words, text_only],
    )
    # 4 (word list) + 3 (split) = 7 words over 10s.
    assert speech_density(m) == 0.7


def test_speech_density_no_transcript_is_zero():
    assert speech_density(_silent_manifest("c1", duration=20.0)) == 0.0


def test_speech_density_zero_duration_is_zero():
    m = _talking_manifest("c1", duration=60.0, n_words=30)
    m = m.model_copy(update={"probe": ProbeInfo(duration_s=0.0)})
    assert speech_density(m) == 0.0


def test_speech_density_rounds_to_three_decimals():
    # 10 words / 3s = 3.333333... -> 3.333
    m = _talking_manifest("c1", duration=3.0, n_words=10)
    assert speech_density(m) == 3.333


# --------------------------------------------------------------------------- #
# classify_clips                                                               #
# --------------------------------------------------------------------------- #
def test_classify_boundary_exactly_half_is_aroll():
    m = _talking_manifest("c1", duration=60.0, n_words=30)  # density == 0.5
    assert classify_clips([m]) == {"c1": "aroll"}


def test_classify_below_half_is_broll():
    m = _talking_manifest("c1", duration=60.0, n_words=29)  # density ~0.483
    assert classify_clips([m]) == {"c1": "broll"}


def test_classify_iterates_in_clip_id_order():
    ms = [_silent_manifest("c3"), _talking_manifest("c1"), _silent_manifest("c2")]
    result = classify_clips(ms)
    assert list(result) == ["c1", "c2", "c3"]
    assert result == {"c1": "aroll", "c2": "broll", "c3": "broll"}


# --------------------------------------------------------------------------- #
# plan_overlays — placement math                                               #
# --------------------------------------------------------------------------- #
def _default_run(n_broll: int = 2) -> tuple[EDL, EDL]:
    manifests = [_talking_manifest("a1")] + [
        _silent_manifest(f"b{i}", duration=30.0) for i in range(1, n_broll + 1)
    ]
    edl = _edl()
    return edl, plan_overlays(edl, manifests)


def test_first_cutaway_starts_at_offset_plus_two():
    _, result = _default_run()
    assert result.overlays
    assert result.overlays[0].timeline_start_s == 2.0


def test_cutaway_spacing_is_overlay_len_plus_gap():
    _, result = _default_run()
    starts = [o.timeline_start_s for o in result.overlays]
    # 30s segment, start 2.0, step 3+4=7, must end by 30-1.5=28.5 -> 2, 9, 16, 23.
    assert starts == [2.0, 9.0, 16.0, 23.0]


def test_tail_clearance_of_1_5s_respected():
    manifests = [
        _talking_manifest("a1", duration=60.0, n_words=60),
        _silent_manifest("b1", duration=30.0),
    ]
    seg = Segment(id="seg_01", source_clip="a1", in_=0.0, out=10.0, reason="short talk")
    result = plan_overlays(_edl([seg]), manifests)
    # t=2 fits (5.0 <= 8.5); t=9 would end at 12 > 8.5 -> exactly one cutaway.
    assert len(result.overlays) == 1
    assert result.overlays[0].timeline_start_s + result.overlays[0].duration_s <= 10.0 - 1.5


def test_no_cutaways_on_segment_shorter_than_min_talking():
    manifests = [_talking_manifest("a1"), _silent_manifest("b1")]
    seg = Segment(id="seg_01", source_clip="a1", in_=0.0, out=5.0, reason="brief aside")
    edl = _edl([seg])
    result = plan_overlays(edl, manifests)
    assert result is edl  # nothing placed -> unchanged object


def test_second_segment_offset_used_for_placement():
    manifests = [_talking_manifest("a1"), _silent_manifest("b1", duration=30.0)]
    segs = [
        Segment(id="seg_01", source_clip="a1", in_=30.0, out=35.0, reason="cold open"),
        Segment(id="seg_02", source_clip="a1", in_=0.0, out=20.0, reason="talking"),
    ]
    result = plan_overlays(_edl(segs), manifests)
    # seg_02 starts at timeline 5.0 -> first cutaway at 7.0.
    assert result.overlays[0].timeline_start_s == 7.0


# --------------------------------------------------------------------------- #
# plan_overlays — source pool discipline                                       #
# --------------------------------------------------------------------------- #
def test_round_robin_across_two_broll_clips():
    _, result = _default_run(n_broll=2)
    assert [o.source_clip for o in result.overlays] == ["b1", "b2", "b1", "b2"]


def test_source_windows_never_reused_globally():
    _, result = _default_run(n_broll=2)
    windows = [(o.source_clip, o.in_, o.out) for o in result.overlays]
    assert len(windows) == len(set(windows))
    # Successive windows within one clip advance, no overlap.
    b1 = [(o.in_, o.out) for o in result.overlays if o.source_clip == "b1"]
    assert b1 == [(0.0, 3.0), (3.0, 6.0)]


def test_pool_exhaustion_stops_placement():
    # One b-roll clip with room for exactly one 3s window (4s clip: [0,3], remainder skipped).
    manifests = [_talking_manifest("a1"), _silent_manifest("b1", duration=4.0)]
    result = plan_overlays(_edl(), manifests)
    assert len(result.overlays) == 1
    assert (result.overlays[0].in_, result.overlays[0].out) == (0.0, 3.0)


def test_windows_shorter_than_overlay_len_skipped():
    # 2.5s clip has no 3s window -> empty pool -> unchanged.
    manifests = [_talking_manifest("a1"), _silent_manifest("b1", duration=2.5)]
    edl = _edl()
    result = plan_overlays(edl, manifests)
    assert result is edl


def test_timeline_used_windows_excluded_from_pool():
    # b1 also feeds a timeline segment covering [0, 6]; overlays must come after.
    manifests = [_talking_manifest("a1"), _silent_manifest("b1", duration=12.0)]
    segs = [
        Segment(id="seg_01", source_clip="a1", in_=0.0, out=30.0, reason="talking"),
        Segment(id="seg_02", source_clip="b1", in_=0.0, out=6.0, reason="scenic outro"),
    ]
    result = plan_overlays(_edl(segs), manifests)
    assert len(result.overlays) == 2  # only [6,9] and [9,12] remain
    assert all(o.in_ >= 6.0 for o in result.overlays)


# --------------------------------------------------------------------------- #
# plan_overlays — gating, purity, versioning                                   #
# --------------------------------------------------------------------------- #
def test_enable_broll_false_returns_same_object():
    manifests = [_talking_manifest("a1"), _silent_manifest("b1")]
    edl = _edl(enable_broll=False)
    result = plan_overlays(edl, manifests)
    assert result is edl
    assert result.version == 1 and result.overlays == []


def test_no_broll_clips_returns_same_object():
    manifests = [_talking_manifest("a1"), _talking_manifest("a2")]
    edl = _edl()
    result = plan_overlays(edl, manifests)
    assert result is edl


def test_input_edl_not_mutated():
    manifests = [_talking_manifest("a1"), _silent_manifest("b1", duration=30.0)]
    edl = _edl()
    before = edl.model_dump()
    result = plan_overlays(edl, manifests)
    assert result is not edl
    assert edl.model_dump() == before
    assert edl.overlays == []


def test_version_bump_and_notes():
    edl, result = _default_run()
    assert result.version == edl.version + 1
    assert result.notes == "b-roll pass"


def test_overlay_ids_sequential_and_fields():
    _, result = _default_run()
    assert [o.id for o in result.overlays] == ["ovl_01", "ovl_02", "ovl_03", "ovl_04"]
    for o in result.overlays:
        assert o.mute is True
        assert "seg_01" in o.reason and o.source_clip in o.reason


def test_result_survives_model_validate_round_trip():
    _, result = _default_run()
    dumped = result.model_dump(mode="json", by_alias=True)
    revived = EDL.model_validate(dumped)
    assert revived.model_dump() == result.model_dump()


def test_determinism_two_runs_identical():
    def run() -> EDL:
        manifests = [
            _talking_manifest("a1"),
            _silent_manifest("b1", duration=30.0),
            _silent_manifest("b2", duration=30.0),
        ]
        return plan_overlays(_edl(), manifests)

    assert run().model_dump() == run().model_dump()
