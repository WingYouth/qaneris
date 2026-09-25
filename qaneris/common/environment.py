from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


class EnvironmentBootstrapError(ValueError):
    """Raised when an explicitly requested environment file cannot be loaded."""


def load_runtime_environment(path: Path | None = None) -> Path | None:
    """Load the process environment once, without overriding exported values.

    File selection is explicit argument, then ``QANERIS_ENV_FILE``, then exactly
    ``cwd/.env``.  An operator-selected file must exist; an absent cwd default is normal.
    Existing process values always win because dotenv loading never overrides them.
    """

    configured = (os.getenv("QANERIS_ENV_FILE") or "").strip()
    selected = path if path is not None else (Path(configured) if configured else None)
    candidate = selected.expanduser().resolve() if selected is not None else Path.cwd() / ".env"
    if selected is not None and not candidate.is_file():
        raise EnvironmentBootstrapError(f"environment file does not exist: {candidate}")
    if not candidate.is_file():
        return None
    load_dotenv(dotenv_path=candidate, override=False)
    return candidate
