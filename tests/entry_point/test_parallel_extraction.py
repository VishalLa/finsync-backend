from __future__ import annotations

from typing import Any, List

from app.shared.message import EntryProcessMsg


def test_process_run_dispatches_bank_and_ledger_as_parallel_tasks(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    from entry_point import tasks

    dispatched = {}

    class Workflow:
        def apply_async(self) -> None:
            dispatched["applied"] = True

    def fake_group(*signatures):
        dispatched["signatures"] = signatures
        return "parallel-group"

    def fake_chord(header, callback):
        dispatched["header"] = header
        dispatched["callback"] = callback
        return Workflow()

    monkeypatch.setattr(tasks, "group", fake_group)
    monkeypatch.setattr(tasks, "chord", fake_chord)
    monkeypatch.setattr(tasks, "set_status", lambda **_kwargs: None)
    payload = EntryProcessMsg(
        run_id="parallel-run",
        user_id="user-1",
        ledger_ref="runs/parallel-run/raw/ledger.xlsx",
        bank_ref="runs/parallel-run/raw/bank.xlsx",
        options={"date_format": "%Y-%m-%d"},
    ).to_dict()

    result = tasks.process_run.run(payload)

    assert result == {"status": "queued", "run_id": "parallel-run"}
    assert dispatched["header"] == "parallel-group"
    assert dispatched["applied"] is True
    signatures = dispatched["signatures"]
    assert [signature.task for signature in signatures] == [
        "entry.extract_ledger",
        "entry.extract_bank",
    ]
    assert signatures[0].args == (
        "parallel-run",
        "user-1",
        "runs/parallel-run/raw/ledger.xlsx",
        "%Y-%m-%d",
    )
    assert signatures[1].args == (
        "parallel-run",
        "user-1",
        "runs/parallel-run/raw/bank.xlsx",
        None,
    )
    assert dispatched["callback"].task == "entry.persist_extracted_inputs"


def test_parallel_extraction_results_are_joined_before_input_persist(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    from entry_point import tasks

    written = {}
    statuses = []
    sent = []

    monkeypatch.setattr(tasks, "run_ref", lambda run_id, *parts: "/".join([run_id, *parts]))
    monkeypatch.setattr(tasks, "write_json", lambda ref, obj: written.update({ref: obj}))
    monkeypatch.setattr(tasks, "set_status", lambda **kwargs: statuses.append(kwargs))
    monkeypatch.setattr(
        tasks.celery_app,
        "send_task",
        lambda task_name, args: sent.append((task_name, args)),
    )
    payload = EntryProcessMsg(
        run_id="parallel-run",
        user_id="user-1",
        ledger_ref="runs/parallel-run/raw/ledger.xlsx",
        bank_ref="runs/parallel-run/raw/bank.xlsx",
        options={"run_name": "January reconciliation"},
    ).to_dict()
    extraction_results: List[dict[str, Any]] = [
        {
            "kind": "bank",
            "ref": "runs/parallel-run/norm/bank.json",
            "bank_name": "HDFC Bank",
            "template_version": "v1",
            "warnings": ["Bank warning"],
        },
        {
            "kind": "ledger",
            "ref": "runs/parallel-run/norm/ledger.json",
            "source": "ledger.xlsx",
            "warnings": ["Ledger warning"],
        },
    ]

    result = tasks.persist_extracted_inputs.run(extraction_results, payload)

    assert result == {"status": "queued", "run_id": "parallel-run"}
    metadata = written["parallel-run/norm/metadata.json"]
    assert metadata["warnings"] == ["Ledger warning", "Bank warning"]
    assert metadata["bank_name"] == "HDFC Bank"
    assert len(sent) == 1
    assert sent[0][0] == "db.store_inputs"
    db_payload = sent[0][1][0]
    assert db_payload["ledger_ref"] == "runs/parallel-run/norm/ledger.json"
    assert db_payload["bank_ref"] == "runs/parallel-run/norm/bank.json"
    assert [status["state"] for status in statuses] == ["done", "queued"]
