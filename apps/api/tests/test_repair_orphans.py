"""repair_orphan_events: reconnect fragmented odds history, run 2× safe.

Seeds the exact pre-fix production shape (an mlb event frozen mid-slate plus
an orphan holding the later captures) and asserts the repair repoints, the
append-only guard comes back armed, closing-flag collisions demote instead
of crashing, no-sibling and ambiguous orphans are skipped intact, and a
second run is a byte-level no-op.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.ingestion import store
from app.ingestion.parsers import OddsEvent, OddsOutcome, ScheduledGame
from app.jobs import repair_orphan_events

pytestmark = pytest.mark.integration

GUARDIANS, PIRATES = "Cleveland Guardians", "Pittsburgh Pirates"


def _utc(day, hour, minute=0):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def _outcome(side="home", closing_ok_price=1.91):
    return OddsOutcome(
        book_key="pinnacle", market="moneyline", side=side,
        price_decimal=closing_ok_price, price_american=-110, last_update=None,
    )


def _scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _seed_mlb_event(conn, tables, sport_id, pk, home, away, start):
    event_id, _ = store.upsert_event_from_schedule(
        conn, tables, sport_id,
        ScheduledGame(
            game_pk=pk, start_time=start, status="scheduled",
            home_name=home, away_name=away, home_mlb_id=None, away_mlb_id=None,
            home_probable=None, away_probable=None,
        ),
    )
    return event_id


def _make_orphan(conn, tables, sport_id, source_id, home, away, commence):
    """Reproduce the PRE-FIX resolver path: live_odds_ids=None skips tier
    2.5, so a reissued id for an already-priced matchup creates the orphan."""
    ev = OddsEvent(
        source_id=source_id, home_team=home, away_team=away,
        commence_time=commence, outcomes=(), skipped=(),
    )
    event_id, action = store.find_or_create_event_for_odds(
        conn, tables, sport_id, ev
    )
    assert action == "created"
    return event_id


def _seed_production_shape(db, *, orphan_closing_collides=False):
    """mlb event A (pk 824414) with 2 early snapshots + orphan B holding 4
    later ones (one flagged closing) + no-sibling orphan C with 2."""
    tables = store.reflect_tables(db)
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        a_id = _seed_mlb_event(
            conn, tables, sport_id, 824414, GUARDIANS, PIRATES, _utc(18, 17, 10)
        )
        # A got merged with the original odds id and captured early.
        conn.execute(
            text(
                "UPDATE events SET external_ids = external_ids || "
                "'{\"the_odds_api_id\": \"oid_old\"}' WHERE id = :id"
            ),
            {"id": a_id},
        )
        store.insert_odds_snapshots(
            conn, tables, a_id, (_outcome("home"), _outcome("away")), _utc(17, 19, 35)
        )
        if orphan_closing_collides:
            store.insert_odds_snapshots(
                conn, tables, a_id, (_outcome("home"),), _utc(17, 20, 0),
                is_closing=True,
            )
        # B: the reissued id spawned an orphan that took the late captures.
        b_id = _make_orphan(
            conn, tables, sport_id, "oid_new", GUARDIANS, PIRATES, _utc(18, 17, 11)
        )
        store.insert_odds_snapshots(
            conn, tables, b_id, (_outcome("home"), _outcome("away")), _utc(18, 12, 0)
        )
        store.insert_odds_snapshots(
            conn, tables, b_id, (_outcome("home"),), _utc(18, 16, 40),
            is_closing=True,
        )
        store.insert_odds_snapshots(
            conn, tables, b_id, (_outcome("away"),), _utc(18, 16, 40)
        )
        # C: odds-only event with no mlb sibling (the All-Star shape).
        c_id = _make_orphan(
            conn, tables, sport_id, "oid_allstar", "National League",
            "American League", _utc(15, 0, 1),
        )
        store.insert_odds_snapshots(
            conn, tables, c_id, (_outcome("home"), _outcome("away")), _utc(14, 22, 0)
        )
    return a_id, b_id, c_id


def test_dry_run_reports_without_writing(db):
    a_id, b_id, c_id = _seed_production_shape(db)
    summary = repair_orphan_events.run(dry_run=True, engine=db)

    assert summary["orphans_found"] == 2
    assert summary["repointable"] == 1
    assert summary["snapshots_repointed"] == 4
    assert summary["exact_dupes_deleted"] == 0
    assert summary["closing_flags_cleared"] == 0
    assert summary["events_deleted"] == 1
    assert len(summary["skipped_no_sibling"]) == 1

    # Nothing moved: B still owns its 4 rows, A its 2, C its 2.
    for event_id, expected in ((a_id, 2), (b_id, 4), (c_id, 2)):
        assert (
            _scalar(
                db,
                "SELECT count(*) FROM odds_snapshots WHERE event_id = :id",
                id=event_id,
            )
            == expected
        )


def test_repair_repoints_transfers_id_and_rearms_guard_run_twice(db):
    a_id, b_id, c_id = _seed_production_shape(db)
    summary = repair_orphan_events.run(engine=db)

    assert summary["repointable"] == 1
    assert summary["snapshots_repointed"] == 4
    assert summary["events_deleted"] == 1
    assert summary["closing_flags_cleared"] == 0
    assert [s["orphan_id"] for s in summary["skipped_no_sibling"]] == [str(c_id)]

    # The full history (2 + 4) now lives under the mlb_game_pk event, which
    # also records the reissued (current) odds id; the orphan row is gone.
    assert (
        _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE event_id = :id", id=a_id)
        == 6
    )
    assert (
        _scalar(
            db,
            """
            SELECT count(*) FROM events
            WHERE external_ids ->> 'mlb_game_pk' = '824414'
              AND external_ids ->> 'the_odds_api_id' = 'oid_new'
            """,
        )
        == 1
    )
    assert _scalar(db, "SELECT count(*) FROM events WHERE id = :id", id=b_id) == 0
    # The orphan's closing flag survived (no collision in this shape).
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM odds_snapshots WHERE event_id = :id AND is_closing",
            id=a_id,
        )
        == 1
    )
    # No-sibling orphan untouched.
    assert (
        _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE event_id = :id", id=c_id)
        == 2
    )

    # The append-only guard is re-armed: mutating a snapshot must fail.
    with pytest.raises(DBAPIError, match="append-only"):
        with db.begin() as conn:
            conn.execute(text("UPDATE odds_snapshots SET is_closing = false"))

    # Second run: nothing left to repair, byte-identical archive.
    again = repair_orphan_events.run(engine=db)
    assert again["orphans_found"] == 1  # only the no-sibling orphan remains
    assert again["repointable"] == 0
    assert again["snapshots_repointed"] == 0
    assert again["events_deleted"] == 0
    assert (
        _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE event_id = :id", id=a_id)
        == 6
    )


def test_closing_collision_demotes_orphan_flag_keeps_siblings(db):
    a_id, _, _ = _seed_production_shape(db, orphan_closing_collides=True)
    summary = repair_orphan_events.run(engine=db)

    # Both A and the orphan held is_closing for (pinnacle, moneyline, home):
    # the orphan's is demoted, the sibling's true closing line survives, and
    # the orphan's price row still lands on A as non-closing evidence.
    assert summary["closing_flags_cleared"] == 1
    assert summary["snapshots_repointed"] == 4
    assert (
        _scalar(
            db,
            """
            SELECT count(*) FROM odds_snapshots
            WHERE event_id = :id AND is_closing
              AND market = 'moneyline' AND side = 'home'
            """,
            id=a_id,
        )
        == 1
    )
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM odds_snapshots WHERE event_id = :id AND is_closing",
            id=a_id,
        )
        == 1
    )
    assert (
        _scalar(db, "SELECT count(*) FROM odds_snapshots WHERE event_id = :id", id=a_id)
        == 7
    )


def test_ambiguous_sibling_is_skipped_intact(db):
    """Identical-start doubleheader: two mlb siblings -> never guess."""
    tables = store.reflect_tables(db)
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        for pk in (930001, 930002):
            event_id = _seed_mlb_event(
                conn, tables, sport_id, pk, GUARDIANS, PIRATES, _utc(18, 18, 0)
            )
            # Both legs already priced under (stale) ids — the state in which
            # the pre-fix resolver spawned the orphan in the first place.
            conn.execute(
                text(
                    "UPDATE events SET external_ids = external_ids || "
                    "jsonb_build_object('the_odds_api_id', "
                    "CAST(:oid AS text)) WHERE id = :id"
                ),
                {"id": event_id, "oid": f"oid_stale_{pk}"},
            )
        orphan_id = _make_orphan(
            conn, tables, sport_id, "oid_x", GUARDIANS, PIRATES, _utc(18, 18, 0)
        )
        store.insert_odds_snapshots(
            conn, tables, orphan_id, (_outcome("home"),), _utc(18, 12, 0)
        )

    summary = repair_orphan_events.run(engine=db)
    assert summary["repointable"] == 0
    assert len(summary["skipped_ambiguous"]) == 1
    assert summary["skipped_ambiguous"][0]["orphan_id"] == str(orphan_id)
    assert len(summary["skipped_ambiguous"][0]["siblings"]) == 2
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM odds_snapshots WHERE event_id = :id",
            id=orphan_id,
        )
        == 1
    )
