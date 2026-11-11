from __future__ import annotations

import re
import json
import logging
from datetime import  datetime
from typing import List, Dict, Any, Optional, Tuple, Union

from pydantic import BaseModel
from langchain_ollama import ChatOllama
from langchain_core.output_parsers import JsonOutputParser

from shared.settings import Settings
from schema import LedgerFormat, BankStatement
from ..fuzzy_matcher.subset_sum import find_subsets
from ..utils.helper import _amounts_equal, _to_paise

MAX_SUBSET_OPTIONS: int = 20
MAX_SUBSET_SEARCH_NODES: int = 200_000
MAX_SUBSET_SIZE: int = 6

# Shared LLM instance
_SHARED_LLM: Optional[ChatOllama] = None
logger = logging.getLogger(__name__)


def _prepare_llm(config: Settings) -> ChatOllama:
    try:
        return ChatOllama(
            model=config.OLLAMA_NAME,
            temperature=0.0,
            num_ctx=8192,
            repeat_penalty=1.1,
            base_url=config.OLLAMA_URL
        )
    except Exception as exc:
        logger.exception("Failed to initialize ChatOllama")
        raise RuntimeError(f"Failed to initialize ChatOllama: {exc}") from exc

def get_shared_llm(config: Settings) -> ChatOllama:
    global _SHARED_LLM
    if _SHARED_LLM is None:
        _SHARED_LLM = _prepare_llm(config=config)
    return _SHARED_LLM

def reset_shared_llm() -> None:
    global _SHARED_LLM
    _SHARED_LLM = None


# JSON parsing (LLM output is not always clean JSON)
def _safe_parse_json(
    llm_output: Any,
    schema: type[BaseModel],
) -> Optional[Any]:
    text = llm_output.content if hasattr(llm_output, "content") else str(llm_output)

    parser = JsonOutputParser(pydantic_object=schema)
    try:
        parsed = parser.invoke(text)
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
        return parsed
    except Exception:
        cleaned = re.sub(r"```(?:json)?\s*", "", text).rstrip(".")
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        found   = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)

        if found:
            cleaned = found.group(0)
        
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return schema.model_validate(data)
            return data
        except Exception:
            return None

def _make_parser(schema: type[BaseModel]):
    def _parse(llm_output):
        return _safe_parse_json(llm_output, schema)
    return _parse


# Prompt formatting
def _format_record_for_prompt(
    rec: Dict[str, Any],
    is_bank: bool
) -> str:
    dr_key = "debit_amount"
    cr_key = "credit_amount"
    id_key = "row_index" if is_bank else "ledger_id"
    dt_key = "date"      if is_bank else "transaction_date"
    nm_key = "narration" if is_bank else "account_name"

    return (
        f"- {rec.get(id_key, 'N/A')} | "
        f"{rec.get(dt_key, 'N/A')} | "
        f"Dr:{rec.get(dr_key, 0.0):.2f} Cr:{rec.get(cr_key, 0.0):.2f} | "
        f"NARR: {rec.get(nm_key, 'N/A')}"
    )
    

# Direction / amount helpers
def _get_direction_amount(
    template: Union[LedgerFormat | BankStatement]
) -> Tuple[float, str]:
    if template.debit_amount and template.debit_amount > 0:
        return template.debit_amount, "debit"
    if template.credit_amount and template.credit_amount > 0:
        return template.credit_amount, "credit"
    return 0.0, "unknown"


def _directions_compatible(
    gl_dir: str,
    bk_dir: str,
    same_side: bool = True
) -> bool:
    if "unknown" in (gl_dir, bk_dir):
        return True
    return gl_dir == bk_dir if same_side else gl_dir != bk_dir


