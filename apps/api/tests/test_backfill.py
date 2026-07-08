"""Backfill of historical results: parser units + DB integration."""

from datetime import date

import pytest
from sqlalchemy import text

from app.ingestion.parsers import parse_schedule_results
from app.jobs import backfill_results

from conftest import load_fixture


class TestParseScheduleResults:
    def test_final_games_with_f5_sums(self):
        results = {r.game_pk: r for r in parse_schedule_results(load_fixture("mlb_schedule_results.json"))}
        assert set(results) == {800001, 800002}  # the Preview game is skipped

        full_game = results[800001]
        assert (full_game.home_score, full_game.away_score) == (5, 3)
        # Innings 1-5: home 0+3+0+0+1, away 1+0+0+2+0. The empty bottom-9th
        # (home didn't bat) must not affect the F5 partials.
        assert (full_game.f5_home_score, full_game.f5_away_score) == (4, 3)

    def test_short_linescore_yields_null_f5(self):
        results = {r.game_pk: r for r in parse_schedule_results(load_fixture("mlb_schedule_results.json"))}
        rain_shortened = results[800002]
        assert (rain_shortened.home_score, rain_shortened.away_score) == (2, 1)
        assert rain_shortened.f5_home_score is None
        assert rain_shortened.f5_away_score is None

    def test_final_without_score_is_skipped(self):
        payload = load_fixture("mlb_schedule_results.json")
        del payload["dates"][0]["games"][0]["teams"]["home"]["score"]
        results = parse_schedule_results(payload)
        assert [r.game_pk for r in results] == [800002]

    def test_incomplete_fifth_inning_yields_null_f5(self):
        payload = load_fixture("mlb_schedule_results.json")
        payload["dates"][0]["games"][0]["linescore"]["innings"][4]["home"] = {}
        result = parse_schedule_results(payload)[0]
        assert result.f5_home_score is None and result.f5_away_score is None


