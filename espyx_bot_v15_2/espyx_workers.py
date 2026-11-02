"""
EspyX Bot — cálculos pesados en procesos aparte.

El backtest («Comparar estrategias») y el entrenamiento de los modelos son Python puro y usan la CPU al 100%.
Si corren en un hilo de la misma ventana, el GIL de Python hace que cada redibujo de la interfaz espere su turno
(medido: 100 llamadas a Tk pasan de 0,5 ms a ~170 ms) y la ventana se «traba». Aquí corren en procesos separados,
con prioridad baja, así la ventana y las entradas al mercado nunca esperan por ellos.
"""
import multiprocessing as mp
import os
import sys
import threading
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import strategy as strat

_Q = None   # cola de progreso hacia la ventana (solo dentro de los procesos de trabajo)


def init_worker(q):
    """Se ejecuta una vez en cada proceso de trabajo: guarda la cola de progreso y baja la prioridad."""
    global _Q
    _Q = q
    try:
        if sys.platform.startswith("win"):
            import ctypes
            BELOW_NORMAL = 0x00004000
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(10)
    except Exception:
        pass


def _send(tag, text):
    if _Q is not None:
        try:
            _Q.put_nowait((tag, text))
        except Exception:
            pass


# ------------------------------------------------------------------ tareas (deben ser funciones de módulo)
def compare_task(tag, candles, durations, payout, opts):
    """Backtest completo de un mercado. Devuelve la lista de resultados de strategy.compare."""
    def prog(key, i, tot):
        _send(tag, f"{strat.SHORT.get(key, key)} ({i + 1}/{tot})")
    return strat.compare(candles, durations, payout, margin=opts.get("margin", 0.02),
                         optimize=opts.get("optimize", True), allow_htf=opts.get("allow_htf", True),
                         allow_vol=opts.get("allow_vol", True), allow_pat=opts.get("allow_pat", True),
                         trend_mode=opts.get("trend_mode", "on"), progress=prog)


def fit_task(candles, key, params, minutes, vote_params=None):
    """Entrena el modelo de una estrategia (probabilístico, conjunto o memoria de patrones)."""
    return strat.fit_model(candles, key, params, minutes, vote_params)


# ------------------------------------------------------------------ grupo de procesos
class WorkPool:
    """Procesos de trabajo creados al primer uso. Si no se pueden crear, usa un hilo (como antes) para no fallar."""

    def __init__(self, on_progress=None, workers=None):
        self.on_progress = on_progress
        self.workers = workers or max(1, min(3, (os.cpu_count() or 2) - 1))
        self._ex = None
        self._q = None
        self._fallback = None
        self._lock = threading.Lock()
        self._broken = 0
        self.mode = "sin iniciar"

    def _ensure(self):
        with self._lock:
            if self._ex is not None:
                return self._ex
            if self.mode == "hilos":
                return None
            try:
                ctx = mp.get_context("spawn")
                self._q = ctx.Queue()
                self._ex = ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx, initializer=init_worker,
                                               initargs=(self._q,))
                threading.Thread(target=self._drain, args=(self._q,), daemon=True).start()
                self.mode = f"{self.workers} proceso(s)"
                return self._ex
            except Exception:
                self._ex, self.mode = None, "hilos"
                return None

    def _drain(self, q):
        while True:
            try:
                tag, text = q.get()
            except Exception:
                return
            cb = self.on_progress
            if cb:
                try:
                    cb(tag, text)
                except Exception:
                    pass

    def _thread_submit(self, fn, *args):
        if self._fallback is None:
            self._fallback = ThreadPoolExecutor(max_workers=1)
        return self._fallback.submit(fn, *args)

    def submit(self, fn, *args) -> Future:
        ex = self._ensure()
        if ex is None:
            return self._thread_submit(fn, *args)
        try:
            return ex.submit(fn, *args)
        except (BrokenProcessPool, RuntimeError):
            with self._lock:                     # un proceso murió: se recrea; si se repite, se usan hilos
                self._kill(self._ex)
                self._ex = None
                self._broken += 1
                if self._broken >= 2:
                    self.mode = "hilos"
            ex = self._ensure()
            return ex.submit(fn, *args) if ex is not None else self._thread_submit(fn, *args)

    @staticmethod
    def _kill(ex):
        if ex is None:
            return
        try:
            for proc in list((getattr(ex, "_processes", None) or {}).values()):
                proc.terminate()
        except Exception:
            pass
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def shutdown(self):
        """Al cerrar la ventana: corta los cálculos en curso (si no, Python seguiría vivo hasta terminarlos)."""
        self._kill(self._ex)
        self._ex = None
        if self._fallback is not None:
            try:
                self._fallback.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
