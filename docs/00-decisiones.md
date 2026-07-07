# Registro de decisiones de alcance (2026-07-07)

Decisiones tomadas por el owner del proyecto vía cuestionario inicial. Cada cambio
posterior a estas decisiones debe registrarse aquí con fecha y razón.

| # | Decisión | Valor elegido | Notas |
|---|----------|---------------|-------|
| 1 | Deporte/mercado MVP | **MLB: Moneyline + First-5-Innings Moneyline** | Única liga prioritaria en temporada en julio 2026; permite validar el pipeline con partidos reales diarios. NRFI/YRFI y strikeouts de pitchers quedan para fase 2. |
| 2 | Modelo de uso | **SaaS desde el inicio** | Implica auth, multi-tenant, disclaimers legales y separación API/front desde el día 1. |
| 3 | Presupuesto mensual APIs | **≤ $50 USD/mes** | The Odds API tier 20K créditos (~$30/mes) + free tiers de datos deportivos. |
| 4 | Interfaz | **Dashboard web** | Next.js. CLI interno para desarrollo puede existir, pero no es producto. |
| 5 | Región de operación | **Worldwide** | Diseño agnóstico al sportsbook. El sistema es informativo: no coloca apuestas, no procesa dinero de apuestas. Revisar regulación por jurisdicción antes de cobrar suscripciones. |
| 6 | Sportsbooks relevantes | **Pinnacle (línea de referencia/CLV), Bet365 e internacionales, books locales MX (Caliente, Codere), otros** | Pinnacle se usa como estándar de línea justa aunque el usuario no apueste ahí. Books MX no tienen API pública: el usuario compara manualmente. |
| 7 | Validación | **Backtest walk-forward + paper trading** | Doble validación antes de dinero real y antes de mostrar métricas a usuarios. |
| 8 | Gestión de riesgo | **Kelly fraccional configurable por usuario, default Kelly/8 con cap 1–2% del bankroll por pick** | El motor calcula Kelly completo; el usuario elige fracción con default conservador. |
| 9 | Stack | **Python (FastAPI, XGBoost/sklearn, Postgres) + Next.js para el dashboard** | El ecosistema de datos deportivos vive en Python; el front SaaS en Next.js. |
| 10 | Automatización | **Scan diario automático del slate + análisis bajo demanda** | Cron que evalúa todos los juegos del día y publica value bets; endpoint para analizar un partido/mercado puntual. |

## Principios no negociables (del brief original)

- No se promete rentabilidad. El producto vende claridad, control de riesgo y trazabilidad.
- Winrate NO es la métrica principal. Se mide calibración (Brier, log loss, ECE), EV, ROI, yield y CLV, cada una con su definición explícita.
- Toda probabilidad mostrada viene del modelo estadístico calibrado, nunca de un LLM.
- El LLM es capa de research/explicación: resume lesiones, lineups y contexto, y redacta la explicación del pick a partir de features estructuradas. No inventa números.
- Cada pick queda auditado: odds al momento, versión del modelo, snapshot de features, línea de cierre, resultado.
- El backtest usa solo información disponible antes del primer pitch (as-of features) y odds realistas, nunca el closing line como si fuera apostable.
