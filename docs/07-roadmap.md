# 07 — Roadmap, riesgos y costos

Este documento cierra la propuesta: fases con entregables y criterios de salida
medibles, riesgos técnicos con su mitigación, el riesgo legal del SaaS y el
presupuesto mensual aproximado. Las decisiones de alcance viven en
`00-decisiones.md`; los criterios go/no-go detallados en
`06-backtesting-y-metricas.md`. Este doc no promete rentabilidad: define cómo
saber, con el menor costo posible, si el sistema tiene edge o no.

**Regla general del roadmap**: ninguna fase arranca sin que la anterior haya
cumplido su criterio de salida. Los criterios son medibles a propósito — si no se
pueden verificar con una query o una métrica, no son criterios.

---

## 1. Roadmap por fases

Fechas orientativas asumiendo arranque a mediados de julio 2026 (temporada MLB en
curso, que es exactamente lo que necesita el pipeline: partidos reales todos los días).

| Fase | Nombre | Duración estimada | Depende de |
|---|---|---|---|
| F0 | Fundaciones (datos + snapshots) | 2–3 semanas (jul 2026) | — |
| F1 | Modelo (ML y F5) | 3–4 semanas (ago 2026) | F0 completa |
| F2 | Paper trading + dashboard interno | 4–8 semanas (sep–oct 2026) | F1 completa |
| F3 | SaaS beta | condicional | F2 **aprobada** (go/no-go de doc-06) |
| F4 | Expansión de mercados MLB | condicional | F2 aprobada |
| F5 | Multi-deporte | condicional, según calendario | F2 aprobada + F4 opcional |

### F0 — Fundaciones (2–3 semanas)

Objetivo: que los datos fluyan y se acumulen **antes** de escribir una sola línea de
modelo. La razón está en `02-fuentes-de-datos.md` §5: el historial de odds propias es
el activo más caro de reconstruir después (comprar historical odds a posteriori cuesta
más que todo el plan mensual), así que la acumulación empieza el día 1.

Entregables:

- Repo con estructura `apps/api`, `apps/web`, `infra/` y CI mínima (lint + tests).
- Postgres con el schema de `infra/schema.sql` aplicado (entidades y triggers de
  auditoría de `03-modelo-de-datos.md`).
- Ingesta MLB Stats API corriendo en cron: schedule, pitchers probables, lineups
  (con su flag `is_confirmed`), resultados.
- Snapshots de The Odds API corriendo en cron según el plan de créditos de
  `02-fuentes-de-datos.md` §2 (snapshots periódicos + snapshot de cierre por ola de
  primer pitch), escribiendo en `odds_snapshots` (append-only).
- Cliente de odds detrás de una interfaz `OddsClient` (ver riesgo de proveedor único
  en §2 de este doc).

**Criterio de salida (medible)**: **14 días consecutivos de snapshots sin huecos**.
Operativamente: para el 100% de los juegos del slate de esos 14 días existen (a) el
snapshot de cierre de ML y F5, y (b) ninguna ventana mayor a ~4 horas sin snapshot
periódico entre la apertura del mercado y el primer pitch. Se verifica con una query
sobre `odds_snapshots` (gap detection por `event_id, captured_at`), no a ojo. Si hay
huecos, se arregla el cron y el reloj de 14 días se reinicia.

### F1 — Modelo (3–4 semanas)

Objetivo: modelos calibrados para MLB Moneyline y First-5-Innings Moneyline, y la
primera respuesta honesta a la pregunta "¿sabemos algo que el mercado no?".

Entregables:

- Pipeline de features as-of (`04-features-y-modelos.md` §1): todo feature calculado
  solo con información disponible antes del momento de decisión.
- Baselines: market prior (probabilidad no-vig de Pinnacle) y regresión logística;
  luego XGBoost para ML y F5.
- Calibración con gate de `ECE ≤ 0.03` en ventana rolling (`04-features-y-modelos.md` §3).
- Backtest walk-forward contra el market prior (`06-backtesting-y-metricas.md`),
  usando odds de snapshots propios o realistas — nunca el closing como apostable.

**Criterio de salida (medible)**: **log loss del modelo < log loss del market prior
en validación temporal** (walk-forward, promedio sobre las ventanas de validación) **y
ECE ≤ 0.03** en esas mismas ventanas, para cada mercado que pase a F2 (ML y F5 se
evalúan por separado; puede pasar uno y el otro no). Como dice
`04-features-y-modelos.md`: no batir al market prior en log loss significa que el
modelo no aporta información sobre el mercado — se itera o se detiene, no se publica.

