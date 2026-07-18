"""Music & Beat Agent tests — library loading, track picking, grids, drop alignment.

Everything runs dependency-free: grid construction exercises only the synthetic
fallback path (no librosa), and libraries are built in tmp dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ave.agents.music import apply_music
from ave.analysis.manifest import ClipManifest, Highlight
from ave.config import Settings
from ave.edl.schema import EDL, Brief, Segment, Tone
from ave.music.grid import drop_time, grid_for_track
from ave.music.library import TrackMeta, load_library, pick_track


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _brief(**kw) -> Brief:
    kw.setdefault("target_duration_s", 30.0)
    return Brief(**kw)


def _seg(n: int, clip: str, in_: float, out: float, **kw) -> Segment:
    return Segment(id=f"seg_{n}", source_clip=clip, out=out, reason="test", **{"in": in_}, **kw)


def _track(track_id: str, bpm: float, genre: str = "", **kw) -> TrackMeta:
    kw.setdefault("duration_s", 100.0)
    return TrackMeta(track_id=track_id, bpm=bpm, genre=genre, **kw)


def _write_track(dir: Path, track_id: str, filename: str | None = None, **kw) -> Path:
    data = {"track_id": track_id, "bpm": 120.0, "duration_s": 100.0, **kw}
    path = dir / (filename or f"{track_id}.json")
    path.write_text(json.dumps(data))
    return path


def _settings() -> Settings:
    return Settings(ave_beat_snap_tolerance_ms=180)


# --------------------------------------------------------------------------- #
# load_library                                                                 #
# --------------------------------------------------------------------------- #
def test_load_library_missing_dir_is_empty(tmp_path: Path):
    assert load_library(tmp_path / "does_not_exist") == []


def test_load_library_skips_non_track_json(tmp_path: Path):
    _write_track(tmp_path, "real")
    (tmp_path / "readme.json").write_text(json.dumps({"note": "not a track"}))
    (tmp_path / "no_bpm.json").write_text(json.dumps({"track_id": "half"}))
    (tmp_path / "broken.json").write_text("{ not json")
    (tmp_path / "README.md").write_text("ignored entirely")
    lib = load_library(tmp_path)
    assert [t.track_id for t in lib] == ["real"]


def test_load_library_sorted_by_track_id(tmp_path: Path):
    # File names deliberately sort opposite to track ids.
    _write_track(tmp_path, "zeta", filename="a_first.json")
    _write_track(tmp_path, "alpha", filename="z_last.json")
    lib = load_library(tmp_path)
    assert [t.track_id for t in lib] == ["alpha", "zeta"]


def test_load_library_reads_full_metadata(tmp_path: Path):
    _write_track(
        tmp_path,
        "full",
        title="T",
        artist="A",
        genre="electronic",
        energy_curve=[0.1, 0.9],
        downbeat_offset_s=0.25,
        license="CC0-1.0",
        source="test",
        file="full.wav",
    )
    (t,) = load_library(tmp_path)
    assert t.genre == "electronic"
    assert t.energy_curve == [0.1, 0.9]
    assert t.downbeat_offset_s == 0.25
    assert t.file == "full.wav"


# --------------------------------------------------------------------------- #
# pick_track                                                                   #
# --------------------------------------------------------------------------- #
def test_pick_track_explicit_pin_found():
    lib = [_track("a", 90), _track("b", 128)]
    got = pick_track(_brief(music_track_id="b"), lib)
    assert got is not None and got.track_id == "b"


def test_pick_track_explicit_pin_missing_returns_none():
    lib = [_track("a", 90)]
    assert pick_track(_brief(music_track_id="ghost"), lib) is None


def test_pick_track_tone_genre_match_beats_bpm():
    # The ambient track's BPM is far from the tutorial default (100), but genre wins.
    lib = [_track("close_bpm", 100, genre="electronic"), _track("amb", 60, genre="ambient")]
    got = pick_track(_brief(tone=Tone.tutorial), lib)
    assert got is not None and got.track_id == "amb"


def test_pick_track_genre_tie_breaks_on_track_id():
    lib = [_track("bbb", 128, genre="pop"), _track("aaa", 90, genre="pop")]
    got = pick_track(_brief(tone=Tone.vlog), lib)
    assert got is not None and got.track_id == "aaa"


def test_pick_track_bpm_closest_fallback():
    # No electronic track: energetic default BPM is 128 -> 120 wins over 145.
    lib = [_track("fast", 145, genre="rock"), _track("mid", 120, genre="jazz")]
    got = pick_track(_brief(tone=Tone.energetic), lib)
    assert got is not None and got.track_id == "mid"


def test_pick_track_bpm_tie_breaks_on_track_id():
    # Both are 8 BPM from the energetic default of 128.
    lib = [_track("zz", 136, genre="rock"), _track("aa", 120, genre="jazz")]
    got = pick_track(_brief(tone=Tone.energetic), lib)
    assert got is not None and got.track_id == "aa"


def test_pick_track_empty_library_and_no_auto_pick():
    assert pick_track(_brief(), []) is None
    assert pick_track(_brief(auto_pick_music=False), [_track("a", 120)]) is None


def test_pick_track_deterministic():
    lib = [_track("b", 100, genre="pop"), _track("a", 100, genre="pop")]
    picks = {pick_track(_brief(tone=Tone.vlog), lib).track_id for _ in range(5)}
    assert picks == {"a"}


# --------------------------------------------------------------------------- #
# grid_for_track                                                               #
# --------------------------------------------------------------------------- #
def test_grid_for_track_synthetic_interval():
    grid = grid_for_track(_track("t", 120, duration_s=10.0))
    assert grid.beats[:4] == [0.0, 0.5, 1.0, 1.5]  # 60/120 = 0.5s per beat
    assert grid.downbeats[:2] == [0.0, 2.0]
    assert grid.bpm == 120


def test_grid_for_track_downbeat_offset_shifts_all_beats():
    grid = grid_for_track(_track("t", 120, duration_s=10.0, downbeat_offset_s=0.25))
    assert grid.beats[:3] == [0.25, 0.75, 1.25]
    assert grid.downbeats[:2] == [0.25, 2.25]
    assert grid.is_downbeat(0.25) and not grid.is_downbeat(0.75)


def test_grid_for_track_negative_offset_drops_negative_times():
    grid = grid_for_track(_track("t", 120, duration_s=10.0, downbeat_offset_s=-0.25))
    assert all(b >= 0.0 for b in grid.beats)
    assert grid.beats[0] == 0.25  # the 0.0 beat shifted to -0.25 and was dropped


def test_grid_for_track_bad_audio_path_falls_back(tmp_path: Path):
    grid = grid_for_track(
        _track("t", 60, duration_s=5.0), audio_path=str(tmp_path / "missing.wav")
    )
    assert grid.beats[:3] == [0.0, 1.0, 2.0]  # synthetic fallback at 60 BPM


# --------------------------------------------------------------------------- #
# drop_time                                                                    #
# --------------------------------------------------------------------------- #
def test_drop_time_math():
    meta = _track("t", 120, duration_s=100.0, energy_curve=[0.1, 0.2, 1.0, 0.4])
    assert drop_time(meta) == pytest.approx(50.0)  # index 2 of 4 -> 2/4 * 100


def test_drop_time_empty_curve():
    assert drop_time(_track("t", 120, energy_curve=[])) == 0.0


def test_drop_time_tie_first_occurrence_wins():
    meta = _track("t", 120, duration_s=80.0, energy_curve=[0.2, 0.9, 0.9, 0.1])
    assert drop_time(meta) == pytest.approx(1 / 4 * 80.0)


# --------------------------------------------------------------------------- #
# apply_music                                                                  #
# --------------------------------------------------------------------------- #
def _edl(**brief_kw) -> EDL:
    return EDL(
        project_id="p1",
        brief=_brief(**brief_kw),
        timeline=[
            _seg(0, "c1", 0.0, 4.0),   # timeline [0, 4)
            _seg(1, "c1", 10.0, 14.0),  # timeline [4, 8)
        ],
    )


def _manifest(highlights: list[Highlight]) -> ClipManifest:
    return ClipManifest(clip_id="c1", source_path="/dev/null", highlights=highlights)


def _library_with_drop(tmp_path: Path) -> Path:
    # 120 BPM, drop at 2/4 * 100s = 50s; genre matches the energetic tone default.
    _write_track(
        tmp_path,
        "beat_120",
        genre="electronic",
        energy_curve=[0.1, 0.2, 1.0, 0.4],
        file="missing.wav",  # not on disk -> synthetic grid path
    )
    return tmp_path


def test_apply_music_no_track_passthrough(tmp_path: Path):
    edl = _edl()
    out = apply_music(edl, [], tmp_path / "empty", settings=_settings())
    assert out is edl  # same object, no bump
    assert out.version == 1 and out.music.track_id is None


def test_apply_music_pinned_track_missing_passthrough(tmp_path: Path):
    _library_with_drop(tmp_path)
    edl = _edl(music_track_id="ghost")
    out = apply_music(edl, [], tmp_path, settings=_settings())
    assert out is edl


def test_apply_music_sets_track_and_bumps_version(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    edl = _edl()
    out = apply_music(edl, [_manifest([])], lib, settings=_settings())
    assert out is not edl
    assert out.version == edl.version + 1
    assert out.music.track_id == "beat_120"
    assert out.music.ducking is True
    assert out.music.duck_db == -14.0  # schema default preserved
    assert out.music.fade_in_s == 0.5 and out.music.fade_out_s == 1.5
    # Both segment boundaries (0.0s, 4.0s) lie exactly on the 120 BPM grid.
    assert len(out.music.sync_map) == 2
    assert [p.beat_s for p in out.music.sync_map] == [0.0, 4.0]
    assert all(p.is_downbeat for p in out.music.sync_map)
    # Input EDL untouched (immutability).
    assert edl.music.track_id is None and edl.version == 1


def test_apply_music_no_highlights_offset_zero(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    out = apply_music(_edl(), [_manifest([])], lib, settings=_settings())
    assert out.music.offset_s == 0.0


def test_apply_music_drop_alignment_math(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    highlights = [
        # Best in-timeline highlight: inside seg_1's source window [10, 14).
        Highlight(start_s=11.0, end_s=12.0, score=0.9),
        # Higher score but overlaps no segment -> must be ignored.
        Highlight(start_s=90.0, end_s=91.0, score=5.0),
        # Lower score inside seg_0 -> must lose to the 0.9 one.
        Highlight(start_s=1.0, end_s=2.0, score=0.3),
    ]
    out = apply_music(_edl(), [_manifest(highlights)], lib, settings=_settings())
    # Highlight timeline position: offset_of(seg_1)=4.0 + (11-10)/1 = 5.0.
    # Drop at 50.0s -> music starts 45.0s in so the drop lands on the highlight.
    assert out.music.offset_s == pytest.approx(45.0)


def test_apply_music_offset_clamped_to_zero(tmp_path: Path):
    # Drop at t=0 (curve max at index 0): drop - position < 0 must clamp to 0.
    _write_track(
        tmp_path, "early_drop", genre="electronic", energy_curve=[1.0, 0.2], file=""
    )
    highlights = [Highlight(start_s=11.0, end_s=12.0, score=0.9)]
    out = apply_music(_edl(), [_manifest(highlights)], tmp_path, settings=_settings())
    assert out.music.track_id == "early_drop"
    assert out.music.offset_s == 0.0


def test_apply_music_respects_speed_in_alignment(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    edl = EDL(
        project_id="p1",
        brief=_brief(),
        timeline=[
            _seg(0, "c1", 0.0, 4.0),                 # timeline [0, 4)
            _seg(1, "c1", 10.0, 14.0, speed=2.0),    # timeline [4, 6)
        ],
    )
    highlights = [Highlight(start_s=12.0, end_s=13.0, score=1.0)]
    out = apply_music(edl, [_manifest(highlights)], lib, settings=_settings())
    # Position: 4.0 + (12-10)/2 = 5.0; drop 50 -> offset 45.
    assert out.music.offset_s == pytest.approx(45.0)


def test_apply_music_result_round_trips_schema(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    highlights = [Highlight(start_s=11.0, end_s=12.0, score=0.9)]
    out = apply_music(_edl(), [_manifest(highlights)], lib, settings=_settings())
    again = EDL.model_validate(out.model_dump(mode="json", by_alias=True))
    assert again.content_hash() == out.content_hash()
    assert again.music.offset_s == out.music.offset_s


def test_apply_music_deterministic(tmp_path: Path):
    lib = _library_with_drop(tmp_path)
    highlights = [Highlight(start_s=11.0, end_s=12.0, score=0.9)]
    a = apply_music(_edl(), [_manifest(highlights)], lib, settings=_settings())
    b = apply_music(_edl(), [_manifest(highlights)], lib, settings=_settings())
    assert a.content_hash() == b.content_hash()
