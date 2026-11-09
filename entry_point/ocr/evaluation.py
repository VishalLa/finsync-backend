from __future__ import annotations

import csv
import json
import re

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Set, Any

_ALIASES = {
    "debit": ("debit_amount", "debit", "withdrawal amt.", "withdrawal amt", "withdrawal amount"),
    "credit": ("credit_amount", "credit", "deposit amt.", "deposit amt", "deposit amount"),
    "amount": ("amount", "transaction amount"),
    "dates": ("transaction_date", "transaction date", "tran date", "date"),
    "txn_ids": (
        "txn_id", "txn id", "transaction id", "chq no", "chq./ref.no.",
        "reference_id", "reference id", "ref no", "ref",
    ),
}


def _column(
    headers: List[str], 
    aliases: Tuple[str, ...]
) -> Optional[str]:
    normalized = {
        re.sub(r"[^a-z0-9]+", " ", header.lower()).strip(): header
        for header in headers
    }
    
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()
        if key in normalized:
            return normalized[key]
        
    return None


def _canonical(
    field: str, 
    value: str
) -> str:
    text = value.strip()
    if field == "amounts":
        cleaned = re.sub(r"[^\d,.\-()]", "", text)
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
            
        try:
            return str(Decimal(cleaned.strip("()")).quantize(Decimal("0.01")))
        except InvalidOperation:
            return text
        
    if field == "dates":
        for fmt in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
            
    return text.casefold()


def _read_sidecar(csv_path: Path) -> Dict:
    sidecar = csv_path.with_suffix(".meta.json")
    if not sidecar.exists():
        return {}
    
    with sidecar.open(encoding="utf-8") as file:
        return json.load(file)


def _source_rows(
    truth_rows: List[Dict[str, str]], 
    truth_headers: list[str], 
    prediction: Path
) -> List[Dict[str, str]]:
    source_column = _column(truth_headers, ("appears_in_files",))
    metadata = _read_sidecar(prediction)
    
    if source_column and metadata.get("source_pdf"):
        source_filename = Path(str(metadata["source_pdf"])).name.casefold()
        expected_names = {
            prediction.stem.casefold(),
            Path(source_filename).stem.casefold(),
        }
        
        source_rows = []
        for row in truth_rows:
            appearance = row.get(source_column, "").strip()
            if source_filename and source_filename in appearance.casefold():
                source_rows.append(row)
                continue
            
            try:
                parsed_appearance = json.loads(appearance)
            except json.JSONDecodeError:
                parsed_appearance = None
                
            if isinstance(parsed_appearance, list):
                source_tokens = [
                    str(value) for value in parsed_appearance
                ]
            else:
                source_tokens = re.split(
                    r"[,;\s|]+", appearance.strip("[]{}\"'")
                )
                
            names = {
                Path(token).stem.casefold()
                for token in source_tokens
                if token.strip()
            }
            if names & expected_names:
                source_rows.append(row)
                
        if source_rows:
            return source_rows

    bank_column = _column(truth_headers, ("bank",))
    account_column = _column(truth_headers, ("account", "account_number"))
    
    candidates = truth_rows
    bank_name = str(metadata.get("bank_name") or "").casefold()
    account_number = re.sub(r"\W", "", str(metadata.get("account_number") or "")).casefold()
    
    if bank_column and bank_name:
        bank_key = "axis" if "axis" in bank_name else "hdfc" if "hdfc" in bank_name else bank_name
        candidates = [
            row for row in candidates
            if bank_key in str(row.get(bank_column, "")).casefold()
        ]
        
    if account_column and account_number:
        candidates = [
            row for row in candidates
            if re.sub(r"\W", "", row.get(account_column, "")).casefold() == account_number
        ]
        
    if (bank_name or account_number) and not candidates:
        raise ValueError(
            "No ground-truth rows match the prediction document's bank/account metadata."
        )
        
    return candidates


