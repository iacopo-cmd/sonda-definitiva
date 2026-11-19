# Sonda definitiva — MiniBench Net

Sonda di **MiniBench Net** («Valutazione indipendente di VPS e connettività»), versione 0.15, con il benchmark
[minibench.sh](https://github.com/iacopo-cmd/minibench.sh) già dentro. Gira su una VPS Linux come demone di systemd:
test della CPU e dello steal il primo giorno ogni 9 minuti circa e poi ogni ora circa, controllo ogni 3, ping ogni 27, traceroute ogni 12 ore (gli intervalli li dà il server).

- **Iscrizione automatica (v2)**: la sonda si mette d'accordo col server (chiavi X25519) e ricava da sola la sua chiave: niente da approvare.
- **Legge soltanto**: non cambia la configurazione, non apre porte, non installa pacchetti.
- **Privacy**: per riconoscere la macchina manda solo impronte HMAC-SHA256 firmate con un segreto che non lascia la
  macchina (niente MAC, hostname né chiavi SSH); per la rete solo sì/no (IPv6, PTR, NAT, DHCP).

## Installazione: la fa Claude

Niente da configurare (il config è già compilato). Da root:

```bash
cd /root && git clone https://github.com/iacopo-cmd/sonda-definitiva.git && cd sonda-definitiva
claude "installa la sonda seguendo CLAUDE.md"
```

Claude legge [CLAUDE.md](CLAUDE.md), verifica la macchina, installa e accende. La chiave non la dà nessuno: la sonda si iscrive
da sola (`--iscrivi`) e ricava la sua chiave d'accesso, salvata in `/etc/minibench-net/chiave` (600). Ogni invio è poi una busta
cifrata e firmata con il segreto della macchina, sopra l'HTTPS.

Requisiti: Linux con systemd, python3 ≥ 3.9, openssl, ping, findmnt, ip; traceroute consigliato. Licenza: MIT.
