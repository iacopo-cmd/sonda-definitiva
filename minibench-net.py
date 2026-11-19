#!/usr/bin/env python3
"""minibench-net - client di MiniBench Net. Gira da root (per dmi_uuid) come DEMONE (--demone): tiene da solo il suo
ritmo e fa i lavori quando scadono. Ogni lavoro e' separato e ha il suo intervallo:

  check     impronte della macchina cronometrate (autocheck)   -> /v1/check     ogni 3 minuti
  ping      4 ping per target (il 1° a parte)                   -> /v1/ping      ogni 27 minuti
  percorsi  traceroute ICMP verso tutti i target                 -> /v1/percorsi  ogni 12 ore
  test      minibench.sh (CPU) + impronte                        -> /v1/test      ogni 9 minuti

9 e 27 non dividono l'ora: i test scivolano (:00, :09, :18...) e in poche ore toccano tutti i minuti, giorno e notte.
Il demone aggiorna /var/lib/minibench-net/battito a ogni giro: un timer (minibench-net-vivo) lo controlla e lo riavvia.

Gli intervalli li da' il SERVER (/v1/target); il config.env della macchina li puo' cambiare (OGNI_CHECK_MIN,
OGNI_PING_MIN, OGNI_PERCORSI_ORE, OGNI_TEST_MIN). TEST_EVITA_MINUTI=18-23: il test CPU non parte in quei minuti
dell'ora UTC (es. un altro benchmark che gira gia'). Ogni lavoro ha un numero progressivo (aumenta PRIMA di ogni tentativo)
e manda l'ora del client: il server vede le consegne perse e lo scarto dell'orologio.

File:  /etc/minibench-net/config.env  (URL, nome, provider...)   /etc/minibench-net/chiave  (la chiave, 600)
       /var/lib/minibench-net/  (id_locale, sale, stato, storico CSV di minibench)
Impronte (schema 2, 17/11/2026): HMAC-SHA256 con un SALE segreto della macchina (/var/lib/minibench-net/sale, 256 bit, 600)
che NON esce mai dalla macchina: chi riceve l'impronta (anche il server) non puo' risalire al valore ne' confrontarlo con
altre fonti. Si raccolgono solo valori casuali e non pubblici: id_locale, dmi_uuid, machine_id, disco_uuid.
0.15 (19/11/2026, Iacopo: «il minibench deve scalare col tempo»; «meglio non scrivere file»; «viene da noi e noi gli diciamo
ogni quanto ripassare»): l'orario lo da' il SERVER (/v1/target: torna_tra e seq; test CPU fitto il primo giorno, poi ogni
3.547 s). Su disco NON si scrive piu' niente di continuo: stato solo in memoria, battito, storico di minibench.sh e lucchetto
in RAM (/run/minibench-net, o /dev/shm); /var/lib/minibench-net/battito diventa un collegamento al battito in RAM (per il
guardiano vecchio). Su disco restano solo id_locale, sale e chiave, scritti una volta.
0.15.1 (19/11): solo il numero di versione, per provare che il server se ne accorge (dati.cambi_versione, avviso su Telegram).
Uso:   minibench-net.py --demone        (il servizio: gira per sempre)
       minibench-net.py                 (un giro solo: i lavori scaduti)
       minibench-net.py --solo LAVORO   (quel lavoro adesso)   --prova LAVORO (stampa il JSON, non manda)
       minibench-net.py --verifica [--prima] [--url URL]   bussa al server e controlla che ci sia tutto, PRIMA di
                        installare o di accendere: root, systemd, niente container, programmi, cartelle, server, chiave.
                        Esce con 0 se si puo' partire, 1 se no (e dice perche'). --prima: prima dell'installazione.
       minibench-net.py --chiedi-chiave [--aspetta]   (0.13) la macchina CHIEDE la sua chiave (/chiedi, con nick, nome e
                        provider del config), noi approviamo, lei la RITIRA (/ritira) e la scrive in /etc/minibench-net/chiave
                        (600). La chiave non passa da nessuna chat e non si stampa mai. --aspetta: riprova da sola ogni
                        7 minuti per 3 ore finche' non e' approvata.
       minibench-net.py --installa-agente   (0.14) TUTTO IN UN COMANDO, per un agente (OpenClaw, Hermes...) in un container
                        senza root: verifica, chiede la chiave e aspetta l'approvazione, primo test, accende il demone in
                        background (log in dati/demone.log) e stampa un rapporto di 5 righe. Si puo' rilanciare quando si vuole:
                        riprende da dove era (chiave gia' presa, demone gia' acceso).
"""
import concurrent.futures as cf
import csv
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

VERSIONE = "0.15.1"
CONF, CHIAVE, STATO_DIR = "/etc/minibench-net/config.env", "/etc/minibench-net/chiave", "/var/lib/minibench-net"
QUI = os.path.dirname(os.path.realpath(__file__))
# VERSIONE PER CONTAINER / SENZA ROOT (0.12, 17/11/2026): con MINIBENCH_DIR tutto sta in una cartella sola:
#   $MINIBENCH_DIR/config.env   $MINIBENCH_DIR/chiave   $MINIBENCH_DIR/stato/ (id_locale, sale, stato, storico)
# Niente root e niente systemd: il demone si lancia in primo piano (--demone) da chi gestisce il container.
CARTELLA = os.environ.get("MINIBENCH_DIR", "").strip()
if not CARTELLA and "--installa-agente" in sys.argv:
    CARTELLA = os.path.join(QUI, "dati")                 # 0.14: agente (root o no) -> tutto nella cartella «dati» accanto al client
    os.environ["MINIBENCH_DIR"] = CARTELLA
# 0.15: quello che cambia di continuo sta in RAM, non sul disco
if os.geteuid() == 0 and not CARTELLA and os.path.isdir("/run"):
    RAM_DIR = "/run/minibench-net"
