"""Genera la DWIN_SET della grafica nuova dello schermo FLSUN SR (DWIN T5L, DGUS II, 320×480), 2026-10-18.

Le pagine disegnate con grafica.py diventano i file che lo schermo legge dalla sua microSD:
  32.icl          sfondi: la pagina n è l'immagine n; l'immagine 20+n è la stessa pagina coi pulsanti premuti
  NN.icl          le parti che cambiano, come icone ritagliate dallo stesso sfondo: Klipper scrive un valore e lo
                  schermo mostra l'icona giusta (un valore fuori scala la nasconde e si vede lo sfondo)
  14ShowFile.bin  cosa mostra ogni pagina (icone variabili e due testi per il nome del file)
  13TouchFile.bin i pulsanti: ognuno scrive il suo codice in VP_TASTO (0x5000) e, se serve, cambia pagina da solo
Niente 22_Config.bin, T5LCFG.CFG né sistema operativo: restano quelli che ci sono.

Due tipi di parti che cambiano:
  Icona  un'immagine per valore: pillole di stato, anello 0…100 %, barre, obiettivi «→ 220°» della pagina pronta
  Riga   numeri composti cifra per cifra: ogni cella è un'icona con le cifre 0–9 e pochi segni, disegnati sullo
         sfondo di quel punto; le parti fisse («:», «°», « m») stanno nello sfondo. Con un'immagine per valore
         (temperature 0…300, metri di filo…) servivano ~11 MB e lo schermo ne ha 8 liberi.
         Le cifre hanno tutte la larghezza della più larga (come le cifre tabellari), così non ballano.

Formati letti dalla DWIN_SET originale FLSUN v1.3: vedi le funzioni sotto. I pulsanti usano
il tocco «regolazione incrementale» (0xFE02, quello dei tasti ▲▼ della ventola FLSUN): +codice<<8 con massimo
codice<<8, cioè scrive sempre codice<<8. Il codice sta nel byte alto perché la scheda, con la seriale dei TMC, legge
solo il primo byte dei dati.

Uso:  python3 genera.py               tutte le lingue di testi.py (serve Pillow)
      python3 genera.py --lingua en   una sola
      → lingue/xx/DWIN_SET/ (da copiare sulla microSD), lingue/xx/mappa.json (VP, pagine, campi, codici dei tasti e
        messaggi per schermo_flsun.py), verifica/xx/ (pagine ricomposte dai file, con valori d'esempio)
"""
import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

from grafica import (A, ACQUA, BLU, BORDO, CARTA, GIALLO, GRIGIO, L, PIATTO_C, ROSSO, S, SCURO, SFONDO_BASSO,
                     SPENTO, STRETTI, TESTO, UGELLO_C, VERDE, VIOLA, QUI, Pagina, bottone, carattere, chiaro, con_icona,
                     ic_bobina,
                     ic_carica, ic_casa, ic_croce, ic_fiamma, ic_fiocco, ic_freccia, ic_indietro, ic_pausa,
                     ic_piatto, ic_play, ic_regola, ic_scarica, ic_spunta, ic_su, ic_ugello, larghezza,
                     mattonella_bassa, bottone_icona, mescola, pillola, ritaglio, segmenti)

from testi import DECIMALE, LINGUE, MESSAGGI, TRADUZIONI  # noqa: E402

LINGUA = os.environ.get("FLSUN_LINGUA", "it")
DEC = DECIMALE[LINGUA]


def _(t):
    # il testo italiano nella lingua scelta (manca la traduzione: KeyError, meglio che una pagina mezza italiana)
    return t if LINGUA == "it" else TRADUZIONI[LINGUA][t]


CARTELLA = os.path.join(QUI, "lingue", LINGUA)
USCITA = os.path.join(CARTELLA, "DWIN_SET")
VERIFICA = os.path.join(QUI, "verifica", LINGUA)
QUALITA = 95                              # JPEG 4:4:4 come gli ICL FLSUN (che usano 90–100)
BLOCCO = 256 * 1024                       # la memoria dello schermo è a blocchi da 256 kB: il file n parte dal blocco n
ULTIMO_BLOCCO = 63                        # 16 MB
MAX_ICL = 1.6e6                           # file di icone non più grandi di quelli FLSUN (il più grande è 1,9 MB)

# ---- pagine, VP e tasti (schermo_flsun.py li legge da mappa.json) ----
AVVIO, PRONTA, STAMPA, PAUSA, FILAMENTO, FINITA, PRERISCALDA, ANNULLA, FILE, AVVIA, REGOLA = range(11)
PAGINE = dict(AVVIO=AVVIO, PRONTA=PRONTA, STAMPA=STAMPA, PAUSA=PAUSA, FILAMENTO=FILAMENTO, FINITA=FINITA,
              PRERISCALDA=PRERISCALDA, ANNULLA=ANNULLA, FILE=FILE, AVVIA=AVVIA, REGOLA=REGOLA)
PREMUTA = 20                               # immagine dei pulsanti premuti della pagina n: 20 + n
NASCOSTO = 0x00FF                          # valore fuori scala per tutte le icone: si vede lo sfondo

VP = dict(
    TASTO=0x5000,          # i pulsanti ci scrivono codice<<8; Klipper legge e rimette 0
    VIVO=0x5001,           # Klipper ci scrive 0x5A00: se lo rilegge 0, lo schermo si è riavviato
    STATO=0x5010,          # pillola della pagina pronta (STATI)
    UGELLO_OB=0x5012, UGELLO_BARRA=0x5013,     # pagina pronta: «→ 220°» (0 = spento); 0…100 % del riscaldamento
    PIATTO_OB=0x5015, PIATTO_BARRA=0x5016,
    FASE=0x5020,           # pillola della stampa (FASI)
    ANELLO=0x5021,         # 0…100 %
    MATERIALE=0x5030,      # 0 nessuno, 1 PLA, 2 PETG
    SOTTO_FILO=0x5033,     # SOTTO_FILO
    PASSO=0x5035,          # 0 = 1 mm, 1 = 10 mm, 2 = 50 mm
    RAFFREDDA_BARRA=0x5045,  # fine stampa: 0…100 % del raffreddamento del piatto
    SOTTO_PIATTO=0x5046,   # 0 si raffredda, 1 puoi staccarlo
    NOME=0x5100,           # nome del file, 30 caratteri (pagina stampa)
    NOME_CENTRO=0x5120,    # nome del file centrato con spazi (pagina finita)
    GRUPPO=0x5050,         # elenco dei file: 0 Recenti, 1 A–Z
    CARTA_RIGA=0x5051,     # … +i: riquadro della riga i (0 c'è, NASCOSTO no)
    ZPASSO=0x5060,         # regola: 0 = 0,01 mm, 1 = 0,02, 2 = 0,05
    SOTTO_Z=0x5061,        # regola: SOTTO_Z
    LISTA_NOME=0x5300,     # … +0x20*i: nome del file della riga i
    LISTA_INFO=0x5310,     # … +0x20*i: «4h 40m  130 g  PLA»
    LISTA_PAGINA=0x53A0,   # «2/6» centrato con spazi
    AVVIA_NOME=0x5400, AVVIA_NOME2=0x5410,     # nome del file su due righe
    AVVIA_DURATA=0x5420, AVVIA_FILO=0x5428, AVVIA_MATERIALE=0x5430, AVVIA_TEMP=0x5438,
    AVVIA_MSG=0x5440, AVVIA_MSG2=0x5450,       # due righe grigie: «Controllo la stampante…»
    AVVIA_ERR=0x5460, AVVIA_ERR2=0x5470,       # le stesse in rosso, per gli errori (una coppia o l'altra)
)
RIGHE_LISTA = 5
VP_CELLE = 0x5200          # da qui le celle delle righe, in fila
STATI = [(_("Pronta"), VERDE), (_("Scalda"), UGELLO_C), (_("In temperatura"), UGELLO_C), (_("Si raffredda"), PIATTO_C),
         (_("Occupata"), VIOLA), (_("Errore"), ROSSO)]
FASI = [(_("In stampa"), ACQUA), (_("Scalda"), UGELLO_C), (_("In pausa"), GIALLO)]
SOTTO_FILO = [(_("Spento"), SPENTO), (_("Scaldo…"), UGELLO_C), (_("Pronto"), VERDE), (_("Carico…"), VIOLA),
              (_("Scarico…"), VIOLA), (_("Muovo…"), VIOLA), (_("Errore"), ROSSO)]
