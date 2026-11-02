"""EspyX Bot — tema oscuro y componentes visuales propios (solo tkinter, sin librerías extra)."""
import hashlib
import os
import sys
import re
import time
import tkinter as tk
from tkinter import ttk

import espyx_assets as A

# ------------------------------------------------------------------ paleta «terminal profesional» (v15)
# Negro grafito, un solo acento dorado; verde y rojo SOLO para ganar/perder; naranja para OTC y avisos.
BG = "#0B0C0F"
PANEL = "#0F1115"
CARD = "#13161B"
CARD2 = "#1A1E25"
INPUT = "#0D0F13"
BORDER = "#252A33"
TEXT = "#E7E9ED"
MUTED = "#7E8591"
DIM = "#3A404B"
ACCENT = "#E8B04A"        # dorado
ACCENT2 = "#F2C771"       # dorado claro (información, operaciones abiertas)
GOLD_BG = "#2A2314"       # fondo dorado muy oscuro (selección)
GREEN = "#2FD480"
RED = "#FF5C6C"
AMBER = "#F08A24"         # naranja: OTC y avisos (distinto del dorado)
ON_ACCENT = "#14110A"     # texto sobre el dorado

if sys.platform.startswith("win"):
    FONT, MONO = "Segoe UI", "Consolas"
elif sys.platform == "darwin":
    FONT, MONO = "Helvetica Neue", "Menlo"
else:
    FONT, MONO = "DejaVu Sans", "DejaVu Sans Mono"
_MONO_PREF = ("Cascadia Mono", "JetBrains Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono")


def font(size, bold=False):
    return (FONT, size, "bold") if bold else (FONT, size)


def mono(size, bold=False):
    """Fuente monoespaciada para cifras (los números no «bailan» al cambiar)."""
    return (MONO, size, "bold") if bold else (MONO, size)


# ------------------------------------------------------------------ motor de animación (uno solo para toda la ventana)
def ease(t):
    return 1 - (1 - t) ** 3


def mixc(c1, c2, t):
    """Mezcla dos colores «#rrggbb»."""
    return A.rgb_hex(A.mix(A.hex_rgb(c1), A.hex_rgb(c2), max(0.0, min(1.0, t))))


class Anim:
    """Un único reloj (~30 cuadros/s) que SOLO corre mientras hay algo animándose. Cada animación cambia propiedades de
    figuras ya dibujadas (posición, color, texto); nunca redibuja la ventana. Con la ventana minimizada salta al final."""
    root = None
    _tw = {}
    _job = None
    FRAME = 33

    @classmethod
    def init(cls, root):
        cls.root = root

    @classmethod
    def tween(cls, key, ms, fn, done=None):
        """fn(p) con p de 0 a 1 (ya suavizado). Una animación nueva con la misma clave reemplaza a la anterior."""
        if cls.root is None:
            fn(1.0)
            if done:
                done()
            return
        cls._tw[key] = [time.perf_counter(), ms / 1000.0, fn, done]
        if cls._job is None:
            cls._job = cls.root.after(cls.FRAME, cls._step)

    @classmethod
    def cancel(cls, key):
        cls._tw.pop(key, None)

    @classmethod
    def _step(cls):
        cls._job = None
        now = time.perf_counter()
        try:
            hidden = cls.root.state() == "iconic"
        except Exception:
            hidden = True
        for key, (t0, dur, fn, done) in list(cls._tw.items()):
            p = 1.0 if (hidden or dur <= 0) else min(1.0, (now - t0) / dur)
            try:
                fn(ease(p))
            except Exception:
                p = 1.0
            if p >= 1.0:
                if cls._tw.get(key, [None])[2] is fn:
                    cls._tw.pop(key, None)
                if done:
                    try:
                        done()
                    except Exception:
                        pass
        if cls._tw:
            cls._job = cls.root.after(cls.FRAME, cls._step)


QUOTES = {"USD", "EUR", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK", "DKK", "PLN", "HUF", "CZK", "TRY", "MXN",
          "ZAR", "SGD", "HKD", "THB", "INR", "BRL", "RUB", "ILS", "XOF", "CNH", "CNY"}


def is_ccy_pair(name):
    """¿Es un par de dos códigos de 3 letras (divisas, metales o cripto contra divisa)? EURUSD, XAUUSD, BTCUSD…"""
    n = name.upper().replace("-OTC", "").replace("/", "")
    return len(n) == 6 and n.isalpha() and n[3:] in QUOTES


def split_pair(name):
    n = name.upper()
    otc = n.endswith("-OTC")
    n = n.replace("-OTC", "").replace("/", "")
    return n[:3], n[3:6], otc


def pretty_pair(name):
    """EURUSD -> EUR/USD; APPLE-OTC -> APPLE · OTC (acciones, índices y demás se muestran tal cual)."""
    otc = name.upper().endswith("-OTC")
    if is_ccy_pair(name):
        b, q, _ = split_pair(name)
        txt = f"{b}/{q}"
    else:
        txt = name[:-4] if otc else name
    return txt + ("  · OTC" if otc else "")


