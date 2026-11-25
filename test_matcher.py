#!/usr/bin/env python3
"""Run the reconciliation pipeline against ledger and bank CSV files.

Ollama-backed matching and journal drafting are disabled unless --with-ai is set.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# This file lives under tests/, so the package root is its parent directory.
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
package_dir = BACKEND_DIR / "matcher"
package_spec = importlib.util.spec_from_file_location(
    "matcher", package_dir / "__init__.py",
    submodule_search_locations=[str(package_dir)],
)
if package_spec is None or package_spec.loader is None:
    raise RuntimeError(f"Could not load matcher package from {package_dir}")
matcher_package = importlib.util.module_from_spec(package_spec)
sys.modules["matcher"] = matcher_package
package_spec.loader.exec_module(matcher_package)

from app.shared.settings import Config  # noqa: E402
from matcher import MatchMemory, reconcile  # noqa: E402
from matcher.memory_match import JSONFileBackend  # noqa: E402
from schema import BankStatement, LedgerFormat  # noqa: E402


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(4096)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(stream, dialect=dialect))
    if not rows:
        raise ValueError(f"CSV has no data rows: {path}")
    return rows


def _value(row: dict[str, str], *names: str, default: str = "") -> str:
    values = {_key(k): (v or "").strip() for k, v in row.items() if k}
    for name in names:
        if values.get(_key(name), ""):
            return values[_key(name)]
    return default


def _amount(value: str) -> float:
    cleaned = (value or "").strip().replace(",", "").replace("₹", "").replace("$", "")
    if not cleaned:
        return 0.0
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return float(cleaned)


def _date(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value.split()[0] if "%H" not in fmt else value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date value {value!r}; use ISO YYYY-MM-DD or DD-MM-YYYY")


def _ledger_records(path: Path) -> list[LedgerFormat]:
    records = []
    for i, row in enumerate(_read_csv(path), start=2):
        account = _value(row, "account_name", "account", "particulars", "description", "narration")
        if not account:
            raise ValueError(f"Ledger row {i}: missing account/name/particulars column value")
        records.append(LedgerFormat(
            ledger_id=_value(row, "ledger_id", "id") or None,
            account_name=account,
            transaction_date=_date(_value(row, "transaction_date", "date", "voucher date")),
            debit_amount=_amount(_value(row, "debit_amount", "debit", "dr_amount", "dr")),
            credit_amount=_amount(_value(row, "credit_amount", "credit", "cr_amount", "cr")),
            reference_id=_value(row, "reference_id", "reference", "voucher no", "voucher number", "journal_id") or None,
            voucher_type=_value(row, "voucher_type") or None,
        ))
    return records


def _bank_records(path: Path) -> list[BankStatement]:
    records = []
    for row_index, row in enumerate(_read_csv(path), start=1):
        records.append(BankStatement(
            row_index=row_index,
            bank_name=_value(row, "bank_name") or path.stem,
            template_version="csv-cli",
            date=_date(_value(row, "date", "transaction_date", "value_date")),
            narration=_value(row, "narration", "description", "particulars", "remarks"),
            debit_amount=_amount(_value(row, "debit_amount", "debit", "withdrawal", "dr")),
            credit_amount=_amount(_value(row, "credit_amount", "credit", "deposit", "cr")),
            balance=_amount(_value(row, "balance")) if _value(row, "balance") else None,
            txn_id=_value(row, "txn_id", "transaction_id", "reference", "ref", "utr") or None,
        ))
    return records


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reconciliation directly on ledger and bank CSVs.")
    parser.add_argument("--files", nargs=2, metavar=("LEDGER_CSV", "BANK_CSV"), required=True)
    parser.add_argument("--output", "--ouput", dest="output", required=True, help="Output JSON file")
    parser.add_argument("--opposite-side", action="store_true", help="Use opposite debit/credit orientation")
    parser.add_argument("--with-ai", action="store_true", help="Enable Ollama AI matching and AI journal drafting")
    parser.add_argument("--memory", help="Optional JSON file to load/save learned match patterns")
    args = parser.parse_args()

    ledger_path, bank_path = map(Path, args.files)
    output_path = Path(args.output)
    try:
        ledger_records = _ledger_records(ledger_path)
        bank_records = _bank_records(bank_path)
        config = Config(
            _env_file=None,
            DEBUG=False,
            SECRET_KEY="local-matcher-cli",
            JWT_SECRET_KEY="local-matcher-cli",
            REDIS_URL="redis://127.0.0.1:6379/0",
            DATABASE_URL="sqlite:////tmp/latice-matcher-cli.sqlite",
            STORAGE_DIR="/tmp/latice-matcher-cli-storage",
        )
        memory = MatchMemory(JSONFileBackend(args.memory)) if args.memory else MatchMemory()
        warnings: list[str] = []
        result = reconcile(
            config=config,
            ledger_result={"records": ledger_records, "warnings": []},
            bank_result={
                "records": bank_records,
                "warnings": [],
                "bank_name": bank_records[0].bank_name if bank_records else bank_path.stem,
                "template_version": "csv-cli",
            },
            all_warnings=warnings,
            same_side=not args.opposite_side,
            auto_detect_same_side=True,
            memory=memory,
            enable_ai_matching=args.with_ai,
            enable_residual_reconciliation=True,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(_jsonable(result), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        parser.exit(2, f"test_matcher.py: error: {exc}\n")

    print(f"Wrote reconciliation results to {output_path}")
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
