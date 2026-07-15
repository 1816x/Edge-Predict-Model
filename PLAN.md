# PLAN — Estado operativo y siguiente jugada

> Documento vivo: refleja **dónde está el proyecto hoy y qué sigue**. El plan
> maestro por fases con criterios de salida completos vive en
> `docs/07-roadmap.md`; los umbrales go/no-go en `docs/06-backtesting-y-metricas.md`.
> Actualizar este archivo en cada hito.

**Última actualización**: 2026-07-14 · **Fase actual: F0 (día 5/14 del reloj limpio) + F1 con bloques de abridor, bullpen y ofensiva de equipo en producción**

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
- [x] **Tanda F1.1 en producción** (PR #11, 2026-07-10): bloque de bullpen (docs/04 §1.4) sobre las líneas de relevistas ya archivadas — `bullpen_ip_l3d`, `bullpen_b2b_flag`, `bullpen_xfip_30d`, `bullpen_ip_expected`, ventanas por día UTC terminando ayer (regla intradía-segura), SOLO Moneyline (F5 lo excluye por diseño; primeras columnas por-mercado: 40 vs 32). 168 tests; revisión adversarial con 1 fix real (sin archivo de relevistas vivo el bloque es None/NaN, nunca ceros fabricados). `bullpen_coverage` 0.9994.
- [x] **train_f1 v3 corrido** (run #37): ML mejora en 2022–2025 (logística calibrada 0.6804/0.6832/0.6829/0.6855; la mejor celda es hist_gb 2025 con −0.0020) y empeora levemente en 2026 parcial (+0.0012, dentro de lo esperable para 1,371 juegos). El control F5 quedó idéntico al run #34 dentro del ruido de corrida (±0.0002, lbfgs sin converger en runners distintos) — la exclusión por diseño funciona. Nota técnica pendiente: escalar features para que la logística converja (ConvergenceWarning en cada corrida).
- [x] **Tanda F1.2 en producción** (PR #16, 2026-07-14): **bloque de ofensiva de equipo** (docs/04 §1.2) — `team_woba_30d`, `team_woba_season`, `team_woba_vs_opp_hand_30d` (shrunk), `team_iso_30d`, `team_k_pct_30d`, `team_bb_pct_30d`, `team_ops_30d`, en AMBOS mercados (54/46 columnas). Migración 004 (`batting_game_logs` por jugador-juego, desbloquea §1.5 sin re-ingerir), fórmulas compartidas en `app/features/offense.py` (constantes wOBA fijas FanGraphs 2017, as-of-válidas), ventanas por día UTC terminando ayer, split vs mano por proxy de abridor rival. También: crons redundantes `:23/:53` + snapshot en el cron diario (fix del diagnóstico de los audits rojos 07-10/11/12: un juego temprano/día sin cobertura por el retraso del slot único de 15:23), y `logistic_scaled` (escalado que destraba la convergencia). 186 tests; revisión adversarial de 4 dimensiones con 2 bugs confirmados ejecutando el parser (conteos negativos a la puerta, PA basura no-numérica) + 4 endurecimientos.
- [x] **Backfill de bateo 2018–2026 completo** (runs #68–#80, 9 corridas verdes): **~396K líneas, 0 anomalías** en todo el corpus — validación anti-drift de nombres de campo contra la API real (el smoke del 07-12 dio 0 anomalías sobre 306 líneas). skip-existing por tabla: el pitcheo intacto, solo se escribió bateo. 2020 corta (899 juegos) y el DH universal 2022+ (menos zero-PA) confirmados en los summaries.
- [x] **train_f1 v4 corrido** (run #81): **offense_coverage 0.9925/0.9929** (de 0 a casi total). El efecto en log loss es MARGINAL y honesto: en las celdas limpias 2022–2025 el bloque ofensivo mueve la logística sin escalar en promedio ≈ −0.00016 (nivel de ruido, net levemente positivo); F5 más limpio (mejora 2/4, ruido 2/4). El escalado es un wash en LL pero elimina el ConvergenceWarning. **Lectura**: la ofensiva a nivel de agregado de equipo es marginal porque la forma de equipo ya capturaba gran parte de la señal; el pago real llega con el bloque de lineup (§1.5, wOBA por bateador ponderada por orden real), que esta tanda deja habilitado sin re-ingerir. El gate 2026 ya tiene n=71 (los modelos baten al prior ahí, pero n<200 → NO evaluado, publicación sigue bloqueada), en camino a n≥200 hacia ~07-24.
- [ ] **Reloj de F0 REINICIADO 2026-07-10 → meta ≈ 2026-07-24**: el primer `audit_snapshots` (run #36) confirmó huecos reales los días 1–2 causados por los retrasos de los crons `:00` viejos (el snapshot de las 15:00 del 07-09 disparó a las 17:24, después de los juegos tempranos). La cadencia nueva (sync+snapshot a las XX:23/:53, snapshot también en el cron diario 14:17, audit diario `--fail-on-gaps`) corre desde el 07-14. **Ojo**: los audits del 07-10/11/12 salieron rojos por UN juego temprano/día sin cobertura (diagnóstico y fix en la tanda F1.2); el reloj sin huecos efectivo empieza a contar tras ese fix — vigilar el email del audit del 07-15 en adelante.
- [ ] **Gate real de F1 pendiente de datos**: batir el log loss del market prior (docs/04 §2.4) exige odds pre-juego archivadas. Con ~14 juegos/día con snapshot sharp, el subconjunto 2026 alcanza n≥200 hacia ~2026-07-24 (coincide con el cierre de F0) — la primera evaluación real del gate saldrá sola en el train_f1 de esa fecha.

## 2. Cómo trabajamos (ramas y reparto)

**Ramas** — flujo de dos ramas, simple a propósito:

| Rama | Qué es | Regla |
|---|---|---|
| `main` | **Producción.** Lo que ejecutan los crons de Actions cada día. | Solo recibe código vía Pull Request. Es la fuente de verdad. |
| rama de trabajo de Claude (hoy: `claude/project-continuation-plan-9546oi`) | Cada tanda de cambios se desarrolla y testea aquí. | El nombre cambia por sesión. Tras cada merge, la siguiente tanda parte de `main`. |

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

1. **(Claude — SIGUIENTE TANDA)** **Bloque de lineup** (docs/04 §1.5), el pago real que la F1.2 dejó habilitado. La tabla `batting_game_logs` ya guarda `batting_order` por jugador-juego, así que NO hay que re-ingerir: se construyen `lineup_woba_proj` (wOBA as-of por bateador ponderada por el orden) y `top4_woba_vs_hand` (crítico para F5: la primera vuelta concentra los PA de las entradas 1-5), con el flag honesto `is_confirmed`. Esto es lo que convierte la ofensiva de "agregado de equipo marginal" (medido en v4) en señal por-bateador. Extiende el vector en ambos mercados; F5 lo sobrepondera (§1.9). Requiere archivar el lineup as-of publicado (nuevo, hacia adelante) y, para backtest, el orden del box score con la limitación honesta de que no había lineup confirmado archivado (mismo supuesto documentado que los probables del bloque abridor). Reusar el patrón de paridad online/bulk y las fórmulas de `app/features/offense.py`.
2. **(Después, misma o próxima tanda)** Retirar la logística SIN escalar de `train.py` (cumplió su turno de atribución en v4); dejar solo `logistic_scaled` y `hist_gb`. Hay un test que protege el markdown (`test_markdown_summary_renders_every_model_column`) — actualizarlo al quitar el modelo.
3. **(Ambos)** Vigilar el email del audit diario (~8:17 AM CST). **Los rojos del 07-10/11/12 ya están diagnosticados y corregidos** (snapshot en el cron diario). Rojo del 07-15 en adelante = diagnóstico antes que nada; si sale verde varios días, el reloj F0 sin huecos por fin corre limpio.
4. **(Automático)** El train_f1 de ~2026-07-24 trae la primera evaluación real del gate (market prior n≥200 en 2026; hoy va en n=71) y coincide con el cierre del reloj F0. Dejar que el archivo crezca.
5. **(Después, tandas propias)** Interacción mano del abridor × splits (ya hay bateo + splits vs mano); `closer_available_flag` (§1.4 restante, exige transacciones/IL); park factors y clima (§1.6-1.7).
6. **(Tú — opcional)** Si el gate de ~07-24 se ve prometedor, considerar el plan 20K de The Odds API para activar los closing runs (CLV real, hoy comentados en el workflow).

## 4. Fases y gates (resumen — detalle en `docs/07-roadmap.md`)

| Fase | Qué es | Criterio de salida (gate) | Estado |
|---|---|---|---|
| **F0 Fundaciones** | Ingesta + archivo de líneas propio | 14 días de snapshots sin huecos | 🟢 **en curso** (reloj 07-10 → meta 07-24; cadencia :23/:53 + snapshot en cron diario tras el fix del 07-14; audit diario) |
| **F1 Modelo** | Features as-of, entrenamiento, calibración, backtest | log loss < market prior en walk-forward **y** ECE ≤ 0.03 | 🟡 abridor+bullpen+ofensiva en producción; falta lineup (§1.5) para que la ofensiva rinda; gate esperando archivo de odds (n=71→200 ≈ 07-24) |
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
| 2026-07-10 | **Tanda F1.1 mergeada** (PR #11): bloque de bullpen solo-ML con columnas por-mercado; revisión adversarial (1 fix: None/NaN sin archivo vivo). train_f1 v3 (run #37): ML mejora 2022–2025, control F5 intacto |
| 2026-07-14 | **Tanda F1.2 mergeada** (PR #16): bloque de ofensiva de equipo (§1.2) en ambos mercados, migración 004 `batting_game_logs`, crons `:23/:53`+snapshot diario (fix audits rojos), `logistic_scaled`. 186 tests; revisión adversarial de 4 dimensiones (2 bugs confirmados ejecutando el parser + 4 endurecimientos) |
| 2026-07-14 | **Backfill de bateo 2018–2026 completo** (runs #68–#80): ~396K líneas, 0 anomalías; smoke real del 07-12 validó nombres de campo contra la API. train_f1 v4 (run #81): offense_coverage 0.9925 — efecto marginal y honesto (la forma de equipo ya capturaba la señal); el pago real espera al bloque de lineup §1.5. Gate 2026 en n=71/200 |
