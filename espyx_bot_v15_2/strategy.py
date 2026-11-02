"""
Estrategias, modelo probabilístico y backtest de EspyX (Python puro, sin librerías externas).

La MISMA lógica calcula las señales en el backtest y en vivo: lo que mides es lo que opera.

Convención de una operación de opciones binarias:
  - La señal se calcula al CIERRE de la vela i (precio de entrada = cierre de i).
  - Dura `minutes` velas de 1 min: gana si el cierre de la vela i+minutes queda del lado correcto
    del precio de entrada (empate = devolución).
  - Pago `payout` (ej. 0.85): gana +0.85 por cada 1 apostado, pierde -1.
  - Punto de equilibrio: efectividad mínima para no perder = 1 / (1 + payout)  (≈ 54.1% con 85%).

v14 — contexto multi-temporalidad (sección «contexto»):
  - Lectura única de la tendencia con las velas de 1 min, 5 min, 15 min y 1 h (alcista / bajista / lateral + fuerza).
  - Fase del movimiento: impulso, estirado, retroceso en curso, retroceso agotado o tendencia rota (Fibonacci del impulso).
  - Soportes y resistencias de 15 min y 1 h, y el espacio que queda hasta ellos.
  - Filtro «mtf»: ninguna estrategia entra contra la tendencia, ni mientras el retroceso sigue en curso, ni pegada a un
    nivel en contra, ni con una duración más corta que la temporalidad que va en contra.
"""
import bisect
import math
import random
from dataclasses import dataclass

STRATEGIES = {
    "structure": "Estructura (impulso + retroceso a la zona dorada)",
    "ensemble": "Conjunto (todas combinadas)",
    "pattern": "Patrones históricos (memoria del mercado)",
    "retrace": "Retroceso MTF (tendencia 1m–1h + retroceso agotado)",
    "regime": "Adaptativa por régimen (tendencia / lateral)",
    "reversion": "Reversión (RSI + Bollinger)",
    "trend": "Tendencia (EMAs + RSI)",
    "pullback": "Pullback a favor de la tendencia",
    "breakout": "Ruptura de rango (Donchian)",
    "prob": "Probabilístico (modelo logístico)",
}
SHORT = {"structure": "Estructura", "ensemble": "Conjunto", "pattern": "Patrones", "retrace": "Retroceso MTF", "regime": "Adaptativa",
         "reversion": "Reversión", "trend": "Tendencia", "pullback": "Pullback", "breakout": "Ruptura",
         "prob": "Probabilístico"}
TECH = ["reversion", "trend", "pullback", "breakout", "regime"]   # las estrategias técnicas que alimentan al Conjunto
MODEL_KEYS = ("prob", "ensemble", "pattern")                     # las que necesitan un modelo entrenado con el histórico
DESCRIPTIONS = {
    "structure": "Impulso grande, retroceso pequeño, siguiente impulso. Con la tendencia ya detectada (1m·5m·15m·1h), espera a que el retroceso toque la zona dorada (entre el 50% y el 61,8% del último impulso) sin pasarla, y entra a favor de la tendencia cuando cierra una vela de rechazo en esa zona (cierre a favor o mecha larga, con giro del RSI o ruptura de la mini estructura del retroceso). Si el retroceso no llega al 50% o pasa del 61,8%, no se opera.",
    "retrace": "Opera solo a favor de la tendencia de 1m, 5m, 15m y 1h: espera a que el precio retroceda (entre el 23,6% y el 78,6% del último impulso), a que el retroceso se agote (vela de rechazo + giro del RSI o ruptura de la mini línea del retroceso) y a que quede espacio hasta el próximo soporte o resistencia de 15m/1h. Nunca entra mientras el retroceso sigue cayendo ni si pasó del 78,6% (tendencia rota).",
    "pattern": "Mira hacia atrás: describe la situación actual (dirección de las últimas velas, posición en Bollinger, RSI, régimen y mechas) y busca cuántas veces ocurrió lo mismo en el histórico y qué hizo el precio después. Solo entra si esa estadística, con suficientes casos, supera el equilibrio más el margen.",
    "regime": "Lee el régimen del mercado (ratio de eficiencia + EMAs): en tendencia alcista compra el retroceso cuando gira, en bajista vende el rebote cuando gira (siempre a favor), y en lateral opera el rechazo en los extremos de Bollinger (o se abstiene).",
    "ensemble": "Junta las 4 estrategias técnicas y el modelo probabilístico en una sola: aprende en cada par cuánto pesa el voto de cada una y entra solo si la probabilidad de ganar supera el equilibrio más un margen.",
    "reversion": "Compra cuando el precio cae bajo la banda inferior de Bollinger con RSI bajo (y vende en el extremo contrario): apuesta a que rebota.",
    "trend": "Compra si la EMA rápida está sobre la lenta, el cierre sobre la rápida y el RSI marca fuerza; vende al revés: sigue la tendencia.",
    "pullback": "Como Reversión, pero solo a favor de la tendencia de fondo (EMA50 vs EMA200): compra los retrocesos en tendencia alcista.",
    "breakout": "Compra cuando el cierre rompe el máximo de las últimas N velas con tendencia a favor; vende cuando rompe el mínimo.",
    "prob": "Aprende de las velas pasadas la probabilidad de que el precio suba y solo entra si esa probabilidad supera el punto de equilibrio más un margen.",
}


@dataclass
class Params:
    rsi_period: int = 14
    rsi_lo: float = 30.0
    rsi_hi: float = 70.0
    bb_period: int = 20
    bb_k: float = 2.0
    ema_fast: int = 9
    ema_slow: int = 21
    trend_fast: int = 50     # tendencia de fondo (solo «pullback»)
    trend_slow: int = 200
    dc_period: int = 20      # ventana de la ruptura (Donchian)
    prob_margin: float = 0.02  # confianza extra sobre el equilibrio (modelo probabilístico)
    er_period: int = 20        # ventana del ratio de eficiencia (régimen)
    er_trend: float = 0.25     # ER por encima de este valor = hay tendencia; por debajo = lateral
    pull_rsi: float = 45.0     # en tendencia alcista compra si RSI <= esto (en bajista vende si RSI >= 100 - esto)
    range_skip: bool = False   # True = en lateral no opera
    pat_min: int = 30          # patrones: veces mínimas que debe haberse visto la secuencia de velas para operar
    pat_k: float = 20.0        # patrones: cuánto se «encoge» cada estadística hacia la del nivel más general
    pat_level: int = 0         # patrones: descripción más fina que se usa (0 = completa, 1 = sin RSI ni mechas, 2 = solo velas)
    rt_lo: float = 0.382       # retroceso MTF: profundidad mínima del retroceso (fracción del impulso)
    rt_hi: float = 0.618       # retroceso MTF: profundidad máxima (más allá de 0,786 la tendencia se da por rota)
    rt_room: float = 1.5       # retroceso MTF: espacio mínimo hasta el próximo nivel (en ATR de la vela base)
    rt_level: bool = False     # retroceso MTF: exigir que el retroceso haya rebotado en un soporte/resistencia de 15m/1h
    rt_min: float = 0.25       # retroceso MTF: fuerza mínima de la tendencia (0–1)
    st_imp: float = 3.0        # estructura: tamaño mínimo del impulso (en ATR de la vela base)
    st_touch: int = 3          # estructura: velas como máximo desde que el retroceso tocó la zona dorada
    st_room: float = 0.0       # estructura: espacio mínimo hasta el próximo nivel de 15m/1h (ATR; 0 = no se exige)

    def warmup(self, strategy=None):
        base = max(self.rsi_period + 1, self.bb_period, self.ema_slow * 3, 60)
        if strategy == "pullback":
            base = max(base, self.trend_slow + 1)
        if strategy == "breakout":
            base = max(base, self.dc_period + 2)
        if strategy == "ensemble":
            base = max(base, self.trend_slow + 1, self.dc_period + 2, self.er_period + 6)
        if strategy in ("regime", "pattern"):
            base = max(base, self.er_period + 6)
        if strategy in ("retrace", "structure"):
            base = max(base, PB_WINDOW + 5)
        return base


MIN_TRADES = 30   # operaciones mínimas para que un resultado cuente


def breakeven(payout):
    return 1.0 / (1.0 + payout)


# ------------------------------------------------------------------ velas
def normalize_candles(raw):
    """Velas de Exnova ({from, open, close, min, max}) -> {t, o, h, l, c}, ordenadas y sin repetidas."""
    out = {}
    for r in raw or []:
        try:
            t = int(r.get("from", r.get("t")))
            o, c = float(r["open"]), float(r["close"])
            h = float(r.get("max", r.get("high", max(o, c))))
            l = float(r.get("min", r.get("low", min(o, c))))
        except (KeyError, TypeError, ValueError):
            continue
        out[t] = {"t": t, "o": o, "h": h, "l": l, "c": c}
    return [out[k] for k in sorted(out)]


def closed_only(candles, now_epoch, size=60):
    """Descarta la vela que aún se está formando."""
    return [c for c in candles if c["t"] + size <= now_epoch]


def random_walk(n=20000, seed=1, vol=0.0002, start=1.1000, t0=1_700_000_000):
    """Mercado sintético sin ninguna ventaja: sirve para comprobar que el backtest no se engaña."""
    rnd = random.Random(seed)
    out, price, t = [], start, t0
    for _ in range(n):
        o = price
        c = o * (1 + rnd.gauss(0, vol))
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 3)))
        out.append({"t": t, "o": o, "h": hi, "l": lo, "c": c})
        price, t = c, t + 60
    return out


# ------------------------------------------------------------------ indicadores
def bar_seconds(candles, default=60):
    """Tamaño de vela (s) deducido de las marcas de tiempo: la menor diferencia positiva entre velas seguidas."""
    best = None
    for a, b in zip(candles[:60], candles[1:61]):
        d = b["t"] - a["t"]
        if d > 0 and (best is None or d < best):
            best = d
    return int(best or default)


