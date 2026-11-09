from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from schema import BankStatement, LedgerFormat

from ledger_handler.bank_loader import load_bank_statement
from ledger_handler.ledger_loader import load_ledger

from .classification import classify_document
from .bank_parser import parser_for_layout
from .common import (
    ExtractedPage,
    ParseResult,
    ProcessedDocument,
)
from .parsers import UnifiedGeneralLedgerParser, write_csv
from .pdf_reader import extract_pdf_pages

logger = logging.getLogger(__name__)
_BALANCE_TOLERANCE = 0.02


def _transaction_values(row: Dict[str, str], is_ledger: bool) -> Tuple[float, float]:
    if is_ledger:
        debit_key, credit_key = "debit_amount", "credit_amount"
    elif "Withdrawal Amt." in row:
        debit_key, credit_key = "Withdrawal Amt.", "Deposit Amt."
    else:
        debit_key, credit_key = "Debit", "Credit"
        
    return float(row.get(debit_key) or 0), float(row.get(credit_key) or 0)


def _set_transaction_values(
    row: Dict[str, str], debit: float, credit: float, is_ledger: bool
) -> None:
    if is_ledger:
        debit_key, credit_key = "debit_amount", "credit_amount"
    elif "Withdrawal Amt." in row:
        debit_key, credit_key = "Withdrawal Amt.", "Deposit Amt."
    else:
        debit_key, credit_key = "Debit", "Credit"
        
    row[debit_key] = f"{debit:.2f}"
    row[credit_key] = f"{credit:.2f}"


def validate_parse_result(
    result: ParseResult, 
    *, 
    is_ledger: bool
) -> Tuple[int, int, List[str]]:
    """Validate running balances and repair a clearly swapped debit/credit pair."""
    warnings: List[str] = []
    opening = result.metadata.get("opening_balance")
    previous_balance = float(opening) if opening is not None else None
    checks = failures = 0
    balance_key = "balance" if is_ledger else (
        "Closing Balance" if "Closing Balance" in result.headers else "Balance"
    )
    row_warnings = result.row_warnings

    for index, row in enumerate(result.rows):
        balance_text = row.get(balance_key, "")
        try:
            balance = (
                float(result.row_balances[index])
                if index < len(result.row_balances) and result.row_balances[index] is not None
                else float(balance_text) if balance_text else None
            )
        except ValueError:
            warnings.append(f"Row {index + 1}: balance value could not be validated.")
            balance = None
        
        debit, credit = _transaction_values(row, is_ledger)
        if balance is None:
            if previous_balance is not None:
                previous_balance += credit - debit
            continue
        
        if previous_balance is None:
            previous_balance = balance
            continue

        expected = previous_balance + credit - debit
        checks += 1
        if abs(expected - balance) > _BALANCE_TOLERANCE:
            swapped_expected = previous_balance + debit - credit
            if abs(swapped_expected - balance) <= _BALANCE_TOLERANCE:
                _set_transaction_values(row, credit, debit, is_ledger)
                if index < len(row_warnings):
                    row_warnings[index].append(
                        "Debit and credit were swapped to satisfy the running-balance check."
                    )
                warnings.append(
                    f"Row {index + 1}: repaired debit/credit assignment using the balance delta."
                )
            else:
                failures += 1
                message = (
                    f"Row {index + 1}: running balance does not match the transaction amount."
                )
                warnings.append(message)
                if index < len(row_warnings):
                    row_warnings[index].append(message)
        
        previous_balance = balance

    if result.totals is not None:
        debit_sum = sum(_transaction_values(row, is_ledger)[0] for row in result.rows)
        credit_sum = sum(_transaction_values(row, is_ledger)[1] for row in result.rows)
        
        for field_name, actual in (
            ("debit_amount", debit_sum),
            ("credit_amount", credit_sum),
        ):
            expected_total = result.totals.get(field_name)
            if expected_total is not None and round(expected_total, 2) != round(actual, 2):
                raise ValueError(
                    f"Ledger total {field_name} does not equal the sum of transaction rows."
                )
                
    return checks, failures, warnings


def _quarantine(source: Path, output_dir: Path) -> Path:
    target_dir = output_dir / "quarantine"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    counter = 1
    
    while target.exists():
        target = target_dir / f"{source.stem}-{counter}{source.suffix}"
        counter += 1
    
    return Path(shutil.move(str(source), str(target)))


