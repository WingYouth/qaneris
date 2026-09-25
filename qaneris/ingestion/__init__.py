"""Ingestion sources feeding the existing trusted Qaneris pipeline.

An ingestion source only turns an external file into a deterministic, immutable relational
database. From that database on, the normal Scan → Graph → Grounding → Query chain applies; no
ingestion source owns a second query engine.
"""
