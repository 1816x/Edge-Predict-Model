# 04 — Features y modelos (MLB Moneyline + F5 Moneyline)

Este documento define el feature engineering, los modelos, la estrategia de calibración
y el checklist anti-leakage del MVP: **MLB Moneyline (ML)** y **First-5-Innings Moneyline (F5)**.
Las fuentes de datos concretas están en `02-fuentes-de-datos.md`; las tablas donde se
persisten features y snapshots están en `03-modelo-de-datos.md`; el consumo de la
probabilidad calibrada por el motor de EV/stake está en `05-motor-ev-y-bankroll.md`;
la validación walk-forward que decide si un modelo se publica está en
`06-backtesting-y-metricas.md`.

Principio rector de todo el documento: **cada feature se calcula "as-of", es decir, solo
con información que existía antes del momento de decisión** (en producción: la hora a la
que se genera el pick; en backtest: un timestamp simulado anterior al primer pitch).
Ninguna feature puede incluir datos del juego que se está prediciendo. Este principio se
repite bloque por bloque porque es donde más proyectos amateur se rompen.

---

## 1. Feature engineering

### 1.1 Convenciones generales

- **Ventanas rolling**: se definen en días calendario (`30d`) o en unidades de juego
  (`last_5_starts`). MLB juega casi a diario, así que 30 días ≈ 26-28 juegos de equipo;
  para abridores, que abren cada ~5 días, las ventanas se definen por aperturas.
- **Regla as-of**: toda ventana rolling termina en `t-1` respecto al juego a predecir.
  "wOBA rolling 30d" para un juego del 15 de julio usa juegos del 15 de junio al **14 de
  julio inclusive**, nunca el del 15.
- **Dobles carteleras y cortes intradía**: el corte real es `decision_ts`, no el día
  calendario. Para el juego 2 de una doble cartelera, las stats del juego 1 del mismo
  día solo entran al feature vector si el juego 1 estaba **finalizado antes del
  `decision_ts`** del snapshot. Si el pipeline no puede garantizar ese orden con
  timestamps confiables, la regla segura del MVP es excluir todo juego del mismo día
  (costo: una feature ligeramente desactualizada; beneficio: cero leakage intradía).
  Afecta sobre todo a `bullpen_ip_l3d`, `bullpen_b2b_flag` y `games_last_7d`.
- **Shrinkage por muestra chica**: las ventanas cortas son ruidosas. Donde aplique
  (splits vs mano, primeras semanas de temporada), la feature se regulariza hacia la
  media de liga o hacia el valor de temporada previa con un peso proporcional al tamaño
  de muestra. Es preferible una feature "aburrida" y estable a una "reactiva" que el
  modelo aprenda a sobreponderar.
- **Nombres de features en inglés** (van a código y feature store):
  `team_woba_30d`, `sp_kbb_pct_l5_starts`, `bullpen_ip_l3d`, `is_confirmed_lineup`, etc.
- **Dos vectores de features por juego**: ML y F5 comparten bloques pero con pesos y
  presencia distinta. F5 **excluye** el bloque de bullpen y sobrepondera abridor y
  primera vuelta del lineup (ver 1.9).

### 1.2 Bloque: ofensiva de equipo

| Feature (nombre en código) | Definición | Ventana | Regla as-of |
|---|---|---|---|
| `team_woba_30d` | wOBA de equipo | rolling 30d | Juegos hasta `t-1`. Excluye el juego a predecir. |
| `team_ops_30d` | OPS de equipo | rolling 30d | Ídem. |
| `team_woba_season` | wOBA acumulada de temporada | temporada hasta la fecha | Acumulado **hasta `t-1`**, no el valor de fin de temporada (error clásico de leakage con datos históricos agregados). |
| `team_woba_vs_lhp_30d` / `team_woba_vs_rhp_30d` | wOBA split vs mano del pitcher | rolling 30d (con shrinkage hacia split de temporada) | Ídem. Se selecciona el split según la mano del **abridor rival probable** conocida al momento de decisión. |
| `team_iso_30d` | Poder aislado (SLG − AVG) | rolling 30d | Ídem. |
| `team_k_pct_30d`, `team_bb_pct_30d` | K% y BB% ofensivos | rolling 30d | Ídem. |