# ------------------------------------------------------------------ imágenes (cache en memoria y en disco)
def _icon_cache_dir():
    """Carpeta de íconos ya dibujados (se invalida sola si cambia el dibujo de las banderas)."""
    try:
        with open(A.__file__, "rb") as fh:
            ver = hashlib.sha1(fh.read()).hexdigest()[:10]
        import secure_store
        d = os.path.join(secure_store.user_dir(), "iconos-" + ver)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return None


class Icons:
    """Banderas y logo. Dibujar un ícono en Python puro cuesta ~12 ms; se guardan en disco (PNG) y la próxima vez
    se cargan al instante."""

    def __init__(self):
        self._c = {}
        self._dir = _icon_cache_dir()

    @staticmethod
    def _photo(rows, clear=()):
        img = tk.PhotoImage(width=len(rows[0]), height=len(rows))
        img.put(" ".join("{" + " ".join(r) + "}" for r in rows))
        for x, y in clear:
            try:
                img.transparency_set(x, y, True)
            except Exception:
                break
        return img

    def _disk(self, key):
        if not self._dir:
            return None
        name = "_".join(str(k) for k in key).replace("#", "").replace("/", "") + ".png"
        return os.path.join(self._dir, name)

    def pair(self, name, size=26, bg=CARD):
        if not is_ccy_pair(name):                       # acción, índice…: un solo círculo de color propio
            return self._single(name.upper().replace("-OTC", ""), size, bg)
        b, q, _ = split_pair(name)
        key = ("p", b + q, size, bg)
        img = self._c.get(key)
        if img is None:
            path = self._disk(key)
            if path and os.path.exists(path):
                try:
                    img = tk.PhotoImage(file=path)
                except Exception:
                    img = None
            if img is None:
                rows, _clear = A.pair_icon(b, q, size, bg)
                img = self._photo(rows)
                if path:
                    try:
                        img.write(path, format="png")
                    except Exception:
                        pass
            self._c[key] = img
        return img

    def _single(self, code, size, bg):
        safe = "".join(ch for ch in code if ch.isalnum())[:24] or "X"
        key = ("s", safe, size, bg)
        img = self._c.get(key)
        if img is None:
            path = self._disk(key)
            if path and os.path.exists(path):
                try:
                    img = tk.PhotoImage(file=path)
                except Exception:
                    img = None
            if img is None:
                rows, _clear = A.flag_icon(safe, size, bg)
                img = self._photo(rows)
                if path:
                    try:
                        img.write(path, format="png")
                    except Exception:
                        pass
            self._c[key] = img
        return img

    def logo(self, size, bg=BG, transparent=False):
        key = ("l", size, bg, transparent)
        if key not in self._c:
            rows, clear = A.logo_icon(size, bg)
            self._c[key] = self._photo(rows, clear if transparent else ())
        return self._c[key]


