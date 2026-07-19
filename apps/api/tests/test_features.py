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

    # --- Bullpen block (§1.4). Reliever league in [D-365, Jul 5]: R1 twice
    # (Jul 4/5) + R2 (Jul 5) + R3 (Jun 5): K8 BB6 HBP1 HR2 FB11, 21 outs.
    # The June 5th line sits OUTSIDE the 30d team window (Jun 6..Jul 5) and
    # R3's same-day relief line on Jul 6 must not count anywhere.
    lg_hrfb_bp, lg_core_bp = 2 / 11, (13 * 2 + 3 * 7 - 2 * 8) / 7
    assert home["bullpen_ip_l3d"] == 5.0  # X: 9 + 6 outs on Jul 4-5
    assert home["bullpen_b2b_flag"] == 1  # 6 outs yesterday
    assert home["bullpen_xfip_30d"] == round(
        (13 * 7 * lg_hrfb_bp + 3 * 4 - 2 * 5 + 15 * lg_core_bp) / 20, 4
    )
    assert home["bullpen_ip_expected"] == 5.0  # SP1: (18+12)/2 outs per start
    assert away["bullpen_ip_l3d"] == 1.0  # H: R2's 3 outs on Jul 5
    assert away["bullpen_b2b_flag"] == 1  # exactly at the 3-out threshold
    assert away["bullpen_xfip_30d"] == round(
        (13 * 3 * lg_hrfb_bp + 3 * 3 - 2 * 1 + 15 * lg_core_bp) / 16, 4
    )
    assert away["bullpen_ip_expected"] == 5.0  # SP2: 15 outs in his one start

    # bullpen_il_depletion (§1.4b): None here — the seed archives no player
    # transactions, so the IL archive is not alive as-of and the count is
    # unknown (never a fabricated 0), exactly like star_out_flag in this seed.
    assert home["bullpen_il_depletion"] is None
    assert away["bullpen_il_depletion"] is None

    assert features["feature_version"] == "team_form_sp_bp_off_lineup_star_bpil_v7"


