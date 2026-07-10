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
    """Target game H vs A on 2026-07-08T23:00Z plus curated history,
    including starter game logs and probables for the July 6th game.

    Shared by the feature-builder tests and the bulk-vs-online parity test.
    Probables MATCH the actual starters on purpose: parity between the
    online builder (probable-based) and the training frame (actual-starter
    based) only holds when they agree.
    """
    from datetime import datetime, timezone

    from sqlalchemy import text

    from app.ingestion import store
    from app.ingestion.parsers import GameResult, PitchingLine, ScheduledGame

    H, A, X = "Boston Red Sox", "New York Yankees", "Tampa Bay Rays"
    SP1, SP2 = 500001, 500002  # X's righty ace, H's lefty
    R1, R2, R3 = 700101, 700102, 700103  # relievers: X, H, X

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

    def _line(pid, is_home, is_starter, outs, bf, k, bb, hbp, hr, fly, sac, pitches):
        return PitchingLine(
            mlb_person_id=pid, full_name=f"P{pid}", pitch_hand=None,
            is_home=is_home, is_starter=is_starter, outs_recorded=outs,
            batters_faced=bf, strikeouts=k, walks=bb, hit_batsmen=hbp,
            home_runs=hr, fly_outs=fly, ground_outs=None, sac_flies=sac,
            pitches_thrown=pitches,
        )

    tables = store.reflect_tables(
        db,
        (
            "sports", "teams", "events", "event_results", "feature_snapshots",
            "players", "pitching_game_logs", "event_probables",
        ),
    )
    ts = lambda m, d, h: datetime(2026, m, d, h, 0, tzinfo=timezone.utc)  # noqa: E731
    with db.begin() as conn:
        conn.execute(text("TRUNCATE players CASCADE"))
        sport_id = store.get_sport_id(conn, tables)
        target_id = _seed(
            conn, tables, sport_id, 900010, ts(7, 8, 23), H, A, status="scheduled"
        )
        # H history: a win and a loss inside the 30d window...
        e900001 = _seed(conn, tables, sport_id, 900001, ts(7, 5, 23), H, X,
                        GameResult(900001, 5, 3, 3, 1))
        e900002 = _seed(conn, tables, sport_id, 900002, ts(7, 6, 23), X, H,
                        GameResult(900002, 7, 2, 4, 0))
        # ...one game outside the 30d window (June 5th)...
        e900003 = _seed(conn, tables, sport_id, 900003, ts(6, 5, 23), H, X,
                        GameResult(900003, 1, 0, 0, 0))
        # ...and one AFTER as_of (starts 07-09T00:00Z): must never leak in.
        _seed(conn, tables, sport_id, 900004, ts(7, 9, 0), H, X,
              GameResult(900004, 10, 0, 8, 0))
        # A history: single loss on July 4th.
        e900005 = _seed(conn, tables, sport_id, 900005, ts(7, 4, 23), A, X,
                        GameResult(900005, 2, 6, 1, 2))

        # --- Starter block seeds (docs/04 §1.3) ------------------------------
        player_cache: dict[int, object] = {}
        store.bulk_upsert_players(
            conn, tables, sport_id,
            [
                {"mlb_person_id": SP1, "full_name": "Righty Ace", "pitch_hand": "R"},
                {"mlb_person_id": SP2, "full_name": "Lefty Homegrown", "pitch_hand": "L"},
                {"mlb_person_id": R1, "full_name": "Setup Righty", "pitch_hand": "R"},
                {"mlb_person_id": R2, "full_name": "Long Man", "pitch_hand": "L"},
                {"mlb_person_id": R3, "full_name": "Mopup Arm", "pitch_hand": "R"},
            ],
            player_cache,
        )
        teams = store.load_team_cache(conn, tables, sport_id)
        # SP1 (pitches for X): starts on June 5th and July 4th.
        store.bulk_upsert_pitching_logs(
            conn, tables, e900003, teams[H], teams[X],
            [_line(SP1, False, True, 18, 24, 6, 2, 0, 1, 5, 1, 92)], player_cache,
        )
        store.bulk_upsert_pitching_logs(
            conn, tables, e900005, teams[A], teams[X],
            [_line(SP1, False, True, 12, 18, 3, 4, 0, 2, 6, 0, 88)], player_cache,
        )
        # SP2 (pitches for H): one start on July 5th, pitch count unrecorded.
        store.bulk_upsert_pitching_logs(
            conn, tables, e900001, teams[H], teams[X],
            [_line(SP2, True, True, 15, 20, 7, 3, 1, 0, 4, 0, None)], player_cache,
        )
        # The July 6th game itself: SP1 started for X (home), SP2 for H
        # (away), and X's reliever R3 pitched in relief — that same-day
        # line must NEVER enter the July 6th bullpen windows (intraday-safe
        # rule) nor the starter block (strict < start).
        store.bulk_upsert_pitching_logs(
            conn, tables, e900002, teams[X], teams[H],
            [
                _line(SP1, True, True, 16, 22, 5, 1, 0, 1, 4, 0, 90),
                _line(SP2, False, True, 14, 19, 6, 2, 0, 1, 3, 1, 85),
                _line(R3, True, False, 3, 4, 1, 0, 0, 0, 1, 0, 15),
            ],
            player_cache,
        )
        # --- Bullpen seeds (docs/04 §1.4, for the July 6th game: D=Jul 6,
        # 30d window = Jun 6..Jul 5, fatigue = Jul 3..Jul 5) -----------------
        # X bullpen: 9 outs on Jul 4 + 6 outs on Jul 5 (b2b) ...
        store.bulk_upsert_pitching_logs(
            conn, tables, e900005, teams[A], teams[X],
            [_line(R1, False, False, 9, 12, 2, 3, 0, 1, 4, 0, 30)], player_cache,
        )
        store.bulk_upsert_pitching_logs(
            conn, tables, e900001, teams[H], teams[X],
            [_line(R1, False, False, 6, 8, 3, 1, 0, 0, 2, 0, 25)], player_cache,
        )
        # ...and a June 5th line JUST outside the 30d window (boundary).
        store.bulk_upsert_pitching_logs(
            conn, tables, e900003, teams[H], teams[X],
            [_line(R3, False, False, 3, 4, 2, 0, 0, 0, 1, 0, 12)], player_cache,
        )
        # H bullpen: exactly 3 outs yesterday — the b2b threshold edge.
        store.bulk_upsert_pitching_logs(
            conn, tables, e900001, teams[H], teams[X],
            [_line(R2, True, False, 3, 5, 1, 2, 1, 1, 1, 1, 20)], player_cache,
        )
        # Probables announced the morning of July 6th, matching the starters.
        store.record_probables(
            conn, tables,
            [
                {"event_id": e900002, "side": "home",
                 "player_id": player_cache[SP1], "first_seen_at": ts(7, 6, 12)},
                {"event_id": e900002, "side": "away",
                 "player_id": player_cache[SP2], "first_seen_at": ts(7, 6, 12)},
            ],
        )
    return db, tables, target_id
