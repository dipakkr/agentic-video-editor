"""Storage abstraction for uploads, proxies, renders, and EDL revisions.

`local` writes under the data dir (default for dev/CLI). `s3` targets any
S3-compatible endpoint (MinIO in docker-compose). Rendering only ever reads from and
writes to this interface, so swapping backends never touches agent code.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

from ave.config import Settings


class Storage(Protocol):
    def project_dir(self, project_id: str) -> Path: ...
    def put_file(self, project_id: str, rel: str, src: str | Path) -> str: ...
    def path_for(self, project_id: str, rel: str) -> Path: ...
    def write_json(self, project_id: str, rel: str, obj: dict) -> str: ...
    def read_json(self, project_id: str, rel: str) -> dict: ...


class LocalStorage:
    """Filesystem-backed storage rooted at `data_dir`."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)

    def project_dir(self, project_id: str) -> Path:
        d = self.root / "projects" / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, project_id: str, rel: str) -> Path:
        p = self.project_dir(project_id) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_file(self, project_id: str, rel: str, src: str | Path) -> str:
        dst = self.path_for(project_id, rel)
        shutil.copy2(src, dst)
        return str(dst)

    def write_json(self, project_id: str, rel: str, obj: dict) -> str:
        p = self.path_for(project_id, rel)
        p.write_text(json.dumps(obj, indent=2))
        return str(p)

    def read_json(self, project_id: str, rel: str) -> dict:
        return json.loads(self.path_for(project_id, rel).read_text())


def get_storage(settings: Settings) -> Storage:
    if settings.ave_storage_backend == "s3":
        # boto3-backed backend lives in the `worker` extra; import lazily so the core
        # install stays lean and local dev never needs S3.
        from ave.storage.s3 import S3Storage  # noqa: PLC0415

        return S3Storage(settings)
    return LocalStorage(settings.ave_data_dir)