def test_offense_block_hand_computed_values(seeded):
    """docs/04 §1.2 block for the July 6th game (D = Jul 6, windows are UTC
    days ending Jul 5). Every number below is pencil-derived from the
    conftest batting seeds with the frozen 2017 weights.

    Away (H, probables say it faces SP1 = R):
      30d window = e900001 only (e900003 is Jun 5, one day OUTSIDE the
      [Jun 6, Jul 5] window; H's own same-day line on e900002 must be
      excluded by the intraday rule): AB6 H3 2B1 3B1 BB2 HBP1 SF0 SH1 ->
      woba_den 9, num = 2*.693 + .723 + .877 + 1.232 + 1.552 = 5.770.
      Season adds e900003 (num 3.212 / den 8).
      Split vs R: 30d has NO R-classified game (e900001's opposing side
      archived no starter -> NULL hand), trailing year has e900003 (vs
      SP1, R): pure prior = 3.212/8.
    Home (X, faces SP2 = L):
      30d = e900005 (NULL hand) + e900001 (vs SP2, L): AB7 H2 BB1 ->
      woba_den 8, num = .693 + 2*.877 = 2.447; same-day X line on
      e900002 excluded. Split vs L: window == target (e900001 only,
      1.570/5) -> shrunk value equals the raw split.
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

    away = features["away"]  # H
    assert away["team_woba_30d"] == round(5.770 / 9, 4)
    assert away["team_woba_season"] == round(8.982 / 17, 4)
    assert away["team_woba_vs_opp_hand_30d"] == round(3.212 / 8, 4)
    assert away["team_iso_30d"] == round((6 - 3) / 6, 4)  # TB 6 (1B+2B+3B)
    assert away["team_k_pct_30d"] == round(2 / 10, 4)  # PA = 6+2+1+0+1
    assert away["team_bb_pct_30d"] == round(2 / 10, 4)
    assert away["team_ops_30d"] == round(6 / 9 + 6 / 6, 4)  # OBP + SLG

    home = features["home"]  # X
    assert home["team_woba_30d"] == round(2.447 / 8, 4)
    assert home["team_woba_season"] == home["team_woba_30d"]  # same two games
    # (1.570 + 200 * (1.570/5)) / (5 + 200) == 1.570/5 == 0.314 exactly.
    assert home["team_woba_vs_opp_hand_30d"] == 0.314
    assert home["team_iso_30d"] == 0.0  # two singles: a TRUE zero, not NULL
    assert home["team_k_pct_30d"] == round(3 / 8, 4)
    assert home["team_bb_pct_30d"] == round(1 / 8, 4)
    assert home["team_ops_30d"] == round(3 / 8 + 2 / 7, 4)

    # F5 carries the same offense block (only bullpen is excluded from F5).
    with db.connect() as conn:
        f5 = builder.build_features(
            conn, tables, event_id, "f5_moneyline",
            datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc),
        )
    assert f5["away"]["team_woba_30d"] == away["team_woba_30d"]


def test_bullpen_block_is_none_before_the_archive_is_alive(seeded):
    """June 5th: no reliever line exists in the trailing year (R3's own
    same-day line doesn't count) — the block must be None, NOT a fabricated
    'fully rested' zero. Zeros are only true while the archive is alive."""
    from sqlalchemy import text

    db, tables, _ = seeded
    with db.connect() as conn:
        event_id = conn.execute(
            text("SELECT id FROM events WHERE external_ids ->> 'mlb_game_pk' = '900003'")
        ).scalar_one()
        features = builder.build_features(
            conn, tables, event_id, "moneyline",
            datetime(2026, 6, 5, 23, 0, tzinfo=timezone.utc),
        )
    for side in ("home", "away"):
        assert features[side]["bullpen_ip_l3d"] is None
        assert features[side]["bullpen_b2b_flag"] is None
        assert features[side]["bullpen_xfip_30d"] is None


def test_f5_vector_excludes_bullpen_by_design(seeded):
    """docs/04 §1.4: bullpen features are REMOVED from F5, not zero-weighted."""
    from sqlalchemy import text

    db, tables, _ = seeded
    with db.connect() as conn:
        event_id = conn.execute(
            text("SELECT id FROM events WHERE external_ids ->> 'mlb_game_pk' = '900002'")
        ).scalar_one()
        features = builder.build_features(
            conn, tables, event_id, "f5_moneyline",
            datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc),
        )
    for side in ("home", "away"):
        assert not any(k.startswith("bullpen_") for k in features[side])
        # The starter block itself is intact.
        assert features[side]["sp_kbb_pct_l5_starts"] is not None


def test_starter_block_none_without_probable(seeded):
    """The target game has no probables recorded: the whole block is None.

    The offense vs-hand split is selected by the opposing PROBABLE's hand,
    so it must be None too — while the rest of the offense block keeps its
    values (home) or stays None because the team has no batting archive at
    all (away, the A team): no fabricated zeros either way."""
    db, tables, target_id = seeded
    with db.connect() as conn:
        features = builder.build_features(conn, tables, target_id, "moneyline", AS_OF)
    for side in ("home", "away"):
        assert all(features[side][name] is None for name in builder.SP_FEATURES)
        assert features[side]["team_woba_vs_opp_hand_30d"] is None
    # H's 30d window for the July 8th target: e900001 + e900002 (yesterday
    # relative to game day counts): (5.770 + 3.157) / (9 + 5).
    assert features["home"]["team_woba_30d"] == round(8.927 / 14, 4)
    assert features["home"]["team_woba_season"] == round(12.139 / 22, 4)
    for name in builder.OFFENSE_FEATURE_NAMES:
        assert features["away"][name] is None, name


def test_lineup_block_none_without_archived_snapshot(seeded):
    """The July 8th target has no event_lineups snapshot: the online block is
    honestly is_confirmed=0 with None wOBA features (never the realized
    box-score order — that would leak), in BOTH markets."""
    db, tables, target_id = seeded
    with db.connect() as conn:
        for market in ("moneyline", "f5_moneyline"):
            features = builder.build_features(conn, tables, target_id, market, AS_OF)
            for side in ("home", "away"):
                assert features[side]["lineup_is_confirmed"] == 0, (market, side)
                assert features[side]["lineup_woba_proj"] is None, (market, side)
                assert features[side]["top4_woba_vs_hand"] is None, (market, side)


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
