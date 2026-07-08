"""Apply one SQL migration file to the configured database.

Usage::

    python -m app.jobs.apply_migration --file infra/migrations/001-....sql

Runs the file's statements in a single transaction (the files also carry
their own BEGIN/COMMIT for psql compatibility; those are skipped here).
Migrations must be idempotent — see infra/migrations/ conventions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.engine import make_engine


def run(file_path: str, *, engine=None) -> dict:
    engine = engine or make_engine(get_settings().database_url)
    sql = Path(file_path).read_text(encoding="utf-8")
    statements = []
    for chunk in sql.split(";"):
        lines = [
            line for line in chunk.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        statement = "\n".join(lines).strip()
        if statement and statement.upper() not in ("BEGIN", "COMMIT"):
            statements.append(statement)
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return {"job": "apply_migration", "file": file_path, "statements": len(statements)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Path to the .sql migration file")
    args = parser.parse_args()
    print(json.dumps(run(args.file)))
