"""F0 snapshot-archive audit: coverage per day and pregame gaps per event.

The F0 exit gate is "14 days of snapshots with no holes" (docs/07). GitHub
cron schedules get delayed or skipped under load, so holes are a real
operational risk; this read-only job is the detector PLAN.md §3 points at.

Definitions (aligned with the 4-snapshots/day free-tier cadence):
- An event's pregame window is the 10 hours before its start.
- A HOLE is (a) an event that reaches first pitch with ZERO pregame
  snapshots, or (b) more than ``--max-gap-hours`` between consecutive
  captures from the FIRST capture through the event start. The leading
  edge (window start -> first capture) is not a hole: early games simply
  meet fewer cron slots, and flagging cadence design would bury real
  missed-run signals in noise.
- Only events that already started are audited; a future game's window is
  incomplete by definition.

Usage::

    python -m app.jobs.audit_snapshots                    # last 14 days
    python -m app.jobs.audit_snapshots --start-date 2026-07-08 --max-gap-hours 4
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db.engine import make_engine

PREGAME_WINDOW = timedelta(hours=10)
DEFAULT_MAX_GAP_HOURS = 4.0
DEFAULT_DAYS_BACK = 14
_EVENT_LIST_CAP = 50

_DAYS_SQL = """
SELECT (e.start_time_utc AT TIME ZONE 'UTC')::date AS day,
       count(*) AS events,
       count(*) FILTER (
           WHERE EXISTS (
               SELECT 1 FROM odds_snapshots os
               WHERE os.event_id = e.id AND os.captured_at <= e.start_time_utc
           )
       ) AS events_with_pregame
