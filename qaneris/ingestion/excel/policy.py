"""Central limits for deterministic Excel ingestion.

Every bound that the validator enforces lives here: the defaults are locked by tests and nothing
in the ingestion package is allowed to carry a private magic number. The values come from
``docs/excel-ingestion.md`` §5 and §7.

This module also owns the *artifact identity* of a policy. The managed artifact directory is
content-addressed by workspace, policy and exact file bytes, so the policy has to collapse into a
deterministic, filesystem-safe path segment: see :func:`artifact_key`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields

POLICY_VERSION = "excel-ingestion-v1"

_MAX_VERSION_SLUG = 40


@dataclass(frozen=True)
class ExcelIngestionPolicy:
    """Resource and shape limits applied before any cell value is trusted."""

    max_file_size_bytes: int = 25 * 1024 * 1024
    max_visible_sheets: int = 32
    max_rows_per_sheet: int = 100_000
    max_columns_per_sheet: int = 256
    max_total_data_cells: int = 1_000_000
    max_text_length: int = 32_768
    max_zip_entries: int = 10_000
    max_uncompressed_size_bytes: int = 200 * 1024 * 1024
    max_column_name_length: int = 128
    version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "version":
                if not value:
                    raise ValueError("policy version cannot be empty")
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"policy limit {field.name} must be a positive integer")


DEFAULT_EXCEL_POLICY = ExcelIngestionPolicy()


def policy_digest(policy: ExcelIngestionPolicy) -> str:
    """Content-addressed identity of one policy: its version plus every limit.

    Deriving the digest from the whole policy, not just ``version``, means a changed limit can never
    silently reuse the artifact identity of the old limits: changing either the version or any bound
    produces a new identity, so a finalized ``source.xlsx`` / ``data.sqlite3`` is never rewritten by
    a different policy.
    """
    payload = json.dumps(
        asdict(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def artifact_key(policy: ExcelIngestionPolicy) -> str:
    """The single path segment that separates artifacts of different policies.

    It is readable (the sanitized version) and unique (the content digest), and it never contains a
    path separator or a ``.``/``..`` segment.
    """
    return f"{_version_slug(policy.version)}-{policy_digest(policy)}"


def _version_slug(version: str) -> str:
    """Reduce a human-supplied version to a safe, non-empty path segment."""
    cleaned = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "-"
        for character in version
    )
    words = [word for word in cleaned.split("-") if word]
    slug = "-".join(words).strip("._-")
    return (slug or "policy")[:_MAX_VERSION_SLUG]
