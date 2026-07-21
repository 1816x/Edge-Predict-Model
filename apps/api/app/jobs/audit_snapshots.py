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

RED (exit 2 under ``--fail-on-gaps``) means "action required", so zero-
snapshot events are CLASSIFIED before failing (``audit_is_red``):
- capture_miss — RED. Either the event has snapshots in history but none
  inside the window (we stopped capturing a priced game), or NO capture run
  fired anywhere during its pregame window (we never looked, so "the market
  didn't price it" cannot be claimed — the file-alive rule: a zero is only
  true when the archive was demonstrably alive).
- unpriced — zero snapshots ever AND at least one capture run landed inside
  the event's window: the archive was alive and the feed did not carry the
  game (some doubleheader game 2s never get a line). Not red — nothing on
  our side needs action. Reported distinctly so coverage stays honest.
- orphan_events — started events carrying only ``the_odds_api_id`` (no
  ``mlb_game_pk``): odds-identity fragmentation (id reissue / team-name
  drift). Their snapshots are invisible to every mlb_game_pk join, so this
  is RED — the audit is the regression detector for that bug class. NOTE:
  detection is windowed to the audited range, so the daily cron runs the
  audit with the same 3-day lookback as the backfills (a skipped cron day
  still gets checked twice more).

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

# events_with_pregame uses the SAME 10h window as the per-event gap logic:
# with an unbounded lower edge the summary contradicts itself (an event whose
# only snapshot landed 15h early counted as "with pregame" while the gap
# audit flagged it as zero-snapshot — the 2026-07-19/20 red runs).
_DAYS_SQL = """
SELECT (e.start_time_utc AT TIME ZONE 'UTC')::date AS day,
       count(*) AS events,
       count(*) FILTER (
           WHERE EXISTS (
               SELECT 1 FROM odds_snapshots os
               WHERE os.event_id = e.id
                 AND os.captured_at <= e.start_time_utc
                 AND os.captured_at >= e.start_time_utc - :window
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
SELECT (os.captured_at AT TIME ZONE 'UTC')::date AS day,
       count(DISTINCT date_trunc('minute', os.captured_at)) AS capture_runs,
       count(*) AS snapshot_rows
FROM odds_snapshots os
JOIN events e ON e.id = os.event_id
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport
  AND (os.captured_at AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
GROUP BY 1
ORDER BY 1
"""

