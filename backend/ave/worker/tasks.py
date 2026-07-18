"""Redis-backed worker (arq) — resumable pipeline jobs.

Every pipeline stage is checkpointed by the orchestrator, so a job that dies mid-render
resumes from the last completed stage rather than re-running analysis. M1 defines the job
surface; M3 routes the API's `run`/`feedback` through `enqueue` instead of running
in-process. Lives behind the `worker` extra (arq).
"""

from __future__ import annotations

from ave.config import get_settings
from ave.edl.schema import Brief
from ave.orchestrator.graph import Orchestrator, PipelineState
from ave.storage.store import get_storage


async def run_pipeline_job(ctx: dict, project_id: str, brief_json: dict, clips: dict) -> dict:
    """arq task: run the pipeline for a project. Resumable via orchestrator checkpoints."""
    settings = get_settings()
    storage = get_storage(settings)
    brief = Brief.model_validate(brief_json)

    def on_progress(stage: str, status: str, data: dict) -> None:
        # M3: publish to Redis so the API SSE endpoint can fan out to clients.
        redis = ctx.get("redis")
        if redis is not None:
            import json

            redis.publish(f"progress:{project_id}", json.dumps(
                {"stage": stage, "status": status, "data": data}))

    orch = Orchestrator(storage, settings=settings, on_progress=on_progress)
    state = PipelineState(project_id=project_id, brief=brief, clips=clips)
    state = orch.run(state)
    return {"edl_version": state.edl.version if state.edl else None, "render": state.render_result}


class WorkerSettings:
    """arq worker settings. Run with: `arq ave.worker.tasks.WorkerSettings`."""

    functions = [run_pipeline_job]

    @staticmethod
    def redis_settings():
        from arq.connections import RedisSettings

        return RedisSettings.from_dsn(get_settings().redis_url)
