# PLAN — Estado operativo y siguiente jugada

> Documento vivo: refleja **dónde está el proyecto hoy y qué sigue**. El plan
> maestro por fases con criterios de salida completos vive en
> `docs/07-roadmap.md`; los umbrales go/no-go en `docs/06-backtesting-y-metricas.md`.
> Actualizar este archivo en cada hito.

**Última actualización**: 2026-07-09 (noche) · **Fase actual: F0 (reloj reiniciado 2026-07-10) + F1 con bloque de abridor en producción**

---

## 1. Dónde estamos

- [x] Propuesta técnica completa (`docs/00` a `docs/07`), verificada (fórmulas, pricing, anti-leakage)
- [x] Schema Postgres con auditoría inmutable (`infra/schema.sql`) aplicado en Supabase
- [x] Backend FastAPI con core cuantitativo real y testeado (devig, EV, Kelly, CLV)
- [x] Dashboard Next.js scaffold (build verde)
- [x] Crons en GitHub Actions (`.github/workflows/ingesta.yml`) con secrets configurados
- [x] **Decisión de créditos tomada**: variable `ODDS_INCLUDE_F5=false` en Actions (free tier; el plan 20K queda para cuando el modelo lo justifique)
- [x] **Backfill histórico 2018–2026 completo**: 19,277 juegos de temporada regular con resultado y parciales F5 (corridas #7–#16, con dos bugs de producción diagnosticados y corregidos en el camino: PRs #5 y #6)
- [x] **Tanda F1 en producción** (PR #9, 2026-07-09): migración 003 aplicada, backfill de pitcheo 2018–2026 completo (9 corridas verdes, ~124K líneas, 0 anomalías de parseo, 0 eventos faltantes), features de abridor en builder y dataset (paridad testeada), crons robustos, `audit_snapshots`, tubería del market prior. 160 tests.
- [x] **Pipeline F1 v2 corrido** (run #34, con bloque de abridor, sp_coverage 0.928 — el ~7% restante son arranques fríos legítimos: abril 2018 sin liga previa, debuts): mejora el log loss en las 10 celdas del walk-forward vs v1. Moneyline logística calibrada: 0.6814/0.6836/0.6840/0.6859/0.6910 (2022–2026) — ahora SÍ bate al home_rate también en 2026. F5 mejora el doble que ML (0.6739/0.6818/0.6795/0.6843/0.6947), como predecía docs/04 §1.3; en F5-2026 parcial el mejor es hist_gb (0.6916 ≈ home_rate 0.6916). ECE ≤ 0.031 en todo.
- [ ] **Reloj de F0 REINICIADO 2026-07-10 → meta ≈ 2026-07-24**: el primer `audit_snapshots` (run #36) confirmó huecos reales los días 1–2 causados por los retrasos de los crons `:00` viejos (el snapshot de las 15:00 del 07-09 disparó a las 17:24, después de los juegos tempranos). La cadencia nueva (sync+snapshot a las XX:23, audit diario `--fail-on-gaps` en el cron de 14:17) corre desde el merge — el reloj limpio empieza el 07-10.
- [ ] **Gate real de F1 pendiente de datos**: batir el log loss del market prior (docs/04 §2.4) exige odds pre-juego archivadas. Con ~14 juegos/día con snapshot sharp, el subconjunto 2026 alcanza n≥200 hacia ~2026-07-24 (coincide con el cierre de F0) — la primera evaluación real del gate saldrá sola en el train_f1 de esa fecha.

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

1. **(Ambos)** Vigilar 1–2 días la cadencia nueva: cron diario de 14:17 UTC con sus 4 pasos (sync + resultados 3d + pitcheo 3d + audit de ayer con `--fail-on-gaps`) y snapshots a las XX:23 precedidos de sync. Si el audit pinta rojo, diagnóstico antes que nada.
2. **(Automático)** El train_f1 de ~2026-07-24 trae la primera evaluación real del gate (market prior n≥200 en 2026) y coincide con el cierre del reloj F0. No hay nada que hacer más que dejar que el archivo crezca.
3. **(Claude — siguiente tanda, cuando digas)** Con el bloque de abridor rindiendo, las opciones en orden de valor esperado:
   - **Bloque de bullpen** (docs/04 §1.4, solo ML): `pitching_game_logs` ya guarda TODAS las líneas (relevistas incluidos) — es feature engineering puro, sin ingesta nueva.
   - **Ofensiva real de equipo** (docs/04 §1.2): wOBA/ISO/K%/BB% exigen ingerir líneas de bateo (extensión natural de `backfill_pitching` a boxscore completo).
   - Interacción mano del abridor × splits (necesita lo anterior).
4. **(Tú — opcional)** Si el gate de ~07-24 se ve prometedor, considerar el plan 20K de The Odds API para activar los closing runs (CLV real, hoy comentados en el workflow).

## 4. Fases y gates (resumen — detalle en `docs/07-roadmap.md`)

| Fase | Qué es | Criterio de salida (gate) | Estado |
|---|---|---|---|
| **F0 Fundaciones** | Ingesta + archivo de líneas propio | 14 días de snapshots sin huecos | 🟢 **en curso** (reloj reiniciado 07-10 → meta 07-24; cadencia nueva + audit diario) |
| **F1 Modelo** | Features as-of, entrenamiento, calibración, backtest | log loss < market prior en walk-forward **y** ECE ≤ 0.03 | 🟡 features de abridor en producción y rindiendo; gate esperando archivo de odds (n≥200 ≈ 07-24) |
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
| 2026-07-09 | **Tanda F1 mergeada** (PR #9): ingesta de pitcheo + features de abridor + crons robustos + audit de huecos + tubería market prior. 160 tests; revisión adversarial con 10 fixes |
| 2026-07-09 | Migración 003 aplicada y **backfill de pitcheo 2018–2026 completo** en Actions (9 corridas verdes, ~124K líneas, 0 anomalías) |
| 2026-07-09 | **train_f1 v2 (run #34)**: el bloque de abridor mejora el log loss en las 10 celdas del walk-forward; F5 mejora el doble que ML; en 2026 la logística ya bate al baseline. sp_coverage 0.928 |
| 2026-07-09 | Primer `audit_snapshots` (run #36): huecos reales días 1–2 por los crons `:00` viejos → **reloj F0 reiniciado: 2026-07-10 → meta 2026-07-24** |