SOTTO_PIATTO = [(_("Si raffredda"), PIATTO_C), (_("Puoi staccarlo"), VERDE)]
GRUPPI_FILE = (_("Recenti"), "A–Z")
SOTTO_Z = [(_("Solo per questa stampa"), GRIGIO), (_("Al limite"), GIALLO), (_("Errore"), ROSSO)]
TASTI = dict(RAFFREDDA=1, AZZERA=2, PRERISCALDA_PLA=3, PRERISCALDA_PETG=4, PLA=5, PETG=6, CARICA=7, SCARICA=8,
             PASSO_1=9, PASSO_10=10, PASSO_50=11, ARRETRA=12, AVANZA=13, FATTO=14, PAUSA=15, RIPRENDI=16,
             ANNULLA_SI=17, OK=18, FILAMENTO_INDIETRO=19, ANNULLA_NO=20, APRI_FILAMENTO=21, APRI_PRERISCALDA=22,
             PRERISCALDA_INDIETRO=23, APRI_ANNULLA=24, APRI_FILE=25, FILE_INDIETRO=26, GRUPPO_RECENTI=27,
             GRUPPO_AZ=28, FILE_SU=29, FILE_GIU=30, FILE_1=31, FILE_2=32, FILE_3=33, FILE_4=34, FILE_5=35,
             AVVIA_SI=36, AVVIA_NO=37, APRI_REGOLA=38, REGOLA_INDIETRO=39, Z_GIU=40, Z_SU=41, ZPASSO_1=42,
             ZPASSO_2=43, ZPASSO_3=44)
LUNGHEZZA_NOME = 30
COLONNE_NOME = 26          # caratteri da 11 px che stanno nei 288 px del riquadro
GRUPPI = {PRONTA: "pronta", STAMPA: "stampa", PAUSA: "stampa", FILAMENTO: "filamento", FINITA: "finita",
          REGOLA: "regola"}

TRATTINO = "–"
CIFRE = list("0123456789") + [TRATTINO]
GRADI = "°"
BARRA = "/ "


@dataclass
class Icona:
    vp: int
    box: tuple             # x, y, larghezza, altezza
    valori: range
    disegna: object        # disegna(p, v)


@dataclass
class Testo:
    vp: int
    box: tuple             # x0, y0, x1, y1
    colore: tuple
    lunghezza: int = LUNGHEZZA_NOME       # caratteri (da 11 px: il riquadro deve starci)


@dataclass
class Tasto:
    box: tuple             # x0, y0, x1, y1
    codice: int
    dopo: int = None       # pagina da mostrare subito (lo fa lo schermo da solo)


@dataclass
class Celle:
    nome: str
    n: int
    glifi: list
    allinea: str = "dx"    # dove va un testo più corto delle celle: "sx" o "dx"
    aria: int = 0          # pixel in più per cella (sul nero il bordo sfumato di una cifra larga, il «4», toccava)


@dataclass
class Riga:
    x: float
    base: float            # linea di base
    px: int
    peso: str
    colore: tuple
    parti: list            # Celle o testo fisso
    ancora: str = "l"      # x è l'inizio (l), il centro (m) o la fine (r) della riga
    margine: int = 2       # pixel sopra e sotto il disegno (la riduzione sfuma i bordi di un pixel)

    def disponi(self):
        """[(parte, x, larghezza)]: per le celle x è intero e la larghezza è quella di una cella."""
        misure = []
        for parte in self.parti:
            if isinstance(parte, str):
                misure.append((parte, larghezza(parte, self.px, self.peso)))
            else:
                misure.append((parte, math.ceil(max(larghezza(g, self.px, self.peso) for g in parte.glifi)) + parte.aria))
        tot = sum(w if isinstance(p, str) else w * p.n for p, w in misure)
        x = self.x - {"l": 0, "m": tot / 2, "r": tot}[self.ancora]
        out = []
        for p, w in misure:
            if isinstance(p, str):
                out.append((p, x, w))
                x += w
            else:
                x = round(x)
                out.append((p, x, w))
                x += w * p.n
        return out

    def verticale(self):
        f = carattere(self.px, self.peso)
        b = [f.getbbox(g, anchor="ls") for p in self.parti if isinstance(p, Celle) for g in p.glifi]
        return (math.floor(self.base + min(x[1] for x in b) / S) - self.margine,
                math.ceil(self.base + max(x[3] for x in b) / S) + self.margine)

    def fissi(self, p):
        for parte, x, w in self.disponi():
            if isinstance(parte, str):
                p.scrivi(x, self.base, parte, self.px, self.peso, self.colore, "ls")

    def icone(self, vp_di):
        y0, y1 = self.verticale()
        out = []
        for parte, x, w in self.disponi():
            if isinstance(parte, str):
                continue
            for i in range(parte.n):
                def disegna(p, v, cx=x + i * w, w=w, glifi=parte.glifi):
                    g = glifi[v]
                    if g in CIFRE:                # cifre e trattino al centro della cella, gli altri segni a sinistra
                        p.scrivi(cx + w / 2, self.base, g, self.px, self.peso, self.colore, "ms")
                    else:                         # 1 px dal bordo: la riduzione sfuma il segno di un pixel
                        p.scrivi(cx + 1, self.base, g, self.px, self.peso, self.colore, "ls")
                out.append(Icona(vp_di[parte.nome] + i, (x + i * w, y0, w, y1 - y0), range(len(parte.glifi)),
                                 disegna))
        return out


@dataclass
class Schermata:
    numero: int
    statico: object        # statico(p, premuto)
    icone: list = field(default_factory=list)
    righe: list = field(default_factory=list)
    testi: list = field(default_factory=list)
    tasti: list = field(default_factory=list)


# ---- parti che cambiano: icone intere ----

def obiettivo_pillola(y, colore, massimo):
    def disegna(p, v):
        if v == 0:
            p.scrivi(284, y + 16, _("spento"), 14, "Medium", SPENTO, "rm")
            return
        t = f"→ {v}°"
        w = larghezza(t, 15, "Semibold") + 22
        p.rett(288 - w, y + 3, 288, y + 29, 13, mescola(colore, CARTA, 0.16))
        p.scrivi(288 - w / 2, y + 16, t, 15, "Semibold", colore, "mm")
    return Icona(0, (176, y, 120, 32), range(0, massimo + 1), disegna)


def barra(y, colore):
    return Icona(0, (34, y, 252, 10), range(0, 101), lambda p, v: p.barra(36, y + 2, 284, v / 100, colore))


def pillola_stato(stati, x, cy, a_destra, box):
    return Icona(0, box, range(len(stati)),
                 lambda p, v: pillola(p, x, cy, stati[v][0], stati[v][1], a_destra=a_destra))


def sottotitolo(stati, x, cy, w):
    return Icona(0, (x - 2, cy - 9, w, 18), range(len(stati)),
                 lambda p, v: p.scrivi(x, cy, stati[v][0], 13, "Medium", stati[v][1], "lm"))


def con_vp(icona, vp):
    icona.vp = vp
    return icona


# ---- parti che cambiano: righe di cifre ----

def tempo(x, base, ore, minuti):           # «1:12»
    return Riga(x, base, 19, "Semibold", TESTO, [Celle(ore, 2, CIFRE), ":", Celle(minuti, 2, CIFRE, "sx")], "m")


def filo(x, base):                         # «12,4 m»
    return Riga(x, base, 19, "Semibold", TESTO, [Celle("filo_m", 3, CIFRE), DEC, Celle("filo_dec", 1, CIFRE, "sx"),
                                                 " m"], "m")


def obiettivo(x, base, px, nome):          # «/ 220°», sparisce tutto con l'obiettivo a 0
    return Riga(x, base, px, "Medium", GRIGIO, [Celle(nome, 5, CIFRE + [GRADI, BARRA], "sx")])


# ---- pagine ----

def intestazione(p, titolo, premuto):
    p.tondo(34, 30, 18, chiaro(CARTA) if premuto else CARTA, BORDO, 1)
    ic_indietro(p, 34, 30, 18, TESTO)
    p.scrivi(62, 30, titolo, 21, "Bold", TESTO, "lm")


def carta_temperatura(p, x0, y0, x1, y1, nome, colore, icona):
    p.rett(x0, y0, x1, y1, 20, CARTA, BORDO)
    p.tondo(x0 + 32, y0 + 32, 18, mescola(colore, CARTA, 0.18))
    icona(p, x0 + 32, y0 + 32, 20, colore)
    p.scrivi(x0 + 60, y0 + 32, nome, 15, "Semibold", GRIGIO, "lm")


