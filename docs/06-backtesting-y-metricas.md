# 06 — Backtesting, paper trading y métricas

Este documento define cómo se valida el sistema antes de que toque dinero real y antes
de que ninguna métrica se muestre a usuarios del SaaS: el esquema de backtesting
walk-forward, el problema (real y usualmente ignorado) de qué odds usar en un backtest,
el protocolo de paper trading, la definición exacta de cada métrica y los criterios
go/no-go. Las definiciones de edge, EV, Kelly y CLV vienen de `05-motor-ev-y-bankroll.md`;
la calibración y las reglas anti-leakage del modelo, de `04-features-y-modelos.md`; las
tablas donde todo esto se registra (`odds_snapshots`, `picks`, `clv_records`,
`daily_scans`), de `03-modelo-de-datos.md`.

Principio rector, sin adornos: **un backtest nunca demuestra rentabilidad; como mucho
descarta modelos malos.** La evidencia que cuenta se acumula en orden de credibilidad
creciente: backtest walk-forward → paper trading con precios reales apostables →
dinero real con stakes mínimos. Cada etapa puede matar el proyecto, y eso es una
feature, no un bug.

---

## 1. Backtesting walk-forward

### 1.1 Esquema temporal para MLB

Nunca shuffle. Nunca validación cruzada aleatoria. La única partición admisible es
temporal: se entrena con el pasado, se valida con un futuro que el modelo no vio, y se
repite avanzando la ventana (expanding window: cada fold añade la temporada anterior al
train set).

Con datos desde 2018 (Statcast está maduro, `pybaseball` cubre el rango, ver
`02-fuentes-de-datos.md`) y estando hoy a mitad de la temporada 2026, el esquema
concreto es:

| Fold | Entrena con | Valida sobre | Notas |
|---|---|---|---|
| 1 | 2018–2022 | 2023 | 2023 introduce pitch clock, shift ban y bases más grandes: cambio de régimen real; útil para ver cómo degrada el modelo ante reglas nuevas. |
| 2 | 2018–2023 | 2024 | |
| 3 | 2018–2024 | 2025 | Último fold de validación "limpio" completo. |
| Producción | 2018–2025 | opera 2026 | Reentrenos mensuales dentro de temporada (§1.2). 2026 no se usa para seleccionar modelo: es operación, no validación. |

Dos asteriscos sobre los datos:

- **2020 es una temporada de 60 juegos** (COVID), con reglas transitorias (universal DH
  adelantado, extra innings con corredor). Se incluye en train con peso reducido o se
  excluye; la decisión se registra en la config del experimento y se mantiene fija en
  todos los folds.
- La selección de hiperparámetros y de features se hace **solo** con los folds 1–2; el
  fold 3 se toca una única vez, al final, como estimación honesta de generalización.
  Si se itera contra el fold 3, deja de ser validación y pasa a ser train con pasos
  extra.

```
Temporada:   2018   2019   2020*  2021   2022   2023   2024   2025   2026
             ├──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┼──────┤
Fold 1       [██████ train ██████████████████████][valid]
Fold 2       [██████ train █████████████████████████████][valid]
Fold 3       [██████ train ████████████████████████████████████][valid]
Producción   [██████ train ███████████████████████████████████████████][opera→
                                                                * 2020: 60 juegos
```

### 1.2 Reentrenos mensuales dentro de temporada

Dentro de una temporada (tanto en los folds de validación como en producción) el modelo
no se congela en abril: se reentrena al inicio de cada mes con todos los datos hasta el
fin del mes anterior. El backtest debe simular exactamente este ciclo — si producción
reentrena mensualmente, el backtest que no lo hace está midiendo otro sistema.

```
Temporada 2024 (fold 2, valid):

        abr        may        jun        jul        ago        sep/oct
        ├──────────┼──────────┼──────────┼──────────┼──────────┼──────────┤
M_abr   train: ≤2023                     → predice abril
M_may   train: ≤2023 + abr 2024          → predice mayo
M_jun   train: ≤2023 + abr–may 2024      → predice junio
M_jul   train: ≤2023 + abr–jun 2024      → predice julio
  ...   (y así hasta el cierre de temporada)
```

Abril merece mención honesta: el modelo entra con cero datos de la temporada en curso,
los rosters cambiaron y las features rolling arrancan frías. Es esperable que las
métricas de abril sean peores; se reportan por separado (breakdown mensual, §4.9) y, si
la degradación es sistemática, la respuesta correcta es subir el umbral de edge en
abril o no publicar picks ese mes — no maquillar el promedio anual.

