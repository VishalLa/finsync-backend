"""Run labelled synthetic evaluation and print separate m×n quality metrics."""
from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).parent))

from matcher.fuzzy_matcher.fuzzy_match import FuzzyMatcher
from matcher.fuzzy_matcher.relationship import Relationship
from schema import BankStatement, LedgerFormat
from m2m_synthetic import generate


def main() -> None:
    ledger_rows, bank_rows, labels = generate()
    ledger = [LedgerFormat(
        ledger_id=row["ledger_id"], account_name=row["description"],
        transaction_date=row["date"], debit_amount=row["amount"],
        reference_id=row["reference"],
    ) for row in ledger_rows]
    bank = [BankStatement(
        row_index=row["row_index"], bank_name="synthetic", template_version="1",
        date=row["date"], narration=row["description"], debit_amount=row["amount"],
        txn_id=row["reference"],
    ) for row in bank_rows]
    matcher = FuzzyMatcher(ledger, bank, tolerances={"DEFAULT": 1.0})
    started = time.perf_counter()
    result = Relationship(matcher).match_many_to_many()
    runtime = time.perf_counter() - started

    accepted = []
    for match in matcher.fuzzy_matches:
        if not str(match.get("adjustment_type", "")).startswith("Many-to-Many"):
            continue
        accepted.append((set(str(match["ledger_id"]).split(" & ")),
                         set(int(x) for x in str(match["bank_id"]).split(" & "))))

    positives = [x for x in labels if x["kind"] == "positive"]
    correct = [p for p in positives if any(l == set(p["ledger_ids"]) and b == set(p["bank_ids"]) for l, b in accepted)]
    true_accepted = sum(any(l == set(x["ledger_ids"]) and b == set(x["bank_ids"]) for x in positives) for l, b in accepted)
    negative_rows = [x for x in labels if x["kind"] == "negative_equal_sum"]
    accidental = sum(any(l & set(n["ledger_ids"]) and b & set(n["bank_ids"]) for n in negative_rows) for l, b in accepted)

    ref_small = [p for p in positives if len(p["ledger_ids"]) <= 5 and p["ledger_ids"][0][1:5].isdigit()
                 and int(p["ledger_ids"][0][1:5]) % 2 == 0
                 and int(p["ledger_ids"][0][1:5]) % 10 != 0]
    duplicate_groups = [x for x in labels if x["kind"] == "duplicate_review"]
    proposals = result["PROPOSED_MATCHES"]
    def candidate_sets(proposal):
        yield ({str(c["id"]) for c in proposal["ledger_components"]},
               {c["id"] for c in proposal["bank_components"]})
        for alternative in proposal["alternatives"]:
            yield (set(alternative["ledger_ids"]), set(alternative["bank_ids"]))
    routed = sum(any(set(p["ledger_ids"]) == l and set(p["bank_ids"]) == b
                     for q in proposals for l, b in candidate_sets(q)) for p in duplicate_groups)

    total_ref_rows = sum(len(x["ledger_ids"]) + len(x["bank_ids"]) for x in ref_small)
    recovered_ref_rows = sum(len(x["ledger_ids"]) + len(x["bank_ids"]) for x in correct if x in ref_small)
    stats = result["M2M_STATS"]
    print({
        "groups": len(labels),
        "accepted_groups": len(accepted),
        "accepted_group_precision": true_accepted / len(accepted) if accepted else None,
        "positive_group_recall": len(correct) / len(positives) if positives else None,
        "positive_groups": len(positives),
        "referenced_group_recall_shapes_up_to_5x5_excluding_duplicate_groups": sum(p in correct for p in ref_small) / len(ref_small) if ref_small else None,
        "referenced_groups_shapes_up_to_5x5_excluding_duplicate_groups": len(ref_small),
        "row_recall_referenced_shapes_up_to_5x5": recovered_ref_rows / total_ref_rows if total_ref_rows else None,
        "accidental_match_rate_on_negative_groups": accidental / len(negative_rows) if negative_rows else None,
        "negative_groups": len(negative_rows),
        "duplicate_review_routing_rate": routed / len(duplicate_groups) if duplicate_groups else None,
        "duplicate_groups": len(duplicate_groups),
        "budget_exhaustion_rate": stats["m2m_budget_exhaustions"] / stats["m2m_components_searched"] if stats["m2m_components_searched"] else 0,
        "runtime_seconds": round(runtime, 3),
        "stats": stats,
    })


if __name__ == "__main__":
    main()