def _align_rows(
    predicted_rows: List[Dict[str, str]],
    truth_rows: List[Dict[str, str]],
    predicted_headers: List[str],
    truth_headers: List[str],
) -> List[Tuple[Optional[Dict[str, str]], Optional[Dict[str, str]]]]:
    predicted_ref = _column(predicted_headers, _ALIASES["txn_ids"])
    truth_ref = _column(truth_headers, _ALIASES["txn_ids"])
    truth_by_ref: Dict[str, List[int]] = {}

    if predicted_ref and truth_ref:
        for index, row in enumerate(truth_rows):
            reference = _canonical("txn_ids", row.get(truth_ref, ""))
            if reference:
                truth_by_ref.setdefault(reference, []).append(index)

    paired_truth: Set[int] = set()
    pair_by_prediction: Dict[int, int] = {}
    unpaired_prediction: List[int] = []
    
    for prediction_index, row in enumerate(predicted_rows):
        reference = (
            _canonical("txn_ids", row.get(predicted_ref, ""))
            if predicted_ref
            else ""
        )
        
        candidates = truth_by_ref.get(reference, []) if reference else []
        match = next(
            (
                index 
                for index in candidates 
                if index not in paired_truth
            ), None
        )
        
        if match is None:
            unpaired_prediction.append(prediction_index)
        else:
            pair_by_prediction[prediction_index] = match
            paired_truth.add(match)

    unpaired_truth = [
        index 
        for index in range(len(truth_rows)) 
        if index not in paired_truth
    ]
    
    for prediction_index, truth_index in zip(unpaired_prediction, unpaired_truth):
        pair_by_prediction[prediction_index] = truth_index
        paired_truth.add(truth_index)
        
    pairs = [
        (
            row,
            truth_rows[pair_by_prediction[index]]
            if index in pair_by_prediction
            else None,
        )
        for index, row in enumerate(predicted_rows)
    ]
    pairs.extend(
        (None, truth_rows[index])
        for index in range(len(truth_rows))
        if index not in paired_truth
    )
    
    return pairs


