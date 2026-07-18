"""FastAPI application — the web surface for the pipeline.

M1 exposes the pipeline over HTTP with in-process execution and a Server-Sent-Events
progress stream. M3 swaps the in-process `run` for an enqueue onto the Redis/arq worker
(the seam is already here: `submit_pipeline`), adds the timeline/preview endpoints, and
wires the natural-language feedback loop. Endpoints are intentionally thin — all logic
lives in the agents/orchestrator so the CLI and API share one code path.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ave.config import get_settings
from ave.edl.schema import AspectRatio, Brief, Platform, Tone
from ave.orchestrator.graph import Orchestrator, PipelineState
from ave.storage.store import get_storage

app = FastAPI(title="Agentic Video Editor", version="0.1.0")

_ASPECT_FOR = {
    Platform.youtube: AspectRatio.wide,
    Platform.reels: AspectRatio.vertical,
    Platform.shorts: AspectRatio.vertical,
    Platform.tiktok: AspectRatio.vertical,
}

# In-memory progress bus (M1). M3 moves this to Redis pub/sub so multiple workers/clients
# can subscribe across processes.
_events: dict[str, list[dict]] = {}


class CreateProject(BaseModel):
    platform: Platform = Platform.youtube
    target_duration_s: float = 60.0
    tone: Tone = Tone.energetic
    music_track_id: str | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm": bool(get_settings().anthropic_api_key)}


@app.post("/projects")
def create_project(body: CreateProject) -> dict:
    pid = f"proj_{uuid.uuid4().hex[:10]}"
    settings = get_settings()
    storage = get_storage(settings)
    brief = Brief(
        platform=body.platform,
        target_duration_s=body.target_duration_s,
        tone=body.tone,
        aspect_ratio=_ASPECT_FOR.get(body.platform, AspectRatio.wide),
        music_track_id=body.music_track_id,
        auto_pick_music=body.music_track_id is None,
    )
    storage.write_json(pid, "brief.json", brief.model_dump(mode="json"))
    _events[pid] = []
    return {"project_id": pid, "brief": brief.model_dump(mode="json")}


@app.post("/projects/{pid}/clips")
async def upload_clip(pid: str, file: UploadFile) -> dict:
    settings = get_settings()
    storage = get_storage(settings)
    dst = storage.path_for(pid, f"uploads/{file.filename}")
    data = await file.read()
    Path(dst).write_bytes(data)
    return {"stored": str(dst), "bytes": len(data)}


@app.post("/projects/{pid}/run")
async def run_pipeline(pid: str) -> dict:
    """Run analysis→editorial→render. M3: enqueue onto the worker instead of awaiting."""
    settings = get_settings()
    storage = get_storage(settings)
    try:
        brief = Brief.model_validate(storage.read_json(pid, "brief.json"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"unknown project {pid}: {exc}")

    uploads_dir = storage.project_dir(pid) / "uploads"
    clips = {
        f"clip_{i:02d}": str(p)
        for i, p in enumerate(sorted(uploads_dir.glob("*")), start=1)
        if p.is_file()
    }
    if not clips:
        raise HTTPException(400, "no clips uploaded")

    _events.setdefault(pid, [])

    def on_progress(stage: str, status: str, data: dict) -> None:
        _events[pid].append({"stage": stage, "status": status, "data": data})

    orch = Orchestrator(storage, settings=settings, on_progress=on_progress)
    state = PipelineState(project_id=pid, brief=brief, clips=clips)
    # Offload the blocking pipeline to a thread so the event loop stays responsive.
    state = await asyncio.to_thread(orch.run, state)
    return {
        "project_id": pid,
        "edl_version": state.edl.version if state.edl else None,
        "render": state.render_result,
    }


@app.get("/projects/{pid}/edl")
def get_edl(pid: str) -> dict:
    storage = get_storage(get_settings())
    try:
        return storage.read_json(pid, "edl/latest.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"no EDL for {pid}: {exc}")


class Feedback(BaseModel):
    note: str


@app.post("/projects/{pid}/feedback")
async def feedback(pid: str, body: Feedback) -> dict:
    """Natural-language revision → incremental re-render (M3 wires the note into the editor)."""
    storage = get_storage(get_settings())
    try:
        edl_json = storage.read_json(pid, "edl/latest.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"no EDL for {pid}: {exc}")
    from ave.edl.schema import EDL

    edl = EDL.model_validate(edl_json)
    state = PipelineState(project_id=pid, brief=edl.brief, clips={}, edl=edl)
    orch = Orchestrator(storage, on_progress=lambda *a: _events.setdefault(pid, []).append(
        {"stage": a[0], "status": a[1], "data": a[2]}))
    state = await asyncio.to_thread(orch.apply_feedback, state, body.note)
    return {"project_id": pid, "edl_version": state.edl.version if state.edl else None}


@app.get("/projects/{pid}/events")
async def events(pid: str) -> StreamingResponse:
    """SSE stream of pipeline progress events."""

    async def gen():
        sent = 0
        for _ in range(600):  # ~cap the stream lifetime (M1)
            queue = _events.get(pid, [])
            while sent < len(queue):
                yield f"data: {json.dumps(queue[sent])}\n\n"
                sent += 1
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")
