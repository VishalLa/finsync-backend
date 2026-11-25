from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List, Tuple

from entry_point.ledger_handler.bank_processor import detect_bank
from entry_point.ledger_handler.bank_loader import load_bank_statement
from entry_point.ocr.bank_parser import (
    BANK_TEMPLATE_HEADERS,
    PARSER_REGISTRY,
    parser_for_layout,
)
from entry_point.ocr.classification import classify_document
from entry_point.ocr.common import DocumentLayout, ExtractedPage, OCRBox, ParseResult
from entry_point.ocr.parsers import write_csv
from schema.template import TEMPLATE_REGISTRY
from entry_point.ocr.pipeline import (
    extract_document,
    load_processed_document,
    run_reconciliation,
    validate_parse_result,
)
from entry_point.ocr.evaluation import evaluate_csv
from schema import BankStatement, LedgerFormat


def _box(text: str, x: float, y: float) -> OCRBox:
    return OCRBox(text, 0.99, x, y, x + max(8, len(text) * 4), y + 5)


def _page(text: str, rows: List[List[Tuple[str, float]]]) -> ExtractedPage:
    boxes = [
        _box(value, x, 10.0 + index * 20.0)
        for index, row in enumerate(rows)
        for value, x in row
    ]
    return ExtractedPage(1, text, boxes, "pdfplumber", [])


def test_hdfc_pdf_rows_load_as_schema_records(
    tmp_path: Path, monkeypatch
) -> None:
    page = _page(
        "HDFC BANK\nAccount No: 123456789\nOpening Balance: Rs. 1000.00\n"
        "Date Narration Chq./Ref.No. Value Dt Withdrawal Amt. Deposit Amt. Closing Balance\n"
        "05/01/25 ATM Withdrawal 1234 05/01/25 10.00 990.00",
        [
            [
                ("Date", 10), ("Narration", 110), ("Chq./Ref.No.", 250),
                ("Value Dt", 330), ("Withdrawal Amt.", 420),
                ("Deposit Amt.", 520), ("Closing Balance", 620),
            ],
            [
                ("05/01/25", 10), ("ATM Withdrawal", 110), ("1234", 250),
                ("05/01/25", 330), ("10.00", 420), ("", 520), ("990.00", 620),
            ],
            [
                ("Date", 10), ("Narration", 110), ("Chq./Ref.No.", 250),
                ("Value Dt", 330), ("Withdrawal Amt.", 420),
                ("Deposit Amt.", 520), ("Closing Balance", 620),
            ],
            [("END OF STATEMENT", 110)],
        ],
    )
    page.warnings.append("Page 1: median OCR confidence is below 0.80.")
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"synthetic pdf placeholder")
    monkeypatch.setattr(
        "entry_point.ocr.pipeline.extract_pdf_pages", lambda *args, **kwargs: [page]
    )
    document = extract_document(source, tmp_path / "output")
    loaded = load_processed_document(document)

    template, header_row, _ = detect_bank(str(document.csv_path))
    record = loaded["records"][0]
    assert template is not None and template.bank_name == "HDFC Bank"
    assert header_row == 0
    assert len(loaded["records"]) == 1
    assert isinstance(record, BankStatement)
    assert record.date == "2025-01-05"
    assert record.debit_amount == 10.0
    assert record.balance == 990.0
    assert record.txn_id == "1234"
    assert record.__dict__["account_number"] == "123456789"
    assert loaded["metadata"]["opening_balance"] == 1000.0
    assert loaded["metadata"]["closing_balance"] is None
    assert set(record.model_dump()) == set(BankStatement.model_fields)
    assert any("confidence" in warning for warning in record.parse_warnings)
    assert BANK_TEMPLATE_HEADERS["HDFC_V1"] == [
        "Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.",
        "Deposit Amt.", "Closing Balance",
    ]


