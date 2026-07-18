# I Built an Agentic Video Editor That Turns 20 Raw Clips Into a Publishable Video

*Raw footage in → beat-synced, captioned, b-rolled, QC-checked video out — with AI agents doing every editorial job, and a human only at the final review. Here's the full technical story: the architecture, the hard problems, and how the entire codebase was itself built by a manager agent orchestrating specialist sub-agents.*

---

Video editing is the most repetitive creative work I know. You shoot 20 clips, and then you spend hours doing largely mechanical things: cutting dead air, hunting for the strongest opening moment, laying music under dialogue, nudging cuts onto beats, typing captions, cropping for Reels. Every one of those steps has *rules*. And anything with rules is automatable — if you can get the architecture right.

So I built an **agentic video editor**: upload 15–20 clips plus a one-line brief (platform, length, tone), and a team of specialist AI agents analyzes the footage, plans an edit, syncs it to music, captions it, quality-checks itself, and hands you a draft with export presets and YouTube metadata. You review at the end, give feedback in plain English ("make the intro punchier, remove the last segment"), and only the affected parts re-render.

This post is the full technical breakdown. Six pull requests, 281 tests, and one architectural decision that made everything else possible.

---

## The one decision that mattered: the EDL is the single source of truth

Every agent system lives or dies by its shared state. Mine is an **Edit Decision List (EDL)** — one versioned JSON document that describes the entire edit:

```jsonc
{
  "schema_version": "1.1.0",
  "project_id": "proj_ab12cd34ef",
  "version": 4,                      // monotonic; EVERY revision is persisted
  "brief": { "platform": "youtube", "target_duration_s": 60, "tone": "energetic" },
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
  "overlays":  [ /* b-roll cutaways, each with a reason */ ],
  "graphics":  { "title_card": { "text": "we almost lost the entire dataset…" } },
  "music":     { "track_id": "energetic_128", "offset_s": 95.0, "ducking": true,
                 "sync_map": [ { "beat_s": 3.0, "timeline_s": 3.0, "is_downbeat": true } ] },
  "captions":  { "style": "karaoke_bold" },
  "output":    { "aspect_ratio": "16:9", "target_lufs": -14, "reframe": "pad" }
}
```

Three properties of this document carry the whole system:

**1. Rendering is a pure function of the EDL.** The render agent compiles the EDL into an exact ffmpeg filtergraph — same EDL + same assets ⇒ byte-identical output. No hidden state, no "it worked on my machine."

**2. Every segment carries a mandatory `reason`.** The editorial agent must justify every cut ("Strongest hook — opens on the highest-energy moment"). This makes the edit *explainable* — the UI shows a "Cut decisions" list — and it makes natural-language feedback tractable, because the revision agent can locate "the part where I'm setting up the camera" by reading reasons and transcripts.

**3. Content-hashing gives you incremental re-rendering for free.** The EDL's `content_hash()` excludes the version number and notes. So when a user's feedback produces a structurally identical EDL, the hash matches and *nothing* re-renders. When one segment changes, only the stages downstream of the timeline change re-run — analysis never repeats.

```python
def content_hash(self) -> str:
    payload = self.model_dump(mode="json", by_alias=True,
                              exclude={"version", "notes"})
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()
```

## The agent team

The system is an **orchestrator + specialist agents** graph — a checkpointed state machine where each node reads and mutates the EDL:

```
ingest → editorial → b-roll → graphics → music+beat → captions → render → QC → release
              ↑                                                            |
              └────────── NL feedback / QC retry routing ──────────────────┘
```