Notas:

- Los splits vs mano en ventana de 30d dejan muestras chicas contra zurdos (los PA vs
  LHP son una fracción minoritaria del mes a nivel equipo, y mucho menor por bateador
  individual; depende del calendario de abridores que tocó): ruidosos. Shrinkage
  obligatorio hacia el split de temporada (y en abril, hacia temporada previa ponderada).
- La mano del abridor rival viene del *probable pitcher* publicado por MLB Stats API.
  Si al momento de decisión el probable cambia, el snapshot de features queda auditado
  con el probable que se usó (ver `03-modelo-de-datos.md`).

### 1.3 Bloque: pitcher abridor (el bloque más importante, sobre todo para F5)

| Feature | Definición | Ventana | Regla as-of |
|---|---|---|---|
| `sp_kbb_pct_l5_starts` | K% − BB% del abridor | últimas 5 aperturas | Aperturas anteriores al juego. |
| `sp_kbb_pct_season` | K% − BB% de temporada | temporada hasta la fecha | Acumulado hasta `t-1`. |
| `sp_xfip_season`, `sp_siera_season` | xFIP / SIERA de temporada | temporada hasta la fecha | Ídem. Son estimadores de habilidad más estables que ERA; ERA se excluye deliberadamente como feature (ruido de secuencia y defensa). |
| `sp_xfip_l5_starts` | xFIP rolling | últimas 5 aperturas | Ídem. |
| `sp_days_rest` | Días de descanso desde su apertura anterior | puntual | Calculable con el calendario; conocido antes del juego. |
| `sp_pitch_count_l2_starts` | Pitcheos lanzados en sus últimas 2 aperturas | últimas 2 aperturas | Proxy de carga de trabajo reciente y de qué tan largo lo dejarán ir. |
| `sp_tto_decay` | Tendencia times-through-order: delta de wOBA permitida 1a vuelta vs 2a/3a vuelta | temporada hasta la fecha (con shrinkage) | Solo PA de juegos anteriores. Relevante para ML (¿cuánto sobrevive la 3a vuelta?); para F5 importa la 1a-2a vuelta, donde casi todos los abridores son mejores. |
| `sp_hand` | Mano (L/R) | estática | Interactúa con los splits ofensivos del rival. |
| `sp_velo_delta_l3_starts` | Delta de velocidad promedio de fastball vs su media de temporada | últimas 3 aperturas vs temporada | Solo aperturas anteriores al juego; Statcast publica los datos de cada apertura al día siguiente, así que siempre están disponibles al `decision_ts`. Señal temprana de fatiga/lesión vía pybaseball. Fase 2 si complica el MVP. |

Regla as-of específica del bloque: **el abridor usado en el feature vector es el
"probable" publicado al momento de decisión, no el que realmente abrió**. Si hubo
cambio de abridor después de publicar el pick, eso es riesgo real del apostador y el
backtest debe reflejarlo igual (no se corrige retroactivamente el feature vector).

Dos notas de implementación anti-leakage:

- **xFIP/SIERA "hasta `t-1`" no se pueden bajar de un dump**: FanGraphs y similares
  sirven el valor final de temporada. Hay que recomputarlos desde game logs con las
  constantes de liga as-of (HR/FB% de liga hasta la fecha o de temporada previa; ver
  checklist §4, ítems 6 y 9). Bajar el xFIP final de temporada y unirlo por
  `(pitcher, season)` es leakage silencioso.
