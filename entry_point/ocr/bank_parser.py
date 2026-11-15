from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Type

from schema.template import TEMPLATE_REGISTRY

from .common import DocumentLayout, ExtractedPage, LayoutParser, ParseResult
from .parsers import (
    _HEADER_ALIASES,
    _cluster_rows,
    _find_header,
    _find_header_anchors,
    _format_money,
    _metadata,
    _money,
    _parse_date,
    _row_cells,
)

BANK_TEMPLATE_HEADERS: Dict[str, List[str]] = {
    "HDFC_V1": [
        "Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.",
        "Deposit Amt.", "Closing Balance",
    ],
    "AXIS_V1": [
        "Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance",
        "Init. Br",
    ],
    "ICICI_V1": [
        "Transaction Date", "Transaction Remarks", "Cheque Number",
        "Withdrawal Amount (INR )", "Deposit Amount (INR )", "Balance (INR )",
    ],
    "IDBI_V1": ["Date", "Description", "Cheque Number", "Type", "Amount"],
    "KOTAK_V1": [
        "Transaction Date", "Description", "Chq/Ref Number", "Debit", "Credit",
        "Balance",
    ],
    "PNB_V1": [
        "Transction Date", "Cheque Number", "Withdrawal", "Deposit", "Balance",
        "Narration",
    ],
    "PNB_V2": [
        "Transaction Date", "Cheque Number", "Withdrawal", "Deposit", "Balance",
        "Narration",
    ],
    "SBI_V1": [
        "Txn Date", "Value Date", "Description", "Ref No./Cheque No.", "Debit",
        "Credit", "Balance",
    ],
    "YES_V1": [
        "Reference No", "Transction Date", "Credited Amount", "Debited Amount",
        "Balance", "Description",
    ],
    "YES_V2": [
        "Reference No", "Transaction Date", "Credited Amount", "Debited Amount",
        "Balance", "Description",
    ],
    "GENERIC_V1": ["Date", "Txn_ID", "Narration", "Debit", "Credit", "Balance"],
    "GENERIC_SIGNED_V1": ["Date", "Description", "Amount", "Balance"],
}

_FOOTER_ROW_RE = re.compile(
    r"\b(opening balance|closing balance|statement summary|end of statement|"
    r"grand total|total\b|page\s+\d+)\b",
    re.IGNORECASE,
)


