"""Bulk as-of dataset builder for training.

Replicates the semantics of ``app/features/builder.py`` (the online per-event
builder) in vectorized form over the whole events/event_results archive:

- rolling 30-day team form STRICTLY BEFORE each game's start time,
- F5 aggregates only over games with F5 partials recorded,
- rest days vs the previous game, 7-day schedule density,
- the starter block (docs/04 §1.3): shrunk K-BB% / xFIP-core over the last
  5 starts and season-to-date, rest days, recent pitch count, handedness,
- the team offense block (docs/04 §1.2): wOBA/OPS/ISO/K%/BB% over UTC-day
  windows ending yesterday plus the vs-opposing-hand shrunk wOBA split
  (formulas shared with the online builder via app/features/offense.py).

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

from app.features.lineup import (
    LINEUP_BATTER_WINDOW_DAYS,
    LINEUP_FEATURE_NAMES,
    batter_woba_asof,
    batter_woba_vs_hand_asof,
    weighted_lineup_woba,
    weighted_top4_woba,
)
from app.features.offense import (
    OFFENSE_FEATURE_NAMES,
    OFFENSE_ROLLING_DAYS,
    OFFENSE_SPLIT_TARGET_DAYS,
    SUM_KEYS,
    offense_rates,
    shrunk_split,
    woba,
    woba_parts,
)
from app.features.transactions import il_effect, il_out_asof, top_k_star_players

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

# Bullpen block (docs/04 §1.4) — MONEYLINE ONLY, day-based windows ending
# YESTERDAY (intraday-safe rule of §1.1: same-day games excluded wholesale).
# Knobs MUST mirror app/features/builder.py exactly (parity guard).
BULLPEN_FATIGUE_DAYS = 3
BULLPEN_QUALITY_DAYS = 30
BULLPEN_LEAGUE_DAYS = 365
BULLPEN_B2B_MIN_OUTS = 3

BP_FEATURE_NAMES = (
    "bullpen_ip_l3d",
    "bullpen_b2b_flag",
    "bullpen_xfip_30d",
    "bullpen_ip_expected",
)

_TEAM_FORM_NAMES = (
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

# Moneyline vector: everything. F5 excludes the bullpen block BY DESIGN —
# leverage relievers do not participate in innings 1-5, so the features
# are removed rather than zero-weighted (docs/04 §1.4). The offense (§1.2)
# and lineup (§1.5) blocks enter BOTH markets; F5 leans on the lineup's
# top4_woba_vs_hand (first turn of the order, §1.9).
FEATURE_COLUMNS = [
    f"{side}_{name}"
    for side in ("home", "away")
    for name in (
        *_TEAM_FORM_NAMES, *OFFENSE_FEATURE_NAMES, *LINEUP_FEATURE_NAMES,
        *SP_FEATURE_NAMES, *BP_FEATURE_NAMES
    )
]
F5_FEATURE_COLUMNS = [
    f"{side}_{name}"
    for side in ("home", "away")
    for name in (
        *_TEAM_FORM_NAMES, *OFFENSE_FEATURE_NAMES, *LINEUP_FEATURE_NAMES, *SP_FEATURE_NAMES
    )
]


def feature_columns(market: str) -> list[str]:
    """The feature vector for one market (bullpen enters moneyline only)."""
    return FEATURE_COLUMNS if market == "moneyline" else F5_FEATURE_COLUMNS

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


_BULLPEN_SQL = """
SELECT l.team_id,
       e.start_time_utc,
       l.outs_recorded,
       l.strikeouts,
       l.walks,
       l.hit_batsmen,
       l.home_runs,
       l.fly_outs,
       l.sac_flies
FROM pitching_game_logs l
JOIN events e ON e.id = l.event_id
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport AND NOT l.is_starter
ORDER BY e.start_time_utc
"""


def load_bullpen_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """One row per RELIEVER line per game (bullpen block, docs/04 §1.4)."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_BULLPEN_SQL), conn, params={"sport": sport})
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df


