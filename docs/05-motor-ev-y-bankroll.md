# 05 — Motor de EV y gestión de bankroll

Este documento define la matemática del motor de decisión de EDGE: conversión de odds,
remoción del vig, cálculo de edge y EV, stake sizing con Kelly fraccional, la política
de NO apostar y la medición de CLV. Todas las fórmulas de aquí son las definiciones
canónicas del proyecto; el código en `apps/api/` debe implementarlas tal cual y los
tests deben reproducir los ejemplos numéricos de este documento dígito por dígito.

Las probabilidades del modelo (`p_model`) vienen del pipeline de ML calibrado
(ver `04-features-y-modelos.md`). Este motor no genera probabilidades: las consume.
El LLM no participa en ningún cálculo de este documento.

Convención de redondeo en los ejemplos: toda la aritmética se muestra a 4 decimales.
En producción se calcula con precisión completa y se redondea solo al presentar.

## 1. De odds a probabilidad

### Odds decimales y americanas

Las odds decimales expresan el pago total por unidad apostada (stake incluido).
Las americanas usan dos escalas: negativas (cuánto hay que arriesgar para ganar 100)
y positivas (cuánto se gana arriesgando 100). Conversión:

```text
Americana negativa (A < 0):  odds_decimal = 1 + 100 / |A|
Americana positiva (A > 0):  odds_decimal = 1 + A / 100

Ejemplos:
-150  →  1 + 100/150 = 1.6667
+130  →  1 + 130/100 = 2.3000
```

Internamente EDGE trabaja siempre en decimales; las americanas son solo formato de
entrada/salida para el usuario.

### Probabilidad implícita

La probabilidad implícita es la probabilidad a la cual una odd sería una apuesta de
EV cero. Definición canónica:

```text
p_imp = 1 / odds_decimal
```

Equivalente directo desde americanas: `|A| / (|A| + 100)` si A < 0, y
`100 / (A + 100)` si A > 0.

### Qué es el vig

Si el book cotizara probabilidades justas, las probabilidades implícitas de los dos
lados de un mercado sumarían exactamente 1. No lo hacen: suman más de 1, y ese exceso
es el margen del book (vig, vigorish, juice). La suma se llama **overround**.

Ejemplo clásico, un mercado -110 / -110:

```text
p_imp cada lado = 110 / 210 = 0.5238
suma (overround) = 0.5238 + 0.5238 = 1.0476
vig = overround − 1 = 0.0476  →  4.76 puntos de probabilidad
```

El book cobra ese 4.76% estructuralmente: apostar a ciegas contra el vig es EV
negativo por construcción. Todo el motor de EDGE existe para responder una sola
pregunta: ¿hay casos donde `p_model` supera a la probabilidad justa del mercado por
más que los umbrales, después de quitar el vig?

## 2. Quitar el vig: método multiplicativo

El método default del MVP es el **multiplicativo (proporcional)**: normalizar las
probabilidades implícitas para que sumen 1. Para un mercado de 2 lados:

```text
p_fair_i = p_imp_i / (p_imp_1 + p_imp_2)
```

### Ejemplo completo: mercado ML -150 / +130

Paso 1 — convertir a decimales:

```text
favorito  -150  →  1 + 100/150 = 1.6667
underdog  +130  →  1 + 130/100 = 2.3000
```

Paso 2 — probabilidad implícita de cada lado:

```text
p_imp_fav = 1 / 1.6667 = 150/250 = 0.6000
p_imp_dog = 1 / 2.3000 = 100/230 = 0.4348
```

Paso 3 — suma (overround):

```text
overround = 0.6000 + 0.4348 = 1.0348
vig = 0.0348  →  3.48 puntos de probabilidad
```

Paso 4 — normalización:

```text
p_fair_fav = 0.6000 / 1.0348 = 0.5798
p_fair_dog = 0.4348 / 1.0348 = 0.4202
verificación: 0.5798 + 0.4202 = 1.0000
```

