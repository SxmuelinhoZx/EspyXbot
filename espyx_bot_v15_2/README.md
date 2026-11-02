# EspyX Bot (v15 — estrategia Estructura en zona dorada, interés compuesto por par y nueva interfaz terminal)

## Abrir
- Windows: doble clic en `Abrir_EspyX.bat` (la primera vez instala lo que falta; necesita Python 3.9+ de python.org con "Add Python to PATH").
- Mac: doble clic en `Abrir_EspyX.command`.

## Usar
1. Correo y contraseña, Demo o Real, Conectar (si Exnova pide código SMS, la ventana lo pide). Puedes guardar varias cuentas
   y, si quieres, su contraseña (cifrada por el sistema; ver v13).
2. Pestaña **Operar**: pega las señales (formato de Telegram tal cual), pon monto por operación y capital total.
3. Protecciones (0 = desactivado):
   - Parar tras X pérdidas seguidas (por defecto 3)
   - Parar tras X pérdidas en total
   - Parar al ganar $ (meta del día)
   - El capital total es también el tope de pérdida: si el capital libre no alcanza para otra operación, para.
4. Iniciar. Verás: saldo en vivo, próxima señal con cuenta regresiva, operaciones abiertas con su tiempo restante,
   resultado, ganadas/perdidas/seguidas y capital libre.
5. Pestaña **Historial y registro**: operaciones cerradas y registro. También se guardan en `historial.csv` (abre con Excel).
6. **Martingala por par** (opcional, desactivada por defecto): multiplicador y niveles máximos. Cada par lleva su propio nivel:
   tras perder, el gale va en ESE par (en la siguiente vela o en su próxima señal); tras ganar vuelve al base.
   «Al agotar niveles»: detener el bot o volver al monto base.
   Al iniciar te muestra el costo del peor caso. Nunca usa más que el capital total (si no alcanza, se detiene).
7. **Si Exnova rechaza una operación** («asset is not available…») el bot consulta el estado del par y de su gemelo OTC y
   explica la causa en el registro. Con «Si el par está cerrado, probar con OTC» reintenta con el OTC (no sigue el mercado real).
8. Botón arriba a la derecha: cambiar entre Demo y Real (solo con el bot detenido).

## Reglas
- Las horas de las señales son las de tu computador (hay un «Ajuste de hora» si tu proveedor usa otra zona), pero el segundo
  exacto de entrada se toma del reloj de Exnova. Salta señales que ya pasaron (más de 10 s).
- Si el bot se detiene por una protección, suena un aviso y muestra un resumen.
- Prueba SIEMPRE en Demo antes de usar Real.
- Si Exnova no informa el resultado de una operación a tiempo, queda como «sin confirmar»: revísala en Exnova.

La librería `exnovaapi` (no oficial, CassDS en GitHub, MIT) va incluida con pequeños ajustes (tiempos máximos de espera).

## Pares y tipos de operación (v7)
- El bot opera solo en **BINARIAS** (nunca digital; desde la v13, Blitz solo si lo eliges en Automático). Si Exnova rechaza la operación como «turbo» (1–5 min),
  reintenta como binaria (tipo 1) con la misma expiración y la marca `[binaria]` en el registro.
- Al conectar, lee de Exnova los ids reales de cada par para no depender de los de la librería.
- Botón **🔎 Verificar pares**: muestra, sin operar, si cada par pegado figura ABIERTO o CERRADO en las dos modalidades
  de binarias (1–5 min y 5+ min) y guarda el detalle en `diagnostico_activos.txt`. Si algo falla, envíame ese archivo.

## Nueva interfaz (v8)
- Pantalla de inicio de sesión con tema oscuro, selector Demo/Real y opción «Recordar mi correo» (desde la v13 también puedes guardar la contraseña, cifrada por el sistema y nunca en la carpeta del bot).
- **Panel**: resultado de la sesión, **efectividad (%)** con anillo, capital libre, cuenta regresiva de la próxima señal, cola de señales con su estado (pendiente / abierta / ganó / perdió), operaciones abiertas y **rendimiento por par**.
- Banderas de las divisas de cada par (dibujadas por el programa, no necesitan internet ni archivos extra).
- **Configurar**: pegar señales (con vista previa de cuántas leyó), operación, protecciones y martingala.
- **Historial**: tarjetas con efectividad y resultado acumulados por cuenta (Demo/Real) leídos de `historial.csv`, más la tabla de operaciones.
- **Registro** con colores. La lógica de operación (señales, protecciones, martingala, binarias) es la misma de la v7.

