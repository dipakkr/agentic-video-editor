"""S3-compatible storage backend (MinIO in docker-compose, any S3 in prod).

Local files are staged under a working dir then mirrored to the bucket, so agents that
expect real filesystem paths (ffmpeg needs them) keep working. Lives behind the `worker`
extra (`boto3`); imported lazily from `store.get_storage`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ave.config import Settings


class S3Storage:
    def __init__(self, settings: Settings):
        import boto3  # type: ignore

        self.settings = settings
        self.bucket = settings.s3_bucket
        self.work = settings.ave_data_dir / "s3cache"
        self.work.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region,
        )
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            self.client.create_bucket(Bucket=self.bucket)

    def _key(self, project_id: str, rel: str) -> str:
        return f"projects/{project_id}/{rel}"

    def project_dir(self, project_id: str) -> Path:
        d = self.work / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path_for(self, project_id: str, rel: str) -> Path:
        p = self.project_dir(project_id) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_file(self, project_id: str, rel: str, src: str | Path) -> str:
        key = self._key(project_id, rel)
        self.client.upload_file(str(src), self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def write_json(self, project_id: str, rel: str, obj: dict) -> str:
        local = self.path_for(project_id, rel)
        local.write_text(json.dumps(obj, indent=2))
        self.client.put_object(
            Bucket=self.bucket, Key=self._key(project_id, rel),
            Body=json.dumps(obj, indent=2).encode(), ContentType="application/json",
        )
        return f"s3://{self.bucket}/{self._key(project_id, rel)}"

    def read_json(self, project_id: str, rel: str) -> dict:
        resp = self.client.get_object(Bucket=self.bucket, Key=self._key(project_id, rel))
        return json.loads(resp["Body"].read())
