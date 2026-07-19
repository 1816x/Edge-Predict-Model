"""As-of feature builder (F1 groundwork).

Implements the blocks of docs/04-features-y-modelos.md computable from the
own archive:
- team form (§1.1-1.2, §1.8): rolling win%, runs for/against per game,
  their First-5-Innings versions, rest days and schedule density;
- starting pitcher (§1.3): shrunk K-BB% and xFIP-core over the last 5
  starts and season-to-date, rest days, recent pitch count and handedness,
  from the pitching_game_logs archive (migration 003). The pitcher used
  here is the PROBABLE published at ``as_of_ts`` (event_probables); if no
  probable is known the block is None — never fabricated. Training uses
  the actual starter instead (app/ml/dataset.py); that probable-vs-actual
  gap is a documented backtest approximation.
- bullpen (§1.4, MONEYLINE ONLY): collective fatigue and quality from the
  same archive's reliever lines (is_starter = false). The block is
  excluded from the F5 vector by design — leverage bullpen does not
  participate in innings 1-5 (docs/04 §1.4 rationale). Its windows use
  CALENDAR DAYS (UTC) ending YESTERDAY, not timestamps: the intraday-safe
  rule of §1.1 (we cannot guarantee that a doubleheader's game 1 finished
  before decision time, so same-day games are excluded wholesale).
- team offense (§1.2, BOTH markets): wOBA/OPS/ISO/K%/BB% over the batting
  archive (migration 004), day windows ending yesterday like the bullpen
  block, plus a vs-opposing-hand wOBA split selected by the as-of
  probable's handedness and shrunk toward the team's trailing-year split.
  Formulas are shared with the bulk path via app/features/offense.py.
- lineup (§1.5, BOTH markets): per-batter as-of wOBA weighted by the real
  batting order (lineup_woba_proj) and the top-4 vs-hand wOBA (F5-critical),
  from the lineup PUBLISHED at as_of (event_lineups, migration 005) with an
  honest lineup_is_confirmed flag. No archived snapshot -> the block is None
  with is_confirmed=0; the online path never reads the realized box-score
  order. Formulas shared with the bulk path via app/features/lineup.py.
- IL / transactions (§1.5, §1.4b), from the transactions archive (migration
  006) plus the batting/reliever archives: star_out_flag (§1.5, BOTH markets)
  counts the team's top-2 batters on the IL as-of; bullpen_il_depletion
  (§1.4b, MONEYLINE ONLY) counts the team's top-K quality relievers (by
  xFIP-30d) on the IL as-of — the honest reformulation of
  closer_available_flag, NOT closer identity. Both are None (never a
  fabricated 0) without a live archive as-of. Classifier and replay shared
  with the bulk path via app/features/transactions.py.

NOT implemented yet (no placeholder numbers are fabricated for them):
- park factors (§1.6) and weather (§1.7): TODO(F1)
- TTO decay and fastball velocity delta (§1.3): TODO(F1.1) — need
  play-by-play / Statcast sources.

Anti-leakage: every query is bounded strictly below ``as_of_ts`` and
``build_features`` refuses an ``as_of_ts`` after the event's start time —
the cross-table invariant that infra/schema.sql delegates to this engine
(docs/03-modelo-de-datos.md). League constants for shrinkage are computed
as-of too (docs/04 §4 checklist item 9): a season-final league average
would leak the future into April rows.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Date, Table, cast, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

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
from app.features.transactions import (
    il_effect,
    il_out_asof,
    top_k_bullpen_arms,
    top_k_star_players,
)

ROLLING_WINDOW = timedelta(days=30)
RECENT_WINDOW = timedelta(days=7)

# Starter block (docs/04 §1.3). Windows are in STARTS (a starter pitches
# every ~5 days), with a 365-day hard floor so "last 5 starts" never reaches
# into a pitcher's ancient history after a long absence; league constants
# use the same rolling year (stable, and unaffected by the 2020 short
# season boundary the way a calendar-year constant would be).
SP_WINDOW = timedelta(days=365)
SP_LAST_STARTS = 5
SP_PITCH_COUNT_STARTS = 2
# Shrinkage toward the as-of league rate (docs/04 §1.1): pseudo-sample of
# 60 batters faced for K-BB%, 15 innings for xFIP-core. Early-season
# windows are noisy; a boring stable feature beats a reactive one.
SP_SHRINK_BF = 60.0
SP_SHRINK_IP = 15.0
# A gap longer than this is an IL stint / season start, not "rest": the
# feature goes None and the model's imputation handles it.
SP_MAX_REST_DAYS = 30

SP_FEATURES = (
    "sp_kbb_pct_l5_starts",
    "sp_kbb_pct_season",
    "sp_xfip_l5_starts",
    "sp_xfip_season",
    "sp_days_rest",
    "sp_pitch_count_l2_starts",
    "sp_is_lhp",
)

# Bullpen block (docs/04 §1.4) — MONEYLINE ONLY, day-based windows.
BULLPEN_FATIGUE_DAYS = 3
BULLPEN_QUALITY_DAYS = 30
BULLPEN_LEAGUE_DAYS = 365
# "Played yesterday AND the bullpen actually worked": one full inning. A
# named constant because docs/04 leaves the threshold open to tuning.
BULLPEN_B2B_MIN_OUTS = 3

BP_FEATURES = (
    "bullpen_ip_l3d",
    "bullpen_b2b_flag",
    "bullpen_xfip_30d",
    "bullpen_ip_expected",
    "bullpen_il_depletion",
)

FEATURE_TABLES = (
    "events",
    "event_results",
    "feature_snapshots",
    "players",
    "pitching_game_logs",
    "event_probables",
    "batting_game_logs",
    "event_lineups",
    "player_transactions",
)


def _round(value: float | None, digits: int = 4) -> float | None:
    """Stable rounding so the canonical hash never flaps on float noise."""
    return None if value is None else round(value, digits)


def _utc_date(ts: datetime):
    """Calendar date in UTC. psycopg localizes timestamptz to the SESSION
    time zone, so a bare .date()/.year would follow whatever the server is
    configured to — while the bulk dataset path is hard-pinned to UTC. Every
    date/year comparison in this module goes through UTC to keep the two
    paths identical regardless of database configuration."""
    return ts.astimezone(timezone.utc).date()


def _team_form(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    team_id: uuid.UUID,
    event_start: datetime,
    as_of_ts: datetime,
) -> dict[str, Any]:
    """Rolling form for one team from finished games strictly before as_of_ts."""
    events, results = t["events"], t["event_results"]
    rows = conn.execute(
        select(
            events.c.start_time_utc,
            (events.c.home_team_id == team_id).label("was_home"),
            results.c.home_score,
            results.c.away_score,
            results.c.f5_home_score,
            results.c.f5_away_score,
        )
        .select_from(events.join(results, results.c.event_id == events.c.id))
        .where(
            events.c.sport_id == sport_id,
            events.c.status == "final",
            (events.c.home_team_id == team_id) | (events.c.away_team_id == team_id),
            events.c.start_time_utc < as_of_ts,
        )
        .order_by(events.c.start_time_utc.desc())
    ).all()

    window_floor = as_of_ts - ROLLING_WINDOW
    wins = games = runs_for = runs_against = 0
    f5_games = f5_for = f5_against = 0
    games_last_7d = 0
    last_game_start: datetime | None = rows[0].start_time_utc if rows else None

    for row in rows:
        if row.start_time_utc >= as_of_ts - RECENT_WINDOW:
            games_last_7d += 1
        if row.start_time_utc < window_floor:
            continue
        scored, allowed = (
            (row.home_score, row.away_score)
            if row.was_home
            else (row.away_score, row.home_score)
        )
        games += 1
        wins += int(scored > allowed)
        runs_for += scored
        runs_against += allowed
        if row.f5_home_score is not None and row.f5_away_score is not None:
            f5_scored, f5_allowed = (
                (row.f5_home_score, row.f5_away_score)
                if row.was_home
                else (row.f5_away_score, row.f5_home_score)
            )
            f5_games += 1
            f5_for += f5_scored
            f5_against += f5_allowed

    return {
        "games_30d": games,
        "win_pct_30d": _round(wins / games) if games else None,
        "runs_pg_30d": _round(runs_for / games) if games else None,
        "runs_allowed_pg_30d": _round(runs_against / games) if games else None,
        "f5_games_30d": f5_games,
        "f5_runs_pg_30d": _round(f5_for / f5_games) if f5_games else None,
        "f5_runs_allowed_pg_30d": _round(f5_against / f5_games) if f5_games else None,
        "rest_days": (_utc_date(event_start) - _utc_date(last_game_start)).days
        if last_game_start
        else None,
        "games_last_7d": games_last_7d,
    }


def _fb_expr(logs: Table):
    """Fly balls proxy: fly outs + sac flies + home runs (docs/04 §1.3).

    Rows missing fly_outs undercount FB slightly; the proxy is consistent
    across pitchers and time, which is what a model feature needs (order
    and stability, not absolute scale). The ingest summary counts the
    NULLs so drift is visible.
    """
    return (
        func.coalesce(logs.c.fly_outs, 0)
        + func.coalesce(logs.c.sac_flies, 0)
        + logs.c.home_runs
    )


def _league_pitching(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    as_of_ts: datetime,
) -> dict[str, float] | None:
    """As-of league rates over starter rows in the trailing year.

    Returns lg_kbb (per batter faced), lg_hrfb (HR per fly ball) and
    lg_xfip_core (per inning), or None while the archive is too young to
    define a league (then the shrunk features stay None — not fabricated).
    """
    events, logs = t["events"], t["pitching_game_logs"]
    row = conn.execute(
        select(
            func.sum(logs.c.outs_recorded).label("outs"),
            func.sum(logs.c.batters_faced).label("bf"),
            func.sum(logs.c.strikeouts).label("k"),
            func.sum(logs.c.walks).label("bb"),
            func.sum(logs.c.hit_batsmen).label("hbp"),
            func.sum(logs.c.home_runs).label("hr"),
            func.sum(_fb_expr(logs)).label("fb"),
        )
        .select_from(logs.join(events, events.c.id == logs.c.event_id))
        .where(
            events.c.sport_id == sport_id,
            logs.c.is_starter,
            events.c.start_time_utc >= as_of_ts - SP_WINDOW,
            events.c.start_time_utc < as_of_ts,
        )
    ).one()
    if not row.bf or not row.outs or not row.fb:
        return None
    innings = row.outs / 3.0
    return {
        "lg_kbb": (row.k - row.bb) / row.bf,
        "lg_hrfb": row.hr / row.fb,
        "lg_xfip_core": (13.0 * row.hr + 3.0 * (row.bb + row.hbp) - 2.0 * row.k) / innings,
    }


def _probable_player(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    side: str,
    as_of_ts: datetime,
):
    """The probable announced at as_of_ts: latest first_seen_at <= as_of."""
    probables, players = t["event_probables"], t["players"]
    return conn.execute(
        select(probables.c.player_id, players.c.pitch_hand)
        .select_from(probables.join(players, players.c.id == probables.c.player_id))
        .where(
            probables.c.event_id == event_id,
            probables.c.side == side,
            probables.c.first_seen_at <= as_of_ts,
        )
        .order_by(probables.c.first_seen_at.desc())
        .limit(1)
    ).first()


def _shrunk_kbb(k: float, bb: float, bf: float, lg_kbb: float) -> float:
    """K-BB% regularized toward the league rate with SP_SHRINK_BF pseudo-BF."""
    return (k - bb + SP_SHRINK_BF * lg_kbb) / (bf + SP_SHRINK_BF)


def _xfip_core(
    k: float, bb_hbp: float, fb: float, innings: float,
    lg_hrfb: float, lg_xfip_core: float,
) -> float:
    """xFIP without the additive league constant, shrunk with SP_SHRINK_IP.

    xFIP replaces actual HR with expected HR (fly balls x league HR/FB) —
    exactly the variance correction a 5-start window needs, since FIP over
    5 starts is dominated by home-run noise. The additive constant that
    maps FIP to ERA scale is omitted on purpose: it shifts every pitcher
    equally and a model feature only cares about ordering.
    """
    core = 13.0 * fb * lg_hrfb + 3.0 * bb_hbp - 2.0 * k
    return (core + SP_SHRINK_IP * lg_xfip_core) / (innings + SP_SHRINK_IP)


def _utc_day_expr(events: Table):
    """SQL expression: the event's calendar date in UTC (for day windows)."""
    return cast(func.timezone("UTC", events.c.start_time_utc), Date)