def statico_avvio(p, premuto):
    w = larghezza("FLSUN ", 34, "Heavy") + larghezza("SR", 34, "Heavy")
    x = 160 - w / 2
    p.scrivi(x, 220, "FLSUN", 34, "Heavy", TESTO, "lm")
    p.scrivi(x + larghezza("FLSUN ", 34, "Heavy"), 220, "SR", 34, "Heavy", GRIGIO, "lm")
    p.scrivi(160, 262, _("Avvio di Klipper…"), 15, "Medium", GRIGIO, "mm")


def statico_pronta(p, premuto):
    p.scrivi(18, 30, "FLSUN", 17, "Heavy", TESTO, "lm")
    p.scrivi(18 + larghezza("FLSUN ", 17, "Heavy"), 30, "SR", 17, "Heavy", GRIGIO, "lm")
    carta_temperatura(p, 16, 54, 304, 174, _("Ugello"), UGELLO_C, ic_ugello)
    carta_temperatura(p, 16, 184, 304, 304, _("Piatto"), PIATTO_C, ic_piatto)
    # 2026-10-19 «Stampa un file»: le quattro mattonelle diventano basse (icona accanto alla scritta)
    bottone(p, 16, 314, 304, 360, _("Stampa un file"), SCURO, ic_play, fondo=ACQUA, px=17, premuto=premuto)
    mattonella_bassa(p, 16, 368, 156, 416, _("Filamento"), VIOLA, ic_bobina, premuto)
    mattonella_bassa(p, 164, 368, 304, 416, _("Preriscalda"), UGELLO_C, ic_fiamma, premuto)
    mattonella_bassa(p, 16, 422, 156, 470, _("Raffredda"), PIATTO_C, ic_fiocco, premuto)
    mattonella_bassa(p, 164, 422, 304, 470, _("Azzera"), VERDE, ic_casa, premuto)


def statico_stampa_comune(p, premuto, etichette=(_("Trascorso"), _("Resta"), _("Filo"))):
    y0, y1 = 298, 354
    p.rett(16, y0, 304, y1, 18, CARTA, BORDO)
    for i, et in enumerate(etichette):
        p.scrivi(16 + 48 + i * 96, y0 + 17, et, 12, "Medium", GRIGIO, "mm")
        if i:
            p.linea([(16 + i * 96, y0 + 14), (16 + i * 96, y1 - 14)], 1, BORDO)
    for x0, icona, colore in ((16, ic_ugello, UGELLO_C), (164, ic_piatto, PIATTO_C)):
        p.rett(x0, 364, x0 + 140, 408, 16, CARTA, BORDO)
        p.tondo(x0 + 24, 386, 14, mescola(colore, CARTA, 0.18))
        icona(p, x0 + 24, 386, 16, colore)


TRE_BOTTONI = ((16, 106), (115, 205), (214, 304))


def statico_stampa(p, premuto):
    statico_stampa_comune(p, premuto)
    testi = (_("Pausa"), _("Regola"), _("Annulla"))
    # se in questa lingua una scritta non ci sta con l'icona, tutta la fila va senza icone (meglio che a pezzi)
    icone = all(con_icona(t, 14, 15, x1 - x0) for (x0, x1), t in zip(TRE_BOTTONI, testi))
    for (x0, x1), testo, colore, icona in zip(TRE_BOTTONI, testi, (GIALLO, ACQUA, ROSSO),
                                              (ic_pausa, ic_regola, ic_croce)):
        bottone(p, x0, 418, x1, 468, testo, colore, icona if icone else None, px=14, k=15, premuto=premuto)


def statico_pausa(p, premuto):
    statico_stampa_comune(p, premuto)
    bottone(p, 16, 418, 156, 468, _("Riprendi"), VERDE, ic_play, premuto=premuto)
    bottone(p, 164, 418, 304, 468, _("Annulla"), ROSSO, ic_croce, premuto=premuto)


def statico_filamento(p, premuto):
    intestazione(p, _("Filamento"), premuto)
    y0, y1 = 110, 190
    p.rett(16, y0, 304, y1, 20, CARTA, BORDO)
    p.tondo(48, y0 + 32, 18, mescola(UGELLO_C, CARTA, 0.18))
    ic_ugello(p, 48, y0 + 32, 20, UGELLO_C)
    p.scrivi(76, y0 + 24, _("Ugello"), 15, "Semibold", GRIGIO, "lm")
    for x0, testo, icona, primo in ((16, _("Carica"), ic_carica, True), (164, _("Scarica"), ic_scarica, False)):
        x1 = x0 + 140
        f = mescola(VIOLA, CARTA, 0.2) if primo else CARTA
        p.rett(x0, 200, x1, 290, 20, chiaro(f) if premuto else f, mescola(VIOLA, BORDO, 0.45) if primo else BORDO)
        icona(p, (x0 + x1) / 2, 232, 44, VIOLA if primo else TESTO)
        p.scrivi((x0 + x1) / 2, 274, testo, 17, "Bold", TESTO, "ms")
    p.scrivi(20, 312, _("Muovi il filo"), 13, "Semibold", GRIGIO, "lm")
    bottone(p, 16, 368, 156, 420, _("Arretra"), TESTO, ic_su, fondo=CARTA, premuto=premuto)
    bottone(p, 164, 368, 304, 420, _("Avanza"), TESTO, ic_freccia, fondo=CARTA, premuto=premuto)
    bottone(p, 16, 430, 304, 470, _("Fatto · spegni l'ugello"), GRIGIO, fondo=mescola(CARTA, SFONDO_BASSO, 0.6), px=15,
            premuto=premuto)
    if premuto:            # i segmenti cambiano (sono icone), ma premuti si accendono tutti
        segmenti(p, 16, 60, 304, 100, ("PLA · 220°", "PETG · 250°"), None, True)
        segmenti(p, 16, 324, 304, 360, ("1 mm", "10 mm", "50 mm"), None, True)


def statico_finita(p, premuto):
    cx, cy = 160, 104
    p.tondo(cx, cy, 44, VERDE)
    ic_spunta(p, cx, cy, 52, SCURO)
    p.scrivi(cx, 186, _("Stampa finita"), 27, "Bold", TESTO, "ms")
    for x0, et in ((16, _("Durata")), (164, _("Filo"))):
        p.rett(x0, 232, x0 + 140, 290, 18, CARTA, BORDO)
        p.scrivi(x0 + 70, 249, et, 12, "Medium", GRIGIO, "mm")
    y0, y1 = 300, 404
    p.rett(16, y0, 304, y1, 20, CARTA, BORDO)
    p.tondo(48, y0 + 32, 18, mescola(PIATTO_C, CARTA, 0.18))
    ic_piatto(p, 48, y0 + 32, 20, PIATTO_C)
    p.scrivi(76, y0 + 24, _("Piatto"), 15, "Semibold", GRIGIO, "lm")
    p.scrivi(160, y1 - 13, _("Il pezzo si stacca sotto {t}°").format(t=32), 13, "Medium", GRIGIO, "ms")
    bottone(p, 16, 418, 304, 468, "OK", SCURO, fondo=VERDE, px=18, premuto=premuto)


def statico_preriscalda(p, premuto):
    intestazione(p, _("Preriscalda"), premuto)
    for y0, nome, ug, pi in ((64, "PLA", 220, 60), (176, "PETG", 250, 80)):
        f = chiaro(CARTA) if premuto else CARTA
        p.rett(16, y0, 304, y0 + 100, 20, f, BORDO)
        p.tondo(56, y0 + 50, 24, mescola(UGELLO_C, f, 0.18))
        ic_fiamma(p, 56, y0 + 50, 26, UGELLO_C, fondo=f)
        p.scrivi(96, y0 + 38, nome, 24, "Bold", TESTO, "lm")
        p.scrivi(96, y0 + 66, _("Ugello {u}° · Piatto {p}°").format(u=ug, p=pi), 14, "Medium", GRIGIO, "lm")
    f = chiaro(CARTA) if premuto else CARTA
    p.rett(16, 288, 304, 348, 20, f, BORDO)
    p.tondo(56, 318, 18, mescola(PIATTO_C, f, 0.18))
    ic_fiocco(p, 56, 318, 19, PIATTO_C)
    p.scrivi(90, 318, _("Spegni tutto"), 17, "Semibold", TESTO, "lm")
    bottone(p, 16, 418, 304, 468, _("Annulla"), GRIGIO, fondo=mescola(CARTA, SFONDO_BASSO, 0.6), px=16, premuto=premuto)


