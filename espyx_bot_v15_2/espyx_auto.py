"""
EspyX Bot — modo automático (v15).

Página «Automático» + motor que, en SU PROPIO HILO y con la hora del servidor de Exnova, analiza los pares justo al cierre
de cada vela y abre las operaciones en los primeros segundos de la vela siguiente:

- Binarias: velas de 1 min y vencimiento EXACTO al cierre de vela (1–5 min). Si por cualquier motivo la orden no puede
  salir antes del «segundo máximo de entrada», no se abre (antes salían operaciones de 1:30).
- Blitz: velas de 30 s y operaciones de 30 o 60 s exactos.
- Martingala POR PAR: tras perder, el gale va en la siguiente vela de ESE par y solo si el análisis lo confirma.
- Backtest y entrenamiento de modelos en procesos aparte (la ventana no se traba).
- v14: filtro de tendencia multi-temporalidad (1m · 5m · 15m · 1h) obligatorio por defecto: nunca contra la tendencia,
  nunca mientras el retroceso sigue en curso, nunca pegado a un soporte/resistencia de 15m/1h en contra y con una
  duración que encaje con la temporalidad que acompaña. Estrategia nueva «Retroceso MTF». Desglose del backtest
  a favor / en contra / por fase. Respaldo de duración si Exnova no deja comprar a 5 min y vigilante bayesiano.
Se usa como mixin de la ventana principal.
"""
import json
import os
import threading
import time
import tkinter as tk
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tkinter import messagebox

import espyx_workers as W
import strategy as strat
from espyx_ui import (ACCENT, ACCENT2, AMBER, BG, BORDER, CARD, GREEN, INPUT, MUTED, RED, TEXT, Btn, Card,
                      ComboField, Field, ScrollFrame, StatCard, font, is_ccy_pair, make_tree, pretty_pair, rrect,
                      mono, toggle_row)
import news as NEWS
from candle_store import CandleStore
from martingala import PairCompound, PairMartingale

AUTO_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
              "AUDCAD", "EURAUD", "EURCAD", "AUDJPY", "GBPAUD", "GBPCAD", "EURCHF", "NZDJPY"]
KEYS = list(strat.STRATEGIES)
TITLE_SHORT = {"structure": "ESTRUCTURA", "ensemble": "CONJUNTO", "pattern": "PATRONES", "retrace": "RETROCESO", "regime": "ADAPTATIVA",
               "reversion": "REVERSIÓN", "trend": "TENDENCIA", "pullback": "PULLBACK", "breakout": "RUPTURA",
               "prob": "PROBAB."}
TAGS = {"good": "win", "warn": "skip", "bad": "loss", "mute": "muted"}
COLORS = {"good": GREEN, "warn": AMBER, "bad": RED, "mute": MUTED}
MARKETS = ["Automático: el que esté abierto", "Solo mercado normal", "Solo OTC"]
MARKET_KEYS = ["auto", "normal", "otc"]
KINDS = ["binary", "blitz", "binary5"]
KIND_NAMES = ["Binarias · velas de 1 min (vencen a 1–5 min)", "Blitz · velas de 30 s (duran 30 o 60 s)",
              "Binarias · velas de 5 min (vencen a 5 min)"]
KIND_SHORT = {"binary": "Binarias 1m", "blitz": "Blitz", "binary5": "Binarias 5m"}
BAR = {"binary": 60, "blitz": 30, "binary5": 300}            # tamaño de vela (s) que analiza cada tipo de operación
ENTRY_CAP = {"binary": 25, "blitz": 6, "binary5": 25}         # tope del «segundo máximo de entrada»
RESULT_WAIT = {"binary": 6.0, "blitz": 5.0, "binary5": 6.0}  # tope de espera del resultado de lo que vence al cierre
CLOSE_DELAY = 0.4                            # s tras el cierre de la vela para pedir los datos (el servidor la cierra)
MODEL_MAX_AGE = 3600        # los modelos (probabilístico / conjunto / patrones) se reentrenan cada hora con velas frescas
MODEL_CANDLES = 3000
LIVE_CANDLES = {"binary": 2000, "blitz": 3000, "binary5": 800}   # velas por mercado: ≥ 22 h para la tendencia de 1 h
TABLE_MAX_AGE = 120         # cada cuánto se vuelve a preguntar a Exnova qué activos están abiertos
GATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "espyx_gate.json")   # historial Demo (puerta)
NEWS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "espyx_noticias.json")   # v15.2
NEWS_LEVELS = [("High",), ("High", "Medium")]
NEWS_LEVEL_NAMES = ["Solo alto impacto (rojas)", "Alto y medio impacto (rojas y naranjas)"]
CANDLES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "velas")          # v15.2: velas guardadas
VIG_WINDOW = 40             # vigilante: operaciones en vivo recientes que mira (por estrategia y mercado)
VIG_MIN = 5                 # vigilante: mínimo de operaciones en vivo para poder pausar
VIG_PRIOR = 16              # vigilante: cuánto pesa (en operaciones) la efectividad del backtest como punto de partida
VIG_P = 0.80                # vigilante: pausa si es más probable que esto que la efectividad real esté bajo el equilibrio
HEALTH_BLOCK = 3600         # segundos que se pausa una estrategia degradada
DUR_BLOCK = 1800            # segundos que se deja una duración que Exnova rechazó con «Time for purchasing options is over»
TREND_NAMES = ["Obligatorio: nunca contra la tendencia ni el retroceso", "Que lo decida el backtest (por estrategia)",
               "Desactivado (como la v13)"]
DUR_NAMES = ["Según la tendencia: la duración validada que encaje", "Automática: la mejor por par (backtest)",
             "Fija: la de Configurar (Blitz: 30 s)"]
DUR_KEYS = ["trend", "auto", "fixed"]
PAIR_FILTER_NAMES = ["Solo los que superan el equilibrio (backtest)", "Solo los N mejores por efectividad confiable",
                     "Todos los elegidos (aunque no tengan efectividad)"]
PAIR_FILTER_KEYS = ["good", "top", "all"]
VERDICT_SHORT = {"Supera equilibrio": "Supera equil.", "Como el azar": "Como azar", "Pierde dinero": "Pierde"}
STATUS_COL = {"normal": GREEN, "otc": AMBER, "closed": RED, None: "#3A404B"}
CATEGORIES = ["Forex", "Materias primas", "Índices", "Acciones", "Cripto"]
_FX = {"USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK", "DKK", "PLN", "HUF", "CZK", "TRY", "MXN",
       "ZAR", "SGD", "HKD", "THB", "INR", "BRL", "RUB", "ILS", "XOF", "CNH", "CNY"}
_METALS = ("XAU", "XAG", "XPT", "XPD")
_COMMOD = {"UKOUSD", "USOUSD", "XNGUSD", "NATGAS", "COPPER", "OIL", "BRENT", "WTI", "GOLD", "SILVER"}
_INDEX_KEYS = ("US30", "US100", "US500", "US2000", "SPX500", "SP500", "NDAQ100", "NASDAQ", "GER30", "GERMANY30", "DAX",
               "UK100", "FTSE", "FRANCE40", "FR40", "EU50", "JAPAN225", "JP225", "NIKKEI", "HK33", "HONGKONG50",
               "AUS200", "SPAIN35", "SP35", "DOLLAR_INDEX", "YEN_INDEX", "DXY", "VIX", "TA25")


def asset_category(name):
    """Categoría de un activo de Exnova por su nombre (sin -OTC)."""
    n = name.upper().replace("-OTC", "")
    if n.endswith("-L"):
        n = n[:-2]
    flat = n.replace("/", "")
    if any(k in n for k in _INDEX_KEYS):
        return "Índices"
    if flat in _COMMOD or (len(flat) == 6 and flat[:3] in _METALS) or n == "XAU/XAG":
        return "Materias primas"
    if len(flat) == 6 and flat.isalpha() and flat[:3] in _FX and flat[3:] in _FX:
        return "Forex"
    if flat.endswith("USD") and flat[:-3] and flat[:-3] not in _FX:
        return "Cripto"
    return "Acciones"


def mmss(seconds):
    s = max(0, int(seconds + 0.999))
    return f"{s // 60:02d}:{s % 60:02d}"


def rate_color(rate):
    return GREEN if rate >= 0.55 else AMBER if rate >= 0.45 else RED


def parse_hhmm(text, name):
    try:
        h, m = text.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        raise ValueError(f"«{name}» debe tener el formato HH:MM (ej. 08:30).")
    return h * 60 + m


def split_full(full):
    return full.replace("-OTC", ""), full.endswith("-OTC")


def dur_label(m, kind):
    """Duración legible: 2 velas de 1 min = «2 min»; 2 velas de 30 s = «60 s»; 1 vela de 5 min = «5 min»."""
    if kind == "blitz":
        return f"{m * BAR['blitz']} s"
    return f"{m * BAR[kind] // 60} min"


def dkey(m, kind):
    """Clave de duración para la puerta Demo → Real y el vigilante (compatible con espyx_gate.json de la v12)."""
    return {"binary": str(m), "blitz": f"b{m}", "binary5": f"v{m}"}[kind]


def dkey_kind(dk):
    dk = str(dk)
    return "blitz" if dk.startswith("b") else "binary5" if dk.startswith("v") else "binary"


def dkey_label(dk):
    dk = str(dk)
    if dk.startswith("b"):
        return f"{int(dk[1:]) * BAR['blitz']} s Blitz"
    if dk.startswith("v"):
        return f"{int(dk[1:]) * 5} min (velas 5m)"
    return f"{dk} min"


def purchase_closed(msg):
    """¿Exnova rechazó la compra porque esa expiración ya no admite compras? (pasa en algunos pares con la de 5 min)"""
    return "time for purchasing" in str(msg or "").lower()


def active_not_found(msg):
    """Exnova no reconoce el activo en esa modalidad («Active %!s(MISSING) not found»)."""
    m = str(msg or "").lower()
    return "active" in m and "not found" in m


def pct_txt(wl, be=None):
    """«58% (120)» de un par [ganadas, perdidas]; con `be` añade los puntos sobre/bajo el equilibrio."""
    w, l = wl
    n = w + l
    if not n:
        return "—"
    return f"{w / n * 100:.0f}% ({n})" + (f" {(w / n - be) * 100:+.0f}pp" if be is not None else "")


# ------------------------------------------------------------------ piezas visuales propias
class PairChip(tk.Canvas):
    """Ficha seleccionable con las banderas del par y un punto de estado del mercado."""

    def __init__(self, master, pair, icons, command, bg=CARD, width=152, height=40):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.pair, self.icons, self.command = pair, icons, command
        self.on, self._cw, self._ch, self.status = False, width, height, None
        self._drawn = None
        self.bind("<Button-1>", self._click)
        self._draw()

    def set(self, on):
        if self.on != on:
            self.on = on
            self._draw()

    def set_status(self, status):
        if self.status != status:
            self.status = status
            self._draw()

    def _click(self, _e):
        self.on = not self.on
        self._draw()
        self.command(self.pair, self.on)

    def _draw(self):
        self.delete("all")
        w, h = self._cw, self._ch
        fill = "#221D12" if self.on else INPUT
        rrect(self, 1, 1, w - 1, h - 1, 5, fill=fill, outline=ACCENT if self.on else BORDER, width=1)
        img = self.icons.pair(self.pair, 22, fill)
        self.create_image(10, h / 2, image=img, anchor="w")
        label = pretty_pair(self.pair)
        x0 = 10 + img.width() + 8
        size = 10 if len(label) <= 8 else 9 if len(label) <= 10 else 8
        if len(label) > 13:
            label = label[:12] + "…"
        self.create_text(x0, h / 2, text=label, anchor="w", fill=TEXT if self.on else MUTED, font=font(size, True))
        if self.status == "both":                     # normal y OTC abiertos: punto verde + punto naranja
            self.create_oval(w - 27, h / 2 - 4, w - 19, h / 2 + 4, fill=GREEN, outline="")
            self.create_oval(w - 16, h / 2 - 4, w - 8, h / 2 + 4, fill=AMBER, outline="")
        else:
            self.create_oval(w - 17, h / 2 - 4, w - 9, h / 2 + 4, fill=STATUS_COL[self.status], outline="")


