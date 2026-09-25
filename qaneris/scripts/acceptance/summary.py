"""The machine-readable verdict handoff shared by the Excel acceptance scripts.

Each product-path acceptance already prints its checks for a human. The combined RS-EXCEL-02 runner
executes those scripts as subprocesses and needs their verdicts as *data* - so a script writes this
summary only when ``QANERIS_ACCEPTANCE_SUMMARY`` names a path, and a standalone run is unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENVIRONMENT_VARIABLE = "QANERIS_ACCEPTANCE_SUMMARY"


def write_summary(checks: dict[str, bool], **facts: Any) -> None:
    """Record one acceptance run's checks and identity facts, when a path was requested."""
    configured = os.getenv(ENVIRONMENT_VARIABLE, "").strip()
    if not configured:
        return
    path = Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    failed = sorted(name for name, passed in checks.items() if not passed)
    path.write_text(
        json.dumps(
            {
                "status": "passed" if not failed else "failed",
                "checks": checks,
                "failed_checks": failed,
                **facts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
