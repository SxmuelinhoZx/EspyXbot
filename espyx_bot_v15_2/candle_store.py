"""v15.2 · Velas guardadas en el PC.

Cada mercado (EUR/USD normal, EUR/USD OTC, …) y cada tamaño de vela (30 s, 1 min, 5 min) se guarda en
velas/<mercado>_<segundos>.json.gz. La siguiente vez solo se descarga lo que falta desde la última vela guardada:
«Comparar» con 20.000–50.000 velas tarda casi lo mismo que con 8000 y el histórico crece solo con el uso.

Solo se guardan velas CERRADAS. Si entre lo guardado y lo nuevo queda un hueco (el PC estuvo días sin abrir el bot),
se baja lo nuevo hasta llegar a lo guardado; si el hueco es mayor que lo que se pidió, se descarta lo viejo para no
pegar dos tramos lejanos como si fueran seguidos.
"""
import gzip
import json
import os
import re
import threading
import time

CHUNK = 900                          # velas por petición (Exnova entrega hasta ~1000)
MAX_KEEP = {30: 60000, 60: 60000, 300: 30000}     # tope por archivo: 1 min ≈ 41 días · 5 min ≈ 104 días


def _norm(raw):
    """Velas de Exnova o ya normalizadas -> {t: (o, h, l, c)}."""
    out = {}
    for r in raw or []:
        try:
            if "from" in r:
                t, o, c = int(r["from"]), float(r["open"]), float(r["close"])
                h, l_ = float(r.get("max", max(o, c))), float(r.get("min", min(o, c)))
            else:
                t, o, h, l_, c = int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
        except (KeyError, TypeError, ValueError):
            continue
        out[t] = (o, h, l_, c)
    return out


class CandleStore:
    def __init__(self, folder):
        self.folder = folder
        self._locks = {}
        self._glock = threading.Lock()

    # ------------------------------------------------------------ archivos
    def _path(self, full, bar):
        safe = re.sub(r"[^A-Za-z0-9_-]+", "", full.replace("/", ""))
        return os.path.join(self.folder, f"{safe}_{int(bar)}.json.gz")

    def _lock(self, full, bar):
        with self._glock:
            return self._locks.setdefault((full, int(bar)), threading.Lock())

    def _load(self, full, bar):
        try:
            with gzip.open(self._path(full, bar), "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            return {int(r[0]): tuple(r[1:5]) for r in data.get("c", [])}
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            return {}

    def _save(self, full, bar, merged):
        keep = MAX_KEEP.get(int(bar), 60000)
        ts = sorted(merged)[-keep:]
        os.makedirs(self.folder, exist_ok=True)
        path = self._path(full, bar)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
            json.dump({"m": full, "bar": int(bar), "c": [[t, *merged[t]] for t in ts]}, fh, separators=(",", ":"))
        os.replace(tmp, path)

    def count(self, full, bar):
        return len(self._load(full, bar))

    def summary(self):
        """(archivos, velas aproximadas, MB) de lo guardado."""
        try:
            files = [f for f in os.listdir(self.folder) if f.endswith(".json.gz")]
        except OSError:
            return 0, 0.0
        mb = sum(os.path.getsize(os.path.join(self.folder, f)) for f in files) / 1e6
        return len(files), mb

    def clear(self):
        try:
            for f in os.listdir(self.folder):
                if f.endswith(".json.gz") or f.endswith(".tmp"):
                    os.remove(os.path.join(self.folder, f))
        except OSError:
            pass

    # ------------------------------------------------------------ descarga incremental
    def history(self, broker, full, bar, total, progress=None, max_requests=80):
        """Las últimas `total` velas CERRADAS de un mercado, descargando solo lo que no está guardado.
        Devuelve velas {t, o, h, l, c} ordenadas. progress(n_disponibles, n_descargadas)."""
        bar = int(bar)
        with self._lock(full, bar):
            have = self._load(full, bar)
            now = broker.now()
            last_closed = int(now // bar) * bar - bar          # inicio de la última vela cerrada
            have = {t: v for t, v in have.items() if t <= last_closed}
            have_last = max(have) if have else None
            new, end, req = {}, None, 0
            # 1) lo nuevo: hacia atrás desde ahora hasta tocar lo guardado (o hasta `total` si no hay nada guardado)
            while req < max_requests:
                if have_last is not None and new and min(new) <= have_last:
                    break
                if have_last is None and len(new) >= total:
                    break
                if have_last is not None and len(new) >= total + CHUNK:     # hueco mayor que lo pedido
                    have = {}
                    have_last = None
                    break
                if have_last is not None and not new:
                    missing = int((last_closed - have_last) // bar) + 5
                    n = max(5, min(CHUNK, missing))
                else:
                    n = CHUNK
                raw = broker.get_candles(full, bar, n, end)
                req += 1
                got = _norm(raw)
                if not got or all(t in new for t in got):
                    break
                new.update(got)
                end = min(got) - 1
                if progress:
                    progress(len(have) + len(new), len(new))
            merged = dict(have)
            merged.update({t: v for t, v in new.items() if t <= last_closed})
            # 2) si aún no alcanza, lo más viejo (antes de la primera vela disponible)
            while len(merged) < total and req < max_requests and merged:
                end = min(merged) - 1
                raw = broker.get_candles(full, bar, min(CHUNK, total - len(merged) + 50), end)
                req += 1
                got = _norm(raw)
                got = {t: v for t, v in got.items() if t not in merged}
                if not got:
                    break                                     # Exnova no tiene más historia de este mercado
                merged.update(got)
                if progress:
                    progress(len(merged), req)
            if merged:
                try:
                    self._save(full, bar, merged)
                except OSError:
                    pass                                      # sin permiso de escritura: se sigue sin guardar
            ts = sorted(merged)[-int(total):]
            return [{"t": t, "o": merged[t][0], "h": merged[t][1], "l": merged[t][2], "c": merged[t][3]} for t in ts]

    def merge(self, full, bar, candles):
        """Agrega velas cerradas ya descargadas (p. ej. las del análisis en vivo)."""
        if not candles:
            return
        bar = int(bar)
        with self._lock(full, bar):
            merged = self._load(full, bar)
            merged.update(_norm(candles))
            try:
                self._save(full, bar, merged)
            except OSError:
                pass


def age_text(seconds):
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} días"


if __name__ == "__main__":       # prueba rápida sin conexión
    import random

    class _B:
        def now(self):
            return 1_700_000_000 + 0.5

        def get_candles(self, pair, size, count, end=None):
            end = self.now() if end is None else end
            last = int(end // size) * size
            return [{"from": last - k * size, "open": 1, "close": 1 + random.random() / 100, "min": .99, "max": 1.02}
                    for k in range(count - 1, -1, -1)]
    s = CandleStore("/tmp/velas_test")
    s.clear()
    t0 = time.time()
    a = s.history(_B(), "EURUSD", 60, 3000)
    print(len(a), s.count("EURUSD", 60), f"{time.time() - t0:.2f}s")
