# EDGE — Plataforma cuantitativa de decisión para apuestas deportivas

> Codename provisional. El repo se reutilizó de un proyecto anterior (estaba vacío); renombrar
> repo y producto cuando el owner elija marca.

Sistema SaaS que estima **probabilidades calibradas** para mercados deportivos, las compara
contra la **probabilidad implícita sin vig** del mercado y detecta apuestas con **expected
value positivo**, con gestión de riesgo (Kelly fraccional) y trazabilidad completa de cada pick.

**Lo que esto NO es**: una máquina de picks seguros ni una promesa de rentabilidad. Es una
plataforma de decision support: probabilidad modelada vs mercado, edge, EV, CLV y auditoría.
El producto es informativo — no coloca apuestas ni custodia fondos.

## MVP

**MLB: Moneyline + First-5-Innings Moneyline** (decisión razonada en `docs/00-decisiones.md`
y `docs/01-propuesta-tecnica.md`). Fase 2: NRFI/YRFI y strikeouts de pitchers. Después:
NFL/Champions (sept 2026), NBA/NHL (oct 2026).

## Flujo end-to-end

```text
Usuario ingresa partido -> resolver evento -> obtener odds -> obtener stats
  -> crear features (as-of) -> modelo -> calibración -> no-vig -> edge/EV
  -> stake (Kelly fraccional) -> recomendación -> guardar pick -> medir CLV/resultado
```

## Estructura del repo

```
docs/
  00-decisiones.md            Registro de decisiones de alcance + addenda
  01-propuesta-tecnica.md     Visión, arquitectura, papel del LLM, SaaS, dashboard, flujo e2e
  02-fuentes-de-datos.md      APIs por deporte, pros/contras/costos, plan de créditos (verificado)
  03-modelo-de-datos.md       Entidades, auditoría inmutable, queries clave
  04-features-y-modelos.md    Feature engineering ML/F5, modelos, calibración, anti-leakage
  05-motor-ev-y-bankroll.md   No-vig, edge, EV, Kelly, CLV — con ejemplos verificados
  06-backtesting-y-metricas.md Walk-forward, paper trading, métricas, criterios go/no-go
  07-roadmap.md               Fases F0-F5, riesgos, costos (~$37/mes escenario base)
infra/
  schema.sql                  DDL Postgres 16 (14 tablas + 2 vistas; validado contra PG 16 real)
apps/
  api/                        FastAPI. Core cuantitativo REAL y testeado (83 tests):
                              devig, ev, kelly, clv. Rutas v1 + clientes de ingesta documentados.
  web/                        Next.js 15 dashboard scaffold (picks, detalle/audit, performance,
                              bankroll). Mock data etiquetada; disclaimer permanente.
```

## Quickstart

```bash
# Backend (Python 3.12)
cd apps/api
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest          # 83 tests del core cuantitativo
.venv/bin/uvicorn app.main:app --reload   # http://localhost:8000/docs

# Base de datos
psql "$DATABASE_URL" -f infra/schema.sql

# Frontend
cd apps/web
npm install && npm run dev   # http://localhost:3000 (NEXT_PUBLIC_API_URL apunta al API)
```

## Estado

**Fase actual: propuesta técnica + scaffold (pre-F0).** Nada de esto ha sido backtesteado ni
opera con dinero real. El proyecto tiene criterios explícitos para **matarse a sí mismo** si
no demuestra edge (ver `docs/06-backtesting-y-metricas.md` §go/no-go): si tras ≥300 picks de
paper trading no bate el CLV ni el log loss del market prior, se descarta con costo hundido
acotado.

## Principios

- Winrate no es la métrica. Se mide calibración (Brier, log loss, ECE), EV, yield, ROI y CLV — cada una definida y separada.
- El LLM explica; el modelo estadístico calcula. Ninguna probabilidad sale de un LLM.
- Todo pick es auditable: odds al momento, versión del modelo, snapshot de features, closing line, resultado.
- Backtest honesto: features as-of, sin shuffle, odds realistas, todo lo evaluado queda registrado (no solo lo publicado).

---

*Herramienta informativa y educativa. No constituye asesoría financiera. +18. Juega responsablemente.*
