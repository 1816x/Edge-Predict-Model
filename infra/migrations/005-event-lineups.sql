-- 005 (2026-07-15): lineups archivados as-of (bloque lineup, docs/04 §1.5).
--
-- El bloque de lineup (§1.5) necesita QUÉ orden al bate estaba publicado al
-- momento de decisión, con el flag honesto is_confirmed. Igual que
-- event_probables para el abridor: en producción se usa el lineup publicado
-- as-of, no el que realmente jugó. El backtest histórico reconstruye el
-- lineup realizado del box score (batting_game_logs.batting_order) porque
-- no hay snapshots pre-juego archivados para temporadas anteriores al
-- pipeline — sesgo optimista documentado, simétrico al probable-vs-abridor
-- del bloque §1.3.
--
-- event_lineups es append-por-snapshot: un snapshot = las filas que
-- comparten un first_seen_at para un (event, side). Se inserta un snapshot
-- nuevo cada vez que el orden anunciado DIFIERE del vigente (dedupe en la
-- capa store, NO por constraint: un re-anuncio del lineup debe poder
-- registrarse). El "lineup vigente as-of T" son las filas con el mayor
-- first_seen_at <= T. Reusa players (dimensión genérica creada en la 003) y
-- el mismo encoding en centenas de batting_order (100 = leadoff titular,
-- 200 = 2do titular, ... subs 101/201, y el titular de un slot es
-- batting_order % 100 == 0).
--
-- Idempotente: safe de correr más de una vez. Ningún statement (NI ningún
-- comentario) puede llevar punto-y-coma interno ni cuerpos con dobles
-- signos de dólar, porque app.jobs.apply_migration separa statements con un
-- split naive sobre ese carácter.

BEGIN;

CREATE TABLE IF NOT EXISTS event_lineups (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      uuid NOT NULL REFERENCES events (id),
    side          text NOT NULL CHECK (side IN ('home', 'away')),
    batting_order integer NOT NULL CHECK (batting_order >= 100),
    player_id     uuid NOT NULL REFERENCES players (id),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_lineups_asof
    ON event_lineups (event_id, side, first_seen_at DESC);

COMMIT;