def statico_annulla(p, premuto):
    cx, cy = 160, 118
    p.tondo(cx, cy, 44, ROSSO)
    ic_croce(p, cx, cy, 44, (40, 8, 10))
    p.scrivi(cx, 212, _("Annullare la stampa?"), 23, "Bold", TESTO, "ms")
    p.scrivi(cx, 244, _("La stampa si ferma e"), 15, "Medium", GRIGIO, "ms")
    p.scrivi(cx, 266, _("non si può riprendere."), 15, "Medium", GRIGIO, "ms")
    bottone(p, 16, 360, 304, 410, _("Sì, annulla"), (40, 8, 10), fondo=ROSSO, px=17, premuto=premuto)
    bottone(p, 16, 418, 304, 468, _("No, continua"), TESTO, fondo=CARTA, px=17, premuto=premuto)


def y_riga(i):                             # elenco dei file: riquadro della riga i
    return 108 + i * 61


def statico_file(p, premuto):
    intestazione(p, _("Stampa"), premuto)
    bottone_icona(p, 16, 418, 96, 468, ic_su, TESTO, premuto=premuto)
    bottone_icona(p, 224, 418, 304, 468, ic_freccia, TESTO, premuto=premuto)
    if premuto:            # gruppi e righe sono icone: premuti si accendono tutti
        segmenti(p, 16, 58, 304, 98, GRUPPI_FILE, None, True)
        for i in range(RIGHE_LISTA):
            carta_riga(p, i, True)


def carta_riga(p, i, premuto=False):
    y = y_riga(i)
    p.rett(16, y, 304, y + 56, 14, chiaro(CARTA) if premuto else CARTA, BORDO)


def statico_avvia(p, premuto):
    intestazione(p, _("Stampa"), premuto)
    p.rett(16, 58, 304, 120, 18, CARTA, BORDO)
    for x0, y0, et in ((16, 128, _("Durata")), (164, 128, _("Filo")), (16, 192, _("Materiale")), (164, 192, _("Ugello / Piatto"))):
        p.rett(x0, y0, x0 + 140, y0 + 56, 16, CARTA, BORDO)
        p.scrivi(x0 + 14, y0 + 15, et, 12, "Medium", GRIGIO, "lm")
    p.scrivi(160, 282, _("Il piatto è libero e pulito?"), 18, "Bold", TESTO, "mm")
    bottone(p, 16, 360, 304, 410, _("Sì, stampa"), SCURO, ic_play, fondo=VERDE, px=17, premuto=premuto)
    bottone(p, 16, 418, 304, 468, _("Annulla"), GRIGIO, fondo=mescola(CARTA, SFONDO_BASSO, 0.6), px=16, premuto=premuto)


def statico_regola(p, premuto):
    intestazione(p, _("Regola"), premuto)
    y0, y1 = 58, 200
    p.rett(16, y0, 304, y1, 20, CARTA, BORDO)
    p.tondo(48, y0 + 32, 18, mescola(UGELLO_C, CARTA, 0.18))
    ic_ugello(p, 48, y0 + 32, 20, UGELLO_C)
    p.scrivi(76, y0 + 32, _("Altezza dell'ugello"), 15, "Semibold", GRIGIO, "lm")
    for x0, testo, sotto, icona in ((16, _("Abbassa"), _("schiaccia di più"), ic_freccia),
                                    (164, _("Alza"), _("schiaccia di meno"), ic_su)):
        x1 = x0 + 140
        f = chiaro(CARTA) if premuto else CARTA
        p.rett(x0, 210, x1, 300, 20, f, BORDO)
        icona(p, (x0 + x1) / 2, 236, 30, ACQUA)
        p.scrivi((x0 + x1) / 2, 274, testo, 17, "Bold", TESTO, "ms")
        p.scrivi((x0 + x1) / 2, 291, sotto, 12, "Medium", GRIGIO, "ms")
    unita_z(p)
    p.scrivi(20, 316, _("Passo"), 13, "Semibold", GRIGIO, "lm")
    bottone(p, 16, 418, 304, 468, _("Torna alla stampa"), GRIGIO, fondo=mescola(CARTA, SFONDO_BASSO, 0.6), px=16,
            premuto=premuto)
    if premuto:
        segmenti(p, 16, 328, 304, 364, PASSI_Z, None, True)


PASSI_Z = tuple("0%s0%d mm" % (DEC, n) for n in (1, 2, 5))
RIGA_Z = Riga(146, 160, 44, "Bold", TESTO, [Celle("z_segno", 1, ["+", "−"]), Celle("z_int", 1, CIFRE), DEC,
                                            Celle("z_dec", 2, CIFRE, "sx")], "m")


def unita_z(p):
    parte, x, w = RIGA_Z.disponi()[-1]
    p.scrivi(x + w * parte.n + 6, 160, "mm", 20, "Semibold", GRIGIO, "ls")


def anello_percento(p, v):
    # niente aloni sfumati (2026-10-18): sullo schermo vero ogni sfumatura scura diventa a gradini
    cx, cy, rr, sp = 160, 186, 86, 14
    fr = v / 100
    p.anello(cx, cy, rr, sp, fr, ACQUA, BLU)
    n = str(v)
    px = 60 if v < 100 else 52
    w = larghezza(n, px, "Bold") + 2 + larghezza("%", 26, "Semibold")
    p.scrivi(cx - w / 2, cy + 20, n, px, "Bold", TESTO, "ls")
    p.scrivi(cx - w / 2 + larghezza(n, px, "Bold") + 2, cy + 20, "%", 26, "Semibold", GRIGIO, "ls")