_BATTING_SQL = """
SELECT b.event_id,
       b.team_id,
       b.is_home,
       e.start_time_utc,
       sum(b.at_bats) AS at_bats,
       sum(b.hits) AS hits,
       sum(b.doubles) AS doubles,
       sum(b.triples) AS triples,
       sum(b.home_runs) AS home_runs,
       sum(b.walks) AS walks,
       sum(b.intentional_walks) AS intentional_walks,
       sum(b.strikeouts) AS strikeouts,
       sum(b.hit_by_pitch) AS hit_by_pitch,
       sum(b.sac_flies) AS sac_flies,
       sum(b.sac_bunts) AS sac_bunts,
       max(ph.pitch_hand) AS opp_starter_hand
FROM batting_game_logs b
JOIN events e ON e.id = b.event_id
JOIN sports s ON s.id = e.sport_id
LEFT JOIN (
    SELECT l.event_id, l.is_home, p.pitch_hand
    FROM pitching_game_logs l
    JOIN players p ON p.id = l.player_id
    WHERE l.is_starter
) ph ON ph.event_id = b.event_id AND ph.is_home <> b.is_home
WHERE s.key = :sport
GROUP BY b.event_id, b.team_id, b.is_home, e.start_time_utc
ORDER BY e.start_time_utc
"""


def load_batting_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """One row per (game, team): aggregated batting sums plus the hand of
    the ACTUAL opposing starter (the split-classification proxy, docs/04
    §1.2 — NULL when that side has no starter row or an unknown hand)."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_BATTING_SQL), conn, params={"sport": sport})
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df


# Per-PLAYER batting rows (NOT aggregated to team): the lineup block (§1.5)
# needs each batter's own as-of wOBA and the realized batting_order. Same
# opposing-starter-hand proxy as _BATTING_SQL. (event_id, player_id) is the
# PK, so grouping by it plus the passthrough columns collapses the LEFT JOIN
# to one opposing-starter hand per row without touching the sums.
_LINEUP_SQL = """
SELECT b.event_id,
       b.team_id,
       b.is_home,
       b.player_id,
       b.batting_order,
       e.start_time_utc,
       sum(b.at_bats) AS at_bats,
       sum(b.hits) AS hits,
       sum(b.doubles) AS doubles,
       sum(b.triples) AS triples,
       sum(b.home_runs) AS home_runs,
       sum(b.walks) AS walks,
       sum(b.intentional_walks) AS intentional_walks,
       sum(b.strikeouts) AS strikeouts,
       sum(b.hit_by_pitch) AS hit_by_pitch,
       sum(b.sac_flies) AS sac_flies,
       sum(b.sac_bunts) AS sac_bunts,
       max(ph.pitch_hand) AS opp_starter_hand
FROM batting_game_logs b
JOIN events e ON e.id = b.event_id
JOIN sports s ON s.id = e.sport_id
LEFT JOIN (
    SELECT l.event_id, l.is_home, p.pitch_hand
    FROM pitching_game_logs l
    JOIN players p ON p.id = l.player_id
    WHERE l.is_starter
) ph ON ph.event_id = b.event_id AND ph.is_home <> b.is_home
WHERE s.key = :sport
GROUP BY b.event_id, b.team_id, b.is_home, b.player_id, b.batting_order, e.start_time_utc
ORDER BY e.start_time_utc
"""


def load_lineup_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """One row per (game, batter): per-player batting sums, batting_order and
    the ACTUAL opposing starter's hand (docs/04 §1.5). batting_order rides
    the realized box score — the backtest reconstruction of the lineup that
    actually played (is_confirmed=false), never a pre-game archived order."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_LINEUP_SQL), conn, params={"sport": sport})
    df["start_time_utc"] = pd.to_datetime(df["start_time_utc"], utc=True)
    return df


