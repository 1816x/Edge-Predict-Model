"""One-shot repair: re-home odds snapshots stranded on orphan duplicate events.

Before the tier-2.5 re-stamp fix, a reissued The Odds API event id spawned a
DUPLICATE event row (odds id only, no ``mlb_game_pk``) and every later capture
landed there — fragmenting the game's odds history across two rows (production
bug, 2026-07-18/19). The ingest fix stops NEW orphans; this job reconnects the
history already written.

For each orphan (has ``the_odds_api_id``, lacks ``mlb_game_pk``) with EXACTLY
one sibling — an ``mlb_game_pk`` event, same team pair, start within
``RESTAMP_WINDOW`` — it repoints the orphan's ``odds_snapshots`` to the
sibling, transfers the (current) odds id, and deletes the emptied orphan.
Orphans with zero siblings (the All-Star exhibition; the 2026-07-09
Orioles–Cubs feed listing whose commence_time drifted ~5 h from the MLB start)
or two-plus siblings (identical-start doubleheader) are SKIPPED and reported —
never guessed at.

``odds_snapshots`` is append-only by design (``trg_odds_snapshots_immutable``);
the repoint requires disabling that trigger. Everything — disable, repairs,
re-enable — runs in ONE transaction: any failure rolls the whole thing back,
so the trigger can never be left off. Prices, timestamps and books are never
modified; only which event a row points at is corrected. The single mutation
of snapshot CONTENT is demoting an orphan's ``is_closing`` flag when the
sibling already holds a closing row for the same (book, market, side) — the
partial unique index allows only one, and the sibling's is the true closing
line; the orphan's price row survives as non-closing evidence.

Idempotent: a second run finds no repairable orphans and is a no-op. Safe to
re-dispatch. ``--dry-run`` reports what would change without writing.

Requires the ``DATABASE_URL`` role to own ``odds_snapshots`` (ALTER TABLE).

Usage::

    python -m app.jobs.repair_orphan_events --dry-run
    python -m app.jobs.repair_orphan_events
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.config import get_settings
from app.db.engine import make_engine
from app.ingestion.store import RESTAMP_WINDOW

# Orphan = event the odds feed created that never reconciled with the MLB
# schedule. Sibling candidates use the same discriminator as the tier-2.5
# re-stamp (team pair + RESTAMP_WINDOW): repairing is retroactive re-stamping.
_PAIRS_SQL = """
SELECT o.id AS orphan_id,
       o.external_ids ->> 'the_odds_api_id' AS odds_api_id,
       o.start_time_utc AS orphan_start,
       sibs.ids AS sibling_ids
FROM events o
CROSS JOIN LATERAL (
    SELECT COALESCE(array_agg(s.id ORDER BY s.id), '{}') AS ids
    FROM events s
    WHERE s.external_ids ? 'mlb_game_pk'
      AND s.sport_id = o.sport_id
      AND s.home_team_id = o.home_team_id
      AND s.away_team_id = o.away_team_id
      AND abs(extract(epoch FROM s.start_time_utc - o.start_time_utc)) <= :window_s
) sibs
WHERE o.external_ids ? 'the_odds_api_id'
  AND NOT (o.external_ids ? 'mlb_game_pk')
ORDER BY o.start_time_utc
"""

# Step 2 — the one content mutation (see module docstring): if orphan and
# sibling BOTH hold an is_closing row for the same outcome, demote the
# orphan's so the repoint cannot violate uq_odds_closing.
_DEMOTE_CLOSING_SQL = """
UPDATE odds_snapshots o SET is_closing = false
WHERE o.event_id = :orphan_id AND o.is_closing
  AND EXISTS (
      SELECT 1 FROM odds_snapshots s
      WHERE s.event_id = :sibling_id AND s.is_closing
        AND s.book_id = o.book_id AND s.market = o.market AND s.side = o.side
  )
"""

# Step 3 — defensive: an orphan row that exactly duplicates a sibling row on
# the dedupe key (event, book, market, side, captured_at) would collide on
# repoint. In practice zero rows (one cycle resolves a game to one event id).
_DELETE_EXACT_DUPES_SQL = """
DELETE FROM odds_snapshots o
WHERE o.event_id = :orphan_id
  AND EXISTS (
      SELECT 1 FROM odds_snapshots s
      WHERE s.event_id = :sibling_id AND s.book_id = o.book_id
        AND s.market = o.market AND s.side = o.side
        AND s.captured_at = o.captured_at
  )
"""

_REPOINT_SQL = """
UPDATE odds_snapshots SET event_id = :sibling_id WHERE event_id = :orphan_id
"""

# Step 5 — only a fully drained orphan may be deleted; anything still
# referencing it (unexpected) makes the DELETE fail loudly and roll back.
_DELETE_ORPHAN_SQL = """
DELETE FROM events
WHERE id = :orphan_id
  AND NOT EXISTS (SELECT 1 FROM odds_snapshots WHERE event_id = :orphan_id)