def _league_bullpen(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    event_day,
) -> dict[str, float] | None:
    """As-of league rates over RELIEVER lines in the trailing year.

    Separate from the starter league on purpose: reliever HR/FB and
    xFIP-core run at different levels than starters', and mixing them
    would bias the shrinkage target. Day-based window ending yesterday
    (the block's intraday-safe rule)."""
    events, logs = t["events"], t["pitching_game_logs"]
    day = _utc_day_expr(events)
    row = conn.execute(
        select(
            func.sum(logs.c.outs_recorded).label("outs"),
            func.sum(logs.c.strikeouts).label("k"),
            func.sum(logs.c.walks).label("bb"),
            func.sum(logs.c.hit_batsmen).label("hbp"),
            func.sum(logs.c.home_runs).label("hr"),
            func.sum(_fb_expr(logs)).label("fb"),
        )
        .select_from(logs.join(events, events.c.id == logs.c.event_id))
        .where(
            events.c.sport_id == sport_id,
            ~logs.c.is_starter,
            day >= event_day - timedelta(days=BULLPEN_LEAGUE_DAYS),
            day < event_day,
        )
    ).one()
    if not row.outs or not row.fb:
        return None
    innings = row.outs / 3.0
    return {
        "lg_hrfb": row.hr / row.fb,
        "lg_xfip_core": (13.0 * row.hr + 3.0 * (row.bb + row.hbp) - 2.0 * row.k) / innings,
    }


