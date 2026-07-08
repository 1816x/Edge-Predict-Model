"""Odds snapshot cron (F0): archive the current MLB lines.

Appends one immutable row per (event, book, market, side) to
``odds_snapshots``. Events resolve to the schedule by The Odds API id, then
by team pair + closest start time (doubleheader-safe); unmatched events are
created and merged later by the schedule sync.

Pregame archive only: events whose start time is already past are skipped
(in-play lines are out of the MVP's scope, docs/01).

With ``--closing-window-min N``, rows for events starting within the next N
minutes are flagged ``is_closing`` — the partial unique index keeps at most
one closing row per outcome, so re-runs are safe.

Archiving own snapshots from day 1 is the F0 exit criterion: it is what
makes an honest backtest possible later (docs/06).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.odds_client import OddsClient
from app.ingestion.parsers import parse_odds_event


def run(
    *,
    closing_window_min: int | None = None,
    client: OddsClient | None = None,
    engine: Engine | None = None,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot the current slate's odds; returns a summary dict."""
    client = client or OddsClient()
    engine = engine or make_engine(get_settings().database_url)
    captured_at = captured_at or datetime.now(timezone.utc)

    payload = client.get_mlb_odds()
    tables = store.reflect_tables(engine)
    summary: dict[str, Any] = {
        "job": "snapshot_odds",
        "captured_at": captured_at.isoformat(),
        "events_in_feed": len(payload),
        "events_matched": 0,
        "events_created": 0,
        "events_started_skipped": 0,
        "snapshots_inserted": 0,
        "outcomes_skipped": [],
    }

    with engine.begin() as conn:
        sport_id = store.get_sport_id(conn, tables)
        for raw_event in payload:
            ev = parse_odds_event(raw_event)
            summary["outcomes_skipped"].extend(ev.skipped)
            if ev.commence_time <= captured_at:
                summary["events_started_skipped"] += 1
                continue
            if not ev.outcomes:
                continue
            event_id, created = store.find_or_create_event_for_odds(
                conn, tables, sport_id, ev
            )
            summary["events_created" if created else "events_matched"] += 1
            is_closing = (
                closing_window_min is not None
                and ev.commence_time <= captured_at + timedelta(minutes=closing_window_min)
            )
            summary["snapshots_inserted"] += store.insert_odds_snapshots(
                conn, tables, event_id, ev.outcomes, captured_at, is_closing
            )

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--closing-window-min",
        type=int,
        default=None,
        help="Flag is_closing on events starting within the next N minutes",
    )
    args = parser.parse_args()
    print(json.dumps(run(closing_window_min=args.closing_window_min)))
