from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple, Set

from schema import RecognizedPattern

from utils.helper import _STOPWORDS, _INDIAN_PREFIX_RE, _amounts_equal

_VPA_RE = re.compile(r"@[a-z0-9.\-]+", re.I)
_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE  = re.compile(r"[^a-z0-9 ]+")

_SIGNATURE_NOISE = set(_STOPWORDS) | {
    "pvt", "private", "limited", "india", "indian", "ltd", "llp",
    "pay", "paid", "payu", "paytm", "phonepe", "gpay", "bhim", "ybl", "ibl",
    "okicici", "okhdfcbank", "oksbi", "okaxis", "axl", "apl",
    "bank", "branch", "clg", "chq", "cheque", "cash",
    "by", "to", "from", "via", "for", "of", "and",
}

_MIN_TOKEN_LEN = 2
_MIN_CORE_LEN = 3


def _signature_tokens(text: Optional[str]) -> List[str]:
    if not text:
        return []
    
    t = str(text).lower()
    t = _VPA_RE.sub(" ", t)
    t = _INDIAN_PREFIX_RE.sub(" ", t)
    t = _NON_ALNUM_RE.sub(" ", t)
    t = _WHITESPACE_RE.sub(" ", t).strip()
    
    tokens: Set[str] = set()
    for tok in t.split():
        if any(ch.isdigit() for ch in tok):
            continue
        if len(tok) < _MIN_TOKEN_LEN or tok in _SIGNATURE_NOISE:
            continue
        tokens.add(tok)
    return sorted(tokens)


def _normalize_signature(text: Optional[str]) -> str:
    tokens = _signature_tokens(text)
    if not any(len(tok) >= _MIN_CORE_LEN for tok in tokens):
        return ""
    return " ".join(tokens)


def _pattern_key(ledger_sig: str, bank_sig: str) -> str:
    return f"{ledger_sig}::{bank_sig}"


def _has_core(tokens: Set[str]) -> bool:
    return any(len(t) >= _MIN_CORE_LEN for t in tokens)


class MemoryBackend(Protocol):
    def load(self) -> Dict[str, dict]: ...
    def save(self, data: Dict[str, dict]): ...


class JSONFileBackend:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    

class InMemoryBackend:

    def __init__(self) -> None:
        self._data: Dict[str, dict] = {}

    def load(self) -> Dict[str, dict]:
        return dict(self._data)

    def save(self, data: Dict[str, dict]) -> None:
        self._data = dict(data)