def _bullpen_block(
    conn: Connection,
    t: dict[str, Table],
    team_id: uuid.UUID,
    event_day,
    league_bp: dict[str, float] | None,
) -> dict[str, Any]:
    """docs/04 §1.4 bullpen fatigue/quality for one team (MONEYLINE only).

    ip_l3d and b2b are TRUE ZEROS when the team's relievers did not pitch
    in the window (a rested bullpen, e.g. season opener) — but only while
    the reliever archive is alive at all (``league_bp`` not None). With no
    archived reliever lines in the trailing year, a zero would fabricate
    "fully rested" where the truth is "no data": the whole block stays
    None instead, mirroring the bulk path's NaN. Same-day games are
    excluded wholesale (intraday-safe rule, §1.1)."""
    if league_bp is None:
        return {
            "bullpen_ip_l3d": None,
            "bullpen_b2b_flag": None,
            "bullpen_xfip_30d": None,
        }
    events, logs = t["events"], t["pitching_game_logs"]
    day = _utc_day_expr(events)
    rows = conn.execute(
        select(
            day.label("day"),
            logs.c.outs_recorded,
            logs.c.strikeouts,
            logs.c.walks,
            logs.c.hit_batsmen,
            _fb_expr(logs).label("fb"),
        )
        .select_from(logs.join(events, events.c.id == logs.c.event_id))
        .where(
            logs.c.team_id == team_id,
            ~logs.c.is_starter,
            day >= event_day - timedelta(days=BULLPEN_QUALITY_DAYS),
            day < event_day,
        )
    ).all()

    fatigue_floor = event_day - timedelta(days=BULLPEN_FATIGUE_DAYS)
    yesterday = event_day - timedelta(days=1)
    yesterday_outs = sum(r.outs_recorded for r in rows if r.day == yesterday)
    block: dict[str, Any] = {
        "bullpen_ip_l3d": _round(
            sum(r.outs_recorded for r in rows if r.day >= fatigue_floor) / 3.0
        ),
        "bullpen_b2b_flag": int(yesterday_outs >= BULLPEN_B2B_MIN_OUTS),
        "bullpen_xfip_30d": None,
    }
    if rows and league_bp is not None:
        k = sum(r.strikeouts for r in rows)
        bb_hbp = sum(r.walks + r.hit_batsmen for r in rows)
        fb = sum(r.fb for r in rows)
        innings = sum(r.outs_recorded for r in rows) / 3.0
        block["bullpen_xfip_30d"] = _round(
            _xfip_core(
                k, bb_hbp, fb, innings,
                league_bp["lg_hrfb"], league_bp["lg_xfip_core"],
            )
        )
    return block