### F2 — Paper trading + dashboard interno (4–8 semanas)

Objetivo: validar el sistema completo en condiciones reales, sin dinero, con el
pipeline de auditoría funcionando de punta a punta.

Entregables:

- Scan diario automático del slate (cron): análisis de todos los juegos, aplicación
  de umbrales de publicación (`05-motor-ev-y-bankroll.md`).
- Pick log completo y auditable: odds al momento (FK a `odds_snapshots`), versión de
  modelo, snapshot de features, closing line, resultado (`03-modelo-de-datos.md`).
- CLV tracking contra el closing no-vig de Pinnacle, automático por pick.
- Dashboard Next.js **interno** (sin auth multi-tenant todavía): picks del día,
  historial, curvas de calibración, CLV, yield/ROI simulados.

**Criterio de salida (medible)**: **≥ 300 picks registrados en paper trading y los
criterios go/no-go de `06-backtesting-y-metricas.md` evaluados y documentados** —
evaluados, no necesariamente aprobados. El resultado de F2 es una decisión escrita:
go (pasar a F3/F4), iterar (volver a F1 con hipótesis concretas) o matar el proyecto
(§5 de este doc).

Nota de calendario, para ser honestos: la temporada regular MLB 2026 termina a
principios de octubre. Si el sample de 300 picks no se completa antes (los playoffs
aportan pocos juegos por día), la fase se extiende hasta completar el sample — en el
peor caso, hasta abril 2027 con el inicio de la siguiente temporada, o se complementa
con los deportes de F5 si ya hay pipeline para ellos. **El gate es el sample, no la
fecha.** Recortar el sample para cumplir calendario invalida la validación.

### F3 — SaaS beta (solo si F2 pasa el go/no-go)

Objetivo: convertir el sistema interno en producto para terceros. Todo lo de esta
fase es trabajo de producto, no de modelo — por eso está gateada por F2.

Entregables:

- Auth y multi-tenancy (usuarios, sesiones, configuración por usuario de fracción de
  Kelly y cap, ver `01-propuesta-tecnica.md`).
- Suscripciones (Stripe o merchant of record, ver §4).
- Disclaimers en producto: +18, juego responsable, "esto no es asesoría financiera ni
  garantiza ganancias", metodología y métricas publicadas con su definición.
- Onboarding: explicación de qué es edge/EV/CLV y por qué el winrate no es la métrica.

**Criterio de salida (medible)**: **beta cerrada operando con usuarios reales**
(invitados, gratis o con precio simbólico) **y revisión legal de las jurisdicciones
objetivo completada ANTES de cobrar la primera suscripción** (§3 de este doc). Sin
revisión legal no se cobra, aunque el producto funcione.

### F4 — Expansión de mercados MLB

Objetivo: añadir mercados donde puede haber más ineficiencia, con los mismos gates
metodológicos que ML/F5.

- **NRFI/YRFI** (No Run First Inning): pariente natural del F5 — reusa el bloque de
  features de primera vuelta del lineup (`04-features-y-modelos.md` §1.5). Mercado de
  nicho: menos liquidez, vig más alto, límites más bajos.
- **Strikeouts de pitchers (props)**: advertencia explícita de `01-propuesta-tecnica.md`:
  sample chico por jugador, dependencia fuerte de lineups confirmados tarde, y books
  que **limitan rápido a los ganadores en props**. Se entra con expectativas moderadas
  y stakes recortados, o no se entra.

**Criterio de salida**: cada mercado nuevo repite los gates de F1 y F2 a escala
reducida — log loss < market prior de ese mercado en validación temporal, ECE dentro
del umbral, y su propio periodo de paper trading antes de exponerse a usuarios. Un
mercado que no pasa su gate no se publica, aunque "ya esté programado".

### F5 — Multi-deporte según calendario

El calendario 2026 marca las ventanas naturales: **NFL y Champions League arrancan en
septiembre 2026; NBA y NHL en octubre 2026**. Son anclas de calendario, no
compromisos: la expansión solo ocurre si los gates previos pasaron, y cada deporte
repite F1–F2 (backtest + paper trading propios).

Qué se reusa y qué no:

