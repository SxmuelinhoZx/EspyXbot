"""v15.2 · Filtro de noticias económicas.

Calendario de la semana de ForexFactory (feed público JSON, gratis): por cada noticia trae la hora, la divisa afectada
(USD, EUR, GBP…) y el impacto (alto / medio / bajo). El bot no entra en un mercado desde X min antes hasta Y min
después de una noticia de su divisa (EUR/USD se frena con noticias de EUR y de USD; el oro y los índices de EE. UU. con
las de USD; el DAX con las de EUR…).

· Se descarga al iniciar sesión y se refresca cada 4 h en segundo plano (el feed pide no consultarlo seguido).
· Se guarda en espyx_noticias.json: si no hay internet sirve la última copia (mientras sea de esta semana).
· Por defecto solo aplica al mercado normal: el OTC lo genera el bróker y no reacciona a las noticias.
"""
import json
import os
import ssl
import threading
import time
import urllib.request
from datetime import datetime

URLS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json")
REFRESH = 4 * 3600
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
IMPACT_ES = {"High": "alto", "Medium": "medio", "Low": "bajo", "Holiday": "feriado"}
CCYS = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"}

# índices, materias primas y cripto -> divisa cuyas noticias los mueven
_KEYWORDS = (
    (("XAU", "XAG", "GOLD", "SILVER", "OIL", "WTI", "BRENT", "NATGAS", "COPPER", "XPT", "XPD"), "USD"),
    (("US30", "US100", "US500", "USTEC", "NAS", "SPX", "SP500", "DJ", "DOW", "NDX", "RUSSELL", "US2000"), "USD"),
    (("GER", "DE30", "DE40", "DAX", "EU50", "STOXX", "FRA", "CAC", "ESP35", "IBEX", "ITA40", "NETH"), "EUR"),
    (("UK100", "FTSE"), "GBP"),
    (("JP225", "JPN225", "NIKKEI", "N225"), "JPY"),
    (("AUS200", "ASX"), "AUD"),
    (("HK50", "HSI", "CHINA", "CN50"), "CNY"),
    (("BTC", "ETH", "LTC", "XRP", "SOL", "DOGE", "ADA", "BNB", "CRYPTO"), "USD"),
)


def currencies(market, category=None):
    """Divisas cuyas noticias mueven un mercado. EURUSD / EURUSD-OTC -> {EUR, USD}; acciones -> {USD}."""
    s = market.upper().replace("-OTC", "").replace("-OP", "").replace("/", "").replace("_", "")
    if len(s) >= 6 and s[:3] in CCYS and s[3:6] in CCYS:
        return {s[:3], s[3:6]}
    for keys, ccy in _KEYWORDS:
        if any(k in s for k in keys):
            return {ccy}
    return {"USD"}                   # acciones (casi todas de EE. UU.) y lo que no se reconozca


def _parse_time(txt):
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


class NewsCalendar:
    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda m: None)
        self.lock = threading.Lock()
        self.events = []             # [{"t", "ccy", "impact", "title"}]
        self.fetched = 0.0           # epoch de la última descarga buena
        self.error = None
        self._busy = False
        self._load()

    # ------------------------------------------------------------ disco
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.events = [e for e in d.get("events", []) if isinstance(e.get("t"), (int, float))]
            self.fetched = float(d.get("fetched", 0))
        except (OSError, ValueError, TypeError):
            pass

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched": self.fetched, "events": self.events}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ descarga
    @staticmethod
    def parse(data):
        out = []
        for e in data or []:
            if not isinstance(e, dict):
                continue
            t = _parse_time(e.get("date", ""))
            ccy = str(e.get("country", "")).upper().strip()
            imp = str(e.get("impact", "")).strip().capitalize()
            if t is None or not ccy:
                continue
            out.append({"t": t, "ccy": ccy, "impact": imp, "title": str(e.get("title", "")).strip()})
        out.sort(key=lambda e: e["t"])
        return out

    def refresh(self, force=False, fetch=None):
        """Descarga el calendario si toca (o si force). fetch(url)->bytes se puede inyectar (pruebas).
        Devuelve (ok, mensaje)."""
        with self.lock:
            if self._busy:
                return False, "ya se está descargando"
            if not force and time.time() - self.fetched < REFRESH and self.events:
                return True, "al día"
            self._busy = True
        try:
            last = None
            for url in URLS:
                try:
                    raw = (fetch or self._fetch)(url)
                    evs = self.parse(json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw))
                    if not evs:
                        raise ValueError("el calendario llegó vacío")
                    with self.lock:
                        self.events, self.fetched, self.error = evs, time.time(), None
                    self._save()
                    return True, f"{len(evs)} noticias de la semana"
                except Exception as e:            # noqa: BLE001  (red, JSON, límite de consultas…)
                    last = e
            with self.lock:
                self.error = str(last)
            return False, f"no pude descargar el calendario ({last})"
        finally:
            with self.lock:
                self._busy = False

    @staticmethod
    def _fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 EspyX"})
        with urllib.request.urlopen(req, timeout=12, context=ssl.create_default_context()) as r:
            return r.read()

    def refresh_async(self, force=False, done=None):
        def work():
            ok, msg = self.refresh(force)
            if done:
                done(ok, msg)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ consultas
    def covers(self, now):
        """¿El calendario guardado cubre este momento? (el feed es de domingo a sábado)"""
        with self.lock:
            evs = self.events
        return bool(evs) and evs[0]["t"] - 3 * 86400 <= now <= evs[-1]["t"] + 2 * 86400

    def relevant(self, levels):
        with self.lock:
            return [e for e in self.events if e["impact"] in levels]

    def blocking(self, market, now, before_min=15, after_min=15, levels=("High",), category=None):
        """La noticia que frena este mercado ahora (o None)."""
        ccys = currencies(market, category)
        lo, hi = now - after_min * 60, now + before_min * 60
        for e in self.relevant(levels):
            if e["t"] > hi:
                break
            if e["t"] >= lo and (e["ccy"] in ccys or e["ccy"] == "ALL"):
                return e
        return None

    def upcoming(self, now, levels=("High",), ccys=None, n=6, after_min=15):
        out = []
        for e in self.relevant(levels):
            if e["t"] < now - after_min * 60:
                continue
            if ccys and e["ccy"] not in ccys and e["ccy"] != "ALL":
                continue
            out.append(e)
            if len(out) >= n:
                break
        return out


def fmt_event(e, now=None):
    when = datetime.fromtimestamp(e["t"])
    txt = f"{DIAS[when.weekday()]} {when:%H:%M} · {e['ccy']} · {e['title']} ({IMPACT_ES.get(e['impact'], e['impact'].lower())})"
    if now is not None:
        d = e["t"] - now
        if d >= 0:
            txt += f" · en {int(d // 3600)} h {int(d % 3600 // 60):02d} min" if d >= 3600 else f" · en {int(d // 60)} min"
        else:
            txt += f" · hace {int(-d // 60)} min"
    return txt