## Modo automático (v9) — pestaña «Automático»
El bot puede analizar el mercado cada minuto y entrar solo, sin que le pegues señales (o junto con las que pegues).
- **Pares a operar**: fichas con las banderas de 18 pares. Se pueden cambiar en cualquier momento, también con el bot en marcha.
- **5 estrategias**: Reversión (RSI + Bollinger), Tendencia (EMAs + RSI), Pullback a favor de la tendencia, Ruptura de rango (Donchian)
  y **Probabilístico** (modelo logístico que aprende de las velas pasadas la probabilidad de ganar y solo entra si supera el punto
  de equilibrio más un margen; se reentrena cada hora).
- **Comparar estrategias**: descarga las velas anteriores de cada par elegido (1000–20000), ajusta los parámetros de cada estrategia
  en el 60% inicial y las evalúa TODAS en el 40% final (datos que no se usaron para ajustar nada). El ranking muestra operaciones,
  efectividad, diferencia frente al equilibrio (≈54% con pago de 85%) y un veredicto; ★ marca la mejor por par. Se ordena por el límite
  inferior del intervalo de confianza del 95%, así que una estrategia con pocas operaciones no gana por suerte.
- **Con cuál operar**: «Mejor por par» (usa la ganadora de cada par, y solo si supera el equilibrio) o una estrategia fija para todos.
  Si activas una combinación que el backtest no respalda, te lo advierte y te pide confirmar.
- **Efectividad dinámica**: tarjetas con anillo en la pestaña (en vivo del modo automático y del backtest), columna «En vivo» en el
  ranking, y las del Panel siguen funcionando. «Decisiones del bot» muestra, minuto a minuto, qué leyó y por qué entró o no.
- Límites: máx. operaciones abiertas, máx. entradas por hora. Usa tu monto, capital, protecciones y martingala de siempre.
- Importante: en 1 minuto el precio es casi aleatorio. Un backtest bueno no garantiza ganancias futuras; prueba en Demo primero.

## Novedades v10
**Operar lo que esté abierto (normal u OTC)**
- Pestaña Automático → «Mercado a operar»: *Automático* (usa el mercado normal si está abierto y, si no, el OTC), *Solo normal* o *Solo OTC*.
- Botón **Detectar abiertos hoy**: consulta a Exnova y marca cada ficha con un punto (verde = normal abierto, ámbar = solo OTC, rojo = cerrado)
  y elige de un clic los pares que se pueden operar. El estado se vuelve a consultar cada 2 minutos mientras el bot corre.
- Usa el **pago real** de cada activo (no uno fijo) para calcular el punto de equilibrio, y **no opera** activos con pago menor al
  «Pago mínimo» (por defecto 80%: con 70% de pago necesitas acertar ≈59% solo para no perder).
- El backtest se hace sobre el mercado que se operaría hoy; si mañana cambia (p. ej. fin de semana → OTC), pulsa «Comparar» otra vez.

**Estrategia «Conjunto» (todas combinadas)**
- Junta en una sola las 4 estrategias técnicas (como votos) y las características del modelo probabilístico, y **aprende en cada par cuánto
  pesa cada voto**. Entra solo si la probabilidad de ganar supera el equilibrio más el margen. Aparece en el ranking junto a las demás;
  «Mejor por par» elige la que resulte más confiable. El motivo de cada decisión muestra los votos (▲ compra, ▼ venta, · nada).
- Ojo: combinar no garantiza más efectividad. En las pruebas igualó a la mejor estrategia individual y evitó los falsos positivos,
  pero no la superó siempre.

**Filtros que ayudan a no entrar a ciegas**
- *Horario*: opera solo dentro de una ventana (hora de tu PC). La mayor liquidez suele darse cuando se solapan Londres y Nueva York.
- *Pausa tras perder*: espera X minutos antes de volver a entrar cuando el mercado deja de comportarse como en el backtest.
- *Máx. entradas por hora* y *Máx. operaciones abiertas*: limitan el ritmo de operación.