def _bullpen_il_block(
    conn: Connection,
    t: dict[str, Table],
    team_id: uuid.UUID,
    event_day,
    league_bp: dict[str, float] | None,
) -> int | None:
    """Count of the team's top-K quality relievers on the IL as-of (§1.4b).

    The honest reformulation of the deferred ``closer_available_flag``: it does
    NOT identify the closer (we store no saves/leverage). It ranks the team's
    relievers by the SAME xFIP-30d the bullpen block already uses (lower is
    better), takes the top ``BULLPEN_IL_TOP_K`` established arms, and counts how
    many are on the IL as-of ``date < event_day`` (<= t-1). MONEYLINE only.

    None (never a fabricated 0) when ANY gate is not satisfiable as-of: the
    reliever archive is not alive (``league_bp is None`` — same gate as
    ``_bullpen_block``), the transactions archive is not alive (identical to
    ``_star_out_block``), or no reliever clears the min-outs establishment gate.
    A real 0 means all three gates pass, the top-K are known, and none is out.
    Structurally the twin of ``_star_out_block`` (relievers-by-xFIP instead of
    top-2 batters-by-wOBA), and independent of the fatigue block above."""
    if league_bp is None:
        return None
    txns = t["player_transactions"]
    alive = conn.execute(
        select(txns.c.id).where(txns.c.transaction_date < event_day).limit(1)
    ).first()
    if alive is None:
        return None  # transactions archive not alive as-of: unknown, not a 0

    events, logs = t["events"], t["pitching_game_logs"]
    day = _utc_day_expr(events)
    rows = conn.execute(
        select(
            logs.c.player_id.label("player_id"),
            func.sum(logs.c.strikeouts).label("k"),
            func.sum(logs.c.walks + logs.c.hit_batsmen).label("bb_hbp"),
            func.sum(_fb_expr(logs)).label("fb"),
            func.sum(logs.c.outs_recorded).label("outs"),
        )
        .select_from(logs.join(events, events.c.id == logs.c.event_id))
        .where(
            logs.c.team_id == team_id,
            ~logs.c.is_starter,
            day >= event_day - timedelta(days=BULLPEN_QUALITY_DAYS),
            day < event_day,
        )
        .group_by(logs.c.player_id)
    ).all()
    player_xfips = {
        r.player_id: (
            _xfip_core(
                r.k, r.bb_hbp, r.fb, r.outs / 3.0,
                league_bp["lg_hrfb"], league_bp["lg_xfip_core"],
            ),
            float(r.outs),
        )
        for r in rows
        if r.outs
    }
    arms = top_k_bullpen_arms(player_xfips)
    if not arms:
        return None  # no established reliever identifiable as-of: unknown, not 0

    tx_rows = conn.execute(
        select(
            txns.c.player_id,
            txns.c.type_code,
            txns.c.type_desc,
            txns.c.description,
            txns.c.transaction_date,
            txns.c.mlb_transaction_id,
        ).where(txns.c.player_id.in_(arms), txns.c.transaction_date < event_day)
    ).all()
    moves: dict[Any, list] = {}
    for r in tx_rows:
        effect = il_effect(r.type_code, r.type_desc, r.description)
        if effect is None:
            continue
        moves.setdefault(r.player_id, []).append(
            (r.transaction_date, r.mlb_transaction_id, effect)
        )
    return sum(1 for pid in arms if il_out_asof(moves.get(pid, []), event_day))


