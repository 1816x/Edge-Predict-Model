"""Daily MLB schedule sync (F0 cron).

Fetches the slate for one date from the MLB Stats API and upserts teams and
events. Idempotent: re-running refreshes status/start times without
duplicating rows (events are keyed by ``mlb_game_pk``).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import parse_schedule


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
    with engine.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        for game in games:
            _, was_created = store.upsert_event_from_schedule(conn, tables, sport_id, game)
            created += int(was_created)

    return {
        "job": "sync_schedule",
        "date": date_iso,
        "games_in_feed": len(games),
        "events_created": created,
        "events_refreshed": len(games) - created,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Slate date YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()
    print(json.dumps(run(args.date)))
