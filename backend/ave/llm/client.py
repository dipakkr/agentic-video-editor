"""Anthropic client wrapper: schema-constrained JSON output, retries, cost caps,
and full prompt audit logging.

Every prompt sent to the model is appended to a plain-text audit log — both a global
`data/logs/prompts.txt` and a per-project `data/logs/prompts_<project>.txt`. This makes
every editorial decision traceable to the exact prompt that produced it, which is
essential for an autonomous editing agent.

If no API key is configured the client operates in `available=False` mode and callers
fall back to deterministic planners — the pipeline still runs end-to-end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import ValidationError, validate

from ave.config import Settings, get_settings


class LLMCallCapExceeded(RuntimeError):
    """Raised when a project exceeds its configured LLM call budget."""


class LLMSchemaError(RuntimeError):
    """Raised when the model cannot produce schema-valid JSON within the retry budget."""


class PromptLogger:
    """Append-only text log of every prompt passed to the LLM."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log(self, *, project_id: str, agent: str, system: str, user: str, meta: dict) -> None:
        # `new Date()`-free timestamp is fine here — this is runtime, not a workflow script.
        ts = datetime.now(timezone.utc).isoformat()
        block = (
            f"\n{'=' * 80}\n"
            f"[{ts}] project={project_id} agent={agent} "
            f"meta={json.dumps(meta, sort_keys=True)}\n"
            f"{'-' * 80}\n"
            f"SYSTEM:\n{system}\n"
            f"{'-' * 80}\n"
            f"USER:\n{user}\n"
        )
        for path in (self.log_dir / "prompts.txt", self.log_dir / f"prompts_{project_id}.txt"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(block)


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.prompt_logger = PromptLogger(self.settings.prompt_log_dir)
        self._calls: dict[str, int] = {}
        self._client = None
        if self.settings.anthropic_api_key:
            try:
                import anthropic  # type: ignore

                self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _charge(self, project_id: str) -> None:
        used = self._calls.get(project_id, 0)
        if used >= self.settings.ave_max_llm_calls_per_project:
            raise LLMCallCapExceeded(
                f"project {project_id} hit LLM cap "
                f"({self.settings.ave_max_llm_calls_per_project} calls)"
            )
        self._calls[project_id] = used + 1

    def complete_json(
        self,
        *,
        project_id: str,
        agent: str,
        system: str,
        user: str,
        schema: dict,
        max_retries: int = 2,
        max_tokens: int = 4096,
    ) -> dict:
        """Call the model and return schema-valid JSON, retrying on invalid output.

        The prompt is logged *before* the call so failed/blocked calls are still audited.
        """
        if not self.available:
            raise RuntimeError("LLM unavailable (no ANTHROPIC_API_KEY); use a fallback planner.")

        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            self._charge(project_id)
            reminder = "" if attempt == 0 else (
                f"\n\nYour previous reply was invalid: {last_err}. "
                f"Return ONLY JSON matching the schema, nothing else."
            )
            self.prompt_logger.log(
                project_id=project_id,
                agent=agent,
                system=system,
                user=user + reminder,
                meta={"attempt": attempt, "model": self.settings.ave_llm_model},
            )
            resp = self._client.messages.create(  # type: ignore[union-attr]
                model=self.settings.ave_llm_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user + reminder}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content)
            try:
                data = _extract_json(text)
                validate(instance=data, schema=schema)
                return data
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_err = exc
                continue
        raise LLMSchemaError(f"{agent}: no schema-valid output after {max_retries + 1} tries: {last_err}")


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply (tolerates ```json fences / prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model reply")
    return json.loads(text[start : end + 1])