def _offense_block(
    conn: Connection,
    t: dict[str, Table],
    team_id: uuid.UUID,
    event_day,
    opp_hand: str | None,
) -> dict[str, Any]:
    """docs/04 §1.2 team offense for one side, day windows ending YESTERDAY.

    Windows are UTC calendar days [D-30, D-1] (and season-to-date / the
    trailing year for the split target) — the intraday-safe rule of §1.1,
    exactly like the bullpen block: a doubleheader's game 1 can never leak
    into game 2's vector.

    ``opp_hand`` is the L/R hand of the OPPOSING probable starter at
    as_of (None when unknown): it selects which starter-hand split the
    shrunk vs-hand feature reports. Split classification of PAST games
    uses the actual starter faced (the opposing side's is_starter row) —
    a boxscore-computable proxy for true PA-level splits, documented in
    docs/00. Empty windows are None per rate: a team with no archived
    batting contributes Nones, never fabricated zeros.
    """
    block: dict[str, Any] = {name: None for name in OFFENSE_FEATURE_NAMES}
    batting, events = t["batting_game_logs"], t["events"]
    logs, players = t["pitching_game_logs"], t["players"]
    day = _utc_day_expr(events)

    starter_hand = (
        select(logs.c.event_id, logs.c.is_home, players.c.pitch_hand)
        .select_from(logs.join(players, players.c.id == logs.c.player_id))
        .where(logs.c.is_starter)
        .subquery()
    )
    rows = conn.execute(
        select(
            day.label("day"),
            *[func.sum(batting.c[key]).label(key) for key in SUM_KEYS],
            func.max(starter_hand.c.pitch_hand).label("opp_hand"),
        )
        .select_from(
            batting.join(events, events.c.id == batting.c.event_id).outerjoin(
                starter_hand,
                (starter_hand.c.event_id == batting.c.event_id)
                & (starter_hand.c.is_home != batting.c.is_home),
            )
        )
        .where(
            batting.c.team_id == team_id,
            day >= event_day - timedelta(days=OFFENSE_SPLIT_TARGET_DAYS),
            day < event_day,
        )
        .group_by(batting.c.event_id, day)
    ).all()
    if not rows:
        return block

    def _sums(subset) -> dict[str, float]:
        return {key: float(sum(getattr(r, key) for r in subset)) for key in SUM_KEYS}

    floor_30 = event_day - timedelta(days=OFFENSE_ROLLING_DAYS)
    win30 = [r for r in rows if r.day >= floor_30]
    block.update(offense_rates(_sums(win30)))
    season = [r for r in rows if r.day >= event_day.replace(month=1, day=1)]
    if season:
        block["team_woba_season"] = woba(_sums(season))
    if opp_hand in ("L", "R"):
        hand30 = [r for r in win30 if r.opp_hand == opp_hand]
        hand365 = [r for r in rows if r.opp_hand == opp_hand]
        num_30, den_30 = woba_parts(_sums(hand30))
        num_365, den_365 = woba_parts(_sums(hand365))
        block["team_woba_vs_opp_hand_30d"] = shrunk_split(
            num_30, den_30, num_365, den_365
        )
    return block