# ------------------------------------------------------------------ el mixin
class AutoMixin:
    # ============================================================ estado
    def _auto_init(self):
        self.auto_flag = threading.Event()     # modo automático activado
        self.auto_cfg = {}                      # configuración que lee el motor
        self.auto_sel = {"EURUSD"}              # pares elegidos (nombre base)
        self.auto_kind = "binary"               # tipo de operación elegido en la pestaña
        self._res = {k: {} for k in KINDS}      # tipo -> mercado (EURUSD / EURUSD-OTC) -> resultados del backtest
        self._best = {k: {} for k in KINDS}     # tipo -> mercado -> estrategia ganadora
        self.auto_live = {}                     # (estrategia, mercado, tipo) -> {"n","w","l"}
        self.vstore = CandleStore(CANDLES_DIR)   # v15.2: velas guardadas en el PC (solo se baja lo nuevo)
        self.news = NEWS.NewsCalendar(NEWS_FILE)    # v15.2: calendario económico de la semana
        self.auto_models = {}                   # (mercado, estrategia, duración, tipo, params) -> {"model","at","training"}
        self._models_lock = threading.Lock()
        self.auto_entries = []                  # instantes de las últimas entradas (límite por hora)
        self.auto_status = {"t": 0.0, "table": None}   # estado de los activos según Exnova
        self._status_lock = threading.Lock()
        self.auto_pause_until = 0.0
        self.mg_auto = PairMartingale()         # martingala por par del modo automático
        self.ic_auto = PairCompound()           # v15: ciclos de interés compuesto por par
        self.auto_roll = {}                     # (estrategia, mercado, tipo) -> últimos resultados en vivo (vigilante)
        self.auto_blocked = {}                  # (estrategia, mercado, tipo) -> (hasta, motivo) pausas del vigilante
        self.auto_dur_block = {}                # (mercado, duración, tipo) -> (hasta, motivo) duraciones que Exnova no acepta
        self.auto_gate = self._gate_load()      # "estrategia|mercado|duración" -> {"n","w","l"} solo cuentas Demo (puerta)
        self._cc = {}                           # (mercado, vela) -> velas cerradas recientes (caché en vivo)
        self._cc_lock = threading.Lock()
        self._cc_filled = {}                    # (mercado, vela) -> cuándo se descargó su histórico completo
        self._cc_filling = set()                # históricos que se están descargando en segundo plano
        self._cc_fail = {}                      # (mercado, vela) -> último fallo al descargar su histórico
        self._io = ThreadPoolExecutor(max_workers=6, thread_name_prefix="espyx-io")
        self._fill_io = ThreadPoolExecutor(max_workers=2, thread_name_prefix="espyx-fill")
        self.pool = W.WorkPool(on_progress=self._pool_progress)
        self.auto_bt_running = False
        self._auto_reverting = False
        self._last_compare = 0.0
        self._auto_thread = None
        self._watch_thread = None
        self._engine_lock = threading.Lock()    # nunca dos motores a la vez (duplicarían las órdenes)
        self._next_analysis = None
        self.chips = {}
        self.auto_catalog = {p: "Forex" for p in AUTO_PAIRS}   # activo (sin -OTC) -> categoría (se amplía al detectar)
        self.auto_category = "Forex"

    @property
    def auto_results(self):
        return self._res[self.auto_kind]

    @property
    def auto_best(self):
        return self._best[self.auto_kind]

    # ============================================================ página
    def _build_auto(self, page):
        sf = ScrollFrame(page)
        sf.pack(fill="both", expand=True)
        root = sf.inner
        self._auto_scroll = sf

        # ---- tarjetas de indicadores (efectividad dinámica en el mismo estilo del Panel)
        cards = tk.Frame(root, bg=BG)
        cards.pack(fill="x")
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1, uniform="astat")
        self.card_a_state = StatCard(cards, "MODO AUTOMÁTICO", height=104)
        self.card_a_live = StatCard(cards, "EFECTIVIDAD EN VIVO · AUTO", height=104)
        self.card_a_bt = StatCard(cards, "BACKTEST", height=104)
        self.card_a_next = StatCard(cards, "PRÓXIMO ANÁLISIS", height=104)
        for i, c in enumerate((self.card_a_state, self.card_a_live, self.card_a_bt, self.card_a_next)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 3 else 7))

        # ---- pares
        pc = Card(root, "Pares a operar")
        pc.pack(fill="x", pady=(14, 0))
        tools = tk.Frame(pc.head, bg=CARD)
        tools.pack(side="right")
        Btn(tools, "Ninguno", lambda: self._auto_pick_all(False), kind="ghost", width=90, height=30, size=9,
            bg=CARD).pack(side="right", padx=(8, 0))
        Btn(tools, "Todos", lambda: self._auto_pick_all(True), kind="ghost", width=80, height=30, size=9,
            bg=CARD).pack(side="right", padx=(8, 0))
        self.detect_btn = Btn(tools, "Detectar abiertos hoy", self._auto_detect, kind="primary", width=190, height=30,
                              size=9, bg=CARD)
        self.detect_btn.pack(side="right")
        catrow = tk.Frame(pc.body, bg=CARD)
        catrow.pack(fill="x", pady=(0, 10))
        tk.Label(catrow, text="Categoría:", bg=CARD, fg=MUTED, font=font(9, True)).pack(side="left", padx=(0, 8))
        self.cat_btns = {}
        for cat in CATEGORIES:
            b_ = Btn(catrow, cat, lambda c=cat: self._auto_set_category(c), kind="ghost", width=150, height=30, size=9,
                     bg=CARD)
            b_.pack(side="left", padx=(0, 8))
            self.cat_btns[cat] = b_
        self.cat_info = tk.Label(catrow, text="", bg=CARD, fg=MUTED, font=font(9))
        self.cat_info.pack(side="left", padx=(6, 0))
        self.chip_grid = tk.Frame(pc.body, bg=CARD)
        self.chip_grid.pack(fill="x")
        self._auto_build_chips()
        leg = tk.Frame(pc.body, bg=CARD)
        leg.pack(fill="x")
        for col, text in ((GREEN, "mercado normal abierto"), (AMBER, "OTC abierto"), (RED, "cerrado"),
                          ("#3A404B", "sin consultar")):
            tk.Label(leg, text="●", bg=CARD, fg=col, font=font(10)).pack(side="left")
            tk.Label(leg, text=text, bg=CARD, fg=MUTED, font=font(9)).pack(side="left", padx=(3, 16))
        tk.Label(leg, text="●", bg=CARD, fg=GREEN, font=font(10)).pack(side="left")
        tk.Label(leg, text="●", bg=CARD, fg=AMBER, font=font(10)).pack(side="left")
        tk.Label(leg, text="los dos abiertos", bg=CARD, fg=MUTED, font=font(9)).pack(side="left", padx=(3, 16))
        tk.Label(pc.body, text="Pulsa «Detectar abiertos hoy»: consulta a Exnova TODO lo que se puede operar ahora (divisas, "
                               "materias primas, índices, acciones y cripto), arma las categorías y marca los abiertos de la "
                               "categoría que estás viendo. Lo elegido en otras categorías se conserva. Puedes cambiar los "
                               "pares en cualquier momento, incluso con el bot en marcha.",
                 bg=CARD, fg=MUTED, font=font(9), anchor="w", wraplength=1080, justify="left").pack(fill="x", pady=(6, 0))

        # ---- cuerpo: control | ranking + decisiones
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, pady=(14, 0))
        body.grid_columnconfigure(0, weight=2, uniform="ab")
        body.grid_columnconfigure(1, weight=3, uniform="ab")

        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctl = Card(left, "Control del modo automático")
        ctl.pack(fill="x")
        b = ctl.body
        self.auto_on = tk.BooleanVar(value=False)
        toggle_row(b, "Operar automáticamente", self.auto_on).pack(anchor="w", pady=(0, 12))
        self.auto_on.trace_add("write", lambda *a: self._auto_toggled())
        self.auto_kind_cb = ComboField(b, "Tipo de operación", KIND_NAMES)
        self.auto_kind_cb.pack(fill="x", pady=(0, 6))
        self.auto_kind_cb.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_kind_changed())
        self.auto_kind_desc = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left",
                                       anchor="w")
        self.auto_kind_desc.pack(fill="x", pady=(0, 10))
        names = ["Mejor por par (según backtest)"] + [strat.STRATEGIES[k] for k in KEYS]
        self.auto_strat = ComboField(b, "Estrategia con la que operar", names)
        self.auto_strat.current(1 + KEYS.index("structure"))      # v15: la estrategia principal por defecto
        self.auto_strat.pack(fill="x", pady=(0, 6))
        self.auto_strat.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_option_changed())
        self.auto_desc = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left",
                                  anchor="w")
        self.auto_desc.pack(fill="x", pady=(0, 10))
        pf = tk.Frame(b, bg=CARD)
        pf.pack(fill="x", pady=(0, 6))
        pf.grid_columnconfigure(0, weight=3)
        pf.grid_columnconfigure(1, weight=1)
        self.auto_pairf = ComboField(pf, "Qué pares operar", PAIR_FILTER_NAMES)
        self.auto_pairf.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.auto_pairf.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        self.auto_topn = Field(pf, "N mejores", "3")
        self.auto_topn.grid(row=0, column=1, sticky="ew")
        self.auto_topn.entry.bind("<FocusOut>", lambda e: self._auto_sync())
        self.auto_topn.entry.bind("<Return>", lambda e: self._auto_sync())
        self.auto_pairf_desc = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left",
                                        anchor="w")
        self.auto_pairf_desc.pack(fill="x", pady=(0, 10))
        self.auto_trend = ComboField(b, "Filtro de tendencia (1m · 5m · 15m · 1h)", TREND_NAMES)
        self.auto_trend.pack(fill="x", pady=(0, 6))
        self.auto_trend.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_trend_changed())
        self.auto_trend_desc = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left",
                                        anchor="w")
        self.auto_trend_desc.pack(fill="x", pady=(0, 10))
        self.auto_market = ComboField(b, "Mercado a operar", MARKETS)
        self.auto_market.pack(fill="x", pady=(0, 10))
        self.auto_market.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        self.auto_dur = ComboField(b, "Duración de las operaciones", DUR_NAMES)
        self.auto_dur.pack(fill="x", pady=(0, 6))
        self.auto_dur.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        self.auto_dur_desc = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left",
                                      anchor="w")
        self.auto_dur_desc.pack(fill="x", pady=(0, 10))
        dr = tk.Frame(b, bg=CARD)
        dr.pack(fill="x")
        dr.grid_columnconfigure(0, weight=1, uniform="dr")
        dr.grid_columnconfigure(1, weight=1, uniform="dr")
        self.auto_durs = Field(dr, "Binarias: duraciones (min)", "1,2,3,4,5")
        self.auto_bdurs = Field(dr, "Blitz: duraciones (s)", "30,60")
        self.auto_durs.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.auto_bdurs.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 10))
        g = tk.Frame(b, bg=CARD)
        g.pack(fill="x")
        g.grid_columnconfigure(0, weight=1, uniform="ac")
        g.grid_columnconfigure(1, weight=1, uniform="ac")
        self.auto_payout = Field(g, "Pago de respaldo (%)", "85")
        self.auto_minpay = Field(g, "Pago mínimo para operar (%)", "80")
        self.auto_maxopen = Field(g, "Máx. operaciones abiertas", "1")
        self.auto_perhour = Field(g, "Máx. entradas por hora", "12")
        self.auto_margin = Field(g, "Margen de confianza prob. (%)", "2")
        self.auto_pause = Field(g, "Pausa tras perder (min)", "0")
        self.auto_window = Field(g, "Entrar como máximo al segundo", "8")
        flds = (self.auto_payout, self.auto_minpay, self.auto_maxopen, self.auto_perhour, self.auto_margin,
                self.auto_pause, self.auto_window)
        for i, f in enumerate(flds):
            f.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 6) if i % 2 == 0 else (6, 0), pady=(0, 10))
            f.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f.entry.bind("<Return>", lambda e: self._auto_sync())
        for f in (self.auto_durs, self.auto_bdurs):
            f.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f.entry.bind("<Return>", lambda e: self._auto_sync())
        tk.Label(b, text="El bot analiza en el instante en que cierra la vela (con la hora del servidor de Exnova) y entra "
                         "en los primeros segundos de la siguiente. Si por la red o por la cantidad de pares la orden no "
                         "alcanza a salir antes de este segundo, NO entra: así nunca abre una operación de 1:30 que el "
                         "backtest no midió. En Blitz el máximo útil es 6 s.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(0, 10))
        self.auto_hours = tk.BooleanVar(value=False)
        toggle_row(b, "Operar solo dentro de un horario", self.auto_hours).pack(anchor="w", pady=(2, 8))
        self.auto_hours.trace_add("write", lambda *a: self._auto_sync())
        hr = tk.Frame(b, bg=CARD)
        hr.pack(fill="x")
        hr.grid_columnconfigure(0, weight=1, uniform="hr")
        hr.grid_columnconfigure(1, weight=1, uniform="hr")
        self.auto_from = Field(hr, "Desde (HH:MM)", "08:00")
        self.auto_to = Field(hr, "Hasta (HH:MM)", "12:00")
        self.auto_from.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.auto_to.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 10))
        for f in (self.auto_from, self.auto_to):
            f.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f.entry.bind("<Return>", lambda e: self._auto_sync())
        tk.Label(b, text="El horario usa la hora de tu PC. La mayor liquidez suele darse cuando se solapan Londres y "
                         "Nueva York; ajústalo a tu zona horaria. Una pausa de 2–5 min tras perder evita seguir "
                         "entrando cuando el mercado cambia de comportamiento (no frena los gales de la martingala).",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(10, 0))

        # ---- v15.2: filtro de noticias
        nw = Card(left, "Filtro de noticias económicas")
        nw.pack(fill="x", pady=(14, 0))
        b = nw.body
        self.auto_news = tk.BooleanVar(value=True)
        toggle_row(b, "No operar cerca de noticias (mercado normal)", self.auto_news).pack(anchor="w", pady=(0, 10))
        self.auto_news.trace_add("write", lambda *a: self._auto_sync())
        self.auto_news_lvl = ComboField(b, "Qué noticias frenan al bot", NEWS_LEVEL_NAMES)
        self.auto_news_lvl.pack(fill="x", pady=(0, 8))
        self.auto_news_lvl.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        nrow = tk.Frame(b, bg=CARD)
        nrow.pack(fill="x", pady=(0, 6))
        nrow.grid_columnconfigure(0, weight=1, uniform="nw")
        nrow.grid_columnconfigure(1, weight=1, uniform="nw")
        self.auto_news_before = Field(nrow, "Minutos antes", "15")
        self.auto_news_before.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.auto_news_after = Field(nrow, "Minutos después", "15")
        self.auto_news_after.grid(row=0, column=1, sticky="ew")
        for f_ in (self.auto_news_before, self.auto_news_after):
            f_.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f_.entry.bind("<Return>", lambda e: self._auto_sync())
        self.auto_news_otc = tk.BooleanVar(value=False)
        toggle_row(b, "Aplicarlo también en OTC", self.auto_news_otc).pack(anchor="w", pady=(4, 8))
        self.auto_news_otc.trace_add("write", lambda *a: self._auto_sync())
        hrow = tk.Frame(b, bg=CARD)
        hrow.pack(fill="x", pady=(2, 6))
        tk.Label(hrow, text="Próximas noticias de tus pares", bg=CARD, fg=TEXT, font=font(10, True),
                 anchor="w").pack(side="left")
        self.news_btn = Btn(hrow, "Actualizar", lambda: self._auto_news_refresh(force=True), kind="ghost", width=100,
                            height=28, size=9, bg=CARD)
        self.news_btn.pack(side="right")
        self.news_list = tk.Label(b, text="Calendario sin descargar (se baja al iniciar sesión).", bg=CARD, fg=MUTED,
                                  font=mono(9), wraplength=380, justify="left", anchor="w")
        self.news_list.pack(fill="x")
        self.news_state = tk.Label(b, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=380, justify="left",
                                   anchor="w")
        self.news_state.pack(fill="x", pady=(6, 0))

        # ---- apuesta del modo automático (fijo / martingala por par / Kelly)
        sk = Card(left, "Apuesta del modo automático")
        sk.pack(fill="x", pady=(14, 0))
        self.auto_stake = ComboField(sk.body, "Cómo se calcula el monto",
                                     ["Fijo (el monto de Configurar)",
                                      "Interés compuesto por par (ciclos)",
                                      "Martingala por par (gale en la siguiente vela)",
                                      "Proporcional a la ventaja (Kelly)"])
        self.auto_stake.current(1)                   # v15: interés compuesto en lugar de martingala
        self.auto_stake.pack(fill="x", pady=(0, 10))
        self.auto_stake.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        sg = tk.Frame(sk.body, bg=CARD)
        sg.pack(fill="x")
        for c in (0, 1):
            sg.grid_columnconfigure(c, weight=1, uniform="sg")
        self.auto_mgmult = Field(sg, "MG · Multiplicador", "2")
        self.auto_mglevels = Field(sg, "MG · Niveles máximos", "2")
        self.auto_mgwait = Field(sg, "MG · Velas para confirmar", "1")
        self.auto_kfrac = Field(sg, "Kelly · Fracción (%)", "25")
        self.auto_kcap = Field(sg, "Kelly · Tope (% capital)", "5")
        self.auto_icsteps = Field(sg, "IC · Reinversiones por ciclo", "1")
        for i, f in enumerate((self.auto_icsteps, self.auto_mgmult, self.auto_mglevels, self.auto_mgwait, self.auto_kfrac,
                               self.auto_kcap)):
            f.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 6) if i % 2 == 0 else (6, 0), pady=(0, 10))
            f.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f.entry.bind("<Return>", lambda e: self._auto_sync())
        self.auto_mgdir = ComboField(sk.body, "MG · Dirección del gale",
                                     ["La que indique el análisis en esa vela", "Solo si repite la dirección que perdió"])
        self.auto_mgdir.pack(fill="x", pady=(0, 10))
        self.auto_mgdir.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        self.auto_mgend = ComboField(sk.body, "MG · Al agotar los niveles", ["Volver al monto base", "Desactivar el modo automático"])
        self.auto_mgend.pack(fill="x", pady=(0, 8))
        self.auto_mgend.cb.bind("<<ComboboxSelected>>", lambda e: self._auto_sync())
        tk.Label(sk.body, text="Interés compuesto POR PAR (recomendado): si un par gana, su siguiente operación va por "
                               "monto + ganancia; tras completar las reinversiones del ciclo (1 = dos ganadas seguidas) o al "
                               "perder, vuelve al monto base. Un ciclo nunca pierde más que el monto base.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(0, 8))
        tk.Label(sk.body, text="Martingala POR PAR: cada divisa lleva su propio nivel. Si en una vela entran 4 pares y pierden 2, "
                               "en la siguiente vela de ESOS 2 el bot vuelve a analizar y solo entra (monto × multiplicador) si "
                               "su análisis da señal; los que ganaron siguen con el monto base. Si el análisis no confirma "
                               "dentro de las «velas para confirmar», el ciclo se cierra y ese par vuelve al monto base. "
                               "No cambia la efectividad por operación: solo aumenta lo que arriesgas tras perder (mira la "
                               "simulación). Kelly apuesta más cuanto mayor es la ventaja estimada y NO entra si no hay ventaja.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x")

        # ---- mejoras de efectividad
        im = Card(left, "Mejoras de efectividad")
        im.pack(fill="x", pady=(14, 0))
        self.auto_htf = tk.BooleanVar(value=True)
        self.auto_vol = tk.BooleanVar(value=True)
        self.auto_pat = tk.BooleanVar(value=True)
        self.auto_ccy = tk.BooleanVar(value=True)
        self.auto_health = tk.BooleanVar(value=True)
        self.auto_gate_on = tk.BooleanVar(value=True)
        toggle_row(im.body, "Permitir confirmar con la tendencia mayor (velas ×5)", self.auto_htf).pack(anchor="w", pady=(0, 6))
        toggle_row(im.body, "Permitir filtro de volatilidad", self.auto_vol).pack(anchor="w", pady=(0, 6))
        toggle_row(im.body, "Permitir confirmar con patrones históricos", self.auto_pat).pack(anchor="w", pady=(0, 6))
        tk.Label(im.body, text="El backtest decide, para cada estrategia y duración, qué filtros usar. «Patrones históricos» "
                               "veta una entrada si, en las situaciones parecidas del pasado, el precio fue más veces en "
                               "sentido contrario.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(0, 10))
        toggle_row(im.body, "Limitar pares correlacionados", self.auto_ccy).pack(anchor="w", pady=(0, 6))
        self.auto_ccymax = Field(im.body, "Máx. operaciones por divisa y sentido", "1")
        self.auto_ccymax.pack(fill="x", pady=(0, 10))
        toggle_row(im.body, "Vigilante de salud de las estrategias", self.auto_health).pack(anchor="w", pady=(0, 6))
        tk.Label(im.body, text=f"Parte de la efectividad del backtest y la corrige con cada resultado en vivo (últimas "
                               f"{VIG_WINDOW} ops). Desde {VIG_MIN} operaciones, si es más probable que no (≥ "
                               f"{VIG_P * 100:.0f}%) que esa estrategia esté rindiendo bajo el equilibrio en ese par, la pausa "
                               f"{HEALTH_BLOCK // 60} min y «Mejor por par» pasa a la siguiente (antes hacían falta 15 "
                               f"operaciones y casi nunca saltaba).",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(0, 10))
        self.auto_recmp = Field(im.body, "Volver a comparar solo cada (horas, 0 = nunca)", "0")
        self.auto_recmp.pack(fill="x", pady=(0, 6))
        tk.Label(im.body, text="Con el bot en marcha repite la comparación con velas frescas para que «Mejor por par» se adapte "
                               "si el mercado cambia de comportamiento (corre aparte, no frena las entradas).",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=360, justify="left", anchor="w").pack(fill="x", pady=(0, 10))
        toggle_row(im.body, "Exigir validación en Demo antes de operar en Real", self.auto_gate_on).pack(anchor="w", pady=(0, 6))
        self.auto_gatemin = Field(im.body, "Ops mínimas en Demo por estrategia", "30")
        self.auto_gatemin.pack(fill="x", pady=(0, 6))
        for f in (self.auto_ccymax, self.auto_gatemin, self.auto_recmp):
            f.entry.bind("<FocusOut>", lambda e: self._auto_sync())
            f.entry.bind("<Return>", lambda e: self._auto_sync())
        for v in (self.auto_htf, self.auto_vol, self.auto_pat, self.auto_ccy, self.auto_health, self.auto_gate_on):
            v.trace_add("write", lambda *a: self._auto_sync())

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        rk = Card(right, "Ranking de estrategias por par (backtest)")
        rk.pack(fill="x")
        tools = tk.Frame(rk.head, bg=CARD)
        tools.pack(side="right")
        self.compare_btn = Btn(tools, "Comparar estrategias", self._auto_compare, kind="primary", width=190,
                               height=32, size=9, bg=CARD)
        self.compare_btn.pack(side="right")
        top = tk.Frame(rk.body, bg=CARD)
        top.pack(fill="x", pady=(0, 10))
        top.grid_columnconfigure(0, weight=1, uniform="bt")
        top.grid_columnconfigure(1, weight=1, uniform="bt")
        top.grid_columnconfigure(2, weight=1, uniform="bt")
        self.bt_count = Field(top, "Velas a analizar", "20000")
        self.bt_opt = tk.BooleanVar(value=True)
        self.bt_count.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        opt = toggle_row(top, "Ajustar parámetros", self.bt_opt)
        opt.grid(row=0, column=1, sticky="sw", pady=(0, 6))
        self.pick_good_btn = Btn(top, "✔ Elegir confirmados", self._auto_pick_confirmed, kind="ghost", width=170,
                                 height=34, size=9, bg=CARD)
        self.pick_good_btn.grid(row=0, column=2, sticky="se", pady=(0, 2))
        srow = tk.Frame(rk.body, bg=CARD)
        srow.pack(fill="x", pady=(0, 10))
        self.store_lbl = tk.Label(srow, text="", bg=CARD, fg=MUTED, font=font(9), anchor="w", justify="left",
                                  wraplength=420)
        self.store_lbl.pack(side="left", fill="x", expand=True)
        Btn(srow, "Borrar velas guardadas", self._auto_store_clear, kind="ghost", width=170, height=28, size=9,
            bg=CARD).pack(side="right")
        self._auto_store_label()
        fr, self.rank_tree = make_tree(
            rk.body, "Mercado", [("estr", "Estrategia", 96, "w"), ("cfg", "Dur.", 50, "w"),
                                 ("selr", "Selec.", 52, "center"), ("pct", "Prueba", 76, "center"),
                                 ("vs", "vs eq.", 58, "center"), ("ver", "Veredicto", 104, "w"),
                                 ("live", "En vivo", 52, "center")],
            height=8, icon_w=118)
        fr.pack(fill="x")
        self.auto_prog = tk.Label(rk.body, text="Pulsa «Comparar estrategias»: descarga las velas anteriores de cada par "
                                                "elegido (del tipo de operación elegido), prueba las 9 estrategias con el "
                                                "filtro de tendencia elegido y marca la mejor (★). ✔ Confirmada = supera el equilibrio en la "
                                                "selección Y en la prueba final. 20.000 velas de 1 min ≈ "
                                                "14 días. Las velas quedan guardadas en el PC: la próxima vez solo se baja "
                                                "lo nuevo. Corre en segundo plano: puedes seguir usando la ventana.",
                                  bg=CARD, fg=MUTED, font=font(9), wraplength=560, justify="left", anchor="w")
        self.auto_prog.pack(fill="x", pady=(10, 0))

        du = Card(right, "Efectividad según la duración (fila elegida del ranking)")
        du.pack(fill="x", pady=(14, 0))
        self.dur_target = tk.Label(du.body, text="Elige una fila del ranking para ver cómo rinde a cada duración.",
                                   bg=CARD, fg=TEXT, font=font(10, True), anchor="w", wraplength=600, justify="left")
        self.dur_target.pack(fill="x", pady=(0, 8))
        fr, self.dur_tree = make_tree(
            du.body, "Duración", [("ops", "Ops", 50, "center"), ("pct", "Efect.", 70, "center"),
                                  ("vs", "vs equil.", 80, "center"), ("cfg", "Filtros", 130, "w"),
                                  ("ver", "Veredicto", 110, "w")],
            height=5, icon_w=110)
        fr.pack(fill="x")
        self.dur_note = tk.Label(du.body, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=560, justify="left",
                                 anchor="w")
        self.dur_note.pack(fill="x", pady=(10, 0))

        cx = Card(right, "¿A favor o en contra de la tendencia? (backtest de la fila elegida)")
        cx.pack(fill="x", pady=(14, 0))
        self.ctx_target = tk.Label(cx.body, text="Elige una fila del ranking para ver cómo rindieron sus señales según el "
                                                 "contexto.", bg=CARD, fg=TEXT, font=font(10, True), anchor="w",
                                   wraplength=600, justify="left")
        self.ctx_target.pack(fill="x", pady=(0, 8))
        fr, self.ctx_tree = make_tree(
            cx.body, "Situación de la entrada", [("ops", "Ops", 50, "center"), ("pct", "Efect.", 70, "center"),
                                                 ("vs", "vs equil.", 80, "center"), ("all", "Todas las estrategias", 150,
                                                                                     "center")],
            height=13, icon_w=250)
        fr.pack(fill="x")
        self.ctx_note = tk.Label(cx.body, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=560, justify="left",
                                 anchor="w")
        self.ctx_note.pack(fill="x", pady=(10, 0))

        mg = Card(right, "Gestión de capital: fijo vs interés compuesto vs martingala (sobre el backtest)")
        mg.pack(fill="x", pady=(14, 0))
        self.mg_target = tk.Label(mg.body, text="Elige una fila del ranking (o se usa la mejor del primer par).",
                                  bg=CARD, fg=TEXT, font=font(10, True), anchor="w", wraplength=600, justify="left")
        self.mg_target.pack(fill="x", pady=(0, 8))
        fr, self.mg_tree = make_tree(
            mg.body, "Escenario", [("ciclos", "Ciclos ✔", 75, "center"), ("net", "Neto", 90, "center"),
                                   ("staked", "Apostado", 80, "center"), ("yield", "Rend.", 70, "center"),
                                   ("dd", "Peor caída", 85, "center"), ("maxs", "Máx.", 55, "center")],
            height=6, icon_w=170)
        fr.pack(fill="x")
        self.mg_note = tk.Label(mg.body, text="", bg=CARD, fg=MUTED, font=font(9), wraplength=560, justify="left",
                                anchor="w")
        self.mg_note.pack(fill="x", pady=(10, 0))
        self.rank_tree.bind("<<TreeviewSelect>>", lambda e: self._auto_sel_changed())

        hl = Card(right, "Salud en vivo y puerta Demo → Real")
        hl.pack(fill="x", pady=(14, 0))
        fr, self.health_tree = make_tree(
            hl.body, "Mercado", [("est", "Estrategia", 135, "w"), ("ops", "Ops", 42, "center"),
                                 ("pct", "Efect.", 52, "center"), ("lo", "IC inf.", 56, "center"),
                                 ("state", "Estado", 170, "w")],
            height=4, icon_w=140)
        fr.pack(fill="x")
        tk.Label(hl.body, text="Para operar en Real, cada estrategia debe haber acumulado en Demo las operaciones mínimas y que el "
                               "límite inferior de su efectividad supere el equilibrio. Solo cuentan las operaciones automáticas "
                               "en cuenta Demo. Se guarda en espyx_gate.json.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=560, justify="left", anchor="w").pack(fill="x", pady=(10, 0))

        dc = Card(right, "Decisiones del bot (razonamiento)")
        dc.pack(fill="x", pady=(14, 0))
        fr, self.dec_tree = make_tree(
            dc.body, "Mercado", [("hora", "Hora", 80, "center"), ("estr", "Estrategia", 110, "w"),
                                 ("dec", "Decisión", 130, "center"), ("why", "Motivo", 250, "w")],
            height=6, icon_w=140)
        fr.pack(fill="x")

        self._auto_kind_changed(sync=False)
        self._auto_option_changed(update_only=True)
        self._auto_trend_changed(sync=False)
        self._auto_dur_desc_update()
        self._auto_refresh_cards()
        for fld in (self.amount, self.auto_mgmult):
            fld.entry.bind("<FocusOut>", lambda e: self._auto_mg_refresh(), add="+")
            fld.entry.bind("<Return>", lambda e: self._auto_mg_refresh(), add="+")
        self.after(1500, lambda: self._auto_news_refresh())
        self.after(2000, self._auto_news_tick)

    # ============================================================ v15.2: filtro de noticias
    def _auto_news_refresh(self, force=False):
        """Descarga el calendario en segundo plano (si toca o si se pulsa «Actualizar»)."""
        self.news_btn.set_enabled(False)
        self.news_btn.set_text("…")

        def done(ok, msg):
            def ui():
                self.news_btn.set_enabled(True)
                self.news_btn.set_text("Actualizar")
                if force or not ok:
                    self._log(f"Calendario de noticias: {msg}")
                self._auto_news_render()
            self._ui(ui)
        self.news.refresh_async(force, done)

    def _auto_news_tick(self):
        """Cada 30 s: redibuja la lista (cuenta regresiva) y cada 4 h vuelve a bajar el calendario."""
        try:
            if time.time() - self.news.fetched > NEWS.REFRESH and time.time() - getattr(self, "_news_try", 0) > 1800:
                self._news_try = time.time()
                self._auto_news_refresh()
            self._auto_news_render()
        finally:
            self.after(30000, self._auto_news_tick)

    def _auto_news_ccys(self):
        out = set()
        for p in self.auto_sel:
            out |= NEWS.currencies(p, self.auto_catalog.get(p))
        return out

    def _auto_news_settings(self):
        try:
            before = float(self.auto_news_before.get().replace(",", "."))
            after = float(self.auto_news_after.get().replace(",", "."))
        except ValueError:
            before = after = 15.0
        return NEWS_LEVELS[max(0, self.auto_news_lvl.current())], before, after

    def _auto_news_render(self):
        now = time.time()
        levels, before, after = self._auto_news_settings()
        ccys = self._auto_news_ccys()
        evs = self.news.upcoming(now, levels, ccys or None, n=6, after_min=after)
        lines = []
        for e in evs:
            live = e["t"] - before * 60 <= now <= e["t"] + after * 60
            lines.append(("⛔ " if live else "• ") + NEWS.fmt_event(e, now))
        txt = "\n".join(lines) if lines else (
            "Sin noticias de " + ("/".join(sorted(ccys)) if ccys else "tus pares") + " en lo que queda de la semana."
            if self.news.events else "Calendario sin descargar (se baja al iniciar el bot y cada 4 h).")
        if self.news_list.cget("text") != txt:
            self.news_list.config(text=txt, fg=TEXT if lines else MUTED)
        if self.news.events and not self.news.covers(now):
            st, col = "⚠ El calendario guardado es de otra semana: pulsa «Actualizar» (¿hay internet?).", AMBER
        elif self.news.error and not self.news.events:
            st, col = f"⚠ No pude descargar el calendario: {self.news.error[:90]}", AMBER
        elif self.news.fetched:
            st = (f"Fuente: calendario de ForexFactory · actualizado hace "
                  f"{max(0, int((now - self.news.fetched) // 60))} min. ⛔ = el bot no entra ahora en esos pares.")
            col = MUTED
            if not self.auto_news.get():
                st, col = "Filtro APAGADO: el bot entra aunque haya noticias.", AMBER
        else:
            st, col = "", MUTED
        if self.news_state.cget("text") != st:
            self.news_state.config(text=st, fg=col)

    def _auto_news_block(self, full, a, now=None):
        """La noticia que impide entrar ahora en este mercado (o None)."""
        if not a.get("news_on") or (full.endswith("-OTC") and not a.get("news_otc")):
            return None
        base = split_full(full)[0]
        return self.news.blocking(full, time.time() if now is None else now, a.get("news_before", 15),
                                  a.get("news_after", 15), a.get("news_levels", ("High",)),
                                  self.auto_catalog.get(base))

    # ============================================================ lectura de la configuración
    def _auto_cfg_read(self):
        def num(fld, name, lo, hi=None, integer=False):
            try:
                v = float(fld.get().replace(",", ".") or 0)
            except ValueError:
                raise ValueError(f"«{name}» debe ser un número.")
            if v < lo or (hi is not None and v > hi):
                raise ValueError(f"«{name}» debe estar entre {lo:g} y {hi:g}." if hi is not None
                                 else f"«{name}» no puede ser menor que {lo:g}.")
            if integer and v != int(v):
                raise ValueError(f"«{name}» debe ser un número entero.")
            return int(v) if integer else v

        sel = self._auto_selected()
        if not sel:
            raise ValueError("Elige al menos un par en «Pares a operar».")
        idx = self.auto_strat.current()
        kind = KINDS[self.auto_kind_cb.current()]
        try:
            durs = sorted({int(x) for x in self.auto_durs.get().replace(" ", "").split(",") if x})
            if not durs or any(d < 1 or d > 5 for d in durs):
                raise ValueError
        except ValueError:
            raise ValueError("«Binarias: duraciones (min)» debe ser una lista de minutos entre 1 y 5 (ej. 1,2,3,4,5).")
        try:
            bsec = sorted({int(x) for x in self.auto_bdurs.get().replace(" ", "").split(",") if x})
            if not bsec or any(s not in (30, 60) for s in bsec):
                raise ValueError
        except ValueError:
            raise ValueError("«Blitz: duraciones (s)» solo admite 30 y 60 segundos (ej. 30,60).")
        window = num(self.auto_window, "Entrar como máximo al segundo", 2, 25)
        cfg = dict(
            mode="best" if idx == 0 else KEYS[idx - 1], pairs=sel, market=MARKET_KEYS[self.auto_market.current()],
            kind=kind, entry_window=min(window, ENTRY_CAP[kind]),
            payout=num(self.auto_payout, "Pago de respaldo (%)", 30, 100) / 100.0,
            min_payout=num(self.auto_minpay, "Pago mínimo (%)", 0, 100) / 100.0,
            max_open=num(self.auto_maxopen, "Máx. operaciones abiertas", 1, 10, True),
            per_hour=num(self.auto_perhour, "Máx. entradas por hora", 1, 240, True),
            margin=num(self.auto_margin, "Margen de confianza", 0, 20) / 100.0,
            pause_min=num(self.auto_pause, "Pausa tras perder (min)", 0, 120),
            pair_filter=PAIR_FILTER_KEYS[max(0, self.auto_pairf.current())],
            top_n=num(self.auto_topn, "N mejores", 1, 50, True),
            need_good=PAIR_FILTER_KEYS[max(0, self.auto_pairf.current())] != "all",
            hours_on=self.auto_hours.get(), h_from=0, h_to=0,
            dur_mode=DUR_KEYS[max(0, self.auto_dur.current())],
            trend_mode=strat.TREND_MODES[max(0, self.auto_trend.current())],
            durations=durs if kind == "binary" else [1] if kind == "binary5" else [s // BAR["blitz"] for s in bsec],
            allow_htf=self.auto_htf.get(), allow_vol=self.auto_vol.get(), allow_pat=self.auto_pat.get(),
            stake_mode=("fixed", "ic", "mg", "kelly")[self.auto_stake.current()],
            ic_steps=num(self.auto_icsteps, "IC · Reinversiones por ciclo", 1, 4, True),
            mg_mult=num(self.auto_mgmult, "Multiplicador de martingala", 1.1, 5),
            mg_levels=num(self.auto_mglevels, "Niveles de martingala", 1, 6, True),
            mg_wait=num(self.auto_mgwait, "MG · Velas para confirmar", 1, 10, True),
            mg_same=self.auto_mgdir.current() == 1,
            mg_stop=self.auto_mgend.current() == 1,
            kelly_frac=num(self.auto_kfrac, "Fracción de Kelly (%)", 1, 100) / 100.0,
            kelly_cap=num(self.auto_kcap, "Tope por operación (% del capital)", 0.5, 50) / 100.0,
            ccy_on=self.auto_ccy.get(), ccy_max=num(self.auto_ccymax, "Máx. por divisa", 1, 5, True),
            health_on=self.auto_health.get(), gate_on=self.auto_gate_on.get(),
            gate_min=num(self.auto_gatemin, "Ops mínimas en Demo", 10, 500, True),
            recompare_h=num(self.auto_recmp, "Volver a comparar cada (horas)", 0, 48),
            news_on=self.auto_news.get(), news_otc=self.auto_news_otc.get(),
            news_levels=NEWS_LEVELS[max(0, self.auto_news_lvl.current())],
            news_before=num(self.auto_news_before, "Noticias · minutos antes", 0, 120),
            news_after=num(self.auto_news_after, "Noticias · minutos después", 0, 120))
        if cfg["hours_on"]:
            cfg["h_from"] = parse_hhmm(self.auto_from.get(), "Desde")
            cfg["h_to"] = parse_hhmm(self.auto_to.get(), "Hasta")
        return cfg

    def _auto_apply_mg(self, cfg):
        self.mg_auto.configure(cfg["mg_mult"], cfg["mg_levels"], cfg["mg_wait"], cfg["mg_same"],
                               "stop" if cfg["mg_stop"] else "reset")
        self.ic_auto.configure(cfg.get("ic_steps", 1))

    def _auto_pairf_desc_update(self):
        key = PAIR_FILTER_KEYS[max(0, self.auto_pairf.current())]
        try:
            self.auto_topn.entry.config(state="normal" if key == "top" else "disabled")
        except tk.TclError:
            pass
        txt = {"good": "Solo entra en los pares con ✔ Confirmada: superan el equilibrio en la prueba final del backtest "
                       "Y en el tramo de selección. Los demás se omiten aunque estén marcados.",
               "top": "Como el anterior, pero de los confirmados que estén abiertos solo opera los N con mejor efectividad "
                      "confiable (selección + prueba juntas). Si uno se cierra, entra el siguiente de la lista.",
               "all": "⚠ Opera todos los marcados aunque el backtest diga que pierden. Úsalo solo en Demo para probar."}[key]
        self.auto_pairf_desc.config(text=txt, fg=AMBER if key == "all" else MUTED)

    def _auto_sync(self):
        """Si el modo está encendido, aplica al instante los cambios (pares, estrategia, mercado, límites)."""
        self._auto_option_changed(update_only=True)
        self._auto_pairf_desc_update()
        if hasattr(self, "news_list"):
            self._auto_news_render()
        self._auto_dur_desc_update()
        if self.auto_flag.is_set():
            try:
                cfg = self._auto_cfg_read()
            except ValueError as e:
                self._log(f"Modo automático: no se aplicó el cambio ({e})")
                self._toast(str(e), RED)
            else:
                if cfg["kind"] != self.auto_cfg.get("kind"):
                    self.mg_auto.reset()
                    self.ic_auto.reset()
                self.auto_cfg = cfg
                self._auto_apply_mg(cfg)
        self._auto_refresh_cards()

    def _auto_kind_changed(self, sync=True):
        self.auto_kind = KINDS[self.auto_kind_cb.current()]
        if self.auto_kind == "binary":
            txt = ("Binarias de 1–5 min con vencimiento EXACTO al cierre de vela, igual que en el backtest. Para operaciones de "
                   "30 o 60 segundos usa Blitz.")
        elif self.auto_kind == "binary5":
            txt = ("Binarias que se analizan con velas de 5 min y vencen al cierre de la vela siguiente (5 min exactos). Menos "
                   "ruido que en 1 min y menos entradas. Necesita su propio backtest: pulsa «Comparar» con este tipo elegido "
                   "(8000 velas de 5 min ≈ 4 semanas; quedan guardadas en el PC).")
        else:
            txt = ("Blitz: el bot analiza velas de 30 s y abre operaciones Blitz de 30 o 60 s exactos desde la compra. "
                   "Necesita su propio backtest (pulsa «Comparar» con este tipo elegido). Las expiraciones de 5–15 s son "
                   "solo para cuentas VIP; algunos pares solo tienen Blitz en OTC.")
        self.auto_kind_desc.config(text=txt)
        if self.chips:
            self._auto_update_chips()
        if sync:
            self._auto_fill_ranking()
            self._auto_sel_changed()
            self._auto_sync()

    def _auto_option_changed(self, update_only=False):
        idx = self.auto_strat.current()
        if idx == 0:
            self.auto_desc.config(text="Usa, para cada par, la estrategia que mejor resultado confiable dio en la "
                                       "última comparación. Primero pulsa «Comparar estrategias».")
        else:
            self.auto_desc.config(text=strat.DESCRIPTIONS[KEYS[idx - 1]])
        if not update_only:
            self._auto_sync()

    def _auto_trend_changed(self, sync=True):
        idx = max(0, self.auto_trend.current())
        txt = ("Ninguna estrategia entra contra la tendencia de 1m·5m·15m·1h (en lateral claro se permiten ambos "
               "sentidos), ni mientras el retroceso sigue en curso, ni si pasó del 78,6% del impulso (tendencia rota), ni "
               "pegada a un soporte/resistencia de 15m/1h en contra, ni con una duración más corta que la temporalidad que "
               "va en contra. El backtest mide exactamente esto.",
               "El backtest prueba cada estrategia con y sin el filtro de tendencia (en la primera mitad de las velas) y se "
               "queda, en cada par, con lo que rinda mejor.",
               "Sin filtro de tendencia: las estrategias de reversión pueden entrar contra la tendencia (como en la v13).")[idx]
        mode = strat.TREND_MODES[idx]
        stale = any(it.get("trend_mode", "off") != mode for kind in KINDS for items in self._res[kind].values()
                    for it in items[:1])
        if stale:
            txt += " ⚠ La última comparación se hizo con otro filtro: vuelve a pulsar «Comparar estrategias»."
        self.auto_trend_desc.config(text=txt, fg=AMBER if stale else MUTED)
        if sync:
            self._auto_sync()

    def _auto_dur_desc_update(self):
        idx = max(0, self.auto_dur.current())
        self.auto_dur_desc.config(text=(
            "En cada vela prueba las duraciones que superan el equilibrio en el backtest (de la mejor a la peor) y entra con "
            "la primera que da señal y encaja con la tendencia: 1 min solo si la de 5 min acompaña; 2–3 min si acompaña la "
            "de 15 min; 4–5 min si acompañan la de 15 min y la de 1 h.",
            "Usa la duración que eligió el backtest en cada par. Si Exnova no deja comprar a esa duración (p. ej. «Time for "
            "purchasing options is over» a 5 min), pasa a la siguiente validada durante 30 min.",
            "Usa siempre la duración de Configurar (en Blitz, 30 s).")[idx])

    def _auto_chip_clicked(self, pair, on):
        (self.auto_sel.add if on else self.auto_sel.discard)(pair)
        self._auto_sync()

    def _auto_pick_all(self, on):
        """«Todos» marca los de la categoría que se está viendo; «Ninguno» desmarca todo (todas las categorías)."""
        if on:
            self.auto_sel |= {p for p, c in self.auto_catalog.items() if c == self.auto_category}
        else:
            self.auto_sel = set()
        for p, ch in self.chips.items():
            ch.set(p in self.auto_sel)
        self._auto_sync()

    # ============================================================ catálogo de activos (forex, materias primas, …)
    def _auto_order(self, names):
        """Ordena activos: por categoría, los pares clásicos primero y el resto por nombre."""
        def key(p):
            c = self.auto_catalog.get(p) or asset_category(p)
            cat_i = CATEGORIES.index(c) if c in CATEGORIES else 99
            return (cat_i, AUTO_PAIRS.index(p) if p in AUTO_PAIRS else 99, p)
        return sorted(names, key=key)

    def _auto_selected(self):
        return self._auto_order(self.auto_sel)

    def _auto_build_chips(self):
        """(Re)dibuja las fichas de la categoría elegida."""
        g = self.chip_grid
        for w in g.winfo_children():
            w.destroy()
        self.chips = {}
        names = self._auto_order([p for p, c in self.auto_catalog.items() if c == self.auto_category])
        for i, pair in enumerate(names):
            ch = PairChip(g, pair, self.icons, self._auto_chip_clicked)
            ch.grid(row=i // 6, column=i % 6, padx=(0, 8), pady=(0, 8), sticky="w")
            ch.set(pair in self.auto_sel)
            self.chips[pair] = ch
        if not names:
            tk.Label(g, text="No hay activos de esta categoría abiertos ahora (pulsa «Detectar abiertos hoy»).",
                     bg=CARD, fg=MUTED, font=font(9)).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 8))
        for cat, b_ in getattr(self, "cat_btns", {}).items():
            n = sum(1 for c in self.auto_catalog.values() if c == cat)
            nsel = sum(1 for p in self.auto_sel if self.auto_catalog.get(p) == cat)
            b_.set_text(f"{cat} ({n})" + (f" · {nsel}✓" if nsel else ""))
            b_.set_kind("primary" if cat == self.auto_category else "ghost")
        if hasattr(self, "cat_info"):
            self.cat_info.config(text=f"elegidos en total: {len(self.auto_sel)}")
        self._auto_update_chips()

    def _auto_set_category(self, cat):
        if cat != self.auto_category:
            self.auto_category = cat
            self._auto_build_chips()

    # ============================================================ mercados abiertos (normal / OTC)
    def _auto_table(self, force=False, max_age=TABLE_MAX_AGE):
        """Tabla de activos de Exnova ({nombre: {'turbo': {...}}}) con caché. None si no se pudo consultar.
        NUNCA se llama desde el hilo de la ventana (Exnova puede tardar varios segundos)."""
        st = self.auto_status
        if not force and st["table"] is not None and time.time() - st["t"] < max_age:
            return st["table"]
        fn = getattr(self.broker, "sync_assets", None)
        if fn is None:
            return st["table"]
        with self._status_lock:
            if not force and st["table"] is not None and time.time() - st["t"] < max_age:
                return st["table"]
            try:
                with self.broker_lock:
                    tab = fn()
            except Exception:
                tab = None
            if tab:
                st["table"], st["t"] = tab, time.time()
                self._ui_once("chips", self._auto_update_chips)
        return st["table"]

    @staticmethod
    def _mkt_info(table, full, kind="binary"):
        """(abierto, pago%) de un activo. Binarias: SOLO la modalidad turbo (vence a 1–5 min), que es la que opera el bot
        (v14.3: la binaria clásica vence a 15 min o más; muchas acciones solo tienen esa y Exnova rechazaba la compra con
        «Active not found»). Blitz: su propia modalidad si Exnova la informa (si no, la turbo como referencia)."""
        ent = (table or {}).get(full) or {}
        t = ent.get("blitz") if kind == "blitz" and ent.get("blitz") else ent.get("turbo")
        if not t:
            return False, None
        return bool(t.get("open")), t.get("payout")

    def _auto_markets(self, base, mode):
        """Mercados candidatos de un par, en orden de preferencia, según el modo elegido."""
        return {"normal": [base], "otc": [base + "-OTC"], "auto": [base, base + "-OTC"]}[mode]

    def _auto_resolve(self, base, a, table):
        """[(mercado, pago o None)] abiertos y con pago suficiente. Con la tabla desconocida devuelve todos los candidatos."""
        out = []
        for full in self._auto_markets(base, a["market"]):
            if table is None:
                out.append((full, None))
                continue
            open_, pay = self._mkt_info(table, full, a.get("kind", "binary"))
            if open_ and (pay is None or pay / 100.0 >= a["min_payout"]):
                out.append((full, pay))
        return out

    def _auto_update_chips(self):
        tab = self.auto_status["table"]
        kind = getattr(self, "auto_kind", "binary")
        for base, ch in self.chips.items():
            if tab is None:
                ch.set_status(None)
                continue
            n_open = self._mkt_info(tab, base, kind)[0]
            o_open = self._mkt_info(tab, base + "-OTC", kind)[0]
            ch.set_status("both" if n_open and o_open else "normal" if n_open else "otc" if o_open else "closed")

    def _auto_detect(self):
        if self.broker is None:
            messagebox.showerror("Mercados", "Primero inicia sesión.")
            return
        self.detect_btn.set_enabled(False)
        self.detect_btn.set_text("Consultando…")
        mode = MARKET_KEYS[self.auto_market.current()]
        kind = KINDS[self.auto_kind_cb.current()]

        def work():
            tab = self._auto_table(force=True)
            if tab and hasattr(self, "_write_dump"):          # deja constancia de lo que dijo Exnova (para revisar)
                self._write_dump(sorted(tab), tab)

            def done():
                self.detect_btn.set_enabled(True)
                self.detect_btn.set_text("Detectar abiertos hoy")
                if not tab:
                    messagebox.showerror("Mercados", "Exnova no respondió con la lista de activos. Inténtalo de nuevo.")
                    return
                minpay = self._auto_minpay_value()
                # catálogo: todo lo que Exnova tenga abierto ahora (normal u OTC) para este tipo de operación
                cat = {p: "Forex" for p in AUTO_PAIRS}
                for name in tab:
                    base = name[:-4] if name.upper().endswith("-OTC") else name
                    if self._mkt_info(tab, base, kind)[0] or self._mkt_info(tab, base + "-OTC", kind)[0]:
                        cat.setdefault(base, asset_category(base))
                self.auto_catalog = cat
                opened, n_n, n_o, per = [], 0, 0, {}
                for base, c in cat.items():
                    no, npay = self._mkt_info(tab, base, kind)
                    oo, opay = self._mkt_info(tab, base + "-OTC", kind)
                    n_n += no
                    n_o += oo
                    if no or oo:
                        per[c] = per.get(c, 0) + 1
                    if c != self.auto_category:
                        continue
                    for full in self._auto_markets(base, mode):
                        o, pay = self._mkt_info(tab, full, kind)
                        if o and (pay is None or pay / 100.0 >= minpay):
                            opened.append(base)
                            break
                # se reemplaza la elección SOLO en la categoría que se está viendo
                self.auto_sel = {p for p in self.auto_sel if cat.get(p) != self.auto_category} | set(opened)
                self._auto_build_chips()
                has_blitz = any("blitz" in (tab.get(p) or {}) for p in tab)
                self._log(f"Mercados ({KIND_SHORT[kind]}): {n_n} normales abiertos y {n_o} OTC "
                          f"abiertos · " + " · ".join(f"{c} {per.get(c, 0)}" for c in CATEGORIES)
                          + f". Elegidos {len(opened)} de {self.auto_category} (pago mínimo {minpay * 100:.0f}%)."
                          + ("" if kind != "blitz" or has_blitz else
                             " Exnova no informó la lista Blitz: se usa la de binarias como referencia."))
                if not opened:
                    messagebox.showinfo("Mercados", f"Ningún activo de «{self.auto_category}» cumple las condiciones ahora "
                                                    f"mismo (mercado cerrado o pago por debajo del mínimo).")
                self._auto_sync()
            self._ui(done)

        threading.Thread(target=work, daemon=True).start()

    def _auto_minpay_value(self):
        try:
            return float(self.auto_minpay.get().replace(",", ".")) / 100.0
        except ValueError:
            return 0.0

    # ============================================================ activar / desactivar
    def _auto_strategy_for(self, full, cfg=None):
        """Estrategia que corresponde a un mercado según el modo elegido."""
        cfg = cfg or self.auto_cfg
        kind = cfg.get("kind", self.auto_kind)
        return self._best[kind].get(full) if cfg.get("mode") == "best" else cfg.get("mode")

    def _auto_confirmed(self, kind=None, mode=None):
        """[(mercado, puntaje)] de los mercados con ✔ Confirmada para la estrategia elegida, del mejor al peor."""
        kind = kind or self.auto_kind
        if mode is None:
            idx = self.auto_strat.current()
            mode = "best" if idx <= 0 else KEYS[idx - 1]
        out = []
        for full, items in self._res[kind].items():
            if mode == "best":
                key = self._best[kind].get(full)
                it = next((x for x in items if x["key"] == key), None)
            else:
                it = self._auto_result_for(full, mode, kind)
            if it and strat.passes(it):
                out.append((full, strat.confirm_score(it)))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    def _auto_store_label(self):
        n, mb = self.vstore.summary()
        self.store_lbl.config(text=(f"Guardadas en el PC: {n} archivo(s) de velas · {mb:.1f} MB (carpeta «velas»). "
                                    f"Cada «Comparar» solo descarga lo nuevo y el histórico crece con el uso."
                                    if n else "Aún no hay velas guardadas: el primer «Comparar» las descarga y las guarda "
                                              "en la carpeta «velas» del bot."))

    def _auto_store_clear(self):
        if self.auto_bt_running:
            self._toast("Espera a que termine la comparación.", AMBER)
            return
        if not messagebox.askyesno("Velas guardadas", "¿Borrar todas las velas guardadas en el PC? La próxima comparación "
                                                      "las vuelve a descargar desde cero."):
            return
        self.vstore.clear()
        self._auto_store_label()
        self._log("Velas guardadas borradas.")

    def _auto_pick_confirmed(self):
        """Marca solo los pares confirmados (si «Qué pares operar» = N mejores, solo los N primeros)."""
        conf = self._auto_confirmed()
        if not conf:
            self._toast("Ningún mercado quedó ✔ Confirmado con esta estrategia y tipo: no hay pares que elegir.", AMBER)
            return
        pf = PAIR_FILTER_KEYS[max(0, self.auto_pairf.current())]
        if pf == "all":
            self.auto_pairf.current(0)
            pf = "good"
        bases = []
        for full, _ in conf:
            b_ = split_full(full)[0]
            if b_ not in bases:
                bases.append(b_)
        if pf == "top":
            try:
                n = max(1, int(float(self.auto_topn.get())))
            except ValueError:
                n = 3
            bases = bases[:n]
        self.auto_sel = set(bases)
        for p_, ch in self.chips.items():
            ch.set(p_ in self.auto_sel)
        self._auto_sync()
        self._log("Pares confirmados elegidos: " + ", ".join(f"{pretty_pair(f)} ({sc * 100:+.1f} pts)" for f, sc in conf))
        self._toast(f"Elegidos {len(bases)} par(es) confirmados: {', '.join(bases)}", GREEN)

    def _auto_result_for(self, full, key, kind=None):
        for it in self._res[kind or self.auto_kind].get(full, []):
            if it["key"] == key:
                return it
        return None

    def _auto_toggled(self):
        if self._auto_reverting:
            return
        if not self.auto_on.get():
            self.auto_flag.clear()
            pend = self.mg_auto.snapshot()
            self.mg_auto.reset()
            self.ic_auto.reset()
            self._log("Modo automático DESACTIVADO (las operaciones abiertas siguen hasta cerrar)."
                      + (f" Se cancelan {len(pend)} gale(s) pendiente(s)." if pend else ""))
            self._auto_refresh_cards()
            return
        try:
            cfg = self._auto_cfg_read()
        except ValueError as e:
            self._auto_revert()
            messagebox.showerror("Modo automático", str(e))
            return
        kind = cfg["kind"]
        problems = []
        if cfg["mode"] == "best":
            missing = [p for p in cfg["pairs"]
                       if not any(m in self._best[kind] for m in self._auto_markets(p, cfg["market"]))]
            if missing:
                self._auto_revert()
                messagebox.showinfo("Modo automático",
                                    f"Para «Mejor por par» primero pulsa «Comparar estrategias» con los pares elegidos y el "
                                    f"tipo «{KIND_SHORT[kind]}» "
                                    f"(faltan: {', '.join(pretty_pair(p) for p in missing)}).")
                return
        else:
            for p in cfg["pairs"]:
                it = None
                for m in self._auto_markets(p, cfg["market"]):
                    it = self._auto_result_for(m, cfg["mode"], kind)
                    if it:
                        break
                if it is None:
                    problems.append(f"{pretty_pair(p)}: sin backtest de esta estrategia")
                elif not strat.passes(it):
                    problems.append(f"{pretty_pair(p)}: no quedó ✔ Confirmada en el backtest (prueba final + selección)")
        extra = []
        if cfg.get("news_on") and not self.news.covers(time.time()):
            extra.append("⚠ Filtro de noticias encendido pero no hay calendario de esta semana (¿sin internet?): hasta "
                         "que se descargue, el bot NO sabe cuándo hay noticias. Pulsa «Actualizar» en Filtro de noticias.")
        if cfg.get("need_good"):
            conf = {f for f, _ in self._auto_confirmed(kind, cfg["mode"])}
            mine = [m for p in cfg["pairs"] for m in self._auto_markets(p, cfg["market"]) if m in conf]
            if not mine:
                self._auto_revert()
                messagebox.showinfo("Modo automático",
                                    "Ninguno de los pares marcados quedó ✔ Confirmado con esta estrategia y tipo, así que "
                                    "con «Qué pares operar» en modo confirmados el bot no entraría en ninguno.\n\n"
                                    "Pulsa «Elegir confirmados» en el ranking o vuelve a «Comparar estrategias».")
                return
            if problems:      # con el filtro activo esos pares no se operan: es un aviso, no un riesgo
                extra.append(f"Se operarán solo los confirmados ({', '.join(pretty_pair(m) for m in mine[:8])}); "
                             f"los demás marcados se omitirán.")
                problems = []
            if cfg.get("pair_filter") == "top":
                extra.append(f"Solo los {cfg['top_n']} mejores que estén abiertos en cada vela.")
        if cfg["stake_mode"] == "ic":
            try:
                base = float(self.amount.get().replace(",", "."))
            except ValueError:
                base = 5.0
            tmp = PairCompound(cfg["ic_steps"])
            pay = cfg["payout"]
            be = strat.breakeven(pay)
            extra.append(f"Interés compuesto por par ({cfg['ic_steps']} reinversión(es) por ciclo): lo máximo que se pierde en "
                         f"un ciclo es el monto base (${base:,.2f}); la operación más grande sería ≈ "
                         f"${tmp.top_stake(base, pay):,.2f}. No cambia el equilibrio ({be * 100:.1f}%): si la estrategia "
                         f"no lo supera, el interés compuesto pierde más rápido que el monto fijo.")
        if cfg["stake_mode"] == "mg":
            try:
                base = float(self.amount.get().replace(",", "."))
            except ValueError:
                base = 5.0
            tmp = PairMartingale(cfg["mg_mult"], cfg["mg_levels"])
            worst = tmp.worst_case(base)
            extra.append(f"Martingala por par ×{cfg['mg_mult']:g} con {cfg['mg_levels']} niveles: perder {cfg['mg_levels'] + 1} "
                         f"seguidas en UN par cuesta ${worst:,.2f}; con {cfg['max_open']} pares a la vez el peor caso es "
                         f"${worst * cfg['max_open']:,.2f}.")
        if extra and not problems:
            if not messagebox.askyesno("Modo automático", "\n".join(extra) + "\n\n¿Activar?"):
                self._auto_revert()
                return
        if problems:
            if extra:
                problems += extra
            txt = ("El backtest no respalda esta combinación:\n\n  • " + "\n  • ".join(problems[:8]) +
                   "\n\nOperar así tiende a perder dinero. ¿Activar de todas formas?")
            if not messagebox.askyesno("Modo automático", txt):
                self._auto_revert()
                return
        if self.account_type == "REAL" and self._session_active():
            warn = ("El bot operará solo con tu cuenta REAL."
                    + ("" if cfg["gate_on"] else
                       "\n\n⚠ «Exigir validación en Demo» está APAGADO: puede operar activos y estrategias que nunca se "
                       "probaron en Demo.") + "\n\n¿Activar?")
            if not messagebox.askyesno("Dinero REAL", warn):
                self._auto_revert()
                return
        self.auto_cfg = cfg
        self._auto_apply_mg(cfg)
        self.mg_auto.reset()
        self.ic_auto.reset()
        self.auto_pause_until = 0.0
        self.auto_flag.set()
        self._log(f"Modo automático ACTIVADO: {self._auto_mode_name()} · {KIND_SHORT[kind]} en "
                  f"{', '.join(cfg['pairs'])} ({MARKETS[MARKET_KEYS.index(cfg['market'])].lower()}); entra como máximo al "
                  f"segundo {cfg['entry_window']:g} de cada vela.")
        if self._session_active():
            self._auto_start_engine(self.sess)
        elif self.worker and self.worker.is_alive():
            self._log("La sesión anterior está terminando (esperando que cierren sus operaciones). El modo automático "
                      "queda ARMADO: pulsa «Iniciar bot» cuando termine.")
        self._auto_refresh_cards()

    def _auto_revert(self):
        self._auto_reverting = True
        self.auto_on.set(False)
        self._auto_reverting = False

    def _auto_mode_name(self, mode=None):
        """Nombre de la estrategia elegida. Desde un hilo de trabajo hay que pasar `mode` (no se tocan los widgets)."""
        if mode is None:
            idx = self.auto_strat.current()
            mode = "best" if idx == 0 else KEYS[idx - 1]
        return "mejor estrategia por par" if mode == "best" else strat.SHORT.get(mode, mode)

    # ============================================================ tarjetas dinámicas
    def _auto_refresh_cards(self):
        on = self.auto_flag.is_set()
        running = self._session_active()
        n = len(self.auto_sel)
        state = "ACTIVO" if (on and running) else "ARMADO" if on else "APAGADO"
        kind_txt = KIND_SHORT[self.auto_kind]
        sub = (f"{self._auto_mode_name()[:20]} · {kind_txt} · {n} par{'es' if n != 1 else ''}" if on
               else "Enciéndelo en el control")
        self.card_a_state.set(state, sub, color=GREEN if on else MUTED)

        w = l = 0
        for d in self.auto_live.values():
            w += d["w"]
            l += d["l"]
        if w + l:
            r = w / (w + l)
            self.card_a_live.set(f"{w + l} ops", f"{w} ganadas · {l} perdidas", ring=r,
                                 ring_text=f"{r * 100:.0f}%", ring_color=rate_color(r))
        else:
            self.card_a_live.set("0 ops", "sin operaciones aún", ring=None, ring_text="—")
        self._auto_refresh_bt_card()

    def _auto_refresh_bt_card(self):
        sel = self._auto_selected()
        idx = self.auto_strat.current() if hasattr(self, "auto_strat") else 0
        mode = MARKET_KEYS[self.auto_market.current()] if hasattr(self, "auto_market") else "auto"
        full = None
        if sel:
            for m in self._auto_markets(sel[0], mode):
                if m in self.auto_results:
                    full = m
                    break
        if full is None:
            self.card_a_bt.title = "BACKTEST"
            self.card_a_bt.set("—", "pulsa «Comparar»")
            return
        key = self.auto_best.get(full) if idx == 0 else KEYS[idx - 1]
        it = self._auto_result_for(full, key) if key else None
        if it is None:
            self.card_a_bt.title = "BACKTEST"
            self.card_a_bt.set("—", f"sin resultado para {pretty_pair(full)}")
            return
        v = it["valid"]
        text, color = strat.verdict(v)
        col = COLORS[color]
        self.card_a_bt.title = (f"BACKTEST · {pretty_pair(full).replace('  · ', ' ')} · {TITLE_SHORT[key]} "
                                f"{dur_label(it.get('minutes', 1), self.auto_kind)}")
        d = v["wins"] + v["losses"]
        if d:
            self.card_a_bt.set(f"{d} ops", f"equilibrio {v['breakeven'] * 100:.1f}% · {text}", color=TEXT,
                               ring=v["winrate"], ring_text=f"{v['winrate'] * 100:.0f}%", ring_color=col)
        else:
            self.card_a_bt.set("0 ops", text, ring=None, ring_text="—")

    def _auto_next_close(self, now_srv=None):
        kind = self.auto_cfg.get("kind", self.auto_kind) if self.auto_flag.is_set() else self.auto_kind
        bar = BAR[kind]
        now_srv = now_srv if now_srv is not None else (self.broker.now() if self.broker else time.time())
        return (int(now_srv // bar) + 1) * bar, now_srv

    def _auto_tick(self, now):
        """Llamado cada 0.5 s desde la ventana: cuenta regresiva del próximo análisis."""
        on = self.auto_flag.is_set()
        running = self._session_active()
        sel = self._auto_selected()
        icon = self.icons.pair(sel[0], 30) if sel else None
        if on and running:
            if now < self.auto_pause_until:
                self.card_a_next.set(mmss(self.auto_pause_until - now), "En pausa tras perder (los gales siguen)",
                                     color=AMBER, icon=icon)
            else:
                nxt, srv = self._auto_next_close()
                self.card_a_next.set(mmss(nxt - srv), f"{self._auto_mode_name()[:22]} · {len(sel)} pares",
                                     color=ACCENT2, icon=icon)
        elif on:
            self.card_a_next.set("—", "Pulsa «Iniciar bot»")
        else:
            self.card_a_next.set("—", "Modo apagado")
        want = "ACTIVO" if (on and running) else "ARMADO" if on else "APAGADO"
        if self.card_a_state.s["value"] != want:
            self._auto_refresh_cards()

    def _auto_next_card_text(self):
        """Texto para la tarjeta «Próxima señal» del Panel cuando solo hay modo automático."""
        nxt, srv = self._auto_next_close()
        return mmss(nxt - srv), f"Análisis automático · {self._auto_mode_name()}"

    # ============================================================ ranking (backtest)
    def _auto_prog_text(self, text):
        self._ui_once("auto_prog", lambda: self.auto_prog.config(text=text))

    def _pool_progress(self, tag, text):
        """Progreso que mandan los procesos de cálculo (llega desde otro hilo)."""
        self._auto_prog_text(f"Analizando {pretty_pair(tag)}: {text}")

    def _auto_fill_ranking(self):
        t = self.rank_tree
        sel = t.selection()
        t.delete(*t.get_children())
        kind = self.auto_kind

        rank = {b: i for i, b in enumerate(self._auto_order({split_full(f)[0] for f in self.auto_results}))}

        def order(full):
            base, otc = split_full(full)
            return (rank.get(base, 999), otc)

        for full in sorted(self.auto_results, key=order):
            best = self.auto_best.get(full)
            for it in self.auto_results[full]:
                v = it["valid"]
                text, color = strat.verdict(v)
                sv = it.get("sel")
                ok_both = strat.passes(it)
                if color == "good" and not ok_both:
                    text, color = "Solo prueba", "warn"
                elif ok_both:
                    text = "✔ Confirmada"
                d = v["wins"] + v["losses"]
                sd = (sv["wins"] + sv["losses"]) if sv else 0
                sel_txt = f"{sv['winrate'] * 100:.0f}%" if sd else "—"
                test_txt = f"{v['winrate'] * 100:.0f}%·{d}" if d else "—"
                star = "★ " if it["key"] == best else "   "
                live = self.auto_live.get((it["key"], full, kind))
                live_txt = "—" if not live or live["w"] + live["l"] == 0 else \
                    f"{100 * live['w'] / (live['w'] + live['l']):.0f}% ({live['w'] + live['l']})"
                shown = tuple(f for f in it["flags"] if not (f == "mtf" and it.get("trend_mode") == "on"))   # implícito
                cfgtxt = dur_label(it["minutes"], kind) + (
                    f" · {strat.flags_label(shown, BAR[kind], short=True)}" if shown else "")
                t.insert("", "end", iid=f"{full}|{it['key']}", text="  " + pretty_pair(full),
                         image=self.icons.pair(full, 26),
                         values=(star + it["name"], cfgtxt, sel_txt, test_txt,
                                 f"{(v['winrate'] - v['breakeven']) * 100:+.1f}pp" if d else "—",
                                 VERDICT_SHORT.get(text, text), live_txt),
                         tags=(TAGS[color],))
        for iid in sel:
            if t.exists(iid):
                t.selection_set(iid)

    def _auto_compare(self, silent=False):
        if self.auto_bt_running:
            return
        if self.broker is None:
            if not silent:
                messagebox.showerror("Backtest", "Primero inicia sesión.")
            return
        try:
            cfg = self._auto_cfg_read()
            count = int(float(self.bt_count.get().replace(",", ".")))
            if not 1000 <= count <= 50000:
                raise ValueError("«Velas a analizar» debe estar entre 1000 y 50000 (más velas = conclusiones más fiables).")
        except ValueError as e:
            if silent:
                self._log(f"Re-comparación automática omitida: {e}")
            else:
                messagebox.showerror("Backtest", str(e))
            return
        kind = cfg["kind"]
        bar = BAR[kind]
        if cfg["dur_mode"] in ("auto", "trend"):
            durs = cfg["durations"]
        else:
            durs = [int(self.expiry.get())] if kind == "binary" else [1]
        opts = dict(margin=cfg["margin"], optimize=self.bt_opt.get(), allow_htf=cfg["allow_htf"],
                    allow_vol=cfg["allow_vol"], allow_pat=cfg["allow_pat"], trend_mode=cfg["trend_mode"])
        self.auto_bt_running = True
        self.compare_btn.set_enabled(False)
        self.compare_btn.set_text("Comparando…")
        self.card_a_bt.set("…", "analizando", ring=None, ring_text="…")
        durs_txt = ", ".join(dur_label(d, kind) for d in durs)

        def work():
            done, futures = 0, []
            targets = []
            try:
                self._auto_prog_text("Consultando a Exnova qué mercados están abiertos…")
                table = self._auto_table(force=True)
                for base in cfg["pairs"]:
                    cands = self._auto_resolve(base, cfg, table)
                    if cands:
                        targets.append(cands[0])
                    else:
                        self._log(f"Backtest {pretty_pair(base)}: mercado cerrado o pago bajo el mínimo; se omite.")
                for pi, (full, pay) in enumerate(targets):
                    payout = pay / 100.0 if pay else cfg["payout"]
                    tag = f"{pretty_pair(full)} ({pi + 1}/{len(targets)})"
                    self._auto_prog_text(f"Descargando velas de {bar} s de {tag}…")
                    try:
                        before = self.vstore.count(full, bar)
                        candles = self.vstore.history(self.broker, full, bar, count, progress=lambda n_, k_: (
                            self._auto_prog_text(f"Velas de {tag}: {min(n_, count)}/{count} (guardadas {before})")))
                        if len(candles) < count:
                            self._log(f"Backtest {pretty_pair(full)}: Exnova solo tiene {len(candles)} velas de {bar} s "
                                      f"(pediste {count}); se usan esas.")
                        if len(candles) < 800:
                            raise ValueError(f"solo llegaron {len(candles)} velas (se necesitan al menos 800)")
                    except Exception as e:
                        self._log(f"Backtest {pretty_pair(full)}: no se pudo descargar ({e})")
                        continue
                    fut = self.pool.submit(W.compare_task, full, candles, durs, payout, opts)
                    futures.append((full, payout, candles[0]["t"], candles[-1]["t"], len(candles), fut))
                    self._auto_prog_text(f"{tag}: calculando en segundo plano… (la ventana sigue libre)")
                for full, payout, t0, t1, n, fut in futures:
                    try:
                        items = fut.result(timeout=1800)
                    except Exception as e:
                        self._log(f"Backtest {pretty_pair(full)}: no se pudo ({e})")
                        continue
                    old_best = self._best[kind].get(full)
                    self._res[kind][full] = items
                    top = items[0]
                    if strat.sel_score(top)[0]:                # ★ = la mejor en el tramo de SELECCIÓN
                        self._best[kind][full] = top["key"]
                    else:
                        self._best[kind].pop(full, None)   # ninguna estrategia tiene operaciones suficientes
                    done += 1
                    span = f"{datetime.fromtimestamp(t0):%d/%m %H:%M} → {datetime.fromtimestamp(t1):%d/%m %H:%M}"
                    self._log(f"Backtest {pretty_pair(full)} ({n} velas de {bar} s, {span}, pago {payout * 100:.0f}%): "
                              f"mejor = {top['name']} a {dur_label(top['minutes'], kind)} "
                              f"({strat.flags_label(top['flags'], bar)}) · elección "
                              f"{(top.get('sel') or top['valid'])['winrate'] * 100:.1f}% en "
                              f"{(top.get('sel') or top['valid'])['trades']} ops · prueba final "
                              f"{top['valid']['winrate'] * 100:.1f}% en {top['valid']['trades']} ops"
                              + (" · ✔ Confirmada" if strat.passes(top) else ""))
                    self._log("   " + self._auto_ctx_summary(items, kind))
                    new_best = self._best[kind].get(full)
                    if silent and old_best and new_best != old_best:
                        self._log(f"   ↻ {pretty_pair(full)}: la mejor estrategia cambió de {strat.SHORT[old_best]} a "
                                  f"{strat.SHORT.get(new_best, 'ninguna')}.")
                    self._ui_once("rank", lambda: (self._auto_fill_ranking(), self._auto_refresh_cards()))
            except Exception as e:      # nunca dejar el botón bloqueado
                self._log(f"Backtest: error inesperado ({e})")

            def finish():
                self.auto_bt_running = False
                self._last_compare = time.time()
                self.compare_btn.set_enabled(True)
                self.compare_btn.set_text("Comparar estrategias")
                self._auto_fill_ranking()
                self._auto_refresh_cards()
                self._auto_store_label()
                self._auto_sel_changed()
                self._auto_trend_changed(sync=False)
                good = [f for f, _ in targets if f in self._res[kind] and strat.passes(self._res[kind][f][0])]
                good.sort(key=lambda f: strat.confirm_score(self._res[kind][f][0]), reverse=True)
                n_rows = sum(len(self._res[kind].get(f, [])) for f, _ in targets)
                fp = n_rows * 0.025
                if done == 0:
                    self.auto_prog.config(text="No se pudo completar la comparación (mira el Registro).")
                else:
                    self.auto_prog.config(
                        text=(f"Listo ({KIND_SHORT[kind]}): {done} mercado(s) comparados en "
                              f"{durs_txt} con el filtro de tendencia «{TREND_NAMES[strat.TREND_MODES.index(cfg['trend_mode'])].split(':')[0].lower()}». "
                              f"Tres tramos: con el primer 50% se ajustan parámetros, filtros y duración; con el siguiente "
                              f"25% se elige la ★ de cada par (columna «Selec.»); las cifras y el veredicto salen del "
                              f"último 25% (columna «Prueba»), que no se usó para elegir nada. "
                              + (f"✔ Confirmadas ({len(good)}): {', '.join(pretty_pair(f) for f in good)}. "
                                 if good else "Ninguna quedó confirmada: operar así tiende a perder. ")
                              + (f"Ojo: con {n_rows} combinaciones probadas, por puro azar se esperan ≈{fp:.1f} "
                                 f"falsas «confirmadas»; confírmalas en Demo (forward) antes de pasar a Real."
                                 if n_rows else "")))
            self._ui(finish)

        threading.Thread(target=work, daemon=True).start()

    def _auto_maybe_recompare(self, a):
        h = a.get("recompare_h") or 0
        if h > 0 and not self.auto_bt_running and time.time() - self._last_compare >= h * 3600:
            self._last_compare = time.time()          # evita dispararla dos veces
            self._log(f"↻ Re-comparación automática (cada {h:g} h) con velas frescas…")
            self._ui(lambda: self._auto_compare(silent=True))

    def _auto_plan(self, full, a, cfg):
        """Qué estrategia, duraciones, parámetros y filtros usar en un mercado ahora. Devuelve (plan|None, motivo).
        plan["durs"] = duraciones a probar en este cierre, en orden de preferencia (cada una con sus parámetros y filtros):
          · «según la tendencia»: todas las validadas, de mejor a peor en el tramo de ajuste del backtest; en vivo se usa
            la primera que da señal y pasa el filtro de tendencia (que exige que la duración encaje con la tendencia);
          · «automática»: la elegida por el backtest; si Exnova no la acepta en ese par, la siguiente validada;
          · «fija»: la de Configurar.
        plan["minutes"/"params"/"flags"/"sub"/"valid"] = los de la primera duración (compatibilidad)."""
        kind = a["kind"]
        items = self._res[kind].get(full, [])
        if a["mode"] == "best":
            best = self._best[kind].get(full)
            cands = sorted(items, key=lambda it: it["key"] != best)     # la ganadora primero, luego el resto en orden
        else:
            it = self._auto_result_for(full, a["mode"], kind)
            cands = [it] if it else []
        if not cands:
            return None, "sin backtest de este mercado (pulsa Comparar)"
        reason = "sin estrategia disponible"
        now = time.time()
        dmode = a.get("dur_mode", "auto")
        force_mtf = a.get("trend_mode") == "on"
        for it in cands:
            by = it.get("by_minutes") or {}
            vig = self.auto_blocked.get((it["key"], full, kind))
            if a.get("health_on") and vig and vig[0] > now:
                reason = f"{strat.SHORT[it['key']]} pausada por el vigilante ({vig[1]})"
                continue
            if dmode == "fixed":
                order = [cfg["minutes"] if kind == "binary" else 1]
            else:
                order = sorted(by, key=lambda m: (-strat.train_score(by[m]), m))
                if dmode == "auto" and it["minutes"] in order:
                    order.remove(it["minutes"])
                    order.insert(0, it["minutes"])
            durs = []
            for pos, m in enumerate(order):
                sub = by.get(m)
                if sub is None:
                    reason = f"sin backtest a {dur_label(m, kind)} (vuelve a comparar con esa duración)"
                    continue
                blk = self.auto_dur_block.get((full, m, kind))
                if blk and blk[0] > now:
                    reason = f"Exnova no acepta {dur_label(m, kind)} en este par ahora ({blk[1]})"
                    continue
                weak = not strat.passes(sub)
                # «según la tendencia» solo SUMA duraciones que superan el equilibrio (la primera sigue la regla de siempre)
                if weak and (a.get("need_good") or (dmode == "trend" and pos > 0)):
                    reason = (f"{strat.SHORT[it['key']]} no supera el equilibrio en la prueba final del backtest" if pos == 0
                              else f"a {dur_label(m, kind)} no supera el equilibrio en el backtest")
                    if dmode == "auto" and pos == 0:
                        break                    # la duración elegida no vale: no se busca otra por ese motivo
                    continue
                flags = tuple(sub["flags"])
                if force_mtf and "mtf" not in flags:
                    flags = ("mtf",) + flags     # «obligatorio» se respeta aunque la comparación fuera de otro modo
                p = strat.Params(**{**sub["params"].__dict__, "prob_margin": a["margin"]})
                durs.append(dict(minutes=m, params=p, flags=flags, sub=sub, valid=sub["valid"]))
                if dmode != "trend":
                    break
            if not durs:
                continue
            d0 = durs[0]
            return dict(key=it["key"], durs=durs, minutes=d0["minutes"], params=d0["params"], flags=d0["flags"],
                        sub=d0["sub"], valid=d0["valid"], item=it), None
        return None, reason

    def _auto_corr_ok(self, full, action, a):
        """No abre dos operaciones que apuesten en el mismo sentido sobre una misma divisa (EUR/USD y GBP/USD a la vez)."""
        with self.lock:
            opens = [dict(o) for o in self.open_trades.values()]
        if any(o.get("pair") == full for o in opens):
            return False, "ya hay una operación abierta en este mercado"
        if not a["ccy_on"]:
            return True, ""

        def expo(pair, act):
            if not is_ccy_pair(pair):               # acciones, índices…: no comparten divisas
                return {}
            b, q = pair.replace("-OTC", "").replace("/", "")[:3], pair.replace("-OTC", "").replace("/", "")[3:6]
            s = 1 if act == "call" else -1
            return {b: s, q: -s}

        new = expo(full, action)
        for cur, sign in new.items():
            n = sum(1 for o in opens if o.get("pair") and o.get("action") and expo(o["pair"], o["action"]).get(cur) == sign)
            if n >= a["ccy_max"]:
                return False, f"ya hay {n} operación(es) {'larga' if sign > 0 else 'corta'} en {cur} (límite {a['ccy_max']})"
        return True, ""

    def _auto_stake(self, cfg, a, plan, winp, payout, full):
        """(monto, nota, nivel de martingala). Fijo, martingala POR PAR, o Kelly (proporcional a la ventaja estimada)."""
        with self.lock:
            free = cfg["capital"] + self.st["pnl"] - self.st["open"]
        if a["stake_mode"] == "fixed":
            return cfg["amount"], "monto fijo", 0
        if a["stake_mode"] == "ic":
            amt, step = self.ic_auto.stake(cfg["amount"], full)
            if step and amt > free:
                self.ic_auto.on_result(full, amt, step, None)        # no alcanza: el ciclo se cierra y vuelve al base
                amt, step = self.ic_auto.stake(cfg["amount"], full)
                if amt > free:
                    return None, f"el capital libre (${free:.2f}) no alcanza para ${amt:.2f}", 0
                return amt, "interés compuesto: el capital libre no alcanzaba para el paso siguiente; vuelve al monto base", 0
            return amt, (f"INTERÉS COMPUESTO paso {step}/{a.get('ic_steps', 1)} de {pretty_pair(full)} (monto + ganancia)"
                         if step else "interés compuesto: monto base"), 0
        if a["stake_mode"] == "mg":
            amt, lvl = self.mg_auto.stake(cfg["amount"], full)
            if amt > free:
                return None, (f"el capital libre (${free:.2f}) no alcanza para el nivel {lvl} de martingala de "
                              f"{pretty_pair(full)} (${amt:.2f})"), lvl
            note = (f"GALE nivel {lvl}/{a['mg_levels']} de {pretty_pair(full)} (×{a['mg_mult']:g})" if lvl
                    else "martingala por par: nivel 0 (monto base)")
            return amt, note, lvl
        v = plan["valid"]
        p_est = min(winp, v["winrate"]) if winp is not None else v["ci_low"]
        be = strat.breakeven(payout)
        f = p_est - (1.0 - p_est) / payout
        if f <= 0:
            return None, f"sin ventaja suficiente (p estimada {p_est * 100:.1f}% ≤ equilibrio {be * 100:.1f}%)", 0
        frac = min(a["kelly_cap"], a["kelly_frac"] * f)
        amount = round(free * frac, 2)
        if amount < 1.0:
            return None, f"la apuesta calculada (${amount:.2f}) es menor que $1", 0
        return amount, f"Kelly {frac * 100:.1f}% del capital libre (p estimada {p_est * 100:.1f}%)", 0

    def _auto_gate_ok(self, key, full, dk, payout, a):
        d = self.auto_gate.get(f"{key}|{full}|{dk}") or {"n": 0, "w": 0, "l": 0}
        dec = d["w"] + d["l"]
        if dec < a["gate_min"]:
            return False, f"falta validar en Demo ({dec}/{a['gate_min']} operaciones)"
        lo, _ = strat.wilson(d["w"], dec)
        be = strat.breakeven(payout)
        if lo <= be:
            return False, (f"en Demo rinde {d['w'] / dec * 100:.0f}% (confianza inferior {lo * 100:.0f}%), "
                           f"no supera el equilibrio {be * 100:.1f}%")
        return True, ""

    def _gate_load(self):
        try:
            with open(GATE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _gate_save(self):
        try:
            tmp = GATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.auto_gate, fh)
            os.replace(tmp, GATE_FILE)
        except Exception:
            pass

    def _auto_health_refresh(self):
        t = self.health_tree
        t.delete(*t.get_children())
        now = time.time()
        rows, seen = [], set()
        try:
            gmin = self.auto_cfg.get("gate_min") or int(float(self.auto_gatemin.get()))
        except ValueError:
            gmin = 30
        for ks in sorted(self.auto_gate):
            try:
                key, full, dk = ks.split("|")
            except ValueError:
                continue
            if key not in strat.SHORT:
                continue
            kind = dkey_kind(dk)
            seen.add((key, full, kind))
            d = self.auto_gate.get(ks) or {"n": 0, "w": 0, "l": 0}
            dec = d["w"] + d["l"]
            lo = strat.wilson(d["w"], dec)[0] if dec else 0.0
            blk = self.auto_blocked.get((key, full, kind))
            pay = self._mkt_info(self.auto_status["table"], full, kind)[1]
            be = strat.breakeven(pay / 100.0 if pay else 0.85)
            if blk and blk[0] > now:
                state, tag = f"⚠ pausada hasta {datetime.fromtimestamp(blk[0]):%H:%M}", "loss"
            elif dec >= gmin and lo > be:
                state, tag = "✔ habilitada para Real", "win"
            elif dec >= gmin:
                state, tag = "no supera el equilibrio", "loss"
            else:
                state, tag = f"en prueba ({dec}/{gmin})", "skip"
            rows.append((f"{strat.SHORT[key]} · {dkey_label(dk)}", full, dec, f"{d['w'] / dec * 100:.0f}%" if dec else "—",
                         f"{lo * 100:.0f}%" if dec else "—", state, tag))
        for (key, full, kind), dq in list(self.auto_roll.items()):
            if (key, full, kind) in seen or key not in strat.SHORT:
                continue
            n_, w_ = len(dq), sum(dq)
            blk = self.auto_blocked.get((key, full, kind))
            if blk and blk[0] > now:
                state, tag = f"⚠ pausada hasta {datetime.fromtimestamp(blk[0]):%H:%M}", "loss"
            else:
                state, tag = "en vivo (vigilante)", "skip"
            rows.append((f"{strat.SHORT[key]} · {KIND_SHORT[kind]}", full, n_,
                         f"{w_ / n_ * 100:.0f}%" if n_ else "—", "—", state, tag))
        for name, full, dec, wr, lo, state, tag in rows:
            t.insert("", "end", text="  " + pretty_pair(full), image=self.icons.pair(full, 26),
                     values=(name, dec, wr, lo, state), tags=(tag,))

    def _auto_sel_changed(self):
        self._auto_dur_refresh()
        self._auto_ctx_refresh()
        self._auto_mg_refresh()

    # ============================================================ contexto del backtest (a favor / en contra / fases)
    @staticmethod
    def _auto_ctx_summary(items, kind):
        """Una línea con el desglose por contexto de TODAS las estrategias de un mercado (señales sin filtrar)."""
        bd = strat.merge_breakdowns([it.get("ctx") for it in items])
        al, ph, rm, gt = bd.get("align", {}), bd.get("phase", {}), bd.get("room", {}), bd.get("gate", {})
        if not any(sum(v) for v in al.values()):
            return "Contexto: sin operaciones para desglosar."
        parts = [f"a favor {pct_txt(al.get('favor', [0, 0]))}", f"en contra {pct_txt(al.get('contra', [0, 0]))}",
                 f"lateral {pct_txt(al.get('lateral', [0, 0]))}"]
        fases = [f"{k} {pct_txt(ph[k])}" for k in strat.PHASES if sum(ph.get(k, [0, 0])) >= 10]
        txt = "Contexto de las señales sin filtrar (todas las estrategias, último 50%): " + " · ".join(parts)
        if fases:
            txt += " │ a favor según la fase: " + " · ".join(fases)
        if sum(rm.get("nospace", [0, 0])):
            txt += f" │ pegadas a un nivel en contra {pct_txt(rm['nospace'])}"
        if sum(gt.get("pass", [0, 0])) or sum(gt.get("block", [0, 0])):
            txt += (f" │ el filtro MTF deja pasar {pct_txt(gt.get('pass', [0, 0]))} y bloquearía "
                    f"{pct_txt(gt.get('block', [0, 0]))}")
        return txt

    def _auto_ctx_refresh(self):
        t = self.ctx_tree
        t.delete(*t.get_children())
        full, key, it = self._auto_mg_target()
        if it is None or not it.get("ctx"):
            self.ctx_target.config(text="Elige una fila del ranking (después de «Comparar estrategias») para ver cómo "
                                        "rindieron sus señales a favor y en contra de la tendencia.")
            self.ctx_note.config(text="")
            return
        kind = self.auto_kind
        be = it["valid"]["breakeven"]
        bd = it["ctx"]
        pool = strat.merge_breakdowns([x.get("ctx") for x in self.auto_results.get(full, [])])
        self.ctx_target.config(text=f"{pretty_pair(full).replace('  · ', ' ')} · {it['name']} a "
                                    f"{dur_label(it['minutes'], kind)} · equilibrio {be * 100:.1f}%")
        rows = [("TENDENCIA 1m·5m·15m·1h", None, None)]
        rows += [(strat.bd_label(k), "align", k) for k in ("favor", "contra", "lateral")]
        rows += [("A FAVOR · SEGÚN LA FASE", None, None)]
        rows += [("   " + strat.bd_label(k), "phase", k) for k in strat.PHASES]
        rows += [("NIVELES DE 15m Y 1h", None, None)]
        rows += [(strat.bd_label(k), "room", k) for k in ("space", "nospace")]
        rows += [("FILTRO DE TENDENCIA", None, None)]
        rows += [(strat.bd_label(k), "gate", k) for k in ("pass", "block")]
        for n_, (label, g, k) in enumerate(rows):
            if g is None:
                t.insert("", "end", iid=f"h{n_}", text="  " + label, values=("", "", "", ""), tags=("muted",))
                continue
            w, l = (bd.get(g) or {}).get(k, [0, 0])
            d = w + l
            pw, pl = (pool.get(g) or {}).get(k, [0, 0])
            if not d and not (pw + pl):
                continue
            r = w / d if d else None
            tag = "muted" if d < 20 else ("win" if r > be else "loss")
            t.insert("", "end", iid=f"r{n_}", text="  " + label,
                     values=(d, f"{r * 100:.1f}%" if d else "—", f"{(r - be) * 100:+.1f} pp" if d else "—",
                             pct_txt([pw, pl])), tags=(tag,))
        self.ctx_note.config(text=(
            f"Señales de «{it['name']}» SIN ningún filtro en el último 50% de las velas, clasificadas por el contexto en el "
            f"momento de entrar (la columna de la derecha suma las {len(self.auto_results.get(full, []))} estrategias de "
            f"este par). Verde = sobre el equilibrio, rojo = debajo, gris = menos de 20 operaciones (puede ser azar). "
            f"Si «En contra» y «retroceso en curso» salen en rojo y «A favor» / «retroceso agotado» en verde, el filtro de "
            f"tendencia obligatorio es lo que conviene en este par."))

    def _auto_dur_refresh(self):
        t = self.dur_tree
        t.delete(*t.get_children())
        full, key, it = self._auto_mg_target()
        kind = self.auto_kind
        if it is None or not it.get("by_minutes"):
            self.dur_target.config(text="Elige una fila del ranking para ver cómo rinde a cada duración.")
            self.dur_note.config(text="")
            return
        self.dur_target.config(text=f"{pretty_pair(full).replace('  · ', ' ')} · {it['name']}")
        for m in sorted(it["by_minutes"]):
            b = it["by_minutes"][m]
            v = b["valid"]
            text, color = strat.verdict(v)
            d = v["wins"] + v["losses"]
            star = "★ " if m == it["minutes"] else "   "
            t.insert("", "end", iid=f"d{m}", text=f"  {star}{dur_label(m, kind)}",
                     values=(v["trades"], f"{v['winrate'] * 100:.1f}%" if d else "—",
                             f"{(v['winrate'] - v['breakeven']) * 100:+.1f} pp" if d else "—",
                             strat.flags_label(b["flags"], BAR[kind], short=True), VERDICT_SHORT.get(text, text)),
                     tags=(TAGS[color],))
        self.dur_note.config(text=("★ = la mejor duración en el primer 50% de las velas (las cifras son del último 25%). Con "
                                   "«Duración: automática» el bot usa siempre la ★; con «según la tendencia» prueba en cada "
                                   "vela las que superan el equilibrio, en este orden, y entra con la primera cuya temporalidad "
                                   "guía acompaña (1 min → 5m, 2–3 min → 15m, 4–5 min → 15m y 1h). Escoger a ojo la mejor de "
                                   "varias duraciones infla el resultado: confírmalo en Demo."))

    # ============================================================ simulación de martingala
    def _auto_mg_target(self):
        """(mercado, estrategia, resultado) de la fila elegida en el ranking, o de la mejor del primer par."""
        sel = self.rank_tree.selection()
        if sel and "|" in sel[0]:
            full, key = sel[0].split("|", 1)
            it = self._auto_result_for(full, key)
            if it:
                return full, key, it
        pairs = self._auto_selected()
        mode = MARKET_KEYS[self.auto_market.current()]
        for base in pairs[:1]:
            for full in self._auto_markets(base, mode):
                if full in self.auto_results:
                    key = self.auto_best.get(full) or self.auto_results[full][0]["key"]
                    return full, key, self._auto_result_for(full, key)
        return None, None, None

    def _auto_mg_refresh(self):
        t = self.mg_tree
        t.delete(*t.get_children())
        full, key, it = self._auto_mg_target()
        if it is None or not it["valid"].get("seq"):
            self.mg_target.config(text="Pulsa «Comparar estrategias» y elige una fila del ranking para simular.")
            self.mg_note.config(text="")
            return

        def num(fld, default):
            try:
                v = float(fld.get().replace(",", "."))
                return v if v > 0 else default
            except ValueError:
                return default

        base, mult = num(self.amount, 5.0), max(1.1, num(self.auto_mgmult, 2.0))
        _, pay = self._mkt_info(self.auto_status["table"], full, self.auto_kind)
        payout = pay / 100.0 if pay else num(self.auto_payout, 85.0) / 100.0
        v = it["valid"]
        self.mg_target.config(text=f"{pretty_pair(full).replace('  · ', ' ')} · {it['name']} · {v['trades']} operaciones "
                                   f"· base ${base:g} · multiplicador ×{mult:g} · pago {payout * 100:.0f}%")
        rows = []
        scen = [("fix", 0, "Monto fijo"), ("ic", 1, "Int. compuesto · 1 ciclo"), ("ic", 2, "Int. compuesto · 2 reinv."),
                ("mg", 1, "Martingala · 1 nivel"), ("mg", 2, "Martingala · 2 niveles")]
        for typ, lv, name in scen:
            if typ == "ic":
                r = strat.simulate_compound(v["seq"], payout, lv, base)
            else:
                r = strat.simulate_mg(v["seq"], payout, lv if typ == "mg" else 0, mult, base)
            rows.append((typ, lv, r))
            ciclos = (f"{r['cycle_rate'] * 100:.1f}%" if typ != "fix" else f"{v['winrate'] * 100:.1f}%")
            t.insert("", "end", iid=f"{typ}{lv}", text="  " + name,
                     values=(ciclos, (f"{r['net']:+,.0f}$" if abs(r['net']) >= 1000 else f"{r['net']:+.2f}$"),
                             f"{r['staked']:,.0f}$", f"{r['yield'] * 100:+.1f}%", f"-{r['max_dd']:.0f}$",
                             f"{r['max_stake']:.0f}$"),
                     tags=("win" if r["net"] > 0 else "loss",))
        p = v["winrate"]
        be = strat.breakeven(payout)
        ic1 = strat.compound_expectancy(p, payout, 1)
        worst = base * sum(mult ** k for k in range(3))
        self.mg_note.config(text=(
            f"Interés compuesto: al ganar, la siguiente operación de ese par va por monto + ganancia; al completar el ciclo "
            f"o al perder vuelve a ${base:g}. Lo máximo que pierde un ciclo es ${base:g} (la martingala de 2 niveles puede "
            f"perder ${worst:g} de golpe). Ninguna gestión cambia el equilibrio ({be * 100:.1f}%): con la efectividad del "
            f"backtest ({p * 100:.1f}%) cada ciclo de interés compuesto vale en promedio {ic1 * 100:+.1f}% del monto base. "
            f"Si hay ventaja, el interés compuesto la acelera; si no la hay, pierde más rápido que el monto fijo."))

    # ============================================================ decisiones (tabla de razonamiento)
    def _auto_decision(self, full, key, dec, why, tag="muted"):
        now = datetime.now()

        def add():
            t = self.dec_tree
            if full:
                t.insert("", 0, text="  " + pretty_pair(full), image=self.icons.pair(full, 26),
                         values=(f"{now:%H:%M:%S}", strat.SHORT.get(key, "—"), dec, why), tags=(tag,))
            else:
                t.insert("", 0, text="  Modo automático", values=(f"{now:%H:%M:%S}", "—", dec, why), tags=(tag,))
            kids = t.get_children()
            if len(kids) > 150:
                t.delete(*kids[150:])
        self._ui(add)

    @staticmethod
    def _auto_in_hours(a, now):
        m = now.hour * 60 + now.minute
        f, t = a["h_from"], a["h_to"]
        return f <= m < t if f <= t else (m >= f or m < t)

    # ============================================================ motor (hilo propio)
    def _auto_start_engine(self, sess):
        """Arranca el hilo del modo automático y el vigía de mercados para esta sesión (si no corren ya).
        Con candado: la ventana y el hilo de la sesión pueden pedirlo en el mismo instante."""
        if sess is None or sess.over.is_set():
            return
        with self._engine_lock:
            th = self._auto_thread
            if not (th and th.is_alive() and getattr(th, "sess", None) is sess):
                th = threading.Thread(target=self._auto_engine, args=(sess,), daemon=True, name="espyx-auto")
                th.sess = sess
                self._auto_thread = th          # el motor anterior (si lo hubiera) se retira al ver que ya no es este
                th.start()
            wt = self._watch_thread
            if not (wt and wt.is_alive() and getattr(wt, "sess", None) is sess):
                wt = threading.Thread(target=self._auto_watch, args=(sess,), daemon=True, name="espyx-watch")
                wt.sess = sess
                self._watch_thread = wt
                wt.start()

    def _auto_watch(self, sess):
        """Mantiene fresca la lista de activos abiertos (y sus pagos) sin frenar al motor."""
        while not self.stop_event.is_set() and not sess.over.is_set():
            if self.auto_flag.is_set():
                self._auto_table()
            self.stop_event.wait(5.0)

    def _auto_engine(self, sess):
        """Hilo del modo automático: despierta al cierre de cada vela (hora del servidor) y analiza."""
        last = None
        warmed = None
        me = threading.current_thread()
        while not self.stop_event.is_set() and not sess.over.is_set():
            if self._auto_thread is not me:         # hay otro motor más nuevo: este se retira
                return
            if not self.auto_flag.is_set() or not self.auto_cfg:
                last = None
                self.stop_event.wait(0.3)
                continue
            a = dict(self.auto_cfg)
            kind = a["kind"]
            bar = BAR[kind]
            sig = (kind, tuple(a["pairs"]), a["mode"], a["market"], a["dur_mode"])
            if warmed != sig:
                warmed = sig
                try:
                    self._auto_prewarm(a, sess.cfg)
                except Exception as e:
                    self._log(f"Modo automático: no pude preparar los modelos ({e})")
            now = self.broker.now()
            close = int(now // bar) * bar
            since = now - close
            if last is None:
                # recién activado: si estamos justo al inicio de una vela se analiza ya; si no, se espera la siguiente
                last = close - bar if since <= min(3.0, a["entry_window"] - 1) else close
            if close != last and since >= CLOSE_DELAY:
                last = close
                if since <= a["entry_window"] - 0.5:
                    t0 = time.time()
                    try:
                        self._auto_cycle(sess, a, close)
                    except Exception as e:  # el análisis nunca debe tumbar al bot
                        self._log(f"Modo automático: error en el análisis ({e})")
                        self._log_file(traceback.format_exc())
                    took = time.time() - t0
                    if took > a["entry_window"]:
                        self._log(f"Modo automático: el análisis tardó {took:.1f} s; elige menos pares o sube el segundo "
                                  f"máximo de entrada.")
                else:
                    self._auto_decision(None, None, "Tarde", f"el análisis empezaría en el segundo {since:.0f} de la vela "
                                                            f"(máximo {a['entry_window']:g}); se espera la siguiente", "skip")
                self._auto_maybe_recompare(a)
            nxt = close + bar + CLOSE_DELAY
            self.stop_event.wait(min(0.25, max(0.01, nxt - self.broker.now())))

    def _auto_prewarm(self, a, cfg):
        """Al activar: descarga en segundo plano el histórico de cada mercado (para leer la tendencia de 1 h) y empieza a
        entrenar los modelos que hará falta usar (así están listos antes)."""
        table = self.auto_status["table"]
        kind = a["kind"]
        for base in a["pairs"]:
            for full, _pay in self._auto_resolve(base, a, table)[:1]:
                self._auto_fill_async(full, BAR[kind], LIVE_CANDLES[kind])
                plan, _ = self._auto_plan(full, a, cfg)
                if not plan:
                    continue
                for d in plan["durs"]:
                    if plan["key"] in strat.MODEL_KEYS:
                        self._auto_model(full, plan["key"], d["params"], d["minutes"], d["sub"], kind)
                    if "pat" in d["flags"]:
                        self._auto_model(full, "pattern", strat.Params(), d["minutes"], None, kind)

    def _auto_need(self, plan=None, kind="binary"):
        """Velas cerradas que necesita el análisis en vivo: las de la vela base y ≥ 22 h para la tendencia de 1 h."""
        return LIVE_CANDLES.get(kind, LIVE_CANDLES["binary"])

    def _auto_fill_async(self, full, bar, need):
        """Descarga en segundo plano el histórico de un mercado (si no está ya en la caché o descargándose)."""
        ck = (full, bar)
        with self._cc_lock:
            have = len(self._cc.get(ck) or [])
            if ck in self._cc_filling or have >= need - 30 or time.time() - self._cc_fail.get(ck, 0) < 90:
                return
            self._cc_filling.add(ck)

        def work():
            try:
                now = self.broker.now()
                self._auto_candles(full, bar, need, int(now // bar) * bar, fill=True)
            except Exception as e:
                with self._cc_lock:
                    self._cc_fail[ck] = time.time()
                self._log(f"No pude descargar el histórico de {pretty_pair(full)} ({e}); se reintenta en 1–2 min.")
            finally:
                with self._cc_lock:
                    self._cc_filling.discard(ck)
        self._fill_io.submit(work)

    def _auto_candles(self, full, bar, need, close, fill=True):
        """Velas CERRADAS hasta el cierre `close` (la última es la que acaba de cerrar), como mucho `need`.
        Caché por mercado: la primera vez descarga el histórico completo (≥ 22 h, para la tendencia de 1 h) y después solo
        pide las últimas velas, así el análisis de todos los pares tarda décimas de segundo.
        Con `fill=False` nunca descarga el histórico (devuelve None si aún falta) para no frenar al motor."""
        ck = (full, bar)
        with self._cc_lock:
            cur = list(self._cc.get(ck) or [])
            filled_at = self._cc_filled.get(ck, 0)
        gap = int((close - bar - cur[-1]["t"]) // bar) if cur else None
        short = len(cur) < need - 30 and time.time() - filled_at > 1800      # (un mercado con poco histórico no se repite)
        if not cur or short or gap > 800:
            if not fill:
                return None
            hist = self.vstore.history(self.broker, full, bar, need + 10)    # v15.2: solo baja lo que no está guardado
            merged = {c["t"]: c for c in cur}
            for c in hist:
                merged[c["t"]] = c
            cur = [merged[t] for t in sorted(merged) if t + bar <= close]
            with self._cc_lock:
                self._cc_filled[ck] = time.time()
            gap = int((close - bar - cur[-1]["t"]) // bar) if cur else 0
        count = 6 if gap <= 3 else min(1000, gap + 6)
        merged = {c["t"]: c for c in cur}
        for attempt in range(2):
            raw = self.broker.get_candles(full, bar, count, endtime=close + 1, timeout=6)
            for c in strat.normalize_candles(raw):
                merged[c["t"]] = c
            if (close - bar) in merged:
                break
            time.sleep(0.35)            # el servidor aún no cerró la vela: un reintento
            count = 4
        out = [merged[t] for t in sorted(merged) if t + bar <= close]
        out = out[-need:]
        with self._cc_lock:
            self._cc[ck] = out
        return out

    def _auto_wait_results(self, markets, until):
        """Espera (como mucho hasta `until`, hora del servidor) el resultado de las operaciones de estos mercados que
        vencen antes de esa hora: así se sabe si toca gale y el par puede volver a entrar en esta misma vela.
        (En Blitz la operación dura exactamente una vela desde la compra, así que vence 1–2 s DESPUÉS del cierre.)"""
        with self.lock:
            evs = [v["done"] for v in self.open_trades.values()
                   if v.get("src") == "auto" and v.get("pair") in markets and v.get("done") is not None
                   and v.get("end") and v["end"] <= until - 0.4]
        for ev in evs:
            left = until - self.broker.now()
            if left <= 0:
                break
            ev.wait(left)

    def _auto_cycle(self, sess, a, close):
        cfg = sess.cfg
        kind, bar = a["kind"], BAR[a["kind"]]
        deadline = close + a["entry_window"]
        stamp = datetime.fromtimestamp(close)
        mg_on = a["stake_mode"] == "mg"
        in_hours = (not a["hours_on"]) or self._auto_in_hours(a, stamp)
        paused = time.time() < self.auto_pause_until
        table = self.auto_status["table"]
        self._next_analysis = close + bar

        pend0 = self.mg_auto.snapshot() if mg_on else {}
        if not pend0 and not in_hours:
            self._auto_decision(None, None, "Fuera de horario",
                                f"solo opera entre {a['h_from'] // 60:02d}:{a['h_from'] % 60:02d} y "
                                f"{a['h_to'] // 60:02d}:{a['h_to'] % 60:02d}", "skip")
            return
        if not pend0 and paused:
            self._auto_decision(None, None, "En pausa",
                                f"pausa tras perder: quedan {mmss(self.auto_pause_until - time.time())}", "skip")
            return

        # 1) qué mercado y plan corresponde a cada par
        targets = []
        for base in a["pairs"]:
            cands = self._auto_resolve(base, a, table)
            if not cands:
                self._auto_decision(self._auto_markets(base, a["market"])[0], None, "Cerrado",
                                    "mercado cerrado o pago por debajo del mínimo", "skip")
                continue
            note = news_note = None
            chosen = None
            for full, pay in cands:
                ev = self._auto_news_block(full, a)
                if ev:
                    mins = int(round((ev["t"] - time.time()) / 60))
                    note = (full, None, "Noticia",
                            f"{ev['ccy']} · {ev['title']} ({NEWS.IMPACT_ES.get(ev['impact'], '')}) "
                            + (f"en {mins} min" if mins > 0 else f"hace {-mins} min" if mins < 0 else "ahora")
                            + f": pausa de {a['news_before']:g} min antes a {a['news_after']:g} min después", "skip")
                    news_note = news_note or note
                    continue
                plan, why_not = self._auto_plan(full, a, cfg)
                if plan:
                    chosen = (base, full, pay, plan)
                    break
                note = (full, None, "Omitido", why_not, "muted")
            if chosen:
                targets.append(chosen)
            elif news_note or note:
                self._auto_decision(*(news_note or note))

        # 1b) «Solo los N mejores»: de los pares confirmados que pueden operar ahora, solo los N con mejor efectividad
        #     confiable (selección + prueba final juntas). Un par con gale o ciclo de interés compuesto a medias se respeta.
        if a.get("pair_filter") == "top" and len(targets) > a.get("top_n", 3):
            busy = set(self.mg_auto.snapshot()) if mg_on else set()
            if a["stake_mode"] == "ic":
                busy |= {m for m, st in self.ic_auto.snapshot().items() if st.get("step", 0) > 0}
            ranked = sorted(targets, key=lambda t: strat.confirm_score(t[3]["sub"]), reverse=True)
            keep = {t[1] for t in ranked[:a["top_n"]]} | {t[1] for t in targets if t[1] in busy}
            for i, t in enumerate(ranked):
                if t[1] not in keep:
                    self._auto_decision(t[1], t[3]["key"], "Omitido",
                                        f"fuera de los {a['top_n']} mejores (puesto {i + 1} por efectividad confiable)",
                                        "muted")
            targets = [t for t in targets if t[1] in keep]

        # 2) velas de todos los mercados A LA VEZ y 3) resultados de lo que venció en este cierre (para los gales)
        need = self._auto_need(kind=kind)
        futs = {t[1]: self._io.submit(self._auto_candles, t[1], bar, need, close, False) for t in targets}
        self._auto_wait_results({t[1] for t in targets}, min(deadline - 0.8, close + RESULT_WAIT[kind]))
        pending = {m: st for m, st in self.mg_auto.snapshot().items() if not st.get("opened")} if mg_on else {}

        # 4) primero los gales (tienen prioridad en cupos y capital), luego las entradas nuevas
        targets.sort(key=lambda t: 0 if t[1] in pending else 1)
        opened_gales = set()
        halt = False
        for base, full, pay, plan in targets:
            if halt or self.stop_event.is_set() or not self.auto_flag.is_set():
                break
            gale = pending.get(full)
            key = plan["key"]
            if not gale and not in_hours:
                self._auto_decision(full, key, "Fuera de horario", "solo se completan los gales pendientes", "skip")
                continue
            if not gale and paused:
                self._auto_decision(full, key, "En pausa", "pausa tras perder (solo se completan los gales)", "skip")
                continue
            if kind == "blitz":
                bp = self._mkt_info(table, full, "blitz")[1]
                payout = (bp or pay or a["payout"] * 100) / 100.0
            else:
                payout = pay / 100.0 if pay else a["payout"]
            with self.lock:
                n_open = len(self.open_trades)
            if n_open >= a["max_open"]:
                self._auto_decision(full, key, "Omitido", f"{n_open} operación(es) abierta(s) (máximo {a['max_open']})"
                                    + (" · el gale queda en espera" if gale else ""), "skip" if gale else "muted")
                continue
            mw = cfg.get("max_wins") or 0
            if mw and not gale:
                with self.lock:
                    w_now = self.st["w"]
                if w_now + n_open >= mw:      # v15.2: no abrir más de las que pueden completar la meta de ganadas
                    self._auto_decision(full, key, "Omitido", f"{w_now} ganada(s) + {n_open} abierta(s) ya pueden "
                                                              f"completar la meta de {mw} ganadas", "muted")
                    continue
            self.auto_entries = [t for t in self.auto_entries if time.time() - t < 3600]
            if not gale and len(self.auto_entries) >= a["per_hour"]:
                self._auto_decision(full, key, "Omitido", f"límite de {a['per_hour']} entradas por hora")
                continue
            try:
                candles = futs[full].result(timeout=max(0.2, deadline - self.broker.now()))
            except Exception as e:
                self._auto_decision(full, key, "Error", f"no pude leer las velas a tiempo: {e or 'tiempo agotado'}", "loss")
                continue
            if candles is None:
                self._auto_fill_async(full, bar, need)
                self._auto_decision(full, key, "Preparando", f"descargando el histórico ({need} velas) para leer la "
                                                             f"tendencia de 1 h; opera en cuanto termine", "skip")
                continue
            if not candles or candles[-1]["t"] != close - bar:
                self._auto_decision(full, key, "Sin datos", "Exnova no entregó la vela que acaba de cerrar "
                                                            "(¿mercado cerrado o conexión lenta?)", "skip")
                continue
            view = strat.LiveView(candles)
            for attempt in range(2):
                durs = plan["durs"]
                if self.account_type == "REAL" and a["gate_on"]:
                    allowed, gmsg = [], None
                    for d in durs:
                        ok, gm = self._auto_gate_ok(key, full, dkey(d["minutes"], kind), payout, a)
                        if ok:
                            allowed.append(d)
                        else:
                            gmsg = gmsg or gm
                    if not allowed:
                        self._auto_decision(full, key, "Puerta Demo", gmsg, "skip")
                        break
                    durs = allowed
                d, action, why, winp = self._auto_eval(full, key, durs, candles, view, payout, kind)
                if not action:
                    if gale:
                        self._auto_decision(full, key, f"Gale {gale['level']} en espera",
                                            f"el análisis no confirma ({why})", "skip")
                    else:
                        self._auto_decision(full, key, "Sin señal", why, "muted")
                    break
                m = d["minutes"]
                if gale and not self.mg_auto.direction_ok(full, action):
                    self._auto_decision(full, key, f"Gale {gale['level']} en espera",
                                        f"el análisis indica {'COMPRAR' if action == 'call' else 'VENDER'}, contrario a la "
                                        f"operación perdida", "skip")
                    break
                ok, cmsg = self._auto_corr_ok(full, action, a)
                if not ok:
                    self._auto_decision(full, key, "Omitido", f"{strat.SHORT[key]} da señal, pero {cmsg}", "skip")
                    break
                amount, info, level = self._auto_stake(cfg, a, d, winp, payout, full)
                ic_step = self.ic_auto.stake(cfg["amount"], full)[1] if a["stake_mode"] == "ic" else 0
                if amount is None:
                    self._auto_decision(full, key, "Omitido", f"{strat.SHORT[key]} da señal, pero {info}", "skip")
                    break
                stop = self._limit_reason(cfg)
                if stop:
                    self._auto_decision(full, key, "Detenido", stop, "skip")
                    halt = True                      # como antes: un límite alcanzado corta todo el ciclo
                    break
                now = self.broker.now()
                if now > deadline:
                    self._auto_decision(full, key, "Tarde", f"la orden saldría en el segundo {now - close:.0f} de la vela "
                                                            f"(máximo {a['entry_window']:g}): no se entra", "skip")
                    break
                word = "COMPRAR" if action == "call" else "VENDER"
                fl = f" · filtros: {strat.flags_label(d['flags'], bar)}" if d["flags"] else ""
                dec = (("▲ " if action == "call" else "▼ ") + f"{word} {dur_label(m, kind)}" + (f" · G{level}" if level else "")
                       + (f" · IC{ic_step}" if ic_step else ""))
                self._auto_decision(full, key, dec, why + fl, "open")
                self._log(f"🤖 {pretty_pair(full)} · {strat.SHORT[key]} a {dur_label(m, kind)}: {word} — {why}{fl} · {info}")
                otc_txt = " (OTC)" if full.endswith("-OTC") else ""
                label = (f"{split_full(full)[0]}{otc_txt} {stamp:%H:%M:%S} {word} [auto"
                         + (" blitz" if kind == "blitz" else "") + (f" G{level}" if level else "")
                         + (f" IC{ic_step}" if ic_step else "") + "]")
                res = self._auto_open(sess, full, action, label, key, m, amount, payout, info, kind, close, level, a,
                                      ic_step)
                if res is True:
                    self.auto_entries.append(time.time())
                    if gale:
                        opened_gales.add(full)
                    break
                if res == "closed" and attempt == 0 and self.broker.now() < deadline - 1.0:
                    plan2, _ = self._auto_plan(full, a, cfg)          # ya sin la duración que Exnova no acepta
                    if plan2 and plan2["key"] == key:
                        self._log(f"   ↻ {pretty_pair(full)}: se prueba en esta misma vela con "
                                  f"{', '.join(dur_label(x['minutes'], kind) for x in plan2['durs'])}.")
                        plan = plan2
                        continue
                break

        # 5) gales pendientes que no se abrieron en esta vela: se descuenta su espera
        for mkt, st in pending.items():
            if mkt in opened_gales:
                continue
            lvl = self.mg_auto.on_bar_without_entry(mkt)
            if lvl is not None:
                self._log(f"Martingala {pretty_pair(mkt)}: el análisis no confirmó el gale nivel {lvl} en "
                          f"{self.mg_auto.wait_bars} vela(s); se cierra el ciclo y el par vuelve al monto base.")

    def _auto_eval(self, full, key, durs, candles, view, payout, kind):
        """Prueba las duraciones del plan en su orden y devuelve (duración, acción, motivo, prob. de ganar) con la primera
        que da señal y pasa los filtros, o (None, None, motivo, None) si ninguna."""
        first_why, tried = None, []
        for d in durs:
            m, p = d["minutes"], d["params"]
            model = self._auto_model(full, key, p, m, d["sub"], kind) if key in strat.MODEL_KEYS else None
            pat_model = (self._auto_model(full, "pattern", strat.Params(), m, None, kind)
                         if "pat" in d["flags"] else None)
            action, why, winp = strat.live_decision(candles, key, p, model, payout, d["flags"], pat_model, minutes=m,
                                                    view=view)
            if action:
                return d, action, why, winp
            tried.append(dur_label(m, kind))
            first_why = first_why or why
        if len(tried) > 1:
            first_why = f"{first_why} (probadas: {', '.join(tried)})"
        return None, None, first_why or "sin duraciones disponibles", None

    def _auto_vigilante(self, key, pair, kind, profit, meta):
        """Vigilante bayesiano: parte de la efectividad del backtest (encogida a medio camino del equilibrio, porque elegir
        la mejor de muchas la infla) con peso VIG_PRIOR operaciones y la corrige con cada resultado en vivo. Si la
        probabilidad de que la efectividad real esté bajo el equilibrio llega a VIG_P, pausa esa estrategia en ese par."""
        vk = (key, pair, kind)
        dq = self.auto_roll.setdefault(vk, deque(maxlen=VIG_WINDOW))
        dq.append(1 if profit > 0 else 0)
        if not self.auto_cfg.get("health_on") or len(dq) < VIG_MIN:
            return
        w_, n_ = sum(dq), len(dq)
        be = strat.breakeven(meta.get("payout") or 0.85)
        it = self._auto_result_for(pair, key, kind)
        v = it["valid"] if it else None
        bt = v["winrate"] if v and v["wins"] + v["losses"] >= strat.MIN_TRADES else be + 0.04
        prior = be + 0.5 * (bt - be)
        p_bad = strat.beta_cdf(be, prior * VIG_PRIOR + w_, (1.0 - prior) * VIG_PRIOR + n_ - w_)
        if p_bad >= VIG_P:
            why = (f"{w_}/{n_} ganadas en vivo: {p_bad * 100:.0f}% de probabilidad de estar bajo el equilibrio "
                   f"{be * 100:.1f}%")
            self.auto_blocked[vk] = (time.time() + HEALTH_BLOCK, why)
            dq.clear()
            self._log(f"⚠ Vigilante: {strat.SHORT[key]} en {pretty_pair(pair)} pausada {HEALTH_BLOCK // 60} min ({why}). "
                      f"«Mejor por par» pasa a la siguiente estrategia validada.")

    # ============================================================ modelos (en procesos aparte)
    def _auto_model(self, full, key, p, minutes, sub=None, kind="binary"):
        psig = tuple(sorted(p.__dict__.items())) if key == "pattern" else None
        mk = (full, key, minutes, kind, psig)
        with self._models_lock:
            ent = self.auto_models.get(mk)
            old = ent["model"] if ent else None
            now = time.time()
            if ent and ent["training"]:
                return old
            if ent and old is not None and now - ent["at"] < MODEL_MAX_AGE:
                return old
            if ent and old is None and now - ent.get("fail", 0) < 120:
                return None                                 # falló hace poco: no reintentar en bucle
            self.auto_models[mk] = {"model": old, "at": ent["at"] if ent else 0, "training": True,
                                    "fail": ent.get("fail", 0) if ent else 0}
        threading.Thread(target=self._auto_train, args=(mk, full, key, p, minutes, sub, kind), daemon=True).start()
        if old is None:
            self._log(f"Preparando «{strat.SHORT[key]}» de {pretty_pair(full)} a {dur_label(minutes, kind)} con las "
                      f"últimas {MODEL_CANDLES} velas (en segundo plano)…")
        return old

    def _auto_train(self, mk, full, key, p, minutes, sub=None, kind="binary"):
        model = None
        try:
            candles = strat.normalize_candles(self.broker.history(full, MODEL_CANDLES, BAR[kind]))
            vp = None
            if key == "ensemble":
                m0 = (sub or {}).get("model")
                vp = m0.vote_params if m0 is not None and m0.vote_params else strat.default_vote_params(p)
            model = self.pool.submit(W.fit_task, candles, key, p, minutes, vp).result(timeout=900)
            if model:
                model.trained_at = time.time()
                what = "memoria de patrones" if key == "pattern" else f"modelo «{strat.SHORT[key]}»"
                self._log(f"Listo: {what} de {pretty_pair(full)} a {dur_label(minutes, kind)} con {model.n_train} ejemplos; "
                          f"el precio subió el {model.base_rate * 100:.1f}% de las veces en ese tramo.")
            else:
                self._log(f"«{strat.SHORT[key]}» de {pretty_pair(full)}: pocas velas para entrenar.")
        except Exception as e:
            self._log(f"No se pudo preparar «{strat.SHORT.get(key, key)}» de {pretty_pair(full)}: {e}")
        with self._models_lock:
            old = (self.auto_models.get(mk) or {}).get("model")
            self.auto_models[mk] = {"model": model or old, "at": time.time() if model else 0, "training": False,
                                    "fail": 0 if model else time.time()}

    # ============================================================ abrir y cerrar operaciones automáticas
    def _auto_open(self, sess, full, action, label, key, m, amount, payout, info, kind, close, level, a, ic_step=0):
        """Abre la operación. Devuelve True (abierta), False (no se abrió) o "closed" (Exnova no admite esa duración ahora:
        queda bloqueada DUR_BLOCK segundos en ese par y el motor puede probar otra validada en esta misma vela)."""
        cfg = sess.cfg
        # capital, cupo y «un mercado a la vez» se apartan de forma atómica ANTES de comprar
        tok, why = self._reserve(cfg, amount, full, max_open=a["max_open"])
        if tok is None:
            self._auto_decision(full, key, "Omitido", why + (" · el gale queda en espera" if level else ""), "skip")
            return False
        deadline = close + a["entry_window"]
        secs = m * BAR[kind]
        end = close + secs if kind != "blitz" else None   # binarias: cierre exacto de la vela (como el backtest)
        tag = {"label": label, "pair": full, "action": action, "amount": amount, "end": end, "strategy": key,
               "minutes": m, "payout": payout, "kind": kind, "src": "auto", "secs": secs, "cfg": cfg}
        try:
            if kind == "blitz":
                ok, tid = self.broker.open_trade(amount, full, action, kind="blitz", seconds=secs, deadline=deadline,
                                                 tag=tag)
                end = self.broker.now() + secs
            else:
                ok, tid = self.broker.open_trade(amount, full, action, m, expiry=end, deadline=deadline, tag=tag)
        except Exception as e:
            self._release(tok)
            self._log(f"No se pudo abrir {label}: {e}")
            return False
        if not ok:
            self._release(tok)
            self._log(f"Exnova rechazó {label}: {tid}")
            if active_not_found(tid):
                self.auto_dur_block.update({(full, mm, kind): (time.time() + 3600, "Exnova no reconoce el activo")
                                            for mm in range(1, 6)})
                self._log(f"   → Exnova no reconoce {pretty_pair(full)} para {KIND_SHORT[kind]}: "
                          f"se deja 60 min y el bot sigue con los demás.")
                self._auto_decision(full, key, "Rechazada", "Exnova no reconoce este activo para esta modalidad; se deja "
                                                            "60 min", "skip")
                return False
            if purchase_closed(tid):
                self.auto_dur_block[(full, m, kind)] = (time.time() + DUR_BLOCK, "Exnova: «Time for purchasing options "
                                                                                 "is over»")
                self._log(f"   → Exnova no acepta compras a {dur_label(m, kind)} en {pretty_pair(full)} ahora mismo: esa "
                          f"duración se deja {DUR_BLOCK // 60} min y se usan las otras validadas en el backtest.")
                self._auto_decision(full, key, "Rechazada", f"{dur_label(m, kind)} no admitida ahora por Exnova; se usan "
                                                            f"las otras duraciones validadas", "skip")
                return "closed"
            if self.broker._no_disponible(tid):
                threading.Thread(target=self._diagnose, args=(full, cfg), daemon=True).start()
            return False
        cfg_t = dict(cfg, minutes=max(1, secs // 60))
        meta = {"label": label, "pair": full, "action": action, "amount": amount, "end": end, "strategy": key,
                "minutes": m, "payout": payout, "kind": kind, "src": "auto", "level": level,
                "mg": a["stake_mode"] == "mg", "ic": a["stake_mode"] == "ic", "ic_step": ic_step,
                "done": threading.Event()}
        self._commit(tok, tid, meta)
        if level:
            self.mg_auto.on_opened(full)
        kind_txt = " [blitz]" if kind == "blitz" else (" [binaria]" if getattr(self.broker, "last_open_kind", "turbo") == "binary"
                                                      else "")
        left = end - self.broker.now()
        self._log(f"✔ Abierta {label}{kind_txt} por ${amount:.2f} · {info} · vence {datetime.fromtimestamp(end):%H:%M:%S} "
                  f"(dura {left:.0f} s, entrada en el segundo {self.broker.now() - close:.1f})")
        self._show_balance(self.live_bal - amount)
        self._refresh_stats()
        th = threading.Thread(target=self._track, args=(tid, label, amount, full, None, cfg_t), daemon=True)
        th.start()
        sess.trackers.append(th)
        return True

    def _auto_after_trade(self, key, pair, profit, meta=None):
        """Al cerrar una operación automática: martingala del par, efectividad en vivo, puerta Demo, vigilante y pausa."""
        meta = meta or {}
        kind = meta.get("kind", "binary")
        m = meta.get("minutes") or 1
        dk = dkey(m, kind)
        if meta.get("mg"):
            lvl = meta.get("level", 0)
            ev, nxt = self.mg_auto.on_result(pair, lvl, profit, meta.get("action"))
            if ev == "gale":
                amt = self.mg_auto.amount(self.cfg.get("amount", 1.0), nxt)
                self._log(f"Martingala {pretty_pair(pair)}: perdió en nivel {lvl} → gale nivel {nxt} (${amt:.2f}) en la "
                          f"siguiente vela de {pretty_pair(pair)} si el análisis lo confirma.")
            elif ev == "win" and lvl:
                self._log(f"Martingala {pretty_pair(pair)}: ciclo GANADO en el gale nivel {lvl}; vuelve al monto base.")
            elif ev == "tie" and lvl:
                self._log(f"Martingala {pretty_pair(pair)}: empate en nivel {lvl}; se repite ese nivel.")
            elif ev == "unknown":
                self._log(f"Martingala {pretty_pair(pair)}: el resultado no se confirmó; por seguridad el ciclo se cierra y "
                          f"el par vuelve al monto base (revisa la operación en Exnova).")
            elif ev == "exhausted":
                self._log(f"Martingala {pretty_pair(pair)}: se agotaron los {self.mg_auto.levels} niveles (ciclo perdido)."
                          + (" Se desactiva el modo automático." if self.mg_auto.on_exhaust == "stop" else
                             " El par vuelve al monto base."))
                if self.mg_auto.on_exhaust == "stop":
                    self._ui(lambda: self.auto_on.set(False))
        if meta.get("ic") and not meta.get("late"):
            ev, nxt = self.ic_auto.on_result(pair, meta.get("amount", 0.0), meta.get("ic_step", 0), profit)
            if ev == "up":
                self._log(f"Interés compuesto {pretty_pair(pair)}: ganó → la siguiente de {pretty_pair(pair)} va por "
                          f"${nxt:.2f} (monto + ganancia).")
            elif ev == "cycle":
                self._log(f"Interés compuesto {pretty_pair(pair)}: ✔ CICLO COMPLETO; vuelve al monto base.")
            elif ev == "reset":
                self._log(f"Interés compuesto {pretty_pair(pair)}: perdió en el paso {meta.get('ic_step', 0)}; vuelve al "
                          f"monto base (el ciclo pierde solo el monto base).")
            elif ev == "unknown":
                self._log(f"Interés compuesto {pretty_pair(pair)}: resultado sin confirmar; por seguridad vuelve al monto base.")
        if key and profit is not None:
            d = self.auto_live.setdefault((key, pair, kind), {"n": 0, "w": 0, "l": 0})
            d["n"] += 1
            if profit > 0:
                d["w"] += 1
            elif profit < 0:
                d["l"] += 1
                pm = self.auto_cfg.get("pause_min", 0) if self.auto_cfg else 0
                if pm:
                    self.auto_pause_until = time.time() + pm * 60
                    self._log(f"Pausa de {pm:g} min tras la pérdida (modo automático; los gales pendientes siguen).")
            if profit != 0:
                gk = f"{key}|{pair}|{dk}"
                if self.account_type != "REAL":            # solo el Demo cuenta para habilitar el Real
                    g = self.auto_gate.setdefault(gk, {"n": 0, "w": 0, "l": 0})
                    g["n"] += 1
                    g["w" if profit > 0 else "l"] += 1
                    self._gate_save()
                if not meta.get("late"):
                    self._auto_vigilante(key, pair, kind, profit, meta)
        self._ui_once("auto_after", lambda: (self._auto_refresh_cards(), self._auto_fill_ranking(),
                                             self._auto_health_refresh()))
