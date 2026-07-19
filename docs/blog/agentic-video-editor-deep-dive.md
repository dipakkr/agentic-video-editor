# I Built an AI Video Editor Where a Team of Agents Does the Editing

## Raw clips in, publishable video out — beat-synced cuts, captions, music, and QC, with a human reviewing only at the end. Here's the full technical breakdown.

---

Video editing is the last mile of content creation, and it's brutal. You film twenty clips, and then you spend six hours scrubbing timelines: finding the good takes, cutting dead air, syncing cuts to music, styling captions, exporting three aspect ratios, writing a title and description. The creative decisions take minutes. The mechanical execution eats your day.

So I built an **agentic video editor**: you upload 2–20 raw clips plus a short brief ("YouTube, ~8 minutes, energetic"), and a team of specialist AI agents analyzes the footage, plans an edit, snaps cuts to the beat of a music track, burns in styled captions, runs quality control on its own output, and hands you platform-ready exports with titles, a chaptered description, and thumbnail candidates. You review at the end — or type natural-language feedback like *"tighten the intro and drop the third clip"* and get an incremental re-render in seconds.

This post is the full technical story: the architecture, the data structure everything hinges on, the beat-snapping math, why I chose ffmpeg over Remotion, and the engineering principles that kept an LLM-driven pipeline debuggable and deterministic.

---

## The core insight: agents need a shared document, not a shared conversation

My first instinct was the obvious one: chain LLM calls. Transcribe the clips, dump everything into a prompt, ask for an edit plan, ask another prompt to refine it. It works for a demo and falls apart immediately after — outputs drift, nothing is reproducible, and there's no way to re-render just one change.

The fix was to stop thinking about the agents and start thinking about the **artifact they collaborate on**. In professional editing, that artifact has existed for decades: the **Edit Decision List (EDL)**. So the entire system is organized around one versioned JSON document that every agent reads and mutates:

```jsonc
{
  "schema_version": "1.1.0",
  "project_id": "proj_ab12cd34ef",
  "version": 4,                       // monotonic; every revision is persisted
  "brief": { "platform": "youtube", "target_duration_s": 480,
             "tone": "energetic", "aspect_ratio": "16:9" },
  "timeline": [
    {
      "id": "seg_01",
      "source_clip": "clip_03",
      "in": 12.48, "out": 19.92,
      "transition_in": "hard",
      "cut_snapped_to_beat": true,
      "snapped_beat_s": 12.5,
      "reason": "Strongest hook — 'we almost lost the entire dataset'"
    }
  ],
  "music":    { "track_id": "energetic_128", "ducking": true,
                "sync_map": [ { "beat_s": 0.52, "timeline_s": 0.5, "is_downbeat": true } ] },
  "captions": { "style": "karaoke_bold", "language": "en" },
  "output":   { "aspect_ratio": "16:9", "width": 1920, "height": 1080,
                "fps": 30, "target_lufs": -14 }
}
```

Two design decisions here carry the whole system:

**1. Every segment has a mandatory `reason`.** The Editorial Agent must justify every cut ("strongest hook," "removes 4s of dead air"). This makes the edit explainable to the user — and, crucially, it's what lets the feedback loop later figure out *which* segment you mean when you say "cut the boring part in the middle."

**2. Rendering is a pure function of the EDL.** Same EDL + same source files → bit-identical output, every time. The EDL has a `content_hash()` that deliberately excludes metadata like `version` and `notes`, so two structurally identical edits hash the same. This one property buys determinism, de-duplication, and incremental re-rendering — more on that below.

## Architecture: an orchestrator and seven specialists

The system is an orchestrator running a graph of specialist agents:

```
User: clips + brief
        │
        ▼
┌─────────────────────────────────────────────────┐
│  Orchestrator (checkpointed state machine)      │
│                                                 │
│  Ingest & Analysis ──► Editorial (the brain)    │
│        ▲                    │                   │
│        │               Music & Beat             │
│        │                    │                   │
│   NL feedback           Captions                │
│   re-enters here            │                   │
│                          Render (ffmpeg)        │
│                             │                   │
│                            QC ──fail──► retry   │
│                             │   (max 2, routed  │
│                             ▼    to the agent   │
│                          Release   at fault)    │
└─────────────────────────────────────────────────┘
        │
        ▼
Platform-ready exports + titles/description/thumbnails
```

Every agent talks to the EDL, not to the other agents. Postgres persists every EDL version, S3/MinIO holds media, and Redis + an arq worker run the jobs. A Next.js UI streams per-agent progress over SSE and renders the timeline.

Here's what each specialist does:

**1. Ingest & Analysis** runs `ffprobe`, generates a mezzanine proxy (H.264, EBU R128 loudness-normalized), detects scenes with PySceneDetect, transcribes with WhisperX (word-level timestamps + speaker diarization), flags dead air / filler words / blur, and ranks highlight moments. It emits one *manifest* per clip — the raw material every downstream agent reasons over.