## Novedades v11
**Estrategia «Adaptativa por régimen»** (tendencia / lateral)
- Mide si el mercado está en tendencia o lateral (ratio de eficiencia de Kaufman + EMAs). En tendencia alcista compra el retroceso cuando gira;
  en bajista vende el rebote cuando gira (siempre a favor de la tendencia); en lateral opera el rechazo en los extremos de Bollinger
  (o se abstiene si lo eliges). Sus parámetros se ajustan por par en la comparación.
- Cada decisión del bot muestra ahora el régimen leído (alcista / bajista / lateral y su ER). El modelo probabilístico y el Conjunto
  reciben el régimen como información y aprenden qué hacer en cada uno; el Conjunto vota también con la Adaptativa.
- El ajuste de parámetros ahora premia el límite inferior del intervalo de confianza (no la suerte con pocas operaciones).
- Entra al ranking como una estrategia más: no es mejor por definición, el backtest dice en qué pares funciona.

**«¿Y con martingala?» — simulación sobre el backtest**
- Elige una fila del ranking: el panel reproduce sus operaciones reales del backtest con 0, 1 y 2 niveles de martingala (usa tu monto base,
  multiplicador y pago) y muestra ciclos ganados, neto, total apostado, rendimiento por dólar apostado, peor caída y apuesta máxima.
- La martingala NO cambia la efectividad por operación ni crea ventaja: sube el % de ciclos ganados pero un ciclo fallido cuesta
  1 + m + m² veces la base. Si la estrategia no supera el equilibrio, apostar más solo pierde más rápido.

## Novedades v12
**Operar a 2, 3, 4 o 5 minutos (duración óptima por par)**
- «Duración de las operaciones»: *Automática* (el backtest elige la mejor por par y estrategia entre las duraciones indicadas en
  «Duraciones a probar») o *Fija* (la de Configurar). El panel «Efectividad según la duración» muestra cómo rinde la fila elegida a cada duración.
- Protocolo sin trampa: parámetros, filtros y duración se eligen con el primer 60% de las velas; las cifras que ves son del último 40%.
  Escoger a ojo la mejor de varias duraciones infla el resultado: confírmala en Demo.

**Apuesta del modo automático: fijo, martingala o Kelly**
- *Martingala propia del automático*: multiplicador, niveles máximos y qué hacer al agotarlos (volver al monto base o desactivar el
  modo automático). Nunca usa más que el capital libre. (Desde la v13 lleva un nivel POR PAR: ver «Novedades v13».)
  Al activarla te muestra cuánto cuesta perder todos los niveles seguidos. La simulación «¿Y con martingala?» usa ese multiplicador.
- *Kelly*: apuesta proporcional a la ventaja estimada (conservadora), con fracción y tope sobre el capital; sin ventaja no entra.

**Mejoras de efectividad**
- *Confirmación con velas de 5 min* y *filtro de volatilidad*: el backtest decide para cada estrategia y duración si usa uno, ambos o ninguno.
- *Limitar pares correlacionados*: no abre dos operaciones en el mismo sentido sobre una misma divisa (p. ej. EUR/USD y GBP/USD a la vez)
  ni dos operaciones simultáneas en el mismo mercado.
- *Vigilante de salud*: si en vivo una estrategia rinde por debajo del equilibrio (últimas 30 operaciones, mínimo 15) se pausa 60 min y
  «Mejor por par» pasa a la siguiente.
- *Puerta Demo → Real*: en cuenta Real solo opera combinaciones (estrategia · mercado · duración) que acumularon en Demo las operaciones
  mínimas con límite inferior de efectividad sobre el equilibrio. Se guarda en espyx_gate.json. Si desactivas la opción, opera sin esa exigencia.

## Novedades v15.2 · Filtro de noticias y velas guardadas

**Filtro de noticias económicas** (pestaña Automático → «Filtro de noticias económicas», encendido por defecto):
- Calendario de la semana de ForexFactory (gratis). Se baja al abrir el bot y cada 4 h; se guarda en
  espyx_noticias.json para cuando no haya internet.
- No entra en un mercado desde X min antes hasta Y min después (15/15 por defecto) de una noticia de su divisa:
  EUR/USD se frena con noticias de EUR y de USD; oro, petróleo, índices de EE. UU. y acciones con las de USD;
  DAX con EUR; FTSE con GBP; Nikkei con JPY.
