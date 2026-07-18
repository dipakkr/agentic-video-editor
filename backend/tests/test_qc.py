"""Tests for the QC module: individual checks, aggregation, and the QC agent."""

from __future__ import annotations

import json

from ave.agents.qc import run_qc
from ave.analysis.manifest import ClipManifest, ProbeInfo, TranscriptSegment, Word
from ave.config import Settings
from ave.edl.schema import EDL, Brief, Captions, Segment
from ave.qc.checks import (
    CheckResult,
    QCReport,
    check_beat_alignment,
    check_caption_alignment,
    check_duplicate_usage,
    check_duration,
    check_loudness,
    check_segment_bounds,
    run_all,
)
from ave.storage.store import LocalStorage


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #
def make_segment(
    seg_id: str = "seg_1",
    clip: str = "clip_a",
    in_: float = 0.0,
    out: float = 5.0,
    speed: float = 1.0,
    snapped: float | None = None,
) -> Segment:
    return Segment(
        id=seg_id,
        source_clip=clip,
        in_=in_,
        out=out,
        speed=speed,
        cut_snapped_to_beat=snapped is not None,
        snapped_beat_s=snapped,
        reason="test",
    )


def make_edl(
    segments: list[Segment],
    target: float = 30.0,
    tolerance_pct: float = 10.0,
    captions: Captions | None = None,
) -> EDL:
    return EDL(
        project_id="proj_qc",
        brief=Brief(target_duration_s=target, duration_tolerance_pct=tolerance_pct),
        timeline=segments,
        captions=captions or Captions(),
    )


def make_word(word: str, start: float, end: float) -> Word:
    return Word(word=word, start_s=start, end_s=end)


def make_manifest(clip_id: str = "clip_a", duration: float = 60.0) -> ClipManifest:
    """A probed clip with one clean transcript sentence."""
    return ClipManifest(
        clip_id=clip_id,
        source_path=f"/media/{clip_id}.mp4",
        probe=ProbeInfo(duration_s=duration),
        transcript=[
            TranscriptSegment(
                start_s=0.0,
                end_s=2.5,
                text="hello brave new world",
                words=[
                    make_word("hello", 0.0, 0.4),
                    make_word("brave", 0.8, 1.2),
                    make_word("new", 1.3, 1.6),
                    make_word("world", 1.7, 2.1),
                ],
            ),
        ],
    )


def make_settings(**overrides) -> Settings:
    defaults = {"ave_beat_snap_tolerance_ms": 180, "ave_target_lufs": -14.0}
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


# --------------------------------------------------------------------------- #
# check_duration                                                              #
# --------------------------------------------------------------------------- #
def test_duration_within_tolerance_passes():
    edl = make_edl([make_segment(out=29.0)], target=30.0, tolerance_pct=10.0)
    res = check_duration(edl)
    assert res.passed
    assert res.responsible_agent == "editorial"
    assert "29.000" in res.details and "30.000" in res.details


def test_duration_outside_tolerance_fails():
    edl = make_edl([make_segment(out=10.0)], target=30.0, tolerance_pct=10.0)
    res = check_duration(edl)
    assert not res.passed
    assert "3.000" in res.details  # the ± tolerance is spelled out


# --------------------------------------------------------------------------- #
# check_segment_bounds                                                        #
# --------------------------------------------------------------------------- #
def test_bounds_within_clip_passes():
    edl = make_edl([make_segment(in_=1.0, out=59.0)])
    res = check_segment_bounds(edl, [make_manifest(duration=60.0)])
    assert res.passed
    assert res.responsible_agent == "editorial"


def test_bounds_out_past_duration_fails_and_names_segment():
    edl = make_edl([make_segment(seg_id="seg_bad", in_=1.0, out=70.0)])
    res = check_segment_bounds(edl, [make_manifest(duration=60.0)])
    assert not res.passed
    assert "seg_bad" in res.details


def test_bounds_unknown_clip_fails():
    edl = make_edl([make_segment(seg_id="seg_ghost", clip="clip_missing")])
    res = check_segment_bounds(edl, [make_manifest()])
    assert not res.passed
    assert "seg_ghost" in res.details and "clip_missing" in res.details


def test_bounds_unmeasured_clip_skipped_but_noted():
    edl = make_edl([make_segment(seg_id="seg_unprobed", out=999.0)])
    res = check_segment_bounds(edl, [make_manifest(duration=0.0)])
    assert res.passed
    assert "unmeasured" in res.details and "seg_unprobed" in res.details


def test_bounds_mixed_violation_and_unmeasured():
    edl = make_edl(
        [
            make_segment(seg_id="seg_ok", clip="clip_a", out=10.0),
            make_segment(seg_id="seg_bad", clip="clip_a", in_=50.0, out=80.0),
            make_segment(seg_id="seg_unprobed", clip="clip_b", out=500.0),
        ]
    )
    res = check_segment_bounds(
        edl, [make_manifest("clip_a", 60.0), make_manifest("clip_b", 0.0)]
    )
    assert not res.passed
    assert "seg_bad" in res.details
    assert "seg_ok" not in res.details.split("skipped")[0]


