from __future__ import annotations

import os
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from pydantic import Field
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()


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


class Config(Settings):
    DEBUG: bool = False

    DATABASE_URL: Optional[str] = None
    POSTGRES_USER: Optional[str] = None
    POSTGRES_PASSWORD: Optional[str] = None
    POSTGRES_HOST: Optional[str] = None
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: Optional[str] = None
    POOL_WORKERS: Optional[int] = None

    SECRET_KEY: str
    JWT_SECRET_KEY: str
    ALGORITHM: str = "HS256"

    SQLALCHEMY_SYNC_DATABASE_URI: Optional[str] = None
    SQLALCHEMY_ASYNC_DATABASE_URI: Optional[str] = None

    BILL_UPLOAD_FOLDER: str = "/data/uploads"
    LOG_FILE: str = "debug.log"
    LOG_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s"
    SCHEDULER_API_ENABLED: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    def _replace_host(self, url: str, host: str, port: Optional[int] = None) -> str:
        parsed = urlparse(url)
        auth = parsed.username or ""
        if parsed.password:
            auth += f":{parsed.password}"
        host_port = host if port is None else f"{host}:{port}"
        netloc = f"{auth}@{host_port}" if auth else host_port
        return urlunparse(parsed._replace(netloc=netloc))

    def _normalize_postgres_url(self, url: str) -> str:
        url = url.replace("postgres://", "postgresql://", 1)
        if self.POSTGRES_HOST:
            url = self._replace_host(url, self.POSTGRES_HOST, self.POSTGRES_PORT)
        return url

    def _build_postgres_url(self) -> Optional[str]:
        if self.DATABASE_URL and self.DATABASE_URL.startswith(
            ("postgres://", "postgresql://", "postgresql+")
        ):
            return self._normalize_postgres_url(self.DATABASE_URL)

        if self.POSTGRES_USER and self.POSTGRES_HOST and self.POSTGRES_DB:
            credentials = self.POSTGRES_USER
            if self.POSTGRES_PASSWORD:
                credentials += f":{self.POSTGRES_PASSWORD}"
            return (
                f"postgresql://{credentials}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}"
                f"/{self.POSTGRES_DB}"
            )
        return None

    def model_post_init(self, __context) -> None:
        super().model_post_init(__context)
        postgres_url = self._build_postgres_url()
        if postgres_url:
            self.SQLALCHEMY_SYNC_DATABASE_URI = postgres_url.replace(
                "postgresql://", "postgresql+psycopg2://", 1
            )
            self.SQLALCHEMY_ASYNC_DATABASE_URI = postgres_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
            return

        if self.DATABASE_URL and self.DATABASE_URL.startswith("sqlite"):
            self.SQLALCHEMY_SYNC_DATABASE_URI = self.DATABASE_URL
            self.SQLALCHEMY_ASYNC_DATABASE_URI = self.DATABASE_URL.replace(
                "sqlite:///", "sqlite+aiosqlite:///", 1
            )
            logging.warning("SQLite DATABASE_URL is configured. Postgres is preferred.")
            return

        raise ValueError(
            "PostgreSQL configuration is required. Set DATABASE_URL to a postgres URL "
            "or provide POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT and POSTGRES_DB."
        )

    @classmethod
    def from_env(cls) -> "Config":
        try:
            return cls(
                SECRET_KEY=os.environ.get("SECRET_KEY", ""),
                JWT_SECRET_KEY=os.environ.get("JWT_SECRET_KEY", ""),
            )
        except ValidationError as exc:
            missing = [
                err["loc"][0]
                for err in exc.errors()
                if err.get("type") == "missing" and err.get("loc")
            ]
            if missing:
                raise ValueError(
                    "Missing required environment variable(s): "
                    f"{', '.join(str(name) for name in missing)}. "
                    "Set them in your shell environment or in a .env file."
                ) from exc
            raise ValueError(f"Invalid environment configuration: {exc}") from exc


def ensure_database_exists(sync_uri: str) -> None:
    """Create the configured PostgreSQL database when it does not exist."""
    if not sync_uri or not sync_uri.startswith(
        ("postgres://", "postgresql://", "postgresql+")
    ):
        return

    # Keep database dependencies out of API and worker startup paths that only
    # import shared settings. Database services install these packages.
    from sqlalchemy import create_engine
    from sqlalchemy_utils import create_database, database_exists

    try:
        engine = create_engine(sync_uri)
        if not database_exists(engine.url):
            create_database(engine.url)
            logging.info("Successfully created database at: %s", engine.url.database)
    except Exception as exc:
        logging.error("Failed to check or create database: %s", exc)