# ------------------------------------------------------------------ dibujo
def rrect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def _rrect_pts(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class Btn(tk.Canvas):
    """Botón plano estilo terminal. kind: primary (dorado), danger, success, ghost.
    Se dibuja UNA vez; pasar el ratón solo cambia colores (con una transición corta)."""
    _STY = {  # kind -> (relleno, relleno hover, borde, borde hover, texto, texto hover)
        "primary": (ACCENT, "#F5C566", ACCENT, "#F5C566", ON_ACCENT, ON_ACCENT),
        "ghost": (CARD2, "#22262F", BORDER, ACCENT, TEXT, ACCENT2),
        "danger": ("#2A1318", RED, RED, RED, RED, "#FFFFFF"),
        "success": ("#10261A", GREEN, GREEN, GREEN, GREEN, ON_ACCENT),
    }

    def __init__(self, master, text, command=None, kind="primary", width=160, height=40, size=11, bg=BG):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.kind, self.fsize = text, command, kind, size
        self._bw, self._bh = width, height
        self.enabled, self.hover = True, False
        self._shape = self._label = None
        self._h = 0.0                                   # 0 = normal, 1 = ratón encima
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", self._conf)
        self._draw()

    def _conf(self, e):
        if (e.width, e.height) != (self._bw, self._bh):
            self._bw, self._bh = e.width, e.height
            self._draw()

    def _set_hover(self, v):
        if self.hover == v:
            return
        self.hover = v
        if not self.enabled:
            return
        a, b = self._h, 1.0 if v else 0.0
        Anim.tween(("btn", id(self)), 120, lambda p: self._paint(a + (b - a) * p))

    def _release(self, e):
        if self.enabled and self.command and 0 <= e.x <= self._bw and 0 <= e.y <= self._bh:
            self.command()

    def set_enabled(self, on):
        if self.enabled != on:
            self.enabled = on
            self.config(cursor="hand2" if on else "arrow")
            self._paint(1.0 if (on and self.hover) else 0.0)

    def set_text(self, text):
        if self.text != text:
            self.text = text
            if self._label is not None:
                self.itemconfig(self._label, text=text)

    def set_kind(self, kind):
        if self.kind != kind:
            self.kind = kind
            self._paint(1.0 if self.hover else 0.0)

    def _paint(self, h):
        self._h = h
        if self._shape is None:
            return
        if not self.enabled:
            self.itemconfig(self._shape, fill="#16191F", outline="#16191F")
            self.itemconfig(self._label, fill="#4A505B")
            return
        f0, f1, o0, o1, t0, t1 = self._STY.get(self.kind, self._STY["ghost"])
        self.itemconfig(self._shape, fill=mixc(f0, f1, h), outline=mixc(o0, o1, h))
        self.itemconfig(self._label, fill=mixc(t0, t1, h))

    def _draw(self):
        self.delete("all")
        w, h = self._bw, self._bh
        if w < 10:
            self._shape = self._label = None
            return
        r = min(6, h / 2.0)
        self._shape = self.create_polygon(_rrect_pts(1, 1, w - 1, h - 1, r), smooth=True, width=1)
        self._label = self.create_text(w / 2, h / 2, text=self.text, font=font(self.fsize, True))
        self._paint(1.0 if self.hover else 0.0)


class Toggle(tk.Canvas):
    """Interruptor on/off ligado a una BooleanVar; la perilla se desliza."""

    def __init__(self, master, variable, bg=CARD):
        super().__init__(master, width=40, height=22, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self._p = 1.0 if variable.get() else 0.0
        self._track = self.create_polygon(_rrect_pts(1, 2, 39, 20, 9), smooth=True, outline="")
        self._knob = self.create_oval(0, 0, 1, 1, outline="")
        self.bind("<Button-1>", lambda e: self.var.set(not self.var.get()))
        self.var.trace_add("write", lambda *a: self._animate())
        self._paint(self._p)

    def _animate(self):
        a, b = self._p, 1.0 if self.var.get() else 0.0
        if a == b:
            return
        Anim.tween(("tg", id(self)), 140, lambda p: self._paint(a + (b - a) * p))

    def _paint(self, p):
        self._p = p
        x = 11 + 18 * p
        self.coords(self._knob, x - 7, 4, x + 7, 18)
        self.itemconfig(self._track, fill=mixc("#2A2F38", ACCENT, p))
        self.itemconfig(self._knob, fill=mixc("#AEB4BE", ON_ACCENT, p))


def toggle_row(master, text, variable, bg=CARD):
    row = tk.Frame(master, bg=bg)
    Toggle(row, variable, bg=bg).pack(side="left")
    lb = tk.Label(row, text=text, bg=bg, fg=TEXT, font=font(10), cursor="hand2")
    lb.pack(side="left", padx=(10, 0))
    lb.bind("<Button-1>", lambda e: variable.set(not variable.get()))
    return row


class Segmented(tk.Canvas):
    """Selector de dos o más opciones. options = [(valor, texto, color)]. El marcador se desliza a la opción elegida."""

    def __init__(self, master, options, command=None, width=380, height=44, bg=BG):
        super().__init__(master, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.options, self.command = options, command
        self.value = options[0][0]
        self._bw, self._bh = width, height
        self._pos = 0.0
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda e: (setattr(self, "_bw", e.width), self._draw()))
        self._draw()

    def _idx(self, value):
        return next((i for i, o in enumerate(self.options) if o[0] == value), 0)

    def _click(self, e):
        idx = min(len(self.options) - 1, int(e.x / (self._bw / len(self.options))))
        self.select(self.options[idx][0])

    def select(self, value):
        a, b = self._pos, float(self._idx(value))
        self.value = value
        Anim.tween(("seg", id(self)), 160, lambda p: self._move(a + (b - a) * p))
        if self.command:
            self.command(value)

    def _move(self, pos):
        self._pos = pos
        w, h, n = self._bw, self._bh, len(self.options)
        seg = (w - 8) / n
        x1 = 4 + pos * seg
        if getattr(self, "_mark", None) is None:
            return
        self.coords(self._mark, *_rrect_pts(x1, 4, x1 + seg, h - 4, 5))
        k = self._idx(self.value)
        self.itemconfig(self._mark, fill=self.options[k][2])
        for i, t in enumerate(self._txt):
            on = abs(pos - i) < 0.5
            self.itemconfig(t, fill=(ON_ACCENT if self.options[i][2] == ACCENT else "#FFFFFF") if on else MUTED)

    def _draw(self):
        self.delete("all")
        w, h, n = self._bw, self._bh, len(self.options)
        rrect(self, 1, 1, w - 1, h - 1, 6, fill=CARD, outline=BORDER)
        self._mark = self.create_polygon(0, 0, 0, 0, smooth=True, outline="")
        seg = (w - 8) / n
        self._txt = [self.create_text(4 + i * seg + seg / 2, h / 2, text=t, font=font(10, True))
                     for i, (_v, t, _c) in enumerate(self.options)]
        self._move(float(self._idx(self.value)))


class Field(tk.Frame):
    """Etiqueta + campo de texto con borde que se ilumina en dorado al enfocar."""

    def __init__(self, master, label, value="", show=None, width=10, bg=CARD):
        super().__init__(master, bg=bg)
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=font(9), anchor="w").pack(fill="x")
        self.box = tk.Frame(self, bg=INPUT, highlightthickness=1, highlightbackground=BORDER)
        self.box.pack(fill="x", pady=(4, 0))
        self.entry = tk.Entry(self.box, bg=INPUT, fg=TEXT, insertbackground=ACCENT, relief="flat", bd=0,
                              font=mono(11), width=width, highlightthickness=0, show=show or "",
                              selectbackground=GOLD_BG, selectforeground=ACCENT2)
        self.entry.pack(fill="x", padx=10, pady=7)
        self.entry.insert(0, value)
        self.entry.bind("<FocusIn>", lambda e: self.box.config(highlightbackground=ACCENT))
        self.entry.bind("<FocusOut>", lambda e: self.box.config(highlightbackground=BORDER))

    def get(self):
        return self.entry.get()

    def set(self, value):
        st = str(self.entry.cget("state"))
        if st == "disabled":                 # un campo deshabilitado ignora insert: se habilita un instante
            self.entry.config(state="normal")
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        if st == "disabled":
            self.entry.config(state="disabled")

    def focus(self):
        self.entry.focus_set()


class ComboField(tk.Frame):
    def __init__(self, master, label, values, width=12, bg=CARD):
        super().__init__(master, bg=bg)
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=font(9), anchor="w").pack(fill="x")
        self.cb = ttk.Combobox(self, state="readonly", values=values, width=width, style="Dark.TCombobox",
                               font=font(11))
        self.cb.pack(fill="x", pady=(4, 0))
        self.cb.current(0)

    def get(self):
        return self.cb.get()

    def current(self, i=None):
        return self.cb.current() if i is None else self.cb.current(i)


