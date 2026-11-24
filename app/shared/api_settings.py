from __future__ import annotations

from app.shared.settings import Settings


class APISettings(Settings):
    DEBUG: bool = False
    SECRET_KEY: str
    JWT_SECRET_KEY: str

    @classmethod
    def from_env(cls) -> "APISettings":
        return cls()