# Raw IL/transactions moves per player (star_out_flag, §1.5). Classification is
# NOT done in SQL: the raw text rides through so app.features.transactions owns
# the versioned taxonomy (identical to the online builder's Python-side call).
_TRANSACTIONS_SQL = """
SELECT t.player_id,
       t.type_code,
       t.type_desc,
       t.description,
       t.transaction_date,
       t.mlb_transaction_id
FROM player_transactions t
JOIN players p ON p.id = t.player_id
JOIN sports s ON s.id = p.sport_id
WHERE s.key = :sport
ORDER BY t.transaction_date
"""


def load_transactions_frame(engine: Engine, sport: str = "mlb") -> pd.DataFrame:
    """Raw player transactions for the IL replay (star_out_flag, §1.5)."""
    with engine.connect() as conn:
        df = pd.read_sql(text(_TRANSACTIONS_SQL), conn, params={"sport": sport})
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

    # bullpen_ip_expected (§1.4) rides here: mean IP per start of the game's
    # starter, derived from the exact rows this loop already walks.
    out = {
        name: np.full(len(pt), np.nan)
        for name in (*SP_FEATURE_NAMES, "bullpen_ip_expected")
    }
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
            if hi > lo365:
                w365 = idx[lo365:hi]
                out["bullpen_ip_expected"][i] = round(
                    float(outs[w365].mean()) / 3.0, 4
                )
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


def _utc_epoch_days(series: pd.Series) -> np.ndarray:
    """tz-aware timestamps -> integer UTC calendar days (for day windows)."""
    return (
        series.dt.tz_convert("UTC").dt.tz_localize(None)
        .to_numpy(dtype="datetime64[ns]")
        .astype("datetime64[D]")
        .astype(int)
    )


