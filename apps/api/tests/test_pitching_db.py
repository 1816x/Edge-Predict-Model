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
    # The batters (700010/700011/700021) must NOT appear in people lookups:
    # position players would multiply the calls for a field no batting
    # feature reads.
    assert summary["hands_backfilled"] == 2
    assert client.people_calls == [[700001, 700002]]
    # Batting rides the same fetch: 4 kept lines per boxscore — catcher,
    # DH, away leadoff AND the away starter's own batting line (two-way
    # case). The broken batter drops with an anomaly and the pinch runner
    # is skipped as zero-PA. Asserting the anomaly CONTENT (not just the
    # count) pins the job wiring: the zero-PA and anomaly counters are
    # both 2 here and a transposition would otherwise pass the suite.
    assert summary["batting_lines_upserted"] == 8
    assert summary["batting_zero_pa_skipped"] == 2
    assert summary["batting_anomalies_total"] == 2
    assert summary["batting_anomalies"][0].endswith(
        "away:700020:batting_missing:atBats"
    )
    assert "batting_note" not in summary

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pitching_game_logs")).scalar() == 6
        assert conn.execute(text("SELECT count(*) FROM batting_game_logs")).scalar() == 8
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
        # Pitchers resolved; batters stored with hand NULL (never asked).
        # 700002 is the two-way case (pitching AND batting line in the same
        # box): his batter entry must not clobber the pitcher entry, and his
        # /people-resolved hand must survive.
        assert hands == {
            608566: "R", 700001: "R", 700002: "L",
            700010: None, 700011: None, 700021: None,
        }
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
        catcher = conn.execute(
            text(
                """
                SELECT b.at_bats, b.doubles, b.home_runs, b.walks,
                       b.batting_order, b.plate_appearances
                FROM batting_game_logs b JOIN players p ON p.id = b.player_id
                WHERE p.mlb_person_id = 700010 LIMIT 1
                """
            )
        ).one()
        assert tuple(catcher) == (4, 1, 1, 1, 200, 5)

    # Resume: everything already ingested is skipped without HTTP.
    rerun = backfill_pitching.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    assert rerun["boxscores_fetched"] == 0
    assert rerun["events_skipped_existing"] == 2

    # Historical-fill path: an event with pitching but no batting (the
    # state of the whole 2018-2026 archive when migration 004 lands) is
    # pending again, re-fetches ONCE and writes only the batting half.
    with db.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM batting_game_logs WHERE event_id IN (
                    SELECT id FROM events
                    WHERE external_ids ->> 'mlb_game_pk' = '910001'
                )
                """
            )
        )
    refill = backfill_pitching.run(
        "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
    )
    assert refill["boxscores_fetched"] == 1
    assert refill["events_skipped_existing"] == 1
    assert refill["batting_lines_upserted"] == 4
    assert refill["lines_upserted"] == 0  # pitching half untouched
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pitching_game_logs")).scalar() == 6
        assert conn.execute(text("SELECT count(*) FROM batting_game_logs")).scalar() == 8

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
        assert conn.execute(text("SELECT count(*) FROM batting_game_logs")).scalar() == 8

    # Force across CHUNKS: the fake client lists the same games for every
    # chunk (the suspended-game shape: one gamePk under two dates). The
    # run-level dedupe must process each game once — without it the same
    # boxscore is fetched, deleted and re-upserted per chunk and every
    # summary counter it touches counts double.
    two_day = backfill_pitching.run(
        "2024-06-01", "2024-06-02", client=client, engine=db,
        sleep_seconds=0, force=True, chunk_days=1,
    )
    assert two_day["boxscores_fetched"] == 2  # one per REAL game, not per chunk
    assert two_day["batting_lines_upserted"] == 8
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM batting_game_logs")).scalar() == 8


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
def test_sync_lineups_archives_records_on_change_and_respects_as_of(db):
    from app.jobs import sync_lineups

    _seed_events(db, [910010], date_iso="2026-07-08")
    client = FakeMlbClient([_schedule_game(910010, "2026-07-08")])
    # now BEFORE the 23:10Z start and inside the lookahead window: pre-game.
    before = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)

    first = sync_lineups.run(
        "2026-07-08", client=client, engine=db, now=before, sleep_seconds=0.0
    )
    # Fixture posts 2 home + 3 away starters (multiples of 100); both sides
    # archived, one snapshot each.
    assert first["games_in_window"] == 1
    assert first["sides_posted"] == 2
    assert first["snapshots_new"] == 2
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT side, batting_order FROM event_lineups ORDER BY side, batting_order")
        ).all()
    assert [(r.side, r.batting_order) for r in rows] == [
        ("away", 100), ("away", 600), ("away", 900),
        ("home", 100), ("home", 200),
    ]

    # Same posted lineup again: nothing new (store-layer dedupe).
    second = sync_lineups.run(
        "2026-07-08", client=client, engine=db, now=before, sleep_seconds=0.0
    )
    assert second["snapshots_new"] == 0

    # A lineup change (swap the home leadoff): ONE new snapshot, the old one
    # stays audited.
    client.get_boxscore = lambda pk: _boxscore_with_home_leadoff(999999)
    third = sync_lineups.run(
        "2026-07-08", client=client, engine=db, now=before, sleep_seconds=0.0
    )
    assert third["snapshots_new"] == 1

    # As-of safety: a game already started is NEVER archived.
    after = datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc)
    started = sync_lineups.run(
        "2026-07-08", client=client, engine=db, now=after, sleep_seconds=0.0
    )
    assert started["games_in_window"] == 0
    assert started["boxscores_fetched"] == 0


def _boxscore_with_home_leadoff(person_id: int) -> dict:
    """The fixture boxscore with the home leadoff slot reassigned."""
    box = load_fixture("mlb_boxscore.json")
    box["teams"]["home"]["players"]["ID700011"]["person"]["id"] = person_id
    box["teams"]["home"]["players"]["ID700011"]["person"]["fullName"] = "New Leadoff"
    return box


@pytest.mark.integration
def test_sync_lineups_survives_a_boxscore_error(db):
    """A single game's boxscore error must NOT crash the job: it is chained
    under `set -e` before the irreplaceable odds snapshot, so a transient MLB
    error becomes a summary anomaly, never an abort."""
    from app.ingestion.mlb_client import MlbApiError
    from app.jobs import sync_lineups

    _seed_events(db, [910010], date_iso="2026-07-08")
    client = FakeMlbClient([_schedule_game(910010, "2026-07-08")])

    def _boom(pk):
        raise MlbApiError("500 boom")

    client.get_boxscore = _boom
    before = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    result = sync_lineups.run(
        "2026-07-08", client=client, engine=db, now=before, sleep_seconds=0.0
    )
    assert result["boxscore_errors"] == 1
    assert result["snapshots_new"] == 0  # nothing archived, but no crash


class FakeTxnClient:
    """Serves the transactions fixture for any date range and counts calls."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def get_transactions(self, start_date, end_date, sport_id: int = 1):
        self.calls.append((start_date, end_date))
        return load_fixture("mlb_transactions.json")