def test_axis_native_headers_detect_and_load(tmp_path: Path) -> None:
    page = _page(
        "AXIS BANK",
        [
            [
                ("Tran Date", 10), ("Chq No", 100), ("Particulars", 190),
                ("Debit", 340), ("Credit", 430), ("Balance", 520),
                ("Init. Br", 620),
            ],
            [
                ("05-01-2025", 10), ("AX-1", 100), ("NEFT receipt", 190),
                ("", 340), ("50.00", 430), ("150.00", 520), ("001", 620),
            ],
        ],
    )
    layout = classify_document(page.text)
    parsed = parser_for_layout(layout).parse([page], layout, Path("axis.pdf"))
    csv_path = tmp_path / "axis.csv"
    write_csv(csv_path, parsed.headers, parsed.rows)

    template, header_row, _ = detect_bank(str(csv_path))
    loaded = load_bank_statement(str(csv_path))

    assert parsed.headers == BANK_TEMPLATE_HEADERS["AXIS_V1"]
    assert template is not None and template.bank_name == "Axis Bank"
    assert header_row == 0
    assert len(loaded["records"]) == len(parsed.rows) == 1
    assert loaded["records"][0].date == "2025-01-05"
    assert loaded["records"][0].credit_amount == 50.0
    assert set(loaded["records"][0].model_dump()) == set(BankStatement.model_fields)


def test_every_registered_bank_template_has_a_working_parser() -> None:
    assert set(PARSER_REGISTRY) == set(TEMPLATE_REGISTRY)

    for template_key, template in TEMPLATE_REGISTRY.items():
        headers = BANK_TEMPLATE_HEADERS[template_key]
        assert template["fingerprint"].issubset(set(headers))
        header_row = [(header, 20.0 + index * 100.0) for index, header in enumerate(headers)]
        transaction_row = []
        for index, header in enumerate(headers):
            value = ""
            if header == template["date_column"]:
                value = "05/01/2025"
            elif header == template["narration_column"]:
                value = "Sample transaction"
            elif header == template.get("txn_id_column"):
                value = "REF-1"
            elif header == template.get("debit_column"):
                value = "10.00"
            elif header == template.get("credit_column"):
                value = ""
            elif header == template.get("balance_column"):
                value = "90.00"
            elif header == template.get("amount_column"):
                value = "10.00"
            elif header == template.get("type_column"):
                value = "Dr"
            elif header.lower().replace(" ", "") in {"valuedt", "valuedate"}:
                value = "05/01/2025"
            transaction_row.append((value, 20.0 + index * 100.0))

        page = _page(
            " ".join(headers),
            [header_row, transaction_row],
        )
        layout = DocumentLayout(
            kind="bank",
            bank_name=template["bank_name"],
            parser_name=template_key,
        )
        parser = parser_for_layout(layout)
        parsed = parser.parse([page], layout, Path("statement.pdf"))

        assert parsed.headers == headers
        assert len(parsed.rows) == 1
        assert parsed.rows[0][template["date_column"]]


def test_bank_classification_selects_registered_date_header_version() -> None:
    pnb_v1 = classify_document("PUNJAB NATIONAL BANK\nTransction Date")
    pnb_v2 = classify_document("PUNJAB NATIONAL BANK\nTransaction Date")
    yes_v1 = classify_document("YES BANK\nTransction Date")
    yes_v2 = classify_document("YES BANK\nTransaction Date")
    pnb_header_only = classify_document("Transction Date Withdrawal Deposit")
    yes_header_only = classify_document(
        "Transaction Date Debited Amount Credited Amount"
    )

    assert pnb_v1.parser_name == "PNB_V1"
    assert pnb_v2.parser_name == "PNB_V2"
    assert yes_v1.parser_name == "YES_V1"
    assert yes_v2.parser_name == "YES_V2"
    assert pnb_header_only.parser_name == "PNB_V1"
    assert yes_header_only.parser_name == "YES_V2"


def test_balance_delta_repairs_swapped_bank_amounts() -> None:
    parsed = ParseResult(
        headers=["Debit", "Credit", "Balance"],
        rows=[{"Debit": "10.00", "Credit": "0.00", "Balance": "110.00"}],
        metadata={"opening_balance": 100.0},
        row_warnings=[[]],
        row_balances=[110.0],
    )

    checks, failures, warnings = validate_parse_result(parsed, is_ledger=False)

    assert checks == 1
    assert failures == 0
    assert parsed.rows[0]["Debit"] == "0.00"
    assert parsed.rows[0]["Credit"] == "10.00"
    assert any("repaired debit/credit" in warning for warning in warnings)