class SQLAlchemyBackend:

    from sqlalchemy.orm import Session
    def __init__(
        self, 
        session_factory: Session, 
        model_cls=None
    ) -> None:
        self._session_factory = session_factory
        self._model_cls = model_cls

    def _get_model_cls(self):
        if self._model_cls is not None:
            return self._model_cls
        from database import MatchPatternModel
        self._model_cls = MatchPatternModel
        return self._model_cls

    def load(self) -> Dict[str, dict]:
        model_cls = self._get_model_cls()
        session = self._session_factory()
        try:
            rows = session.query(model_cls).all()
            return {row.pattern_key: row.to_dict() for row in rows}
        finally:
            session.close()

    def save(self, data: Dict[str, dict]) -> None:
        model_cls = self._get_model_cls()
        session = self._session_factory()

        try:
            existing = {
                row.pattern_key: row
                for row in session.query(model_cls).all()
            }
            seen_keys = set()

            for key, d in data.items():
                seen_keys.add(key)
                row = existing.get(key)
                if row is not None:
                    row.ledger_signature = d.get("ledger_signature", "")
                    row.bank_signature   = d.get("bank_signature", "")
                    row.match_phase      = d.get("match_phase", "fuzzy")
                    row.adjustment_type  = d.get("adjustment_type")
                    row.times_seen       = int(d.get("times_seen", 1))
                else:
                    session.add(model_cls(
                        pattern_key=key,
                        ledger_signature=d.get("ledger_signature", ""),
                        bank_signature=d.get("bank_signature", ""),
                        match_phase=d.get("match_phase", "fuzzy"),
                        adjustment_type=d.get("adjustment_type"),
                        times_seen=int(d.get("times_seen", 1)),
                    ))

            for key, row in existing.items():
                if key not in seen_keys:
                    session.delete(row)

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class MatchMemory:
    def __init__(
        self, 
        backend: Optional[MemoryBackend] = None
    ) -> None:
        self.backend = backend or InMemoryBackend()
        self._patterns: Dict[str, RecognizedPattern] = {}
        self._by_ledger: Dict[str, List[RecognizedPattern]] = {}
        self._index_dirty = True
        
        for d in self.backend.load().values():
            p = RecognizedPattern.from_dict(d)
            p.ledger_signature = _normalize_signature(p.ledger_signature)
            p.bank_signature = _normalize_signature(p.bank_signature)
            
            if not p.ledger_signature or not p.bank_signature:
                continue
            key = _pattern_key(p.ledger_signature, p.bank_signature)
            existing = self._patterns.get(key)
            if existing:
                existing.times_seen += p.times_seen
            else:
                self._patterns[key] = p
    
    
    def _ensure_index(self) -> None:
        if not self._index_dirty:
            return
        self._by_ledger = {}
        for p in self._patterns.values():
            self._by_ledger.setdefault(p.ledger_signature, []).append(p)
        self._index_dirty = False


    def lookup(
        self, 
        ledger_text: Optional[str], 
        bank_text:   Optional[str]
    ) -> Optional[RecognizedPattern]:
        ledger_sig = _normalize_signature(ledger_text)
        bank_tokens = set(_signature_tokens(bank_text))
        if not ledger_text or not bank_tokens:
            return None
        
        self._ensure_index()
        best: Optional[RecognizedPattern] = None
        for p in self._by_ledger.get(ledger_sig, []):
            sorted = set(p.bank_signature.split())
            if sorted and sorted <= bank_tokens:
                if best is None or len(sorted) > len(best.bank_signature.split()):
                    best = p
        
        return best
    
    
    def is_recognized(
        self,
        ledger_text: Optional[str],
        bank_text: Optional[str]
    ) -> bool:
        return self.lookup(
            ledger_text=ledger_text,
            bank_text=bank_text
        ) is not None


    def record_match(
        self,
        ledger_text:     Optional[str],
        bank_text:       Optional[str],
        match_phase:     str,
        adjustment_type: Optional[str] = None,
    ) -> None:
        ledger_sig = _normalize_signature(ledger_text)
        bank_tokens = set(_signature_tokens(bank_text))
        if not ledger_sig or not _has_core(bank_tokens):
            return  # nothing stable to key on

        self._ensure_index()
        for p in self._by_ledger.get(ledger_sig, []):
            stored = set(p.bank_signature.split())
            common = stored & bank_tokens
            if not _has_core(common):
                continue
            
            new_sig = " ".join(stored(common))
            if new_sig != p.bank_signature:
                self._patterns.pop(
                    _pattern_key(
                        ledger_sig=ledger_sig, 
                        bank_sig=p.bank_signature), 
                    None
                )
                p.bank_signature = new_sig
                clash = self._patterns.get(
                    _pattern_key(
                        ledger_sig=ledger_sig,
                        bank_sig=new_sig
                    )
                )
                if clash is not None and clash is not p:
                    clash.times_seen += p.times_seen
                    p = clash
                else:
                    self._patterns[_pattern_key(
                        ledger_sig=ledger_sig, 
                        bank_sig=new_sig
                    )] = p
                self._index_dirty = True
            
            p.times_seen += 1
            p.match_phase = match_phase
            p.adjustment_type = adjustment_type
            return
        
        new_p = RecognizedPattern(
            ledger_signature=ledger_sig,
            bank_signature=" ".join(sorted(bank_tokens)),
            match_phase=match_phase,
            adjustment_type=adjustment_type
        )
        self._patterns[_pattern_key(
            ledger_sig=new_p.ledger_signature,
            bank_sig=new_p.bank_signature
        )] = new_p
        self._index_dirty = True
        
        
    def record_matches_from_records(
        self,
        matches:    List[dict],
        gl_by_id:   Dict[str, str],   
        bank_by_id: Dict[str, str],   
        match_phase: str,
    ) -> None:
        for m in matches:
            lid = str(m.get("ledger_id", ""))
            bid = str(m.get("bank_id", ""))
            ledger_text = gl_by_id.get(lid)
            bank_text   = bank_by_id.get(bid)
            if ledger_text is None or bank_text is None:
                continue
            self.record_match(
                ledger_text, bank_text,
                match_phase=match_phase,
                adjustment_type=m.get("adjustment_type"),
            )


    def stats(self) -> dict:
        return {
            "total_patterns": len(self._patterns),
            "recurring_patterns": sum(
                1 for p in self._patterns.values() if p.times_seen > 1
            ),
        }


    def save(self) -> None:
        self.backend.save(
            {k: p.to_dict() for k, p in self._patterns.items()}
        )


