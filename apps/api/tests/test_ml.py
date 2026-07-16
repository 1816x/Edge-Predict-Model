"""F1 pipeline: metric sanity, synthetic walk-forward, bulk-vs-online parity."""

from datetime import date, datetime, timedelta, timezone

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

from app.features import lineup as lu  # noqa: E402
from app.ml import dataset as ds  # noqa: E402  (after importorskip)
from app.ml import train as tr  # noqa: E402


class TestMetrics:
    def test_log_loss_and_brier_known_values(self):
        y = np.array([1, 0])
        p = np.array([0.8, 0.2])
        assert tr.log_loss(y, p) == pytest.approx(-np.log(0.8), rel=1e-6)
        assert tr.brier(y, p) == pytest.approx(0.04, rel=1e-9)

    def test_ece_perfectly_calibrated_bins(self):
        # 100 events at p=0.75 with exactly 75 hits -> ECE 0 in that bin.
        y = np.array([1] * 75 + [0] * 25)
        p = np.full(100, 0.75)
        assert tr.ece(y, p) == pytest.approx(0.0, abs=1e-9)

    def test_ece_detects_overconfidence(self):
        y = np.array([1] * 50 + [0] * 50)  # reality: 50%
        p = np.full(100, 0.9)  # model says 90%
        assert tr.ece(y, p) == pytest.approx(0.4, abs=1e-9)

    def test_platt_fixes_systematic_overconfidence(self):
        rng = np.random.default_rng(7)
        true_p = rng.uniform(0.3, 0.7, 4000)
        y = (rng.uniform(size=4000) < true_p).astype(int)
        # Overconfident distortion of the true probability.
        p_raw = np.clip(true_p + (true_p - 0.5) * 0.8, 0.01, 0.99)
        cal = tr.PlattCalibrator.fit(p_raw, y)
        assert tr.ece(y, cal.apply(p_raw)) < tr.ece(y, p_raw)


def _synthetic_games(n_seasons: int = 6, per_season: int = 400) -> pd.DataFrame:
    """Synthetic archive: 8 teams with fixed strengths, home edge, F5 partials."""
    rng = np.random.default_rng(11)
    strengths = {t: s for t, s in zip(range(8), np.linspace(-0.6, 0.6, 8))}
    rows = []
    eid = 0
    for season in range(2018, 2018 + n_seasons):
        day = datetime(season, 4, 1, tzinfo=timezone.utc)
        for g in range(per_season):
            home, away = rng.choice(8, size=2, replace=False)
            logit = 0.25 + strengths[home] - strengths[away]  # home edge
            p_home = 1 / (1 + np.exp(-logit))
            home_win = rng.uniform() < p_home
            hs, as_ = (int(5 + rng.poisson(2)), int(2 + rng.poisson(1.5)))
            if not home_win:
                hs, as_ = as_, hs
            f5h, f5a = max(0, hs - int(rng.integers(0, 3))), max(0, as_ - int(rng.integers(0, 3)))
            rows.append(
                {
                    "event_id": f"e{eid}",
                    "start_time_utc": day + timedelta(hours=int(g % 5)),
                    "home_team_id": f"t{home}",
                    "away_team_id": f"t{away}",
                    "home_score": hs,
                    "away_score": as_,
                    "f5_home_score": f5h,
                    "f5_away_score": f5a,
                }
            )
            eid += 1
            if g % 4 == 3:
                day += timedelta(days=1)
    df = pd.DataFrame(rows)
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df.sort_values("start_time_utc").reset_index(drop=True)


class TestWalkForward:
    def test_learns_signal_and_respects_time(self):
        import json

        games = _synthetic_games()
        frame = ds.build_training_frame(games, "moneyline")
        report = tr.walk_forward_report(frame, min_train_seasons=4)
        # The report must survive json.dumps — a stray numpy scalar killed
        # the first production training run at the finish line.
        json.dumps(report)
        # 6 seasons, min 4 to train -> exactly 2022 and 2023 get tested.
        assert sorted(report["seasons"]) == [2022, 2023]
        for rep in report["seasons"].values():
            # With real team-strength signal, a learned model must beat the
            # constant-0.5 baseline on log loss.
            assert rep["hist_gb"]["calibrated"]["log_loss"] < rep["baseline_constant"]["log_loss"]
            assert rep["logistic_scaled"]["calibrated"]["log_loss"] < rep["baseline_constant"]["log_loss"]

    def test_f5_market_drops_pushes(self):
        games = _synthetic_games(n_seasons=2, per_season=60)
        frame = ds.build_training_frame(games, "f5_moneyline")
        merged = frame.merge(games, on="event_id")
        assert (merged["f5_home_score"] != merged["f5_away_score"]).all()

    def test_unknown_market_rejected(self):
        with pytest.raises(ValueError, match="unknown market"):
            ds.build_training_frame(_synthetic_games(1, 10), "spreads")

    def test_no_pitching_data_degrades_to_nan_and_still_trains(self):
        # Before the pitching backfill runs, sp_* columns are 100% NaN; the
        # walk-forward must impute (median -> 0.0 fallback) and not crash.
        games = _synthetic_games()
        frame = ds.build_training_frame(games, "moneyline", pitching=None)
        sp_cols = [c for c in ds.FEATURE_COLUMNS if "_sp_" in c]
        assert len(sp_cols) == 14
        assert frame[sp_cols].isna().all().all()
        report = tr.walk_forward_report(frame, min_train_seasons=4)
        assert sorted(report["seasons"]) == [2022, 2023]


