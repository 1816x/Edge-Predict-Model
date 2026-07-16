"""Player transactions / IL backfill + daily incremental (docs/04 §1.5).

Walks a date range against the MLB Stats API /transactions feed and archives
every move RAW into ``player_transactions`` (migration 006). The "on IL as-of
date D" state is a replay computed later in the feature layer
(``app/features/transactions.py``); this job only stores evidence. With NO
arguments it processes yesterday (UTC): the daily cron and the historical
backfill are the same idempotent job.

Idempotent by the feed's natural key ``mlb_transaction_id`` (ON CONFLICT DO
UPDATE) — re-running a range is a no-op (or corrects a re-emitted move), so
there is no per-row skip-existing to maintain, unlike the boxscore backfill.
The endpoint is range-based (no per-game fanout), so chunks can be large: one
call covers many days of transactions.

Degradation contract: with migration 006 not applied yet, the whole job
degrades to a note in the summary instead of failing the cron — same contract
as sync_lineups/backfill for their migrations.

Drift canary: MLB Stats API has no official docs, and the IL text is free-form
(and was "disabled list" before 2019). The summary reports the distinct
``type_desc`` seen and, crucially, ``il_desc_unclassified`` — rows whose text
mentions the IL/DL but that ``il_effect`` could not classify. A non-zero count
means the feed drifted and the versioned classifier needs a look BEFORE trusting
the star_out_flag it feeds.

Usage::

    python -m app.jobs.sync_transactions                       # yesterday UTC
    python -m app.jobs.sync_transactions --start-date 2018-03-01 --end-date 2026-07-15
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError

from app.config import get_settings
from app.db.engine import make_engine
from app.features.transactions import il_effect, mentions_il
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import parse_transactions

_SUMMARY_LIST_CAP = 20


def _yesterday_utc() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _capped_append(summary: dict[str, Any], key: str, item: str) -> None:
    if len(summary[key]) < _SUMMARY_LIST_CAP:
        summary[key].append(item)


def run(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    chunk_days: int = 45,
    sleep_seconds: float = 0.25,
    client: MlbClient | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Archive raw transactions for [start_date, end_date]; default yesterday."""
    client = client or MlbClient()
    engine = engine or make_engine(get_settings().database_url)
    start = date.fromisoformat(start_date or _yesterday_utc())
    end = date.fromisoformat(end_date or _yesterday_utc())
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")

    try:
        tables = store.reflect_tables(engine, ("sports", "teams", "players"))
        tables.update(store.reflect_tables(engine, (store.TRANSACTIONS_TABLE,)))
    except InvalidRequestError:
        return {
            "job": "sync_transactions",
            "skipped": "player_transactions table missing; apply migration 006",
        }

    summary: dict[str, Any] = {
        "job": "sync_transactions",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "chunks": 0,
        "transactions_seen": 0,
        "transactions_upserted": 0,
        "players_upserted": 0,
        "il_placements": 0,
        "il_activations": 0,
        "parse_anomalies_total": 0,
        "parse_anomalies": [],
        "type_desc_distinct": [],
        "il_desc_unclassified_total": 0,
        "il_desc_unclassified": [],
    }
    type_descs: set[str] = set()

    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
        payload = client.get_transactions(chunk_start.isoformat(), chunk_end.isoformat())
        batch = parse_transactions(payload)
        summary["transactions_seen"] += len(batch.rows)
        for anomaly in batch.anomalies:
            summary["parse_anomalies_total"] += 1
            _capped_append(summary, "parse_anomalies", anomaly)

        for row in batch.rows:
            if row.type_desc:
                type_descs.add(row.type_desc)
            effect = il_effect(row.type_code, row.type_desc, row.description)
            if effect == 1:
                summary["il_placements"] += 1
            elif effect == -1:
                summary["il_activations"] += 1
            elif mentions_il(row.type_desc, row.description):
                # Names the IL/DL but no verb matched: feed drift to eyeball.
                summary["il_desc_unclassified_total"] += 1
                _capped_append(
                    summary,
                    "il_desc_unclassified",
                    f"{row.mlb_transaction_id}:{(row.description or row.type_desc or '')[:80]}",
                )

        if batch.rows:
            with engine.begin() as conn:
                sport_id = store.get_sport_id(conn, tables)
                player_cache = store.load_player_cache(conn, tables)
                store.bulk_upsert_players(
                    conn, tables, sport_id,
                    [
                        {
                            "mlb_person_id": r.mlb_person_id,
                            "full_name": r.full_name,
                            "pitch_hand": None,
                        }
                        for r in batch.rows
                    ],
                    player_cache,
                )
                summary["players_upserted"] += len({r.mlb_person_id for r in batch.rows})
                team_by_mlb_id = store.load_team_cache_by_mlb_id(conn, tables, sport_id)
                summary["transactions_upserted"] += store.bulk_upsert_transactions(
                    conn, tables, list(batch.rows), player_cache, team_by_mlb_id
                )

        summary["chunks"] += 1
        chunk_start = chunk_end + timedelta(days=1)
        if chunk_start <= end and sleep_seconds > 0:
            time.sleep(sleep_seconds)

    summary["type_desc_distinct"] = sorted(type_descs)[:_SUMMARY_LIST_CAP]
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    parser.add_argument("--end-date", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    parser.add_argument("--chunk-days", type=int, default=45)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
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