- «Solo alto impacto» (por defecto) o «Alto y medio». Solo mercado normal; «Aplicarlo también en OTC» es opcional.
- Si el par también está abierto en OTC y el mercado es «Automático», puede operar el OTC mientras el normal espera.
- La lista «Próximas noticias de tus pares» marca con ⛔ las que están frenando al bot en este momento.
- En Decisiones aparece «Noticia» con el nombre y la hora de la noticia.
- El backtest no puede aplicar este filtro (el calendario gratuito solo trae la semana actual).

**Velas guardadas en el PC** (carpeta «velas»):
- «Comparar» solo descarga lo nuevo desde la última vela guardada. Por defecto analiza 20.000 velas (máx. 50.000).
- El histórico crece con el uso (tope por mercado: 60.000 velas de 1 min / 30 s, 30.000 de 5 min).
- El análisis en vivo también arranca desde lo guardado.
- Botón «Borrar velas guardadas» en el ranking.

**Parar tras X ganadas** (Configurar → Protecciones): para la sesión al llegar a X operaciones ganadas.
En automático no abre más operaciones a la vez de las que pueden completar la meta
(ej. meta 3 con 2 ganadas: solo 1 abierta a la vez).

## Novedades v15.1 · Tramo de validación y «solo los pares con efectividad»

**Backtest en 3 tramos** (antes 60/40):
- 50% ajuste: parámetros, filtros y duración.
- 25% selección: se elige la ★ de cada par (columna «Selec.» del ranking).
- 25% prueba final: no se usa para elegir nada; de aquí salen las cifras y el veredicto (columna «Prueba»).
- **✔ Confirmada** = supera el equilibrio en la prueba final (de forma confiable) **y** en la selección.
  «Solo prueba» = ganó en la prueba pero no en la selección: probable suerte; no se opera con el filtro activo.
- El resumen de «Comparar» dice cuántas confirmadas falsas se esperan por azar (≈2,5% de las combinaciones probadas).

**Qué pares operar** (Control del modo automático):
- *Solo los que superan el equilibrio*: entra solo en los ✔ Confirmada; los demás marcados se omiten.
- *Solo los N mejores*: de los confirmados abiertos en cada vela, solo los N con mejor efectividad confiable
  (límite inferior del intervalo con selección + prueba, menos el equilibrio). Si uno cierra, entra el siguiente.
  Un par con gale o ciclo de interés compuesto a medias se respeta hasta terminarlo.
- *Todos los elegidos*: opera todo lo marcado aunque pierda en el backtest (solo para Demo).
- Botón **✔ Elegir confirmados** (ranking): marca solo los pares confirmados (los N primeros si usas «N mejores»).
- Si ninguno de los marcados está confirmado, el modo automático no se activa y te dice por qué.

## Novedades v15

**1. Estrategia principal: «Estructura (impulso + retroceso a la zona dorada)»**
- Impulso grande, retroceso pequeño, siguiente impulso. Con la tendencia ya detectada (lectura 1m·5m·15m·1h), espera a que
  el retroceso toque la **zona dorada (50–61,8% del último impulso) sin pasarla** y entra a favor de la tendencia cuando
  cierra una vela de rechazo en la zona (cierre a favor o mecha larga, con giro del RSI o ruptura de la mini estructura).
  Si el retroceso no llega al 50% o pasa del 61,8%, no se opera.
- Viene elegida por defecto en «Estrategia con la que operar». El backtest ajusta el tamaño mínimo del impulso, cuántas
  velas después del toque se acepta la entrada y si exige espacio hasta el próximo nivel; la zona 50–61,8% es fija.
- Da pocas entradas (solo las que cumplen todo): para medirla con confianza compara con más velas (12.000–20.000).
- Si no supera el equilibrio en el backtest, el bot te lo advierte antes de activarla.

**2. Gestión «Interés compuesto por par»** (por defecto, en lugar de la martingala)
- Si un par gana, su siguiente operación va por monto + ganancia; al completar el ciclo (1 reinversión = 2 ganadas
  seguidas) o al perder, vuelve al monto base. Cada par lleva su propio ciclo.
- Un ciclo nunca pierde más que el monto base (la martingala de 2 niveles podía perder 7 veces el monto de golpe).
- No cambia el equilibrio: ganancia esperada por ciclo = (p·(1+pago))^(reinversiones+1) − 1, que es 0 justo en el
  equilibrio. Si la estrategia gana, acelera; si no, pierde más rápido que el monto fijo.