class Data:
    """Series de precios con indicadores en caché."""

    def __init__(self, candles, bar=None):
        self.candles = candles
        self.c = [x["c"] for x in candles]
        self.o = [x["o"] for x in candles]
        self.h = [x["h"] for x in candles]
        self.l = [x["l"] for x in candles]
        self.t = [x["t"] for x in candles]
        self.bar = int(bar or bar_seconds(candles))
        self._cache = {}

    def ema(self, period):
        key = ("ema", period)
        if key not in self._cache:
            v, n = self.c, len(self.c)
            out = [None] * n
            if n >= period:
                k = 2.0 / (period + 1)
                s = sum(v[:period]) / period
                out[period - 1] = s
                for i in range(period, n):
                    s = v[i] * k + s * (1 - k)
                    out[i] = s
            self._cache[key] = out
        return self._cache[key]

    def rsi(self, period):
        key = ("rsi", period)
        if key not in self._cache:
            v, n = self.c, len(self.c)
            out = [None] * n
            if n > period:
                g = l = 0.0
                for i in range(1, period + 1):
                    d = v[i] - v[i - 1]
                    g += max(d, 0.0)
                    l += max(-d, 0.0)
                ag, al = g / period, l / period
                out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
                for i in range(period + 1, n):
                    d = v[i] - v[i - 1]
                    ag = (ag * (period - 1) + max(d, 0.0)) / period
                    al = (al * (period - 1) + max(-d, 0.0)) / period
                    out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
            self._cache[key] = out
        return self._cache[key]

    def bb(self, period):
        """(media, desviación estándar) móviles."""
        key = ("bb", period)
        if key not in self._cache:
            v, n = self.c, len(self.c)
            mid, std = [None] * n, [None] * n
            for i in range(period - 1, n):
                w = v[i - period + 1:i + 1]
                m = sum(w) / period
                mid[i] = m
                std[i] = math.sqrt(sum((x - m) ** 2 for x in w) / period)
            self._cache[key] = (mid, std)
        return self._cache[key]

    def donchian(self, period):
        """(máximo, mínimo) de las `period` velas ANTERIORES (sin incluir la actual)."""
        key = ("dc", period)
        if key not in self._cache:
            n = len(self.c)
            hh, ll = [None] * n, [None] * n
            for i in range(period, n):
                hh[i] = max(self.h[i - period:i])
                ll[i] = min(self.l[i - period:i])
            self._cache[key] = (hh, ll)
        return self._cache[key]

    def er(self, period):
        """Ratio de eficiencia de Kaufman: |cambio neto| / recorrido total (0 = ruido lateral, 1 = línea recta)."""
        key = ("er", period)
        if key not in self._cache:
            n = len(self.c)
            out = [None] * n
            path = 0.0
            diffs = [0.0] + [abs(self.c[i] - self.c[i - 1]) for i in range(1, n)]
            for i in range(1, n):
                path += diffs[i]
                if i > period:
                    path -= diffs[i - period]
                if i >= period and path > 0:
                    out[i] = abs(self.c[i] - self.c[i - period]) / path
            self._cache[key] = out
        return self._cache[key]

    def htf_dir(self, bucket=None, fast=5, slow=13):
        """Dirección de la tendencia en velas 5 veces mayores (+1 alcista, -1 bajista, 0 sin datos) para cada vela.
        Con velas de 1 min son velas de 5 min; con las de 30 s (Blitz), de 2,5 min.
        Solo usa velas mayores YA CERRADAS en ese momento (sin mirar el futuro)."""
        bucket = bucket or self.bar * 5
        key = ("htf", bucket, fast, slow)
        if key not in self._cache:
            n = len(self.c)
            out = [0] * n
            kf, ks = 2.0 / (fast + 1), 2.0 / (slow + 1)
            ef = es = None
            cnt = 0
            for i in range(n):
                if (self.t[i] + self.bar) % bucket == 0:     # esta vela cierra una vela mayor
                    cl = self.c[i]
                    ef = cl if ef is None else cl * kf + ef * (1 - kf)
                    es = cl if es is None else cl * ks + es * (1 - ks)
                    cnt += 1
                out[i] = (1 if ef > es else -1 if ef < es else 0) if cnt >= slow else 0
            self._cache[key] = out
        return self._cache[key]

    def range_ratio(self, period=50):
        """Rango (máx-mín) de la vela dividido por el rango medio de las `period` anteriores."""
        key = ("rr", period)
        if key not in self._cache:
            n = len(self.c)
            rg = [self.h[i] - self.l[i] for i in range(n)]
            out = [None] * n
            acc = sum(rg[:period])
            for i in range(period, n):
                avg = acc / period
                out[i] = rg[i] / avg if avg > 0 else None
                acc += rg[i] - rg[i - period]
            self._cache[key] = out
        return self._cache[key]

    def retstd(self, period=20):
        """Desviación estándar móvil de los retornos de 1 vela."""
        key = ("rs", period)
        if key not in self._cache:
            n = len(self.c)
            r = [0.0] + [self.c[i] / self.c[i - 1] - 1.0 for i in range(1, n)]
            out = [None] * n
            for i in range(period, n):
                w = r[i - period + 1:i + 1]
                m = sum(w) / period
                out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / period)
            self._cache[key] = out
        return self._cache[key]


# ------------------------------------------------------------------ estrategias técnicas
def signals(data, strategy, p):
    """'call' / 'put' / None por vela (solo con datos hasta esa vela, sin mirar el futuro). No sirve para «prob»."""
    if strategy == "retrace":
        return retrace_signals(data, p)
    if strategy == "structure":
        return structure_signals(data, p)
    n = len(data.c)
    out = [None] * n
    rs, (mid, std) = data.rsi(p.rsi_period), data.bb(p.bb_period)
    ef, es = data.ema(p.ema_fast), data.ema(p.ema_slow)
    tf, ts = data.ema(p.trend_fast), data.ema(p.trend_slow)
    hh, ll = data.donchian(p.dc_period)
    er = data.er(p.er_period) if strategy == "regime" else None
    for i in range(p.warmup(strategy), n):
        r, m, s, f, sl = rs[i], mid[i], std[i], ef[i], es[i]
        if None in (r, m, s, f, sl):
            continue
        c = data.c[i]
        up, lo = m + p.bb_k * s, m - p.bb_k * s
        if strategy == "reversion":
            if c < lo and r < p.rsi_lo:
                out[i] = "call"
            elif c > up and r > p.rsi_hi:
                out[i] = "put"
        elif strategy == "pullback":
            if tf[i] is None or ts[i] is None:
                continue
            if c < lo and r < p.rsi_lo and tf[i] > ts[i]:
                out[i] = "call"
            elif c > up and r > p.rsi_hi and tf[i] < ts[i]:
                out[i] = "put"
        elif strategy == "trend":
            if f > sl and c > f and 55 <= r <= p.rsi_hi:
                out[i] = "call"
            elif f < sl and c < f and p.rsi_lo <= r <= 45:
                out[i] = "put"
        elif strategy == "breakout":
            if hh[i] is None:
                continue
            if c > hh[i] and f > sl:
                out[i] = "call"
            elif c < ll[i] and f < sl:
                out[i] = "put"
        elif strategy == "regime":
            e = er[i]
            if e is None:
                continue
            slope = sl - es[i - 5]
            bull, bear = data.c[i] > data.o[i], data.c[i] < data.o[i]       # vela que gira a favor
            rmin, rmax = min(rs[i - 3], rs[i - 2], rs[i - 1]), max(rs[i - 3], rs[i - 2], rs[i - 1])
            if e >= p.er_trend and f > sl and slope > 0:          # tendencia alcista: compra cuando el retroceso GIRA
                if rmin <= p.pull_rsi and bull and r > rs[i - 1]:
                    out[i] = "call"
            elif e >= p.er_trend and f < sl and slope < 0:        # tendencia bajista: vende cuando el rebote GIRA
                if rmax >= 100.0 - p.pull_rsi and bear and r < rs[i - 1]:
                    out[i] = "put"
            elif e < p.er_trend and not p.range_skip:             # lateral: extremo de las bandas + vela de rechazo
                if rmin <= p.rsi_lo and data.c[i - 1] < mid[i - 1] - p.bb_k * std[i - 1] and bull:
                    out[i] = "call"
                elif rmax >= p.rsi_hi and data.c[i - 1] > mid[i - 1] + p.bb_k * std[i - 1] and bear:
                    out[i] = "put"
    return out


def regime_label(data, i, p):
    """('alcista'|'bajista'|'lateral', ER) en la vela i, con la misma regla que usa la estrategia Adaptativa."""
    e = data.er(p.er_period)[i]
    f, sl = data.ema(p.ema_fast)[i], data.ema(p.ema_slow)[i]
    if e is None or f is None or sl is None or i < 5 or data.ema(p.ema_slow)[i - 5] is None:
        return "sin datos", None
    slope = sl - data.ema(p.ema_slow)[i - 5]
    if e >= p.er_trend and f > sl and slope > 0:
        return "alcista", e
    if e >= p.er_trend and f < sl and slope < 0:
        return "bajista", e
    return "lateral", e


# ------------------------------------------------------------------ filtros de calidad de la entrada
FILTER_NAMES = {"mtf": "tendencia MTF", "htf": "5 min", "vol": "volatilidad", "pat": "patrones"}
VOL_K = 3.0   # una vela con rango > 3 veces el medio se considera un pico (noticia / movimiento anormal)


def filter_ok(data, i, action, flags, pat=None, m=1):
    """(ok, motivo) de una entrada de `m` velas en la vela i bajo los filtros activos.
    `pat(i)` -> P(sube) según la memoria de patrones (o None si esa situación tiene pocos antecedentes)."""
    if "mtf" in flags:
        ok, why = trend_gate(data, i, action, m)
        if not ok:
            return False, why
    if "htf" in flags:
        d = data.htf_dir()[i]
        name = htf_name(data.bar)
        if d == 0:
            return False, f"filtro {name}: aún sin tendencia mayor"
        if (action == "call") != (d > 0):
            return False, f"filtro {name}: la tendencia mayor es {'alcista' if d > 0 else 'bajista'}"
    if "vol" in flags:
        rr = data.range_ratio()[i]
        if rr is not None and rr > VOL_K:
            return False, f"filtro volatilidad: vela {rr:.1f}× más grande que lo normal"
    if "pat" in flags:
        if pat is None:
            return False, "filtro patrones: la memoria de patrones aún no está lista"
        pu = pat(i)
        if pu is not None and ((action == "call" and pu < 0.5) or (action == "put" and pu > 0.5)):
            return False, (f"filtro patrones: en situaciones parecidas el precio {'bajó' if action == 'call' else 'subió'} "
                           f"el {max(pu, 1 - pu) * 100:.0f}% de las veces")
    return True, ""


def apply_filters(data, sig, flags, pat=None, m=1):
    """Devuelve las señales dejando solo las que pasan los filtros (lista nueva)."""
    if not flags:
        return sig
    return [None if (a is None or not filter_ok(data, i, a, flags, pat, m)[0]) else a for i, a in enumerate(sig)]


def htf_name(bar=60):
    s = bar * 5
    return f"{s // 60} min" if s % 60 == 0 else f"{s / 60:.1f} min".replace(".", ",")


FILTER_SHORT = {"mtf": "MTF", "vol": "vol.", "pat": "patr."}


def flags_label(flags, bar=60, short=False):
    if not flags:
        return "sin filtros"
    names = FILTER_SHORT if short else FILTER_NAMES
    return "+".join(htf_name(bar).replace(" ", "") if f == "htf" else names.get(f, f) for f in flags)


