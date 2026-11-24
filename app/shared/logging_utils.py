from __future__ import annotations

from functools import wraps
import logging
import time

logger = logging.getLogger(__name__)


def log_call(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        except Exception:
            logger.exception(
                "%s.%s failed after %.3fs",
                function.__module__,
                function.__qualname__,
                time.perf_counter() - started,
            )
            raise

    return wrapper