| Se reusa tal cual | Se construye por deporte |
|---|---|
| Ingesta de odds (mismos snapshots de The Odds API cubren todos los deportes; el costo incremental en créditos es marginal, ver `02-fuentes-de-datos.md` §4) | Fuente de stats (nflfastR/NFLverse, `nba_api`, NHL API, football-data.org — ver doc-02) |
| No-vig, edge, EV, Kelly (`05-motor-ev-y-bankroll.md` — agnóstico al deporte) | Feature engineering completo (EPA/play en NFL, xG en NHL/soccer, pace/usage en NBA) |
| Pick log, CLV tracking, auditoría (schema de doc-03 ya es multi-deporte vía `sport_key`) | Modelo + calibración + backtest walk-forward por mercado |
| Dashboard y scan diario | Política de lineups/inactivos propia (QB out ≠ lineup MLB tardío) |

Orden sugerido: NFL primero (datos públicos excelentes vía NFLverse, un slate semanal
que simplifica la operación), NBA/NHL después (slate diario, más volumen), Champions
al final (datos de soccer más fragmentados y mercado muy eficiente en ligas top).

---

## 2. Riesgos técnicos y mitigación

| Riesgo | Impacto si se materializa | Mitigación concreta |
|---|---|---|
| **Data leakage** (features con información posterior al momento de decisión) | Backtest espectacular, producción mediocre; la peor forma de enterarse es con dinero real | Checklist anti-leakage de `04-features-y-modelos.md` §4 aplicado en code review de **todo** PR que toque features; features as-of con `cutoff_ts` explícito; test automatizado que compara feature vector de backtest vs producción para el mismo evento |
| **Overfitting** (el modelo memoriza la muestra) | Métricas de validación infladas, edge inexistente | Validación temporal estricta (walk-forward, jamás shuffle aleatorio); gate de market prior: si no bate el log loss del mercado en ventanas fuera de muestra, no se publica; regularización y features por bloques (doc-04) |
| **Odds no disponibles al momento real** (backtest sobre líneas que nadie pudo apostar) | EV simulado imposible de capturar en la práctica | Snapshots propios con `captured_at` desde el día 1 (F0); el backtest solo consume precios de `odds_snapshots` anteriores al momento simulado de decisión; el closing se usa para CLV, nunca como precio apostable |
| **Lineups tardíos** (pick generado antes del lineup oficial) | Features de lineup falsas en backtest; picks basados en supuestos rotos (scratch del abridor) | Flag `is_confirmed` honesto en el feature vector (doc-04 §1.5): proyección declarada cuando no hay lineup oficial; política de re-análisis: si tras publicar cambia el abridor o la línea se mueve más del umbral de config (doc-05 §Guardrails), el pick se re-evalúa y se marca — nunca se edita en silencio |
| **Rate limits / costos de API** | Ingesta rota a mitad de temporada o factura inesperada | Presupuesto de créditos de `02-fuentes-de-datos.md` §2 con margen ~3.8× sobre el escenario base; contador de créditos consumidos (los headers de The Odds API lo reportan) con alerta al 70%; caché local de respuestas y snapshots agregados por slate en vez de por evento |
| **ToS de scraping** | Bloqueo de IP, pérdida de fuente, riesgo legal para un producto de pago | Evitarlo en el MVP: MLB Stats API + The Odds API cubren todo lo necesario; pybaseball se usa para históricos puntuales, no para scraping continuo; cualquier fuente scrapeada queda explícitamente fuera del producto comercial (doc-02) |
| **Exposición de API keys** | Keys revocadas, cuota robada, acceso a la DB | Env vars siempre, jamás keys en el repo (`.gitignore` de `.env` desde el commit 1); secret manager del proveedor de hosting en producción; keys distintas por entorno y rotación si hay sospecha |
| **Deriva del modelo** (la liga cambia: reglas, pelota, meta de bullpens) | Calibración que se degrada en silencio mientras se siguen publicando picks | Gate de ECE rolling de 60 días (doc-04 §3): si ECE > 0.03, el sistema **deja de publicar automáticamente**; reentrenos programados por ventana walk-forward; monitoreo de calibración por mercado en el dashboard interno |
| **Dependencia de un solo proveedor de odds** | The Odds API cambia pricing, cobertura o desaparece → el producto se queda ciego | Interfaz `OddsClient` desde F0: el resto del sistema consume snapshots normalizados de `odds_snapshots`, no el formato del proveedor (doc-03 ya desacopla el schema); el historial acumulado es propio y sobrevive al proveedor; alternativas identificadas en doc-02 §1 para migrar si hace falta |