- **Ingest & Analysis** probes every clip (ffprobe), builds normalized proxies (H.264 + EBU R128 loudness), detects shots (PySceneDetect), transcribes with word-level timestamps (WhisperX), flags dead air and filler words, and ranks *highlights* — candidate strong moments scored by information density and energy.
- **Editorial** (the brain) turns the brief + analysis manifests into the EDL: opens on the strongest hook inside 3 seconds, drops dead air and filler, keeps narrative continuity, hits the target duration ±10%.
- **B-roll** classifies clips by speech density (≥0.5 words/sec = A-roll) and plans full-frame cutaways over long talking segments — round-robin across the b-roll pool, never reusing a source window.
- **Graphics** plans an opening title card from the hook line. (It deliberately *refuses* to invent lower-third name straps when diarization can't supply a real name — an agent that fabricates graphics is worse than no agent.)
- **Music & Beat** auto-picks a royalty-free track by tone and BPM, builds a beat grid, snaps cuts to it, and aligns the track's drop with the strongest visual moment.
- **Captions** generates karaoke-style word captions for Shorts/Reels or clean subtitles for YouTube — burned in via ASS/libass, plus `.srt`/`.vtt` sidecars.
- **Render** compiles everything to one deterministic ffmpeg filtergraph.
- **QC** runs six checks (duration, segment bounds, duplicate footage, beat alignment, caption/transcript alignment, loudness) and *routes failures back to the responsible agent* — max two retry loops, then the findings surface to the human alongside the draft.
- **Release** writes three title options, a chaptered description from the transcript, hashtags, and thumbnail candidates. Publishing to YouTube exists — but it is hard-gated behind explicit confirmation and uploads private. The system can never auto-publish.

## Beat-synced cuts: the differentiator

A cut that lands 200 ms off the beat *feels* wrong even to viewers who can't say why. This was the feature I refused to compromise on, so the snapping policy is pure, dependency-free Python with its own test suite:

```python
def snap_time(t, grid, *, tolerance_ms=180,
              prefer_downbeat=False, downbeat_bias_ms=90):
    """Snap t to the nearest beat within tolerance. Major transitions
    prefer a downbeat when one is comparably close. Outside tolerance,
    leave the cut on its content — never yank it audibly off."""
```

The rules that took real iteration:

1. **Snap the *timeline* boundary, not the source in/out point** — the ear judges where the cut lands in the program, not in the source clip. The shift is absorbed by trimming the source in-point, with a guard against inverting the segment.
2. **Downbeats win for major transitions** — but only within a bias window (90 ms). A downbeat 400 ms away must not beat a regular beat 50 ms away.
3. **Outside the tolerance window (±180 ms), don't snap at all.** A cut placed for content reasons is better than a cut dragged audibly off its content.
4. **Align the drop.** The music agent finds the track's energy peak and offsets playback so the drop lands on the strongest visual moment: `offset = max(0, drop_time − hook_timeline_position)`.

When librosa is available, the grid comes from real beat tracking; otherwise a synthetic constant-BPM grid from the track's metadata keeps everything working — which brings me to the pattern that shaped the whole codebase.

## Every agent has a deterministic fallback

The LLM is the *narrative brain*, not the skeleton. Every agent that calls a model follows the same contract:

1. All LLM output is **schema-constrained JSON** — validated, rejected, and retried on mismatch (max 2 retries).
2. On *any* failure — no API key, network error, invalid output after retries — the agent falls through to a **deterministic rule-based implementation** that produces a valid, reasonable result.
3. Every optional heavy dependency (ffmpeg, WhisperX, librosa, MediaPipe) degrades the same way: the analysis manifest records which passes actually ran, and downstream agents reason with what exists.

This bought me three things. The pipeline runs end-to-end in CI with *zero* external services. The deterministic path is a reproducible baseline the LLM path has to beat. And no user ever sees a dead pipeline because one optional feature hiccuped.

Cost control rides the same rails: a hard cap on LLM calls per project, token-frugal manifest digests (the model sees usable windows and top highlights, not raw transcripts), low-res proxies for previews, and full-res only on final export.

There's one more piece of discipline here: **every prompt sent to the model is appended to a plain-text audit log before the call is made** — agent name, attempt number, model, system prompt, user prompt. When an autonomous system is making editorial decisions about your footage, you want a paper trail of exactly what it was asked.

## The feedback loop: minimal ops, minimal re-work

"Make the intro punchier and remove the last segment" becomes a typed list of edit operations:

```python
op: Literal["remove_segment", "trim", "reorder", "retime",
            "set_transition", "change_caption_style", "set_target_duration"]
```

The revision agent's LLM path emits these ops (schema-constrained, minimal-change instructions); its fallback parses keywords deterministically. A pure `apply_ops` function applies them with guards — a trim can't collapse a segment below 0.3s, a removal can't empty the timeline — and every touched segment's `reason` gets annotated with why it changed.

Then the incremental machinery kicks in: unchanged content hash → nothing re-runs. Changed timeline → re-enter the graph at the b-roll stage (cutaway positions are stale), replan downstream, re-render. Ingest and the original editorial pass never repeat. Rendered outputs are cached by content hash, so even a revision that's later undone costs nothing the second time.

## The meta-story: this codebase was built by agents, too

Here's the part I find funniest: the development process mirrored the product's architecture.

I ran the build as a **manager agent** that planned six milestone phases, then delegated each feature to **specialist sub-agents** — one wrote the music/beat module against a strict interface contract, another the captions module, another the QC checks, another the React UI. The manager wrote the integration seams (orchestrator, filtergraph, API), reviewed every delivery against its contract, ran the full suite, and merged one PR per phase.

Some numbers from the run:

| Phase | Scope | Tests after merge |
|-------|-------|------------------|
| M1 | Pipeline core: analysis → EDL → ffmpeg rough cut | 29 |
| M2 | Music auto-pick + beat-sync + captions in render | 100 |
| M3 | NL feedback → edit ops + incremental re-render + web UI | 165 |
| M4 | QC gate + release kit + multi-aspect export + publish gate | 239 |
| M5 | B-roll intelligence + graphics + per-agent customization | 281 |
| M6 | 15–20-clip end-to-end demo + CI | 281 |

The same disciplines applied to the build as to the product. Sub-agents got interface *contracts* ("this signature is a contract — do not rename"), not vibes. Every delegation prompt was logged verbatim to a text file, exactly like the product logs its LLM prompts. And the sub-agents caught real bugs I'd have missed — one refused to mark a flaky test as unrelated and instead diagnosed precisely how a downstream graphics pass was clobbering feedback provenance notes.

The best bug of the build surfaced from a smoke test, not a unit test: the editorial fallback *discarded* any footage window overlapping the chosen hook, so a single-shot talking clip contributed only its 6-second hook — which quietly starved the b-roll agent of long talking segments to cut away from. The fix (subtract the hook window, keep the remainders) is two lines. Finding it required running the *whole* system on realistic input. Agents wrote the modules; end-to-end runs found the truth.

## What the demo produces

One command — `python scripts/demo.py --clips 18` — runs the full promise: 18 clips, target 60 seconds, energetic, YouTube.

The result: a **59.75s edit** (within ±10%), opening on the strongest hook with a title card, **6 b-roll cutaways** placed over long talking segments, all **4 cuts snapped to a 128 BPM grid** (2 on downbeats), music ducked −14 dB under dialogue with the drop aligned to the hook, karaoke captions with sidecar files, **six QC checks passed**, three title options, a chaptered description, hashtags, and thumbnail candidates — every single decision carrying a human-readable reason.

## Lessons

1. **Make the shared state a document, not a database of fragments.** One versioned EDL made determinism, caching, explainability, and multi-agent coordination all fall out of a single design choice.
2. **Pure functions at the boundaries.** EDL → filtergraph, ops → EDL, cues → subtitles: every pure boundary became trivially testable, which is why 281 tests run in under a second.
3. **LLMs propose, schemas dispose.** Never let free-form model output touch your state. Validate, retry, and always have a deterministic floor.
4. **Reasons are a feature.** Forcing the system to justify every cut cost one string field and paid for itself in the UI, the feedback loop, and debugging.
5. **The last 200 ms is the product.** Anyone can concat clips. The difference between "AI-generated video" and something you'd publish is beat alignment, ducking curves, caption safe zones — the details with numbers attached.

The repo is milestone-by-milestone reproducible — each phase was its own PR with its own tests, and the whole pipeline runs with zero external dependencies (it degrades to planning + deterministic render plans without ffmpeg, and upgrades itself when real media tools are present).

Raw clips in, publishable video out. The human's job is now the only part that was ever really creative: deciding whether it's *good*.

---

*Built with Python/FastAPI, Pydantic, ffmpeg, librosa, WhisperX, PySceneDetect, Next.js/TypeScript, and the Anthropic API — orchestrated end-to-end by Claude Code agents under human direction.*
