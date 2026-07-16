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

from sqlalchemy import MetaData, Row, Table, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine

from app.ingestion.parsers import (
    BattingLine,
    GameResult,
    OddsEvent,
    OddsOutcome,
    PitchingLine,
    PlayerTransaction,
    ScheduledGame,
)

INGESTION_TABLES = (
    "sports",
    "books",
    "teams",
    "events",
    "event_results",
    "odds_snapshots",
)

# Migration 003 tables, reflected separately: a database that has not run
# the migration yet must not break sync_schedule/snapshot_odds reflection
# in the window between merging code and applying the migration.
PITCHING_TABLES = (
    "sports",
    "teams",
    "events",
    "players",
    "pitching_game_logs",
    "event_probables",
)

# Migration 004 table, reflected separately for the same degradation
# contract: pre-004 the boxscore job keeps ingesting pitching and only
# skips the batting half (with a note), never the whole run.
BATTING_TABLE = "batting_game_logs"

# Migration 005 table, reflected separately (same degradation contract):
# pre-005 the lineup-archive job skips with a note while the rest of the
# pre-game crons keep running.
LINEUP_TABLE = "event_lineups"

# Migration 006 table, reflected separately (same degradation contract):
# pre-006 sync_transactions skips with a note and the feature layer treats
# the archive as not-yet-alive (star_out_flag stays None).
TRANSACTIONS_TABLE = "player_transactions"

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
    lacking_external_key: str,
) -> Row | None:
    """Closest-start match within EVENT_MATCH_WINDOW for the same team pair.

    Only events that do NOT yet carry ``lacking_external_key`` are eligible:
    an event that already owns an identity from the same feed is a DIFFERENT
    game (traditional doubleheaders list two games with identical teams and
    even identical start times), and merging would clobber its identity —
    the exact bug the 2018 backfill exposed.
    """
    events = t["events"]
    seconds_off = func.abs(func.extract("epoch", events.c.start_time_utc - start_time))
    return conn.execute(
        select(events.c.id, events.c.external_ids)
        .where(
            events.c.sport_id == sport_id,
            events.c.home_team_id == home_team_id,
            events.c.away_team_id == away_team_id,
            ~events.c.external_ids.has_key(lacking_external_key),
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

    match = _find_event_by_teams(
        conn, t, sport_id, home_id, away_id, game.start_time, "mlb_game_pk"
    )
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

    match = _find_event_by_teams(
        conn, t, sport_id, home_id, away_id, ev.commence_time, "the_odds_api_id"
    )
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


def upsert_event_result(
    conn: Connection, t: dict[str, Table], event_id: uuid.UUID, result: GameResult
) -> None:
    """Insert or refresh the final score for one event (backfill/settlement)."""
    event_results = t["event_results"]
    values = {
        "event_id": event_id,
        "home_score": result.home_score,
        "away_score": result.away_score,
        "f5_home_score": result.f5_home_score,
        "f5_away_score": result.f5_away_score,
        "source": "mlb_stats_api",
    }
    conn.execute(
        pg_insert(event_results)
        .values(values)
        .on_conflict_do_update(
            index_elements=["event_id"],
            set_={k: v for k, v in values.items() if k != "event_id"},
        )
    )


def load_team_cache(conn: Connection, t: dict[str, Table], sport_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """All known teams for a sport, keyed by name (backfill hot path)."""
    teams = t["teams"]
    rows = conn.execute(
        select(teams.c.name, teams.c.id).where(teams.c.sport_id == sport_id)
    ).all()
    return {row.name: row.id for row in rows}


def bulk_upsert_schedule_chunk(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    games: list[ScheduledGame],
    results: dict[int, "GameResult"],
    team_cache: dict[str, uuid.UUID],
) -> dict[str, int]:
    """Chunk-sized bulk version of the schedule/results upsert.

    The per-game path costs ~6 round trips per game — fatal against a remote
    Postgres (the first production backfill timed out). This path does a
    handful of statements per chunk with identical semantics:
    pk-first identity, merge-only-into-events-without-pk fallback, results
    upserted with ON CONFLICT.
    """
    events = t["events"]
    counts = {"events_created": 0, "events_refreshed": 0, "results_upserted": 0}
    if not games:
        return counts

    # Suspended games appear under BOTH their original date and their resume
    # date in the schedule feed (same gamePk twice). Keep the LAST listing —
    # the resume-date one carries the final state — or the bulk INSERT would
    # collide with itself on uq_events_mlb_game_pk (production run #13).
    deduped: dict[int, ScheduledGame] = {}
    for game in games:
        deduped[game.game_pk] = game
    games = list(deduped.values())

    all_teams = {g.home_name: g.home_mlb_id for g in games}
    all_teams.update({g.away_name: g.away_mlb_id for g in games})
    for name, mlb_id in all_teams.items():
        if name not in team_cache:
            team_cache[name] = get_or_create_team(conn, t, sport_id, name, mlb_id)

    pk_texts = [str(g.game_pk) for g in games]
    existing = {
        row.pk: row
        for row in conn.execute(
            select(
                events.c.id,
                events.c.external_ids["mlb_game_pk"].astext.label("pk"),
                events.c.start_time_utc,
                events.c.status,
            ).where(events.c.external_ids["mlb_game_pk"].astext.in_(pk_texts))
        )
    }

    # Candidate merge targets: events in the chunk's window WITHOUT an MLB
    # identity (created by the odds job). Rare — only current-season overlap.
    starts = [g.start_time for g in games]
    orphan_rows = conn.execute(
        select(
            events.c.id, events.c.external_ids, events.c.home_team_id,
            events.c.away_team_id, events.c.start_time_utc,
        ).where(
            events.c.sport_id == sport_id,
            ~events.c.external_ids.has_key("mlb_game_pk"),
            events.c.start_time_utc >= min(starts) - EVENT_MATCH_WINDOW,
            events.c.start_time_utc <= max(starts) + EVENT_MATCH_WINDOW,
        )
    ).all()
    orphans = list(orphan_rows)

    event_ids: dict[int, uuid.UUID] = {}
    to_insert: list[dict] = []
    for game in games:
        pk = str(game.game_pk)
        home_id = team_cache[game.home_name]
        away_id = team_cache[game.away_name]
        row = existing.get(pk)
        if row is not None:
            event_ids[game.game_pk] = row.id
            if row.start_time_utc != game.start_time or row.status != game.status:
                conn.execute(
                    update(events)
                    .where(events.c.id == row.id)
                    .values(start_time_utc=game.start_time, status=game.status)
                )
                counts["events_refreshed"] += 1
            continue
        merge = min(
            (
                o for o in orphans
                if o.home_team_id == home_id and o.away_team_id == away_id
                and abs((o.start_time_utc - game.start_time).total_seconds())
                <= EVENT_MATCH_WINDOW.total_seconds()
            ),
            key=lambda o: abs((o.start_time_utc - game.start_time).total_seconds()),
            default=None,
        )
        if merge is not None:
            orphans.remove(merge)
            conn.execute(
                update(events)
                .where(events.c.id == merge.id)
                .values(
                    external_ids={**(merge.external_ids or {}), "mlb_game_pk": pk},
                    start_time_utc=game.start_time,
                    status=game.status,
                )
            )
            event_ids[game.game_pk] = merge.id
            counts["events_refreshed"] += 1
            continue
        to_insert.append(
            {
                "sport_id": sport_id,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "start_time_utc": game.start_time,
                "status": game.status,
                "external_ids": {"mlb_game_pk": pk},
            }
        )

    if to_insert:
        inserted = conn.execute(
            pg_insert(events)
            .values(to_insert)
            .returning(events.c.id, events.c.external_ids["mlb_game_pk"].astext)
        ).all()
        for event_id, pk in inserted:
            event_ids[int(pk)] = event_id
        counts["events_created"] = len(inserted)

    result_rows = [
        {
            "event_id": event_ids[pk],
            "home_score": r.home_score,
            "away_score": r.away_score,
            "f5_home_score": r.f5_home_score,
            "f5_away_score": r.f5_away_score,
            "source": "mlb_stats_api",
        }
        for pk, r in results.items()
        if pk in event_ids
    ]
    if result_rows:
        stmt = pg_insert(t["event_results"]).values(result_rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                col: getattr(stmt.excluded, col)
                for col in ("home_score", "away_score", "f5_home_score", "f5_away_score", "source")
            },
        )
        conn.execute(stmt)
        counts["results_upserted"] = len(result_rows)

    return counts


def load_player_cache(conn: Connection, t: dict[str, Table]) -> dict[int, uuid.UUID]:
    """All known players keyed by MLB person id (pitching backfill hot path)."""
    players = t["players"]
    rows = conn.execute(select(players.c.mlb_person_id, players.c.id)).all()
    return {row.mlb_person_id: row.id for row in rows}


def bulk_upsert_players(
    conn: Connection,
    t: dict[str, Table],
    sport_id: uuid.UUID,
    entries: list[dict],
    player_cache: dict[int, uuid.UUID],
) -> dict[int, uuid.UUID]:
    """Upsert players by MLB person id; returns {mlb_person_id: player_id}.

    ``entries`` dicts carry mlb_person_id, full_name and pitch_hand (may be
    None). A known hand is never clobbered by a payload that omits it:
    the conflict action COALESCEs the incoming hand with the stored one.
    Updates ``player_cache`` in place.
    """
    if not entries:
        return player_cache
    players = t["players"]
    # Dedupe by person id, preferring rows that DO carry a hand.
    deduped: dict[int, dict] = {}
    for e in entries:
        pid = int(e["mlb_person_id"])
        held = deduped.get(pid)
        if held is None or (held.get("pitch_hand") is None and e.get("pitch_hand")):
            deduped[pid] = e
    rows = [
        {
            "sport_id": sport_id,
            "mlb_person_id": pid,
            "full_name": e["full_name"],
            "pitch_hand": e.get("pitch_hand"),
        }
        for pid, e in deduped.items()
    ]
    stmt = pg_insert(players).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["mlb_person_id"],
        set_={
            # Callers fabricate 'MLB person {id}' when a payload omits the
            # name (full_name is NOT NULL); that placeholder must never
            # overwrite a real name already on file.
            "full_name": case(
                (stmt.excluded.full_name.like("MLB person %"), players.c.full_name),
                else_=stmt.excluded.full_name,
            ),
            "pitch_hand": func.coalesce(stmt.excluded.pitch_hand, players.c.pitch_hand),
        },
    ).returning(players.c.mlb_person_id, players.c.id)
    for person_id, player_id in conn.execute(stmt):
        player_cache[person_id] = player_id
    return player_cache


def bulk_upsert_pitching_logs(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    home_team_id: uuid.UUID,
    away_team_id: uuid.UUID,
    lines: list[PitchingLine],
    player_cache: dict[int, uuid.UUID],
) -> int:
    """Upsert one game's pitching lines (DO UPDATE: MLB corrects boxscores)."""
    if not lines:
        return 0
    logs = t["pitching_game_logs"]
    rows = [
        {
            "event_id": event_id,
            "player_id": player_cache[line.mlb_person_id],
            "team_id": home_team_id if line.is_home else away_team_id,
            "is_home": line.is_home,
            "is_starter": line.is_starter,
            "outs_recorded": line.outs_recorded,
            "batters_faced": line.batters_faced,
            "strikeouts": line.strikeouts,
            "walks": line.walks,
            "hit_batsmen": line.hit_batsmen,
            "home_runs": line.home_runs,
            "fly_outs": line.fly_outs,
            "ground_outs": line.ground_outs,
            "sac_flies": line.sac_flies,
            "pitches_thrown": line.pitches_thrown,
            "source": "mlb_stats_api",
        }
        for line in lines
    ]
    stmt = pg_insert(logs).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id", "player_id"],
        set_={
            col: getattr(stmt.excluded, col)
            for col in (
                "team_id", "is_home", "is_starter", "outs_recorded",
                "batters_faced", "strikeouts", "walks", "hit_batsmen",
                "home_runs", "fly_outs", "ground_outs", "sac_flies",
                "pitches_thrown", "source",
            )
        },
    )
    conn.execute(stmt)
    return len(rows)


