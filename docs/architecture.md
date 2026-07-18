# Architecture notes

## Design principles

1. **The EDL is the single source of truth.** Every agent reads and mutates one versioned
   JSON document. Rendering is a *pure function* of the EDL. This buys us determinism,
   content-hash de-duplication, incremental re-rendering, and an explainable edit (every
   segment has a `reason`).
2. **Graceful degradation over hard failure.** ffmpeg, WhisperX, librosa, MediaPipe, and
   the LLM are all optional. Each analysis pass records whether it actually ran
   (`manifest.analysis_features`); downstream agents reason about missing signals rather
   than crashing. This keeps M1 runnable in CI and on a laptop with nothing installed.
3. **One code path for CLI and API.** All logic lives in the agents + orchestrator. The CLI
   and FastAPI app are thin drivers, so behaviour can't drift between them.
4. **Milestone seams up front.** The Music/Beat, Caption, QC, and Release stages have
   defined insertion points in the orchestrator graph; the beat-snap policy, feedback loop,
   worker, and API already exist as stubs so later milestones don't reshape M1.

## The orchestrator graph

A checkpointed state machine (LangGraph-equivalent). State is persisted to storage after
every node, so a crash resumes from the last completed stage.

```
ingest → editorial → [music+beat → captions]* → render → [qc]* → [release]*
                ^                                    |
                └──────── feedback / qc-retry ───────┘
        (* = M2/M4; render re-runs only when the EDL content hash changes)
```

- **Checkpointing:** `PipelineState.checkpoint()` writes `state.json`; `Orchestrator.run`
  loops from `state.stage`, so resuming is just re-invoking `run` with the loaded state.
- **Incremental re-render:** `apply_feedback` bumps the EDL and re-runs render *only* if
  `content_hash()` changed. Identical hashes short-circuit (safe because render is pure).

## Editorial: LLM path + deterministic fallback

The LLM does narrative reasoning over transcripts and returns JSON constrained to
`EDITORIAL_SCHEMA`; invalid output is rejected and retried (`LLMClient.complete_json`),
then parsed into the strict Pydantic `EDL`. When no API key is present — or on any LLM
error — a rule-based planner greedily selects the best usable windows, opens on the
highest-scoring hook, drops dead-air/filler, and hits the target duration ±tolerance. The
fallback guarantees end-to-end runs and gives the LLM a reproducible baseline.

**Cost controls:** per-project call cap (`AVE_MAX_LLM_CALLS_PER_PROJECT`), token-frugal
manifest digests (only usable windows + top highlights are sent), proxies for preview and
full-res only for the final render.

## Beat-sync (the differentiator)

`ave/beat/snap.py` keeps the *policy* in pure, tested Python independent of any audio lib:

- `snap_time` snaps a cut to the nearest beat **within tolerance** (default 180 ms — a cut
  200 ms off feels wrong), preferring a downbeat for major transitions when it's comparably
  close (`downbeat_bias_ms`). Outside tolerance the cut is left on its content.
- `snap_edl` snaps each segment's *timeline boundary* (what the ear judges) and absorbs the
  shift by trimming the source `in`-point, guarding against segment inversion, and records a
  `sync_map`. Deterministic: same EDL + same grid → same output.
- librosa/madmom populate the grid in production; a synthetic constant-BPM grid drives tests
  and offline fallback.

## Render backend: ffmpeg over Remotion

Chosen for M1 because the render is a pure function of the EDL and ffmpeg gives
deterministic, fast, dependency-light frame-accurate trims + concat/xfade, with captions as
burned-in libass/ASS later (M2) — no Node/Chromium in the hot path. `media/filtergraph.py`
compiles the EDL to exact `ffmpeg` argv; `build_hardcut` (concat, the default) and
`build_xfade` (progressive crossfade chain) cover the transition set. Remotion stays a
viable alternate backend for heavily animated caption/graphic styles.

## Storage & jobs

- **Storage** abstracts local FS (dev/CLI) and S3/MinIO (prod) behind one interface;
  agents only ever touch that interface, so swapping backends never touches agent code.
- **Jobs:** arq (Redis) worker runs the orchestrator; because every stage is checkpointed,
  long renders survive restarts. The API's `run`/`feedback` will enqueue onto the worker in
  M3 (the seam — `worker/tasks.py` — already exists).

## Observability

Every LLM prompt is appended to a plain-text audit log (global + per-project) *before* the
call, capturing agent, attempt, model, system, and user prompt — so every autonomous
editorial decision is traceable to the prompt that produced it.
