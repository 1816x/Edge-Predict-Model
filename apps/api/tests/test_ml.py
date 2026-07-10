"""F1 pipeline: metric sanity, synthetic walk-forward, bulk-vs-online parity."""

from datetime import datetime, timedelta, timezone

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

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
            assert rep["logistic"]["calibrated"]["log_loss"] < rep["baseline_constant"]["log_loss"]

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
        assert len(ds.FEATURE_COLUMNS) == 40
        assert len(ds.F5_FEATURE_COLUMNS) == 32
        assert ds.feature_columns("f5_moneyline") == ds.F5_FEATURE_COLUMNS


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
        assert sub["logistic_calibrated"]["n"] == 50

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
        assert sub["gate"]["beaten_by"] == {"logistic": True, "hist_gb": True}

        import json

        json.dumps(report)


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
    # Not enough seasons to test anything: the report stays honest and empty.
    assert block["report"]["seasons"] == {}


@pytest.mark.integration
def test_bulk_features_match_online_builder(seeded):
    """The training dataset and the online builder must produce IDENTICAL
    numbers for the same game — otherwise train/serve skew poisons F1."""
    from datetime import datetime as dt

    from app.features import builder
    from app.ml.dataset import build_training_frame, load_results_frame

    from app.ml.dataset import load_bullpen_frame, load_pitching_frame

    db, tables, target_id = seeded
    games = load_results_frame(db)

    # The online builder computed the July 5th game's form for team H; the
    # bulk frame's row for that same game must match field by field. The
    # seeded probables MATCH the actual starters, so the starter block must
    # agree too (probable-based online vs actual-starter bulk). The bullpen
    # block (day windows) must agree as well.
    frame = build_training_frame(
        games, "moneyline",
        pitching=load_pitching_frame(db), bullpen=load_bullpen_frame(db),
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
