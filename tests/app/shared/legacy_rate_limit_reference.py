"""Rate limiting via Flask-Limiter (Redis, sliding-window-counter).

Requires: Flask-Limiter[redis]>=3.8 (older versions don't know the
"sliding-window-counter" strategy).

Usage in an API file:

    from app.shared.rate_limit import rate_limit, fail_closed

    @app.route("/x", methods=["POST"])
    @rate_limit("heavy")          # tiers: auth | heavy | normal | poll
    @jwt_required()
    def x(): ...

    # Auth routes (fail closed if Redis is down):
    @app.route("/login", methods=["POST"])
    @rate_limit("auth")
    @fail_closed
    def login(): ...
"""
from __future__ import annotations

import logging
from functools import lru_cache, wraps
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse

from flask import Flask, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from flask_limiter import Limiter, RateLimitExceeded
from flask_limiter.util import get_remote_address
from pydantic_settings import BaseSettings, SettingsConfigDict

from .settings import get_settings

logger = logging.getLogger(__name__)


class RateLimitSettings(BaseSettings):
    RATELIMIT_ENABLED: bool = True
    # Leave empty to derive from REDIS_URL with RATELIMIT_REDIS_DB as the db index.
    RATELIMIT_REDIS_URL: Optional[str] = None
    RATELIMIT_REDIS_DB: int = 1

    RATELIMIT_DEFAULT: str = "200 per minute"   # per IP, every route
    RATELIMIT_AUTH: str = "5 per minute"        # per IP
    RATELIMIT_HEAVY: str = "10 per hour"        # per user
    RATELIMIT_NORMAL: str = "60 per minute"     # per user
    RATELIMIT_POLL: str = "120 per minute"      # per user

    MAX_ACTIVE_RUNS_PER_USER: int = 3
    ACTIVE_RUN_TTL_SECONDS: int = 7200          # matches Celery visibility_timeout

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )


@lru_cache(maxsize=1)
def rate_limit_settings() -> RateLimitSettings:
    return RateLimitSettings()


def limiter_redis_url() -> str:
    cfg = rate_limit_settings()
    if cfg.RATELIMIT_REDIS_URL:
        return cfg.RATELIMIT_REDIS_URL
    parsed = urlparse(get_settings().REDIS_URL)
    return urlunparse(parsed._replace(path=f"/{cfg.RATELIMIT_REDIS_DB}"))


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def ip_key() -> str:
    # request.remote_addr is already corrected by ProxyFix (one hop: Caddy).
    return f"ip:{get_remote_address()}"


def user_or_ip_key() -> str:
    """JWT identity when a valid token is present, otherwise client IP.

    Invalid/expired tokens fall back to IP so an attacker can't pick their own
    bucket by sending garbage.
    """
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
    except Exception:
        identity = None
    return f"user:{identity}" if identity is not None else ip_key()


# --------------------------------------------------------------------------- #
# Limiter
# --------------------------------------------------------------------------- #
limiter = Limiter(
    key_func=ip_key,
    default_limits=[lambda: rate_limit_settings().RATELIMIT_DEFAULT],
    strategy="sliding-window-counter",
    headers_enabled=True,
    swallow_errors=True,  # fail OPEN if Redis is down; see fail_closed for auth
    key_prefix="lattic:rl",
)

_TIERS = {
    "auth": ("RATELIMIT_AUTH", ip_key),
    "heavy": ("RATELIMIT_HEAVY", user_or_ip_key),
    "normal": ("RATELIMIT_NORMAL", user_or_ip_key),
    "poll": ("RATELIMIT_POLL", user_or_ip_key),
}


def rate_limit(tier: str):
    """Decorator factory: apply one of the configured tiers to a route."""
    attr, key_func = _TIERS[tier]
    return limiter.limit(
        lambda: getattr(rate_limit_settings(), attr),
        key_func=key_func,
    )


def fail_closed(fn: Callable) -> Callable:
    """Return 503 if the limiter backend is unreachable (use on auth routes)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            healthy = limiter.storage is not None and limiter.storage.check()
        except Exception:
            healthy = False
        if not healthy:
            logger.error("Rate-limit storage unavailable; rejecting auth request")
            response = jsonify(
                error="Service Unavailable",
                message="Service temporarily unavailable. Please retry shortly.",
            )
            response.headers["Retry-After"] = "5"
            return response, 503
        return fn(*args, **kwargs)

    return wrapper


def _handle_rate_limited(error: RateLimitExceeded):
    logger.warning(
        "Rate limit exceeded: limit=%s ip=%s", error.description, get_remote_address()
    )
    # X-RateLimit-* / Retry-After headers are added by Flask-Limiter's after_request.
    return (
        jsonify(
            error="Too Many Requests",
            message="Rate limit exceeded. Please slow down and retry later.",
        ),
        429,
    )


def init_rate_limiting(app: Flask) -> None:
    cfg = rate_limit_settings()
    app.config["RATELIMIT_ENABLED"] = cfg.RATELIMIT_ENABLED
    app.config["RATELIMIT_STORAGE_URI"] = limiter_redis_url()
    limiter.init_app(app)
    app.register_error_handler(RateLimitExceeded, _handle_rate_limited)