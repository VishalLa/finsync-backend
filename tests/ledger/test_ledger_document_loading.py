from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Tuple

from entry_point.ledger_handler.ledger_loader import load_ledger, parse_date
from entry_point.ocr.classification import classify_document
from entry_point.ocr.common import ExtractedPage, OCRBox
from entry_point.ocr.parsers import (
    LEDGER_HEADERS,
    UnifiedGeneralLedgerParser,
    write_csv,
)
from entry_point.ocr.pipeline import validate_parse_result
from schema import LedgerFormat


def _box(text: str, x: float, y: float) -> OCRBox:
    return OCRBox(text, 0.99, x, y, x + max(8, len(text) * 4), y + 5)


def _page(text: str, rows: List[List[Tuple[str, float]]]) -> ExtractedPage:
    boxes = [
        _box(value, x, 10.0 + index * 20.0)
        for index, row in enumerate(rows)
        for value, x in row
    ]
    return ExtractedPage(1, text, boxes, "pdfplumber", [])


def test_unified_ledger_csv_loads_schema_rows_and_sidechannel(tmp_path: Path) -> None:
    page = _page(
        "UNIFIED GENERAL LEDGER\nTxn ID Bank Balance\nAccount No: 000123456",
        [
            [
                ("Date", 10), ("Txn ID", 110), ("Account Name", 220),
                ("Debit", 400), ("Credit", 490), ("Bank Balance", 590),
            ],
            [
                ("2025-02-01", 10), ("TX-1", 110), ("Office supplies", 220),
                ("25.50", 400), ("", 490), ("74.50", 590),
            ],
        ],
    )
    layout = classify_document(page.text)
    assert layout.kind == "ledger"
    assert layout.ledger_type == "unified_general_ledger"
    parsed = UnifiedGeneralLedgerParser().parse([page], layout, Path("ledger.pdf"))
    checks, failures, _ = validate_parse_result(parsed, is_ledger=True)
    assert checks == 0 and failures == 0

    csv_path = tmp_path / "ledger.csv"
    write_csv(csv_path, parsed.headers, parsed.rows)
    with csv_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    loaded = load_ledger(str(csv_path), date_format="%Y-%m-%d")
    record = loaded["records"][0]

    assert parsed.headers == LEDGER_HEADERS
    assert len(loaded["records"]) == len(rows) == 1
    assert isinstance(record, LedgerFormat)
    assert record.transaction_date == "2025-02-01"
    assert record.account_number == "000123456"
    assert record.__dict__["ledger_type"] == "unified_general_ledger"
    assert set(record.model_dump()) == set(LedgerFormat.model_fields)


def test_ledger_date_fallback_prefers_day_first_for_two_digit_year() -> None:
    parsed, warning = parse_date("05/01/25", "%Y-%m-%d")
    assert parsed == "2025-01-05"
    assert warning is None