def bulk_upsert_batting_logs(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    home_team_id: uuid.UUID,
    away_team_id: uuid.UUID,
    lines: list[BattingLine],
    player_cache: dict[int, uuid.UUID],
) -> int:
    """Upsert one game's batting lines (DO UPDATE: MLB corrects boxscores)."""
    if not lines:
        return 0
    logs = t["batting_game_logs"]
    rows = [
        {
            "event_id": event_id,
            "player_id": player_cache[line.mlb_person_id],
            "team_id": home_team_id if line.is_home else away_team_id,
            "is_home": line.is_home,
            "at_bats": line.at_bats,
            "hits": line.hits,
            "doubles": line.doubles,
            "triples": line.triples,
            "home_runs": line.home_runs,
            "walks": line.walks,
            "intentional_walks": line.intentional_walks,
            "strikeouts": line.strikeouts,
            "hit_by_pitch": line.hit_by_pitch,
            "sac_flies": line.sac_flies,
            "sac_bunts": line.sac_bunts,
            "batting_order": line.batting_order,
            "plate_appearances": line.plate_appearances,
            "source": "mlb_stats_api",
        }
        for line in lines
    ]
    stmt = pg_insert(logs).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id", "player_id"],
        set_={
            col: getattr(stmt.excluded, col)
            for col in (
                "team_id", "is_home", "at_bats", "hits", "doubles", "triples",
                "home_runs", "walks", "intentional_walks", "strikeouts",
                "hit_by_pitch", "sac_flies", "sac_bunts", "batting_order",
                "plate_appearances", "source",
            )
        },
    )
    conn.execute(stmt)
    return len(rows)