elif os.path.isdir("/dev/shm"):
    RAM_DIR = f"/dev/shm/minibench-net-{os.geteuid()}"
else:
    RAM_DIR = None                                     # niente RAM disponibile: si usa STATO_DIR come prima
if CARTELLA:
    CARTELLA = os.path.abspath(os.path.expanduser(CARTELLA))
    CONF, CHIAVE, STATO_DIR = f"{CARTELLA}/config.env", f"{CARTELLA}/chiave", f"{CARTELLA}/stato"


def in_contenitore():
    """Nome del container (docker, podman, lxc...) o None. Le misure da container si segnano a parte."""
    if shutil.which("systemd-detect-virt"):
        c = subprocess.run(["systemd-detect-virt", "--container"], capture_output=True, text=True).stdout.strip()
        if c and c != "none":
            return c
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/run/.containerenv"):
        return "podman"
    try:
        if re.search(r"docker|containerd|kubepods|libpod|lxc", open("/proc/1/cgroup").read()):
            return "container"
    except OSError:
        pass
    return None


def script_minibench(conf):
    """minibench.sh: dal config, oppure accanto al client, nella cartella, o nel clone del repo pubblico accanto."""
    for p in (conf.get("MINIBENCH"), f"{QUI}/minibench.sh", f"{CARTELLA}/minibench.sh" if CARTELLA else None,
              f"{QUI}/../minibench.sh/minibench.sh"):
        if p and os.path.isfile(p):
            return p
    raise SystemExit("minibench.sh non trovato: mettilo accanto al client o indica MINIBENCH= nel config")
T = lambda: time.clock_gettime(time.CLOCK_MONOTONIC)


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%SZ"), *a, flush=True)


def leggi_conf():
    c = {}
    for riga in open(CONF):
        riga = riga.strip()
        if riga and not riga.startswith("#") and "=" in riga:
            k, v = riga.split("=", 1)
            c[k.strip()] = v.strip().strip('"')
    return c


_STATO = {}


def stato(nuovo=None):
    """0.15: lo stato (numeri di consegna, ora dell'ultimo lavoro, target) sta SOLO in memoria. Se la sonda riparte,
    il server le ridice quando tornare e da che numero ripartire (/v1/target: torna_tra, seq)."""
    global _STATO
    if nuovo is None:
        return _STATO
    _STATO = nuovo


def ram(nome):
    """Percorso di un file che cambia di continuo: in RAM se si puo', altrimenti nella cartella di stato."""
    d = RAM_DIR or STATO_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, nome)


# ---------------- 1. minibench ----------------
INTERI = {"vcpu", "core", "thread_per_core", "vcpu_sistema", "core_sistema", "x", "disco_4k_iops", "schema", "conferma"}
TESTI = {"giorno_utc", "ora_utc", "verdetto", "clocksource", "ntp_sync", "cpu_modello", "affinity", "disco_fs",
         "openssl_ver", "python_ver", "container", "x86_livello", "cpu_flag"}
VIA = {"offset_ita", "disco_dir", "disco_4k_GBs"}     # disco_dir e' un percorso locale: non esce dalla macchina


def minibench(script, storico):
    log("minibench.sh: test in corso (~25 s)")
    r = subprocess.run(["bash", script, "-r", "0", storico], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise SystemExit(f"minibench.sh uscito con {r.returncode}: {r.stderr[-300:]}")
    if os.path.getsize(storico) > 900_000:                 # minibench.sh propone l'archivio sopra 1 MB: lo faccio io
        subprocess.run(["bash", script, "-A", storico], capture_output=True, timeout=120)
        log("storico locale archiviato (minibench.sh -A)")
    righe = list(csv.DictReader(open(storico)))          # csv vero: modello e affinity hanno virgole dentro
    t = {}
    for k, v in righe[-1].items():
        if k in VIA or k is None:
            continue
        v = (v or "").strip()
        if v in ("", "n/d", "nessuno"):
            t[k] = {"x": 1, "conferma": 0}.get(k)
        elif k in INTERI:
            t[k] = int(float(v))
        elif k in TESTI:
            t[k] = v
        else:
            try:
                t[k] = float(v)
            except ValueError:
                t[k] = None
    return t


# ---------------- 2. impronte (autocheck cronometrato) ----------------
def id_locale():
    p = f"{STATO_DIR}/id_locale"
    if not os.path.exists(p):
        open(p, "w").write(uuid.uuid4().hex + "\n")
        os.chmod(p, 0o600)
    return open(p).read().strip()


def sale():
    """Segreto della macchina per le impronte: nasce qui, resta qui. Se si perde (reinstallazione), le impronte cambiano tutte."""
    p = f"{STATO_DIR}/sale"
    if not os.path.exists(p):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (os.urandom(32).hex() + "\n").encode())
        os.close(fd)
    return bytes.fromhex(open(p).read().strip())


# ---- rete SENZA i programmi ip e ping (0.14, dalla variante mg2 di Monitor Global, 17/11/2026): nei container minimi non ci sono ----
def mio_indirizzo(v6=False):
    """L'indirizzo con cui la macchina esce verso Internet (UDP connect: non manda niente). None se non c'e' rotta."""
    try:
        with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("2001:4860:4860::8888" if v6 else "1.1.1.1", 53))
            return s.getsockname()[0]
    except OSError:
        return None


def scheda_principale():
    """La scheda con la rotta predefinita IPv4, da /proc/net/route (niente programma ip)."""
    try:
        for riga in open("/proc/net/route").read().splitlines()[1:]:
            c = riga.split()
            if len(c) > 7 and c[1] == "00000000" and c[7] == "00000000":
                return c[0]
    except OSError:
        pass
    return None


def ha_ipv6_globale():
    """Un indirizzo IPv6 globale su una scheda (da /proc/net/if_inet6: ambito 00 = globale)."""
    try:
        return any(r.split()[3] == "00" for r in open("/proc/net/if_inet6") if len(r.split()) > 3 and not r.startswith("00000000000000000000000000000001"))
    except OSError:
        return False


