"""Tests for the captions module: cue remapping, grouping, writers, and the agent."""

from __future__ import annotations

import re

import pytest

from ave.agents.captions import generate_captions
from ave.analysis.manifest import ClipManifest, TranscriptSegment, Word
from ave.captions.cues import Cue, CueWord, build_cues, mode_for_style
from ave.captions.writers import (
    STYLE_PRESETS,
    format_ass_time,
    format_srt_time,
    to_ass,
    to_srt,
    to_vtt,
)
from ave.edl.schema import (
    EDL,
    AspectRatio,
    Brief,
    Captions,
    CaptionStyle,
    OutputSpec,
    Segment,
)
from ave.storage.store import LocalStorage


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
def make_word(word: str, start: float, end: float, filler: bool = False) -> Word:
    return Word(word=word, start_s=start, end_s=end, is_filler=filler)


def make_manifest(clip_id: str = "clip_a") -> ClipManifest:
    """Two transcript sentences; one filler word in the first."""
    return ClipManifest(
        clip_id=clip_id,
        source_path=f"/media/{clip_id}.mp4",
        transcript=[
            TranscriptSegment(
                start_s=0.0,
                end_s=2.5,
                text="hello um brave new world",
                words=[
                    make_word("hello", 0.0, 0.4),
                    make_word("um", 0.5, 0.7, filler=True),
                    make_word("brave", 0.8, 1.2),
                    make_word("new", 1.3, 1.6),
                    make_word("world", 1.7, 2.1),
                ],
            ),
            TranscriptSegment(
                start_s=3.0,
                end_s=4.6,
                text="second sentence here",
                words=[
                    make_word("second", 3.0, 3.4),
                    make_word("sentence", 3.5, 4.0),
                    make_word("here", 4.1, 4.5),
                ],
            ),
        ],
    )


def make_segment(
    seg_id: str = "seg_1",
    clip: str = "clip_a",
    in_: float = 0.0,
    out: float = 5.0,
    speed: float = 1.0,
) -> Segment:
    return Segment(id=seg_id, source_clip=clip, in_=in_, out=out, speed=speed, reason="test")


def make_edl(
    segments: list[Segment],
    captions: Captions | None = None,
    output: OutputSpec | None = None,
) -> EDL:
    return EDL(
        project_id="proj_test",
        brief=Brief(target_duration_s=30.0),
        timeline=segments,
        captions=captions or Captions(),
        output=output or OutputSpec(),
    )


# --------------------------------------------------------------------------- #
# Remap math                                                                  #
# --------------------------------------------------------------------------- #
def test_remap_identity_at_speed_1_offset_0():
    edl = make_edl([make_segment(in_=0.0, out=5.0)])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    words = cues[0].words
    assert words[0].word == "hello"
    assert words[0].start_s == pytest.approx(0.0)
    assert words[0].end_s == pytest.approx(0.4)


def test_remap_subtracts_in_point():
    # in=1.0: "brave" (0.8-1.2, mid 1.0) is included and shifted left by 1.0.
    edl = make_edl([make_segment(in_=1.0, out=5.0)])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    brave = cues[0].words[0]
    assert brave.word == "brave"
    # source start 0.8 is before in=1.0 -> clamped to the segment start (offset 0).
    assert brave.start_s == pytest.approx(0.0)
    assert brave.end_s == pytest.approx(0.2)
    new = cues[0].words[1]
    assert new.start_s == pytest.approx(0.3)
    assert new.end_s == pytest.approx(0.6)


def test_remap_speed_rescales_times():
    edl = make_edl([make_segment(in_=1.0, out=3.0, speed=2.0)])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    words = {w.word: w for w in cues[0].words}
    # "new": (1.3-1.0)/2 = 0.15 .. (1.6-1.0)/2 = 0.3
    assert words["new"].start_s == pytest.approx(0.15)
    assert words["new"].end_s == pytest.approx(0.3)


