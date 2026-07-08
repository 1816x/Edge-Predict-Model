-- 001 (2026-07-08): traditional doubleheaders broke UNIQUE (teams, start).
--
-- The 2018 backfill hit two DISTINCT games with identical teams and
-- identical listed start times (traditional doubleheader). Event identity
-- moves entirely to external ids: the mlb_game_pk partial unique index
-- (already present) plus a new one for the_odds_api_id.
--
-- Idempotent: safe to run more than once.

BEGIN;

ALTER TABLE events
    DROP CONSTRAINT IF EXISTS events_sport_id_home_team_id_away_team_id_start_time_utc_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_events_odds_api_id
    ON events ((external_ids ->> 'the_odds_api_id')) WHERE external_ids ? 'the_odds_api_id';

COMMIT;
