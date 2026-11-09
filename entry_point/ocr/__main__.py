from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .evaluation import evaluate_csv
from .pipeline import run_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ocr")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Extract and optionally load PDF statements.")
    run.add_argument("--input", required=True, help="PDF file or directory of PDFs.")
    run.add_argument("--output", required=True, help="Output directory.")
    run.add_argument("--mode", choices=("none", "load", "reconcile"), default="none")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--force-ocr", action="store_true")
    run.add_argument("--gpu", action="store_true", help="Enable PaddleOCR GPU inference.")
    run.add_argument("--ground-truth", help="Optional master_ground_truth.csv for metrics.")
    evaluate = commands.add_parser("evaluate", help="Compare extracted CSV with ground truth.")
    evaluate.add_argument("--prediction", required=True)
    evaluate.add_argument("--ground-truth", required=True)
    evaluate.add_argument("--train-fraction", type=float, default=0.8)
    return parser


def main() -> None:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "evaluate":
        print(json.dumps(
            evaluate_csv(
                args.prediction, args.ground_truth, train_fraction=args.train_fraction
            ),
            indent=2,
        ))
        return

    result = run_pipeline(
        args.input,
        args.output,
        mode=args.mode,
        dry_run=args.dry_run,
        force_ocr=args.force_ocr,
        use_gpu=args.gpu,
    )
    print(json.dumps({
        "documents": [
            {
                "source": item.source.name,
                "csv": str(item.csv_path),
                "kind": item.layout.kind,
                "bank_name": item.layout.bank_name,
                "rows": item.row_count,
                "quarantined": item.quarantined,
                "warnings": item.warnings,
            }
            for item in result["documents"]
        ],
        "loaded_bank_records": len(result["bank_records"]),
        "loaded_ledger_records": len(result["ledger_records"]),
        "reconciliation_handoffs": (
            len(result["reconciliation"]["handoffs"])
            if result["reconciliation"] is not None
            else 0
        ),
    }, indent=2))
    if args.ground_truth:
        predictions = sorted((Path(args.output) / "csv").glob("*.csv"))
        for prediction in predictions:
            if prediction.name.endswith(".meta.json"):
                continue
            print(json.dumps({
                "prediction": prediction.name,
                "metrics": evaluate_csv(prediction, args.ground_truth),
            }, indent=2))


if __name__ == "__main__":
    main()
