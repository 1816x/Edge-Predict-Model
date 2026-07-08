# EDGE — Dashboard web (`apps/web`)

Dashboard del MVP: Next.js 15 (App Router) + TypeScript + React 19, CSS plano
sin frameworks. Consume el API FastAPI de `apps/api` (surface v1). Es una
herramienta informativa y educativa: muestra análisis, no coloca apuestas ni
procesa dinero.

## Requisitos

- Node.js 18.18+ (recomendado 20+).
- El API de `apps/api` corriendo en local (opcional: sin API, las páginas
  muestran datos de ejemplo etiquetados como **MOCK**).

## Puesta en marcha

```bash
cp .env.example .env.local
npm install
npm run dev
```

Abre `http://localhost:3000`.

## Variables de entorno

| Variable | Descripción | Default |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | URL base del API (FastAPI, `apps/api`) | `http://localhost:8000` |

## Rutas

| Ruta | Contenido |
|---|---|
| `/` | Picks de hoy: tabla de value bets del scan diario |
| `/picks/[id]` | Detalle de un pick con audit trail (odds al momento, versión de modelo, features, explicación, CLV) |
| `/performance` | Métricas agregadas sobre picks registrados (nunca backtests) |
| `/settings/bankroll` | Configuración de bankroll, fracción de Kelly (default 1/8) y cap por pick (default 2%) |

## Estructura

```
app/                      páginas (App Router)
components/               PickCard, EdgeBadge
lib/api.ts                cliente tipado del API v1 (usa NEXT_PUBLIC_API_URL)
lib/types.ts              tipos espejo de los response models del API
lib/format.ts             formato de números (probabilidades %, odds 2 decimales)
```

Convenciones de formato: probabilidades como % con 1 decimal, edge/EV en
puntos porcentuales con signo, odds decimales con 2 decimales.

## Comportamiento sin API

Si `fetch` al API falla (no levantado, timeout), cada página cae a datos de
ejemplo con un banner **MOCK** visible. Ningún dato MOCK representa partidos ni
resultados reales.

## Pendiente (fuera de este scaffold)

- Auth y multi-tenant (SaaS): ver `docs/01-propuesta-tecnica.md`.
- Persistencia de la configuración de bankroll (hoy solo estado local).
- Gráficas reales de calibración y drawdown en `/performance`.
- Formulario de análisis bajo demanda (`POST /api/v1/analyze`, ya tipado en
  `lib/api.ts`).
