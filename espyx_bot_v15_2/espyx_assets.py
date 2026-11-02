"""
EspyX Bot — gráficos generados por código (sin archivos de imagen ni librerías extra).

Todo aquí es matemática pura: devuelve filas de colores '#rrggbb' que app.py convierte en imágenes de Tk.
Incluye banderas circulares de divisas, el ícono de un par (dos banderas solapadas) y el logo de EspyX.
"""
import colorsys
import math
import zlib


def hex_rgb(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def rgb_hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def mix(c1, c2, t):
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


def _disc(u, v, cx, cy, r):
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


# ------------------------------------------------------------------ banderas
# Cada función recibe (u, v) en [-1, 1] (v positivo hacia abajo) y devuelve (r, g, b).
WHITE, RED, BLUE = (255, 255, 255), (206, 17, 38), (0, 40, 104)


def _jack(u, v, field=(1, 33, 105)):
    """Union Jack aproximada, para u, v en [-1, 1]."""
    cross_w, cross_r = 0.40, 0.24
    if abs(u) < cross_r / 2 + 0.0 and True:
        pass
    if abs(u) < cross_r / 2 or abs(v) < cross_r / 2:
        return (200, 16, 46)
    if abs(u) < cross_w / 2 or abs(v) < cross_w / 2:
        return WHITE
    d1, d2 = abs(u - v), abs(u + v)
    if min(d1, d2) < 0.10:
        return (200, 16, 46)
    if min(d1, d2) < 0.30:
        return WHITE
    return field


def flag_usd(u, v):
    if u < 0.05 and v < 0.12:
        # cantón azul con puntitos blancos
        for gx in (-0.75, -0.40, -0.05):
            for gy in (-0.78, -0.45, -0.12):
                if _disc(u, v, gx, gy, 0.07):
                    return WHITE
        return (60, 59, 110)
    stripe = int((v + 1) / 2 * 7)
    return (178, 34, 52) if stripe % 2 == 0 else WHITE


def flag_eur(u, v):
    for k in range(12):
        a = k * math.pi / 6
        if _disc(u, v, 0.55 * math.cos(a), 0.55 * math.sin(a), 0.11):
            return (255, 204, 0)
    return (0, 51, 153)


def flag_jpy(u, v):
    return (188, 0, 45) if _disc(u, v, 0, 0, 0.46) else WHITE


def flag_gbp(u, v):
    return _jack(u, v)


def flag_aud(u, v):
    if u < 0 and v < 0:
        return _jack(u * 2 + 1, v * 2 + 1, (0, 0, 139))
    for cx, cy, r in ((-0.5, 0.55, 0.20), (0.55, -0.55, 0.11), (0.28, 0.02, 0.11),
                      (0.72, 0.08, 0.11), (0.52, 0.62, 0.11), (0.58, 0.28, 0.06)):
        if _disc(u, v, cx, cy, r):
            return WHITE
    return (0, 0, 139)


def flag_nzd(u, v):
    if u < 0 and v < 0:
        return _jack(u * 2 + 1, v * 2 + 1, (0, 36, 125))
    for cx, cy in ((0.50, -0.45), (0.72, 0.05), (0.25, 0.05), (0.50, 0.62)):
        if _disc(u, v, cx, cy, 0.14):
            return WHITE if not _disc(u, v, cx, cy, 0.09) else (204, 20, 43)
    return (0, 36, 125)


def flag_cad(u, v):
    if abs(u) > 0.5:
        return (216, 6, 33)
    if _disc(u, v, 0, 0.02, 0.27) or (abs(u) < 0.06 and 0.2 < v < 0.5):
        return (216, 6, 33)
    return WHITE


def flag_chf(u, v):
    if (abs(u) < 0.17 and abs(v) < 0.55) or (abs(v) < 0.17 and abs(u) < 0.55):
        return WHITE
    return (213, 43, 30)


def flag_brl(u, v):
    if _disc(u, v, 0, 0, 0.38):
        return (0, 39, 118)
    if abs(u) / 0.9 + abs(v) / 0.62 < 1:
        return (254, 223, 0)
    return (0, 156, 59)


def flag_mxn(u, v):
    if _disc(u, v, 0, 0, 0.18):
        return (140, 100, 40)
    if u < -0.34:
        return (0, 104, 71)
    if u > 0.34:
        return (206, 17, 38)
    return WHITE


def flag_cny(u, v):
    for cx, cy, r in ((-0.45, -0.45, 0.28), (0.05, -0.80, 0.08), (0.28, -0.52, 0.08),
                      (0.28, -0.22, 0.08), (0.05, 0.02, 0.08)):
        if _disc(u, v, cx, cy, r):
            return (255, 222, 0)
    return (222, 41, 16)


def flag_try(u, v):
    if _disc(u, v, 0.45, 0, 0.14):
        return WHITE
    if _disc(u, v, -0.10, 0, 0.50) and not _disc(u, v, 0.05, 0, 0.40):
        return WHITE
    return (227, 10, 23)


def flag_inr(u, v):
    if abs(math.hypot(u, v) - 0.26) < 0.045:
        return (0, 0, 128)
    if v < -0.34:
        return (255, 153, 51)
    if v > 0.34:
        return (19, 136, 8)
    return WHITE


def flag_zar(u, v):
    if u < -0.25 and abs(v) < 0.55 - (u + 0.25) * -0.0 and (abs(v) < -(u + 0.25) * 0.9 + 0.1):
        return (0, 0, 0) if abs(v) < 0.35 else (255, 184, 28)
    if abs(v) < 0.16:
        return (0, 122, 77) if u > -0.9 else WHITE
    return (222, 56, 49) if v < 0 else (0, 20, 137)


FLAGS = {
    "USD": flag_usd, "EUR": flag_eur, "JPY": flag_jpy, "GBP": flag_gbp, "AUD": flag_aud,
    "NZD": flag_nzd, "CAD": flag_cad, "CHF": flag_chf, "BRL": flag_brl, "MXN": flag_mxn,
    "CNY": flag_cny, "TRY": flag_try, "INR": flag_inr, "ZAR": flag_zar,
}


def _fallback(code):
    """Bola de color única por divisa (para códigos sin bandera dibujada)."""
    h = (zlib.crc32(code.encode()) % 360) / 360.0
    base = tuple(int(255 * x) for x in colorsys.hls_to_rgb(h, 0.45, 0.65))
    light = tuple(min(255, int(c + 70)) for c in base)

    def f(u, v):
        return light if _disc(u, v, 0, 0, 0.34) else base
    return f


def flag_fn(code):
    return FLAGS.get(code.upper()) or _fallback(code.upper())


# ------------------------------------------------------------------ render
def render(w, h, sampler, bg, ss=3):
    """Devuelve (filas de '#rrggbb', lista de píxeles totalmente vacíos). Antialias por supermuestreo."""
    bgc = hex_rgb(bg) if isinstance(bg, str) else bg
    bgs = rgb_hex(bgc)
    n = ss * ss
    rows, clear = [], []
    for y in range(h):
        row = []
        for x in range(w):
            r = g = b = 0.0
            cov = 0
            for j in range(ss):
                for i in range(ss):
                    c = sampler(x + (i + 0.5) / ss, y + (j + 0.5) / ss)
                    if c is not None:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        cov += 1
            if cov == 0:
                row.append(bgs)
                clear.append((x, y))
                continue
            a = cov / n
            row.append(rgb_hex(((r / cov) * a + bgc[0] * (1 - a),
                                (g / cov) * a + bgc[1] * (1 - a),
                                (b / cov) * a + bgc[2] * (1 - a))))
        rows.append(row)
    return rows, clear


def flag_icon(code, size, bg):
    r = size / 2.0
    fn = flag_fn(code)

    def sampler(px, py):
        u, v = (px - r) / r, (py - r) / r
        return fn(u, v) if u * u + v * v <= 1 else None
    return render(size, size, sampler, bg)


def pair_icon(base, quote, size, bg):
    """Dos banderas circulares solapadas (la de la divisa quote encima, con un aro del color de fondo)."""
    w = int(size * 1.62)
    r = size / 2.0
    ring = max(1.4, size / 14.0)
    cb = (w - r, r)
    fa, fb = flag_fn(base), flag_fn(quote)
    bgc = hex_rgb(bg)

    def sampler(px, py):
        dbx, dby = px - cb[0], py - r
        db = math.hypot(dbx, dby)
        if db <= r:
            return fb(dbx / r, dby / r)
        if db <= r + ring:
            return bgc
        dax, day = px - r, py - r
        if math.hypot(dax, day) <= r:
            return fa(dax / r, day / r)
        return None
    return render(w, size, sampler, bg)


def logo_icon(size, bg):
    """Logo EspyX (v15): cuadrado dorado con una X oscura, estilo terminal."""
    c1, c2 = hex_rgb("#F5C566"), hex_rgb("#C8902F")
    rad = 0.22

    def sampler(px, py):
        u, v = (px / size) * 2 - 1, (py / size) * 2 - 1
        # cuadrado redondeado
        qx, qy = abs(u) - (1 - rad), abs(v) - (1 - rad)
        if qx > 0 and qy > 0 and math.hypot(qx, qy) > rad:
            return None
        if abs(u) > 1 or abs(v) > 1:
            return None
        t = (u + v + 2) / 4
        col = mix(c1, c2, t)
        # X blanca (un trazo más grueso que el otro)
        d1, d2 = abs(u - v) / math.sqrt(2), abs(u + v) / math.sqrt(2)
        inside = max(abs(u), abs(v)) < 0.56
        if inside and d1 < 0.115:
            return (20, 17, 10)
        if inside and d2 < 0.115:
            return mix((20, 17, 10), col, 0.25)
        return col
    return render(size, size, sampler, bg)
