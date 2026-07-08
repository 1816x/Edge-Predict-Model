# 03 — Modelo de datos (Postgres 16)

Este documento describe el schema relacional del MVP (MLB Moneyline + First-5-Innings
Moneyline). El DDL completo y ejecutable vive en `infra/schema.sql`; fue validado contra
PostgreSQL 16 (schema, triggers de inmutabilidad, constraints y todas las queries de este
documento se ejecutaron contra una instancia real).

El objetivo de diseño no es solo persistir datos: es que **cada pick publicado sea
reconstruible por completo** meses después, sin ambigüedad. Un pick debe poder responder:

1. ¿Qué odds había en el momento exacto (book, precio, timestamp)? → `odds_snapshots`
2. ¿Qué versión de modelo corrió (código, ventana de entrenamiento, métricas)? → `model_versions`
3. ¿Qué features vio el modelo (vector exacto + hash de integridad + corte as-of)? → `feature_snapshots`
4. ¿Qué probabilidad produjo (cruda y calibrada)? → `predictions`
5. ¿Qué se recomendó (precio tomado, fair prob, edge, EV, Kelly, stake)? → `picks`
6. ¿Qué línea cerró y hubo CLV? → `clv_records`
7. ¿Qué resultó y cuánto se ganó/perdió? → `pick_results` + `event_results`

Y una octava pregunta que casi nadie registra y que aquí es obligatoria: **¿qué se evaluó
y NO se publicó?** → `predictions` (toda evaluación se guarda) + `daily_scans` (ledger del
cron diario). Sin ese denominador, cualquier métrica publicada tiene sesgo de selección
(ver `06-backtesting-y-metricas.md`).

## Convenciones (no negociables en este schema)

| Convención | Implementación |
|---|---|
| Primary keys | `uuid` con `gen_random_uuid()` (built-in desde PG 13, sin extensiones) |
| Tiempos | `timestamptz` siempre; la aplicación escribe y lee en UTC |
| Dinero, precios, probabilidades | `NUMERIC`, nunca `float` (`numeric(14,2)` bankroll, `numeric(8,3)` odds decimales, `numeric(7,6)` probabilidades) |
| Probabilidades | `CHECK (x >= 0 AND x <= 1)` en toda columna de probabilidad |
| Odds decimales | `CHECK (price_decimal > 1.0)` |
| Odds americanas | `integer` con `CHECK (x <= -100 OR x >= 100)` (no existe el rango (−100, 100)) |
| Probabilidad implícita | Columna **generada** `round(1.0 / price_decimal, 6)`: no puede divergir del precio almacenado |
| Enums | Tipos Postgres (`market_code`, `event_status`, `outcome_side`, `pick_status`); fase 2 agrega valores con `ALTER TYPE ... ADD VALUE` |
| Auditoría | Tablas append-only protegidas por triggers (ver sección de inmutabilidad) |

## Diagrama ER (simplificado)

```
 users                          sports ─────────< teams
 (bankroll, kelly_fraction,        │                │
  stake_cap_pct)                   │       ┌────────┴────────┐
                                   └─────< events (home_team, away_team,
                                             │     start_time_utc, status)
                                             │ 1:1
                     ┌───────────────────────┼──────────────────────┐
                     ▼                       ▼                      ▼
              odds_snapshots           event_results         feature_snapshots
              (book, market, side,     (scores finales        (features JSONB,
               price, captured_at,      y de F5)               feature_hash,
               is_closing) [A-O]                               as_of_ts) [A-O]
                     ▲                                              ▲
       books ────────┤                                              │
         │           │                                              │
         │      model_versions ─────< predictions >─────────────────┘
         │      (git_sha, metrics)    (side, p_raw,
         │                             p_calibrated) [A-O]
         │                                   │ 1:0..1
         │                                   ▼
         └────────────────────────────────< picks ── odds_snapshot_id ─▶ odds_snapshots
                                             (price_taken, p_fair_at_pick,
                                              edge, ev, kelly, stake, status)
                                                │ 1:0..1         │ 1:0..1
                                                ▼                ▼
                                          pick_results      clv_records [A-O]
                                          (result,          (closing_price,
                                           profit_units)     clv_prob_pts,
                                           [A-O]             beat_close)

 daily_scans  (una fila por fecha+deporte: juegos evaluados vs picks publicados)

 [A-O] = append-only (trigger bloquea UPDATE/DELETE)
 ──<   = uno a muchos
```