# ------------------------------------------------------------------ contexto multi-temporalidad (v14)
# Todo se calcula vela a vela SIN mirar el futuro: en la vela base i solo se usan las velas mayores ya cerradas y la que
# se está formando con el precio de cierre de i (exactamente lo que muestra el gráfico de esa temporalidad en ese instante).
MTF_TFS = (60, 300, 900, 3600)                         # 1 min, 5 min, 15 min y 1 h
MTF_W = {60: 0.15, 300: 0.30, 900: 0.30, 3600: 0.25}   # peso de cada temporalidad en la lectura única
MTF_MAJOR = {900: 0.55, 3600: 0.45}                    # tendencia de fondo (15 min + 1 h)
TF_NAME = {60: "1m", 300: "5m", 900: "15m", 3600: "1h"}
TREND_S = 0.25        # |lectura| desde aquí = hay tendencia (alcista / bajista)
TREND_MAJOR = 0.40    # aunque la lectura sea baja, si 15m + 1h marcan esto no se opera en su contra
GUIDE_MIN = -0.35     # la temporalidad que «manda» en una duración no puede ir en contra más que esto
ROOM_K = 1.0          # espacio mínimo hasta el nivel: ROOM_K × ATR de la vela base × √(duración en velas)
EXT_K = 4.0           # «estirado»: cierre a más de EXT_K ATR de la EMA21 de la vela base (o RSI ≥ 85 / ≤ 15)
IMP_MIN = 3.0         # impulso mínimo (en ATR de la vela base) para hablar de retroceso
PB_WINDOW = 60        # velas base en las que se busca el último impulso
FIB_LO, FIB_HI = 0.236, 0.786
LVL_TFS = (900, 3600)
LVL_LOOK = {900: 12 * 3600, 3600: 48 * 3600}           # antigüedad máxima de los niveles de 15 min y 1 h
LVL_TOUCH = 0.5       # el retroceso «rebota en el nivel» si su extremo quedó a menos de 0,5 ATR de él
PHASES = ("impulso", "consolidación", "estirado", "retroceso en curso", "retroceso agotado", "rota", "sin impulso")
BLOCK_PHASES = {"estirado", "retroceso en curso", "rota"}


def _tf_scores(data, tf, fast=8, slow=21, atr_n=14, er_n=10):
    """Puntuación de tendencia de la temporalidad `tf` (-1 bajista … +1 alcista) para cada vela base, o None sin datos.
    Mitad: separación EMA8–EMA21 medida en rangos medios de esa temporalidad. Mitad: eficiencia con signo de sus últimas
    10 velas (cuánto del recorrido fue en una sola dirección)."""
    n, bar = len(data.c), data.bar
    out = [None] * n
    if tf < bar or tf % bar:
        return out
    kf, ks = 2.0 / (fast + 1), 2.0 / (slow + 1)
    closes, ranges = [], []        # velas mayores ya cerradas
    ef = es = None                 # EMAs tras la última vela mayor cerrada
    cur = ch = cl = None
    ef_t = es_t = last_c = None
    T, H, L, C = data.t, data.h, data.l, data.c
    tanh = math.tanh
    for i in range(n):
        b = T[i] // tf
        if b != cur:
            if cur is not None:                        # la vela mayor anterior quedó cerrada
                closes.append(last_c)
                ranges.append(ch - cl)
                ef, es = ef_t, es_t
            cur, ch, cl = b, H[i], L[i]
        else:
            if H[i] > ch:
                ch = H[i]
            if L[i] < cl:
                cl = L[i]
        c = C[i]
        ef_t = c if ef is None else c * kf + ef * (1.0 - kf)
        es_t = c if es is None else c * ks + es * (1.0 - ks)
        last_c = c
        nb = len(closes)
        if nb < slow:
            continue
        atr = sum(ranges[-atr_n:]) / atr_n
        if atr <= 0:
            continue
        seq = closes[-er_n:]
        path = abs(c - seq[-1])
        for k in range(1, er_n):
            path += abs(seq[k] - seq[k - 1])
        ers = (c - seq[0]) / path if path > 0 else 0.0
        out[i] = 0.5 * tanh((ef_t - es_t) / atr) + 0.5 * ers
    return out


def _tf_bars(data, tf):
    """Velas de la temporalidad `tf` armadas con las velas base: [(inicio, máx, mín, índice base en que quedó cerrada)].
    El índice es None para la última si aún se está formando."""
    n, bar = len(data.c), data.bar
    out = []
    cur = None
    for i in range(n):
        b = data.t[i] // tf
        if b != cur:
            if out and out[-1][3] is None:
                out[-1][3] = i              # le faltaba su última vela (hueco): se sabe cerrada al empezar la siguiente
            cur = b
            out.append([b * tf, data.h[i], data.l[i], None])
        else:
            o = out[-1]
            if data.h[i] > o[1]:
                o[1] = data.h[i]
            if data.l[i] < o[2]:
                o[2] = data.l[i]
        if data.t[i] + bar >= (b + 1) * tf:              # su última vela base: queda cerrada en este cierre
            out[-1][3] = i
    return out


class MTFContext:
    """Lectura de tendencia, fase del movimiento y niveles para cada vela de una serie. Se calcula una vez por serie."""

    def __init__(self, data):
        self.data = data
        n = len(data.c)
        self.tfs = [tf for tf in MTF_TFS if tf >= data.bar and tf % data.bar == 0]
        self.sc = {tf: _tf_scores(data, tf) for tf in self.tfs}
        self.S, self.M, self.D, self.F = [None] * n, [0.0] * n, [0] * n, [0.0] * n
        cols = [(MTF_W[tf], self.sc[tf]) for tf in self.tfs]
        majors = [(MTF_MAJOR[tf], self.sc[tf]) for tf in LVL_TFS if tf in self.sc]
        for i in range(n):
            ws = vs = 0.0
            for w, col in cols:
                v = col[i]
                if v is not None:
                    ws += w
                    vs += w * v
            if ws < 0.5:                 # sin 15 min como mínimo no hay lectura fiable
                continue
            s = vs / ws
            wm = vm = 0.0
            for w, col in majors:
                v = col[i]
                if v is not None:
                    wm += w
                    vm += w * v
            mj = vm / wm if wm else 0.0
            self.S[i], self.M[i] = s, mj
            if s >= TREND_S:
                self.D[i], self.F[i] = 1, s
            elif s <= -TREND_S:
                self.D[i], self.F[i] = -1, -s
            elif abs(mj) >= TREND_MAJOR:
                self.D[i], self.F[i] = (1 if mj > 0 else -1), abs(mj)
            else:
                self.F[i] = abs(s)
        # ATR de la vela base (rango verdadero medio de 14 velas)
        atr = [None] * n
        acc, win = 0.0, []
        for i in range(n):
            tr = data.h[i] - data.l[i]
            if i:
                tr = max(tr, abs(data.h[i] - data.c[i - 1]), abs(data.l[i] - data.c[i - 1]))
            win.append(tr)
            acc += tr
            if len(win) > 14:
                acc -= win.pop(0)
            if len(win) == 14 and acc > 0:
                atr[i] = acc / 14
        self.atr = atr
        self.e21 = data.ema(21)
        self.rsi = data.rsi(14)
        self._neg = None
        self._ph, self._lv, self._gate = {}, {}, {}
        self._sw = None

    # ---------------------------------------------------------- tendencia
    def ready(self, i):
        return self.S[i] is not None

    def label(self, i):
        d = self.D[i]
        return "alcista" if d > 0 else "bajista" if d < 0 else "lateral"

    def arrows(self, i):
        out = []
        for tf in self.tfs:
            v = self.sc[tf][i]
            a = "·" if v is None else "▲" if v >= 0.15 else "▼" if v <= -0.15 else "="
            out.append(f"{TF_NAME[tf]}{a}")
        return " ".join(out)

    # ---------------------------------------------------------- fase del movimiento
    def _arrays(self, d):
        dt = self.data
        if d > 0:
            return dt.h, dt.l, dt.c, dt.o, self.e21, self.rsi, False
        if self._neg is None:
            self._neg = ([-x for x in dt.l], [-x for x in dt.h], [-x for x in dt.c], [-x for x in dt.o],
                         [None if x is None else -x for x in self.e21],
                         [None if x is None else 100.0 - x for x in self.rsi])
        return self._neg + (True,)

    def phase(self, i, d):
        """(fase, profundidad del retroceso 0–1 o None, extremo del retroceso) respecto de una tendencia d (+1 / -1)."""
        key = (i, d)
        got = self._ph.get(key)
        if got is not None:
            return got
        res = self._phase(i, d)
        self._ph[key] = res
        return res

    def _phase(self, i, d):
        H, L, C, O, E, R, neg = self._arrays(d)
        atr = self.atr[i]
        if atr is None or i < 4 or E[i] is None or R[i] is None or R[i - 3] is None:
            return "sin impulso", None, None
        j0 = max(0, i - PB_WINDOW + 1)
        seg = H[j0:i + 1]
        mx = max(seg)
        top = j0 + len(seg) - 1 - seg[::-1].index(mx)          # el máximo más reciente del tramo
        lows = L[j0:top + 1]
        mn = min(lows)
        imp = mx - mn
        if imp < IMP_MIN * atr:
            return "sin impulso", None, None
        ext = (C[i] - E[i]) / atr
        hot = ext > EXT_K or R[i] >= 85.0
        if top == i:
            return ("estirado" if hot else "impulso"), 0.0, None
        after = L[top + 1:i + 1]
        pl = min(after)                                        # extremo del retroceso (el punto más bajo tras el máximo)
        depth = (mx - pl) / imp
        extreme = -pl if neg else pl
        if depth > FIB_HI:
            return "rota", depth, extreme
        cur = (mx - C[i]) / imp
        if top >= i - 1 and cur < FIB_LO:
            return ("estirado" if hot else "impulso"), depth, extreme
        if depth < FIB_LO:
            return ("estirado" if hot else "consolidación"), depth, extreme
        prev_low = min(L[top + 1:i]) if i - top > 1 else mx
        bear = C[i] < O[i]
        falling = bear and (L[i] < prev_low or C[i] < C[i - 1])          # la vela sigue a favor del retroceso
        rng = H[i] - L[i]
        rej = C[i] > O[i] or (rng > 0 and (min(O[i], C[i]) - L[i]) / rng >= 0.6)   # vela de rechazo
        rsi_turn = R[i] > R[i - 1] and min(R[i - 3], R[i - 2], R[i - 1], R[i]) <= 50.0
        mini_break = C[i] > max(H[i - 1], H[i - 2])          # rompe la mini línea de tendencia del retroceso
        if not falling and rej and (rsi_turn or mini_break):
            return "retroceso agotado", depth, extreme
        return "retroceso en curso", depth, extreme

    # ---------------------------------------------------------- soportes y resistencias de 15 min y 1 h
    def _swings(self):
        if self._sw is not None:
            return self._sw
        sw = []
        for tf in LVL_TFS:
            if tf not in self.sc:
                continue
            bars = _tf_bars(self.data, tf)
            for k in range(2, len(bars) - 2):
                conf = bars[k + 2][3]
                if conf is None:
                    continue
                t0, h, l = bars[k][0], bars[k][1], bars[k][2]
                hs = [bars[k + j][1] for j in (-2, -1, 1, 2)]
                ls = [bars[k + j][2] for j in (-2, -1, 1, 2)]
                if h > hs[0] and h > hs[1] and h >= hs[2] and h >= hs[3]:
                    sw.append((conf, t0, h, tf))
                if l < ls[0] and l < ls[1] and l <= ls[2] and l <= ls[3]:
                    sw.append((conf, t0, l, tf))
        sw.sort()
        self._sw = (sw, [s[0] for s in sw])
        return self._sw

    def levels(self, i):
        """[(nivel, temporalidad)] confirmados al cierre de la vela i (máximos y mínimos de giro de 15 min y 1 h)."""
        got = self._lv.get(i)
        if got is not None:
            return got
        sw, conf = self._swings()
        now = self.data.t[i]
        lim = max(LVL_LOOK.values())
        out = []
        k = bisect.bisect_right(conf, i) - 1
        while k >= 0:
            c_i, t0, v, tf = sw[k]
            if now - t0 > lim + 4 * 3600:       # lo anterior es aún más viejo (confirmado antes)
                break
            if now - t0 <= LVL_LOOK[tf]:
                out.append((v, tf))
            k -= 1
        self._lv[i] = out
        return out

    def room(self, i, d):
        """(espacio en ATR de la vela base, nivel, temporalidad) hasta el nivel más cercano en el sentido d."""
        atr = self.atr[i]
        c = self.data.c[i]
        best = None
        for v, tf in self.levels(i):
            dist = (v - c) * d
            if dist > 0 and (best is None or dist < best[0]):
                best = (dist, v, tf)
        if best is None or not atr:
            return math.inf, None, None
        return best[0] / atr, best[1], best[2]

    def bounce(self, i, d, extreme):
        """¿El extremo del retroceso tocó un soporte (d=+1) o resistencia (d=-1) de 15m/1h? -> temporalidad o None."""
        atr = self.atr[i]
        if extreme is None or not atr:
            return None
        c = self.data.c[i]
        for v, tf in self.levels(i):
            if (c - v) * d >= 0 and abs(v - extreme) <= LVL_TOUCH * atr:
                return tf
        return None


