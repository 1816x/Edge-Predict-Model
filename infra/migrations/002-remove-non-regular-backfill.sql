-- 002 (2026-07-08): purge spring-training/postseason rows from the backfill.
--
-- The first full backfill ingested every Final game in the date ranges,
-- including ~3.7K spring training and postseason games (the schedule feed
-- carries them alongside the regular season). The training corpus must be
-- regular season only (docs/04 §5), and those rows cannot be told apart
-- after the fact, so: delete every backfill-only event (no odds identity)
-- and re-run the backfill, which now filters gameType = 'R' at ingest.
--
-- Events with a the_odds_api_id are preserved: they are live-slate events
-- referenced by odds_snapshots. Idempotent (second run deletes 0 rows).

BEGIN;

DELETE FROM event_results
WHERE event_id IN (
    SELECT id FROM events WHERE NOT (external_ids ? 'the_odds_api_id')
);

DELETE FROM events
WHERE NOT (external_ids ? 'the_odds_api_id');

COMMIT;