def schermate():
    nome_stampa = Testo(VP["NOME"], (16, 50, 304, 74), TESTO)
    icone_stampa = [con_vp(pillola_stato(FASI, 16, 28, False, (12, 12, 160, 34)), VP["FASE"]),
                    Icona(VP["ANELLO"], (44, 80, 232, 214), range(0, 101), anello_percento)]
    righe_stampa = [
        Riga(304, 33, 14, "Semibold", GRIGIO, [_("fine "), Celle("fine_ore", 2, CIFRE, aria=1), ":",
                                               Celle("fine_min", 2, CIFRE, "sx", aria=1)], "r"),
        tempo(64, 346, "ore", "minuti"), tempo(160, 346, "resta_ore", "resta_min"), filo(256, 346),
        Riga(102, 393, 19, "Semibold", TESTO, [Celle("ugello", 3, CIFRE), GRADI], "r"),
        obiettivo(106, 392, 13, "ugello_ob"),
        Riga(250, 393, 19, "Semibold", TESTO, [Celle("piatto", 3, CIFRE), GRADI], "r"),
        obiettivo(254, 392, 13, "piatto_ob"),
    ]
    (a0, a1), (b0, b1), (c0, c1) = TRE_BOTTONI
    tasti_stampa = [Tasto((a0, 418, a1, 468), TASTI["PAUSA"]), Tasto((b0, 418, b1, 468), TASTI["APRI_REGOLA"], REGOLA),
                    Tasto((c0, 418, c1, 468), TASTI["APRI_ANNULLA"], ANNULLA)]
    tasti_pausa = [Tasto((16, 418, 156, 468), TASTI["RIPRENDI"]), Tasto((164, 418, 304, 468), TASTI["APRI_ANNULLA"],
                                                                       ANNULLA)]
    tre = [(20 + i * (280 / 3), 20 + (i + 1) * (280 / 3)) for i in range(3)]
    grande = CIFRE + [GRADI]
    return [
        Schermata(AVVIO, statico_avvio),
        Schermata(PRONTA, statico_pronta, icone=[
            con_vp(pillola_stato(STATI, 304, 30, True, (140, 14, 168, 32)), VP["STATO"]),
            con_vp(obiettivo_pillola(70, UGELLO_C, 300), VP["UGELLO_OB"]),
            con_vp(barra(152, UGELLO_C), VP["UGELLO_BARRA"]),
            con_vp(obiettivo_pillola(200, PIATTO_C, 150), VP["PIATTO_OB"]),
            con_vp(barra(282, PIATTO_C), VP["PIATTO_BARRA"]),
        ], righe=[Riga(34, 150, 50, "Bold", TESTO, [Celle("ugello", 4, grande, "sx")], margine=1),
                  Riga(34, 280, 50, "Bold", TESTO, [Celle("piatto", 4, grande, "sx")], margine=1)],
                  tasti=[Tasto((16, 314, 304, 360), TASTI["APRI_FILE"], FILE),
                         Tasto((16, 368, 156, 416), TASTI["APRI_FILAMENTO"], FILAMENTO),
                         Tasto((164, 368, 304, 416), TASTI["APRI_PRERISCALDA"], PRERISCALDA),
                         Tasto((16, 422, 156, 470), TASTI["RAFFREDDA"]),
                         Tasto((164, 422, 304, 470), TASTI["AZZERA"])]),
        Schermata(STAMPA, statico_stampa, icone=icone_stampa, righe=righe_stampa, testi=[nome_stampa],
                  tasti=tasti_stampa),
        Schermata(PAUSA, statico_pausa, icone=icone_stampa, righe=righe_stampa, testi=[nome_stampa],
                  tasti=tasti_pausa),
        Schermata(FILAMENTO, statico_filamento, icone=[
            Icona(VP["MATERIALE"], (15, 59, 290, 42), range(0, 3),
                  lambda p, v: segmenti(p, 16, 60, 304, 100, ("PLA · 220°", "PETG · 250°"), v - 1 if v else None)),
            con_vp(sottotitolo(SOTTO_FILO, 76, 153, 76), VP["SOTTO_FILO"]),
            con_vp(barra(166, UGELLO_C), VP["UGELLO_BARRA"]),
            Icona(VP["PASSO"], (15, 323, 290, 38), range(0, 3),
                  lambda p, v: segmenti(p, 16, 324, 304, 360, ("1 mm", "10 mm", "50 mm"), v)),
        ], righe=[Riga(238, 152, 34, "Bold", TESTO, [Celle("ugello", 3, CIFRE), GRADI], "r"),
                  obiettivo(242, 152, 15, "ugello_ob")],
                  tasti=[Tasto((12, 10, 56, 50), TASTI["FILAMENTO_INDIETRO"], PRONTA),
                         Tasto((20, 64, 160, 96), TASTI["PLA"]), Tasto((160, 64, 300, 96), TASTI["PETG"]),
                         Tasto((16, 200, 156, 290), TASTI["CARICA"]), Tasto((164, 200, 304, 290), TASTI["SCARICA"])]
                 + [Tasto((round(a), 328, round(b), 356), c) for (a, b), c in
                    zip(tre, (TASTI["PASSO_1"], TASTI["PASSO_10"], TASTI["PASSO_50"]))]
                 + [Tasto((16, 368, 156, 420), TASTI["ARRETRA"]), Tasto((164, 368, 304, 420), TASTI["AVANZA"]),
                    Tasto((16, 430, 304, 470), TASTI["FATTO"], PRONTA)]),
        Schermata(FINITA, statico_finita, icone=[
            con_vp(sottotitolo(SOTTO_PIATTO, 76, 343, 120), VP["SOTTO_PIATTO"]),
            con_vp(barra(362, PIATTO_C), VP["RAFFREDDA_BARRA"]),
        ], righe=[tempo(86, 280, "ore", "minuti"), filo(234, 280),
                  Riga(288, 344, 34, "Bold", TESTO, [Celle("piatto", 3, CIFRE), GRADI], "r")],
                  testi=[Testo(VP["NOME_CENTRO"], (16, 196, 304, 218), GRIGIO)],
                  tasti=[Tasto((16, 418, 304, 468), TASTI["OK"], PRONTA)]),
        Schermata(PRERISCALDA, statico_preriscalda, tasti=[
            Tasto((12, 10, 56, 50), TASTI["PRERISCALDA_INDIETRO"], PRONTA),
            Tasto((16, 64, 304, 164), TASTI["PRERISCALDA_PLA"], PRONTA),
            Tasto((16, 176, 304, 276), TASTI["PRERISCALDA_PETG"], PRONTA),
            Tasto((16, 288, 304, 348), TASTI["RAFFREDDA"], PRONTA),
            Tasto((16, 418, 304, 468), TASTI["PRERISCALDA_INDIETRO"], PRONTA)]),
        Schermata(ANNULLA, statico_annulla, tasti=[Tasto((16, 360, 304, 410), TASTI["ANNULLA_SI"]),
                                                   Tasto((16, 418, 304, 468), TASTI["ANNULLA_NO"], STAMPA)]),
        # le righe non cambiano pagina da sole: Klipper scrive prima nome e dati del file, poi mostra «avvia»
        Schermata(FILE, statico_file, icone=[
            Icona(VP["GRUPPO"], (15, 57, 290, 42), range(0, 2),
                  lambda p, v: segmenti(p, 16, 58, 304, 98, GRUPPI_FILE, v))]
            + [Icona(VP["CARTA_RIGA"] + i, (15, y_riga(i) - 1, 290, 58), range(0, 1),
                     lambda p, v, i=i: carta_riga(p, i)) for i in range(RIGHE_LISTA)],
                  testi=[t for i in range(RIGHE_LISTA) for t in
                         (Testo(VP["LISTA_NOME"] + 0x20 * i, (28, y_riga(i) + 7, 300, y_riga(i) + 29), TESTO, 24),
                          Testo(VP["LISTA_INFO"] + 0x20 * i, (28, y_riga(i) + 31, 300, y_riga(i) + 53), GRIGIO, 24))]
                  + [Testo(VP["LISTA_PAGINA"], (112, 432, 208, 454), GRIGIO, 8)],
                  tasti=[Tasto((12, 10, 56, 50), TASTI["FILE_INDIETRO"], PRONTA),
                         Tasto((20, 62, 160, 94), TASTI["GRUPPO_RECENTI"]), Tasto((160, 62, 300, 94), TASTI["GRUPPO_AZ"])]
                  + [Tasto((16, y_riga(i), 304, y_riga(i) + 56), TASTI["FILE_%d" % (i + 1)]) for i in range(RIGHE_LISTA)]
                  + [Tasto((16, 418, 96, 468), TASTI["FILE_SU"]), Tasto((224, 418, 304, 468), TASTI["FILE_GIU"])]),
        Schermata(AVVIA, statico_avvia,
                  testi=[Testo(VP["AVVIA_NOME"], (28, 65, 300, 87), TESTO, 24),
                         Testo(VP["AVVIA_NOME2"], (28, 91, 300, 113), TESTO, 24),
                         Testo(VP["AVVIA_DURATA"], (30, 156, 152, 178), TESTO, 10),
                         Testo(VP["AVVIA_FILO"], (178, 156, 300, 178), TESTO, 10),
                         Testo(VP["AVVIA_MATERIALE"], (30, 220, 152, 242), TESTO, 10),
                         Testo(VP["AVVIA_TEMP"], (178, 220, 300, 242), TESTO, 10),
                         Testo(VP["AVVIA_MSG"], (17, 302, 303, 324), GRIGIO, 26),
                         Testo(VP["AVVIA_MSG2"], (17, 326, 303, 348), GRIGIO, 26),
                         Testo(VP["AVVIA_ERR"], (17, 302, 303, 324), ROSSO, 26),
                         Testo(VP["AVVIA_ERR2"], (17, 326, 303, 348), ROSSO, 26)],
                  tasti=[Tasto((12, 10, 56, 50), TASTI["AVVIA_NO"], FILE),
                         Tasto((16, 360, 304, 410), TASTI["AVVIA_SI"]),
                         Tasto((16, 418, 304, 468), TASTI["AVVIA_NO"], FILE)]),
        Schermata(REGOLA, statico_regola, icone=[
            con_vp(sottotitolo(SOTTO_Z, 34, 184, 250), VP["SOTTO_Z"]),
            Icona(VP["ZPASSO"], (15, 327, 290, 38), range(0, 3),
                  lambda p, v: segmenti(p, 16, 328, 304, 364, PASSI_Z, v))],
                  righe=[RIGA_Z],
                  tasti=[Tasto((12, 10, 56, 50), TASTI["REGOLA_INDIETRO"], STAMPA),
                         Tasto((16, 210, 156, 300), TASTI["Z_GIU"]), Tasto((164, 210, 304, 300), TASTI["Z_SU"])]
                  + [Tasto((round(a), 332, round(b), 360), c) for (a, b), c in
                     zip(tre, (TASTI["ZPASSO_1"], TASTI["ZPASSO_2"], TASTI["ZPASSO_3"]))]
                  + [Tasto((16, 418, 304, 468), TASTI["REGOLA_INDIETRO"], STAMPA)]),
    ]


def campi(pagine):
    """{gruppo: {nome: {vp, n, glifi, allinea}}}: le celle di ogni riga, coi VP in fila da VP_CELLE. Stampa e pausa
    hanno le stesse righe e quindi gli stessi VP."""
    out, vp = {}, VP_CELLE
    for s in pagine:
        g = out.setdefault(GRUPPI.get(s.numero), {})
        for riga in s.righe:
            for parte in riga.parti:
                if isinstance(parte, Celle) and parte.nome not in g:
                    g[parte.nome] = dict(vp=vp, n=parte.n, glifi=parte.glifi, allinea=parte.allinea)
                    vp += parte.n
    out.pop(None, None)
    return out


