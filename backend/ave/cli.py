"""`ave` CLI — trigger the M1 pipeline end-to-end from the terminal.

Examples:
    ave run clip1.mp4 clip2.mp4 --platform youtube --duration 60 --tone energetic
    ave validate path/to/edl.json
    ave schema            # dump the EDL JSON schema
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ave.config import get_settings
from ave.edl.schema import EDL, AspectRatio, Brief, Platform, Tone, json_schema
from ave.orchestrator.graph import Orchestrator, PipelineState
from ave.storage.store import get_storage

app = typer.Typer(add_completion=False, help="Agentic Video Editor — raw clips in, publishable video out.")
console = Console()

_ASPECT_FOR = {
    Platform.youtube: AspectRatio.wide,
    Platform.reels: AspectRatio.vertical,
    Platform.shorts: AspectRatio.vertical,
    Platform.tiktok: AspectRatio.vertical,
}


@app.command()
def run(
    clips: list[Path] = typer.Argument(..., help="Input video clips (2–20)."),
    platform: Platform = typer.Option(Platform.youtube, help="Target platform."),
    duration: float = typer.Option(60.0, help="Target duration (seconds)."),
    tone: Tone = typer.Option(Tone.energetic, help="Desired tone."),
    music: str = typer.Option(None, help="Music track id (omit for auto-pick)."),
    no_music: bool = typer.Option(False, "--no-music", help="Skip the music/beat-sync pass."),
    caption_style: str = typer.Option(
        None, help="Force a caption style: karaoke_bold | phrase_pop | clean_subtitle | none."
    ),
    project_id: str = typer.Option(None, help="Reuse an existing project id (resume)."),
):
    """Analyze clips, plan an edit, and render a rough cut (M1)."""
    settings = get_settings()
    storage = get_storage(settings)
    pid = project_id or f"proj_{uuid.uuid4().hex[:10]}"

    missing = [str(c) for c in clips if not c.exists()]
    if missing:
        console.print(f"[red]Missing files:[/red] {missing}")
        raise typer.Exit(1)

    brief = Brief(
        platform=platform,
        target_duration_s=duration,
        tone=tone,
        aspect_ratio=_ASPECT_FOR.get(platform, AspectRatio.wide),
        music_track_id=music,
        auto_pick_music=music is None,
    )
    clip_map = {f"clip_{i:02d}": str(c.resolve()) for i, c in enumerate(clips, start=1)}

    console.print(f"[bold cyan]Project[/bold cyan] {pid}  ·  {len(clip_map)} clips  ·  {platform.value} · {duration}s")

    def progress(stage: str, status: str, data: dict) -> None:
        console.print(f"  [dim]{stage:<10}[/dim] [yellow]{status}[/yellow] "
                      f"{json.dumps({k: v for k, v in data.items() if k != 'features'})}")

    orch = Orchestrator(storage, settings=settings, on_progress=progress)
    state = PipelineState(
        project_id=pid, brief=brief, clips=clip_map,
        no_music=no_music, caption_style=caption_style,
    )
    try:
        state = orch.run(state)
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[red]Pipeline error:[/red] {exc}")
        console.print("[dim]Tip: install ffmpeg + the media/ml extras for full analysis "
                      "and rendering. See README → Quick start.[/dim]")
        raise typer.Exit(1)

    edl = state.edl
    assert edl is not None
    _print_edl(edl)
    console.print(f"\n[green]Render:[/green] {json.dumps(state.render_result, indent=2)}")
    console.print(f"[dim]Artifacts under[/dim] {storage.project_dir(pid)}")


@app.command()
def export(
    project_id: str = typer.Argument(..., help="Project id with a completed pipeline run."),
    preset: list[str] = typer.Option(
        None, help="Export presets (repeatable): youtube shorts reels tiktok square. "
                   "Default: the project's brief platform."
    ),
):
    """Render platform export variants (16:9 / 9:16 / 1:1) from the project's latest EDL."""
    from ave.media.exports import EXPORT_PRESETS, export_all
    from ave.orchestrator.graph import Orchestrator

    settings = get_settings()
    storage = get_storage(settings)
    try:
        edl = EDL.model_validate(storage.read_json(project_id, "edl/latest.json"))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]No EDL for {project_id}:[/red] {exc}")
        raise typer.Exit(1)
    bad = [p for p in (preset or []) if p not in EXPORT_PRESETS]
    if bad:
        console.print(f"[red]Unknown presets:[/red] {bad} · available: {sorted(EXPORT_PRESETS)}")
        raise typer.Exit(1)
    manifests = Orchestrator(storage, settings=settings).load_manifests(project_id)
    results = export_all(edl, manifests, project_id, storage, presets=preset or None)
    for name, res in results.items():
        status = "rendered" if res.get("executed") else "dry plan"
        console.print(f"  [green]{name:<8}[/green] {status:<9} "
                      f"{res.get('duration_s')}s  {res.get('output_path') or res.get('plan_path')}")


@app.command()
def publish(
    project_id: str = typer.Argument(...),
    video: Path = typer.Option(..., help="Rendered file to upload."),
    title: str = typer.Option(..., help="Video title."),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="REQUIRED. Publishing never happens without this explicit flag.",
    ),
    client_secrets: Path = typer.Option(None, help="OAuth client_secrets.json path."),
):
    """Upload to YouTube — always requires --confirm; uploads default to private."""
    from ave.agents.publish import PublishNotConfigured, PublishNotConfirmed, publish_youtube

    settings = get_settings()
    storage = get_storage(settings)
    description = ""
    try:
        kit = storage.read_json(project_id, f"release/kit_v"
                                            f"{EDL.model_validate(storage.read_json(project_id, 'edl/latest.json')).version}.json")
        description = kit.get("description", "")
    except Exception:  # noqa: BLE001 — kit is optional for publish
        pass
    try:
        result = publish_youtube(
            str(video), title=title, description=description,
            confirm=confirm,
            client_secrets_path=str(client_secrets) if client_secrets else None,
        )
        console.print(f"[green]Uploaded (private):[/green] {result['url']}")
    except (PublishNotConfirmed, PublishNotConfigured) as exc:
        console.print(f"[red]Not published:[/red] {exc}")
        raise typer.Exit(1)


@app.command()
def validate(edl_path: Path):
    """Validate an EDL JSON file against the schema."""
    try:
        edl = EDL.model_validate_json(edl_path.read_text())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]INVALID[/red]: {exc}")
        raise typer.Exit(1)
    console.print(f"[green]VALID[/green] · v{edl.version} · {len(edl.timeline)} segments "
                  f"· {edl.total_duration_s}s · hash {edl.content_hash()[:12]}")


@app.command()
def schema(out: Path = typer.Option(None, help="Write schema to this path instead of stdout.")):
    """Emit the EDL JSON Schema."""
    text = json.dumps(json_schema(), indent=2)
    if out:
        out.write_text(text)
        console.print(f"wrote {out}")
    else:
        console.print_json(text)


def _print_edl(edl: EDL) -> None:
    table = Table(title=f"EDL v{edl.version} · {edl.total_duration_s}s · within target: {edl.within_target()}")
    table.add_column("seg"); table.add_column("clip"); table.add_column("in→out")
    table.add_column("trans"); table.add_column("reason", overflow="fold")
    for s in edl.timeline:
        table.add_row(s.id, s.source_clip, f"{s.in_:.2f}→{s.out:.2f}",
                      s.transition_in.value, s.reason)
    console.print(table)


if __name__ == "__main__":
    app()
