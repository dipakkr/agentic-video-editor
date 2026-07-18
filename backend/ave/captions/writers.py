"""Caption file writers — SRT / WebVTT sidecars and burned-in ASS subtitles.

SRT and VTT are plain sidecar formats for platform upload; ASS is the styled format
libass burns into the video during render. Styling lives in ``STYLE_PRESETS`` (one
preset per :class:`~ave.edl.schema.CaptionStyle`) with the EDL's ``Captions`` font
settings layered on top. ASS output enforces a platform-UI safe zone: vertical and
square outputs keep captions at least 12% of the frame height off the bottom edge
(Reels/Shorts/TikTok overlay chrome there); 16:9 keeps at least 5%.
"""

from __future__ import annotations

import math

from ave.captions.cues import Cue
from ave.edl.schema import AspectRatio, Captions, CaptionStyle, OutputSpec

# ASS style presets keyed by CaptionStyle values. Colours are &HAABBGGRR& strings
# (AA=00 -> opaque). Alignment uses numpad convention: 2 = bottom-center.
STYLE_PRESETS: dict[str, dict] = {
    CaptionStyle.karaoke_bold.value: {
        "fontname": "Inter",
        "fontsize": 64,
        "primary_colour": "&H00FFFFFF&",
        "outline_colour": "&H00000000&",
        "outline": 4,
        "bold": -1,
        "alignment": 2,
    },
    CaptionStyle.phrase_pop.value: {
        "fontname": "Inter",
        "fontsize": 56,
        "primary_colour": "&H00FFFFFF&",
        "outline_colour": "&H00000000&",
        "outline": 3,
        "bold": -1,
        "alignment": 2,
    },
    CaptionStyle.clean_subtitle.value: {
        "fontname": "Inter",
        "fontsize": 44,
        "primary_colour": "&H00FFFFFF&",
        "outline_colour": "&H00000000&",
        "outline": 2,
        "bold": 0,
        "alignment": 2,
    },
    CaptionStyle.none.value: {
        "fontname": "Inter",
        "fontsize": 44,
        "primary_colour": "&H00FFFFFF&",
        "outline_colour": "&H00000000&",
        "outline": 2,
        "bold": 0,
        "alignment": 2,
    },
}

# Minimum bottom margin as a fraction of frame height, per aspect ratio.
_SAFE_ZONE_FRAC = {
    AspectRatio.vertical: 0.12,
    AspectRatio.square: 0.12,
    AspectRatio.wide: 0.05,
}


def format_srt_time(t: float) -> str:
    """Format seconds as SRT/VTT-style HH:MM:SS,mmm (comma separator)."""
    ms_total = max(0, round(t * 1000))
    s_total, ms = divmod(ms_total, 1000)
    m_total, s = divmod(s_total, 60)
    h, m = divmod(m_total, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(t: float) -> str:
    """Format seconds as ASS H:MM:SS.cc (centiseconds)."""
    cs_total = max(0, round(t * 100))
    s_total, cs = divmod(cs_total, 100)
    m_total, s = divmod(s_total, 60)
    h, m = divmod(m_total, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def to_srt(cues: list[Cue]) -> str:
    """Serialise cues as SubRip (1-indexed blocks, comma millisecond times)."""
    blocks = [
        f"{i}\n{format_srt_time(c.start_s)} --> {format_srt_time(c.end_s)}\n{c.text}"
        for i, c in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(cues: list[Cue]) -> str:
    """Serialise cues as WebVTT (dot millisecond times)."""
    blocks = [
        f"{format_srt_time(c.start_s).replace(',', '.')} --> "
        f"{format_srt_time(c.end_s).replace(',', '.')}\n{c.text}"
        for c in cues
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def _margin_v(captions: Captions, output: OutputSpec) -> int:
    """Bottom margin in pixels from position_y, floored by the platform safe zone."""
    requested = (1.0 - captions.position_y) * output.height
    minimum = math.ceil(_SAFE_ZONE_FRAC.get(output.aspect_ratio, 0.05) * output.height)
    return max(round(requested), minimum)


def _karaoke_text(cue: Cue) -> str:
    """Per-word {\\kNN} karaoke tags; NN = word duration in centiseconds (>= 1)."""
    if not cue.words:
        return cue.text
    parts = []
    for w in cue.words:
        cs = max(1, round((w.end_s - w.start_s) * 100))
        parts.append(f"{{\\k{cs}}}{w.word}")
    return " ".join(parts)


def to_ass(cues: list[Cue], captions: Captions, output: OutputSpec) -> str:
    """Build a complete ASS document for burn-in via libass.

    The style comes from ``STYLE_PRESETS[captions.style.value]`` with the EDL's
    font/font_size honoured as overrides. karaoke_bold renders per-word ``{\\k}``
    timing tags; other styles emit plain phrase/sentence lines.
    """
    preset = STYLE_PRESETS[captions.style.value]
    fontname = captions.font or preset["fontname"]
    fontsize = captions.font_size or preset["fontsize"]
    margin_v = _margin_v(captions, output)

    header = (
        "[Script Info]\n"
        "Title: AVE captions\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {output.width}\n"
        f"PlayResY: {output.height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{fontname},{fontsize},{preset['primary_colour']},"
        f"{preset['primary_colour']},{preset['outline_colour']},&H00000000&,"
        f"{preset['bold']},0,0,0,100,100,0,0,1,{preset['outline']},0,"
        f"{preset['alignment']},40,40,{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )

    karaoke = captions.style is CaptionStyle.karaoke_bold
    lines = []
    for cue in cues:
        text = _karaoke_text(cue) if karaoke else cue.text
        lines.append(
            f"Dialogue: 0,{format_ass_time(cue.start_s)},{format_ass_time(cue.end_s)},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(lines) + ("\n" if lines else "")
