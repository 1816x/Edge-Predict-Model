-- 004 (2026-07-10): batting game logs (bloque de ofensiva de equipo, F1.2).
--
-- El bloque de ofensiva (docs/04 §1.2) necesita las líneas de bateo por
-- juego para wOBA/OPS/ISO/K%/BB% as-of. Se guardan POR JUGADOR (no el
-- agregado de equipo) por la misma filosofía de la migración 003: el fetch
-- del boxscore ya se paga, y las filas por bateador desbloquean el bloque
-- de lineup (docs/04 §1.5 — lineup_woba_proj pondera wOBA as-of por
-- bateador) sin re-ingerir ~19K boxscores. batting_order viaja gratis en
-- el payload y sirve para ese mismo bloque.
--
-- Semántica de columnas (espejo de la filosofía de pitching_game_logs):
-- las requeridas (at_bats, hits, strikeouts, walks) las garantiza el
-- parser o la línea se descarta con anomalía. Los eventos contables que
-- el feed omite cuando son cero (doubles, triples, home_runs, walks
-- intencionales, hit_by_pitch, sac_flies, sac_bunts) llegan como 0 del
-- parser. plate_appearances y batting_order son NULLables: auditoría, no
-- denominadores (el PA de las features se deriva de componentes para ser
-- uniforme entre eras). Los CHECKs de consistencia interna atrapan
-- boxscores corruptos en la puerta en vez de envenenar features en
-- silencio. NOT append-only: MLB corrige boxscores, upsert DO UPDATE.
--
-- El parser excluye líneas con PA derivada cero (corredores emergentes,
-- reemplazos defensivos): no aportan a ninguna feature de tasas.
--
-- Idempotente: safe de correr más de una vez. Ningún statement (NI ningún
-- comentario) puede llevar punto-y-coma interno ni cuerpos $$, porque
-- app.jobs.apply_migration separa statements con un split naive sobre ese
-- carácter.

BEGIN;

CREATE TABLE IF NOT EXISTS batting_game_logs (
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

CREATE INDEX IF NOT EXISTS idx_batting_logs_player
    ON batting_game_logs (player_id);

CREATE INDEX IF NOT EXISTS idx_batting_logs_team
    ON batting_game_logs (team_id);

DROP TRIGGER IF EXISTS trg_batting_game_logs_updated_at ON batting_game_logs;

CREATE TRIGGER trg_batting_game_logs_updated_at
    BEFORE UPDATE ON batting_game_logs
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

COMMIT;
