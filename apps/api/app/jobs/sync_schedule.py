"""Daily MLB schedule sync (F0 cron).

Fetches the slate for one date from the MLB Stats API and upserts teams and
events. Idempotent: re-running refreshes status/start times without
duplicating rows (events are keyed by ``mlb_game_pk``).

Also archives the PROBABLE starting pitchers the payload already carries
(zero extra HTTP): ``event_probables`` keeps one row per (event, side,
pitcher) with when it was first seen, so the feature builder can answer
"which probable was public at decision time" (docs/04 §1.3 as-of rule).
If migration 003 has not been applied yet, that step degrades to a warning
in the summary instead of failing the cron.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import ScheduledGame, parse_schedule


def _record_probables(
    engine: Engine,
    games: list[ScheduledGame],
    event_ids: dict[int, object],
    now: datetime,
) -> dict[str, Any]:
    """Persist newly-seen probables; returns the summary sub-dict."""
    try:
        tables = store.reflect_tables(engine, store.PITCHING_TABLES)
    except InvalidRequestError:
        return {"skipped": "players/event_probables tables missing; apply migration 003"}

    entries = []
    for game in games:
        for side, pid, name in (
            ("home", game.home_probable_id, game.home_probable),
            ("away", game.away_probable_id, game.away_probable),
        ):
            if pid is not None and game.game_pk in event_ids:
                entries.append((game.game_pk, side, int(pid), name or f"MLB person {pid}"))

    if not entries:
        return {"seen": 0, "new": 0}

    with engine.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        player_cache = store.load_player_cache(conn, tables)
        store.bulk_upsert_players(
            conn,
            tables,
            sport_id,
            [
                {"mlb_person_id": pid, "full_name": name, "pitch_hand": None}
                for _, _, pid, name in entries
            ],
            player_cache,
        )
        new = store.record_probables(
            conn,
            tables,
            [
                {
                    "event_id": event_ids[pk],
                    "side": side,
                    "player_id": player_cache[pid],
                    "first_seen_at": now,
                }
                for pk, side, pid, _ in entries
            ],
        )
    return {"seen": len(entries), "new": new}


def run(
    date_iso: str | None = None,
    *,
    client: MlbClient | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Sync one day's slate; returns a summary dict for logs/daily_scans."""
    client = client or MlbClient()
    engine = engine or make_engine(get_settings().database_url)
    date_iso = date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    games = parse_schedule(client.get_schedule(date_iso))
    tables = store.reflect_tables(engine)
    created = 0
    event_ids: dict[int, object] = {}
    with engine.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        for game in games:
            event_id, was_created = store.upsert_event_from_schedule(
                conn, tables, sport_id, game
            )
            event_ids[game.game_pk] = event_id
            created += int(was_created)

    return {
        "job": "sync_schedule",
        "date": date_iso,
        "games_in_feed": len(games),
        "events_created": created,
        "events_refreshed": len(games) - created,
        "probables": _record_probables(
            engine, games, event_ids, datetime.now(timezone.utc)
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Slate date YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()
    print(json.dumps(run(args.date)))
