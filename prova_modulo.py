"""Prova di schermo_flsun.py (grafica nuova) senza stampante né schermo.

Una Klipper finta (reactor, riscaldatori, print_stats, G-code che registra i comandi, virtual_sdcard con file veri in una
cartella temporanea), un Moonraker finto per i dati dei file e uno schermo finto che legge i
file generati da genera.py: tiene la memoria dei VP, cambia pagina con 0x0084, risponde alle letture come lo schermo
vero attraverso tmcuart (solo A5 06 83 VH VL 01 DH) e i tocchi fanno quello che dice 13TouchFile.bin (codice in
VP_TASTO, pagina dopo). A ogni passo ridisegna cosa mostrerebbe lo schermo (genera.ricomponi) in verifica/modulo/.

Uso:  python3 prova_modulo.py [--lingua it|en|fr|es|de]      (serve Pillow; non tocca stampante né schermo)
"""
import http.server
import importlib
import json
import os
import shlex
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.parse

from PIL import Image, ImageDraw, ImageFont

QUI = os.path.dirname(os.path.abspath(__file__))
LINGUA = sys.argv[sys.argv.index("--lingua") + 1] if "--lingua" in sys.argv else "it"
os.environ["FLSUN_LINGUA"] = LINGUA
sys.path.insert(0, QUI)
import genera  # noqa: E402

CARTELLA = os.path.join(QUI, "lingue", LINGUA)          # la grafica e mappa.json di questa lingua (da genera.py)
MAPPA = json.load(open(os.path.join(CARTELLA, "mappa.json"), encoding="utf-8"))
MSG = {k: genera.in_ascii(v) for k, v in MAPPA["messaggi"].items()}
DEC = MAPPA["decimale"]
USCITA = os.path.join(QUI, "verifica", "modulo" if LINGUA == "it" else "modulo_" + LINGUA)


# ---------------------------------------------------------------- Klipper finta

class Orologio:
    t = 1000.


class Reactor:
    NOW, NEVER = 0., 9e99

    def __init__(self, orologio):
        self.o, self.callback = orologio, []

    def register_timer(self, f):
        return f

    def update_timer(self, t, quando):
        pass

    def register_callback(self, f):
        self.callback.append(f)

    def monotonic(self):
        return self.o.t

    def pause(self, t):
        return t


class Riscaldatore:
    def __init__(self, t):
        self.temperature, self.target = t, 0.

    def get_status(self, eventtime):
        return {"temperature": self.temperature, "target": self.target}


class PrintStats:
    def __init__(self):
        self.state, self.total_duration, self.print_duration = "standby", 0., 0.
        self.filament_used, self.filename = 0., ""

    def get_status(self, eventtime):
        return dict(state=self.state, total_duration=self.total_duration, print_duration=self.print_duration,
                    filament_used=self.filament_used, filename=self.filename)


class SdCard:
    progress = 0.
    sdcard_dirname = None

    def get_status(self, eventtime):
        return {"progress": self.progress}

    def get_file_list(self, check_subdirs=False):
        # come virtual_sdcard; un po' lenta, cosi' «Carico l'elenco...» si vede
        time.sleep(0.2)
        flist = []
        for root, dirs, files in os.walk(self.sdcard_dirname, followlinks=True):
            for name in files:
                if name[name.rfind(".") + 1:] not in ("gcode", "g", "gco"):
                    continue
                full = os.path.join(root, name)
                flist.append((full[len(self.sdcard_dirname) + 1:], os.path.getsize(full)))
        return sorted(flist, key=lambda f: f[0].lower())


class Errore(Exception):
    pass


class GCodeMove:
    z = 0.

    def get_status(self, eventtime):
        return {"homing_origin": [0., 0., self.z, 0.]}


