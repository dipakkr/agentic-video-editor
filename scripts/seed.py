"""Seed script: generate 3 sample clips + 2 music tracks for offline dev.

Uses ffmpeg's synthetic sources (testsrc + sine tone) so no copyrighted material is ever
downloaded — every asset is generated locally. Run:

    python scripts/seed.py            # writes to ./data/seed
    python scripts/seed.py --run      # also runs the M1 pipeline on the sample clips

If ffmpeg is unavailable it prints instructions and exits cleanly.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = ROOT / "data" / "seed"
MUSIC_DIR = ROOT / "assets" / "music"

# Distinct colours/tones per clip so scene detection and highlight ranking have signal.
CLIPS = [
    {"name": "clip_01_intro.mp4", "pattern": "testsrc2=size=1280x720:rate=30", "freq": 440, "dur": 8},
    {"name": "clip_02_demo.mp4", "pattern": "smptebars=size=1280x720:rate=30", "freq": 660, "dur": 10},
    {"name": "clip_03_outro.mp4", "pattern": "testsrc=size=1280x720:rate=30", "freq": 330, "dur": 7},
]
TRACKS = [
    {"file": "energetic_128.wav", "bpm": 128, "dur": 20},
    {"file": "cinematic_90.wav", "bpm": 90, "dur": 20},
]


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_clip(spec: dict, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"{spec['pattern']}:duration={spec['dur']}",
         "-f", "lavfi", "-i", f"sine=frequency={spec['freq']}:duration={spec['dur']}",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(out)],
        check=True,
    )


def _make_track(spec: dict, out: Path) -> None:
    # A click-like tone at the track BPM gives beat detection something to grip.
    beat_hz = spec["bpm"] / 60.0
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi",
         "-i", f"sine=frequency=220:beep_factor={beat_hz}:duration={spec['dur']}",
         str(out)],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Run the M1 pipeline after seeding.")
    args = parser.parse_args()

    if not _have_ffmpeg():
        print("ffmpeg not found — install it to generate sample media.\n"
              "  macOS: brew install ffmpeg   ·   Debian/Ubuntu: apt-get install ffmpeg")
        return 1

    SEED_DIR.mkdir(parents=True, exist_ok=True)
    clip_paths = []
    for spec in CLIPS:
        dst = SEED_DIR / spec["name"]
        _make_clip(spec, dst)
        clip_paths.append(dst)
        print(f"  clip  {dst.relative_to(ROOT)}")

    for spec in TRACKS:
        dst = MUSIC_DIR / spec["file"]
        _make_track(spec, dst)
        print(f"  music {dst.relative_to(ROOT)}")

    print(f"\nSeeded {len(clip_paths)} clips + {len(TRACKS)} tracks under {SEED_DIR}")

    if args.run:
        print("\nRunning M1 pipeline on sample clips...\n")
        subprocess.run(
            [sys.executable, "-m", "ave.cli", "run", *[str(p) for p in clip_paths],
             "--platform", "youtube", "--duration", "20", "--tone", "energetic"],
            cwd=ROOT / "backend",
            check=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
