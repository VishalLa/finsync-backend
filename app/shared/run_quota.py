from __future__ import annotations

import logging
import time
from functools import lru_cache
from urllib.parse import urlparse, urlunparse

import redis

from app.shared.settings import get_settings, rate_limit_settings

logger = logging.getLogger(__name__)

_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local max_runs = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local run_id = ARGV[5]
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
if redis.call('ZCARD', key) >= max_runs then
    return 0
end
redis.call('ZADD', key, now, run_id)
redis.call('EXPIRE', key, ttl)
return 1
"""


def _limiter_redis_url() -> str:
    settings = rate_limit_settings()
    if settings.RATELIMIT_REDIS_URL:
        return settings.RATELIMIT_REDIS_URL
    parsed = urlparse(get_settings().REDIS_URL)
    return urlunparse(parsed._replace(path=f"/{settings.RATELIMIT_REDIS_DB}"))


@lru_cache(maxsize=1)
def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        _limiter_redis_url(),
        socket_connect_timeout=1,
        socket_timeout=1,
        decode_responses=True,
    )


def acquire(user_id: str, run_id: str) -> bool:
    settings = rate_limit_settings()
    now = time.time()
    ttl = settings.ACTIVE_RUN_TTL_SECONDS
    key = f"lattic:active_runs:{user_id}"
    try:
        result = _redis_client().eval(
            _ACQUIRE_SCRIPT,
            1,
            key,
            now,
            now - ttl,
            settings.MAX_ACTIVE_RUNS_PER_USER,
            ttl,
            run_id,
        )
        return bool(result)
    except Exception:
        logger.exception("Could not check active reconciliation quota for user %s", user_id)
        return True


def release(user_id: str, run_id: str) -> None:
    try:
        _redis_client().zrem(f"lattic:active_runs:{user_id}", run_id)
    except Exception:
        logger.exception("Could not release active reconciliation quota for user %s", user_id)