---

## 3. Riesgo legal/regulatorio del SaaS (worldwide)

Sin rodeos: **vender pronósticos deportivos está regulado o restringido en varias
jurisdicciones**, y "worldwide" multiplica el problema en vez de diluirlo. Lo que hay
que asumir:

- La regulación varía radicalmente por país: en algunas jurisdicciones los servicios
  de pronósticos/tipsters se tratan como actividad conexa al juego (con requisitos de
  licencia o registro), en otras como contenido informativo, y en varias la
  **publicidad y promoción de apuestas** está fuertemente restringida aunque el
  servicio en sí no lo esté. No se puede asumir que "solo informamos" es defensa
  suficiente en todas partes.
- Lo que juega a favor del diseño actual: el producto **no acepta apuestas, no coloca
  apuestas, no procesa dinero de apuestas ni custodia bankrolls** (decisión #5 de
  `00-decisiones.md`). Vende análisis, trazabilidad y gestión de riesgo. Esa
  separación hay que mantenerla a rajatabla: cualquier feature que acerque el producto
  a "intermediario de apuestas" (deep links con afiliación a books, colocación
  automática, custodia de fondos) cambia la categoría regulatoria por completo.
- Obligatorio en producto desde el primer usuario externo: disclaimers de **+18**,
  mensajes de **juego responsable** con recursos de ayuda, y lenguaje explícito de que
  no se garantizan ganancias ni es asesoría financiera. Esto además es coherente con
  el principio no negociable de no prometer rentabilidad.
- **Consultar a un abogado antes de cobrar la primera suscripción** — es el criterio
  de salida de F3, no un consejo opcional. La consulta debe cubrir las jurisdicciones
  objetivo reales (empezando por México y los países desde donde lleguen los primeros
  usuarios), la clasificación del servicio en cada una, y las obligaciones fiscales de
  vender suscripciones transfronterizas (IVA/VAT — un merchant of record tipo Paddle o
  Lemon Squeezy puede simplificar esto frente a Stripe directo).
- Si la revisión legal lo pide, **bloqueo por geografía**: no vender (o directamente
  no servir) en jurisdicciones problemáticas. Es más barato renunciar a un mercado que
  litigar en él. El diseño SaaS con auth centralizada lo hace trivial de implementar.
- Punto conexo de doc-02: la exhibición de odds de terceros en el dashboard puede
  requerir permiso o proveedor licenciado según jurisdicción — entra en la misma
  revisión legal.

Nada de esto bloquea F0–F2 (uso interno, sin usuarios ni cobros). Bloquea, por
diseño, el paso de F3 a cobrar.

---

## 4. Costos aproximados mensuales

### MVP (F0–F2, operación interna)

| Concepto | Proveedor / plan | Costo mensual (USD) |
|---|---|---|
| Odds | The Odds API, plan 20K créditos (`02-fuentes-de-datos.md`) | ~$30 |
| Stats MLB | MLB Stats API + pybaseball | $0 |
| Hosting API + cron | Railway / Fly.io / Render (instancia chica) | $5–10 |
| Postgres gestionado | Neon / Supabase (free tier → primer tier de pago) | $0–19 |
| Dashboard | Vercel (Hobby → Pro) | $0–20 |
| Dominio | ~$10–12/año amortizado | ~$1 |
| LLM (explicaciones de picks) | ver desglose abajo | $0–5 |
| **Total** | | **≈ $36–85** |

**Escenario base ≤ $50/mes**: The Odds API ($30) + hosting mínimo ($5) + Postgres y
Vercel en free tier ($0) + dominio ($1) + LLM solo para picks publicados (<$1) ≈
**$37/mes**. El presupuesto de la decisión #3 (`00-decisiones.md`, ≤$50/mes) se
cumple con margen.

**Qué recortar si se pasa de $50**, en orden:

1. Vercel Pro → Hobby (el dashboard interno de F2 no necesita Pro).
2. Postgres de pago → free tier de Neon/Supabase (el volumen de F0–F2 cabe: los
   snapshots del escenario base son cientos de filas/día).
