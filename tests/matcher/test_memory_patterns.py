from types import SimpleNamespace

from matcher.memory_match import (
    InMemoryBackend,
    MatchMemory,
    _normalize_signature,
    memory_matcher,
)


def test_memory_patterns_normalize_migrate_and_match_across_runs() -> None:
    normalize = _normalize_signature
    first = normalize("UPI/402312345678/ZOMATO/zomato@hdfcbank/Order food")
    second = normalize("UPI/998877665544/ZOMATO/zomato@hdfcbank/Order food")
    assert first == second and first
    assert normalize("Sharma Traders Pvt Ltd") == normalize("TRADERS SHARMA")
    assert normalize("NEFT/N123456789012/ACME CORP/HDFC0001234") == "acme"
    assert normalize("UPI/123456789012") == ""
    assert normalize(None) == normalize("") == ""

    memory = MatchMemory()
    memory.record_match(
        "Zomato Ltd",
        "UPI/402312345678/ZOMATO/zomato@hdfcbank/Order food",
        "fuzzy",
    )
    assert memory.is_recognized(
        "Zomato Ltd",
        "UPI/111122223333/ZOMATO/zomato@hdfcbank/Order food",
    )
    memory.record_match(
        "Zomato Ltd", "UPI/555566667777/ZOMATO/zomato@hdfcbank/Lunch", "ai"
    )
    pattern = memory.lookup(
        "Zomato Ltd", "UPI/000000000001/ZOMATO/zomato@hdfcbank/Dinner party"
    )
    assert pattern.bank_signature == "zomato"
    assert pattern.times_seen == 2
    assert len(memory._patterns) == 1
    assert not memory.is_recognized(
        "Zomato Ltd", "UPI/1/SWIGGY/swiggy@icici/Order food"
    )
    assert not memory.is_recognized("Other Vendor", "UPI/1/ZOMATO/zomato@hdfcbank/x")

    backend = InMemoryBackend()
    backend.save(
        {
            "old1": {
                "ledger_signature": "zomato ltd",
                "bank_signature": "upi 402312345678 zomato zomato hdfcbank order food",
                "match_phase": "fuzzy",
                "adjustment_type": None,
                "times_seen": 2,
            },
            "old2": {
                "ledger_signature": "zomato ltd",
                "bank_signature": "upi 999999999999 zomato zomato hdfcbank order food",
                "match_phase": "fuzzy",
                "adjustment_type": None,
                "times_seen": 3,
            },
        }
    )
    migrated = MatchMemory(backend=backend)
    assert len(migrated._patterns) == 1
    assert next(iter(migrated._patterns.values())).times_seen == 5

    memory.save()
    reloaded = MatchMemory(backend=memory.backend)
    assert reloaded.is_recognized("Zomato Ltd", "UPI/42/ZOMATO/z@x/y")

    ledger = [
        SimpleNamespace(
            ledger_id="L1", account_name="Zomato Ltd", debit_amount=500.0,
            credit_amount=0.0,
        )
    ]
    bank = [
        SimpleNamespace(
            row_index=7,
            narration="UPI/777788889999/ZOMATO/zomato@hdfcbank/Snacks",
            debit_amount=500.0,
            credit_amount=0.0,
        )
    ]
    matches, ledger_left, bank_left = memory_matcher(
        ledger, bank, memory, same_side=True, amount_tol=2.0
    )
    assert len(matches) == 1
    assert matches[0]["ledger_id"] == "L1"
    assert matches[0]["bank_id"] == 7
    assert not ledger_left and not bank_left