- **Limitación honesta del histórico de probables**: para temporadas anteriores al
  lanzamiento del pipeline no existen snapshots archivados de "probable pitcher as-of".
  El backtest histórico solo conoce al abridor que realmente abrió, lo que elimina el
  riesgo de scratch y sesga el resultado **ligeramente al alza**. Se documenta como
  supuesto del backtest y desaparece hacia adelante conforme el pipeline archiva sus
  propios snapshots de probables (producción y paper trading no tienen este sesgo).

### 1.4 Bloque: bullpen (clave para ML, irrelevante para F5)

| Feature | Definición | Ventana | Regla as-of |
|---|---|---|---|
| `bullpen_ip_l3d` | Innings acumulados por el bullpen en los últimos 3 días | rolling 3d | Juegos hasta `t-1`. Mide fatiga colectiva. |
| `bullpen_b2b_flag` | El equipo jugó ayer y su bullpen lanzó ≥ X innings | puntual | Calendario + box scores previos. |
| `bullpen_xfip_30d` | xFIP colectivo del bullpen | rolling 30d | Ídem. |
| `closer_available_flag` | Cerrador/leverage arms disponibles (no usados 2-3 días seguidos, no en IL) | puntual | Se infiere de uso reciente + transacciones/IL publicadas antes del juego. Es una inferencia, no un dato oficial: se guarda como flag con su heurística versionada. En backtest, las transacciones solo cuentan si están fechadas ≤ `t-1` (ver la regla de granularidad en 1.5). |
| `bullpen_ip_expected` | Innings esperados del bullpen dado el abridor (proxy: promedio de IP/apertura del abridor) | derivada (aperturas hasta `t-1`) | Conecta el bloque abridor con el bullpen: abridor corto ⇒ más exposición al bullpen. El promedio de IP/apertura se calcula solo con aperturas anteriores al juego. |

**Por qué este bloque importa para ML y no para F5**: el mercado F5 se liquida al
terminar la 5a entrada. En la enorme mayoría de juegos, las entradas 1-5 las lanza el
abridor (y ocasionalmente el primer relevo largo). El bullpen de leverage — setup,
cerrador, decisiones del manager en entradas 7-9 — simplemente **no participa** del
resultado F5. Para Moneyline de juego completo, en cambio, alrededor del 40% de los
innings los lanza el bullpen (los abridores promedian ~5 entradas por apertura en la
MLB reciente), y un bullpen fundido por una serie larga o un extra-innings de ayer
es una de las señales más accionables que el mercado a veces ajusta tarde. Conclusión
operativa: `bullpen_*` entra al vector de features de ML y **se excluye por diseño del
vector de F5** (no se le pone peso cero: no entra, para no gastar grados de libertad).

### 1.5 Bloque: lineup

| Feature | Definición | Regla as-of |
|---|---|---|
| `is_confirmed` (bool) | El lineup oficial ya fue publicado al momento de decisión | Flag honesto: `true` solo si MLB Stats API ya devolvió el lineup oficial. |
| `lineup_woba_proj` | wOBA ponderada del lineup (confirmado o proyectado) | Si `is_confirmed=false`, se usa lineup proyectado (más frecuente en los últimos 30 días) y el flag lo declara. La wOBA de cada bateador usada en la ponderación también es as-of (`t-1`), no la de fin de temporada. |
| `top4_woba_vs_hand` | wOBA del top 4 del orden vs la mano del abridor rival | Crítico para F5: la primera vuelta del lineup concentra los PA de las entradas 1-5. |
| `star_out_flag` | Bateador top-2 del equipo fuera del lineup / en IL | Solo con información publicada antes de decisión. Las transacciones/IL históricas de MLB Stats API traen **fecha, no timestamp**: en backtest, un movimiento fechado el mismo día del juego se trata como desconocido (regla conservadora: solo transacciones con fecha ≤ `t-1`), aunque en producción el scan sí pueda verlo en vivo. |

