-- 006 (2026-07-16): archivo crudo de transacciones/IL as-of (bloque lineup
-- star_out_flag docs/04 §1.5, y el futuro closer/bullpen-IL §1.4).
--
-- El feature star_out_flag necesita saber si un bateador top-2 del equipo
-- estaba EN LA LISTA DE LESIONADOS (IL) al momento de decisión. La MLB Stats
-- API expone eso en el feed de transacciones (endpoint /transactions), que
-- trae colocaciones y activaciones de IL. A diferencia de event_probables o
-- event_lineups (espejos as-of por evento), esto es un archivo por-jugador y
-- por-fecha, sin atadura a un juego.
--
-- Se guarda el movimiento CRUDO, un registro por transacción. El estado "en
-- IL as-of la fecha D" se calcula por replay en la capa de features (el
-- ultimo movimiento IL con transaction_date estrictamente anterior a D): un
-- stint (start, end) NO se materializa aqui porque la activacion es un evento
-- futuro respecto a la colocacion y precomputar el end violaria as-of. La
-- clasificacion IL (colocacion vs activacion) tampoco se almacena: vive
-- versionada en app/features/transactions.py sobre type_desc + description,
-- asi un cambio de taxonomia es un bump de feature_version, no un re-backfill.
-- Por eso se guarda el texto crudo type_code, type_desc y description tal cual
-- llegan del feed (description es el free-text con el detalle del movimiento).
--
-- Regla as-of (docs/04 §1.5): las transacciones historicas traen FECHA sin
-- hora (transaction_date), asi que en backtest un movimiento fechado el mismo
-- dia del juego se trata como desconocido (corte conservador transaction_date
-- menor estricto que el dia UTC del evento, o sea fecha menor-igual t-1).
-- first_seen_at es solo proveniencia forward-only, NO gobierna ese corte.
--
-- Idempotencia por la clave natural del feed: mlb_transaction_id es UNIQUE,
-- asi que un upsert ON CONFLICT converge y re-correr el backfill es no-op (o
-- corrige un movimiento re-emitido). from_team_id/to_team_id son auditoria
-- (las features no los usan). player_id reusa players (dimension de la 003).
--
-- Idempotente: safe de correr mas de una vez. Ningun statement (NI ningun
-- comentario) puede llevar punto-y-coma interno ni cuerpos con dobles signos
-- de dolar, porque app.jobs.apply_migration separa statements con un split
-- naive sobre ese caracter.

BEGIN;

CREATE TABLE IF NOT EXISTS player_transactions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mlb_transaction_id bigint UNIQUE,
    player_id          uuid NOT NULL REFERENCES players (id),
    from_team_id       uuid REFERENCES teams (id),
    to_team_id         uuid REFERENCES teams (id),
    type_code          text,
    type_desc          text,
    description        text,
    transaction_date   date NOT NULL,
    first_seen_at      timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_player_transactions_asof
    ON player_transactions (player_id, transaction_date);

COMMIT;
