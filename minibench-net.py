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
Uso:   minibench-net.py --demone        (il servizio: gira per sempre)
       minibench-net.py                 (un giro solo: i lavori scaduti)
       minibench-net.py --solo LAVORO   (quel lavoro adesso)   --prova LAVORO (stampa il JSON, non manda)
       minibench-net.py --verifica [--prima] [--url URL]   bussa al server e controlla che ci sia tutto, PRIMA di
                        installare o di accendere: root, systemd, niente container, programmi, cartelle, server, chiave.
                        Esce con 0 se si puo' partire, 1 se no (e dice perche'). --prima: prima dell'installazione.
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
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

VERSIONE = "0.12"
CONF, CHIAVE, STATO_DIR = "/etc/minibench-net/config.env", "/etc/minibench-net/chiave", "/var/lib/minibench-net"
QUI = os.path.dirname(os.path.realpath(__file__))
# VERSIONE PER CONTAINER / SENZA ROOT (0.12, 17/11/2026): con MINIBENCH_DIR tutto sta in una cartella sola:
#   $MINIBENCH_DIR/config.env   $MINIBENCH_DIR/chiave   $MINIBENCH_DIR/stato/ (id_locale, sale, stato, storico)
# Niente root e niente systemd: il demone si lancia in primo piano (--demone) da chi gestisce il container.
CARTELLA = os.environ.get("MINIBENCH_DIR", "").strip()
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


def stato(nuovo=None):
    p = f"{STATO_DIR}/stato.json"
    if nuovo is None:
        try:
            return json.load(open(p))
        except (OSError, ValueError):
            return {}
    tmp = p + ".tmp"
    json.dump(nuovo, open(tmp, "w"), indent=1)
    os.replace(tmp, p)


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


def scheda_principale():
    out = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
    m = re.search(r"\bdev (\S+)", out)
    return m.group(1) if m else None


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
    r = subprocess.run(["ip", "-6", "route", "get", "2001:4860:4860::8888"], capture_output=True, text=True)
    if r.returncode != 0 or "unreachable" in (r.stdout + r.stderr).lower():
        return False
    return subprocess.run(["ping", "-6", "-n", "-c", "1", "-W", "2", "2001:4860:4860::8888"], capture_output=True).returncode == 0


def _ptr(ip):
    """Il nome inverso (PTR) di un indirizzo, o None. Con un tempo massimo: un DNS lento non ferma il giro."""
    try:
        out = subprocess.run(["getent", "hosts", ip], capture_output=True, text=True, timeout=6).stdout.split()
        return out[1].lower().rstrip(".") if len(out) > 1 and out[1] != ip else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def qualita(tg):
    """Lista di controllo della rete: IPv6 (ok / indirizzo senza rotta / assente) e nomi inversi. Manda solo si/no."""
    out = subprocess.run(["ip", "-6", "route", "get", "2001:4860:4860::8888"], capture_output=True, text=True).stdout
    m = re.search(r"\bsrc (\S+)", out)
    ha_indirizzo = bool(subprocess.run(["ip", "-6", "addr", "show", "scope", "global"], capture_output=True, text=True).stdout.strip())
    v6 = "ok" if ha_ipv6() else ("senza_rotta" if ha_indirizzo else "assente")
    import ipaddress
    mio4 = tg.get("tuo_ip") or ""
    try:
        pubblico = ipaddress.ip_address(mio4).version == 4 and ipaddress.ip_address(mio4).is_global
    except ValueError:
        pubblico = False
    if not pubblico:                 # il server ci vede da un indirizzo interno (es. stessa macchina): uso quello con cui esco
        o4 = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
        m4 = re.search(r"\bsrc (\S+)", o4)
        mio4 = m4.group(1) if m4 else ""
        try:
            pubblico = ipaddress.ip_address(mio4).is_global
        except ValueError:
            pubblico = False
    p4 = _ptr(mio4) if pubblico else None
    p6 = _ptr(m.group(1)) if v6 == "ok" and m else None
    q = {"ipv6": v6, "ptr4": (p4 is not None) if pubblico else None}
    # indirizzo IPv4 della scheda con la rotta: privato = la macchina sta dietro NAT (l'IP che vede il server e' di un router,
    # e puo' cambiare senza che la macchina se ne accorga); «dynamic» = assegnato con DHCP. Si manda solo si/no.
    o4 = subprocess.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
    s4, d4 = re.search(r"\bsrc (\S+)", o4), re.search(r"\bdev (\S+)", o4)
    if s4:
        try:
            q["ip_nat"] = not ipaddress.ip_address(s4.group(1)).is_global
        except ValueError:
            pass
    if s4 and d4:
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


def prendi_target(url, chiave, st, forza=False):
    """Target e intervalli dal server: si ricaricano ogni 10 minuti (se il server non risponde, tengo quelli vecchi)."""
    if forza or not st.get("target") or time.time() - st.get("target_preso", 0) > 600:
        cod, tg = chiama(f"{url}/v1/target", chiave)
        if cod == 200:
            st["target"], st["target_preso"] = tg, time.time()
            stato(st)
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
    p = f"{STATO_DIR}/battito"
    open(p + ".tmp", "w").write(f"{time.time():.0f} {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
    os.replace(p + ".tmp", p)


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
    mancano = [c for c in ("openssl", "ping", "findmnt", "ip") if not shutil.which(c)]
    segna(not mancano, "programmi necessari presenti" if not mancano else "mancano: " + ", ".join(mancano))
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


def main():
    args = sys.argv[1:]
    if "--verifica" in args:
        raise SystemExit(verifica(args))
    prova = args[args.index("--prova") + 1] if "--prova" in args else None
    solo = args[args.index("--solo") + 1] if "--solo" in args else prova
    demone = "--demone" in args
    if solo and solo not in LAVORI:
        raise SystemExit(f"lavori: {', '.join(LAVORI)}")
    os.makedirs(STATO_DIR, mode=0o700, exist_ok=True)
    blocco = open(f"{STATO_DIR}/.giro.lock", "w")
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
        a = T()
        v6 = ha_ipv6()
        with cf.ThreadPoolExecutor(8) as ex:
            percorsi = list(ex.map(traceroute, bersagli(tg, v6)))
        log(f"traceroute: {len(percorsi)} in {T() - a:.0f} s (IPv6: {'si' if v6 else 'no'})")
        manda("percorsi", url, chiave, st, {"percorsi": percorsi}, prova)
    elif lavoro == "test":
        time.sleep(6)                      # pausa: check e ping dello stesso giro non devono risultare come «macchina occupata»
        occ = occupata(3.0)
        test = minibench(script_minibench(conf), f"{STATO_DIR}/minibench.csv")
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
