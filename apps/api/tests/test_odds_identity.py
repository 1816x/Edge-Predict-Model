"""Odds event identity under The Odds API id reissue (tier 2.5 re-stamp).

Reproduces the 2026-07-18/19 production bug: the feed reissues a game's
event id (±1 min commence drift), the old id vanishes, and pre-fix the
resolver spawned a duplicate orphan event that silently fragmented the
game's odds history. These tests pin the fixed behavior: re-stamp on
reissue, doubleheader legs never cross-matched, honest refusal (visible
``created_ambiguous``) when the identical-start edge makes the target
undecidable, and team-name drift resolved through the alias map.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.ingestion import store
from app.ingestion.parsers import ScheduledGame
from app.jobs import snapshot_odds

pytestmark = pytest.mark.integration

GUARDIANS, PIRATES = "Cleveland Guardians", "Pittsburgh Pirates"


def _feed_event(source_id, home, away, commence, price=1.91):
    return {
        "id": source_id,
        "commence_time": commence,
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": home, "price": price},
                            {"name": away, "price": price},
                        ],
                    }
                ],
            }
        ],
    }


class FeedClient:
    """Slate-only fake; per-event calls must never happen (include_f5=False)."""

    def __init__(self, payload):
        self.payload = payload

    def get_mlb_odds(self, **kwargs):
        return self.payload

    def get_event_odds(self, event_id, **kwargs):
        raise AssertionError("per-event call with include_f5=False")


def _cycle(db, payload, captured_at):
    return snapshot_odds.run(
        client=FeedClient(payload), engine=db, captured_at=captured_at,
        include_f5=False,
    )


def _seed_schedule(db, games):
    tables = store.reflect_tables(db)
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        for pk, home, away, start in games:
            store.upsert_event_from_schedule(
                conn, tables, sport_id,
                ScheduledGame(
                    game_pk=pk, start_time=start, status="scheduled",
                    home_name=home, away_name=away,
                    home_mlb_id=None, away_mlb_id=None,
                    home_probable=None, away_probable=None,
                ),
            )
    return tables


def _scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _utc(day, hour, minute=0):
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_id_reissue_restamps_same_event_no_orphan(db):
    """A. Reissued id (+1 min drift) lands on the SAME event; idempotent."""
    _seed_schedule(db, [(824414, GUARDIANS, PIRATES, _utc(18, 17, 10))])

    first = _cycle(
        db,
        [_feed_event("oid_c137", GUARDIANS, PIRATES, "2026-07-18T17:10:00Z")],
        _utc(18, 6),
    )
    assert first["events_matched"] == 1  # tier 2 merged onto the mlb row
    assert first["events_created"] == 0
    assert first["snapshots_inserted"] == 2

    # The feed reissues the id with the observed ±1 min commence drift.
    second = _cycle(
        db,
        [_feed_event("oid_d8bf", GUARDIANS, PIRATES, "2026-07-18T17:11:00Z")],
        _utc(18, 12),
    )
    assert second["events_restamped"] == 1
    assert second["events_matched"] == 1
    assert second["events_created"] == 0
    assert second["restamp_ambiguous"] == []

    # ONE event row: both cycles' snapshots live under the mlb_game_pk.
    assert _scalar(db, "SELECT count(*) FROM events") == 1
    assert (
        _scalar(
            db,
            """
            SELECT count(*) FROM events
            WHERE external_ids ->> 'mlb_game_pk' = '824414'
              AND external_ids ->> 'the_odds_api_id' = 'oid_d8bf'
            """,
        )
        == 1
    )
    # start_time_utc stays MLB-authoritative (17:10, not the drifted 17:11).
    assert _scalar(db, "SELECT max(start_time_utc) FROM events") == _utc(18, 17, 10)
    assert _scalar(db, "SELECT count(*) FROM odds_snapshots") == 4

    # Idempotency: the reissued id now hits tier 1; same instant dedupes.
    third = _cycle(
        db,
        [_feed_event("oid_d8bf", GUARDIANS, PIRATES, "2026-07-18T17:11:00Z")],
        _utc(18, 12),
    )
    assert third["events_restamped"] == 0
    assert third["events_matched"] == 1
    assert third["snapshots_inserted"] == 0
    assert _scalar(db, "SELECT count(*) FROM events") == 1


def test_split_doubleheader_reissue_never_touches_other_leg(db):
    """B. Re-stamp of leg 1's id leaves leg 2 (id still live) untouched."""
    _seed_schedule(
        db,
        [
            (824414, GUARDIANS, PIRATES, _utc(18, 17, 10)),
            (824412, GUARDIANS, PIRATES, _utc(18, 23, 10)),
        ],
    )
    first = _cycle(
        db,
        [
            _feed_event("oid_a", GUARDIANS, PIRATES, "2026-07-18T17:10:00Z"),
            _feed_event("oid_b", GUARDIANS, PIRATES, "2026-07-18T23:10:00Z"),
        ],
        _utc(18, 6),
    )
    assert first["events_matched"] == 2
    assert first["events_created"] == 0

    # Only leg 1's id is reissued; oid_b stays live in the same payload.
    second = _cycle(
        db,
        [
            _feed_event("oid_a2", GUARDIANS, PIRATES, "2026-07-18T17:11:00Z"),
            _feed_event("oid_b", GUARDIANS, PIRATES, "2026-07-18T23:10:00Z"),
        ],
        _utc(18, 12),
    )
    assert second["events_restamped"] == 1
    assert second["events_created"] == 0

    assert _scalar(db, "SELECT count(*) FROM events") == 2
    leg = lambda pk: _scalar(  # noqa: E731
        db,
        "SELECT external_ids ->> 'the_odds_api_id' FROM events "
        "WHERE external_ids ->> 'mlb_game_pk' = :pk",
        pk=pk,
    )
    assert leg("824414") == "oid_a2"
    assert leg("824412") == "oid_b"