### 1.3 Reglas duras del backtest

1. **Features as-of**: toda feature se calcula con la información disponible antes del
   primer pitch del juego evaluado, con timestamps explícitos (`feature_snapshots` en
   `03-modelo-de-datos.md`; reglas en `04-features-y-modelos.md` §anti-leakage).
2. **El calibrador solo ve el pasado**: para evaluar el mes M, el calibrador (Platt/
   isotonic según `04-features-y-modelos.md` §3) se ajusta exclusivamente con
   predicciones out-of-fold anteriores a M. Ajustar el calibrador con datos de la
   ventana evaluada infla la calibración medida y es la forma más silenciosa de
   leakage.
3. **Nunca shuffle, nunca k-fold aleatorio**, ni siquiera "solo para hiperparámetros".
   Los juegos de una misma semana comparten información (mismos pitchers, misma racha,
   mismas lesiones); el shuffle la filtra del futuro al pasado.
4. **Un experimento = una config versionada**: seed, rango de fechas, features, política
   de odds (§2), umbrales de publicación. Dos backtests solo son comparables si difieren
   en exactamente una cosa.
5. **Se evalúan dos capas separadas**: (i) la calidad probabilística del modelo (log
   loss, Brier, ECE sobre *todos* los juegos) y (ii) la política de apuesta (EV, yield,
   drawdown sobre los picks que pasan umbral). Un modelo puede estar bien calibrado y
   aun así no generar picks con edge; son diagnósticos distintos.

---

## 2. El problema de las odds en backtest

Aquí es donde muere la mayoría de los backtests "rentables" publicados en GitHub, y hay
que decirlo sin anestesia.

### 2.1 Por qué backtestear contra closing lines sobreestima

El closing line es la línea **más eficiente** del mercado: incorpora todo el flujo de
información y de dinero hasta segundos antes del primer pitch (lineups confirmados,
clima, movimientos de dinero sharp). Dos consecuencias:

