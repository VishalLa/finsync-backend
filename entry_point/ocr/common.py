from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class DocumentLayout:
    kind: str
    bank_name: str
    ledger_type: Optional[str] = None
    parser_name: Optional[str] = None


@dataclass(frozen=True)
class OCRBox:
    text: str
    confidence: float
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def y_center(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)


@dataclass
class ExtractedPage:
    page_number: int
    text: str
    boxes: List[OCRBox]
    source: str
    warnings: List[str]


@dataclass
class TableRow:
    page_number: int
    boxes: List[OCRBox]

    @property
    def text(self) -> str:
        return " ".join(box.text for box in sorted(self.boxes, key=lambda item: item.x0))


@dataclass
class ParseResult:
    headers: List[str]
    rows: List[Dict[str, str]]
    metadata: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)
    row_warnings: List[List[str]] = field(default_factory=list)
    row_balances: List[Optional[float]] = field(default_factory=list)
    totals: Optional[Dict[str, float]] = None


@dataclass
class ProcessedDocument:
    source: Path
    csv_path: Path
    metadata_path: Path
    layout: DocumentLayout
    row_count: int
    warnings: List[str] = field(default_factory=list)
    quarantined: bool = False


class LayoutParser:
    layout_name = "generic"

    def parse(
        self,
        _pages: Sequence[ExtractedPage],
        _layout: DocumentLayout,
        _source_path: Path,
    ) -> ParseResult:
        raise NotImplementedError
