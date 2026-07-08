# 02 — Fuentes de datos

Inventario de APIs y fuentes por deporte, con costos, límites y riesgos. Complementa
`00-decisiones.md` (presupuesto ≤ $50 USD/mes, decisión #3) y alimenta el schema de
`03-modelo-de-datos.md` y el pipeline de `01-propuesta-tecnica.md`.

**Nota de verificación**: los datos de pricing y mecánica de créditos se verificaron
el 2026-07-07 (y se re-verificaron con un segundo pase adversarial el mismo día)
contra fuentes secundarias que citan la documentación oficial, mirrors públicos de la
doc en GitHub y búsqueda de código de uso real de la API. El sitio de The Odds API
bloquea el fetch automatizado desde este entorno (403), así que todo lo que no se pudo
confirmar está marcado **"a confirmar"**. Antes de contratar el plan, validar los
números contra <https://the-odds-api.com/> y
<https://the-odds-api.com/liveapi/guides/v4/>.

---

## 1. Odds: comparativa de proveedores

| Proveedor | Qué da | Pros | Contras | Costo | Riesgo |
|---|---|---|---|---|---|
| **The Odds API** ([docs v4](https://the-odds-api.com/liveapi/guides/v4/)) | Odds pre-match y live de decenas de books (US/UK/EU/AU), mercados featured (h2h, spreads, totals) + additional markets (periodos, props), historical odds desde 2020 | Self-service, pricing público, cubre Pinnacle, incluye mercados F5 de MLB, historical incluido en planes pagos | Additional markets solo por evento (más caro en créditos) y limitados a deportes US y books seleccionados; snapshots, no streaming | Free 500 créditos/mes; **20K → $30/mes**; 100K → $59; 5M → $119; 15M → $249 | Bajo. Proveedor establecido; el riesgo es operativo (cobertura de un book puede cambiar sin aviso) |
| **Sportradar** ([developer portal](https://developer.sportradar.com/)) | Feeds enterprise: stats + odds + live, 80+ deportes, datos oficiales de ligas | Calidad y SLA enterprise, cobertura total | Pricing opaco, contrato de ventas, sobredimensionado para MVP | Sin precio público; estimaciones de terceros van de ~$1,250/mes a $10K+/mes según feed. Trial de 30 días con rate limits reducidos | Nulo en ToS, **prohibitivo en costo** para presupuesto ≤$50 |
| **SportsGameOdds** ([pricing](https://sportsgameodds.com/pricing)) | Odds de 80+ books (incl. Pinnacle), grading, línea histórica según plan | Free tier de prueba (plan "Amateur"), buena cobertura de books | Free tier con límites bajos (rate limit ~10 req/min; cuotas mensuales exactas **a confirmar**), insuficiente para producción; el salto es directo al plan Rookie de $99/mes — fuera de presupuesto | Free / $99 (Rookie) – $499 (Pro) /mes | Bajo en ToS, alto en costo relativo |
| **OpticOdds** ([sitio](https://opticodds.com/pricing)) | Odds real-time streaming (SSE) de 200+ books, orientado a trading | Latencia mínima, cobertura masiva | Enterprise puro: sin free tier, sin trial público, ventas por formulario; reportes de terceros hablan de ~$5K/mes por deporte | Sin precio público (enterprise) | Prohibitivo para MVP |
| **Scraping directo de books** (Pinnacle, Bet365, Caliente…) | Lo que se logre extraer | "Gratis", cualquier mercado visible en el sitio | Viola ToS de los books, HTML/APIs internas cambian sin aviso, bloqueos por IP/fingerprint, riesgo de ban de cuentas | Costo en horas de ingeniería y mantenimiento permanente | **Alto**: legal (ToS) + fragilidad técnica. Descartado como fuente primaria |

**Recomendación MVP: The Odds API, plan 20K créditos ($30/mes USD).** Es el único
proveedor con pricing público que cabe en el presupuesto, cubre Pinnacle (línea de
referencia para no-vig y CLV, ver `05-motor-ev-y-bankroll.md`) y ofrece el mercado F5
de MLB. Quedan ~$20/mes de holgura para cualquier otro gasto de API.

### 1.1 Mecánica de créditos de The Odds API (verificada)

Esto define todo el plan de consumo, así que va explícito:

- **Endpoint `/v4/sports/{sport}/odds` (featured markets: h2h, spreads, totals)**:
  una llamada devuelve **todos los juegos del slate**. Costo =
  `[mercados únicos devueltos] × [regiones solicitadas]`. Ej.: h2h en regiones
  `us,eu` = 1 × 2 = **2 créditos** por snapshot de todo el slate.
- Se cobra por mercados **devueltos**, no solicitados: si pides 5 mercados y solo hay
  datos de 2, pagas 2. Respuestas vacías no cuestan. Los endpoints `/sports` y
  `/events` (listado de eventos, sin odds) son gratis.
- **Additional markets (periodos, alternates, props)** — aquí vive el F5 de MLB:
  clave de mercado **`h2h_1st_5_innings`** (1st 5 Innings Moneyline), listada en la
  [lista oficial de mercados](https://the-odds-api.com/sports-odds-data/betting-markets.html).
  Solo se consultan vía el endpoint **por evento**
  `/v4/sports/{sport}/events/{eventId}/odds`, con el mismo costo
  `mercados × regiones` **pero por evento**: un slate de 15 juegos cuesta 15× lo que
  costaría una llamada agregada.
- **Advertencia (confirmado)**: la documentación limita los additional markets a
  deportes US y **books seleccionados** — la página oficial de MLB habla de "most US
  bookmakers" para innings markets. Lo que sigue **a confirmar** es si **Pinnacle
  publica `h2h_1st_5_innings` vía The Odds API**. Verificación concreta al contratar:
  llamar al endpoint de evento con `markets=h2h_1st_5_innings&regions=eu,us`
  y revisar qué bookmakers responden. Si Pinnacle no aparece, la línea de referencia
  F5 para no-vig tendrá que ser un consenso de books US (documentar la decisión en
  `00-decisiones.md` si ocurre).
- **Historical odds** ([página oficial](https://the-odds-api.com/historical-odds-data/)):
  disponible solo en planes pagos. Featured markets: snapshots desde junio 2020
  (intervalos de 10 min; de 5 min desde septiembre 2022). **Additional markets (incl.
  F5): solo hay historia desde 2023-05-03** — pedir fechas anteriores devuelve error
  (`HISTORICAL_MARKETS_UNAVAILABLE_AT_DATE`). Costo = **10× el equivalente live**:
  `10 × mercados × regiones` por snapshot agregado, y
  `10 × mercados × regiones × evento` en el endpoint histórico por evento. Es decir,
  reconstruir historia de additional markets (F5) es ~150× más caro que capturarla en
  vivo para un slate de 15 juegos. Conclusión operativa en §6.
- **Regiones**: `us`, `us2`, `uk`, `eu`, `au` (y `fr`, `se`). **Pinnacle está en la
  región `eu`** (confirmado 2026-07-07 contra mirrors públicos de la
  [lista oficial de bookmakers](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html);
  re-verificar en runtime porque la cobertura por book cambia sin aviso; reportes de
  terceros señalan que el feed público de Pinnacle puede ir por detrás de su sitio).
  **Bet365**: los mirrors vigentes de la lista **no** muestran `bet365` en `uk`; solo
  existe `bet365_au` (región `au`, disponible únicamente en planes pagos y con
  cobertura de mercados limitada) — **a confirmar** al contratar. Si Bet365 no resulta
  utilizable vía API, el usuario compara sus precios manualmente (igual que los books
  MX). Si se agrega una región extra, cada snapshot cuesta 3 regiones en vez de 2 (el
  plan de §2 lo soporta con margen). Optimización disponible: el parámetro
  `bookmakers` sustituye a `regions` y se factura a razón de **1 región por cada 10
  books** — pedir solo `pinnacle` + los books US relevantes puede bajar el costo por
  snapshot a 1 "región".
- Los books MX (Caliente, Codere) **no** están en The Odds API ni tienen API pública
  (decisión #6): el usuario compara esos precios manualmente.

---

## 2. Plan de consumo de créditos (presupuesto ≤ $50/mes)

Supuestos: slate MLB de ~15 juegos/día, 2 mercados (`h2h` y `h2h_1st_5_innings`),
regiones `us + eu` (Pinnacle + books US), 30 días/mes. Los juegos MLB salen en ~3
"olas" horarias (≈13:00, ≈19:00, ≈21:40 ET), lo que permite programar el snapshot de
cierre por ola en vez de por juego.

### Moneyline (featured market, endpoint agregado — barato)

```text
costo por snapshot del slate = 1 mercado × 2 regiones            = 2 créditos
snapshots/día = 6 periódicos (cada ~3h) + 3 de cierre (una por ola) = 9
créditos/día  = 9 × 2                                             = 18
créditos/mes  = 18 × 30                                           = 540
```

### F5 Moneyline (additional market, endpoint por evento — el rubro caro)

```text
costo por snapshot del slate = 15 eventos × 1 mercado × 2 regiones = 30 créditos
snapshots/día = 4 periódicos + 1 de cierre por juego (≈1 extra
                por juego, agrupado por ola ≈ 15 llamadas extra)*  ≈ 5 pasadas
créditos/día  ≈ 5 × 30                                             = 150
créditos/mes  ≈ 150 × 30                                           = 4,500
```

\* El snapshot "de cierre" F5 se toma ~10–20 min antes del primer pitch de cada ola;
como el endpoint es por evento, cuesta lo mismo agrupado o no.

### Total y margen

| Rubro | Créditos/mes |
|---|---:|
| ML (9 snapshots/día, 2 regiones) | 540 |
| F5 (≈5 pasadas/día por evento, 2 regiones) | 4,500 |
| Scores/resultados para settle (endpoint scores: 2 créditos/llamada con `daysFrom`, ~2 llamadas/día) | ~120 |
| **Subtotal escenario base** | **~5,200** |
| Escenario alto: +región `uk` en todo (×1.5) | ~7,800 |
| Escenario alto + doble frecuencia F5 en horas previas al cierre (las pasadas F5 extra van a 2 regiones: `uk` no aporta additional markets, que son de books US) | ~12,300 |
| **Plan 20K — margen sobre escenario base** | **~3.8×** |

Incluso el escenario alto deja ~38% del plan libre para re-scans bajo demanda
(decisión #10), pruebas de desarrollo y algún backfill histórico puntual de featured
markets (ej. 30 días de closing ML: `30 días × 10 × 1 × 2 = 600` créditos). El plan
20K a $30/mes cumple el presupuesto con holgura; **no** contratar 100K de entrada.

Regla operativa: registrar `x-requests-remaining` (header que devuelve la API en cada
respuesta) en cada ingesta y alertar si el ritmo proyectado supera el 80% del plan.

---

## 3. MLB — fuentes de stats (MVP, detallado)

### 3.1 MLB Stats API (`statsapi.mlb.com`)

- **Qué da**: schedule, probables (pitchers anunciados), lineups confirmados,
  boxscores, play-by-play (live feed), rosters, transacciones (incl. IL), park info.
  Es la fuente primaria del MVP para todo lo estructural del juego.
- **Acceso**: HTTP público, sin API key. Documentación oficial mínima
  (<https://statsapi.mlb.com/docs/>); en la práctica se usa la documentación
  comunitaria y el wrapper [toddrob99/MLB-StatsAPI](https://github.com/toddrob99/MLB-StatsAPI) (Python).
- **Latencia**: casi tiempo real para lineups/boxscores/live feed (segundos-minutos).
- **Límites**: sin rate limit publicado; usar backoff y cache locales por cortesía.
- **Riesgo ToS — decirlo claro**: el copyright de MLBAM
  (referenciado en `gdx.mlb.com/components/copyright.txt`) autoriza uso
  **individual, no comercial y no masivo**; uso comercial requiere autorización
  escrita de MLBAM. EDGE es un SaaS: esto es un **riesgo legal real**, no teórico.
  Mitigaciones: (a) el producto vende análisis derivado, no re-publica el feed crudo;
  (b) evaluar licencia/permiso o proveedor licenciado antes de cobrar suscripciones
  (mismo checkpoint regulatorio de la decisión #5); (c) mantener la capa de ingesta
  desacoplada para poder cambiar de fuente. Registrar la resolución en `00-decisiones.md`.
- **Confiabilidad**: alta (es la fuente que alimenta MLB.com). Cambios de schema
  ocurren pero son raros.

### 3.2 pybaseball (Statcast/Baseball Savant, FanGraphs, Baseball Reference)

- **Qué da** ([repo](https://github.com/jldbc/pybaseball)): Statcast pitch-level
  (velocidad, movimiento, xwOBA, barrel%, etc.) vía Baseball Savant; stats agregadas
  y proyecciones vía FanGraphs; histórico vía Baseball Reference. Insumo principal de
  features de pitchers/bateadores (ver `04-features-y-modelos.md`).
- **Costo**: gratis (es un scraper/cliente, no una API con contrato).
- **Latencia**: Statcast se consolida horas después de cada juego — suficiente para
  features as-of del día siguiente; no sirve para intra-día.
- **Límites/riesgo**: FanGraphs y B-Ref son sitios con ToS que limitan scraping
  automatizado; B-Ref ha bloqueado scrapers agresivos. Riesgo **medio**: mitigar con
  cache local agresivo (una descarga diaria batch, nunca por-request de usuario) y
  respetando rate limits del propio pybaseball. Savant tolera descargas moderadas.
- **Confiabilidad**: alta en datos, media en interfaz (cuando un sitio cambia HTML,
  pybaseball se rompe hasta el siguiente release).

### 3.3 Retrosheet (histórico)

- **Qué da** (<https://www.retrosheet.org/>): play-by-play y game logs históricos de
  décadas, ideal para construir el dataset de backtest de ML y F5 (runs por inning).
- **Costo**: gratis. **Licencia**: exige el aviso de atribución de Retrosheet en
  cualquier trabajo derivado — incluirlo en el repo y en la doc del dataset.
- **Latencia**: N/A (archivos batch, actualizaciones periódicas post-temporada).
- **Confiabilidad**: muy alta para histórico; no sirve para nada operativo del día.

### 3.4 Park factors (Baseball Savant)

- **Qué da**: factores por estadio (<https://baseballsavant.mlb.com/leaderboard/statcast-park-factors>),
  relevantes para F5/totals y para ajustar ofensiva esperada.
- **Costo/latencia**: gratis; cambian lento (se refrescan pocas veces por temporada),
  así que se ingieren como tabla semi-estática con fecha de vigencia (as-of).
- **Riesgo**: bajo; mismo perfil Savant que §3.2.

### 3.5 Clima

- **Open-Meteo** (<https://open-meteo.com/>): forecast por coordenadas de estadio.
  Free tier: 10,000 llamadas/día **solo uso no comercial** (CC BY 4.0, requiere
  atribución). **Ojo**: EDGE como SaaS es uso comercial → el plan comercial arranca
  en **€29/mes** ([pricing](https://open-meteo.com/en/pricing)), lo que se comería el
  margen del presupuesto.
- Alternativa para MVP: **NWS / api.weather.gov** (gratis, sin restricción comercial,
  dominio público del gobierno de EE.UU.) cubre los 29 estadios en EE.UU.; solo
  Toronto queda fuera. Para Toronto usar **Environment Canada (GeoMet)**, cuya
  licencia abierta permite uso comercial con atribución; **no** usar el free tier de
  Open-Meteo para ese estadio: la restricción es por tipo de uso (comercial), no por
  volumen, así que "volumen trivial" no lo exime. Decisión sugerida: NWS como fuente
  primaria de clima + GeoMet para Toronto.
- **Qué da para el modelo**: temperatura, viento (velocidad/dirección), precipitación
  → features de carry de bola y riesgo de delay/postponement. Latencia: forecasts
  horarios, suficiente con 2 pulls/día por estadio (~30–60 llamadas/día en total).

### 3.6 Umpires (fase 2+, no MVP)

- El umpire de home plate afecta K% y runs (zona amplia/estrecha), útil para NRFI y
  props de K en fase 2. **Problema**: no hay fuente oficial estable pre-juego; las
  asignaciones se publican el mismo día y los datos de tendencia (ej. Umpire
  Scorecards) vienen de scraping o proyectos comunitarios — **frágiles y con ToS
  dudoso**. La asignación sí aparece en el live feed de MLB Stats API, pero a menudo
  demasiado cerca del primer pitch para ser útil.
- Decisión: **fuera del MVP**. Si en fase 2 el backtest muestra que la señal paga el
  costo de mantenimiento, se integra con scraping propio versionado y monitoreado.

---

## 4. Lesiones y lineups MLB

### 4.1 Fuente primaria: MLB Stats API

- **IL / transacciones**: el endpoint de transactions y los rosters reflejan
  movimientos de Injured List con latencia de minutos tras el anuncio oficial. Es
  suficiente para el MVP: no necesitamos rumores, necesitamos hechos confirmados
  as-of (regla anti-leakage del brief: el backtest solo puede usar lo que era público
  antes del primer pitch, con timestamp).
- **Lineups confirmados**: aparecen en el boxscore/live feed del juego cuando los
  clubes los publican, típicamente **~1–4 horas antes del primer pitch** (varía por
  equipo y por juego día/noche).

### 4.2 Impacto en cuándo apostar (operativo)

- **F5 depende del abridor**: un pick F5 publicado antes de confirmar al probable es
  apostar a ciegas contra un mercado que sí reaccionará al anuncio. Regla del MVP:
  el motor puede *pre-evaluar* con probables anunciados, pero un pick F5 solo se
  **publica** con probable confirmado y se **re-evalúa** (o invalida) si hay scratch.
- **ML full-game** es menos sensible al lineup exacto pero sí al abridor y al estado
  del bullpen. Mismo principio: el snapshot de features de cada pick guarda qué
  estaba confirmado y qué era proyección al momento de publicar (auditoría, ver
  `03-modelo-de-datos.md`).
- Consecuencia para el plan de créditos de §2: los snapshots de odds "de cierre por
  ola" coinciden con la ventana post-lineup, que es cuando el pick es accionable y
  cuando el CLV se puede medir de forma honesta.

### 4.3 Alternativas de scraping (Rotowire, etc.) — con su riesgo

Rotowire, MLB.com starting lineups, FantasyPros y similares publican lineups
proyectados/confirmados antes que algunos feeds. **Riesgo explícito**: son sitios
comerciales cuyos ToS prohíben scraping; el HTML cambia sin aviso; y un lineup
"proyectado" de terceros dentro del pipeline introduce ruido no auditable. Decisión:
**no scraping de lineups en MVP**. La MLB Stats API da lo confirmado, que es lo único
que el motor debería tratar como hecho. Si en fases futuras se quiere la señal
"proyectado", se aísla como feature de baja confianza con fuente y timestamp propios.

---

## 5. Fases futuras (resumen por deporte)

| Deporte | Fuente stats | Costo | Notas y riesgos |
|---|---|---|---|
| **NBA** | [`nba_api`](https://github.com/swar/nba_api) (endpoints de NBA.com/stats) | Gratis | Endpoints no oficiales: headers/bloqueos cambian periódicamente; mismo perfil ToS que MLB Stats API (datos propietarios de la liga). Odds: mismos featured markets de The Odds API, sin costo marginal de plan. |
| **NFL** | [nflverse / nflfastR](https://nflfastr.com/) | Gratis | Play-by-play limpio desde 1999, EPA/CPOE precalculados, distribución por releases en GitHub — la fuente gratuita más sólida de todas las ligas. Cadencia semanal encaja con slate NFL. |
| **NHL** | NHL API (`api-web.nhle.com`, no oficial-documentada) + [MoneyPuck](https://moneypuck.com/about.htm) como referencia conceptual de xG | Gratis | API pública sin key pero sin docs oficiales (comunidad). MoneyPuck publica CSVs útiles para prototipos; no es fuente contractual. Goalie confirmado = el "lineup" crítico, mismo patrón que §4. |
| **Champions League** | [football-data.org](https://www.football-data.org/) (free tier: 12 competiciones incl. UCL, 10 req/min, scores con delay) + [StatsBomb Open Data](https://github.com/statsbomb/open-data) (event data, solo partidos seleccionados) + [soccerdata](https://pypi.org/project/soccerdata/) (scrapers FBref/Understat/etc.) | Gratis / freemium | **El más fragmentado**: ninguna fuente gratuita da xG actual + lineups + odds de forma estable. soccerdata es scraping con ToS de terceros (FBref/Understat) — mismo riesgo que §3.2 pero peor mantenido. Datos de calidad (Opta, StatsBomb licenciado) son caros. Presupuestar tiempo de integración 2–3× lo de MLB antes de comprometer fechas. |

En todos los casos, la capa de odds ya está resuelta por The Odds API (mismo plan,
mismos créditos); el costo incremental real de cada deporte es stats + features +
modelo, no odds. Ver `07-roadmap.md` para el orden.

---

## 6. Qué es caro o poco confiable (explícito)

1. **Sportradar y OpticOdds**: calidad enterprise real, pero pricing opaco por
   contrato (estimaciones de terceros: cuatro-cinco cifras mensuales). Para un MVP
   con presupuesto ≤$50/mes ni siquiera son opción a evaluar; reconsiderar solo si el
   SaaS factura lo suficiente para justificar SLA y datos oficiales.
2. **Scraping de sportsbooks**: viola ToS, se rompe con cada rediseño, invita
   bloqueos, y contamina la auditabilidad (odds sin fuente contractual). No es
   "gratis": es deuda de mantenimiento permanente. Descartado como fuente primaria;
   como máximo, verificación manual puntual de books MX por parte del usuario.
3. **Odds históricas de calidad**: los datasets comerciales buenos (closing lines
   multi-book, con timestamps) son caros, y el endpoint histórico de The Odds API
   cuesta 10× el live — reconstruir historia de F5 (por evento) para una temporada
   costaría del orden de 50K+ créditos, más que todo el plan mensual; y antes de
   2023-05-03 la historia de additional markets ni siquiera existe en el proveedor
   (§1.1).
   **Recomendación operativa: acumular snapshots propios desde el día 1.** El plan de
   §2 ya captura open→moves→close de ML y F5 con Pinnacle incluido; en 2–3 meses eso
   produce un dataset de odds propio, con timestamps reales y sin costo marginal, que
   es exactamente lo que exige el backtest anti-leakage (odds apostables al momento,
   nunca el closing como si fuera apostable — ver `06-backtesting-y-metricas.md`).
   El backfill histórico se limita a featured markets (ML) si el backtest lo
   justifica, con presupuesto acotado (~600 créditos por mes de historia, 1 snapshot
   de cierre diario, 2 regiones).
4. **MLB Stats API en uso comercial**: gratis y confiable técnicamente, pero su
   copyright restringe uso comercial sin autorización (§3.1). Es la dependencia legal
   más importante del MVP y debe resolverse antes de cobrar suscripciones.
5. **Umpires y lineups proyectados**: fuentes frágiles/scraping (§3.6, §4.3). Fase
   2+, nunca ruta crítica del MVP.
