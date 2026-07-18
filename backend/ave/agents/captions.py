"""Captions Agent — turn the EDL + transcripts into subtitle artifacts.

Produces three artifacts per project with distinct roles:

* ``subs.ass`` — the *burned-in* track: styled (fonts, outlines, karaoke timing,
  platform safe zones) and consumed by the Render Agent, which hands it to
  libass/ffmpeg so captions are pixels in the final video. Required for
  Reels/Shorts/TikTok where most viewing is muted and player captions are unreliable.
* ``subs.srt`` / ``subs.vtt`` — *sidecar* tracks: unstyled, uploaded alongside the
  video (YouTube captions, web players) so platforms index the speech and viewers
  can toggle captions off.

Degrades gracefully: a disabled style or a missing transcript yields a structured
"nothing to do" result rather than an error, so the pipeline never fails on
caption-less footage.
"""

from __future__ import annotations

from ave.analysis.manifest import ClipManifest
from ave.captions.cues import build_cues, mode_for_style
from ave.captions.writers import to_ass, to_srt, to_vtt
from ave.edl.schema import EDL, CaptionStyle
from ave.storage.store import Storage


def generate_captions(
    edl: EDL, manifests: list[ClipManifest], project_id: str, storage: Storage
) -> dict:
    """Generate SRT/VTT/ASS caption files for the project. Never raises for
    missing transcripts — returns a zero-cue result instead."""
    empty = {"cue_count": 0, "srt": None, "vtt": None, "ass": None}

    if edl.captions.style is CaptionStyle.none:
        return {**empty, "note": "captions disabled"}

    cues = build_cues(edl, manifests, mode=mode_for_style(edl.captions.style))
    if not cues:
        return {**empty, "note": "no transcript"}

    srt_path = storage.path_for(project_id, "captions/subs.srt")
    srt_path.write_text(to_srt(cues))
    vtt_path = storage.path_for(project_id, "captions/subs.vtt")
    vtt_path.write_text(to_vtt(cues))
    ass_path = storage.path_for(project_id, "captions/subs.ass")
    ass_path.write_text(to_ass(cues, edl.captions, edl.output))

    return {
        "cue_count": len(cues),
        "srt": str(srt_path),
        "vtt": str(vtt_path),
        "ass": str(ass_path),
        "note": "ok",
    }