- La tarjeta «Gestión de capital» compara sobre el backtest: monto fijo, interés compuesto (1 y 2 reinversiones) y
  martingala (1 y 2 niveles).

**3. Binarias con velas de 5 minutos** (Tipo de operación → «Binarias · velas de 5 min»): analiza velas de 5 min y vence a
  los 5 min exactos. Menos ruido y menos entradas; tiene su propio backtest.

**4. Interfaz «terminal profesional»**
- Negro grafito con un solo acento dorado; verde y rojo solo para ganar/perder; naranja para OTC y avisos; cifras con
  fuente monoespaciada.
- Animaciones: el saldo y las cifras suben/bajan, los anillos de efectividad se llenan, curva de resultado de la sesión
  en el Panel, punto «EN VIVO» que late, pestañas con subrayado que se desliza, interruptores que se deslizan, botones con
  transición y avisos que aparecen y se desvanecen.
- Sin costo de fluidez: un solo reloj de animación que corre solo mientras algo se anima, cambia figuras ya dibujadas (no
  redibuja la ventana) y se pausa con la ventana minimizada. Medido con 6 pares comparándose: congelamiento máximo
  23–31 ms (v14.3: 29–63 ms), cambio de pestaña con mediana de 10–11 ms (igual que antes).

**Correcciones incluidas (v14.3)**
- «Active not found» en acciones (Alibaba, Coke, Citi…): esas acciones solo tienen binarias de vencimiento largo (8 min o
  más), no de 1–5 min. Ahora solo se ofrecen los activos con 1–5 min abiertos, cada compra usa el id de su modalidad y, si
  Exnova no reconoce un activo, se deja 60 min y el bot sigue con los demás.
- Al activar en cuenta REAL con «Exigir validación en Demo» apagado, el aviso lo dice claramente.

## Novedades v14.2
- **Más mercados**: «Detectar abiertos hoy» consulta TODO lo que Exnova tiene abierto y lo ordena en categorías: Forex,
  Materias primas (oro, plata, petróleo…), Índices (US30, NASDAQ, GER30…), Acciones (Apple, Tesla…) y Cripto. Elige la
  categoría con los botones; «Detectar» y «Todos» actúan sobre la categoría que estás viendo y lo elegido en las demás se
  conserva. Todo lo demás (backtest, filtro de tendencia, martingala por par) funciona igual con cualquier activo.
- **Dos puntos** en la ficha cuando el mercado normal y el OTC están abiertos a la vez (verde = normal, naranja = OTC).
- El filtro de pares correlacionados solo se aplica a pares de divisas/metales (las acciones e índices no comparten divisas).
- Ojo: comparar muchos activos tarda más (cada uno descarga 8000 velas) y, entre más se prueban, más fácil es que alguno salga
  «ganador» por pura suerte: confirma en Demo antes de usar Real.

## Novedades v14.1
- **Corregido: «Detectar abiertos hoy» no reconocía el mercado normal.** Exnova llama a los pares normales `EURUSD-op`,
  `GBPUSD-op`… y deja el nombre viejo (`EURUSD`, id 1) deshabilitado; el bot leía el viejo y marcaba todos cerrados (solo veía
  OTC). Ahora unifica los dos nombres, usa el que está abierto y su id real para operar y pedir velas (también en Blitz).
- «Detectar abiertos hoy» guarda `diagnostico_activos.txt` con lo que dice Exnova de cada par normal y OTC.

## Novedades v14

Objetivo: dejar de perder las entradas que van **contra la tendencia** y las que van **contra el retroceso** (entrar a favor
mientras el precio todavía está retrocediendo).

**1. Lectura única de la tendencia con 1 min, 5 min, 15 min y 1 h**
- Cada temporalidad da una puntuación de −1 a +1: mitad separación EMA8–EMA21 (medida en su rango medio), mitad eficiencia de sus
  últimas 10 velas (cuánto del recorrido fue en una sola dirección). Pesos: 1m 15% · 5m 30% · 15m 30% · 1h 25%.
- Desde ±25% es **alcista/bajista** (con su fuerza en %). Si la lectura es menor pero 15m + 1h marcan ±40%, manda esa tendencia de
  fondo. Si no, **lateral**.
