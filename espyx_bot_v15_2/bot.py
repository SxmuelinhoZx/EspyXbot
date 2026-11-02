#!/usr/bin/env python3
"""
Bot de señales para Exnova.

Lee un archivo con señales tipo:
    EUR/USD 12:03 PM
    VENDER / SELL
    ***
y abre cada operación a su hora con el capital que configures.

Uso:
    python bot.py --signals signals.txt --dry-run     # solo simula, no opera
    python bot.py --signals signals.txt               # opera (según config.json)
"""
import argparse
import itertools
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- parser

SIGNAL_RE = re.compile(
    r"(?P<a>[A-Z]{3})\s*/?\s*(?P<b>[A-Z]{3})"          # EUR/USD
    r"[\s(\[]*(?P<otc>otc)?[\s)\]]*"                   # (otc) opcional
    r"(?P<h>\d{1,2})\s*[:.]\s*(?P<m>\d{2})\s*"          # 13:00
    r"(?P<ap>[AP]\.?M\.?)?"                              # AM / PM (opcional, pegado o con espacio)
    r"[^\d]*?"                                           # basura entre medio (*** , espacios, etc.)
    r"(?P<dir>VENDER|COMPRAR|SELL|BUY)",
    re.IGNORECASE,
)


@dataclass
class Signal:
    pair: str       # ej. "EURUSD"
    hour: int       # 0-23
    minute: int
    action: str     # "call" (comprar) o "put" (vender)
    otc: bool = False

    def label(self):
        otc = " (OTC)" if self.otc else ""
        return f"{self.pair}{otc} {self.hour:02d}:{self.minute:02d} {'COMPRAR' if self.action == 'call' else 'VENDER'}"


def parse_signals(text: str):
    signals = []
    for m in SIGNAL_RE.finditer(text):
        h, mi = int(m["h"]), int(m["m"])
        ap = (m["ap"] or "").upper().replace(".", "")
        # Si la hora ya viene en 24h (13:00 PM) se ignora el AM/PM.
        if h <= 12:
            if ap == "PM" and h != 12:
                h += 12
            elif ap == "AM" and h == 12:
                h = 0
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            continue
        action = "call" if m["dir"].upper() in ("COMPRAR", "BUY") else "put"
        signals.append(Signal((m["a"] + m["b"]).upper(), h, mi, action, bool(m["otc"])))
    # Quita repetidas (misma hora, par, dirección y OTC) conservando el orden
    seen, unique = set(), []
    for s in signals:
        key = (s.pair, s.hour, s.minute, s.action, s.otc)
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


# ---------------------------------------------------------------- broker

MIN_TURBO_LEFT = 31      # Exnova deja comprar una binaria de 1–5 min solo si faltan más de 30 s para su vencimiento
BLITZ_SECONDS = (30, 60)  # duraciones Blitz para usuarios normales (las de 5–15 s son solo para VIP)
BUY_WAIT = 5.0            # segundos que se espera la confirmación de una compra
LATE_WATCH = 150.0        # segundos que se sigue vigilando una compra sin confirmar (por si Exnova la abre tarde)
EMPTY_REJECT = ("Exnova rechazó la orden sin indicar el motivo (suele pasar cuando el servidor va saturado o esa vela ya "
                "no admite compras); no se reintenta para no entrar tarde")


