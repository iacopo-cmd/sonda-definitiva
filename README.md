# Sonda definitiva — MiniBench Net

Sonda di **MiniBench Net** («Valutazione indipendente di VPS e connettività»), versione 0.12, con il benchmark
[minibench.sh](https://github.com/iacopo-cmd/minibench.sh) già dentro. Gira su una VPS Linux come demone di systemd:
test della CPU e dello steal ogni 9 minuti, controllo ogni 3, ping ogni 27, traceroute ogni 12 ore (gli intervalli li dà il server).

- **Serve una chiave**, una per macchina, data da noi.
- **Legge soltanto**: non cambia la configurazione, non apre porte, non installa pacchetti.
- **Privacy**: per riconoscere la macchina manda solo impronte HMAC-SHA256 firmate con un segreto che non lascia la
  macchina (niente MAC, hostname né chiavi SSH); per la rete solo sì/no (IPv6, PTR, NAT, DHCP).

## Installazione (da root)

```bash
cd /root && git clone https://github.com/iacopo-cmd/sonda-definitiva.git
bash sonda-definitiva/installa.sh sonda-definitiva/minibench.sh      # prima verifica, poi installa; non accende niente
umask 077; printf '%s\n' "$CHIAVE" > /etc/minibench-net/chiave; unset CHIAVE   # la chiave che ti diamo noi
python3 -I /opt/minibench-net/minibench-net.py --verifica                 # deve finire con PRONTO
systemctl enable --now minibench-net.service minibench-net-vivo.timer
```

Controllo: `journalctl -u minibench-net.service -f` (righe `check: 201`, `test: 201`).
Istruzioni passo per passo: [ISTRUZIONI-AGENTE.md](ISTRUZIONI-AGENTE.md).
Requisiti: Linux con systemd, python3 ≥ 3.9, openssl, ping, findmnt, ip; traceroute consigliato. Licenza: MIT.
