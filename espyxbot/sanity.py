"""Control: sobre paseo aleatorio NO debe haber ventaja (WR ~50%). Si sale >56% hay bug/look-ahead."""
import numpy as np, pandas as pd, itertools
from engine import backtest, walk_forward, DEFAULT
rng = np.random.default_rng(1)
n = 120_000
r = rng.normal(0, 0.0004, n)
c = 1.1 * np.exp(np.cumsum(r)); o = np.r_[c[0], c[:-1]]
df = pd.DataFrame({"open": o, "close": c,
                   "high": np.maximum(o, c) * (1 + abs(rng.normal(0, 1e-4, n))),
                   "low": np.minimum(o, c) * (1 - abs(rng.normal(0, 1e-4, n)))},
                  index=pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC"))
print("Backtest completo:", backtest(df)[1])
grid = [dict(DEFAULT, ema_trend=e, rsi_n=n_, rsi_lo=lo, rsi_hi=100 - lo)
        for e, n_, lo in itertools.product([50, 100, 200], [5, 7, 14], [25, 30, 35])]
bp, ins, oos = walk_forward(df, grid)
print("IN-SAMPLE (mejor del grid):", ins)
print("OUT-OF-SAMPLE:", oos)