## Entidades y por qué existen

### Tenancy y catálogo

- **`users`** — Multi-tenant básico del SaaS más el perfil de bankroll:
  `bankroll` (`numeric(14,2)`), `kelly_fraction` (default **0.125** = Kelly/8) y
  `stake_cap_pct` (default **0.02** = 2% del bankroll por pick), según la decisión #8 de
  `00-decisiones.md`. El motor calcula Kelly completo una sola vez por pick; el stake de
  cada usuario se deriva en render con su propio perfil (ver `05-motor-ev-y-bankroll.md`).
  Auth (password/OAuth, sesiones) se resuelve en la capa API y no forma parte de este schema.
- **`sports`** — Catálogo de ligas (`mlb` en el MVP). Existe para que fase 3+ (NBA, NFL,
  NHL, Champions) no requiera cambios estructurales.
- **`books`** — Catálogo de sportsbooks con la clave de The Odds API. `is_sharp = true`
  marca el book de referencia para la línea justa y el CLV (Pinnacle, decisión #6).
  Books MX sin API (Caliente, Codere) existen como filas para picks comparados a mano.
- **`teams`** — Equipos por deporte con `external_ids` JSONB para mapear MLB Stats API y
  The Odds API sin acoplar el schema a un proveedor.

### Eventos

- **`events`** — El juego: deporte, home, away, `start_time_utc`, `status`
  (`scheduled/live/final/postponed/cancelled`) y `external_ids` JSONB (`mlb_game_pk`,
  id de The Odds API). El unique `(sport, home, away, start_time_utc)` tolera
  doubleheaders de MLB (mismos equipos, distinta hora). Hay un índice único parcial sobre
  `external_ids->>'mlb_game_pk'` para idempotencia de la ingesta.
- **`event_results`** — Scores finales del juego **y del F5** (1:1 con `events`).
  Existe por dos razones: settlement de picks, y sobre todo **calibración sin sesgo de
  selección**: para medir calibración necesitamos el resultado de *todo lo evaluado*, no
  solo de lo publicado. Es la única tabla "de resultados" mutable (correcciones de la
  fuente oficial), con `updated_at` automático.

### Evidencia de mercado

- **`odds_snapshots`** (append-only) — Una fila por `(event, book, market, side)` en cada
  captura. Guarda precio decimal **y** americano, `implied_prob` generada, `captured_at`
  y el flag `is_closing`. Es la evidencia cruda de "qué odds existían en ese momento";
  por eso jamás se actualiza: una línea que se mueve produce una fila nueva. Un índice
  único parcial garantiza **un solo closing** por `(event, book, market, side)`.

### Modelo y evaluaciones

- **`model_versions`** (catálogo, filas nunca se reescriben en la práctica) — Cada
  artefacto entrenado que produjo un número publicado: `name`, mercado, `trained_at`,
  ventana de entrenamiento (`train_window_start/end`), `metrics` JSONB (log loss, Brier,
  ECE del walk-forward), `git_sha` del código de entrenamiento y método de calibración.
  Sin esto, "el modelo dijo 54%" es inauditable.
- **`feature_snapshots`** (append-only) — El vector exacto de features que vio el modelo:
  `features` JSONB, `feature_hash` (SHA-256 del JSON canónico, con CHECK de formato) y
  `as_of_ts`, el corte de información. El invariante anti-leakage
  (`as_of_ts <= events.start_time_utc`) es cross-table y lo garantiza el feature engine,
  no un CHECK (ver `04-features-y-modelos.md`). Snapshots idénticos se deduplican por
  `(event, market, feature_hash)` y se reutilizan con `ON CONFLICT DO NOTHING`.
- **`predictions`** (append-only) — **Toda** evaluación del modelo, publicada o no:
  `side` (la probabilidad se refiere a P(side gana)), `p_raw` (salida del modelo) y
  `p_calibrated` (tras la capa de calibración; los picks se cortan sobre esta). Dos
  foreign keys compuestas garantizan a nivel de base que la predicción referencia un
  `model_version` del **mismo mercado** y un `feature_snapshot` del **mismo evento y
  mercado** — un cruce imposible queda bloqueado por el DDL, no por disciplina de código.

### Núcleo de auditoría

- **`picks`** (inmutable salvo `status`) — La recomendación publicada, congelada en el
  momento de publicación: FK al `odds_snapshot` exacto del precio tomado, `book`,
  `price_taken_decimal/american`, `p_fair_at_pick` (no-vig multiplicativo sobre el book
  sharp, ver `05-motor-ev-y-bankroll.md`), `edge = p_model − p_fair` (CHECK `> 0`; el
  umbral de publicación ≥ 2% es configurable y vive en la aplicación), `ev_per_unit`,
  `kelly_full` y `stake_suggested_pct`. Este último es la fracción de bankroll bajo el
  perfil **default** (`min(kelly_full × 0.125, 0.02)`); como los picks son globales y no
  por usuario, cada dashboard reescala con el perfil del usuario. `status`:
  `pending/won/lost/push/void`, con transición permitida solo desde `pending`.
- **`pick_results`** (append-only) — Settlement separado del pick para que la fila del
  pick quede congelada: `settled_at`, `result` y `profit_units` por unidad apostada
  (won → `price − 1`, lost → `−1`, push/void → `0`; consistencia forzada por CHECK).
- **`clv_records`** (append-only) — CLV contra el closing sin vig del book de referencia:
  `closing_price_decimal`, `closing_p_fair` y
  `clv_prob_pts = closing_p_fair − 1/price_taken_decimal` (en puntos de probabilidad).
  `beat_close` es redundante a propósito y un CHECK lo fuerza igual a
  `clv_prob_pts > 0`, para que ningún bug de escritura los desalinee.

### Ledger operacional

- **`daily_scans`** — Una fila por corrida del cron y deporte: juegos programados,
  juegos evaluados, candidatos que superaron umbrales y picks publicados, más `details`
  JSONB (razones de skip, créditos de API consumidos). Junto con `predictions` demuestra
  el denominador: *todo lo evaluado queda registrado, no solo lo publicado*. No es
  append-only porque el run actualiza `finished_at` y contadores al cerrar; el registro
  de auditoría fino son las `predictions` de esa corrida.

## Inmutabilidad: cómo se garantiza

Dos funciones trigger en `infra/schema.sql`:

- `edge_forbid_mutation()` — bloquea `UPDATE` y `DELETE` en `odds_snapshots`,
  `feature_snapshots`, `predictions`, `pick_results` y `clv_records`. Corregir un dato de
  auditoría requiere una migración explícita y deliberada (deshabilitar el trigger en una
  transacción documentada), nunca un `UPDATE` casual desde la aplicación.
- `edge_picks_guard()` — en `picks` prohíbe `DELETE`, prohíbe modificar cualquier columna
  distinta de `status`, y solo permite transiciones de estado que salgan de `pending`.
  Un pick liquidado no puede "re-liquidarse" ni editarse.

Esto se probó contra Postgres 16: los cinco escenarios de violación (update de snapshot,
delete de predictions, edición de `edge` en un pick, re-transición `won → lost`, precio
decimal ≤ 1.0) fallan con error, y el flujo legítimo completo (scan → snapshot → predicción
→ pick → settlement → CLV) inserta sin fricción.

## Índices para el dashboard

| Query del dashboard | Índice |
|---|---|
| Feed de picks por fecha | `picks (published_at DESC)` |
| Picks filtrados por estado | `picks (status, published_at DESC)` |
| Picks por book | `picks (book_id, published_at DESC)` |
| Movimiento de línea de un juego | `odds_snapshots (event_id, book_id, market, captured_at DESC)` |
| Lookup del closing | único parcial `odds_snapshots (event_id, book_id, market, side) WHERE is_closing` |
| Slate del día | `events (sport_id, start_time_utc)` + parcial sobre `status IN ('scheduled','live')` |
| Ingesta idempotente de juegos | único parcial sobre `external_ids->>'mlb_game_pk'` |
| Reportes de calibración | `predictions (event_id, market, created_at DESC)` y `predictions (model_version_id, created_at DESC)` |
| Performance agregada mensual | `pick_results (settled_at)` + vista `v_monthly_performance` |

Dos vistas empaquetan los joins recurrentes:

- **`v_prediction_outcomes`** — resultado realizado de **cada** predicción (`side_won`
  true/false, `NULL` si el juego no terminó o el mercado empujó, p. ej. empate en F5).
  Base de todos los reportes de calibración libres de sesgo de selección.
- **`v_monthly_performance`** — picks liquidados por mes: ganados/perdidos, unidades
  netas, yield por unidad y CLV beat-rate en una sola consulta.

## Queries clave (validadas contra el schema)

### 1. Audit trail completo de un pick

Reconstruye los siete elementos de auditoría de un pick en una sola consulta:

```sql
SELECT
    pk.id                       AS pick_id,
    s.key                       AS sport,
    ht.name                     AS home,
    at.name                     AS away,
    e.start_time_utc,
    p.market,
    p.side,
    -- (1) odds al momento: book, precio, timestamp
    b.key                       AS book,
    os.price_decimal            AS price_at_pick,
    os.price_american,
    os.implied_prob             AS implied_prob_at_pick,
    os.captured_at              AS odds_captured_at,
    -- (2) versión de modelo
    mv.name                     AS model_version,
    mv.git_sha,
    mv.trained_at,
    -- (3) features exactas que vio
    fs.feature_hash,
    fs.as_of_ts                 AS features_as_of,
    fs.features,
    -- (4) probabilidades producidas
    p.p_raw,
    p.p_calibrated,
    -- (5) recomendación congelada
    pk.p_fair_at_pick,
    pk.edge,
    pk.ev_per_unit,
    pk.kelly_full,
    pk.stake_suggested_pct,
    pk.published_at,
    pk.status,
    -- (6) línea de cierre y CLV
    c.closing_price_decimal,
    c.closing_p_fair,
    c.clv_prob_pts,
    c.beat_close,
    -- (7) resultado
    r.result,
    r.profit_units,
    r.settled_at
FROM picks pk
JOIN predictions p        ON p.id  = pk.prediction_id
JOIN events e             ON e.id  = p.event_id
JOIN sports s             ON s.id  = e.sport_id
JOIN teams ht             ON ht.id = e.home_team_id
JOIN teams at             ON at.id = e.away_team_id
JOIN books b              ON b.id  = pk.book_id
JOIN odds_snapshots os    ON os.id = pk.odds_snapshot_id
JOIN model_versions mv    ON mv.id = p.model_version_id
JOIN feature_snapshots fs ON fs.id = p.feature_snapshot_id
LEFT JOIN clv_records c   ON c.pick_id = pk.id
LEFT JOIN pick_results r  ON r.pick_id = pk.id
WHERE pk.id = :pick_id;
```

Los `LEFT JOIN` de CLV y resultado permiten auditar picks aún pendientes.

### 2. CLV beat-rate mensual

CLV en puntos de probabilidad (`closing_p_fair − 1/price_taken`) y porcentaje de picks
que le ganaron al cierre — la métrica adelantada de si el proceso tiene edge real,
antes de que la varianza del resultado converja (ver `06-backtesting-y-metricas.md`):

```sql
SELECT
    date_trunc('month', pk.published_at)::date                   AS month,
    count(*)                                                     AS picks_with_clv,
    round(avg(c.clv_prob_pts), 4)                                AS avg_clv_prob_pts,
    round(avg(CASE WHEN c.beat_close THEN 1.0 ELSE 0.0 END), 4)  AS beat_rate
FROM clv_records c
JOIN picks pk ON pk.id = c.pick_id
GROUP BY 1
ORDER BY 1;
```

### 3. Calibración por bucket (sobre TODO lo evaluado)

Corre sobre `v_prediction_outcomes`, es decir, sobre todas las predicciones con resultado
— no solo los picks publicados. `gap ≈ 0` en cada bucket es lo que significa "modelo
calibrado"; este query alimenta el reporte de ECE en ventana rolling:

```sql
SELECT
    width_bucket(p_calibrated, 0, 1, 10)                              AS bucket,
    count(*)                                                          AS n,
    round(avg(p_calibrated), 4)                                       AS avg_model_prob,
    round(avg(CASE WHEN side_won THEN 1.0 ELSE 0.0 END), 4)           AS observed_freq,
    round(avg(CASE WHEN side_won THEN 1.0 ELSE 0.0 END)
          - avg(p_calibrated), 4)                                     AS gap
FROM v_prediction_outcomes
WHERE side_won IS NOT NULL          -- excluye juegos sin terminar y pushes de F5
GROUP BY 1
ORDER BY 1;
```

### 4. Control de sesgo de selección

Cuánto se evaluó vs cuánto se publicó, por corrida del cron:

```sql
SELECT
    ds.scan_date,
    s.key AS sport,
    ds.games_scheduled,
    ds.games_evaluated,
    ds.candidates_over_threshold,
    ds.picks_published,
    round(ds.picks_published::numeric / nullif(ds.games_evaluated, 0), 3) AS publish_rate
FROM daily_scans ds
JOIN sports s ON s.id = ds.sport_id
ORDER BY ds.scan_date DESC;
```

## Decisiones de diseño tomadas en este schema

1. **`event_results` y `books` se agregaron** al conjunto mínimo de entidades. Sin scores
   por evento no hay settlement ni calibración sobre lo no publicado (el requisito
   anti-sesgo lo exige); `books` normaliza el book de referencia (`is_sharp`) en lugar de
   repetir strings.
2. **`predictions` referencia su `feature_snapshot` y lleva `side`.** El requisito de
   auditoría ("qué features vio") obliga al FK; sin `side`, `p_calibrated` sería ambigua.
3. **`picks` referencia el `odds_snapshot` exacto** además de copiar precio/book: la
   copia congela el valor, el FK prueba su procedencia.
4. **FKs compuestas** (`predictions → model_versions (id, market)` y
   `predictions → feature_snapshots (id, event_id, market)`) hacen imposible a nivel de
   base cruzar un modelo de moneyline con features de F5.
5. **`stake_suggested_pct` es global bajo el perfil default** (Kelly/8, cap 2%); el stake
   por usuario se calcula en render con `users.bankroll/kelly_fraction/stake_cap_pct`.
   Los picks no son por tenant: publicar N copias del mismo pick rompería la auditoría.
6. **El invariante as-of no es un CHECK**: `as_of_ts <= events.start_time_utc` es
   cross-table; lo garantiza el feature engine y lo verifica el backtest
   (`04-features-y-modelos.md`). Un CHECK contra `created_at` se descartó por frágil
   (clock skew, backfills históricos).
7. **Los umbrales de publicación no viven en el DDL.** La base fuerza lo estructural
   (`edge > 0`, probabilidades en [0,1]); el umbral `edge ≥ 2%`, `EV ≥ +2%` y
   `ECE ≤ 0.03` es configuración de la aplicación y puede cambiar sin migración.
8. **Enums de Postgres** en vez de tablas lookup para `market_code`/`status`: los valores
   son parte del contrato del dominio y fase 2 los extiende con
   `ALTER TYPE ... ADD VALUE 'nrfi'` sin reescribir datos.

## Extensiones previstas (fase 2+)

- Nuevos mercados: `ALTER TYPE market_code ADD VALUE` (`nrfi`, `pitcher_ks`, `total`);
  los lados `over/under/yes/no` ya existen en `outcome_side`.
- Props de jugadores: requerirá una tabla `players` y una referencia opcional
  `player_id` en `predictions`/`odds_snapshots`; se diseña cuando llegue, no antes.
- Si `odds_snapshots` crece (varios books × mercados × capturas diarias), particionar
  por rango de `captured_at` es el camino natural; el MVP con MLB no lo necesita.
- Métodos de no-vig alternativos (Shin, power) solo cambian cómo la aplicación calcula
  `p_fair_*`; el schema no cambia (ver `05-motor-ev-y-bankroll.md`).

## Referencias

- Decisiones de alcance: `00-decisiones.md`
- Arquitectura y flujo end-to-end: `01-propuesta-tecnica.md`
- Fuentes que alimentan estas tablas: `02-fuentes-de-datos.md`
- Features, calibración y anti-leakage: `04-features-y-modelos.md`
- Definiciones de no-vig, edge, EV, Kelly y CLV: `05-motor-ev-y-bankroll.md`
- Métricas y criterios go/no-go: `06-backtesting-y-metricas.md`
- DDL ejecutable: `infra/schema.sql`