**2. Editorial** is the brain: brief + manifests in, validated EDL out. It removes dead air and filler, opens on the strongest hook within the first three seconds, keeps narrative continuity, and hits the target duration within tolerance.

**3. Music & Beat** picks a track from a royalty-free library by tone and BPM, extracts a beat grid with librosa, snaps cuts onto beats, and aligns the biggest highlight with the track's drop.

**4. Captions** turns word-level timestamps into styled karaoke/subtitle cues — burned in via libass, plus sidecar `.srt`/`.vtt`.

**5. Render** compiles the EDL into an ffmpeg filtergraph and executes it.

**6. QC** checks the *output*, not the plan: A/V sync, caption alignment, loudness, black frames, duration.

**7. Release** generates three title options, a chaptered description, hashtags, and thumbnail candidates.

## The Editorial Agent: schema-constrained LLM with a deterministic net underneath

The LLM does what LLMs are genuinely good at — narrative reasoning over transcripts: "this take is stronger," "this tangent kills the pacing," "open with this line." But I never let it produce free-form output. It must return JSON conforming to a strict schema; the client validates every response and retries with the validation error injected into the prompt:

```python
for attempt in range(max_retries + 1):
    self._charge(project_id)          # per-project cost cap
    self.prompt_logger.log(...)       # audit log, written BEFORE the call
    resp = self._client.messages.create(model=..., system=system,
                                        messages=[{"role": "user", "content": user + reminder}])
    try:
        data = _extract_json(text)
        validate(instance=data, schema=schema)
        return data
    except (json.JSONDecodeError, ValidationError) as exc:
        last_err = exc               # fed back into the next attempt's prompt
```

And if there's no API key at all — or the LLM fails every retry — a **deterministic rule-based planner** takes over: greedily select the highest-scoring usable windows, open on the best hook, drop dead air, hit the duration target. It produces a serviceable (if less clever) edit with zero external services. That fallback turned out to be the single best engineering decision in the project: the entire pipeline runs end-to-end in CI with nothing installed, and the LLM has a reproducible baseline to beat.

Two cost controls matter at scale: a per-project LLM call cap, and *token-frugal manifest digests* — the model never sees full transcripts, only usable windows and top highlights.

## Beat-syncing: the feature that makes edits feel professional

Watch any good editor work and you'll notice cuts land *on the beat*. A cut 200 ms off-beat feels subtly wrong even if you can't articulate why. This was the feature I most wanted to get right, so the snapping *policy* lives in pure, unit-tested Python, completely independent of any audio library:

```python
def snap_time(t, grid, *, tolerance_ms=180, prefer_downbeat=False, downbeat_bias_ms=90):
    """Snap `t` to the nearest beat within tolerance.

    When `prefer_downbeat` (major transitions), a downbeat is chosen over a
    closer regular beat as long as it's within tolerance and no more than
    `downbeat_bias_ms` further away. Outside tolerance we leave the cut
    untouched rather than yank it audibly off its content.
    """
```

The three rules encoded there:

1. **Snap within 180 ms, never beyond.** Beyond the tolerance, moving the cut is worse than leaving it — you'd rip it audibly off its content.
2. **Major transitions prefer downbeats.** Hard cuts and whips land on the "1" of the bar when a downbeat is comparably close, because that's where a human editor puts them.
3. **Snap the timeline boundary, not the source in-point.** The ear judges *when* in the video a cut happens, not where in the source clip. So `snap_edl` moves the timeline boundary onto the beat and absorbs the shift by trimming the source `in`-point — guarding against segment inversion — and records every snap in a `sync_map`.

librosa populates the real beat grid in production; a synthetic constant-BPM grid drives tests and the offline fallback. Deterministic either way: same EDL + same grid → same output.

## Rendering: why ffmpeg beat Remotion

I seriously considered Remotion (React-based rendering — great for animated graphics). I chose a raw **ffmpeg filtergraph compiler** instead, and the reason is the purity requirement: I needed the render to be a *deterministic function* of the EDL, with no Node/Chromium runtime in the hot path.

The compiler (`media/filtergraph.py`) turns an EDL into exact ffmpeg argv. Each segment becomes a normalize chain — trim, PTS reset, speed change, scale-and-pad (or center-crop for 9:16 reframes) — and then either a `concat` (hard cuts, the fast path) or a progressive `xfade`/`acrossfade` chain when transitions are requested.

My favorite part is the audio mixing, which does real sidechain ducking — the dialogue drives a compressor on the music bed, so music automatically dips under speech and swells back in gaps:

```python
chains.append(f"[{a_label}]asplit=2[dlg][sc]")
chains.append(
    "[music][sc]sidechaincompress="
    "threshold=0.02:ratio=12:attack=25:release=350:makeup=1[ducked]"
)
chains.append("[dlg][ducked]amix=inputs=2:duration=first:normalize=0[mixed]")
```