def _star_out_block(conn, t, team_id, event_day) -> int | None:
    """Count of the team's top-2 established batters on the IL as-of (§1.5).

    Independent of the lineup snapshot: a star can be out whether or not a
    lineup is posted, so this uses only team batting + the transactions archive
    (never the slot machinery). None (not 0) when the transactions archive is
    NOT alive as-of the game, or when no established star is identifiable — a
    zero would fabricate "everyone healthy" where the truth is "no data". A
    real 0 means the archive is alive, the top-2 are known, and neither is out.
    """
    txns = t["player_transactions"]
    alive = conn.execute(
        select(txns.c.id).where(txns.c.transaction_date < event_day).limit(1)
    ).first()
    if alive is None:
        return None  # archive not alive as-of: unknown, never a fabricated 0

    batting, events = t["batting_game_logs"], t["events"]
    day = _utc_day_expr(events)
    rows = conn.execute(
        select(
            batting.c.player_id.label("player_id"),
            *[func.sum(batting.c[key]).label(key) for key in SUM_KEYS],
        )
        .select_from(batting.join(events, events.c.id == batting.c.event_id))
        .where(
            batting.c.team_id == team_id,
            day >= event_day - timedelta(days=LINEUP_BATTER_WINDOW_DAYS),
            day < event_day,
        )
        .group_by(batting.c.player_id)
    ).all()
    player_sums = {
        r.player_id: {key: float(getattr(r, key)) for key in SUM_KEYS} for r in rows
    }
    stars = top_k_star_players(player_sums)
    if not stars:
        return None  # no established star identifiable as-of: unknown, not 0

    tx_rows = conn.execute(
        select(
            txns.c.player_id,
            txns.c.type_code,
            txns.c.type_desc,
            txns.c.description,
            txns.c.transaction_date,
            txns.c.mlb_transaction_id,
        ).where(txns.c.player_id.in_(stars), txns.c.transaction_date < event_day)
    ).all()
    moves: dict[Any, list] = {}
    for r in tx_rows:
        effect = il_effect(r.type_code, r.type_desc, r.description)
        if effect is None:
            continue
        moves.setdefault(r.player_id, []).append(
            (r.transaction_date, r.mlb_transaction_id, effect)
        )
    return sum(1 for pid in stars if il_out_asof(moves.get(pid, []), event_day))


def _lineup_block(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    side: str,
    team_id: uuid.UUID,
    event_day,
    opp_hand: str | None,
    as_of_ts: datetime,
) -> dict[str, Any]:
    """docs/04 §1.5 lineup features for one side, from the as-of published order.

    The lineup is the one ARCHIVED in event_lineups with the greatest
    first_seen_at <= as_of (production, lineup_is_confirmed=1); if none is on
    file the block is None with is_confirmed=0 — the online path NEVER falls
    back to the realized box-score order, which only becomes known at game
    time (that is the bulk/backtest reconstruction, is_confirmed=0). Per-batter
    wOBA uses the same day windows as the offense block (365 UTC days ending
    yesterday, so a doubleheader's game 1 can never leak into game 2) and the
    same opposing-starter-hand classification of past games. Empty windows or
    a batter with no trailing-year line contribute None, never fabricated
    zeros (the slot drops from the PA-share weighting).
    """
    block: dict[str, Any] = {name: None for name in LINEUP_FEATURE_NAMES}
    block["lineup_is_confirmed"] = 0
    # star_out_flag is independent of the lineup snapshot and must survive the
    # early returns below (no snapshot / no slots), so compute it FIRST.
    block["star_out_flag"] = _star_out_block(conn, t, team_id, event_day)

    lineups = t["event_lineups"]
    latest_ts = conn.execute(
        select(func.max(lineups.c.first_seen_at)).where(
            lineups.c.event_id == event_id,
            lineups.c.side == side,
            lineups.c.first_seen_at <= as_of_ts,
        )
    ).scalar()
    if latest_ts is None:
        return block
    slot_rows = conn.execute(
        select(lineups.c.batting_order, lineups.c.player_id).where(
            lineups.c.event_id == event_id,
            lineups.c.side == side,
            lineups.c.first_seen_at == latest_ts,
        )
    ).all()
    slot_to_player = {
        r.batting_order // 100: r.player_id
        for r in slot_rows
        if r.batting_order % 100 == 0
    }
    if not slot_to_player:
        return block
    block["lineup_is_confirmed"] = 1

    batting, events = t["batting_game_logs"], t["events"]
    logs, players = t["pitching_game_logs"], t["players"]
    day = _utc_day_expr(events)
    starter_hand = (
        select(logs.c.event_id, logs.c.is_home, players.c.pitch_hand)
        .select_from(logs.join(players, players.c.id == logs.c.player_id))
        .where(logs.c.is_starter)
        .subquery()
    )
    rows = conn.execute(
        select(
            batting.c.player_id.label("player_id"),
            *[func.sum(batting.c[key]).label(key) for key in SUM_KEYS],
            func.max(starter_hand.c.pitch_hand).label("opp_hand"),
        )
        .select_from(
            batting.join(events, events.c.id == batting.c.event_id).outerjoin(
                starter_hand,
                (starter_hand.c.event_id == batting.c.event_id)
                & (starter_hand.c.is_home != batting.c.is_home),
            )
        )
        .where(
            batting.c.player_id.in_(list(slot_to_player.values())),
            batting.c.team_id == team_id,
            day >= event_day - timedelta(days=LINEUP_BATTER_WINDOW_DAYS),
            day < event_day,
        )
        .group_by(batting.c.player_id, batting.c.event_id)
    ).all()

    per_player: dict[uuid.UUID, list] = {}
    for r in rows:
        per_player.setdefault(r.player_id, []).append(r)

    def _sums(subset) -> dict[str, float]:
        return {key: float(sum(getattr(r, key) for r in subset)) for key in SUM_KEYS}

    slot_to_woba = {
        slot: batter_woba_asof(_sums(per_player.get(player, [])))
        for slot, player in slot_to_player.items()
    }
    block["lineup_woba_proj"] = weighted_lineup_woba(slot_to_woba)

    if opp_hand in ("L", "R"):
        slot_to_vs_hand: dict[int, float | None] = {}
        for slot in range(1, 5):
            player = slot_to_player.get(slot)
            if player is None:
                continue
            games = per_player.get(player, [])
            hand_games = [r for r in games if r.opp_hand == opp_hand]
            slot_to_vs_hand[slot] = batter_woba_vs_hand_asof(
                _sums(hand_games), _sums(games)
            )
        block["top4_woba_vs_hand"] = weighted_top4_woba(slot_to_vs_hand)

    return block


