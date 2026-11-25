from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_entry_task_is_registered_on_its_own_app() -> None:
    project_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["STORAGE_DIR"] = "/tmp/latice-entry-task-import"
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from entry_point.celery_app import celery_app\n"
                "celery_app.loader.import_task_module('entry_point.tasks')\n"
                "from entry_point.tasks import process_run\n"
                "assert process_run.name == 'entry.process_run'\n"
            ),
        ],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_requirements_file_is_utf8() -> None:
    requirements = Path(__file__).resolve().parents[2] / "req.txt"
    assert "celery==" in requirements.read_text(encoding="utf-8")
