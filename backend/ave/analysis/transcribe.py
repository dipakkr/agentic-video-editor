"""Speech transcription with word-level timestamps + diarization.

WhisperX is the intended engine. It's heavy (torch + models), so it's fully optional:
when disabled or unavailable we return an empty transcript and the Editorial Agent
falls back to energy/motion-based editing. This is the graceful-degradation contract —
never fail the pipeline for a missing optional feature.
"""

from __future__ import annotations

from ave.analysis.manifest import TranscriptSegment, Word

# Common English filler tokens flagged for optional removal by the editor.
FILLER_WORDS = {"um", "uh", "erm", "ah", "like", "you know", "i mean", "sort of", "kind of"}


def transcribe(
    path: str,
    *,
    enabled: bool = False,
    model_name: str = "base",
    language: str = "en",
) -> tuple[list[TranscriptSegment], bool]:
    """Return (segments, ran). `ran` is False when transcription was skipped."""
    if not enabled:
        return [], False
    try:
        import whisperx  # type: ignore
    except Exception:
        return [], False

    try:
        device = "cpu"
        model = whisperx.load_model(model_name, device, compute_type="int8")
        audio = whisperx.load_audio(path)
        result = model.transcribe(audio, language=language)
        align_model, meta = whisperx.load_align_model(language_code=language, device=device)
        result = whisperx.align(result["segments"], align_model, meta, audio, device)
        segments = _to_segments(result.get("segments", []))
        return segments, True
    except Exception:
        return [], False


def _to_segments(raw: list[dict]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for seg in raw:
        words = [
            Word(
                word=w.get("word", "").strip(),
                start_s=float(w.get("start", seg.get("start", 0.0))),
                end_s=float(w.get("end", seg.get("end", 0.0))),
                speaker=w.get("speaker"),
                is_filler=w.get("word", "").strip().lower() in FILLER_WORDS,
            )
            for w in seg.get("words", [])
            if "start" in w
        ]
        segments.append(
            TranscriptSegment(
                start_s=float(seg.get("start", 0.0)),
                end_s=float(seg.get("end", 0.0)),
                text=seg.get("text", "").strip(),
                speaker=seg.get("speaker"),
                words=words,
            )
        )
    return segments