class Card(tk.Frame):
    """Panel con borde fino y título con marca dorada. Pon el contenido en .body"""

    def __init__(self, master, title=None, bg=CARD, pad=16):
        super().__init__(master, bg=bg, highlightthickness=1, highlightbackground=BORDER)
        if title:
            head = tk.Frame(self, bg=bg)
            head.pack(fill="x", padx=pad, pady=(12, 10))
            tk.Frame(head, bg=ACCENT, width=6, height=6).pack(side="left", pady=(1, 0))
            self.title_lbl = tk.Label(head, text=title, bg=bg, fg=TEXT, font=font(10, True))
            self.title_lbl.pack(side="left", padx=(9, 0))
            self.head = head
        self.body = tk.Frame(self, bg=bg)
        self.body.pack(fill="both", expand=True, padx=pad, pady=(0, pad))


_NUM = re.compile(r"[+\-−]?\$?\d[\d,]*(?:\.\d+)?")


def _num_parts(text):
    """('$1,015.70 ops') -> (prefijo, número, sufijo, decimales, con_signo, con_$, con_miles) o None."""
    if not isinstance(text, str) or ":" in text:
        return None
    m = _NUM.search(text)
    if not m:
        return None
    tok = m.group(0)
    sign = tok[0] in "+-−"
    body = tok[1:] if sign else tok
    dollar = body.startswith("$")
    body = body.lstrip("$")
    dec = len(body.split(".")[1]) if "." in body else 0
    try:
        val = float(body.replace(",", ""))
    except ValueError:
        return None
    if tok[0] in "-−":
        val = -val
    return text[:m.start()], val, text[m.end():], dec, sign or tok[0] in "-−", dollar, "," in body


def _num_fmt(parts, v):
    pre, _v, post, dec, signed, dollar, comma = parts
    body = f"{abs(v):,.{dec}f}" if comma else f"{abs(v):.{dec}f}"
    if dollar:
        body = "$" + body
    sg = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return pre + sg + body + post


