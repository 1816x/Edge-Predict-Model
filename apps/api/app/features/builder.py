"""As-of feature builder (F1 groundwork).

Implements the team-form blocks of docs/04-features-y-modelos.md §1.1-1.2
and §1.8 that are computable from the events/event_results archive (daily
sync + historical backfill): rolling win%, runs for/against per game, their
First-5-Innings versions, rest days and schedule density.

NOT implemented yet — these need per-game pitching logs, Statcast and
external sources, and no placeholder numbers are fabricated for them:
- starting pitcher block (docs/04 §1.3): TODO(F1)
- bullpen availability (§1.4, ML only): TODO(F1)
- lineup / star-out flags (§1.5): TODO(F1)
- park factors (§1.6) and weather (§1.7): TODO(F1)

Anti-leakage: every query is bounded strictly below ``as_of_ts`` and
``build_features`` refuses an ``as_of_ts`` after the event's start time —
the cross-table invariant that infra/schema.sql delegates to this engine
(docs/03-modelo-de-datos.md).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection

ROLLING_WINDOW = timedelta(days=30)
RECENT_WINDOW = timedelta(days=7)

FEATURE_TABLES = ("events", "event_results", "feature_snapshots")


def _round(value: float | None, digits: int = 4) -> float | None:
    """Stable rounding so the canonical hash never flaps on float noise."""
    return None if value is None else round(value, digits)


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
        "rest_days": (event_start.date() - last_game_start.date()).days
        if last_game_start
        else None,
        "games_last_7d": games_last_7d,
    }


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

    # as_of_ts intentionally NOT included in the dict: it lives in its own
    # column, and keeping it out lets identical vectors captured at different
    # times dedupe on (event, market, feature_hash) as the schema intends.
    return {
        "feature_version": "team_form_v1",
        "market": market,
        "home": _team_form(
            conn, t, event.sport_id, event.home_team_id, event.start_time_utc, as_of_ts
        ),
        "away": _team_form(
            conn, t, event.sport_id, event.away_team_id, event.start_time_utc, as_of_ts
        ),
        # TODO(F1): starting pitcher, bullpen, lineup, park, weather blocks
        # (docs/04 §1.3-1.7). Absent on purpose — never fabricated.
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
