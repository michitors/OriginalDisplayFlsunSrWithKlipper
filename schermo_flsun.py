# Schermo originale della FLSUN SR (DWIN T5L, 320×480) con una grafica nuova, guidato direttamente da Klipper
#
# Va insieme alla DWIN_SET fatta da genera.py (sulla microSD dello schermo) e a schermo_flsun_mappa.json, che sta accanto
# a questo file in klippy/extras (VP, pagine e codici dei pulsanti: li scrive genera.py). Con la grafica FLSUN originale
# sullo schermo questo modulo non va: pagine, indirizzi e pulsanti sono altri.
#
# Collegamento: USART3 della scheda (PB10 -> schermo, PB11 <- schermo, 115200 baud) con la seriale software dei driver
# TMC (tmcuart): al massimo 8 caratteri per invio e, delle risposte, arriva solo il byte alto della prima parola. Per
# questo i pulsanti scrivono il loro codice nel byte alto di VP_TASTO.
#
# Lo schermo non decide niente: ogni pulsante scrive il suo codice e alcuni cambiano pagina da soli. Klipper legge
# VP_TASTO ogni pochi decimi di secondo (due letture uguali, poi lo rimette a 0), fa l'azione e tiene il conto della
# pagina, che lo schermo non fa leggere. Ogni codice dice anche su che pagina si e' arrivati. Se lo schermo si riaccende
# da solo, VP_VIVO torna 0 e Klipper riscrive tutto.
#
# Le stampe sono quelle di Klipper (print_stats + virtual_sdcard), partite da Mainsail, da Fluidd o dallo schermo.
# Pausa, Riprendi e Annulla: PAUSE / RESUME / CANCEL_PRINT (le macro di mainsail.cfg o fluidd.cfg). «Stampa un file»: i
# file di virtual_sdcard con durata, filo, materiale e temperature come li ha letti Moonraker (se c'e'), avvio con
# SDCARD_PRINT_FILE dopo «Sì, stampa». «Regola»: altezza dell'ugello in stampa con SET_GCODE_OFFSET Z_ADJUST (MOVE=1),
# al massimo LIMITE_Z; a fine stampa la correzione si toglie. Le azioni G-code partono da una callback del reactor (la
# lettura dei pulsanti non si ferma) e una per volta: mentre carica, scarica, muove il filo o fa l'homing gli altri
# pulsanti d'azione non fanno niente.
#
# Comandi:
#   SCHERMO_TASTO CODICE=n        come premere il pulsante n (prove senza toccare lo schermo)
#   SCHERMO_PAGINA PAGINA=n       mostra la pagina n
#   SCHERMO_STATO                 cosa crede il modulo (pagina, filamento, azione in corso)
#   SCHERMO_MANDA TESTO="..."     manda i byte allo schermo (\xNN per i byte)
#   SCHERMO_CHIEDI TESTO="..."    manda (max 8 byte) e legge la risposta
import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import tmc_uart

MAX_CARATTERI = 8          # tmcuart: 10 byte di bit = 8 caratteri da 10 bit (inizio + 8 + fine)
MAPPA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schermo_flsun_mappa.json")
VIVO = 0x5a00              # in VP_VIVO finche' lo schermo non si riavvia
TUTTO_OGNI = 600.          # s: riscrive comunque tutto
VIVO_OGNI = 5.             # s: controlla VP_VIVO
PAGINA_OGNI = 5.           # s: rimanda comunque la pagina (vedi _ciclo)
ERRORE_PER = 15.           # s: quanto resta «Errore» dopo un'azione non riuscita
FINESTRE = {"PRERISCALDA": 60., "ANNULLA": 30., "FILE": 120., "REGOLA": 60.}   # s: poi torna da solo alla pagina di prima
DOPPIO = 1.                # s: lo stesso pulsante rivisto entro questo tempo e' lo stesso tocco (vedi _ciclo) …
RIPETIBILI = ("FILE_SU", "FILE_GIU", "Z_GIU", "Z_SU", "ARRETRA", "AVANZA")   # … tranne questi, che si toccano di fila
RAFFICA, RAFFICA_OGNI = 2., 0.1    # s: dopo un tocco i pulsanti si leggono piu' spesso, i tocchi di fila non si fondono
AZZERA_PROVE = 3           # volte che VP_TASTO si rimette a 0 se rileggendolo non lo e'
# preriscaldo e filamento: nome, ugello, piatto (PLA come il firmware FLSUN)
MATERIALI = {1: ("PLA", 220, 60), 2: ("PETG", 250, 80)}
PASSI = (1., 10., 50.)     # mm di Arretra / Avanza
CARICA_MM, CARICA_F = 350., 600     # firmware FLSUN v1.4 (Bowden): 350 mm a 10 mm/s …
SPURGO_MM, SPURGO_F = 50., 150      # … poi 50 mm piano, finche' esce dall'ugello
SCARICA_MM, SCARICA_F = 300., 1200  # 300 mm a 20 mm/s, dopo 10 mm in avanti per ammorbidire la punta
MUOVI_F = 300                       # Arretra / Avanza a 5 mm/s
STACCA = 32.               # °C: sotto, il pezzo si stacca dal piatto
FASI = {"stampa": 0, "riscaldamento": 1, "pausa": 2}
TRATTINO = "–"
PASSI_Z = (0.01, 0.02, 0.05)       # mm per tocco di Abbassa / Alza
LIMITE_Z = 0.30                    # mm: oltre, Abbassa / Alza non fanno niente («Al limite»)
AVVISO_PER = 4.                    # s: quanto resta «Al limite»
ELENCO_VECCHIO = 20.               # s: riaprendo l'elenco dei file dopo tanto, lo rilegge
MAX_DATI = 300                     # file (i piu' recenti) di cui chiedere a Moonraker durata, filo e temperature
SEPARATORI = "_- .+"               # dove tagliare i nomi dei file
COLONNE_RIGA, COLONNE_MSG = 24, 26