def _starter_block(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    side: str,
    event_start: datetime,
    as_of_ts: datetime,
    league: dict[str, float] | None,
) -> dict[str, Any]:
    """docs/04 §1.3 starter features for one side, from the as-of probable.

    Also emits ``bullpen_ip_expected`` (§1.4): the starter's mean innings
    per start in the window — a short starter means more bullpen exposure.
    It rides in this block because it is derived from the same rows;
    build_features drops it from the F5 vector along with the rest of the
    bullpen block."""
    block: dict[str, Any] = {
        name: None for name in (*SP_FEATURES, "bullpen_ip_expected")
    }
    probable = _probable_player(conn, t, event_id, side, as_of_ts)
    if probable is None:
        return block
    if probable.pitch_hand in ("L", "R"):
        block["sp_is_lhp"] = int(probable.pitch_hand == "L")

    events, logs = t["events"], t["pitching_game_logs"]
    rows = conn.execute(
        select(
            events.c.start_time_utc,
            logs.c.outs_recorded,
            logs.c.batters_faced,
            logs.c.strikeouts,
            logs.c.walks,
            logs.c.hit_batsmen,
            _fb_expr(logs).label("fb"),
            logs.c.pitches_thrown,
        )
        .select_from(logs.join(events, events.c.id == logs.c.event_id))
        .where(
            logs.c.player_id == probable.player_id,
            logs.c.is_starter,
            events.c.start_time_utc >= as_of_ts - SP_WINDOW,
            events.c.start_time_utc < as_of_ts,
        )
        .order_by(events.c.start_time_utc.desc())
    ).all()
    if not rows:
        return block

    rest = (_utc_date(event_start) - _utc_date(rows[0].start_time_utc)).days
    if rest <= SP_MAX_REST_DAYS:
        block["sp_days_rest"] = rest

    pitches = [
        r.pitches_thrown
        for r in rows[:SP_PITCH_COUNT_STARTS]
        if r.pitches_thrown is not None
    ]
    if pitches:
        block["sp_pitch_count_l2_starts"] = int(sum(pitches))

    block["bullpen_ip_expected"] = _round(
        sum(r.outs_recorded for r in rows) / len(rows) / 3.0
    )

    if league is None:
        return block

    def _rates(window) -> tuple[float | None, float | None]:
        if not window:
            return None, None
        k = sum(r.strikeouts for r in window)
        bb = sum(r.walks for r in window)
        bf = sum(r.batters_faced for r in window)
        bb_hbp = sum(r.walks + r.hit_batsmen for r in window)
        fb = sum(r.fb for r in window)
        innings = sum(r.outs_recorded for r in window) / 3.0
        return (
            _round(_shrunk_kbb(k, bb, bf, league["lg_kbb"])),
            _round(
                _xfip_core(
                    k, bb_hbp, fb, innings, league["lg_hrfb"], league["lg_xfip_core"]
                )
            ),
        )

    season_year = _utc_date(event_start).year
    season = [r for r in rows if _utc_date(r.start_time_utc).year == season_year]
    block["sp_kbb_pct_l5_starts"], block["sp_xfip_l5_starts"] = _rates(
        rows[:SP_LAST_STARTS]
    )
    block["sp_kbb_pct_season"], block["sp_xfip_season"] = _rates(season)
    return block