@pytest.mark.integration
def test_sync_transactions_idempotent_with_drift_canary(db):
    """The transactions/IL archive job: idempotent by mlb_transaction_id, and
    the summary counts IL placements/activations plus the il_desc_unclassified
    drift canary (0 when every IL-mention classifies)."""
    from app.ingestion import store
    from app.jobs import sync_transactions

    # Seed the Yankees so from_team resolves (unknown teams stay NULL).
    tables = store.reflect_tables(db, ("sports", "teams", "players"))
    with db.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        store.get_or_create_team(conn, tables, sport_id, H, 147)

    client = FakeTxnClient()
    first = sync_transactions.run(
        "2026-07-10", "2026-07-13", client=client, engine=db, sleep_seconds=0.0
    )
    assert first["transactions_seen"] == 4
    assert first["transactions_upserted"] == 4
    # 900001 placed + 900007 transferred-to-60-day = 2 placements; 900002 = 1
    # activation; the 2019-era "disabled list" wording would also classify.
    assert first["il_placements"] == 2
    assert first["il_activations"] == 1
    assert first["il_desc_unclassified_total"] == 0
    assert first["parse_anomalies_total"] == 3  # no-person, no-id, no-date

    # Re-run: idempotent (no duplicate rows by natural key).
    second = sync_transactions.run(
        "2026-07-10", "2026-07-13", client=client, engine=db, sleep_seconds=0.0
    )
    assert second["transactions_upserted"] == 4
    with db.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM player_transactions")
        ).scalar() == 4


