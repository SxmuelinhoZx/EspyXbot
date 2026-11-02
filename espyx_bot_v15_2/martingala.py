"""
EspyX Bot — martingala POR PAR.

Antes (v12) había un solo contador de pérdidas seguidas para todo el bot: si en una misma vela se abrían 4 operaciones y
perdían 2, el monto doblado caía en la SIGUIENTE operación que se abriera, aunque fuera de otra divisa.
Ahora cada mercado (EURUSD, GBPUSD-OTC, …) lleva su propio nivel:

  · Gana  -> ese par vuelve al monto base (ciclo ganado).
  · Pierde -> ese par queda con un «gale pendiente» (nivel + 1) para sus próximas `wait_bars` velas. El gale solo se
              abre si el análisis vuelve a dar señal en ese par (y, si se exige, en la misma dirección).
              Si el análisis no confirma dentro de esas velas, el ciclo se cierra y el par vuelve al monto base.
  · Agota los niveles -> ciclo perdido: vuelve al monto base o se detiene (según la configuración).
  · Empate -> conserva el nivel (Exnova devuelve lo apostado).
Las demás divisas no se enteran: siguen con su monto normal.
"""
import threading


class PairMartingale:
    def __init__(self, mult=2.0, levels=2, wait_bars=1, same_dir=False, on_exhaust="reset"):
        self.lock = threading.Lock()
        self.state = {}        # mercado -> {"level", "dir", "left", "lost"}
        self.exhausted = []    # mercados que agotaron sus niveles en esta sesión
        self.configure(mult, levels, wait_bars, same_dir, on_exhaust)

    def configure(self, mult=2.0, levels=2, wait_bars=1, same_dir=False, on_exhaust="reset"):
        self.mult = float(mult)
        self.levels = int(levels)
        self.wait_bars = max(1, int(wait_bars))
        self.same_dir = bool(same_dir)
        self.on_exhaust = on_exhaust            # "reset" (vuelve al base) o "stop" (detener)

    def reset(self):
        with self.lock:
            self.state.clear()
            self.exhausted.clear()

    # ------------------------------------------------------------ consultas
    def level(self, market):
        with self.lock:
            st = self.state.get(market)
            return st["level"] if st else 0

    def pending(self, market):
        """Copia del gale pendiente de un mercado (o None)."""
        with self.lock:
            st = self.state.get(market)
            return dict(st) if st else None

    def amount(self, base, level):
        return round(max(1.0, base * self.mult ** level), 2)

    def stake(self, base, market):
        lvl = self.level(market)
        return self.amount(base, lvl), lvl

    def direction_ok(self, market, action):
        """¿Puede abrirse el gale en esta dirección? (si se exige repetir la dirección de la operación perdida)"""
        with self.lock:
            st = self.state.get(market)
        return not (st and self.same_dir and st.get("dir") and st["dir"] != action)

    def worst_case(self, base):
        """Lo que cuesta perder todos los niveles seguidos en UN par."""
        return sum(self.amount(base, k) for k in range(self.levels + 1))

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    # ------------------------------------------------------------ eventos
    def on_result(self, market, level_used, profit, action):
        """Actualiza el par al cerrar una de sus operaciones. Devuelve (evento, nivel siguiente) con evento en
        'win' (ciclo ganado), 'gale' (gale pendiente), 'exhausted' (se agotaron los niveles), 'tie',
        'unknown' (resultado sin confirmar: el ciclo se cierra por seguridad) o 'none'."""
        with self.lock:
            if profit is None:
                # sin resultado confirmado no se sabe si ganó o perdió: por seguridad el ciclo se cierra (monto base)
                st = self.state.pop(market, None)
                return ("unknown" if (st or level_used) else "none"), 0
            if profit > 0:
                self.state.pop(market, None)
                return "win", 0
            if profit == 0:
                if level_used > 0:
                    self.state[market] = {"level": level_used, "dir": action, "left": self.wait_bars, "lost": 0}
                return "tie", level_used
            if level_used < self.levels:
                nxt = level_used + 1
                self.state[market] = {"level": nxt, "dir": action, "left": self.wait_bars, "lost": 0}
                return "gale", nxt
            self.state.pop(market, None)
            self.exhausted.append(market)
            return "exhausted", 0

    def on_opened(self, market):
        """El gale de este mercado ya se abrió: deja de estar «pendiente» (su resultado decidirá el siguiente paso)."""
        with self.lock:
            st = self.state.get(market)
            if st:
                st["left"] = 0
                st["opened"] = True

    def on_bar_without_entry(self, market):
        """Pasó una vela sin abrir el gale pendiente de este mercado. Devuelve el nivel abandonado si se agotó la
        espera (el ciclo se cierra y vuelve al monto base) o None si todavía puede esperar."""
        with self.lock:
            st = self.state.get(market)
            if not st or st.get("opened"):
                return None
            st["left"] -= 1
            if st["left"] <= 0:
                self.state.pop(market, None)
                return st["level"]
            return None


class PairCompound:
    """Ciclos de interés compuesto POR PAR (lo contrario de la martingala: se sube el monto al GANAR, no al perder).

      · Gana  -> si aún quedan pasos, la siguiente operación de ESE par va por (monto usado + ganancia).
                 Si completó los pasos del ciclo, el ciclo se cierra (ganado) y vuelve al monto base.
      · Pierde -> vuelve al monto base. Lo máximo que se pierde en un ciclo es el monto base: la ganancia
                  que se arriesga en el paso siguiente salió del propio ciclo.
      · Empate -> repite el mismo monto.
      · Resultado sin confirmar -> por seguridad vuelve al monto base.
    `steps` = cuántas veces se reinvierte la ganancia (1 = ciclo de 2 operaciones ganadas, como en el video).
    """

    def __init__(self, steps=1):
        self.lock = threading.Lock()
        self.state = {}        # mercado -> {"step": n, "amount": monto de la próxima operación}
        self.steps = max(1, int(steps))

    def configure(self, steps=1):
        self.steps = max(1, int(steps))

    def reset(self):
        with self.lock:
            self.state.clear()

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}

    def stake(self, base, market):
        """(monto, paso) para la próxima operación de un mercado."""
        with self.lock:
            st = self.state.get(market)
        if not st:
            return round(max(1.0, base), 2), 0
        return round(max(1.0, st["amount"]), 2), st["step"]

    def worst_cycle(self, base):
        """Lo máximo que se puede perder en un ciclo (siempre el monto base)."""
        return round(max(1.0, base), 2)

    def top_stake(self, base, payout):
        """La operación más grande de un ciclo completo (el último paso)."""
        return round(max(1.0, base) * (1.0 + payout) ** self.steps, 2)

    def on_result(self, market, amount_used, step_used, profit):
        """Devuelve (evento, monto siguiente): 'up' (sube al siguiente paso), 'cycle' (ciclo ganado, vuelve al base),
        'reset' (perdió, vuelve al base), 'tie' (repite) o 'unknown' (sin confirmar, vuelve al base)."""
        with self.lock:
            if profit is None:
                self.state.pop(market, None)
                return "unknown", None
            if profit == 0:
                if step_used:
                    self.state[market] = {"step": step_used, "amount": amount_used}
                return "tie", amount_used
            if profit < 0:
                had = self.state.pop(market, None)
                return ("reset" if (had or step_used) else "loss"), None
            if step_used < self.steps:
                nxt = round(amount_used + profit, 2)
                self.state[market] = {"step": step_used + 1, "amount": nxt}
                return "up", nxt
            self.state.pop(market, None)
            return "cycle", None