Regla dura: **jamás usar el lineup real del box score en backtest si al momento
simulado de decisión no estaba confirmado**. El backtest debe reproducir la misma
incertidumbre que producción: si el pick se genera a las 10:00 y el lineup salió a las
15:00, el feature vector usa proyección con `is_confirmed=false`. El modelo puede
aprender a ser menos agresivo cuando el flag es falso — eso es una virtud, no un bug.

**Implementación (tanda F1.3, detalle y constantes en `docs/00` addendum 2026-07-15):**

- **Fórmula de ponderación** (el doc dice "ponderada"; se fija aquí):
  `lineup_woba_proj = Σ_slot PA_share[slot]·wOBA_bateador,as-of / Σ_slot PA_share[slot]`
  sobre los slots presentes con wOBA no-None (renormalización: un lineup incompleto o un
  bateador sin línea del año simplemente cae del promedio). `top4_woba_vs_hand` es la misma
  suma ponderada sobre los slots 1-4 con `PA_share[:4]` renormalizado, usando la wOBA de
  cada bateador vs la mano del abridor rival. `PA_share` es un vector fijo por slot
  (as-of-safe, congelado). La wOBA por-bateador es 365d as-of shrunk hacia un prior de liga
  congelado; **bateador con 0 PA en el año → slot descartado, nunca el prior** (fabricaría
  un bateador). El código llama al flag `lineup_is_confirmed`.
- **Limitación honesta del histórico (simétrica a §1.3):** para temporadas anteriores al
  pipeline NO existen snapshots archivados de "lineup as-of". El backtest reconstruye el
  lineup REALIZADO del box score (`batting_order`) con `is_confirmed=false` — reproduce la
  incertidumbre de producción en el flag, pero la COMPOSICIÓN es la realizada, lo que sesga
  el resultado **ligeramente al alza** (elimina el riesgo de un cambio de último minuto en
  quién juega). Se documenta como supuesto del backtest y desaparece hacia adelante conforme
  `sync_lineups` archiva sus propios snapshots (`event_lineups`, migración 005); producción
  y paper trading no tienen este sesgo. Como en el backtest `is_confirmed` es constante-0,
  no aporta señal entrenable hasta que ese archivo madure — se conserva por honestidad.

**Implementación de `star_out_flag` (tanda F1.4, detalle en `docs/00` addendum 2026-07-16):**
ingiere la fuente NUEVA de transacciones/IL (feed `/transactions` de la MLB Stats API,
tabla `player_transactions`, migración 006, job `sync_transactions`). `star_out_flag` es la
cuenta 0/1/2 de los top-2 bateadores establecidos (≥200 PA, por wOBA as-of) del equipo en IL
as-of `event_day−1` (replay del último movimiento IL con `date < event_day`; corte ≤ t-1
porque la fecha viene sin hora). IL-based, no lineup-absence; ambos mercados; None/NaN
(nunca 0) sin archivo vivo o sin star identificable. El clasificador IL reconoce "injured
list" (2019+) y "disabled list" (pre-2019). `closer_available_flag` (§1.4) se implementó en
la tanda **F1.4b** como `bullpen_il_depletion` — la variante honesta "depleción del bullpen
por IL" (cuenta 0..K de los top-K brazos de calidad por xFIP-30d en IL as-of t-1, solo
Moneyline), **NO** identidad de cerrador (no guardamos saves/entradas); ver addendum
2026-07-19 en `docs/00`.

### 1.6 Bloque: park factors

| Feature | Definición | Ventana | Regla as-of |
|---|---|---|---|
| `park_factor_runs` | Factor de carreras del estadio (100 = neutral) | multi-año (3 años), actualizado por temporada | Se usa el factor calculado con temporadas **anteriores** a la fecha del juego; nunca el factor "final" de la temporada en curso. |
| `park_factor_hr` | Factor de home runs | ídem | Ídem. |
| `roof_type` | `open` / `retractable` / `dome` | estática | Determina si el bloque clima aplica. |

