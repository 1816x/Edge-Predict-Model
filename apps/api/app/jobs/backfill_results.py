"""Historical results backfill (F1 groundwork).

Walks a date range in small chunks against the MLB Stats API schedule
endpoint (free, one request per chunk, linescore hydrated) and upserts
teams, events and final scores — including the First-5-Innings partials
derived from the inning-by-inning linescore.

This is the training corpus for docs/04's models: ~2,430 games per season,
so 2018-2025 lands around 19K events with one call per ~10 days. A
politeness delay keeps the request volume low (docs/02 ToS note).

Usage::

    python -m app.jobs.backfill_results --start-date 2024-03-20 --end-date 2024-10-01
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import parse_schedule, parse_schedule_results


def run(
    start_date: str,
    end_date: str,
    *,
    chunk_days: int = 10,
    sleep_seconds: float = 0.4,
    client: MlbClient | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Backfill [start_date, end_date] inclusive; returns a summary dict."""
    client = client or MlbClient()
    engine = engine or make_engine(get_settings().database_url)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")

    tables = store.reflect_tables(engine)
    summary: dict[str, Any] = {
        "job": "backfill_results",
        "start_date": start_date,
        "end_date": end_date,
        "chunks": 0,
        "games_in_feed": 0,
        "finals_with_score": 0,
        "results_upserted": 0,
        "f5_missing": 0,
    }

    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
        payload = client.get_schedule_range(chunk_start.isoformat(), chunk_end.isoformat())
        games = parse_schedule(payload)
        results = {r.game_pk: r for r in parse_schedule_results(payload)}

        with engine.begin() as conn:
            sport_id = store.get_sport_id(conn, tables)
            for game in games:
                event_id, _ = store.upsert_event_from_schedule(conn, tables, sport_id, game)
                result = results.get(game.game_pk)
                if result is not None:
                    store.upsert_event_result(conn, tables, event_id, result)
                    summary["results_upserted"] += 1
                    summary["f5_missing"] += int(result.f5_home_score is None)

        summary["chunks"] += 1
        summary["games_in_feed"] += len(games)
        summary["finals_with_score"] += len(results)
        chunk_start = chunk_end + timedelta(days=1)
        if chunk_start <= end and sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--chunk-days", type=int, default=10)
    parser.add_argument("--sleep-seconds", type=float, default=0.4)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.start_date,
                args.end_date,
                chunk_days=args.chunk_days,
                sleep_seconds=args.sleep_seconds,
            )
        )
    )