def test_running_balance_carries_forward_across_missing_balance_rows() -> None:
    parsed = ParseResult(
        headers=["Debit", "Credit", "Balance"],
        rows=[
            {"Debit": "10.00", "Credit": "0.00", "Balance": ""},
            {"Debit": "0.00", "Credit": "20.00", "Balance": "110.00"},
        ],
        metadata={"opening_balance": 100.0},
        row_balances=[None, 110.0],
    )

    checks, failures, _ = validate_parse_result(parsed, is_ledger=False)

    assert checks == 1
    assert failures == 0


def test_failed_running_balances_quarantine_and_refuse_loading(
    tmp_path: Path, monkeypatch
) -> None:
    page = _page(
        "HDFC BANK\nOpening Balance: 100.00",
        [
            [
                ("Date", 10), ("Narration", 110), ("Chq./Ref.No.", 250),
                ("Value Dt", 330), ("Withdrawal Amt.", 420),
                ("Deposit Amt.", 520), ("Closing Balance", 620),
            ],
            [
                ("05/01/25", 10), ("ATM Withdrawal", 110), ("1234", 250),
                ("05/01/25", 330), ("10.00", 420), ("", 520), ("500.00", 620),
            ],
        ],
    )
    source = tmp_path / "bad-statement.pdf"
    source.write_bytes(b"synthetic pdf placeholder")
    monkeypatch.setattr(
        "entry_point.ocr.pipeline.extract_pdf_pages", lambda *args, **kwargs: [page]
    )

    document = extract_document(source, tmp_path / "output")

    assert document.quarantined
    assert document.source.parent.name == "quarantine"
    assert document.source.exists()
    try:
        load_processed_document(document)
    except RuntimeError as error:
        assert "quarantined" in str(error)
    else:
        raise AssertionError("Quarantined PDFs must not be loaded.")


def test_reconciliation_handoff_groups_by_account_number() -> None:
    bank = BankStatement(row_index=0, bank_name="HDFC Bank", template_version="v1")
    bank.__dict__["account_number"] = "1001"
    ledger = LedgerFormat(account_name="Bank", account_number="1001")
    ledger.__dict__["ledger_type"] = "unified_general_ledger"
    calls = []

    handoff = run_reconciliation(
        [bank],
        [ledger],
        matcher=lambda **payload: calls.append(payload) or "ready",
    )

    assert len(handoff["handoffs"]) == 1
    assert handoff["handoffs"][0]["matcher_result"] == "ready"
    assert calls[0]["bank_name"] == "HDFC Bank"
    assert calls[0]["ledger_type"] == "unified_general_ledger"


def test_evaluation_reports_amount_date_and_txn_id_splits(tmp_path: Path) -> None:
    prediction = tmp_path / "prediction.csv"
    truth = tmp_path / "truth.csv"
    with prediction.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Date", "Txn_ID", "Debit", "Credit"])
        writer.writerow(["05/01/25", "A1", "10.00", "0.00"])
        writer.writerow(["06/01/25", "A2", "0.00", "20.00"])
    with truth.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            "date", "ref", "debit", "credit", "appears_in_files", "bank", "account",
        ])
        writer.writerow(["2025-01-05", "A1", "10.00", "0.00", "sample.pdf", "HDFC", "001"])
        writer.writerow(["2025-01-06", "A2", "0.00", "20.00", "sample.pdf", "HDFC", "001"])
        writer.writerow(["2025-02-01", "B1", "1.00", "0.00", "other.pdf", "AXIS", "002"])
    prediction.with_suffix(".meta.json").write_text(
        json.dumps({
            "source_pdf": "sample.pdf",
            "bank_name": "HDFC Bank",
            "account_number": "001",
            "page_sources": ["pdfplumber"],
        }),
        encoding="utf-8",
    )
    report = evaluate_csv(prediction, truth)

    assert report["all_ground_truth_rows"] == 3
    assert report["ground_truth_rows"] == 2
    assert report["aligned_rows"] == 2
    assert report["splits"]["all"]["amounts"]["precision"] == 1.0
    assert report["target_accuracy"] == 0.99
    assert report["target_met"] is True
    assert report["splits"]["all"]["dates"]["recall"] == 1.0
    assert report["splits"]["test"]["txn_ids"]["precision"] == 1.0
