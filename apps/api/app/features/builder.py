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

NOT implemented yet (no placeholder numbers are fabricated for them):
- bullpen availability (§1.4, ML only): TODO(F1)
- lineup / star-out flags (§1.5): TODO(F1)
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

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

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

FEATURE_TABLES = (
    "events",
    "event_results",
    "feature_snapshots",
    "players",
    "pitching_game_logs",
    "event_probables",
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


def _starter_block(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    side: str,
    event_start: datetime,
    as_of_ts: datetime,
    league: dict[str, float] | None,
) -> dict[str, Any]:
    """docs/04 §1.3 starter features for one side, from the as-of probable."""
    block: dict[str, Any] = {name: None for name in SP_FEATURES}
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

    # as_of_ts intentionally NOT included in the dict: it lives in its own
    # column, and keeping it out lets identical vectors captured at different
    # times dedupe on (event, market, feature_hash) as the schema intends.
    return {
        "feature_version": "team_form_sp_v2",
        "market": market,
        "home": {
            **_team_form(
                conn, t, event.sport_id, event.home_team_id, event.start_time_utc, as_of_ts
            ),
            **_starter_block(
                conn, t, event_id, "home", event.start_time_utc, as_of_ts, league
            ),
        },
        "away": {
            **_team_form(
                conn, t, event.sport_id, event.away_team_id, event.start_time_utc, as_of_ts
            ),
            **_starter_block(
                conn, t, event_id, "away", event.start_time_utc, as_of_ts, league
            ),
        },
        # TODO(F1): bullpen, lineup, park, weather blocks (docs/04 §1.4-1.7)
        # and TTO/velocity (§1.3). Absent on purpose — never fabricated.
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
