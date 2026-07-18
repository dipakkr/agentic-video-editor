"""Thin, testable wrappers around ffmpeg / ffprobe.

Kept dependency-free (stdlib subprocess) so the pipeline can probe and transcode
without any Python media libraries. Higher-level agents build EDL-driven filtergraphs
on top of `run_ffmpeg`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


class FFmpegNotAvailable(RuntimeError):
    """Raised when ffmpeg/ffprobe binaries are not on PATH."""


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise FFmpegNotAvailable(
            f"`{binary}` not found on PATH. Install ffmpeg (see README) or run analysis "
            f"in fallback mode."
        )
    return path


@dataclass
class ProbeResult:
    duration_s: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str
    audio_channels: int
    has_audio: bool


def _parse_fps(rate: str) -> float:
    if not rate or rate in ("0/0", "N/A"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


def ffprobe(path: str) -> ProbeResult:
    """Probe a media file. Raises FFmpegNotAvailable if ffprobe is missing."""
    exe = _require("ffprobe")
    cmd = [
        exe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})

    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0")

    return ProbeResult(
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=round(fps, 3),
        video_codec=video.get("codec_name", ""),
        audio_codec=audio.get("codec_name", ""),
        audio_channels=int(audio.get("channels") or 0),
        has_audio=bool(audio),
    )


def run_ffmpeg(args: list[str], *, overwrite: bool = True) -> subprocess.CompletedProcess:
    """Run an ffmpeg command (args after the binary). Raises on non-zero exit."""
    exe = _require("ffmpeg")
    cmd = [exe, "-hide_banner", "-loglevel", "error"]
    if overwrite:
        cmd.append("-y")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def make_proxy(src: str, dst: str, height: int = 720, fps: float = 30.0) -> str:
    """Transcode a mezzanine proxy: capped height, unified fps, normalised loudness.

    H.264 high-bitrate + EBU R128 loudnorm so preview renders are cheap but faithful.
    """
    vf = f"scale=-2:{height},fps={fps}"
    args = [
        "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        dst,
    ]
    run_ffmpeg(args)
    return dst
