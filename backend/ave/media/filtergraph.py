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

    if out.reframe == "center_crop":
        # Fill the canvas: scale to cover, then crop the center. This is the 9:16/1:1
        # auto-reframe fallback; subject-tracked crop anchors arrive with M5.
        fit = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}"
        )
    else:
        fit = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        )
    video = (
        f"[{idx}:v]trim=start={seg.in_}:end={seg.out},{setpts}{speed_v},"
        f"{fit},setsar=1,fps={fps}[{v_label}]"
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


def augment_with_overlays_and_graphics(
    plan: FFmpegPlan,
    edl: EDL,
    *,
    overlay_sources: dict[str, str] | None = None,
) -> FFmpegPlan:
    """Layer M5 visuals onto a base plan: full-frame b-roll cutaways + graphics.

    Overlays are true cutaways — the b-roll picture replaces the frame for its window
    (delayed via setpts, gated with overlay `enable`) while the program audio continues
    untouched underneath. Graphics render with drawtext: a centered title card and
    lower-third straps, each time-gated. Applied BEFORE the music/captions augment so
    burned-in captions stay on top of everything.

    Pure + deterministic, keyed off the base plan's terminal labels like the M2 augment.
    """
    overlay_sources = overlay_sources or {}
    v_label = plan.maps[0].strip("[]")
    a_label = plan.maps[1]
    inputs = list(plan.inputs)
    chains = [plan.filtergraph]
    w, h, fps = edl.output.width, edl.output.height, edl.output.fps

    current = v_label
    for i, ovl in enumerate(edl.overlays):
        src = overlay_sources.get(ovl.id)
        if src is None:
            continue  # unresolvable b-roll: skip the cutaway, never fail the render
        idx = len(inputs)
        inputs.append(src)
        start = ovl.timeline_start_s
        end = round(start + ovl.duration_s, 4)
        prep, out_label = f"ov{i}", f"vo{i}"
        chains.append(
            f"[{idx}:v]trim=start={ovl.in_}:end={ovl.out},setpts=PTS-STARTPTS,"
            f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
            f"setsar=1,fps={fps},setpts=PTS+{start}/TB[{prep}]"
        )
        chains.append(
            f"[{current}][{prep}]overlay=eof_action=pass:"
            f"enable='between(t,{start},{end})'[{out_label}]"
        )
        current = out_label

    for i, item in enumerate(_graphics_items(edl)):
        out_label = f"vg{i}"
        chains.append(f"[{current}]{item}[{out_label}]")
        current = out_label

    if current == v_label:
        return plan  # nothing to add
    graph = ";".join(chains)
    return _finalize(inputs, graph, [f"[{current}]", a_label], edl.output)


def _graphics_items(edl: EDL) -> list[str]:
    """drawtext filter fragments for the EDL's graphics spec (time-gated)."""
    items: list[str] = []
    h = edl.output.height
    card = edl.graphics.title_card
    if card is not None:
        end = round(card.start_s + card.duration_s, 4)
        items.append(
            f"drawtext=text='{_escape_drawtext(card.text)}':fontsize={h // 10}:"
            f"fontcolor=white:borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable='between(t,{card.start_s},{end})'"
        )
    for lt in edl.graphics.lower_thirds:
        end = round(lt.start_s + lt.duration_s, 4)
        items.append(
            f"drawtext=text='{_escape_drawtext(lt.text)}':fontsize={h // 22}:"
            f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12:"
            f"x=w*0.05:y=h*0.78:"
            f"enable='between(t,{lt.start_s},{end})'"
        )
    return items


def _escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext (order matters: backslash first)."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\\\\\'")
        .replace("%", "\\%")
    )


def augment_with_music_and_captions(
    plan: FFmpegPlan,
    edl: EDL,
    *,
    music_path: str | None = None,
    ass_path: str | None = None,
) -> FFmpegPlan:
    """Layer the M2 audio identity onto a base plan: music bed + ducking + captions.

    Works generically on the base plan's terminal labels (plan.maps), so it composes with
    both the hardcut and xfade strategies. Music is trimmed to the timeline window
    (honouring `music.offset_s`), faded in/out, ducked under dialogue with
    `sidechaincompress` (sidechain-style ducking), mixed, then mastered with loudnorm to
    the output's target LUFS. Captions are burned in via the libass `ass` filter when an
    .ass file is provided; sidecars always ship regardless.

    Pure function: same plan + same EDL + same paths => identical argv (determinism).
    """
    v_label = plan.maps[0].strip("[]")
    a_label = plan.maps[1].strip("[]")
    inputs = list(plan.inputs)
    chains = [plan.filtergraph]
    total = edl.total_duration_s
    m = edl.music

    final_a = a_label
    if music_path is not None:
        music_idx = len(inputs)
        inputs.append(music_path)
        fade_out_start = max(0.0, total - m.fade_out_s)
        music_chain = (
            f"[{music_idx}:a]atrim=start={m.offset_s}:end={m.offset_s + total},"
            f"asetpts=PTS-STARTPTS,aresample=48000,"
            f"afade=t=in:st=0:d={m.fade_in_s},"
            f"afade=t=out:st={fade_out_start}:d={m.fade_out_s}[music]"
        )
        chains.append(music_chain)
        if m.ducking:
            # Sidechain ducking: dialogue drives a compressor on the music bed. level_sc
            # scales sidechain sensitivity; ratio/threshold approximate a -12..-15 dB duck
            # under speech, with musical attack/release so pumping stays unobtrusive.
            chains.append(f"[{a_label}]asplit=2[dlg][sc]")
            chains.append(
                "[music][sc]sidechaincompress="
                "threshold=0.02:ratio=12:attack=25:release=350:makeup=1[ducked]"
            )
            chains.append("[dlg][ducked]amix=inputs=2:duration=first:normalize=0[mixed]")
        else:
            chains.append(f"[{a_label}][music]amix=inputs=2:duration=first:normalize=0[mixed]")
        final_a = "mixed"

    # Master the program bus to the target loudness regardless of music presence.
    chains.append(
        f"[{final_a}]loudnorm=I={edl.output.target_lufs}:TP=-1.5:LRA=11[afinal]"
    )

    final_v = v_label
    if ass_path is not None:
        chains.append(f"[{v_label}]ass=filename='{_escape_filter_path(ass_path)}'[vfinal]")
        final_v = "vfinal"
    else:
        chains.append(f"[{v_label}]null[vfinal]")
        final_v = "vfinal"

    graph = ";".join(chains)
    return _finalize(inputs, graph, [f"[{final_v}]", "[afinal]"], edl.output)


def _escape_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


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
