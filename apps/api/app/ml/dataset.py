"""Bulk as-of dataset builder for training.

Replicates the semantics of ``app/features/builder.py`` (the online per-event
builder) in vectorized form over the whole events/event_results archive:

- rolling 30-day team form STRICTLY BEFORE each game's start time,
- F5 aggregates only over games with F5 partials recorded,
- rest days vs the previous game, 7-day schedule density,
- the starter block (docs/04 §1.3): shrunk K-BB% / xFIP-core over the last
  5 starts and season-to-date, rest days, recent pitch count, handedness.

One deliberate train/serve difference: training rows use the ACTUAL starter
(``pitching_game_logs.is_starter``) because that is who generated the
outcome, while the online builder uses the as-of PROBABLE — the pitcher a
bettor actually knows. The gap (late scratches) is a documented backtest
approximation; ``event_probables`` archives the truth going forward so it
can be measured.

The as-of cutoff for every training row is the game's own start time, and
every aggregate uses ``start_time < cutoff`` (strict), so nothing from the
game itself — or any simultaneous/later game — leaks in. League constants
for the shrinkage are prefix-sums over the same strict cutoff (docs/04 §4
item 9). Parity between this bulk path and the online builder is enforced
by tests/test_ml.py.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

ROLLING_DAYS = 30
RECENT_DAYS = 7

# Starter block knobs — MUST mirror app/features/builder.py exactly, or the
# parity test fails (train/serve skew guard).
SP_WINDOW_DAYS = 365
SP_LAST_STARTS = 5
SP_PITCH_COUNT_STARTS = 2
SP_SHRINK_BF = 60.0
SP_SHRINK_IP = 15.0
SP_MAX_REST_DAYS = 30

SP_FEATURE_NAMES = (
    "sp_kbb_pct_l5_starts",
    "sp_kbb_pct_season",
    "sp_xfip_l5_starts",
    "sp_xfip_season",
    "sp_days_rest",
    "sp_pitch_count_l2_starts",
    "sp_is_lhp",
)

FEATURE_COLUMNS = [
    f"{side}_{name}"
    for side in ("home", "away")
    for name in (
        "games_30d",
        "win_pct_30d",
        "runs_pg_30d",
        "runs_allowed_pg_30d",
        "f5_games_30d",
        "f5_runs_pg_30d",
        "f5_runs_allowed_pg_30d",
        "rest_days",
        "games_last_7d",
        *SP_FEATURE_NAMES,
    )
]

_RESULTS_SQL = """
SELECT e.id AS event_id,
       e.start_time_utc,
       e.home_team_id,
       e.away_team_id,
       er.home_score,
       er.away_score,
       er.f5_home_score,
       er.f5_away_score
FROM events e
JOIN event_results er ON er.event_id = e.id
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport AND e.status = 'final'
ORDER BY e.start_time_utc
"""


def load_results_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """One row per finished game with scores, ordered by start time."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_RESULTS_SQL), conn, params={"sport": sport})
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df


_PITCHING_SQL = """
SELECT l.event_id,
       l.player_id,
       l.is_home,
       e.start_time_utc,
       l.outs_recorded,
       l.batters_faced,
       l.strikeouts,
       l.walks,
       l.hit_batsmen,
       l.home_runs,
       l.fly_outs,
       l.sac_flies,
       l.pitches_thrown,
       p.pitch_hand
FROM pitching_game_logs l
JOIN players p ON p.id = l.player_id
JOIN events e ON e.id = l.event_id
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport AND l.is_starter
ORDER BY e.start_time_utc
"""