Los park factors afectan a totales más que a moneyline, pero interactúan con el perfil
del equipo (equipo de poder en parque de HR) y son baratos de calcular; se incluyen.

### 1.7 Bloque: clima

| Feature | Definición | Regla as-of |
|---|---|---|
| `temp_f` | Temperatura pronosticada a la hora del juego | Pronóstico disponible al momento de decisión, **no** el clima observado del box score. |
| `wind_speed`, `wind_dir_out` | Viento y si sopla hacia afuera | Ídem. |
| `roof_closed_flag` | Techo cerrado (parques retráctiles) | Si no hay confirmación, se estima por pronóstico y se marca como estimado. |

En domos y techo cerrado el bloque se anula (features en valor neutro + flag). El
impacto del clima en moneyline es de segundo orden (más relevante para totales, fase
2 con NRFI/YRFI); se incluye porque el costo marginal es bajo y el pipeline ya lo
necesitará después.

### 1.8 Bloque: descanso, viaje y localía

| Feature | Definición | Regla as-of |
|---|---|---|
| `team_rest_days` | Días sin jugar | Calendario, conocido de antemano. |
| `games_last_7d` | Juegos en los últimos 7 días (dobles carteleras) | Juegos hasta `t-1`. |
| `travel_tz_delta` | Cambio de husos horarios del último viaje | Calendario. |
| `is_home` | Localía | Trivial pero obligatoria; la ventaja de local en MLB es real y el mercado la pone en precio — el modelo la necesita para no re-derivarla mal de otras features. |

### 1.9 Por qué F5 es un buen mercado para modelar

El vector F5 se reduce deliberadamente a: **abridor (bloque 1.3 completo) + primera
vuelta del lineup (`top4_woba_vs_hand`, splits vs mano) + park/clima + localía**. Razones:

1. **Domina el abridor**: las entradas 1-5 son casi en su totalidad el enfrentamiento
   abridor vs lineup rival. Es la parte del juego con mejores estimadores de habilidad
   (K-BB%, xFIP/SIERA sobre muestras de pitcheo grandes).
2. **Elimina el ruido del bullpen**: la mayor fuente de varianza difícil de modelar en
   ML (decisiones del manager, disponibilidad día a día, relievers volátiles en muestras
   chicas) desaparece del problema por definición del mercado.
3. **Menos variables ⇒ menos overfitting**: un problema con menos fuentes de varianza y
   features más informativas es mejor terreno para un dataset de 10-19K juegos (ver §5).
4. Contrapartida honesta: el mercado F5 tiene **menos liquidez y límites más bajos** que
   ML, y hay empates F5 (push/tres vías según el book) que el motor de EV debe manejar
   (ver `05-motor-ev-y-bankroll.md`). Menos ruido no significa edge garantizado: el
   mercado F5 también sabe quién es el abridor.

---

## 2. Modelos

Escalera de tres niveles. Cada nivel existe para medir al siguiente, no como adorno.

### 2.1 Baseline 1: market prior

`p_fair` de la **línea de apertura** (o de la línea disponible al momento de decisión),
sin vig, con el método multiplicativo definido en `05-motor-ev-y-bankroll.md`:

```text
p_imp_i  = 1 / odds_decimal_i
p_fair_i = p_imp_i / (p_imp_1 + p_imp_2)
```

No tiene features ni entrenamiento. Es el punto de referencia contra el que se mide
todo lo demás, porque el mercado (en particular Pinnacle) ya es un predictor excelente.

### 2.2 Baseline 2: regresión logística con 10-15 features core

