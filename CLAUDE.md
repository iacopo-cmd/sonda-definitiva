# Istruzioni per Claude: installa la sonda su QUESTA macchina

Sei un agente (Claude Code) avviato dentro la cartella `sonda-definitiva` su una VPS Linux, da root.
Il tuo compito: **installare e accendere la sonda di MiniBench Net**. Non serve configurare niente: il config è già
compilato (`config.env.esempio`, macchina netcup). Non modificarlo e non modificare nessun file di questa cartella.

## Regole
- Lavora a blocchi e scrivi all'umano dopo ogni blocco: cosa hai fatto, esito, cosa fai dopo.
- La **chiave** è un segreto: **non chiederla in chat, non leggerla, non stamparla**. La scrive l'umano nel file (passo 3);
  tu controlli solo che il file esista con permessi 600.
- Non installare pacchetti, non aprire porte, non toccare niente fuori da `/opt/minibench-net`, `/etc/minibench-net`,
  `/var/lib/minibench-net` e dai tre file di systemd `minibench-net*`.
- Se un comando fallisce o la verifica dice **FERMO**: fermati e riporta le righe esatte. Non improvvisare correzioni.
  Se mancano programmi, di' quali e chiedi all'umano se installarli (su Debian/Ubuntu: `apt install python3 openssl iputils-ping traceroute`).

## 1. Verifica prima di installare
```bash
python3 -I minibench-net.py --verifica --prima
```
L'ultima riga deve dire PRONTO (la chiave a questo punto manca ancora: va bene).

## 2. Installa (non accende niente)
```bash
bash installa.sh minibench.sh
```

## 3. La chiave: la mette l'umano
Scrivi all'umano esattamente questo e aspetta che risponda «fatto»:

> Metti la chiave della macchina, in un tuo terminale (non qui in chat), con:
> `read -rsp 'chiave: ' K && printf '%s\n' "$K" > /etc/minibench-net/chiave && chmod 600 /etc/minibench-net/chiave; unset K`

Poi controlla senza leggerla:
```bash
stat -c '%a %s' /etc/minibench-net/chiave     # deve dire 600 e una lunghezza > 20
```

## 4. Verifica completa e accendi
```bash
python3 -I /opt/minibench-net/minibench-net.py --verifica      # deve finire con PRONTO (chiave accettata)
systemctl enable --now minibench-net.service minibench-net-vivo.timer
sleep 90; systemctl is-active minibench-net.service
journalctl -u minibench-net.service --since "-3 min" --no-pager | tail -20
```
Se la verifica dice «chiave RIFIUTATA», fermati: non riprovare (5 errori in un giorno = indirizzo bannato).

Nel registro devono comparire `check: 201` e poi `test: 201` (il primo test CPU dura qualche minuto e carica la macchina: è normale).
Da qui la sonda gira da sola: test CPU ogni 9 minuti, check ogni 3, ping ogni 27, traceroute ogni 12 ore.

## 5. Riporta all'umano
`hostname`, sistema (`. /etc/os-release; echo $PRETTY_NAME`), esito di `systemctl is-active minibench-net.service`
e le ultime 20 righe del registro. **Mai** la chiave né il contenuto di `/var/lib/minibench-net/sale` e `id_locale`.