class StatCard(tk.Canvas):
    """Indicador estilo terminal: título, cifra grande (monoespaciada), subtítulo y, opcionalmente, un anillo de
    porcentaje, un ícono o una mini curva (`spark`). Las cifras suben/bajan y el anillo se llena con una animación corta;
    solo se actualizan los elementos que cambian (no se redibuja la tarjeta)."""

    def __init__(self, master, title, bg=BG, height=114):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.title = title
        self.s = dict(value="—", sub="", color=TEXT, ring=None, ring_text="", ring_color=ACCENT, icon=None, spark=None)
        self._ids = None
        self._ring_now = 0.0
        self._shown_value = "—"
        self._size = None
        self.bind("<Configure>", lambda e: self._layout())

    def set(self, value, sub="", color=TEXT, ring=None, ring_text="", ring_color=ACCENT, icon=None, spark=None):
        new = dict(value=value, sub=sub, color=color, ring=ring, ring_text=ring_text, ring_color=ring_color, icon=icon,
                   spark=tuple(spark) if spark else None)
        if new == self.s and getattr(self, "_drawn_title", None) == self.title:
            return                                   # nada cambió (el reloj llama cada 0,5 s)
        old = self.s
        self.s = new
        if self._ids is None:
            return
        if (old["ring"] is None) != (new["ring"] is None) or (old["icon"] is None) != (new["icon"] is None) \
                or (old["spark"] is None) != (new["spark"] is None) or self._drawn_title != self.title:
            self._layout(force=True)
            return
        self._update(old)

    # ---- dibujo base (solo al cambiar de tamaño o de estructura)
    def _layout(self, force=False):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        if not force and self._size == (w, h) and self._ids is not None:
            return
        self._size = (w, h)
        self._drawn_title = self.title
        s = self.s
        self.delete("all")
        ids = {}
        self.create_rectangle(0, 0, w - 1, h - 1, fill=CARD, outline=BORDER)
        self.create_line(1, 1, 26, 1, fill=ACCENT, width=2)
        ids["title"] = self.create_text(16, 20, text=self.title, anchor="w", fill=MUTED, font=font(8, True))
        x = 16
        ids["icon"] = None
        if s["icon"] is not None:
            ids["icon"] = self.create_image(16, 58, image=s["icon"], anchor="w")
            x = 16 + s["icon"].width() + 10
        ids["value"] = self.create_text(x, 58, text=s["value"], anchor="w", fill=s["color"], font=mono(20, True))
        ids["sub"] = self.create_text(16, h - 18, text=s["sub"], anchor="w", fill=MUTED, font=font(9))
        ids["ring_bg"] = ids["ring"] = ids["ring_txt"] = ids["spark"] = ids["spark_dot"] = None
        if s["ring"] is not None or s["ring_text"]:
            cx, cy, r = w - 46, h / 2 + 2, 26
            ids["ring_bg"] = self.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#20242C", width=5)
            ids["ring"] = self.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=0, style="arc",
                                          outline=s["ring_color"], width=5)
            ids["ring_txt"] = self.create_text(cx, cy, text=s["ring_text"], fill=s["ring_color"], font=mono(10, True))
        elif s["spark"]:
            ids["spark"] = self.create_line(0, 0, 0, 0, fill=ACCENT, width=2, smooth=False)
            ids["spark_dot"] = self.create_oval(0, 0, 0, 0, fill=ACCENT, outline="")
        self._ids = ids
        self._shown_value = s["value"]
        self._ring_now = 0.0
        self._paint_ring(s["ring"] or 0.0)
        if ids["spark"] is not None:
            self._paint_spark(s["spark"], 1.0)
        if s["ring"]:
            self._anim_ring(0.0, s["ring"])

    # ---- actualizaciones (solo itemconfig/coords)
    def _update(self, old):
        s, ids = self.s, self._ids
        if ids["icon"] is not None and s["icon"] is not None:
            self.itemconfig(ids["icon"], image=s["icon"])
        self.itemconfig(ids["sub"], text=s["sub"])
        self.itemconfig(ids["value"], fill=s["color"])
        self._anim_value(self._shown_value, s["value"])
        if ids["ring"] is not None:
            self.itemconfig(ids["ring"], outline=s["ring_color"])
            self.itemconfig(ids["ring_txt"], text=s["ring_text"], fill=s["ring_color"] if s["ring"] is not None else MUTED)
            if (s["ring"] or 0.0) != self._ring_now:
                self._anim_ring(self._ring_now, s["ring"] or 0.0)
        if ids["spark"] is not None and s["spark"] != old["spark"]:
            grow = old["spark"] is not None and len(s["spark"]) == len(old["spark"]) + 1
            if grow:
                Anim.tween(("sp", id(self)), 450, lambda p: self._paint_spark(self.s["spark"], p))
            else:
                self._paint_spark(s["spark"], 1.0)

    def _anim_value(self, a, b):
        pa, pb = _num_parts(a), _num_parts(b)
        if pa is None or pb is None or pa[0] != pb[0] or pa[2] != pb[2] or pa[1] == pb[1] or not self.winfo_viewable():
            self.itemconfig(self._ids["value"], text=b)
            self._shown_value = b
            return
        va, vb = pa[1], pb[1]

        def step(p):
            txt = b if p >= 1 else _num_fmt(pb, va + (vb - va) * p)
            self.itemconfig(self._ids["value"], text=txt)
            self._shown_value = txt
        Anim.tween(("val", id(self)), 450, step)

    def _anim_ring(self, a, b):
        if not self.winfo_viewable():
            self._paint_ring(b)
            return
        Anim.tween(("ring", id(self)), 550, lambda p: self._paint_ring(a + (b - a) * p))

    def _paint_ring(self, v):
        self._ring_now = v
        if self._ids and self._ids.get("ring") is not None:
            self.itemconfig(self._ids["ring"], extent=-min(359.9, 360.0 * max(0.0, v)) if v > 0 else 0)

    def _paint_spark(self, data, p):
        ids = self._ids
        if not data or ids.get("spark") is None:
            return
        w, h = self._size
        x0, x1, y0, y1 = w * 0.52, w - 18, 30, h - 34
        vals = list(data)
        if len(vals) == 1:
            vals = vals * 2
        lo, hi = min(vals + [0.0]), max(vals + [0.0])
        span = (hi - lo) or 1.0
        n = len(vals)
        pts = []
        for k, v in enumerate(vals):
            x = x0 + (x1 - x0) * k / (n - 1)
            y = y1 - (y1 - y0) * (v - lo) / span
            pts.append((x, y))
        if p < 1.0 and n >= 2:                      # el último tramo «crece»
            (xa, ya), (xb, yb) = pts[-2], pts[-1]
            pts[-1] = (xa + (xb - xa) * p, ya + (yb - ya) * p)
        col = GREEN if vals[-1] > 0 else RED if vals[-1] < 0 else ACCENT
        self.coords(ids["spark"], *[c for pt in pts for c in pt])
        self.itemconfig(ids["spark"], fill=col)
        xe, ye = pts[-1]
        self.coords(ids["spark_dot"], xe - 3, ye - 3, xe + 3, ye + 3)
        self.itemconfig(ids["spark_dot"], fill=col)