The whole program bus is then mastered with `loudnorm` to −14 LUFS (YouTube's target), and captions are burned in with the libass `ass` filter. One EDL fans out to 16:9, 9:16, and 1:1 exports through the same compiler.

## The feedback loop: natural language → typed edit operations

This is where the "agentic" part pays off for the user. You type *"make the intro punchier and remove the second clip"*, and the Revise Agent translates that into a small vocabulary of typed operations:

```python
class EditOp(BaseModel):
    op: Literal["remove_segment", "trim", "reorder", "retime",
                "set_transition", "change_caption_style", "set_target_duration"]
    segment_id: str | None = None
    ...
    reason: str = ""   # why this op, traced back to the user's note
```

Why an op vocabulary instead of letting the LLM rewrite the EDL directly? Because **constrained mutations can't corrupt the document**. `apply_ops` is pure — it deep-copies the EDL, applies each op with validation (a trim can never collapse a segment below 0.3 s; speeds clamp to 0.25–4×), collects invalid ops as human-readable skip messages instead of raising, and only bumps the version if something actually changed.

Then the purity of rendering kicks in:

```python
if revised.content_hash() == before:
    # nothing structurally changed → the whole pipeline short-circuits
    return state
# otherwise re-enter at music_beat: cuts moved, so beats re-snap,
# captions re-map, and render re-runs — ingest and editorial never repeat
state.stage = Stage.music_beat
return self.run(state)
```

A feedback round never re-transcribes, never re-plans from scratch, and never re-renders an unchanged timeline. Revision latency is dominated by the one thing that actually changed.

## QC: the pipeline grades its own homework

An autonomous editor that ships broken output is worse than no editor. The QC agent runs six checks on the rendered draft — duration vs. target, duplicate source overlap, caption/transcript alignment sampling, loudness, black frames, A/V sync — and the interesting part is **failure routing**: each check knows which agent is responsible, and failures re-enter the graph *at that agent's stage*:

```python
_QC_RETRY_STAGE = {
    "music": Stage.music_beat,
    "captions": Stage.captions,
    "render": Stage.render,
}
_MAX_QC_RETRIES = 2
```

Editorial failures are deliberately *not* auto-retried — an automatic re-plan could oscillate forever, while a one-line human note through the feedback loop converges immediately. And when the retry budget runs out, the user gets the draft *with* the failure report attached. Never a dead end.

## Engineering principles that made it work

Looking back, five principles did most of the heavy lifting:

**1. Graceful degradation everywhere.** ffmpeg, WhisperX, librosa, and the LLM are all optional. Every analysis pass records whether it actually ran, and downstream agents reason about missing signals instead of crashing. No API key? Deterministic planner. No ffmpeg? A resolved, replayable dry-render plan instead of a video. The pipeline *never* hard-fails on a missing dependency — which means it runs in CI, on a bare laptop, and in production with the same code path.

**2. Checkpoint every stage.** The orchestrator persists state after every node, so a crash mid-render resumes from the last completed stage instead of re-running an hour of analysis. Long renders survive worker restarts for free.

**3. Log every prompt, before the call.** Every prompt sent to the LLM is appended to a plain-text audit log — agent, attempt, model, system and user prompt — *before* the API call, so even failed or blocked calls are recorded. When the editor makes a weird cut, I can trace the exact prompt that produced it. For an autonomous system making creative decisions, this isn't observability nice-to-have; it's the debugging strategy.

**4. One code path for CLI and API.** All logic lives in the agents and orchestrator; the CLI and FastAPI app are thin drivers. Behavior can't drift between "works on my machine" and production.

**5. Keep the clever parts pure and tested.** The beat-snap policy, the EDL→filtergraph compiler, and `apply_ops` are all pure functions with no I/O and no ML dependencies. The test suite covers tolerance windows, downbeat preference, hash determinism, "unchanged hash skips render," and a full end-to-end run with every optional dependency absent.

## What I'd tell you if you're building something similar

- **Design the shared artifact first, the agents second.** The EDL schema was the highest-leverage design work in the project. Agents came and went; the document's invariants (versioning, reasons, content hash) never changed.
- **LLMs propose, code disposes.** Every LLM output in the system is either schema-validated JSON or a constrained op vocabulary applied by pure code. The model never touches state directly.
- **A deterministic fallback isn't a compromise — it's infrastructure.** It gives you CI, a baseline, offline dev, and an honest answer to "what does the LLM actually add?"
- **Purity is a superpower in media pipelines.** "Render is a function of the EDL" sounds academic until it hands you content-addressed caching, incremental re-renders, and reproducible bug reports.

## What's next

The current roadmap: b-roll intelligence (classify speech-heavy clips as A-roll, place cutaways over long talking segments — with reasons, of course), title cards and lower thirds via an overlay track in EDL v1.1, subject-tracked reframing for vertical exports, and a full per-agent customization surface so users can pin a music track, set duck depth, or swap caption presets from the brief.

The one thing that will never change: the publish step **always requires explicit human confirmation**. Agents plan, cut, mix, caption, and QC — but a human presses the button.

---

*The project is open source — the EDL schema, the beat-snap policy, the filtergraph compiler, and the orchestrator are all readable in an afternoon. If you're building agentic systems for creative work, I'd love to hear what shared artifact* your *agents collaborate on.*
