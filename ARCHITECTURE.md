# Bank Statement and Ledger PDF Ingestion

## Package boundaries

- `entry_point/ocr/` owns PDF page extraction, OCR, document classification, table
  reconstruction, CSV/meta output, validation, evaluation, and CLI orchestration.
- `entry_point/ledger_handler/` owns CSV-to-schema loading and bank template detection.
- `schema/bank_rec_schema.py` remains the frozen record contract. OCR-only metadata
  such as account numbers, opening balances, holder, and IFSC is kept in a `.meta.json`
  sidecar or attached as a non-schema side-channel attribute.

## Extraction and loading

Each PDF page first uses pdfplumber text and word boxes. Pages with fewer than 50
text characters (or all pages when `--force-ocr` is set) are rasterized at 250 dpi,
deskewed, denoised, and sent to PaddleOCR. CPU is the default; `--gpu` opts into GPU
inference. The page source, OCR confidence, and bounding boxes are preserved during
reconstruction. Bounding boxes are normalized to a common page coordinate system so
mixed text/OCR pages can share table-column anchors. OCR imports are lazy so loading
CSVs does not require PaddleOCR.

The first page selects the HDFC, Axis, unified-general-ledger, or generic bank parser.
The parser registry is extensible. Reconstructed transactions are written to
`output/csv/<document>.csv`; metadata and per-row warnings are written beside it as
`<document>.meta.json`. Numeric OCR corrections are restricted to money/balance cells.

Bank CSVs use the native HDFC and Axis template headers so the existing bank detector
can identify them. Ledger CSVs use the schema field aliases, ISO transaction dates,
and include `ledger_type` on each row. The pipeline loads each generated CSV using
`load_bank_statement` or `load_ledger`, checks row counts, attaches sidecar warnings,
and prepares account-number-matched matcher handoffs.

Running balances use the extracted opening balance where available. Rows whose
debit/credit assignment alone explains a balance mismatch are repaired. If more than
5% of checked rows still fail, the source PDF is moved to `output/quarantine/` and is
not loaded. A ledger total row, when present, must equal the transaction totals.

## CLI

From `Backend/`:

```shell
python -m ocr run --input samples_v2/ --output output/ --mode none
python -m ocr run --input samples_v2/ --output output/ --mode load --dry-run
python -m ocr run --input samples_v2/ --output output/ --mode reconcile --force-ocr
python -m ocr evaluate --prediction output/csv/statement.csv \
  --ground-truth ../test/data-files/files/master_ground_truth.csv
```

`none`, `load`, and `reconcile` select extraction-only, loader, and matcher-handoff
modes. Dry-run writes extraction CSVs and sidecars but does not load records or move
quarantined originals. Reconciliation handoffs are returned by
`run_reconciliation(bank_records, ledger_records, ...)`; pass a matcher callback to
invoke an application-specific reconciliation function.

Evaluation first scopes ground-truth rows to the PDF named in the sidecar (or its
bank/account metadata), then aligns transaction references before falling back to
row order. It reports precision/recall for amount, date, and transaction-ID fields
on deterministic train/test row splits and the whole aligned set. Accuracy targets
of 99% (text-layer) and 95% (scanned) are reported only for documents whose pages
all used the same extraction path. They require representative PDFs and matching
ground-truth rows; synthetic integration tests do not certify those percentages.

## Dependencies and privacy

The pinned OCR/runtime dependencies are in `entry_point/requirements.txt` and
`req.txt` (used by the OCR worker image). PaddleOCR 2.7.3 and PaddlePaddle 2.6.2 are
configured for CPU by default. `--gpu` requires a compatible PaddlePaddle GPU build
for the host. Account numbers are not logged; they remain in the generated metadata
needed for downstream matching.
