# Roadmap — manager plan for all phases

**End goal:** a user submits 15–20 raw clips; the platform autonomously produces a
polished, publishable edit — beat-synced cuts, mixed music, styled captions, b-roll
cutaways, graphics — reviewable in a web timeline, revisable by natural language, and
exportable per-platform with full per-agent customization.

**Process:** a manager agent (orchestrating this repo) plans each phase, delegates
individual features to specialist sub-agents, verifies + integrates their output, and
merges one PR per phase into `main`. All delegation prompts are logged in
`docs/orchestration/prompt-log.txt`; all product LLM prompts are logged at runtime to
`data/logs/prompts*.txt`.

---

## Phase M1 — Pipeline core ✅ (PR #1, merged)

Upload → analysis → EDL → ffmpeg rough cut. Versioned EDL as single source of truth,
checkpointed orchestrator, deterministic editorial fallback, beat-snap policy, prompt
audit log, CLI, seeds, 29 tests.

## Phase M2 — Sound & Words ✅ (PR #2, merged)

The edit gets its audio identity and captions.

| # | Feature | Owner | Deliverable |
|---|---------|-------|-------------|
| 2.1 | Music library + auto-pick | sub-agent A | `ave/music/library.py` (TrackMeta, load_library, pick_track by tone/BPM, deterministic), `ave/music/grid.py` (grid_for_track: librosa when available else synthetic from metadata BPM + downbeat offset; drop_time from energy curve) |
| 2.2 | Music & Beat agent | sub-agent A | `ave/agents/music.py` — pick track, build grid, `snap_edl`, set music fields, align biggest highlight to the drop, populate sync_map |
| 2.3 | Caption cues + writers | sub-agent B | `ave/captions/cues.py` (segment→timeline word remapping, phrase/sentence grouping), `ave/captions/writers.py` (SRT/VTT/ASS incl. karaoke \k tags, style presets, safe zones per aspect) |
| 2.4 | Caption agent | sub-agent B | `ave/agents/captions.py` — sidecars via storage + burn-in payload; graceful when no transcript |
| 2.5 | Render integration | manager | filtergraph: music input + dialogue ducking + fades + −14 LUFS loudnorm + ASS burn-in |
| 2.6 | Orchestrator nodes + CLI | manager | `music_beat` and `captions` stages between editorial and render; `--no-music`, `--caption-style` flags |

**Acceptance:** tests for pick determinism, grid math, drop alignment, cue remapping,
SRT/VTT/ASS goldens, duck-window argv, new graph stages; full suite green.

## Phase M3 — Interactive loop ✅ (PR #3, merged)

Human-in-the-loop revision with incremental re-render.

| # | Feature | Owner |
|---|---------|-------|
| 3.1 | `EditOp` schema + pure `apply_ops` (remove/trim/reorder/retime/set_transition/change_style/target_duration) | sub-agent |
| 3.2 | Revise agent: LLM ops (schema-constrained, EDL digest w/ reasons) + deterministic keyword fallback | sub-agent |
| 3.3 | Per-stage fingerprint cache in orchestrator — unchanged stages skip (incremental re-render) | manager |
| 3.4 | API: EDL version history, artifact serving, feedback wiring, SSE progress | manager |
| 3.5 | UI: timeline lanes (segments/beat markers/captions/music), preview player, per-agent progress, feedback box, version history | sub-agent |

**Acceptance:** ops application + fallback parsing tests; "unchanged hash skips render"
test; API TestClient flows; `tsc` clean.

## Phase M4 — Ship it ✅ (PR #4, merged)

| # | Feature | Owner |
|---|---------|-------|
| 4.1 | QC agent: duration, duplicate source-overlap, caption/transcript alignment sampling, loudness, black frames; failure routing (max 2 retries → surface) | sub-agent |
| 4.2 | Release agent: 3 titles, chaptered description, hashtags, thumbnail candidates (LLM + deterministic fallback) | sub-agent |
| 4.3 | Export presets from one EDL: 16:9 / 9:16 (center-crop reframe fallback) / 1:1; ≤90s short trims | manager |
| 4.4 | YouTube publish adapter behind an explicit-confirmation gate — never auto-publish | manager |

## Phase M5 — Rich content & customization ✅ (PR #5, merged)

| # | Feature | Owner |
|---|---------|-------|
| 5.1 | EDL v1.1: optional overlay track (b-roll) + graphics blocks (title card, lower third) — backward compatible | manager |
| 5.2 | B-roll intelligence: classify clips (speech-heavy = A-roll, low-speech = b-roll pool); place cutaways over long talking segments, with reasons | sub-agent |
| 5.3 | Graphics rendering: drawtext/ASS title card + lower thirds, style presets | sub-agent |
| 5.4 | Full customization: per-agent config in the brief (caption preset, transitions, music pin, duck depth, LUFS, toggles) surfaced in CLI + API + UI settings panel | manager |

## Phase M6 — Demo & hardening ✅ (PR #6)

| # | Feature | Owner |
|---|---------|-------|
| 6.1 | `scripts/demo.py`: seed 15–20 varied clips → full pipeline → demo report (timeline, EDL + reasons, QC report, release metadata, prompt-log excerpt) | manager |
| 6.2 | Visual demo page of the end-to-end result | manager |
| 6.3 | GitHub Actions CI: pytest + tsc on every PR | sub-agent |
| 6.4 | Final README/docs pass | manager |

**Definition of done:** one command runs the 15–20-clip demo end-to-end and produces the
full artifact chain; each phase merged to `main` as its own PR; suite green throughout.
