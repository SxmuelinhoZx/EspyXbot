# EspyXbot — motor de backtesting (estado: SIN validar)

- `espyxbot/engine.py`: indicadores, estrategia base (pullback a favor de EMA + RSI + ATR + horario),
  gestión de capital (% fijo, pausa diaria y por racha), reporte y walk-forward (entrena 60 % / prueba 40 %).
- `espyxbot/sanity.py`: control en paseo aleatorio (resultado: ~49-52 %, sin ventaja falsa).

## Pendiente (bloqueado)
No hay resultado de win rate real: falta el script original y datos históricos reales.
Uso: `engine.load_csv("datos.csv")` (time,open,high,low,close) y luego `walk_forward(...)`.
Payout 80 % => breakeven 55,56 %; 75 % => 57,14 %.
