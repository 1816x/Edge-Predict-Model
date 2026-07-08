"""As-of feature builder: rolling form, leakage guard, snapshot dedupe."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.features import builder
from app.ingestion import store
from app.ingestion.parsers import GameResult, ScheduledGame

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 7, 8, 22, 0, tzinfo=timezone.utc)

H, A, X = "Boston Red Sox", "New York Yankees", "Tampa Bay Rays"


def _game(pk, start, home, away, status="final"):
    return ScheduledGame(
        game_pk=pk,
        start_time=start,
        status=status,
        home_name=home,
        away_name=away,
        home_mlb_id=None,
        away_mlb_id=None,
        home_probable=None,
        away_probable=None,
    )


def _seed(conn, tables, sport_id, pk, start, home, away, result=None, status="final"):
    event_id, _ = store.upsert_event_from_schedule(
        conn, tables, sport_id, _game(pk, start, home, away, status)
    )
    if result is not None:
        store.upsert_event_result(conn, tables, event_id, result)
    return event_id


@pytest.fixture
def seeded(db):
    """Target game H vs A on 2026-07-08T23:00Z plus curated history."""
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


def test_team_form_rolling_windows(seeded):
    db, tables, target_id = seeded
    with db.connect() as conn:
        features = builder.build_features(conn, tables, target_id, "moneyline", AS_OF)

    home = features["home"]
    # Window floor is June 8th: exactly the two July games count.
    assert home["games_30d"] == 2
    assert home["win_pct_30d"] == 0.5
    assert home["runs_pg_30d"] == 3.5  # (5 + 2) / 2
    assert home["runs_allowed_pg_30d"] == 5.0  # (3 + 7) / 2
    assert home["f5_games_30d"] == 2
    assert home["f5_runs_pg_30d"] == 1.5  # (3 + 0) / 2
    assert home["f5_runs_allowed_pg_30d"] == 2.5  # (1 + 4) / 2
    assert home["rest_days"] == 2  # July 8th minus July 6th
    assert home["games_last_7d"] == 2

    away = features["away"]
    assert away["games_30d"] == 1
    assert away["win_pct_30d"] == 0.0
    assert away["runs_pg_30d"] == 2.0
    assert away["runs_allowed_pg_30d"] == 6.0
    assert away["rest_days"] == 4
    assert away["games_last_7d"] == 1


def test_future_game_never_leaks(seeded):
    db, tables, target_id = seeded
    with db.connect() as conn:
        features = builder.build_features(conn, tables, target_id, "moneyline", AS_OF)
    # The 10-0 blowout on July 9th exists in the DB; if it leaked, home
    # win_pct would be 2/3 and runs_pg would jump.
    assert features["home"]["games_30d"] == 2
    assert features["home"]["win_pct_30d"] == 0.5


def test_as_of_after_start_is_refused(seeded):
    db, tables, target_id = seeded
    late = datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc)
    with db.connect() as conn:
        with pytest.raises(ValueError, match="anti-leakage"):
            builder.build_features(conn, tables, target_id, "moneyline", late)


def test_snapshot_dedupes_identical_vectors(seeded):
    db, tables, target_id = seeded
    earlier = datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)
    with db.begin() as conn:
        features_a = builder.build_features(conn, tables, target_id, "f5_moneyline", earlier)
        features_b = builder.build_features(conn, tables, target_id, "f5_moneyline", AS_OF)
        # No games between 20:00 and 22:00: identical vectors, one stored row.
        assert features_a == features_b
        id_a = builder.save_feature_snapshot(
            conn, tables, target_id, "f5_moneyline", features_a, earlier
        )
        id_b = builder.save_feature_snapshot(
            conn, tables, target_id, "f5_moneyline", features_b, AS_OF
        )
    assert id_a == id_b
    with db.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM feature_snapshots")).scalar()
        stored_hash = conn.execute(text("SELECT feature_hash FROM feature_snapshots")).scalar()
    assert count == 1
    assert stored_hash == builder.canonical_feature_hash(features_a)
