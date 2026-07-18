"""Publishing adapter — ALWAYS behind an explicit user confirmation.

Non-negotiable product rule: the system never auto-publishes. Every code path here
requires `confirm=True` passed explicitly by a human-initiated call (CLI `--confirm`,
or the UI's confirmation dialog). Anything less raises `PublishNotConfirmed` before any
network interaction could occur.

The YouTube upload itself uses the YouTube Data API v3 with per-user OAuth. The heavy
client (`google-api-python-client` + `google-auth-oauthlib`) is an optional dependency;
without it — or without credentials — we fail with actionable setup instructions rather
than pretending to publish.
"""

from __future__ import annotations

from pathlib import Path


class PublishNotConfirmed(RuntimeError):
    """Raised when a publish is attempted without explicit user confirmation."""


class PublishNotConfigured(RuntimeError):
    """Raised when OAuth credentials / client libraries are missing."""


def publish_youtube(
    video_path: str,
    *,
    title: str,
    description: str,
    tags: list[str] | None = None,
    confirm: bool = False,
    client_secrets_path: str | None = None,
    privacy_status: str = "private",
) -> dict:
    """Upload a rendered video to YouTube. Requires explicit confirmation.

    Returns {"video_id": ..., "url": ...} on success. Uploads default to `private` so
    even a confirmed publish never goes live without a second, human step on YouTube.
    """
    if not confirm:
        raise PublishNotConfirmed(
            "Publishing requires explicit confirmation. Re-run with confirm=True "
            "(CLI: ave publish --confirm). The system never auto-publishes."
        )
    if not Path(video_path).exists():
        raise FileNotFoundError(f"render not found: {video_path}")
    if not client_secrets_path or not Path(client_secrets_path).exists():
        raise PublishNotConfigured(
            "YouTube publishing needs OAuth client secrets. Create an OAuth client in "
            "Google Cloud Console (YouTube Data API v3), download client_secrets.json, "
            "and pass its path (CLI: --client-secrets)."
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional heavy dependency
        raise PublishNotConfigured(
            "Install the YouTube client libraries: pip install "
            "google-api-python-client google-auth-oauthlib"
        ) from exc

    # pragma: no cover — network path, exercised only in a configured environment.
    flow = InstalledAppFlow.from_client_secrets_file(
        client_secrets_path, scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    creds = flow.run_local_server(port=0)
    youtube = build("youtube", "v3", credentials=creds)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or [],
                "categoryId": "22",
            },
            "status": {"privacyStatus": privacy_status},
        },
        media_body=MediaFileUpload(video_path, chunksize=-1, resumable=True),
    )
    response = request.execute()
    video_id = response.get("id", "")
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
