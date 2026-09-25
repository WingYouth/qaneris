"""Shared lexical normalization without importing retrieval or grounding."""

from __future__ import annotations

import re


def normalize_term(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", value.casefold()))
