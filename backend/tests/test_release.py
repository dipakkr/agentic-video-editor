"""Release metadata tests — titles, chapters, description, hashtags, thumbnails, and the
release-kit agent (LLM replace/validate/fallback). Everything runs offline: the LLM is
either absent (no API key) or a fake object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ave.agents.release import ReleaseKit, build_release_kit
from ave.analysis.manifest import ClipManifest, Highlight, TranscriptSegment
from ave.config import Settings
from ave.edl.schema import EDL, Brief, Platform, Segment, Tone
from ave.release.metadata import (
    gen_chapters,
    gen_description,
    gen_hashtags,
    gen_titles,
    thumbnail_candidates,
)
from ave.storage.store import LocalStorage


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _brief(**kw) -> Brief:
    kw.setdefault("target_duration_s", 30.0)
    return Brief(**kw)


def _seg(n: int, clip: str, in_: float, out: float, **kw) -> Segment:
    kw.setdefault("reason", f"reason for segment {n}")
    return Segment(id=f"seg_{n:02d}", source_clip=clip, out=out, **{"in": in_}, **kw)


def _edl(segments: list[Segment], **brief_kw) -> EDL:
    return EDL(project_id="p1", brief=_brief(**brief_kw), timeline=segments)


def _ts(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start_s=start, end_s=end, text=text)


def _hl(start: float, end: float, score: float, text: str = "") -> Highlight:
    return Highlight(start_s=start, end_s=end, score=score, text=text)


def _manifest(clip_id: str = "c1", transcript=None, highlights=None) -> ClipManifest:
    return ClipManifest(
        clip_id=clip_id,
        source_path="/dev/null",
        transcript=transcript or [],
        highlights=highlights or [],
    )


def _two_seg_edl(**brief_kw) -> EDL:
    # seg_01: source [0,4) -> timeline [0,4); seg_02: source [10,14) -> timeline [4,8).
    return _edl([_seg(1, "c1", 0.0, 4.0), _seg(2, "c1", 10.0, 14.0)], **brief_kw)


class FakeLLM:
    """Stand-in for LLMClient: fixed reply or a raise, plus call capture."""

    def __init__(self, response: dict | None = None, raises: bool = False):
        self.available = True
        self.response = response or {}
        self.raises = raises
        self.calls: list[dict] = []

    def complete_json(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("boom")
        return self.response


def _settings(tmp_path: Path) -> Settings:
    return Settings(ave_data_dir=tmp_path, anthropic_api_key="")


# --------------------------------------------------------------------------- #
# gen_titles                                                                   #
# --------------------------------------------------------------------------- #
def test_titles_count_distinct_and_length_cap():
    long_text = "an incredibly detailed walkthrough of the entire agentic pipeline " * 4
    m = _manifest(highlights=[_hl(0.0, 2.0, 0.9, long_text)])
    titles = gen_titles(_two_seg_edl(), [m])
    assert len(titles) == 3
    assert len(set(titles)) == 3
    assert all(len(t) <= 90 for t in titles)


def test_titles_derived_from_strongest_highlight():
    m = _manifest(
        highlights=[
            _hl(0.0, 2.0, 0.3, "a weaker moment"),
            _hl(2.0, 4.0, 0.9, "editing with agents is wild"),
        ]
    )
    titles = gen_titles(_two_seg_edl(), [m])
    assert titles[0] == "Editing with agents is wild"
    assert titles[1].startswith("How ")
    assert "editing with agents is wild" in titles[1].lower()
    assert "[" in titles[2] and "]" in titles[2]


def test_titles_no_transcript_generic_but_tone_flavored():
    titles = gen_titles(_two_seg_edl(tone=Tone.cinematic), [_manifest()])
    assert len(titles) == 3 and len(set(titles)) == 3
    assert any("cinematic" in t for t in titles)
    assert all(len(t) <= 90 for t in titles)


def test_titles_deterministic():
    m = _manifest(highlights=[_hl(0.0, 2.0, 0.9, "same seed every time")])
    assert gen_titles(_two_seg_edl(), [m]) == gen_titles(_two_seg_edl(), [m])
    assert gen_titles(_two_seg_edl(), []) == gen_titles(_two_seg_edl(), [])


# --------------------------------------------------------------------------- #
# gen_chapters                                                                 #
# --------------------------------------------------------------------------- #
def test_chapters_zero_first_and_per_segment_offsets():
    m = _manifest(
        transcript=[
            _ts(0.0, 4.0, "welcome to the deep dive on editing agents"),
            _ts(10.0, 14.0, "now the render pipeline kicks in fast"),
        ]
    )
    chapters = gen_chapters(_two_seg_edl(), [m])
    assert chapters[0]["time_s"] == 0.0
    assert [c["time_s"] for c in chapters] == [0.0, 4.0]
    # Labels: first ~6 words of each segment's transcript slice.
    assert chapters[0]["label"] == "welcome to the deep dive on"
    assert chapters[1]["label"] == "now the render pipeline kicks in"


def test_chapters_reason_prefix_when_no_transcript():
    edl = _edl([_seg(1, "c1", 0.0, 4.0, reason="hook opening scene of the launch day vlog")])
    chapters = gen_chapters(edl, [])
    assert chapters == [{"time_s": 0.0, "label": "hook opening scene of the launch"}]


def test_chapters_merge_consecutive_duplicate_labels():
    m = _manifest(transcript=[_ts(0.0, 14.0, "one long take covering everything")])
    chapters = gen_chapters(_two_seg_edl(), [m])
    assert len(chapters) == 1
    assert chapters[0] == {"time_s": 0.0, "label": "one long take covering everything"}


def test_chapters_empty_timeline_still_has_zero_chapter():
    chapters = gen_chapters(_edl([]), [])
    assert chapters == [{"time_s": 0.0, "label": "Intro"}]


def test_chapters_respect_speed_in_offsets():
    edl = _edl([_seg(1, "c1", 0.0, 4.0, speed=2.0), _seg(2, "c1", 10.0, 14.0)])
    chapters = gen_chapters(edl, [])
    assert [c["time_s"] for c in chapters] == [0.0, 2.0]  # 4s source at 2x -> 2s timeline


# --------------------------------------------------------------------------- #
# gen_description                                                              #
# --------------------------------------------------------------------------- #
def test_description_summary_from_top_highlight_then_chapters():
    m = _manifest(highlights=[_hl(0.0, 2.0, 1.0, "the big reveal works")])
    desc = gen_description(_two_seg_edl(), [m])
    summary, rest = desc.split("\n\n", 1)
    assert "The big reveal works" in summary
    assert rest.startswith("Chapters:")
    assert "00:00" in rest and "00:04" in rest


def test_description_mmss_zero_padded_past_a_minute():
    # seg_01 occupies [0, 65) so seg_02 starts at 01:05.
    edl = _edl([_seg(1, "c1", 0.0, 65.0), _seg(2, "c1", 70.0, 80.0)])
    desc = gen_description(edl, [])
    assert "00:00" in desc and "01:05" in desc


def test_description_boilerplate_without_highlights():
    desc = gen_description(_two_seg_edl(tone=Tone.tutorial, platform=Platform.youtube), [])
    assert "tutorial" in desc and "youtube" in desc
    assert "\n\nChapters:\n" in desc


# --------------------------------------------------------------------------- #
# gen_hashtags                                                                 #
# --------------------------------------------------------------------------- #
def test_hashtags_platform_and_tone_base_tags():
    tags = gen_hashtags(_two_seg_edl(platform=Platform.shorts, tone=Tone.energetic), [])
    assert tags[0] == "#shorts"
    assert "#energetic" in tags
    assert all(t.startswith("#") for t in tags)


def test_hashtags_keywords_by_frequency_stopwords_and_short_words_dropped():
    m = _manifest(
        transcript=[
            _ts(0.0, 5.0, "python python python editor editor render this that with cat dog")
        ]
    )
    tags = gen_hashtags(_two_seg_edl(platform=Platform.youtube), [m])
    kw = [t for t in tags if t not in ("#youtube", "#energetic")]
    assert kw == ["#python", "#editor", "#render"]  # frequency order
    assert "#this" not in tags and "#that" not in tags and "#with" not in tags  # stopwords
    assert "#cat" not in tags and "#dog" not in tags  # length < 4


def test_hashtags_frequency_ties_break_alphabetically():
    m = _manifest(transcript=[_ts(0.0, 5.0, "zebra apple mango")])
    tags = gen_hashtags(_two_seg_edl(platform=Platform.youtube), [m])
    kw = [t for t in tags if t not in ("#youtube", "#energetic")]
    assert kw == ["#apple", "#mango", "#zebra"]


def test_hashtags_dedup_and_cap():
    words = " ".join(f"word{i:02d}" for i in range(20))
    m = _manifest(transcript=[_ts(0.0, 5.0, "energetic energetic energetic " + words)])
    tags = gen_hashtags(_two_seg_edl(tone=Tone.energetic), [m], max_tags=6)
    assert len(tags) == 6
    assert tags.count("#energetic") == 1  # tone tag deduped against transcript keyword
    assert len(set(tags)) == 6


def test_hashtags_deterministic():
    m = _manifest(transcript=[_ts(0.0, 5.0, "alpha beta beta gamma gamma gamma")])
    runs = {tuple(gen_hashtags(_two_seg_edl(), [m])) for _ in range(5)}
    assert len(runs) == 1


# --------------------------------------------------------------------------- #
# thumbnail_candidates                                                         #
# --------------------------------------------------------------------------- #
def test_thumbnails_top_k_score_then_clip_id_order():
    edl = _edl(
        [_seg(1, "c1", 0.0, 4.0), _seg(2, "c2", 0.0, 4.0), _seg(3, "c1", 10.0, 14.0)]
    )
    m1 = _manifest("c1", highlights=[_hl(1.0, 2.0, 0.9, "peak"), _hl(11.0, 12.0, 0.5)])
    m2 = _manifest("c2", highlights=[_hl(1.0, 3.0, 0.9, "also peak")])
    thumbs = thumbnail_candidates(edl, [m1, m2], k=2)
    assert len(thumbs) == 2
    # Score ties at 0.9 break on clip_id: c1 before c2; the 0.5 highlight is cut by k.
    assert [(t["clip_id"], t["score"]) for t in thumbs] == [("c1", 0.9), ("c2", 0.9)]
    assert thumbs[0]["reason"] == "peak"


def test_thumbnails_remap_math_with_speed():
    edl = _edl([_seg(1, "c1", 0.0, 4.0), _seg(2, "c1", 10.0, 14.0, speed=2.0)])
    m = _manifest("c1", highlights=[_hl(11.0, 13.0, 0.8)])  # midpoint 12.0 in seg_02
    (t,) = thumbnail_candidates(edl, [m], k=3)
    assert t["source_time_s"] == pytest.approx(12.0)
    # offset_of(seg_02)=4.0, plus (12-10)/2.0 = 1.0 -> 5.0 on the timeline.
    assert t["timeline_s"] == pytest.approx(5.0)
    assert t["reason"] == "high-energy moment"  # no text on the highlight


def test_thumbnails_filter_out_of_timeline_highlights():
    edl = _edl([_seg(1, "c1", 0.0, 4.0)])
    m = _manifest(
        "c1",
        highlights=[_hl(50.0, 52.0, 5.0, "outside"), _hl(1.0, 2.0, 0.2, "inside")],
    )
    thumbs = thumbnail_candidates(edl, [m], k=3)
    assert [t["reason"] for t in thumbs] == ["inside"]  # high scorer outside is dropped


def test_thumbnails_wrong_clip_highlight_not_placed():
    edl = _edl([_seg(1, "c1", 0.0, 4.0)])
    m = _manifest("c2", highlights=[_hl(1.0, 2.0, 0.9)])  # c2 never on the timeline
    thumbs = thumbnail_candidates(edl, [m], k=3)
    assert all(t["reason"] == "segment midpoint fallback" for t in thumbs)


def test_thumbnails_fewer_than_k_returns_what_exists():
    edl = _two_seg_edl()
    m = _manifest("c1", highlights=[_hl(1.0, 2.0, 0.9, "only one")])
    assert len(thumbnail_candidates(edl, [m], k=3)) == 1


def test_thumbnails_midpoint_fallback_and_k_bound():
    edl = _two_seg_edl()
    thumbs = thumbnail_candidates(edl, [_manifest("c1")], k=5)
    assert len(thumbs) == 2  # k > available segments
    assert thumbs[0] == {
        "clip_id": "c1",
        "source_time_s": 2.0,
        "timeline_s": 2.0,
        "score": 0.0,
        "reason": "segment midpoint fallback",
    }
    # seg_02 midpoint: source 12.0 -> timeline 4.0 + 2.0.
    assert thumbs[1]["source_time_s"] == pytest.approx(12.0)
    assert thumbs[1]["timeline_s"] == pytest.approx(6.0)


def test_thumbnails_deterministic():
    edl = _two_seg_edl()
    m = _manifest("c1", highlights=[_hl(1.0, 2.0, 0.9, "a"), _hl(11.0, 12.0, 0.9, "b")])
    assert thumbnail_candidates(edl, [m]) == thumbnail_candidates(edl, [m])


# --------------------------------------------------------------------------- #
# build_release_kit                                                            #
# --------------------------------------------------------------------------- #
def _kit_inputs(tmp_path: Path):
    edl = _two_seg_edl()
    m = _manifest(
        "c1",
        transcript=[_ts(0.0, 4.0, "welcome to the pipeline deep dive")],
        highlights=[_hl(1.0, 2.0, 0.9, "the pipeline deep dive")],
    )
    return edl, [m], LocalStorage(tmp_path)


def test_build_release_kit_llm_none_is_deterministic_base(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    kit = build_release_kit(edl, manifests, "p1", storage, settings=_settings(tmp_path))
    assert isinstance(kit, ReleaseKit)
    assert kit.titles == gen_titles(edl, manifests)
    assert kit.description == gen_description(edl, manifests)
    assert kit.hashtags == gen_hashtags(edl, manifests)
    assert kit.chapters == gen_chapters(edl, manifests)
    assert kit.thumbnails == thumbnail_candidates(edl, manifests)


def test_build_release_kit_writes_versioned_report(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    edl = edl.bump("v2 for release")
    kit = build_release_kit(edl, manifests, "p1", storage, settings=_settings(tmp_path))
    on_disk = storage.read_json("p1", "release/kit_v2.json")
    assert on_disk == kit.model_dump()
    assert (tmp_path / "projects" / "p1" / "release" / "kit_v2.json").exists()


def test_build_release_kit_llm_success_replaces_copy_fields(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    llm = FakeLLM(
        response={
            "titles": ["One", "Two", "Three"],
            "description": "A short honest description.",
            "hashtags": ["#editing", "video", "#editing"],
        }
    )
    kit = build_release_kit(edl, manifests, "p1", storage, llm=llm)
    assert kit.titles == ["One", "Two", "Three"]
    assert kit.description == "A short honest description."
    assert kit.hashtags == ["#editing", "#video"]  # normalized + deduped
    # Thumbnails and chapters stay deterministic regardless of the LLM.
    assert kit.chapters == gen_chapters(edl, manifests)
    assert kit.thumbnails == thumbnail_candidates(edl, manifests)
    assert llm.calls[0]["agent"] == "release"
    assert "seed_titles" in llm.calls[0]["user"]


def test_build_release_kit_wrong_title_count_keeps_deterministic(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    llm = FakeLLM(
        response={
            "titles": ["Only", "  "],  # one blank -> not exactly 3 non-empty
            "description": "Still valid.",
            "hashtags": ["#ok"],
        }
    )
    kit = build_release_kit(edl, manifests, "p1", storage, llm=llm)
    assert kit.titles == gen_titles(edl, manifests)  # titles rejected
    assert kit.description == "Still valid."  # other fields still accepted
    assert kit.hashtags == ["#ok"]


def test_build_release_kit_llm_hashtags_capped_at_ten(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    llm = FakeLLM(
        response={
            "titles": ["One", "Two", "Three"],
            "description": "d",
            "hashtags": [f"tag{i:02d}" for i in range(15)],
        }
    )
    kit = build_release_kit(edl, manifests, "p1", storage, llm=llm)
    assert len(kit.hashtags) == 10
    assert kit.hashtags[0] == "#tag00"


def test_build_release_kit_llm_exception_falls_back(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    kit = build_release_kit(edl, manifests, "p1", storage, llm=FakeLLM(raises=True))
    assert kit.titles == gen_titles(edl, manifests)
    assert kit.description == gen_description(edl, manifests)
    assert kit.hashtags == gen_hashtags(edl, manifests)
    # The kit is still persisted even when the LLM path blew up.
    assert storage.read_json("p1", "release/kit_v1.json") == kit.model_dump()


def test_build_release_kit_deterministic_across_runs(tmp_path: Path):
    edl, manifests, storage = _kit_inputs(tmp_path)
    a = build_release_kit(edl, manifests, "p1", storage, settings=_settings(tmp_path))
    b = build_release_kit(edl, manifests, "p1", storage, settings=_settings(tmp_path))
    assert a.model_dump() == b.model_dump()
