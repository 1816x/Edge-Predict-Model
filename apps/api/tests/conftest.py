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


@pytest.fixture
def seeded(db):
    """Target game H vs A on 2026-07-08T23:00Z plus curated history.

    Shared by the feature-builder tests and the bulk-vs-online parity test.
    """
    from datetime import datetime, timezone

    from app.ingestion import store
    from app.ingestion.parsers import GameResult, ScheduledGame

    H, A, X = "Boston Red Sox", "New York Yankees", "Tampa Bay Rays"

    def _game(pk, start, home, away, status="final"):
        return ScheduledGame(
            game_pk=pk, start_time=start, status=status,
            home_name=home, away_name=away, home_mlb_id=None, away_mlb_id=None,
            home_probable=None, away_probable=None,
        )

    def _seed(conn, tables, sport_id, pk, start, home, away, result=None, status="final"):
        event_id, _ = store.upsert_event_from_schedule(
            conn, tables, sport_id, _game(pk, start, home, away, status)
        )
        if result is not None:
            store.upsert_event_result(conn, tables, event_id, result)
        return event_id

    tables = store.reflect_tables(
        db, ("sports", "teams", "events", "event_results", "feature_snapshots")
    )
    ts = lambda m, d, h: datetime(2026, m, d, h, 0, tzinfo=timezone.utc)  # noqa: E731
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        target_id = _seed(
            conn, tables, sport_id, 900010, ts(7, 8, 23), H, A, status="scheduled"
        )
        # H history: a win and a loss inside the 30d window...
        _seed(conn, tables, sport_id, 900001, ts(7, 5, 23), H, X,
              GameResult(900001, 5, 3, 3, 1))
        _seed(conn, tables, sport_id, 900002, ts(7, 6, 23), X, H,
              GameResult(900002, 7, 2, 4, 0))
        # ...one game outside the 30d window (June 5th)...
        _seed(conn, tables, sport_id, 900003, ts(6, 5, 23), H, X,
              GameResult(900003, 1, 0, 0, 0))
        # ...and one AFTER as_of (starts 07-09T00:00Z): must never leak in.
        _seed(conn, tables, sport_id, 900004, ts(7, 9, 0), H, X,
              GameResult(900004, 10, 0, 8, 0))
        # A history: single loss on July 4th.
        _seed(conn, tables, sport_id, 900005, ts(7, 4, 23), A, X,
              GameResult(900005, 2, 6, 1, 2))
    return db, tables, target_id
