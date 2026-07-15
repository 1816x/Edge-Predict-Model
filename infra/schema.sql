-- ============================================================================
-- EDGE — Postgres 16 schema
-- ----------------------------------------------------------------------------
-- Design principles (see docs/03-modelo-de-datos.md for the full rationale):
--   * Immutable audit trail: every published pick can be fully reconstructed
--     (odds at pick time, model version, feature snapshot + hash, fair prob,
--     closing line, result). Append-only tables enforce this with triggers.
--   * Everything evaluated is recorded, not only what gets published
--     (predictions + daily_scans), so selection bias can be measured.
--   * UUID primary keys, TIMESTAMPTZ everywhere (always UTC), NUMERIC for
--     money/prices/probabilities (never float), CHECK constraints on
--     probabilities [0, 1] and decimal prices > 1.0.
--
-- Requires: PostgreSQL >= 13 (gen_random_uuid() is built-in, no extension).
-- Run with: psql -v ON_ERROR_STOP=1 -f infra/schema.sql
-- ============================================================================

BEGIN;

-- ============================================================================
-- Enum types
-- ============================================================================

-- Phase 2 markets are added with: ALTER TYPE market_code ADD VALUE 'nrfi';
CREATE TYPE market_code AS ENUM ('moneyline', 'f5_moneyline');

CREATE TYPE event_status AS ENUM ('scheduled', 'live', 'final', 'postponed', 'cancelled');

-- 'home'/'away' cover MLB ML and F5 ML; 'over'/'under'/'yes'/'no' are reserved
-- for phase 2 markets (totals, NRFI/YRFI) so historical rows never need rewrites.
CREATE TYPE outcome_side AS ENUM ('home', 'away', 'over', 'under', 'yes', 'no');

CREATE TYPE pick_status AS ENUM ('pending', 'won', 'lost', 'push', 'void');

-- ============================================================================
-- Trigger functions
-- ============================================================================

-- Generic guard for append-only audit tables: any UPDATE or DELETE fails.
CREATE OR REPLACE FUNCTION edge_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
END;
$$;

-- Picks guard: rows are immutable except for the status column, and status
-- may only transition away from 'pending' (settlement or void). Never DELETE.
CREATE OR REPLACE FUNCTION edge_picks_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'picks rows cannot be deleted';
    END IF;
    IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'picks rows are immutable except for the status column';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND OLD.status <> 'pending' THEN
        RAISE EXCEPTION 'pick status can only transition from pending (attempted % -> %)',
            OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

-- Keeps updated_at fresh on mutable tables.
CREATE OR REPLACE FUNCTION edge_set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ============================================================================
-- Tenancy and reference tables
-- ============================================================================