- Sin mirar el futuro: en cada vela usa las velas mayores ya cerradas y la que se está formando con el precio actual (lo que ves en el
  gráfico de esa temporalidad en ese momento). Probado: cortar la serie en cualquier vela da exactamente la misma lectura.
- Necesita ≥ 22 h de velas para la de 1 h: al activar, el bot descarga el histórico de cada par (2000 velas de 1 min / 3000 de 30 s);
  mientras tanto ese par aparece como «Preparando» y no opera.

**2. Fase del movimiento (retroceso)**
- Busca el último impulso (60 velas) y mide cuánto retrocedió (Fibonacci): < 23,6% consolidación · 23,6–78,6% retroceso ·
  > 78,6% **tendencia rota**.
- **Retroceso agotado** = la vela ya no sigue a favor del retroceso + vela de rechazo (cierre a favor de la tendencia o mecha ≥ 60%)
  + giro del RSI desde ≤ 50 **o** ruptura de la mini línea del retroceso (cierre por encima de los máximos de las 2 velas anteriores).
  Si no, **retroceso en curso**.
- **Estirado** = a más de 4 ATR de la EMA21 o RSI ≥ 85 / ≤ 15 (donde suele empezar el retroceso).

**3. Soportes y resistencias de 15 min y 1 h**
- Máximos y mínimos de giro (5 velas) ya confirmados: de 15m las últimas 12 h, de 1h las últimas 48 h.
- Espacio mínimo hasta el nivel en contra: 1 ATR × √(minutos de la operación). «Rebote en nivel» = el retroceso tocó un soporte
  (o resistencia) a menos de 0,5 ATR y giró.

**4. Filtro de tendencia (Automático → «Filtro de tendencia»)**
- **Obligatorio** (por defecto). Ninguna estrategia entra:
  1. contra la tendencia (en lateral claro se permiten los dos sentidos);
  2. a favor mientras el retroceso sigue en curso, si está estirado o si la tendencia está rota;
  3. con una duración más corta que la temporalidad que va en contra: 1 min necesita que acompañe la de 5m; 2–3 min, la de 15m;
     4–5 min, la de 15m y la de 1h;
  4. pegada a un soporte/resistencia de 15m/1h en contra.
- **Que lo decida el backtest**: el 60% inicial de las velas decide, en cada par y estrategia, si el filtro mejora.
- **Desactivado**: como la v13.
- El backtest se hace CON el filtro: lo que mide es lo que opera (probado: 339 de 339 decisiones idénticas en vivo y en backtest).
  En un mercado al azar sigue sin encontrar ventaja (no se inventa resultados).
- Si cambias el filtro, vuelve a pulsar «Comparar estrategias» (la ventana te avisa).

**5. Estrategia nueva: «Retroceso MTF»**
- Solo a favor de la tendencia: espera el retroceso, que se agote dentro de la zona elegida (23,6/38,2 – 61,8/78,6%) y que quede
  espacio hasta el próximo nivel; opcionalmente exige rebote en un soporte/resistencia de 15m/1h. El backtest ajusta la zona, el
  espacio, la fuerza mínima de la tendencia y si exige nivel.

**6. Duración «según la tendencia»** (Automático → «Duración de las operaciones», por defecto)
- En cada vela prueba las duraciones que superan el equilibrio en el backtest, de mejor a peor, y entra con la primera que da señal y
  encaja con la tendencia. Así una entrada en un retroceso de 5m no va a 1 min (iría contra la de 5m) sino a 3 min si la de 15m acompaña.
- «Automática» (la mejor por par) y «Fija» siguen disponibles.

**7. El backtest muestra a favor vs en contra (para verificarlo en OTC con datos)**
- Tarjeta nueva «¿A favor o en contra de la tendencia?»: para la fila elegida del ranking, las señales SIN ningún filtro del último 40%,
  clasificadas por el contexto al entrar: a favor / en contra / lateral, fase (impulso, consolidación, estirado, retroceso en curso,
  retroceso agotado, rota), con espacio o pegadas a un nivel, y cuáles dejaría pasar el filtro. La columna derecha suma todas las
  estrategias del par. Verde = sobre el equilibrio, rojo = debajo, gris = menos de 20 operaciones.