# Liveness ledger for the file-alive rule: every distinct capture instant
# (minute-truncated cron firing) near the audited range. An event's pregame
# window can start on the previous UTC day (window is 10h), hence the 1-day
# pad on the lower bound.
_INSTANTS_SQL = """
SELECT DISTINCT date_trunc('minute', os.captured_at) AS instant
FROM odds_snapshots os
JOIN events e ON e.id = os.event_id
JOIN sports s ON s.id = e.sport_id
WHERE s.key = :sport
  AND os.captured_at >= CAST(:start AS date) - INTERVAL '1 day'
  AND os.captured_at < CAST(:end AS date) + INTERVAL '2 day'
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

# Unpriced-vs-miss discriminator: audited events with at least one snapshot
# ANYWHERE in history (no window). Zero-in-window + present here = a real
# capture miss; zero-in-window + absent here = the market never priced it.
_EVER_PRICED_SQL = """
SELECT DISTINCT e.id AS event_id
FROM events e
JOIN sports s ON s.id = e.sport_id
JOIN odds_snapshots os ON os.event_id = e.id
WHERE s.key = :sport
  AND e.status NOT IN ('postponed', 'cancelled')
  AND (e.start_time_utc AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
  AND e.start_time_utc <= :now
"""

# Identity-fragmentation detector: a STARTED event still carrying only the
# odds id means the odds feed's game never reconciled with the MLB schedule
# (id reissue or team-name drift) — its snapshots are invisible to every
# mlb_game_pk join. sync_schedule runs before every snapshot, so in a healthy
# pipeline this set is empty.
_ORPHANS_SQL = """
SELECT e.id AS event_id,
       e.external_ids ->> 'the_odds_api_id' AS odds_api_id,
       e.start_time_utc,
       ht.name AS home,
       at.name AS away
FROM events e
JOIN sports s ON s.id = e.sport_id
JOIN teams ht ON ht.id = e.home_team_id
JOIN teams at ON at.id = e.away_team_id
WHERE s.key = :sport
  AND e.external_ids ? 'the_odds_api_id'
  AND NOT (e.external_ids ? 'mlb_game_pk')
  AND e.status NOT IN ('postponed', 'cancelled')
  AND (e.start_time_utc AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
  AND e.start_time_utc <= :now
ORDER BY e.start_time_utc
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


def audit_is_red(result: dict[str, Any]) -> bool:
    """RED = action required (pure, unit-testable; see module docstring).

    - a priced event with an intra-window gap, or
    - a capture miss (priced-but-unwindowed, or zero snapshots with no
      capture run alive in the event's window — a total outage lands here
      because every affected event fails the liveness test), or
    - identity fragmentation (orphan odds-only events).
    An event the market never priced (``events_unpriced``, liveness-proven)
    is reported but never fails the run by itself.
    """
    if result["events_gapped_with_captures"] > 0:
        return True
    if result["events_capture_miss"] > 0:
        return True
    return bool(result["orphan_events"])


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
        day_rows = conn.execute(
            text(_DAYS_SQL), {**params, "window": PREGAME_WINDOW}
        ).mappings().all()
        run_rows = {r["day"]: r for r in conn.execute(text(_RUNS_SQL), params).mappings()}
        capture_rows = conn.execute(
            text(_CAPTURES_SQL), {**params, "window": PREGAME_WINDOW, "now": now}
        ).mappings().all()
        ever_priced = {
            r["event_id"]
            for r in conn.execute(text(_EVER_PRICED_SQL), {**params, "now": now}).mappings()
        }
        instants = [
            r["instant"] for r in conn.execute(text(_INSTANTS_SQL), params).mappings()
        ]
        orphan_rows = conn.execute(
            text(_ORPHANS_SQL), {**params, "now": now}
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
    zero_snapshots = capture_miss = unpriced = gapped_with_captures = 0
    for event_id, entry in events.items():
        gaps = find_gaps(entry["start"], entry["captures"], max_gap)
        if not gaps:
            continue
        if entry["captures"]:
            kind = "gap"
            gapped_with_captures += 1
        else:
            zero_snapshots += 1
            window_start = entry["start"] - PREGAME_WINDOW
            file_alive = any(
                window_start <= i <= entry["start"] for i in instants
            )
            if event_id in ever_priced or not file_alive:
                # Priced-but-unwindowed, OR nobody ever looked during its
                # window — either way "unpriced" cannot be claimed.
                kind = "capture_miss"
                capture_miss += 1
            else:
                kind = "unpriced"
                unpriced += 1
        if len(gapped) < _EVENT_LIST_CAP:
            gapped.append(
                {
                    "mlb_game_pk": entry["pk"],
                    "start_time_utc": entry["start"].isoformat(),
                    "captures": len(set(entry["captures"])),
                    "kind": kind,
                    "gaps": [[a.isoformat(), b.isoformat()] for a, b in gaps],
                }
            )
    events_with_gaps = gapped_with_captures + zero_snapshots

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
        "events_gapped_with_captures": gapped_with_captures,
        "events_zero_snapshots": zero_snapshots,
        "events_capture_miss": capture_miss,
        "events_unpriced": unpriced,
        "gapped_events": gapped,
        "orphan_events": [
            {
                "the_odds_api_id": r["odds_api_id"],
                "start_time_utc": r["start_time_utc"].isoformat(),
                "home": r["home"],
                "away": r["away"],
            }
            for r in orphan_rows
        ],
    }


def _markdown_summary(result: dict[str, Any]) -> str:
    lines = [
        "",
        f"## Auditoría de snapshots F0 — {result['start_date']} → {result['end_date']}",
        "",
        f"Eventos auditados: {result['events_audited']} · limpios: "
        f"{result['events_clean']} · con huecos (> {result['max_gap_hours']} h): "
        f"{result['events_with_gaps']} · capturas perdidas: "
        f"{result['events_capture_miss']} · sin línea en el mercado: "
        f"{result['events_unpriced']} · huérfanos de identidad: "
        f"{len(result['orphan_events'])}",
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
            lines.append(
                f"- pk {ev['mlb_game_pk']} [{ev['kind']}] "
                f"(inicio {ev['start_time_utc']}): {gaps}"
            )
    if result["orphan_events"]:
        lines.append("")
        lines.append("Huérfanos de identidad (odds sin mlb_game_pk — fragmentación):")
        for ev in result["orphan_events"][:10]:
            lines.append(
                f"- {ev['away']} @ {ev['home']} (inicio {ev['start_time_utc']}, "
                f"odds id {ev['the_odds_api_id']})"
            )
        if result["events_unpriced"]:
            lines.append(
                "Ojo: con huérfanos presentes, la etiqueta 'sin línea' no es "
                "confiable — las líneas del gemelo pueden vivir en el huérfano."
            )
    if not audit_is_red(result):
        lines.append("")
        lines.append("Sin acción requerida en la ventana auditada. ✔")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="YYYY-MM-DD (default: 14 días atrás)")
    parser.add_argument("--end-date", help="YYYY-MM-DD (default: hoy UTC)")
    parser.add_argument("--max-gap-hours", type=float, default=DEFAULT_MAX_GAP_HOURS)
    parser.add_argument(
        "--fail-on-gaps", action="store_true",
        help="exit 2 si hay acción requerida (audit_is_red): capturas "
        "perdidas, huecos intra-ventana, huérfanos o apagón total del día",
    )
    args = parser.parse_args()
    result = run(
        args.start_date, args.end_date, max_gap_hours=args.max_gap_hours
    )
    print(json.dumps(result))
    print(_markdown_summary(result))
    if args.fail_on_gaps and audit_is_red(result):
        sys.exit(2)