Selección sugerida (ajustable en la primera iteración, pero mantener el rango 10-15):
`sp_kbb_pct_l5_starts`, `sp_xfip_season`, `sp_days_rest` de ambos abridores,
`team_woba_30d` y `team_woba_vs_hand_30d` de ambas ofensivas, `bullpen_ip_l3d` de ambos
bullpens (solo ML), `is_home`, `is_confirmed`, `park_factor_runs`. Regularización L2,
estandarización de features (el scaler se ajusta **solo con el train de cada ventana
walk-forward**, nunca con el dataset completo — ajustarlo con todo filtra medias y
varianzas del futuro), entrenamiento por ventanas walk-forward.

Su función: (a) sanity check — si XGBoost no supera a una logística de 12 features, el
problema es de datos/features, no de capacidad del modelo; (b) interpretabilidad — los
coeficientes delatan features con signo absurdo (típico síntoma de leakage).

### 2.3 Candidato: XGBoost / LightGBM

Gradient boosting sobre el vector completo del §1 (uno por mercado: modelo ML y modelo
F5 **separados**, no un modelo con flag de mercado). Hiperparámetros conservadores para
el tamaño de dataset (§5): árboles poco profundos (`max_depth` 3-5), `min_child_weight`
alto, subsampling, early stopping contra una **sub-ventana temporal reservada al final
del train** — nunca contra la ventana de validación walk-forward con la que se reporta
y se decide publicación: usar esa ventana para early stopping (o para elegir
hiperparámetros) es seleccionar el modelo con los mismos datos que luego lo evalúan, y
sesga al alza la métrica del gate de §2.4. La búsqueda de hiperparámetros también
respeta el orden temporal (nunca CV aleatorio) y se evalúa en ventanas anteriores a la
ventana de reporte final.

Opcional razonable como feature adicional del candidato: incluir `p_fair` de apertura
como input ("residual modeling": el modelo aprende cuándo desviarse del mercado). Si se
hace, la comparación contra el baseline 1 sigue siendo obligatoria y hay que verificar
que el modelo no colapse a copiar la línea.

### 2.4 REGLA DURA de publicación

> **Si un modelo no supera el log loss del market prior (baseline 1) en validación
> temporal walk-forward, no se publica. Punto.**

Sin excepciones "porque el ROI del backtest se ve bien" — un ROI atractivo con log loss
peor que el mercado es casi seguro varianza o leakage. El mercado ya es un predictor
excelente; **igualar al mercado no aporta nada al usuario** (el pick tendría edge ~0 por
construcción) y publicarlo sería vender ruido. Batir al market prior en log loss es la
barra mínima de entrada, no la meta. Los criterios go/no-go completos (que además
incluyen ECE, CLV y paper trading) están en `06-backtesting-y-metricas.md`.

---

## 3. Calibración

### 3.1 Por qué importa más que accuracy en betting

El mecanismo, sin apelar a intuición:

- La decisión de apostar y el tamaño del stake dependen del **valor numérico** de la
  probabilidad, no del ranking. Con las definiciones canónicas del proyecto:
  `EV = p_model × (odds_decimal − 1) − (1 − p_model)` y
  `edge = p_model − p_fair`. Ambas son funciones directas de `p_model`.
- Accuracy solo mide si `p_model > 0.5` cae del lado correcto; dos modelos con
  accuracy idéntica pueden producir EVs radicalmente distintos.
- **Sobreconfianza sistemática = apostar edges fantasma**: si el modelo dice 0.58
  cuando la frecuencia real es 0.54, el sistema verá `edge = +4pts` donde hay ~0,
  pasará los umbrales de publicación, y Kelly dimensionará stakes sobre un edge que no
  existe. El resultado es pérdida esperada con apariencia de proceso disciplinado — el
  peor modo de fallo posible, porque las métricas internas (accuracy, winrate) pueden
  verse bien durante meses.
- Peor aún: la selección por umbral de edge **amplifica** el error de calibración. Solo
  se apuesta cuando `p_model` se desvía del mercado; si esas desviaciones son
  sobreconfianza, el filtro selecciona precisamente los picks más contaminados.

### 3.2 Evidencia externa, con lectura crítica