def valori_campo(campo, testo):
    """Il testo da mostrare in un campo → {vp: valore}. Fa lo stesso schermo_flsun.py: i glifi più lunghi per primi,
    i caratteri che non ci sono saltati, le celle vuote nascoste."""
    glifi = sorted(campo["glifi"], key=len, reverse=True)
    pezzi, i = [], 0
    while i < len(testo):
        for g in glifi:
            if testo.startswith(g, i):
                pezzi.append(campo["glifi"].index(g))
                i += len(g)
                break
        else:
            i += 1
    n = campo["n"]
    if campo["allinea"] == "sx":
        celle = (pezzi + [NASCOSTO] * n)[:n]
    else:
        celle = ([NASCOSTO] * n + pezzi)[-n:]
    return {campo["vp"] + k: v for k, v in enumerate(celle)}


# ---- formati DWIN ----

def jpeg(im):
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=QUALITA, subsampling=0, optimize=False, progressive=False)
    return b.getvalue()


def inizio_dati(j):
    """Posizione dei dati compressi, subito dopo l'intestazione SOS (il campo X degli ICL FLSUN)."""
    i = 2
    while True:
        assert j[i] == 0xFF, "JPEG strano"
        m, n = j[i + 1], int.from_bytes(j[i + 2:i + 4], "big")
        if m == 0xDA:
            return i + 2 + n
        i += 2 + n


def allinea(j, x, inizio):
    """Il JPEG j va scritto dal byte inizio del file e i suoi dati compressi partono da x: come fa il programma DWIN
    (tutte le immagini FLSUN), si allungano l'intestazione SOS di 0–3 byte 0xFF perché i dati partano a un indirizzo
    multiplo di 4. Un JPEG così non è più standard (libjpeg lo rifiuta): per rileggerlo c'è standard()."""
    pad = -(inizio + x) % 4
    if not pad:
        return j, x
    s = x - 14                                    # FF DA, lunghezza 12, 3 componenti
    assert j[s:s + 4] == b"\xff\xda\x00\x0c", j[s:s + 4]
    return j[:s + 2] + (12 + pad).to_bytes(2, "big") + j[s + 4:x] + b"\xff" * pad + j[x:], x + pad


def standard(j):
    """Toglie il riempimento di allinea() (anche dai JPEG FLSUN), per aprire l'immagine con PIL."""
    s = j.rfind(b"\xff\xda", 0, inizio_dati(j))
    n = int.from_bytes(j[s + 2:s + 4], "big")
    return j[:s + 2] + (12).to_bytes(2, "big") + j[s + 4:s + 14] + j[s + 2 + n:]


def icl(immagini):
    """{numero: immagine} → file .icl come quelli FLSUN: «DGUS_3», 2 byte di controllo (crc16(), byte basso
    prima), lunghezza−8, 0x04, quante immagini, 0; poi l'indice (posizione di ogni numero, 0 = manca) e per ogni
    immagine: larghezza, altezza, X = lunghezza dell'intestazione JPEG, L = dati arrotondati a 4 byte + 8 zeri.
    Come in tutte le 1000 immagini FLSUN, i dati compressi partono a un indirizzo multiplo di 4 (vedi allinea)."""
    n = max(immagini) + 1
    pos = 0x10 + 4 * n
    indice, corpi = [0] * n, []
    for k in range(n):
        if k not in immagini:
            continue
        im = immagini[k]
        j = jpeg(im)
        x = inizio_dati(j)
        j, x = allinea(j, x, pos + 10)
        dati = len(j) - x
        lung = (dati + 3) // 4 * 4 + 8
        corpo = struct.pack(">HHHI", im.width, im.height, x, lung) + j + b"\0" * (lung - dati)
        indice[k] = pos
        pos += len(corpo)
        corpi.append(corpo)
    resto = (struct.pack(">I", pos - 8) + b"\x04" + struct.pack(">H", len(immagini)) + b"\0"
             + b"".join(struct.pack(">I", o) for o in indice) + b"".join(corpi))
    return b"DGUS_3" + crc16(resto).to_bytes(2, "little") + resto


def crc16(dati):
    """CRC-16 Modbus (0xA001, parte da 0xFFFF) di tutto quello che segue i byte di controllo. Ricavato dai 9 .icl
    FLSUN (tutti tornano); con 0 lo schermo aggiorna, scrive «END», ma tiene le immagini vecchie (prova 2026-10-18)."""
    c = 0xFFFF
    for b in dati:
        c = (c >> 8) ^ _CRC[(c ^ b) & 0xFF]
    return c


_CRC = []
for _i in range(256):
    _c = _i
    for _bit in range(8):
        _c = (_c >> 1) ^ (0xA001 if _c & 1 else 0)
    _CRC.append(_c)


def leggi_icl(dati):
    """Il contrario di icl(): {numero: immagine} (per la verifica)."""
    assert dati[6:8] == crc16(dati[8:]).to_bytes(2, "little"), "controllo .icl sbagliato"
    n = dati[13] << 8 | dati[14]
    voci, i = [], 0x10
    while not voci or i < min([x for x in voci if x] or [len(dati)]):
        voci.append(struct.unpack(">I", dati[i:i + 4])[0])
        i += 4
    out = {}
    for k, o in enumerate(voci):
        if o:
            w, h, x, lung = struct.unpack(">HHHI", dati[o:o + 10])
            assert (o + 10 + x) % 4 == 0
            out[k] = Image.open(io.BytesIO(standard(dati[o + 10:o + 10 + x + lung]))).convert("RGB")
            assert out[k].size == (w, h)
    assert len(out) == n
    return out


def rgb565(c):
    return (c[0] >> 3) << 11 | (c[1] >> 2) << 5 | c[2] >> 3


def riga_icona(vp, x, y, vmin, vmax, imin, imax, libreria):
    """Icona variabile (tipo 0x00), come quelle delle etichette FLSUN: modo 0."""
    r = struct.pack(">BBHH", 0x5A, 0x00, 0xFFFF, 0x000A) + struct.pack(">7H", vp, x, y, vmin, vmax, imin, imax)
    return (r + bytes([libreria, 0])).ljust(32, b"\0")


def riga_testo(vp, box, colore, lunghezza):
    """Testo (tipo 0x11) col carattere interno dello schermo come i testi FLSUN: font 0, 22×22, codifica 0x41."""
    x0, y0, x1, y1 = box
    r = struct.pack(">BBHH", 0x5A, 0x11, 0xFFFF, 0x000D) + struct.pack(">4H", vp, x0, y0, rgb565(colore))
    r += struct.pack(">5H", x0, y0, x1, y1, lunghezza) + bytes([0, 0, 22, 22, 0x41, 0, 0])
    return r.ljust(32, b"\0")


