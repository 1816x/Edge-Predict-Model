"""Pitching backfill + probables: job-level integration against Postgres."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.jobs import backfill_pitching

from conftest import load_fixture

H, A = "Boston Red Sox", "New York Yankees"


def _schedule_game(pk: int, date_iso: str, probables: bool = False) -> dict:
    game = {
        "gamePk": pk,
        "gameDate": f"{date_iso}T23:10:00Z",
        "gameType": "R",
        "status": {"abstractGameState": "Final", "detailedState": "Final"},
        "teams": {
            "home": {"team": {"id": 111, "name": H}},
            "away": {"team": {"id": 147, "name": A}},
        },
    }
    if probables:
        game["teams"]["home"]["probablePitcher"] = {"id": 608566, "fullName": "Kutter Crawford"}
        game["teams"]["away"]["probablePitcher"] = {"id": 700002, "fullName": "Old Data Lefty"}
    return game


class FakeMlbClient:
    """Serves canned schedule/boxscore/people payloads and counts calls."""

    def __init__(self, schedule_games: list[dict]):
        self.schedule_games = schedule_games
        self.boxscore_calls: list[int] = []
        self.people_calls: list[list[int]] = []

    def get_schedule_range(self, start_date, end_date, hydrate="linescore"):
        return {"dates": [{"games": self.schedule_games}]}

    def get_schedule(self, date_iso):
        return {"dates": [{"games": self.schedule_games}]}

    def get_boxscore(self, game_pk: int):
        self.boxscore_calls.append(game_pk)
        return load_fixture("mlb_boxscore.json")

    def get_people(self, person_ids: list[int]):
        self.people_calls.append(list(person_ids))
        return {
            "people": [
                {"id": 700001, "pitchHand": {"code": "R"}},
                {"id": 700002, "pitchHand": {"code": "L"}},
            ]
        }


def _seed_events(db, pks: list[int], date_iso: str = "2024-06-01"):
    from app.ingestion import store
    from app.ingestion.parsers import ScheduledGame

    tables = store.reflect_tables(db)
    start = datetime.fromisoformat(f"{date_iso}T23:10:00+00:00")
    with db.begin() as conn:
        conn.execute(text("TRUNCATE players CASCADE"))
        sport_id = store.get_sport_id(conn, tables)
        for pk in pks:
            store.upsert_event_from_schedule(
                conn, tables, sport_id,
                ScheduledGame(
                    game_pk=pk, start_time=start, status="final",
                    home_name=H, away_name=A, home_mlb_id=111, away_mlb_id=147,
                    home_probable=None, away_probable=None, game_type="R",
                ),
            )


@pytest.mark.integration
def test_backfill_pitching_ingests_resumes_and_forces(db):
    _seed_events(db, [910001, 910002])
    # 910003 is in the feed but has no event row: reported, never invented.
    client = FakeMlbClient(
        [_schedule_game(910001, "2024-06-01"), _schedule_game(910002, "2024-06-01"),
         _schedule_game(910003, "2024-06-01")]
    )

    summary = backfill_pitching.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    # 3 usable lines per boxscore (the away reliever lacks battersFaced).
    assert summary["boxscores_fetched"] == 2
    assert summary["lines_upserted"] == 6
    assert summary["missing_events_total"] == 1
    assert summary["missing_events"] == ["910003"]
    assert summary["parse_anomalies_total"] > 0
    # Hands: Crawford came with the boxscore, the other two via /people.
    assert summary["hands_backfilled"] == 2
    assert client.people_calls == [[700001, 700002]]

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pitching_game_logs")).scalar() == 6
        starters = conn.execute(
            text(
                """
                SELECT p.mlb_person_id, l.is_home
                FROM pitching_game_logs l JOIN players p ON p.id = l.player_id
                WHERE l.is_starter ORDER BY 1, 2
                """
            )
        ).all()
        assert {(r.mlb_person_id, r.is_home) for r in starters} == {
            (608566, True), (700002, False),
        }
        hands = dict(
            conn.execute(text("SELECT mlb_person_id, pitch_hand FROM players")).all()
        )
        assert hands == {608566: "R", 700001: "R", 700002: "L"}
        outs = conn.execute(
            text(
                """
                SELECT l.outs_recorded FROM pitching_game_logs l
                JOIN players p ON p.id = l.player_id
                WHERE p.mlb_person_id = 700002 LIMIT 1
                """
            )
        ).scalar()
        assert outs == 17  # "5.2" IP

    # Resume: everything already ingested is skipped without HTTP.
    rerun = backfill_pitching.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    assert rerun["boxscores_fetched"] == 0
    assert rerun["events_skipped_existing"] == 2

    # Force: re-fetches, upserts in place (no duplicate rows), and the hands
    # already stored are not re-asked to /people.
    people_calls_before = len(client.people_calls)
    forced = backfill_pitching.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0, force=True
    )
    assert forced["boxscores_fetched"] == 2
    assert len(client.people_calls) == people_calls_before
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pitching_game_logs")).scalar() == 6


@pytest.mark.integration
def test_sync_schedule_archives_probable_history(db):
    from app.jobs import sync_schedule

    _seed_events(db, [])
    client = FakeMlbClient([_schedule_game(910010, "2026-07-08", probables=True)])

    first = sync_schedule.run("2026-07-08", client=client, engine=db)
    assert first["probables"] == {"seen": 2, "new": 2}

    # Same slate again: history unchanged.
    second = sync_schedule.run("2026-07-08", client=client, engine=db)
    assert second["probables"] == {"seen": 2, "new": 0}

    # Scratch: the home probable changes -> ONE new history row, the old one
    # stays (auditable who-was-announced-when).
    client.schedule_games[0]["teams"]["home"]["probablePitcher"] = {
        "id": 700001, "fullName": "Bullpen Arm",
    }
    third = sync_schedule.run("2026-07-08", client=client, engine=db)
    assert third["probables"] == {"seen": 2, "new": 1}

    # Re-announcement (X -> Y -> X, rain-shuffled rotation): X's return MUST
    # create a new row, or the as-of resolution would answer Y forever.
    client.schedule_games[0]["teams"]["home"]["probablePitcher"] = {
        "id": 608566, "fullName": "Kutter Crawford",
    }
    fourth = sync_schedule.run("2026-07-08", client=client, engine=db)
    assert fourth["probables"] == {"seen": 2, "new": 1}

    with db.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT ep.side, p.mlb_person_id
                FROM event_probables ep JOIN players p ON p.id = ep.player_id
                ORDER BY ep.first_seen_at, ep.side
                """
            )
        ).all()
        latest_home = conn.execute(
            text(
                """
                SELECT p.mlb_person_id
                FROM event_probables ep JOIN players p ON p.id = ep.player_id
                WHERE ep.side = 'home'
                ORDER BY ep.first_seen_at DESC LIMIT 1
                """
            )
        ).scalar_one()
    assert len(rows) == 4
    assert latest_home == 608566  # X vigente tras el re-anuncio


