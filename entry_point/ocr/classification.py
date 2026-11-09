from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

try:
    from rapidfuzz.fuzz import ratio as _fuzzy_ratio
except ImportError:
    _fuzzy_ratio = None


@dataclass(frozen=True)
class DocumentLayout:
    kind: str
    bank_name: str
    ledger_type: Optional[str] = None
    parser_name: Optional[str] = None


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

    if (
        _has_fuzzy_phrase(text, "HDFC BANK", 0.78)
        or _has_fuzzy_phrase(text, "Withdrawal Amt", 0.72)
    ):
        return DocumentLayout(
            kind="bank", 
            bank_name="HDFC Bank", 
            parser_name="hdfc"
        )
        
    if (
        _has_fuzzy_phrase(text, "AXIS BANK", 0.78)
        or _has_fuzzy_phrase(text, "Tran Date", 0.72)
    ):
        return DocumentLayout(
            kind="bank", 
            bank_name="Axis Bank", 
            parser_name="axis"
        )
        
    return DocumentLayout(kind="bank", bank_name="UNKNOWN")