def load_team_cache_by_mlb_id(
    conn: Connection, t: dict[str, Table], sport_id: uuid.UUID
) -> dict[int, uuid.UUID]:
    """Known teams for a sport keyed by MLB stats id (from external_ids).

    The transactions feed identifies teams by their MLB stats id, not name,
    so from/to team resolution goes through this map (a miss yields NULL —
    audit-only columns, never used by features)."""
    teams = t["teams"]
    rows = conn.execute(
        select(teams.c.id, teams.c.external_ids).where(teams.c.sport_id == sport_id)
    ).all()
    out: dict[int, uuid.UUID] = {}
    for row in rows:
        mlb_id = (row.external_ids or {}).get("mlb_stats_id")
        if mlb_id is not None:
            out[int(mlb_id)] = row.id
    return out


def bulk_upsert_transactions(
    conn: Connection,
    t: dict[str, Table],
    rows: list[PlayerTransaction],
    player_cache: dict[int, uuid.UUID],
    team_by_mlb_id: dict[int, uuid.UUID],
) -> int:
    """Upsert raw player transactions (migration 006, docs/04 §1.5).

    Idempotent by the feed's natural key ``mlb_transaction_id`` (DO UPDATE:
    the feed occasionally re-emits a corrected move). ``player_cache`` must
    already contain every row's player (the job upserts players from the same
    batch first, exactly like sync_lineups). from/to team resolve through
    ``team_by_mlb_id``; a miss is NULL (audit-only, features do not use it).
    """
    if not rows:
        return 0
    txns = t["player_transactions"]
    # Dedupe within the batch by the natural key: a single range call can list
    # the same transaction id once, but ON CONFLICT with duplicate values in
    # one INSERT would error ("cannot affect row a second time").
    deduped: dict[int, PlayerTransaction] = {}
    for r in rows:
        deduped[r.mlb_transaction_id] = r
    values = [
        {
            "mlb_transaction_id": r.mlb_transaction_id,
            "player_id": player_cache[r.mlb_person_id],
            "from_team_id": (
                team_by_mlb_id.get(r.from_team_mlb_id)
                if r.from_team_mlb_id is not None
                else None
            ),
            "to_team_id": (
                team_by_mlb_id.get(r.to_team_mlb_id)
                if r.to_team_mlb_id is not None
                else None
            ),
            "type_code": r.type_code,
            "type_desc": r.type_desc,
            "description": r.description,
            "transaction_date": r.transaction_date,
        }
        for r in deduped.values()
    ]
    stmt = pg_insert(txns).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["mlb_transaction_id"],
        set_={
            col: getattr(stmt.excluded, col)
            for col in (
                "player_id", "from_team_id", "to_team_id", "type_code",
                "type_desc", "description", "transaction_date",
            )
        },
    )
    conn.execute(stmt)
    return len(values)


