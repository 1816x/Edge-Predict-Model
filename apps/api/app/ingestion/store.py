"""Persistence for normalized ingestion rows.

Tables are reflected from the live database (see ``app.db.engine``); the
schema's single source of truth is ``infra/schema.sql``. All writers take an
open Connection so a job can commit one atomic transaction per run.

Event identity strategy (doubleheader-safe):
1. Exact match by external id (``mlb_game_pk`` for the schedule job,
   ``the_odds_api_id`` for the odds job).
2. Otherwise, same team pair with the closest start time within
   EVENT_MATCH_WINDOW — close enough to absorb feed clock drift, small
   enough that a doubleheader's two games (hours apart) never cross-match.
3. Otherwise, a new event row is created and external ids merge later.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import MetaData, Row, Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.ingestion.parsers import OddsEvent, OddsOutcome, ScheduledGame

INGESTION_TABLES = ("sports", "books", "teams", "events", "odds_snapshots")

EVENT_MATCH_WINDOW = timedelta(hours=3)


def reflect_tables(
    engine: Engine, names: tuple[str, ...] = INGESTION_TABLES
) -> dict[str, Table]:
    """Reflect the given tables; fails loudly if the schema is not applied."""
    meta = MetaData()
    meta.reflect(bind=engine, only=list(names))
    return {name: meta.tables[name] for name in names}


def get_sport_id(conn: Connection, t: dict[str, Table], key: str = "mlb") -> uuid.UUID:
    row = conn.execute(select(t["sports"].c.id).where(t["sports"].c.key == key)).first()
    if row is None:
        raise LookupError(f"sport '{key}' not seeded; apply infra/schema.sql first")
    return row.id


def get_or_create_book(conn: Connection, t: dict[str, Table], key: str) -> uuid.UUID:
    """Return the book id, auto-registering unknown The Odds API book keys."""
    books = t["books"]
    row = conn.execute(select(books.c.id).where(books.c.key == key)).first()
    if row is not None:
        return row.id
    return conn.execute(
        books.insert()
        .values(key=key, display_name=key.replace("_", " ").title())
        .returning(books.c.id)
    ).scalar_one()


def get_or_create_team(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    name: str,
    mlb_stats_id: int | None = None,
) -> uuid.UUID:
    """Match teams by full name (both feeds use e.g. 'New York Yankees')."""
    teams = t["teams"]
    row = conn.execute(
        select(teams.c.id, teams.c.external_ids).where(
            teams.c.sport_id == sport_id, teams.c.name == name
        )
    ).first()
    if row is not None:
        existing = row.external_ids or {}
        if mlb_stats_id is not None and "mlb_stats_id" not in existing:
            conn.execute(
                update(teams)
                .where(teams.c.id == row.id)
                .values(external_ids={**existing, "mlb_stats_id": mlb_stats_id})
            )
        return row.id
    external = {"mlb_stats_id": mlb_stats_id} if mlb_stats_id is not None else {}
    return conn.execute(
        teams.insert()
        .values(sport_id=sport_id, name=name, external_ids=external)
        .returning(teams.c.id)
    ).scalar_one()


def _find_event_by_teams(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    home_team_id: uuid.UUID,
    away_team_id: uuid.UUID,
    start_time: datetime,
) -> Row | None:
    """Closest-start match within EVENT_MATCH_WINDOW for the same team pair."""
    events = t["events"]
    seconds_off = func.abs(func.extract("epoch", events.c.start_time_utc - start_time))
    return conn.execute(
        select(events.c.id, events.c.external_ids)
        .where(
            events.c.sport_id == sport_id,
            events.c.home_team_id == home_team_id,
            events.c.away_team_id == away_team_id,
            seconds_off <= EVENT_MATCH_WINDOW.total_seconds(),
        )
        .order_by(seconds_off)
        .limit(1)
    ).first()


def upsert_event_from_schedule(
    conn: Connection, t: dict[str, Table], sport_id: uuid.UUID, game: ScheduledGame
) -> tuple[uuid.UUID, bool]:
    """Insert or refresh one scheduled game; returns (event_id, created)."""
    events = t["events"]
    home_id = get_or_create_team(conn, t, sport_id, game.home_name, game.home_mlb_id)
    away_id = get_or_create_team(conn, t, sport_id, game.away_name, game.away_mlb_id)

    row = conn.execute(
        select(events.c.id).where(
            events.c.external_ids["mlb_game_pk"].astext == str(game.game_pk)
        )
    ).first()
    if row is not None:
        # The MLB feed is authoritative for status and (rescheduled) start times.
        conn.execute(
            update(events)
            .where(events.c.id == row.id)
            .values(start_time_utc=game.start_time, status=game.status)
        )
        return row.id, False

    match = _find_event_by_teams(conn, t, sport_id, home_id, away_id, game.start_time)
    if match is not None:
        # Event first seen by the odds job; attach the MLB identity to it.
        conn.execute(
            update(events)
            .where(events.c.id == match.id)
            .values(
                external_ids={**(match.external_ids or {}), "mlb_game_pk": str(game.game_pk)},
                start_time_utc=game.start_time,
                status=game.status,
            )
        )
        return match.id, False

    new_id = conn.execute(
        events.insert()
        .values(
            sport_id=sport_id,
            home_team_id=home_id,
            away_team_id=away_id,
            start_time_utc=game.start_time,
            status=game.status,
            external_ids={"mlb_game_pk": str(game.game_pk)},
        )
        .returning(events.c.id)
    ).scalar_one()
    return new_id, True


def find_or_create_event_for_odds(
    conn: Connection, t: dict[str, Table], sport_id: uuid.UUID, ev: OddsEvent
) -> tuple[uuid.UUID, bool]:
    """Resolve an odds feed event to an events row; returns (event_id, created)."""
    events = t["events"]
    row = conn.execute(
        select(events.c.id).where(
            events.c.external_ids["the_odds_api_id"].astext == ev.source_id
        )
    ).first()
    if row is not None:
        return row.id, False

    home_id = get_or_create_team(conn, t, sport_id, ev.home_team)
    away_id = get_or_create_team(conn, t, sport_id, ev.away_team)

    match = _find_event_by_teams(conn, t, sport_id, home_id, away_id, ev.commence_time)
    if match is not None:
        conn.execute(
            update(events)
            .where(events.c.id == match.id)
            .values(
                external_ids={**(match.external_ids or {}), "the_odds_api_id": ev.source_id}
            )
        )
        return match.id, False

    new_id = conn.execute(
        events.insert()
        .values(
            sport_id=sport_id,
            home_team_id=home_id,
            away_team_id=away_id,
            start_time_utc=ev.commence_time,
            status="scheduled",
            external_ids={"the_odds_api_id": ev.source_id},
        )
        .returning(events.c.id)
    ).scalar_one()
    return new_id, True


def insert_odds_snapshots(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    outcomes: tuple[OddsOutcome, ...],
    captured_at: datetime,
    is_closing: bool = False,
) -> int:
    """Append snapshot rows; duplicates (same capture instant, or a second
    closing row per outcome) are silently skipped via ON CONFLICT DO NOTHING.
    Returns the number of rows actually inserted."""
    if not outcomes:
        return 0
    book_ids: dict[str, uuid.UUID] = {}
    rows = []
    for outcome in outcomes:
        if outcome.book_key not in book_ids:
            book_ids[outcome.book_key] = get_or_create_book(conn, t, outcome.book_key)
        rows.append(
            {
                "event_id": event_id,
                "book_id": book_ids[outcome.book_key],
                "market": outcome.market,
                "side": outcome.side,
                "price_decimal": round(outcome.price_decimal, 3),
                "price_american": outcome.price_american,
                "captured_at": captured_at,
                "is_closing": is_closing,
            }
        )
    # RETURNING yields only the rows actually inserted under DO NOTHING,
    # which is a reliable count (rowcount is -1 on some driver paths).
    snaps = t["odds_snapshots"]
    result = conn.execute(
        pg_insert(snaps).values(rows).on_conflict_do_nothing().returning(snaps.c.id)
    )
    return len(result.fetchall())