class ChunkRecordingClient:
    """Serves the fixture for every chunk and records the ranges requested."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def get_schedule_range(self, start_date: str, end_date: str, hydrate: str = "linescore"):
        self.calls.append((start_date, end_date))
        return load_fixture("mlb_schedule_results.json")


@pytest.mark.integration
def test_backfill_upserts_events_and_results(db):
    client = ChunkRecordingClient()
    summary = backfill_results.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    assert summary["chunks"] == 1
    assert summary["games_in_feed"] == 3
    assert summary["results_upserted"] == 2
    assert summary["f5_missing"] == 1

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar() == 3
        assert conn.execute(text("SELECT count(*) FROM event_results")).scalar() == 2
        f5 = conn.execute(
            text(
                """
                SELECT er.f5_home_score, er.f5_away_score
                FROM event_results er JOIN events e ON e.id = er.event_id
                WHERE e.external_ids ->> 'mlb_game_pk' = '800001'
                """
            )
        ).one()
        assert tuple(f5) == (4, 3)

    # Idempotent: the second pass updates in place, no duplicate rows.
    rerun = backfill_results.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    assert rerun["results_upserted"] == 2
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM event_results")).scalar() == 2


@pytest.mark.integration
def test_backfill_chunking_covers_range_without_overlap(db):
    client = ChunkRecordingClient()
    backfill_results.run(
        "2024-06-01", "2024-06-25", chunk_days=10, client=client, engine=db, sleep_seconds=0
    )
    assert client.calls == [
        ("2024-06-01", "2024-06-10"),
        ("2024-06-11", "2024-06-20"),
        ("2024-06-21", "2024-06-25"),
    ]


@pytest.mark.integration
def test_traditional_doubleheader_same_listed_start(db):
    """Two DISTINCT gamePks, same teams, IDENTICAL listed start time (as in
    the real 2018 schedule) must yield two events with their own identities —
    the constraint/merge bug the first production backfill exposed."""
    from datetime import datetime, timezone

    from app.ingestion import store
    from app.ingestion.parsers import ScheduledGame

    start = datetime(2018, 6, 18, 21, 5, tzinfo=timezone.utc)

    def game(pk):
        return ScheduledGame(
            game_pk=pk, start_time=start, status="final",
            home_name="Chicago Cubs", away_name="St. Louis Cardinals",
            home_mlb_id=112, away_mlb_id=138,
            home_probable=None, away_probable=None,
        )

    tables = store.reflect_tables(db)
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        id1, created1 = store.upsert_event_from_schedule(conn, tables, sport_id, game(530769))
        id2, created2 = store.upsert_event_from_schedule(conn, tables, sport_id, game(530770))
        # Re-run both: identities must stay put.
        id1b, _ = store.upsert_event_from_schedule(conn, tables, sport_id, game(530769))
        id2b, _ = store.upsert_event_from_schedule(conn, tables, sport_id, game(530770))

    assert created1 and created2
    assert id1 != id2
    assert (id1b, id2b) == (id1, id2)
    with db.connect() as conn:
        pks = conn.execute(
            text("SELECT external_ids ->> 'mlb_game_pk' FROM events ORDER BY 1")
        ).scalars().all()
        assert pks == ["530769", "530770"]


@pytest.mark.integration
def test_bulk_chunk_handles_doubleheader_and_adopts_orphan(db):
    """Bulk path: (a) two same-time games insert as two events; (b) an event
    created earlier by the odds job (no mlb_game_pk) is adopted, not duped."""
    from datetime import datetime, timezone

    from app.ingestion import store
    from app.ingestion.parsers import GameResult, OddsEvent, ScheduledGame

    start = datetime(2018, 6, 18, 21, 5, tzinfo=timezone.utc)

    def game(pk, home="Chicago Cubs", away="St. Louis Cardinals", when=start):
        return ScheduledGame(
            game_pk=pk, start_time=when, status="final",
            home_name=home, away_name=away, home_mlb_id=None, away_mlb_id=None,
            home_probable=None, away_probable=None,
        )

    tables = store.reflect_tables(db)
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        # Orphan from the odds job: same matchup as pk 530771, 5 min drift.
        store.find_or_create_event_for_odds(
            conn, tables, sport_id,
            OddsEvent(
                source_id="abc123", home_team="Boston Red Sox",
                away_team="New York Yankees",
                commence_time=datetime(2018, 6, 18, 23, 5, tzinfo=timezone.utc),
                outcomes=(), skipped=(),
            ),
        )
        cache = store.load_team_cache(conn, tables, sport_id)
        games = [
            game(530769), game(530770),  # traditional doubleheader, same time
            game(530771, "Boston Red Sox", "New York Yankees",
                 datetime(2018, 6, 18, 23, 10, tzinfo=timezone.utc)),
        ]
        results = {530769: GameResult(530769, 3, 1, 2, 0)}
        counts = store.bulk_upsert_schedule_chunk(
            conn, tables, sport_id, games, results, cache
        )

    assert counts["events_created"] == 2  # the two doubleheader games
    assert counts["events_refreshed"] == 1  # the adopted orphan
    assert counts["results_upserted"] == 1
    with db.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM events")).scalar()
        adopted = conn.execute(
            text(
                """
                SELECT count(*) FROM events
                WHERE external_ids ->> 'the_odds_api_id' = 'abc123'
                  AND external_ids ->> 'mlb_game_pk' = '530771'
                """
            )
        ).scalar()
    assert total == 3
    assert adopted == 1


@pytest.mark.integration
def test_migration_001_is_idempotent(db):
    """The deployed-DB migration must run cleanly even on a fresh schema
    that already ships the final shape."""
    from pathlib import Path

    from app.jobs import apply_migration

    migration = Path(__file__).parents[3] / "infra" / "migrations" / "001-events-identity-doubleheaders.sql"
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    assert first["statements"] == second["statements"] == 2


def test_backfill_rejects_inverted_range():
    with pytest.raises(ValueError, match="before start_date"):
        backfill_results.run(
            "2024-06-02", "2024-06-01", client=ChunkRecordingClient(), engine=object()
        )
    # date.fromisoformat guards malformed input before any network call
    with pytest.raises(ValueError):
        backfill_results.run("junk", "2024-06-01", client=ChunkRecordingClient(), engine=object())
