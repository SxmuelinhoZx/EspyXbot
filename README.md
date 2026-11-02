# EspyXbot — motor de backtesting (estado: SIN validar)

- `espyxbot/engine.py`: indicadores, estrategia base (pullback a favor de EMA + RSI + ATR + horario),
  gestión de capital (% fijo, pausa diaria y por racha), reporte y walk-forward (entrena 60 % / prueba 40 %).
- `espyxbot/sanity.py`: control en paseo aleatorio (resultado: ~49-52 %, sin ventaja falsa).

## Pendiente (bloqueado)
No hay resultado de win rate real: falta el script original y datos históricos reales.
Uso: `engine.load_csv("datos.csv")` (time,open,high,low,close) y luego `walk_forward(...)`.
Payout 80 % => breakeven 55,56 %; 75 % => 57,14 %.

## Actualización: bot v15.2 recibido
- Código original en `espyx_bot_v15_2/` (sin modificar). Ya trae backtest en 3 tramos (ajuste 50 % / selección 25 % / prueba 25 %),
  intervalo de Wilson, filtro de tendencia 1m-1h, filtro de noticias, interés compuesto por par y puerta Demo→Real.
- `control_azar.py`: corre `strategy.compare` sobre mercado aleatorio. Resultado (20.000 velas, seed 7, pago 85 %):
  Tendencia 197/418 (47 %), Pullback 36/86, Régimen 47/103, Estructura 9/14 (ruido por pocas operaciones). No hay ventaja falsa.
- Sin datos reales de Exnova no hay win rate demostrable. Para correr el backtest real: abrir el bot, conectar (Demo) y pulsar
  «Comparar» con 20.000 velas; o exportar velas a CSV/`velas/*.json.gz` y pasarlas a `strategy.compare`.
