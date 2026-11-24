from dataclasses import asdict, dataclass, field, fields
from typing import Any

SCHEMA_VERSION = 1

# task names (the prefix before the dot picks the queue, see config.TASK_ROUTES)
T_ENTRY_PROCESS = "entry.process_run"
T_OCR_EXTRACT = "ocr.extract_file"
T_MATCHER_RUN = "matcher.run"
T_LEDGER_POST = "ledger.post_run"
T_SERVICE_SAVE = "service.save_results"
T_REPORT_BUILD = "report.build"
T_DB_STORE_INPUTS = "db.store_inputs"
T_DB_STORE_RESULTS = "db.store_results"
T_DB_MARK_FAILED = "db.mark_run_failed"


class BadMessage(ValueError):
    """The message does not match the contract. Do not retry; fix the sender."""


class _Msg:
    @classmethod
    def from_dict(cls, data: Any):
        if not isinstance(data, dict):
            raise BadMessage(f"{cls.__name__}: expected a dict, got {type(data).__name__}")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise BadMessage(
                f"{cls.__name__}: schema_version {data.get('schema_version')!r}, "
                f"this worker understands {SCHEMA_VERSION}"
            )
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise BadMessage(f"{cls.__name__}: unknown fields {sorted(unknown)}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise BadMessage(f"{cls.__name__}: {exc}") from exc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EntryProcessMsg(_Msg):
    """API -> entry_point.queue: raw uploads are saved, please parse them."""
    run_id: str
    user_id: str
    ledger_ref: str  # runs/<run_id>/raw/ledger.xlsx
    bank_ref: str    # runs/<run_id>/raw/bank.xlsx
    options: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION


@dataclass
class OcrExtractMsg(_Msg):
    """Entry Point -> ocr.queue: extract data from one pdf/png/jpg bill."""
    run_id: str
    user_id: str
    file_ref: str
    kind: str = "bill"
    schema_version: int = SCHEMA_VERSION


@dataclass
class MatcherRunMsg(_Msg):
    """Entry Point -> matcher.queue: normalized record files are ready."""
    run_id: str
    user_id: str
    ledger_ref: str  # runs/<run_id>/norm/ledger.json
    bank_ref: str    # runs/<run_id>/norm/bank.json
    bank_name: str = "UNKNOWN"
    template_version: str = "v1"
    options: dict = field(default_factory=dict)  # JSON-safe flags only
    schema_version: int = SCHEMA_VERSION


@dataclass
class LedgerPostMsg(_Msg):
    """Entry Point -> ledger.queue: bills are extracted, post journal entries."""
    run_id: str
    user_id: str
    bills_ref: str  # runs/<run_id>/norm/bills.json
    schema_version: int = SCHEMA_VERSION


@dataclass
class ServiceSaveMsg(_Msg):
    """Matcher / Ledger -> service.queue: a result file is ready, store it."""
    run_id: str
    user_id: str
    result_ref: str  # runs/<run_id>/results/matcher_result.json
    source: str = "matcher"
    schema_version: int = SCHEMA_VERSION


@dataclass
class DBPersistInputsMsg(_Msg):
    """Entry Point -> DB writer: normalized records are ready to persist."""
    run_id: str
    user_id: str
    ledger_ref: str
    bank_ref: str
    metadata_ref: str
    schema_version: int = SCHEMA_VERSION


@dataclass
class DBPersistResultsMsg(_Msg):
    """Service -> DB writer: matcher results are ready to persist."""
    run_id: str
    user_id: str
    result_ref: str
    schema_version: int = SCHEMA_VERSION


@dataclass
class DBMarkFailedMsg(_Msg):
    """Any worker -> DB writer: a stage failed permanently."""
    run_id: str
    user_id: str
    error: str
    schema_version: int = SCHEMA_VERSION


@dataclass
class ReportMsg(_Msg):
    """Service -> report.queue: build an Excel report."""
    run_id: str
    user_id: str
    kind: str  # bank-reconciliation | gstr1 | journal | ledger | tds
    params: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION