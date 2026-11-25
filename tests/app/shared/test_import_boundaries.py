from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_light_imports_do_not_load_database_matcher_or_ocr() -> None:
    project_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import service.reports.bank_recon_xlsx\n"
                "for prefix in ('matcher', 'paddleocr', 'database'):\n"
                "    assert not any(name == prefix or name.startswith(prefix + '.') "
                "for name in sys.modules), prefix\n"
                "import entry_point.ledger_handler.bank_loader\n"
                "assert not any(name == 'paddleocr' or name.startswith('paddleocr.') "
                "for name in sys.modules)\n"
            ),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
