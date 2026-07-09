"""As-of feature builder: rolling form, leakage guard, snapshot dedupe."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.features import builder

pytestmark = pytest.mark.integration

AS_OF = datetime(2026, 7, 8, 22, 0, tzinfo=timezone.utc)

# The `seeded` fixture (target game + curated history) lives in conftest.py:
# it is shared with the bulk-vs-online parity test in test_ml.py.


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


def test_starter_block_hand_computed_values(seeded):
    """docs/04 §1.3 block for the July 6th game, from the as-of probables.

    League before 2026-07-06T23:00Z = three starter rows (SP1 on Jun 5 and
    Jul 4, SP2 on Jul 5): sumK 16, sumBB 9, sumBF 62, sumHR 3, sumFB 19,
    sum(BB+HBP) 10, outs 45 (15 IP) -> lg_kbb 7/62, lg_hrfb 3/19,
    lg_xfip_core 37/15.
    """
    from sqlalchemy import text

    db, tables, _ = seeded
    with db.connect() as conn:
        event_id = conn.execute(
            text("SELECT id FROM events WHERE external_ids ->> 'mlb_game_pk' = '900002'")
        ).scalar_one()
        features = builder.build_features(
            conn, tables, event_id, "moneyline",
            datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc),
        )

    home = features["home"]  # SP1: starts Jun 5 (K6 BB2 BF24) + Jul 4 (K3 BB4 BF18)
    lg_kbb, lg_hrfb, lg_core = 7 / 62, 3 / 19, 37 / 15
    assert home["sp_kbb_pct_l5_starts"] == round((3 + 60 * lg_kbb) / 102, 4)
    assert home["sp_kbb_pct_season"] == home["sp_kbb_pct_l5_starts"]  # both starts are 2026
    assert home["sp_xfip_l5_starts"] == round(
        (13 * 15 * lg_hrfb + 3 * 6 - 2 * 9 + 15 * lg_core) / 25, 4
    )
    assert home["sp_days_rest"] == 2  # Jul 6 minus Jul 4
    assert home["sp_pitch_count_l2_starts"] == 180  # 92 + 88
    assert home["sp_is_lhp"] == 0

    away = features["away"]  # SP2: one start Jul 5 (K7 BB3 BF20), pitches NULL
    assert away["sp_kbb_pct_l5_starts"] == round((4 + 60 * lg_kbb) / 80, 4)
    assert away["sp_xfip_l5_starts"] == round(
        (13 * 4 * lg_hrfb + 3 * 4 - 2 * 7 + 15 * lg_core) / 20, 4
    )
    assert away["sp_days_rest"] == 1
    assert away["sp_pitch_count_l2_starts"] is None  # unrecorded, never zero
    assert away["sp_is_lhp"] == 1

    assert features["feature_version"] == "team_form_sp_v2"


def test_starter_block_none_without_probable(seeded):
    """The target game has no probables recorded: the whole block is None."""
    db, tables, target_id = seeded
    with db.connect() as conn:
        features = builder.build_features(conn, tables, target_id, "moneyline", AS_OF)
    for side in ("home", "away"):
        assert all(features[side][name] is None for name in builder.SP_FEATURES)


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
