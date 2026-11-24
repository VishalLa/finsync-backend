from __future__ import annotations

import logging
from functools import wraps
from typing import Callable, TypeVar
from urllib.parse import urlparse, urlunparse

from flask import Flask, g, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address

from app.shared.settings import get_settings, rate_limit_settings

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable)


def limiter_redis_url() -> str:
    settings = rate_limit_settings()
    if settings.RATELIMIT_REDIS_URL:
        return settings.RATELIMIT_REDIS_URL

    redis_url = get_settings().REDIS_URL
    parsed = urlparse(redis_url)
    return urlunparse(parsed._replace(path=f"/{settings.RATELIMIT_REDIS_DB}"))


def ip_key() -> str:
    return get_remote_address()


def user_or_ip_key() -> str:
    try:
        verify_jwt_in_request(optional=True)
        identity = get_jwt_identity()
        if identity is not None:
            return str(identity)
    except Exception:
        pass
    
    return ip_key()


limiter = Limiter(
    key_func=ip_key,
    default_limits=[lambda: rate_limit_settings().RATELIMIT_DEFAULT],
    strategy="sliding-window-counter",
    headers_enabled=True,
    swallow_errors=True,
    key_prefix="lattic:rl",
)


def rate_limit(tier: str) -> Callable[[F], F]:
    tier_fields = {
        "auth": ("RATELIMIT_AUTH", ip_key),
        "heavy": ("RATELIMIT_HEAVY", user_or_ip_key),
        "normal": ("RATELIMIT_NORMAL", user_or_ip_key),
        "poll": ("RATELIMIT_POLL", user_or_ip_key),
    }
    if tier not in tier_fields:
        raise ValueError(f"Unknown rate limit tier: {tier}")
    
    setting_name, key_func = tier_fields[tier]

    def decorate(function: F) -> F:
        return limiter.limit(
            lambda: getattr(rate_limit_settings(), setting_name),
            key_func=key_func,
        )(function)

    return decorate


def fail_closed(function: F) -> F:
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            storage = limiter.storage
            if storage is None or not storage.check():
                raise RuntimeError("Rate limit storage is unavailable")
            
        except Exception:
            logger.exception("Rate limit storage check failed for auth request")
            g.rate_limit_fail_closed = True
            response = jsonify(
                error="Service Unavailable",
                message="Authentication is temporarily unavailable. Please try again.",
            )
            
            response.headers["Retry-After"] = "5"
            return response, 503
        
        return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def _handle_rate_limit_exceeded(error: RateLimitExceeded):
    limit = getattr(error, "limit", None)
    logger.warning(
        "Rate limit exceeded: limit=%s ip=%s",
        getattr(limit, "limit", error.description),
        ip_key(),
    )
    return jsonify(
        error="Too Many Requests", 
        message=error.description
    ), 429


def init_rate_limiting(app: Flask) -> None:
    settings = rate_limit_settings()
    app.config["RATELIMIT_ENABLED"] = settings.RATELIMIT_ENABLED
    app.config["RATELIMIT_STORAGE_URI"] = limiter_redis_url()
    app.config["RATELIMIT_HEADERS_ENABLED"] = True

    @app.after_request
    def _preserve_fail_closed_retry_after(response):
        if getattr(g, "rate_limit_fail_closed", False):
            response.headers["Retry-After"] = "5"
        return response

    limiter.init_app(app)
    app.register_error_handler(RateLimitExceeded, _handle_rate_limit_exceeded)
