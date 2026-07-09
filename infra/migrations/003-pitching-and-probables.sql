-- 003 (2026-07-09): pitching game logs + probables (bloque abridor, F1).
--
-- El bloque de features de abridor (docs/04 §1.3) necesita: (a) las líneas
-- de pitcheo por juego (K, BB, HBP, HR, outs, fly balls) para K-BB% y xFIP
-- as-of, (b) la mano del pitcher, y (c) QUÉ abridor probable estaba
-- publicado al momento de decisión (regla as-of del bloque: en producción
-- se usa el probable, no el que realmente abrió).
--
-- players es una dimensión genérica (no "pitchers"): el bloque de lineup
-- (§1.5) reutilizará la misma tabla. Se guardan las líneas de TODOS los
-- pitchers del juego, con flag is_starter — el costo marginal es cero y
-- desbloquea el bloque de bullpen (§1.4) sin re-ingerir.
--
-- event_probables es append-por-cambio: cada (evento, lado, pitcher) se
-- registra una vez con su first_seen_at. El "probable vigente as-of T" es
-- la fila con mayor first_seen_at <= T. Un scratch tardío queda auditado
-- como historia, no sobreescrito.
--
-- Idempotente: safe de correr más de una vez. Ningún statement (NI ningún
-- comentario) puede llevar punto-y-coma interno ni cuerpos $$, porque
-- app.jobs.apply_migration separa statements con un split naive sobre ese
-- carácter — un punto-y-coma dentro de un comentario parte el comentario a
-- mitad de línea y el fragmento deja de parecer comentario.

BEGIN;

CREATE TABLE IF NOT EXISTS players (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    sport_id      uuid NOT NULL REFERENCES sports (id),
    mlb_person_id integer NOT NULL UNIQUE,
    full_name     text NOT NULL,
    pitch_hand    text CHECK (pitch_hand IN ('L', 'R', 'S')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pitching_game_logs (
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

CREATE INDEX IF NOT EXISTS idx_pitching_logs_player
    ON pitching_game_logs (player_id);

CREATE TABLE IF NOT EXISTS event_probables (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      uuid NOT NULL REFERENCES events (id),
    side          text NOT NULL CHECK (side IN ('home', 'away')),
    player_id     uuid NOT NULL REFERENCES players (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, side, player_id)
);

CREATE INDEX IF NOT EXISTS idx_event_probables_event
    ON event_probables (event_id, side, first_seen_at DESC);

DROP TRIGGER IF EXISTS trg_players_updated_at ON players;

CREATE TRIGGER trg_players_updated_at
    BEFORE UPDATE ON players
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

DROP TRIGGER IF EXISTS trg_pitching_game_logs_updated_at ON pitching_game_logs;

CREATE TRIGGER trg_pitching_game_logs_updated_at
    BEFORE UPDATE ON pitching_game_logs
    FOR EACH ROW EXECUTE FUNCTION edge_set_updated_at();

COMMIT;