class RegisteredBankParser(LayoutParser):
    template_key = "GENERIC_V1"

    def parse(
        self,
        pages: List[ExtractedPage],
        _layout: DocumentLayout,
        _source_path: Path,
    ) -> ParseResult:
        template = TEMPLATE_REGISTRY[self.template_key]
        clustered = _cluster_rows(pages)
        header_index, anchors = _find_header(clustered)
        metadata = _metadata(pages, None)
        headers = BANK_TEMPLATE_HEADERS[self.template_key]
        output: List[Dict[str, str]] = []
        warnings = [warning for page in pages for warning in page.warnings]
        row_warnings: List[List[str]] = []
        row_balances: List[Optional[float]] = []
        previous: Optional[Dict[str, str]] = None

        for row in clustered[header_index + 1:]:
            cells = _row_cells(row, anchors)
            row_text = row.text.strip()
            if not row_text:
                continue
            
            row_anchors = _find_header_anchors(row, _HEADER_ALIASES)
            if {"date", "narration"}.issubset(row_anchors) and any(
                field in row_anchors
                for field in ("debit", "credit", "amount", "balance")
            ):
                continue
            
            if _FOOTER_ROW_RE.search(row_text):
                continue

            date = _parse_date(cells.get("date", ""))
            narration = cells.get("narration", "").strip()
            debit_text = cells.get("debit", "")
            credit_text = cells.get("credit", "")
            amount_text = cells.get("amount", "")
            has_amount = bool((debit_text + credit_text + amount_text).strip())
            
            if date is None and not has_amount and narration and previous is not None:
                narration_column = template["narration_column"]
                previous[narration_column] = (
                    previous[narration_column] + " " + narration
                ).strip()
                continue
            
            if date is None and not has_amount:
                continue

            row_number = len(output) + 1
            row_notes: List[str] = []
            if date is None:
                row_notes.append(f"Row {row_number}: date could not be parsed.")
                
            debit = _money(
                debit_text, row_number=row_number, field_name="debit",
                warnings=row_notes,
            )
            credit = _money(
                credit_text, row_number=row_number, field_name="credit",
                warnings=row_notes,
            )
            amount = _money(
                amount_text, row_number=row_number, field_name="amount",
                warnings=row_notes,
            )
            balance_text = cells.get("balance", "")
            balance = (
                _money(
                    balance_text, row_number=row_number, field_name="balance",
                    warnings=row_notes,
                )
                if balance_text
                else None
            )
            txn_id = cells.get("txn_id", "").strip()
            formatted_date = date.strftime(template["date_format"]) if date else ""
            value_date = _parse_date(cells.get("value_date", ""))
            formatted_value_date = (
                value_date.strftime(template["date_format"]) if value_date else ""
            )
            record = self._format_record(
                headers=headers,
                template=template,
                date=formatted_date,
                value_date=formatted_value_date,
                narration=narration,
                txn_id=txn_id,
                amount=amount,
                debit=debit,
                credit=credit,
                balance=balance,
                cells=cells,
            )
            output.append(record)
            row_warnings.append(row_notes)
            row_balances.append(float(balance) if balance is not None else None)
            previous = record

        if not output:
            raise ValueError(
                f"No transaction rows were found for template {self.template_key}."
            )
        return ParseResult(
            headers=headers,
            rows=output,
            metadata=metadata,
            warnings=warnings,
            row_warnings=row_warnings,
            row_balances=row_balances,
        )

    def _format_record(
        self,
        *,
        headers: List[str],
        template: Dict[str, object],
        date: str,
        value_date: str,
        narration: str,
        txn_id: str,
        amount: Decimal,
        debit: Decimal,
        credit: Decimal,
        balance: Optional[Decimal],
        cells: Dict[str, str],
    ) -> Dict[str, str]:
        record: Dict[str, str] = {header: "" for header in headers}
        record[str(template["date_column"])] = date
        record[str(template["narration_column"])] = narration
        
        if template.get("txn_id_column"):
            record[str(template["txn_id_column"])] = txn_id
        if template.get("debit_column"):
            record[str(template["debit_column"])] = _format_money(debit)
        if template.get("credit_column"):
            record[str(template["credit_column"])] = _format_money(credit)
        if template.get("balance_column") and balance is not None:
            record[str(template["balance_column"])] = _format_money(balance)
        if template.get("amount_column"):
            record[str(template["amount_column"])] = _format_money(amount)
        if template.get("type_column"):
            type_value = cells.get("type", "").strip()
            if not type_value and amount:
                type_value = "Dr" if amount < 0 else "Cr"
            record[str(template["type_column"])] = type_value
            
        for header in headers:
            if header.lower().replace(" ", "") in {"valuedt", "valuedate"}:
                record[header] = value_date
            elif header == "Init. Br":
                record[header] = cells.get("branch", "")
        
        return record


class HdfcParser(RegisteredBankParser):
    template_key = "HDFC_V1"


class AxisParser(RegisteredBankParser):
    template_key = "AXIS_V1"


class IciciParser(RegisteredBankParser):
    template_key = "ICICI_V1"


class IdbiParser(RegisteredBankParser):
    template_key = "IDBI_V1"


class KotakParser(RegisteredBankParser):
    template_key = "KOTAK_V1"


class PunjabNationalBankV1Parser(RegisteredBankParser):
    template_key = "PNB_V1"


class PunjabNationalBankV2Parser(RegisteredBankParser):
    template_key = "PNB_V2"


class SbiParser(RegisteredBankParser):
    template_key = "SBI_V1"


class YesBankV1Parser(RegisteredBankParser):
    template_key = "YES_V1"


class YesBankV2Parser(RegisteredBankParser):
    template_key = "YES_V2"


class GenericBankParser(RegisteredBankParser):
    template_key = "GENERIC_V1"


class GenericSignedAmountParser(RegisteredBankParser):
    template_key = "GENERIC_SIGNED_V1"


PARSER_REGISTRY: Dict[str, Type[LayoutParser]] = {
    "HDFC_V1": HdfcParser,
    "AXIS_V1": AxisParser,
    "ICICI_V1": IciciParser,
    "IDBI_V1": IdbiParser,
    "KOTAK_V1": KotakParser,
    "PNB_V1": PunjabNationalBankV1Parser,
    "PNB_V2": PunjabNationalBankV2Parser,
    "SBI_V1": SbiParser,
    "YES_V1": YesBankV1Parser,
    "YES_V2": YesBankV2Parser,
    "GENERIC_V1": GenericBankParser,
    "GENERIC_SIGNED_V1": GenericSignedAmountParser,
}


def register_bank_parser(
    template_key: str, parser_type: Type[LayoutParser]
) -> None:
    if template_key not in TEMPLATE_REGISTRY:
        raise KeyError(f"Unknown bank template key: {template_key}")
    PARSER_REGISTRY[template_key] = parser_type


def parser_for_layout(layout: DocumentLayout) -> LayoutParser:
    parser_type = PARSER_REGISTRY.get(
        layout.parser_name or "", GenericBankParser
    )
    return parser_type()
