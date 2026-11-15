from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import List, Tuple

from .common import DocumentLayout
from schema.template import TEMPLATE_REGISTRY

try:
    from rapidfuzz.fuzz import ratio as _fuzzy_ratio
except ImportError:
    _fuzzy_ratio = None


LEDGER_TYPE_REGISTRY = {
    "unified_general_ledger": "unified_general_ledger",
}


def _normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _similarity(text: str, phrase: str) -> float:
    left, right = _normalized(text), _normalized(phrase)
    if not left or not right:
        return 0.0
    
    if right in left:
        return 1.0
    
    if _fuzzy_ratio:
        return _fuzzy_ratio(left, right) / 100
    
    return SequenceMatcher(None, left, right).ratio()


def _has_fuzzy_phrase(
    text: str, 
    phrase: str, 
    threshold: float = 0.78
) -> bool:
    normalized_text = _normalized(text)
    normalized_phrase = _normalized(phrase)
    
    if normalized_phrase in normalized_text:
        return True
    
    words = normalized_text.split()
    phrase_words = normalized_phrase.split()
    width = len(phrase_words)
    
    if width == 0:
        return False
    
    candidates = [
        " ".join(words[index:index + width])
        for index in range(max(1, len(words) - width + 1))
    ]
    
    return any(
        _similarity(candidate, normalized_phrase) >= threshold 
        for candidate in candidates
    )


def _select_date_header_version(template_key: str, text: str) -> str:
    normalized_text = _normalized(text)
    if template_key.startswith("PNB_"):
        return "PNB_V1" if "transction date" in normalized_text else "PNB_V2"
    
    if template_key.startswith("YES_"):
        return "YES_V1" if "transction date" in normalized_text else "YES_V2"
    
    return template_key


def classify_document(
    first_page_text: str
) -> DocumentLayout:
    text = first_page_text or ""
    if _has_fuzzy_phrase(text, "UNIFIED GENERAL LEDGER", 0.76):
        if (
            _has_fuzzy_phrase(text, "Txn ID", 0.68)
            or _has_fuzzy_phrase(text, "Bank Balance", 0.72)
        ):
            return DocumentLayout(
                kind="ledger",
                bank_name="UNKNOWN",
                ledger_type=LEDGER_TYPE_REGISTRY["unified_general_ledger"],
                parser_name="unified_general_ledger",
            )

    detection_rules: List[Tuple[str, List[str]]] = [
        ("HDFC_V1", ["HDFC BANK", "Withdrawal Amt.", "Deposit Amt."]),
        ("AXIS_V1", ["AXIS BANK", "Tran Date", "Particulars"]),
        ("ICICI_V1", ["ICICI BANK", "Transaction Remarks", "Withdrawal Amount (INR)"]),
        ("IDBI_V1", ["IDBI BANK", "Type", "Amount", "Description"]),
        ("KOTAK_V1", ["KOTAK BANK", "Chq/Ref Number", "Debit", "Credit"]),
        ("PNB_V1", ["PUNJAB NATIONAL BANK", "Transction Date", "Withdrawal", "Deposit"]),
        ("PNB_V2", ["PUNJAB NATIONAL BANK", "Transaction Date", "Withdrawal", "Deposit"]),
        ("SBI_V1", ["STATE BANK OF INDIA", "Txn Date", "Ref No./Cheque No."]),
        ("YES_V1", ["YES BANK", "Transction Date", "Debited Amount", "Credited Amount"]),
        ("YES_V2", ["YES BANK", "Transaction Date", "Debited Amount", "Credited Amount"]),
    ]
    
    for template_key, signals in detection_rules:
        brand_signal = signals[0]
        if _has_fuzzy_phrase(text, brand_signal, 0.78):
            template_key = _select_date_header_version(template_key, text)
            template = TEMPLATE_REGISTRY[template_key]
            return DocumentLayout(
                kind="bank",
                bank_name=template["bank_name"],
                parser_name=template_key,
            )

        header_signals = signals[1:]
        if header_signals and all(
            _has_fuzzy_phrase(text, signal, 0.7) for signal in header_signals
        ):
            template_key = _select_date_header_version(template_key, text)
            template = TEMPLATE_REGISTRY[template_key]
            return DocumentLayout(
                kind="bank",
                bank_name=template["bank_name"],
                parser_name=template_key,
            )

    if _has_fuzzy_phrase(text, "Description", 0.75) and _has_fuzzy_phrase(
        text, "Amount", 0.75
    ) and _has_fuzzy_phrase(text, "Balance", 0.75):
        template = TEMPLATE_REGISTRY["GENERIC_SIGNED_V1"]
        return DocumentLayout(
            kind="bank", 
            bank_name=template["bank_name"], 
            parser_name="GENERIC_SIGNED_V1"
        )

    if all(
        _has_fuzzy_phrase(text, header, 0.7)
        for header in ("Date", "Txn_ID", "Narration", "Debit", "Credit", "Balance")
    ):
        template = TEMPLATE_REGISTRY["GENERIC_V1"]
        return DocumentLayout(
            kind="bank", bank_name=template["bank_name"], parser_name="GENERIC_V1"
        )

    return DocumentLayout(kind="bank", bank_name="UNKNOWN")