class Broker:
    """Capa fina sobre exnovaapi (librería no oficial). Aislada para poder cambiarla.

    v13:
    - Reloj del servidor (`now()`): todas las horas de entrada y vencimiento se calculan con la hora de Exnova,
      no con la del PC (si el PC va adelantado o atrasado unos segundos, las entradas siguen cayendo al cierre de la vela).
    - Velas por `request_id`: varios pares se piden a la vez sin pisarse (antes se esperaban en fila).
    - `open_trade` con vencimiento EXACTO: nunca convierte en silencio una operación de 1 min en una de 1:30.
    - Blitz (30 s / 60 s) con duración exacta desde la compra.
    v14:
    - Cada compra lleva su PROPIO identificador (la librería usaba siempre «buyraw» y borraba las respuestas de las demás
      compras: una respuesta tardía podía atribuirse a la orden equivocada).
    - Si Exnova confirma una compra después de los 5 s de espera, el vigía la detecta y avisa (`on_late_order`) para que
      se registre y se siga como cualquier otra (antes quedaba abierta sin que el bot la contara).
    - Rechazos sin texto -> mensaje legible.
    """

    def __init__(self, email, password):
        try:
            from exnovaapi.stable_api import Exnova
        except ImportError:
            raise RuntimeError("Falta la librería exnovaapi. Ejecuta instalar.bat (o mira el README).")
        self.api = Exnova(email, password)
        self.account_type = "PRACTICE"
        self._candles_lock = threading.Lock()   # solo para el modo antiguo (si Exnova no devolviera el request_id)
        self._trade_lock = threading.Lock()     # las compras comparten estado dentro de la librería: de una en una
        self._rid = itertools.count(1)
        self.keyed_candles = None               # None = aún no se sabe; True = respuestas por request_id
        self._offset = 0.0
        self.assets, self.id_fixes = {}, []
        self.last_open_kind = "turbo"
        self.last_expiry = None
        self.on_late_order = None               # callback(tag, id): una compra que se dio por fallida sí se abrió
        self._late = {}                         # request_id -> (enviada, tag) de compras sin confirmar
        self._late_lock = threading.Lock()
        self._late_thread = None

    # ------------------------------------------------------------ reloj del servidor
    def clock_offset(self):
        """Segundos que hay que sumar a la hora del PC para obtener la de Exnova (positivo = el PC va atrasado)."""
        try:
            samples = list(self.api.api.timesync_samples)[-15:]
        except Exception:
            samples = []
        if samples:
            # cada muestra = (hora del servidor al enviar) - (hora local al recibir) = desfase - retraso de red (>= 0);
            # la mayor es la de menor retraso, o sea la más fiel
            self._offset = max(srv - loc for srv, loc in samples)
        return self._offset

    def now(self):
        """Hora actual de Exnova (epoch, con decimales)."""
        return time.time() + self.clock_offset()

    def wait(self, event, seconds):
        """Espera `seconds` (de reloj del servidor) o hasta que `event` se active. Devuelve True si se activó."""
        return event.wait(max(0.0, seconds))

    # ------------------------------------------------------------ sesión y cuentas
    def connect(self, sms_code=None):
        """Devuelve (ok, motivo). Si motivo == '2FA', Exnova pidió un código por SMS."""
        if sms_code:
            return self.api.connect_2fa(sms_code)
        return self.api.connect()

    def accounts(self, timeout=10):
        """Lista fresca de cuentas [{id,type,amount,currency}] pidiéndola a Exnova."""
        raw = self.api.api
        raw.balances_raw = None
        raw.get_balances()
        t0 = time.time()
        while raw.balances_raw is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        msg = (raw.balances_raw or {}).get("msg")
        if isinstance(msg, list) and msg:
            return msg
        prof = raw.profile.msg or {}
        return prof.get("balances") or []

    def raw_accounts(self):
        """JSON crudo tal como lo manda Exnova (para diagnóstico)."""
        return json.dumps(self.accounts(), ensure_ascii=False)

    def raw_profile(self):
        """Cuentas y saldo según el perfil de la sesión (segunda fuente, para comparar)."""
        prof = self.api.api.profile.msg or {}
        keep = {k: prof.get(k) for k in ("balance", "balance_id", "balance_type", "currency", "balances")}
        return json.dumps(keep, ensure_ascii=False)

    def describe_accounts(self):
        names = {1: "REAL", 4: "DEMO", 2: "TORNEO"}
        lines = []
        for b in self.accounts():
            cash = float(b.get("amount") or 0)
            bonus = float(b.get("bonus_amount") or 0)
            lines.append(f"id={b.get('id')} tipo={b.get('type')} ({names.get(b.get('type'), '?')}) "
                         f"efectivo={cash:g} + bono={bonus:g} = total={cash + bonus:g} {b.get('currency', '')}")
        return lines

    def set_account(self, account_type):
        """Selecciona DEMO o REAL, verifica que quedó seleccionada y guarda un diagnóstico."""
        import exnovaapi.global_value as gv
        want = {"REAL": 1, "PRACTICE": 4}[account_type]
        t0 = time.time()
        while not (self.api.api.profile.msg or {}).get("balances") and time.time() - t0 < 15:
            time.sleep(0.2)
        accts = self.accounts()
        try:
            with open(os.path.join(BASE_DIR, "diagnostico.txt"), "w", encoding="utf-8") as f:
                f.write("Cuentas devueltas por Exnova:\n" + "\n".join(self.describe_accounts()) + "\n\nCRUDO:\n"
                        + self.raw_accounts() + "\n\nPERFIL:\n" + self.raw_profile() + "\n")
        except Exception:
            pass
        if not accts:
            raise RuntimeError("Exnova no devolvió ninguna cuenta. Intenta conectar de nuevo.")
        cands = [b for b in accts if b.get("type") == want]
        if not cands:
            raise RuntimeError(f"No encontré la cuenta {account_type}. Cuentas vistas: "
                               + " | ".join(self.describe_accounts()))
        # Si hubiera varias del mismo tipo, usa la de mayor saldo
        target = max(cands, key=lambda b: float(b.get("amount") or 0) + float(b.get("bonus_amount") or 0))
        if gv.balance_id is not None:
            self.api.position_change_all("unsubscribeMessage", gv.balance_id)
        gv.balance_id = target["id"]
        self.api.position_change_all("subscribeMessage", target["id"])
        self.account_type = account_type
        self.balance_id = target["id"]
        try:
            self.sync_assets()
        except Exception:
            pass
        time.sleep(0.3)

    def balance(self):
        import exnovaapi.global_value as gv
        for b in self.accounts():
            if str(b.get("id")) == str(gv.balance_id):
                return float(b.get("amount") or 0) + float(b.get("bonus_amount") or 0)
        raise RuntimeError("No pude leer el saldo de la cuenta seleccionada.")

    # ------------------------------------------------------------ operar
    @staticmethod
    def _no_disponible(msg):
        m = str(msg or "").lower()
        return "not available" in m or "no disponible" in m or "asset" in m

    @staticmethod
    def purchase_closed(msg):
        """¿Exnova dijo que esa expiración ya no admite compras? (pasa en algunos pares con la de 5 min)"""
        return "time for purchasing" in str(msg or "").lower()

    @staticmethod
    def expiry_for(minutes, now):
        """Vencimiento alineado al minuto: el cierre de la vela que termina `minutes` minutos después del minuto actual."""
        return (int(now // 60) + int(minutes)) * 60

    def open_trade(self, amount, pair, action, minutes=1, expiry=None, kind="binary", seconds=None, deadline=None,
                   tag=None):
        """Abre una operación y devuelve (ok, id o mensaje).

        kind="binary": opción de 1–5 min («turbo») que vence EXACTAMENTE en `expiry` (epoch del servidor, múltiplo de 60).
            Si no se indica, vence al cierre de la vela actual + (minutes - 1). Si faltan menos de 31 s para ese
            vencimiento NO abre (antes Exnova pasaba al minuto siguiente y salían operaciones de 1:30).
            Si Exnova dice que el activo no está disponible como turbo, reintenta como «binary» (tipo 1) con el mismo vencimiento.
        kind="blitz": duración exacta de `seconds` (30 o 60) desde la compra.
        deadline: hora del servidor después de la cual ya NO se compra (se mira tras esperar el turno, porque otra compra
        puede tardar unos segundos en responder).
        tag: datos de la operación que se devuelven a `on_late_order` si Exnova la confirma después de darla por fallida."""
        with self._trade_lock:
            if deadline is not None and self.now() > deadline:
                return False, (f"llegó tarde: la orden saldría {self.now() - deadline:.1f} s después del segundo máximo "
                               f"de entrada (otra compra tardó en responder)")
            if kind == "blitz":
                return self._open_blitz(amount, pair, action, int(seconds or 30), tag)
            now = self.now()
            if expiry is None:
                expiry = self.expiry_for(minutes, now)
            left = expiry - now
            if left < MIN_TURBO_LEFT:
                return False, (f"llegó tarde: faltan {left:.0f} s para el vencimiento y Exnova exige más de 30 s "
                               f"(se descarta para no abrir una operación de {(left + 60):.0f} s)")
            self.last_open_kind = "turbo"
            ok, tid = self._buy_raw(amount, pair, action, "turbo", expiry, tag)
            if ok or not self._no_disponible(tid):
                if ok:
                    self.last_expiry = float(expiry)
                return ok, tid
            try:
                ok2, tid2 = self._buy_raw(amount, pair, action, "binary", expiry, tag)
            except Exception as e:
                return ok, f"{tid} | reintento como binaria falló: {e}"
            if ok2:
                self.last_open_kind = "binary"
                self.last_expiry = float(expiry)
                return ok2, tid2
            return ok, f"{tid} | como binaria también: {tid2}"

    @staticmethod
    def _reply(got):
        """Respuesta de Exnova a una compra -> (True, id) / (False, motivo) / None si aún no llegó."""
        if got is None:
            return None
        if isinstance(got, dict):
            if got.get("id") is not None:
                return True, got["id"]
            if "message" in got:
                msg = got.get("message")
                return False, (str(msg).strip() if msg not in (None, "") and str(msg).strip() else EMPTY_REJECT)
            return None
        txt = str(got).strip()
        return False, (txt or EMPTY_REJECT)

    def _wait_reply(self, rid, tag, what):
        api = self.api.api
        t0 = time.time()
        while True:
            rep = self._reply(api.buy_multi_option.get(rid))
            if rep is not None:
                api.buy_multi_option.pop(rid, None)
                if not rep[0] and "%!s(missing)" in str(rep[1]).lower():      # fallo de formato del servidor de Exnova
                    rep = (False, str(rep[1]).replace("%!s(MISSING)", "(el activo)") +
                           " — Exnova no reconoce este activo en esta modalidad")
                return rep
            if time.time() - t0 > BUY_WAIT:
                self._watch_late(rid, tag)
                return False, (f"Exnova no confirmó la compra{' ' + what if what else ''} en {BUY_WAIT:g} s; si la abre "
                               f"más tarde, el bot la registrará y la seguirá igual")
            time.sleep(0.005)

    def _buy_raw(self, amount, pair, action, option, expiry, tag=None):
        """Compra binaria/turbo con identificador propio (no pisa ni borra las respuestas de otras compras)."""
        import exnovaapi.constants as OP
        # el id de ESA modalidad (turbo y binaria clásica pueden tener ids distintos en Exnova)
        active = ((self.assets.get(pair) or {}).get(option) or {}).get("id") or OP.ACTIVES.get(pair)
        if active is None:
            return False, f"El par {pair} no está en la lista de activos."
        api = self.api.api
        rid = f"espyxo{next(self._rid)}"
        api.buy_multi_option.pop(rid, None)
        api.buyv3_by_raw_expired(float(amount), active, action, option, int(expiry), request_id=rid)
        return self._wait_reply(rid, tag, "")

    def _open_blitz(self, amount, pair, action, seconds, tag=None):
        import exnovaapi.constants as OP
        active = ((self.assets.get(pair) or {}).get("blitz") or {}).get("id") or OP.ACTIVES.get(pair)
        if active is None:
            return False, f"El par {pair} no está en la lista de activos."
        pay = self.payout(pair, "blitz") or self.payout(pair, "turbo") or 85
        api = self.api.api
        rid = f"espyxb{next(self._rid)}"
        api.buy_multi_option.pop(rid, None)
        expired = int(round(self.now())) + int(seconds)
        api.buy_blitz_option(float(amount), active, action, int(seconds), int(pay), None, rid, expired=expired)
        ok, tid = self._wait_reply(rid, tag, "Blitz")
        if ok:
            self.last_open_kind = "blitz"
            self.last_expiry = float(expired)
        return ok, tid

    # ------------------------------------------------------------ compras que Exnova confirma tarde
    def _watch_late(self, rid, tag):
        with self._late_lock:
            self._late[rid] = (time.time(), tag)
            if self._late_thread is None or not self._late_thread.is_alive():
                self._late_thread = threading.Thread(target=self._late_loop, daemon=True, name="espyx-late")
                self._late_thread.start()

    def _late_loop(self):
        api = self.api.api
        while True:
            time.sleep(0.5)
            with self._late_lock:
                items = list(self._late.items())
            if not items:
                with self._late_lock:
                    if not self._late:
                        self._late_thread = None
                        return
                continue
            for rid, (sent, tag) in items:
                rep = self._reply(api.buy_multi_option.get(rid))
                if rep is None and time.time() - sent < LATE_WATCH:
                    continue
                with self._late_lock:
                    self._late.pop(rid, None)
                api.buy_multi_option.pop(rid, None)
                if rep and rep[0] and self.on_late_order:
                    try:
                        self.on_late_order(tag, rep[1])
                    except Exception:
                        pass

    def result(self, trade_id, timeout=None):
        """Bloquea hasta que cierre. Devuelve la ganancia neta (negativa si pierde)."""
        win, profit = self.api.check_win_v4(trade_id, timeout)
        return float(profit)

    # ------------------------------------------------------------ activos
    def sync_assets(self):
        """Lee de Exnova los activos (ids reales, estado y pago) y corrige los ids de la librería.
        Devuelve {nombre: {'turbo': {...}, 'binary': {...}, ['blitz': {...}]}} o None si Exnova no respondió."""
        import exnovaapi.constants as OP_code
        data = self.api.get_all_init_v2()
        if not data:
            return None
        table, fixes = {}, []
        for kind in ("turbo", "binary", "blitz"):
            for aid, a in ((data.get(kind) or {}).get("actives", {}) or {}).items():
                raw = str(a.get("name", "")).split(".")[-1]
                # v14.1: Exnova llama al mercado NORMAL «EURUSD-op» (id 1861…) y deja el viejo «EURUSD» (id 1)
                # deshabilitado. Antes se leía el viejo y todos los pares normales salían cerrados.
                name = raw[:-3] if raw.lower().endswith("-op") else raw
                enabled, susp = bool(a.get("enabled")), bool(a.get("is_suspended"))
                try:
                    payout = round(100 - float(a["option"]["profit"]["commission"]))
                except Exception:
                    payout = None
                ent = {"id": int(aid), "open": enabled and not susp, "enabled": enabled, "suspended": susp,
                       "payout": payout, "name": raw}
                old = table.setdefault(name, {}).get(kind)
                # si el mismo par aparece dos veces, manda el que está abierto (y, a igualdad, el de mayor pago)
                if old is None or (ent["open"], ent["payout"] or 0) > (old["open"], old["payout"] or 0):
                    table[name][kind] = ent
        for name, kinds in table.items():
            # id con el que se opera y se piden velas: el de la modalidad abierta (turbo primero)
            pick = next((kinds[k] for k in ("turbo", "binary") if k in kinds and kinds[k]["open"]), None) \
                or next((kinds[k] for k in ("turbo", "binary") if k in kinds), None)
            if pick is None:
                continue
            if OP_code.ACTIVES.get(name) != pick["id"]:
                fixes.append((name, OP_code.ACTIVES.get(name), pick["id"]))
            OP_code.ACTIVES[name] = pick["id"]
        self.assets, self.id_fixes = table, fixes
        return table

    def payout(self, pair, kind="turbo"):
        """Pago (%) conocido de un activo en una modalidad, o None."""
        k = (self.assets.get(pair) or {}).get(kind) or {}
        return k.get("payout")

    def asset_status(self, pair):
        """Estado de un par: {'turbo': {...}, 'binary': {...}} ({} si no existe; None si Exnova no respondió)."""
        table = self.sync_assets()
        return None if table is None else table.get(pair, {})

    # ------------------------------------------------------------ velas
    def get_candles(self, pair, size=60, count=200, endtime=None, timeout=15):
        """Velas crudas de Exnova (lista de dicts con from/open/close/min/max), con tiempo máximo de espera."""
        import exnovaapi.constants as OP
        active = OP.ACTIVES.get(pair)
        if active is None:
            raise ValueError(f"El par {pair} no está en la lista de activos.")
        api = self.api.api
        if endtime is None:
            endtime = self.now()
        rid = f"espyxc{next(self._rid)}"
        if self.keyed_candles:
            api.getcandles(active, size, count, int(endtime), request_id=rid)
            t0 = time.time()
            while True:
                data = api.candles_by_req.pop(rid, None)
                if data is not None:
                    return list(data)
                if time.time() - t0 > timeout:
                    raise TimeoutError("Exnova no respondió con las velas.")
                time.sleep(0.01)
        with self._candles_lock:          # modo antiguo: una petición a la vez
            api.candles.candles_data = None
            api.getcandles(active, size, count, int(endtime), request_id=rid)
            t0 = time.time()
            while api.candles.candles_data is None:
                if time.time() - t0 > timeout:
                    raise TimeoutError("Exnova no respondió con las velas.")
                time.sleep(0.01)
            data = list(api.candles.candles_data)
            if self.keyed_candles is None:            # ¿Exnova devuelve el request_id? (se mira una vez)
                self.keyed_candles = api.candles_by_req.pop(rid, None) is not None
            return data

    def history(self, pair, total=3000, size=60, progress=None, chunk=900):
        """Descarga hasta `total` velas hacia atrás, en bloques. progress(n_descargadas)."""
        got, end = {}, None
        for _ in range(80):
            if len(got) >= total:
                break
            raw = self.get_candles(pair, size, min(chunk, total - len(got) + 50), end)
            if not raw or all(c.get("from") in got for c in raw):
                break
            for c in raw:
                got[c["from"]] = c
            end = min(c["from"] for c in raw) - 1
            if progress:
                progress(len(got))
        return sorted(got.values(), key=lambda c: c["from"])[-total:]

    def expiry_epoch(self, minutes):
        """Momento (epoch del servidor) en que vencerá una operación abierta ahora con la regla de Exnova."""
        try:
            from exnovaapi.expiration import get_expiration_time
            return float(get_expiration_time(int(self.now()), minutes)[0])
        except Exception:
            return None


class DryBroker:
    def __init__(self, start=1000.0):
        self._bal = start

    def balance(self):
        return self._bal

    def now(self):
        return time.time()

    def open_trade(self, amount, pair, action, minutes=1, expiry=None, kind="binary", seconds=None, deadline=None,
                   tag=None):
        dur = f"{seconds} s (blitz)" if kind == "blitz" else f"{minutes} min"
        print(f"   [SIMULACIÓN] {action.upper()} {pair} ${amount:.2f} a {dur}")
        return True, "dry"

    def result(self, trade_id, timeout=None):
        return 0.0

    def expiry_epoch(self, minutes):
        return None


# ---------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg, broker, dry):
        self.cfg, self.broker, self.dry = cfg, broker, dry
        self.tz = ZoneInfo(cfg["timezone"])
        self.lock = threading.Lock()
        self.pnl = 0.0
        self.consec_losses = 0
        self.wins = self.losses = 0
        self.stop = False
        self.start_balance = broker.balance()

    def amount(self):
        a = self.cfg["amount"]
        if a["mode"] == "percent":
            val = self.broker.balance() * a["value"] / 100
        else:
            val = a["value"]
        return round(max(1.0, min(val, a.get("max", val))), 2)

    def check_limits(self):
        c = self.cfg["risk"]
        if self.pnl <= -abs(c["max_daily_loss"]):
            return f"pérdida diaria máxima alcanzada ({self.pnl:.2f})"
        if self.pnl >= c["daily_profit_target"] > 0:
            return f"meta diaria alcanzada (+{self.pnl:.2f})"
        if self.consec_losses >= c["max_consecutive_losses"]:
            return f"{self.consec_losses} pérdidas seguidas"
        return None

    def track(self, trade_id, sig, amount):
        try:
            profit = self.broker.result(trade_id)
        except Exception as e:
            print(f"   ! No pude leer el resultado de {sig.label()}: {e}")
            return
        with self.lock:
            self.pnl += profit
            if profit > 0:
                self.wins += 1
                self.consec_losses = 0
            elif profit < 0:
                self.losses += 1
                self.consec_losses += 1
            print(f"   → {sig.label()}: {'GANÓ' if profit > 0 else 'PERDIÓ' if profit < 0 else 'EMPATE'} "
                  f"{profit:+.2f} | acumulado hoy {self.pnl:+.2f} | W{self.wins}/L{self.losses}")

    def run(self, signals):
        now = datetime.now(self.tz)
        base = now.replace(second=0, microsecond=0)
        schedule = []
        for s in signals:
            t = base.replace(hour=s.hour, minute=s.minute)
            schedule.append((t, s))
        schedule.sort(key=lambda x: x[0])

        print(f"Hora actual ({self.cfg['timezone']}): {now:%H:%M:%S}. {len(schedule)} señales cargadas.")
        print(f"Cuenta: {'SIMULACIÓN' if self.dry else self.broker.account_type} | saldo ${self.start_balance:.2f}\n")
        threads = []
        max_late = self.cfg.get("max_late_seconds", 10)

        for t, s in schedule:
            entry = t + timedelta(seconds=self.cfg.get("entry_offset_seconds", 0))
            late = (datetime.now(self.tz) - entry).total_seconds()
            if late > max_late:
                print(f"- {s.label()} ya pasó, la salto.")
                continue
            reason = self.check_limits()
            if reason:
                print(f"\nDETENIDO: {reason}")
                break
            wait = -late
            if wait > 0:
                print(f"- Esperando {s.label()} ({int(wait)} s)...")
                time.sleep(wait)
            reason = self.check_limits()  # re-chequear tras la espera (por si cerró una pérdida)
            if reason:
                print(f"\nDETENIDO: {reason}")
                break

            pair = s.pair + ("-OTC" if (s.otc or self.cfg.get("use_otc")) else "")
            amt = self.amount()
            ok, tid = self.broker.open_trade(amt, pair, s.action, self.cfg["expiration_minutes"])
            if not ok:
                print(f"   ! No se pudo abrir {s.label()}: {tid}")
                continue
            print(f"   ✔ Abierta {s.label()} por ${amt:.2f}")
            if not self.dry:
                th = threading.Thread(target=self.track, args=(tid, s, amt), daemon=True)
                th.start()
                threads.append(th)

        for th in threads:
            th.join()
        print(f"\nFin. Resultado del día: {self.pnl:+.2f} | W{self.wins}/L{self.losses}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="signals.txt")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="no abre operaciones, solo muestra qué haría")
    ap.add_argument("--parse-only", action="store_true", help="solo muestra cómo se leyó la lista")
    args = ap.parse_args()

    with open(args.signals, encoding="utf-8") as f:
        signals = parse_signals(f.read())
    if args.parse_only:
        for s in signals:
            print(s.label())
        print(f"{len(signals)} señales.")
        return
    if not signals:
        sys.exit("No encontré señales en el archivo.")

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    if args.dry_run:
        broker = DryBroker()
    else:
        if cfg["account_type"] == "REAL":
            if input("Vas a operar con dinero REAL. Escribe SI para continuar: ").strip() != "SI":
                sys.exit("Cancelado.")
        broker = Broker(cfg["email"], cfg["password"])
        ok, reason = broker.connect()
        if not ok and reason == "2FA":
            ok, reason = broker.connect(input("Código SMS de Exnova: ").strip())
        if not ok:
            sys.exit(f"No se pudo conectar a Exnova: {reason}")
        broker.set_account(cfg["account_type"])

    Bot(cfg, broker, args.dry_run).run(signals)


if __name__ == "__main__":
    main()
