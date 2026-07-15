"""Pre-game lineup archive cron (F1.3, docs/04 §1.5).

Archives the PUBLISHED starting lineup as-of, so the feature builder can
answer "which batting order was public at decision time" with an honest
``is_confirmed`` flag — the batting-order analogue of ``event_probables``
for starters (§1.3). One boxscore call per pre-game game whose lineup may
be posted; ``event_lineups`` keeps one snapshot per (event, side) and a new
snapshot is appended only when the announced order DIFFERS (store-layer
dedupe, same reasoning as ``record_probables``).

As-of safety: a game whose start time is already past is NEVER archived —
that lineup is no longer "the order known before decision", it is history.
Games further out than ``--lookahead-hours`` are skipped too: their lineup
is not posted yet (MLB posts ~1-4h pre-game), so the boxscore call would
find nothing. The job records nothing until the lineup is actually posted,
which is exactly the honest pre-posted state (the block stays
``is_confirmed`` false / None in production until a snapshot exists).

Degradation contract: with migration 005 not applied yet, the whole job
degrades to a note in the summary instead of failing the cron — same
contract as sync_schedule/backfill for their migrations.

Cost: one schedule-range call plus one boxscore call per pre-game game
inside the lookahead window (MLB Stats API is free; a politeness delay
spaces the boxscore calls). Runs on the same :23/:53 pre-game cadence as
the odds snapshots.

Usage::

    python -m app.jobs.sync_lineups                    # today+tomorrow UTC
    python -m app.jobs.sync_lineups --lookahead-hours 8
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import parse_boxscore_lineup, parse_schedule

_SUMMARY_LIST_CAP = 20


def _capped_append(summary: dict[str, Any], key: str, item: str) -> None:
    if len(summary[key]) < _SUMMARY_LIST_CAP:
        summary[key].append(item)


def run(
    date_iso: str | None = None,
    *,
    lookahead_hours: float = 8.0,
    sleep_seconds: float = 0.25,
    client: MlbClient | None = None,
    engine: Engine | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Archive posted lineups for the current pre-game slate; returns summary."""
    client = client or MlbClient()
    engine = engine or make_engine(get_settings().database_url)
    now = now or datetime.now(timezone.utc)
    start_day = date_iso or now.strftime("%Y-%m-%d")
    # Fetch today AND tomorrow (UTC): a game officially dated today can start
    # just after midnight UTC and a lineup posted late still belongs here.
    end_day = (
        datetime.fromisoformat(start_day).replace(tzinfo=timezone.utc) + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    try:
        tables = store.reflect_tables(engine, store.PITCHING_TABLES)
        tables.update(store.reflect_tables(engine, (store.LINEUP_TABLE,)))
    except InvalidRequestError:
        return {
            "job": "sync_lineups",
            "skipped": "players/event_lineups tables missing; apply migration 005",
        }

    summary: dict[str, Any] = {
        "job": "sync_lineups",
        "as_of": now.isoformat(),
        "lookahead_hours": lookahead_hours,
        "games_pre_game": 0,
        "games_in_window": 0,
        "boxscores_fetched": 0,
        "missing_events_total": 0,
        "missing_events": [],
        "players_upserted": 0,
        "sides_posted": 0,
        "snapshots_new": 0,
        "parse_anomalies_total": 0,
        "parse_anomalies": [],
    }

    games = [
        g
        for g in parse_schedule(client.get_schedule_range(start_day, end_day))
        if g.game_type == "R"
    ]
    horizon = now + timedelta(hours=lookahead_hours)
    # As-of safety: strictly future starts only; lookahead bounds the fetch.
    pending = []
    seen_pks: set[int] = set()
    for game in games:
        if game.game_pk in seen_pks:
            continue
        seen_pks.add(game.game_pk)
        if game.start_time <= now:
            continue
        summary["games_pre_game"] += 1
        if game.start_time <= horizon:
            pending.append(game)
    summary["games_in_window"] = len(pending)
    if not pending:
        return summary

    events_t = tables["events"]
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                events_t.c.id,
                events_t.c.external_ids["mlb_game_pk"].astext.label("pk"),
            ).where(
                events_t.c.external_ids["mlb_game_pk"].astext.in_(
                    [str(g.game_pk) for g in pending]
                )
            )
        ).all()
    event_by_pk = {int(r.pk): r.id for r in rows}

    # (event_id, side) -> ordered [(batting_order, mlb_person_id), ...]
    lineups: list[tuple[Any, str, list[tuple[int, int]]]] = []
    person_entries: dict[int, dict] = {}
    for game in pending:
        event_id = event_by_pk.get(game.game_pk)
        if event_id is None:
            # The event universe belongs to sync_schedule; never invent one.
            summary["missing_events_total"] += 1
            _capped_append(summary, "missing_events", str(game.game_pk))
            continue
        box = parse_boxscore_lineup(client.get_boxscore(game.game_pk))
        summary["boxscores_fetched"] += 1
        for anomaly in box.anomalies:
            summary["parse_anomalies_total"] += 1
            _capped_append(summary, "parse_anomalies", f"{game.game_pk}:{anomaly}")
        for side_key, is_home in (("home", True), ("away", False)):
            side_slots = sorted(
                (s for s in box.slots if s.is_home is is_home),
                key=lambda s: s.batting_order,
            )
            if not side_slots:
                continue  # lineup not posted yet — nothing to archive
            for slot in side_slots:
                person_entries.setdefault(
                    slot.mlb_person_id,
                    {
                        "mlb_person_id": slot.mlb_person_id,
                        "full_name": slot.full_name,
                        "pitch_hand": None,
                    },
                )
            lineups.append(
                (event_id, side_key, [(s.batting_order, s.mlb_person_id) for s in side_slots])
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if lineups:
        with engine.begin() as conn:
            sport_id = store.get_sport_id(conn, tables)
            player_cache = store.load_player_cache(conn, tables)
            store.bulk_upsert_players(
                conn, tables, sport_id, list(person_entries.values()), player_cache
            )
            summary["players_upserted"] = len(person_entries)
            for event_id, side, pairs in lineups:
                summary["sides_posted"] += 1
                if store.record_lineup(
                    conn,
                    tables,
                    event_id,
                    side,
                    [(order, player_cache[pid]) for order, pid in pairs],
                    now,
                ):
                    summary["snapshots_new"] += 1

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Slate date YYYY-MM-DD (default: today UTC)")
    parser.add_argument(
        "--lookahead-hours",
        type=float,
        default=8.0,
        help="Only fetch boxscores for games starting within the next N hours",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.date,
                lookahead_hours=args.lookahead_hours,
                sleep_seconds=args.sleep_seconds,
            )
        )
    )