def memory_matcher(
    residual_ledger: list,
    residual_bank:   list,
    memory: "MatchMemory",
    same_side: bool,
    amount_tol: float,
) -> Tuple[List[dict], list, list]:
    memory_matches: List[dict] = []
    
    gl_by_id = {}
    for record in residual_ledger:
        record_id = str(getattr(record, "ledger_id", ""))
        if record_id in gl_by_id:
            raise ValueError(f"Duplicate ledger ID in memory matching pool: {record_id}")
        gl_by_id[record_id] = record
        
    bank_by_id = {}
    for record in residual_bank:
        record_id = str(getattr(record, "row_index", ""))
        if record_id in bank_by_id:
            raise ValueError(f"Duplicate bank ID in memory matching pool: {record_id}")
        bank_by_id[record_id] = record
        
    used_ledger: set[str] = set()
    used_bank: set[str] = set()

    def _safe_amt(rec, attr: str) -> float:
        try:
            v = getattr(rec, attr, 0.0)
            return float(v) if v not in (None, "") else 0.0
        except (TypeError, ValueError):
            return 0.0

    for ledger_id, gl in gl_by_id.items():
        gl_debit  = _safe_amt(gl, "debit_amount")
        gl_credit = _safe_amt(gl, "credit_amount")
        account_name = getattr(gl, "account_name", "")

        found = None
        for bank_id, bank in bank_by_id.items():
            if bank_id in used_bank:
                continue
            narration = getattr(bank, "narration", "")
            if not memory.is_recognized(account_name, narration):
                continue

            b_debit  = _safe_amt(bank, "debit_amount")
            b_credit = _safe_amt(bank, "credit_amount")

            if same_side:
                amt_ok = (
                    (gl_debit  > 0 and _amounts_equal(gl_debit, b_debit, amount_tol)) or
                    (gl_credit > 0 and _amounts_equal(gl_credit, b_credit, amount_tol))
                )
            else:
                amt_ok = (
                    (gl_debit  > 0 and _amounts_equal(gl_debit, b_credit, amount_tol)) or
                    (gl_credit > 0 and _amounts_equal(gl_credit, b_debit, amount_tol))
                )

            if amt_ok:
                found = bank
                break

        if found is not None:
            pattern = memory.lookup(account_name, getattr(found, "narration", ""))
            inner_label = pattern.adjustment_type if pattern else None
            if inner_label and inner_label.startswith("Recognized Recurring Pattern"):
                inner_label = None
            memory_matches.append({
                "ledger_id":        getattr(gl, "ledger_id", None),
                "bank_id":          getattr(found, "row_index", None),
                "adjustment_type":  (
                    f"Recognized Recurring Pattern"
                    + (f" ({inner_label})" if inner_label else "")
                ),
                "confidence_score": "High",
                "details": (
                    f"Counterparty narration recognized from "
                    f"{pattern.times_seen if pattern else 1} prior run(s); "
                    f"amount and direction confirmed for this run."
                ),
                "match_phase": "memory",
            })
            used_ledger.add(ledger_id)
            used_bank.add(str(getattr(found, "row_index", "")))

    return (
        memory_matches,
        [record for record_id, record in gl_by_id.items() if record_id not in used_ledger],
        [record for record_id, record in bank_by_id.items() if record_id not in used_bank],
    )