def show_file(righe):
    """{pagina: [righe da 32 byte]} → 14ShowFile.bin: «\\x14DGUS_2», 0x10 0x00, ultima pagina; tabella da 0x10 con
    4 byte per pagina (numero di righe LE16, posizione BE16) fino a 0x4000; poi le righe e 32 byte 0xFF in fondo."""
    ultima = max(righe)
    tabella, corpo, pos = bytearray(), bytearray(), 0x4000
    for pagina in range((0x4000 - 0x10) // 4):
        r = righe.get(pagina, [])
        tabella += struct.pack("<H", len(r)) + struct.pack(">H", pos)
        for x in r:
            assert len(x) == 32
            corpo += x
        pos += 32 * len(r)
    assert pos + 32 <= 0x10000
    testa = b"\x14DGUS_2\x10\x00" + bytes([ultima]) + b"\0" * 6
    return testa + bytes(tabella) + bytes(corpo) + b"\xff" * 32


def riga_tocco(pagina, box, codice, dopo, premuta):
    """Un pulsante di 13TouchFile.bin (16 byte + 16 di estensione): pagina, riquadro, pagina dopo (0xFF00 = resta),
    immagine da premuto (0xFF00 = nessuna), 0xFE02 «regolazione incrementale» sul VP dei tasti: modo parola,
    in su, si ferma al massimo, passo = massimo = codice<<8, minimo 0, niente ripetizione tenendo premuto."""
    x0, y0, x1, y1 = box
    r = struct.pack(">8H", pagina, x0, y0, x1, y1, 0xFF00 if dopo is None else dopo,
                    0xFF00 if premuta is None else premuta, 0xFE02)
    return r + bytes([0xFE]) + struct.pack(">H", VP["TASTO"]) + bytes([0x00, 0x01, 0x00]) + \
        struct.pack(">3H", codice << 8, 0, codice << 8) + b"\0" * 4


# ---- costruzione ----

def tutte_le_icone(s, mappa):
    gruppo = mappa.get(GRUPPI.get(s.numero), {})
    vp_di = {nome: c["vp"] for nome, c in gruppo.items()}
    return s.icone + [ic for riga in s.righe for ic in riga.icone(vp_di)]


def costruisci():
    shutil.rmtree(USCITA, ignore_errors=True)
    os.makedirs(USCITA)
    os.makedirs(VERIFICA, exist_ok=True)
    controlla_messaggi()
    pagine = schermate()
    mappa = campi(pagine)
    sfondi, sfondi_S = {}, {}
    for s in pagine:
        for premuto in (False, True) if s.tasti else (False,):
            p = Pagina()
            s.statico(p, premuto)
            for riga in s.righe:
                riga.fissi(p)
            if premuto:
                sfondi[PREMUTA + s.numero] = p.riduci()
            else:
                sfondi_S[s.numero] = p.im
                sfondi[s.numero] = p.riduci()
    icone = {s.numero: tutte_le_icone(s, mappa) for s in pagine}

    # controlli: riquadri dentro lo schermo e che non si sovrappongono
    for s in pagine:
        boxes = [(i.box, f"icona VP {i.vp:04x}") for i in icone[s.numero]] + \
                [((t.box[0], t.box[1], t.box[2] - t.box[0], t.box[3] - t.box[1]), f"testo VP {t.vp:04x}")
                 for t in s.testi]
        for k, ((x, y, w, h), nome) in enumerate(boxes):
            assert 0 <= x and 0 <= y and x + w <= L and y + h <= A, (s.numero, nome)
            for (x2, y2, w2, h2), nome2 in boxes[k + 1:]:
                # un testo tutto dentro un'icona va bene: lo schermo disegna le variabili in ordine (prima le icone,
                # poi i testi), così il nome sta sopra il riquadro della riga dell'elenco dei file
                sopra = (nome.startswith("icona") and nome2.startswith("testo") and x <= x2 and y <= y2
                         and x2 + w2 <= x + w and y2 + h2 <= y + h)
                # due testi nello stesso riquadro: uno grigio e uno rosso, se ne scrive uno solo (l'altro vuoto)
                sopra = sopra or (nome.startswith("testo") and nome2.startswith("testo") and (x, y, w, h) == (x2, y2, w2, h2))
                assert sopra or x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y, (s.numero, nome, nome2)

    # tutte le immagini di ogni parte che cambia; una serie uguale a un'altra o a un suo pezzo (stesso disegno sullo
    # stesso fondo: le celle di una riga, stampa e pausa…) si salva una volta sola. Avviso se un disegno esce dal
    # suo riquadro (il bordo dell'icona sarebbe diverso dallo sfondo vuoto)
    serie, impronte, avvisi = [], [], []
    for s in pagine:
        for ic in icone[s.numero]:
            vuoto = ritaglio(sfondi_S[s.numero], ic.box, lambda p: None)
            imm, hh = [], []
            for v in ic.valori:
                im = ritaglio(sfondi_S[s.numero], ic.box, lambda p, v=v: ic.disegna(p, v))
                imm.append(im)
                hh.append(hashlib.sha1(im.tobytes() + bytes(str(im.size), "ascii")).hexdigest())
                bordo = [(x, y) for x in range(im.width) for y in (0, im.height - 1)] + \
                        [(x, y) for y in range(im.height) for x in (0, im.width - 1)]
                diff = max(max(abs(a - b) for a, b in zip(im.getpixel(q), vuoto.getpixel(q))) for q in bordo)
                if diff > 24:
                    avvisi.append(f"pagina {s.numero} VP {ic.vp:04x} valore {v}: il disegno tocca il bordo ({diff})")
            for k, h in enumerate(impronte):
                for inizio in range(len(h) - len(hh) + 1):
                    if h[inizio:inizio + len(hh)] == hh:
                        ic.posto = (k, inizio)
                        break
                else:
                    continue
                break
            else:
                ic.posto = (len(serie), 0)
                serie.append(imm)
                impronte.append(hh)

    # librerie: 32 = sfondi; poi le serie in file da al massimo MAX_ICL, ognuno al suo blocco
    file_icl = {32: icl(sfondi)}
    prossimo = 32 + math.ceil(len(file_icl[32]) / BLOCCO)
    dove = {}                                     # serie → (libreria, primo numero)
    corrente, misura, in_attesa = {}, 0, []

    def chiudi():
        nonlocal prossimo, corrente, misura, in_attesa
        if corrente:
            dati = icl(corrente)
            file_icl[prossimo] = dati
            for k, primo in in_attesa:
                dove[k] = (prossimo, primo)
            prossimo += math.ceil(len(dati) / BLOCCO)
            corrente, misura, in_attesa = {}, 0, []

    for k, imm in enumerate(serie):
        quanto = sum(len(jpeg(i)) + 24 for i in imm)
        if corrente and misura + quanto > MAX_ICL:
            chiudi()
        in_attesa.append((k, len(corrente)))
        for im in imm:
            corrente[len(corrente)] = im
        misura += quanto
    chiudi()
    assert prossimo - 1 <= ULTIMO_BLOCCO, f"non ci sta: arriverebbe al blocco {prossimo - 1}"

    righe = {}
    for s in pagine:
        r = []
        for ic in icone[s.numero]:
            k, inizio = ic.posto
            lib, primo = dove[k]
            r.append(riga_icona(ic.vp, ic.box[0], ic.box[1], ic.valori.start, ic.valori.stop - 1, primo + inizio,
                                primo + inizio + len(ic.valori) - 1, lib))
        for t in s.testi:
            assert (t.box[2] - t.box[0]) >= 11 * t.lunghezza - 2 or t.lunghezza == LUNGHEZZA_NOME, (s.numero, t)
            r.append(riga_testo(t.vp, t.box, t.colore, t.lunghezza))
        righe[s.numero] = r
    tocchi = b""
    for s in pagine:
        for t in s.tasti:
            tocchi += riga_tocco(s.numero, t.box, t.codice, t.dopo, PREMUTA + s.numero)
    tocchi += b"\xff\xff"

    open(os.path.join(USCITA, "14ShowFile.bin"), "wb").write(show_file(righe))
    open(os.path.join(USCITA, "13TouchFile.bin"), "wb").write(tocchi)
    for n, dati in file_icl.items():
        open(os.path.join(USCITA, f"{n}.icl"), "wb").write(dati)
    json.dump({"pagine": PAGINE, "testi": {str(t.vp): t.lunghezza for s in pagine for t in s.testi},
               "righe_lista": RIGHE_LISTA, "sotto_z": [s for s, _ in SOTTO_Z],
               "gruppi": {str(k): v for k, v in GRUPPI.items()}, "campi": mappa, "nascosto": NASCOSTO,
               "vp": VP, "tasti": TASTI, "stati": [s for s, _ in STATI], "fasi": [f for f, _ in FASI],
               "sotto_filo": [s for s, _ in SOTTO_FILO], "sotto_piatto": [s for s, _ in SOTTO_PIATTO],
               "lunghezza_nome": LUNGHEZZA_NOME, "colonne_nome": COLONNE_NOME, "lingua": LINGUA,
               "messaggi": MESSAGGI[LINGUA], "decimale": DEC},
              open(os.path.join(CARTELLA, "mappa.json"), "w"), indent=1, ensure_ascii=False)

    print("file:")
    for f in sorted(os.listdir(USCITA), key=lambda f: (len(f), f)):
        dim = os.path.getsize(os.path.join(USCITA, f))
        blocchi = f" blocchi {f.split('.')[0]}–{int(f.split('.')[0]) + math.ceil(dim / BLOCCO) - 1}" \
            if f.endswith(".icl") else ""
        print(f"  {f:16s} {dim / 1024:8.0f} kB{blocchi}")
    print(f"serie di icone: {len(serie)}, immagini: {sum(len(x) for x in serie)}, ultimo blocco {prossimo - 1}")
    for a in avvisi[:30] + sorted(STRETTI):
        print("AVVISO", a)
    return pagine, mappa


# ---- verifica: pagine ricomposte leggendo i file generati, come farebbe lo schermo ----

def in_ascii(t):
    # come in_ascii() di schermo_flsun.py: il carattere dello schermo ha solo l'ASCII
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"),
                 ("…", "..."), ("–", "-"), ("¿", ""), ("¡", "")):
        t = t.replace(a, b)
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")