def build_features(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    market: str,
    as_of_ts: datetime,
) -> dict[str, Any]:
    """Build the as-of feature dict for one event/market.

    Raises:
        ValueError: if ``as_of_ts`` is after the event's start time — that
            snapshot would describe information unavailable at decision time.
        LookupError: if the event does not exist.
    """
    events = t["events"]
    event = conn.execute(
        select(
            events.c.sport_id,
            events.c.home_team_id,
            events.c.away_team_id,
            events.c.start_time_utc,
        ).where(events.c.id == event_id)
    ).first()
    if event is None:
        raise LookupError(f"event {event_id} not found")
    if as_of_ts > event.start_time_utc:
        raise ValueError(
            f"as_of_ts {as_of_ts.isoformat()} is after event start "
            f"{event.start_time_utc.isoformat()} (anti-leakage invariant, docs/03)"
        )

    league = _league_pitching(conn, t, event.sport_id, as_of_ts)
    event_day = _utc_date(event.start_time_utc)
    sides = {}
    for side, team_id in (("home", event.home_team_id), ("away", event.away_team_id)):
        sides[side] = {
            **_team_form(
                conn, t, event.sport_id, team_id, event.start_time_utc, as_of_ts
            ),
            **_starter_block(
                conn, t, event_id, side, event.start_time_utc, as_of_ts, league
            ),
        }

    # Offense (§1.2) and lineup (§1.5) blocks, BOTH markets. Second loop on
    # purpose: their vs-hand splits are selected by the OPPOSING probable's
    # handedness, which the starter block above already resolved as sp_is_lhp.
    for side, team_id in (("home", event.home_team_id), ("away", event.away_team_id)):
        opp = "away" if side == "home" else "home"
        opp_hand = {1: "L", 0: "R"}.get(sides[opp].get("sp_is_lhp"))
        sides[side].update(_offense_block(conn, t, team_id, event_day, opp_hand))
        sides[side].update(
            _lineup_block(conn, t, event_id, side, team_id, event_day, opp_hand, as_of_ts)
        )

    # Bullpen block enters the MONEYLINE vector only: leverage relievers do
    # not participate in innings 1-5, so F5 excludes it BY DESIGN — the
    # features are removed, not zero-weighted (docs/04 §1.4).
    if market == "moneyline":
        league_bp = _league_bullpen(conn, t, event.sport_id, event_day)
        for side, team_id in (("home", event.home_team_id), ("away", event.away_team_id)):
            sides[side].update(
                _bullpen_block(conn, t, team_id, event_day, league_bp)
            )
            # bullpen_il_depletion (§1.4b): integer count (0..K) or None — NOT
            # rounded. Independent of the fatigue block; same league_bp gate.
            sides[side]["bullpen_il_depletion"] = _bullpen_il_block(
                conn, t, team_id, event_day, league_bp
            )
    else:
        for side in sides:
            sides[side].pop("bullpen_ip_expected")

    # as_of_ts intentionally NOT included in the dict: it lives in its own
    # column, and keeping it out lets identical vectors captured at different
    # times dedupe on (event, market, feature_hash) as the schema intends.
    return {
        "feature_version": "team_form_sp_bp_off_lineup_star_bpil_v7",
        "market": market,
        "home": sides["home"],
        "away": sides["away"],
        # TODO(F1): park, weather blocks (docs/04 §1.6-1.7) and TTO/velocity
        # (§1.3, needs play-by-play / Statcast). Absent on purpose — never
        # fabricated.
    }


def canonical_feature_hash(features: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON encoding (sorted keys, no whitespace)."""
    canonical = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_feature_snapshot(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    market: str,
    features: dict[str, Any],
    as_of_ts: datetime,
) -> uuid.UUID:
    """Persist a snapshot; identical vectors dedupe on (event, market, hash)."""
    snapshots = t["feature_snapshots"]
    feature_hash = canonical_feature_hash(features)
    inserted = conn.execute(
        pg_insert(snapshots)
        .values(
            event_id=event_id,
            market=market,
            features=features,
            feature_hash=feature_hash,
            as_of_ts=as_of_ts,
        )
        .on_conflict_do_nothing()
        .returning(snapshots.c.id)
    ).first()
    if inserted is not None:
        return inserted.id
    return conn.execute(
        select(snapshots.c.id).where(
            snapshots.c.event_id == event_id,
            snapshots.c.market == market,
            snapshots.c.feature_hash == feature_hash,
        )
    ).scalar_one()
