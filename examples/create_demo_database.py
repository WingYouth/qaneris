from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_SCHEMA = Path(__file__).with_name("sales_2026_07.sql")


def create_demo_database(
    output_path: str | Path,
    *,
    schema_path: str | Path = DEFAULT_SCHEMA,
    overwrite: bool = False,
) -> Path:
    """Create the deterministic SQLite sales database and return its resolved path."""
    output = Path(output_path).expanduser().resolve()
    schema = Path(schema_path).expanduser().resolve()
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output database already exists: {output}")
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    sql = schema.read_text(encoding="utf-8")
    with sqlite3.connect(output) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(sql)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the SmartData SQLite demo database")
    parser.add_argument(
        "output",
        nargs="?",
        default="examples/sales_2026_07.db",
        help="Output SQLite path (default: examples/sales_2026_07.db)",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    args = parser.parse_args()
    output = create_demo_database(args.output, overwrite=args.force)
    print(output)


if __name__ == "__main__":
    main()