"""

# Step 6 — the orphan carried the game's CURRENT odds id (the feed reissued
# to it); record it on the sibling. Runs after the orphan row is gone, so
# uq_events_odds_api_id cannot collide.
_TRANSFER_ID_SQL = """
UPDATE events
SET external_ids = external_ids
    || jsonb_build_object('the_odds_api_id', CAST(:odds_api_id AS text))
WHERE id = :sibling_id
"""

_COUNT_SNAPS_SQL = "SELECT count(*) FROM odds_snapshots WHERE event_id = :event_id"

_DRY_CLOSING_SQL = _DEMOTE_CLOSING_SQL.replace(
    "UPDATE odds_snapshots o SET is_closing = false",
    "SELECT count(*) FROM odds_snapshots o",
)
_DRY_DUPES_SQL = _DELETE_EXACT_DUPES_SQL.replace(
    "DELETE FROM odds_snapshots o", "SELECT count(*) FROM odds_snapshots o"
)


def _repair_pair(conn: Connection, orphan_id, sibling_id, odds_api_id, summary) -> None:
    demoted = conn.execute(
        text(_DEMOTE_CLOSING_SQL), {"orphan_id": orphan_id, "sibling_id": sibling_id}
    ).rowcount
    deduped = conn.execute(
        text(_DELETE_EXACT_DUPES_SQL), {"orphan_id": orphan_id, "sibling_id": sibling_id}
    ).rowcount
    repointed = conn.execute(
        text(_REPOINT_SQL), {"orphan_id": orphan_id, "sibling_id": sibling_id}
    ).rowcount
    deleted = conn.execute(text(_DELETE_ORPHAN_SQL), {"orphan_id": orphan_id}).rowcount
    if deleted != 1:  # snapshots left behind would mean the repoint failed
        raise RuntimeError(f"orphan {orphan_id} not drained after repoint; aborting")
    conn.execute(
        text(_TRANSFER_ID_SQL), {"sibling_id": sibling_id, "odds_api_id": odds_api_id}
    )
    summary["closing_flags_cleared"] += demoted
    summary["exact_dupes_deleted"] += deduped
    summary["snapshots_repointed"] += repointed
    summary["events_deleted"] += deleted
    summary["repaired"].append(
        {"orphan_id": str(orphan_id), "sibling_id": str(sibling_id), "snapshots": repointed}
    )


def run(*, dry_run: bool = False, engine: Engine | None = None) -> dict[str, Any]:
    """Repair all uniquely-resolvable orphans; returns a summary dict."""
    engine = engine or make_engine(get_settings().database_url)
    summary: dict[str, Any] = {
        "job": "repair_orphan_events",
        "dry_run": dry_run,
        "orphans_found": 0,
        "repointable": 0,
        "snapshots_repointed": 0,
        "closing_flags_cleared": 0,
        "exact_dupes_deleted": 0,
        "events_deleted": 0,
        "repaired": [],
        "skipped_no_sibling": [],
        "skipped_ambiguous": [],
    }
    window_s = RESTAMP_WINDOW.total_seconds()

    with engine.begin() as conn:
        pairs = conn.execute(text(_PAIRS_SQL), {"window_s": window_s}).all()
        summary["orphans_found"] = len(pairs)
        actionable = [p for p in pairs if len(p.sibling_ids) == 1]
        summary["repointable"] = len(actionable)
        for p in pairs:
            if len(p.sibling_ids) == 0:
                summary["skipped_no_sibling"].append(
                    {"orphan_id": str(p.orphan_id), "start": p.orphan_start.isoformat()}
                )
            elif len(p.sibling_ids) > 1:
                summary["skipped_ambiguous"].append(
                    {
                        "orphan_id": str(p.orphan_id),
                        "start": p.orphan_start.isoformat(),
                        "siblings": [str(s) for s in p.sibling_ids],
                    }
                )

        if dry_run:
            for p in actionable:
                args = {"orphan_id": p.orphan_id, "sibling_id": p.sibling_ids[0]}
                summary["closing_flags_cleared"] += conn.execute(
                    text(_DRY_CLOSING_SQL), args
                ).scalar_one()
                pair_dupes = conn.execute(text(_DRY_DUPES_SQL), args).scalar_one()
                snaps = conn.execute(
                    text(_COUNT_SNAPS_SQL), {"event_id": p.orphan_id}
                ).scalar_one()
                summary["exact_dupes_deleted"] += pair_dupes
                summary["snapshots_repointed"] += snaps - pair_dupes
                summary["events_deleted"] += 1
            return summary

        if not actionable:
            return summary

        # The append-only guard comes off ONLY inside this transaction: a
        # failure anywhere below rolls back the disable itself.
        conn.execute(
            text("ALTER TABLE odds_snapshots DISABLE TRIGGER trg_odds_snapshots_immutable")
        )
        for p in actionable:
            _repair_pair(conn, p.orphan_id, p.sibling_ids[0], p.odds_api_id, summary)
        conn.execute(
            text("ALTER TABLE odds_snapshots ENABLE TRIGGER trg_odds_snapshots_immutable")
        )

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="reporta qué cambiaría sin escribir nada",
    )
    args = parser.parse_args()
    print(json.dumps(run(dry_run=args.dry_run)))