def _somma(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    return ~(s + (s >> 16)) & 0xFFFF


def ping_python(ip, v6=False, n=4, intervallo=0.5, attesa=2.0):
    """Ping con i socket ICMP senza privilegi (net.ipv4.ping_group_range, aperto di norma nei container Docker).
    Ritorna {seq: ms} dei ping arrivati, oppure None se il sistema non li permette."""
    try:
        s = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM,
                          socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP)
    except OSError:
        return None
    t = {}
    with s:
        s.settimeout(attesa)
        for seq in range(1, n + 1):
            corpo = struct.pack("!d", time.time()) + b"minibench-net"
            testa = struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, 0, seq)
            pacco = testa[:2] + struct.pack("!H", 0 if v6 else _somma(testa + corpo)) + testa[4:] + corpo
            a = T()
            try:
                s.sendto(pacco, (ip, 0))
                while True:
                    risposta = s.recv(2048)
                    if len(risposta) >= 8 and risposta[0] == (129 if v6 else 0) and struct.unpack("!H", risposta[6:8])[0] == seq:
                        t[seq] = (T() - a) * 1000
                        break
            except OSError:
                pass
            if seq < n:
                time.sleep(intervallo)
    return t


FONTI = {
    "id_locale": id_locale,
    "dmi_uuid": lambda: open("/sys/class/dmi/id/product_uuid").read().strip().lower(),
    "machine_id": lambda: open("/etc/machine-id").read().strip().lower(),
    "disco_uuid": lambda: subprocess.run(["findmnt", "-no", "UUID", "/"], capture_output=True, text=True).stdout.strip(),
}


def impronte(prima: dict):
    imp, tempi, grezzi = {}, {}, {}
    t0 = T()
    s = sale()
    for tipo, fonte in FONTI.items():
        a = T()
        try:
            v = fonte()
            if not v:
                raise ValueError
        except PermissionError:
            tempi[tipo] = {"lettura_us": int((T() - a) * 1e6), "esito": "non_leggibile"}
            continue
        except (OSError, ValueError, IndexError, TypeError):
            tempi[tipo] = {"lettura_us": int((T() - a) * 1e6), "esito": "assente"}
            continue
        b = T()
        imp[tipo] = hmac.new(s, f"minibench-net:{tipo}:{v}".encode(), hashlib.sha256).hexdigest()[:32]
        c = T()
        tempi[tipo] = {"lettura_us": int((b - a) * 1e6), "hash_us": int((c - b) * 1e6), "esito": "ok"}
        grezzi[tipo] = v.replace("-", "")
    totale = int((T() - t0) * 1e6)
    # su OpenStack machine-id = dmi_uuid: stesso valore, conta una volta sola
    if "machine_id" in imp and "dmi_uuid" in imp and grezzi["machine_id"] == grezzi["dmi_uuid"]:
        imp["machine_id"] = "=dmi_uuid"
    esito = "prima_volta" if not prima else ("uguale" if prima == imp else "cambiata")
    return imp, tempi, totale, esito


# ---------------- 3. ping ----------------
def ha_ipv6():
    """IPv6 che funziona davvero: una rotta verso fuori E una risposta (provato una volta per giro)."""
    if not mio_indirizzo(v6=True):
        return False
    if shutil.which("ping"):
        return subprocess.run(["ping", "-6", "-n", "-c", "1", "-W", "2", "2001:4860:4860::8888"], capture_output=True).returncode == 0
    return bool(ping_python("2001:4860:4860::8888", v6=True, n=1))


def _ptr(ip):
    """Il nome inverso (PTR) di un indirizzo, o None. Con un tempo massimo: un DNS lento non ferma il giro."""
    try:
        out = subprocess.run(["getent", "hosts", ip], capture_output=True, text=True, timeout=6).stdout.split()
        return out[1].lower().rstrip(".") if len(out) > 1 and out[1] != ip else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def qualita(tg):
    """Lista di controllo della rete: IPv6 (ok / indirizzo senza rotta / assente) e nomi inversi. Manda solo si/no."""
    src6 = mio_indirizzo(v6=True)
    ha_indirizzo = ha_ipv6_globale()
    v6 = "ok" if ha_ipv6() else ("senza_rotta" if ha_indirizzo else "assente")
    import ipaddress
    mio4 = tg.get("tuo_ip") or ""
    try:
        pubblico = ipaddress.ip_address(mio4).version == 4 and ipaddress.ip_address(mio4).is_global
    except ValueError:
        pubblico = False
    if not pubblico:                 # il server ci vede da un indirizzo interno (es. stessa macchina): uso quello con cui esco
        mio4 = mio_indirizzo() or ""
        try:
            pubblico = ipaddress.ip_address(mio4).is_global
        except ValueError:
            pubblico = False
    p4 = _ptr(mio4) if pubblico else None
    p6 = _ptr(src6) if v6 == "ok" and src6 else None
    q = {"ipv6": v6, "ptr4": (p4 is not None) if pubblico else None}
    # indirizzo IPv4 della scheda con la rotta: privato = la macchina sta dietro NAT (l'IP che vede il server e' di un router,
    # e puo' cambiare senza che la macchina se ne accorga); «dynamic» = assegnato con DHCP. Si manda solo si/no.
    src4, dev4 = mio_indirizzo(), scheda_principale()
    if src4:
        try:
            q["ip_nat"] = not ipaddress.ip_address(src4).is_global
        except ValueError:
            pass
    s4 = re.match(r"(.*)", src4 or "") if src4 else None
    d4 = re.match(r"(.*)", dev4 or "") if dev4 else None
    if s4 and d4 and shutil.which("ip"):                # DHCP si sa solo col programma ip
        riga = [l for l in subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", d4.group(1)], capture_output=True, text=True).stdout.splitlines()
                if f" {s4.group(1)}/" in l]
        if riga:
            q["dhcp4"] = " dynamic " in riga[0] + " "
    if v6 == "ok":
        q["ptr6"] = p6 is not None
        if p4 and p6:
            q["ptr_uguali"] = p4 == p6
    return q