def _bullpen_features(bullpen: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """Per-game bullpen fatigue/quality (bulk twin of builder._bullpen_block).

    Windows are UTC calendar days ending YESTERDAY relative to each game's
    day (intraday-safe rule). ip_l3d/b2b are TRUE ZEROS when the team's
    relievers did not pitch in the window — but only while the reliever
    archive is alive at that date (a valid as-of league): games before the
    first archived reliever line stay NaN, or a partial archive would
    fabricate "fully rested" bullpens season-wide. The quality rate stays
    NaN without sample. Mirrors builder._bullpen_block exactly.
    """
    bp = bullpen.sort_values("start_time_utc").reset_index(drop=True)
    days = _utc_epoch_days(bp["start_time_utc"])
    outs = bp["outs_recorded"].to_numpy(dtype=float)
    k = bp["strikeouts"].to_numpy(dtype=float)
    bb = bp["walks"].to_numpy(dtype=float)
    hbp = bp["hit_batsmen"].to_numpy(dtype=float)
    fb = (
        bp["fly_outs"].fillna(0).to_numpy(dtype=float)
        + bp["sac_flies"].fillna(0).to_numpy(dtype=float)
        + bp["home_runs"].to_numpy(dtype=float)
    )

    def _cum(a: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(a)])

    lg = {"k": _cum(k), "bb": _cum(bb), "hbp": _cum(hbp), "fb": _cum(fb),
          "outs": _cum(outs), "hr": _cum(bp["home_runs"].to_numpy(dtype=float))}

    def _league_at(day: int) -> tuple[float, float] | None:
        lo = int(np.searchsorted(days, day - BULLPEN_LEAGUE_DAYS, side="left"))
        hi = int(np.searchsorted(days, day, side="left"))
        s_outs, s_fb = lg["outs"][hi] - lg["outs"][lo], lg["fb"][hi] - lg["fb"][lo]
        if s_outs == 0 or s_fb == 0:
            return None
        s_hr = lg["hr"][hi] - lg["hr"][lo]
        s_bb_hbp = (lg["bb"][hi] - lg["bb"][lo]) + (lg["hbp"][hi] - lg["hbp"][lo])
        s_k = lg["k"][hi] - lg["k"][lo]
        return (
            s_hr / s_fb,
            (13.0 * s_hr + 3.0 * s_bb_hbp - 2.0 * s_k) / (s_outs / 3.0),
        )

    teams: dict = {}
    for team_id, group in bp.groupby("team_id", sort=False):
        pos = group.index.to_numpy()
        teams[team_id] = {
            "days": days[pos],
            "outs": _cum(outs[pos]),
            "k": _cum(k[pos]),
            "bb_hbp": _cum(bb[pos] + hbp[pos]),
            "fb": _cum(fb[pos]),
        }

    game_days = _utc_epoch_days(games["start_time_utc"])
    out = {
        f"{side}_{name}": np.full(len(games), np.nan)
        for side in ("home", "away")
        for name in ("bullpen_ip_l3d", "bullpen_b2b_flag", "bullpen_xfip_30d")
    }
    team_cols = {
        "home": games["home_team_id"].to_numpy(dtype=object),
        "away": games["away_team_id"].to_numpy(dtype=object),
    }
    for i in range(len(games)):
        day = int(game_days[i])
        league = _league_at(day)
        if league is None:
            continue  # archive not alive yet at this date: NaN, not zeros
        for side in ("home", "away"):
            t = teams.get(team_cols[side][i])
            if t is None:
                out[f"{side}_bullpen_ip_l3d"][i] = 0.0
                out[f"{side}_bullpen_b2b_flag"][i] = 0.0
                continue
            hi = int(np.searchsorted(t["days"], day, side="left"))
            lo3 = int(np.searchsorted(t["days"], day - BULLPEN_FATIGUE_DAYS, side="left"))
            loy = int(np.searchsorted(t["days"], day - 1, side="left"))
            lo30 = int(np.searchsorted(t["days"], day - BULLPEN_QUALITY_DAYS, side="left"))
            out[f"{side}_bullpen_ip_l3d"][i] = round(
                (t["outs"][hi] - t["outs"][lo3]) / 3.0, 4
            )
            out[f"{side}_bullpen_b2b_flag"][i] = float(
                (t["outs"][hi] - t["outs"][loy]) >= BULLPEN_B2B_MIN_OUTS
            )
            if hi > lo30 and league is not None:
                lg_hrfb, lg_core = league
                s_k = t["k"][hi] - t["k"][lo30]
                s_bb_hbp = t["bb_hbp"][hi] - t["bb_hbp"][lo30]
                s_fb = t["fb"][hi] - t["fb"][lo30]
                s_ip = (t["outs"][hi] - t["outs"][lo30]) / 3.0
                core = 13.0 * s_fb * lg_hrfb + 3.0 * s_bb_hbp - 2.0 * s_k
                out[f"{side}_bullpen_xfip_30d"][i] = round(
                    (core + SP_SHRINK_IP * lg_core) / (s_ip + SP_SHRINK_IP), 4
                )

    features = pd.DataFrame(out)
    features["event_id"] = games["event_id"].to_numpy()
    return features


def _opp_hand_map(pitching: pd.DataFrame | None) -> dict:
    """(event_id, side) -> 'L'/'R' hand of the side's OPPOSING actual
    starter, from the starter rows (training convention: the actual
    starter, like the sp block — the online builder uses the probable)."""
    if pitching is None or len(pitching) == 0:
        return {}
    hands: dict = {}
    for row in pitching.itertuples():
        if row.pitch_hand in ("L", "R"):
            # The starter on is_home answers the OTHER side's split.
            hands[(row.event_id, "away" if row.is_home else "home")] = row.pitch_hand
    return hands


