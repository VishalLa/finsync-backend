from __future__ import annotations

from bisect import bisect_left
from typing import List, Optional, Dict, Any, Tuple
from schema import BankStatement, LedgerFormat
from utils.helper import (
    extract_utr,
    text_similarity,
    _get_iso_date_str,
    _safe_amount,
    _safe_str,
    _to_date,
)

_DEFAULT_AMOUNT_TOL = 0.05
_MAX_CANDIDATES_PER_LEDGER = 50
_REF_TIER = 2
_UTR_TIER = 1
_NONE_TIER = 0


def _cents(x: float) -> int:
    return int(round(abs(x) * 100))


def _date_key(rec: object, is_bank: bool) -> Optional[str]:
    raw = _get_iso_date_str(rec=rec, is_bank=is_bank)
    if not raw:
        return None
    d = _to_date(raw)
    return d.isoformat() if d else raw


def exact_match(
    gl_records: List[LedgerFormat], 
    bank_records: List[BankStatement],
    same_side: bool = True,        # True = cashbook, False = standard GL
    amount_tol: float = _DEFAULT_AMOUNT_TOL,  # driven by TOLERANCES["EXACT"]
    strict_ref_conflict: bool = False,
    show: bool = True
) -> Dict[str, Any]:
    tol_c = _cents(amount_tol)

    gl_dates = [_date_key(g, False) for g in gl_records]
    bk_dates = [_date_key(b, True) for b in bank_records]
    
    gl_ref = [_safe_str(g, "reference_id").strip() for g in gl_records]
    bk_ref = [_safe_str(b, "txn_id").strip() for b in bank_records]
    
    gl_utr = [
        extract_utr(getattr(g, "reference_id", None)) or extract_utr(getattr(g, "account_name", None))
        for g in gl_records
    ]
    bk_utr = [
        extract_utr(getattr(b, "txn_id", None)) or extract_utr(getattr(b, "narration", None))
        for b in bank_records
    ]
    
    # bank index: (date, direction) -> sorted [(paise, bank_idx)]
    index: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
    for bi, bank in enumerate(bank_records):
        d = bk_dates[bi]
        if not d:
            continue
        deb = _cents(_safe_amount(bank, "debit_amount"))
        cre = _cents(_safe_amount(bank, "credit_amount"))
        if deb > 0:
            index.setdefault((d, "debit"), []).append((deb, bi))
        if cre > 0:
            index.setdefault((d, "credit"), []).append((cre, bi))
    for lst in index.values():
        lst.sort()
    
    # generate candidate edges
    # edge = (-tier, -text_sim, amount_diff_paise, gi, bi, matched_amount)
    edges: List[tuple] = []
    competing: Dict[int, int] = {}      # gi -> number of surviving candiates
    for gi, gl in enumerate(gl_records):
        d = gl_dates[gi]
        if not d:
            continue
        
        probes: List[Tuple[str, float]] = []
        gl_debit = _safe_amount(gl, "debit_amount")
        gl_credit = _safe_amount(gl, "credit_amount")
        if gl_debit > 0:
            probes.append(("debit" if same_side else "credit", gl_debit))
        if gl_credit > 0:
            probes.append(("credit" if same_side else "debit", gl_credit))
            
        gl_edges: List[tuple] = []
        for bank_dir, amount in probes:
            lst = index.get((d, bank_dir))
            if not lst:
                continue
            
            c = _cents(amount)
            lo = bisect_left(lst, (c - tol_c, -1))
            hi = bisect_left(lst, (c + tol_c + 1, -1))
            
            for bank_cents, bi in lst[lo:hi]:
                # confirmation tier & conflict veto
                if gl_ref[gi] and bk_ref[bi] and gl_ref[gi] == bk_ref[bi]:
                    tier = _REF_TIER
                elif gl_utr[gi] and bk_utr[bi] and gl_utr[gi] == bk_utr[bi]:
                    tier = _UTR_TIER
                else:
                    tier = _NONE_TIER
                    if gl_utr[gi] and bk_utr[bi]:
                        continue
                    if strict_ref_conflict and gl_ref[gi] and bk_utr[bi]:
                        continue
                    
                sim = 0.0
                if tier == _NONE_TIER:
                    sim = text_similarity(
                        account_name=_safe_str(gl, "account_name"), 
                        narration=_safe_str(bank_records[bi], "narration")
                    )
                gl_edges.append((-tier, -sim, abs(bank_cents - c), gi, bi, amount))
                
        if gl_edges:
            gl_edges.sort()
            competing[gi] = len(gl_edges)
            competing[gi] = len(gl_edges)
            edges.extend(gl_edges[:_MAX_CANDIDATES_PER_LEDGER])
    
    edges.sort()
    ledger_used = [False] * len(gl_records)
    bank_used = [False] * len(bank_records)
    exact_matches: List[Dict[str, Any]] = []
    
    for neg_tier, _neg_sim, _diff, gi, bi, matched_amount in edges:
        if ledger_used[gi] or bank_used[bi]:
            continue
        
        tier = -neg_tier
        ledger_used[gi] = True
        bank_used[bi] = True
        gl = gl_records[gi]
        bank = bank_records[bi]
        exact_matches.append({
            "ledger_id": gl.ledger_id,
            "bank_id": bank.row_index,
            "amount": matched_amount,
            "date": gl_dates[gi],
            "reference_matched": tier > _NONE_TIER,
            "confirmation_method": (
                "reference_id" if tier == _REF_TIER
                else "narration_utr" if tier == _UTR_TIER
                else "amount_date_only"
            ),
            # >1 means other bank rows were equally plausible on date+amount;
            # useful for lowering confidence downstream.
            "competing_candidates": competing.get(gi, 1),
        })
        
    pending_ledger = [g for gi, g in enumerate(gl_records) if not ledger_used[gi]]
    pending_bank = [b for bi, b in enumerate(bank_records) if not bank_used[bi]]

    return {
        "EXACT_MATCHES": exact_matches,
        "PENDING_FUZZY_LEDGER": pending_ledger,
        "PENDING_FUZZY_BANK": pending_bank,
    }