def due_righe(t, colonne=26):
    # come a_capo() di schermo_flsun.py per i messaggi della conferma: [riga1, riga2], o None se non ci sta
    righe, riga = [], ""
    for parola in in_ascii(t).split():
        if riga and len(riga) + 1 + len(parola) > colonne:
            righe.append(riga)
            riga = parola
        else:
            riga = (riga + " " + parola).strip()
    righe.append(riga)
    return (righe + [""])[:2] if len(righe) <= 2 and all(len(r) <= colonne for r in righe) else None


def controlla_messaggi():
    # i messaggi del modulo ci devono stare: righe dell'elenco 24 caratteri, conferma 2 righe da 26
    for lingua, m in MESSAGGI.items():
        assert set(m) == set(MESSAGGI["it"]), (lingua, set(m) ^ set(MESSAGGI["it"]))
        for k in ("carico_elenco", "non_leggo", "manca_sd", "nessun_file"):
            assert len(in_ascii(m[k])) <= 24, (lingua, k, m[k])
        for k in ("avvio", "avviata", "occupata"):
            assert due_righe(m[k]), (lingua, k, m[k])
    for lingua, t in TRADUZIONI.items():
        assert set(t) == set(TRADUZIONI["en"]), (lingua, set(t) ^ set(TRADUZIONI["en"]))


ESEMPIO = {
    VP["STATO"]: 1, VP["UGELLO_OB"]: 220, VP["UGELLO_BARRA"]: 83, VP["PIATTO_OB"]: 60, VP["PIATTO_BARRA"]: 94,
    VP["FASE"]: 0, VP["ANELLO"]: 47, VP["MATERIALE"]: 1, VP["SOTTO_FILO"]: 1, VP["PASSO"]: 1,
    VP["RAFFREDDA_BARRA"]: 54, VP["SOTTO_PIATTO"]: 0, VP["GRUPPO"]: 0, VP["ZPASSO"]: 1, VP["SOTTO_Z"]: 0,
    **{VP["CARTA_RIGA"] + i: 0 for i in range(RIGHE_LISTA)},
}
CAMPI_ESEMPIO = {
    "pronta": dict(ugello="187°", piatto="58°"),
    "stampa": dict(fine_ore="21", fine_min="35", ore="1", minuti="12", resta_ore="1", resta_min="21", filo_m="4",
                   filo_dec="2", ugello="205", ugello_ob="/ 205°", piatto="60", piatto_ob="/ 60°"),
    "filamento": dict(ugello="187", ugello_ob="/ 220°"),
    "finita": dict(ore="1", minuti="34", filo_m="12", filo_dec="7", piatto="45"),
    "regola": dict(z_segno="+", z_int="0", z_dec="04"),
}
TESTI_ESEMPIO = {VP["NOME"]: "phone_stand_fast", VP["NOME_CENTRO"]: "     phone_stand_fast",
                 VP["LISTA_PAGINA"]: "  1/2",
                 VP["AVVIA_NOME"]: "spool_holder_with_bearin", VP["AVVIA_NOME2"]: "g_608",
                 VP["AVVIA_DURATA"]: "4h 53m", VP["AVVIA_FILO"]: "105 g", VP["AVVIA_MATERIALE"]: "PLA",
                 VP["AVVIA_TEMP"]: "220 / 60", **dict(zip((VP["AVVIA_MSG"], VP["AVVIA_MSG2"]), due_righe(MESSAGGI[LINGUA]["avvio"])))}
for _i, (_n, _d) in enumerate([("lamp_base_with_cable..v2", "13h 17m  238 g  PLA"),
                               ("spool_holder_with..g_608", "4h 53m  105 g  PLA"),
                               ("spool_holder_with..g_688", "6h 17m  120 g  PLA"),
                               ("cable_clip_8mm_set10", "13m  4 g  PLA"),
                               ("phone_stand_light", "7h 13m  130 g  PLA")]):
    TESTI_ESEMPIO[VP["LISTA_NOME"] + 0x20 * _i] = _n[:24]
    TESTI_ESEMPIO[VP["LISTA_INFO"] + 0x20 * _i] = _d


def carattere_fisso(px):
    # carattere a passo fisso per i testi delle pagine ricomposte (sullo schermo c'e' il suo 22×22)
    for f in ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "DejaVuSansMono.ttf", "consola.ttf"):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            pass
    return ImageFont.load_default()


def ricomponi(pagina, valori, testi, cartella=USCITA):
    show = open(os.path.join(cartella, "14ShowFile.bin"), "rb").read()
    librerie = {}

    def libreria(n):
        if n not in librerie:
            librerie[n] = leggi_icl(open(os.path.join(cartella, f"{n}.icl"), "rb").read())
        return librerie[n]
    im = libreria(32)[pagina].copy()
    n = struct.unpack("<H", show[0x10 + 4 * pagina:0x12 + 4 * pagina])[0]
    pos = struct.unpack(">H", show[0x12 + 4 * pagina:0x14 + 4 * pagina])[0]
    d = ImageDraw.Draw(im)
    mono = carattere_fisso(18)                                           # passo 11 px come il carattere 22×22
    for k in range(n):
        r = show[pos + 32 * k:pos + 32 * k + 32]
        if r[1] == 0x00:
            vp, x, y, vmin, vmax, imin, imax = struct.unpack(">7H", r[6:20])
            v = valori.get(vp, 0)
            if vmin <= v <= vmax:
                im.paste(libreria(r[20])[imin + v - vmin], (x, y))
        elif r[1] == 0x11:
            vp, x, y, col = struct.unpack(">4H", r[6:14])
            colore = ((col >> 11) << 3, ((col >> 5) & 63) << 2, (col & 31) << 3)
            d.text((x, y), testi.get(vp, ""), font=mono, fill=colore)
    return im


def verifica(pagine, mappa, tocchi_visibili=False):
    tocchi = open(os.path.join(USCITA, "13TouchFile.bin"), "rb").read()
    viste = []
    for s in pagine:
        valori = dict(ESEMPIO)
        gruppo = GRUPPI.get(s.numero)
        esempio = dict(CAMPI_ESEMPIO.get(gruppo, {}))
        if s.numero == PAUSA:
            valori[VP["FASE"]] = 2
            esempio.update(resta_ore=TRATTINO, resta_min=TRATTINO * 2, fine_ore=TRATTINO * 2, fine_min=TRATTINO * 2,
                           piatto_ob="")
        for nome, testo in esempio.items():
            valori.update(valori_campo(mappa[gruppo][nome], testo))
        im = ricomponi(s.numero, valori, TESTI_ESEMPIO)
        premuta = ricomponi(PREMUTA + s.numero, {}, {}) if s.tasti else None
        if tocchi_visibili:
            d = ImageDraw.Draw(im)
            i = 0
            while i + 16 <= len(tocchi) and tocchi[i:i + 2] != b"\xff\xff":
                pag, x0, y0, x1, y1, dopo, on, cod = struct.unpack(">8H", tocchi[i:i + 16])
                if pag == s.numero:
                    d.rectangle([x0, y0, x1, y1], outline=(255, 0, 255))
                    d.text((x0 + 3, y0 + 2), f"{tocchi[i + 22]}" + (f">{dopo}" if dopo != 0xFF00 else ""),
                           fill=(255, 0, 255))
                i += 32 if cod else 16
        im.save(os.path.join(VERIFICA, f"pagina_{s.numero}.png"))
        viste.append((s.numero, im, premuta))
    tela = Image.new("RGB", (len(viste) * (L + 20) + 20, 2 * (A + 20) + 20), (60, 60, 60))
    for k, (n, im, premuta) in enumerate(viste):
        tela.paste(im, (20 + k * (L + 20), 20))
        if premuta:
            tela.paste(premuta, (20 + k * (L + 20), A + 40))
    tela.save(os.path.join(VERIFICA, "tutte.png"))
    print(os.path.join(VERIFICA, "tutte.png"))


if __name__ == "__main__":
    if "--lingua" in sys.argv:
        lingue = [sys.argv[sys.argv.index("--lingua") + 1]]
    elif "FLSUN_LINGUA" in os.environ:
        lingue = None
    else:
        lingue = LINGUE
    if lingue is None:
        print(f"== {LINGUA}")
        verifica(*costruisci(), tocchi_visibili="--tocchi" in sys.argv)
    else:
        # una lingua per volta, ognuna nel suo processo (i testi si leggono all'avvio)
        altri = [a for a in sys.argv[1:] if a == "--tocchi"]
        for lingua in lingue:
            subprocess.run([sys.executable, os.path.abspath(__file__)] + altri, check=True,
                           env=dict(os.environ, FLSUN_LINGUA=lingua))