@pytest.mark.integration
def test_sync_transactions_degrades_without_migration_006(db):
    """Pre-006 window (code merged, migration not applied): the whole job
    degrades to a skipped note instead of failing the daily cron."""
    from pathlib import Path

    from sqlalchemy import text as sql_text

    from app.jobs import apply_migration, sync_transactions

    with db.begin() as conn:
        conn.execute(sql_text("DROP TABLE player_transactions"))
    try:
        summary = sync_transactions.run(
            "2026-07-10", "2026-07-13", client=FakeTxnClient(), engine=db,
            sleep_seconds=0.0,
        )
        assert "apply migration 006" in summary["skipped"]
    finally:
        migration = (
            Path(__file__).parents[3] / "infra" / "migrations"
            / "006-player-transactions.sql"
        )
        apply_migration.run(str(migration), engine=db)


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
def test_backfill_degrades_to_pitching_only_without_migration_004(db):
    """Pre-004 window (code merged, migration not applied yet): the batting
    half degrades to a note and pitching ingestion continues untouched —
    the daily cron must warn, never paint the whole run red."""
    from pathlib import Path

    from sqlalchemy import text as sql_text

    from app.jobs import apply_migration

    _seed_events(db, [910001])
    client = FakeMlbClient([_schedule_game(910001, "2024-06-01")])
    with db.begin() as conn:
        conn.execute(sql_text("DROP TABLE batting_game_logs"))
    try:
        summary = backfill_pitching.run(
            "2024-06-01", "2024-06-01", client=client, engine=db, sleep_seconds=0
        )
        assert "apply migration 004" in summary["batting_note"]
        assert summary["lines_upserted"] == 3  # pitching half fully ingested
        assert summary["batting_lines_upserted"] == 0
    finally:
        migration = (
            Path(__file__).parents[3] / "infra" / "migrations" / "004-batting-game-logs.sql"
        )
        apply_migration.run(str(migration), engine=db)


@pytest.mark.integration
def test_migration_003_is_idempotent(db):
    from pathlib import Path

    from app.jobs import apply_migration

    migration = Path(__file__).parents[3] / "infra" / "migrations" / "003-pitching-and-probables.sql"
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    assert first["statements"] == second["statements"] == 9


@pytest.mark.integration
def test_migration_004_is_idempotent(db):
    """Runs through the REAL apply_migration splitter (the naive ';' split
    that a semicolon inside a comment famously broke in migration 003)."""
    from pathlib import Path

    from app.jobs import apply_migration

    migration = Path(__file__).parents[3] / "infra" / "migrations" / "004-batting-game-logs.sql"
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    # 1 tabla + 2 índices + 1 drop trigger + 1 create trigger (BEGIN/COMMIT
    # los salta el splitter).
    assert first["statements"] == second["statements"] == 5


@pytest.mark.integration
def test_migration_005_is_idempotent(db):
    """Through the REAL apply_migration splitter: a ';' inside any comment
    would split it mid-line and break the statement count (the migration-003
    bug this guards against)."""
    from pathlib import Path

    from app.jobs import apply_migration

    migration = Path(__file__).parents[3] / "infra" / "migrations" / "005-event-lineups.sql"
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    # 1 tabla + 1 índice (BEGIN/COMMIT los salta el splitter).
    assert first["statements"] == second["statements"] == 2


@pytest.mark.integration
def test_migration_006_is_idempotent(db):
    """Through the REAL apply_migration splitter: the F1.4 transactions/IL
    archive. A ';' inside any comment would split it mid-line and inflate the
    statement count (the migration-003 bug this guards against — caught exactly
    that in this migration's first draft: 'docs/04 §1.5; y ...')."""
    from pathlib import Path

    from app.jobs import apply_migration

    migration = (
        Path(__file__).parents[3] / "infra" / "migrations" / "006-player-transactions.sql"
    )
    first = apply_migration.run(str(migration), engine=db)
    second = apply_migration.run(str(migration), engine=db)
    # 1 tabla + 1 índice (BEGIN/COMMIT los salta el splitter).
    assert first["statements"] == second["statements"] == 2


def test_backfill_pitching_rejects_inverted_range():
    with pytest.raises(ValueError, match="before start_date"):
        backfill_pitching.run(
            "2024-06-02", "2024-06-01", client=FakeMlbClient([]), engine=object()
        )