def evaluate_csv(
    predicted_csv: str | Path,
    ground_truth_csv: str | Path,
    *,
    train_fraction: float = 0.8,
) -> Dict[str, Any]:
    """Compare aligned CSV rows and report deterministic train/test metrics."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1.")
    
    with Path(predicted_csv).open(encoding="utf-8-sig", newline="") as file:
        predicted_reader = csv.DictReader(file)
        predicted_rows = list(predicted_reader)
        predicted_headers = predicted_reader.fieldnames or []
        
    with Path(ground_truth_csv).open(encoding="utf-8-sig", newline="") as file:
        truth_reader = csv.DictReader(file)
        truth_rows = list(truth_reader)
        truth_headers = truth_reader.fieldnames or []
        
    all_ground_truth_rows = len(truth_rows)
    truth_rows = _source_rows(truth_rows, truth_headers, Path(predicted_csv))

    field_columns: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    amount_pairs = []
    
    for role in ("debit", "credit"):
        predicted_column = _column(predicted_headers, _ALIASES[role])
        truth_column = _column(truth_headers, _ALIASES[role])
        if predicted_column and truth_column:
            amount_pairs.append((predicted_column, truth_column))
            
    if not amount_pairs:
        predicted_column = _column(predicted_headers, _ALIASES["amount"])
        truth_column = _column(truth_headers, _ALIASES["amount"])
        if predicted_column and truth_column:
            amount_pairs.append((predicted_column, truth_column))
            
    field_columns["amounts"] = tuple(amount_pairs)
    for field_name in ("dates", "txn_ids"):
        predicted_column = _column(predicted_headers, _ALIASES[field_name])
        truth_column = _column(truth_headers, _ALIASES[field_name])
        if predicted_column and truth_column:
            field_columns[field_name] = ((predicted_column, truth_column),)
        else:
            field_columns[field_name] = ()
            
    if not any(field_columns.values()):
        raise ValueError(
            "The prediction and ground-truth CSVs share no supported amount, date, or txn_id fields."
        )

    aligned_rows = _align_rows(
        predicted_rows, truth_rows, predicted_headers, truth_headers
    )
    metadata = _read_sidecar(Path(predicted_csv))
    sources = set(metadata.get("page_sources", []))
    target_accuracy = (
        0.99 if sources == {"pdfplumber"}
        else 0.95 if sources == {"paddleocr"}
        else None
    )
    train_end = int(len(aligned_rows) * train_fraction)
    partitions = {
        "train": range(0, train_end),
        "test": range(train_end, len(aligned_rows)),
        "all": range(0, len(aligned_rows)),
    }
    report: Dict[str, Any] = {
        "predicted_rows": len(predicted_rows),
        "ground_truth_rows": len(truth_rows),
        "all_ground_truth_rows": all_ground_truth_rows,
        "aligned_rows": len(aligned_rows),
        "alignment": "transaction-reference first, then row order",
        "target_accuracy": target_accuracy,
        "unavailable_fields": [
            field_name for field_name, columns in field_columns.items() if not columns
        ],
        "splits": {},
    }
    
    for split_name, row_indices in partitions.items():
        split_metrics = {}
        for field_name, columns in field_columns.items():
            if not columns:
                split_metrics[field_name] = {
                    "precision": None,
                    "recall": None,
                    "accuracy": None,
                    "target_met": False if target_accuracy is not None else None,
                    "true_positive": 0,
                    "false_positive": 0,
                    "false_negative": 0,
                }
                continue
            
            true_positive = false_positive = false_negative = correct_rows = 0
            for index in row_indices:
                predicted_row, expected_row = aligned_rows[index]
                comparisons = [
                    (
                        predicted_row.get(predicted_column, "").strip()
                        if predicted_row is not None
                        else "",
                        expected_row.get(truth_column, "").strip()
                        if expected_row is not None
                        else "",
                    )
                    for predicted_column, truth_column in columns
                ]
                
                predicted_present = any(predicted for predicted, _ in comparisons)
                expected_present = any(expected for _, expected in comparisons)
                
                correct = predicted_present and expected_present and all(
                    bool(predicted) == bool(expected)
                    and (
                        not predicted
                        or _canonical(
                            "amounts" if field_name == "amounts" else field_name,
                            predicted,
                        )
                        == _canonical(
                            "amounts" if field_name == "amounts" else field_name,
                            expected,
                        )
                    )
                    for predicted, expected in comparisons
                )
                
                exact = all(
                    bool(predicted) == bool(expected)
                    and (
                        not predicted
                        or _canonical(
                            "amounts" if field_name == "amounts" else field_name,
                            predicted,
                        )
                        == _canonical(
                            "amounts" if field_name == "amounts" else field_name,
                            expected,
                        )
                    )
                    for predicted, expected in comparisons
                )
                
                correct_rows += int(
                    predicted_row is not None and expected_row is not None and exact
                )
                true_positive += int(correct)
                false_positive += int(predicted_present and not correct)
                false_negative += int(expected_present and not correct)
                
            precision_denominator = true_positive + false_positive
            recall_denominator = true_positive + false_negative
            row_count = len(row_indices)
            accuracy = correct_rows / row_count if row_count else 0.0
            
            split_metrics[field_name] = {
                "precision": (
                    true_positive / precision_denominator if precision_denominator else 0.0
                ),
                "recall": true_positive / recall_denominator if recall_denominator else 0.0,
                "accuracy": accuracy,
                "target_met": (
                    accuracy >= target_accuracy if target_accuracy is not None else None
                ),
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
            }
            
        report["splits"][split_name] = split_metrics
        
    if target_accuracy is not None:
        report["target_met"] = all(
            metric["target_met"]
            for metric in report["splits"]["all"].values()
        )
    else:
        report["target_met"] = None
        
    return report
