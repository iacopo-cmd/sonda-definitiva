# Sonda definitiva — MiniBench Net

Sonda di **MiniBench Net** («Valutazione indipendente di VPS e connettività»), versione 0.12, con il benchmark
[minibench.sh](https://github.com/iacopo-cmd/minibench.sh) già dentro. Gira su una VPS Linux come demone di systemd:
test della CPU e dello steal ogni 9 minuti, controllo ogni 3, ping ogni 27, traceroute ogni 12 ore (gli intervalli li dà il server).

- **Serve una chiave**, una per macchina, data da noi.
- **Legge soltanto**: non cambia la configurazione, non apre porte, non installa pacchetti.
- **Privacy**: per riconoscere la macchina manda solo impronte HMAC-SHA256 firmate con un segreto che non lascia la
  macchina (niente MAC, hostname né chiavi SSH); per la rete solo sì/no (IPv6, PTR, NAT, DHCP).

## Installazione: la fa Claude

Niente da configurare (il config è già compilato). Da root:

```bash
cd /root && git clone https://github.com/iacopo-cmd/sonda-definitiva.git && cd sonda-definitiva
claude "installa la sonda seguendo CLAUDE.md"
```

Claude legge [CLAUDE.md](CLAUDE.md), verifica la macchina, installa e accende. L'unica cosa che fai tu: quando te lo
chiede, scrivi la chiave nel file `/etc/minibench-net/chiave` dal tuo terminale (la chiave non passa mai dalla chat).

Requisiti: Linux con systemd, python3 ≥ 3.9, openssl, ping, findmnt, ip; traceroute consigliato. Licenza: MIT.