def mtf_context(data):
    ctx = data._cache.get("mtf")
    if ctx is None:
        ctx = data._cache["mtf"] = MTFContext(data)
    return ctx


def guide_tfs(seconds):
    """Temporalidades que «mandan» en una operación de esta duración (no puede ir contra ellas)."""
    if seconds <= 60:
        return (300,)
    if seconds <= 180:
        return (900,)
    return (900, 3600)


def _fmt_dur(seconds):
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"


def trend_gate(data, i, action, m=1):
    """(ok, motivo) de la regla de tendencia multi-temporalidad para una entrada de `m` velas en la vela i:
    1) nunca contra la tendencia (lateral claro = se permiten ambos sentidos);
    2) a favor, solo si el movimiento no está estirado, el retroceso no sigue en curso y la tendencia no está rota;
    3) la duración debe encajar: la temporalidad que manda en esa duración no puede ir en contra;
    4) debe quedar espacio hasta el próximo soporte/resistencia de 15 min / 1 h."""
    ctx = mtf_context(data)
    key = (i, action, m)
    got = ctx._gate.get(key)
    if got is None:
        got = ctx._gate[key] = _trend_gate(ctx, data, i, action, m)
    return got


def _trend_gate(ctx, data, i, action, m):
    if not ctx.ready(i):
        return False, "tendencia MTF: aún sin velas suficientes de 15 min / 1 h"
    d = 1 if action == "call" else -1
    D = ctx.D[i]
    if D and d != D:
        return False, f"va en contra de la tendencia {ctx.label(i)} ({ctx.F[i] * 100:.0f}%; {ctx.arrows(i)})"
    if D:
        ph, depth, _ = ctx.phase(i, D)
        if ph in BLOCK_PHASES:
            if ph == "rota":
                return False, f"el retroceso llegó al {depth * 100:.0f}% del impulso: tendencia rota"
            if ph == "estirado":
                return False, "el movimiento está estirado (lejos de su media): es donde suele empezar el retroceso"
            return False, f"el retroceso sigue en curso ({depth * 100:.0f}% del impulso, sin giro confirmado)"
    secs = m * data.bar
    for tf in guide_tfs(secs):
        col = ctx.sc.get(tf)
        v = col[i] if col else None
        if v is not None and v * d < GUIDE_MIN:
            return False, (f"una operación de {_fmt_dur(secs)} iría contra la tendencia de {TF_NAME[tf]}: "
                           f"para esa duración tiene que acompañar")
    room, lvl, ltf = ctx.room(i, d)
    need = ROOM_K * math.sqrt(max(1, m))          # movimiento típico en `m` velas base (ATR de la vela base × √m)
    if room < need:
        return False, (f"{'resistencia' if d > 0 else 'soporte'} de {TF_NAME[ltf]} a {room:.1f} ATR "
                       f"(se piden {need:.1f}): sin espacio para el movimiento")
    return True, ""


def apply_gate(data, sig, m):
    """Señales que pasan el filtro de tendencia multi-temporalidad (lista nueva)."""
    return [None if (a is None or not trend_gate(data, i, a, m)[0]) else a for i, a in enumerate(sig)]


def context_text(data, i, action=None):
    """Resumen legible del contexto en la vela i (para el registro y la tabla de decisiones)."""
    ctx = mtf_context(data)
    if not ctx.ready(i):
        return "tendencia: sin datos suficientes de 15m/1h"
    D = ctx.D[i]
    txt = f"tendencia {ctx.label(i)} {ctx.F[i] * 100:.0f}% ({ctx.arrows(i)})"
    if D:
        ph, depth, ext = ctx.phase(i, D)
        txt += f" · {ph}" + (f" {depth * 100:.0f}%" if depth else "")
        if ph == "retroceso agotado":
            b = ctx.bounce(i, D, ext)
            if b:
                txt += f" en {'soporte' if D > 0 else 'resistencia'} de {TF_NAME[b]}"
    d = (1 if action == "call" else -1) if action else (D or 1)
    room, lvl, ltf = ctx.room(i, d)
    if lvl is not None:
        txt += f" · {'resistencia' if d > 0 else 'soporte'} {TF_NAME[ltf]} a {room:.1f} ATR"
    return txt


GOLD_LO, GOLD_HI = 0.50, 0.618      # zona dorada de Fibonacci


def structure_at(data, i, p):
    """(acción|None, motivo) de la estrategia «Estructura»: tendencia detectada + impulso + retroceso que toca la zona
    dorada (50–61,8%) sin pasarla + vela de rechazo en la zona. Solo usa velas cerradas hasta i."""
    ctx = mtf_context(data)
    if not ctx.ready(i):
        return None, "sin lectura de tendencia (faltan velas de 15m/1h)"
    D = ctx.D[i]
    if not D:
        return None, "mercado lateral: no hay estructura de tendencia"
    H, L, C, O, E, R, neg = ctx._arrays(D)
    atr = ctx.atr[i]
    if atr is None or i < 4 or R[i] is None or R[i - 3] is None:
        return None, "pocas velas para leer la estructura"
    j0 = max(0, i - PB_WINDOW + 1)
    seg = H[j0:i + 1]
    mx = max(seg)
    top = j0 + len(seg) - 1 - seg[::-1].index(mx)          # extremo del impulso (el más reciente)
    mn = min(L[j0:top + 1])
    imp = mx - mn
    word = "alcista" if D > 0 else "bajista"
    if imp < p.st_imp * atr:
        return None, f"tendencia {word}, pero sin un impulso claro (mide {imp / atr:.1f} ATR; se piden {p.st_imp:g})"
    if top >= i:
        return None, f"tendencia {word}: el impulso sigue en curso — se espera el retroceso"
    after = L[top + 1:i + 1]
    pl = min(after)
    k_pl = top + 1 + len(after) - 1 - after[::-1].index(pl)   # vela donde el retroceso llegó más lejos
    depth = (mx - pl) / imp
    if depth < GOLD_LO:
        return None, f"retroceso al {depth * 100:.0f}%: aún no llega a la zona dorada (50–61,8%)"
    if depth > GOLD_HI:
        return None, f"el retroceso pasó la zona dorada ({depth * 100:.0f}% del impulso): no se opera"
    if i - k_pl > p.st_touch:
        return None, f"tocó la zona dorada hace {i - k_pl} velas: la reacción ya pasó"
    prev_low = min(L[top + 1:i]) if i - top > 1 else mx
    falling = C[i] < O[i] and (L[i] < prev_low or C[i] < C[i - 1])
    rng = H[i] - L[i]
    rej = C[i] > O[i] or (rng > 0 and (min(O[i], C[i]) - L[i]) / rng >= 0.6)
    rsi_turn = R[i] > R[i - 1] and min(R[i - 3], R[i - 2], R[i - 1], R[i]) <= 50.0
    mini_break = C[i] > max(H[i - 1], H[i - 2])
    if falling or not rej or not (rsi_turn or mini_break):
        return None, f"retroceso en la zona dorada ({depth * 100:.0f}%), esperando la vela de rechazo"
    if p.st_room > 0:
        room = ctx.room(i, D)[0]
        if room < p.st_room:
            return None, f"rechazo en la zona dorada, pero solo {room:.1f} ATR hasta el próximo nivel"
    act = "call" if D > 0 else "put"
    return act, (f"tendencia {word} · retroceso en la zona dorada ({depth * 100:.0f}% del impulso) · vela de rechazo"
                 + (" + ruptura de la mini estructura" if mini_break else " + giro del RSI"))


def structure_signals(data, p):
    ctx = mtf_context(data)
    n = len(data.c)
    out = [None] * n
    for i in range(p.warmup("structure"), n):
        if ctx.D[i]:
            out[i] = structure_at(data, i, p)[0]
    return out


def retrace_at(data, i, p):
    """(acción|None, motivo) de la estrategia «Retroceso MTF» en la vela i."""
    ctx = mtf_context(data)
    if not ctx.ready(i):
        return None, "sin lectura de tendencia (faltan velas de 15m/1h)"
    D = ctx.D[i]
    if not D:
        return None, "mercado lateral: no hay tendencia que seguir"
    if ctx.F[i] < p.rt_min:
        return None, f"tendencia {ctx.label(i)} débil ({ctx.F[i] * 100:.0f}%; se pide {p.rt_min * 100:.0f}%)"
    ph, depth, ext = ctx.phase(i, D)
    if ph != "retroceso agotado":
        return None, f"tendencia {ctx.label(i)}: {ph}" + (f" ({depth * 100:.0f}%)" if depth else "") + \
            (" — se espera a que se agote" if ph == "retroceso en curso" else " — se espera un retroceso")
    if not (p.rt_lo <= depth <= p.rt_hi):
        return None, (f"retroceso agotado al {depth * 100:.0f}%, fuera de la zona {p.rt_lo * 100:.0f}–"
                      f"{p.rt_hi * 100:.0f}%")
    room = ctx.room(i, D)[0]
    if room < p.rt_room:
        return None, f"retroceso agotado, pero solo {room:.1f} ATR hasta el próximo nivel (se piden {p.rt_room:g})"
    b = ctx.bounce(i, D, ext)
    if p.rt_level and not b:
        return None, "retroceso agotado, pero no rebotó en un soporte/resistencia de 15m/1h"
    act = "call" if D > 0 else "put"
    return act, (f"retroceso agotado al {depth * 100:.0f}% del impulso" + (f", rebote en nivel de {TF_NAME[b]}" if b else "")
                 + (f", espacio {room:.1f} ATR" if room != math.inf else ", sin niveles cerca"))


