# PLAN — Estado operativo y siguiente jugada

> Documento vivo: refleja **dónde está el proyecto hoy y qué sigue**. El plan
> maestro por fases con criterios de salida completos vive en
> `docs/07-roadmap.md`; los umbrales go/no-go en `docs/06-backtesting-y-metricas.md`.
> Actualizar este archivo en cada hito.

**Última actualización**: 2026-07-08 · **Fase actual: F0 (fundaciones) — EN PRODUCCIÓN**

---

## 1. Dónde estamos

La infraestructura completa quedó operando el 2026-07-08:

- [x] Propuesta técnica completa (`docs/00` a `docs/07`), verificada (fórmulas, pricing, anti-leakage)
- [x] Schema Postgres con auditoría inmutable (`infra/schema.sql`) aplicado en Supabase
- [x] Backend FastAPI con core cuantitativo real y testeado (devig, EV, Kelly, CLV)
- [x] Dashboard Next.js scaffold (build verde)
- [x] Jobs de ingesta con tests de integración contra Postgres real (109 tests)
- [x] Crons en GitHub Actions (`.github/workflows/ingesta.yml`) con secrets configurados
- [x] Primer slate sincronizado (15 juegos) y primer snapshot real: **1,168 precios archivados**, F5 incluido, cero errores (run #6)
- [ ] **Reloj de F0 corriendo**: 14 días de snapshots sin huecos (inicio 2026-07-08 → meta ≈ 2026-07-22)
- [ ] Decisión de créditos tomada (variable `ODDS_INCLUDE_F5=false` **o** plan 20K de The Odds API)
- [ ] Backfill histórico 2018-2025 corrido (habilita F1)

## 2. Cómo trabajamos (ramas y reparto)

**Ramas** — flujo de dos ramas, simple a propósito:

| Rama | Qué es | Regla |
|---|---|---|
| `main` | **Producción.** Lo que ejecutan los crons de Actions cada día. | Solo recibe código vía Pull Request. Es la fuente de verdad. |
| `claude/repo-cleanup-p72947` | **Rama de trabajo de Claude.** Cada tanda de cambios se desarrolla y testea aquí. | Tras cada merge se reinicia desde `main`. Si la ves "atrasada" o igual a `main`, es normal: significa que todo lo suyo ya se mergeó. |

Ciclo: Claude push a su rama → PR a `main` → merge → los crons toman el código nuevo en su siguiente corrida. Nunca se pushea directo a `main`.

**Reparto de responsabilidades:**

| | Owner (tú) | Claude |
|---|---|---|
| Cuentas, pagos, API keys, secrets | ✔ | |
| Merges de PRs (o pedir que los haga) | ✔ | (con tu ok) |
| Lanzar backfills en Actions | ✔ | |
| Código, tests, fixes, docs, PRs | | ✔ |
| Diagnóstico de corridas rojas | | ✔ |

## 3. Siguiente jugada (esta semana)

1. **(Tú — urgente)** Decidir créditos: crear la Variable `ODDS_INCLUDE_F5=false` en Actions **o** contratar el plan 20K (~$30/mes). Sin decisión, el free tier se agota ≈ 4 días y las corridas fallarán por cuota.
2. **(Tú)** Correr los backfills, una temporada por corrida: Actions → Ingesta EDGE → Run workflow → `backfill_results` → args `--start-date 2024-03-20 --end-date 2024-10-01` (repetir para 2018…2025). Gratis, ~minutos cada uno.
3. **(Claude — desbloqueado por el paso 2)** Pipeline de entrenamiento F1:
   - Features de abridor (K-BB%, xFIP as-of) sobre game logs — hoy solo existen los bloques de forma de equipo (`app/features/builder.py`).
   - Baseline market prior + regresión logística + XGBoost, walk-forward 2018→2025.
   - Calibración (Platt) + gate duro: **si no bate el log loss del market prior, no se publica** (`docs/04 §2.4`).
4. **(Ambos)** Vigilar el archivo de snapshots: query de huecos en `docs/07` / README de `apps/api`. 14 días limpios cierran F0.

## 4. Fases y gates (resumen — detalle en `docs/07-roadmap.md`)

| Fase | Qué es | Criterio de salida (gate) | Estado |
|---|---|---|---|
| **F0 Fundaciones** | Ingesta + archivo de líneas propio | 14 días de snapshots sin huecos | 🟢 **en curso** (día 1/14) |
| **F1 Modelo** | Features as-of, entrenamiento, calibración, backtest | log loss < market prior en walk-forward **y** ECE ≤ 0.03 | ⏳ bloqueada por backfill |
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
