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

# The Next.js dev/preview UI is a separate origin; progress SSE + uploads need CORS.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/projects/{pid}/edl/versions")
def list_edl_versions(pid: str) -> dict:
    """Every EDL revision is persisted; expose the version history for the UI."""
    storage = get_storage(get_settings())
    edl_dir = storage.project_dir(pid) / "edl"
    if not edl_dir.exists():
        raise HTTPException(404, f"no EDLs for {pid}")
    versions = sorted(
        int(p.stem[1:]) for p in edl_dir.glob("v*.json") if p.stem[1:].isdigit()
    )
    return {"project_id": pid, "versions": versions}


@app.get("/projects/{pid}/edl/versions/{version}")
def get_edl_version(pid: str, version: int) -> dict:
    storage = get_storage(get_settings())
    try:
        return storage.read_json(pid, f"edl/v{version}.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"no EDL v{version} for {pid}: {exc}")


@app.get("/projects/{pid}/renders/latest")
def latest_render(pid: str):
    """Serve the most recent rendered file for the preview player."""
    from fastapi.responses import FileResponse

    storage = get_storage(get_settings())
    renders = storage.project_dir(pid) / "renders"
    if renders.exists():
        files = sorted(renders.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if files:
            return FileResponse(str(files[-1]), media_type="video/mp4")
    raise HTTPException(404, "no render yet")


@app.get("/projects/{pid}/qc")
def get_qc_report(pid: str) -> dict:
    """Latest QC report (highest EDL version with a persisted report)."""
    storage = get_storage(get_settings())
    qc_dir = storage.project_dir(pid) / "qc"
    if qc_dir.exists():
        reports = sorted(qc_dir.glob("report_v*.json"),
                         key=lambda p: int(p.stem.split("v")[-1]))
        if reports:
            import json as _json

            return _json.loads(reports[-1].read_text())
    raise HTTPException(404, f"no QC report for {pid}")


@app.get("/projects/{pid}/release")
def get_release_kit(pid: str) -> dict:
    """Latest release kit (titles, description with chapters, hashtags, thumbnails)."""
    storage = get_storage(get_settings())
    rel_dir = storage.project_dir(pid) / "release"
    if rel_dir.exists():
        kits = sorted(rel_dir.glob("kit_v*.json"),
                      key=lambda p: int(p.stem.split("v")[-1]))
        if kits:
            import json as _json

            return _json.loads(kits[-1].read_text())
    raise HTTPException(404, f"no release kit for {pid}")


@app.get("/projects/{pid}/artifacts")
def list_artifacts(pid: str) -> dict:
    """Everything the pipeline produced, for the UI's artifact browser."""
    storage = get_storage(get_settings())
    root = storage.project_dir(pid)
    if not root.exists():
        raise HTTPException(404, f"unknown project {pid}")
    files = [
        str(p.relative_to(root))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    return {"project_id": pid, "files": files}


class Feedback(BaseModel):
    note: str


@app.post("/projects/{pid}/feedback")
async def feedback(pid: str, body: Feedback) -> dict:
    """Natural-language revision → Revise Agent → incremental downstream re-run."""
    settings = get_settings()
    storage = get_storage(settings)
    try:
        edl_json = storage.read_json(pid, "edl/latest.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"no EDL for {pid}: {exc}")
    from ave.edl.schema import EDL

    edl = EDL.model_validate(edl_json)
    _events.setdefault(pid, [])
    orch = Orchestrator(
        storage, settings=settings,
        on_progress=lambda s, st, d: _events[pid].append(
            {"stage": s, "status": st, "data": d}),
    )
    state = PipelineState(project_id=pid, brief=edl.brief, clips={}, edl=edl)
    state = await asyncio.to_thread(orch.apply_feedback, state, body.note)
    return {
        "project_id": pid,
        "edl_version": state.edl.version if state.edl else None,
        "render": state.render_result,
    }


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