Walsh & Joshi (2023), *"Machine learning for sports betting: should model selection be
based on accuracy or calibration?"* (arXiv:2303.06021,
https://arxiv.org/abs/2303.06021), entrenan modelos con datos de NBA de varias
temporadas y corren experimentos de apuesta sobre una sola temporada usando odds
publicadas; reportan que usar calibración — en lugar de accuracy — como criterio de
selección de modelo produjo mayores retornos en promedio (ROI de +34.69% contra
−35.17%).

Lectura crítica obligatoria: el resultado es **direccionalmente útil, no una
garantía**. Es un experimento en NBA (no MLB), con un universo de modelos, periodo y
supuestos de ejecución concretos; magnitudes de ROI de esa escala no son trasladables a
producción contra límites y líneas reales, y un solo paper no es un cuerpo de evidencia.
Lo que sí sostiene — y coincide con el mecanismo de 3.1, que es matemático y no
empírico — es la dirección: **en betting, optimizar y seleccionar por calibración
domina a optimizar por accuracy**. EDGE adopta esa dirección; no cita esos ROIs como
expectativa propia (regla del brief: no prometer números).

### 3.3 Métodos y protocolo

| Método | Cómo funciona | Cuándo usarlo |
|---|---|---|
| **Platt / sigmoid** | Ajusta una logística de 2 parámetros sobre los scores | Default del MVP: robusto con pocas muestras, difícil de sobreajustar. |
| **Isotonic regression** | Ajuste monótono no paramétrico | Necesita bastantes más datos (miles de observaciones en la ventana de calibración) o produce escalones sobreajustados. Revisitar cuando haya ≥2-3 temporadas de out-of-fold acumulado. |

Protocolo:

1. **Calibrar sobre predicciones out-of-fold temporales**: el calibrador se ajusta con
   predicciones que el modelo hizo sobre datos que no vio al entrenar, generadas por el
   propio esquema walk-forward. Jamás calibrar sobre predicciones in-sample (daría una
   ilusión de calibración perfecta).
2. El calibrador es parte del artefacto versionado del modelo (mismo hash/versión que
   el modelo y el feature set; ver `03-modelo-de-datos.md` para el registro de
   `model_version` en cada pick).
3. **Gate de publicación**: `ECE ≤ 0.03` en ventana rolling de 60 días (umbral
   canónico del brief, configurable). Si el ECE rolling supera el umbral, el sistema
   deja de publicar picks de ese modelo/mercado hasta re-entrenar o re-calibrar —
   automático, no discrecional.
4. Monitoreo continuo: curva de calibración y ECE por mercado (ML y F5 por separado)
   en el dashboard interno; definiciones de Brier/log loss/ECE en
   `06-backtesting-y-metricas.md`.

---

## 4. Checklist anti-leakage (accionable)

Cada ítem es verificable en code review o con un test automatizado. Un "sí" dudoso
cuenta como "no".

1. **Joins as-of estrictos**: toda unión entre el juego y cualquier tabla de stats se
   hace con condición temporal (`stat_date < decision_ts`), idealmente con
   `pd.merge_asof` o su equivalente SQL (`LATERAL ... WHERE ts < decision_ts ORDER BY ts DESC LIMIT 1`).
   Prohibido el join por `(team, season)` a un agregado de temporada completa.
2. **Jamás stats que incluyan el juego a predecir**: toda ventana rolling termina en
   `t-1`. Test automatizado: para una muestra de juegos, recomputar la feature
   excluyendo el juego y verificar igualdad con la almacenada.
3. **Lineups solo si `is_confirmed`, o proyección con flag**: el feature vector de
   backtest usa el lineup que estaba publicado al `decision_ts` simulado; si no lo
   estaba, usa proyección y `is_confirmed=false`. Nunca el lineup del box score final.