def load_pitching_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """One row per STARTER per game (feature windows count starts only)."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_PITCHING_SQL), conn, params={"sport": sport})
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df


_MARKET_PRIOR_SQL = """
WITH paired AS (
    SELECT os.event_id,
           os.book_id,
           os.captured_at,
           max(os.price_decimal) FILTER (WHERE os.side = 'home') AS home_price,
           max(os.price_decimal) FILTER (WHERE os.side = 'away') AS away_price
    FROM odds_snapshots os
    JOIN books b ON b.id = os.book_id
    JOIN events e ON e.id = os.event_id
    JOIN sports sp ON sp.id = e.sport_id
    WHERE b.is_sharp
      AND os.market = :market
      AND sp.key = :sport
      AND os.captured_at <= e.start_time_utc
    GROUP BY 1, 2, 3
)
SELECT DISTINCT ON (event_id) event_id, home_price, away_price
FROM paired
WHERE home_price IS NOT NULL AND away_price IS NOT NULL
ORDER BY event_id, captured_at DESC
"""


def load_market_prior(
    engine: Engine, market: str, sport: str = "mlb"
) -> pd.DataFrame:
    """Devigged sharp-book prior per event: columns event_id, market_prior_p_home.

    Uses the LAST pregame snapshot of the sharp reference book (Pinnacle,
    docs/00 decision #6) where BOTH sides were captured at the same instant.
    Last-before-start rather than opening line because the training vector's
    as-of cutoff is the game start — the fairest market to compare against
    is the one closest to that cutoff (docs/04 §2.1). Rows whose prices sum
    to an impossible sub-1.0 overround are skipped, not repaired.

    Only events archived since F0 started (2026-07-08) can have a prior:
    the frame is expected to be TINY until the archive matures. The gate
    logic in app/ml/train.py refuses to conclude anything below MIN_GATE_N.
    """
    from app.core.devig import no_vig_two_way

    with engine.connect() as conn:
        rows = conn.execute(
            text(_MARKET_PRIOR_SQL), {"market": market, "sport": sport}
        ).all()
    priors = []
    for row in rows:
        try:
            p_home, _, _ = no_vig_two_way(float(row.home_price), float(row.away_price))
        except ValueError:
            continue
        priors.append({"event_id": row.event_id, "market_prior_p_home": p_home})
    return pd.DataFrame(priors, columns=["event_id", "market_prior_p_home"])


def _team_long_frame(games: pd.DataFrame) -> pd.DataFrame:
    """(game, team) rows with scored/allowed from the team's perspective."""
    home = pd.DataFrame(
        {
            "event_id": games["event_id"],
            "team_id": games["home_team_id"],
            "start_time_utc": games["start_time_utc"],
            "scored": games["home_score"],
            "allowed": games["away_score"],
            "f5_scored": games["f5_home_score"],
            "f5_allowed": games["f5_away_score"],
        }
    )
    away = pd.DataFrame(
        {
            "event_id": games["event_id"],
            "team_id": games["away_team_id"],
            "start_time_utc": games["start_time_utc"],
            "scored": games["away_score"],
            "allowed": games["home_score"],
            "f5_scored": games["f5_away_score"],
            "f5_allowed": games["f5_home_score"],
        }
    )
    return pd.concat([home, away], ignore_index=True)


def _team_form_features(team_games: pd.DataFrame) -> pd.DataFrame:
    """As-of form for every game of ONE team (rows sorted by start time).

    For each row i, aggregates run over rows j with start[j] < start[i] and
    start[j] >= start[i] - 30d — identical to the online builder called with
    as_of_ts = the game's start time.
    """
    tg = team_games.sort_values("start_time_utc").reset_index(drop=True)
    # tz-aware -> UTC-naive datetime64[ns]; tz-aware Series would surface as
    # object-dtype Timestamps and break searchsorted/astype below.
    starts = (
        tg["start_time_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
        .to_numpy(dtype="datetime64[ns]")
    )
    scored = tg["scored"].to_numpy(dtype=float)
    allowed = tg["allowed"].to_numpy(dtype=float)
    won = (scored > allowed).astype(float)
    f5_ok = tg["f5_scored"].notna().to_numpy() & tg["f5_allowed"].notna().to_numpy()
    f5_scored = tg["f5_scored"].to_numpy(dtype=float)
    f5_allowed = tg["f5_allowed"].to_numpy(dtype=float)

    out = {name: np.full(len(tg), np.nan) for name in (
        "games_30d", "win_pct_30d", "runs_pg_30d", "runs_allowed_pg_30d",
        "f5_games_30d", "f5_runs_pg_30d", "f5_runs_allowed_pg_30d",
        "rest_days", "games_last_7d",
    )}

    for i in range(len(tg)):
        cutoff = starts[i]
        # Strict '<' via searchsorted on the left side of the cutoff.
        hi = int(np.searchsorted(starts, cutoff, side="left"))
        lo30 = int(np.searchsorted(starts, cutoff - np.timedelta64(ROLLING_DAYS, "D"), side="left"))
        lo7 = int(np.searchsorted(starts, cutoff - np.timedelta64(RECENT_DAYS, "D"), side="left"))
        out["games_last_7d"][i] = hi - lo7
        if hi > 0:
            prev_day = starts[hi - 1].astype("datetime64[D]")
            out["rest_days"][i] = (cutoff.astype("datetime64[D]") - prev_day).astype(int)
        n = hi - lo30
        out["games_30d"][i] = n
        if n > 0:
            window = slice(lo30, hi)
            out["win_pct_30d"][i] = round(won[window].mean(), 4)
            out["runs_pg_30d"][i] = round(scored[window].mean(), 4)
            out["runs_allowed_pg_30d"][i] = round(allowed[window].mean(), 4)
            mask = f5_ok[lo30:hi]
            f5_n = int(mask.sum())
            out["f5_games_30d"][i] = f5_n
            if f5_n > 0:
                out["f5_runs_pg_30d"][i] = round(f5_scored[window][mask].mean(), 4)
                out["f5_runs_allowed_pg_30d"][i] = round(f5_allowed[window][mask].mean(), 4)
        else:
            out["f5_games_30d"][i] = 0.0

    features = pd.DataFrame(out)
    features["event_id"] = tg["event_id"].to_numpy()
    features["team_id"] = tg["team_id"].to_numpy()
    return features


def _starter_features(pitching: pd.DataFrame) -> pd.DataFrame:
    """As-of starter block for every starter row (docs/04 §1.3, bulk twin
    of builder._starter_block + builder._league_pitching).

    For starter row i (pitcher P starting game G), every window runs over
    P's starts strictly before G within the trailing year; the shrinkage
    league constants are prefix-sums over ALL starter rows strictly before
    G's start in the same trailing year.
    """
    pt = pitching.sort_values("start_time_utc").reset_index(drop=True)
    starts = (
        pt["start_time_utc"].dt.tz_convert("UTC").dt.tz_localize(None)
        .to_numpy(dtype="datetime64[ns]")
    )
    k = pt["strikeouts"].to_numpy(dtype=float)
    bb = pt["walks"].to_numpy(dtype=float)
    hbp = pt["hit_batsmen"].to_numpy(dtype=float)
    bf = pt["batters_faced"].to_numpy(dtype=float)
    hr = pt["home_runs"].to_numpy(dtype=float)
    outs = pt["outs_recorded"].to_numpy(dtype=float)
    fb = (
        pt["fly_outs"].fillna(0).to_numpy(dtype=float)
        + pt["sac_flies"].fillna(0).to_numpy(dtype=float)
        + hr
    )
    pitches = pt["pitches_thrown"].to_numpy(dtype=float)  # NaN where NULL

    def _cum(a: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(a)])

    cum_k, cum_bb, cum_hbp = _cum(k), _cum(bb), _cum(hbp)
    cum_bf, cum_hr, cum_fb, cum_outs = _cum(bf), _cum(hr), _cum(fb), _cum(outs)
    window_days = np.timedelta64(SP_WINDOW_DAYS, "D")

    def _league_at(cutoff: np.datetime64) -> tuple[float, float, float] | None:
        hi = int(np.searchsorted(starts, cutoff, side="left"))
        lo = int(np.searchsorted(starts, cutoff - window_days, side="left"))
        s_bf, s_outs = cum_bf[hi] - cum_bf[lo], cum_outs[hi] - cum_outs[lo]
        s_fb = cum_fb[hi] - cum_fb[lo]
        if s_bf == 0 or s_outs == 0 or s_fb == 0:
            return None
        s_k, s_bb, s_hbp = (
            cum_k[hi] - cum_k[lo], cum_bb[hi] - cum_bb[lo], cum_hbp[hi] - cum_hbp[lo]
        )
        s_hr = cum_hr[hi] - cum_hr[lo]
        return (
            (s_k - s_bb) / s_bf,
            s_hr / s_fb,
            (13.0 * s_hr + 3.0 * (s_bb + s_hbp) - 2.0 * s_k) / (s_outs / 3.0),
        )

    out = {name: np.full(len(pt), np.nan) for name in SP_FEATURE_NAMES}
    hand = pt["pitch_hand"].to_numpy(dtype=object)
    out["sp_is_lhp"] = np.where(
        hand == "L", 1.0, np.where(hand == "R", 0.0, np.nan)
    )

    for _, group in pt.groupby("player_id", sort=False):
        idx = group.index.to_numpy()
        p_starts = starts[idx]
        for j, i in enumerate(idx):
            cutoff = p_starts[j]
            hi = int(np.searchsorted(p_starts, cutoff, side="left"))
            lo365 = int(np.searchsorted(p_starts, cutoff - window_days, side="left"))
            if hi > 0:
                rest = int(
                    (cutoff.astype("datetime64[D]")
                     - p_starts[hi - 1].astype("datetime64[D]")).astype(int)
                )
                if rest <= SP_MAX_REST_DAYS:
                    out["sp_days_rest"][i] = rest
            w2 = idx[max(lo365, hi - SP_PITCH_COUNT_STARTS):hi]
            if len(w2) and not np.isnan(pitches[w2]).all():
                out["sp_pitch_count_l2_starts"][i] = np.nansum(pitches[w2])
            league = _league_at(cutoff)
            if league is None or hi == lo365:
                continue
            lg_kbb, lg_hrfb, lg_xfip_core = league

            def _rates(window: np.ndarray) -> tuple[float, float]:
                s_k, s_bb, s_bf = k[window].sum(), bb[window].sum(), bf[window].sum()
                s_bb_hbp = s_bb + hbp[window].sum()
                s_fb, s_ip = fb[window].sum(), outs[window].sum() / 3.0
                kbb = (s_k - s_bb + SP_SHRINK_BF * lg_kbb) / (s_bf + SP_SHRINK_BF)
                core = 13.0 * s_fb * lg_hrfb + 3.0 * s_bb_hbp - 2.0 * s_k
                xfip = (core + SP_SHRINK_IP * lg_xfip_core) / (s_ip + SP_SHRINK_IP)
                return round(kbb, 4), round(xfip, 4)

            w5 = idx[max(lo365, hi - SP_LAST_STARTS):hi]
            out["sp_kbb_pct_l5_starts"][i], out["sp_xfip_l5_starts"][i] = _rates(w5)
            year = cutoff.astype("datetime64[Y]").astype(int) + 1970
            soy = int(np.searchsorted(p_starts, np.datetime64(f"{year}-01-01"), side="left"))
            ws = idx[max(soy, lo365):hi]
            if len(ws):
                out["sp_kbb_pct_season"][i], out["sp_xfip_season"][i] = _rates(ws)

    features = pd.DataFrame(out)
    features["event_id"] = pt["event_id"].to_numpy()
    features["is_home"] = pt["is_home"].to_numpy()
    return features


def _merge_starter_features(
    frame: pd.DataFrame, pitching: pd.DataFrame | None
) -> pd.DataFrame:
    """Attach home_sp_*/away_sp_* columns; NaN block when no data exists."""
    if pitching is None or len(pitching) == 0:
        for side in ("home", "away"):
            for name in SP_FEATURE_NAMES:
                frame[f"{side}_{name}"] = np.nan
        return frame
    sp = _starter_features(pitching)
    # One starter per (event, side) by construction; a duplicate would mean
    # corrupted logs and silently doubled rows after the merge.
    sp = sp.drop_duplicates(subset=["event_id", "is_home"], keep="first")
    for side, flag in (("home", True), ("away", False)):
        side_sp = (
            sp[sp["is_home"] == flag]
            .drop(columns=["is_home"])
            .set_index("event_id")
            .add_prefix(f"{side}_")
        )
        frame = frame.merge(side_sp, how="left", left_on="event_id", right_index=True)
    return frame


def build_training_frame(
    games: pd.DataFrame, market: str, pitching: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Game rows with home_/away_ features, target and season.

    market='moneyline': target = home team won the game (no ties in finals;
    defensive drop if scores are equal).
    market='f5_moneyline': target = home led after 5 innings; F5 ties (push)
    and games without F5 partials are dropped — a push is not a loss and
    teaching the model otherwise would poison it.

    ``pitching`` is ``load_pitching_frame``'s output; without it the sp_*
    columns are NaN (the models impute, the report says so — never silently
    fabricated).
    """
    if market not in ("moneyline", "f5_moneyline"):
        raise ValueError(f"unknown market {market!r}")

    long_frame = _team_long_frame(games)
    form = pd.concat(
        [_team_form_features(g) for _, g in long_frame.groupby("team_id", sort=False)],
        ignore_index=True,
    )

    frame = games.merge(
        form.add_prefix("home_"),
        left_on=["event_id", "home_team_id"],
        right_on=["home_event_id", "home_team_id"],
    ).merge(
        form.add_prefix("away_"),
        left_on=["event_id", "away_team_id"],
        right_on=["away_event_id", "away_team_id"],
    )
    frame = _merge_starter_features(frame, pitching)

    if market == "moneyline":
        frame = frame[frame["home_score"] != frame["away_score"]].copy()
        frame["target"] = (frame["home_score"] > frame["away_score"]).astype(int)
    else:
        frame = frame[
            frame["f5_home_score"].notna()
            & frame["f5_away_score"].notna()
            & (frame["f5_home_score"] != frame["f5_away_score"])
        ].copy()
        frame["target"] = (frame["f5_home_score"] > frame["f5_away_score"]).astype(int)

    frame["season"] = frame["start_time_utc"].dt.year
    return frame[
        ["event_id", "start_time_utc", "season", "target", *FEATURE_COLUMNS]
    ].sort_values("start_time_utc").reset_index(drop=True)
