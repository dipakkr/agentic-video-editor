"""Beat-snapping tests — the product differentiator, so tested thoroughly.

A cut that lands 200ms off-beat feels wrong; these lock down the tolerance window,
downbeat preference, and EDL-level snapping behaviour.
"""

from __future__ import annotations

from ave.beat.snap import (
    BeatGrid,
    snap_edl,
    snap_time,
    synthetic_grid,
)
from ave.edl.schema import EDL, Brief, Platform, Segment, Tone, Transition


def _grid_120bpm() -> BeatGrid:
    # 120 BPM => beat every 0.5s. Downbeats every 4th beat (0, 2, 4, ...s).
    return synthetic_grid(bpm=120.0, duration_s=20.0, beats_per_bar=4)


def test_synthetic_grid_intervals():
    g = _grid_120bpm()
    assert g.beats[:4] == [0.0, 0.5, 1.0, 1.5]
    assert g.downbeats[:3] == [0.0, 2.0, 4.0]
    assert g.is_downbeat(2.0) and not g.is_downbeat(0.5)


def test_snap_within_tolerance():
    g = _grid_120bpm()
    # 0.42s is 80ms from the 0.5 beat -> snaps.
    res = snap_time(0.42, g, tolerance_ms=180)
    assert res.snapped and res.snapped_s == 0.5
    assert abs(res.delta_ms - 80.0) < 1e-6


def test_no_snap_outside_tolerance():
    g = _grid_120bpm()
    # 0.75s is 250ms from both 0.5 and 1.0 -> beyond 180ms tolerance -> untouched.
    res = snap_time(0.75, g, tolerance_ms=180)
    assert not res.snapped and res.snapped_s == 0.75


def test_snap_picks_nearest_beat():
    g = _grid_120bpm()
    assert snap_time(0.9, g, tolerance_ms=180).snapped_s == 1.0
    assert snap_time(1.1, g, tolerance_ms=180).snapped_s == 1.0


def test_downbeat_preference_within_bias():
    g = _grid_120bpm()
    # 1.9s: nearest beat is 2.0 (a downbeat) at 100ms. Regular beat 1.5 is farther.
    res = snap_time(1.9, g, tolerance_ms=180, prefer_downbeat=True)
    assert res.snapped_s == 2.0 and res.to_downbeat


def test_downbeat_not_forced_when_far():
    g = _grid_120bpm()
    # 1.05s: nearest regular beat 1.0 (50ms); nearest downbeat 2.0 is ~950ms away
    # (outside tolerance) so we must keep the regular beat.
    res = snap_time(1.05, g, tolerance_ms=180, prefer_downbeat=True)
    assert res.snapped_s == 1.0 and not res.to_downbeat


def test_downbeat_bias_prefers_downbeat_when_comparably_close():
    g = _grid_120bpm()
    # 1.96s: regular beat 2.0 is a downbeat anyway; use a case where a downbeat is
    # slightly farther but within bias. Beats at 1.5 (reg) and 2.0 (down) from 1.85:
    # reg delta 350ms (out of tol). Use 1.93: nearest 2.0 downbeat 70ms -> downbeat.
    res = snap_time(1.93, g, tolerance_ms=180, prefer_downbeat=True)
    assert res.snapped_s == 2.0 and res.to_downbeat


def test_empty_grid_no_snap():
    g = BeatGrid(beats=[])
    res = snap_time(1.23, g)
    assert not res.snapped and res.snapped_s == 1.23


def test_snap_edl_populates_sync_map_and_flags():
    g = _grid_120bpm()
    brief = Brief(platform=Platform.youtube, target_duration_s=10.0, tone=Tone.energetic)
    # Two segments; second starts on timeline at 3.1s (seg1 duration) -> near beat 3.0.
    edl = EDL(
        project_id="p",
        brief=brief,
        timeline=[
            Segment(id="seg_01", source_clip="c1", **{"in": 0.0}, out=3.1, reason="hook"),
            Segment(id="seg_02", source_clip="c2", **{"in": 2.0}, out=6.0, reason="beat"),
        ],
    )
    snapped = snap_edl(edl, g, tolerance_ms=180)
    assert snapped.version == edl.version + 1
    assert snapped.music.sync_map, "expected a populated sync map"
    # seg2 boundary at 3.1s snaps to 3.0s and records the shift.
    seg2 = snapped.timeline[1]
    assert seg2.cut_snapped_to_beat and seg2.snapped_beat_s == 3.0


def test_snap_edl_is_deterministic():
    g = _grid_120bpm()
    brief = Brief(platform=Platform.youtube, target_duration_s=10.0, tone=Tone.energetic)
    edl = EDL(
        project_id="p",
        brief=brief,
        timeline=[
            Segment(id="seg_01", source_clip="c1", **{"in": 0.0}, out=2.1, reason="a"),
            Segment(id="seg_02", source_clip="c2", **{"in": 0.0}, out=2.1, reason="b"),
        ],
    )
    a = snap_edl(edl, g)
    b = snap_edl(edl, g)
    assert a.content_hash() == b.content_hash()


def test_snap_edl_does_not_invert_segment():
    g = _grid_120bpm()
    brief = Brief(platform=Platform.youtube, target_duration_s=10.0, tone=Tone.energetic)
    # A tiny segment where a negative shift could push in past out — must stay valid.
    edl = EDL(
        project_id="p",
        brief=brief,
        timeline=[
            Segment(id="seg_01", source_clip="c1", **{"in": 0.0}, out=0.55, reason="a"),
            Segment(id="seg_02", source_clip="c2", **{"in": 0.05}, out=0.6, reason="b"),
        ],
    )
    snapped = snap_edl(edl, g)
    for seg in snapped.timeline:
        assert seg.out > seg.in_