def sistema():
    """Contatori di rete cumulativi del kernel e carico: li manda col check, il server fa le differenze.
    eth = la scheda con la rotta verso Internet (ci passa tutto: anche Docker e i tunnel, incapsulati);
    vpn = somma dei tunnel; docker = somma dei ponti dei contenitori."""
    def conta(nome):
        try:
            return (int(open(f"/sys/class/net/{nome}/statistics/rx_bytes").read()), int(open(f"/sys/class/net/{nome}/statistics/tx_bytes").read()))
        except (OSError, ValueError):
            return None
    rete = {}
    principale = scheda_principale()
    if principale and conta(principale):
        rete["eth"] = dict(zip(("rx", "tx"), conta(principale)))
    for tipo, prefissi in (("vpn", ("tun", "tap", "wg", "tailscale", "ppp")), ("docker", ("docker", "br-"))):
        somma = [0, 0]
        trovata = False
        for nome in os.listdir("/sys/class/net"):
            if nome.startswith(prefissi) and nome != principale and conta(nome):
                rx, tx = conta(nome)
                somma[0] += rx; somma[1] += tx; trovata = True
        if trovata:
            rete[tipo] = {"rx": somma[0], "tx": somma[1]}
    processi = contenitori = 0
    for p in os.listdir("/proc"):
        if p.isdigit():
            processi += 1
            try:
                if open(f"/proc/{p}/comm").read().strip().startswith("containerd-shim"):
                    contenitori += 1                      # uno per contenitore acceso, anche quelli delle altre utenze
            except OSError:
                pass
    try:
        acceso = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError):
        acceso = None
    return {"rete": rete, "contenitori": contenitori, "processi": processi, "acceso_da_s": acceso}


def occupata(secondi=2.0):
    """Quota di CPU gia' usata da altri programmi della macchina, guardata per 2 secondi PRIMA del test (steal e attese escluse)."""
    def leggi():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:9]]      # user nice system idle iowait irq softirq steal
        return v[0] + v[1] + v[2] + v[5] + v[6], sum(v)
    try:
        a_lavoro, a_tot = leggi()
        time.sleep(secondi)
        b_lavoro, b_tot = leggi()
        return round(100.0 * (b_lavoro - a_lavoro) / max(1, b_tot - a_tot), 1)
    except (OSError, ValueError, IndexError):
        return None


def bersagli(tg, con_v6):
    """I target da misurare: tutti su IPv4 e, se la macchina ha IPv6, quelli che hanno anche l'indirizzo v6."""
    b = [{"id": t["id"], "ip": t["ip"], "v6": False} for t in tg["target"]]
    if con_v6:
        b += [{"id": t["id"], "ip": t["ip6"], "v6": True} for t in tg["target"] if t.get("ip6")]
    return b


def ping(bersaglio):
    if not shutil.which("ping"):                         # container minimo: ping in Python
        t = ping_python(bersaglio["ip"], bersaglio["v6"]) or {}
        return {"target_id": bersaglio["id"], "v6": bersaglio["v6"], "primo_ms": t.get(1), "altri": [t[s] for s in (2, 3, 4) if s in t]}
    out = subprocess.run(["ping", "-6" if bersaglio["v6"] else "-4", "-n", "-c", "4", "-i", "0.5", "-W", "2", bersaglio["ip"]],
                         capture_output=True, text=True, timeout=30).stdout
    t = {int(s): float(v) for s, v in re.findall(r"icmp_seq=(\d+) .*?time=([\d.]+)", out)}
    return {"target_id": bersaglio["id"], "v6": bersaglio["v6"], "primo_ms": t.get(1), "altri": [t[s] for s in (2, 3, 4) if s in t]}


# ---------------- 5. traceroute ----------------
def traceroute(bersaglio):
    if shutil.which("traceroute"):
        icmp = ["-I"] if os.geteuid() == 0 else []        # ICMP come il ping: molte reti scartano l'UDP (da root)
        out = subprocess.run(["traceroute", "-6" if bersaglio["v6"] else "-4", "-n", *icmp, "-q", "1", "-w", "1", "-m", "30", bersaglio["ip"]],
                             capture_output=True, text=True, timeout=120).stdout
        hop = []
        for riga in out.splitlines()[1:]:
            m = re.match(r"\s*\d+\s+(\S+)", riga)
            if m:
                hop.append("*" if m.group(1) == "*" else m.group(1))
    else:                                                  # tracepath: c'e' quasi sempre (iputils), senza root
        out = subprocess.run(["tracepath", "-6" if bersaglio["v6"] else "-4", "-n", "-m", "30", bersaglio["ip"]], capture_output=True, text=True, timeout=180).stdout
        visti = {}
        for n, h in re.findall(r"^\s*(\d+)\??:\s+(\S+)", out, re.M):
            visti.setdefault(int(n), "*" if h == "no" else h)
        hop = [visti[k] for k in sorted(visti)]
    while len(hop) > 1 and hop[-1] == "*":                 # gli asterischi in fondo non dicono niente
        hop.pop()
    hop = [h for h in hop if h == "*" or re.match(r"^[0-9a-fA-F:.]+$", h)] or ["*"]
    return {"target_id": bersaglio["id"], "v6": bersaglio["v6"], "hop": hop[:64]}


