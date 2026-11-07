from __future__ import annotations

from datetime import date, datetime
from functools import wraps
import logging
import time
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import SQLAlchemyError


logger = logging.getLogger(__name__)


def _log_call(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception(
                "%s.%s failed after %.3fs",
                fn.__module__,
                fn.__qualname__,
                time.perf_counter() - started,
            )
            raise

    return wrapper


def _log_db_errors(action: str):
    def decorator(fn):
        @_log_call
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except SQLAlchemyError:
                logger.exception("Database error while %s", action)
                raise

        return wrapper

    return decorator


def _coerce_date(value: Any) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _coerce_row_dates(
    data: Dict[str, Any],
    date_fields: List[str],
) -> Dict[str, Any]:
    result = dict(data)
    for field in date_fields:
        if field in result:
            result[field] = _coerce_date(result[field])
    return result