def record_probables(
    conn: Connection,
    t: dict[str, Table],
    entries: list[dict],
) -> int:
    """Append probables that DIFFER from the currently-recorded one.

    ``entries`` dicts carry event_id, side ('home'/'away'), player_id and
    first_seen_at. Dedupe is against the LATEST recorded probable per
    (event, side) — deliberately NOT a unique constraint on the pitcher:
    a re-announcement (X scratched for Y, then X returns) must insert a
    new X row, or the as-of resolution would answer Y forever. Re-running
    the same slate inserts nothing. Returns how many rows were new.
    """
    if not entries:
        return 0
    probables = t["event_probables"]
    rows = conn.execute(
        select(
            probables.c.event_id,
            probables.c.side,
            probables.c.player_id,
            probables.c.first_seen_at,
        ).where(probables.c.event_id.in_(list({e["event_id"] for e in entries})))
    ).all()
    current: dict[tuple, tuple] = {}
    for row in rows:
        key = (row.event_id, row.side)
        if key not in current or row.first_seen_at > current[key][1]:
            current[key] = (row.player_id, row.first_seen_at)
    to_insert = [
        e
        for e in entries
        if current.get((e["event_id"], e["side"]), (None, None))[0] != e["player_id"]
    ]
    if not to_insert:
        return 0
    conn.execute(probables.insert().values(to_insert))
    return len(to_insert)