Estas `p_fair` son la línea justa del mercado según ese book. Para la línea de
referencia del proyecto se usa Pinnacle (decisión #6 de `00-decisiones.md`): book de
límites altos y vig bajo cuya línea es el mejor estimador de consenso disponible
dentro del presupuesto (ver `02-fuentes-de-datos.md` para cómo se obtiene vía
The Odds API).

### Limitaciones y alternativas futuras

El método multiplicativo reparte el vig proporcionalmente entre ambos lados, pero los
books no lo cargan de forma proporcional: sistemáticamente inflan más el precio de los
underdogs largos (favorite-longshot bias), así que la normalización proporcional tiende
a sobreestimar la probabilidad justa del longshot y subestimar la del favorito. Para
moneyline de MLB, donde los precios rara vez son extremos, el error es pequeño y el
método multiplicativo es un default razonable y transparente. Existen métodos que
modelan el sesgo explícitamente — el estimador de Shin y el power method (normalizar
`p_imp^k` con `k` tal que sumen 1) — y quedan como mejora futura post-MVP; la interfaz
del módulo de no-vig debe recibir el método como parámetro para poder cambiarlo sin
tocar el resto del motor.

## 3. Edge y EV

Definiciones canónicas:

```text
edge = p_model − p_fair                            (en puntos de probabilidad)
EV   = p_model × (odds_decimal − 1) − (1 − p_model)   (por unidad apostada)
```

Punto importante: el edge se mide contra la línea justa de referencia (Pinnacle sin
vig), pero el EV se calcula con las odds reales del book donde el usuario puede
apostar, con su vig incluido. Puede existir edge contra la línea justa y aun así EV
insuficiente en el book concreto porque su vig se lo come.

### Ejemplo encadenado

Mismo mercado -150 / +130 del ejemplo anterior. Supongamos que el modelo calibrado
asigna al underdog `p_model = 0.4500` (y por tanto 0.5500 al favorito).

Lado underdog, tomado a +130 (decimal 2.3000):

```text
edge = 0.4500 − 0.4202 = 0.0298   →  +2.98 puntos de probabilidad
EV   = 0.4500 × (2.3000 − 1) − (1 − 0.4500)
     = 0.4500 × 1.3000 − 0.5500
     = 0.5850 − 0.5500
     = 0.0350   →  +3.50% por unidad apostada
```

Lado favorito, a -150 (decimal 1.6667):

```text
edge = 0.5500 − 0.5798 = −0.0298   (negativo: sin edge)
EV   = 0.5500 × 0.6667 − 0.4500 = 0.3667 − 0.4500 = −0.0833
```

En un mercado de 2 lados los edges contra la misma línea justa son simétricos: si un
lado tiene edge positivo, el otro lo tiene negativo. El underdog pasa los umbrales
default del MVP (`edge ≥ 2%`, `EV ≥ +2%`); el favorito ni se considera.

## 4. Kelly: cuánto apostar

### Derivación breve y fórmula canónica

El criterio de Kelly elige la fracción del bankroll `f` que maximiza el crecimiento
esperado del logaritmo del bankroll, `E[log(riqueza)]`, en apuestas repetidas. Con
probabilidad de ganar `p`, de perder `q = 1 − p` y ganancia neta `b` por unidad
(`b = odds_decimal − 1`), maximizar `p·log(1 + f·b) + q·log(1 − f)` da:

```text
f* = (p × (b + 1) − 1) / b        con  b = odds_decimal − 1
```

`f*` solo es positivo si hay edge (si `p > 1/odds_decimal`); con EV ≤ 0, Kelly dice
no apostar. Nótese que el numerador `p × (b + 1) − 1` es exactamente el EV por unidad,
es decir `f* = EV / b`: Kelly escala el stake con el EV y lo penaliza por la varianza
del precio (a mayor cuota, menor fracción para el mismo EV).

### Ejemplo numérico (encadenado)

Underdog a +130, `p = 0.4500`, `b = 1.3000`:

```text
f* = (0.4500 × 2.3000 − 1) / 1.3000
   = (1.0350 − 1.0000) / 1.3000
   = 0.0350 / 1.3000
   = 0.0269   →  2.69% del bankroll
```

### Por qué Kelly completo es peligroso en la práctica

Kelly completo es óptimo solo si `p` es exacta. Pero `p` es una estimación de un
modelo con error, y el error no es simétrico en sus consecuencias: los picks donde el
modelo sobreestima `p` aparentan más edge, pasan los umbrales con más frecuencia y
reciben stakes mayores. El resultado es **overbetting sistemático**: en promedio se
apuesta más que el Kelly verdadero precisamente en los picks donde el modelo está más
equivocado. Apostar por encima del Kelly verdadero no solo aumenta la varianza:
reduce el crecimiento esperado, y pasado el doble del Kelly verdadero el crecimiento
esperado se vuelve negativo aun con edge real. A eso se suman drawdowns brutales:
incluso con `p` exacta, Kelly completo produce caídas de bankroll que ningún usuario
tolera en la práctica (ver tabla), y con `p` estimada son peores.

### Kelly fraccional y stake final

Por eso el motor calcula Kelly completo pero el stake aplica una fracción configurable
por usuario, con default **1/8**, y un cap absoluto por pick de **1–2% del bankroll**
(decisión #8 de `00-decisiones.md`). Definición canónica del stake:

```text
stake = bankroll × min(f* × fracción_usuario, cap_usuario)
```

Con el ejemplo anterior y bankroll de $10,000, fracción 1/8 y cap 2%:

```text
f* × 1/8 = 0.0269 / 8 = 0.0034
min(0.0034, 0.0200) = 0.0034
stake = 10,000 × 0.0034 = $34   (0.34% del bankroll)
```

Tabla ilustrativa — mismo pick (`f* = 0.0269`) con distintas fracciones. El drawdown
es cualitativo: no publicamos números de drawdown "esperado" sin simularlos sobre
nuestra propia distribución de picks (eso pertenece a `06-backtesting-y-metricas.md`).

| Fracción | % del bankroll en este pick | Drawdown esperado (cualitativo) |
|---|---|---|
| Kelly 1 | 2.69% | Brutal: caídas de más de la mitad del bankroll son esperables en rachas normales; insostenible con `p` estimada. |
| Kelly 1/2 | 1.35% | Severo: crecimiento teórico ~75% del de Kelly 1 con mucha menos varianza, pero drawdowns aún duros. |
| Kelly 1/4 | 0.67% | Moderado: sacrifica crecimiento a cambio de una curva de bankroll tolerable. |
| Kelly 1/8 (default) | 0.34% | Suave: prioriza supervivencia y robustez al error de estimación de `p`; el costo es crecimiento lento. |

La fracción óptima depende de cuánto error real tenga `p_model`, cosa que solo el
paper trading y el CLV acumulado pueden acotar. Empezar en 1/8 y subir con evidencia
es reversible; empezar en Kelly 1 y quebrar el bankroll no.

## 5. Política de NO apostar

Tan importante como decidir cuándo apostar es decidir cuándo no. El default correcto
de este sistema es no publicar pick: la mayoría de los mercados están bien preciados
la mayoría del tiempo. El motor descarta un candidato si se cumple **cualquiera** de
estas condiciones:

1. **`edge < umbral`** (default 2 puntos de probabilidad). Edge pequeño es
   indistinguible del ruido de estimación del modelo.
2. **`EV < umbral`** (default +2% por unidad) calculado con las odds reales del book
   del usuario. Filtra los casos con edge contra la línea justa pero precio apostable malo.
3. **Calibración degradada: `ECE > 0.03` en la ventana rolling de 60 días** del
   mercado correspondiente (ver `04-features-y-modelos.md`). Si el modelo está
   descalibrado, sus `p_model` no son interpretables como probabilidades y todo lo
   anterior (edge, EV, Kelly) queda invalidado. El sistema entra en modo
   solo-monitoreo hasta recuperar calibración.
4. **Mercado sin ambos lados cotizados** en la fuente de referencia. Sin los dos
   lados no hay overround y no se puede quitar el vig con confianza; no se estima
   `p_fair` con un solo precio.
5. **Línea movida más de X entre el análisis y la publicación** (X configurable;
   valor inicial propuesto: 1 punto de probabilidad implícita en el precio apostable).
   Si el precio ya no es el que se analizó, el EV calculado no describe la apuesta
   disponible: se re-analiza con el precio nuevo o se descarta. Además, un movimiento
   fuerte en contra suele significar información nueva (lineup, lesión) que el
   snapshot de features no capturó.
6. **Límites de stake del book por debajo del stake calculado**. Si el book no acepta
   el stake que dicta la fórmula, el pick se publica con el stake truncado al límite
   y anotado, o se descarta si el límite lo vuelve irrelevante. Límites bajos en un
   mercado concreto son además una señal del book de que ese mercado es débil.

Cada descarte se registra con su razón (ver `03-modelo-de-datos.md`): la política de
no apostar también se audita y también se backtestea.

## 6. CLV: closing line value

### Definición

El CLV compara el precio que se tomó al publicar el pick contra la **línea de cierre
de Pinnacle sin vig** (no-vig por el método de la sección 2). Se reporta en puntos de
probabilidad y en beat-rate.

```text
CLV (puntos de probabilidad) = p_fair_close − p_imp_tomada

p_imp_tomada = 1 / odds_decimal_tomadas   (el precio real que se tomó, con su vig)
p_fair_close = probabilidad no-vig del mismo lado en el cierre de Pinnacle
beat-rate    = % de picks con CLV > 0
```

CLV positivo significa que el precio tomado era mejor que el consenso final del
mercado: se pagó "menos probabilidad" de la que el mercado terminó asignando.

### Por qué es el mejor proxy temprano de edge real

La línea de cierre agrega toda la información que entró al mercado hasta el inicio
del juego — lineups confirmados, clima, dinero informado — y en books de límites
altos como Pinnacle es el estimador más eficiente disponible del resultado. Batirla
sistemáticamente indica que el modelo identifica precios malos antes de que el
mercado los corrija, y eso correlaciona con EV positivo a largo plazo. La ventaja
práctica es estadística: el resultado de un pick es una moneda al aire de altísima
varianza (se necesitan cientos de picks para separar señal de ruido en el ROI),
mientras que el CLV da lectura desde el primer día y con decenas de picks. Con
matices: no es garantía. Un modelo puede batir el cierre y aun así perder si el
cierre de ese mercado concreto no es eficiente, si el vig pagado supera el edge
capturado, o si el CLV positivo se concentra en picks de EV marginal. CLV es la
métrica de proceso; ROI y yield son las de resultado (definiciones en
`06-backtesting-y-metricas.md`). El MVP exige ambas.

### Ejemplo numérico (encadenado)

Se publicó el pick del underdog a +130 (`p_imp_tomada = 0.4348`). Pinnacle cierra el
mercado en -128 / +118.

```text
Cierre a decimales:
  -128  →  1 + 100/128 = 1.7813
  +118  →  1 + 118/100 = 2.1800

p_imp del cierre:
  favorito: 128/228 = 0.5614
  underdog: 100/218 = 0.4587
  overround = 0.5614 + 0.4587 = 1.0201

No-vig del cierre (lado tomado, underdog):
  p_fair_close = 0.4587 / 1.0201 = 0.4497

CLV = 0.4497 − 0.4348 = 0.0149   →  +1.49 puntos de probabilidad
```

La línea se movió a favor del pick: el mercado terminó valorando al underdog en
44.97% y el pick lo compró a un precio que implicaba 43.48%. Este pick suma al
beat-rate. Si el promedio de CLV de la cartera es positivo y estable tras unas
decenas de picks de paper trading, es la primera evidencia seria de que el edge del
modelo existe; si es negativo, no hay que esperar al ROI para preocuparse.

## 7. Umbrales default del MVP

Todos los valores son configurables por entorno/usuario según corresponda; estos son
los defaults con los que arranca el MVP. Un pick se publica solo si pasa **todas**
las condiciones de la política de la sección 5.

| Parámetro | Default MVP | Configurable | Notas |
|---|---|---|---|
| Edge mínimo | ≥ 2 puntos de probabilidad | Sí (config) | Contra `p_fair` de Pinnacle (método multiplicativo). |
| EV mínimo | ≥ +2% por unidad | Sí (config) | Con las odds reales del book apostable. |
| ECE máximo del modelo | ≤ 0.03 | Sí (config) | Ventana rolling de 60 días, por mercado. Si se excede: modo solo-monitoreo. |
| Método de no-vig | Multiplicativo | Sí (config) | Shin / power method como mejora futura (sección 2). |
| Fracción de Kelly | 1/8 | Sí (por usuario) | El motor siempre calcula y guarda `f*` completo. |
| Cap de stake por pick | 1–2% del bankroll | Sí (por usuario) | `stake = bankroll × min(f* × fracción, cap)`. |
| Movimiento máximo de línea análisis→publicación | 1 punto de prob. implícita (propuesto) | Sí (config) | Valor inicial a calibrar durante paper trading. |
| Línea de referencia no-vig y CLV | Pinnacle | Sí (config) | Decisión #6 de `00-decisiones.md`. |

Los umbrales de edge/EV/ECE y la disciplina de la sección 5 son los que separan este
sistema de un generador de picks: ningún cambio de umbral se hace en caliente por
resultados de corto plazo, solo con evidencia de backtest y paper trading
(criterios go/no-go en `06-backtesting-y-metricas.md`).