class LiveDot(tk.Canvas):
    """Punto de estado: «live» late en verde, «warn» late en naranja, «idle» gris fijo. El latido cambia solo el tamaño
    y el color de un aro (un elemento), unas 12 veces por segundo, y se detiene si no se ve."""

    def __init__(self, master, bg=PANEL, size=16):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._bg, self._sz = bg, size
        c = size / 2
        self._halo = self.create_oval(c, c, c, c, outline="", fill=bg)
        self._dot = self.create_oval(c - 4, c - 4, c + 4, c + 4, outline="", fill=DIM)
        self.state = "idle"
        self._job = None
        self._t0 = time.perf_counter()

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        col = {"live": GREEN, "warn": AMBER}.get(state, DIM)
        self.itemconfig(self._dot, fill=col)
        if state == "idle":
            if self._job:
                self.after_cancel(self._job)
                self._job = None
            c = self._sz / 2
            self.coords(self._halo, c, c, c, c)
        elif self._job is None:
            self._t0 = time.perf_counter()
            self._job = self.after(80, self._beat)

    def _beat(self):
        self._job = None
        if self.state == "idle":
            return
        try:
            visible = self.winfo_viewable() and self.winfo_toplevel().state() != "iconic"
        except Exception:
            visible = False
        if visible:
            ph = ((time.perf_counter() - self._t0) % 1.4) / 1.4
            c = self._sz / 2
            r = 4 + 4 * ph
            col = {"live": GREEN, "warn": AMBER}.get(self.state, DIM)
            self.coords(self._halo, c - r, c - r, c + r, c + r)
            self.itemconfig(self._halo, fill=mixc(col, self._bg, 0.35 + 0.65 * ph))
            self.tag_raise(self._dot)
        self._job = self.after(80 if visible else 400, self._beat)


class StatusLabel(tk.Frame):
    """Estado del bot con punto que late. Acepta .config(text=..., fg=...) como la etiqueta de antes."""

    def __init__(self, master, text="", bg=PANEL, fg=MUTED):
        super().__init__(master, bg=bg)
        self.dot = LiveDot(self, bg=bg)
        self.dot.pack(side="left", padx=(0, 6))
        self.lbl = tk.Label(self, text="", bg=bg, fg=fg, font=mono(10, True))
        self.lbl.pack(side="left")
        self.config(text=text, fg=fg)

    def config(self, text=None, fg=None, **kw):
        if text is not None:
            t = text.lstrip("●").strip()
            self.lbl.config(text=t)
            up = t.upper()
            self.dot.set_state("live" if "OPERANDO" in up or "ACTIVO" in up else
                               "warn" if ("DETENIENDO" in up or "TERMINANDO" in up) else "idle")
        if fg is not None:
            self.lbl.config(fg=fg)
        if kw:
            super().config(**kw)

    configure = config

    def cget(self, key):
        return self.lbl.cget(key)


class CountLabel(tk.Label):
    """Etiqueta cuya cifra sube/baja con una animación corta al cambiar (p. ej. el saldo)."""

    def set_text(self, text):
        old = self.cget("text")
        pa, pb = _num_parts(old), _num_parts(text)
        if pa is None or pb is None or pa[0] != pb[0] or pa[2] != pb[2] or pa[1] == pb[1] or not self.winfo_viewable():
            self.config(text=text)
            return
        va, vb = pa[1], pb[1]
        Anim.tween(("cl", id(self)), 600, lambda p: self.config(text=text if p >= 1 else _num_fmt(pb, va + (vb - va) * p)))


