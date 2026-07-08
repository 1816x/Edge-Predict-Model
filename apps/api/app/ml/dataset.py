"""Bulk as-of dataset builder for training.

Replicates the semantics of ``app/features/builder.py`` (the online per-event
builder) in vectorized form over the whole events/event_results archive:

- rolling 30-day team form STRICTLY BEFORE each game's start time,
- F5 aggregates only over games with F5 partials recorded,
- rest days vs the previous game, 7-day schedule density.

The as-of cutoff for every training row is the game's own start time, and
every aggregate uses ``start_time < cutoff`` (strict), so nothing from the
game itself — or any simultaneous/later game — leaks in. Parity between this
bulk path and the online builder is enforced by tests/test_ml.py.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

ROLLING_DAYS = 30
RECENT_DAYS = 7

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


def build_training_frame(games: pd.DataFrame, market: str) -> pd.DataFrame:
    """Game rows with home_/away_ features, target and season.

    market='moneyline': target = home team won the game (no ties in finals;
    defensive drop if scores are equal).
    market='f5_moneyline': target = home led after 5 innings; F5 ties (push)
    and games without F5 partials are dropped — a push is not a loss and
    teaching the model otherwise would poison it.
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
