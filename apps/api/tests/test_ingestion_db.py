"""Integration tests: parsers + store + jobs against the real schema.

Skipped unless ``EDGE_TEST_DATABASE_URL`` points at a Postgres with
``infra/schema.sql`` applied (see tests/conftest.py). These tests verify the
F0 invariants end to end: idempotent schedule sync, doubleheader-safe event
matching, append-only odds snapshots with dedupe, and the closing-line flag.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.jobs import snapshot_odds, sync_schedule

from conftest import load_fixture

pytestmark = pytest.mark.integration

CAPTURE_TS = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)


class FakeMlbClient:
    def get_schedule(self, date_iso: str):
        return load_fixture("mlb_schedule.json")


class FakeOddsClient:
    def get_mlb_odds(self, **kwargs):
        return load_fixture("odds_api_mlb.json")


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def test_schedule_sync_is_idempotent(db):
    first = sync_schedule.run("2026-07-08", client=FakeMlbClient(), engine=db)
    assert first["games_in_feed"] == 3
    assert first["events_created"] == 3

    second = sync_schedule.run("2026-07-08", client=FakeMlbClient(), engine=db)
    assert second["events_created"] == 0
    assert second["events_refreshed"] == 3

    assert _scalar(db, "SELECT count(*) FROM events") == 3
    # Padres/Dodgers appear twice (doubleheader) but teams dedupe by name.
    assert _scalar(db, "SELECT count(*) FROM teams") == 4
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM events WHERE external_ids ? 'mlb_game_pk'",
        )
        == 3
    )


def test_snapshot_odds_matches_schedule_and_creates_unknown(db):
    sync_schedule.run("2026-07-08", client=FakeMlbClient(), engine=db)
    summary = snapshot_odds.run(
        client=FakeOddsClient(), engine=db, captured_at=CAPTURE_TS
    )

    # Yankees@RedSox and the doubleheader opener match the schedule; the
    # Cubs@Cardinals event is not in the slate fixture and gets created.
    assert summary["events_matched"] == 2
    assert summary["events_created"] == 1
    assert summary["events_started_skipped"] == 0
    # 6 valid outcomes for event A + 2 (Dodgers game) + 2 (Cardinals game).
    assert summary["snapshots_inserted"] == 10
    assert _scalar(db, "SELECT count(*) FROM events") == 4

    # The matched event now carries BOTH external identities.
    merged = _scalar(
        db,
        """
        SELECT count(*) FROM events
        WHERE external_ids ->> 'mlb_game_pk' = '745001'
          AND external_ids ->> 'the_odds_api_id' = 'e912aa27b1c4f03d8e5a6b7c8d9e0f1a'
        """,
    )
    assert merged == 1

    # Doubleheader safety: the 20:05Z odds event must attach to the 20:10Z
    # opener (5 min away), never to the 02:10Z nightcap.
    opener_snapshots = _scalar(
        db,
        """
        SELECT count(*) FROM odds_snapshots s
        JOIN events e ON e.id = s.event_id
        WHERE e.external_ids ->> 'mlb_game_pk' = :pk
        """,
        pk="745002",
    )
    nightcap_snapshots = _scalar(
        db,
        """
        SELECT count(*) FROM odds_snapshots s
        JOIN events e ON e.id = s.event_id
        WHERE e.external_ids ->> 'mlb_game_pk' = :pk
        """,
        pk="745003",
    )
    assert opener_snapshots == 2
    assert nightcap_snapshots == 0

    # implied_prob is a generated column: 1 / 2.05 rounded to 6 decimals.
    implied = _scalar(
        db,
        "SELECT implied_prob FROM odds_snapshots WHERE price_decimal = 2.050 LIMIT 1",
    )
    assert implied == Decimal("0.487805")

    # Re-running with the same capture instant inserts nothing (dedupe key).
    rerun = snapshot_odds.run(client=FakeOddsClient(), engine=db, captured_at=CAPTURE_TS)
    assert rerun["snapshots_inserted"] == 0
    assert _scalar(db, "SELECT count(*) FROM odds_snapshots") == 10


def test_closing_flag_only_within_window_and_never_duplicated(db):
    late_capture = datetime(2026, 7, 8, 19, 50, tzinfo=timezone.utc)
    summary = snapshot_odds.run(
        closing_window_min=20,
        client=FakeOddsClient(),
        engine=db,
        captured_at=late_capture,
    )
    assert summary["snapshots_inserted"] == 10

    # Only the 20:05Z Dodgers game falls inside the 20-minute closing window.
    assert _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE is_closing") == 2
    closing_ok = _scalar(
        db,
        """
        SELECT count(*) FROM odds_snapshots s
        JOIN events e ON e.id = s.event_id
        WHERE s.is_closing AND e.external_ids ->> 'the_odds_api_id' = :oid
        """,
        oid="f7c3d2e1a0b9c8d7e6f5a4b3c2d1e0f9",
    )
    assert closing_ok == 2

    # A second closing run minutes later: the partial unique index rejects a
    # second closing row per outcome; non-closing events snapshot normally.
    second = snapshot_odds.run(
        closing_window_min=20,
        client=FakeOddsClient(),
        engine=db,
        captured_at=datetime(2026, 7, 8, 19, 55, tzinfo=timezone.utc),
    )
    assert second["snapshots_inserted"] == 8  # 10 minus the 2 closing dupes
    assert _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE is_closing") == 2


def test_pregame_only_skips_started_events(db):
    after_first_pitch = datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc)
    summary = snapshot_odds.run(
        client=FakeOddsClient(), engine=db, captured_at=after_first_pitch
    )
    # 23:10Z and 20:05Z already started; only the 00:15Z game snapshots.
    assert summary["events_started_skipped"] == 2
    assert summary["snapshots_inserted"] == 2


def test_odds_snapshots_are_append_only(db):
    snapshot_odds.run(client=FakeOddsClient(), engine=db, captured_at=CAPTURE_TS)
    with pytest.raises(Exception, match="append-only"):
        with db.begin() as conn:
            conn.execute(text("UPDATE odds_snapshots SET price_decimal = 3.0"))
    with pytest.raises(Exception, match="append-only"):
        with db.begin() as conn:
            conn.execute(text("DELETE FROM odds_snapshots"))