class Tabs(tk.Frame):
    """Pestañas con subrayado dorado que se desliza a la pestaña elegida."""

    def __init__(self, master, items, command, bg=BG):
        super().__init__(master, bg=bg)
        self.bg, self.command, self.items = bg, command, items
        self.lbl = {}
        row = tk.Frame(self, bg=bg)
        row.pack(side="top", anchor="w")
        tk.Frame(self, bg=bg, height=3).pack(side="top", fill="x")
        for key, text in items:
            lb = tk.Label(row, text=text, bg=bg, fg=MUTED, font=font(11, True), cursor="hand2", pady=8)
            lb.pack(side="left", padx=(0, 28))
            lb.bind("<Button-1>", lambda e, k=key: self.select(k))
            lb.bind("<Enter>", lambda e, k=key: self._hover(k, True))
            lb.bind("<Leave>", lambda e, k=key: self._hover(k, False))
            self.lbl[key] = lb
        self.ul = tk.Frame(self, bg=ACCENT, height=2)
        self._ux = self._uw = None
        self.current = None

    def _hover(self, key, on):
        if key != self.current:
            self.lbl[key].config(fg=TEXT if on else MUTED)

    def _target(self, key):
        lb = self.lbl[key]
        return lb.winfo_x(), max(10, lb.winfo_width())

    def select(self, key):
        prev = self.current
        self.current = key
        for k in self.lbl:
            self.lbl[k].config(fg=ACCENT if k == key else MUTED)
        if self.command:
            self.command(key)
        if self._ux is None:                       # solo la primera vez hace falta medir antes de dibujar
            self.update_idletasks()
        x, w = self._target(key)
        y = self.lbl[key].winfo_y() + self.lbl[key].winfo_height() + 1
        if prev is None or self._ux is None:
            self._ux, self._uw = x, w
            self.ul.place(x=x, y=y, width=w, height=2)
            return
        xa, wa = self._ux, self._uw

        def step(p):
            self._ux, self._uw = xa + (x - xa) * p, wa + (w - wa) * p
            self.ul.place(x=int(self._ux), y=y, width=int(self._uw), height=2)
        Anim.tween(("tabs", id(self)), 220, step)