def codifica(dati):
    # Bit della seriale (inizio 0, 8 bit dal meno significativo, fine 1) impacchettati come vuole tmcuart
    out = pos = 0
    for d in bytearray(dati):
        out |= ((d << 1) | 0x200) << pos
        pos += 10
    return bytearray((out >> (i * 8)) & 0xff for i in range((pos + 7) // 8))


def decodifica(grezzo):
    # Campioni letti da tmcuart (un bit per volta, dal primo bit d'inizio visto) -> caratteri; salta i campioni che
    # non stanno in un carattere intero (pause tra caratteri, inizio a meta')
    bit = [(b >> i) & 1 for b in bytearray(grezzo) for i in range(8)]
    out, i = bytearray(), 0
    while i + 10 <= len(bit):
        if bit[i] == 0 and bit[i + 9] == 1:
            out.append(sum(bit[i + 1 + k] << k for k in range(8)))
            i += 10
        else:
            i += 1
    return bytes(out)


def valori_campo(campo, testo):
    # Il testo di un campo a celle -> {vp: valore}, come valori_campo() di genera.py: i glifi piu' lunghi per primi, i
    # caratteri che non ci sono saltati, le celle vuote nascoste
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
    n, vuoto = campo["n"], campo["nascosto"]
    celle = (pezzi + [vuoto] * n)[:n] if campo["allinea"] == "sx" else ([vuoto] * n + pezzi)[-n:]
    return dict((campo["vp"] + k, v) for k, v in enumerate(celle))


def ore_minuti(s):
    if s is None:
        return TRATTINO, TRATTINO * 2
    h, m = divmod(int(max(s, 0.)) // 60, 60)
    return "%d" % min(h, 99), "%02d" % m


def metri(mm):
    if mm is None:
        return TRATTINO, TRATTINO
    m, d = divmod(int(max(mm, 0.)) // 100, 10)
    return "%d" % min(m, 999), "%d" % d


def nome_corto(nome, colonne):
    nome = (nome or "").rsplit("/", 1)[-1]
    if nome.lower().endswith(".gcode"):
        nome = nome[:-6]
    return nome.encode("ascii", "replace").decode("ascii")[:colonne]


# messaggi del modulo se mappa.json non li ha (mappa.json li porta nella lingua scelta, vedi testi.py)
MESSAGGI = {"carico_elenco": "Carico l'elenco...", "non_leggo": "Non leggo i file", "manca_sd": "Manca virtual_sdcard",
            "nessun_file": "Nessun file", "avvio": "Avvio la stampa...", "avviata": "Stampa avviata",
            "occupata": "Stampante occupata, riprova"}


def in_ascii(t):
    # il carattere dello schermo ha solo l'ASCII: «è» -> «e», «ü» -> «ue», «…» -> «...»
    t = t or ""
    for a, b in (("…", "..."), ("–", "-"), ("·", "-"), ("≤", "<="), ("°", ""), ("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
                 ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"), ("¿", ""), ("¡", "")):
        t = t.replace(a, b)
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")


def nomi_riga(nomi, colonne):
    # nomi dei file in una riga ciascuno: l'inizio e la fine (i file simili cambiano spesso in fondo), tagliati
    # dove possibile tra le parole (barretta_petg_250..rmale, non barretta_petg_25..ormale). Se due nomi diventano
    # uguali (barretta_petg_250_..., ..._251_...), il taglio si sposta finche' si distinguono
    pieni = [in_ascii(nome_corto(n, 999)) for n in nomi]

    def taglia(n, testa):
        return n if len(n) <= colonne else n[:testa] + ".." + n[len(n) - (colonne - 2 - testa):]

    def punti(n, testa):
        if len(n) <= colonne:
            return 0.
        coda = colonne - 2 - testa
        return ((n[testa] in SEPARATORI and n[testa - 1] not in SEPARATORI)
                + (n[-coda - 1] in SEPARATORI and n[-coda] not in SEPARATORI) - abs(testa - (colonne - 9)) * 0.15)
    teste = range(6, colonne - 3)
    brevi = [taglia(n, max(teste, key=lambda t: punti(n, t))) for n in pieni]
    gruppi = {}
    for i, b in enumerate(brevi):
        gruppi.setdefault(b, []).append(i)
    for quali in gruppi.values():
        if len(quali) < 2 or len({pieni[i] for i in quali}) < len(quali):
            continue
        for testa in sorted(teste, key=lambda t: -sum(punti(pieni[i], t) for i in quali)):
            prova = [taglia(pieni[i], testa) for i in quali]
            if len(set(prova)) == len(prova):
                for i, b in zip(quali, prova):
                    brevi[i] = b
                break
    return brevi


def a_capo(t, colonne, righe=2):
    # parole su righe da colonne caratteri (le parole troppo lunghe si spezzano). Se non ci sta, prima via le
    # parentesi «(con Klipper: ...)», poi tre puntini in fondo
    if "(" in t and len(_a_capo(t, colonne)) > righe:
        t = re.sub(r"\s*\([^()]*\)", "", t)
    out = _a_capo(t, colonne)
    if len(out) > righe:
        out = out[:righe]
        out[-1] = out[-1][:colonne - 3].rstrip(" .,:;") + "..."
    return out + [""] * (righe - len(out))


def _a_capo(t, colonne):
    out, riga = [], ""
    for parola in in_ascii(t).split():
        while len(parola) > colonne:
            if riga:
                out.append(riga)
                riga = ""
            out.append(parola[:colonne])
            parola = parola[colonne:]
        if not riga:
            riga = parola
        elif len(riga) + 1 + len(parola) <= colonne:
            riga += " " + parola
        else:
            out.append(riga)
            riga = parola
    if riga:
        out.append(riga)
    return out


def dati_moonraker(m):
    # /server/files/metadata di Moonraker -> quello che mostrano l'elenco e la conferma
    t, tipo = m.get("estimated_time"), (m.get("filament_type") or "").split(";")[0].strip()
    return {"minuti": t / 60. if t else None, "grammi": m.get("filament_weight_total") or None,
            "metri": m["filament_total"] / 1000. if m.get("filament_total") else None, "filamento": tipo or None,
            "ugello": m.get("first_layer_extr_temp"), "piatto": m.get("first_layer_bed_temp")}


def quanto_filo(v, decimale=","):
    if v.get("grammi"):
        return "%d g" % round(v["grammi"])
    return ("%.1f m" % v["metri"]).replace(".", decimale) if v.get("metri") else ""


def durata(minuti):
    if minuti is None:
        return TRATTINO
    h, m = divmod(int(minuti), 60)
    return "%dh %02dm" % (h, m) if h else "%dm" % m


def centrato(t, colonne):
    return " " * ((colonne - len(t)) // 2) + t


class SchermoFlsun:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        try:
            with open(MAPPA_FILE) as f:
                m = json.load(f)
        except (OSError, ValueError) as e:
            raise config.error("schermo_flsun: manca %s (%s): e' mappa.json, fatto da genera.py" % (MAPPA_FILE, e))
        self.P = m["pagine"]
        self.VP = m["vp"]
        self.T = m["tasti"]
        self.nomi_tasti = dict((v, k) for k, v in self.T.items())
        self.gruppi = dict((int(k), v) for k, v in m["gruppi"].items())
        self.campi = m["campi"]
        for gruppo in self.campi.values():
            for c in gruppo.values():
                c["nascosto"] = m["nascosto"]
        self.lunghezza_nome, self.colonne_nome = m["lunghezza_nome"], m["colonne_nome"]
        self.lunghezze = dict((int(k), v) for k, v in m.get("testi", {}).items())
        self.righe_lista = m.get("righe_lista", 5)
        self.nascosto = m["nascosto"]
        self.nomi_pagine = dict((v, k) for k, v in self.P.items())
        # i messaggi nella lingua della grafica (mappa.json di quella lingua)
        self.msg = dict((k, in_ascii(v)) for k, v in dict(MESSAGGI, **m.get("messaggi", {})).items())
        self.decimale = m.get("decimale", ",")
        ppins = self.printer.lookup_object("pins")
        rx = ppins.lookup_pin(config.get("rx_pin", "PB11"), can_pullup=True)
        tx = ppins.lookup_pin(config.get("tx_pin", "PB10"))
        if rx["chip"] is not tx["chip"]:
            raise config.error("schermo_flsun: rx_pin e tx_pin sulla stessa scheda")
        self.mcu = rx["chip"]
        self.rx_pin, self.tx_pin, self.pullup = rx["pin"], tx["pin"], rx["pullup"]
        self.baud = config.getint("baud", 115200, minval=1200)
        self.intervallo = config.getfloat("intervallo", 1., minval=0.3)            # numeri sullo schermo
        self.tasti_ogni = config.getfloat("tasti_ogni", 0.3, minval=0.1)           # lettura dei pulsanti
        self.tasti_in_stampa = config.getfloat("tasti_in_stampa", 0.6, minval=0.1)  # … durante una stampa
        self.moonraker = config.get("moonraker", "http://127.0.0.1:7125").rstrip("/")  # "" = solo i nomi dei file
        self.oid = self.mcu.create_oid()
        self.cmd_queue = self.mcu.alloc_command_queue()
        # la stessa serratura dei driver TMC: una seriale software alla volta, se no i tempi dei bit si disturbano
        self.mutex = tmc_uart.lookup_tmc_uart_mutex(self.mcu)
        self.invia = None
        self.mcu.register_config_callback(self._build_config)
        self.scritti = {}          # VP -> valore gia' sullo schermo (testi: VP -> byte)
        self.pagina = None         # pagina che lo schermo mostra (secondo noi); None = da riscrivere
        self.pagina_da = 0.
        self.tutto_ora = self.prossimo_vivo = self.prossimo_aggiorna = self.prossima_pagina = 0.
        self.materiale, self.passo = 0, 1
        self.ultimo_tasto, self.ultimo_tasto_da = None, -DOPPIO
        self.tasto_prima = self.raffica_fino = 0.
        self.occupato = None       # azione G-code in corso: "carico", "scarico", "muovo", "azzero"
        self.errore = None         # (ora, testo) dell'ultima azione non riuscita
        self.ultima = None         # la stampa in corso all'ultimo giro
        self.finita = None         # stampa finita da mostrare finche' non si preme OK
        self.z_schermo = 0.        # mm di «Regola» in questa stampa: a fine stampa si tolgono
        self.richieste = queue.Queue()
        self.mostra = False        # rimanda la pagina anche se e' gia' quella (lo schermo e' rimasto su un'altra)
        # elenco dei file: (time.time(), voci o None, errore), gruppo e pagina scelti; dati di Moonraker gia' chiesti
        self.elenco, self.elenco_chiesto, self.dati_file = (0., None, None), False, {}
        self.gruppo = self.pagina_lista = 0
        self.scelto = None         # file scelto (voce dell'elenco)
        self.avvio = None          # {"stato": "controllo" | "ok" | "errore", "messaggio": ...}
        self.zpasso, self.avviso_z = 1, None
        self.filo = None
        self.heaters = self.ps = self.sd = None
        self.timer = self.reactor.register_timer(self._ciclo)
        self.printer.register_event_handler("klippy:ready", self._pronto)
        self.printer.register_event_handler("klippy:shutdown", self._fermo)
        self.printer.register_event_handler("klippy:disconnect", self._fermo)
        self.gcode = self.printer.lookup_object("gcode")
        for nome, desc in (("SCHERMO_TASTO", "Come premere un pulsante dello schermo FLSUN"),
                           ("SCHERMO_PAGINA", "Mostra una pagina dello schermo FLSUN"),
                           ("SCHERMO_STATO", "Cosa crede il modulo dello schermo FLSUN"),
                           ("SCHERMO_MANDA", "Manda byte allo schermo FLSUN (\\xNN)"),
                           ("SCHERMO_CHIEDI", "Manda byte allo schermo FLSUN e legge la risposta")):
            self.gcode.register_command(nome, getattr(self, "cmd_" + nome), desc=desc)

    def _build_config(self):
        bit_ticks = self.mcu.seconds_to_clock(1. / self.baud)
        self.mcu.add_config_cmd(
            "config_tmcuart oid=%d rx_pin=%s pull_up=%d tx_pin=%s bit_time=%d"
            % (self.oid, self.rx_pin, self.pullup, self.tx_pin, bit_ticks))
        self.invia = self.mcu.lookup_query_command(
            "tmcuart_send oid=%c write=%*s read=%c",
            "tmcuart_response oid=%c read=%*s", oid=self.oid,
            cq=self.cmd_queue, is_async=True)

    # --- trasmissione ---
    def manda(self, dati, leggi=0):
        # a pezzi da 8 caratteri; ogni pezzo aspetta la risposta della scheda, quindi i pezzi escono in fila
        letto = b""
        for i in range(0, len(dati), MAX_CARATTERI):
            with self.mutex:
                params = self.invia.send([self.oid, codifica(dati[i:i + MAX_CARATTERI]), leggi])
            letto = params.get("read", b"")
        return letto

    def parola(self, vp, valore):
        self.manda(bytes([0x5a, 0xa5, 5, 0x82, vp >> 8, vp & 0xff, (valore >> 8) & 0xff, valore & 0xff]))

    def leggi(self, vp):
        # byte alto della parola vp (la risposta arriva come A5 06 83 VH VL 01 DH: il resto non ci sta), o None
        r = decodifica(self.manda(bytes([0x5a, 0xa5, 4, 0x83, vp >> 8, vp & 0xff, 1]), 10))
        i = r.find(bytes([0x83, vp >> 8, vp & 0xff, 1]))
        return r[i + 4] if 0 <= i and i + 4 < len(r) else None

    def valore(self, vp, v):
        v = int(v) & 0xffff
        if self.scritti.get(vp) != v:
            self.parola(vp, v)
            self.scritti[vp] = v

    def testo(self, vp, s):
        # testo ASCII finito da zeri fino alla lunghezza del campo; si mandano solo le parole cambiate
        lung = self.lunghezze.get(vp, self.lunghezza_nome)
        b = s.encode("ascii", "replace")[:lung]
        b += b"\0" * (lung + (lung % 2) - len(b))
        prima = self.scritti.get(vp)
        for i in range(0, len(b), 2):
            if not isinstance(prima, bytes) or prima[i:i + 2] != b[i:i + 2]:
                self.parola(vp + i // 2, (b[i] << 8) | b[i + 1])
        self.scritti[vp] = b

    def vai_a_pagina(self, n, eventtime):
        self.parola(0x0085, n)
        self.parola(0x0084, 0x5a01)
        self.pagina, self.pagina_da = n, eventtime
        self.prossima_pagina = eventtime + PAGINA_OGNI

    def da_capo(self, eventtime):
        # schermo appena acceso (o riacceso): niente e' scritto, la pagina non si sa
        self.scritti, self.pagina, self.tutto_ora = {}, None, eventtime
        self.parola(self.VP["VIVO"], VIVO)
        self.parola(self.VP["TASTO"], 0)

    # --- giro ---
    def _pronto(self):
        self.heaters = (self.printer.lookup_object("extruder", None), self.printer.lookup_object("heater_bed", None))
        self.ps = self.printer.lookup_object("print_stats", None)
        self.sd = self.printer.lookup_object("virtual_sdcard", None)
        self.pagina = None
        self.tutto_ora = -2 * TUTTO_OGNI          # al primo giro riscrive tutto
        self.reactor.update_timer(self.timer, self.reactor.NOW)
        if self.filo is None:
            self.filo = threading.Thread(target=self._filo_file, name="schermo_flsun_file", daemon=True)
            self.filo.start()

    def _fermo(self, *args):
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _ciclo(self, eventtime):
        try:
            if eventtime - self.tutto_ora > TUTTO_OGNI:
                self.da_capo(eventtime)
            elif eventtime >= self.prossimo_vivo:
                self.prossimo_vivo = eventtime + VIVO_OGNI
                if self.leggi(self.VP["VIVO"]) == 0:
                    logging.info("schermo_flsun: lo schermo si e' riacceso, riscrivo tutto")
                    self.da_capo(eventtime)
            codice = self._leggi_tasto()
            if (codice and codice == self.ultimo_tasto and self.nomi_tasti.get(codice) not in RIPETIBILI
                    and eventtime - self.ultimo_tasto_da < DOPPIO):
                # 2026-10-18: Filamento e poi di nuovo Filamento, con lo schermo gia' sulla pagina Filamento. Era quasi
                # di sicuro l'azzeramento di VP_TASTO perso per strada (ora _leggi_tasto lo ricontrolla); questo resta
                # per sicurezza. Non per i RIPETIBILI: con la finestra che ripartiva a ogni tocco, ▼ toccato di fila
                # contava una volta sola (2026-10-19)
                logging.info("schermo_flsun: pulsante %s di nuovo subito, e' lo stesso tocco", codice)
            elif codice:
                self.ultimo_tasto, self.ultimo_tasto_da = codice, eventtime
                self.raffica_fino = eventtime + RAFFICA
                self._tasto(codice, eventtime)
                self.prossimo_aggiorna = 0.
            if eventtime >= self.prossimo_aggiorna or self.pagina is None:
                self._aggiorna(eventtime)
                self.prossimo_aggiorna = eventtime + self.intervallo
            if (not codice and self.pagina is not None and eventtime >= self.prossima_pagina
                  and not self.leggi(self.VP["TASTO"])):          # un tocco appena arrivato ha gia' cambiato pagina
                # sulla stampante vera, in stampa lo schermo e' tornato da solo alla pagina d'avvio SENZA perdere la
                # memoria (VP_VIVO ancora giusto), quindi niente ce lo dice: la pagina si rimanda ogni tanto.
                # Non con vai_a_pagina: pagina_da conta il tempo delle FINESTRE e non deve ripartire
                self.parola(0x0085, self.pagina)
                self.parola(0x0084, 0x5a01)
                self.prossima_pagina = eventtime + PAGINA_OGNI
        except Exception:
            logging.exception("schermo_flsun: giro non riuscito")
            self.scritti, self.pagina = {}, None
        ogni = self.tasti_in_stampa if self.ultima else self.tasti_ogni
        if eventtime < self.raffica_fino:
            ogni = min(ogni, RAFFICA_OGNI)
        return self.reactor.monotonic() + ogni

    def _leggi_tasto(self):
        a = self.leggi(self.VP["TASTO"])
        if not a:
            return None
        if self.leggi(self.VP["TASTO"]) != a:          # una lettura storta, o un altro tocco in mezzo: al prossimo giro
            return None
        # rimesso a 0 e riletto: alle scritture lo schermo non risponde, una disturbata si perde e il codice rimasto
        # in VP_TASTO al giro dopo sembrerebbe un tocco nuovo
        for _ in range(AZZERA_PROVE):
            self.parola(self.VP["TASTO"], 0)
            dopo = self.leggi(self.VP["TASTO"])
            if dopo == 0:
                break
            logging.info("schermo_flsun: VP_TASTO e' %s dopo l'azzeramento, lo rifaccio", dopo)
        self.scritti.pop(self.VP["TASTO"], None)
        return a

    # --- stampa e temperature ---
    def _temperature(self, eventtime):
        out = []
        for h in self.heaters or (None, None):
            st = h.get_status(eventtime) if h is not None else {"temperature": 0., "target": 0.}
            out.append((st["temperature"], st["target"]))
        return out

    def _stampa_klipper(self, eventtime, temperature):
        if self.ps is None:
            return None
        st = self.ps.get_status(eventtime)
        if st["state"] not in ("printing", "paused"):
            return None
        p = self.sd.get_status(eventtime)["progress"] if self.sd is not None else 0.
        resta = st["print_duration"] * (1. - p) / p if p > 0.01 else None
        # prima del primo filo estruso (print_duration ancora 0) con ugello o piatto sotto l'obiettivo: «Scalda»
        scalda = st["print_duration"] < 1. and any(ob > 0 and t < ob - 3 for t, ob in temperature)
        return {"fase": "pausa" if st["state"] == "paused" else "riscaldamento" if scalda else "stampa",
                "pct": 100. * p, "trascorso": st["total_duration"], "resta": resta, "filo": st["filament_used"],
                "nome": st["filename"]}

    def _completata(self, u, eventtime):
        st = self.ps.get_status(eventtime)
        if st["state"] != "complete":
            return None
        return dict(u, trascorso=st["total_duration"], filo=st["filament_used"])

    def _stampa(self, eventtime, temperature):
        s = self._stampa_klipper(eventtime, temperature)
        if s:
            if self.ultima is None:
                self.finita = None
            self.ultima = s
            return s
        if self.ultima is not None:
            u, self.ultima = self.ultima, None
            fatta = self._completata(u, eventtime)
            if fatta:
                self.finita = dict(fatta, piatto0=max(temperature[1][0], STACCA + 1.))
            logging.info("schermo_flsun: stampa %s %s", u["nome"], "finita" if fatta else "fermata")
            self._togli_z()
        return None

    def _togli_z(self):
        # finita o annullata: via la correzione di «Regola», valeva solo per questa stampa
        z, self.z_schermo = self.z_schermo, 0.
        if abs(z) < 1e-6:
            return

        def lavoro(eventtime):
            try:
                self.gcode.run_script("SET_GCODE_OFFSET Z_ADJUST=%.3f" % -z)
            except Exception as e:
                logging.info("schermo_flsun: correzione di Regola non tolta: %s", e)
        self.reactor.register_callback(lavoro)

    # --- pagine ---
    def _pagina_voluta(self, stampa, eventtime):
        P, ora = self.P, self.pagina
        dentro = eventtime - self.pagina_da
        if stampa:
            principale = P["PAUSA"] if stampa["fase"] == "pausa" else P["STAMPA"]
            if ora == P["ANNULLA"] and dentro < FINESTRE["ANNULLA"]:
                return ora
            if ora == P["REGOLA"] and principale == P["STAMPA"] and dentro < FINESTRE["REGOLA"]:
                return ora
            return principale
        if self.finita:
            return P["FINITA"]
        if self.avvio and self.avvio["stato"] == "controllo":     # la stampa sta partendo: non si scappa
            return P["AVVIA"]
        if ora == P["FILAMENTO"] or (ora == P["PRERISCALDA"] and dentro < FINESTRE["PRERISCALDA"]):
            return ora
        if ora in (P["FILE"], P["AVVIA"]) and dentro < FINESTRE["FILE"]:
            return ora
        return P["PRONTA"]

    def _aggiorna(self, eventtime):
        (ug, ug_ob), (pi, pi_ob) = self._temperature(eventtime)
        stampa = self._stampa(eventtime, ((ug, ug_ob), (pi, pi_ob)))
        pagina = self._pagina_voluta(stampa, eventtime)
        V, P = self.VP, self.P
        icone, campi, testi = {}, {}, {}

        def barra(t, ob):
            return int(round(min(max(t / ob, 0.), 1.) * 100)) if ob > 0 else 0
        sbaglio = self.errore and eventtime - self.errore[0] < ERRORE_PER
        if pagina == P["PRONTA"]:
            caldo = [(t, ob) for t, ob in ((ug, ug_ob), (pi, pi_ob)) if ob > 0]
            stato = (5 if sbaglio else 4 if self.occupato else 1 if any(t < ob - 3 for t, ob in caldo)
                     else 2 if caldo else 3 if ug > 50 or pi > 35 else 0)
            icone.update({V["STATO"]: stato, V["UGELLO_OB"]: min(int(round(ug_ob)), 300),
                          V["UGELLO_BARRA"]: barra(ug, ug_ob), V["PIATTO_OB"]: min(int(round(pi_ob)), 150),
                          V["PIATTO_BARRA"]: barra(pi, pi_ob)})
            campi.update(ugello="%d°" % round(ug), piatto="%d°" % round(pi))
        elif pagina in (P["STAMPA"], P["PAUSA"], P["ANNULLA"]) and stampa:
            icone.update({V["FASE"]: FASI.get(stampa["fase"], 0),
                          V["ANELLO"]: int(min(max(stampa["pct"] or 0., 0.), 100.))})
            o, m = ore_minuti(stampa["trascorso"])
            ro, rm = ore_minuti(stampa["resta"])
            fine = None
            if stampa["resta"] is not None:
                fine = time.strftime("%H:%M", time.localtime(time.time() + stampa["resta"]))
            fo, fm = fine.split(":") if fine else (TRATTINO * 2, TRATTINO * 2)
            fm_, fd = metri(stampa["filo"])
            campi.update(ore=o, minuti=m, resta_ore=ro, resta_min=rm, fine_ore=fo, fine_min=fm, filo_m=fm_,
                         filo_dec=fd, ugello="%d" % round(ug), piatto="%d" % round(pi),
                         ugello_ob="/ %d°" % round(ug_ob) if ug_ob > 0 else "",
                         piatto_ob="/ %d°" % round(pi_ob) if pi_ob > 0 else "")
            testi[V["NOME"]] = nome_corto(stampa["nome"], self.colonne_nome)
        elif pagina == P["FILAMENTO"]:
            scalda = ug_ob > 0 and ug < ug_ob - 3
            sotto = (6 if sbaglio else 1 if self.occupato in ("carico", "scarico", "muovo") and scalda
                     else {"carico": 3, "scarico": 4, "muovo": 5}.get(self.occupato)
                     or (0 if ug_ob <= 0 else 1 if scalda else 2))
            icone.update({V["MATERIALE"]: self.materiale, V["SOTTO_FILO"]: sotto, V["UGELLO_BARRA"]: barra(ug, ug_ob),
                          V["PASSO"]: self.passo})
            campi.update(ugello="%d" % round(ug), ugello_ob="/ %d°" % round(ug_ob) if ug_ob > 0 else "")
        elif pagina == P["FINITA"] and self.finita:
            f = self.finita
            o, m = ore_minuti(f["trascorso"])
            fm_, fd = metri(f["filo"])
            t0 = max(f["piatto0"], pi)
            icone.update({V["SOTTO_PIATTO"]: 1 if pi <= STACCA else 0,
                          V["RAFFREDDA_BARRA"]: int(round(min(max((t0 - pi) / (t0 - STACCA), 0.), 1.) * 100))})
            campi.update(ore=o, minuti=m, filo_m=fm_, filo_dec=fd, piatto="%d" % round(pi))
            n = nome_corto(f["nome"], self.colonne_nome)
            testi[V["NOME_CENTRO"]] = " " * ((self.colonne_nome - len(n)) // 2) + n
        elif pagina == P["FILE"]:
            self._pagina_file(icone, testi)
        elif pagina == P["AVVIA"]:
            self._pagina_avvia(testi)
        elif pagina == P["REGOLA"]:
            z = self._offset_z(eventtime)
            avviso = 2 if sbaglio else 1 if self.avviso_z and eventtime - self.avviso_z < AVVISO_PER else 0
            icone.update({V["ZPASSO"]: self.zpasso, V["SOTTO_Z"]: avviso})
            c = "%.2f" % min(abs(z), 9.99)
            campi.update(z_segno="" if c == "0.00" else "+" if z > 0 else "−", z_int=c[0], z_dec=c[2:])
        # prima i numeri, poi la pagina: compare gia' riempita
        for vp, v in icone.items():
            self.valore(vp, v)
        gruppo = self.campi.get(self.gruppi.get(pagina, ""), {})
        for nome, t in campi.items():
            if nome in gruppo:
                for vp, v in valori_campo(gruppo[nome], t).items():
                    self.valore(vp, v)
        for vp, t in testi.items():
            self.testo(vp, t)
        if pagina != self.pagina or self.mostra:
            self.mostra = False
            self.vai_a_pagina(pagina, eventtime)

    def _voci(self):
        # gruppo 0 «Recenti»: i piu' nuovi prima; gruppo 1 «A–Z»: per nome (l'elenco arriva gia' cosi')
        voci = list(self.elenco[1] or [])
        if self.gruppo == 0:
            voci.sort(key=lambda v: -v["modificato"])
        return voci

    def _pagina_file(self, icone, testi):
        V, n = self.VP, self.righe_lista
        voci = self._voci()
        pagine = max(1, (len(voci) + n - 1) // n)
        self.pagina_lista = min(max(self.pagina_lista, 0), pagine - 1)
        icone[V["GRUPPO"]] = self.gruppo
        quando, tutte, errore = self.elenco
        brevi = nomi_riga([v.get("nome") for v in voci], COLONNE_RIGA)
        if self.sd is None:
            avviso = self.msg["manca_sd"]
        elif tutte is None:
            avviso = self.msg["non_leggo"] if errore and not self.elenco_chiesto else self.msg["carico_elenco"]
        else:
            avviso = None if voci else self.msg["nessun_file"]
        for i in range(n):
            k = self.pagina_lista * n + i
            v = voci[k] if avviso is None and k < len(voci) else None
            icone[V["CARTA_RIGA"] + i] = 0 if v else self.nascosto
            if v:
                info = "  ".join(x for x in (durata(v["minuti"]) if v.get("minuti") else "", quanto_filo(v, self.decimale),
                                             in_ascii(v.get("filamento") or "")) if x)
                nome = brevi[k]
            else:
                nome, info = (avviso if i == 0 and avviso else ""), ""
            testi[V["LISTA_NOME"] + 0x20 * i] = nome
            testi[V["LISTA_INFO"] + 0x20 * i] = info
        testi[V["LISTA_PAGINA"]] = centrato("%d/%d" % (self.pagina_lista + 1, pagine), 8) if voci and not avviso else ""

    def _pagina_avvia(self, testi):
        V, v = self.VP, self.scelto or {}
        n = in_ascii(nome_corto(v.get("nome"), 999))
        if len(n) > 2 * COLONNE_RIGA:
            n = n[:2 * COLONNE_RIGA - 18] + ".." + n[-16:]
        testi.update({V["AVVIA_NOME"]: n[:COLONNE_RIGA], V["AVVIA_NOME2"]: n[COLONNE_RIGA:],
                      V["AVVIA_DURATA"]: in_ascii(durata(v.get("minuti"))),
                      V["AVVIA_FILO"]: quanto_filo(v, self.decimale) or "-",
                      V["AVVIA_MATERIALE"]: in_ascii(v.get("filamento") or "-")[:10],
                      V["AVVIA_TEMP"]: "%d / %d" % (round(v["ugello"]), round(v["piatto"]))
                      if v.get("ugello") and v.get("piatto") is not None else "-"})
        a = self.avvio or {}
        msg = a.get("messaggio", "")
        righe = a_capo(msg[:1].upper() + msg[1:], COLONNE_MSG)
        rosso = a.get("stato") == "errore"
        for vp, vp2, scrivi in ((V["AVVIA_MSG"], V["AVVIA_MSG2"], not rosso), (V["AVVIA_ERR"], V["AVVIA_ERR2"], rosso)):
            testi[vp], testi[vp2] = righe if scrivi else ("", "")

    def _offset_z(self, eventtime):
        gm = self.printer.lookup_object("gcode_move", None)
        return gm.get_status(eventtime)["homing_origin"][2] if gm is not None else 0.

    def _chiedi_elenco(self):
        if self.sd is not None and not self.elenco_chiesto:
            self.elenco_chiesto = True
            self.richieste.put("elenco")

    # --- pulsanti ---
    def _tasto(self, codice, eventtime):
        nome = self.nomi_tasti.get(codice)
        logging.info("schermo_flsun: pulsante %s (%s) sulla pagina %s, %.1f s dopo il tocco prima", codice, nome,
                     self.pagina, eventtime - self.tasto_prima)
        self.tasto_prima = eventtime
        if nome is None:
            return
        P = self.P
        stampa = self.ultima
        # dove si e' arrivati (lo schermo ha gia' cambiato pagina da solo, se il pulsante lo fa)
        dopo = {"APRI_FILAMENTO": "FILAMENTO", "APRI_PRERISCALDA": "PRERISCALDA", "APRI_ANNULLA": "ANNULLA",
                "PLA": "FILAMENTO", "PETG": "FILAMENTO", "CARICA": "FILAMENTO", "SCARICA": "FILAMENTO",
                "PASSO_1": "FILAMENTO", "PASSO_10": "FILAMENTO", "PASSO_50": "FILAMENTO", "ARRETRA": "FILAMENTO",
                "AVANZA": "FILAMENTO", "PAUSA": "STAMPA", "RIPRENDI": "PAUSA", "ANNULLA_SI": "STAMPA",
                "ANNULLA_NO": "STAMPA", "APRI_FILE": "FILE", "GRUPPO_RECENTI": "FILE", "GRUPPO_AZ": "FILE",
                "FILE_SU": "FILE", "FILE_GIU": "FILE", "AVVIA_SI": "AVVIA", "AVVIA_NO": "FILE",
                "APRI_REGOLA": "REGOLA", "REGOLA_INDIETRO": "STAMPA", "Z_GIU": "REGOLA", "Z_SU": "REGOLA",
                "ZPASSO_1": "REGOLA", "ZPASSO_2": "REGOLA", "ZPASSO_3": "REGOLA"}.get(nome, "PRONTA")
        if nome.startswith("FILE_") and nome[5:].isdigit():
            dopo = "FILE"
        self.pagina, self.pagina_da = P[dopo], eventtime
        if nome.startswith("PASSO_"):
            self.passo = {"PASSO_1": 0, "PASSO_10": 1, "PASSO_50": 2}[nome]
        elif nome == "OK":
            self.finita = None
        elif nome == "RAFFREDDA":
            self._gcode(None, "TURN_OFF_HEATERS")
            self.materiale = 0
        elif nome in ("PRERISCALDA_PLA", "PRERISCALDA_PETG"):
            _, u, p = MATERIALI[1 if nome.endswith("PLA") else 2]
            self._gcode(None, "M104 S%d\nM140 S%d" % (u, p))
        elif nome in ("PLA", "PETG"):
            self.materiale = 1 if nome == "PLA" else 2
            self._gcode(None, "M104 S%d" % MATERIALI[self.materiale][1])
        elif nome == "FATTO":
            self._gcode(None, "M104 S0")
            self.materiale = 0
        elif nome == "AZZERA":
            if not stampa:
                self._gcode("azzero", "G28")
        elif nome in ("CARICA", "SCARICA", "ARRETRA", "AVANZA"):
            if stampa:
                return
            if not self.materiale:
                self.materiale = 1
            t = MATERIALI[self.materiale][1]
            if nome in ("ARRETRA", "AVANZA"):
                ob = self._temperature(eventtime)[0][1]
                t = int(round(ob)) if ob > 0 else t
                mm = PASSI[self.passo] * (1 if nome == "AVANZA" else -1)
                mosse, cosa = "G1 E%.1f F%d" % (mm, MUOVI_F), "muovo"
            elif nome == "CARICA":
                mosse, cosa = "G1 E%.1f F%d\nG1 E%.1f F%d" % (CARICA_MM, CARICA_F, SPURGO_MM, SPURGO_F), "carico"
            else:
                mosse, cosa = "G1 E10 F300\nG1 E-%.1f F%d" % (SCARICA_MM, SCARICA_F), "scarico"
            self._gcode(cosa, "SAVE_GCODE_STATE NAME=schermo_filo\nM104 S%d\nM109 S%d\nM83\n%s\nM400\n"
                              "RESTORE_GCODE_STATE NAME=schermo_filo" % (t, t, mosse))
        elif nome == "APRI_FILE":
            if stampa:
                return
            self.pagina_lista, self.scelto = 0, None
            if not (self.avvio and self.avvio["stato"] == "controllo"):
                self.avvio = None
            if self.elenco[1] is None or time.time() - self.elenco[0] > ELENCO_VECCHIO:
                self._chiedi_elenco()
        elif nome in ("GRUPPO_RECENTI", "GRUPPO_AZ"):
            self.gruppo, self.pagina_lista = (0 if nome == "GRUPPO_RECENTI" else 1), 0
        elif nome in ("FILE_SU", "FILE_GIU"):
            self.pagina_lista += -1 if nome == "FILE_SU" else 1          # _pagina_file lo tiene tra prima e ultima
        elif nome.startswith("FILE_") and nome[5:].isdigit():
            voci = self._voci() if self.elenco[1] is not None else []
            k = self.pagina_lista * self.righe_lista + int(nome[5:]) - 1
            if stampa or k >= len(voci):                     # riga vuota
                return
            self.scelto, self.avvio = voci[k], None
            # lo schermo resta sull'elenco: prima nome e dati del file, poi la pagina (nel giro, subito dopo)
            self.pagina, self.mostra = P["AVVIA"], True
        elif nome == "AVVIA_SI":
            if stampa or not self.scelto or (self.avvio and self.avvio["stato"] in ("controllo", "ok")):
                return
            self._avvia(self.scelto["percorso"])
        elif nome == "AVVIA_NO":
            if self.avvio and self.avvio["stato"] == "controllo":
                return                                           # _pagina_voluta lo riporta su «avvia»
            self.avvio = None
        elif nome in ("Z_GIU", "Z_SU"):
            if not stampa:
                return
            passo = PASSI_Z[self.zpasso] * (-1 if nome == "Z_GIU" else 1)
            if abs(self._offset_z(eventtime) + passo) > LIMITE_Z + 1e-6:
                self.avviso_z = eventtime
                return
            self.z_schermo += passo

            def no(passo=passo):
                self.z_schermo -= passo
            self._gcode(None, "SET_GCODE_OFFSET Z_ADJUST=%.3f MOVE=1" % passo, no)
        elif nome.startswith("ZPASSO_"):
            self.zpasso = int(nome[7:]) - 1
        elif nome in ("PAUSA", "RIPRENDI", "ANNULLA_SI") and stampa:
            self._gcode(None, {"PAUSA": "PAUSE", "RIPRENDI": "RESUME", "ANNULLA_SI": "CANCEL_PRINT"}[nome])

    def _gcode(self, cosa, script, se_no=None):
        # una azione per volta: mentre ne va una (homing, carica, scarica, muovi) le altre si ignorano. se_no() se non
        # parte o non riesce
        if self.occupato:
            logging.info("schermo_flsun: occupato (%s), ignoro %r", self.occupato, script)
            if se_no:
                se_no()
            return
        self.occupato = cosa or "comando"

        def lavoro(eventtime):
            try:
                self.gcode.run_script(script)
            except Exception as e:
                logging.info("schermo_flsun: %r non riuscito: %s", script, e)
                self.errore = (self.reactor.monotonic(), str(e))
                self.gcode.respond_info("Schermo: %s" % e)
                if se_no:
                    se_no()
            finally:
                self.occupato = None
                self.prossimo_aggiorna = 0.
        self.reactor.register_callback(lavoro)

    def _avvia(self, percorso):
        # come Moonraker quando si stampa da Mainsail o Fluidd
        if self.occupato:
            self.avvio = {"stato": "errore", "messaggio": self.msg["occupata"]}
            return
        self.avvio = {"stato": "controllo", "messaggio": self.msg["avvio"]}
        logging.info("schermo_flsun: avvio %s dallo schermo", percorso)

        def lavoro(eventtime):
            try:
                self.gcode.run_script('SDCARD_PRINT_FILE FILENAME="%s"' % percorso)
                self.avvio = {"stato": "ok", "messaggio": self.msg["avviata"]}
            except Exception as e:
                logging.info("schermo_flsun: %s non avviato: %s", percorso, e)
                self.avvio = {"stato": "errore", "messaggio": str(e)}
            self.prossimo_aggiorna = 0.
        self.reactor.register_callback(lavoro)

    # --- elenco dei file (filo a parte: legge la cartella e chiede a Moonraker, di Klipper cambia solo self.elenco) ---
    def _filo_file(self):
        while True:
            self.richieste.get()
            try:
                voci = self._leggi_file()
                self.elenco = (time.time(), voci, None)
            except Exception as e:
                logging.exception("schermo_flsun: elenco dei file non riuscito")
                voci, self.elenco = None, (time.time(), self.elenco[1], str(e))
            self.elenco_chiesto = False
            self.prossimo_aggiorna = 0.
            if voci and self.moonraker:
                self._dati_file(voci)

    def _leggi_file(self):
        cartella, voci = self.sd.sdcard_dirname, []
        for percorso, _ in self.sd.get_file_list(check_subdirs=True):
            try:
                quando = os.path.getmtime(os.path.join(cartella, percorso))
            except OSError:
                continue
            voci.append({"percorso": percorso, "nome": percorso.rsplit("/", 1)[-1], "modificato": quando})
        voci.sort(key=lambda v: (v["nome"].lower(), v["percorso"]))
        return voci

    def _dati_file(self, voci):
        # durata, filo, materiale e temperature come li ha letti Moonraker dal G-code; prima i file piu' recenti
        for v in sorted(voci, key=lambda v: -v["modificato"])[:MAX_DATI]:
            chiave = (v["percorso"], v["modificato"])
            if chiave not in self.dati_file:
                url = "%s/server/files/metadata?filename=%s" % (self.moonraker, urllib.parse.quote(v["percorso"]))
                try:
                    with urllib.request.urlopen(url, timeout=5) as f:
                        self.dati_file[chiave] = dati_moonraker(json.loads(f.read()).get("result") or {})
                except urllib.error.HTTPError as e:            # file che Moonraker non conosce (ancora)
                    logging.info("schermo_flsun: Moonraker non ha i dati di %s: %s", v["percorso"], e)
                    self.dati_file[chiave] = {}
                except Exception as e:                         # Moonraker spento: per ora solo i nomi
                    logging.info("schermo_flsun: Moonraker non risponde: %s", e)
                    return
            v.update(self.dati_file[chiave])
            self.prossimo_aggiorna = 0.

    # --- comandi ---
    def cmd_SCHERMO_TASTO(self, gcmd):
        self._tasto(gcmd.get_int("CODICE", minval=1, maxval=255), self.reactor.monotonic())
        self.prossimo_aggiorna = 0.

    def cmd_SCHERMO_PAGINA(self, gcmd):
        self.vai_a_pagina(gcmd.get_int("PAGINA", minval=0, maxval=255), self.reactor.monotonic())

    def cmd_SCHERMO_STATO(self, gcmd):
        gcmd.respond_info("Schermo: pagina %s, materiale %s, passo %g mm, azione %s, stampa %s, finita %s, errore %s, "
                          "file %s, avvio %s, file nell'elenco %s"
                          % (self.nomi_pagine.get(self.pagina, self.pagina), self.materiale, PASSI[self.passo],
                             self.occupato, self.ultima and self.ultima["nome"], self.finita and self.finita["nome"],
                             self.errore and self.errore[1], self.scelto and self.scelto.get("nome"),
                             self.avvio, None if self.elenco[1] is None else len(self.elenco[1])))

    def _byte(self, gcmd):
        testo = gcmd.get("TESTO")
        return testo.encode("latin-1", "replace").decode("unicode_escape").encode("latin-1", "replace")

    def cmd_SCHERMO_MANDA(self, gcmd):
        dati = self._byte(gcmd)
        self.manda(dati)
        gcmd.respond_info("Mandati allo schermo %d caratteri: %r" % (len(dati), dati))

    def cmd_SCHERMO_CHIEDI(self, gcmd):
        dati = self._byte(gcmd)
        if len(dati) > MAX_CARATTERI:
            raise gcmd.error("SCHERMO_CHIEDI: al massimo %d byte" % MAX_CARATTERI)
        prove = gcmd.get_int("PROVE", 1, minval=1, maxval=200)
        visti = {}
        for _ in range(prove):
            r = decodifica(self.manda(dati, 10))
            visti[r] = visti.get(r, 0) + 1
        righe = ["%3dx %s" % (n, r.hex(" ") or "(niente)") for r, n in sorted(visti.items(), key=lambda v: -v[1])]
        gcmd.respond_info("Chiesto %s\n%s" % (dati.hex(" "), "\n".join(righe)))


def load_config(config):
    return SchermoFlsun(config)
