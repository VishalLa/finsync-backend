from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any, Dict, List

from entry_point.ocr.evaluation import evaluate_csv
from entry_point.ocr.pipeline import run_pipeline


BACKEND_DIR = Path(__file__).resolve().parents[1]
PDF_FIXTURES_DIR = BACKEND_DIR / "test" / "files"
EXTRACTED_DIR = BACKEND_DIR / "test" / "extracted"
GROUND_TRUTH_PATH = PDF_FIXTURES_DIR / "master_ground_truth.csv"
METRIC_FIELDS = ("amounts", "dates", "txn_ids")


def _metric_value(
    metrics: Dict[str, Any], field: str, measure: str
) -> Any:
    return metrics["splits"]["all"][field][measure]


def test_extract_sample_pdfs_and_write_evaluation_matrix(
    tmp_path: Path,
) -> None:
    pdf_paths = sorted(PDF_FIXTURES_DIR.glob("*.pdf"))
    assert pdf_paths, f"No PDF fixtures found in {PDF_FIXTURES_DIR}"
    assert GROUND_TRUTH_PATH.is_file(), (
        f"Ground-truth CSV not found: {GROUND_TRUTH_PATH}"
    )

    extracted_csv_dir = EXTRACTED_DIR / "csv"
    extracted_csv_dir.mkdir(parents=True, exist_ok=True)
    matrix_rows: List[Dict[str, Any]] = []
    extraction_errors: List[str] = []

    for pdf_path in pdf_paths:
        run_output = tmp_path / pdf_path.stem
        try:
            result = run_pipeline(
                str(pdf_path),
                str(run_output),
                mode="none",
                dry_run=True,
            )
        except Exception as exc:
            error = f"{pdf_path.name}: {type(exc).__name__}: {exc}"
            extraction_errors.append(error)
            row: Dict[str, Any] = {
                "source_pdf": pdf_path.name,
                "kind": "",
                "bank_name": "",
                "ledger_type": "",
                "rows": 0,
                "quarantined": False,
                "warnings": "",
                "csv": "",
                "metadata": "",
                "extraction_error": error,
                "evaluation_error": "",
            }
            for field in METRIC_FIELDS:
                for measure in ("precision", "recall", "accuracy"):
                    row[f"{field}_{measure}"] = None
            matrix_rows.append(row)
            continue

        documents = result["documents"]
        assert len(documents) == 1, (
            f"Expected one extracted document for {pdf_path.name}, "
            f"got {len(documents)}"
        )

        document = documents[0]
        assert document.row_count > 0, f"No transaction rows extracted from {pdf_path.name}"
        assert document.csv_path.is_file()
        assert document.metadata_path.is_file()

        destination_csv = extracted_csv_dir / document.csv_path.name
        destination_metadata = extracted_csv_dir / document.metadata_path.name
        shutil.copy2(document.csv_path, destination_csv)
        shutil.copy2(document.metadata_path, destination_metadata)

        row: Dict[str, Any] = {
            "source_pdf": pdf_path.name,
            "kind": document.layout.kind,
            "bank_name": document.layout.bank_name,
            "ledger_type": document.layout.ledger_type or "",
            "rows": document.row_count,
            "quarantined": document.quarantined,
            "warnings": " | ".join(document.warnings),
            "csv": str(destination_csv.relative_to(BACKEND_DIR)),
            "metadata": str(destination_metadata.relative_to(BACKEND_DIR)),
            "extraction_error": "",
            "evaluation_error": "",
        }
        try:
            metrics = evaluate_csv(destination_csv, GROUND_TRUTH_PATH)
        except ValueError as exc:
            row["evaluation_error"] = str(exc)
            for field in METRIC_FIELDS:
                for measure in ("precision", "recall", "accuracy"):
                    row[f"{field}_{measure}"] = None
        else:
            for field in METRIC_FIELDS:
                for measure in ("precision", "recall", "accuracy"):
                    row[f"{field}_{measure}"] = _metric_value(
                        metrics, field, measure
                    )
        matrix_rows.append(row)

    matrix_path = EXTRACTED_DIR / "metrics_matrix.csv"
    fieldnames = [
        "source_pdf", "kind", "bank_name", "ledger_type", "rows",
        "quarantined", "warnings", "csv", "metadata", "extraction_error",
        "evaluation_error",
        *(
            f"{field}_{measure}"
            for field in METRIC_FIELDS
            for measure in ("precision", "recall", "accuracy")
        ),
    ]
    with matrix_path.open("w", encoding="utf-8", newline="") as matrix_file:
        writer = csv.DictWriter(matrix_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(matrix_rows)

    assert len(matrix_rows) == len(pdf_paths)
    assert matrix_path.is_file()
    assert not extraction_errors, "\n".join(extraction_errors)