def _synthetic_pitching() -> pd.DataFrame:
    """One pitcher ('the league'): a 2023 start + five 2024 starts + target.

    2023-09-20: K10 BB0 BF30 outs21 HR0 FB(5+0+0) pitches100
    2024 (Apr 1/6/11/16/21): K6 BB2 BF25 outs18 HR1 fly4 sac1 pitches90 each
    2024-04-26: the target start (its own stats never enter its features).
    """
    rows = [
        dict(event_id="g0", player_id="p1", is_home=True,
             start_time_utc=datetime(2023, 9, 20, 23, 0, tzinfo=timezone.utc),
             outs_recorded=21, batters_faced=30, strikeouts=10, walks=0,
             hit_batsmen=0, home_runs=0, fly_outs=5, sac_flies=0,
             pitches_thrown=100, pitch_hand="L"),
    ]
    for n, day in enumerate((1, 6, 11, 16, 21, 26)):
        rows.append(
            dict(event_id=f"g{n + 1}", player_id="p1", is_home=True,
                 start_time_utc=datetime(2024, 4, day, 23, 0, tzinfo=timezone.utc),
                 outs_recorded=18, batters_faced=25, strikeouts=6, walks=2,
                 hit_batsmen=0, home_runs=1, fly_outs=4, sac_flies=1,
                 pitches_thrown=90, pitch_hand="L"),
        )
    return pd.DataFrame(rows)


class TestStarterFeatures:
    def _by_event(self):
        feats = ds._starter_features(_synthetic_pitching())
        return {row["event_id"]: row for _, row in feats.iterrows()}

    def test_first_career_start_has_no_history(self):
        row = self._by_event()["g0"]
        for name in ds.SP_FEATURE_NAMES:
            if name != "sp_is_lhp":
                assert pd.isna(row[name]), name

    def test_l5_window_and_shrinkage_hand_computed(self):
        # Target 04-26: last 5 = the 2024 starts. K-BB sum 20, BF 125.
        # League as-of = all 6 priors: lg_kbb = 30/155.
        row = self._by_event()["g6"]
        lg_kbb = 30 / 155
        expected = (20 + 60 * lg_kbb) / (125 + 60)
        assert row["sp_kbb_pct_l5_starts"] == pytest.approx(round(expected, 4))
        # All five l5 starts are 2024: season == l5.
        assert row["sp_kbb_pct_season"] == row["sp_kbb_pct_l5_starts"]

    def test_xfip_core_hand_computed(self):
        # League: HR 5, FB 35, K 40, BB+HBP 10, IP 37.
        row = self._by_event()["g6"]
        lg_hrfb = 5 / 35
        lg_core = (13 * 5 + 3 * 10 - 2 * 40) / 37
        expected = (13 * 30 * lg_hrfb + 3 * 10 - 2 * 30 + 15 * lg_core) / (30 + 15)
        assert row["sp_xfip_l5_starts"] == pytest.approx(round(expected, 4))

    def test_season_boundary_excludes_prior_year(self):
        # First 2024 start: l5 sees the 2023-09-20 start, season(2024) is
        # empty -> season features stay NaN while l5 is computed.
        row = self._by_event()["g1"]
        lg_kbb = 10 / 30  # league as-of = only the 2023 start
        expected = (10 + 60 * lg_kbb) / (30 + 60)
        assert row["sp_kbb_pct_l5_starts"] == pytest.approx(round(expected, 4))
        assert pd.isna(row["sp_kbb_pct_season"])

    def test_rest_days_none_after_long_layoff(self):
        by_event = self._by_event()
        # 194 days since 2023-09-20: IL/offseason, not "rest".
        assert pd.isna(by_event["g1"]["sp_days_rest"])
        assert by_event["g6"]["sp_days_rest"] == 5

    def test_pitch_count_l2(self):
        by_event = self._by_event()
        assert by_event["g6"]["sp_pitch_count_l2_starts"] == 180
        assert by_event["g1"]["sp_pitch_count_l2_starts"] == 100

    def test_handedness(self):
        assert self._by_event()["g0"]["sp_is_lhp"] == 1.0


def _synthetic_bullpen() -> pd.DataFrame:
    """One team's relievers ('the league'): 6 outs on 06-01, 2 on 06-02."""
    rows = [
        dict(team_id="T1",
             start_time_utc=datetime(2024, 6, 1, 23, 0, tzinfo=timezone.utc),
             outs_recorded=6, strikeouts=4, walks=1, hit_batsmen=0,
             home_runs=1, fly_outs=3, sac_flies=0),
        dict(team_id="T1",
             start_time_utc=datetime(2024, 6, 2, 23, 0, tzinfo=timezone.utc),
             outs_recorded=2, strikeouts=1, walks=1, hit_batsmen=0,
             home_runs=0, fly_outs=1, sac_flies=0),
    ]
    return pd.DataFrame(rows)


def _bullpen_games() -> pd.DataFrame:
    games = pd.DataFrame(
        [
            dict(event_id="g1", home_team_id="T1", away_team_id="T2",
                 start_time_utc=datetime(2024, 6, 3, 20, 0, tzinfo=timezone.utc)),
            dict(event_id="g2", home_team_id="T1", away_team_id="T2",
                 start_time_utc=datetime(2024, 6, 2, 12, 0, tzinfo=timezone.utc)),
        ]
    )
    games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
    return games


