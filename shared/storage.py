from __future__ import annotations

import json
import os
import re
import shutil
import tempfile

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from functools import lru_cache
from typing import Any

from shared.settings import get_settings

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@lru_cache(maxsize=1)
def storage_dir() -> Path:
    return Path(get_settings().STORAGE_DIR).resolve()


def run_ref(run_id: str, *parts: str) -> str:
    """Build a ref inside one run's folder: run_ref("r42", "norm", "ledger.parquet")."""
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"bad run_id: {run_id!r}")
    return "/".join(["runs", run_id, *parts])


def resolve(ref: str) -> Path:
    """Turn a ref into a real path, refusing anything outside STORAGE_DIR."""
    if not ref or ref.startswith(("/", "\\")) or ".." in Path(ref).parts:
        raise ValueError(f"unsafe ref: {ref!r}")
    
    base = storage_dir()
    path = (base / ref).resolve()
    
    if base not in path.parents:
        raise ValueError(f"ref escapes storage: {ref!r}")
    
    return path


def _atomic_write(ref: str, writer) -> None:
    """Write to a temp file, then rename. A reader never sees a half-written file."""
    path = resolve(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    
    try:
        writer(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# raw uploads (API side)
def save_stream(ref: str, fileobj) -> None:
    """Stream an upload to storage in chunks (never load a huge file into memory).
    Flask: save_stream(run_ref(run_id, "raw", "bank.xlsx"), request.files["bank_file"].stream)
    """
    def _w(tmp):
        with open(tmp, "wb") as out:
            shutil.copyfileobj(fileobj, out, length=1024 * 1024)

    _atomic_write(ref, _w)


# parquet (intermediate tables)
def write_parquet(ref: str, df: Any) -> None:
    _atomic_write(ref, lambda tmp: df.to_parquet(tmp, index=False))


def read_parquet(ref: str, columns=None) -> Any:
    import pandas as pd

    return pd.read_parquet(resolve(ref), columns=columns)


# json (results)
def _json_default(o):
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return str(o)


def write_json(ref: str, obj) -> None:
    def _w(tmp):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, default=_json_default)

    _atomic_write(ref, _w)


def read_json(ref: str):
    with open(resolve(ref), encoding="utf-8") as f:
        return json.load(f)


# records <-> DataFrame
def _as_dict(obj) -> dict:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(vars(obj))


def records_to_df(records) -> Any:
    """List of LedgerFormat / BankStatement objects -> DataFrame (for write_parquet)."""
    import pandas as pd

    return pd.DataFrame([_as_dict(r) for r in records])


def df_to_records(df: Any, cls, date_columns=()):
    """DataFrame (from read_parquet) -> list of cls objects. Empty cells become None."""
    import pandas as pd

    df = df.copy()
    for col in date_columns:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col]).dt.date
    df = df.astype(object).where(df.notna(), None)
    return [cls(**row) for row in df.to_dict("records")]