def record_lineup(
    conn: Connection,
    t: dict[str, Table],
    event_id: uuid.UUID,
    side: str,
    ordered_pairs: list[tuple[int, uuid.UUID]],
    first_seen_at: datetime,
) -> int:
    """Append a lineup snapshot iff it DIFFERS from the latest recorded one.

    ``ordered_pairs`` is the published starting lineup as (batting_order,
    player_id), one entry per slot. A "snapshot" is the set of rows sharing
    one first_seen_at for (event, side); we insert ALL rows only when the
    (slot, player) set differs from the most recent snapshot — dedupe in the
    store layer, same reasoning as ``record_probables`` (a re-announced
    lineup must be recordable, so no unique constraint forbids it). Re-running
    the same slate with an unchanged lineup inserts nothing. Returns how many
    rows were inserted (0 or len(ordered_pairs)).
    """
    if not ordered_pairs:
        return 0
    lineups = t["event_lineups"]
    existing = conn.execute(
        select(
            lineups.c.batting_order,
            lineups.c.player_id,
            lineups.c.first_seen_at,
        )
        .where(lineups.c.event_id == event_id, lineups.c.side == side)
        .order_by(lineups.c.first_seen_at.desc())
    ).all()
    if existing:
        latest_ts = existing[0].first_seen_at
        latest = {
            (r.batting_order, r.player_id)
            for r in existing
            if r.first_seen_at == latest_ts
        }
        if latest == {(order, pid) for order, pid in ordered_pairs}:
            return 0
    conn.execute(
        lineups.insert().values(
            [
                {
                    "event_id": event_id,
                    "side": side,
                    "batting_order": order,
                    "player_id": pid,
                    "first_seen_at": first_seen_at,
                }
                for order, pid in ordered_pairs
            ]
        )
    )
    return len(ordered_pairs)


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