class TestBullpenFeatures:
    def _rows(self):
        feats = ds._bullpen_features(_synthetic_bullpen(), _bullpen_games())
        return {row["event_id"]: row for _, row in feats.iterrows()}

    def test_fatigue_and_b2b_threshold(self):
        g1 = self._rows()["g1"]  # D = 06-03: both lines in [D-3, D-1]
        assert g1["home_bullpen_ip_l3d"] == pytest.approx(round(8 / 3, 4))
        # Yesterday (06-02) the bullpen threw 2 outs < 3: NOT back-to-back.
        assert g1["home_bullpen_b2b_flag"] == 0.0

    def test_same_day_lines_are_excluded(self):
        # g2 is played on 06-02: that day's 2-out line must not count
        # (intraday-safe rule) — only 06-01 enters the windows.
        g2 = self._rows()["g2"]
        assert g2["home_bullpen_ip_l3d"] == 2.0
        assert g2["home_bullpen_b2b_flag"] == 1.0  # 6 outs on 06-01

    def test_xfip_hand_computed(self):
        # g2's league = the 06-01 line only: HR 1, FB 4, K 4, BB+HBP 1, IP 2.
        g2 = self._rows()["g2"]
        lg_hrfb, lg_core = 1 / 4, (13 * 1 + 3 * 1 - 2 * 4) / 2.0
        expected = (13 * 4 * lg_hrfb + 3 * 1 - 2 * 4 + 15 * lg_core) / (2 + 15)
        assert g2["home_bullpen_xfip_30d"] == pytest.approx(round(expected, 4))

    def test_team_without_lines_rests_at_zero_but_quality_unknown(self):
        g1 = self._rows()["g1"]
        assert g1["away_bullpen_ip_l3d"] == 0.0
        assert g1["away_bullpen_b2b_flag"] == 0.0
        assert pd.isna(g1["away_bullpen_xfip_30d"])  # no sample: unknown

    def test_game_before_the_archive_is_alive_stays_nan(self):
        # A game on the archive's very first day has an empty as-of league:
        # zeros would fabricate 'fully rested' where the truth is 'no data'.
        games = _bullpen_games()
        games.loc[len(games)] = dict(
            event_id="g0", home_team_id="T1", away_team_id="T2",
            start_time_utc=datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
        )
        feats = ds._bullpen_features(_synthetic_bullpen(), games)
        g0 = feats[feats["event_id"] == "g0"].iloc[0]
        for side in ("home", "away"):
            assert pd.isna(g0[f"{side}_bullpen_ip_l3d"])
            assert pd.isna(g0[f"{side}_bullpen_b2b_flag"])
            assert pd.isna(g0[f"{side}_bullpen_xfip_30d"])

    def test_f5_frame_carries_no_bullpen_columns(self):
        games = _synthetic_games(n_seasons=2, per_season=60)
        f5 = ds.build_training_frame(games, "f5_moneyline")
        ml = ds.build_training_frame(games, "moneyline")
        assert not any("bullpen" in c for c in f5.columns)
        assert sum("bullpen" in c for c in ml.columns) == 8
        # The offense block (docs/04 §1.2) enters BOTH vectors: 7 per side.
        assert sum("team_woba" in c or "team_ops" in c or "team_iso" in c
                   or "team_k_pct" in c or "team_bb_pct" in c for c in f5.columns) == 14
        # The lineup block (docs/04 §1.5) also enters BOTH vectors: 3 per side.
        assert sum("lineup" in c or "top4" in c for c in ml.columns) == 6
        assert sum("lineup" in c or "top4" in c for c in f5.columns) == 6
        # star_out_flag (§1.5, F1.4) enters BOTH vectors too: 1 per side.
        assert sum("star_out_flag" in c for c in ml.columns) == 2
        assert sum("star_out_flag" in c for c in f5.columns) == 2
        assert len(ds.FEATURE_COLUMNS) == 62
        assert len(ds.F5_FEATURE_COLUMNS) == 54
        assert ds.feature_columns("f5_moneyline") == ds.F5_FEATURE_COLUMNS


def _synthetic_offense_frames():
    """Team T1's aggregated batting games around a 2024-04-20 target (D).

    Dates chosen to pin every window edge:
      rZ 2023-04-01 (before D-365: excluded everywhere; absurd numbers
         make any leak visible), hand L
      rB 2023-12-20 (prev year: split target yes, season no), hand L
      rC 2024-03-21 (= D-30: inside the 30d window edge), hand None
      rD 2024-03-20 (= D-31: outside 30d, inside season/365), hand L
      rE 2024-04-19 (= D-1: inside everything), hand L
      rF 2024-04-20 (same day as D: excluded, intraday-safe rule), hand L
    """
    def _row(eid, day, hand, ab, h, d2, d3, hr, bb, ibb, so, hbp, sf, sh):
        return dict(
            event_id=eid, team_id="T1", is_home=True,
            start_time_utc=datetime(*day, 23, 0, tzinfo=timezone.utc),
            at_bats=ab, hits=h, doubles=d2, triples=d3, home_runs=hr,
            walks=bb, intentional_walks=ibb, strikeouts=so, hit_by_pitch=hbp,
            sac_flies=sf, sac_bunts=sh, opp_starter_hand=hand,
        )

    batting = pd.DataFrame(
        [
            _row("bZ", (2023, 4, 1), "L", 10, 10, 0, 0, 10, 0, 0, 0, 0, 0, 0),
            _row("bB", (2023, 12, 20), "L", 8, 2, 0, 0, 0, 2, 0, 3, 0, 0, 0),
            _row("bC", (2024, 3, 21), None, 5, 1, 0, 0, 0, 0, 0, 2, 0, 0, 0),
            _row("bD", (2024, 3, 20), "L", 6, 3, 1, 0, 1, 0, 0, 2, 0, 0, 0),
            _row("bE", (2024, 4, 19), "L", 4, 2, 0, 0, 0, 1, 1, 1, 0, 1, 0),
            _row("bF", (2024, 4, 20), "L", 3, 3, 0, 0, 3, 0, 0, 0, 0, 0, 0),
        ]
    )
    batting["start_time_utc"] = pd.to_datetime(batting["start_time_utc"], utc=True)
    games = pd.DataFrame(
        [
            dict(event_id="g1", home_team_id="T1", away_team_id="T2",
                 start_time_utc=datetime(2024, 4, 20, 20, 0, tzinfo=timezone.utc)),
            dict(event_id="g2", home_team_id="T1", away_team_id="T2",
                 start_time_utc=datetime(2024, 4, 20, 12, 0, tzinfo=timezone.utc)),
        ]
    )
    games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
    # g1's opposing starter is a lefty; g2's hand is unknown (no starter).
    return batting, games, {("g1", "home"): "L"}


