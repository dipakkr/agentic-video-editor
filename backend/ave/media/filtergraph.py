"""Compile an EDL timeline into an ffmpeg filtergraph.

Rendering is a pure function of the EDL: this module produces the exact `ffmpeg` argv
for a given EDL + resolved source paths, so the same inputs always yield the same output
(determinism guarantee). M1 renders picture+source-audio only — music and captions are
layered in M2.

Two strategies:
  * `build_hardcut` — trim/normalize each segment and `concat`. This is the default
    (hard cuts) and is the fastest, leanest path.
  * `build_xfade` — a progressive `xfade`/`acrossfade` chain, used when any segment
    requests a crossfade/whip/fade transition.
"""

from __future__ import annotations

from dataclasses import dataclass

from ave.edl.schema import EDL, OutputSpec, Segment, Transition

_XFADE_MODE = {
    Transition.crossfade: "fade",
    Transition.whip: "smoothleft",
    Transition.fade_from_black: "fadeblack",
}


@dataclass
class FFmpegPlan:
    inputs: list[str]              # ordered -i source paths
    filtergraph: str              # -filter_complex value
    maps: list[str]               # output stream labels to map
    args: list[str]               # full ffmpeg argv (after the binary)


def _seg_chain(seg: Segment, idx: int, out: OutputSpec) -> tuple[str, str, str]:
    """Filter chain normalising one segment to the output canvas.

    Returns (video_chain, audio_chain, label_suffix). Letterbox-pads to the target
    aspect so mixed-resolution sources composite cleanly; resets PTS for concat.
    """
    w, h, fps = out.width, out.height, out.fps
    v_label, a_label = f"v{idx}", f"a{idx}"
    setpts = "setpts=PTS-STARTPTS"
    speed_v = f",setpts=PTS/{seg.speed}" if seg.speed != 1.0 else ""

    video = (
        f"[{idx}:v]trim=start={seg.in_}:end={seg.out},{setpts}{speed_v},"
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[{v_label}]"
    )
    # atempo only accepts 0.5..2.0; chain factors for larger speed changes.
    atempo = _atempo_chain(seg.speed)
    audio = (
        f"[{idx}:a]atrim=start={seg.in_}:end={seg.out},asetpts=PTS-STARTPTS"
        f"{atempo},aresample=48000[{a_label}]"
    )
    return video, audio, str(idx)


def _atempo_chain(speed: float) -> str:
    if speed == 1.0:
        return ""
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(round(remaining, 4))
    return "".join(f",atempo={f}" for f in factors)


def build_hardcut(edl: EDL, sources: dict[str, str]) -> FFmpegPlan:
    """Concat-based render (hard cuts)."""
    segs = edl.timeline
    inputs = [sources[s.source_clip] for s in segs]
    chains: list[str] = []
    concat_labels: list[str] = []
    for i, seg in enumerate(segs):
        v, a, _ = _seg_chain(seg, i, edl.output)
        chains.append(v)
        chains.append(a)
        concat_labels.append(f"[v{i}][a{i}]")
    concat = f"{''.join(concat_labels)}concat=n={len(segs)}:v=1:a=1[vout][aout]"
    graph = ";".join(chains + [concat])
    return _finalize(inputs, graph, ["[vout]", "[aout]"], edl.output)


def build_xfade(edl: EDL, sources: dict[str, str]) -> FFmpegPlan:
    """Progressive xfade/acrossfade chain when transitions are requested."""
    segs = edl.timeline
    inputs = [sources[s.source_clip] for s in segs]
    chains: list[str] = []
    for i, seg in enumerate(segs):
        v, a, _ = _seg_chain(seg, i, edl.output)
        chains.append(v)
        chains.append(a)

    prev_v, prev_a = "v0", "a0"
    offset = segs[0].timeline_duration_s
    for i in range(1, len(segs)):
        seg = segs[i]
        dur = max(0.1, seg.transition_duration_s or 0.4)
        mode = _XFADE_MODE.get(seg.transition_in)
        vo, ao = f"vx{i}", f"ax{i}"
        start = max(0.0, offset - dur)
        if mode:
            chains.append(
                f"[{prev_v}][v{i}]xfade=transition={mode}:duration={dur}:offset={start}[{vo}]"
            )
            chains.append(f"[{prev_a}][a{i}]acrossfade=d={dur}[{ao}]")
            offset = start + seg.timeline_duration_s
        else:  # hard cut inside an otherwise-transitioned timeline
            chains.append(f"[{prev_v}][v{i}]concat=n=2:v=1:a=0[{vo}]")
            chains.append(f"[{prev_a}][a{i}]concat=n=2:v=0:a=1[{ao}]")
            offset += seg.timeline_duration_s
        prev_v, prev_a = vo, ao

    graph = ";".join(chains)
    return _finalize(inputs, graph, [f"[{prev_v}]", f"[{prev_a}]"], edl.output)


def build(edl: EDL, sources: dict[str, str]) -> FFmpegPlan:
    """Pick the strategy based on requested transitions."""
    if not edl.timeline:
        raise ValueError("cannot render an empty timeline")
    has_transition = any(
        s.transition_in in _XFADE_MODE for s in edl.timeline[1:]
    )
    return build_xfade(edl, sources) if has_transition else build_hardcut(edl, sources)


def _finalize(inputs: list[str], graph: str, maps: list[str], out: OutputSpec) -> FFmpegPlan:
    args: list[str] = []
    for path in inputs:
        args += ["-i", path]
    args += ["-filter_complex", graph]
    for label in maps:
        args += ["-map", label]
    args += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(out.fps),
        "-movflags", "+faststart",
    ]
    return FFmpegPlan(inputs=inputs, filtergraph=graph, maps=maps, args=args)