# --------------------------------------------------------------------------- #
# check_duplicate_usage                                                       #
# --------------------------------------------------------------------------- #
def test_duplicate_overlap_over_half_second_fails():
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=5.0),
            make_segment("seg_2", in_=4.0, out=8.0),  # 1.0s overlap
        ]
    )
    res = check_duplicate_usage(edl)
    assert not res.passed
    assert "seg_1+seg_2" in res.details
    assert res.responsible_agent == "editorial"


def test_duplicate_overlap_under_half_second_passes():
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=5.0),
            make_segment("seg_2", in_=4.6, out=8.0),  # 0.4s overlap
        ]
    )
    assert check_duplicate_usage(edl).passed


def test_duplicate_same_window_different_clips_passes():
    edl = make_edl(
        [
            make_segment("seg_1", clip="clip_a", in_=0.0, out=5.0),
            make_segment("seg_2", clip="clip_b", in_=0.0, out=5.0),
        ]
    )
    assert check_duplicate_usage(edl).passed


# --------------------------------------------------------------------------- #
# check_beat_alignment                                                        #
# --------------------------------------------------------------------------- #
def test_beat_alignment_no_snapped_cuts_passes():
    edl = make_edl([make_segment()])
    res = check_beat_alignment(edl, tolerance_ms=180)
    assert res.passed
    assert res.details == "no snapped cuts"
    assert res.responsible_agent == "music"


def test_beat_alignment_within_tolerance_passes():
    # seg_1 occupies 0..2s, so seg_2 starts at timeline 2.0; beat at 2.1 -> 100ms drift.
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=2.0),
            make_segment("seg_2", in_=10.0, out=12.0, snapped=2.1),
        ]
    )
    assert check_beat_alignment(edl, tolerance_ms=180).passed


def test_beat_alignment_outside_tolerance_fails():
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=2.0),
            make_segment("seg_2", in_=10.0, out=12.0, snapped=2.5),  # 500ms drift
        ]
    )
    res = check_beat_alignment(edl, tolerance_ms=180)
    assert not res.passed
    assert "seg_2" in res.details


# --------------------------------------------------------------------------- #
# check_caption_alignment                                                     #
# --------------------------------------------------------------------------- #
def test_caption_alignment_happy_path():
    edl = make_edl([make_segment(in_=0.0, out=5.0)])
    res = check_caption_alignment(edl, [make_manifest()])
    assert res.passed
    assert res.responsible_agent == "captions"
    assert "all match" in res.details


def test_caption_alignment_no_cues_passes():
    # Manifest with no transcript -> build_cues yields nothing.
    manifest = ClipManifest(
        clip_id="clip_a", source_path="/media/clip_a.mp4", probe=ProbeInfo(duration_s=60.0)
    )
    edl = make_edl([make_segment()])
    res = check_caption_alignment(edl, [manifest])
    assert res.passed
    assert res.details == "no captions to check"


def test_caption_alignment_transcript_mismatch_fails():
    # Corrupted transcript: word tokens don't appear in any segment's text, so the
    # rebuilt cue text can't be found in the transcript word set.
    manifest = make_manifest()
    manifest.transcript[0].words[1] = make_word("glorbulated", 0.8, 1.2)
    edl = make_edl([make_segment(in_=0.0, out=5.0)])
    res = check_caption_alignment(edl, [manifest])
    assert not res.passed
    assert "glorbulated" in res.details


def test_caption_alignment_ignores_case_and_punctuation():
    manifest = make_manifest()
    manifest.transcript[0].words[0] = make_word("Hello,", 0.0, 0.4)  # cue side
    manifest.transcript[0].text = "HELLO brave new world!"  # transcript side
    edl = make_edl([make_segment(in_=0.0, out=5.0)])
    assert check_caption_alignment(edl, [manifest]).passed


def test_caption_alignment_sampling_is_deterministic_and_bounded():
    # Many sentences -> more cues than sample_n; sampling must be stride-based.
    words = []
    transcript = []
    for i in range(12):
        w = make_word("hello", i * 1.0, i * 1.0 + 0.4)
        transcript.append(
            TranscriptSegment(start_s=i * 1.0, end_s=i * 1.0 + 0.5, text="hello", words=[w])
        )
        words.append(w)
    manifest = ClipManifest(
        clip_id="clip_a",
        source_path="/media/clip_a.mp4",
        probe=ProbeInfo(duration_s=60.0),
        transcript=transcript,
    )
    edl = make_edl([make_segment(in_=0.0, out=15.0)])
    first = check_caption_alignment(edl, [manifest], sample_n=5)
    second = check_caption_alignment(edl, [manifest], sample_n=5)
    assert first.passed
    assert first == second  # deterministic
    assert "5/12" in first.details


