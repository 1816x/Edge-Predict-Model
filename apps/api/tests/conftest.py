"""Shared fixtures.

Integration tests need a real Postgres with ``infra/schema.sql`` applied and
its URL in ``EDGE_TEST_DATABASE_URL`` (SQLAlchemy format, e.g.
``postgresql+psycopg://postgres@/edgetest?host=/tmp/pg0/sock``). Without it
those tests are skipped; the pure unit tests always run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="session")
def db_engine():
    url = os.environ.get("EDGE_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "EDGE_TEST_DATABASE_URL not set (Postgres with infra/schema.sql required)"
        )
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    """Engine with ingestion tables truncated (seeds in sports/books survive).

    TRUNCATE bypasses the append-only row triggers (those fire only on UPDATE
    and DELETE), which is exactly what a test reset needs.
    """
    from sqlalchemy import text

    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE teams CASCADE"))
    return db_engine