def extract_document(
    source: Path,
    output_dir: Path,
    *,
    force_ocr: bool = False,
    use_gpu: bool = False,
    dry_run: bool = False,
) -> ProcessedDocument:
    pages: List[ExtractedPage] = extract_pdf_pages(
        source, force_ocr=force_ocr, use_gpu=use_gpu
    )
    if not pages:
        raise ValueError(f"No pages could be read from {source.name}.")
    
    layout = classify_document(pages[0].text)
    warnings: List[str] = []
    
    if layout.bank_name == "UNKNOWN" and layout.kind == "bank":
        warning = "Bank layout was not recognized; using UNKNOWN."
        logger.warning("%s Source: %s", warning, source.name)
        warnings.append(warning)

    parser = (
        UnifiedGeneralLedgerParser()
        if layout.kind == "ledger"
        else parser_for_layout(layout)
    )
    result = parser.parse(pages, layout, source)
    checks, failures, validation_warnings = validate_parse_result(
        result, 
        is_ledger=layout.kind == "ledger"
    )
    warnings.extend(result.warnings)
    warnings.extend(validation_warnings)
    quarantined = checks > 0 and failures / checks > 0.05
    if quarantined:
        warning = (
            f"Balance validation failed for {failures}/{checks} checked rows "
            + (
                "and the document will be quarantined; loading is aborted."
                if not dry_run
                else "and the document would be quarantined; loading is aborted."
            )
        )
        warnings.append(warning)

    csv_dir = output_dir / "csv"
    csv_path = csv_dir / f"{source.stem}.csv"
    metadata_path = csv_dir / f"{source.stem}.meta.json"
    if csv_path.exists() or metadata_path.exists():
        raise FileExistsError(
            f"Output for {source.name} already exists; choose a different output directory."
        )
        
    write_csv(csv_path, result.headers, result.rows)
    metadata = {
        **result.metadata,
        "source_pdf": source.name,
        "document_kind": layout.kind,
        "bank_name": layout.bank_name,
        "ledger_type": layout.ledger_type,
        "parser": layout.parser_name,
        "page_sources": [page.source for page in pages],
        "row_warnings": result.row_warnings,
        "warnings": warnings,
        "balance_checks": checks,
        "balance_failures": failures,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    quarantined_path = None
    if quarantined:
        if not dry_run:
            quarantined_path = _quarantine(source, output_dir)
            logger.warning("%s Source: %s", warnings[-1], quarantined_path.name)
        
    return ProcessedDocument(
        source=quarantined_path or source,
        csv_path=csv_path,
        metadata_path=metadata_path,
        layout=layout,
        row_count=len(result.rows),
        warnings=warnings,
        quarantined=quarantined,
    )


def _merge_sidecar_warnings(
    records: Sequence[BankStatement | LedgerFormat], metadata: Dict[str, Any]
) -> None:
    document_warnings = metadata.get("warnings", [])
    row_warnings = metadata.get("row_warnings", [])
    
    for index, record in enumerate(records):
        record.parse_warnings.extend(document_warnings)
        if index < len(row_warnings):
            record.parse_warnings.extend(row_warnings[index])
        
        if isinstance(record, BankStatement):
            record.__dict__["account_number"] = metadata.get("account_number")
            record.__dict__["holder"] = metadata.get("holder")
            record.__dict__["ifsc"] = metadata.get("ifsc")
            record.__dict__["period"] = metadata.get("period")


def load_processed_document(document: ProcessedDocument) -> Dict[str, Any]:
    if document.quarantined:
        raise RuntimeError(f"Refusing to load quarantined document {document.source.name}.")
    
    metadata = json.loads(document.metadata_path.read_text(encoding="utf-8"))
    if document.layout.kind == "ledger":
        loaded = load_ledger(str(document.csv_path), date_format="%Y-%m-%d")
        records: List[LedgerFormat] = loaded["records"]
    else:
        loaded = load_bank_statement(str(document.csv_path))
        records = loaded["records"]
        for record in records:
            record.bank_name = document.layout.bank_name
        loaded["bank_name"] = document.layout.bank_name
    
    if len(records) != document.row_count:
        raise AssertionError(
            f"Loaded {len(records)} records from {document.row_count} CSV transaction rows "
            f"for {document.source.name}."
        )
        
    _merge_sidecar_warnings(records, metadata)
    loaded["warnings"] = list(loaded.get("warnings", [])) + metadata.get("warnings", [])
    loaded["metadata"] = metadata
    return loaded


def run_reconciliation(
    bank_records: Sequence[BankStatement],
    ledger_records: Sequence[LedgerFormat],
    *,
    bank_name: Optional[str] = None,
    ledger_type: Optional[str] = None,
    matcher: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Build matcher-ready, account-number-matched inputs for each statement."""
    banks_by_account: Dict[str, List[BankStatement]] = {}
    ledgers_by_account: Dict[str, List[LedgerFormat]] = {}
    
    for record in bank_records:
        account_number = record.__dict__.get("account_number")
        if account_number:
            banks_by_account.setdefault(str(account_number), []).append(record)
            
    for record in ledger_records:
        if record.account_number:
            ledgers_by_account.setdefault(record.account_number, []).append(record)

    handoffs: List[Dict[str, Any]] = []
    matched_accounts = banks_by_account.keys() & ledgers_by_account.keys()
    
    for account_number, bank_group in banks_by_account.items():
        ledger_group = ledgers_by_account.get(account_number, [])
        if not ledger_group:
            continue
        
        payload = {
            "bank_name": bank_name or bank_group[0].bank_name,
            "ledger_type": ledger_type or ledger_group[0].__dict__.get("ledger_type"),
            "account_number": account_number,
            "bank_records": list(bank_group),
            "ledger_records": list(ledger_group),
        }
        payload["matcher_result"] = matcher(**payload) if matcher else None
        handoffs.append(payload)
    
    return {
        "handoffs": handoffs,
        "unmatched_bank_records": len(bank_records) - sum(
            len(banks_by_account[account]) for account in matched_accounts
        ),
        "unmatched_ledger_records": len(ledger_records) - sum(
            len(ledgers_by_account[account]) for account in matched_accounts
        ),
    }


def run_pipeline(
    input_path: str | Path,
    output_path: str | Path,
    *,
    mode: str = "none",
    dry_run: bool = False,
    force_ocr: bool = False,
    use_gpu: bool = False,
) -> Dict[str, Any]:
    if mode not in {"none", "load", "reconcile"}:
        raise ValueError("mode must be one of: none, load, reconcile")
    
    source_path = Path(input_path)
    sources = [source_path] if source_path.is_file() else sorted(source_path.glob("*.pdf"))
    sources = [source for source in sources if source.suffix.lower() == ".pdf"]
    if not sources:
        raise FileNotFoundError(f"No PDF statements found in {source_path}.")
    
    output_dir = Path(output_path)
    processed = [
        extract_document(
            source, output_dir, force_ocr=force_ocr, use_gpu=use_gpu,
            dry_run=dry_run,
        )
        for source in sources
    ]
    
    loaded_documents: List[Tuple[ProcessedDocument, Dict[str, Any]]] = []
    if mode != "none" and not dry_run:
        for document in processed:
            if document.quarantined:
                continue
            loaded_documents.append((document, load_processed_document(document)))

    bank_records: List[BankStatement] = []
    ledger_records: List[LedgerFormat] = []
    ledger_types = set()
    bank_names = set()
    
    for document, loaded in loaded_documents:
        if document.layout.kind == "ledger":
            ledger_records.extend(loaded["records"])
            if document.layout.ledger_type:
                ledger_types.add(document.layout.ledger_type)
        else:
            bank_records.extend(loaded["records"])
            if document.layout.bank_name != "UNKNOWN":
                bank_names.add(document.layout.bank_name)
                
    handoff = None
    
    if mode == "reconcile" and not dry_run:
        handoff = run_reconciliation(
            bank_records,
            ledger_records,
            bank_name=next(iter(bank_names)) if len(bank_names) == 1 else None,
            ledger_type=next(iter(ledger_types)) if len(ledger_types) == 1 else None,
        )
    
    return {
        "documents": processed,
        "loaded_documents": loaded_documents,
        "bank_records": bank_records,
        "ledger_records": ledger_records,
        "reconciliation": handoff,
    }