# ------------------------------------------------------------------ estilos ttk
def setup_style(root):
    global MONO
    Anim.init(root)
    try:                                         # la mejor fuente monoespaciada instalada
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
        MONO = next((f for f in _MONO_PREF if f in fams), MONO)
    except Exception:
        pass
    st = ttk.Style(root)
    st.theme_use("clam")

    def fixed_map(option):  # arreglo conocido de Tk 8.6.9: los colores de las filas no se aplicaban
        return [e for e in st.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]
    st.map("Treeview", foreground=fixed_map("foreground"), background=fixed_map("background"))

    st.configure("Dark.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=36,
                 borderwidth=0, relief="flat", font=font(10))
    st.layout("Dark.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    st.map("Dark.Treeview", background=[("selected", GOLD_BG)], foreground=[("selected", ACCENT2)])
    st.configure("Dark.Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                 font=font(8, True), padding=(8, 8), borderwidth=0)
    st.map("Dark.Treeview.Heading", background=[("active", PANEL)])

    st.configure("Dark.Vertical.TScrollbar", background="#2A2F38", troughcolor=CARD, bordercolor=CARD,
                 arrowcolor=MUTED, lightcolor="#2A2F38", darkcolor="#2A2F38", gripcount=0, relief="flat",
                 arrowsize=11)
    st.map("Dark.Vertical.TScrollbar", background=[("active", "#4A4232")])

    st.configure("Dark.TCombobox", fieldbackground=INPUT, background=INPUT, foreground=TEXT, arrowcolor=ACCENT,
                 bordercolor=BORDER, lightcolor=INPUT, darkcolor=INPUT, selectbackground=INPUT,
                 selectforeground=TEXT, padding=7, relief="flat")
    st.map("Dark.TCombobox", fieldbackground=[("readonly", INPUT)], selectbackground=[("readonly", INPUT)],
           foreground=[("readonly", TEXT)], bordercolor=[("focus", ACCENT), ("hover", "#4A4232")])
    st.configure("Edit.TCombobox", fieldbackground=INPUT, background=INPUT, foreground=TEXT, arrowcolor=ACCENT,
                 bordercolor=BORDER, lightcolor=INPUT, darkcolor=INPUT, selectbackground=GOLD_BG,
                 selectforeground=ACCENT2, insertcolor=ACCENT, padding=7, relief="flat")
    st.map("Edit.TCombobox", fieldbackground=[("focus", INPUT)], bordercolor=[("focus", ACCENT)])
    root.option_add("*TCombobox*Listbox.background", CARD2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", GOLD_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", ACCENT2)
    root.option_add("*TCombobox*Listbox.font", font(10))
    install_wheel(root)


# ------------------------------------------------------------------ rueda del ratón
# Antes: al bajar por la pestaña Automático con el puntero sobre un desplegable, la rueda CAMBIABA su valor (estrategia,
# mercado, apuesta…) en vez de bajar la página, y además la página y las tablas se desplazaban a la vez.
# Ahora un único manejador decide: si el puntero está sobre una tabla que tiene más filas que las visibles, se desplaza
# la tabla; si no, se desplaza la página que la contiene (de forma suave, agrupando los giros rápidos). Los desplegables
# nunca cambian de valor con la rueda.
_SCROLLERS = {}


def install_wheel(root):
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Shift-MouseWheel>"):
        root.bind_class("TCombobox", seq, lambda e: None)      # reemplaza la vinculación que cambiaba el valor
    root.bind_all("<MouseWheel>", _on_wheel, add="+")
    root.bind_all("<Button-4>", lambda e: _on_wheel(e, -120), add="+")
    root.bind_all("<Button-5>", lambda e: _on_wheel(e, 120), add="+")


def _on_wheel(e, delta=None):
    d = e.delta if delta is None else -delta
    if not d:
        return
    try:
        w = e.widget.winfo_containing(e.x_root, e.y_root)
    except Exception:          # p. ej. la lista desplegable de un combobox (creada por Tcl): se encarga ella misma
        return
    node = w
    while node is not None:
        sf = _SCROLLERS.get(str(node))
        if sf is not None:
            sf.scroll_wheel(d)
            return
        try:
            cls = node.winfo_class()
        except Exception:
            return
        if cls in ("Treeview", "Text", "Listbox"):
            try:
                first, last = node.yview()
            except Exception:
                first, last = 0.0, 1.0
            if first > 0.0 or last < 1.0:
                return         # la tabla tiene su propio desplazamiento: se mueve ella, no la página
        node = node.master


class ScrollFrame(tk.Frame):
    """Contenedor con barra de desplazamiento vertical. Pon el contenido en .inner"""

    def __init__(self, master, bg=BG):
        super().__init__(master, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=1)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style="Dark.Vertical.TScrollbar")
        self.inner = tk.Frame(self.cv, bg=bg)
        self.win = self.cv.create_window((0, 0), window=self.inner, anchor="nw")
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self._px, self._job, self._region = 0.0, None, None
        self.inner.bind("<Configure>", self._inner_conf)
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        _SCROLLERS[str(self)] = self
        self.bind("<Destroy>", lambda e: _SCROLLERS.pop(str(self), None) if e.widget is self else None)

    def _inner_conf(self, _e=None):
        region = self.cv.bbox("all")
        if region != self._region:
            self._region = region
            self.cv.configure(scrollregion=region)

    def scroll_wheel(self, delta):
        if self.inner.winfo_height() <= self.cv.winfo_height():
            return
        # Windows: 120 por «clic» de rueda; macOS: valores pequeños y continuos
        px = -delta * (0.6 if abs(delta) >= 60 else 4.0)
        self._px += px
        if self._job is None:
            self._job = self.after(12, self._flush)

    def _flush(self):
        self._job = None
        step = int(self._px)
        if step:
            self._px -= step
            self.cv.yview_scroll(step, "units")


class ComboEntry(tk.Frame):
    """Etiqueta + campo editable con lista desplegable (p. ej. correos guardados)."""

    def __init__(self, master, label, values=(), value="", width=40, bg=CARD):
        super().__init__(master, bg=bg)
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=font(9), anchor="w").pack(fill="x")
        self.cb = ttk.Combobox(self, values=list(values), width=width, style="Edit.TCombobox", font=font(11))
        self.cb.pack(fill="x", pady=(4, 0))
        self.cb.insert(0, value)
        self.entry = self.cb

    def get(self):
        return self.cb.get()

    def set(self, value):
        self.cb.delete(0, "end")
        self.cb.insert(0, value)

    def set_values(self, values):
        self.cb.configure(values=list(values))

    def focus(self):
        self.cb.focus_set()


def make_tree(master, icon_heading, cols, height=6, icon_w=170):
    """Tabla oscura con columna de ícono (#0). cols = [(id, encabezado, ancho, anclaje)]."""
    fr = tk.Frame(master, bg=CARD)
    tree = ttk.Treeview(fr, style="Dark.Treeview", columns=[c[0] for c in cols], show="tree headings",
                        height=height, selectmode="browse")
    tree.heading("#0", text=icon_heading, anchor="w")
    tree.column("#0", width=icon_w, minwidth=icon_w, stretch=False, anchor="w")
    for i, (cid, head, w, anchor) in enumerate(cols):
        tree.heading(cid, text=head, anchor=anchor)
        tree.column(cid, width=w, minwidth=40, anchor=anchor, stretch=(i == len(cols) - 1))
    sb = ttk.Scrollbar(fr, orient="vertical", command=tree.yview, style="Dark.Vertical.TScrollbar")
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    for tag, color in (("pend", TEXT), ("next", ACCENT2), ("open", ACCENT2), ("win", GREEN), ("loss", RED),
                       ("skip", AMBER), ("muted", MUTED)):
        tree.tag_configure(tag, foreground=color)
    return fr, tree