def test_identical_start_doubleheader_residual_and_ambiguous_refusal(db):
    """C. Identical-start DH: ids fill both legs (C1); a reissue with BOTH
    legs superseded is refused visibly as created_ambiguous (C2)."""
    _seed_schedule(
        db,
        [
            (930001, GUARDIANS, PIRATES, _utc(18, 18, 0)),
            (930002, GUARDIANS, PIRATES, _utc(18, 18, 0)),
        ],
    )
    # C1: two live ids resolve to the two id-less legs (tier 2, arbitrary
    # but stable assignment). Two events, both priced, no third row.
    first = _cycle(
        db,
        [
            _feed_event("oid_1", GUARDIANS, PIRATES, "2026-07-18T18:00:00Z"),
            _feed_event("oid_2", GUARDIANS, PIRATES, "2026-07-18T18:00:00Z"),
        ],
        _utc(18, 6),
    )
    assert first["events_matched"] == 2
    assert first["events_created"] == 0
    assert _scalar(db, "SELECT count(*) FROM events") == 2
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM events WHERE external_ids ? 'the_odds_api_id'",
        )
        == 2
    )

    # C2: a lone new id arrives while BOTH stored ids are superseded — any
    # re-stamp could clobber the wrong leg, so the resolver refuses and
    # creates visibly instead.
    second = _cycle(
        db,
        [_feed_event("oid_3", GUARDIANS, PIRATES, "2026-07-18T18:00:00Z")],
        _utc(18, 12),
    )
    assert second["events_created"] == 1
    assert second["events_restamped"] == 0
    assert [amb["source_id"] for amb in second["restamp_ambiguous"]] == ["oid_3"]
    assert _scalar(db, "SELECT count(*) FROM events") == 3


def test_genuinely_new_games_still_create(db):
    """D. The re-stamp tier never absorbs a real new listing."""
    # No schedule row at all: a brand-new matchup creates.
    first = _cycle(
        db,
        [_feed_event("oid_new", GUARDIANS, PIRATES, "2026-07-18T20:00:00Z")],
        _utc(18, 6),
    )
    assert first["events_created"] == 1
    assert first["restamp_ambiguous"] == []

    # Same matchup NEXT DAY while yesterday's id is stale: the stale event
    # is outside RESTAMP_WINDOW, so the new listing creates its own row.
    second = _cycle(
        db,
        [_feed_event("oid_new2", GUARDIANS, PIRATES, "2026-07-19T20:00:00Z")],
        _utc(19, 6),
    )
    assert second["events_created"] == 1
    assert second["events_restamped"] == 0
    assert _scalar(db, "SELECT count(*) FROM events") == 2
    assert (
        _scalar(
            db,
            "SELECT external_ids ->> 'the_odds_api_id' FROM events "
            "WHERE start_time_utc = :s",
            s=_utc(18, 20),
        )
        == "oid_new"
    )


def test_team_alias_resolves_to_one_row_and_merges_event(db):
    """E. Confirmed alias (All-Star naming) lands on the MLB team row."""
    NL, AL = "National League All-Stars", "American League All-Stars"
    _seed_schedule(db, [(823443, NL, AL, _utc(15, 0, 0))])

    summary = _cycle(
        db,
        [
            _feed_event(
                "oid_as", "National League", "American League",
                "2026-07-15T00:01:00Z",
            )
        ],
        datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc),
    )
    assert summary["events_matched"] == 1  # tier 2 merged, not created
    assert summary["events_created"] == 0

    # ONE team row per league, named canonically, with the alias stamped.
    assert _scalar(db, "SELECT count(*) FROM teams") == 2
    assert (
        _scalar(
            db,
            "SELECT external_ids ->> 'the_odds_api' FROM teams WHERE name = :n",
            n=NL,
        )
        == "National League"
    )
    assert (
        _scalar(
            db,
            """
            SELECT count(*) FROM events
            WHERE external_ids ->> 'mlb_game_pk' = '823443'
              AND external_ids ->> 'the_odds_api_id' = 'oid_as'
            """,
        )
        == 1
    )