@pytest.mark.integration
def test_placeholder_name_never_clobbers_real_name(db):
    from app.ingestion import store

    _seed_events(db, [])
    tables = store.reflect_tables(db, store.PITCHING_TABLES)
    cache: dict[int, object] = {}
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        store.bulk_upsert_players(
            conn, tables, sport_id,
            [{"mlb_person_id": 1, "full_name": "Zack Wheeler", "pitch_hand": "R"}],
            cache,
        )
        # A schedule payload that omits fullName fabricates a placeholder;
        # it must not destroy the real name already on file.
        store.bulk_upsert_players(
            conn, tables, sport_id,
            [{"mlb_person_id": 1, "full_name": "MLB person 1", "pitch_hand": None}],
            cache,
        )
        # A brand-new player with no known name DOES keep the placeholder.
        store.bulk_upsert_players(
            conn, tables, sport_id,
            [{"mlb_person_id": 2, "full_name": "MLB person 2", "pitch_hand": None}],
            cache,
        )
    with db.connect() as conn:
        names = dict(
            conn.execute(text("SELECT mlb_person_id, full_name FROM players")).all()
        )
    assert names == {1: "Zack Wheeler", 2: "MLB person 2"}


@pytest.mark.integration
def test_migration_003_is_idempotent(db):
    from pathlib import Path

    from app.jobs import apply_migration

    migration = Path(__file__).parents[3] / "infra" / "migrations" / "003-pitching-and-probables.sql"
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    assert first["statements"] == second["statements"] == 9


def test_backfill_pitching_rejects_inverted_range():
    with pytest.raises(ValueError, match="before start_date"):
        backfill_pitching.run(
            "2024-06-02", "2024-06-01", client=FakeMlbClient([]), engine=object()
        )
