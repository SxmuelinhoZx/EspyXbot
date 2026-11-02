"""Motor de backtesting para opciones binarias (EspyXbot).

Modelo: se decide al CIERRE de la vela i, se entra al OPEN de i+1 y la
operación expira al CIERRE de i+1 (expiración = 1 vela). Sin look-ahead.
Gana si el cierre supera/queda bajo la apertura según la dirección.
"""
import numpy as np
import pandas as pd


def load_csv(path):
    """CSV con columnas: time,open,high,low,close (time parseable)."""
    df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").set_index("time")[["open", "high", "low", "close"]]


def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def signals(df, p):
    """Pullback a favor de tendencia, con filtros confluentes.
    +1 CALL, -1 PUT, 0 sin operar. Usa solo datos hasta la vela i."""
    c = df.close
    trend = ema(c, p["ema_trend"])
    slope = trend.diff(p["slope_n"])
    r = rsi(c, p["rsi_n"])
    a = atr(df, 14)
    a_rel = a / a.rolling(100).mean()          # volatilidad relativa (evita rango muerto)
    body = (c - df.open)
    hour = df.index.hour
    ok_time = (hour >= p["h_from"]) & (hour < p["h_to"])
    ok_vol = (a_rel > p["atr_min"]) & (a_rel < p["atr_max"])
    up = (c > trend) & (slope > 0) & (r < p["rsi_lo"]) & (body > 0)   # retroceso + vela de giro
    dn = (c < trend) & (slope < 0) & (r > p["rsi_hi"]) & (body < 0)
    sig = pd.Series(0, index=df.index)
    sig[up & ok_time & ok_vol] = 1
    sig[dn & ok_time & ok_vol] = -1
    return sig


DEFAULT = dict(ema_trend=100, slope_n=10, rsi_n=7, rsi_lo=35, rsi_hi=65,
               atr_min=0.8, atr_max=2.5, h_from=7, h_to=17)


def backtest(df, p=DEFAULT, payout=0.80, stake_pct=0.01, cap0=1000.0,
             max_daily_loss=0.05, max_streak_pause=4):
    """Monto fijo % del capital. Pausa diaria por pérdida máx. y tras N pérdidas seguidas."""
    sig = signals(df, p).shift(1).fillna(0)        # señal de i-1 -> entrada en open de i
    res = np.where(sig > 0, (df.close > df.open), (df.close < df.open))
    res = np.where(df.close == df.open, np.nan, res)  # empate = devolución
    cap, peak, mdd, streak, losing = cap0, cap0, 0.0, 0, 0
    day, day_start, pause_day = None, cap0, None
    rows = []
    for t, s, w in zip(df.index, sig.values, res):
        d = t.date()
        if d != day:
            day, day_start, losing = d, cap, 0
        if s == 0 or d == pause_day or losing >= max_streak_pause:
            continue
        if np.isnan(w):
            continue
        stake = cap * stake_pct
        cap += stake * payout if w else -stake
        losing = 0 if w else losing + 1
        peak = max(peak, cap); mdd = max(mdd, (peak - cap) / peak)
        if (day_start - cap) / day_start >= max_daily_loss:
            pause_day = d
        rows.append((t, bool(w), cap))
    tr = pd.DataFrame(rows, columns=["time", "win", "cap"])
    return tr, report(tr, payout, mdd, cap0)


def report(tr, payout, mdd, cap0):
    n = len(tr)
    if n == 0:
        return dict(trades=0)
    w = tr.win.values
    def maxrun(v):
        best = cur = 0
        for x in v:
            cur = cur + 1 if x else 0
            best = max(best, cur)
        return best
    wr = w.mean()
    se = np.sqrt(wr * (1 - wr) / n)
    return dict(trades=n, wins=int(w.sum()), win_rate=round(wr * 100, 2),
                wr_ic95_inf=round((wr - 1.96 * se) * 100, 2),
                breakeven_wr=round(100 / (1 + payout), 2),
                max_win_streak=maxrun(w), max_loss_streak=maxrun(~w),
                max_drawdown_pct=round(mdd * 100, 2),
                final_capital=round(tr.cap.iloc[-1], 2), cap0=cap0)


def walk_forward(df, grid, train=0.6, **kw):
    """Elige parámetros en el tramo de entrenamiento y reporta SOLO el tramo fuera de muestra."""
    k = int(len(df) * train)
    tr_df, te_df = df.iloc[:k], df.iloc[k:]
    best, bp = -1, None
    for p in grid:
        _, r = backtest(tr_df, p, **kw)
        if r["trades"] >= 100 and r["wr_ic95_inf"] > best:
            best, bp = r["wr_ic95_inf"], p
    if bp is None:
        return None, None, None
    return bp, backtest(tr_df, bp, **kw)[1], backtest(te_df, bp, **kw)[1]
