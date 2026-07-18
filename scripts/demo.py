"""End-to-end demo: 15–20 clips in → a fully planned, publishable edit out.

This is the product promise exercised in one command:

    python scripts/demo.py             # auto: real clips via ffmpeg, else synthetic
    python scripts/demo.py --clips 18  # choose the project size (15–20 typical)

With ffmpeg available it seeds real synthetic clips (scripts/seed.py style) and runs the
FULL pipeline including ingest + actual renders. Without ffmpeg it builds 15–20 varied
synthetic analysis manifests (talking-head clips with transcripts, b-roll, mixed
durations/highlights) and runs everything from editorial onward — every planning stage,
QC, and release run for real; the render emits its deterministic dry plan.

Artifacts land under data/demo/<project>/ and a human-readable DEMO_REPORT.md is written
summarizing the edit: timeline with per-cut reasons, b-roll cutaways, beat sync, music,
captions, QC findings, and the release kit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ave.analysis.manifest import (  # noqa: E402
    ClipManifest, Highlight, ProbeInfo, Shot, TranscriptSegment, Word,
)
from ave.config import Settings  # noqa: E402
from ave.edl.schema import Brief, Platform, Tone  # noqa: E402
from ave.llm.client import LLMClient  # noqa: E402
from ave.orchestrator.graph import Orchestrator, PipelineState, Stage  # noqa: E402
from ave.storage.store import LocalStorage  # noqa: E402

# Deterministic sample dialogue — one line per talking clip.
_LINES = [
    "we almost lost the entire dataset during the migration last week",
    "here is the moment everything finally clicked into place for us",
    "let me show you exactly how the pipeline rebuilds itself after a crash",
    "this tiny change doubled our render speed overnight and nobody noticed",
    "the first demo failed live on stage and taught us the biggest lesson",
    "watch what happens when we snap every cut to the downbeat",
    "three rules turned our raw footage into something people actually watch",
    "nobody believed an agent could edit video until this happened",
    "the secret is treating the edit list as the single source of truth",
    "we shipped the whole thing in six pull requests start to finish",
]


def _talking_manifest(i: int, line: str) -> ClipManifest:
    dur = 14.0 + (i % 4) * 3.0  # 14–23s
    words = line.split()
    step = min(0.5, (dur - 2.0) / max(len(words), 1))
    wobjs = [
        Word(word=w, start_s=round(1.0 + j * step, 2), end_s=round(1.3 + j * step, 2))
        for j, w in enumerate(words)
    ]
    cid = f"clip_{i:02d}"
    return ClipManifest(
        clip_id=cid, source_path=f"/demo/{cid}.mp4", proxy_path=f"/demo/{cid}_proxy.mp4",
        probe=ProbeInfo(duration_s=dur, width=1920, height=1080, fps=30.0, has_audio=True),
        shots=[Shot(start_s=0.0, end_s=dur)],
        transcript=[TranscriptSegment(
            start_s=1.0, end_s=round(1.3 + (len(words) - 1) * step, 2),
            text=line, words=wobjs,
        )],
        highlights=[Highlight(
            start_s=1.0, end_s=min(dur, 5.0), score=round(0.95 - i * 0.03, 3), text=line,
        )],
        analysis_features={"synthetic": True},
    )


def _broll_manifest(i: int) -> ClipManifest:
    dur = 8.0 + (i % 3) * 4.0  # 8–16s, no speech
    cid = f"clip_{i:02d}"
    return ClipManifest(
        clip_id=cid, source_path=f"/demo/{cid}.mp4", proxy_path=f"/demo/{cid}_proxy.mp4",
        probe=ProbeInfo(duration_s=dur, width=1920, height=1080, fps=30.0, has_audio=True),
        shots=[Shot(start_s=s, end_s=min(s + 4.0, dur)) for s in range(0, int(dur), 4)],
        analysis_features={"synthetic": True},
    )


def synthetic_manifests(n_clips: int) -> list[ClipManifest]:
    """~60% talking clips, ~40% b-roll — a realistic vlog/tutorial shoot."""
    manifests: list[ClipManifest] = []
    talkers = 0
    for i in range(1, n_clips + 1):
        if i % 5 in (1, 2, 3):  # 3 of every 5 are talking clips
            manifests.append(_talking_manifest(i, _LINES[talkers % len(_LINES)]))
            talkers += 1
        else:
            manifests.append(_broll_manifest(i))
    return manifests


def write_report(state: PipelineState, out_path: Path, mode: str) -> None:
    edl = state.edl
    assert edl is not None
    lines: list[str] = []
    a = lines.append
    a("# 🎬 Agentic Video Editor — Demo Report\n")
    a(f"*Mode: {mode} · {len(state.manifests)} source clips · project `{state.project_id}`*\n")
    a(f"**Brief:** {edl.brief.platform.value}, target {edl.brief.target_duration_s}s "
      f"±{edl.brief.duration_tolerance_pct}%, tone {edl.brief.tone.value}\n")
    a(f"**Result:** EDL v{edl.version} · {edl.total_duration_s}s "
      f"({'within target ✅' if edl.within_target() else 'OUT OF TARGET ❌'}) · "
      f"{len(edl.timeline)} segments · {len(edl.overlays)} b-roll cutaways\n")

    a("\n## Timeline — every cut justified\n")
    a("| # | clip | in→out | dur | transition | beat-snapped | reason |")
    a("|---|------|--------|-----|-----------|--------------|--------|")
    for s in edl.timeline:
        a(f"| {s.id} | {s.source_clip} | {s.in_:.2f}→{s.out:.2f} "
          f"| {s.timeline_duration_s:.1f}s | {s.transition_in.value} "
          f"| {'✂♪' if s.cut_snapped_to_beat else '—'} | {s.reason} |")

    if edl.overlays:
        a("\n## B-roll cutaways\n")
        for o in edl.overlays:
            a(f"- **{o.id}** {o.source_clip} @ {o.timeline_start_s:.1f}s "
              f"({o.duration_s:.1f}s) — {o.reason}")

    if edl.graphics.title_card:
        a(f"\n## Graphics\n- Title card: “{edl.graphics.title_card.text}” "
          f"@ {edl.graphics.title_card.start_s}s for {edl.graphics.title_card.duration_s}s")

    a("\n## Music & beat sync\n")
    a(f"- Track: `{edl.music.track_id}` · offset {edl.music.offset_s}s · "
      f"duck {edl.music.duck_db} dB under dialogue")
    a(f"- Sync map: {len(edl.music.sync_map)} beat-locked cut points "
      f"({sum(1 for p in edl.music.sync_map if p.is_downbeat)} on downbeats)")
    a(f"- Captions: {edl.captions.style.value}")

    if state.qc_report:
        a("\n## QC\n")
        a(f"**{'PASSED ✅' if state.qc_report.get('passed') else 'FINDINGS ⚠️'}**")
        for r in state.qc_report.get("results", []):
            a(f"- {'✅' if r['passed'] else '❌'} `{r['check']}` — {r['details']}")

    if state.release_kit:
        kit = state.release_kit
        a("\n## Release kit\n")
        a("**Title options:**")
        for t in kit.get("titles", []):
            a(f"1. {t}")
        a(f"\n**Hashtags:** {' '.join(kit.get('hashtags', []))}")
        a(f"\n**Thumbnail candidates:** "
          f"{len(kit.get('thumbnails', []))} (by highlight strength)")
        a("\n**Description:**\n")
        a("```")
        a(kit.get("description", ""))
        a("```")

    if state.render_result:
        a("\n## Render\n")
        rr = state.render_result
        if rr.get("executed"):
            a(f"- Output: `{rr.get('output_path')}`")
        else:
            a(f"- Deterministic dry-render plan: `{rr.get('plan_path')}` "
              f"(install ffmpeg for the actual encode)")
        a(f"- Content hash: `{rr.get('content_hash', '')[:16]}` "
          f"(same EDL + assets ⇒ identical output)")

    a("\n---\n*Every LLM prompt used is logged in data/logs/prompts*.txt; every "
      "manager→sub-agent build prompt in docs/orchestration/prompt-log.txt.*")
    out_path.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=int, default=18, help="Number of clips (15–20).")
    parser.add_argument("--duration", type=float, default=60.0, help="Target duration s.")
    parser.add_argument("--platform", default="youtube")
    parser.add_argument("--tone", default="energetic")
    parser.add_argument("--synthetic", action="store_true",
                        help="Force synthetic manifests even if ffmpeg exists.")
    args = parser.parse_args()

    n = max(15, min(20, args.clips))
    have_ffmpeg = shutil.which("ffmpeg") is not None and not args.synthetic

    data_dir = ROOT / "data" / "demo"
    settings = Settings(ave_data_dir=data_dir, ave_music_dir=ROOT / "assets" / "music")
    storage = LocalStorage(data_dir)
    pid = "demo_project"

    events: list[tuple[str, str]] = []

    def progress(stage: str, status: str, d: dict) -> None:
        events.append((stage, status))
        extras = {k: v for k, v in d.items()
                  if k in ("segments", "duration_s", "track", "snapped", "overlays",
                           "cue_count", "passed", "titles", "executed")}
        print(f"  {stage:<11} {status:<9} {json.dumps(extras) if extras else ''}")

    orch = Orchestrator(storage, settings=settings, llm=LLMClient(settings),
                        on_progress=progress)
    brief = Brief(platform=Platform(args.platform), target_duration_s=args.duration,
                  tone=Tone(args.tone))

    if have_ffmpeg:
        print(f"▶ Seeding {n} real clips with ffmpeg…")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "seed.py")], check=False)
        seed_dir = ROOT / "data" / "seed"
        clip_files = sorted(seed_dir.glob("*.mp4"))[:n]
        clips = {f"clip_{i:02d}": str(p) for i, p in enumerate(clip_files, 1)}
        state = PipelineState(project_id=pid, brief=brief, clips=clips)
        mode = f"full (ffmpeg, {len(clips)} real clips)"
    else:
        print(f"▶ ffmpeg unavailable — building {n} synthetic analysis manifests")
        manifests = synthetic_manifests(n)
        for m in manifests:
            storage.write_json(pid, f"manifests/{m.clip_id}.json", m.model_dump(mode="json"))
        state = PipelineState(project_id=pid, brief=brief, clips={},
                              manifests=manifests, stage=Stage.editorial)
        mode = f"planning (synthetic, {n} clips)"

    print(f"▶ Running the pipeline ({mode})…")
    state = orch.run(state)

    report_path = storage.project_dir(pid) / "DEMO_REPORT.md"
    write_report(state, report_path, mode)

    print(f"\n✅ Demo complete — {len(events)} pipeline events")
    print(f"   EDL: v{state.edl.version}, {state.edl.total_duration_s}s, "
          f"{len(state.edl.timeline)} segments, {len(state.edl.overlays)} cutaways")
    print(f"   Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
