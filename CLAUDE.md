# Istruzioni per Claude: installa la sonda su QUESTA macchina

Sei un agente (Claude Code) avviato dentro la cartella `sonda-definitiva` su una VPS Linux, da root.
Il tuo compito: **installare e accendere la sonda di MiniBench Net**. Non serve configurare niente: il config è già
compilato (`config.env.esempio`, macchina netcup). Non modificarlo e non modificare nessun file di questa cartella.

## Regole
- Lavora a blocchi e scrivi all'umano dopo ogni blocco: cosa hai fatto, esito, cosa fai dopo.
- La **chiave** è un segreto: la sonda la chiede e la ritira da sola (passo 3) e la scrive nel file. Tu non la chiedi a
  nessuno, non apri il file, non la stampi. Controlli solo che il file esista con permessi 600.
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

## 3. Iscrizione automatica (v2): niente da approvare
Nessuno ti dà la chiave e tu non la vedi mai. Lancia:
```bash
python3 -I /opt/minibench-net/minibench-net.py --iscrivi
```
Deve dire «ISCRITTA (v2), chiave n. N». La sonda si mette d'accordo col server (chiavi X25519), ricava la sua chiave
d'accesso e la salva in `/etc/minibench-net/chiave` (600): nessuno deve approvare. Se dice «iscrizione NON riuscita»,
fermati e riporta la riga esatta (per esempio un limite di macchine per indirizzo). Non aprire i file della chiave.
La macchina parte in quarantena e entra in classifica da sola dopo qualche giorno di dati coerenti.

## 4. Verifica completa e accendi
```bash
python3 -I /opt/minibench-net/minibench-net.py --verifica      # deve finire con PRONTO (chiave accettata)
systemctl enable --now minibench-net.service minibench-net-vivo.timer
sleep 90; systemctl is-active minibench-net.service
journalctl -u minibench-net.service --since "-3 min" --no-pager | tail -20
```
Se la verifica dice «chiave RIFIUTATA», fermati: non riprovare (5 errori in un giorno = indirizzo bannato).

Nel registro devono comparire `check: 201` e poi `test: 201` (il primo test CPU dura qualche minuto e carica la macchina: è normale).
Da qui la sonda gira da sola: test CPU il primo giorno ogni 9 minuti circa, poi ogni ora circa (lo decide il server), check ogni 3, ping ogni 27, traceroute ogni 12 ore.

## 5. Riporta all'umano
`hostname`, sistema (`. /etc/os-release; echo $PRETTY_NAME`), esito di `systemctl is-active minibench-net.service`
e le ultime 20 righe del registro. **Mai** la chiave né il contenuto di `/var/lib/minibench-net/sale` e `id_locale`.
