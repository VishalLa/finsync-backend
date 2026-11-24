from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_container_runtime() -> bool:
    return os.path.exists("/.dockerenv") or os.path.isdir("/data")


def _default_storage_dir() -> str:
    if os.environ.get("STORAGE_DIR"):
        return os.environ["STORAGE_DIR"]
    if os.environ.get("BILL_UPLOAD_FOLDER"):
        return os.environ["BILL_UPLOAD_FOLDER"]
    if _is_container_runtime():
        return "/data/uploads"
    return str(Path(__file__).resolve().parents[2] / "storage")


def _default_ollama_url() -> str:
    host = "ollama" if _is_container_runtime() else "127.0.0.1"
    return f"http://{host}:11434"


class Settings(BaseSettings):
    REDIS_URL: str
    STORAGE_DIR: str = Field(default_factory=_default_storage_dir)

    OLLAMA_NAME: str = "phi3:latest"
    OLLAMA_URL: str = Field(default_factory=_default_ollama_url)

    Q_ENTRY: str = "entry_point.queue"
    Q_OCR: str = "ocr.queue"
    Q_MATCHER: str = "matcher.queue"
    Q_AI: str = "ai.queue"
    Q_LEDGER: str = "ledger.queue"
    Q_SERVICE: str = "service.queue"
    Q_DB: str = "db_storage.queue"
    Q_REPORT: str = "report.queue"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        os.makedirs(self.STORAGE_DIR, exist_ok=True)

        if self.OLLAMA_URL:
            parsed_ollama = urlparse(self.OLLAMA_URL)
            if parsed_ollama.hostname in {
                "0.0.0.0",
                "127.0.0.1",
                "localhost",
                "ollama_server",
                "ollama",
            }:
                host = "ollama" if _is_container_runtime() else "127.0.0.1"
                port = parsed_ollama.port or 11434
                self.OLLAMA_URL = urlunparse(
                    parsed_ollama._replace(netloc=f"{host}:{port}")
                )
                
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class RateLimitSettings(BaseSettings):
    RATELIMIT_ENABLED: bool = True
    RATELIMIT_REDIS_URL: Optional[str] = None
    RATELIMIT_REDIS_DB: int = 1
    RATELIMIT_DEFAULT: str = "200 per minute"
    RATELIMIT_AUTH: str = "5 per minute"
    RATELIMIT_HEAVY: str = "10 per hour"
    RATELIMIT_NORMAL: str = "60 per minute"
    RATELIMIT_POLL: str = "120 per minute"
    MAX_ACTIVE_RUNS_PER_USER: int = 3
    ACTIVE_RUN_TTL_SECONDS: int = 7200

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )
    
@lru_cache(maxsize=1)
def rate_limit_settings() -> RateLimitSettings:
    return RateLimitSettings()


class APISettings(Settings):
    DEBUG: bool = False
    SECRET_KEY: str
    JWT_SECRET_KEY: str

    @classmethod
    def from_env(cls) -> "APISettings":
        return cls()