def test_remap_clamps_to_segment_bounds():
    manifest = ClipManifest(
        clip_id="clip_a",
        source_path="/media/clip_a.mp4",
        transcript=[
            TranscriptSegment(
                start_s=0.0,
                end_s=4.0,
                text="early edge",
                words=[
                    make_word("early", 0.8, 1.2),  # start before in=1.0 -> clamp low
                    make_word("edge", 2.8, 3.2),  # end after out=3.0 -> clamp high
                ],
            )
        ],
    )
    edl = make_edl([make_segment(in_=1.0, out=3.0, speed=2.0)])
    cues = build_cues(edl, [manifest], mode="sentence")
    words = cues[0].words
    duration = (3.0 - 1.0) / 2.0  # 1.0s on the timeline
    assert words[0].start_s == pytest.approx(0.0)  # clamped at segment start
    assert words[1].end_s == pytest.approx(duration)  # clamped at segment end


def test_second_segment_words_get_timeline_offset():
    seg1 = make_segment("seg_1", in_=0.0, out=2.5)  # timeline 0.0-2.5
    seg2 = make_segment("seg_2", in_=3.0, out=5.0)  # timeline 2.5-4.5
    edl = make_edl([seg1, seg2])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    # seg2's "second" starts at source 3.0 -> timeline 2.5 + (3.0 - 3.0) = 2.5
    second = [c for c in cues if "second" in c.text][-1]
    assert second.start_s == pytest.approx(2.5)
    assert second.end_s == pytest.approx(2.5 + (4.5 - 3.0))


def test_filler_words_skipped():
    edl = make_edl([make_segment()])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    all_text = " ".join(c.text for c in cues)
    assert "um" not in all_text.split()


def test_words_outside_in_out_excluded():
    # out=1.0: only words with midpoint <= 1.0 survive ("hello" mid 0.2, "brave" mid 1.0).
    edl = make_edl([make_segment(in_=0.0, out=1.0)])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    words = [w.word for c in cues for w in c.words]
    assert words == ["hello", "brave"]


def test_empty_transcript_gives_no_cues():
    manifest = ClipManifest(clip_id="clip_a", source_path="/x.mp4", transcript=[])
    edl = make_edl([make_segment()])
    assert build_cues(edl, [manifest]) == []


def test_missing_manifest_gives_no_cues():
    edl = make_edl([make_segment(clip="clip_unknown")])
    assert build_cues(edl, [make_manifest("clip_a")]) == []


# --------------------------------------------------------------------------- #
# Grouping                                                                    #
# --------------------------------------------------------------------------- #
def test_sentence_mode_one_cue_per_transcript_segment():
    edl = make_edl([make_segment()])
    cues = build_cues(edl, [make_manifest()], mode="sentence")
    assert len(cues) == 2
    assert cues[0].text == "hello brave new world"
    assert cues[1].text == "second sentence here"


def test_phrase_mode_word_count_limit():
    # 8 tightly-packed words -> groups of 5 then 3 (span limit never binds).
    words = [make_word(f"w{i}", 0.3 * i, 0.3 * i + 0.2) for i in range(8)]
    manifest = ClipManifest(
        clip_id="clip_a",
        source_path="/x.mp4",
        transcript=[TranscriptSegment(start_s=0.0, end_s=3.0, text="", words=words)],
    )
    edl = make_edl([make_segment(out=10.0)])
    cues = build_cues(edl, [manifest], mode="phrase")
    assert [len(c.words) for c in cues] == [5, 3]


def test_phrase_mode_span_limit():
    # Third word would stretch the group past 2.2s -> new group.
    words = [
        make_word("a", 0.0, 0.4),
        make_word("b", 1.0, 1.5),
        make_word("c", 2.5, 2.9),
    ]
    manifest = ClipManifest(
        clip_id="clip_a",
        source_path="/x.mp4",
        transcript=[TranscriptSegment(start_s=0.0, end_s=3.0, text="", words=words)],
    )
    edl = make_edl([make_segment(out=10.0)])
    cues = build_cues(edl, [manifest], mode="phrase")
    assert [c.text for c in cues] == ["a b", "c"]