class TestOffenseFeatures:
    """Hand-computed values with the frozen 2017 weights.

    wOBA parts per row: rB num 2*.693 + 2*.877 = 3.140, den 10;
    rC num .877, den 5; rD num .877 + 1.232 + 1.98 = 4.089, den 6;
    rE num 2*.877 = 1.754, den 4+1-1+1 = 5.
    """

    def _row(self, event_id="g1"):
        batting, games, hand = _synthetic_offense_frames()
        feats = ds._offense_features(batting, games, hand)
        return feats[feats["event_id"] == event_id].iloc[0]

    def test_30d_window_edges_and_same_day_exclusion(self):
        # 30d = [D-30, D-1] = rC + rE only: rD sits one day outside, rF is
        # same-day. woba = (0.877 + 1.754) / (5 + 5) = 0.2631.
        row = self._row()
        assert row["home_team_woba_30d"] == pytest.approx(round(2.631 / 10, 4))
        # PA = 9+1+0+1+0 = 11; K 3, BB 1. TB = 3 singles, ISO true zero.
        assert row["home_team_k_pct_30d"] == pytest.approx(round(3 / 11, 4))
        assert row["home_team_bb_pct_30d"] == pytest.approx(round(1 / 11, 4))
        assert row["home_team_iso_30d"] == 0.0
        assert row["home_team_ops_30d"] == pytest.approx(round(4 / 11 + 3 / 9, 4))

    def test_season_excludes_prior_year(self):
        # Season 2024 = rD + rC + rE (rB is December, rZ is out of range):
        # (4.089 + 0.877 + 1.754) / (6 + 5 + 5) = 0.42.
        row = self._row()
        assert row["home_team_woba_season"] == pytest.approx(0.42)

    def test_split_shrinks_toward_trailing_year(self):
        # 30d L-games: rE only (1.754/5). Trailing-year L-target: rB+rD+rE
        # = 8.983/21 (rZ excluded: before D-365; rC has no hand).
        # (1.754 + 200*(8.983/21)) / (5 + 200) = 0.4259 — NOT the raw 30d
        # split, NOT the target: genuinely shrunk.
        row = self._row()
        expected = (1.754 + 200 * (8.983 / 21)) / 205
        assert row["home_team_woba_vs_opp_hand_30d"] == pytest.approx(round(expected, 4))
        assert round(expected, 4) not in (round(1.754 / 5, 4), round(8.983 / 21, 4))

    def test_unknown_hand_and_missing_archive_stay_nan(self):
        row_g2 = self._row("g2")
        # g2 has no known opposing hand: the split is unknowable.
        assert pd.isna(row_g2["home_team_woba_vs_opp_hand_30d"])
        # ...but the rest of the block still computes.
        assert row_g2["home_team_woba_30d"] == pytest.approx(round(2.631 / 10, 4))
        # T2 never batted: its whole block is NaN, never zeros.
        for name in ds.OFFENSE_FEATURE_NAMES:
            assert pd.isna(self._row()[f"away_{name}"]), name

    def test_no_batting_frame_degrades_to_nan_and_still_trains(self):
        games = _synthetic_games()
        frame = ds.build_training_frame(games, "moneyline", batting=None)
        off_cols = [c for c in ds.FEATURE_COLUMNS if "team_woba" in c]
        assert frame[off_cols].isna().all().all()
        report = tr.walk_forward_report(frame, min_train_seasons=4)
        assert sorted(report["seasons"]) == [2022, 2023]


def _sums(ab, h, d2, d3, hr, bb, ibb, so, hbp, sf, sh):
    """A batter's counting sums dict (the argument the lineup math takes)."""
    return dict(
        at_bats=ab, hits=h, doubles=d2, triples=d3, home_runs=hr, walks=bb,
        intentional_walks=ibb, strikeouts=so, hit_by_pitch=hbp,
        sac_flies=sf, sac_bunts=sh,
    )