def retrace_signals(data, p):
    """Estrategia «Retroceso MTF»: a favor de la tendencia, solo cuando el retroceso se agotó en la zona de Fibonacci
    elegida y queda espacio hasta el próximo nivel."""
    ctx = mtf_context(data)
    n = len(data.c)
    out = [None] * n
    for i in range(p.warmup("retrace"), n):
        D = ctx.D[i]
        if not D or ctx.F[i] < p.rt_min:          # atajo (lo mismo que comprueba retrace_at)
            continue
        out[i] = retrace_at(data, i, p)[0]
    return out


# ------------------------------------------------------------------ desglose por contexto (para verificar con datos)
BD_GROUPS = ("align", "phase", "room", "gate")
BD_LABELS = {"favor": "A favor de la tendencia", "contra": "En contra de la tendencia", "lateral": "Mercado lateral",
             "nodata": "Sin lectura (pocas velas)", "space": "Con espacio hasta el nivel", "nospace": "Pegada a un nivel en contra",
             "pass": "Pasan el filtro MTF", "block": "Las bloquearía el filtro MTF"}


def context_breakdown(data, sig, idx, seq, m):
    """Clasifica operaciones ya simuladas (`idx` = vela de entrada, `seq` = 'W'/'L'/'T') por su contexto:
    a favor / en contra / lateral, fase del retroceso (solo las a favor), espacio hasta el nivel y si las dejaría pasar
    el filtro MTF. Devuelve {grupo: {cubo: [ganadas, perdidas]}}."""
    ctx = mtf_context(data)
    out = {g: {} for g in BD_GROUPS}
    need = ROOM_K * math.sqrt(max(1, m))

    def add(g, k, won):
        b = out[g].setdefault(k, [0, 0])
        b[0 if won else 1] += 1

    for i, o in zip(idx, seq):
        if o == "T":
            continue
        won = o == "W"
        a = sig[i]
        if a is None:
            continue
        if not ctx.ready(i):
            add("align", "nodata", won)
            continue
        d = 1 if a == "call" else -1
        D = ctx.D[i]
        add("align", "lateral" if not D else "favor" if d == D else "contra", won)
        if D and d == D:
            add("phase", ctx.phase(i, D)[0], won)
        add("room", "space" if ctx.room(i, d)[0] >= need else "nospace", won)
        add("gate", "pass" if trend_gate(data, i, a, m)[0] else "block", won)
    return out


def merge_breakdowns(items):
    out = {g: {} for g in BD_GROUPS}
    for bd in items:
        for g, cubes in (bd or {}).items():
            for k, (w, l) in cubes.items():
                b = out.setdefault(g, {}).setdefault(k, [0, 0])
                b[0] += w
                b[1] += l
    return out


def bd_label(key):
    return BD_LABELS.get(key) or (key[:1].upper() + key[1:])


# ------------------------------------------------------------------ memoria de patrones históricos
# Describe cada vela cerrada con cuatro «resoluciones» (de la más fina a la más general) y cuenta qué hizo el precio
# `minutes` velas después cada vez que se vio esa misma descripción. La probabilidad se estima por niveles: la de la
# situación exacta se «encoge» hacia la de su secuencia de velas, y esta hacia la general. Así una situación vista
# pocas veces no engaña (pesa más lo que se sabe de casos parecidos con muchos antecedentes).
PAT_LEVELS = 4
_ARROW = {1: "▲", -1: "▼", 0: "·"}
_BBZ = ("bajo la banda", "mitad baja", "mitad alta", "sobre la banda")
_RZ = ("RSI bajo", "RSI medio", "RSI alto")
_REG = ("lateral", "alcista", "bajista")
_WK = ("", " · mecha arriba", " · mecha abajo")


def pattern_keys(data, p):
    """Para cada vela: (clave fina, clave media, clave gruesa) o None mientras falten datos (sin mirar el futuro)."""
    ck = ("patk", p.rsi_period, p.bb_period, p.bb_k, p.er_period, p.er_trend, p.ema_fast, p.ema_slow)
    if ck in data._cache:
        return data._cache[ck]
    c, o, h, l = data.c, data.o, data.h, data.l
    n = len(c)
    rs, (mid, std) = data.rsi(p.rsi_period), data.bb(p.bb_period)
    er, ef, es = data.er(p.er_period), data.ema(p.ema_fast), data.ema(p.ema_slow)
    out = [None] * n
    for i in range(max(p.warmup("pattern"), 6), n):
        r_, m_, s_, e_, f_, sl = rs[i], mid[i], std[i], er[i], ef[i], es[i]
        if r_ is None or m_ is None or s_ is None or e_ is None or f_ is None or sl is None or es[i - 5] is None:
            continue
        d0 = 1 if c[i] > o[i] else -1 if c[i] < o[i] else 0
        d1 = 1 if c[i - 1] > o[i - 1] else -1 if c[i - 1] < o[i - 1] else 0
        d2 = 1 if c[i - 2] > o[i - 2] else -1 if c[i - 2] < o[i - 2] else 0
        up, lo = m_ + p.bb_k * s_, m_ - p.bb_k * s_
        bbz = 0 if c[i] < lo else 1 if c[i] < m_ else 2 if c[i] <= up else 3
        rz = 0 if r_ < 35 else 2 if r_ > 65 else 1
        slope = sl - es[i - 5]
        reg = 1 if (e_ >= p.er_trend and f_ > sl and slope > 0) else 2 if (e_ >= p.er_trend and f_ < sl and slope < 0) else 0
        rng = h[i] - l[i]
        wk = 0
        if rng > 0:
            if (h[i] - max(o[i], c[i])) / rng > 0.5:
                wk = 1
            elif (min(o[i], c[i]) - l[i]) / rng > 0.5:
                wk = 2
        out[i] = ((d0, d1, d2, bbz, rz, reg, wk), (d0, d1, d2, bbz, reg), (d0, d1, d2), (d0,))
    data._cache[ck] = out
    return out


def describe_pattern(k):
    if not k:
        return "sin datos"
    d0, d1, d2, bbz, rz, reg, wk = k[0]
    return f"velas {_ARROW[d2]}{_ARROW[d1]}{_ARROW[d0]} · {_BBZ[bbz]} · {_RZ[rz]} · {_REG[reg]}{_WK[wk]}"


def _pat_estimate(counts, k, g, p):
    """(P(sube), casos de la situación exacta, casos de su secuencia de velas) o None si la secuencia tiene menos de
    `pat_min` antecedentes. Estimación jerárquica: general -> última vela -> secuencia -> situación (hasta `pat_level`)."""
    seq = counts[2].get(k[2])
    if not seq or seq[0] + seq[1] < p.pat_min:
        return None
    est = g
    finest = max(0, min(2, p.pat_level))
    n_exact = 0
    for lvl in range(PAT_LEVELS - 1, finest - 1, -1):
        ud = counts[lvl].get(k[lvl])
        if ud:
            n = ud[0] + ud[1]
            est = (ud[0] + p.pat_k * est) / (n + p.pat_k)
            n_exact = n
        else:
            n_exact = 0
    return est, n_exact, seq[0] + seq[1]


def pattern_signals(data, p, minutes, payout):
    """Señales de la memoria de patrones con ventana CRECIENTE: la decisión en la vela i solo usa resultados ya
    conocidos al cierre de i (velas j con j + minutes <= i). Es exactamente lo que el bot habría sabido en ese momento.
    Devuelve (señales, P(sube) estimada por vela)."""
    ck = ("patsig", minutes, round(payout, 4), p.pat_min, p.pat_k, p.pat_level, p.prob_margin,
          p.rsi_period, p.bb_period, p.bb_k, p.er_period, p.er_trend, p.ema_fast, p.ema_slow)
    if ck in data._cache:
        return data._cache[ck]
    keys = pattern_keys(data, p)
    c, n = data.c, len(data.c)
    counts = [dict() for _ in range(PAT_LEVELS)]
    ups = downs = 0
    thr = breakeven(payout) + p.prob_margin
    sig, probs = [None] * n, [None] * n
    for i in range(n):
        j = i - minutes
        if j >= 0:
            kj = keys[j]
            if kj is not None and c[i] != c[j]:
                slot = 0 if c[i] > c[j] else 1
                for lvl in range(PAT_LEVELS):
                    d = counts[lvl].get(kj[lvl])
                    if d is None:
                        counts[lvl][kj[lvl]] = d = [0, 0]
                    d[slot] += 1
                if slot == 0:
                    ups += 1
                else:
                    downs += 1
        k = keys[i]
        if k is None or ups + downs < 50:
            continue
        est = _pat_estimate(counts, k, ups / (ups + downs), p)
        if est is None:
            continue
        pu = est[0]
        probs[i] = pu
        if pu >= thr:
            sig[i] = "call"
        elif 1 - pu >= thr:
            sig[i] = "put"
    data._cache[ck] = (sig, probs)
    return sig, probs


@dataclass
class PatternModel:
    """Memoria de patrones construida con un tramo del histórico (para operar en vivo)."""
    counts: list
    ups: int
    downs: int
    minutes: int
    n_train: int
    params: Params
    trained_at: float = 0.0
    vote_params: dict = None      # compatibilidad con ProbModel (no se usa)

    @property
    def base_rate(self):
        return self.ups / max(1, self.ups + self.downs)

    def estimate(self, k):
        if k is None or self.ups + self.downs < 50:
            return None
        return _pat_estimate(self.counts, k, self.base_rate, self.params)


def build_pattern_model(data, p, minutes, start=0, end=None):
    """Cuenta, en las velas [start, end), qué hizo el precio `minutes` velas después de cada situación."""
    keys = pattern_keys(data, p)
    c, n = data.c, len(data.c)
    end = n if end is None else min(end, n)
    counts = [dict() for _ in range(PAT_LEVELS)]
    ups = downs = 0
    for j in range(max(0, start), end - minutes):
        kj = keys[j]
        if kj is None or c[j + minutes] == c[j]:
            continue
        slot = 0 if c[j + minutes] > c[j] else 1
        for lvl in range(PAT_LEVELS):
            d = counts[lvl].get(kj[lvl])
            if d is None:
                counts[lvl][kj[lvl]] = d = [0, 0]
            d[slot] += 1
        if slot == 0:
            ups += 1
        else:
            downs += 1
    if ups + downs < 200:
        return None
    return PatternModel(counts, ups, downs, minutes, ups + downs, p)


