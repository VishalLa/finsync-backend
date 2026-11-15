from __future__ import annotations

import csv
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .common import (
    DocumentLayout,
    ExtractedPage,
    LayoutParser,
    OCRBox,
    ParseResult,
    TableRow,
)

logger = logging.getLogger(__name__)

LEDGER_HEADERS = [
    "ledger_id", "transaction_date", "account_name", "account_number",
    "debit_amount", "credit_amount", "reference_id", "ledger_type",
    "bank_name", "holder", "category",
]
_HEADER_ALIASES = {
    "date": ("date", "tran date", "transaction date", "transction date", "txn date"),
    "value_date": ("value dt", "value date"),
    "narration": (
        "narration", "particulars", "description", "account name", "ledger name",
        "transaction particulars", "transaction remarks",
    ),
    "txn_id": (
        "chq ref no", "chq no", "cheque no", "txn id", "transaction id",
        "cheque number", "reference id", "ref no", "reference no", "voucher no",
        "chq ref number",
        "ref no cheque no",
    ),
    "debit": (
        "withdrawal amt", "withdrawal", "debit", "debit amount", "dr",
        "withdrawal amount inr", "debited amount",
    ),
    "credit": (
        "deposit amt", "deposit", "credit", "credit amount", "cr",
        "deposit amount inr", "credited amount",
    ),
    "amount": ("amount", "transaction amount"),
    "type": ("type", "dr cr", "transaction type"),
    "balance": ("closing balance", "bank balance", "balance", "balance inr"),
    "branch": ("init br", "branch"),
    "account_number": ("account number", "account no", "account_number", "account no."),
}
_FOOTER_RE = re.compile(
    r"\b(opening balance|closing balance|total\b|grand total|"
    r"end of statement|statement summary|balance brought forward|"
    r"balance carried forward|page \d+)\b",
    re.IGNORECASE,
)
_NUMERIC_OCR_MAP = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5"})
_BLANKS = {"", "-", "--", "na", "n/a", "nil", "none", "null", "nan"}


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _cluster_rows(
    pages: Sequence[ExtractedPage]
) -> List[TableRow]:
    result: List[TableRow] = []
    for page in pages:
        boxes = sorted(page.boxes, key=lambda box: (box.y_center, box.x0))
        heights = sorted(box.height for box in boxes)
        
        line_height = heights[len(heights) // 2] if heights else 10.0
        tolerance = max(3.0, line_height * 0.65)
        
        page_rows: List[List[OCRBox]] = []
        row_centers: List[float] = []
        for box in boxes:
            if page_rows and abs(box.y_center - row_centers[-1]) <= tolerance:
                page_rows[-1].append(box)
                row_centers[-1] = sum(
                    item.y_center 
                    for item in page_rows[-1]) / len(page_rows[-1]
                )
            else:
                page_rows.append([box])
                row_centers.append(box.y_center)
                
        result.extend(
            TableRow(page.page_number, row) for row in page_rows
        )
        
    return result


def _find_header_anchors(
    row: TableRow, 
    aliases: Dict[str, Tuple[str, ...]]
) -> Dict[str, float]:
    boxes = sorted(row.boxes, key=lambda item: item.x_center)
    tokens = [
        (token, box_index)
        for box_index, box in enumerate(boxes)
        for token in _normalized(box.text).split()
    ]
    
    anchors: Dict[str, float] = {}
    for field_name, options in aliases.items():
        candidates: List[Tuple[int, int, float]] = []
        
        for option in options:
            option_tokens = _normalized(option).split()
            width = len(option_tokens)
            for start in range(max(1, len(tokens) - width + 1)):
                phrase = " ".join(token for token, _ in tokens[start:start + width])
                
                if phrase == " ".join(option_tokens):
                    first_box = boxes[tokens[start][1]]
                    last_box = boxes[tokens[start + width - 1][1]]
                    center = (first_box.x_center + last_box.x_center) / 2
                    candidates.append((width, start, center))
                    
        if candidates:
            _, _, anchors[field_name] = max(candidates)
            
    return anchors


def _find_header(
    rows: Sequence[TableRow]
) -> Tuple[int, Dict[str, float]]:
    best: Optional[Tuple[int, Dict[str, float]]] = None
    
    for index, row in enumerate(rows):
        anchors = _find_header_anchors(row, _HEADER_ALIASES)
        
        if "date" not in anchors or "narration" not in anchors:
            continue
        
        if sum(
            field in anchors for field in ("debit", "credit", "amount", "balance")
        ) < 1:
            continue
        
        if best is None or len(anchors) > len(best[1]):
            best = (index, anchors)
            
    if best is None:
        raise ValueError("Could not identify a transaction table header in the PDF.")
    
    return best


def _row_cells(
    row: TableRow, 
    anchors: Dict[str, float]
) -> Dict[str, str]:
    ordered = sorted((x, field) for field, x in anchors.items())
    boundaries = [
        (ordered[index][0] + ordered[index + 1][0]) / 2
        for index in range(len(ordered) - 1)
    ]
    
    cells: Dict[str, List[str]] = {
        field: [] for _, field in ordered
    }
    for box in sorted(row.boxes, key=lambda item: item.x_center):
        column = 0
        while column < len(boundaries) and box.x_center > boundaries[column]:
            column += 1
            
        cells[ordered[column][1]].append(box.text)
        
    return {
        field: " ".join(values).strip() for field, values in cells.items()
    }


def _correct_numeric(
    value: str, 
    *, 
    row_number: int, 
    field_name: str, 
    warnings: List[str]
) -> str:
    corrected = value.translate(_NUMERIC_OCR_MAP)
    if corrected != value:
        warnings.append(
            f"Row {row_number}: corrected OCR characters in numeric field '{field_name}'."
        )
        
        logger.info(
            "Corrected OCR characters in numeric field %s on row %d.",
            field_name,
            row_number,
        )
    
    return corrected


def _money(
    value: str, 
    *, 
    row_number: int, 
    field_name: str, 
    warnings: List[str]
) -> Decimal:
    value = _correct_numeric(
        value.strip(), 
        row_number=row_number, 
        field_name=field_name, 
        warnings=warnings
    )
    if value.lower() in _BLANKS:
        return Decimal("0.00")
    
    negative = value.startswith("(") and value.endswith(")")
    cleaned = re.sub(r"[^\d,.\-]", "", value)
    
    if re.search(r",\d{1,2}$", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
        
    try:
        amount = Decimal(cleaned.strip("()"))
    except InvalidOperation:
        warnings.append(f"Row {row_number}: could not parse {field_name} amount.")
        return Decimal("0.00")
    
    return -abs(amount) if negative else amount


def _parse_date(
    value: str
) -> Optional[datetime]:
    text = value.strip()
    if not text:
        return None

    for date_format in ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    
    try:
        from dateutil import parser
        
        return parser.parse(text, dayfirst=True, fuzzy=False)
    except (ImportError, ValueError, OverflowError):
        return None


def _format_money(value: Decimal) -> str:
    return f"{value:.2f}"


def _metadata(
    pages: Sequence[ExtractedPage], 
    ledger_type: Optional[str]
) -> Dict[str, Any]:
    full_text = "\n".join(page.text for page in pages)
    account_number = None
    account_match = re.search(
        r"(?:account\s*(?:number|no\.?)|a/c\s*(?:no\.?)?)[ \t]*[:#-]?[ \t]*"
        r"([0-9Xx*-]{5,})",
        full_text,
        re.IGNORECASE,
    )
    
    if account_match:
        account_number = account_match.group(1).strip()
    
    ifsc_match = re.search(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", full_text, re.IGNORECASE)
    period_match = re.search(
        r"(?:period|from)\s*[:\-]?\s*([0-9A-Za-z/ .-]{6,40}?)\s+"
        r"(?:to|till|through)\s+([0-9A-Za-z/ .-]{6,40})",
        full_text,
        re.IGNORECASE,
    )
    holder_match = re.search(
        r"(?:account holder|customer name|name)\s*[:\-]\s*([^\n]{2,80})",
        full_text,
        re.IGNORECASE,
    )
    opening_match = re.search(
        r"opening balance[ \t]*[:\-]?[ \t]*(?:(?:Rs\.?|INR|₹)[ \t]*)?"
        r"([()\-0-9OIlS,.$ ]+)",
        full_text,
        re.IGNORECASE,
    )
    closing_match = re.search(
        r"closing balance[ \t]*[:\-]?[ \t]*(?:(?:Rs\.?|INR|₹)[ \t]*)?"
        r"([()\-0-9OIlS,.$ ]+)",
        full_text,
        re.IGNORECASE,
    )

    def read_meta_amount(
        match: Optional[re.Match[str]]
    ) -> Optional[float]:
        if not match:
            return None
        
        value = re.sub(r"[^\d,.\-()]", "", match.group(1))
        corrected = value.translate(_NUMERIC_OCR_MAP)
        if corrected != value:
            logger.info("Corrected OCR characters in statement balance metadata.")
            
        value = corrected
        if not re.search(r"\d", value):
            return None
        
        try:
            return float(_money(
                value, 
                row_number=0, 
                field_name="metadata", 
                warnings=[]
            ))
        except (ValueError, InvalidOperation):
            return None

    return {
        "account_number": account_number,
        "holder": holder_match.group(1).strip() if holder_match else None,
        "ifsc": ifsc_match.group(0).upper() if ifsc_match else None,
        "period": (
            {
                "from": period_match.group(1).strip(), "to": period_match.group(2).strip()
            }
            if period_match
            else None
        ),
        "opening_balance": read_meta_amount(opening_match),
        "closing_balance": read_meta_amount(closing_match),
        "ledger_type": ledger_type,
    }


class UnifiedGeneralLedgerParser(LayoutParser):
    layout_name = "unified_general_ledger"

    def parse(
        self, 
        pages: Sequence[ExtractedPage], 
        layout: DocumentLayout, 
        source_path: Path
    ) -> ParseResult:
        clustered = _cluster_rows(pages)
        header_index, anchors = _find_header(clustered)
        metadata = _metadata(pages, layout.ledger_type)
        bank_name = layout.bank_name if layout.bank_name != "UNKNOWN" else ""
        
        output: List[Dict[str, str]] = []
        row_warnings: List[List[str]] = []
        balances: List[Optional[float]] = []
        warnings = [warning for page in pages for warning in page.warnings]
        totals: Optional[Dict[str, float]] = None
        previous: Optional[Dict[str, str]] = None

        for row in clustered[header_index + 1:]:
            cells = _row_cells(row, anchors)
            row_text = row.text.strip()
            row_anchors = _find_header_anchors(row, _HEADER_ALIASES)
            if {"date", "narration"}.issubset(row_anchors) and sum(
                field in row_anchors for field in ("debit", "credit", "amount", "balance")
            ) >= 1:
                continue
            
            date = _parse_date(cells.get("date", ""))
            account_name = cells.get("narration", "").strip()
            amount_text = cells.get("amount", "")
            has_amount = bool(
                (cells.get("debit", "") + cells.get("credit", "") + amount_text).strip()
            )
            
            if _FOOTER_RE.search(row_text):
                if "total" in row_text.lower() and has_amount:
                    total_notes: List[str] = []
                    debit_total = _money(
                        cells.get("debit", ""), row_number=0,
                        field_name="total debit", warnings=total_notes
                    )
                    credit_total = _money(
                        cells.get("credit", ""), row_number=0,
                        field_name="total credit", warnings=total_notes
                    )
                    totals = {
                        "debit_amount": float(debit_total), 
                        "credit_amount": float(credit_total)
                    }
                continue
            
            if re.search(
                r"\b(opening balance|closing balance|end of statement|page\s+\d+)\b", 
                row_text, 
                re.IGNORECASE
            ):
                continue
            
            if date is None and not has_amount and account_name and previous is not None:
                previous["account_name"] = (
                    previous["account_name"] + " " + account_name
                ).strip()
                continue
            
            if date is None and not has_amount:
                continue

            row_number = len(output) + 1
            row_notes: List[str] = []
            if date is None:
                row_notes.append(f"Row {row_number}: date could not be parsed.")
                
            debit = _money(
                cells.get("debit", ""), row_number=row_number,
                field_name="debit", warnings=row_notes
            )
            credit = _money(
                cells.get("credit", ""), row_number=row_number,
                field_name="credit", warnings=row_notes
            )
            if not cells.get("debit") and not cells.get("credit") and amount_text:
                amount = _money(
                    amount_text, row_number=row_number, field_name="amount",
                    warnings=row_notes
                )
                debit, credit = (
                    (abs(amount), Decimal("0.00"))
                    if amount < 0
                    else (Decimal("0.00"), abs(amount))
                )
                
            balance_text = cells.get("balance", "")
            balance = (
                _money(balance_text, row_number=row_number, field_name="balance", warnings=row_notes)
                if balance_text
                else None
            )
            transaction_date = date.date().isoformat() if date else ""
            ledger_id = cells.get("txn_id", "").strip() or (
                f"{source_path.stem}-{row.page_number}-{row_number}"
            )
            record = {
                "ledger_id": ledger_id,
                "transaction_date": transaction_date,
                "account_name": account_name,
                "account_number": metadata.get("account_number") or "",
                "debit_amount": _format_money(debit),
                "credit_amount": _format_money(credit),
                "reference_id": cells.get("txn_id", "").strip(),
                "ledger_type": layout.ledger_type or "unified_general_ledger",
                "bank_name": bank_name,
                "holder": metadata.get("holder") or "",
                "category": layout.ledger_type or "unified_general_ledger",
            }
            
            output.append(record)
            row_warnings.append(row_notes)
            balances.append(float(balance) if balance is not None else None)
            previous = record

        if not output:
            raise ValueError("No ledger transaction rows were found in the PDF table.")
        
        return ParseResult(
            headers=LEDGER_HEADERS, 
            rows=output, 
            metadata=metadata, 
            warnings=warnings, 
            row_warnings=row_warnings, 
            row_balances=balances, 
            totals=totals
        )


def write_csv(
    path: Path, 
    headers: List[str], 
    rows: List[Dict[str, str]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