def test_cues_sorted_by_start():
    seg1 = make_segment("seg_1", in_=0.0, out=2.5)
    seg2 = make_segment("seg_2", in_=3.0, out=5.0)
    edl = make_edl([seg1, seg2])
    for mode in ("sentence", "phrase"):
        cues = build_cues(edl, [make_manifest()], mode=mode)
        starts = [c.start_s for c in cues]
        assert starts == sorted(starts)


def test_mode_for_style():
    assert mode_for_style(CaptionStyle.karaoke_bold) == "phrase"
    assert mode_for_style(CaptionStyle.phrase_pop) == "phrase"
    assert mode_for_style(CaptionStyle.clean_subtitle) == "sentence"
    assert mode_for_style(CaptionStyle.none) == "sentence"


# --------------------------------------------------------------------------- #
# SRT / VTT writers (golden strings)                                          #
# --------------------------------------------------------------------------- #
GOLDEN_CUES = [
    Cue(start_s=0.0, end_s=1.5, text="Hello world"),
    Cue(start_s=2.0, end_s=3.25, text="Second cue"),
]


def test_to_srt_golden():
    expected = (
        "1\n"
        "00:00:00,000 --> 00:00:01,500\n"
        "Hello world\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:03,250\n"
        "Second cue\n"
    )
    assert to_srt(GOLDEN_CUES) == expected


def test_to_vtt_golden():
    expected = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:01.500\n"
        "Hello world\n"
        "\n"
        "00:00:02.000 --> 00:00:03.250\n"
        "Second cue\n"
    )
    assert to_vtt(GOLDEN_CUES) == expected


def test_empty_cue_lists():
    assert to_srt([]) == ""
    assert to_vtt([]) == "WEBVTT\n\n"


# --------------------------------------------------------------------------- #
# Time formatting                                                             #
# --------------------------------------------------------------------------- #
def test_format_srt_time_edges():
    assert format_srt_time(0.0) == "00:00:00,000"
    assert format_srt_time(59.999) == "00:00:59,999"
    assert format_srt_time(3661.25) == "01:01:01,250"
    assert format_srt_time(3600.0) == "01:00:00,000"


def test_format_ass_time_edges():
    assert format_ass_time(0.0) == "0:00:00.00"
    assert format_ass_time(59.99) == "0:00:59.99"
    assert format_ass_time(59.999) == "0:01:00.00"  # rounds up to the next centisecond
    assert format_ass_time(3661.25) == "1:01:01.25"


# --------------------------------------------------------------------------- #
# ASS writer                                                                  #
# --------------------------------------------------------------------------- #
def _margin_v_of(ass: str) -> int:
    style_line = next(l for l in ass.splitlines() if l.startswith("Style: "))
    return int(style_line.split(",")[-2])


def test_to_ass_playres_from_output():
    ass = to_ass(GOLDEN_CUES, Captions(), OutputSpec(width=1920, height=1080))
    assert "PlayResX: 1920" in ass
    assert "PlayResY: 1080" in ass


def test_to_ass_honours_font_overrides():
    captions = Captions(style=CaptionStyle.clean_subtitle, font="Comic Sans MS", font_size=72)
    ass = to_ass(GOLDEN_CUES, captions, OutputSpec())
    assert "Style: Default,Comic Sans MS,72," in ass


def test_to_ass_karaoke_tags():
    cue = Cue(
        start_s=0.0,
        end_s=1.0,
        text="Hello hi",
        words=[
            CueWord(word="Hello", start_s=0.0, end_s=0.5),
            CueWord(word="hi", start_s=0.5, end_s=0.504),  # sub-centisecond -> min 1
        ],
    )
    ass = to_ass([cue], Captions(style=CaptionStyle.karaoke_bold), OutputSpec())
    assert "{\\k50}Hello" in ass
    assert "{\\k1}hi" in ass


def test_to_ass_plain_text_for_non_karaoke():
    ass = to_ass(GOLDEN_CUES, Captions(style=CaptionStyle.phrase_pop), OutputSpec())
    assert "\\k" not in ass
    assert "Hello world" in ass