class TestLineupMath:
    """Hand-computed lineup math with the frozen 2017 weights and priors
    (PRIOR wOBA 0.320, batter shrink 100 PA, split shrink 50 PA, PA-share
    slot weights). No DB — the pure formulas both paths share."""

    def test_batter_woba_shrinks_toward_prior(self):
        # 4 singles in 10 AB + 2 uBB: num = .877*4 + .693*2 = 4.894, den =
        # 10 + 2 = 12. shrunk = (4.894 + 100*0.320)/(12 + 100) = 0.3294.
        s = _sums(10, 4, 0, 0, 0, 2, 0, 0, 0, 0, 0)
        assert lu.batter_woba_asof(s) == pytest.approx(round(36.894 / 112, 4))

    def test_batter_without_pa_is_dropped_not_shrunk(self):
        # No plate appearances -> None. The league prior is NEVER injected as
        # if it were this batter's line (that would fabricate a hitter).
        assert lu.batter_woba_asof(_sums(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)) is None

    def test_weighted_lineup_renormalizes_over_present_slots(self):
        # Slot 2 has no usable wOBA: weight over slots 1 and 3 only.
        # (0.1216*0.400 + 0.1163*0.200)/(0.1216 + 0.1163) = 0.3022.
        got = lu.weighted_lineup_woba({1: 0.400, 2: None, 3: 0.200})
        expected = (0.1216 * 0.400 + 0.1163 * 0.200) / (0.1216 + 0.1163)
        assert got == pytest.approx(round(expected, 4))

    def test_weighted_lineup_none_when_no_usable_slot(self):
        assert lu.weighted_lineup_woba({1: None, 2: None}) is None

    def test_vs_hand_shrinks_toward_own_overall(self):
        # Overall: 6 singles/20 AB -> target 5.262/20 = 0.2631. Same-hand: 2
        # singles/5 AB -> num 1.754, den 5. (1.754 + 50*0.2631)/(5 + 50).
        overall = _sums(20, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        hand = _sums(5, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        expected = (1.754 + 50 * (5.262 / 20)) / 55
        assert lu.batter_woba_vs_hand_asof(hand, overall) == pytest.approx(
            round(expected, 4)
        )

    def test_vs_hand_empty_split_is_pure_prior(self):
        # Never faced this hand: value is the batter's own overall wOBA.
        overall = _sums(20, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        empty = _sums(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        assert lu.batter_woba_vs_hand_asof(empty, overall) == pytest.approx(
            round(5.262 / 20, 4)
        )

    def test_vs_hand_none_without_overall_sample(self):
        empty = _sums(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        assert lu.batter_woba_vs_hand_asof(empty, empty) is None


def _lineup_row(eid, day, pid, is_home, order, hand, ab, h, bb):
    """A per-batter lineup row (singles-only line keeps the wOBA math easy)."""
    return dict(
        event_id=eid, team_id="T1", is_home=is_home, player_id=pid,
        batting_order=order,
        start_time_utc=datetime(*day, 23, 0, tzinfo=timezone.utc),
        at_bats=ab, hits=h, doubles=0, triples=0, home_runs=0, walks=bb,
        intentional_walks=0, strikeouts=0, hit_by_pitch=0, sac_flies=0,
        sac_bunts=0, opp_starter_hand=hand,
    )


def _lineup_frames():
    """T1 (home) bats p1@1, p2@2, p3@3 in target g1 (2024-04-20 vs a righty).
    p1 and p2 have one prior game each (in the 365d window); p3 has none, so
    its slot drops. Opposing hand R selects the vs-hand split.
    """
    lineup = pd.DataFrame(
        [
            _lineup_row("h1", (2024, 4, 10), "p1", True, None, "R", 10, 4, 0),
            _lineup_row("h2", (2024, 4, 11), "p2", True, None, "R", 9, 3, 0),
            _lineup_row("g1", (2024, 4, 20), "p1", True, 100, "R", 3, 1, 0),
            _lineup_row("g1", (2024, 4, 20), "p2", True, 200, "R", 3, 1, 0),
            _lineup_row("g1", (2024, 4, 20), "p3", True, 300, "R", 3, 1, 0),
        ]
    )
    lineup["start_time_utc"] = pd.to_datetime(lineup["start_time_utc"], utc=True)
    games = pd.DataFrame(
        [dict(event_id="g1", home_team_id="T1", away_team_id="T2",
              start_time_utc=datetime(2024, 4, 20, 20, 0, tzinfo=timezone.utc))]
    )
    games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
    return lineup, games


class TestLineupFeatures:
    """Bulk lineup block (docs/04 §1.5): composition from the realized box
    score (is_confirmed=0), wOBA strictly prior, weighted by PA-share."""

    def _row(self, opp_hand=None):
        lineup, games = _lineup_frames()
        hand = {("g1", "home"): "R"} if opp_hand is None else opp_hand
        feats = ds._lineup_features(lineup, games, hand)
        return feats[feats["event_id"] == "g1"].iloc[0]

    def test_is_confirmed_is_zero_in_backtest(self):
        row = self._row()
        assert row["home_lineup_is_confirmed"] == 0.0

    def test_proj_renormalizes_over_present_batters(self):
        # p1 = (.877*4 + 32)/110 = 0.3228; p2 = (.877*3 + 32)/109 = 0.3177;
        # p3 (no prior line) drops. proj weights slots 1,2 only:
        # (0.1216*0.3228 + 0.1190*0.3177)/(0.1216 + 0.1190).
        row = self._row()
        p1 = round((0.877 * 4 + 32) / 110, 4)
        p2 = round((0.877 * 3 + 32) / 109, 4)
        expected = (0.1216 * p1 + 0.1190 * p2) / (0.1216 + 0.1190)
        assert row["home_lineup_woba_proj"] == pytest.approx(round(expected, 4))

    def test_top4_vs_hand_uses_opposing_hand(self):
        # Both prior games are vs R, so each batter's split == overall wOBA:
        # p1 0.877*4/10 = 0.3508, p2 0.877*3/9 = 0.2923. Weighted over slots
        # 1,2: (0.1216*0.3508 + 0.1190*0.2923)/(0.1216 + 0.1190).
        row = self._row()
        p1 = round(0.877 * 4 / 10, 4)
        p2 = round(0.877 * 3 / 9, 4)
        expected = (0.1216 * p1 + 0.1190 * p2) / (0.1216 + 0.1190)
        assert row["home_top4_woba_vs_hand"] == pytest.approx(round(expected, 4))

    def test_unknown_hand_leaves_top4_nan_but_proj_computed(self):
        row = self._row(opp_hand={})
        assert pd.isna(row["home_top4_woba_vs_hand"])
        assert not pd.isna(row["home_lineup_woba_proj"])

    def test_away_side_without_lineup_is_nan(self):
        # Only T1 (home) has a seeded lineup; the away composition is empty.
        row = self._row()
        assert pd.isna(row["away_lineup_woba_proj"])
        assert row["away_lineup_is_confirmed"] == 0.0

    def test_no_lineup_frame_degrades_to_nan_and_confirmed_zero(self):
        # Pre-backfill (no lineup frame): the wOBA columns are NaN (imputed)
        # but is_confirmed is a concrete 0, never NaN — the backtest regime.
        games = _synthetic_games()
        frame = ds.build_training_frame(games, "moneyline", lineup=None)
        for side in ("home", "away"):
            assert (frame[f"{side}_lineup_is_confirmed"] == 0.0).all()
            assert frame[f"{side}_lineup_woba_proj"].isna().all()
            assert frame[f"{side}_top4_woba_vs_hand"].isna().all()

    def test_doubleheader_game_one_does_not_leak_into_game_two(self):
        # Two T1 home games on the SAME UTC day; p1 explodes in the AM game.
        # The PM game's window is day < 2024-04-20, so the AM line (same day)
        # must NOT enter p1's wOBA — only the 04-18 prior game counts.
        lineup = pd.DataFrame(
            [
                _lineup_row("h0", (2024, 4, 18), "p1", True, None, "R", 10, 4, 0),
                _lineup_row("gam", (2024, 4, 20), "p1", True, 100, "R", 5, 5, 0),
                _lineup_row("gpm", (2024, 4, 20), "p1", True, 100, "R", 4, 0, 0),
            ]
        )
        lineup["start_time_utc"] = pd.to_datetime(lineup["start_time_utc"], utc=True)
        games = pd.DataFrame(
            [
                dict(event_id="gam", home_team_id="T1", away_team_id="T2",
                     start_time_utc=datetime(2024, 4, 20, 17, 0, tzinfo=timezone.utc)),
                dict(event_id="gpm", home_team_id="T1", away_team_id="T2",
                     start_time_utc=datetime(2024, 4, 20, 23, 0, tzinfo=timezone.utc)),
            ]
        )
        games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
        feats = ds._lineup_features(lineup, games, {})
        pm = feats[feats["event_id"] == "gpm"].iloc[0]
        # p1 wOBA from the 04-18 game ONLY: (.877*4 + 32)/110 = 0.3228.
        assert pm["home_lineup_woba_proj"] == pytest.approx(round((0.877 * 4 + 32) / 110, 4))


def _txn_frame(rows):
    """rows: list of (player_id, date, effect_desc, txn_id)."""
    return pd.DataFrame(
        [
            dict(
                player_id=pid,
                type_code="SC",
                type_desc="Status Change",
                description=desc,
                transaction_date=d,
                mlb_transaction_id=tid,
            )
            for pid, d, desc, tid in rows
        ]
    )


class TestStarOutFlag:
    """Bulk star_out_flag (docs/04 §1.5): count of the team's top-2 established
    batters on the IL as-of the game. STAR clears the 200 PA (wOBA-denom) gate
    from one big prior game; p2 is a thin sample that never qualifies."""

    def _frames(self):
        # STAR: 250 AB + 20 BB in the prior game -> den 270 >= 200 (a star).
        # p2: tiny sample, plays g1 so the realized lineup is non-empty.
        lineup = pd.DataFrame(
            [
                _lineup_row("h0", (2024, 4, 10), "STAR", True, None, "R", 250, 80, 20),
                _lineup_row("g1", (2024, 4, 20), "p2", True, 100, "R", 4, 1, 0),
            ]
        )
        lineup["start_time_utc"] = pd.to_datetime(lineup["start_time_utc"], utc=True)
        games = pd.DataFrame(
            [dict(event_id="g1", home_team_id="T1", away_team_id="T2",
                  start_time_utc=datetime(2024, 4, 20, 20, 0, tzinfo=timezone.utc))]
        )
        games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
        return lineup, games

    def _star_out(self, txns):
        lineup, games = self._frames()
        feats = ds._lineup_features(lineup, games, {("g1", "home"): "R"}, txns)
        return feats[feats["event_id"] == "g1"].iloc[0]["home_star_out_flag"]

    _PLACED = "placed on the 10-day injured list"
    _ACTIVATED = "activated from the 10-day injured list"

    def test_star_on_il_before_the_game_counts_one(self):
        txns = _txn_frame([("STAR", date(2024, 4, 15), self._PLACED, 1)])
        assert self._star_out(txns) == 1.0

    def test_star_activated_before_the_game_counts_zero(self):
        txns = _txn_frame([
            ("STAR", date(2024, 4, 15), self._PLACED, 1),
            ("STAR", date(2024, 4, 18), self._ACTIVATED, 2),
        ])
        assert self._star_out(txns) == 0.0

    def test_archive_not_alive_as_of_is_nan_never_zero(self):
        # The only move is ON the game day -> nothing strictly before it -> the
        # archive is not alive as-of -> unknown (NaN), never a fabricated 0.
        txns = _txn_frame([("STAR", date(2024, 4, 20), self._PLACED, 1)])
        assert pd.isna(self._star_out(txns))

    def test_no_transactions_frame_is_nan(self):
        assert pd.isna(self._star_out(None))

    def test_same_day_move_excluded_but_archive_alive_is_zero(self):
        # An earlier UNRELATED move keeps the archive alive; STAR's own move is
        # same-day (excluded) -> STAR not known out -> a TRUE zero.
        txns = _txn_frame([
            ("someone", date(2024, 4, 1), self._PLACED, 9),
            ("STAR", date(2024, 4, 20), self._PLACED, 1),
        ])
        assert self._star_out(txns) == 0.0

    def test_no_qualifying_star_is_nan_even_with_il_moves(self):
        # Only a thin-sample batter exists: no established star to speak of, so
        # the flag is unknown (NaN) even though the archive is alive.
        lineup = pd.DataFrame(
            [_lineup_row("g1", (2024, 4, 20), "p2", True, 100, "R", 4, 1, 0)]
        )
        lineup["start_time_utc"] = pd.to_datetime(lineup["start_time_utc"], utc=True)
        games = pd.DataFrame(
            [dict(event_id="g1", home_team_id="T1", away_team_id="T2",
                  start_time_utc=datetime(2024, 4, 20, 20, 0, tzinfo=timezone.utc))]
        )
        games["start_time_utc"] = pd.to_datetime(games["start_time_utc"], utc=True)
        txns = _txn_frame([("p2", date(2024, 4, 15), self._PLACED, 1)])
        feats = ds._lineup_features(lineup, games, {}, txns)
        assert pd.isna(feats.iloc[0]["home_star_out_flag"])


class TestMarketPriorSubset:
    def _frame(self):
        return ds.build_training_frame(_synthetic_games(), "moneyline")

    def test_no_prior_column_no_subset(self):
        report = tr.walk_forward_report(self._frame(), min_train_seasons=4)
        assert all(
            "market_prior_subset" not in rep for rep in report["seasons"].values()
        )

    def test_gate_not_evaluated_below_min_n(self):
        frame = self._frame()
        frame["market_prior_p_home"] = np.nan
        rows_2022 = frame.index[frame["season"] == 2022][:50]
        frame.loc[rows_2022, "market_prior_p_home"] = 0.5
        report = tr.walk_forward_report(frame, min_train_seasons=4)

        sub = report["seasons"][2022]["market_prior_subset"]
        assert sub["n"] == 50
        assert sub["gate"]["evaluated"] is False
        assert "publishing stays blocked" in sub["gate"]["note"]
        # A constant-0.5 prior scores ln(2) on any outcome mix.
        assert sub["market_prior"]["log_loss"] == pytest.approx(0.69315, abs=1e-4)
        # Models are ALSO scored on those same 50 rows, never the full season.
        assert sub["logistic_scaled_calibrated"]["n"] == 50

        empty = report["seasons"][2023]["market_prior_subset"]
        assert empty["n"] == 0
        assert "gate" not in empty

    def test_gate_evaluated_with_enough_sample(self):
        frame = self._frame()
        frame["market_prior_p_home"] = np.nan
        frame.loc[frame["season"] == 2023, "market_prior_p_home"] = 0.5
        report = tr.walk_forward_report(frame, min_train_seasons=4)

        sub = report["seasons"][2023]["market_prior_subset"]
        assert sub["n"] >= tr.MIN_GATE_N
        assert sub["gate"]["evaluated"] is True
        # Against a coin-flip prior the learned models must win (same claim
        # the baseline_constant assertion makes on the full season).
        assert sub["gate"]["beaten_by"] == {
            "logistic_scaled": True, "hist_gb": True,
        }

        import json

        json.dumps(report)


def test_markdown_summary_renders_every_model_column():
    """_markdown_summary hardcodes per-model keys (logistic_scaled, hist_gb)
    in the season and prior tables and is otherwise only executed by __main__
    in production: without this test, renaming or dropping a model keeps the
    suite green and kills the Actions run at the finish line (the int32
    json.dumps incident, same shape). Renders BOTH tables."""
    from app.jobs import train_f1

    frame = ds.build_training_frame(_synthetic_games(), "moneyline")
    frame["market_prior_p_home"] = np.nan
    frame.loc[frame["season"] == 2023, "market_prior_p_home"] = 0.5
    report = tr.walk_forward_report(frame, min_train_seasons=4)
    result = {
        "markets": {
            "moneyline": {
                "rows": int(len(frame)), "seasons": [2018, 2023],
                "sp_coverage": 0.0, "offense_coverage": 0.0,
                "lineup_coverage": 0.0, "star_out_coverage": 0.0,
                "bullpen_coverage": 0.0, "rows_with_market_prior": 0,
                "report": report,
            }
        },
        "gate_note": "nota",
    }
    md = train_f1._markdown_summary(result)
    assert "| 2022 |" in md and "| 2023 |" in md  # season rows rendered
    assert "Subconjunto con market prior" in md  # prior table rendered


@pytest.mark.integration
def test_load_market_prior_uses_last_pregame_sharp_pair(seeded):
    from sqlalchemy import text

    db, tables, _ = seeded

    def _snap(conn, book_key, side, price, captured_at):
        conn.execute(
            text(
                """
                INSERT INTO odds_snapshots
                    (event_id, book_id, market, side, price_decimal,
                     price_american, captured_at)
                SELECT e.id, b.id, 'moneyline', :side, :price, -110, :captured_at
                FROM events e, books b
                WHERE e.external_ids ->> 'mlb_game_pk' = '900001' AND b.key = :book
                """
            ),
            {"side": side, "price": price, "captured_at": captured_at, "book": book_key},
        )

    ts = lambda h, m=0: datetime(2026, 7, 5, h, m, tzinfo=timezone.utc)  # noqa: E731
    with db.begin() as conn:
        # Early sharp pair, superseded by a later one.
        _snap(conn, "pinnacle", "home", 1.91, ts(18))
        _snap(conn, "pinnacle", "away", 1.91, ts(18))
        _snap(conn, "pinnacle", "home", 1.85, ts(21))
        _snap(conn, "pinnacle", "away", 2.10, ts(21))
        # Incomplete pair (home only): unusable for devig.
        _snap(conn, "pinnacle", "home", 1.80, ts(22))
        # Soft book pair even later: not the reference (is_sharp = false).
        _snap(conn, "bet365", "home", 1.75, ts(22, 30))
        _snap(conn, "bet365", "away", 2.20, ts(22, 30))

    prior = ds.load_market_prior(db, "moneyline")
    assert len(prior) == 1
    p_home_imp, p_away_imp = 1 / 1.85, 1 / 2.10
    expected = p_home_imp / (p_home_imp + p_away_imp)
    assert prior["market_prior_p_home"].iloc[0] == pytest.approx(expected)

    assert ds.load_market_prior(db, "f5_moneyline").empty


@pytest.mark.integration
def test_train_f1_job_reports_sp_coverage(seeded):
    """End-to-end job path: loads pitching, computes coverage, JSON-safe."""
    import json

    from app.jobs import train_f1

    db, _, _ = seeded
    result = train_f1.run(engine=db)
    json.dumps(result)
    block = result["markets"]["moneyline"]
    # 5 finished games; only the July 6th one has BOTH starters with prior
    # history (SP1's June 5th start has no priors at all).
    assert block["rows"] == 5
    assert block["sp_coverage"] == 0.2
    # Reliever archive comes alive after June 5th: 4 of 5 games covered.
    assert block["bullpen_coverage"] == 0.8
    assert "bullpen_coverage" not in result["markets"]["f5_moneyline"]
    # Offense: e900001/e900002/e900004 have both teams with a real 30d
    # batting window; e900003 predates the archive and e900005's home team
    # (A) never bats in the seeds.
    assert block["offense_coverage"] == 0.6
    assert "batting_note" not in result
    # star_out coverage is 0: the seed has no >=200-PA star and no IL archive,
    # so the flag is honestly None everywhere (present in BOTH markets, never a
    # fabricated 0), and no migration-006 note fires (the table exists, empty).
    assert block["star_out_coverage"] == 0.0
    assert result["markets"]["f5_moneyline"]["star_out_coverage"] == 0.0
    assert "transactions_note" not in result
    # Not enough seasons to test anything: the report stays honest and empty.
    assert block["report"]["seasons"] == {}


@pytest.mark.integration
def test_bulk_features_match_online_builder(seeded):
    """The training dataset and the online builder must produce IDENTICAL
    numbers for the same game — otherwise train/serve skew poisons F1."""
    from datetime import datetime as dt

    from app.features import builder
    from app.ml.dataset import build_training_frame, load_results_frame

    from app.ml.dataset import (
        load_batting_frame,
        load_bullpen_frame,
        load_lineup_frame,
        load_pitching_frame,
        load_transactions_frame,
    )

    db, tables, target_id = seeded
    games = load_results_frame(db)

    # The online builder computed the July 5th game's form for team H; the
    # bulk frame's row for that same game must match field by field. The
    # seeded probables MATCH the actual starters, so the starter block must
    # agree too (probable-based online vs actual-starter bulk, and the
    # offense split's selected hand likewise). The bullpen, offense and
    # lineup blocks (day windows) must agree as well — the seeded
    # event_lineups matches the box-score batting_order, so the per-batter
    # wOBA (strictly prior in both paths) is identical.
    frame = build_training_frame(
        games, "moneyline",
        pitching=load_pitching_frame(db), bullpen=load_bullpen_frame(db),
        batting=load_batting_frame(db), lineup=load_lineup_frame(db),
        transactions=load_transactions_frame(db),
    )
    with db.connect() as conn:
        # as_of == start time (training convention) for the July 6th game,
        # where team H (away side) has exactly one prior game in window.
        event_row = games[games["start_time_utc"] == dt(2026, 7, 6, 23, 0, tzinfo=timezone.utc)]
        assert len(event_row) == 1
        event_id = event_row["event_id"].iloc[0]
        online = builder.build_features(
            conn, tables, event_id, "moneyline",
            dt(2026, 7, 6, 23, 0, tzinfo=timezone.utc),
        )
    bulk = frame[frame["event_id"] == event_id].iloc[0]
    for name in (
        "games_30d", "win_pct_30d", "runs_pg_30d", "runs_allowed_pg_30d",
        "f5_games_30d", "f5_runs_pg_30d", "f5_runs_allowed_pg_30d",
        "rest_days", "games_last_7d",
        *ds.OFFENSE_FEATURE_NAMES,
        # lineup wOBA features must byte-match (per-batter wOBA is strictly
        # prior in both paths); lineup_is_confirmed is checked separately.
        "lineup_woba_proj", "top4_woba_vs_hand",
        # star_out_flag must byte-match too (None==NaN here: the seed has no
        # 200-PA star, so both paths agree it is unknown — the real non-vacuous
        # online==bulk check is test_star_out_flag_online_matches_bulk).
        "star_out_flag",
        *ds.SP_FEATURE_NAMES,
        *ds.BP_FEATURE_NAMES,
    ):
        for side in ("home", "away"):
            online_val = online[side][name]
            bulk_val = bulk[f"{side}_{name}"]
            if online_val is None:
                assert pd.isna(bulk_val), f"{side}_{name}: online None, bulk {bulk_val}"
            else:
                assert bulk_val == pytest.approx(online_val), f"{side}_{name}"

    # lineup_is_confirmed is the ONE intentionally divergent field (docs/04
    # §1.5): online reads the archived pre-game snapshot (1), bulk reconstructs
    # the realized box-score order (0). The flag IS the archive-vs-
    # reconstruction regime marker, so it must NOT match — assert it directly.
    for side in ("home", "away"):
        assert online[side]["lineup_is_confirmed"] == 1, side
        assert bulk[f"{side}_lineup_is_confirmed"] == 0, side
    # And the wOBA features are genuinely present (not a vacuous None==NaN pass).
    assert online["home"]["lineup_woba_proj"] is not None
    assert online["away"]["lineup_woba_proj"] is not None
    assert online["away"]["top4_woba_vs_hand"] is not None


@pytest.mark.integration
def test_star_out_flag_online_matches_bulk(db):
    """A qualifying star on the IL: the online builder (_star_out_block, SQL)
    and the bulk dataset (_lineup_features, pandas) must produce the SAME
    star_out_flag — a REAL non-vacuous value, the parity guard for this feature."""
    from datetime import date
    from datetime import datetime as dt

    from sqlalchemy import text

    from app.features import builder
    from app.ingestion import store
    from app.ingestion.parsers import BattingLine, PlayerTransaction, ScheduledGame
    from app.ml import dataset as ds

    H, A = "Boston Red Sox", "New York Yankees"
    STAR = 850001
    tables = store.reflect_tables(db, builder.FEATURE_TABLES + ("sports", "teams"))
    prior = dt(2026, 6, 1, 23, 0, tzinfo=timezone.utc)
    target = dt(2026, 7, 1, 23, 0, tzinfo=timezone.utc)

    with db.begin() as conn:
        conn.execute(text("TRUNCATE players CASCADE"))
        sport_id = store.get_sport_id(conn, tables)
        gp, _ = store.upsert_event_from_schedule(
            conn, tables, sport_id,
            ScheduledGame(game_pk=850100, start_time=prior, status="final",
                          home_name=A, away_name=H, home_mlb_id=147, away_mlb_id=111,
                          home_probable=None, away_probable=None),
        )
        gt, _ = store.upsert_event_from_schedule(
            conn, tables, sport_id,
            ScheduledGame(game_pk=850101, start_time=target, status="scheduled",
                          home_name=H, away_name=A, home_mlb_id=111, away_mlb_id=147,
                          home_probable=None, away_probable=None),
        )
        cache = store.load_player_cache(conn, tables)
        store.bulk_upsert_players(
            conn, tables, sport_id,
            [{"mlb_person_id": STAR, "full_name": "The Star", "pitch_hand": None}], cache,
        )
        teams = store.load_team_cache(conn, tables, sport_id)
        # STAR bats a big prior game for A (home in gp): wOBA denom >= 200.
        store.bulk_upsert_batting_logs(
            conn, tables, gp, teams[A], teams[H],
            [BattingLine(mlb_person_id=STAR, full_name="The Star", is_home=True,
                         at_bats=250, hits=80, doubles=0, triples=0, home_runs=0,
                         walks=20, intentional_walks=0, strikeouts=0, hit_by_pitch=0,
                         sac_flies=0, sac_bunts=0, batting_order=100,
                         plate_appearances=None)],
            cache,
        )
        team_by_mlb = store.load_team_cache_by_mlb_id(conn, tables, sport_id)
        store.bulk_upsert_transactions(
            conn, tables,
            [PlayerTransaction(
                mlb_transaction_id=1, mlb_person_id=STAR, full_name="The Star",
                from_team_mlb_id=147, to_team_mlb_id=147, type_code="SC",
                type_desc="Status Change",
                description="New York Yankees placed The Star on the 10-day injured list.",
                transaction_date=date(2026, 6, 15))],
            cache, team_by_mlb,
        )

    with db.connect() as conn:
        online = builder.build_features(conn, tables, gt, "moneyline", target)
        # Build the bulk games frame from the SAME driver so team_id types match
        # load_lineup_frame's (both via read_sql).
        games_df = pd.read_sql(
            text("SELECT id AS event_id, home_team_id, away_team_id, start_time_utc "
                 "FROM events WHERE id = :id"),
            conn, params={"id": str(gt)},
        )
    games_df["start_time_utc"] = pd.to_datetime(games_df["start_time_utc"], utc=True)
    feats = ds._lineup_features(
        ds.load_lineup_frame(db), games_df, {}, ds.load_transactions_frame(db)
    )
    bulk_row = feats.iloc[0]

    # A is the AWAY team of the target game; its lone established star is on IL.
    assert online["away"]["star_out_flag"] == 1
    assert bulk_row["away_star_out_flag"] == 1
    assert online["away"]["star_out_flag"] == bulk_row["away_star_out_flag"]
