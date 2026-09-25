from __future__ import annotations

from pathlib import Path
from typing import Protocol

from smartdata.contracts.profile import ScanSnapshot


class ProfileDocumentWriter(Protocol):
    def write(self, snapshot: ScanSnapshot, workspace_id: str) -> Path: ...