- En el Registro, una línea por par con el mismo resumen después de cada comparación.
- Cómo leerlo: si «En contra» y «retroceso en curso» salen en rojo y «A favor» / «retroceso agotado» en verde, el filtro obligatorio es lo
  que conviene en ese par. Si un par lo contradice con muchas operaciones, usa «Que lo decida el backtest».

**8. Errores corregidos (encontrados en tu registro.log)**
- **USD/JPY OTC a 5 min rechazada dos veces** («Time for purchasing options is over»): ahora esa duración se deja 30 min en ese par y,
  en la MISMA vela, el bot prueba con la siguiente duración validada.
- **GBP/USD rechazada sin motivo**: la librería usaba el mismo identificador («buyraw») para todas las compras y borraba las respuestas
  anteriores; si Exnova respondía tarde, la respuesta se perdía o podía atribuirse a otra orden, y una compra que sí se abría quedaba
  sin seguir. Ahora cada compra lleva su propio identificador; si Exnova la confirma después de los 5 s, el bot la registra
  («[confirmada tarde]»), la sigue hasta su resultado y la cuenta en el capital, sin mover la martingala. Los rechazos sin texto
  muestran un motivo claro.
- **Vigilante de salud**: exigía 15 operaciones y casi nunca saltaba (USD/CHF perdió 5 de 5 sin pausa). Ahora es bayesiano: parte de la
  efectividad del backtest (encogida a medio camino del equilibrio) y la corrige con cada resultado en vivo. Con un backtest del 62%,
  pausa 60 min con 0/5, 1/7, 2/9 o 3/11 ganadas. En simulación: una estrategia que en vivo rinde 45% se pausa el 93% de las veces
  (mediana: a las ~25 operaciones); una buena (60%) solo el ~20% de las veces en 100 operaciones.
- En vivo los indicadores usan 700 velas (antes 400: la EMA200 del Pullback no alcanzaba a estabilizarse y podía dar una señal
  distinta a la del backtest).
- «Velas a descargar» por defecto: 8000 (≈ 5,5 días de 1 min) para que haya suficientes velas de 1 h.

**Qué esperar**: menos entradas (el filtro descarta las que van contra la tendencia o el retroceso). La ventaja es estadística, no
segura: confírmala en Demo (la puerta Demo → Real sigue activa) antes de usar dinero real.

## Novedades v13

**1. Martingala POR PAR (el gale va en la siguiente vela de la MISMA divisa)**
- Antes había un solo contador para todo el bot: si en una vela entraban 4 pares y perdían 2, el monto doblado caía en la siguiente
  operación que se abriera, aunque fuera de otra divisa. Ahora cada par lleva su propio nivel: el gale va SOLO en los que perdieron,
  en su siguiente vela, y solo si el análisis vuelve a dar señal en ese par. Si no confirma dentro de «MG · Velas para confirmar»
  (1 por defecto), el ciclo se cierra y ese par vuelve al monto base. Los que ganaron siguen con el monto base.
- «MG · Dirección del gale»: la que indique el análisis en esa vela, o solo si repite la dirección que perdió.
- Los gales tienen prioridad en cupos y capital, y la «Pausa tras perder» y el horario no los cortan (solo frenan entradas nuevas).
- Señales pegadas (Configurar → Martingala por par): gale en la siguiente vela del MISMO par y dirección —confirmado con el análisis
  del backtest de ese par, o sin confirmar (gale clásico)— o en la próxima señal del mismo par.
- Con martingala activa, cada par tiene como mucho UNA operación a la vez (si en la vela del gale tu lista trae otra señal del
  mismo par, va solo el gale), y el capital se aparta de forma atómica antes de cada compra: aunque pierdan varias a la vez y
  todas pidan gale en el mismo segundo, nunca se usa más que el «Capital total» (si no alcanza, ese gale no se hace o el bot se detiene).
- Si el resultado de un gale no llega a confirmarse (corte de conexión), ese ciclo se cierra y el par vuelve al monto base.
- Panel → «Rendimiento por par» muestra el nivel de cada par (G1 pendiente / G1 en curso). La simulación «¿Y con martingala?» sigue igual.

**2. Entradas a tiempo (adiós a las de 1:30) y modo Blitz**
- Por qué pasaba: el análisis podía empezar hasta el segundo 45 y revisaba los pares uno por uno (hasta 40 s). Si la compra salía
  después del segundo 30, Exnova pasaba la operación de 1 min al minuto siguiente: 1:30 en vez de 1:00, algo que el backtest nunca midió.