-- Users: basic multi-tenancy plus the per-user bankroll profile. The engine
-- computes full Kelly per pick; each user applies their own fraction and cap
-- (see docs/05-motor-ev-y-bankroll.md). Defaults: Kelly/8, cap 2% of bankroll.
CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           text NOT NULL UNIQUE,
    display_name    text,
    is_active       boolean NOT NULL DEFAULT true,
    -- bankroll_profile
    bankroll        numeric(14, 2) NOT NULL DEFAULT 1000.00 CHECK (bankroll > 0),
    kelly_fraction  numeric(6, 5) NOT NULL DEFAULT 0.125
                    CHECK (kelly_fraction > 0 AND kelly_fraction <= 1),
    stake_cap_pct   numeric(6, 5) NOT NULL DEFAULT 0.02
                    CHECK (stake_cap_pct > 0 AND stake_cap_pct <= 0.10),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sports (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key          text NOT NULL UNIQUE,          -- e.g. 'mlb'
    display_name text NOT NULL,
    is_active    boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Sportsbooks. is_sharp marks the reference book for no-vig fair lines and
-- CLV (Pinnacle per docs/00-decisiones.md, decision #6).
CREATE TABLE books (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key          text NOT NULL UNIQUE,          -- The Odds API bookmaker key, e.g. 'pinnacle'
    display_name text NOT NULL,
    region       text,
    is_sharp     boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE teams (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sport_id     uuid NOT NULL REFERENCES sports (id),
    name         text NOT NULL,
    abbreviation text,
    external_ids jsonb NOT NULL DEFAULT '{}',   -- {"mlb_stats_id": 121, "the_odds_api": "New York Mets"}
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sport_id, name)
);

-- ============================================================================
-- Events (games) and their final results
-- ============================================================================

CREATE TABLE events (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sport_id       uuid NOT NULL REFERENCES sports (id),
    home_team_id   uuid NOT NULL REFERENCES teams (id),
    away_team_id   uuid NOT NULL REFERENCES teams (id),
    start_time_utc timestamptz NOT NULL,
    status         event_status NOT NULL DEFAULT 'scheduled',
    external_ids   jsonb NOT NULL DEFAULT '{}', -- {"mlb_game_pk": "745123", "the_odds_api_id": "..."}
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (home_team_id <> away_team_id)
    -- NO unique on (teams, start_time): traditional MLB doubleheaders list
    -- two DISTINCT games with identical teams and identical listed start
    -- times (verified against the 2018 schedule). Event identity lives in
    -- the external ids (partial unique indexes below) plus the ingestion
    -- layer's closest-start matching.
);

-- Final scores per event. Needed to settle picks AND to compute calibration
-- over every prediction (not just published picks — anti selection bias).
-- F5 scores are MLB-specific; NULL for sports without that market.
CREATE TABLE event_results (
    event_id      uuid PRIMARY KEY REFERENCES events (id),
    home_score    integer NOT NULL CHECK (home_score >= 0),
    away_score    integer NOT NULL CHECK (away_score >= 0),
    f5_home_score integer CHECK (f5_home_score >= 0),
    f5_away_score integer CHECK (f5_away_score >= 0),
    finished_at   timestamptz,
    source        text,                          -- e.g. 'mlb_stats_api'
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- Players, game logs (pitching + batting) and probables (F1 blocks)
-- ============================================================================

-- Generic player dimension (not just pitchers: the lineup block reuses it).
-- pitch_hand is NULLable — old boxscores occasionally omit it, and a known
-- hand must never be clobbered with NULL (the store layer COALESCEs).
CREATE TABLE players (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sport_id      uuid NOT NULL REFERENCES sports (id),
    mlb_person_id integer NOT NULL UNIQUE,
    full_name     text NOT NULL,
    pitch_hand    text CHECK (pitch_hand IN ('L', 'R', 'S')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per pitcher per game (ALL pitchers, is_starter flags the opener of
-- record — unlocks the bullpen block without re-ingesting). outs_recorded is
-- IP as an exact integer (5.2 innings -> 17 outs), never a float. Counting
-- columns that old boxscores may omit (fly_outs, sac_flies, pitches) are
-- NULLable: a hole in the feed must not block the row. NOT append-only:
-- MLB corrects boxscores, upserts are DO UPDATE like event_results.
CREATE TABLE pitching_game_logs (
    event_id       uuid NOT NULL REFERENCES events (id),
    player_id      uuid NOT NULL REFERENCES players (id),
    team_id        uuid NOT NULL REFERENCES teams (id),
    is_home        boolean NOT NULL,
    is_starter     boolean NOT NULL,
    outs_recorded  integer NOT NULL CHECK (outs_recorded >= 0),
    batters_faced  integer NOT NULL CHECK (batters_faced >= 0),
    strikeouts     integer NOT NULL CHECK (strikeouts >= 0),
    walks          integer NOT NULL CHECK (walks >= 0),
    hit_batsmen    integer NOT NULL CHECK (hit_batsmen >= 0),
    home_runs      integer NOT NULL CHECK (home_runs >= 0),
    fly_outs       integer CHECK (fly_outs >= 0),
    ground_outs    integer CHECK (ground_outs >= 0),
    sac_flies      integer CHECK (sac_flies >= 0),
    pitches_thrown integer CHECK (pitches_thrown >= 0),
    source         text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, player_id)
);

-- One row per batter per game with a nonzero derived plate appearance
-- count (pinch runners / defensive subs contribute nothing to rate
-- features and are excluded by the parser). Required counting columns are
-- parser-guaranteed; events the feed omits when zero (doubles, triples,
-- home runs, IBB, HBP, SF, SH) arrive as true zeros. plate_appearances
-- and batting_order are NULLable audit fields, never denominators (the
-- feature layer derives PA from components for cross-era uniformity).
-- Internal-consistency CHECKs stop corrupted boxscores at the door.
-- NOT append-only: MLB corrects boxscores, upserts are DO UPDATE.
CREATE TABLE batting_game_logs (
    event_id          uuid NOT NULL REFERENCES events (id),
    player_id         uuid NOT NULL REFERENCES players (id),
    team_id           uuid NOT NULL REFERENCES teams (id),
    is_home           boolean NOT NULL,
    at_bats           integer NOT NULL CHECK (at_bats >= 0),
    hits              integer NOT NULL CHECK (hits >= 0),
    doubles           integer NOT NULL CHECK (doubles >= 0),
    triples           integer NOT NULL CHECK (triples >= 0),
    home_runs         integer NOT NULL CHECK (home_runs >= 0),
    walks             integer NOT NULL CHECK (walks >= 0),
    intentional_walks integer NOT NULL CHECK (intentional_walks >= 0),
    strikeouts        integer NOT NULL CHECK (strikeouts >= 0),
    hit_by_pitch      integer NOT NULL CHECK (hit_by_pitch >= 0),
    sac_flies         integer NOT NULL CHECK (sac_flies >= 0),
    sac_bunts         integer NOT NULL CHECK (sac_bunts >= 0),
    batting_order     integer CHECK (batting_order >= 100),
    plate_appearances integer CHECK (plate_appearances >= 0),
    source            text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, player_id),
    CHECK (hits <= at_bats),
    CHECK (doubles + triples + home_runs <= hits),
    CHECK (intentional_walks <= walks)
);

-- Probable starters as an append-per-change history: a row is inserted
-- whenever the announced probable DIFFERS from the currently-recorded one
-- (dedupe lives in the store layer, NOT in a unique constraint: a
-- re-announcement X -> Y -> X must record X's return or the as-of
-- resolution would answer Y forever). The probable "as-of T" is the row
-- with the greatest first_seen_at <= T — a late scratch stays audited as
-- history instead of being overwritten (same archive-from-day-one
-- philosophy as odds_snapshots).
CREATE TABLE event_probables (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      uuid NOT NULL REFERENCES events (id),
    side          text NOT NULL CHECK (side IN ('home', 'away')),
    player_id     uuid NOT NULL REFERENCES players (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Published batting orders as an append-per-snapshot history (migration
-- 005). A "snapshot" is the set of rows sharing one first_seen_at for one
-- (event, side); a new snapshot is inserted whenever the announced lineup
-- DIFFERS from the currently-recorded one (dedupe in the store layer, same
-- reasoning as event_probables). The lineup "as-of T" is the rows with the
-- greatest first_seen_at <= T. batting_order uses MLB's hundreds encoding
-- (100 = leadoff starter, 200 = 2-hole, ...); the starter of a slot is
-- batting_order % 100 == 0. Backtest reconstructs the realized lineup from
-- batting_game_logs.batting_order instead (no pre-pipeline snapshots exist)
-- with is_confirmed=false, a documented optimistic bias like §1.3.
CREATE TABLE event_lineups (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      uuid NOT NULL REFERENCES events (id),
    side          text NOT NULL CHECK (side IN ('home', 'away')),
    batting_order integer NOT NULL CHECK (batting_order >= 100),
    player_id     uuid NOT NULL REFERENCES players (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- Odds snapshots (append-only)
-- ============================================================================

-- One row per (event, book, market, side) at a capture instant. This is the
-- raw evidence of "what odds existed at that moment". implied_prob is a
-- generated column so it can never drift from the stored price.
CREATE TABLE odds_snapshots (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id       uuid NOT NULL REFERENCES events (id),
    book_id        uuid NOT NULL REFERENCES books (id),
    market         market_code NOT NULL,
    side           outcome_side NOT NULL,
    price_decimal  numeric(8, 3) NOT NULL CHECK (price_decimal > 1.0),
    price_american integer NOT NULL CHECK (price_american <= -100 OR price_american >= 100),
    implied_prob   numeric(7, 6) GENERATED ALWAYS AS (round(1.0 / price_decimal, 6)) STORED,
    captured_at    timestamptz NOT NULL,
    is_closing     boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- Composite key target so referencing tables can prove event/market match.
    UNIQUE (id, event_id, market),
    -- Dedupe: one row per capture instant and outcome.
    UNIQUE (event_id, book_id, market, side, captured_at)
);

-- ============================================================================
-- Models, features, predictions (append-only)
-- ============================================================================

-- Every trained artifact that ever produced a published number. git_sha ties
-- the artifact to the exact training code (see docs/04-features-y-modelos.md).
CREATE TABLE model_versions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL UNIQUE,     -- e.g. 'mlb_ml_xgb_v3'
    market             market_code NOT NULL,
    trained_at         timestamptz NOT NULL,
    train_window_start date NOT NULL,
    train_window_end   date NOT NULL,
    metrics            jsonb NOT NULL DEFAULT '{}',  -- {"log_loss": ..., "brier": ..., "ece": ...}
    git_sha            text NOT NULL CHECK (git_sha ~ '^[0-9a-f]{7,40}$'),
    calibration_method text,                     -- e.g. 'isotonic', 'platt'
    is_active          boolean NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (train_window_end > train_window_start),
    -- Composite key target: predictions must reference a model of the same market.
    UNIQUE (id, market)
);

-- Exact feature vector the model saw, with an as-of cutoff (anti-leakage) and
-- a SHA-256 hash for cheap integrity checks and deduplication.
CREATE TABLE feature_snapshots (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id     uuid NOT NULL REFERENCES events (id),
    market       market_code NOT NULL,
    features     jsonb NOT NULL,
    feature_hash text NOT NULL CHECK (feature_hash ~ '^[0-9a-f]{64}$'),  -- sha256 of canonical JSON
    -- Information cutoff. The anti-leakage invariant (as_of_ts <= the event's
    -- start_time_utc) is cross-table, so it is enforced by the feature engine,
    -- not by a CHECK; see docs/04-features-y-modelos.md.
    as_of_ts     timestamptz NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Identical feature vectors for the same event/market are stored once;
    -- the engine upserts with ON CONFLICT DO NOTHING and reuses the row.
    UNIQUE (event_id, market, feature_hash),
    -- Composite key target so predictions can prove event/market match.
    UNIQUE (id, event_id, market)
);

-- Every model evaluation, published or not. p_raw is the model output,
-- p_calibrated is after the calibration layer; picks are cut from p_calibrated.
CREATE TABLE predictions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id            uuid NOT NULL REFERENCES events (id),
    market              market_code NOT NULL,
    side                outcome_side NOT NULL,    -- the side p_* refers to: p = P(side wins)
    model_version_id    uuid NOT NULL,
    feature_snapshot_id uuid NOT NULL,
    p_raw               numeric(7, 6) NOT NULL CHECK (p_raw >= 0 AND p_raw <= 1),
    p_calibrated        numeric(7, 6) NOT NULL CHECK (p_calibrated >= 0 AND p_calibrated <= 1),
    created_at          timestamptz NOT NULL DEFAULT now(),
    -- Composite FKs: the referenced model and feature snapshot must belong to
    -- the same market (and, for features, the same event) as this prediction.
    FOREIGN KEY (model_version_id, market) REFERENCES model_versions (id, market),
    FOREIGN KEY (feature_snapshot_id, event_id, market)
        REFERENCES feature_snapshots (id, event_id, market)
);

-- ============================================================================
-- Picks and settlement (audit core)
-- ============================================================================

-- A published recommendation. Frozen at publish time: the price actually
-- available (odds_snapshot_id), the no-vig fair prob, edge, EV and full Kelly.
-- stake_suggested_pct is the fraction of bankroll under the DEFAULT profile
-- (Kelly/8 capped at 2%); each user's UI rescales with their own profile.
CREATE TABLE picks (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prediction_id       uuid NOT NULL UNIQUE REFERENCES predictions (id),
    odds_snapshot_id    uuid NOT NULL REFERENCES odds_snapshots (id),
    book_id             uuid NOT NULL REFERENCES books (id),
    price_taken_decimal numeric(8, 3) NOT NULL CHECK (price_taken_decimal > 1.0),
    price_taken_american integer NOT NULL
                        CHECK (price_taken_american <= -100 OR price_taken_american >= 100),
    p_fair_at_pick      numeric(7, 6) NOT NULL CHECK (p_fair_at_pick >= 0 AND p_fair_at_pick <= 1),
    edge                numeric(7, 6) NOT NULL CHECK (edge > 0),        -- p_model - p_fair
    ev_per_unit         numeric(8, 6) NOT NULL CHECK (ev_per_unit > 0),
    kelly_full          numeric(7, 6) NOT NULL CHECK (kelly_full > 0 AND kelly_full <= 1),
    stake_suggested_pct numeric(6, 5) NOT NULL
                        CHECK (stake_suggested_pct >= 0 AND stake_suggested_pct <= 1),
    published_at        timestamptz NOT NULL DEFAULT now(),
    status              pick_status NOT NULL DEFAULT 'pending'
);

-- Settlement record, separate from picks so the pick row itself stays frozen.
-- profit_units is per 1 unit staked: won -> price - 1, lost -> -1, push/void -> 0.
CREATE TABLE pick_results (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pick_id      uuid NOT NULL UNIQUE REFERENCES picks (id),
    settled_at   timestamptz NOT NULL,
    result       pick_status NOT NULL CHECK (result <> 'pending'),
    profit_units numeric(10, 4) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (result = 'won'  AND profit_units > 0) OR
        (result = 'lost' AND profit_units < 0) OR
        (result IN ('push', 'void') AND profit_units = 0)
    )
);

-- Closing Line Value vs the sharp reference book (Pinnacle), computed after
-- the market closes. clv_prob_pts = closing_p_fair - implied_prob(price taken),
-- in probability points; beat_close is derived and CHECK-enforced consistent.
CREATE TABLE clv_records (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pick_id                  uuid NOT NULL UNIQUE REFERENCES picks (id),
    closing_odds_snapshot_id uuid REFERENCES odds_snapshots (id),
    closing_price_decimal    numeric(8, 3) NOT NULL CHECK (closing_price_decimal > 1.0),
    closing_p_fair           numeric(7, 6) NOT NULL
                             CHECK (closing_p_fair >= 0 AND closing_p_fair <= 1),
    clv_prob_pts             numeric(7, 6) NOT NULL CHECK (clv_prob_pts >= -1 AND clv_prob_pts <= 1),
    beat_close               boolean NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    CHECK (beat_close = (clv_prob_pts > 0))
);

-- ============================================================================
-- Daily scans (selection-bias ledger)
-- ============================================================================

-- One row per daily cron run and sport. Together with predictions (which store
-- EVERY evaluation), this proves how many games were looked at vs how many
-- picks were published — published results can't hide the denominator.
CREATE TABLE daily_scans (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scan_date                 date NOT NULL,
    sport_id                  uuid NOT NULL REFERENCES sports (id),
    started_at                timestamptz NOT NULL,
    finished_at               timestamptz,
    games_scheduled           integer NOT NULL CHECK (games_scheduled >= 0),
    games_evaluated           integer NOT NULL CHECK (games_evaluated >= 0),
    candidates_over_threshold integer NOT NULL DEFAULT 0 CHECK (candidates_over_threshold >= 0),
    picks_published           integer NOT NULL DEFAULT 0 CHECK (picks_published >= 0),
    details                   jsonb NOT NULL DEFAULT '{}',  -- skip reasons, API credits used, etc.
    created_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scan_date, sport_id),
    CHECK (games_evaluated <= games_scheduled),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

-- ============================================================================
-- Indexes for dashboard query patterns
-- ============================================================================

-- Picks by date / by state (main dashboard feed).
CREATE INDEX idx_picks_published_at ON picks (published_at DESC);
CREATE INDEX idx_picks_status_published_at ON picks (status, published_at DESC);
CREATE INDEX idx_picks_book_published_at ON picks (book_id, published_at DESC);

-- Odds history per event/book/market (line movement charts, closing lookup).
CREATE INDEX idx_odds_event_book_market_captured
    ON odds_snapshots (event_id, book_id, market, captured_at DESC);
-- Exactly one closing snapshot per (event, book, market, side).
CREATE UNIQUE INDEX uq_odds_closing
    ON odds_snapshots (event_id, book_id, market, side) WHERE is_closing;

-- Slate views and event lookup by external id.
CREATE INDEX idx_events_sport_start ON events (sport_id, start_time_utc);
CREATE INDEX idx_events_status ON events (status) WHERE status IN ('scheduled', 'live');
CREATE UNIQUE INDEX uq_events_mlb_game_pk
    ON events ((external_ids ->> 'mlb_game_pk')) WHERE external_ids ? 'mlb_game_pk';
CREATE UNIQUE INDEX uq_events_odds_api_id
    ON events ((external_ids ->> 'the_odds_api_id')) WHERE external_ids ? 'the_odds_api_id';

-- Prediction lookups (calibration reports, per-event audit).
CREATE INDEX idx_predictions_event_market ON predictions (event_id, market, created_at DESC);
CREATE INDEX idx_predictions_model_version ON predictions (model_version_id, created_at DESC);

-- Feature snapshot lookup for a given event/market at a point in time.
CREATE INDEX idx_feature_snapshots_event_market_asof
    ON feature_snapshots (event_id, market, as_of_ts DESC);

-- Starter history per pitcher (as-of feature windows) and probable lookup.
CREATE INDEX idx_pitching_logs_player ON pitching_game_logs (player_id);
CREATE INDEX idx_event_probables_event
    ON event_probables (event_id, side, first_seen_at DESC);
CREATE INDEX idx_event_lineups_asof
    ON event_lineups (event_id, side, first_seen_at DESC);

-- Batting history per player (lineup block, §1.5) and per team (offense
-- block windows, §1.2 — the online builder queries by team_id directly).
CREATE INDEX idx_batting_logs_player ON batting_game_logs (player_id);
CREATE INDEX idx_batting_logs_team ON batting_game_logs (team_id);

-- Aggregated performance (monthly yield, CLV beat-rate).
CREATE INDEX idx_pick_results_settled_at ON pick_results (settled_at);
CREATE INDEX idx_daily_scans_date ON daily_scans (scan_date DESC);

-- ============================================================================
-- Immutability and housekeeping triggers
-- ============================================================================

CREATE TRIGGER trg_odds_snapshots_immutable
    BEFORE UPDATE OR DELETE ON odds_snapshots
    FOR EACH ROW EXECUTE FUNCTION edge_forbid_mutation();

CREATE TRIGGER trg_feature_snapshots_immutable
    BEFORE UPDATE OR DELETE ON feature_snapshots
    FOR EACH ROW EXECUTE FUNCTION edge_forbid_mutation();

CREATE TRIGGER trg_predictions_immutable
    BEFORE UPDATE OR DELETE ON predictions
    FOR EACH ROW EXECUTE FUNCTION edge_forbid_mutation();

CREATE TRIGGER trg_pick_results_immutable
    BEFORE UPDATE OR DELETE ON pick_results
    FOR EACH ROW EXECUTE FUNCTION edge_forbid_mutation();

CREATE TRIGGER trg_clv_records_immutable
    BEFORE UPDATE OR DELETE ON clv_records
    FOR EACH ROW EXECUTE FUNCTION edge_forbid_mutation();

CREATE TRIGGER trg_picks_guard
    BEFORE UPDATE OR DELETE ON picks
    FOR EACH ROW EXECUTE FUNCTION edge_picks_guard();

CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

CREATE TRIGGER trg_events_updated_at
    BEFORE UPDATE ON events
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

CREATE TRIGGER trg_event_results_updated_at
    BEFORE UPDATE ON event_results
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

CREATE TRIGGER trg_players_updated_at
    BEFORE UPDATE ON players
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

CREATE TRIGGER trg_pitching_game_logs_updated_at
    BEFORE UPDATE ON pitching_game_logs
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

CREATE TRIGGER trg_batting_game_logs_updated_at
    BEFORE UPDATE ON batting_game_logs
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

-- ============================================================================
-- Views for recurring dashboard/report queries
-- ============================================================================

-- Realized outcome for EVERY prediction (published or not). side_won is NULL
-- while the game is unplayed or when the market pushes (e.g. F5 tie).
-- This is the base for calibration reports free of selection bias.
CREATE VIEW v_prediction_outcomes AS
SELECT
    p.id AS prediction_id,
    p.event_id,
    p.market,
    p.side,
    p.model_version_id,
    p.p_calibrated,
    p.created_at,
    CASE p.market
        WHEN 'moneyline' THEN
            CASE
                WHEN er.home_score = er.away_score THEN NULL
                WHEN (p.side = 'home' AND er.home_score > er.away_score)
                  OR (p.side = 'away' AND er.away_score > er.home_score) THEN true
                ELSE false
            END
        WHEN 'f5_moneyline' THEN
            CASE
                WHEN er.f5_home_score IS NULL OR er.f5_away_score IS NULL THEN NULL
                WHEN er.f5_home_score = er.f5_away_score THEN NULL  -- push
                WHEN (p.side = 'home' AND er.f5_home_score > er.f5_away_score)
                  OR (p.side = 'away' AND er.f5_away_score > er.f5_home_score) THEN true
                ELSE false
            END
    END AS side_won
FROM predictions p
LEFT JOIN event_results er ON er.event_id = p.event_id;

-- Monthly settled performance: units, yield and CLV beat-rate in one place.
CREATE VIEW v_monthly_performance AS
SELECT
    date_trunc('month', pk.published_at) AS month,
    count(*) AS picks_settled,
    count(*) FILTER (WHERE pr.result = 'won') AS won,
    count(*) FILTER (WHERE pr.result = 'lost') AS lost,
    count(*) FILTER (WHERE pr.result IN ('push', 'void')) AS push_void,
    sum(pr.profit_units) AS net_units,
    round(sum(pr.profit_units) / nullif(count(*) FILTER (WHERE pr.result IN ('won', 'lost')), 0), 4)
        AS yield_per_unit,
    round(avg(c.clv_prob_pts), 4) AS avg_clv_prob_pts,
    round(avg(CASE WHEN c.beat_close THEN 1.0 ELSE 0.0 END), 4) AS clv_beat_rate
FROM picks pk
JOIN pick_results pr ON pr.pick_id = pk.id
LEFT JOIN clv_records c ON c.pick_id = pk.id
GROUP BY 1;

-- ============================================================================
-- Minimal seed data (idempotent)
-- ============================================================================

INSERT INTO sports (key, display_name) VALUES
    ('mlb', 'Major League Baseball')
ON CONFLICT (key) DO NOTHING;

INSERT INTO books (key, display_name, region, is_sharp) VALUES
    ('pinnacle', 'Pinnacle', 'eu', true),   -- reference book for no-vig fair line and CLV
    ('bet365', 'Bet365', 'eu', false),
    ('caliente', 'Caliente', 'mx', false),  -- no public API: manual comparison by the user
    ('codere', 'Codere', 'mx', false)
ON CONFLICT (key) DO NOTHING;

COMMIT;
