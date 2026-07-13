"""Full-boxscore backfill + daily incremental: pitching AND batting lines.

Walks a date range against the MLB schedule, fetches ONE boxscore per
finished regular-season game and upserts players, pitching lines
(``infra/migrations/003``, F1 starter/bullpen blocks) and batting lines
(``infra/migrations/004``, F1.2 offense block). With NO arguments it
processes yesterday (UTC): the daily cron and the historical backfill are
the same idempotent job. The module keeps its historical name — the
workflow dispatch option and years of run logs point here.

Resume-cheap by design, PER TABLE: an event is pending if it is missing
pitching logs OR batting logs, and only the missing half is parsed and
upserted from the (single) fetch. That is what makes the historical
batting fill cheap to express: re-running a season that already has
pitching re-fetches each boxscore once and writes only batting.
``--force`` re-fetches everything in range and replaces both tables'
rows (boxscore corrections can REMOVE lines; upserts alone never
converge on that).

Degradation contract: with migration 004 not applied yet, the batting
half is skipped with ``batting_note`` and pitching ingestion continues
untouched — the daily cron must warn, never paint the whole run red.

Cost: one schedule call per chunk plus one boxscore call per pending game
(~2,430/season, ~20-25 min per season with the politeness delay — fits the
workflow's 60-minute timeout one season at a time). Pitch hands come from
batched ``/api/v1/people`` lookups, only for players who actually PITCHED
and whose hand is still unknown — position players never trigger people
lookups (their batting side does not care about pitch hand).

Usage::

    python -m app.jobs.backfill_pitching                       # yesterday
    python -m app.jobs.backfill_pitching --start-date 2024-03-20 --end-date 2024-09-30
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion import store
from app.ingestion.mlb_client import MlbClient
from app.ingestion.parsers import (
    parse_boxscore_batting,
    parse_boxscore_pitching,
    parse_schedule,
)

# Cap the anomaly/missing lists carried in the JSON summary; totals are
# always exact, the lists are just the first examples for a human eye.
_SUMMARY_LIST_CAP = 20
_PEOPLE_BATCH = 100


def _yesterday_utc() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _capped_append(summary: dict[str, Any], key: str, item: str) -> None:
    if len(summary[key]) < _SUMMARY_LIST_CAP:
        summary[key].append(item)


def _resolve_hands(
    client: MlbClient,
    person_ids: list[int],
) -> dict[int, str]:
    """Pitch hand per person id from /api/v1/people, batched."""
    hands: dict[int, str] = {}
    for i in range(0, len(person_ids), _PEOPLE_BATCH):
        batch = person_ids[i : i + _PEOPLE_BATCH]
        payload = client.get_people(batch)
        for person in payload.get("people", []):
            code = ((person.get("pitchHand") or {}).get("code")) or None
            if person.get("id") is not None and code in ("L", "R", "S"):
                hands[int(person["id"])] = code
    return hands


def run(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    chunk_days: int = 7,
    sleep_seconds: float = 0.25,
    force: bool = False,
    client: MlbClient | None = None,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Ingest pitching logs for [start_date, end_date]; defaults: yesterday."""
    client = client or MlbClient()
    engine = engine or make_engine(get_settings().database_url)
    start = date.fromisoformat(start_date or _yesterday_utc())
    end = date.fromisoformat(end_date or _yesterday_utc())
    if end < start:
        raise ValueError(f"end_date {end} is before start_date {start}")

    try:
        tables = store.reflect_tables(engine, store.PITCHING_TABLES)
    except InvalidRequestError:
        # Same degradation contract as sync_schedule: in the window between
        # merging code and applying migration 003, the daily cron must warn,
        # not paint every run red.
        return {
            "job": "backfill_pitching",
            "skipped": "players/pitching tables missing; apply migration 003",
        }
    summary: dict[str, Any] = {
        "job": "backfill_pitching",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "chunks": 0,
        "games_final_regular": 0,
        "events_skipped_existing": 0,
        "boxscores_fetched": 0,
        "players_upserted": 0,
        "lines_upserted": 0,
        "hands_backfilled": 0,
        "missing_events_total": 0,
        "missing_events": [],
        "starter_anomalies_total": 0,
        "starter_anomalies": [],
        "parse_anomalies_total": 0,
        "parse_anomalies": [],
        "null_fly_outs": 0,
        "batting_lines_upserted": 0,
        "batting_zero_pa_skipped": 0,
        "batting_anomalies_total": 0,
        "batting_anomalies": [],
    }

    # Batting is reflected separately (migration 004): pre-004 the batting
    # half degrades to a note while pitching ingestion continues untouched.
    batting_enabled = True
    try:
        tables.update(store.reflect_tables(engine, (store.BATTING_TABLE,)))
    except InvalidRequestError:
        batting_enabled = False
        summary["batting_note"] = (
            "batting_game_logs missing; apply migration 004 "
            "(batting lines skipped this run)"
        )

    events_t = tables["events"]
    logs_t = tables["pitching_game_logs"]
    with engine.connect() as conn:
        player_cache = store.load_player_cache(conn, tables)
        known_hands = {
            row.mlb_person_id
            for row in conn.execute(
                text("SELECT mlb_person_id FROM players WHERE pitch_hand IS NOT NULL")
            )
        }

    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
        payload = client.get_schedule_range(chunk_start.isoformat(), chunk_end.isoformat())
        games = [
            g
            for g in parse_schedule(payload)
            if g.game_type == "R" and g.status == "final"
        ]
        # Suspended games list twice (original + resume date); keep the last.
        deduped = {g.game_pk: g for g in games}
        games = list(deduped.values())
        summary["games_final_regular"] += len(games)

        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    events_t.c.id,
                    events_t.c.home_team_id,
                    events_t.c.away_team_id,
                    events_t.c.external_ids["mlb_game_pk"].astext.label("pk"),
                ).where(
                    events_t.c.external_ids["mlb_game_pk"].astext.in_(
                        [str(g.game_pk) for g in games]
                    )
                )
            ).all()
            event_by_pk = {int(r.pk): r for r in rows}
            have_logs: set = set()
            have_batting: set = set()
            if event_by_pk and not force:
                event_ids = [r.id for r in event_by_pk.values()]
                have_logs = {
                    r.event_id
                    for r in conn.execute(
                        select(logs_t.c.event_id)
                        .where(logs_t.c.event_id.in_(event_ids))
                        .distinct()
                    )
                }
                if batting_enabled:
                    bat_t = tables["batting_game_logs"]
                    have_batting = {
                        r.event_id
                        for r in conn.execute(
                            select(bat_t.c.event_id)
                            .where(bat_t.c.event_id.in_(event_ids))
                            .distinct()
                        )
                    }

        # An event is pending if EITHER half is missing; the single fetch
        # then feeds only the missing half (that is what makes the
        # historical batting fill cheap over events that already have
        # pitching). Without migration 004 the batting half never counts.
        pending = []
        for game in games:
            event = event_by_pk.get(game.game_pk)
            if event is None:
                # The event universe belongs to backfill_results/sync — this
                # job never invents events from a boxscore.
                summary["missing_events_total"] += 1
                _capped_append(summary, "missing_events", str(game.game_pk))
                continue
            need_pitch = event.id not in have_logs
            need_bat = batting_enabled and event.id not in have_batting
            if not need_pitch and not need_bat:
                summary["events_skipped_existing"] += 1
                continue
            pending.append((game, event, need_pitch, need_bat))

        chunk_lines: list[tuple[Any, list]] = []  # (event row, pitching lines)
        chunk_bat_lines: list[tuple[Any, list]] = []  # (event row, batting lines)
        player_entries: dict[int, dict] = {}
        pitcher_ids: set[int] = set()
        for game, event, need_pitch, need_bat in pending:
            payload = client.get_boxscore(game.game_pk)
            summary["boxscores_fetched"] += 1
            if need_pitch:
                box = parse_boxscore_pitching(payload)
                for anomaly in box.anomalies:
                    summary["parse_anomalies_total"] += 1
                    _capped_append(summary, "parse_anomalies", f"{game.game_pk}:{anomaly}")
                for side_is_home in (True, False):
                    side_lines = [l for l in box.lines if l.is_home is side_is_home]
                    if side_lines and not any(l.is_starter for l in side_lines):
                        summary["starter_anomalies_total"] += 1
                        _capped_append(
                            summary,
                            "starter_anomalies",
                            f"{game.game_pk}:{'home' if side_is_home else 'away'}",
                        )
                summary["null_fly_outs"] += sum(1 for l in box.lines if l.fly_outs is None)
                for line in box.lines:
                    pitcher_ids.add(line.mlb_person_id)
                    held = player_entries.get(line.mlb_person_id)
                    if held is None or (held["pitch_hand"] is None and line.pitch_hand):
                        player_entries[line.mlb_person_id] = {
                            "mlb_person_id": line.mlb_person_id,
                            "full_name": line.full_name,
                            "pitch_hand": line.pitch_hand,
                        }
                if box.lines:
                    chunk_lines.append((event, list(box.lines)))
            if need_bat:
                bat = parse_boxscore_batting(payload)
                for anomaly in bat.anomalies:
                    summary["batting_anomalies_total"] += 1
                    _capped_append(summary, "batting_anomalies", f"{game.game_pk}:{anomaly}")
                summary["batting_zero_pa_skipped"] += bat.zero_pa_skipped
                for line in bat.lines:
                    # Batters never clobber a pitcher entry (two-way players
                    # keep their hand) and never enter the /people lookups.
                    if line.mlb_person_id not in player_entries:
                        player_entries[line.mlb_person_id] = {
                            "mlb_person_id": line.mlb_person_id,
                            "full_name": line.full_name,
                            "pitch_hand": None,
                        }
                if bat.lines:
                    chunk_bat_lines.append((event, list(bat.lines)))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        # Boxscores omit pitch hands; resolve the still-unknown ones in
        # batched people lookups so features can use sp_is_lhp. ONLY for
        # players who pitched: position players would multiply the lookups
        # by ~15x for a field no batting feature reads.
        need_hands = [
            pid
            for pid, e in sorted(player_entries.items())
            if e["pitch_hand"] is None and pid in pitcher_ids and pid not in known_hands
        ]
        if need_hands:
            for pid, hand in _resolve_hands(client, need_hands).items():
                # /people can return canonicalized ids that were never asked
                # for (merged person records); only take what we track.
                if pid in player_entries:
                    player_entries[pid]["pitch_hand"] = hand
                    summary["hands_backfilled"] += 1

        if chunk_lines or chunk_bat_lines:
            with engine.begin() as conn:
                if force:
                    # A corrected boxscore can REMOVE a line (wrongly credited
                    # pitcher/batter); upserts alone never converge on that,
                    # so a forced re-fetch replaces each game's rows wholesale.
                    if chunk_lines:
                        conn.execute(
                            logs_t.delete().where(
                                logs_t.c.event_id.in_([e.id for e, _ in chunk_lines])
                            )
                        )
                    if chunk_bat_lines:
                        bat_t = tables["batting_game_logs"]
                        conn.execute(
                            bat_t.delete().where(
                                bat_t.c.event_id.in_([e.id for e, _ in chunk_bat_lines])
                            )
                        )
                store.bulk_upsert_players(
                    conn, tables, store.get_sport_id(conn, tables),
                    list(player_entries.values()), player_cache,
                )
                summary["players_upserted"] += len(player_entries)
                for pid in player_entries:
                    if player_entries[pid]["pitch_hand"] is not None:
                        known_hands.add(pid)
                for event, lines in chunk_lines:
                    summary["lines_upserted"] += store.bulk_upsert_pitching_logs(
                        conn, tables, event.id, event.home_team_id,
                        event.away_team_id, lines, player_cache,
                    )
                for event, lines in chunk_bat_lines:
                    summary["batting_lines_upserted"] += store.bulk_upsert_batting_logs(
                        conn, tables, event.id, event.home_team_id,
                        event.away_team_id, lines, player_cache,
                    )

        summary["chunks"] += 1
        chunk_start = chunk_end + timedelta(days=1)
        if chunk_start <= end and sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    parser.add_argument("--end-date", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument(
        "--force", action="store_true",
        help="re-fetch even events that already have pitching logs",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.start_date,
                args.end_date,
                chunk_days=args.chunk_days,
                sleep_seconds=args.sleep_seconds,
                force=args.force,
            )
        )
    )