# --------------------------------------------------------------------------- #
# check_loudness                                                              #
# --------------------------------------------------------------------------- #
def test_loudness_none_stats_passes_as_unmeasured():
    res = check_loudness(None, target_lufs=-14.0)
    assert res.passed
    assert res.details == "not measured (ffmpeg unavailable)"
    assert res.responsible_agent == "render"


def test_loudness_within_tolerance_passes_string_value():
    # loudnorm prints JSON values as strings.
    assert check_loudness({"output_i": "-14.8"}, target_lufs=-14.0).passed


def test_loudness_outside_tolerance_fails():
    res = check_loudness({"output_i": -20.0}, target_lufs=-14.0)
    assert not res.passed
    assert "-20.00" in res.details


def test_loudness_falls_back_to_input_i():
    assert check_loudness({"input_i": "-13.0"}, target_lufs=-14.0).passed
    assert not check_loudness({"input_i": "-10.0"}, target_lufs=-14.0).passed


def test_loudness_missing_or_bad_keys_fail():
    assert not check_loudness({"input_tp": "-1.0"}, target_lufs=-14.0).passed
    assert not check_loudness({"output_i": "not-a-number"}, target_lufs=-14.0).passed


# --------------------------------------------------------------------------- #
# run_all aggregation                                                         #
# --------------------------------------------------------------------------- #
def _passing_setup() -> tuple[EDL, list[ClipManifest]]:
    edl = make_edl([make_segment(in_=0.0, out=30.0)], target=30.0)
    return edl, [make_manifest(duration=60.0)]


def test_run_all_all_green():
    edl, manifests = _passing_setup()
    report = run_all(edl, manifests, make_settings(), loudnorm_stats={"output_i": "-14.0"})
    assert isinstance(report, QCReport)
    assert report.passed
    assert report.failures_by_agent == {}
    assert len(report.results) == 6
    assert all(isinstance(r, CheckResult) for r in report.results)


def test_run_all_check_names_are_stable():
    edl, manifests = _passing_setup()
    report = run_all(edl, manifests, make_settings())
    assert [r.check for r in report.results] == [
        "duration",
        "segment_bounds",
        "duplicate_usage",
        "beat_alignment",
        "caption_alignment",
        "loudness",
    ]


def test_run_all_aggregates_failures_by_agent():
    # duration off (editorial), duplicate reuse (editorial), loudness off (render).
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=5.0),
            make_segment("seg_2", in_=4.0, out=9.0),
        ],
        target=60.0,
    )
    report = run_all(
        edl, [make_manifest(duration=60.0)], make_settings(), loudnorm_stats={"output_i": -30.0}
    )
    assert not report.passed
    assert report.failures_by_agent["editorial"] == ["duration", "duplicate_usage"]
    assert report.failures_by_agent["render"] == ["loudness"]
    assert "music" not in report.failures_by_agent
    assert "captions" not in report.failures_by_agent


def test_run_all_uses_settings_beat_tolerance():
    edl = make_edl(
        [
            make_segment("seg_1", in_=0.0, out=2.0),
            make_segment("seg_2", in_=10.0, out=38.0, snapped=2.3),  # 300ms drift
        ]
    )
    manifests = [make_manifest(duration=60.0)]
    strict = run_all(edl, manifests, make_settings(ave_beat_snap_tolerance_ms=180))
    lax = run_all(edl, manifests, make_settings(ave_beat_snap_tolerance_ms=400))
    assert "music" in strict.failures_by_agent
    assert "music" not in lax.failures_by_agent


# --------------------------------------------------------------------------- #
# run_qc (agent)                                                              #
# --------------------------------------------------------------------------- #
def test_run_qc_writes_versioned_report(tmp_path):
    edl, manifests = _passing_setup()
    storage = LocalStorage(tmp_path)
    report = run_qc(
        edl,
        manifests,
        "proj_qc",
        storage,
        settings=make_settings(),
        loudnorm_stats={"output_i": "-14.1"},
    )
    assert report.passed

    path = tmp_path / "projects" / "proj_qc" / "qc" / "report_v1.json"
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["passed"] is True
    assert on_disk["failures_by_agent"] == {}
    assert len(on_disk["results"]) == 6


def test_run_qc_report_version_tracks_edl_version(tmp_path):
    edl, manifests = _passing_setup()
    storage = LocalStorage(tmp_path)
    run_qc(edl.bump(), manifests, "proj_qc", storage, settings=make_settings())
    assert (tmp_path / "projects" / "proj_qc" / "qc" / "report_v2.json").exists()


def test_run_qc_returns_failing_report_without_raising(tmp_path):
    edl = make_edl([make_segment(seg_id="seg_ghost", clip="clip_missing", out=1.0)])
    storage = LocalStorage(tmp_path)
    report = run_qc(edl, [], "proj_qc", storage, settings=make_settings())
    assert not report.passed
    assert "editorial" in report.failures_by_agent
    assert (tmp_path / "projects" / "proj_qc" / "qc" / "report_v1.json").exists()
