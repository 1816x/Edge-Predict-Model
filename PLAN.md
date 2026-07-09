# PLAN — Estado operativo y siguiente jugada

> Documento vivo: refleja **dónde está el proyecto hoy y qué sigue**. El plan
> maestro por fases con criterios de salida completos vive en
> `docs/07-roadmap.md`; los umbrales go/no-go en `docs/06-backtesting-y-metricas.md`.
> Actualizar este archivo en cada hito.

**Última actualización**: 2026-07-09 · **Fase actual: F0 (día 2/14) + F1 en construcción**

---

## 1. Dónde estamos

- [x] Propuesta técnica completa (`docs/00` a `docs/07`), verificada (fórmulas, pricing, anti-leakage)
- [x] Schema Postgres con auditoría inmutable (`infra/schema.sql`) aplicado en Supabase
- [x] Backend FastAPI con core cuantitativo real y testeado (devig, EV, Kelly, CLV)
- [x] Dashboard Next.js scaffold (build verde)
- [x] Crons en GitHub Actions (`.github/workflows/ingesta.yml`) con secrets configurados
- [x] **Decisión de créditos tomada**: variable `ODDS_INCLUDE_F5=false` en Actions (free tier; el plan 20K queda para cuando el modelo lo justifique)
- [x] **Backfill histórico 2018–2026 completo**: 19,277 juegos de temporada regular con resultado y parciales F5 (corridas #7–#16, con dos bugs de producción diagnosticados y corregidos en el camino: PRs #5 y #6)
- [x] **Pipeline F1 v1 corrido** (run #18): walk-forward 2022–2026, logística + HistGB + calibración Platt. Con solo forma de equipo (18 features) la logística calibrada mejora ~0.005 de log loss al baseline home_rate en 2022–2025 y NO lo bate en 2026 parcial. ECE 0.01–0.03. **Conclusión: faltan features de abridor** (el bloque más importante según docs/04 §1.3) — es la tanda en curso.
- [ ] **Reloj de F0 corriendo**: 14 días de snapshots sin huecos (inicio 2026-07-08 → meta ≈ 2026-07-22). Riesgo detectado: GitHub retrasa los crons del minuto :00 hasta 4 h; mitigado moviéndolos a minutos raros (14:17, XX:23) y con el job `audit_snapshots` como detector de huecos.
- [ ] **Gate real de F1 pendiente de datos**: batir el log loss del market prior (docs/04 §2.4) exige odds pre-juego archivadas; nuestro archivo empezó 2026-07-08. La tubería de evaluación ya queda lista y el gate se evalúa solo cuando el subconjunto con odds alcance n≥200 (~2 meses de F0/F2).

## 2. Cómo trabajamos (ramas y reparto)

**Ramas** — flujo de dos ramas, simple a propósito:

| Rama | Qué es | Regla |
|---|---|---|
| `main` | **Producción.** Lo que ejecutan los crons de Actions cada día. | Solo recibe código vía Pull Request. Es la fuente de verdad. |
| rama de trabajo de Claude (hoy: `claude/project-continuation-6xswqq`) | Cada tanda de cambios se desarrolla y testea aquí. | El nombre cambia por sesión. Tras cada merge, la siguiente tanda parte de `main`. |

Ciclo: Claude push a su rama → PR a `main` → merge → los crons toman el código nuevo en su siguiente corrida. Nunca se pushea directo a `main`.

**Reparto de responsabilidades:**

| | Owner (tú) | Claude |
|---|---|---|
| Cuentas, pagos, API keys, secrets | ✔ | |
| Merges de PRs (o pedir que los haga) | ✔ | (con tu ok) |
| Lanzar backfills/migraciones en Actions | ✔ | |
| Código, tests, fixes, docs, PRs | | ✔ |
| Diagnóstico de corridas rojas | | ✔ |

## 3. Siguiente jugada (esta semana)

1. **(Claude — en curso, esta tanda)** Bloque de abridor end-to-end:
   - Migración 003: tablas `players`, `pitching_game_logs`, `event_probables`.
   - Job `backfill_pitching` (boxscores de MLB Stats API; también corre a diario tras el sync).
   - Features `sp_kbb_pct_*`, `sp_xfip_*`, `sp_days_rest`, `sp_pitch_count_l2_starts`, `sp_is_lhp` as-of estrictas, en el builder online y en el dataset bulk con paridad testeada.
   - `audit_snapshots` (detector de huecos F0) y tubería del market prior en `train_f1`.
2. **(Tú — al mergear el PR de esta tanda)** En Actions, en este orden:
   1. `apply_migration` con args `--file infra/migrations/003-pitching-and-probables.sql` (puede correrse desde la rama del PR incluso antes del merge: es aditiva e idempotente).
   2. `backfill_pitching`, una temporada por corrida (mismos rangos que los backfills de resultados: 2018-03-29→2018-10-01, 2019-03-20→2019-09-29, 2020-07-23→2020-09-27, 2021-04-01→2021-10-03, 2022-04-07→2022-10-05, 2023-03-30→2023-10-01, 2024-03-20→2024-09-30, 2025-03-18→2025-09-28, 2026-03-25→ayer). Si una corrida muere por timeout, relanzar con los mismos args: retoma donde quedó.
   3. `train_f1` — verificar en el summary que `sp_coverage` >95% y comparar el log loss contra la corrida #18.
   4. `audit_snapshots` — primer reporte de huecos del archivo F0.
3. **(Ambos)** Vigilar 1–2 días que el cron diario de 14:17 UTC corra sus tres pasos (sync + resultados + pitcheo) y que los snapshots salgan a XX:23.

## 4. Fases y gates (resumen — detalle en `docs/07-roadmap.md`)

| Fase | Qué es | Criterio de salida (gate) | Estado |
|---|---|---|---|
| **F0 Fundaciones** | Ingesta + archivo de líneas propio | 14 días de snapshots sin huecos | 🟢 **en curso** (día 2/14) |
| **F1 Modelo** | Features as-of, entrenamiento, calibración, backtest | log loss < market prior en walk-forward **y** ECE ≤ 0.03 | 🟡 en construcción (bloque abridor); gate esperando archivo de odds (n≥200) |
| **F2 Paper trading** | Picks registrados sin dinero, dashboard interno | ≥300 picks y evaluación go/no-go de `docs/06` | — |
| **F3 SaaS beta** | Auth, suscripciones, disclaimers | **Solo si F2 pasa.** Revisión legal ANTES de cobrar | — |
| **F4 Más mercados MLB** | NRFI/YRFI, K's de pitchers | Cada mercado repite gates F1/F2 | — |
| **F5 Multi-deporte** | NFL/Champions (sept), NBA/NHL (oct) | Reusar pipeline; features nuevas por deporte | — |

**El proyecto se mata sin drama** si tras ≥300 picks de paper trading no bate el CLV ni el log loss del mercado — costo hundido acotado (~$150-200 + tiempo). Eso no es pesimismo: es la diferencia entre un sistema cuantitativo y un tipster.

## 5. Reglas que no se negocian

- Winrate no es la métrica: calibración (Brier/log loss/ECE), EV, yield y **CLV**.
- El LLM explica; jamás genera probabilidades ni decide picks.
- Features as-of estrictas; nada que no fuera conocible antes del primer pitch.
- Todo lo evaluado se registra (no solo lo publicado) — anti sesgo de selección.
- No se promete rentabilidad. Nunca.

## 6. Log de hitos

| Fecha | Hito |
|---|---|
| 2026-07-07 | Propuesta técnica completa + scaffold, verificación adversarial (2 errores numéricos corregidos pre-push) |
| 2026-07-08 | Capa de ingesta + backfill + feature builder + crons; 109 tests en verde |
| 2026-07-08 | Supabase provisionado, secrets configurados, `main` establecida |
| 2026-07-08 | Bug 422 del F5 (endpoint por-evento) diagnosticado en producción y corregido (PR #1) |
| 2026-07-08 | **F0 en producción**: run #6 verde — 15 eventos, 1,168 snapshots, F5 completo, 0 errores |
| 2026-07-08 | Pipeline F1 walk-forward (PR #3) + hardening de backfill en producción: concurrencia (PR #4), bulk 5 stmts/chunk (PR #5), dedupe suspendidos (PR #6), solo temporada regular (PR #7), fix json int32 (PR #8) |
| 2026-07-08 | **Backfill 2018–2026 completo** (19,277 juegos) y **primer train_f1 verde** (run #18): señal marginal con solo forma de equipo; gate vs market prior no evaluable aún |
| 2026-07-09 | Tanda F1 en curso: ingesta de pitcheo + features de abridor + crons robustos + audit de huecos + tubería market prior |