FROM events e
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport
  AND e.status NOT IN ('postponed', 'cancelled')
  AND (e.start_time_utc AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
GROUP BY 1
ORDER BY 1
"""

_RUNS_SQL = """
SELECT (captured_at AT TIME ZONE 'UTC')::date AS day,
       count(DISTINCT date_trunc('minute', captured_at)) AS capture_runs,
       count(*) AS snapshot_rows
FROM odds_snapshots
WHERE (captured_at AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
GROUP BY 1
ORDER BY 1
"""

_CAPTURES_SQL = """
SELECT e.id AS event_id,
       e.external_ids ->> 'mlb_game_pk' AS pk,
       e.start_time_utc,
       os.captured_at
FROM events e
JOIN sports s ON s.id = e.sport_id
LEFT JOIN odds_snapshots os
       ON os.event_id = e.id
      AND os.captured_at <= e.start_time_utc
      AND os.captured_at >= e.start_time_utc - :window
WHERE s.key = :sport
  AND e.status NOT IN ('postponed', 'cancelled')
  AND (e.start_time_utc AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
  AND e.start_time_utc <= :now
ORDER BY e.start_time_utc, os.captured_at
"""


def find_gaps(
    start_time: datetime,
    captures: list[datetime],
    max_gap: timedelta = timedelta(hours=DEFAULT_MAX_GAP_HOURS),
    window: timedelta = PREGAME_WINDOW,
) -> list[tuple[datetime, datetime]]:
    """Pregame coverage holes for one event (pure, unit-testable).

    Returns (from, to) pairs. Zero captures in the window -> one hole
    spanning the whole window. Otherwise, holes are consecutive pairs more
    than ``max_gap`` apart along [first capture, ..., last capture, start].
    """
    window_start = start_time - window
    caps = sorted({c for c in captures if window_start <= c <= start_time})
    if not caps:
        return [(window_start, start_time)]
    points = [*caps, start_time]
    return [(a, b) for a, b in zip(points, points[1:]) if b - a > max_gap]


def run(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    max_gap_hours: float = DEFAULT_MAX_GAP_HOURS,
    engine: Engine | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    engine = engine or make_engine(get_settings().database_url)
    now = now or datetime.now(timezone.utc)
    end = date.fromisoformat(end_date) if end_date else now.date()
    start = (
        date.fromisoformat(start_date)
        if start_date
        else end - timedelta(days=DEFAULT_DAYS_BACK)
    )
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")
    max_gap = timedelta(hours=max_gap_hours)

    params = {"sport": "mlb", "start": start, "end": end}
    with engine.connect() as conn:
        day_rows = conn.execute(text(_DAYS_SQL), params).mappings().all()
        run_rows = {r["day"]: r for r in conn.execute(text(_RUNS_SQL), params).mappings()}
        capture_rows = conn.execute(
            text(_CAPTURES_SQL), {**params, "window": PREGAME_WINDOW, "now": now}
        ).mappings().all()

    events: dict[Any, dict[str, Any]] = {}
    for row in capture_rows:
        entry = events.setdefault(
            row["event_id"],
            {"pk": row["pk"], "start": row["start_time_utc"], "captures": []},
        )
        if row["captured_at"] is not None:
            entry["captures"].append(row["captured_at"])

    gapped: list[dict[str, Any]] = []
    zero_snapshots = 0
    for entry in events.values():
        gaps = find_gaps(entry["start"], entry["captures"], max_gap)
        if not gaps:
            continue
        if not entry["captures"]:
            zero_snapshots += 1
        if len(gapped) < _EVENT_LIST_CAP:
            gapped.append(
                {
                    "mlb_game_pk": entry["pk"],
                    "start_time_utc": entry["start"].isoformat(),
                    "captures": len(set(entry["captures"])),
                    "gaps": [[a.isoformat(), b.isoformat()] for a, b in gaps],
                }
            )
    events_with_gaps = sum(
        1 for e in events.values() if find_gaps(e["start"], e["captures"], max_gap)
    )

    return {
        "job": "audit_snapshots",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "max_gap_hours": max_gap_hours,
        "days": [
            {
                "day": r["day"].isoformat(),
                "events": r["events"],
                "events_with_pregame": r["events_with_pregame"],
                "capture_runs": (run_rows.get(r["day"]) or {}).get("capture_runs", 0),
                "snapshot_rows": (run_rows.get(r["day"]) or {}).get("snapshot_rows", 0),
            }
            for r in day_rows
        ],
        "events_audited": len(events),
        "events_clean": len(events) - events_with_gaps,
        "events_with_gaps": events_with_gaps,
        "events_zero_snapshots": zero_snapshots,
        "gapped_events": gapped,
    }


def _markdown_summary(result: dict[str, Any]) -> str:
    lines = [
        "",
        f"## Auditoría de snapshots F0 — {result['start_date']} → {result['end_date']}",
        "",
        f"Eventos auditados: {result['events_audited']} · limpios: "
        f"{result['events_clean']} · con huecos (> {result['max_gap_hours']} h): "
        f"{result['events_with_gaps']} · sin ningún snapshot: "
        f"{result['events_zero_snapshots']}",
        "",
        "| Día | Eventos | Con odds pregame | Corridas de captura | Filas |",
        "|---|---|---|---|---|",
    ]
    for day in result["days"]:
        lines.append(
            f"| {day['day']} | {day['events']} | {day['events_with_pregame']} "
            f"| {day['capture_runs']} | {day['snapshot_rows']} |"
        )
    if result["events_with_gaps"]:
        lines.append("")
        lines.append("Huecos (primeros ejemplos):")
        for ev in result["gapped_events"][:10]:
            gaps = "; ".join(f"{a} → {b}" for a, b in ev["gaps"])
            lines.append(f"- pk {ev['mlb_game_pk']} (inicio {ev['start_time_utc']}): {gaps}")
    else:
        lines.append("")
        lines.append("Sin huecos en la ventana auditada. ✔")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="YYYY-MM-DD (default: 14 días atrás)")
    parser.add_argument("--end-date", help="YYYY-MM-DD (default: hoy UTC)")
    parser.add_argument("--max-gap-hours", type=float, default=DEFAULT_MAX_GAP_HOURS)
    parser.add_argument(
        "--fail-on-gaps", action="store_true",
        help="exit 2 si hay huecos: pone en rojo la corrida del cron que audita",
    )
    args = parser.parse_args()
    result = run(
        args.start_date, args.end_date, max_gap_hours=args.max_gap_hours
    )
    print(json.dumps(result))
    print(_markdown_summary(result))
    if args.fail_on_gaps and result["events_with_gaps"] > 0:
        sys.exit(2)
