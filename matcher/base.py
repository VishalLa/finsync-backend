from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from langchain_ollama import ChatOllama

from app.shared.settings import Settings
from schema import BankStatement, LedgerFormat

from .fuzzy_matcher.business_calendar import BusinessCalendar
from .utils.helper import _safe_amount


class Matcher(ABC):
    """Common state and execution interface for reconciliation phases."""

    def __init__(
        self,
        pending_ledger: Optional[List[LedgerFormat]] = None,
        pending_bank: Optional[List[BankStatement]] = None,
        *,
        config: Optional[Settings] = None,
        llm: Optional[ChatOllama] = None,
        same_side: bool = True,
        tolerances: Optional[Dict[str, float | int]] = None,
        calendar: Optional[BusinessCalendar] = None,
        enable_llm: bool = True,
    ) -> None:
        self.ledger_pool: List[LedgerFormat] = list(pending_ledger or [])
        self.bank_pool: List[BankStatement] = list(pending_bank or [])
        self.config = config
        self.llm = llm
        self.same_side = same_side
        self.tolerances: Dict[str, float | int] = dict(tolerances or {})
        self.calendar = calendar or BusinessCalendar.india_banks()
        self.enable_llm = enable_llm

        ledger_out_attr = "debit_amount" if same_side else "credit_amount"
        ledger_in_attr = "credit_amount" if same_side else "debit_amount"
        self.gl_out: Callable[[LedgerFormat], float] = lambda record: _safe_amount(
            record, ledger_out_attr
        )
        self.gl_in: Callable[[LedgerFormat], float] = lambda record: _safe_amount(
            record, ledger_in_attr
        )
        self.bank_out: Callable[[BankStatement], float] = lambda record: _safe_amount(
            record, "debit_amount"
        )
        self.bank_in: Callable[[BankStatement], float] = lambda record: _safe_amount(
            record, "credit_amount"
        )

    @abstractmethod
    def match(self, *args: Any, **kwargs: Any) -> Any:
        """Run this matcher phase and return its phase result."""
        raise NotImplementedError
