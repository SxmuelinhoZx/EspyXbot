#!/usr/bin/env python3
"""
EspyX Bot — bot de señales para Exnova con interfaz moderna (v15).

1) Inicias sesión (correo + contraseña, Demo o Real). Puedes guardar cuentas (la contraseña, cifrada por el sistema).
2) Pegas las señales y/o activas el modo automático; ajustas monto, capital y protecciones.
3) Pulsas Iniciar: el bot abre cada operación a su hora (hora del servidor de Exnova) y te muestra todo en vivo.
"""
import csv
import itertools
import json
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

import strategy as strat
from bot import Broker, parse_signals
from espyx_auto import AutoMixin
from espyx_ui import (ACCENT, ACCENT2, AMBER, BG, BORDER, CARD, GOLD_BG, GREEN, INPUT, MONO, MUTED, PANEL, RED,
                      TEXT, Anim, Btn, Card, ComboEntry, ComboField, CountLabel, Field, Icons, ScrollFrame, Segmented,
                      StatCard, StatusLabel, Tabs, font, make_tree, mixc, mono, pretty_pair, setup_style, toggle_row)
from martingala import PairMartingale
from secure_store import SecureStore

APP_NAME = "EspyX Bot"
VERSION = "v15.2"
MAX_LATE_SECONDS = 10
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "historial.csv")
DUMP_FILE = os.path.join(BASE_DIR, "diagnostico_activos.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "espyx_config.json")
LOG_FILE = os.path.join(BASE_DIR, "registro.log")
LOG_MAX_LINES = 4000


def mmss(seconds):
    s = max(0, int(seconds + 0.999))
    return f"{s // 60:02d}:{s % 60:02d}"


def money(v, sign=False):
    s = "+" if (sign and v > 0) else "-" if v < 0 else ""
    return f"{s}${abs(v):,.2f}"


def rate_color(rate):
    return GREEN if rate >= 0.55 else AMBER if rate >= 0.45 else RED


def label_info(label):
    """De «EURUSD (OTC) 13:30 COMPRAR» saca ('EURUSD-OTC', 'COMPRAR')."""
    up = label.upper()
    parts = label.split()
    base = parts[0] if parts else "—"
    otc = "(OTC)" in up or "[OTC AUTO]" in up
    return base + ("-OTC" if otc else ""), ("VENDER" if "VENDER" in up else "COMPRAR")


def dir_text(action):
    return "▲ COMPRAR" if action in ("call", "COMPRAR") else "▼ VENDER"


def fmt_result(profit):
    if profit is None:
        return "Sin confirmar", "skip"
    if profit > 0:
        return f"Ganó {profit:+.2f}", "win"
    if profit < 0:
        return f"Perdió {profit:+.2f}", "loss"
    return "Empate 0.00", "muted"


class Session:
    """Una ejecución del bot (de «Iniciar» a que termina). Sus hilos se detienen con `over`."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.trackers = []
        self.over = threading.Event()
        self.stop_reason = None


class CodeDialog(tk.Toplevel):
    """Ventana oscura para el código SMS de verificación."""

    def __init__(self, parent, prompt):
        super().__init__(parent)
        self.title("Verificación")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.result = None
        self.transient(parent)
        tk.Label(self, text="Verificación en dos pasos", bg=CARD, fg=TEXT, font=font(15, True)).pack(
            padx=28, pady=(24, 4), anchor="w")
        tk.Label(self, text=prompt, bg=CARD, fg=MUTED, font=font(10), wraplength=340, justify="left").pack(
            padx=28, anchor="w")
        self.field = Field(self, "Código", width=26, bg=CARD)
        self.field.pack(fill="x", padx=28, pady=16)
        row = tk.Frame(self, bg=CARD)
        row.pack(fill="x", padx=28, pady=(0, 24))
        Btn(row, "Cancelar", self._cancel, kind="ghost", width=120, height=40, bg=CARD).pack(side="left")
        Btn(row, "Confirmar", self._ok, kind="primary", width=170, height=40, bg=CARD).pack(side="right")
        self.field.entry.bind("<Return>", lambda e: self._ok())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.field.focus()

    def _ok(self):
        self.result = self.field.get().strip() or None
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class App(AutoMixin, tk.Tk):
    def __init__(self):
        super().__init__()
        # Con hilos de Python haciendo cálculos, cada llamada a la interfaz espera su turno del GIL (5 ms por defecto).
        # Bajarlo a 1 ms hace que la ventana responda mucho antes cuando el bot está analizando.
        sys.setswitchinterval(0.001)
        self.title(f"{APP_NAME} {VERSION}")
        self.configure(bg=BG)
        w, h = 1200, 830
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.minsize(1040, 740)
        setup_style(self)
        self.icons = Icons()
        try:
            self.iconphoto(True, self.icons.logo(64, BG, transparent=True))
        except Exception:
            pass

        self.broker = None
        self.account_type = "PRACTICE"
        self.stop_event = threading.Event()
        self.worker = None
        self.sess = None
        self.ui_q = queue.Queue()      # callables a ejecutar en el hilo de la ventana
        self._once, self._once_lock = {}, threading.Lock()
        self._log_lock = threading.Lock()
        self.lock = threading.Lock()   # estado del bot
        self.broker_lock = threading.Lock()  # una sola consulta de saldo / activos a la vez (nunca desde la ventana)
        self.st = {"pnl": 0.0, "open": 0.0, "w": 0, "l": 0, "consec": 0}
        self.equity = []               # resultado acumulado tras cada operación cerrada (curva de capital)
        self.pair_stats = {}           # par -> {"n","w","l"}
        self.cfg = {}
        self.open_trades = {}          # id -> {label, pair, action, amount, end, ...}
        self._reserving = {}           # compras en curso: capital y cupo ya apartados (atómico entre hilos)
        self._tok = itertools.count(1)
        self.next_signal = None        # (par, acción, "HH:MM", epoch)
        self.live_bal = 0.0
        self.pages = {}
        self.mg_sig = PairMartingale()  # martingala por par de las señales pegadas
        self._sig_gales = set()         # (par, inicio de vela) donde ya entró un gale de señal
        self._hist_tot = {}
        self._starting = False
        self._toast_job = None
        self.store = SecureStore()
        self._auto_init()

        self.login_frame = tk.Frame(self, bg=BG)
        self.main_frame = tk.Frame(self, bg=BG)
        self._build_login()
        self._build_main()
        self.login_frame.pack(fill="both", expand=True)
        self.after(50, self._pump)
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ utilidades de hilo
    def _ui(self, fn):
        self.ui_q.put(fn)

    def _ui_once(self, key, fn):
        """Como _ui, pero si llegan muchas peticiones iguales antes de que la ventana las atienda, solo corre la última."""
        with self._once_lock:
            first = key not in self._once
            self._once[key] = fn
        if first:
            self.ui_q.put(lambda: self._run_once(key))

    def _run_once(self, key):
        with self._once_lock:
            fn = self._once.pop(key, None)
        if fn:
            fn()

    def _log(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        self._log_file(line)
        self._ui(lambda: self._append_log(line))

    def _log_file(self, text):
        """También guarda el registro en registro.log (útil para revisar qué pasó o enviarlo si algo falla)."""
        try:
            with self._log_lock:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
                    os.replace(LOG_FILE, LOG_FILE.replace(".log", ".anterior.log"))
                with open(LOG_FILE, "a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.now():%Y-%m-%d} {text}\n")
        except Exception:
            pass

    def _append_log(self, line):
        low = line.lower()
        if "✔" in line or " ganó" in low or "conectado" in low or "cuenta cambiada" in low or "ciclo ganado" in low:
            tag = "ok"
        elif any(k in low for k in ("perdió", "rechaz", "no se pudo", "detenido", "error", "no pude", "agotaron")):
            tag = "bad"
        elif any(k in low for k in ("salto", "ya pasó", "sin confirmar", "ojo", "deteniendo", "tarde", "gale", "martingala")):
            tag = "warn"
        else:
            tag = "info"
        box = self.log_box
        box.config(state="normal")
        box.insert("end", line + "\n", tag)
        n = int(box.index("end-1c").split(".")[0])
        if n > LOG_MAX_LINES:                    # el registro en pantalla no crece sin límite (se volvía lento)
            box.delete("1.0", f"{n - LOG_MAX_LINES}.0")
        box.see("end")
        box.config(state="disabled")

    def _pump(self):
        """Atiende lo que mandan los hilos, como mucho 30 ms seguidos para que la ventana nunca se congele."""
        t0 = time.perf_counter()
        try:
            while time.perf_counter() - t0 < 0.03:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception as e:  # nunca dejar morir el bucle de la ventana
                    print("Error de interfaz:", e)
                    self._log_file("Error de interfaz: " + traceback.format_exc())
        except queue.Empty:
            pass
        self.after(15 if not self.ui_q.empty() else 50, self._pump)

    def _toast(self, text, color=AMBER, ms=7000):
        """Aviso breve en la barra inferior (no bloquea la ventana): aparece y se desvanece con un fundido."""
        self.toast_lbl.config(text=text, fg=PANEL)
        Anim.tween("toast", 260, lambda p: self.toast_lbl.config(fg=mixc(PANEL, color, p)))
        if self._toast_job:
            self.after_cancel(self._toast_job)

        def out():
            Anim.tween("toast", 400, lambda p: self.toast_lbl.config(fg=mixc(color, PANEL, p)),
                       done=lambda: self.toast_lbl.config(text=""))
        self._toast_job = self.after(ms, out)

    # ------------------------------------------------------------ configuración guardada
    def _load_conf(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        if "accounts" not in data:                 # formato de la v12: un solo correo
            em = data.get("email") or ""
            data["accounts"] = [{"email": em, "account": data.get("account", "PRACTICE"), "pw": False}] if em else []
            data["last"] = em
        return data

    def _save_conf(self, data):
        try:
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            pass

    # ------------------------------------------------------------ login
    def _build_login(self):
        f = self.login_frame
        f.grid_columnconfigure(0, weight=5, uniform="cols")
        f.grid_columnconfigure(1, weight=6, uniform="cols")
        f.grid_rowconfigure(0, weight=1)
        left = tk.Frame(f, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew")
        right = tk.Frame(f, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        # ---- panel de marca
        box = tk.Frame(left, bg=PANEL)
        box.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(box, image=self.icons.logo(92, PANEL), bg=PANEL).pack(anchor="w")
        br = tk.Frame(box, bg=PANEL)
        br.pack(anchor="w", pady=(18, 0))
        tk.Label(br, text="ESPYX", bg=PANEL, fg=TEXT, font=mono(32, True)).pack(side="left")
        tk.Label(br, text=" ▸ TERMINAL", bg=PANEL, fg=ACCENT, font=mono(14, True)).pack(side="left", pady=(14, 0))
        tk.Label(box, text="Tus señales de Exnova, ejecutadas con disciplina.", bg=PANEL, fg=MUTED,
                 font=font(12)).pack(anchor="w", pady=(2, 22))
        for t in ("Pega las señales tal como llegan de Telegram",
                  "Capital, límites de pérdida y meta del día",
                  "Resultados, efectividad y saldo en vivo"):
            row = tk.Frame(box, bg=PANEL)
            row.pack(anchor="w", pady=4)
            tk.Label(row, text="✓", bg=PANEL, fg=ACCENT2, font=font(12, True)).pack(side="left")
            tk.Label(row, text=t, bg=PANEL, fg=TEXT, font=font(11)).pack(side="left", padx=(10, 0))
        chips = tk.Frame(box, bg=PANEL)
        chips.pack(anchor="w", pady=(30, 0))
        for pair, d in (("EURUSD", "▲ COMPRAR"), ("GBPJPY", "▼ VENDER"), ("AUDCAD", "▲ COMPRAR")):
            c = tk.Label(chips, image=self.icons.pair(pair, 26, CARD), text="  " + pretty_pair(pair) + "   " + d,
                         compound="left", bg=CARD, fg=TEXT, font=font(10, True), padx=12, pady=9)
            c.pack(anchor="w", pady=4)

        # ---- formulario
        form = tk.Frame(right, bg=BG)
        form.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(form, text="Inicia sesión", bg=BG, fg=TEXT, font=font(26, True)).pack(anchor="w")
        tk.Label(form, text="Conecta tu cuenta de Exnova para empezar.", bg=BG, fg=MUTED,
                 font=font(11)).pack(anchor="w", pady=(2, 22))
        self.conf = self._load_conf()
        emails = [a["email"] for a in self.conf.get("accounts", []) if a.get("email")]
        self.email = ComboEntry(form, "CORREO (elige una cuenta guardada o escribe otra)", emails, "", width=40, bg=BG)
        self.email.pack(fill="x", pady=(0, 14))
        self.email.cb.bind("<<ComboboxSelected>>", lambda e: self._pick_account(self.email.get()))
        self.email.cb.bind("<KeyRelease>", self._email_typed)
        self.password = Field(form, "CONTRASEÑA", "", show="•", width=40, bg=BG)
        self.password.pack(fill="x")
        self.password.entry.bind("<Key>", lambda e: setattr(self, "_pw_autofilled", False), add="+")
        row = tk.Frame(form, bg=BG)
        row.pack(fill="x", pady=(10, 4))
        self.remember = tk.BooleanVar(value=bool(emails))
        toggle_row(row, "Recordar mi correo", self.remember, bg=BG).pack(side="left")
        self.show_pw = tk.Label(row, text="Mostrar contraseña", bg=BG, fg=ACCENT2, font=font(10), cursor="hand2")
        self.show_pw.pack(side="right")
        self.show_pw.bind("<Button-1>", self._toggle_pw)
        row2 = tk.Frame(form, bg=BG)
        row2.pack(fill="x", pady=(4, 18))
        self.remember_pw = tk.BooleanVar(value=False)
        toggle_row(row2, "Recordar mi contraseña", self.remember_pw, bg=BG).pack(side="left")
        self.forget_lbl = tk.Label(row2, text="Olvidar esta cuenta", bg=BG, fg=MUTED, font=font(10), cursor="hand2")
        self.forget_lbl.pack(side="right")
        self.forget_lbl.bind("<Button-1>", lambda e: self._forget_account())
        self.remember_pw.trace_add("write", lambda *a: self._remember_changed("pw"))
        self.remember.trace_add("write", lambda *a: self._remember_changed("email"))

        tk.Label(form, text="TIPO DE CUENTA", bg=BG, fg=MUTED, font=font(9), anchor="w").pack(fill="x")
        self.seg = Segmented(form, [("PRACTICE", "Demo (práctica)", ACCENT), ("REAL", "Real", RED)],
                             command=self._on_seg, width=380, height=44, bg=BG)
        self.seg.pack(fill="x", pady=(6, 0))
        self.real_warn = tk.Label(form, text="Con la cuenta Real operarás con tu dinero.", bg=BG, fg=AMBER,
                                  font=font(9), anchor="w")
        self.login_btn = Btn(form, "Conectar", self._login, kind="primary", width=380, height=48, size=12, bg=BG)
        self.login_btn.pack(fill="x", pady=(22, 0))
        self.login_msg = tk.Label(form, text="", bg=BG, fg=RED, font=font(10), wraplength=380, justify="left",
                                  anchor="w")
        self.login_msg.pack(fill="x", pady=(14, 0))
        self.pw_note = tk.Label(form, text="", bg=BG, fg="#5C6370", font=font(9), wraplength=380, justify="left",
                                anchor="w")
        self.pw_note.pack(fill="x", pady=(18, 0))
        self.password.entry.bind("<Return>", lambda e: self._login())
        self.email.cb.bind("<Return>", lambda e: self.password.focus())
        self._pw_autofilled = False
        last = self.conf.get("last") or (emails[0] if emails else "")
        if last:
            self.email.set(last)
            self._pick_account(last)
        self._on_seg(self.seg.value)
        self._pw_note_refresh()

    def _account_entry(self, email):
        for a in self.conf.get("accounts", []):
            if a.get("email", "").lower() == (email or "").strip().lower():
                return a
        return None

    def _pick_account(self, email):
        """Al elegir una cuenta guardada: rellena la contraseña (si se guardó) y el tipo de cuenta."""
        ent = self._account_entry(email)
        if not ent:
            return
        self.remember.set(True)
        if ent.get("account") in ("PRACTICE", "REAL"):
            self.seg.value = ent["account"]
            self.seg._draw()
            self._on_seg(ent["account"])
        pw = self.store.load(ent["email"]) if ent.get("pw") else None
        if pw:
            self.password.set(pw)
            self._pw_autofilled = True
            self.remember_pw.set(True)
        else:
            if self._pw_autofilled:
                self.password.set("")
            self._pw_autofilled = False
            self.remember_pw.set(False)
        self._pw_note_refresh()

    def _email_typed(self, _e=None):
        ent = self._account_entry(self.email.get())
        if ent:
            if not self._pw_autofilled:
                self._pick_account(ent["email"])
        elif self._pw_autofilled:                # escribió otro correo: no dejar la contraseña de la otra cuenta
            self.password.set("")
            self._pw_autofilled = False

    def _remember_changed(self, which):
        if which == "pw" and self.remember_pw.get() and not self.remember.get():
            self.remember.set(True)
        if which == "email" and not self.remember.get() and self.remember_pw.get():
            self.remember_pw.set(False)
        self._pw_note_refresh()

    def _pw_note_refresh(self):
        if self.remember_pw.get():
            if self.store.available:
                txt = f"Tu contraseña se guardará {self.store.describe()}. Nunca en la carpeta del bot."
                self.pw_note.config(text=txt, fg="#5C6370" if self.store.secure else AMBER)
            else:
                self.pw_note.config(text="Este sistema no ofrece dónde guardar la contraseña de forma segura; no se guardará.",
                                    fg=AMBER)
        else:
            self.pw_note.config(text="Tu contraseña no se guarda. Solo se usa para iniciar sesión en Exnova.", fg="#5C6370")

    def _forget_account(self):
        email = self.email.get().strip()
        ent = self._account_entry(email)
        if not ent:
            self.login_msg.config(text="Esa cuenta no está guardada.", fg=MUTED)
            return
        if not messagebox.askyesno("Olvidar cuenta", f"¿Borrar {ent['email']} (y su contraseña guardada) de este equipo?"):
            return
        self.store.delete(ent["email"])
        self.conf["accounts"] = [a for a in self.conf.get("accounts", []) if a is not ent]
        if (self.conf.get("last") or "").lower() == ent["email"].lower():
            self.conf["last"] = ""
        self._save_conf(self.conf)
        self.email.set_values([a["email"] for a in self.conf["accounts"]])
        self.email.set("")
        self.password.set("")
        self._pw_autofilled = False
        self.remember_pw.set(False)
        self.login_msg.config(text="Cuenta olvidada en este equipo.", fg=MUTED)

    def _toggle_pw(self, _e=None):
        hidden = self.password.entry.cget("show") != ""
        self.password.entry.config(show="" if hidden else "•")
        self.show_pw.config(text="Ocultar contraseña" if hidden else "Mostrar contraseña")

    def _on_seg(self, value):
        if value == "REAL":
            self.real_warn.pack(fill="x", pady=(8, 0), after=self.seg)
        else:
            self.real_warn.pack_forget()

    def _login(self):
        email, pwd = self.email.get().strip(), self.password.get()
        if not email or not pwd:
            self.login_msg.config(text="Escribe tu correo y contraseña.", fg=RED)
            return
        self.login_btn.set_enabled(False)
        self.login_btn.set_text("Conectando…")
        self.login_msg.config(text="Conectando con Exnova…", fg=MUTED)
        account = self.seg.value
        threading.Thread(target=self._login_worker, args=(email, pwd, account), daemon=True).start()

    def _login_worker(self, email, pwd, account):
        try:
            broker = Broker(email, pwd)
            ok, reason = broker.connect()
            if not ok and reason == "2FA":
                code = self._ask_on_ui("Exnova te envió un código por SMS. Escríbelo:")
                if not code:
                    raise RuntimeError("Se canceló la verificación.")
                ok, reason = broker.connect(code.strip())
            if not ok:
                raise RuntimeError(f"No se pudo iniciar sesión: {reason}")
            broker.set_account(account)
            bal = broker.balance()
        except Exception as e:
            msg = str(e)
            self._ui(lambda: self._login_failed(msg))
            return
        # ¿Exnova devuelve el request_id de las velas? (si sí, se piden varios pares a la vez)
        try:
            broker.get_candles("EURUSD-OTC" if datetime.now().weekday() >= 5 else "EURUSD", 60, 3)
        except Exception:
            pass
        self._ui(lambda: self._login_ok(broker, account, bal, email, pwd))

    def _ask_on_ui(self, prompt):
        """Pide el código en el hilo de la ventana y espera la respuesta."""
        q = queue.Queue()

        def show():
            d = CodeDialog(self, prompt)
            self.wait_window(d)
            q.put(d.result)
        self._ui(show)
        return q.get()

    def _login_failed(self, msg):
        self.login_btn.set_enabled(True)
        self.login_btn.set_text("Conectar")
        self.login_msg.config(text=msg, fg=RED)

    def _login_ok(self, broker, account, bal, email, pwd=None):
        self.broker, self.account_type, self.live_bal = broker, account, bal
        if hasattr(broker, "on_late_order"):
            broker.on_late_order = self._late_order       # compras que Exnova confirma después de darlas por fallidas
        # cuentas guardadas
        accts = [a for a in self.conf.get("accounts", []) if a.get("email", "").lower() != email.lower()]
        pw_saved = False
        if self.remember.get():
            if self.remember_pw.get() and pwd:
                pw_saved = self.store.save(email, pwd)
                if not pw_saved:
                    self._log("No se pudo guardar la contraseña en este equipo (se guardó solo el correo).")
            else:
                self.store.delete(email)
            accts.insert(0, {"email": email, "account": account, "pw": pw_saved})
            self.conf["last"] = email
        else:
            self.store.delete(email)
            if (self.conf.get("last") or "").lower() == email.lower():
                self.conf["last"] = ""
        self.conf["accounts"] = accts[:12]
        self._save_conf(self.conf)
        self.email.set_values([a["email"] for a in self.conf["accounts"]])
        self.password.set("")
        self._pw_autofilled = False
        self.login_btn.set_enabled(True)
        self.login_btn.set_text("Conectar")
        self.login_msg.config(text="")
        self.login_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)
        self.tabs.select("panel")
        self._set_account_label(account, bal)
        self._load_history()
        self._log(f"Conectado. Cuenta {'REAL' if account == 'REAL' else 'DEMO'}, saldo ${bal:,.2f}."
                  + (" Contraseña guardada de forma segura." if pw_saved else ""))
        off = broker.clock_offset()
        if abs(off) >= 1.5:
            self._log(f"Ojo: el reloj de tu PC va {abs(off):.0f} s {'atrasado' if off > 0 else 'adelantado'} respecto a "
                      f"Exnova. El bot usa la hora del servidor para entrar a tiempo.")
        if broker.keyed_candles is False:
            self._log("Aviso: Exnova no devolvió el identificador de las velas; se pedirán de una en una (algo más lento).")

        def describe():
            try:
                for line in broker.describe_accounts():
                    self._log(f"   cuenta Exnova: {line}")
            except Exception:
                pass
        threading.Thread(target=describe, daemon=True).start()

    # ------------------------------------------------------------ cuenta demo/real
    def _set_account_label(self, account, bal):
        real = account == "REAL"
        self.acct_chip.config(text="REAL" if real else "DEMO", bg="#2A1318" if real else "#0F2419",
                              fg=RED if real else GREEN)
        self.bal_lbl.set_text(f"${bal:,.2f}")
        self.switch_btn.set_text("Cambiar a DEMO" if real else "Cambiar a REAL")

    def _switch_account(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Bot en marcha", "Detén el bot antes de cambiar de cuenta.")
            return
        target = "PRACTICE" if self.account_type == "REAL" else "REAL"
        if target == "REAL" and not messagebox.askyesno(
                "Dinero REAL", "Vas a cambiar a tu cuenta REAL. ¿Continuar?"):
            return
        self.switch_btn.set_enabled(False)
        self.start_btn.set_enabled(False)
        self.bal_lbl.config(text="…")

        def work():
            try:
                with self.broker_lock:
                    self.broker.set_account(target)
                    bal = self.broker.balance()
            except Exception as e:
                msg = str(e)
                self._ui(lambda: self._switch_done(None, None, msg))
                return
            self._ui(lambda: self._switch_done(target, bal, None))

        threading.Thread(target=work, daemon=True).start()

    def _switch_done(self, target, bal, err):
        self.switch_btn.set_enabled(True)
        self.start_btn.set_enabled(True)
        if err:
            messagebox.showerror("No se pudo cambiar de cuenta", err)
            self._set_account_label(self.account_type, self.live_bal)
            return
        self.account_type, self.live_bal = target, bal
        self._set_account_label(target, bal)
        self._refresh_hist_cards()
        self._log(f"Cuenta cambiada a {'REAL' if target == 'REAL' else 'DEMO'}. Saldo ${bal:,.2f}.")

    # ------------------------------------------------------------ pantalla principal
    def _build_main(self):
        f = self.main_frame
        # ---- cabecera
        head = tk.Frame(f, bg=PANEL)
        head.pack(side="top", fill="x")
        inner = tk.Frame(head, bg=PANEL)
        inner.pack(fill="x", padx=24, pady=12)
        tk.Label(inner, image=self.icons.logo(40, PANEL), bg=PANEL).pack(side="left")
        tb = tk.Frame(inner, bg=PANEL)
        tb.pack(side="left", padx=12)
        brand = tk.Frame(tb, bg=PANEL)
        brand.pack(anchor="w")
        tk.Label(brand, text="ESPYX", bg=PANEL, fg=TEXT, font=mono(17, True)).pack(side="left")
        tk.Label(brand, text=f" ▸ TERMINAL {VERSION}", bg=PANEL, fg=ACCENT, font=mono(10, True)).pack(side="left",
                                                                                                     pady=(5, 0))
        self.clock_lbl = tk.Label(tb, text="", bg=PANEL, fg=MUTED, font=mono(9))
        self.clock_lbl.pack(anchor="w")
        self.switch_btn = Btn(inner, "Cambiar a REAL", self._switch_account, kind="ghost", width=150, height=38,
                              size=10, bg=PANEL)
        self.switch_btn.pack(side="right")
        bal = tk.Frame(inner, bg=PANEL)
        bal.pack(side="right", padx=26)
        tk.Label(bal, text="SALDO", bg=PANEL, fg=MUTED, font=font(8, True)).pack(anchor="e")
        self.bal_lbl = CountLabel(bal, text="$0.00", bg=PANEL, fg=TEXT, font=mono(20, True))
        self.bal_lbl.pack(anchor="e")
        self.acct_chip = tk.Label(inner, text="DEMO", bg="#0F2419", fg=GREEN, font=mono(10, True), padx=14, pady=7)
        self.acct_chip.pack(side="right")
        tk.Frame(f, bg=BORDER, height=1).pack(side="top", fill="x")

        # ---- pestañas
        bar = tk.Frame(f, bg=BG)
        bar.pack(side="top", fill="x", padx=24, pady=(10, 0))
        self.tabs = Tabs(bar, [("panel", "Panel"), ("config", "Configurar"), ("auto", "Automático"),
                               ("hist", "Historial"), ("log", "Registro")], self._show_page)
        self.tabs.pack(side="left")
        tk.Frame(f, bg=BORDER, height=1).pack(side="top", fill="x", padx=24)

        # ---- barra de acciones (se empaca antes de las páginas para reservar su espacio)
        foot = tk.Frame(f, bg=PANEL)
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=BORDER, height=1).pack(fill="x")
        fi = tk.Frame(foot, bg=PANEL)
        fi.pack(fill="x", padx=24, pady=12)
        self.start_btn = Btn(fi, "▶   Iniciar bot", self._start, kind="primary", width=200, height=44, size=11,
                             bg=PANEL)
        self.start_btn.pack(side="left")
        self.stop_btn = Btn(fi, "■   Detener", self._stop, kind="danger", width=140, height=44, size=11, bg=PANEL)
        self.stop_btn.pack(side="left", padx=10)
        self.stop_btn.set_enabled(False)
        self.verify_btn = Btn(fi, "Verificar pares", self._verify_pairs, kind="ghost", width=150, height=44,
                              size=10, bg=PANEL)
        self.verify_btn.pack(side="left")
        self.status_lbl = StatusLabel(fi, text="EN ESPERA", bg=PANEL, fg=MUTED)
        self.status_lbl.pack(side="right")
        self.toast_lbl = tk.Label(fi, text="", bg=PANEL, fg=AMBER, font=font(9), anchor="e", justify="right",
                                  wraplength=420)
        self.toast_lbl.pack(side="right", padx=(0, 18))

        # ---- páginas
        host = tk.Frame(f, bg=BG)
        host.pack(side="top", fill="both", expand=True, padx=24, pady=14)
        for key in ("panel", "config", "auto", "hist", "log"):
            self.pages[key] = tk.Frame(host, bg=BG)
        self._build_panel(self.pages["panel"])
        self._build_config(self.pages["config"])
        self._build_auto(self.pages["auto"])
        self._build_hist(self.pages["hist"])
        self._build_log(self.pages["log"])
        self._page = None

    def _show_page(self, key):
        if key == self._page:
            return
        if self._page is not None:
            self.pages[self._page].pack_forget()       # solo se oculta la visible (antes se re-empacaban todas)
        self.pages[key].pack(fill="both", expand=True)
        self._page = key

    # ---- Panel
    def _build_panel(self, page):
        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="x")
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1, uniform="stat")
        self.card_pnl = StatCard(cards, "RESULTADO DE LA SESIÓN")
        self.card_rate = StatCard(cards, "EFECTIVIDAD")
        self.card_cap = StatCard(cards, "CAPITAL LIBRE")
        self.card_next = StatCard(cards, "PRÓXIMA SEÑAL")
        for i, c in enumerate((self.card_pnl, self.card_rate, self.card_cap, self.card_next)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 3 else 7))

        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True, pady=(14, 0))
        body.grid_columnconfigure(0, weight=3, uniform="b")
        body.grid_columnconfigure(1, weight=2, uniform="b")
        body.grid_rowconfigure(0, weight=3)
        body.grid_rowconfigure(1, weight=2)

        q = Card(body, "Cola de señales")
        q.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 7))
        fr, self.queue_tree = make_tree(q.body, "Par", [("hora", "Hora", 70, "center"), ("dir", "Dirección", 120, "center"),
                                                        ("estado", "Estado", 160, "w")], height=5)
        fr.pack(fill="both", expand=True)

        o = Card(body, "Operaciones abiertas")
        o.grid(row=1, column=0, sticky="nsew", padx=(0, 7), pady=(7, 0))
        fr, self.open_tree = make_tree(o.body, "Par", [("dir", "Dirección", 120, "center"),
                                                       ("monto", "Monto", 110, "center"),
                                                       ("resta", "Vence en", 110, "center")], height=3)
        fr.pack(fill="both", expand=True)

        p = Card(body, "Rendimiento por par")
        p.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(7, 0))
        fr, self.pairs_tree = make_tree(p.body, "Par", [("ops", "Ops", 44, "center"), ("rec", "G - P", 60, "center"),
                                                        ("pct", "Efect.", 60, "center"),
                                                        ("mg", "Gestión", 120, "center")], height=8, icon_w=140)
        fr.pack(fill="both", expand=True)
        tk.Label(p.body, text="Efectividad = ganadas ÷ (ganadas + perdidas) de esta sesión. «Gestión» muestra el estado "
                              "de cada par: IC paso 1 = la siguiente va por monto + ganancia; G1 = primer gale.", bg=CARD, fg=MUTED,
                 font=font(9), wraplength=330, justify="left", anchor="w").pack(fill="x", pady=(10, 0))
        self._refresh_stats()

    # ---- Configurar
    def _build_config(self, page):
        sf = ScrollFrame(page)
        sf.pack(fill="both", expand=True)
        page = sf.inner
        sig = Card(page, "Señales")
        sig.pack(fill="x")
        tools = tk.Frame(sig.head, bg=CARD)
        tools.pack(side="right")
        Btn(tools, "Limpiar", self._clear_signals, kind="ghost", width=90, height=30, size=9, bg=CARD).pack(
            side="right", padx=(8, 0))
        Btn(tools, "Pegar del portapapeles", self._paste_signals, kind="ghost", width=170, height=30, size=9,
            bg=CARD).pack(side="right")
        tf = tk.Frame(sig.body, bg=CARD)
        tf.pack(fill="x")
        self.signals_box = tk.Text(tf, height=6, wrap="word", bg=INPUT, fg=TEXT, insertbackground=TEXT, relief="flat",
                                   bd=0, padx=12, pady=10, font=(MONO, 10), highlightthickness=1,
                                   highlightbackground=BORDER, highlightcolor=ACCENT, selectbackground=GOLD_BG,
                                   selectforeground=ACCENT2)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.signals_box.yview, style="Dark.Vertical.TScrollbar")
        self.signals_box.configure(yscrollcommand=sb.set)
        self.signals_box.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self._preview_job = None
        self.signals_box.bind("<KeyRelease>", self._schedule_preview)
        self.signals_box.bind("<<Paste>>", lambda e: self.after(60, self._update_preview))
        self.preview = tk.Label(sig.body, text="", bg=CARD, fg=MUTED, font=font(10), anchor="w")
        self.preview.pack(fill="x", pady=(10, 0))
        self._update_preview()

        row = tk.Frame(page, bg=BG)
        row.pack(fill="both", expand=True, pady=(14, 0))
        for i in range(3):
            row.grid_columnconfigure(i, weight=1, uniform="cfg")
        c1 = Card(row, "Operación")
        c2 = Card(row, "Protecciones (0 = desactivado)")
        c3 = Card(row, "Martingala por par (señales)")
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        c2.grid(row=0, column=1, sticky="nsew", padx=7)
        c3.grid(row=0, column=2, sticky="nsew", padx=(7, 0))

        b = c1.body
        b.grid_columnconfigure(0, weight=1, uniform="o")
        b.grid_columnconfigure(1, weight=1, uniform="o")
        self.amount = Field(b, "Monto por operación ($)", "5")
        self.capital = Field(b, "Capital total a usar ($)", "50")
        self.expiry = ComboField(b, "Duración (minutos)", ["1", "2", "3", "4", "5"])
        self.offset = Field(b, "Ajuste de hora (horas)", "0")
        self.amount.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 10))
        self.capital.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 10))
        self.expiry.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 12))
        self.offset.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 12))
        self.otc = tk.BooleanVar(value=False)
        self.auto_otc = tk.BooleanVar(value=False)
        toggle_row(b, "Usar OTC (fines de semana)", self.otc).grid(row=2, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, 8))
        toggle_row(b, "Si el par está cerrado, probar con OTC", self.auto_otc).grid(row=3, column=0, columnspan=2,
                                                                                     sticky="w")
        tk.Label(b, text="Las señales se abren con la hora del servidor de Exnova y vencen justo al cierre de la vela "
                         "(nunca a 1:30). Si una señal llega más de 10 s tarde, se salta.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=300, justify="left", anchor="w").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        b = c2.body
        self.max_consec = Field(b, "Parar tras X pérdidas seguidas", "3")
        self.max_total = Field(b, "Parar tras X pérdidas en total", "0")
        self.profit_target = Field(b, "Parar al ganar ($)", "0")
        self.max_wins = Field(b, "Parar tras X ganadas", "0")
        for fld in (self.max_consec, self.max_total, self.max_wins, self.profit_target):
            fld.pack(fill="x", pady=(0, 10))
        tk.Label(b, text="El capital total también actúa de tope de pérdida. Las protecciones valen para las señales y "
                         "para el modo automático.", bg=CARD, fg=MUTED, font=font(9),
                 wraplength=300, justify="left", anchor="w").pack(fill="x")

        b = c3.body
        self.mg_on = tk.BooleanVar(value=False)
        toggle_row(b, "Activar martingala", self.mg_on).pack(anchor="w", pady=(0, 12))
        mrow = tk.Frame(b, bg=CARD)
        mrow.pack(fill="x", pady=(0, 10))
        mrow.grid_columnconfigure(0, weight=1, uniform="m")
        mrow.grid_columnconfigure(1, weight=1, uniform="m")
        self.mg_mult = Field(mrow, "Multiplicador", "2")
        self.mg_levels = Field(mrow, "Niveles máx.", "2")
        self.mg_mult.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.mg_levels.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.mg_when = ComboField(b, "¿Cuándo va el gale?", ["En la siguiente vela del MISMO par",
                                                            "En la próxima señal del MISMO par"])
        self.mg_when.pack(fill="x", pady=(0, 10))
        self.mg_confirm = ComboField(b, "Confirmación del gale (siguiente vela)",
                                     ["Solo si el análisis lo confirma", "Sin confirmar (gale clásico)"])
        self.mg_confirm.pack(fill="x", pady=(0, 10))
        self.mg_end = ComboField(b, "Al agotar niveles", ["Detener el bot", "Volver al monto base"])
        self.mg_end.pack(fill="x", pady=(0, 10))
        tk.Label(b, text="Cada par lleva su propio nivel: si pierde EUR/USD, el gale va en EUR/USD (misma dirección) y "
                         "no en la siguiente señal de otra divisa. «Solo si el análisis lo confirma» usa la estrategia "
                         "del backtest de ese par (pestaña Automático → Comparar); si el par no tiene backtest o el "
                         "análisis no confirma, no se hace el gale y el par vuelve al monto base. Nunca usa más que el "
                         "capital total.",
                 bg=CARD, fg=MUTED, font=font(9), wraplength=300, justify="left", anchor="w").pack(fill="x")

    def _schedule_preview(self, _e=None):
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(250, self._update_preview)

    def _update_preview(self, *_):
        self._preview_job = None
        txt = self.signals_box.get("1.0", "end")
        sigs = parse_signals(txt) if txt.strip() else []
        if not sigs:
            self.preview.config(text="Pega tus señales tal cual las recibes (ej. «EUR/USD 13:00 PM — COMPRAR / BUY»).",
                                fg=MUTED)
            return
        otc = sum(1 for s in sigs if s.otc)
        hrs = sorted((s.hour, s.minute) for s in sigs)
        self.preview.config(
            text=f"✓  {len(sigs)} señales leídas · {otc} OTC · de {hrs[0][0]:02d}:{hrs[0][1]:02d} "
                 f"a {hrs[-1][0]:02d}:{hrs[-1][1]:02d}", fg=GREEN)

    def _paste_signals(self):
        try:
            txt = self.clipboard_get()
        except tk.TclError:
            txt = ""
        if txt.strip():
            self.signals_box.delete("1.0", "end")
            self.signals_box.insert("1.0", txt)
        self._update_preview()

    def _clear_signals(self):
        self.signals_box.delete("1.0", "end")
        self._update_preview()

    # ---- Historial
    def _build_hist(self, page):
        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="x")
        for i in range(3):
            cards.grid_columnconfigure(i, weight=1, uniform="h")
        self.hc_ops = StatCard(cards, "OPERACIONES", height=100)
        self.hc_rate = StatCard(cards, "EFECTIVIDAD HISTÓRICA", height=100)
        self.hc_pnl = StatCard(cards, "RESULTADO ACUMULADO", height=100)
        for i, c in enumerate((self.hc_ops, self.hc_rate, self.hc_pnl)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
        card = Card(page, "Operaciones cerradas")
        card.pack(fill="both", expand=True, pady=(14, 0))
        tools = tk.Frame(card.head, bg=CARD)
        tools.pack(side="right")
        Btn(tools, f"Abrir {os.path.basename(HISTORY_FILE)}", self._open_history, kind="ghost", width=170,
            height=30, size=9, bg=CARD).pack()
        fr, self.hist_tree = make_tree(card.body, "Par", [("fecha", "Fecha", 95, "center"), ("hora", "Hora", 80, "center"),
                                                          ("dir", "Dirección", 110, "center"),
                                                          ("monto", "Monto", 80, "center"),
                                                          ("res", "Resultado", 130, "center"),
                                                          ("cuenta", "Cuenta", 80, "center")], height=9)
        fr.pack(fill="both", expand=True)
        self._refresh_hist_cards()

    def _open_history(self):
        if not os.path.exists(HISTORY_FILE):
            messagebox.showinfo("Historial", "Todavía no hay operaciones guardadas.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(HISTORY_FILE)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", HISTORY_FILE])
            else:
                subprocess.Popen(["xdg-open", HISTORY_FILE])
        except Exception as e:
            messagebox.showerror("Historial", f"No pude abrir el archivo: {e}")

    def _read_history_rows(self):
        rows = []
        try:
            with open(HISTORY_FILE, newline="", encoding="utf-8-sig") as fh:
                r = csv.reader(fh)
                next(r, None)
                for row in r:
                    if len(row) >= 5:
                        rows.append(row)
        except OSError:
            pass
        return rows

    def _insert_hist_row(self, fecha, hora, label, monto, profit, cuenta):
        pair, action = label_info(label)
        res, tag = fmt_result(profit)
        self.hist_tree.insert("", 0, text="  " + pretty_pair(pair), image=self.icons.pair(pair, 26),
                              values=(fecha, hora, dir_text(action), f"${float(monto):.2f}", res, cuenta),
                              tags=(tag,))
        kids = self.hist_tree.get_children()
        if len(kids) > 300:
            self.hist_tree.delete(*kids[300:])

    def _hist_add_total(self, cuenta, profit):
        t = self._hist_tot.setdefault(cuenta or "?", [0, 0, 0, 0.0])
        t[0] += 1
        if profit is not None:
            t[3] += profit
            t[1] += profit > 0
            t[2] += profit < 0

    def _load_history(self):
        """Lee historial.csv UNA vez al conectar; después los totales se actualizan en memoria (antes se releía el
        archivo entero tras cada operación)."""
        self.hist_tree.delete(*self.hist_tree.get_children())
        rows = self._read_history_rows()
        self._hist_tot = {}
        for row in rows:
            try:
                p = float(row[4]) if row[4] != "" else None
            except ValueError:
                p = None
            self._hist_add_total(row[5] if len(row) > 5 else "", p)
        for row in rows[-150:]:
            try:
                profit = float(row[4]) if row[4] != "" else None
                self._insert_hist_row(row[0], row[1], row[2], row[3], profit, row[5] if len(row) > 5 else "")
            except (ValueError, IndexError):
                continue
        self._refresh_hist_cards()

    def _refresh_hist_cards(self):
        acct = "REAL" if self.account_type == "REAL" else "DEMO"
        n = w = l = 0
        pnl = 0.0
        for cuenta, (tn, tw, tl, tp) in self._hist_tot.items():
            if cuenta not in (acct, "", "?"):
                continue
            n, w, l, pnl = n + tn, w + tw, l + tl, pnl + tp
        self.hc_ops.title = f"OPERACIONES · {acct}"
        self.hc_ops.set(str(n), f"{w} ganadas · {l} perdidas")
        if w + l:
            rate = w / (w + l)
            self.hc_rate.set(f"{rate * 100:.0f}%", f"sobre {w + l} operaciones cerradas", color=rate_color(rate),
                             ring=rate, ring_text="", ring_color=rate_color(rate))
        else:
            self.hc_rate.set("—", "sin operaciones todavía")
        self.hc_pnl.set(money(pnl, True), f"cuenta {acct}", color=GREEN if pnl > 0 else RED if pnl < 0 else TEXT)

    # ---- Registro
    def _build_log(self, page):
        card = Card(page, "Registro")
        card.pack(fill="both", expand=True)
        tools = tk.Frame(card.head, bg=CARD)
        tools.pack(side="right")
        Btn(tools, "Abrir registro.log", self._open_logfile, kind="ghost", width=150, height=30, size=9,
            bg=CARD).pack()
        fr = tk.Frame(card.body, bg=CARD)
        fr.pack(fill="both", expand=True)
        self.log_box = tk.Text(fr, wrap="word", state="disabled", bg=INPUT, fg=TEXT, relief="flat", bd=0, padx=14,
                               pady=12, font=(MONO, 10), highlightthickness=1, highlightbackground=BORDER,
                               selectbackground=ACCENT)
        sb = ttk.Scrollbar(fr, orient="vertical", command=self.log_box.yview, style="Dark.Vertical.TScrollbar")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("ok", GREEN), ("bad", RED), ("warn", AMBER), ("info", "#B9BEC8")):
            self.log_box.tag_configure(tag, foreground=color)

    def _open_logfile(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(LOG_FILE)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", LOG_FILE])
            else:
                subprocess.Popen(["xdg-open", LOG_FILE])
        except Exception as e:
            messagebox.showerror("Registro", f"No pude abrir el archivo: {e}")

    # ------------------------------------------------------------ reloj y cuentas regresivas
    def _tick(self):
        now = time.time()
        clock = f"Hora de tu PC · {datetime.now():%H:%M:%S}"
        if self.broker is not None:
            off = self.broker.clock_offset()
            if abs(off) >= 1.5:
                clock += f" · tu reloj va {abs(off):.0f} s {'atrasado' if off > 0 else 'adelantado'} (se usa el de Exnova)"
        self.clock_lbl.config(text=clock)
        running = bool(self.worker and self.worker.is_alive())
        srv = self.broker.now() if self.broker is not None else now

        ns = self.next_signal
        if ns:
            pair, action, hhmm, epoch = ns
            self.card_next.set(mmss(epoch - srv), f"{pretty_pair(pair)} · {dir_text(action)} · {hhmm}",
                               color=ACCENT2, icon=self.icons.pair(pair, 30))
        elif self._session_active() and self.auto_flag.is_set():
            v_, s_ = self._auto_next_card_text()
            self.card_next.set(v_, s_, color=ACCENT2)
        elif running:
            self.card_next.set("—", "No quedan más señales por esperar")
        else:
            self.card_next.set("—", "Inicia el bot para empezar")

        with self.lock:
            snap = {tid: dict(v) for tid, v in self.open_trades.items()}
        existing = set(self.open_tree.get_children())
        for tid, v in snap.items():
            iid = str(tid)
            left = v["end"] - srv if v.get("end") else None
            txt = "—" if left is None else (mmss(left) if left > 0 else "cerrando…")
            lvl = v.get("level") or 0
            vals = (dir_text(v["action"]), f"${v['amount']:.2f}" + (f" · G{lvl}" if lvl else ""), txt)
            if iid in existing:
                self.open_tree.item(iid, values=vals)
            else:
                self.open_tree.insert("", "end", iid=iid, text="  " + pretty_pair(v["pair"]),
                                      image=self.icons.pair(v["pair"], 26), values=vals, tags=("open",))
        for iid in existing - {str(t) for t in snap}:
            self.open_tree.delete(iid)
        if running and (self.stop_event.is_set() or (self.sess is not None and self.sess.over.is_set())):
            n = len(snap)
            word = "DETENIENDO" if self.stop_event.is_set() else "TERMINANDO"
            self.status_lbl.config(text=f"●  {word} · {n} operación(es) por cerrar" if n else f"●  {word}…", fg=AMBER)
        try:
            self._auto_tick(now)
        except Exception:
            pass
        self.after(500, self._tick)

    # ------------------------------------------------------------ iniciar / detener
    def _read_settings(self):
        def num(entry, name, minimum=0.0, integer=False):
            try:
                v = float(entry.get().replace(",", ".") or 0)
            except ValueError:
                raise ValueError(f"«{name}» debe ser un número.")
            if v < minimum:
                raise ValueError(f"«{name}» no puede ser menor que {minimum:g}.")
            if integer and v != int(v):
                raise ValueError(f"«{name}» debe ser un número entero.")
            return int(v) if integer else v

        amount = num(self.amount, "Monto por operación", 1)
        capital = num(self.capital, "Capital total")
        if capital < amount:
            raise ValueError("El capital total no puede ser menor que el monto por operación.")
        try:
            margin = float(self.auto_margin.get().replace(",", ".")) / 100.0
        except ValueError:
            margin = 0.02
        return dict(
            amount=amount, capital=capital, minutes=int(self.expiry.get()), otc=self.otc.get(),
            offset=num(self.offset, "Ajuste de hora", -24),
            max_consec=num(self.max_consec, "Pérdidas seguidas", 0, True),
            max_total=num(self.max_total, "Pérdidas en total", 0, True),
            target=num(self.profit_target, "Parar al ganar"),
            max_wins=num(self.max_wins, "Ganadas para parar", 0, True),
            auto_otc=self.auto_otc.get(),
            mg_on=self.mg_on.get(),
            mg_mult=num(self.mg_mult, "Multiplicador", 1.1) if self.mg_on.get() else 1.0,
            mg_levels=num(self.mg_levels, "Niveles máx.", 1, True) if self.mg_on.get() else 0,
            mg_stop=self.mg_end.current() == 0,
            mg_when="candle" if self.mg_when.current() == 0 else "signal",
            mg_confirm=self.mg_confirm.current() == 0,
            margin=margin,
        )

    def _start(self):
        if self._starting or (self.worker and self.worker.is_alive()):
            return
        try:
            cfg = self._read_settings()
        except ValueError as e:
            self.tabs.select("config")
            messagebox.showerror("Datos inválidos", str(e))
            return
        signals = parse_signals(self.signals_box.get("1.0", "end"))
        auto_on = self.auto_flag.is_set()
        if auto_on:
            try:
                self.auto_cfg = self._auto_cfg_read()
                self._auto_apply_mg(self.auto_cfg)
            except ValueError as e:
                self.tabs.select("auto")
                messagebox.showerror("Modo automático", str(e))
                return
        if not signals and not auto_on:
            self.tabs.select("config")
            messagebox.showerror("Sin señales", "No pude leer ninguna señal. Pega la lista en el cuadro "
                                                "o activa el modo automático (pestaña Automático).")
            return
        now = datetime.now()
        base = now.replace(second=0, microsecond=0)
        off = timedelta(hours=cfg["offset"])
        schedule = sorted(((base.replace(hour=s.hour, minute=s.minute) + off, s) for s in signals),
                          key=lambda x: x[0])
        times = [t for t, _ in schedule]
        if not auto_on and not [t for t in times if (now - t).total_seconds() <= MAX_LATE_SECONDS]:
            messagebox.showwarning(
                "Las señales ya pasaron",
                f"La hora de tu PC es {now:%H:%M:%S}.\n\n"
                f"Leí {len(signals)} señales, de {min(times):%H:%M} a {max(times):%H:%M}, y todas ya pasaron.\n\n"
                "Pega señales con horas que aún no lleguen. Si las horas de tu proveedor están en otra zona "
                "horaria, usa el campo «Ajuste de hora».")
            return
        # el saldo se lee en segundo plano: la ventana no se congela mientras Exnova responde
        self._starting = True
        self.start_btn.set_enabled(False)
        self.start_btn.set_text("Iniciando…")

        def work():
            try:
                with self.broker_lock:
                    bal = self.broker.balance()
            except Exception as e:
                msg = f"No pude leer tu saldo: {e}"
                self._ui(lambda: self._start_failed(msg))
                return
            self._ui(lambda: self._start_continue(cfg, schedule, signals, auto_on, bal))

        threading.Thread(target=work, daemon=True).start()

    def _start_failed(self, msg):
        self._starting = False
        self.start_btn.set_text("▶   Iniciar bot")
        self.start_btn.set_enabled(True)
        messagebox.showerror("Error", msg)

    def _start_continue(self, cfg, schedule, signals, auto_on, bal):
        self._starting = False
        self.start_btn.set_text("▶   Iniciar bot")
        self.start_btn.set_enabled(True)
        if cfg["capital"] > bal:
            messagebox.showerror("Capital insuficiente",
                                 f"Tu saldo es ${bal:,.2f}, menor que el capital que asignaste (${cfg['capital']:,.2f}).")
            return
        if cfg["mg_on"] and signals:
            steps = [round(cfg["amount"] * cfg["mg_mult"] ** k, 2) for k in range(cfg["mg_levels"] + 1)]
            worst = sum(steps)
            when = ("en la siguiente vela del mismo par" + (", solo si el análisis lo confirma" if cfg["mg_confirm"] else "")
                    if cfg["mg_when"] == "candle" else "en la próxima señal del mismo par")
            txt = (f"Martingala POR PAR activada (gale {when}): montos por nivel {', '.join(f'${x:g}' for x in steps)}.\n"
                   f"Perder {len(steps)} seguidas en UN par costaría ${worst:,.2f} (tu capital total es "
                   f"${cfg['capital']:,.2f}).")
            if worst > cfg["capital"]:
                txt += "\n\nOJO: el capital no alcanza para todos los niveles; el bot se detendrá antes."
            if not messagebox.askyesno("Martingala", txt + "\n\n¿Continuar?"):
                return
        if self.account_type == "REAL":
            lim = f"{cfg['max_consec']} pérdidas seguidas" if cfg["max_consec"] else "sin límite de pérdidas seguidas"
            if not messagebox.askyesno(
                    "Dinero REAL",
                    f"Vas a operar con dinero REAL.\n\n{len(signals)} señales · ${cfg['amount']:.2f} por operación · "
                    f"capital máximo ${cfg['capital']:.2f}\nParará tras: {lim}."
                    + ("\nModo automático ACTIVADO: operará solo." if auto_on else "") + "\n\n¿Continuar?"):
                return

        with self.lock:
            self.st = {"pnl": 0.0, "open": 0.0, "w": 0, "l": 0, "consec": 0}
            self.equity = []
            self.pair_stats = {}
            self.open_trades = {}
        self.auto_live = {}
        self.auto_entries = []
        self.auto_pause_until = 0.0
        self.mg_auto.reset()
        self.ic_auto.reset()
        self.mg_sig.configure(cfg["mg_mult"], cfg["mg_levels"], 1 if cfg["mg_when"] == "candle" else 10 ** 6, True,
                              "stop" if cfg["mg_stop"] else "reset")
        self.mg_sig.reset()
        self._sig_gales = set()
        self.cfg = cfg
        self.live_bal = bal
        self._set_account_label(self.account_type, bal)
        self._fill_queue(schedule, cfg)
        self._refresh_stats()
        self.stop_event.clear()
        self.sess = Session(cfg)
        self._set_running(True)
        self.tabs.select("panel")
        self.worker = threading.Thread(target=self._run, args=(self.sess, schedule, cfg), daemon=True, name="espyx-run")
        self.worker.start()
        if auto_on:
            self._auto_start_engine(self.sess)

    def _set_running(self, running):
        self.start_btn.set_enabled(not running)
        self.stop_btn.set_enabled(running)
        self.stop_btn.set_text("■   Detener")
        self.switch_btn.set_enabled(not running)
        self.status_lbl.config(text="●  OPERANDO" if running else "●  EN ESPERA", fg=ACCENT2 if running else MUTED)

    def _stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self.stop_btn.set_enabled(False)
        self.stop_btn.set_text("Deteniendo…")
        with self.lock:
            n = len(self.open_trades)
        self.status_lbl.config(text=f"●  DETENIENDO · {n} operación(es) por cerrar" if n else "●  DETENIENDO…", fg=AMBER)
        self._log("Deteniendo… no se abren más operaciones" + (f"; las {n} abiertas siguen hasta cerrar." if n else "."))

    def _finish_ui(self):
        self._set_running(False)
        self.next_signal = None

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Salir", "El bot sigue en marcha. ¿Cerrar de todos modos?"):
                return
        self.stop_event.set()
        try:
            self.pool.shutdown()
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------ cola de señales
    def _fill_queue(self, schedule, cfg):
        self.queue_tree.delete(*self.queue_tree.get_children())
        now = datetime.now()
        for i, (t, s) in enumerate(schedule):
            pair = s.pair + ("-OTC" if (s.otc or cfg["otc"]) else "")
            late = (now - t).total_seconds() > MAX_LATE_SECONDS
            self.queue_tree.insert("", "end", iid=str(i), text="  " + pretty_pair(pair),
                                   image=self.icons.pair(pair, 26),
                                   values=(f"{t:%H:%M}", dir_text(s.action), "Ya pasó" if late else "Pendiente"),
                                   tags=("skip" if late else "pend",))

    def _set_status(self, idx, text, tag):
        if idx is None:
            return

        def apply():
            iid = str(idx)
            if self.queue_tree.exists(iid):
                self.queue_tree.set(iid, "estado", text)
                self.queue_tree.item(iid, tags=(tag,))
                if tag == "next":
                    self.queue_tree.see(iid)
        self._ui(apply)

    # ------------------------------------------------------------ estadísticas, saldo, historial
    def _mg_text(self, pair):
        for mg in (self.mg_auto, self.mg_sig):
            st = mg.pending(pair)
            if st:
                return f"G{st['level']} " + ("en curso" if st.get("opened") else "pendiente")
        st = self.ic_auto.snapshot().get(pair)
        if st:
            return f"IC paso {st['step']} · ${st['amount']:,.2f}"
        return "—"

    def _refresh_stats(self):
        self._ui_once("stats", self._apply_stats)

    def _apply_stats(self):
        with self.lock:
            st = dict(self.st)
            pairs = {k: dict(v) for k, v in self.pair_stats.items()}
        for mg in (self.mg_auto, self.mg_sig, self.ic_auto):
            for mkt in mg.snapshot():
                pairs.setdefault(mkt, {"n": 0, "w": 0, "l": 0})
        cap = self.cfg.get("capital", 0)
        free = cap + st["pnl"] - st["open"]
        total = st["w"] + st["l"]
        rate = (st["w"] / total) if total else None
        pc = GREEN if st["pnl"] > 0 else RED if st["pnl"] < 0 else TEXT
        with self.lock:
            eq = [0.0] + self.equity[-80:]
        self.card_pnl.set(money(st["pnl"], True), f"Pérdidas seguidas: {st['consec']}", color=pc,
                          spark=eq if len(eq) > 1 else None)
        if rate is None:
            self.card_rate.set("0 ops", "sin operaciones cerradas", ring=None, ring_text="—")
        else:
            self.card_rate.set(f"{total} ops", f"{st['w']} ganadas · {st['l']} perdidas", ring=rate,
                               ring_text=f"{rate * 100:.0f}%", ring_color=rate_color(rate))
        self.card_cap.set(f"${free:,.2f}", f"de ${cap:,.2f} asignados · en juego ${st['open']:,.2f}")
        self.pairs_tree.delete(*self.pairs_tree.get_children())
        for pair, d in sorted(pairs.items(), key=lambda kv: -kv[1]["n"]):
            cl = d["w"] + d["l"]
            r = d["w"] / cl if cl else None
            tag = "muted" if r is None else "win" if r >= 0.55 else "skip" if r >= 0.45 else "loss"
            self.pairs_tree.insert("", "end", text="  " + pretty_pair(pair), image=self.icons.pair(pair, 26),
                                   values=(d["n"], f"{d['w']} - {d['l']}", "—" if r is None else f"{r * 100:.0f}%",
                                           self._mg_text(pair)),
                                   tags=(tag,))

    def _show_balance(self, bal):
        self.live_bal = bal
        self._ui_once("balance", lambda: self._set_account_label(self.account_type, bal))

    def _refresh_balance(self, fallback_delta=None):
        """Lee el saldo real de Exnova; si falla, lo estima con el resultado de la operación."""
        try:
            with self.broker_lock:
                bal = self.broker.balance()
        except Exception:
            if fallback_delta is None:
                return
            bal = self.live_bal + fallback_delta
        self._show_balance(bal)

    def _add_history(self, label, amount, profit):
        now = datetime.now()
        cuenta = "REAL" if self.account_type == "REAL" else "DEMO"
        self._ui(lambda: self._insert_hist_row(f"{now:%Y-%m-%d}", f"{now:%H:%M:%S}", label, amount, profit, cuenta))
        try:
            new = not os.path.exists(HISTORY_FILE)
            with open(HISTORY_FILE, "a", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["fecha", "hora", "operacion", "monto", "resultado", "cuenta"])
                w.writerow([f"{now:%Y-%m-%d}", f"{now:%H:%M:%S}", label, f"{amount:.2f}",
                            "" if profit is None else f"{profit:.2f}", cuenta])
        except Exception:
            pass

        def tot():
            self._hist_add_total(cuenta, profit)
            self._refresh_hist_cards()
        self._ui(tot)

    def _limit_reason(self, cfg):
        with self.lock:
            st = dict(self.st)
        if cfg["max_consec"] and st["consec"] >= cfg["max_consec"]:
            return f"{st['consec']} pérdidas seguidas"
        if cfg["max_total"] and st["l"] >= cfg["max_total"]:
            return f"{st['l']} pérdidas en total"
        if cfg.get("max_wins") and st["w"] >= cfg["max_wins"]:
            return f"{st['w']} operaciones ganadas (meta de la sesión)"
        if cfg["target"] and st["pnl"] >= cfg["target"]:
            return f"meta de ganancia alcanzada (+${st['pnl']:.2f})"
        if cfg["mg_on"] and cfg["mg_stop"] and self.mg_sig.exhausted:
            return f"se agotaron los niveles de martingala en {pretty_pair(self.mg_sig.exhausted[-1])}"
        free = cfg["capital"] + st["pnl"] - st["open"]
        if free < cfg["amount"]:
            return f"el capital libre (${free:.2f}) no alcanza para otra entrada (${cfg['amount']:.2f})"
        return None

    # ------------------------------------------------------------ diagnóstico de pares
    KIND_LABEL = {"turbo": "1–5 min", "binary": "5+ min", "blitz": "Blitz"}

    def _fmt_pair(self, name, st):
        if not st:
            return f"{name}: no aparece en las binarias de Exnova"
        parts = []
        for kind in ("turbo", "binary", "blitz"):
            k = st.get(kind)
            if not k:
                continue
            estado = "ABIERTO" if k["open"] else ("SUSPENDIDO" if k["suspended"] else "CERRADO")
            pago = f" pago {k['payout']}%" if k["open"] and k["payout"] else ""
            parts.append(f"{self.KIND_LABEL[kind]} {estado}{pago}")
        return f"{name}: " + " · ".join(parts)

    @staticmethod
    def _is_open(st):
        return any(k.get("open") for k in (st or {}).values())

    def _write_dump(self, pairs, table):
        """Guarda en diagnostico_activos.txt lo que Exnova dice de cada par (por si hay que revisarlo)."""
        try:
            with open(DUMP_FILE, "w", encoding="utf-8") as fh:
                fh.write(f"Estado de pares según Exnova — {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
                for p in pairs:
                    fh.write(self._fmt_pair(p, (table or {}).get(p)) + "\n")
                    for kind, k in ((table or {}).get(p) or {}).items():
                        fh.write(f"    {kind}: {k}\n")
                fixes = getattr(self.broker, "id_fixes", [])
                fh.write(f"\nIds corregidos respecto a la librería (nombre, antes, ahora): {fixes}\n")
        except Exception:
            pass

    def _diagnose(self, pair, cfg):
        """Tras un rechazo: consulta a Exnova el par y su gemelo (normal <-> OTC) en las dos modalidades de binarias.
        Devuelve True si conviene reintentar con el gemelo OTC."""
        is_otc = pair.endswith("-OTC")
        twin = pair[:-4] if is_otc else pair + "-OTC"
        if not hasattr(self.broker, "sync_assets"):
            return False
        try:
            with self.broker_lock:
                table = self.broker.sync_assets()
        except Exception as e:
            self._log(f"   No pude consultar el estado del par: {e}")
            return False
        if table is None:
            self._log("   Exnova no respondió el estado de los pares (intenta de nuevo).")
            return False
        self._write_dump([pair, twin], table)
        a, b = table.get(pair), table.get(twin)
        self._log("   Diagnóstico → " + self._fmt_pair(pair, a) + " | " + self._fmt_pair(twin, b))
        if self._is_open(a):
            self._log(f"   {pair} figura ABIERTO, así que el rechazo no fue por horario. "
                      "Revisa «diagnostico_activos.txt» (se creó en la carpeta del bot) y envíamelo si se repite.")
            return False
        if not is_otc and self._is_open(b):
            extra = "" if cfg.get("auto_otc") else " Puedes marcar «Si el par está cerrado, probar con OTC»."
            self._log(f"   {pair} figura CERRADO pero {twin} está abierto.{extra}")
            return True
        self._log(f"   Ni {pair} ni {twin} figuran abiertos en este momento.")
        return False

    def _verify_pairs(self):
        """Botón «Verificar pares»: muestra el estado de los pares pegados, sin operar."""
        if self.broker is None:
            return
        signals = parse_signals(self.signals_box.get("1.0", "end"))
        pairs = sorted({s.pair + ("-OTC" if (s.otc or self.otc.get()) else "") for s in signals}) or ["EURUSD"]
        self.verify_btn.set_enabled(False)
        self.tabs.select("log")

        def work():
            try:
                with self.broker_lock:
                    table = self.broker.sync_assets()
            except Exception as e:
                self._log(f"No pude consultar los pares: {e}")
                table = None
            if table is None:
                self._log("Exnova no respondió el estado de los pares. Intenta de nuevo.")
            else:
                self._write_dump(pairs, table)
                self._log("Estado de los pares (el bot nunca opera digitales):")
                for p in pairs:
                    self._log("   " + self._fmt_pair(p, table.get(p)))
            self._ui(lambda: self.verify_btn.set_enabled(True))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ martingala por par de las señales
    def _confirm_with_analysis(self, pair, action, close, minutes, margin):
        """¿El análisis del backtest de este par (binarias) da la misma señal al cierre `close`? (ok, motivo)"""
        a = {"mode": "best", "kind": "binary", "dur_mode": "fixed", "need_good": False, "health_on": False,
             "margin": margin, "trend_mode": self.auto_cfg.get("trend_mode", "on") if self.auto_cfg else "on"}
        plan, why = self._auto_plan(pair, a, {"minutes": minutes})
        if not plan:
            if not self._res["binary"].get(pair):
                return False, "el par no tiene backtest para confirmar (pulsa «Comparar estrategias» en Automático)"
            return False, f"no hay estrategia para confirmar ({why})"
        try:
            candles = self._auto_candles(pair, 60, self._auto_need(plan), close)
        except Exception as e:
            return False, f"no pude leer las velas ({e})"
        if not candles or candles[-1]["t"] != close - 60:
            return False, "Exnova no entregó la vela que acaba de cerrar"
        key, p = plan["key"], plan["params"]
        model = self._auto_model(pair, key, p, plan["minutes"], plan["sub"], "binary") if key in strat.MODEL_KEYS else None
        pat = (self._auto_model(pair, "pattern", strat.Params(), plan["minutes"], None, "binary")
               if "pat" in plan["flags"] else None)
        pay = self.broker.payout(pair, "turbo")
        act, why, _ = strat.live_decision(candles, key, p, model, (pay or 85) / 100.0, plan["flags"], pat,
                                          minutes=plan["minutes"])
        name = strat.SHORT.get(key, key)
        if act != action:
            return False, f"{name} no lo confirma ({why})"
        return True, f"{name} confirma ({why})"

    def _sig_gale(self, sess, pair, meta, level, cfg):
        """Abre el gale de una señal en la vela siguiente a la que perdió (mismo par y dirección).
        Devuelve (id, etiqueta, monto) o None."""
        close = meta.get("end")
        action = meta["action"]
        if not close:
            return None
        now = self.broker.now()
        why_skip = None
        if self.stop_event.is_set():
            why_skip = "el bot se está deteniendo"
        elif now - close > MAX_LATE_SECONDS:
            why_skip = f"el resultado llegó {now - close:.0f} s después del cierre (máximo {MAX_LATE_SECONDS} s)"
        else:
            lim = self._limit_reason(cfg)
            if lim:
                why_skip = lim
        if why_skip is None and cfg["mg_confirm"]:
            ok, why = self._confirm_with_analysis(pair, action, close, cfg["minutes"], cfg.get("margin", 0.02))
            if not ok:
                why_skip = why
            else:
                self._log(f"   Gale {pretty_pair(pair)}: {why}")
        amount = self.mg_sig.amount(cfg["amount"], level)
        tok = None
        if why_skip is None:
            tok, why = self._reserve(cfg, amount, pair)
            if tok is None:
                why_skip = why
        if why_skip is not None:
            self.mg_sig.on_bar_without_entry(pair)
            self._log(f"Martingala {pretty_pair(pair)}: no se hace el gale nivel {level} — {why_skip}. "
                      f"El par vuelve al monto base.")
            self._ui_once("stats", self._apply_stats)
            return None
        end = close + cfg["minutes"] * 60
        word = "COMPRAR" if action == "call" else "VENDER"
        label = f"{pair.replace('-OTC', '')}{' (OTC)' if pair.endswith('-OTC') else ''} " \
                f"{datetime.fromtimestamp(close):%H:%M} {word} [gale {level}]"
        try:
            ok, tid = self.broker.open_trade(amount, pair, action, cfg["minutes"], expiry=end,
                                             deadline=close + MAX_LATE_SECONDS,
                                             tag={"label": label, "pair": pair, "action": action, "amount": amount,
                                                  "end": end, "src": "signal", "minutes": cfg["minutes"], "cfg": cfg})
        except Exception as e:
            ok, tid = False, str(e)
        if not ok:
            self._release(tok)
            self.mg_sig.on_bar_without_entry(pair)
            self._log(f"Martingala {pretty_pair(pair)}: no se abrió el gale nivel {level} ({tid}). Vuelve al monto base.")
            return None
        self._commit(tok, tid, {"label": label, "pair": pair, "action": action, "amount": amount, "end": end,
                                "src": "signal", "level": level, "done": threading.Event()})
        self.mg_sig.on_opened(pair)
        self._sig_gales.add((pair, int(close)))
        self._log(f"✔ Abierto GALE {level} de {pretty_pair(pair)} por ${amount:.2f} · vence "
                  f"{datetime.fromtimestamp(end):%H:%M:%S} (entrada en el segundo {self.broker.now() - close:.1f})")
        self._show_balance(self.live_bal - amount)
        self._refresh_stats()
        return tid, label, amount

    # ------------------------------------------------------------ reservas (capital, cupos y un par a la vez)
    def _reserve(self, cfg, amount, market, max_open=None, one_per_market=True):
        """Aparta capital y cupo ANTES de comprar. Varios hilos (señales, gales, modo automático) pueden querer abrir en
        el mismo segundo: así nunca se pasa del capital ni del máximo de operaciones. Devuelve (ficha|None, motivo)."""
        with self.lock:
            free = cfg["capital"] + self.st["pnl"] - self.st["open"]
            if amount > free + 1e-9:
                return None, f"el capital libre (${free:.2f}) no alcanza para ${amount:.2f}"
            if one_per_market and (any(v.get("pair") == market for v in self.open_trades.values())
                                   or any(r["market"] == market for r in self._reserving.values())):
                return None, f"ya hay una operación abierta en {pretty_pair(market)}"
            n = len(self.open_trades) + len(self._reserving)
            if max_open is not None and n >= max_open:
                return None, f"{n} operación(es) abierta(s) (máximo {max_open})"
            tok = next(self._tok)
            self._reserving[tok] = {"amount": amount, "market": market}
            self.st["open"] += amount
            return tok, ""

    def _commit(self, tok, tid, meta):
        """La compra salió bien: la reserva pasa a ser una operación abierta."""
        with self.lock:
            self._reserving.pop(tok, None)
            self.open_trades[tid] = meta

    def _release(self, tok):
        """La compra no salió: se devuelve lo apartado."""
        with self.lock:
            r = self._reserving.pop(tok, None)
            if r:
                self.st["open"] -= r["amount"]

    def _late_order(self, tag, tid):
        """Exnova confirmó TARDE una compra que el bot había dado por fallida (no respondió en 5 s): se registra y se
        sigue como las demás (cuenta en el capital, las estadísticas y el historial), pero no mueve la martingala."""
        if not isinstance(tag, dict) or tid is None:
            return
        amount = float(tag.get("amount") or 0)
        with self.lock:
            if tid in self.open_trades:
                return
            meta = {k: tag.get(k) for k in ("label", "pair", "action", "amount", "end", "strategy", "minutes", "payout",
                                            "kind", "src")}
            if not meta.get("end"):
                meta["end"] = (self.broker.now() if self.broker else time.time()) + float(tag.get("secs") or 60)
            meta.update(level=0, mg=False, late=True, done=threading.Event())
            meta["label"] = (meta.get("label") or "operación") + " [confirmada tarde]"
            self.open_trades[tid] = meta
            self.st["open"] += amount
        self._log(f"⚠ Exnova confirmó TARDE {meta['label']} (id {tid}): se registra y se sigue su resultado; no mueve la "
                  f"martingala.")
        cfg = dict(tag.get("cfg") or self.cfg or {}, minutes=max(1, int(meta.get("minutes") or 1)))
        th = threading.Thread(target=self._track, args=(tid, meta["label"], amount, meta.get("pair"), None, cfg),
                              daemon=True)
        th.start()
        sess = self.sess
        if sess is not None:
            sess.trackers.append(th)
        self._refresh_stats()

    def _session_active(self):
        sess = self.sess
        return bool(self.worker and self.worker.is_alive() and sess is not None and not sess.over.is_set())

    # ------------------------------------------------------------ motor
    def _track(self, tid, label, amount, pair, idx, cfg):
        """Espera el resultado de una operación, actualiza estadísticas, saldo, historial y la martingala de ESE par.
        Si toca gale en la siguiente vela (señales), lo abre aquí mismo y lo sigue."""
        sess = self.sess
        while True:
            profit, err = None, None
            with self.lock:
                meta0 = dict(self.open_trades.get(tid) or {})
            end = meta0.get("end") or (time.time() + cfg["minutes"] * 60)
            try:
                wait = max(30.0, end - (self.broker.now() if self.broker else time.time())) + 120
                profit = self.broker.result(tid, wait)
            except Exception as e:
                err = e
            with self.lock:
                meta = dict(self.open_trades.get(tid) or meta0)
                strategy = meta.get("strategy")
                self.open_trades.pop(tid, None)
                self.st["open"] -= amount
                if profit is not None:
                    ps = self.pair_stats.setdefault(pair, {"n": 0, "w": 0, "l": 0})
                    ps["n"] += 1
                    self.st["pnl"] += profit
                    self.equity.append(round(self.st["pnl"], 2))
                    if profit > 0:
                        self.st["w"] += 1
                        self.st["consec"] = 0
                        ps["w"] += 1
                    elif profit < 0:
                        self.st["l"] += 1
                        self.st["consec"] += 1
                        ps["l"] += 1
            if profit is None:
                self._log(f"{label}: no pude confirmar el resultado ({err}). Revísalo en Exnova.")
            else:
                res = "GANÓ" if profit > 0 else "PERDIÓ" if profit < 0 else "EMPATE"
                self._log(f"{label}: {res} {profit:+.2f}")
            text, tag = fmt_result(profit)
            self._set_status(idx, text, tag)
            self._add_history(label, amount, profit)
            nxt = None
            try:
                if meta.get("src") == "auto":
                    self._auto_after_trade(strategy, pair, profit, meta)
                elif cfg.get("mg_on") and not meta.get("late"):
                    lvl = meta.get("level", 0)
                    ev, nl = self.mg_sig.on_result(pair, lvl, profit, meta.get("action"))
                    if ev == "gale":
                        self._log(f"Martingala {pretty_pair(pair)}: perdió en nivel {lvl} → gale nivel {nl}"
                                  + (" en la siguiente vela." if cfg.get("mg_when") == "candle"
                                     else " en la próxima señal de este par."))
                    elif ev == "win" and lvl:
                        self._log(f"Martingala {pretty_pair(pair)}: ciclo GANADO en el gale nivel {lvl}.")
                    elif ev == "exhausted":
                        self._log(f"Martingala {pretty_pair(pair)}: se agotaron los niveles (ciclo perdido)."
                                  + (" El bot se detendrá." if cfg.get("mg_stop") else " El par vuelve al monto base."))
                    elif ev == "unknown":
                        self._log(f"Martingala {pretty_pair(pair)}: el resultado no se confirmó; por seguridad el ciclo se "
                                  f"cierra y el par vuelve al monto base (revisa la operación en Exnova).")
                    if ev in ("gale", "tie") and cfg.get("mg_when") == "candle" and nl > 0 and profit is not None:
                        nxt = self._sig_gale(sess, pair, meta, nl, cfg)
            except Exception as e:
                self._log(f"Error al procesar el resultado de {label}: {e}")
                self._log_file(traceback.format_exc())
            finally:
                ev_done = meta.get("done")
                if ev_done is not None:
                    ev_done.set()
            self._refresh_stats()
            threading.Thread(target=self._refresh_balance, args=(None if profit is None else amount + profit,),
                             daemon=True).start()
            if not nxt:
                return
            tid, label, amount = nxt
            idx = None

    def _run(self, sess, schedule, cfg):
        stop_reason = None
        broker = self.broker
        n_pend = sum(1 for t, _ in schedule if broker.now() - t.timestamp() <= MAX_LATE_SECONDS)
        self._log(f"Hora de tu PC: {datetime.now():%H:%M:%S}. {len(schedule)} señales ({n_pend} por ejecutar, "
                  f"{len(schedule) - n_pend} ya pasadas). Monto ${cfg['amount']:.2f}, capital ${cfg['capital']:.2f}"
                  + (f", martingala por par ×{cfg['mg_mult']:g} hasta nivel {cfg['mg_levels']}." if cfg["mg_on"] else ".")
                  + (f" Modo automático: {self._auto_mode_name(self.auto_cfg.get('mode'))} en "
                     f"{', '.join(self.auto_cfg.get('pairs', []))}."
                     if self.auto_flag.is_set() else ""))
        for _, s in schedule:
            self._log(f"   leída: {s.label()}")
        self._refresh_stats()

        for idx, (t, s) in enumerate(schedule):
            if self.stop_event.is_set() or stop_reason:
                break
            epoch = t.timestamp()                      # inicio exacto del minuto de la señal
            if broker.now() - epoch > MAX_LATE_SECONDS:
                self._log(f"{s.label()} — ya pasó, la salto.")
                self._set_status(idx, "Ya pasó", "skip")
                continue

            pair = s.pair + ("-OTC" if (s.otc or cfg["otc"]) else "")
            self.next_signal = (pair, s.action, f"{s.hour:02d}:{s.minute:02d}", epoch)
            self._set_status(idx, "En espera", "next")
            while not self.stop_event.is_set() and epoch - broker.now() > 0:
                stop_reason = self._limit_reason(cfg)
                if stop_reason:
                    break
                self.stop_event.wait(min(0.2, max(0.0, epoch - broker.now())))
            self.next_signal = None
            if self.stop_event.is_set():
                self._set_status(idx, "Cancelada", "skip")
                break
            stop_reason = stop_reason or self._limit_reason(cfg)
            if stop_reason:
                self._set_status(idx, "Cancelada", "skip")
                break
            if cfg["mg_on"]:
                # el resultado de la operación anterior de ESTE par (si vence ahora) decide si toca gale y con qué monto
                with self.lock:
                    evs = [v["done"] for v in self.open_trades.values() if v.get("pair") == pair and v.get("done")
                           is not None and v.get("end") and v["end"] <= epoch + 0.5]
                for ev in evs:
                    ev.wait(max(0.0, epoch + MAX_LATE_SECONDS - 3 - broker.now()))
                if (pair, int(epoch)) in self._sig_gales or (cfg["mg_when"] == "candle" and self.mg_sig.pending(pair)):
                    self._log(f"{s.label()} — en esta vela va el gale de {pretty_pair(pair)}; la señal no se duplica.")
                    self._set_status(idx, "Omitida (gale)", "skip")
                    continue

            level = 0
            amount = cfg["amount"]
            if cfg["mg_on"] and cfg["mg_when"] == "signal":
                amount, level = self.mg_sig.stake(cfg["amount"], pair)
            tok, why = self._reserve(cfg, amount, pair, one_per_market=cfg["mg_on"])
            if tok is None:
                if "capital" in why:                # nunca usa más que el capital total (como en la v12: se detiene)
                    stop_reason = f"{why} (señal {s.label()})"
                    self._set_status(idx, "Cancelada", "skip")
                    break
                self._log(f"{s.label()} — no se abre: {why} (con martingala por par, una operación por par a la vez).")
                self._set_status(idx, "Omitida", "skip")
                continue
            label = s.label() + (f" [gale {level}]" if level else "")
            end = epoch + cfg["minutes"] * 60          # vence al cierre exacto de la vela
            tag = {"label": label, "pair": pair, "action": s.action, "amount": amount, "end": end, "src": "signal",
                   "minutes": cfg["minutes"], "cfg": cfg}
            try:
                ok, tid = broker.open_trade(amount, pair, s.action, cfg["minutes"], expiry=end,
                                            deadline=epoch + MAX_LATE_SECONDS, tag=tag)
            except Exception as e:
                self._release(tok)
                self._log(f"No se pudo abrir {label}: {e}")
                self._set_status(idx, "Error", "loss")
                continue
            if not ok:
                self._log(f"Exnova rechazó {label}: {tid}")
                self._set_status(idx, "Rechazada", "loss")
                retry_otc = broker._no_disponible(tid) and self._diagnose(pair, cfg)
                if retry_otc and cfg["auto_otc"]:
                    pair = pair + "-OTC"
                    label += " [OTC auto]"
                    self._log(f"   Reintentando con {pair}…")
                    try:
                        ok, tid = broker.open_trade(amount, pair, s.action, cfg["minutes"], expiry=end,
                                                    deadline=epoch + MAX_LATE_SECONDS, tag=dict(tag, pair=pair, label=label))
                    except Exception as e:
                        self._release(tok)
                        self._log(f"No se pudo abrir {label}: {e}")
                        self._set_status(idx, "Error", "loss")
                        continue
                    if not ok:
                        self._release(tok)
                        self._log(f"Exnova rechazó también {label}: {tid}")
                        continue
                else:
                    self._release(tok)
                    continue
            self._commit(tok, tid, {"label": label, "pair": pair, "action": s.action, "amount": amount,
                                    "end": end, "src": "signal", "level": level, "done": threading.Event()})
            if level:
                self.mg_sig.on_opened(pair)
            if getattr(broker, "last_open_kind", "turbo") == "binary":
                label += " [binaria]"
            nivel = f", martingala nivel {level}" if level else ""
            self._log(f"✔ Abierta {label} por ${amount:.2f}{nivel} (vence {datetime.fromtimestamp(end):%H:%M:%S}, "
                      f"entrada en el segundo {broker.now() - epoch:.1f})")
            self._set_status(idx, "Abierta", "open")
            self._show_balance(self.live_bal - amount)
            self._refresh_stats()
            th = threading.Thread(target=self._track, args=(tid, label, amount, pair, idx, cfg), daemon=True)
            th.start()
            sess.trackers.append(th)

        # modo automático: sigue mientras esté activado (el análisis corre en su propio hilo)
        while not self.stop_event.is_set() and not stop_reason and self.auto_flag.is_set():
            stop_reason = self._limit_reason(cfg)
            if stop_reason:
                break
            if not (self._auto_thread and self._auto_thread.is_alive()):
                self._auto_start_engine(sess)
            self.stop_event.wait(0.2)

        self.next_signal = None
        if stop_reason:
            self._log(f"DETENIDO: {stop_reason}.")
            self.stop_event.set()                      # que el modo automático tampoco abra nada más
        if self.stop_event.is_set() and not stop_reason:
            stop_reason = "lo detuviste tú"
        sess.stop_reason = stop_reason
        sess.over.set()
        if self._auto_thread:
            self._auto_thread.join(timeout=15)        # primero el motor: así no queda ninguna operación sin esperar
        while True:
            alive = [th for th in list(sess.trackers) if th.is_alive()]
            if not alive:
                break
            for th in alive:
                th.join()
        with self.lock:
            st = dict(self.st)
        self._log(f"Fin. Resultado: ${st['pnl']:+.2f} · Ganadas {st['w']} · Perdidas {st['l']}")
        self._refresh_stats()
        self._ui(self._finish_ui)
        if stop_reason and not stop_reason.startswith("lo detuviste"):
            def alert():
                self.bell()
                messagebox.showinfo("Bot detenido",
                                    f"El bot se detuvo: {stop_reason}.\n\nResultado: ${st['pnl']:+.2f} "
                                    f"(ganadas {st['w']}, perdidas {st['l']}).")
            self._ui(alert)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    App().mainloop()