def _offense_features(
    batting: pd.DataFrame, games: pd.DataFrame, opp_hand: dict
) -> pd.DataFrame:
    """Per-game offense block (bulk twin of builder._offense_block).

    Windows are UTC calendar days ending YESTERDAY relative to each game's
    day (intraday-safe rule, §1.1): 30d rates, season-to-date wOBA, and a
    vs-opposing-hand wOBA split shrunk toward the trailing-year same-hand
    split. Window sums come from per-team cumulative arrays (overall and
    per opposing hand) and feed the SAME shared formulas the online
    builder uses (app/features/offense.py) — window parity is what the
    online/bulk test checks; formula parity holds by construction.
    Teams with no archived batting stay NaN — never fabricated.
    """
    bt = batting.sort_values("start_time_utc").reset_index(drop=True)
    days = _utc_epoch_days(bt["start_time_utc"])
    raw = {k: bt[k].to_numpy(dtype=float) for k in SUM_KEYS}
    hand_col = bt["opp_starter_hand"].to_numpy(dtype=object)

    def _cum(a: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(a)])

    teams: dict = {}
    for team_id, group in bt.groupby("team_id", sort=False):
        pos = group.index.to_numpy()
        entry: dict = {"days": days[pos]}
        for key in SUM_KEYS:
            entry[key] = _cum(raw[key][pos])
            for hand in ("L", "R"):
                mask = (hand_col[pos] == hand).astype(float)
                entry[f"{hand}_{key}"] = _cum(raw[key][pos] * mask)
        teams[team_id] = entry

    game_days = _utc_epoch_days(games["start_time_utc"])
    game_years = (
        games["start_time_utc"].dt.tz_convert("UTC").dt.year.to_numpy(dtype=int)
    )
    event_ids = games["event_id"].to_numpy(dtype=object)
    team_cols = {
        "home": games["home_team_id"].to_numpy(dtype=object),
        "away": games["away_team_id"].to_numpy(dtype=object),
    }
    out = {
        f"{side}_{name}": np.full(len(games), np.nan)
        for side in ("home", "away")
        for name in OFFENSE_FEATURE_NAMES
    }

    def _window_sums(entry: dict, lo: int, hi: int, prefix: str = "") -> dict:
        return {
            key: float(entry[f"{prefix}{key}"][hi] - entry[f"{prefix}{key}"][lo])
            for key in SUM_KEYS
        }

    for i in range(len(games)):
        day = int(game_days[i])
        jan1 = int(
            np.datetime64(f"{game_years[i]}-01-01").astype("datetime64[D]").astype(int)
        )
        for side in ("home", "away"):
            entry = teams.get(team_cols[side][i])
            if entry is None:
                continue  # no archived batting for this team: NaN block
            d = entry["days"]
            hi = int(np.searchsorted(d, day, side="left"))
            lo30 = int(np.searchsorted(d, day - OFFENSE_ROLLING_DAYS, side="left"))
            lo365 = int(
                np.searchsorted(d, day - OFFENSE_SPLIT_TARGET_DAYS, side="left")
            )
            if hi == lo365:
                continue  # nothing in the trailing year: None, like online
            los = int(np.searchsorted(d, jan1, side="left"))
            for name, value in offense_rates(_window_sums(entry, lo30, hi)).items():
                if value is not None:
                    out[f"{side}_{name}"][i] = value
            if hi > los:
                season_woba = woba(_window_sums(entry, los, hi))
                if season_woba is not None:
                    out[f"{side}_team_woba_season"][i] = season_woba
            hand = opp_hand.get((event_ids[i], side))
            if hand in ("L", "R"):
                num_30, den_30 = woba_parts(
                    _window_sums(entry, lo30, hi, prefix=f"{hand}_")
                )
                num_365, den_365 = woba_parts(
                    _window_sums(entry, lo365, hi, prefix=f"{hand}_")
                )
                split = shrunk_split(num_30, den_30, num_365, den_365)
                if split is not None:
                    out[f"{side}_team_woba_vs_opp_hand_30d"][i] = split

    features = pd.DataFrame(out)
    features["event_id"] = games["event_id"].to_numpy()
    return features