# Candidate filtering (used by the many-to-one residual matcher)
def _filter_candidates_for_bank(
    bank: BankStatement,
    ledger_pool: List[LedgerFormat],
    date_window: int,
    tol: float
) -> List[LedgerFormat]:
    bk_amt, _ = _get_direction_amount(bank)
    if not bank.date:
        return []

    try:
        bk_dt = datetime.strptime(bank.date[:10], "%Y-%m-%d").date()
    except ValueError:
        return []

    scored: List[Tuple[int, LedgerFormat]] = []
    for gl in ledger_pool:
        if not gl.transaction_date:
            continue

        try:
            gl_dt = datetime.strptime(gl.transaction_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        day_diff = abs((bk_dt - gl_dt).days)
        if day_diff > date_window:
            continue

        gl_amt, _ = _get_direction_amount(gl)
        if _to_paise(gl_amt) <= _to_paise(bk_amt) + _to_paise(tol):
            scored.append((day_diff, gl))

    scored.sort(key=lambda x: x[0])
    return [gl for _, gl in scored]


def _find_candidate_subsets_for_bank(
    bank: BankStatement,
    ledger_pool: List[LedgerFormat],
    date_window: int,
    tol: float,
    same_side: bool = True,
) -> Tuple[List[Tuple[LedgerFormat, ...]], bool]:
    bank_amount, bank_direction = _get_direction_amount(bank)
    if bank_amount <= 0:
        return [], True

    candidates = []
    for ledger in _filter_candidates_for_bank(bank, ledger_pool, date_window, tol):
        ledger_amount, ledger_direction = _get_direction_amount(ledger)
        if ledger_amount <= 0 or not _directions_compatible(
            ledger_direction, bank_direction, same_side
        ):
            continue
        candidates.append(ledger)

    if len(candidates) < 2:
        return [], True

    candidate_amounts = [
        int(round(_get_direction_amount(ledger)[0] * 100))
        for ledger in candidates
    ]
    search = find_subsets(
        candidate_amounts,
        target=int(round(bank_amount * 100)),
        tol=int(round(tol * 100)),
        min_size=2,
        max_size=MAX_SUBSET_SIZE,
        max_nodes=MAX_SUBSET_SEARCH_NODES,
        max_solutions=MAX_SUBSET_OPTIONS + 1,
    )
    subsets = [
        tuple(candidates[index] for index in indexes)
        for indexes in search.solutions
    ]
    return subsets, search.exhaustive


# 1-to-1 Math Bouncer
def _passes_1to1_bouncer(
    gl_item: LedgerFormat,
    bk_item: BankStatement,
    tol: float,
    same_side: bool = True
) -> Tuple[bool, str]:
    gl_amt, gl_dir = _get_direction_amount(gl_item)
    bk_amt, bk_dir = _get_direction_amount(bk_item)

    diff = abs(gl_amt - bk_amt)
    if not _amounts_equal(gl_amt, bk_amt, tol):
        return False, (
            f"Amount mismatch: GL={gl_amt:.2f} Bank={bk_amt:.2f} "
            f"diff={diff:.2f} exceeds tol={tol:.2f}"
        )

    if not _directions_compatible(gl_dir, bk_dir, same_side):
        return False, (
            f"Direction mismatch: GL is {gl_dir}, Bank is {bk_dir} "
            f"(same_side={same_side})"
        )

    return True, ""


# Many-to-one Math Bouncer 

def _passes_many_to_one_bouncer(
    gl_items: List[LedgerFormat],
    bk_item: BankStatement,
    tol: float,
    same_side: bool = True
) -> Tuple[bool, str]:
    if not gl_items:
        return False, "No ledger items resolved from suggested IDs."

    bk_amt, bk_dir = _get_direction_amount(bk_item)
    gl_total_paise = 0

    for gl in gl_items:
        gl_amt, gl_dir = _get_direction_amount(gl)
        if not _directions_compatible(gl_dir, bk_dir, same_side):
            return False, (
                f"Direction mismatch on {gl.ledger_id}: "
                f"GL is {gl_dir}, Bank is {bk_dir}"
            )
        gl_total_paise += _to_paise(gl_amt)

    diff_paise = abs(gl_total_paise - _to_paise(bk_amt))
    if diff_paise > _to_paise(tol):
        diff = diff_paise / 100
        return False, (
            f"Sum mismatch: ΣGL={gl_total_paise / 100:.2f} Bank={bk_amt:.2f} "
            f"diff={diff:.2f} exceeds tol={tol:.2f}"
        )

    return True, ""