def test_to_ass_safe_zone_9_16():
    output = OutputSpec(aspect_ratio=AspectRatio.vertical, width=1080, height=1920)
    # position_y=0.99 requests a margin of only ~19px; safe zone must floor it.
    captions = Captions(style=CaptionStyle.karaoke_bold, position_y=0.99)
    ass = to_ass(GOLDEN_CUES, captions, output)
    assert _margin_v_of(ass) >= 0.12 * 1920


def test_to_ass_safe_zone_1_1():
    output = OutputSpec(aspect_ratio=AspectRatio.square, width=1080, height=1080)
    captions = Captions(position_y=1.0)
    ass = to_ass(GOLDEN_CUES, captions, output)
    assert _margin_v_of(ass) >= 0.12 * 1080


def test_to_ass_safe_zone_16_9():
    output = OutputSpec(aspect_ratio=AspectRatio.wide, width=1920, height=1080)
    captions = Captions(position_y=1.0)
    ass = to_ass(GOLDEN_CUES, captions, output)
    assert _margin_v_of(ass) >= 0.05 * 1080


def test_to_ass_position_y_beats_safe_zone_when_higher():
    output = OutputSpec(aspect_ratio=AspectRatio.wide, width=1920, height=1080)
    captions = Captions(position_y=0.5)  # requests 540px, far above the 5% floor
    ass = to_ass(GOLDEN_CUES, captions, output)
    assert _margin_v_of(ass) == 540


def test_to_ass_dialogue_times_and_structure():
    ass = to_ass(GOLDEN_CUES, Captions(), OutputSpec())
    assert "[Script Info]" in ass
    assert "[V4+ Styles]" in ass
    assert "[Events]" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,Hello world" in ass


def test_style_presets_complete():
    keys = {"fontname", "fontsize", "primary_colour", "outline_colour", "outline", "bold",
            "alignment"}
    for style in CaptionStyle:
        preset = STYLE_PRESETS[style.value]
        assert keys <= set(preset)
        assert re.fullmatch(r"&H[0-9A-F]{8}&", preset["primary_colour"])
        assert preset["alignment"] == 2


# --------------------------------------------------------------------------- #
# Captions agent                                                              #
# --------------------------------------------------------------------------- #
def test_generate_captions_disabled(tmp_path):
    storage = LocalStorage(tmp_path)
    edl = make_edl([make_segment()], captions=Captions(style=CaptionStyle.none))
    result = generate_captions(edl, [make_manifest()], "proj_test", storage)
    assert result == {
        "cue_count": 0,
        "srt": None,
        "vtt": None,
        "ass": None,
        "note": "captions disabled",
    }


def test_generate_captions_no_transcript(tmp_path):
    storage = LocalStorage(tmp_path)
    manifest = ClipManifest(clip_id="clip_a", source_path="/x.mp4", transcript=[])
    edl = make_edl([make_segment()])
    result = generate_captions(edl, [manifest], "proj_test", storage)
    assert result["cue_count"] == 0
    assert result["note"] == "no transcript"
    assert result["srt"] is None and result["vtt"] is None and result["ass"] is None


def test_generate_captions_happy_path(tmp_path):
    storage = LocalStorage(tmp_path)
    edl = make_edl([make_segment()], captions=Captions(style=CaptionStyle.karaoke_bold))
    result = generate_captions(edl, [make_manifest()], "proj_test", storage)
    assert result["note"] == "ok"
    assert result["cue_count"] > 0
    for key, head in (("srt", "1\n"), ("vtt", "WEBVTT"), ("ass", "[Script Info]")):
        path = tmp_path / "projects" / "proj_test" / "captions" / f"subs.{key}"
        assert result[key] == str(path)
        assert path.read_text().startswith(head)
    # karaoke style -> ASS contains per-word timing tags
    assert "{\\k" in (tmp_path / "projects" / "proj_test" / "captions" / "subs.ass").read_text()