- Ahora el modo automático corre en su propio hilo, con la hora del servidor de Exnova: analiza justo al cierre de la vela (velas de
  todos los pares a la vez y en caché) y entra en el primer segundo de la siguiente. Las binarias vencen EXACTAMENTE al cierre de la
  vela. Si una orden no alcanza a salir antes de «Entrar como máximo al segundo» (8 por defecto), NO se abre.
- Si el reloj de tu PC está corrido unos segundos ya no importa (la cabecera te avisa cuánto).
- «Tipo de operación → Blitz»: analiza velas de 30 s y abre Blitz de 30 o 60 s exactos desde la compra. Tiene su propio backtest
  (elige Blitz y pulsa «Comparar»). Como cada Blitz dura exactamente una vela desde la compra, de vez en cuando un par se salta una vela
  para no entrar tarde. Las expiraciones de 5–15 s son solo para cuentas VIP; algunos pares solo tienen Blitz en OTC.

**3. Ventana fluida (se trababa al pasar a Automático, al Detener y al bajar por las opciones)**
- Causas encontradas: el backtest y el entrenamiento de modelos corrían en hilos de Python, y la librería de Exnova esperaba sus
  respuestas «girando» al 100% de CPU. Con eso cada redibujo de la ventana esperaba su turno (medido: 100 dibujos pasaban de 0,5 ms a
  170–320 ms). Además, la rueda del ratón sobre un desplegable CAMBIABA su valor (estrategia, mercado, apuesta…) al bajar por la página.
- Ahora: cálculos pesados en procesos aparte (prioridad baja), 41 esperas activas de la librería convertidas en pausas, «Detener»
  responde al instante y muestra cuántas operaciones quedan por cerrar, «Iniciar» ya no congela la ventana mientras lee el saldo,
  la rueda nunca cambia un desplegable, el registro en pantalla no crece sin límite y los íconos se guardan en disco.
- Medido con 6 pares comparándose a la vez: congelamiento máximo 560 ms → 33 ms; cambio Panel ↔ Automático 392 ms → 25 ms.

**4. Cuentas guardadas, también con contraseña**
- La casilla del correo es una lista: al elegir una cuenta guardada se rellenan su contraseña (si la guardaste) y su tipo (Demo/Real).
  «Recordar mi contraseña» y «Olvidar esta cuenta».
- La contraseña nunca se guarda en la carpeta del bot: en Windows va cifrada con la protección de datos del propio Windows (DPAPI: solo
  tu usuario, en este PC, puede leerla) o en el Administrador de credenciales si tienes instalada la librería `keyring`; en Mac, en el Llavero.

**5. Patrones históricos: el bot «mira hacia atrás»**
- Nueva estrategia «Patrones»: describe la situación actual (dirección de las 3 últimas velas, posición en Bollinger, RSI, régimen y
  mechas), busca cuántas veces pasó lo mismo en el histórico y qué hizo el precio después. Solo entra si, con suficientes casos, eso
  supera el equilibrio más el margen. La estadística se calcula por niveles (situación exacta → misma secuencia de velas → general) para
  no fiarse de casos vistos pocas veces.
- Nuevo filtro «patrones»: veta la entrada de cualquier estrategia si en situaciones parecidas el precio fue más veces en sentido
  contrario. El backtest decide en qué estrategia y duración conviene usarlo.
- Sin trampa: en el backtest la memoria solo usa lo que el bot habría sabido en cada momento. Probado: en un mercado al azar no encuentra
  ventaja (como debe ser) y en uno con un patrón real del 68% lo encuentra (70,9% en 299 operaciones de validación).
- «Volver a comparar solo cada X horas»: con el bot en marcha repite la comparación con velas frescas, en segundo plano, para que
  «Mejor por par» se adapte si el mercado cambia.
- Ningún programa sabe lo que viene: el bot mide con el pasado qué situaciones tuvieron ventaja y opera solo esas. Si el mercado cambia
  de comportamiento, el vigilante de salud y la re-comparación lo detectan.

**Otros**
- El registro también se guarda en `registro.log` (botón «Abrir registro.log» en la pestaña Registro).
- La pestaña Configurar se puede desplazar (en pantallas bajas no se corta nada).