1. **No era apostable cuando habrías apostado.** El sistema real publica picks horas
   antes del juego (scan diario, decisión #10 en `00-decisiones.md`). El precio que
   habrías tomado es el de ese momento, no el de cierre. Si el mercado se movió en la
   dirección del pick — que es exactamente lo que pasa cuando el pick es bueno — el
   closing es *peor* precio que el apostable, y el backtest que "apuesta al closing"
   se está regalando el CLV que en la vida real sería su ganancia marginal.
2. Simétricamente, un backtest contra closing **castiga de menos** los picks malos: si
   la línea se movió en contra, el closing es mejor precio del que habrías conseguido.

El efecto neto no es simétrico ni pequeño: la literatura y la experiencia de mercado
coinciden en que el closing de un book sharp (Pinnacle) es un predictor muy difícil de
batir, y un backtest que apuesta contra él está midiendo un juego que no existe. Esta
es la razón del principio no negociable en `00-decisiones.md`: *el backtest usa odds
realistas, nunca el closing como si fuera apostable*.

### 2.2 Opciones disponibles

| Opción | Qué es | Costo | Veredicto |
|---|---|---|---|
| **(a) Odds históricas con timestamps intradía** | Snapshots históricos de The Odds API (disponibles desde junio 2020, intervalos de 10 min) u otro proveedor comercial | Caro: ~10× créditos vs live; reconstruir mercados F5 (additional markets, por evento) ≈ 150× vs capturarlos en vivo (ver `02-fuentes-de-datos.md` §historical). Rompe el presupuesto de $50/mes si se quiere profundidad. Y no cubre 2018–2019 en absoluto. | Deseable pero fuera de presupuesto para el MVP. Comprar ventanas puntuales (p. ej. 30 días) solo si un experimento concreto lo justifica. |
| **(b) Closing lines con haircut explícito** | Backtestear contra closing no-vig pero descontando un haircut documentado del edge medido | Barato (los archivos de closing son accesibles; el closing ML histórico vía The Odds API es la parte barata del historical) | Aceptable **solo** como aproximación provisional, etiquetada como tal en todo reporte. Nunca como evidencia de rentabilidad. |
| **(c) Snapshots propios acumulados desde hoy** | El cron de ingesta ya captura 9 snapshots/día de ML y el cierre por ola (plan de créditos en `02-fuentes-de-datos.md` §2), guardados en `odds_snapshots` con timestamp | Ya pagado dentro del plan de ~$30/mes | **Estrategia principal.** En 2–3 meses hay un dataset de odds intradía propio, exacto al book y al minuto, imposible de comprar a ese precio. |

### 2.3 Decisión

**Principal: (c).** Desde el día 1 el pipeline acumula `odds_snapshots` propios con
timestamps. Todo backtest de la política de apuesta que aspire a ser citado como
evidencia usa exclusivamente estos snapshots: el precio "apostable" en el backtest es
el último snapshot anterior al momento en que el cron habría publicado el pick. El
costo de esta decisión es tiempo: el dataset serio empieza hoy y crece hacia adelante.
No hay atajo honesto.

**Provisional: (b), con etiqueta.** Mientras (c) acumula muestra, se permite un backtest
contra closing no-vig con haircut, bajo estas reglas:

- El haircut se expresa en puntos de probabilidad y se resta del edge medido contra el
  closing (mecánica: con haircut configurado de 1 punto, un edge aparente de 2.5 pts se
  trata como 1.5 pts a efectos de umbral y de EV).
- El valor del haircut es un parámetro de config (`backtest_odds_haircut_prob_pts`),
  elegido deliberadamente conservador, y **se estima empíricamente en cuanto haya datos
  propios**: con los pares (snapshot a hora de publicación, closing) de `odds_snapshots`
  se mide la distribución real del movimiento publicación→cierre por mercado, y ese
  dato reemplaza al placeholder. Hasta entonces no se cita ningún número como si fuera
  un hecho del mercado.
- Todo resultado producido bajo (b) lleva la etiqueta fija:
  **"SIMULACIÓN CONTRA CLOSING CON HAIRCUT — cota optimista, no evidencia de
  rentabilidad"**, en el reporte, en la tabla de resultados y en cualquier gráfico.
  Sin excepciones, incluido el uso interno.

Nota de alcance: la capa (i) del backtest — calidad probabilística del modelo (log
loss, Brier, ECE) — **no necesita odds apostables**, solo resultados de juegos, así que
puede correr sobre 2018–2025 completo. La restricción de odds aplica a la capa (ii),
la simulación de apuestas. Separarlas permite validar el modelo con toda la historia
sin fingir que teníamos precios que no teníamos.

---

## 3. Protocolo de paper trading

El paper trading es la primera prueba contra la realidad: precios reales, timestamps
reales, sin dinero. Dura **4–8 semanas mínimo** y sus reglas son de cumplimiento
literal — cada una existe porque su violación es una forma conocida de auto-engaño.

1. **Todo pick se registra ANTES del primer pitch**, con: timestamp de publicación,
   book accesible al usuario objetivo (Bet365/books MX, no un precio teórico), precio
   decimal realmente apostable en ese book en ese momento (FK al `odds_snapshot`
   exacto, ver `03-modelo-de-datos.md`), versión de modelo y calibrador, snapshot de
   features, edge y EV calculados, stake según el plan Kelly fraccional de
   `05-motor-ev-y-bankroll.md`. Un pick sin registro previo al juego no existe.
2. **Sin borrado ni edición retroactiva, jamás.** Las tablas `picks`, `pick_results` y
   `clv_records` son append-only con triggers que bloquean `UPDATE`/`DELETE`
   (`03-modelo-de-datos.md` §inmutabilidad). Un error se corrige con una fila de
   corrección que referencia la original, nunca reescribiendo la historia.
3. **Se registra también todo lo evaluado y NO apostado.** El cron escribe en
   `daily_scans` cada juego/mercado evaluado, pasara o no el umbral. Esto permite medir
   el **sesgo de selección**: qué fracción del slate genera picks, si esa fracción es
   estable, y si las métricas de los picks difieren de las del universo evaluado por
   razones explicables (edge) o sospechosas (cherry-picking accidental por bugs de
   filtrado). Un sistema que solo guarda sus picks no puede auditarse.
4. **Cada pick se liquida contra el resultado oficial** y contra el closing de Pinnacle
   sin vig (CLV), automáticamente, sin intervención manual.
5. **Mínimo 300 picks antes de leer métricas con alguna confianza.** Y hay que decir la
   tensión en voz alta: con un slate de ~15 juegos/día × 2 mercados (ML + F5) y una
   tasa de publicación realista tras el umbral de edge ≥ 2%, el volumen esperado es de
   pocos picks al día. Es probable que 300 picks requieran las 8 semanas completas o
   más. La respuesta correcta es **extender el periodo**, nunca aflojar el umbral para
   fabricar volumen — eso invalidaría exactamente lo que se intenta medir. El criterio
   de fin es doble: `max(8 semanas, 300 picks)`.
6. Durante el paper trading no se cambia el modelo ni los umbrales. Si se descubre un
   bug grave, se corrige, se anota en el log del experimento y **el reloj se reinicia**.

---

## 4. Métricas: definición exacta y para qué sirve cada una

Convención: `p` = probabilidad del modelo calibrado, `y ∈ {0,1}` = resultado,
`N` = número de observaciones. Las métricas 4.1–4.4 se calculan sobre **todas** las
predicciones (universo evaluado, esté o no apostado); las 4.5–4.10 sobre los picks.

### 4.1 Accuracy — y por qué NO es la métrica

```
accuracy = (1/N) · Σ 1[ (p > 0.5) == y ]
```

Mide qué fracción de veces el lado más probable según el modelo ganó. Sirve como
sanity check grueso (un modelo de MLB ML con accuracy de 0.48 tiene un bug) y para
nada más. No ve el precio, no ve la magnitud de la probabilidad, y optimizar por ella
selecciona modelos sobreconfiados. El resultado central de arXiv:2303.06021 (citado
con detalle en `04-features-y-modelos.md` §3) es exactamente este: seleccionar modelos
por calibración en lugar de accuracy cambió el signo del resultado económico en su
experimento. En este proyecto la accuracy se reporta al fondo de la tabla, nunca como
titular.

### 4.2 Brier score

```
brier = (1/N) · Σ (p − y)²        # menor es mejor; 0.25 = predictor p=0.5 constante
```

Error cuadrático de la probabilidad. Castiga a la vez mala discriminación y mala
calibración. Sirve para comparar versiones del modelo entre sí y contra el baseline de
mercado (Brier de `p_fair` del closing no-vig).

### 4.3 Log loss — la métrica de entrenamiento y la vara contra el mercado

```
log_loss = −(1/N) · Σ [ y·ln(p) + (1−y)·ln(1−p) ]     # menor es mejor; ln(2) ≈ 0.693 = coin flip
```

Castiga brutalmente la sobreconfianza (una predicción de 0.99 que falla domina la
suma). Es la loss de entrenamiento y el comparador principal contra el **market
prior**: el log loss de usar `p_fair` (closing no-vig de Pinnacle) como predicción.
Si el modelo no tiene log loss menor que el mercado, no hay razón para creer que ve
algo que el mercado no ve — y esa comparación es un criterio go/no-go directo (§5).

### 4.4 Calibration curve y ECE

Curva: se agrupan las predicciones en bins (deciles de `p`), y se grafica frecuencia
observada vs probabilidad media predicha por bin. La diagonal es calibración perfecta.

```
ECE = Σ_b (n_b / N) · | freq_observada(b) − p_media_predicha(b) |
```

El ECE resume la curva en un número. Umbral operativo del MVP (de
`00-decisiones.md`/brief): **ECE ≤ 0.03 en ventana rolling de 60 días** por mercado;
si se supera, el modelo deja de publicar picks hasta re-calibrar
(`04-features-y-modelos.md` §3). Para qué sirve: todo el motor de EV multiplica
`p_model` por payouts; si `p_model` está inflada 3 puntos, cada edge reportado está
inflado 3 puntos y el Kelly sobre-apuesta sistemáticamente.

### 4.5 Winrate — con el ejemplo de por qué engaña

```
winrate = picks ganados / picks resueltos
```

Sin el precio al lado, el winrate no dice nada sobre dinero. Ejemplo con la aritmética
completa (odds americanas → decimales: +150 → 2.50, −200 → 1.50; breakeven =
1/odds_decimal):

| Cartera | Odds | Breakeven | Winrate real | 100 picks de 1 unidad | Yield |
|---|---|---|---|---|---|
| Underdogs +150 | 2.50 | 40.0% | **45%** | 45×(+1.50) + 55×(−1) = +67.5 − 55 = **+12.5 u** | **+12.5%** |
| Favoritos −200 | 1.50 | 66.7% | **63%** | 63×(+0.50) + 37×(−1) = +31.5 − 37 = **−5.5 u** | **−5.5%** |

El que "acierta" 45% gana dinero; el que acierta 63% lo pierde. Lo único que importa es
winrate **relativo al breakeven implícito del precio tomado**. Por eso el winrate solo
se reporta junto al breakeven promedio de la cartera y por rango de odds (§4.10), nunca
solo.

### 4.6 ROI vs yield — distinción explícita

Definiciones canónicas del proyecto (no mezclarlas; ver `05-motor-ev-y-bankroll.md`):

```
yield = ganancia neta / total apostado           # eficiencia por unidad de riesgo
ROI   = ganancia neta / bankroll inicial del periodo   # crecimiento del capital
```

Son cosas distintas: el yield mide qué tan bueno es cada pick en promedio; el ROI
depende además del volumen de picks y del stake sizing (un yield de +3% con 400 picks
al 1% del bankroll produce un ROI muy distinto que con 40 picks). Para comparar
modelos se usa yield; para responder "¿cuánto creció la cuenta?" se usa ROI. Y la
advertencia estadística de §5 aplica a ambos: en muestras de cientos de picks son
mayormente ruido.

### 4.7 Units

```
units = ganancia neta expresada en unidades de stake base (1 u = stake de referencia)
```

Con stakes Kelly variables, se reportan dos series: units reales (con el stake
efectivamente asignado) y units a stake plano (1 u por pick), porque la serie plana
aísla la calidad de los picks de la política de sizing.

### 4.8 CLV y beat-rate — la métrica de proceso

Definición (consistente con `clv_records` en `03-modelo-de-datos.md` y con
`05-motor-ev-y-bankroll.md`):

```
clv_prob_pts = closing_p_fair − 1/price_taken_decimal   # closing Pinnacle sin vig
beat_rate    = % de picks con clv_prob_pts > 0
```

Para qué sirve: es la señal más rápida y menos ruidosa de que el sistema ve algo antes
que el mercado. Cada pick genera una observación de CLV **independientemente del
resultado del juego**, así que converge mucho antes que el yield. El promedio de
`clv_prob_pts` (variable continua) tiene más poder estadístico que el beat-rate
(binaria); se monitorean ambos. CLV positivo sostenido no garantiza ganancia (el vig
del book del usuario puede comérselo), pero CLV negativo persistente sí garantiza que
no hay edge que rescatar.

### 4.9 Average edge — y su contraste con lo realizado

```
avg_edge = media(p_model − p_fair) al momento de publicación, sobre picks publicados
```

Sirve como test de coherencia interna: si el edge promedio reclamado al publicar es
+3 pts pero el CLV promedio realizado es ~0, el modelo está sistemáticamente inflando
su ventaja (mala calibración residual, o el mercado corrige información que el modelo
cree exclusiva) y los umbrales de publicación deben subir.

### 4.10 Max drawdown y breakdowns

```
equity_t = Σ_{i≤t} profit_units_i
MDD      = max_t ( max_{s≤t} equity_s − equity_t )    # en unidades; también en % bankroll
```

El MDD calibra expectativas de dolor: con yields pequeños y varianza de betting, rachas
de −15 a −30 unidades a stake plano son estadísticamente normales incluso para
estrategias con edge real. Quien no lo ha visto en el paper trading lo descubrirá con
dinero, que es peor.

**Breakdowns obligatorios** de todas las métricas 4.5–4.9: por mercado (ML vs F5), por
book, por rango de odds (p. ej. bandas de probabilidad implícita), por mes. Razón: un
agregado sano puede esconder un segmento roto (p. ej. todo el CLV positivo concentrado
en favoritos y F5 sangrando), y las decisiones de §5 se toman por mercado, no solo en
agregado. El SQL de referencia está en `03-modelo-de-datos.md` §consultas.

---

## 5. Criterios go/no-go tras paper trading

### 5.1 Primero, la incertidumbre — sin maquillaje

Con 300–500 picks, los intervalos de confianza son anchos. Cálculo explícito para el
beat-rate, con la aproximación normal del intervalo binomial
(`p̂ ± 1.96·√(p̂(1−p̂)/n)`), para un beat-rate observado de 53%:

```
n = 400:  SE = √(0.53·0.47/400) = 0.0250  →  IC 95% = 53% ± 4.9  →  [48.1%, 57.9%]
n = 300:  ± 5.6  →  [47.4%, 58.6%]
n = 500:  ± 4.4  →  [48.6%, 57.4%]
```

Léase bien: **un beat-rate de 53% con 400 picks es estadísticamente indistinguible de
una moneda al aire** (el IC incluye 50%). Para separar 53% de 50% con significancia al
95% se necesita del orden de `n ≈ 1.96²·0.53·0.47/0.03² ≈ 1,063` picks. El promedio de
`clv_prob_pts` converge algo más rápido por ser continuo, pero no cambia el orden de
magnitud del problema.

Y el ROI/yield es aún peor: a odds típicas de ML (~1.9), la desviación estándar del
resultado por pick a stake plano es ≈ 0.95 u, así que el error estándar del yield con
n = 400 es ≈ 0.95/√400 ≈ 4.8 puntos porcentuales — un yield observado de +3% tiene un
IC 95% de aproximadamente **[−6%, +12%]**. Por eso: **el ROI y el yield en muestras de
cientos de picks son mayormente ruido y no participan en la decisión go/no-go.** Se
registran, se grafican, y no se les cree.

### 5.2 La decisión — combinación de señales, no un test único

Precisamente porque ninguna métrica individual alcanza significancia con esta muestra,
los criterios combinan señales independientes (proceso + calidad probabilística +
calibración), todas por mercado (ML y F5 se deciden por separado):

**SEGUIR (pasar a dinero real con stakes mínimos, Kelly/8 con cap, ver
`05-motor-ev-y-bankroll.md`) si se cumplen las TRES:**

1. CLV beat-rate ≥ ~53% **sostenido** (sin tendencia a la baja en las últimas 4
   semanas) y `avg(clv_prob_pts) > 0`;
2. log loss del modelo < log loss del market prior (closing no-vig) en el mismo
   periodo;
3. ECE ≤ 0.03 en la ventana rolling de 60 días.

**MATAR / REDISEÑAR si cualquiera:**

- CLV beat-rate < 50% persistente tras 500+ picks (a esa muestra, un beat-rate bajo ya
  es informativo aunque el IC sea ancho: la hipótesis "vemos algo antes que el
  mercado" no tiene soporte);
- log loss peor que el market prior de forma sostenida — el modelo aporta información
  negativa respecto a mirar la pantalla de Pinnacle, y ningún motor de EV arregla eso.

**ZONA GRIS (todo lo demás): extender la muestra, no lanzar.** Beat-rate en 50–53%,
señales mezcladas entre ML y F5, ECE bueno pero CLV plano: nada de eso justifica
dinero real ni justifica tirar el trabajo. Se extiende el paper trading en bloques de
4 semanas y se re-evalúa. La tentación de "lanzar porque llevamos 3 meses" es
exactamente el sesgo que este protocolo existe para bloquear.

Regla final: pasar a dinero real no termina la evaluación — los mismos criterios
siguen corriendo en producción sobre ventanas rolling, y un mercado que cae a zona de
MATAR deja de publicar picks (kill-switch de calibración en
`04-features-y-modelos.md` §3).

---

## 6. Reporte a usuarios del SaaS

Reglas de publicación de métricas, no negociables, alineadas con el principio de
trazabilidad de `00-decisiones.md`:

1. **Toda métrica pública sale exclusivamente de picks registrados en `picks` con
   timestamp previo al inicio del juego y FK a su `odds_snapshot`** — es decir,
   verificables contra la cadena de auditoría de `03-modelo-de-datos.md`. Si no está
   en el registro inmutable, no existe para el marketing.
2. **Nunca se publican métricas de backtest como si fueran resultados reales.** Si un
   resultado de simulación se muestra (p. ej. en documentación técnica), lleva la
   etiqueta de §2.3, sus supuestos (política de odds, haircut, periodo) y no aparece
   junto a métricas reales sin separación tipográfica clara.
3. Toda métrica publicada muestra su **n** (número de picks) y, para tasas, su
   intervalo de confianza. Publicar "+8% yield" sin decir que es sobre 90 picks es
   técnicamente cierto y funcionalmente una mentira.
4. **El historial completo es visible**: picks ganados y perdidos, drawdowns
   incluidos, y el ratio evaluados/publicados desde `daily_scans`, para que un usuario
   pueda verificar que no hay borrado selectivo. El producto vende claridad, control
   de riesgo y trazabilidad — no promesas de winrate, y el dashboard debe reflejarlo
   hasta en qué métricas pone primero (CLV y calibración antes que winrate).
5. Las métricas de mercados en fase de validación (p. ej. NRFI en fase 2) se muestran
   marcadas como "en paper trading", separadas de los mercados en producción.