3. API + cron consolidados en una sola instancia mínima (o un VPS de $5).
4. Explicaciones LLM solo para picks publicados, no para todo el slate analizado.
5. Lo único **no recortable** es The Odds API $30: es la fuente de odds y del
   historial propio. Si hay que recortar ahí, el proyecto no es viable con este
   diseño y hay que replantear, no degradar los snapshots (romperían F0 y el CLV).

### Costo LLM por pick (capa de explicación)

Recordatorio de `01-propuesta-tecnica.md`: el LLM redacta la explicación a partir de
features estructuradas; jamás produce probabilidades. Eso hace el prompt corto y el
costo predecible.

Estimación con un modelo económico de la gama actual (ej. Claude Haiku 4.5: $1.00 por
millón de tokens de entrada, $5.00 por millón de salida; pricing oficial:
<https://platform.claude.com/docs/en/pricing>):

```text
por pick: ~3,000 tokens entrada (features + contexto lesiones/lineup)
        +  ~400 tokens salida  (explicación de 2-3 párrafos)
costo   ≈ 3,000 × $1/1M + 400 × $5/1M ≈ $0.003 + $0.002 = ~$0.005 USD/pick
```

Es decir, **~medio centavo de dólar por pick**. Con el volumen del slate MLB:

- Solo picks publicados (~0–5/día con los umbrales de doc-05): **< $1/mes**.
- Explicación para todo el slate analizado (~15 juegos × 2 mercados = 30
  análisis/día): 30 × 30 días × $0.005 ≈ **~$4.50/mes**.

Conclusión: el LLM no es driver de costo en el MVP. Si el volumen crece (multi-deporte,
multi-tenant), el Batch API (-50%) y prompts cacheados lo mantienen marginal.

### Costos que escalan al crecer (F3+)

- **Más snapshots / más frecuencia**: dentro del plan 20K hay margen ~3.8×; escalar
  frecuencia F5 u odds por jugador (props, F4) puede forzar el siguiente tier de The
  Odds API.
- **Historical odds**: backfills de histórico consumen créditos aparte (10× por
  snapshot histórico, doc-02 §1.1) — comprarlos es un gasto puntual que solo se
  justifica con evidencia de que el mercado nuevo lo amerita.
- **Más deportes**: los snapshots agregados por deporte suman créditos, y cada
  deporte añade cómputo de features/entrenamiento (más CPU en hosting, más storage).
- **Multi-tenant**: Postgres y hosting suben de tier con usuarios concurrentes;
  email transaccional (Resend/Postmark, ~$10–20/mes); monitoreo/logs.
- **Billing**: Stripe cobra ~2.9% + $0.30 por transacción (más gestión de IVA/VAT
  por país); un merchant of record cobra más por transacción pero absorbe la carga
  fiscal — decisión para F3 junto con la revisión legal (§3).
- **LLM**: escala lineal con picks × deportes × usuarios que pidan análisis bajo
  demanda; sigue siendo el rubro menor.

---

## 5. Criterio de continuidad: cuándo el proyecto sirve y cuándo se descarta

Resumen ejecutivo — los criterios completos, con umbrales y sample mínimo, están en
`06-backtesting-y-metricas.md` y son la fuente de verdad:

- **Sigue** si, tras el sample definido de paper trading (≥ 300 picks), el sistema
  demuestra lo que promete: batir al market prior en log loss fuera de muestra,
  calibración dentro del gate (ECE ≤ 0.03) y **CLV positivo sostenido** (beat-rate
  contra el closing no-vig de Pinnacle). El CLV es la señal más rápida y menos
  ruidosa de edge real (doc-05 §CLV, doc-06).
- **Se mata sin drama** si tras ese sample no bate el CLV ni al market prior. Sin
  renegociar umbrales a posteriori, sin "una temporada más a ver si mejora", sin
  seleccionar el subperiodo bueno. Los umbrales se fijaron antes de ver los
  resultados precisamente para esto.

Y el punto que hace racional intentarlo: **el costo hundido es acotado y conocido de
antemano** — del orden de 3–4 meses × <$50/mes ≈ **$150–200 USD** más el tiempo de
desarrollo. No hay dinero real apostado antes del go/no-go, no hay usuarios pagando
antes de la validación y la revisión legal, y el subproducto de un "no" (pipeline de
datos, historial de odds propio, infraestructura de backtesting) queda reutilizable
para cualquier análisis futuro. El escenario malo es barato; el escenario bueno se
gana el derecho a existir con evidencia, no con fe.