class GCode:
    def __init__(self, stampante):
        self.s, self.comandi, self.fatti, self.risposte = stampante, {}, [], []

    def register_command(self, nome, f, desc=None):
        self.comandi[nome] = f

    def respond_info(self, t):
        self.risposte.append(t)

    def run_script(self, script):
        ug, pi, ps = self.s.oggetti["extruder"], self.s.oggetti["heater_bed"], self.s.oggetti["print_stats"]
        for riga in script.splitlines():
            self.fatti.append(riga)
            p = riga.split()
            if not p:
                continue
            if p[0] == "SDCARD_PRINT_FILE":
                nome = dict(x.split("=", 1) for x in shlex.split(riga)[1:])["FILENAME"]
                if self.s.fallisci:
                    raise Errore(self.s.fallisci)
                ps.state, ps.filename, ps.total_duration, ps.print_duration, ps.filament_used = \
                    "printing", nome, 0., 0., 0.
                self.s.oggetti["virtual_sdcard"].progress = 0.
                continue
            arg = dict((x[0], float(x[1:])) for x in p[1:] if x[:1].isalpha() and x[1:].replace(".", "").replace("-", "").isdigit())
            if p[0] == "M104":
                ug.target = arg["S"]
            elif p[0] == "M140":
                pi.target = arg["S"]
            elif p[0] == "M109":
                ug.target = ug.temperature = arg["S"]
            elif p[0] == "TURN_OFF_HEATERS":
                ug.target = pi.target = 0.
            elif p[0] == "PAUSE":
                ps.state = "paused"
            elif p[0] == "RESUME":
                ps.state = "printing"
            elif p[0] == "CANCEL_PRINT":
                ps.state = "cancelled"
            elif p[0] == "G1" and "E" in arg and ug.temperature < 170:
                raise Errore("Extrude below minimum temp")
            elif p[0] == "SET_GCODE_OFFSET":
                adj = dict(x.split("=") for x in p[1:])
                self.s.oggetti["gcode_move"].z += float(adj["Z_ADJUST"])


class Config:
    error = Errore

    def __init__(self, stampante, valori):
        self.s, self.v = stampante, valori

    def get_printer(self):
        return self.s

    def get(self, nome, predefinito=None):
        return self.v.get(nome, predefinito)

    def getint(self, nome, predefinito=None, **k):
        return int(self.v.get(nome, predefinito))

    def getfloat(self, nome, predefinito=None, **k):
        return float(self.v.get(nome, predefinito))


class Invia:
    def __init__(self, schermo):
        self.schermo = schermo

    def send(self, params):
        return {"read": self.schermo.ricevi(params[1], params[2])}


class Mcu:
    def __init__(self, schermo):
        self.schermo, self.config = schermo, []

    def create_oid(self):
        return 7

    def alloc_command_queue(self):
        return None

    def register_config_callback(self, f):
        self.config.append(f)

    def seconds_to_clock(self, s):
        return int(s * 168e6)

    def add_config_cmd(self, c):
        self.cmd = c

    def lookup_query_command(self, *a, **k):
        return Invia(self.schermo)


class Pins:
    def __init__(self, mcu):
        self.mcu = mcu

    def lookup_pin(self, nome, can_pullup=False):
        return {"chip": self.mcu, "pin": nome, "pullup": 0}


class Stampante:
    def __init__(self, orologio, schermo):
        self.reactor = Reactor(orologio)
        self.mcu = Mcu(schermo)
        self.oggetti = {"pins": Pins(self.mcu), "extruder": Riscaldatore(24.), "heater_bed": Riscaldatore(23.),
                        "print_stats": PrintStats(), "virtual_sdcard": SdCard(), "gcode_move": GCodeMove()}
        self.oggetti["gcode"] = GCode(self)
        self.eventi = {}
        self.fallisci = None       # messaggio d'errore di SDCARD_PRINT_FILE

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, nome, predefinito=None):
        return self.oggetti.get(nome, predefinito)

    def register_event_handler(self, nome, f):
        self.eventi[nome] = f


# ---------------------------------------------------------------- schermo finto

def bit_in_byte(grezzo):
    """Il contrario di codifica(): i caratteri dentro i bit impacchettati per tmcuart."""
    bit = [(b >> i) & 1 for b in bytearray(grezzo) for i in range(8)]
    return bytes(sum(bit[i + 1 + k] << k for k in range(8)) for i in range(0, len(bit) - 9, 10))


