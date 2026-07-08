# 01 — Propuesta técnica (documento madre)

Codename del producto: **EDGE**. Este documento define la visión, el alcance del MVP,
la arquitectura, el papel del LLM, las consideraciones SaaS/legales, el dashboard y el
flujo end-to-end. Las decisiones de alcance que lo sustentan están en
`00-decisiones.md` (fuente de verdad). El detalle de cada subsistema vive en los docs
`02` a `07`.

---

## 1. Visión y posicionamiento

EDGE es una **plataforma cuantitativa de decision support para apuestas deportivas con
gestión de riesgo integrada**. Su trabajo es responder, para cada partido y mercado,
las preguntas que un apostador disciplinado debería hacerse antes de arriesgar dinero:

1. ¿Cuál es mi probabilidad estimada de que ocurra el evento? (`p_model`, calibrada)
2. ¿Cuál es la probabilidad justa que implica el mercado sin vig? (`p_fair`)
3. ¿Existe edge suficiente? (`edge = p_model − p_fair`)
4. ¿Cuál es el valor esperado por unidad apostada? (`EV`)
5. ¿Cuánto apostar, dado mi bankroll y mi tolerancia de riesgo? (Kelly fraccional con cap)
6. ¿La línea cerró a mi favor o en contra? (CLV)
7. ¿Qué parte de la recomendación viene de datos y qué parte es contexto explicativo?

### Lo que EDGE NO es

- **No es un tipster.** No vende "picks seguros", corazonadas ni autoridad de un experto.
  Vende probabilidades calibradas, detección de valor esperado positivo (EV+),
  trazabilidad completa de cada pick y control de riesgo.
- **No promete rentabilidad.** Apostar contra mercados con vig es un juego de márgenes
  finos donde la mayoría pierde. Un modelo bien calibrado con edge pequeño puede ser
  rentable a largo plazo, pero eso se demuestra con backtest walk-forward, paper
  trading y CLV sostenido — nunca se promete por adelantado (ver
  `06-backtesting-y-metricas.md`).
- **No coloca apuestas ni custodia fondos.** Es un producto informativo: el usuario
  decide si apuesta, dónde y cuánto.
- **El winrate no es la métrica principal.** Un modelo puede acertar 43% y ser rentable
  tomando underdogs con cuota alta, o acertar 58% y perder dinero con cuotas malas. Las
  métricas que importan son calibración (Brier, log loss, ECE), edge promedio, EV, ROI,
  yield y CLV, cada una con definición explícita (ver `05-motor-ev-y-bankroll.md` y
  `06-backtesting-y-metricas.md`).

El diferenciador del producto es la **auditabilidad**: cada pick queda registrado con
las odds al momento de la recomendación, la versión del modelo, el snapshot de features
usado, la línea de cierre y el resultado. Nada se borra ni se reescribe; los picks
perdedores son tan visibles como los ganadores.

---

## 2. MVP recomendado y justificación

### 2.1 Dentro del MVP: MLB Moneyline + First-5-Innings (F5) Moneyline

| Criterio | Por qué MLB ML + F5 ML |
|---|---|
| Temporada | Única liga prioritaria **en temporada en julio 2026**. NBA/NFL/NHL están en off-season; validar un pipeline sin partidos reales diarios no es posible. |
| Datos | MLB tiene los mejores datos gratuitos del deporte profesional: MLB Stats API y pybaseball/Statcast sin costo (ver `02-fuentes-de-datos.md`). |
| Volumen de sample | Slate diario de ~15 juegos → ~450 eventos/mes por mercado. Permite acumular muestra estadística rápido para calibración y paper trading. |
| Simplicidad del mercado | Moneyline es un mercado de 2 lados, líquido y con ambos lados cotizados: el no-vig es directo y el settlement es trivial. |
| F5 como complemento | F5 ML aísla a los pitchers abridores (la señal más fuerte pre-juego en MLB) y reduce el ruido de bullpens; comparte casi todo el pipeline con el ML de juego completo. |

Dos mercados sobre la misma liga significa un solo pipeline de ingesta, un solo feature
store y dos cabezas de modelo — el costo marginal del segundo mercado es bajo y duplica
la superficie de aprendizaje.

### 2.2 Explícitamente FUERA del MVP y por qué

