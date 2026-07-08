# EDGE API (backend FastAPI)

Backend del MVP de EDGE: motor cuantitativo (no-vig, edge, EV, Kelly fraccional, CLV),
endpoints de análisis/picks/performance y clientes de ingesta (The Odds API, MLB Stats API).

El sistema es **informativo**: no coloca apuestas ni procesa dinero. Las probabilidades
salen del modelo estadístico (pendiente de implementar; hoy hay stubs deterministas con
`TODO` marcados). El LLM nunca produce probabilidades. Ver `docs/00-decisiones.md` y
`docs/05-motor-ev-y-bankroll.md` en la raíz del repo.

## Requisitos

- Python 3.12+
- (Opcional para correr con datos reales) una API key de [The Odds API](https://the-odds-api.com/)

## Instalación

Desde `apps/api/`:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # y llenar ODDS_API_KEY, DATABASE_URL, LLM_API_KEY
```

## Correr el servidor de desarrollo

Desde `apps/api/` con el venv activado:

```bash
uvicorn app.main:app --reload --port 8000
```

- Healthcheck: `GET http://localhost:8000/health`
- Docs interactivas (OpenAPI): `http://localhost:8000/docs`

Endpoints del MVP:

| Método | Ruta | Estado |
|--------|------|--------|
| `POST` | `/api/v1/analyze` | Usa el core cuantitativo real sobre una probabilidad de modelo **stub** (determinista, marcada con TODO) |
| `GET`  | `/api/v1/picks/today` | Stub tipado (repositorio en memoria vacío) |
| `GET`  | `/api/v1/picks/{pick_id}` | Stub tipado (404 hasta que exista persistencia) |
| `GET`  | `/api/v1/performance` | Stub tipado (métricas en cero/null con TODO) |

## Base de datos y jobs de ingesta (F0)

La fuente de verdad del schema es `infra/schema.sql` (raíz del repo); el código Python
refleja las tablas de la BD viva en vez de duplicarlas como modelos ORM.

```bash
# aplicar el schema
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f ../../infra/schema.sql

# sync del slate del día (MLB Stats API, gratis) — idempotente
python -m app.jobs.sync_schedule --date 2026-07-08

# snapshot de odds actuales (The Odds API, requiere ODDS_API_KEY)
python -m app.jobs.snapshot_odds

# cerca del primer pitch: flaggear la closing line
python -m app.jobs.snapshot_odds --closing-window-min 20

# backfill histórico de resultados + scores F5 (MLB Stats API, gratis)
python -m app.jobs.backfill_results --start-date 2024-03-20 --end-date 2024-10-01
```

Hay un workflow de GitHub Actions (`.github/workflows/ingesta.yml`) que calendariza
estos jobs; solo requiere los secrets `ODDS_API_KEY` y `DATABASE_URL` en el repo.

El feature builder as-of (`app/features/builder.py`) construye los bloques de forma
de equipo de `docs/04` desde este archivo de eventos/resultados, con guardia dura
anti-leakage (rechaza `as_of_ts` posterior al inicio del juego) y snapshots
deduplicados por hash canónico en `feature_snapshots`.

Cadencia recomendada (plan de créditos en `docs/02-fuentes-de-datos.md`): schedule una
vez por la mañana; odds cada 2-4 h + una corrida cercana al inicio de cada juego con
`--closing-window-min`. Acumular snapshots propios desde el día 1 es el criterio de
salida de F0 (`docs/07-roadmap.md`).

## Correr los tests

Desde `apps/api/` con el venv activado:

```bash
pytest
```

Los tests cubren el core cuantitativo (`app/core/`) con casos calculados a mano, los
parsers de ingesta con fixtures grabados, y — si `EDGE_TEST_DATABASE_URL` apunta a un
Postgres con `infra/schema.sql` aplicado — la integración completa de los jobs
(idempotencia, matching de doubleheaders, dedupe append-only, closing flag). Sin esa
variable, los tests de integración se saltan. Los tests **no** llaman APIs externas.

```bash
EDGE_TEST_DATABASE_URL="postgresql+psycopg://user@host/dbname" pytest
```

## Estructura

```
app/
  main.py            # FastAPI app + /health
  config.py          # Settings (pydantic-settings, lee .env)
  core/              # matemáticas del motor: devig, ev, kelly, clv (implementación real)
  api/               # routers: analyze, picks, performance
  db/                # engine + reflection (infra/schema.sql es la fuente de verdad)
  ingestion/         # clientes (The Odds API v4, MLB Stats API) + parsers puros + store
  jobs/              # crons F0: sync_schedule, snapshot_odds (python -m app.jobs.*)
  picks/             # PickRecord (auditoría) + PickRepository (in-memory para tests)
tests/               # pytest: core + parsers (fixtures) + integración de jobs (Postgres)
```