class Schermo:
    def __init__(self, cartella):
        self.tocchi = open(os.path.join(cartella, "13TouchFile.bin"), "rb").read()
        self.riaccendi()
        self.scritture = 0
        self.perdi = 0             # scritture di 0 in VP_TASTO da perdere (disturbate sulla seriale)

    def riaccendi(self):
        self.mem, self.pagina, self.buffer = {}, 0, b""

    def ricevi(self, dati, leggi):
        mod = importlib.import_module("extras.schermo_flsun")
        b = bit_in_byte(dati)
        assert mod.codifica(b)[:len(dati)] == bytearray(dati)
        self.buffer += b
        risposta = b""
        while len(self.buffer) >= 3:
            assert self.buffer[:2] == b"\x5a\xa5", self.buffer.hex()
            n = self.buffer[2]
            if len(self.buffer) < 3 + n:
                break
            f, self.buffer = self.buffer[3:3 + n], self.buffer[3 + n:]
            vp = f[1] << 8 | f[2]
            if f[0] == 0x82:
                if vp == genera.VP["TASTO"] and f[3:5] == b"\0\0" and self.perdi:
                    self.perdi -= 1
                    continue
                for k in range((len(f) - 3) // 2):
                    self.mem[vp + k] = f[3 + 2 * k] << 8 | f[4 + 2 * k]
                    self.scritture += 1
                if vp == 0x0084 and self.mem[0x0084] == 0x5a01:
                    self.pagina = self.mem.get(0x0085, 0)
                    self.mem[0x0084] = 0
            elif f[0] == 0x83:
                risposta = bytes([0xa5, 0x06, 0x83, f[1], f[2], 0x01, self.mem.get(vp, 0) >> 8])
        if leggi and risposta:
            return bytes(mod.codifica(risposta))
        return b""

    def tocca(self, x, y):
        i = 0
        while i + 16 <= len(self.tocchi) and self.tocchi[i:i + 2] != b"\xff\xff":
            pag, x0, y0, x1, y1, dopo, on, cod = struct.unpack(">8H", self.tocchi[i:i + 16])
            if pag == self.pagina and x0 <= x <= x1 and y0 <= y <= y1:
                if cod == 0xFE02:
                    e = self.tocchi[i + 16:i + 32]
                    vp = e[1] << 8 | e[2]
                    passo, minimo, massimo = struct.unpack(">3H", e[6:12])
                    assert e[3:6] == b"\x00\x01\x00"
                    self.mem[vp] = min(self.mem.get(vp, 0) + passo, massimo)
                if dopo != 0xFF00:
                    self.pagina = dopo
                return True
            i += 32 if cod else 16
        raise AssertionError(f"niente da toccare in {x},{y} sulla pagina {self.pagina}")

    def testo(self, vp):
        b = bytearray()
        for k in range(15):
            w = self.mem.get(vp + k, 0)
            b += bytes([w >> 8, w & 0xff])
        return b.split(b"\0")[0].decode("ascii", "replace")

    def foto(self):
        testi = MAPPA["testi"]
        im = genera.ricomponi(self.pagina, dict(self.mem), {int(vp): self.testo(int(vp)) for vp in testi},
                              os.path.join(CARTELLA, "DWIN_SET"))
        return im


# ---------------------------------------------------------------- prova

def main():
    shutil.rmtree(USCITA, ignore_errors=True)
    os.makedirs(USCITA)
    tmp = tempfile.mkdtemp()
    pk = os.path.join(tmp, "extras")
    os.makedirs(pk)
    open(os.path.join(pk, "__init__.py"), "w").close()
    open(os.path.join(pk, "tmc_uart.py"), "w").write(
        "import threading\n_m = threading.Lock()\ndef lookup_tmc_uart_mutex(mcu):\n    return _m\n")
    shutil.copy(os.path.join(QUI, "schermo_flsun.py"), pk)
    shutil.copy(os.path.join(CARTELLA, "mappa.json"), os.path.join(pk, "schermo_flsun_mappa.json"))
    sys.path.insert(0, tmp)
    mod = importlib.import_module("extras.schermo_flsun")

    # file in virtual_sdcard e Moonraker finto (per l'ultima parte)
    cartella = os.path.join(tmp, "gcodes")
    os.makedirs(os.path.join(cartella, "calibration"))
    os.makedirs(os.path.join(cartella, ".thumbs"))
    open(os.path.join(cartella, ".thumbs", "phone_stand_light.png"), "w").close()     # non e' un G-code
    dati, adesso = {}, time.time()

    def file_finto(percorso, quando, d):
        f = os.path.join(cartella, percorso)
        open(f, "w").write("G28\n")
        os.utime(f, (quando, quando))
        if d:
            dati[percorso] = d
    for k, (n, grammi, minuti) in enumerate((("lamp_base_with_cable_channel_v2", 238, 797),
                                             ("spool_holder_with_bearing_608", 105, 293),
                                             ("spool_holder_with_bearing_688", 120, 377),
                                             ("cable_clip_8mm_set10", 4, 13), ("phone_stand_light", 130, 433),
                                             ("phone_stand_light_gyroid15", 124, 450), ("headphone_hook_v4", 0, 0))):
        file_finto(n + ".gcode", adesso - 60 * k, grammi and {
            "estimated_time": 60. * minuti, "filament_weight_total": grammi, "filament_total": grammi / 2.98 * 1000,
            "filament_type": "PLA", "first_layer_extr_temp": 220., "first_layer_bed_temp": 60.})
    for k in range(250, 262):                 # filo in metri: lo slicer non ha scritto il peso
        file_finto("calibration/flow_test_petg_%d_fine_layers.gcode" % k, adesso - 3600 - 60 * (261 - k), {
            "estimated_time": 840., "filament_total": 1140., "filament_type": "PETG", "first_layer_extr_temp": 250.,
            "first_layer_bed_temp": 80.})
    chiesti = []

    class Moonraker(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            nome = urllib.parse.parse_qs(u.query).get("filename", [""])[0]
            chiesti.append(nome)
            if u.path != "/server/files/metadata" or nome not in dati:
                self.send_error(404)
                return
            corpo = json.dumps({"result": dict(dati[nome], filename=nome)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Moonraker)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    o = Orologio()
    schermo = Schermo(os.path.join(CARTELLA, "DWIN_SET"))
    s = Stampante(o, schermo)
    s.oggetti["virtual_sdcard"].sdcard_dirname = cartella
    m = mod.SchermoFlsun(Config(s, {"moonraker": "http://127.0.0.1:%d" % server.server_port}))
    for f in s.mcu.config:
        f()
    s.eventi["klippy:ready"]()
    g, ug, pi, ps, sd = (s.oggetti[k] for k in ("gcode", "extruder", "heater_bed", "print_stats", "virtual_sdcard"))
    foto, controlli = [], []

    prossimo = [None]                         # tra quanto il modulo rilegge i pulsanti

    def giro(secondi=1.1, callback=True):
        o.t += secondi
        prossimo[0] = m._ciclo(o.t) - o.t
        if callback:
            while s.reactor.callback:
                s.reactor.callback.pop(0)(o.t)

    def scatta(titolo):
        foto.append((titolo, schermo.foto()))

    def verifica(cond, testo):
        controlli.append((bool(cond), testo))
        if not cond:
            print("NO:", testo)

    P, T = genera, genera.TASTI
    giro()
    verifica(schermo.pagina == P.PRONTA, "all'avvio: pagina pronta")
    verifica(schermo.mem.get(genera.VP["VIVO"]) == 0x5a00, "VIVO scritto")
    scatta("Avvio: pronta")

    # preriscalda PLA
    schermo.tocca(240, 392)
    verifica(schermo.pagina == P.PRERISCALDA, "tocco Preriscalda: lo schermo va da solo alla pagina")
    giro()
    verifica(m.pagina == P.PRERISCALDA, "Klipper sa che è sulla pagina Preriscalda")
    verifica(schermo.mem[genera.VP["TASTO"]] == 0, "VP_TASTO rimesso a 0")
    scatta("Preriscalda")
    schermo.tocca(160, 110)
    giro()
    verifica(ug.target == 220 and pi.target == 60, "PLA: ugello 220, piatto 60")
    verifica(schermo.pagina == P.PRONTA, "dopo PLA torna a pronta")
    ug.temperature, pi.temperature = 187.4, 58.
    giro()
    verifica(schermo.mem[genera.VP["STATO"]] == 1, "pillola «Scalda»")
    scatta("Scalda (PLA)")
    ug.temperature, pi.temperature = 219.6, 60.2
    giro()
    verifica(schermo.mem[genera.VP["STATO"]] == 2, "pillola «In temperatura»")

    # la pagina Preriscalda si chiude da sola dopo un minuto
    schermo.tocca(240, 392)
    giro()
    for _ in range(60):
        giro()
    verifica(schermo.pagina == P.PRONTA and m.pagina == P.PRONTA, "Preriscalda si chiude da sola dopo 60 s")

    # filamento
    schermo.tocca(80, 392)
    giro()
    verifica(m.pagina == P.FILAMENTO, "tocco Filamento")
    schermo.tocca(230, 80)
    giro()
    verifica(m.materiale == 2 and ug.target == 250, "PETG: 250°")
    ug.temperature = 231.
    giro()
    verifica(schermo.mem[genera.VP["SOTTO_FILO"]] == 1, "sotto «Scaldo…»")
    scatta("Filamento PETG: scaldo")
    schermo.tocca(80, 240)                    # Carica
    giro(callback=False)
    verifica(m.occupato == "carico", "Carica: azione in corso")
    schermo.tocca(230, 240)                   # Scarica mentre carica: ignorato
    giro(callback=False)
    giro(callback=False)
    ug.temperature = 250.
    giro(callback=False)
    verifica(schermo.mem[genera.VP["SOTTO_FILO"]] == 3, "sotto «Carico…»")
    scatta("Carico…")
    giro()
    verifica(m.occupato is None, "carica finita")
    carica = [r for r in g.fatti if r.startswith("G1 E")]
    verifica(carica == ["G1 E350.0 F600", "G1 E50.0 F150"], "Carica: 350 mm a 10 mm/s + 50 mm piano (%s)" % carica)
    verifica("M109 S250" in g.fatti and "RESTORE_GCODE_STATE NAME=schermo_filo" in g.fatti, "Carica: M109 e restore")
    verifica(not any("E-300" in r for r in g.fatti), "Scarica durante Carica ignorata")
    schermo.tocca(250, 340)                   # passo 50
    schermo.tocca(240, 390)                   # Avanza subito dopo: vince l'ultimo tocco (codice più alto = massimo)
    giro()
    verifica("G1 E10.0 F300" in g.fatti and m.passo == 1, "due tocchi tra due letture: conta l'ultimo")
    schermo.tocca(250, 340)                   # passo 50
    giro()
    schermo.perdi = 1                         # l'azzeramento di VP_TASTO si perde (i doppioni del 2026-10-18)
    schermo.tocca(240, 390)                   # Avanza
    giro(0.3)
    verifica(schermo.perdi == 0 and schermo.mem[genera.VP["TASTO"]] == 0, "azzeramento perso: riletto e rifatto")
    verifica(abs(prossimo[0] - 0.1) < 1e-9, "dopo un tocco i pulsanti si leggono ogni 0,1 s (%s)" % prossimo[0])
    giro(0.3)
    giro()
    verifica([r for r in g.fatti if r == "G1 E50.0 F300"] == ["G1 E50.0 F300"], "Avanza 50 mm, una volta sola")
    for _ in range(25):
        giro(0.1)
    verifica(abs(prossimo[0] - 0.3) < 1e-9, "dopo 2 s di nuovo ogni 0,3 s (%s)" % prossimo[0])
    schermo.tocca(70, 340)                    # passo 1
    giro()
    for _ in range(3):                        # Arretra tre volte di fila
        schermo.tocca(80, 390)
        giro(0.3)
    giro()
    verifica([r for r in g.fatti if r == "G1 E-1.0 F300"] == ["G1 E-1.0 F300"] * 3, "Arretra 1 mm tre volte di fila")
    scatta("Filamento: passo 1 mm")
    schermo.tocca(160, 450)                   # Fatto
    giro()
    verifica(ug.target == 0 and schermo.pagina == P.PRONTA and m.pagina == P.PRONTA, "Fatto: ugello spento, pronta")

    # errore: Avanza a freddo con l'estrusore che si lamenta
    ug.temperature = 30.
    m.errore = None

    # stampa da Mainsail
    ps.state, ps.filename, ps.total_duration, ps.print_duration = "printing", "phone_stand_fast.gcode", 4320, 4000
    ps.filament_used, sd.progress = 4234., 0.47
    ug.temperature, ug.target, pi.temperature, pi.target = 205.2, 205., 59.8, 60.
    giro()
    verifica(schermo.pagina == P.STAMPA, "stampa Mainsail: pagina stampa")
    verifica(schermo.testo(genera.VP["NOME"]) == "phone_stand_fast", "nome del file")
    scatta("Stampa (Mainsail)")
    schermo.pagina = P.AVVIO                  # lo schermo torna da solo all'avvio senza perdere la memoria (cubo)
    for _ in range(6):
        giro()
    verifica(schermo.pagina == P.STAMPA, "schermo tornato all'avvio da solo: la pagina giusta torna entro 6 s")
    schermo.tocca(80, 440)                    # Pausa
    giro()
    verifica("PAUSE" in g.fatti, "Pausa → PAUSE")
    giro()
    verifica(schermo.pagina == P.PAUSA, "pagina pausa")
    scatta("Pausa")
    schermo.tocca(230, 440)                   # Annulla → dialogo
    verifica(schermo.pagina == P.ANNULLA, "dialogo annulla")
    giro()
    giro()
    verifica(schermo.pagina == P.ANNULLA, "il dialogo resta aperto")
    scatta("Annullare?")
    schermo.tocca(160, 440)                   # No, continua → lo schermo va a STAMPA, Klipper corregge in PAUSA
    giro()
    verifica(schermo.pagina == P.PAUSA, "No: torna alla pausa")
    schermo.tocca(80, 440)                    # Riprendi
    giro()
    giro()
    verifica("RESUME" in g.fatti and schermo.pagina == P.STAMPA, "Riprendi → RESUME, pagina stampa")
    schermo.tocca(230, 440)
    giro()
    schermo.tocca(160, 385)                   # Sì, annulla
    giro()
    giro()
    verifica("CANCEL_PRINT" in g.fatti and schermo.pagina == P.PRONTA and m.finita is None,
             "Sì → CANCEL_PRINT, pronta (niente pagina finita)")

    # stampa finita
    ps.state, sd.progress = "printing", 0.98
    giro()
    ps.state, ps.total_duration, ps.filament_used = "complete", 5640, 12734.
    pi.target, pi.temperature = 0., 48.
    giro()
    verifica(schermo.pagina == P.FINITA, "stampa finita: pagina finita")
    scatta("Finita")
    pi.temperature = 31.
    giro()
    verifica(schermo.mem[genera.VP["SOTTO_PIATTO"]] == 1, "piatto sotto 32°: puoi staccarlo")
    scatta("Finita, piatto freddo")
    schermo.tocca(160, 440)
    giro()
    verifica(schermo.pagina == P.PRONTA and m.finita is None, "OK: pronta")
    ps.state = "complete"
    giro()
    verifica(schermo.pagina == P.PRONTA, "resta pronta (la stampa finita non ritorna)")

    # lo schermo si riaccende
    schermo.riaccendi()
    giro(5.5)
    verifica(schermo.pagina == P.PRONTA and schermo.mem.get(genera.VP["VIVO"]) == 0x5a00,
             "schermo riacceso: riscritto tutto")
    verifica(schermo.mem.get(genera.VP["STATO"]) is not None, "valori riscritti")

    # stampa un file dallo schermo
    V = genera.VP
    ps.state = "standby"
    giro()
    verifica(schermo.pagina == P.PRONTA, "pronta")
    scatta("Pronta")
    schermo.tocca(160, 337)                   # Stampa un file
    verifica(schermo.pagina == P.FILE, "tocco Stampa un file: lo schermo va all'elenco")
    giro()
    verifica(schermo.testo(V["LISTA_NOME"]) == MSG["carico_elenco"], "elenco in arrivo (%r)" % schermo.testo(V["LISTA_NOME"]))
    time.sleep(0.6)
    giro()
    righe = [schermo.testo(V["LISTA_NOME"] + 0x20 * i) for i in range(5)]
    verifica(righe == ["lamp_base_with_cable..v2", "spool_holder_with..g_608", "spool_holder_with..g_688",
                       "cable_clip_8mm_set10", "phone_stand_light"], "Recenti: i più nuovi prima, nomi accorciati (%s)"
             % righe)
    verifica(schermo.testo(V["LISTA_INFO"]) == "13h 17m  238 g  PLA", "riga: durata, grammi, materiale (%r)"
             % schermo.testo(V["LISTA_INFO"]))
    verifica(schermo.testo(V["LISTA_PAGINA"]) == "  1/4" and all(schermo.mem[V["CARTA_RIGA"] + i] == 0 for i in range(5)),
             "pagina 1/4, cinque riquadri (niente .png)")
    scatta("Elenco: Recenti")
    schermo.tocca(264, 443)                   # giù
    giro()
    verifica(schermo.testo(V["LISTA_NOME"] + 0x20) == "headphone_hook_v4" and schermo.testo(V["LISTA_INFO"] + 0x20) == "",
             "file che Moonraker non conosce: solo il nome")
    scatta("Elenco: pagina 2")
    schermo.tocca(264, 443)                   # giù due volte di fila (2026-10-19 contava solo la prima)
    giro(0.3)
    schermo.tocca(264, 443)
    giro(0.3)
    verifica(schermo.testo(V["LISTA_PAGINA"]) == "  4/4" and schermo.mem[V["CARTA_RIGA"] + 4] == 0xff
             and schermo.testo(V["LISTA_NOME"] + 0x80) == "", "giù due volte di fila: pagina 4/4, una riga vuota (%r)"
             % schermo.testo(V["LISTA_PAGINA"]))
    scatta("Elenco: ultima pagina")
    schermo.tocca(160, 380)                   # riga vuota: niente
    giro()
    verifica(schermo.pagina == P.FILE and m.scelto is None, "riga vuota: niente")
    schermo.tocca(264, 443)                   # giù all'ultima: resta
    giro()
    verifica(m.pagina_lista == 3, "giù dall'ultima pagina: resta")
    for _ in range(2):                        # su due volte di fila
        schermo.tocca(56, 443)
        giro(0.3)
    verifica(schermo.testo(V["LISTA_PAGINA"]) == "  2/4", "su due volte di fila: pagina 2/4 (%r)"
             % schermo.testo(V["LISTA_PAGINA"]))
    schermo.tocca(230, 78)                    # A–Z
    giro()
    righe = [schermo.testo(V["LISTA_NOME"] + 0x20 * i) for i in range(5)]
    verifica(schermo.mem[V["GRUPPO"]] == 1 and schermo.testo(V["LISTA_PAGINA"]) == "  1/4"
             and righe[0] == "cable_clip_8mm_set10" and schermo.testo(V["LISTA_INFO"] + 0x20) == "14m  1%s1 m  PETG" % DEC,
             "A–Z: per nome, filo in metri senza il peso (%r)" % schermo.testo(V["LISTA_INFO"] + 0x20))
    verifica(len(set(righe)) == 5, "nomi simili: si distinguono (%s)" % righe)
    scatta("Elenco: A–Z")
    schermo.tocca(160, 197)                   # seconda riga
    giro()
    verifica(schermo.pagina == P.AVVIA and m.scelto["percorso"] == "calibration/flow_test_petg_250_fine_layers.gcode",
             "riga: pagina di conferma del file giusto")
    verifica(schermo.testo(V["AVVIA_NOME"]) == "flow_test_petg_250_fine_" and schermo.testo(V["AVVIA_NOME2"]) == "layers"
             and schermo.testo(V["AVVIA_TEMP"]) == "250 / 80" and schermo.testo(V["AVVIA_DURATA"]) == "14m"
             and schermo.testo(V["AVVIA_FILO"]) == "1%s1 m" % DEC, "conferma: nome su due righe, temperature, durata, filo")
    scatta("Conferma (PETG)")
    schermo.tocca(160, 443)                   # Annulla
    verifica(schermo.pagina == P.FILE, "Annulla: lo schermo torna all'elenco")
    giro()
    verifica(m.pagina == P.FILE and schermo.pagina == P.FILE, "e Klipper lo sa")
    schermo.tocca(90, 78)                     # Recenti
    giro()
    schermo.tocca(160, 136)                   # prima riga
    giro()
    verifica(m.scelto["nome"] == "lamp_base_with_cable_channel_v2.gcode", "scelto il più recente")
    # Klipper rifiuta
    s.fallisci = "Unable to open file"
    schermo.tocca(160, 385)                   # Sì, stampa
    giro(callback=False)
    verifica((schermo.testo(V["AVVIA_MSG"]) + " " + schermo.testo(V["AVVIA_MSG2"])).strip() == MSG["avvio"],
             "Sì: «%s»" % MSG["avvio"])
    scatta("Avvio la stampa")
    schermo.tocca(160, 443)                   # Annulla mentre parte: resta
    giro(callback=False)
    verifica(m.avvio and m.avvio["stato"] == "controllo" and m.pagina == P.AVVIA and schermo.pagina == P.AVVIA,
             "Annulla mentre parte: resta (%s)" % (m.avvio,))
    giro()                                    # qui parte SDCARD_PRINT_FILE (callback del reactor) …
    giro()                                    # … e qui lo schermo mostra com'e' andata
    verifica('SDCARD_PRINT_FILE FILENAME="lamp_base_with_cable_channel_v2.gcode"' in g.fatti, "SDCARD_PRINT_FILE")
    verifica(schermo.testo(V["AVVIA_ERR"]) == "Unable to open file" and schermo.testo(V["AVVIA_MSG"]) == ""
             and schermo.pagina == P.AVVIA, "rifiuto di Klipper: messaggio in rosso (%r)" % schermo.testo(V["AVVIA_ERR"]))
    scatta("Rifiutata da Klipper")
    s.fallisci = None
    schermo.tocca(160, 385)                   # Sì, stampa di nuovo
    giro()
    giro()
    verifica(schermo.pagina == P.STAMPA and m.ultima and m.ultima["nome"] == "lamp_base_with_cable_channel_v2.gcode",
             "avviata: pagina stampa (%s)" % (m.ultima,))
    ug.target, pi.target, ug.temperature, pi.temperature = 220., 60., 150., 55.
    giro()
    verifica(schermo.mem[V["FASE"]] == 1, "prima del primo filo, sotto temperatura: «Scalda»")
    scatta("Avviata: scalda")
    ug.temperature, pi.temperature, ps.print_duration, ps.total_duration = 220., 60., 600., 900.
    ps.filament_used, sd.progress = 1500., 0.05
    giro()
    verifica(schermo.mem[V["FASE"]] == 0, "in temperatura: «In stampa»")

    # regola l'altezza dell'ugello
    schermo.tocca(160, 443)                   # Regola
    verifica(schermo.pagina == P.REGOLA, "Regola: lo schermo va alla pagina")
    giro()
    verifica(m.pagina == P.REGOLA, "e ci resta")
    schermo.tocca(86, 250)                    # Abbassa
    giro()
    giro()
    verifica("SET_GCODE_OFFSET Z_ADJUST=-0.020 MOVE=1" in g.fatti and abs(s.oggetti["gcode_move"].z + 0.02) < 1e-9,
             "Abbassa: -0,02 mm")
    scatta("Regola: -0,02")
    schermo.tocca(253, 346)                   # passo 0,05
    giro()
    for _ in range(6):                        # Abbassa sei volte di fila, una ogni 0,3 s
        schermo.tocca(86, 250)
        giro(0.3)
    giro()
    verifica(abs(s.oggetti["gcode_move"].z + 0.27) < 1e-9 and schermo.mem[V["SOTTO_Z"]] == 1,
             "al massimo 0,30: -0,27 e «Al limite» (%.3f)" % s.oggetti["gcode_move"].z)
    scatta("Regola: al limite")
    schermo.tocca(234, 250)                   # Alza
    giro()
    verifica(abs(s.oggetti["gcode_move"].z + 0.22) < 1e-9, "Alza: +0,05")
    schermo.tocca(160, 443)                   # Torna alla stampa
    giro()
    verifica(schermo.pagina == P.STAMPA and m.pagina == P.STAMPA, "Torna alla stampa")
    schermo.tocca(160, 443)
    giro()
    for _ in range(60):
        giro()
    verifica(schermo.pagina == P.STAMPA, "Regola si chiude da sola dopo un minuto")

    # fine stampa: la correzione di Regola si toglie
    ps.state, ps.total_duration = "complete", 2000.
    giro()
    verifica(schermo.pagina == P.FINITA and "SET_GCODE_OFFSET Z_ADJUST=0.220" in g.fatti
             and abs(s.oggetti["gcode_move"].z) < 1e-9, "fine stampa: Regola tolta (%.3f)" % s.oggetti["gcode_move"].z)
    scatta("Finita: Regola tolta")

    ok = sum(c for c, _ in controlli)
    print(f"controlli: {ok}/{len(controlli)} giusti, scritture sullo schermo: {schermo.scritture}")
    for i, (titolo, im) in enumerate(foto):
        im.save(os.path.join(USCITA, f"{i:02d}.png"))
    col = 5
    righe = (len(foto) + col - 1) // col
    tela = Image.new("RGB", (col * (genera.L + 16) + 16, righe * (genera.A + 44) + 16), (60, 60, 60))
    d = ImageDraw.Draw(tela)
    f = ImageFont.truetype(os.path.join(QUI, "caratteri", "Inter-SemiBold.ttf"), 18)
    for i, (titolo, im) in enumerate(foto):
        x, y = 16 + (i % col) * (genera.L + 16), 16 + (i // col) * (genera.A + 44)
        d.text((x, y), titolo, font=f, fill=(230, 230, 230))
        tela.paste(im, (x, y + 28))
    tela.save(os.path.join(USCITA, "tutte.png"))
    print(os.path.join(USCITA, "tutte.png"))
    server.shutdown()
    return ok == len(controlli)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