def _merge_offense_features(
    frame: pd.DataFrame,
    batting: pd.DataFrame | None,
    games: pd.DataFrame,
    opp_hand: dict,
) -> pd.DataFrame:
    """Attach the offense columns; NaN block when no batting was loaded."""
    if batting is None or len(batting) == 0:
        for side in ("home", "away"):
            for name in OFFENSE_FEATURE_NAMES:
                frame[f"{side}_{name}"] = np.nan
        return frame
    return frame.merge(
        _offense_features(batting, games, opp_hand), on="event_id", how="left"
    )


def _epoch_day(d) -> int:
    """A python date -> integer UTC calendar days since epoch (matches
    _utc_epoch_days), so transaction dates compare to game days on one basis."""
    return (pd.Timestamp(d) - pd.Timestamp("1970-01-01")).days


def _lineup_features(
    lineup: pd.DataFrame,
    games: pd.DataFrame,
    opp_hand: dict,
    transactions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per-game lineup block (bulk twin of builder._lineup_block).

    Composition (who bats in which slot) is the REALIZED box-score order
    (batting_order % 100 == 0): no pre-pipeline lineup snapshots exist, so
    lineup_is_confirmed is 0 for every training row — a documented optimistic
    bias (§1.5, like the probable-vs-actual gap of §1.3). The wOBA VALUES are
    strictly prior: each batter's 365d window ends yesterday (day < game day),
    keyed by (player, team) to mirror the online builder's team_id filter,
    feeding the SAME shared formulas (app/features/lineup.py). A slot whose
    batter has no trailing-year line drops from the PA-share weighting.
    """
    lt = lineup.sort_values("start_time_utc").reset_index(drop=True)
    days = _utc_epoch_days(lt["start_time_utc"])
    raw = {k: lt[k].to_numpy(dtype=float) for k in SUM_KEYS}
    hand_col = lt["opp_starter_hand"].to_numpy(dtype=object)

    def _cum(a: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(a)])

    # Per (player, team) cumulative arrays: a traded batter's games with his
    # other team must not enter this team's window (the online builder filters
    # batting.team_id == team_id).
    players: dict = {}
    for (pid, tid), group in lt.groupby(["player_id", "team_id"], sort=False):
        pos = group.index.to_numpy()
        entry: dict = {"days": days[pos]}
        for key in SUM_KEYS:
            entry[key] = _cum(raw[key][pos])
            for hand in ("L", "R"):
                mask = (hand_col[pos] == hand).astype(float)
                entry[f"{hand}_{key}"] = _cum(raw[key][pos] * mask)
        players[(pid, tid)] = entry

    # star_out_flag support (§1.5): the team's full batter pool (to rank the
    # top-2) and the per-player IL move history. Independent of the lineup
    # composition, so it is computed for every side BELOW even when no realized
    # lineup exists — mirroring the online builder, which runs _star_out_block
    # before its own no-snapshot early returns.
    team_to_players: dict = {}
    for (pid, tid), entry in players.items():
        team_to_players.setdefault(tid, []).append((pid, entry))
    moves_by_player: dict = {}
    min_txn_day: int | None = None
    if transactions is not None and len(transactions):
        for row in transactions.itertuples(index=False):
            tday = _epoch_day(row.transaction_date)
            # "Archive alive as-of" keys off ANY transaction date, NOT only
            # IL-classified ones, to match the online builder's EXISTS(any
            # player_transactions row < event_day) gate (_star_out_block). The
            # feed's non-IL moves (recalls/options) legitimately predate the
            # season's first IL placement; keying min_txn_day off IL moves only
            # would return None (bulk) where the online path returns a real 0 —
            # a train/serve-skew parity break. So update min_txn_day BEFORE the
            # il_effect filter below.
            if min_txn_day is None or tday < min_txn_day:
                min_txn_day = tday
            effect = il_effect(row.type_code, row.type_desc, row.description)
            if effect is None:
                continue
            moves_by_player.setdefault(row.player_id, []).append(
                (tday, row.mlb_transaction_id, effect)
            )

    # Realized lineup composition per (event_id, is_home): slot -> player.
    composition: dict = {}
    bo = lt["batting_order"].to_numpy(dtype=object)
    ev = lt["event_id"].to_numpy(dtype=object)
    ih = lt["is_home"].to_numpy(dtype=object)
    pl = lt["player_id"].to_numpy(dtype=object)
    for i in range(len(lt)):
        order = bo[i]
        if order is None or (isinstance(order, float) and np.isnan(order)):
            continue
        order = int(order)
        if order < 100 or order % 100 != 0:
            continue  # subs (101, 201, ...) and junk are not starters
        composition.setdefault((ev[i], bool(ih[i])), {})[order // 100] = pl[i]

    game_days = _utc_epoch_days(games["start_time_utc"])
    event_ids = games["event_id"].to_numpy(dtype=object)
    team_cols = {
        "home": games["home_team_id"].to_numpy(dtype=object),
        "away": games["away_team_id"].to_numpy(dtype=object),
    }
    out = {
        f"{side}_{name}": np.full(len(games), np.nan)
        for side in ("home", "away")
        for name in LINEUP_FEATURE_NAMES
    }
    # is_confirmed is a concrete flag (always 0 in backtest), never NaN.
    for side in ("home", "away"):
        out[f"{side}_lineup_is_confirmed"] = np.zeros(len(games))

    def _window_sums(entry: dict, lo: int, hi: int, prefix: str = "") -> dict:
        return {
            key: entry[f"{prefix}{key}"][hi] - entry[f"{prefix}{key}"][lo]
            for key in SUM_KEYS
        }

    def _star_out_bulk(team_id, day) -> int | None:
        # Twin of builder._star_out_block: None when the transactions archive is
        # not alive as-of (min move date not strictly before the game day) or no
        # established star is identifiable; else the count of top-2 batters on IL.
        if min_txn_day is None or min_txn_day >= day:
            return None
        candidates = team_to_players.get(team_id)
        if not candidates:
            return None
        player_sums: dict = {}
        for pid, entry in candidates:
            d = entry["days"]
            hi = int(np.searchsorted(d, day, side="left"))
            lo = int(np.searchsorted(d, day - LINEUP_BATTER_WINDOW_DAYS, side="left"))
            player_sums[pid] = _window_sums(entry, lo, hi)
        stars = top_k_star_players(player_sums)
        if not stars:
            return None
        return sum(1 for pid in stars if il_out_asof(moves_by_player.get(pid, []), day))

    for i in range(len(games)):
        day = int(game_days[i])
        for side in ("home", "away"):
            team_id = team_cols[side][i]
            # star_out is independent of the lineup composition — set it before
            # the no-lineup early continue (mirrors the online builder).
            star = _star_out_bulk(team_id, day)
            if star is not None:
                out[f"{side}_star_out_flag"][i] = star
            comp = composition.get((event_ids[i], side == "home"))
            if not comp:
                continue  # no realized lineup for this side: proj/top4 NaN
            hand = opp_hand.get((event_ids[i], side))
            slot_to_woba: dict = {}
            slot_to_vs_hand: dict = {}
            for slot, player in comp.items():
                entry = players.get((player, team_id))
                if entry is None:
                    slot_to_woba[slot] = None
                    continue
                d = entry["days"]
                hi = int(np.searchsorted(d, day, side="left"))
                lo = int(
                    np.searchsorted(d, day - LINEUP_BATTER_WINDOW_DAYS, side="left")
                )
                slot_to_woba[slot] = batter_woba_asof(_window_sums(entry, lo, hi))
                if hand in ("L", "R") and slot <= 4:
                    slot_to_vs_hand[slot] = batter_woba_vs_hand_asof(
                        _window_sums(entry, lo, hi, prefix=f"{hand}_"),
                        _window_sums(entry, lo, hi),
                    )
            proj = weighted_lineup_woba(slot_to_woba)
            if proj is not None:
                out[f"{side}_lineup_woba_proj"][i] = proj
            if hand in ("L", "R"):
                top4 = weighted_top4_woba(slot_to_vs_hand)
                if top4 is not None:
                    out[f"{side}_top4_woba_vs_hand"][i] = top4

    features = pd.DataFrame(out)
    features["event_id"] = games["event_id"].to_numpy()
    return features


def _merge_lineup_features(
    frame: pd.DataFrame,
    lineup: pd.DataFrame | None,
    games: pd.DataFrame,
    opp_hand: dict,
    transactions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach the lineup columns; NaN block (is_confirmed=0) with no data."""
    if lineup is None or len(lineup) == 0:
        for side in ("home", "away"):
            for name in LINEUP_FEATURE_NAMES:
                frame[f"{side}_{name}"] = (
                    0.0 if name == "lineup_is_confirmed" else np.nan
                )
        return frame
    return frame.merge(
        _lineup_features(lineup, games, opp_hand, transactions),
        on="event_id",
        how="left",
    )


def _merge_bullpen_features(
    frame: pd.DataFrame, bullpen: pd.DataFrame | None, games: pd.DataFrame
) -> pd.DataFrame:
    """Attach the day-window bullpen columns; NaN when no data was loaded."""
    if bullpen is None or len(bullpen) == 0:
        for side in ("home", "away"):
            for name in ("bullpen_ip_l3d", "bullpen_b2b_flag", "bullpen_xfip_30d"):
                frame[f"{side}_{name}"] = np.nan
        return frame
    return frame.merge(_bullpen_features(bullpen, games), on="event_id", how="left")


def _merge_starter_features(
    frame: pd.DataFrame, pitching: pd.DataFrame | None
) -> pd.DataFrame:
    """Attach home_sp_*/away_sp_* columns; NaN block when no data exists."""
    if pitching is None or len(pitching) == 0:
        for side in ("home", "away"):
            for name in (*SP_FEATURE_NAMES, "bullpen_ip_expected"):
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
    games: pd.DataFrame,
    market: str,
    pitching: pd.DataFrame | None = None,
    bullpen: pd.DataFrame | None = None,
    batting: pd.DataFrame | None = None,
    lineup: pd.DataFrame | None = None,
    transactions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Game rows with home_/away_ features, target and season.

    market='moneyline': target = home team won the game (no ties in finals;
    defensive drop if scores are equal).
    market='f5_moneyline': target = home led after 5 innings; F5 ties (push)
    and games without F5 partials are dropped — a push is not a loss and
    teaching the model otherwise would poison it. The F5 vector EXCLUDES
    the bullpen block by design (docs/04 §1.4); the offense (§1.2) and
    lineup (§1.5) blocks enter both markets.

    ``pitching``/``bullpen``/``batting``/``lineup`` are the loaders' outputs;
    without them the corresponding columns are NaN (the models impute, the
    report says so — never silently fabricated), except lineup_is_confirmed
    which is a concrete 0 (the backtest never has an archived pre-game
    lineup, §1.5). The offense and lineup vs-hand splits are selected by the
    pitching frame (actual starters): without pitching those split columns
    are NaN even when batting exists, mirroring the online builder without a
    known probable.
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
    opp_hand = _opp_hand_map(pitching)
    frame = _merge_offense_features(frame, batting, games, opp_hand)
    frame = _merge_lineup_features(frame, lineup, games, opp_hand, transactions)
    if market == "moneyline":
        frame = _merge_bullpen_features(frame, bullpen, games)

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
        ["event_id", "start_time_utc", "season", "target", *feature_columns(market)]
    ].sort_values("start_time_utc").reset_index(drop=True)