4. **Odds del momento de decisión, no closing**: el EV y el edge del pick se calculan
   con la línea capturada al `decision_ts` (snapshot de The Odds API). El closing line
   solo se usa después, para medir CLV. Backtestear "apostando" el closing infla el
   resultado porque el closing ya incorpora información que no estaba disponible.
5. **Splits train/valid por tiempo, nunca shuffle**: separación walk-forward con corte
   por fecha (y sin juegos del mismo día repartidos entre train y valid). Prohibido
   `train_test_split(shuffle=True)` y prohibido K-fold aleatorio, incluso para búsqueda
   de hiperparámetros.
6. **Features "de temporada" calculadas hasta la fecha**: `*_season` significa
   "acumulado de temporada hasta `t-1`", nunca el total final de la temporada bajado de
   un dump histórico. Es el leakage más silencioso al trabajar con datos históricos ya
   agregados (FanGraphs/Baseball Reference sirven totales finales por default).
7. **Sin re-fit del calibrador con datos de validación**: el calibrador se ajusta solo
   con out-of-fold anterior a la ventana que se está evaluando. Re-calibrar con la
   ventana de validación y luego reportar métricas sobre esa misma ventana invalida el
   gate de ECE.
8. **Versionado de features con hash**: cada feature vector persistido lleva
   `feature_set_version` (hash del código de generación + config de ventanas). Un pick
   auditado referencia modelo + calibrador + feature set exactos; si el hash no
   coincide, la comparación backtest-vs-producción es inválida.
9. **Park factors y constantes de liga de temporadas previas**: factores de parque,
   constantes de wOBA (`wOBA scale`, pesos lineales), el HR/FB% de liga que usa xFIP,
   las constantes de SIERA y las medias de liga usadas para normalizar **o como blanco
   de shrinkage** se toman de temporadas anteriores o del acumulado hasta la fecha,
   nunca del valor final de la temporada en curso.
10. **Clima pronosticado, no observado**: en backtest, si no hay pronóstico histórico
    disponible, la feature se degrada honestamente (valor climatológico del mes +
    flag), no se rellena con el clima real del juego.

---

## 5. Sample sizes honestos

- Una temporada MLB regular ≈ **2,430 juegos** (30 equipos × 162 / 2).
- Horizonte útil realista: **4-8 temporadas ⇒ ~10,000-19,500 juegos**. Ir más atrás
  agrega volumen pero mete regímenes de juego distintos que pueden restar más de lo que
  suman.
- **Cuidado con los cambios de régimen, en particular las reglas de 2023**: pitch
  clock, prohibición del shift extremo (shift ban), bases más grandes y límite de
  pickoffs cambiaron BABIP contra la bola en juego, ritmo, robos y cargas de trabajo.
  Un modelo entrenado con peso uniforme 2016-2025 aprende relaciones (p. ej. valor del
  shift, perfiles de contacto) que ya no aplican. También existen regímenes previos
  (pelota "viva" 2019, temporada corta 2020 — considerar excluirla o sub-ponderarla).
- Mitigaciones concretas (elegir al menos una y documentarla en el registro del modelo):
  - **Ponderar recencia**: pesos de muestra decrecientes con la antigüedad (p. ej.
    decaimiento exponencial por temporada), de modo que 2023+ domine sin tirar el
    histórico.
  - **Features de era**: variable categórica `rule_era` (`pre_2023` / `post_2023`) o
    features de contexto de liga (media de carreras/juego de la temporada hasta la
    fecha) para que el modelo condicione en el régimen.
  - Validar siempre sobre las temporadas más recientes: un modelo que solo funciona en
    datos pre-2023 no es publicable.
- Consecuencia de diseño: con 10-19K observaciones, los modelos deben ser modestos
  (§2.3), la selección de features disciplinada, e isotonic calibration esperar a tener
  volumen (§3.3). Los mercados de fase 2 (NRFI/YRFI, K's de pitcher) tienen aún menos
  muestra efectiva por unidad de señal — otra razón para no meterlos al MVP.
