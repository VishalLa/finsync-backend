"""Deterministic labelled data generator for m×n matcher evaluation."""
from __future__ import annotations

import random
from datetime import date, timedelta


SHAPES = [(2, 2), (2, 3), (3, 3), (4, 2), (5, 5), (7, 7), (10, 10)]


def generate(groups: int = 100, seed: int = 20261114):
    rng = random.Random(seed)
    ledger, bank, labels = [], [], []
    base = date(2026, 1, 5)
    for group_no in range(groups):
        nl, nb = SHAPES[group_no % len(SHAPES)]
        parts = [rng.randint(20_00, 180_00) for _ in range(max(nl, nb))]
        # Construct positive partitions with equal totals and nontrivial row amounts.
        total = sum(parts[:nl])
        lparts = parts[:nl]
        for _attempt in range(500):
            cuts = sorted([0, total] + [rng.randrange(1, total) for _ in range(nb - 1)])
            bparts = [cuts[i + 1] - cuts[i] for i in range(nb)]
            l_subs = {sum(c) for size in range(2, nl + 1)
                      for c in __import__("itertools").combinations(lparts, size)}
            b_subs = {sum(c) for size in range(2, nb + 1)
                      for c in __import__("itertools").combinations(bparts, size)}
            # Exclude accidental internal sum joins; intentional duplicate groups
            # below remain the only deliberately ambiguous labelled cases.
            if not any(abs(ls - bs) <= 100 for ls in l_subs for bs in b_subs
                       if not (ls == total and bs == total)):
                break
        ref = f"SYNTH-{group_no:04d}" if group_no % 2 == 0 else None
        offset = group_no * 30
        ledger_ids, bank_ids = [], []
        for i, amount in enumerate(lparts):
            ident = f"L{group_no:04d}-{i:02d}"; ledger_ids.append(ident)
            dt = base + timedelta(days=offset + rng.randrange(0, 3))
            ledger.append({"ledger_id": ident, "date": dt.isoformat(), "amount": amount / 100,
                           "description": f"Acme vendor payment batch {group_no}", "reference": ref})
        for j, amount in enumerate(bparts):
            ident = group_no * 20 + j + 1; bank_ids.append(ident)
            dt = base + timedelta(days=offset + rng.randrange(2, 9))
            bank.append({"row_index": ident, "date": dt.isoformat(), "amount": amount / 100,
                         "description": f"Acme vendor payment batch {group_no}", "reference": ref})
        labels.append({"ledger_ids": ledger_ids, "bank_ids": bank_ids, "kind": "positive"})
        # Deliberate same-counterparty/same-total duplicate group; correct routing is review.
        if group_no % 10 == 0 and max(nl, nb) <= 3:
            dup_l, dup_b = [], []
            for i, source in enumerate(ledger[-nl:]):
                ident = f"D{group_no:04d}-{i:02d}"; dup_l.append(ident)
                ledger.append({**source, "ledger_id": ident})
            for j, source in enumerate(bank[-nb:]):
                ident = 200_000 + group_no * 20 + j; dup_b.append(ident)
                bank.append({**source, "row_index": ident})
            labels.append({"ledger_ids": dup_l, "bank_ids": dup_b, "kind": "duplicate_review"})
    # Negative decoys: random subsets on each side share a total, but their unrelated
    # narratives and references provide no blocking evidence to the matcher.
    for idx in range(max(10, groups // 5)):
        total = rng.randint(10_00, 90_00)
        split = rng.randint(1, total - 1)
        day = (base + timedelta(days=idx)).isoformat()
        l_ids, b_ids = [], []
        for side, amounts, target in (("L", [split, total-split], ledger), ("B", [rng.randint(1,total-1), 0], bank)):
            if side == "B": amounts[1] = total - amounts[0]
            for n, amount in enumerate(amounts):
                ident = f"NEG-{side}{idx}-{n}" if side == "L" else 300_000 + idx * 2 + n
                target.append({"ledger_id" if side == "L" else "row_index": ident,
                               "date": day, "amount": amount / 100,
                               "description": (f"Kestrel procurement charge {idx}-{n}" if side == "L"
                                               else f"Violet orchard settlement {idx}-{n}"), "reference": None})
                (l_ids if side == "L" else b_ids).append(ident)
        labels.append({"ledger_ids": l_ids, "bank_ids": b_ids, "kind": "negative_equal_sum"})
    return ledger, bank, labels


if __name__ == "__main__":
    import json
    from pathlib import Path
    l, b, labels = generate()
    out = Path(__file__).with_name("generated_m2m")
    out.mkdir(exist_ok=True)
    (out / "ledger.json").write_text(json.dumps(l, indent=2))
    (out / "bank.json").write_text(json.dumps(b, indent=2))
    (out / "labels.json").write_text(json.dumps(labels, indent=2))
