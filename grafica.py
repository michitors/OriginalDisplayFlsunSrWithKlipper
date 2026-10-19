"""Strumenti di disegno della grafica nuova dello schermo FLSUN SR (320×480), 2026-10-18.

Colori, misure e icone delle pagine, con il carattere Inter (libero, OFL: caratteri/LICENSE.txt). In più si può disegnare su un ritaglio della pagina (Pagina con
riquadro): le parti che cambiano (numeri, anello, barre) diventano icone ritagliate dallo stesso sfondo, così sullo
schermo combaciano col fondo della pagina.
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

QUI = os.path.dirname(os.path.abspath(__file__))
L, A = 320, 480
S = 4                        # disegno 4 volte più grande e poi riduco: bordi lisci

SFONDO_ALTO, SFONDO_BASSO = (22, 26, 34), (10, 12, 17)   # restano per le tinte di pulsanti e pillole
SFONDO = (0, 0, 0)         # sfondo delle pagine: nero pieno (lo sfumato diventava strisce sullo schermo vero)
CARTA, BORDO, BINARIO = (27, 32, 41), (38, 45, 56), (40, 47, 58)
TESTO, GRIGIO, SPENTO = (242, 244, 248), (139, 149, 167), (88, 97, 112)
UGELLO_C, PIATTO_C = (255, 138, 61), (76, 194, 255)
VERDE, GIALLO, ROSSO, VIOLA = (61, 220, 132), (255, 184, 48), (255, 92, 98), (160, 130, 255)
ACQUA, BLU = (56, 225, 176), (61, 150, 255)
SCURO = (10, 32, 20)                     # testo sul verde pieno

PESI = {"Medium": "Inter-Medium.ttf", "Semibold": "Inter-SemiBold.ttf", "Bold": "Inter-Bold.ttf",
        "Heavy": "Inter-ExtraBold.ttf"}
_caratteri = {}


def carattere(px, peso):
    if (px, peso) not in _caratteri:
        _caratteri[px, peso] = ImageFont.truetype(os.path.join(QUI, "caratteri", PESI[peso]), round(px * S))
    return _caratteri[px, peso]


def mescola(a, b, t):
    """t di a e il resto di b."""
    return tuple(round(a[i] * t + b[i] * (1 - t)) for i in range(3))


def chiaro(c):
    """Colore di un pulsante premuto."""
    return mescola((255, 255, 255), c, 0.12)


_sfumato = None


def sfondo_sfumato():
    global _sfumato
    if _sfumato is None:
        _sfumato = Image.new("RGB", (L * S, A * S))
        # 2026-10-18 sullo schermo vero la sfumatura (22,26,34)→(10,12,17) faceva 3-4 strisce: lo schermo ha 32
        # livelli di rosso e di blu e schiarisce molto gli scuri. Ora tinta unita, nero come FLSUN
        _sfumato.paste(SFONDO, (0, 0, L * S, A * S))
    return _sfumato


def larghezza(testo, px, peso):
    return carattere(px, peso).getlength(testo) / S


STRETTI = set()   # scritte che escono dal loro bottone (genera.py le stampa come AVVISO)


def controlla_spazio(testo, w, spazio):
    if w > spazio:
        STRETTI.add(f"«{testo}» è larga {w:.0f} px, ce ne sono {spazio:.0f}")


class Pagina:
    """Tela ×S. Con riquadro (x, y, l, a in pixel veri) è solo quel pezzo di pagina, ma si disegna sempre con le
    coordinate della pagina intera."""

    def __init__(self, riquadro=None, sfondo=None):
        base = sfondo if sfondo is not None else sfondo_sfumato()
        if riquadro:
            x, y, w, h = riquadro
            self.ox, self.oy = x, y
            self.im = base.crop((x * S, y * S, (x + w) * S, (y + h) * S))
        else:
            self.ox = self.oy = 0
            self.im = base.copy()
        self.d = ImageDraw.Draw(self.im)

    def pt(self, x, y):
        return ((x - self.ox) * S, (y - self.oy) * S)

    def r(self, x0, y0, x1, y1):
        return [round((x0 - self.ox) * S), round((y0 - self.oy) * S),
                round((x1 - self.ox) * S), round((y1 - self.oy) * S)]

    def rett(self, x0, y0, x1, y1, raggio, colore, bordo=None):
        self.d.rounded_rectangle(self.r(x0, y0, x1, y1), radius=round(raggio * S), fill=colore,
                                 outline=bordo, width=S if bordo else 0)

    def tondo(self, cx, cy, raggio, colore, bordo=None, spessore=0):
        self.d.ellipse(self.r(cx - raggio, cy - raggio, cx + raggio, cy + raggio), fill=colore, outline=bordo,
                       width=round(spessore * S))

    def linea(self, punti, spessore, colore):
        self.d.line([self.pt(x, y) for x, y in punti], fill=colore, width=round(spessore * S), joint="curve")
        for x, y in (punti[0], punti[-1]):
            self.tondo(x, y, spessore / 2, colore)

    def poligono(self, punti, colore):
        self.d.polygon([self.pt(x, y) for x, y in punti], fill=colore)

    def scrivi(self, x, y, testo, px, peso, colore, ancora="la"):
        self.d.text(self.pt(x, y), testo, font=carattere(px, peso), fill=colore, anchor=ancora)

    def bagliore(self, disegna, raggio, forza):
        """disegna(d) disegna su uno strato trasparente (coordinate con self.r), che si sfuma sotto la pagina."""
        strato = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
        disegna(ImageDraw.Draw(strato))
        strato = strato.filter(ImageFilter.GaussianBlur(raggio * S))
        strato.putalpha(strato.getchannel("A").point(lambda v: round(v * forza)))
        self.im = Image.alpha_composite(self.im.convert("RGBA"), strato).convert("RGB")
        self.d = ImageDraw.Draw(self.im)

    def anello(self, cx, cy, raggio, spessore, frazione, c0, c1):
        box = self.r(cx - raggio, cy - raggio, cx + raggio, cy + raggio)
        w = round(spessore * S)
        self.d.arc(box, 0, 360, fill=BINARIO, width=w)
        if frazione <= 0:
            return
        n = max(2, round(300 * frazione))
        for i in range(n):
            a0 = -90 + 360 * frazione * i / n
            self.d.arc(box, a0, a0 + 360 * frazione / n + 0.8, fill=mescola(c1, c0, i / (n - 1)), width=w)
        rm = raggio - spessore / 2                    # l'arco di PIL cresce verso l'interno del riquadro
        for ang, col in ((-90, c0), (-90 + 360 * frazione, c1)):
            t = math.radians(ang)
            self.tondo(cx + rm * math.cos(t), cy + rm * math.sin(t), spessore / 2, col)

    def barra(self, x0, y, x1, frazione, colore, alta=6):
        self.rett(x0, y, x1, y + alta, alta / 2, BINARIO)
        if frazione > 0:
            self.rett(x0, y, x0 + max(alta, (x1 - x0) * min(1, frazione)), y + alta, alta / 2, colore)

    def riduci(self):
        """L'immagine vera, in pixel dello schermo."""
        return self.im.resize((self.im.width // S, self.im.height // S), Image.LANCZOS)


def ritaglio(sfondo, box, disegna, margine=3):
    """Icona = il pezzo box (x, y, l, a) dello sfondo ×S con disegna(p) sopra. Si disegna con qualche pixel in più
    attorno e poi si taglia, perché la riduzione ai bordi venga uguale a quella della pagina intera."""
    x, y, w, h = box
    x0, y0 = max(0, x - margine), max(0, y - margine)
    x1, y1 = min(L, x + w + margine), min(A, y + h + margine)
    p = Pagina((x0, y0, x1 - x0, y1 - y0), sfondo)
    disegna(p)
    return p.riduci().crop((x - x0, y - y0, x - x0 + w, y - y0 + h))


# ---- icone (centro cx, cy; k = lato in px) ----

def ic_ugello(p, cx, cy, k, c):
    p.linea([(cx, cy - k * 0.5), (cx, cy - k * 0.25)], k * 0.11, c)
    p.rett(cx - k * 0.36, cy - k * 0.25, cx + k * 0.36, cy + k * 0.12, k * 0.08, c)
    p.poligono([(cx - k * 0.2, cy + k * 0.12), (cx + k * 0.2, cy + k * 0.12),
                (cx + k * 0.06, cy + k * 0.42), (cx - k * 0.06, cy + k * 0.42)], c)


def ic_piatto(p, cx, cy, k, c):
    for dx in (-0.26, 0, 0.26):
        p.linea([(cx + dx * k + 0.07 * k * math.sin(i / 12 * 2 * math.pi), cy - k * 0.44 + i / 12 * k * 0.5)
                 for i in range(13)], k * 0.1, c)
    p.rett(cx - k * 0.48, cy + k * 0.2, cx + k * 0.48, cy + k * 0.36, k * 0.06, c)


def goccia(cx, cy, a, b, n=72):
    return [(cx + a * math.sin(t) * math.sin(t / 2) ** 1.3, cy - b * math.cos(t))
            for t in (2 * math.pi * i / n for i in range(n))]


def ic_fiamma(p, cx, cy, k, c, fondo=CARTA):
    p.poligono(goccia(cx, cy, k * 0.44, k * 0.5), c)
    p.poligono(goccia(cx, cy + k * 0.2, k * 0.2, k * 0.26), mescola(c, fondo, 0.18))


def ic_bobina(p, cx, cy, k, c):
    p.tondo(cx, cy, k * 0.46, None, c, k * 0.1)
    p.tondo(cx, cy, k * 0.14, c)
    for ang in (30, 150, 270):
        t = math.radians(ang)
        p.tondo(cx + k * 0.3 * math.cos(t), cy + k * 0.3 * math.sin(t), k * 0.07, c)


def ic_fiocco(p, cx, cy, k, c):
    r = k * 0.48
    for ang in (90, 30, -30):
        t = math.radians(ang)
        dx, dy = math.cos(t), math.sin(t)
        p.linea([(cx - r * dx, cy - r * dy), (cx + r * dx, cy + r * dy)], k * 0.09, c)
        for verso in (1, -1):
            ux, uy = verso * dx, verso * dy
            bx, by = cx + ux * r * 0.52, cy + uy * r * 0.52
            fine = []
            for rot in (math.radians(45), -math.radians(45)):
                ex, ey = math.cos(rot) * ux - math.sin(rot) * uy, math.sin(rot) * ux + math.cos(rot) * uy
                fine.append((bx + ex * r * 0.34, by + ey * r * 0.34))
            p.linea([fine[0], (bx, by), fine[1]], k * 0.09, c)


def ic_casa(p, cx, cy, k, c):
    p.linea([(cx - k * 0.46, cy - k * 0.02), (cx, cy - k * 0.44), (cx + k * 0.46, cy - k * 0.02)], k * 0.1, c)
    p.linea([(cx - k * 0.3, cy - k * 0.14), (cx - k * 0.3, cy + k * 0.42), (cx + k * 0.3, cy + k * 0.42),
             (cx + k * 0.3, cy - k * 0.14)], k * 0.1, c)


def ic_pausa(p, cx, cy, k, c):
    for dx in (-0.2, 0.2):
        p.rett(cx + dx * k - k * 0.1, cy - k * 0.4, cx + dx * k + k * 0.1, cy + k * 0.4, k * 0.06, c)


def ic_play(p, cx, cy, k, c):
    p.poligono([(cx - k * 0.3, cy - k * 0.42), (cx + k * 0.42, cy), (cx - k * 0.3, cy + k * 0.42)], c)


def ic_croce(p, cx, cy, k, c):
    p.linea([(cx - k * 0.32, cy - k * 0.32), (cx + k * 0.32, cy + k * 0.32)], k * 0.12, c)
    p.linea([(cx + k * 0.32, cy - k * 0.32), (cx - k * 0.32, cy + k * 0.32)], k * 0.12, c)


def ic_freccia(p, cx, cy, k, c, su=False):
    v = -1 if su else 1
    p.linea([(cx, cy - v * k * 0.42), (cx, cy + v * k * 0.4)], k * 0.12, c)
    p.linea([(cx - k * 0.3, cy + v * k * 0.1), (cx, cy + v * k * 0.4), (cx + k * 0.3, cy + v * k * 0.1)], k * 0.12, c)


def ic_su(p, cx, cy, k, c):
    ic_freccia(p, cx, cy, k, c, True)


def ic_indietro(p, cx, cy, k, c):
    p.linea([(cx + k * 0.14, cy - k * 0.3), (cx - k * 0.16, cy), (cx + k * 0.14, cy + k * 0.3)], k * 0.13, c)


def ic_regola(p, cx, cy, k, c):           # due cursori
    for dy, dx in ((-0.22, 0.16), (0.22, -0.18)):
        p.linea([(cx - k * 0.42, cy + dy * k), (cx + k * 0.42, cy + dy * k)], k * 0.09, c)
        p.tondo(cx + dx * k, cy + dy * k, k * 0.14, c)


def ic_spunta(p, cx, cy, k, c):
    p.linea([(cx - k * 0.34, cy + k * 0.02), (cx - k * 0.1, cy + k * 0.28), (cx + k * 0.36, cy - k * 0.26)], k * 0.14, c)


def ic_carica(p, cx, cy, k, c, su=False):
    ic_freccia(p, cx, cy - k * 0.26, k * 0.5, c, su)
    ic_ugello(p, cx, cy + k * 0.28, k * 0.5, c)


def ic_scarica(p, cx, cy, k, c):
    ic_carica(p, cx, cy, k, c, True)


# ---- pezzi ----

def pillola(p, x, cy, testo, colore, a_destra=False, fondo=SFONDO_ALTO):
    w = larghezza(testo, 13, "Semibold") + 34
    x0 = x - w if a_destra else x
    p.rett(x0, cy - 13, x0 + w, cy + 13, 13, mescola(colore, fondo, 0.16))
    p.tondo(x0 + 14, cy, 4, colore)
    p.scrivi(x0 + 24, cy, testo, 13, "Semibold", colore, "lm")


def mattonella(p, x0, y0, x1, y1, testo, colore, icona, premuto=False):
    fondo = chiaro(CARTA) if premuto else CARTA
    p.rett(x0, y0, x1, y1, 20, fondo, BORDO)
    p.tondo(x0 + 32, y0 + 28, 17, mescola(colore, fondo, 0.18))
    icona(p, x0 + 32, y0 + 28, 19, colore)
    p.scrivi(x0 + 16, y1 - 13, testo, 15, "Semibold", TESTO, "ls")
    controlla_spazio(testo, larghezza(testo, 15, "Semibold"), x1 - x0 - 16 - 12)


def mattonella_bassa(p, x0, y0, x1, y1, testo, colore, icona, premuto=False):
    """Come mattonella, ma l'icona a sinistra e la scritta accanto: per le file da 48 px della pagina pronta."""
    fondo = chiaro(CARTA) if premuto else CARTA
    cy = (y0 + y1) / 2
    p.rett(x0, y0, x1, y1, 16, fondo, BORDO)
    p.tondo(x0 + 26, cy, 15, mescola(colore, fondo, 0.18))
    icona(p, x0 + 26, cy, 17, colore)
    spazio = x1 - x0 - 49 - 8
    px = next((px for px in (15, 14, 13) if larghezza(testo, px, "Semibold") <= spazio), 13)  # parole lunghe: un po' piu' piccole
    p.scrivi(x0 + 49, cy, testo, px, "Semibold", TESTO, "lm")
    controlla_spazio(testo, larghezza(testo, px, "Semibold"), spazio)


def bottone_icona(p, x0, y0, x1, y1, icona, colore, fondo=CARTA, premuto=False):
    p.rett(x0, y0, x1, y1, 16, chiaro(fondo) if premuto else fondo, BORDO)
    icona(p, (x0 + x1) / 2, (y0 + y1) / 2, 22, colore)


MARGINE_BOTTONE = 12   # px liberi in tutto ai lati della scritta (con l'icona) di un bottone


def con_icona(testo, px, k, larghezza_bottone):
    # la scritta ci sta nel bottone anche con l'icona accanto? (stessi conti di bottone())
    return larghezza(testo, px, "Semibold") + round(k * 28 / 18) <= larghezza_bottone - MARGINE_BOTTONE


def bottone(p, x0, y0, x1, y1, testo, colore, icona=None, fondo=None, colore_testo=None, px=16, premuto=False,
            k=18):
    f = fondo or mescola(colore, SFONDO_BASSO, 0.17)
    p.rett(x0, y0, x1, y1, 16, chiaro(f) if premuto else f)
    spazio = round(k * 28 / 18)
    w = larghezza(testo, px, "Semibold") + (spazio if icona else 0)
    controlla_spazio(testo, w, x1 - x0 - MARGINE_BOTTONE)
    x, cy = (x0 + x1) / 2 - w / 2, (y0 + y1) / 2
    if icona:
        icona(p, x + k / 2, cy, k, colore)
        x += spazio
    p.scrivi(x, cy, testo, px, "Semibold", colore_testo or colore, "lm")


def segmenti(p, x0, y0, x1, y1, voci, scelta, premuto=False):
    """scelta None = nessuna voce scelta; premuto = tutte accese (per l'immagine del tocco)."""
    p.rett(x0, y0, x1, y1, 14, CARTA, BORDO)
    w = (x1 - x0 - 8) / len(voci)
    for i, v in enumerate(voci):
        a = x0 + 4 + i * w
        if premuto:
            p.rett(a, y0 + 4, a + w, y1 - 4, 11, chiaro((54, 62, 78)))
        elif i == scelta:
            p.rett(a, y0 + 4, a + w, y1 - 4, 11, (54, 62, 78))
        p.scrivi(a + w / 2, (y0 + y1) / 2, v, 14, "Semibold", TESTO if (i == scelta or premuto) else GRIGIO, "mm")
        controlla_spazio(v, larghezza(v, 14, "Semibold"), w - 8)