| Excluido | Razón |
|---|---|
| Props de jugadores (K's de pitcher, hits, total bases) | Sample chico por jugador, dependencia de lineups confirmados tarde, y **límites de mercado**: los books limitan rápido a ganadores en props. Requiere datos y cuidado que el MVP no puede pagar. K's de pitchers entra en fase 2 junto con NRFI/YRFI (`00-decisiones.md`, decisión 1); el resto de props, después (ver `07-roadmap.md`). |
| NRFI/YRFI | Mercado interesante y relacionado con F5, pero de nicho, con menos liquidez y cuotas más castigadas. Fase 2. |
| Soccer / Champions League | Mercado muy eficiente (el más líquido del mundo), datos de calidad son caros (event data/xG comercial), y la Champions arranca en septiembre. No aporta nada en julio. |
| NBA / NFL / NHL | Fuera de temporada en julio 2026. Se añaden por fases cuando el pipeline esté validado en MLB. |
| Live betting | Requiere infraestructura de baja latencia, odds en tiempo real (caras) y modelos in-game distintos. Fuera de alcance por ahora, sin fecha. |
| Modelos de umpire | Señal real para totals/NRFI pero marginal para moneyline; la asignación de umpires se confirma tarde y añade complejidad de ingesta que no paga en el MVP. |

La regla general: **si un mercado no permite acumular sample rápido con datos gratuitos
y auditables, no entra al MVP.**

---

## 3. Arquitectura del sistema

Tres capas con responsabilidades separadas y sin fugas entre ellas:

1. **Pipeline cuantitativo (Python):** ingesta, features, modelo, calibración, EV,
   stake, settlement. Determinista y reproducible. Es la única fuente de números.
2. **Producto SaaS (FastAPI + Next.js):** API multi-tenant, auth, dashboard. Expone lo
   que el pipeline produce; no calcula nada probabilístico por su cuenta.
3. **Capa LLM:** research y explicación en lenguaje natural. Consume salidas del
   pipeline; jamás las produce ni las modifica (ver sección 4).

```text
┌────────────────────────── PIPELINE CUANTITATIVO (Python) ──────────────────────────┐
│                                                                                    │
│  JOBS DE INGESTA (cron)                                                            │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌───────────────────────────┐  │
│  │ Schedule + probables│  │ Snapshots de odds    │  │ Lesiones / lineups /      │  │
│  │ (MLB Stats API)     │  │ (The Odds API,       │  │ noticias                  │  │
│  │                     │  │  Pinnacle = ref)     │  │ (MLB Stats API + feeds)   │  │
│  └──────────┬──────────┘  └──────────┬───────────┘  └─────────────┬─────────────┘  │
│             │                        │                            │                │
│             ▼                        │                            ▼                │
│  ┌─────────────────────┐             │              ┌───────────────────────────┐  │
│  │ FEATURE STORE as-of │             │              │ Contexto crudo (texto)    │──┼──┐
│  │ (solo info previa   │             │              └───────────────────────────┘  │  │
│  │  al primer pitch)   │             │                                             │  │
│  └──────────┬──────────┘             │                                             │  │
│             ▼                        ▼                                             │  │
│  ┌─────────────────────┐  ┌──────────────────────┐                                 │  │
│  │ MODELO PROBABILÍSTI-│  │ NO-VIG FAIR LINE     │                                 │  │
│  │ CO (XGBoost/LGBM)   │  │ p_fair (Pinnacle)    │                                 │  │
│  └──────────┬──────────┘  └──────────┬───────────┘                                 │  │
│             ▼                        │                                             │  │
│  ┌─────────────────────┐             │                                             │  │
│  │ CALIBRACIÓN         │             │                                             │  │
│  │ (isotonic/Platt)    │             │                                             │  │
│  └──────────┬──────────┘             │                                             │  │
│             └───────┬────────────────┘                                             │  │
│                     ▼                                                              │  │
│  ┌──────────────────────────────┐   ┌──────────────────────────────┐               │  │
│  │ MOTOR EV                     │──▶│ STAKE SIZING                 │               │  │
│  │ edge, EV, umbrales           │   │ Kelly completo × fracción,   │               │  │
│  │                              │   │ cap por pick                 │               │  │
│  └──────────────────────────────┘   └──────────────┬───────────────┘               │  │
│                                                    ▼                               │  │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │  │
│  │ PICK LOG INMUTABLE (odds tomadas, model_version, snapshot de features)       │  │  │
│  └──────────────────────────────────────┬───────────────────────────────────────┘  │  │
│                                         ▼                                          │  │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │  │
│  │ SETTLEMENT + CLV (closing line Pinnacle no-vig, resultado, ROI/yield/units)  │  │  │
│  └──────────────────────────────────────────────────────────────────────────────┘  │  │
└──────────────────────────────────────┬─────────────────────────────────────────────┘  │
                                       │  lectura                                       │
┌──────────────────────────────────────▼──────────────────────┐   ┌─────────────────────▼──────┐
│ PRODUCTO SaaS                                               │   │ CAPA LLM                   │
│ ┌─────────────────────────┐  ┌───────────────────────────┐  │   │ features estructuradas +   │
│ │ API (FastAPI)           │◀─│ Dashboard (Next.js)       │  │◀──│ contexto → explicación NL  │
│ │ auth, multi-tenant,     │  │ picks, detalle, perf,     │  │   │ (anotación separada,       │
│ │ /api/v1/*               │  │ bankroll config           │  │   │  nunca modifica números)   │
│ └─────────────────────────┘  └───────────────────────────┘  │   └────────────────────────────┘
└─────────────────────────────────────────────────────────────┘
```

Notas de arquitectura:

- **Feature store as-of:** toda feature se materializa con timestamp y solo se usa
  información disponible antes del primer pitch. Es la defensa principal contra data
  leakage, tanto en producción como en backtest (detalle en `04-features-y-modelos.md`).
- **Snapshots de odds:** las odds se guardan como serie temporal (open → snapshots
  intermedios → close). El pick se evalúa contra la odd disponible al momento del scan,
  nunca contra el closing como si fuera apostable. Pinnacle es la referencia para
  no-vig y CLV; Bet365 y books MX son las odds "apostables" del usuario
  (ver `02-fuentes-de-datos.md` y `05-motor-ev-y-bankroll.md`).
- **Pick log inmutable:** append-only. Correcciones = nuevo registro que referencia al
  anterior, nunca un UPDATE destructivo (schema en `03-modelo-de-datos.md` e
  `infra/schema.sql`).
- **Automatización:** un cron diario escanea el slate completo y publica value bets;
  el endpoint `POST /api/v1/analyze` cubre el análisis bajo demanda.

---

## 4. Papel del LLM: capa de research y explicación

El LLM es **explicador, no predictor**. Su lugar en el sistema es estrecho por diseño.

**Entra al LLM:**

- Las features estructuradas del pick ya calculadas por el pipeline (`p_model`,
  `p_fair`, edge, EV, features principales del modelo con sus valores).
- Contexto recopilado por los jobs de ingesta: lesiones, lineups, noticias, clima,
  bullpen usage reciente.

**Sale del LLM:**

- Una explicación en lenguaje natural del pick, anclada a datos verificables
  ("el modelo asigna X% principalmente por el diferencial de xFIP entre abridores y el
  descanso del bullpen"), más un resumen del contexto cualitativo relevante.

**Guardrails explícitos (no negociables):**

1. El LLM **nunca genera ni modifica probabilidades, edges, EVs ni stakes**. Recibe los
   números como input de solo lectura y los cita tal cual.
2. El LLM **nunca decide si algo es pick o no**. Esa decisión la toman los umbrales del
   motor EV, de forma determinista.
3. Las salidas del LLM se almacenan como **anotaciones separadas** del registro
   cuantitativo (tabla propia, con versión de prompt y timestamp), nunca dentro del
   pick log. Si la anotación falla o alucina, el pick sigue intacto y auditable.
4. Toda cifra mencionada en la explicación debe existir en el input estructurado; el
   render del dashboard marca la explicación como "generada por IA".

**Por qué estos límites:** un LLM no es determinista (misma entrada puede dar distinta
salida), no está calibrado (sus "probabilidades" verbales no corresponden a frecuencias
reales) y puede alucinar datos con total fluidez. Convertir texto en probabilidad
confiable no es una capacidad de LLMs hoy; pretenderlo destruiría la propiedad central
del producto, que es la trazabilidad de cada número hasta un modelo versionado y
auditado.

---

## 5. Consideraciones SaaS worldwide

Decisión de alcance: SaaS desde el inicio, worldwide, informativo (`00-decisiones.md`,
decisiones 2 y 5).

- **Multi-tenant básico:** cada usuario tiene su configuración de bankroll (monto,
  fracción de Kelly, cap) y su vista de picks. Los picks y las métricas del modelo son
  globales (un solo modelo, un solo pick log); lo que es por-tenant es la capa de
  stake sugerido y preferencias. Aislamiento por `user_id` a nivel de fila; no se
  requiere aislamiento por schema en el MVP.
- **Auth:** email + password o proveedor OAuth gestionado; sesiones vía JWT contra la
  API de FastAPI. Nada exótico en el MVP.
- **Producto informativo:** EDGE no coloca apuestas, no se conecta a cuentas de
  sportsbooks, no procesa ni custodia dinero de apuestas. Esto reduce la superficie
  regulatoria pero **no la elimina**.
- **Disclaimers obligatorios en el producto:** solo mayores de 18 años (o la edad legal
  de la jurisdicción del usuario); mensajes de juego responsable con enlaces a ayuda;
  el contenido es informativo/educativo y no constituye asesoría financiera ni de
  apuestas; resultados pasados no garantizan resultados futuros.
- **Riesgo regulatorio, dicho honestamente:** vender picks/pronósticos deportivos por
  suscripción está **regulado o restringido en algunas jurisdicciones** (hay países que
  exigen licencias para servicios de pronósticos, otros que restringen la publicidad de
  apuestas, y algunos donde el gambling y sus servicios auxiliares son ilegales).
  "Worldwide" en la práctica significa: lanzar con disclaimers y geobloqueo básico si
  hace falta, y **revisar con asesoría legal las jurisdicciones objetivo antes de
  cobrar suscripciones**. Este punto es un pre-requisito del hito de monetización en
  `07-roadmap.md`, no una nota al pie.
- **Pagos (cuando lleguen):** procesadores estándar (Stripe) clasifican los servicios
  relacionados con gambling como high-risk y pueden requerir aprobación previa;
  verificar el ToS del procesador antes de integrar cobros.

---

## 6. Dashboard sugerido

Cuatro pantallas para el MVP. El scaffold vive en `apps/web/` (Next.js App Router,
TypeScript).

1. **Picks de hoy** — tabla del scan diario: evento, mercado (ML / F5 ML), lado,
   odds tomadas y book, `p_model`, `p_fair`, edge, EV por unidad, stake sugerido
   (según la config del usuario) y estado (abierto/settled). Orden default por EV
   descendente. Es la pantalla de trabajo diaria.
2. **Detalle de pick** — el audit trail completo de un pick: snapshot de features con
   valores, versión del modelo, odds al momento de la recomendación, historial de la
   línea hasta el cierre, CLV una vez cerrada, resultado y P&L en unidades, más la
   explicación del LLM claramente marcada como anotación generada por IA.
3. **Performance** — calibration curve (predicho vs observado por bucket), ROI, yield,
   CLV beat-rate, drawdown máximo y units, con breakdown por mercado (ML vs F5),
   por book y por rango de odds. Aquí se muestra también el ECE rolling de 60 días que
   habilita o pausa la publicación de picks.
4. **Configuración de bankroll** — monto del bankroll, fracción de Kelly (default 1/8),
   cap por pick (default 1–2%). El motor siempre calcula Kelly completo; esta pantalla
   solo controla cómo se escala a stake sugerido.

---

## 7. API surface v1

Superficie canónica del MVP (FastAPI, scaffold en `apps/api/`):

```text
POST /api/v1/analyze        body {sport:"mlb", market:"ml"|"f5_ml", home_team, away_team, date?} -> {event, market, p_model, p_fair, edge, ev_per_unit, kelly_full, stake_suggested, recommendation, explanation, model_version}
GET  /api/v1/picks/today    -> lista de value bets del scan diario
GET  /api/v1/picks/{pick_id} -> detalle de pick con audit trail completo
GET  /api/v1/performance    -> métricas agregadas: roi, yield, clv_beat_rate, ece, brier, drawdown, units, n_picks
GET  /health
```

Notas:

- `POST /api/v1/analyze` es el análisis bajo demanda: resuelve el evento, corre el
  pipeline completo y devuelve la recomendación con todos los componentes numéricos
  separados (nunca un "sí/no" opaco). `recommendation` es `bet`/`pass` según los
  umbrales configurados; `explanation` es la anotación del LLM.
- `GET /api/v1/picks/today` devuelve el output del cron diario ya persistido; no
  recalcula.
- `kelly_full` siempre viaja en la respuesta; `stake_suggested` aplica la fracción y el
  cap del usuario autenticado.
- Versionado por prefijo `/api/v1`; cambios incompatibles → `/api/v2`.

---

## 8. Flujo end-to-end con un ejemplo MLB

Ejemplo narrado con **números ilustrativos** (no son predicciones ni benchmarks; solo
muestran la mecánica). Definiciones canónicas completas en `05-motor-ev-y-bankroll.md`.

**Escenario:** el usuario pide analizar Dodgers (home) vs Padres (away), mercado F5
moneyline, vía `POST /api/v1/analyze`.

1. **Resolver evento.** El event resolver mapea equipos y fecha al `game_pk` de la MLB
   Stats API y verifica que haya pitchers probables confirmados. Sin probables
   confirmados, el análisis F5 se marca como provisional.
2. **Obtener odds.** Se lee el snapshot más reciente de The Odds API. Ilustrativo:
   Pinnacle cotiza el F5 ML Dodgers a 1.87 y Padres a 2.02; Bet365 tiene Dodgers 1.90.
3. **Stats.** Del feature store salen los agregados as-of: forma del abridor
   (xFIP, K%, BB% últimos 30 días), splits del lineup contra la mano del pitcher,
   park factor, descanso. Solo información anterior al primer pitch.
4. **Features.** Se materializa el vector de features del juego con timestamp y hash;
   ese snapshot exacto quedará ligado al pick.
5. **Modelo.** El modelo F5 (XGBoost/LightGBM, ver `04-features-y-modelos.md`) emite un
   score crudo para "Dodgers ganan el F5". Ilustrativo: 0.578.
6. **Calibración.** El calibrador (isotonic, entrenado out-of-fold) mapea el score a
   probabilidad calibrada. Ilustrativo: `p_model = 0.565`.
7. **No-vig.** Con Pinnacle: `p_imp_dodgers = 1/1.87 = 0.5348`,
   `p_imp_padres = 1/2.02 = 0.4950`, suma = 1.0298 (≈3% de vig).
   Método multiplicativo (default del MVP; métodos que corrigen mejor el
   favorite-longshot bias, como Shin o power, quedan como mejora futura, ver
   `05-motor-ev-y-bankroll.md`): `p_fair = 0.5348 / 1.0298 = 0.5193`.
8. **Edge y EV.** `edge = 0.565 − 0.5193 = +0.0457` (+4.6 puntos). Contra la odd
   apostable de Bet365 (1.90): `EV = 0.565 × 0.90 − 0.435 = +0.0735` por unidad.
   Ambos superan los umbrales default (edge ≥ 2%, EV ≥ +2%) y el ECE rolling de 60
   días está dentro del límite (≤ 0.03) → es pick.
9. **Stake.** Kelly completo con `b = 0.90`:
   `f* = (0.565 × 1.90 − 1) / 0.90 = 0.0817` (8.2% del bankroll). Con fracción default
   1/8: 1.02%; el cap del usuario (default configurable 1–2%; en este ejemplo, 1%) lo
   recorta a **1.0% del bankroll**.
10. **Guardar pick.** Se inserta el registro inmutable: evento, mercado, lado, odd
    tomada (1.90 Bet365), `p_model`, `p_fair`, edge, EV, `kelly_full`, stake sugerido,
    `model_version`, hash del snapshot de features, timestamp. El LLM genera su
    explicación y se guarda como anotación aparte.
11. **Closing line.** Al primer pitch, el job de odds captura el cierre de Pinnacle.
    Ilustrativo: Dodgers F5 cerró 1.80 / Padres 2.10 → `p_fair_close = 0.5385`.
12. **CLV.** El precio tomado (1.90 en Bet365) implica `p_imp = 1/1.90 = 0.5263`; el
    cierre no-vig de Pinnacle implica 0.5385. CLV = 0.5385 − 0.5263 = **+1.2 puntos de
    probabilidad** a favor: se consiguió un precio mejor que el cierre justo, señal de
    que se apostó "por delante" del mercado; cuenta como beat en el beat-rate. La
    referencia no-vig al momento del pick (0.5193 → 0.5385) confirma además que la
    línea se movió hacia el pick. Definición formal en `05-motor-ev-y-bankroll.md`.
13. **Resultado.** Termina el 5.º inning; el job de settlement lee el linescore, marca
    win/loss/push, calcula P&L en unidades y actualiza ROI, yield, drawdown y la
    calibración observada. Todo queda visible en `GET /api/v1/performance` y en la
    pantalla de Performance del dashboard.

El mismo flujo corre en batch cada mañana sobre el slate completo (~15 juegos × 2
mercados); los que pasan umbrales aparecen en `GET /api/v1/picks/today`.

---

## 9. Referencias a los demás documentos

| Doc | Contenido |
|---|---|
| `00-decisiones.md` | Decisiones de alcance del owner (fuente de verdad). |
| `02-fuentes-de-datos.md` | APIs por deporte, costos, plan de créditos de The Odds API. |
| `03-modelo-de-datos.md` | Schema Postgres, entidades, auditoría de picks. |
| `04-features-y-modelos.md` | Feature engineering ML/F5, modelos, calibración, anti-leakage. |
| `05-motor-ev-y-bankroll.md` | No-vig, edge, EV, Kelly fraccional, CLV, ejemplos numéricos. |
| `06-backtesting-y-metricas.md` | Walk-forward, paper trading, métricas, criterios go/no-go. |
| `07-roadmap.md` | Fases, riesgos técnicos, costos aproximados. |
