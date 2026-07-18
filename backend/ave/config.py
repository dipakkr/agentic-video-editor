"""Central settings, loaded from environment / .env.

Every tunable that affects cost, quality, or the graceful-degradation toggles lives
here so behaviour is reproducible and auditable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    ave_llm_model: str = "claude-sonnet-5"
    ave_max_llm_calls_per_project: int = 12

    # Storage
    ave_storage_backend: str = "local"
    ave_data_dir: Path = Path("./data")
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "ave"
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    s3_region: str = "us-east-1"

    # Datastores
    database_url: str = "postgresql+psycopg://ave:ave@localhost:5432/ave"
    redis_url: str = "redis://localhost:6379/0"

    # Media / analysis tuning
    ave_music_dir: Path = Path("./assets/music")
    ave_beat_snap_tolerance_ms: int = 180
    ave_proxy_height: int = 720
    ave_target_lufs: float = -14.0

    # Optional heavy deps (graceful degradation when off / missing)
    ave_enable_whisperx: bool = False
    ave_enable_scenedetect: bool = True
    ave_enable_mediapipe: bool = False
    ave_whisper_model: str = "base"

    @property
    def prompt_log_dir(self) -> Path:
        d = self.ave_data_dir / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()