def model_prob_fn(data, pat_model):
    """Función i -> P(sube) según una memoria de patrones ya construida (para el filtro «pat» en vivo)."""
    pk = pattern_keys(data, pat_model.params)

    def fn(i):
        est = pat_model.estimate(pk[i])
        return est[0] if est else None
    return fn


def pattern_filter_fn(data, minutes, payout):
    """Función i -> P(sube) de la memoria de patrones (parámetros por defecto, ventana creciente) para el filtro «pat»."""
    probs = pattern_signals(data, Params(), minutes, payout)[1]
    return lambda i: probs[i]


# ------------------------------------------------------------------ modelo probabilístico
FEATURE_NAMES = ["ret1", "ret3", "ret5", "ret10", "rsi", "bbz", "emaDiff", "body", "upWick", "loWick",
                 "vol", "horaSin", "horaCos", "er", "er_bbz", "er_rsi", "er_ema"]
VOTE_NAMES = ["voto_reversion", "voto_tendencia", "voto_pullback", "voto_ruptura", "voto_regimen"]


def _clip(x, lim):
    return max(-lim, min(lim, x))


def default_vote_params(p=None):
    return {k: (p or Params()) for k in TECH}


def feature_matrix(data, p, vote_params=None):
    """Una fila de características por vela (None mientras no haya datos suficientes).
    Con `vote_params` ({estrategia: Params}) se añaden los 4 votos de las estrategias técnicas (+1 compra, -1 venta, 0 nada)."""
    vsig = None if vote_params is None else tuple((k, tuple(sorted(vote_params[k].__dict__.items()))) for k in TECH)
    key = ("feat", p.rsi_period, p.bb_period, p.ema_fast, p.ema_slow, vsig)
    if key in data._cache:
        return data._cache[key]
    votes = None
    if vote_params is not None:
        votes = []
        for k in TECH:
            sg = signals(data, k, vote_params[k])
            votes.append([1.0 if x == "call" else -1.0 if x == "put" else 0.0 for x in sg])
    n = len(data.c)
    rs, (mid, std) = data.rsi(p.rsi_period), data.bb(p.bb_period)
    ef, es, vol = data.ema(p.ema_fast), data.ema(p.ema_slow), data.retstd(20)
    erv = data.er(p.er_period)
    # media móvil de 50 velas de la volatilidad
    vavg = [None] * n
    acc, win = 0.0, []
    for i in range(n):
        if vol[i] is not None:
            win.append(vol[i])
            acc += vol[i]
            if len(win) > 50:
                acc -= win.pop(0)
            if len(win) >= 20:
                vavg[i] = acc / len(win)
    out = [None] * n
    c, o, h, l = data.c, data.o, data.h, data.l
    first = max(60, p.warmup("ensemble" if votes else None))
    for i in range(first, n):
        v = vol[i]
        if None in (rs[i], mid[i], std[i], ef[i], es[i], v, vavg[i], erv[i]) or v <= 0 or std[i] <= 0 or vavg[i] <= 0:
            continue
        bz, rz, ed = _clip((c[i] - mid[i]) / std[i], 4), (rs[i] - 50.0) / 25.0, _clip((ef[i] - es[i]) / std[i], 4)
        rng = h[i] - l[i]
        body = (c[i] - o[i]) / rng if rng > 0 else 0.0
        upw = (h[i] - max(o[i], c[i])) / rng if rng > 0 else 0.0
        low = (min(o[i], c[i]) - l[i]) / rng if rng > 0 else 0.0
        hour = (data.t[i] // 3600) % 24
        out[i] = [
            _clip((c[i] / c[i - 1] - 1) / v, 5), _clip((c[i] / c[i - 3] - 1) / (v * 3 ** 0.5), 5),
            _clip((c[i] / c[i - 5] - 1) / (v * 5 ** 0.5), 5), _clip((c[i] / c[i - 10] - 1) / (v * 10 ** 0.5), 5),
            (rs[i] - 50.0) / 25.0, _clip((c[i] - mid[i]) / std[i], 4), _clip((ef[i] - es[i]) / std[i], 4),
            body, upw, low, _clip(v / vavg[i] - 1.0, 3),
            math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
            erv[i], erv[i] * bz, erv[i] * rz, erv[i] * ed,       # el modelo aprende qué hacer según el régimen (ER)
        ]
        if votes:
            out[i] += [votes[j][i] for j in range(len(TECH))]
    data._cache[key] = out
    return out


def _solve(A, b):
    """Resuelve A x = b (eliminación gaussiana con pivoteo parcial)."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            continue
        M[col], M[piv] = M[piv], M[col]
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            if f:
                for k in range(col, n + 1):
                    M[r][k] -= f * M[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))
        x[r] = s / M[r][r] if abs(M[r][r]) > 1e-12 else 0.0
    return x


@dataclass
class ProbModel:
    w: list            # [sesgo, pesos...]
    mu: list
    sd: list
    minutes: int
    n_train: int
    base_rate: float   # % de veces que subió en el entrenamiento
    trained_at: float = 0.0
    vote_params: dict = None   # None = modelo probabilístico simple; dict = Conjunto (con los votos técnicos)

    def weights(self):
        """Peso (en desviaciones estándar) de cada característica; sirve para ver qué estrategia pesa más."""
        names = FEATURE_NAMES + (VOTE_NAMES if self.vote_params else [])
        return dict(zip(names, self.w[1:]))

    def p_up(self, row):
        z = self.w[0]
        for j, x in enumerate(row):
            z += self.w[j + 1] * (x - self.mu[j]) / self.sd[j]
        z = _clip(z, 30)
        return 1.0 / (1.0 + math.exp(-z))


def fit_prob(data, p, minutes, start, end, l2=10.0, iters=12, vote_params=None):
    """Regresión logística (Newton con regularización L2). Entrena con velas [start, end); la etiqueta de la
    vela i es «¿el cierre de i+minutes quedó sobre el de i?», así que nunca usa datos posteriores a `end`."""
    F = feature_matrix(data, p, vote_params)
    X, y = [], []
    for i in range(max(start, 0), min(end, len(data.c)) - minutes):
        if F[i] is None:
            continue
        a, b = data.c[i], data.c[i + minutes]
        if a == b:
            continue
        X.append(F[i])
        y.append(1.0 if b > a else 0.0)
    if len(X) < 200:
        return None
    d = len(X[0])
    n = len(X)
    mu = [sum(r[j] for r in X) / n for j in range(d)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / n) or 1.0 for j in range(d)]
    Z = [[(r[j] - mu[j]) / sd[j] for j in range(d)] for r in X]
    w = [0.0] * (d + 1)
    for _ in range(iters):
        grad = [0.0] * (d + 1)
        H = [[0.0] * (d + 1) for _ in range(d + 1)]
        for z, yi in zip(Z, y):
            s = w[0]
            for j in range(d):
                s += w[j + 1] * z[j]
            pr = 1.0 / (1.0 + math.exp(-_clip(s, 30)))
            g, hs = pr - yi, pr * (1 - pr)
            row = [1.0] + z
            for a in range(d + 1):
                ra = row[a]
                grad[a] += g * ra
                Ha = H[a]
                hr = hs * ra
                for b in range(a, d + 1):
                    Ha[b] += hr * row[b]
        for a in range(1, d + 1):
            grad[a] += l2 * w[a]
            H[a][a] += l2
        for a in range(d + 1):
            for b in range(a):
                H[a][b] = H[b][a]
        step = _solve(H, grad)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s_) for s_ in step) < 1e-5:
            break
    return ProbModel(w, mu, sd, minutes, n, sum(y) / n, vote_params=vote_params)


def prob_signals(data, p, model, payout):
    """Señales del modelo: entra si P(ganar) >= equilibrio + margen. Devuelve (señales, probabilidades P(sube))."""
    F = feature_matrix(data, p, model.vote_params)
    thr = breakeven(payout) + p.prob_margin
    sig, probs = [None] * len(data.c), [None] * len(data.c)
    for i, row in enumerate(F):
        if row is None:
            continue
        pu = model.p_up(row)
        probs[i] = pu
        if pu >= thr:
            sig[i] = "call"
        elif 1 - pu >= thr:
            sig[i] = "put"
    return sig, probs


# ------------------------------------------------------------------ señal en vivo
LIVE_TAIL = 700   # velas con las que se calculan en vivo los indicadores de la vela base (la EMA200 ya converge)


class LiveView:
    """Velas cerradas de un mercado para decidir en vivo. El contexto de 1m–1h usa la serie completa; los indicadores de
    la vela base, solo la cola (mismos valores, mucho más rápido). Se reutiliza para probar varias duraciones."""

    def __init__(self, candles):
        self.candles = candles
        self.full = Data(candles)
        self.tail = Data(candles[-LIVE_TAIL:], bar=self.full.bar) if len(candles) > LIVE_TAIL else self.full
        self.i = len(candles) - 1

    def context(self, action=None):
        return context_text(self.full, self.i, action) if self.candles else ""


def live_decision(candles, key, p, model=None, payout=0.85, flags=(), pat_model=None, minutes=1, view=None):
    """Decisión para la última vela CERRADA. Devuelve (accion|None, texto, probabilidad_de_ganar|None).
    `pat_model` = memoria de patrones del mercado (necesaria si la estrategia usa el filtro «pat»).
    `minutes` = duración de la operación en velas (la usa el filtro de tendencia «mtf»).
    `view` = LiveView ya creada con estas velas (para evaluar varias duraciones sin recalcular)."""
    view = view or LiveView(candles)
    act, why, win = _live_raw(view, key, p, model, payout)
    ctx_txt = view.context(act)
    if act and flags:
        if "mtf" in flags:
            ok, why_g = trend_gate(view.full, view.i, act, minutes)
            if not ok:
                return None, f"{why} · BLOQUEADA ({why_g}) · {ctx_txt}", None
        rest = tuple(f for f in flags if f != "mtf")
        if rest:
            data = view.tail
            pat_fn = model_prob_fn(data, pat_model) if ("pat" in rest and pat_model is not None) else None
            ok, why_f = filter_ok(data, len(data.c) - 1, act, rest, pat_fn, minutes)
            if not ok:
                return None, f"{why} · BLOQUEADA ({why_f}) · {ctx_txt}", None
    return act, f"{why} · {ctx_txt}", win


def _live_raw(view, key, p, model, payout):
    candles = view.candles
    need = p.warmup(key) + 2
    if len(candles) < need:
        return None, f"pocas velas ({len(candles)}; se necesitan {need})", None
    if key == "retrace":
        act, why = retrace_at(view.full, view.i, p)
        return act, why, None
    if key == "structure":
        act, why = structure_at(view.full, view.i, p)
        return act, why, None
    data = view.tail
    i = len(data.c) - 1
    if key == "pattern":
        if model is None:
            return None, "memoria de patrones aún sin construir", None
        k = pattern_keys(data, model.params)[i]
        est = model.estimate(k)
        if est is None:
            return None, f"{describe_pattern(k)} · pocos antecedentes en el histórico", None
        pu, n_exact, n_seq = est
        thr = breakeven(payout) + p.prob_margin
        act = "call" if pu >= thr else "put" if 1 - pu >= thr else None
        win = pu if act == "call" else (1 - pu) if act == "put" else max(pu, 1 - pu)
        return act, (f"{describe_pattern(k)} · vista {n_exact} veces igual y {n_seq} con las mismas velas: "
                     f"P(sube) {pu * 100:.1f}% · se exige ≥ {thr * 100:.1f}% en un sentido"), win
    if key in ("prob", "ensemble"):
        if model is None:
            return None, "modelo aún sin entrenar", None
        row = feature_matrix(data, p, model.vote_params if key == "ensemble" else None)[i]
        if row is None:
            return None, "datos insuficientes para las características", None
        pu = model.p_up(row)
        thr = breakeven(payout) + p.prob_margin
        act = "call" if pu >= thr else "put" if 1 - pu >= thr else None
        win = pu if act == "call" else (1 - pu) if act == "put" else max(pu, 1 - pu)
        txt = f"P(sube) {100 * pu:.1f}% · se exige ≥ {100 * thr:.1f}% para entrar"
        if key == "ensemble" and model.vote_params:
            v = row[len(FEATURE_NAMES):]
            arrow = lambda x: "▲" if x > 0 else "▼" if x < 0 else "·"
            txt = ("Votos " + " ".join(f"{SHORT[k][:4]}{arrow(x)}" for k, x in zip(TECH, v)) + " · " + txt)
        reg, e_ = regime_label(data, i, p)
        return act, txt + (f" · régimen {reg}" if e_ is not None else ""), win
    act = signals(data, key, p)[i]
    mid, std = data.bb(p.bb_period)
    rs, ef, es = data.rsi(p.rsi_period)[i], data.ema(p.ema_fast)[i], data.ema(p.ema_slow)[i]
    pos = ("bajo la banda inferior" if data.c[i] < mid[i] - p.bb_k * std[i]
           else "sobre la banda superior" if data.c[i] > mid[i] + p.bb_k * std[i] else "dentro de las bandas")
    why = f"RSI {rs:.0f} · cierre {pos} · EMA{p.ema_fast} {'>' if ef > es else '<'} EMA{p.ema_slow}"
    if key == "pullback":
        tf, ts = data.ema(p.trend_fast)[i], data.ema(p.trend_slow)[i]
        why += f" · fondo {'alcista' if tf > ts else 'bajista'}"
    if key == "breakout":
        hh, ll = data.donchian(p.dc_period)
        why += f" · rango {p.dc_period}v [{ll[i]:.5g} – {hh[i]:.5g}]"
    reg, e_ = regime_label(data, i, p)
    if e_ is not None:
        why += f" · régimen {reg} (ER {e_:.2f})"
    return act, why, None


# ------------------------------------------------------------------ backtest
def _betacf(a, b, x, itmax=300, eps=3e-12):
    """Fracción continua de la beta incompleta (Numerical Recipes)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def beta_cdf(x, a, b):
    """P(X ≤ x) con X ~ Beta(a, b): probabilidad de que la efectividad real esté por debajo de x."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def wilson(wins, n, z=1.96):
    """Intervalo de confianza del 95% para una proporción."""
    if n == 0:
        return 0.0, 0.0
    ph = wins / n
    den = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def backtest(candles, strategy, p=None, minutes=1, payout=0.85, start=0, end=None, data=None, sig=None, probs=None):
    p = p or Params()
    data = data or Data(candles)
    if sig is None:
        sig = signals(data, strategy, p)
    n = len(data.c)
    end = n if end is None else min(end, n)
    i = max(start, p.warmup(strategy))
    wins = losses = ties = 0
    equity = peak = max_dd = streak = max_streak = 0.0
    psum, pn = 0.0, 0
    seq, idx = [], []
    while i < end - minutes:
        s = sig[i]
        if s is None:
            i += 1
            continue
        idx.append(i)
        entry, exit_ = data.c[i], data.c[i + minutes]
        if probs is not None and probs[i] is not None:
            psum += probs[i] if s == "call" else 1 - probs[i]
            pn += 1
        if exit_ == entry:
            ties += 1
            seq.append("T")
        else:
            win = exit_ > entry if s == "call" else exit_ < entry
            seq.append("W" if win else "L")
            if win:
                wins += 1
                equity += payout
                streak = 0
            else:
                losses += 1
                equity -= 1.0
                streak += 1
                max_streak = max(max_streak, streak)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        i += minutes  # sin operaciones superpuestas
    decided = wins + losses
    lo, hi = wilson(wins, decided)
    be = breakeven(payout)
    return {
        "trades": decided + ties, "wins": wins, "losses": losses, "ties": ties,
        "winrate": wins / decided if decided else 0.0, "ci_low": lo, "ci_high": hi, "breakeven": be,
        "net": equity, "expectancy": equity / decided if decided else 0.0,
        "max_dd": max_dd, "max_streak": int(max_streak),
        "profitable": decided >= MIN_TRADES and lo > be,
        "edge_low": (lo - be) if decided else -1.0,
        "avg_prob": (psum / pn) if pn else None, "seq": "".join(seq), "idx": idx,
    }


def simulate_mg(seq, payout, levels, mult=2.0, base=1.0):
    """Reproduce una secuencia real de resultados ('W'/'L'/'T') aplicando martingala de `levels` niveles
    (0 = sin martingala). Tras ganar o agotar los niveles vuelve al monto base. Devuelve métricas en la moneda de `base`.
    La efectividad por operación NO cambia: solo cambia cuánto se arriesga después de cada pérdida."""
    level, bal, peak, max_dd, max_stake = 0, 0.0, 0.0, 0.0, base
    staked, cycles, cycles_won, lost_cost, worst_cycle, cycle_cost = 0.0, 0, 0, 0.0, 0.0, 0.0
    for ch in seq:
        stake = base * (mult ** level)
        max_stake = max(max_stake, stake)
        if ch == "T":
            continue
        staked += stake
        cycle_cost += stake
        if ch == "W":
            bal += stake * payout
            cycles += 1
            cycles_won += 1
            level, cycle_cost = 0, 0.0
        else:
            bal -= stake
            if level >= levels:                 # se agotaron los niveles: ciclo perdido
                cycles += 1
                lost_cost += cycle_cost
                worst_cycle = max(worst_cycle, cycle_cost)
                level, cycle_cost = 0, 0.0
            else:
                level += 1
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)
    return {"net": bal, "staked": staked, "yield": (bal / staked) if staked else 0.0, "cycles": cycles,
            "cycles_won": cycles_won, "cycle_rate": (cycles_won / cycles) if cycles else 0.0,
            "max_dd": max_dd, "max_stake": max_stake, "worst_cycle": worst_cycle}


def simulate_compound(seq, payout, steps=1, base=1.0):
    """Reproduce una secuencia real de resultados con ciclos de interés compuesto (`steps` reinversiones por ciclo).
    Gana -> la siguiente va por monto + ganancia (hasta completar el ciclo); pierde o completa -> monto base."""
    step, amount = 0, base
    bal = peak = max_dd = staked = 0.0
    max_stake = base
    cycles = cycles_won = 0
    for ch in seq:
        if ch == "T":
            continue
        staked += amount
        max_stake = max(max_stake, amount)
        if ch == "W":
            gain = amount * payout
            bal += gain
            if step < steps:
                step, amount = step + 1, amount + gain
            else:
                cycles += 1
                cycles_won += 1
                step, amount = 0, base
        else:
            bal -= amount
            cycles += 1
            step, amount = 0, base
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)
    return {"net": bal, "staked": staked, "yield": (bal / staked) if staked else 0.0, "cycles": cycles,
            "cycles_won": cycles_won, "cycle_rate": (cycles_won / cycles) if cycles else 0.0,
            "max_dd": max_dd, "max_stake": max_stake, "worst_cycle": base}


def compound_expectancy(p, payout, steps=1):
    """Ganancia esperada por ciclo (en montos base): (p·(1+r))^(steps+1) − 1. Cero justo en el equilibrio p = 1/(1+r)."""
    return (p * (1.0 + payout)) ** (steps + 1) - 1.0


def verdict(r):
    """(texto corto, color) -> color en {'good','warn','bad','mute'}."""
    d = r["wins"] + r["losses"]
    if d == 0:
        return "Sin entradas", "mute"
    if d < MIN_TRADES:
        return "Pocas ops", "warn"
    if r["profitable"]:
        return "Supera equilibrio", "good"
    if r["winrate"] > r["breakeven"]:
        return "Como el azar", "warn"
    return "Pierde dinero", "bad"


def _grid(key):
    g = []
    if key in ("reversion", "pullback"):
        for rp in (7, 14):
            for lo_ in (20.0, 25.0, 30.0):
                for k in (1.8, 2.0, 2.5):
                    for bp in (14, 20):
                        g.append(Params(rsi_period=rp, rsi_lo=lo_, rsi_hi=100.0 - lo_, bb_period=bp, bb_k=k))
    elif key == "trend":
        for fast, slow in ((5, 13), (9, 21), (12, 26)):
            for cap in (65.0, 70.0, 80.0):
                for rp in (7, 14):
                    g.append(Params(rsi_period=rp, ema_fast=fast, ema_slow=slow, rsi_hi=cap, rsi_lo=100.0 - cap))
    elif key == "regime":
        for er_t in (0.15, 0.25, 0.35):
            for pull in (40.0, 45.0, 50.0):
                for skip in (False, True):
                    g.append(Params(er_trend=er_t, pull_rsi=pull, range_skip=skip))
    elif key == "breakout":
        for dc in (10, 20, 40):
            for fast, slow in ((5, 13), (9, 21)):
                g.append(Params(dc_period=dc, ema_fast=fast, ema_slow=slow))
    elif key == "pattern":
        for k in (10.0, 40.0):
            for lvl in (0, 1, 2):
                g.append(Params(pat_min=30, pat_k=k, pat_level=lvl))
    elif key == "structure":
        for imp in (2.0, 3.0, 4.0):
            for touch in (1, 3):
                for room in (0.0, 1.5):
                    g.append(Params(st_imp=imp, st_touch=touch, st_room=room))
    elif key == "retrace":
        for lo_, hi_ in ((0.236, 0.618), (0.382, 0.618), (0.382, 0.786), (0.236, 0.786)):
            for room in (1.0, 2.0):
                for lvl in (False, True):
                    for smin in (0.25, 0.45):
                        g.append(Params(rt_lo=lo_, rt_hi=hi_, rt_room=room, rt_level=lvl, rt_min=smin))
    return g or [Params()]


def tune(data, key, minutes, payout, cut, min_trades=30):
    """Busca los mejores parámetros usando SOLO las velas anteriores a `cut`. Devuelve (params, nota)."""
    best, best_res = None, None
    for p in _grid(key):
        r = backtest(None, key, p, minutes, payout, 0, cut, data)
        if r["wins"] + r["losses"] < min_trades:
            continue
        if best_res is None or r["edge_low"] > best_res["edge_low"]:   # premia lo CONFIABLE, no la suerte con pocas ops
            best, best_res = p, r
    if best is None:
        return Params(), "pocas operaciones al ajustar: se usan los parámetros por defecto"
    return best, "parámetros ajustados en el 50% inicial"


def tune_multi(data, key, durations, payout, cut, min_trades=MIN_TRADES, gate=False):
    """Mejores parámetros por duración usando SOLO las velas anteriores a `cut`. {minutos: (Params, resultado)}.
    Con `gate` los parámetros se eligen sobre las señales que dejaría pasar el filtro de tendencia (lo que de verdad opera)."""
    best = {m: (None, None) for m in durations}
    for p in _grid(key):
        sig = signals(data, key, p)
        for m in durations:
            s_m = apply_gate(data, sig, m) if gate else sig
            r = backtest(None, key, p, m, payout, 0, cut, data, sig=s_m)
            if r["wins"] + r["losses"] < min_trades:
                continue
            if best[m][1] is None or r["edge_low"] > best[m][1]["edge_low"]:
                best[m] = (p, r)
    return {m: ((pr[0], pr[1]) if pr[0] is not None else (Params(), None)) for m, pr in best.items()}


def pick_filters(data, key, sig, m, payout, cut, allow_htf, allow_vol, p, allow_pat=False, allow_mtf=False):
    """Elige, con las velas ANTERIORES a `cut`, qué filtros mejoran la señal. Devuelve (flags, señal filtrada, resultado de ajuste)."""
    base = [()]
    if allow_htf:
        base.append(("htf",))
    if allow_vol:
        base.append(("vol",))
    if allow_htf and allow_vol:
        base.append(("htf", "vol"))
    variants = list(base)
    pat = None
    if allow_pat and key != "pattern":
        pat = pattern_filter_fn(data, m, payout)
        variants += [v + ("pat",) for v in base]
    if allow_mtf:
        variants += [("mtf",) + v for v in list(variants)]
    best = None
    for fl in variants:
        s2 = apply_filters(data, sig, fl, pat, m)
        r = backtest(None, key, p, m, payout, 0, cut, data, sig=s2)
        score = r["edge_low"] if r["wins"] + r["losses"] >= MIN_TRADES else -9.0
        if best is None or score > best[0] + 1e-9:       # solo cambia si mejora de verdad (empate = menos filtros)
            best = (score, fl, s2, r)
    return best[1], best[2], best[3]


TREND_MODES = ("on", "auto", "off")   # filtro de tendencia: obligatorio / lo decide el backtest / desactivado


def train_score(sub):
    """Puntuación de una duración en el tramo de AJUSTE (límite inferior del intervalo menos el equilibrio)."""
    tr = (sub or {}).get("train")
    return tr["edge_low"] if tr and tr["wins"] + tr["losses"] >= MIN_TRADES else -9.0


SPLIT_TRAIN, SPLIT_SEL = 0.50, 0.75     # v15.1: 50% ajuste · 25% selección · 25% prueba final


def compare(candles, minutes=1, payout=0.85, keys=None, split=SPLIT_TRAIN, optimize=True, margin=0.02, progress=None,
            allow_htf=True, allow_vol=True, allow_pat=True, trend_mode="on", split_sel=SPLIT_SEL):
    """Compara estrategias sobre un par en una o varias duraciones (`minutes` = entero o lista).
    Protocolo sin trampa en TRES tramos (v15.1):
      · ajuste (primer 50%): parámetros, filtros y DURACIÓN (por el límite inferior del intervalo de confianza);
      · selección (siguiente 25%, `sel`): elige cuál estrategia es la ★ del par y ordena el ranking;
      · prueba final (último 25%, `valid`): no se usa para elegir NADA; de aquí salen las cifras y el veredicto.
    Antes la ★ se elegía mirando el mismo tramo que se reportaba, y aparecían «ganadoras» de pura suerte.
    `trend_mode`: "on" = toda señal pasa por el filtro de tendencia multi-temporalidad (lo que opera es lo que se mide);
    "auto" = el tramo de ajuste decide si el filtro mejora cada estrategia; "off" = sin filtro (como la v13).
    Cada elemento trae `by_minutes` (resultado en validación de cada duración, con `ctx` = desglose por contexto de las
    señales SIN filtrar fuera del tramo de ajuste) y el elegido en `valid`, `sel`, `params`, `flags`, `minutes`, `ctx`.
    Devuelve la lista ordenada de la mejor a la peor por efectividad CONFIABLE (límite inferior del intervalo)."""
    durations = sorted({int(m) for m in (minutes if isinstance(minutes, (list, tuple, set)) else [minutes])})
    keys = keys or list(STRATEGIES)
    data = Data(candles)
    n_all = len(data.c)
    cut = int(len(candles) * split)
    cut2 = max(cut + 1, int(len(candles) * split_sel))
    gate_on, allow_mtf = trend_mode == "on", trend_mode == "auto"
    pre = ("mtf",) if gate_on else ()
    tuned, out = {}, []
    order = [k for k in TECH if k in keys or "ensemble" in keys] + [k for k in keys if k not in TECH]

    def finish(key, p, m, sig, probs, note, model=None, pat_filter=True):
        """Desglose de las señales sin filtrar, filtro de tendencia, filtros elegidos en el ajuste, selección y prueba."""
        raw = backtest(None, key, p, m, payout, cut, None, data, sig=sig)
        bd = context_breakdown(data, sig, raw["idx"], raw["seq"], m)
        base = apply_gate(data, sig, m) if gate_on else sig
        fl, sig2, tr = pick_filters(data, key, base, m, payout, cut, allow_htf, allow_vol, p,
                                    allow_pat and pat_filter, allow_mtf)
        return {"params": p, "model": model, "flags": pre + tuple(fl), "train": tr, "note": note, "ctx": bd,
                "raw": {k: raw[k] for k in ("trades", "wins", "losses", "winrate", "breakeven")},
                "sel": backtest(candles, key, p, m, payout, cut, cut2, data, sig=sig2, probs=probs),
                "valid": backtest(candles, key, p, m, payout, cut2, None, data, sig=sig2, probs=probs)}

    for n_, key in enumerate(order):
        if progress:
            progress(key, n_, len(order))
        by = {}
        if key in ("prob", "ensemble"):
            for m in durations:
                p = Params(prob_margin=margin)
                vp = ({k: Params(**{**tuned[k][m][0].__dict__, "prob_margin": margin}) if k in tuned and m in tuned[k]
                       else Params(prob_margin=margin) for k in TECH} if key == "ensemble" else None)
                model = fit_prob(data, p, m, 0, cut, vote_params=vp)
                if model is None:
                    empty = backtest(candles, key, p, m, payout, cut2, None, data, sig=[None] * n_all)
                    by[m] = {"params": p, "model": None, "flags": pre, "train": None, "ctx": None, "raw": None,
                             "sel": empty, "valid": empty,
                             "note": "pocas velas para entrenar el modelo"}
                    continue
                sig, probs = prob_signals(data, p, model, payout)
                by[m] = finish(key, p, m, sig, probs, "modelo entrenado en el 50% inicial", model)
        elif key == "pattern":
            # la memoria aprende con ventana creciente (solo el pasado de cada vela); los parámetros se eligen en el tramo de ajuste
            for m in durations:
                best_p, best_r = None, None
                for p in (_grid("pattern") if optimize else [Params()]):
                    p = Params(**{**p.__dict__, "prob_margin": margin})
                    s0 = pattern_signals(data, p, m, payout)[0]
                    r = backtest(None, key, p, m, payout, 0, cut, data, sig=apply_gate(data, s0, m) if gate_on else s0)
                    if r["wins"] + r["losses"] < MIN_TRADES:
                        continue
                    if best_r is None or r["edge_low"] > best_r["edge_low"]:
                        best_p, best_r = p, r
                p = best_p or Params(prob_margin=margin)
                sig, probs = pattern_signals(data, p, m, payout)
                by[m] = finish(key, p, m, sig, probs, "memoria con ventana creciente; parámetros elegidos en el 50% inicial",
                               pat_filter=False)
        else:
            tm = (tune_multi(data, key, durations, payout, cut, gate=gate_on) if optimize
                  else {m: (Params(), None) for m in durations})
            tuned[key] = {m: (tm[m][0], tm[m][1]) for m in durations}
            sig_cache = {}
            for m in durations:
                p = tm[m][0]
                ck = tuple(sorted(p.__dict__.items()))
                sig = sig_cache.get(ck) or sig_cache.setdefault(ck, signals(data, key, p))
                by[m] = finish(key, p, m, sig, None, "parámetros ajustados en el 50% inicial")
        if key not in keys:
            continue
        # duración elegida: la de mayor límite inferior en el tramo de AJUSTE (no en el de validación)
        best_m = max(durations, key=lambda m: (train_score(by[m]), -m))
        b = by[best_m]
        out.append({"key": key, "name": SHORT[key], "params": b["params"], "model": b["model"], "flags": b["flags"],
                    "minutes": best_m, "valid": b["valid"], "sel": b["sel"], "by_minutes": by, "note": b["note"],
                    "ctx": b.get("ctx"), "trend_mode": trend_mode, "split": (split, split_sel)})
    # la ★ y el orden del ranking salen del tramo de SELECCIÓN; la prueba final queda intacta
    out.sort(key=sel_score, reverse=True)
    return out


def sel_score(it):
    s = it.get("sel") or it["valid"]
    return (s["wins"] + s["losses"] >= MIN_TRADES, s["edge_low"])


def confirm_score(it_or_sub):
    """Para ordenar los pares confirmados: límite inferior del intervalo (selección + prueba juntas) menos el equilibrio."""
    v = it_or_sub["valid"]
    s = it_or_sub.get("sel") or {"wins": 0, "losses": 0}
    w, n = v["wins"] + s["wins"], v["wins"] + v["losses"] + s["wins"] + s["losses"]
    return (wilson(w, n)[0] - v["breakeven"]) if n else -1.0


def passes(it_or_sub):
    """¿Supera el equilibrio de forma confiable en la prueba final Y le fue a favor en la selección?"""
    v = it_or_sub["valid"]
    s = it_or_sub.get("sel") or v
    return verdict(v)[1] == "good" and s["wins"] + s["losses"] > 0 and s["winrate"] > s["breakeven"]


def fit_model(candles, key, p, minutes, vote_params=None, start=0, end=None):
    """Entrena el modelo que necesita una estrategia (probabilístico, conjunto o memoria de patrones) con estas velas."""
    data = Data(candles)
    end = len(candles) if end is None else end
    if key == "pattern":
        return build_pattern_model(data, p, minutes, start, end)
    return fit_prob(data, p, minutes, start, end, vote_params=vote_params)
