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

## Correr los tests

Desde `apps/api/` con el venv activado:

```bash
pytest
```

Los tests cubren el core cuantitativo (`app/core/`): conversión de odds, no-vig
multiplicativo, edge/EV, Kelly fraccional con cap y CLV, con casos calculados a mano.
Los tests **no** llaman APIs externas.

## Estructura

```
app/
  main.py            # FastAPI app + /health
  config.py          # Settings (pydantic-settings, lee .env)
  core/              # matemáticas del motor: devig, ev, kelly, clv (implementación real)
  api/               # routers: analyze, picks, performance
  ingestion/         # clientes The Odds API v4 y MLB Stats API (no se llaman en tests)
  picks/             # PickRecord (auditoría) + PickRepository (in-memory para tests)
tests/               # pytest del core cuantitativo
```
