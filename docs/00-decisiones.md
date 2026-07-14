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

## Addenda (2026-07-08, post-autoría y verificación)

- **Default operativo del cap de stake: 2%** (borde superior del rango elegido en la decisión #8),
  configurable por usuario con recomendación 1–2%. Así lo implementan `apps/api` (`stake_cap_pct=0.02`),
  `apps/web` (form de bankroll) y el ejemplo maestro de `05-motor-ev-y-bankroll.md`. Los ejemplos que
  usan 1% están marcados como ilustrativos.
- **Riesgo legal identificado — MLB Stats API**: el copyright de MLBAM restringe su uso a
  individual/no comercial/no masivo. Como el producto es SaaS (decisión #2), esta fuente "gratis"
  debe resolverse (licencia o proveedor alternativo) como parte de la revisión legal de la fase F3,
  **antes de cobrar suscripciones**. Detalle en `02-fuentes-de-datos.md`.
- **Paper trading vs calendario MLB**: la ventana de F2 puede chocar con el fin de la temporada
  regular (~oct 2026). El gate de salida de F2 es el **sample** (≥300 picks), no la fecha; si la
  temporada termina antes, se extiende a 2027 o se complementa con el siguiente deporte de F5.
  Detalle en `07-roadmap.md`.
- **Referencia F5 "a confirmar"**: no está confirmado que Pinnacle cotice `h2h_1st_5_innings` vía
  The Odds API (los additional markets son mayormente de books US). Si no lo cotiza, la línea de
  referencia no-vig para F5 será el consenso de books US. Paso de verificación concreto en
  `02-fuentes-de-datos.md`.

## Addenda (2026-07-10)

- **Producto bilingüe ES/EN desde el diseño**: el dashboard y toda la capa de producto (F3)
  se construyen con i18n desde el día uno — español como mercado primario (gran parte del
  público apostador objetivo no lee inglés) e inglés para alcance. El motor cuantitativo ya
  es neutro al idioma (features, schema y código en inglés, decisión de `04` §1.1); lo que se
  internacionaliza es la UI (`apps/web`, hoy `lang="es"`), las explicaciones de picks que
  genera el LLM (parámetro de idioma por usuario) y los disclaimers/legales — estos últimos
  varían por jurisdicción, no solo por idioma, y se resuelven en la revisión legal de F3
  junto con la decisión #5. Los docs internos y la operación siguen en español.

## Addenda (2026-07-13, tanda F1.2 — bloque de ofensiva)

Concreciones de implementación de `docs/04 §1.2` decididas en esta tanda (el doc es la
especificación; esto registra cómo se aterrizó y por qué):

- **Bateo por jugador-juego** (`batting_game_logs`, migración 004), no agregado por equipo:
  el fetch del boxscore ya se paga y las filas por bateador desbloquean el bloque de lineup
  (§1.5) sin re-ingerir ~19K boxscores — la misma filosofía de la 003 con los relevistas.
  Se excluyen líneas con PA derivada cero (corredores emergentes/defensas: nada que aportar
  a features de tasas). `batting_order` y `plate_appearances` se archivan como auditoría.
- **Constantes wOBA fijas de FanGraphs 2017** (wBB .693, wHBP .723, w1B .877, w2B 1.232,
  w3B 1.552, wHR 1.980): son ANTERIORES a todo el dataset (2018+), así que cumplen el
  checklist §4.9 (nunca constantes de fin de temporada en curso) por construcción, para
  toda fila y sin maquinaria as-of. A una feature solo le importan orden y estabilidad,
  no la escala absoluta — el mismo argumento con el que el bloque abridor omite la
  constante aditiva del xFIP.
- **Denominadores derivados de componentes**: PA = AB+BB+HBP+SF+SH para K%/BB%; denominador
  wOBA = AB+BB−IBB+SF+HBP. Nunca el campo `plateAppearances` del feed (uniformidad entre
  eras; interferencia del catcher excluida uniformemente).
- **Ventanas por día UTC terminando AYER** (30d = días [D−30, D−1]; season = año UTC de D
  hasta D−1): la regla intradía-segura de §1.1, igual que el bloque bullpen de F1.1.
- **Split vs mano = proxy por abridor rival**: sin play-by-play no hay splits por PA; el
  clasificador del pasado es la mano del abridor REAL que el equipo enfrentó (join a
  `pitching_game_logs.is_starter`). Se emite UNA feature seleccionada por la mano del
  rival del juego a predecir (`team_woba_vs_opp_hand_30d`, como la nombra §2.2): probable
  as-of en online, abridor real en bulk (la convención documentada del bloque abridor).
- **Shrinkage del split hacia la ventana móvil de 365 días del propio equipo** con
  pseudo-muestra de 200 PA: implementa "hacia split de temporada, y en abril hacia
  temporada previa ponderada" con UN mecanismo continuo (en abril la ventana de 365d ES
  mayormente la temporada previa), espejo de las ventanas del bloque abridor. Las demás
  tasas van crudas (§1.2 solo obliga shrinkage en splits); ventana vacía → None, jamás
  ceros fabricados.
- **El bloque entra a ML y a F5** (54/46 columnas): la única exclusión por diseño de F5
  sigue siendo el bullpen (§1.4). El refinamiento F5 hacia lineup (§1.9) llegará con §1.5.
- **Fórmulas en un módulo compartido** (`app/features/offense.py`) importado por builder y
  dataset: elimina la clase de skew de fórmula duplicada; la paridad de ventanas sigue
  guardada por el test online/bulk.

## Principios no negociables (del brief original)

- No se promete rentabilidad. El producto vende claridad, control de riesgo y trazabilidad.
- Winrate NO es la métrica principal. Se mide calibración (Brier, log loss, ECE), EV, ROI, yield y CLV, cada una con su definición explícita.
- Toda probabilidad mostrada viene del modelo estadístico calibrado, nunca de un LLM.
- El LLM es capa de research/explicación: resume lesiones, lineups y contexto, y redacta la explicación del pick a partir de features estructuradas. No inventa números.
- Cada pick queda auditado: odds al momento, versión del modelo, snapshot de features, línea de cierre, resultado.
- El backtest usa solo información disponible antes del primer pitch (as-of features) y odds realistas, nunca el closing line como si fuera apostable.