# ---------------- invio ----------------
def chiama(url, chiave, metodo="GET", dati=None):
    corpo = json.dumps(dati).encode() if dati is not None else None
    req = urllib.request.Request(url, corpo, method=metodo, headers={
        "Authorization": "Bearer " + chiave, "Content-Type": "application/json", "User-Agent": f"minibench-net/{VERSIONE}"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"esito": "?"}


LAVORI = ("check", "ping", "percorsi", "test")          # in quest'ordine: il test CPU (pesante) per ultimo


def intervalli(conf, tg):
    def v(chiave, server, per):
        return float(conf.get(chiave) or server) * per
    return {"check": v("OGNI_CHECK_MIN", tg["ogni_check_min"], 60), "ping": v("OGNI_PING_MIN", tg["ogni_ping_min"], 60),
            "percorsi": v("OGNI_PERCORSI_ORE", tg["traceroute_ore"], 3600), "test": v("OGNI_TEST_MIN", tg["ogni_test_min"], 60)}


def minuti_da_evitare(conf):
    r = conf.get("TEST_EVITA_MINUTI", "").strip()
    if not r:
        return set()
    a, _, b = r.partition("-")
    return set(range(int(a), int(b or a) + 1))


def ora_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def leggi_conf_sicuro():
    try:
        return leggi_conf()
    except OSError:
        return {}


def prendi_target(url, chiave, st, forza=False):
    """Target e intervalli dal server: si ricaricano ogni 10 minuti (se il server non risponde, tengo quelli vecchi)."""
    if forza or not st.get("target") or time.time() - st.get("target_preso", 0) > 600:
        cod, tg = chiama(f"{url}/v1/target", chiave)
        if cod == 200:
            if not st.get("allineato") and isinstance(tg.get("torna_tra"), dict):   # 0.15: orario e numeri dal server
                adesso, ogni = time.time(), intervalli(leggi_conf_sicuro(), tg)
                for l in LAVORI:
                    st["seq"][l] = max(st["seq"].get(l, 0), int((tg.get("seq") or {}).get(l, 0)))
                    st["fatto"][l] = adesso + float(tg["torna_tra"].get(l, 0)) - ogni[l]
                st["allineato"] = True
            st["target"], st["target_preso"] = tg, time.time()
        elif not st.get("target"):
            raise SystemExit(f"target: {cod} {tg}")
        else:
            log(f"target: {cod}, tengo quelli di prima")
    return st["target"]


def giro(conf, url, chiave, st, solo=None, prova=None):
    """Fa i lavori scaduti (o solo quello chiesto). Ritorna i secondi fino alla prossima scadenza."""
    tg = prendi_target(url, chiave, st, forza=bool(solo))
    ogni = intervalli(conf, tg)
    adesso = time.time()
    da_fare = [solo] if solo else [l for l in LAVORI if adesso - st["fatto"].get(l, 0) >= ogni[l]]
    if "test" in da_fare and not solo and conf.get("TEST_CPU", "si").strip().lower() in ("no", "0", "off"):
        da_fare.remove("test")                               # TEST_CPU=no: niente test CPU
        st["fatto"]["test"] = adesso
    if "test" in da_fare and not solo and datetime.now(timezone.utc).minute in minuti_da_evitare(conf):
        da_fare.remove("test")
        st["fatto"]["test"] = adesso - ogni["test"] + 60       # riprovo fra un minuto
        log("test CPU rimandato: minuto da evitare (TEST_EVITA_MINUTI)")
    for lavoro in da_fare:
        if not prova:
            st["seq"][lavoro] = st["seq"].get(lavoro, 0) + 1      # PRIMA del tentativo: se va perso, il server vede il buco
            st["fatto"][lavoro] = time.time()
            stato(st)
        try:
            fai(lavoro, conf, url, chiave, st, tg, prova)
        except SystemExit as e:
            log(f"{lavoro}: {e}")
        except Exception as e:                                   # un lavoro andato male non ferma gli altri
            log(f"{lavoro}: errore {type(e).__name__}: {e}")
        stato(st)
    adesso = time.time()
    return max(1.0, min(st["fatto"].get(l, 0) + ogni[l] - adesso for l in LAVORI))


def battito():
    """Il battito per il guardiano (vivo.sh): in RAM; /var/lib/minibench-net/battito diventa un collegamento a quel file."""
    p = ram("battito")
    open(p + ".tmp", "w").write(f"{time.time():.0f} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
    os.replace(p + ".tmp", p)
    vecchio = f"{STATO_DIR}/battito"
    if RAM_DIR and not CARTELLA and os.path.realpath(vecchio) != os.path.realpath(p):
        try:
            os.symlink(p, vecchio + ".nuovo"); os.replace(vecchio + ".nuovo", vecchio)   # una volta sola
        except OSError:
            pass


def verifica(args):
    """Controlli prima di partire (17/11/2026, Iacopo: «prima dei test bussa e vede se e' tutto ok, altrimenti che parte a fare»).
    Non scrive niente e non manda misure: chiede solo /v1/salute e, se la chiave c'e', /v1/target."""
    prima = "--prima" in args
    esiti = []                                            # (bloccante, ok, testo)
    def segna(ok, testo, bloccante=True):
        esiti.append((bloccante, ok, testo))
    cont = in_contenitore()
    if CARTELLA:                                          # versione per container / senza root: tutto in MINIBENCH_DIR
        segna(True, f"modalita' cartella: tutto in {CARTELLA} (niente root ne' systemd richiesti)")
        segna(True, f"dentro un container ({cont}): le misure si segnano come «da container»" if cont else "non e' un container", bloccante=False)
    else:
        segna(os.geteuid() == 0, "gira da root" if os.geteuid() == 0 else
              f"NON gira da root (uid {os.geteuid()}): serve root, oppure la versione per container (MINIBENCH_DIR)")
        ha_systemd = bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")
        segna(ha_systemd, "systemd presente" if ha_systemd else "systemd assente: usa la versione per container (MINIBENCH_DIR)")
        segna(not cont, "non e' un container" if not cont else f"e' un CONTAINER ({cont}): usa la versione per container (MINIBENCH_DIR)")
    segna(sys.version_info >= (3, 9), f"python {sys.version.split()[0]}" + ("" if sys.version_info >= (3, 9) else ": serve 3.9 o piu' recente"))
    mancano = [c for c in ("openssl",) if not shutil.which(c)]
    segna(not mancano, "programmi necessari presenti" if not mancano else "mancano: " + ", ".join(mancano))
    # ping e ip non sono piu' obbligatori (0.12-mg2): se mancano, il client li fa in Python
    if not shutil.which("ping"):
        t = ping_python("1.1.1.1", n=1)
        segna(t is not None, "ping assente: si usa il ping in Python" + (" (provato: risponde)" if t else " (permesso, ma 1.1.1.1 non ha risposto)")
              if t is not None else "ping assente e il ping in Python non e' permesso (net.ipv4.ping_group_range): niente misure di rete",
              bloccante=False)
    if not shutil.which("ip"):
        segna(True, "programma ip assente: rotta e indirizzi letti da /proc (DHCP non rilevabile)", bloccante=False)
    if not shutil.which("findmnt"):
        segna(True, "findmnt assente: impronta del disco saltata", bloccante=False)
    tr = shutil.which("traceroute") or shutil.which("tracepath")
    segna(bool(tr), "traceroute presente" if tr else "traceroute assente: i percorsi non si faranno (consigliato)", bloccante=False)
    for d in ((CARTELLA,) if CARTELLA else ("/etc/minibench-net", STATO_DIR, "/opt/minibench-net")):
        su = d if os.path.isdir(d) else os.path.dirname(d)
        ok = os.access(su, os.W_OK)
        segna(ok, f"{d} scrivibile" if ok else f"{d} NON scrivibile")
    # server
    url = args[args.index("--url") + 1] if "--url" in args else None
    if not url:
        for f in (CONF, os.path.join(QUI, "config.env.esempio")):
            try:
                url = dict(l.strip().split("=", 1) for l in open(f) if "=" in l and not l.lstrip().startswith("#")).get("MINIBENCH_NET_URL")
            except OSError:
                continue
            if url and "SERVER" not in url:
                break
    url = (url or "").rstrip("/")
    if not url or "SERVER" in url:
        segna(False, "indirizzo del server non configurato (MINIBENCH_NET_URL)")
    else:
        try:
            r = urllib.request.urlopen(urllib.request.Request(url + "/v1/salute", headers={"User-Agent": f"minibench-net/{VERSIONE}"}), timeout=20)
            segna(r.status == 200, f"server raggiungibile ({url})")
        except Exception as x:
            segna(False, f"server NON raggiungibile ({url}): {type(x).__name__} {x}")
    # chiave (senza mai mostrarla)
    chiave = None
    try:
        chiave = open(CHIAVE).read().strip()
        perm = oct(os.stat(CHIAVE).st_mode & 0o777)
        segna(perm == "0o600", "chiave presente, permessi 600" if perm == "0o600" else f"chiave con permessi {perm[2:]}: devono essere 600")
    except PermissionError:
        segna(False, f"{CHIAVE} non leggibile")
    except OSError:
        segna(prima, "chiave non ancora messa (si mette dopo l'installazione)" if prima else f"manca la chiave in {CHIAVE}", bloccante=not prima)
    if chiave and url and "SERVER" not in url:
        cod, _ = chiama(url + "/v1/target", chiave)
        segna(cod == 200, "chiave ACCETTATA dal server" if cod == 200 else
              f"chiave RIFIUTATA dal server ({cod}): fermati e chiedi una chiave giusta (5 tentativi sbagliati in un giorno = ban)")
    attivo = not CARTELLA and shutil.which("systemctl") and subprocess.run(["systemctl", "is-active", "-q", "minibench-net.service"]).returncode == 0
    if attivo:
        segna(not prima, "il client e' GIA' installato e acceso su questa macchina" + (": non reinstallarlo" if prima else ""), bloccante=prima)
    for bloc, ok, testo in esiti:
        print(("  ok   " if ok else ("  NO   " if bloc else "  nota ")) + testo)
    blocchi = [t for b, ok, t in esiti if b and not ok]
    print("\nPRONTO: si puo' partire." if not blocchi else f"\nFERMO: {len(blocchi)} problemi da risolvere prima di partire.")
    return 0 if not blocchi else 1


def _modulo(url, percorso, campi):
    """POST di un modulo delle pagine (/chiedi, /ritira): torna (codice HTTP, testo della pagina)."""
    import urllib.parse
    req = urllib.request.Request(url + percorso, urllib.parse.urlencode(campi).encode(), method="POST", headers={
        "User-Agent": f"minibench-net/{VERSIONE}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20000).decode("utf-8", "replace")


def chiedi_chiave(args):
    """0.13 (17/11/2026, Iacopo: «la deve generare e noi accettiamo»): giro /chiedi -> approvazione nostra -> /ritira.
    Il codice della richiesta resta in STATO_DIR/codice (600); la chiave va SOLO nel file CHIAVE (600), mai a video."""
    if os.path.exists(CHIAVE) and os.path.getsize(CHIAVE) > 20:
        print(f"la chiave c'e' gia' in {CHIAVE}: niente da chiedere"); return 0
    conf = leggi_conf()
    url = conf.get("MINIBENCH_NET_URL", "").rstrip("/")
    if not url or "SERVER" in url:
        print("MINIBENCH_NET_URL non configurato"); return 1
    os.makedirs(STATO_DIR, mode=0o700, exist_ok=True)
    fc = f"{STATO_DIR}/codice"
    if not os.path.exists(fc):
        import socket
        campi = {"nick": conf.get("NICK") or "anonimo", "macchina": (conf.get("NOME_HOST") or socket.gethostname())[:64],
                 "hosting": conf.get("TIPO_HOSTING") or "provider", "provider": (conf.get("PROVIDER") or "")[:64],
                 "note": f"chiesta dalla sonda {VERSIONE}, hostname {socket.gethostname()}"[:300]}
        cod, pag = _modulo(url, "/chiedi", campi)
        m = re.search(r'class="codice">([A-Z2-9-]{20,29})<', pag)
        n = re.search(r"richiesta n\. (\d+)", pag)
        if cod != 200 or not m:
            print(f"richiesta NON accettata dal server ({cod}): " + re.sub(r"<[^>]+>", " ", pag)[:200].split("Valutazione")[0].strip()); return 1
        vecchio = os.umask(0o077)
        open(fc, "w").write(m.group(1) + "\n"); os.umask(vecchio)
        print(f"richiesta n. {n.group(1) if n else '?'} mandata per «{campi['macchina']}»: ora deve approvarla chi gestisce il server")
    codice = open(fc).read().strip()
    giri = 26 if "--aspetta" in args else 1
    for i in range(giri):
        cod, pag = _modulo(url, "/ritira", {"codice": codice})
        m = re.search(r'class="codice">(mbn_[A-Za-z0-9_-]{20,})<', pag)
        if m:
            vecchio = os.umask(0o077)
            with open(CHIAVE + ".tmp", "w") as f:
                f.write(m.group(1) + "\n")
            os.chmod(CHIAVE + ".tmp", 0o600); os.replace(CHIAVE + ".tmp", CHIAVE); os.umask(vecchio)
            os.replace(fc, fc + ".usato")
            print(f"chiave RITIRATA e salvata in {CHIAVE} (600). Non viene mostrata."); return 0
        if "Ancora in attesa" in pag:
            print(f"{ora_utc()} in attesa di approvazione" + (f", riprovo tra {1 if i == 0 else 7} minuti" if i < giri - 1 else ": rilancia piu' tardi"), flush=True)
            if i < giri - 1:
                time.sleep(60 if i == 0 else 420)              # il primo riprova presto: di solito approviamo subito
            continue
        stato_r = re.search(r"<h1[^>]*>([^<]+)</h1>", pag)
        print(f"ritiro non riuscito ({cod}): {stato_r.group(1) if stato_r else 'risposta inattesa'}"); return 1
    return 2


def demone_acceso():
    """Il demone tiene il lucchetto .giro.lock: se non si riesce a prenderlo, e' acceso."""
    try:
        f = open(ram(".giro.lock"), "w")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        f.close()


def installa_agente(args):
    """0.14 (17/11/2026, Iacopo: «come il Claude di Edo che si e' auto installato, ma per un agente OpenClaw»).
    Un comando solo, rilanciabile: verifica -> chiave (chiesta e ritirata da sola) -> primo test -> demone in background."""
    io = os.path.realpath(__file__)
    passo = lambda n, t: print(f"\n[{n}/5] {t}", flush=True)
    os.makedirs(STATO_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(CONF):
        es = os.path.join(QUI, "config.env.esempio")
        vecchio = os.umask(0o077); shutil.copyfile(es, CONF); os.umask(vecchio)
    passo(1, "verifica")
    if verifica(["--prima"]) != 0:
        print("\nFERMO alla verifica: riporta le righe con NO."); return 1
    passo(2, "chiave (la chiede la sonda; noi approviamo)")
    if chiedi_chiave(["--aspetta"]) != 0:
        print("\nFERMO: chiave non ancora ritirata. Rilancia lo stesso comando piu' tardi."); return 2
    conf = leggi_conf()
    _, tg = chiama(conf.get("MINIBENCH_NET_URL", "").rstrip("/") + "/v1/target", open(CHIAVE).read().strip())
    passo(3, "primo test della CPU (qualche minuto)")
    if int(((tg or {}).get("seq") or {}).get("test", 0)) > 0:
        print("test gia' fatto in passato: salto")
    else:
        time.sleep(30)                                     # il tempo di rendere «nostra» la chiave sul server
        for giro_t in range(30):                           # fino a un'ora: il tempo di rendere «nostra» la chiave
            r = subprocess.run([sys.executable, "-I", io, "--solo", "test"], capture_output=True, text=True,
                               env={**os.environ, "MINIBENCH_DIR": CARTELLA} if CARTELLA else None)
            righe = [l for l in (r.stdout + r.stderr).splitlines() if "test n." in l]
            print(righe[-1] if righe else (r.stdout + r.stderr)[-300:], flush=True)
            if righe and re.search(r"test n\.\d+: 20[01]", righe[-1]):
                break
            if giro_t < 29:
                print("riprovo tra 2 minuti (la chiave deve ancora essere resa «nostra»?)", flush=True); time.sleep(120)
        else:
            print("\nFERMO: il test non e' stato accettato. Riporta le righe qui sopra."); return 3
    passo(4, "demone in background")
    if demone_acceso():
        print("gia' acceso")
    else:
        logf = open(os.path.join(CARTELLA or STATO_DIR, "demone.log"), "a")
        subprocess.Popen([sys.executable, "-I", "-u", io, "--demone"], stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, env={**os.environ, "MINIBENCH_DIR": CARTELLA} if CARTELLA else None)
        time.sleep(5)
        print("acceso" if demone_acceso() else "NON si e' acceso: guarda demone.log")
    passo(5, "rapporto")
    conf = leggi_conf()
    print(f"sonda {VERSIONE} · cartella {CARTELLA or '/etc + /var/lib'} · server {conf.get('MINIBENCH_NET_URL')}")
    print(f"chiave: presente (600) · demone: {'acceso' if demone_acceso() else 'SPENTO'} · test CPU ogni {conf.get('OGNI_TEST_MIN') or 'come dice il server'} min")
    print("FATTO. Se il container riparte, rilancia: python3 -I minibench-net.py --installa-agente")
    return 0


def main():
    args = sys.argv[1:]
    if "--verifica" in args:
        raise SystemExit(verifica(args))
    if "--chiedi-chiave" in args:
        raise SystemExit(chiedi_chiave(args))
    if "--installa-agente" in args:
        raise SystemExit(installa_agente(args))
    prova = args[args.index("--prova") + 1] if "--prova" in args else None
    solo = args[args.index("--solo") + 1] if "--solo" in args else prova
    demone = "--demone" in args
    if solo and solo not in LAVORI:
        raise SystemExit(f"lavori: {', '.join(LAVORI)}")
    os.makedirs(STATO_DIR, mode=0o700, exist_ok=True)
    blocco = open(ram(".giro.lock"), "w")
    try:
        fcntl.flock(blocco, fcntl.LOCK_EX | fcntl.LOCK_NB)        # una sola istanza
    except BlockingIOError:
        print("il demone e' gia' attivo (o un altro giro): per un lavoro subito, ferma il servizio o aspetta")
        return
    st = stato()
    st.setdefault("seq", {}); st.setdefault("fatto", {})
    if "ultimi_percorsi" in st and "percorsi" not in st["fatto"]:  # stato della versione 0.1
        st["fatto"]["percorsi"] = st.pop("ultimi_percorsi")
    if not demone:
        conf = leggi_conf()
        giro(conf, conf.get("MINIBENCH_NET_URL", "").rstrip("/"), open(CHIAVE).read().strip(), st, solo, prova)
        return
    log(f"demone minibench-net {VERSIONE} acceso")
    while True:
        conf = leggi_conf()                                      # config e chiave riletti a ogni giro: niente riavvii
        try:
            attesa = giro(conf, conf.get("MINIBENCH_NET_URL", "").rstrip("/"), open(CHIAVE).read().strip(), st)
        except SystemExit as e:
            log(f"giro: {e}"); attesa = 60
        except Exception as e:
            log(f"giro: errore {type(e).__name__}: {e}"); attesa = 60
        battito()
        time.sleep(min(attesa, 60))                              # al massimo un minuto: il battito resta fresco


def manda(lavoro, url, chiave, st, dati, prova):
    dati = {"versione_client": VERSIONE, "seq": st["seq"].get(lavoro, 0), "orologio": ora_utc(), **dati}
    if prova:
        print(json.dumps(dati, indent=1)[:4000]); print("byte:", len(json.dumps(dati)))
        return 0, {}
    cod, r = chiama(f"{url}/v1/{lavoro}", chiave, "POST", dati)
    log(f"{lavoro} n.{dati['seq']}: {cod} {r}  ({len(json.dumps(dati))} byte)")
    return cod, r


def fai(lavoro, conf, url, chiave, st, tg, prova):
    if lavoro == "check":
        imp, tempi, tot_us, esito = impronte(st.get("impronte"))
        dati = {"schema_impronte": 2, "contenitore": bool(in_contenitore()), "impronte": imp, "tempi_impronte": tempi, "autocheck": esito, "autocheck_us": tot_us, "intervalli": intervalli(conf, tg),
                "sistema": sistema()}
        if time.time() - st.get("qualita_fatta", 0) > 3600:       # la lista di controllo della rete: una volta l'ora
            dati["qualita"] = qualita(tg)
        cod, r = manda("check", url, chiave, st, dati, prova)
        if cod == 201 and "qualita" in dati:
            st["qualita_fatta"] = time.time()
    elif lavoro == "ping":
        a = T()
        v6 = ha_ipv6()
        with cf.ThreadPoolExecutor(16) as ex:
            pings = list(ex.map(ping, bersagli(tg, v6)))
        log(f"ping: {len(pings)} misure in {T() - a:.1f} s (IPv6: {'si' if v6 else 'no'})")
        manda("ping", url, chiave, st, {"ping": pings}, prova)
    elif lavoro == "percorsi":
        if not (shutil.which("traceroute") or shutil.which("tracepath")):
            log("traceroute: ne' traceroute ne' tracepath su questa macchina: percorsi saltati")
            return
        a = T()
        v6 = ha_ipv6()
        with cf.ThreadPoolExecutor(8) as ex:
            percorsi = list(ex.map(traceroute, bersagli(tg, v6)))
        log(f"traceroute: {len(percorsi)} in {T() - a:.0f} s (IPv6: {'si' if v6 else 'no'})")
        manda("percorsi", url, chiave, st, {"percorsi": percorsi}, prova)
    elif lavoro == "test":
        time.sleep(6)                      # pausa: check e ping dello stesso giro non devono risultare come «macchina occupata»
        occ = occupata(3.0)
        test = minibench(script_minibench(conf), ram("minibench.csv"))
        imp, tempi, tot_us, esito = impronte(st.get("impronte"))
        pubblici = [p for p in conf.get("PUBBLICI", "").replace(" ", "").split(",") if p]
        dettagli = {k: v for k, v in {
            "nick": conf.get("NICK") or None, "nick_visibile": conf.get("NICK_VISIBILE", "no") == "si",
            "nome_host": conf.get("NOME_HOST") or None, "provider_dich": conf.get("PROVIDER") or None,
            "tipo_hosting": conf.get("TIPO_HOSTING") or None, "piano": conf.get("PIANO") or None,
            "paese": conf.get("PAESE") or None, "citta": conf.get("CITTA") or None,
            "vcpu_dichiarate": int(conf["VCPU_DICHIARATE"]) if conf.get("VCPU_DICHIARATE") else None,
            "pubblici": pubblici}.items() if v is not None}
        cod, r = manda("test", url, chiave, st, {"occupata_pct": occ, "schema_impronte": 2, "contenitore": bool(in_contenitore()), "impronte": imp, "tempi_impronte": tempi, "autocheck": esito,
                                                 "autocheck_us": tot_us, "test": test, "dettagli": dettagli}, prova)
        if cod == 201:
            st["impronte"] = imp                                 # le impronte buone: base dell'autocheck
            st["ultimo_test"] = f"{test['giorno_utc']} {test['ora_utc']}"


if __name__ == "__main__":
    main()
