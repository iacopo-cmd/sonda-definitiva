#!/bin/bash
# installa.sh - mette il client di MiniBench Net su questa macchina (da root).  Uso: installa.sh [percorso/minibench.sh]
set -euo pipefail
QUI=$(dirname "$(readlink -f "$0")")
MB=${1:-$QUI/../minibench.sh/minibench.sh}
[ -f "$MB" ] || { echo "manca minibench.sh: scaricalo da https://github.com/iacopo-cmd/minibench.sh e passalo come argomento"; exit 2; }
command -v python3 >/dev/null || { echo "manca python3"; exit 2; }
# prima di copiare qualsiasi cosa: bussa al server e controlla che ci sia tutto (root, systemd, niente container, programmi...)
python3 -I "$QUI/minibench-net.py" --verifica --prima || { echo "installazione NON fatta: risolvi i problemi qui sopra"; exit 2; }
install -d -m 755 /opt/minibench-net; install -d -m 700 /etc/minibench-net /var/lib/minibench-net
install -m 755 "$QUI/minibench-net.py" /opt/minibench-net/minibench-net.py
install -m 755 "$QUI/vivo.sh" /opt/minibench-net/vivo.sh
install -m 755 "$MB" /opt/minibench-net/minibench.sh
[ -e /etc/minibench-net/config.env ] || install -m 600 "$QUI/config.env.esempio" /etc/minibench-net/config.env
[ -e /etc/minibench-net/chiave ] || { echo "metti la chiave in /etc/minibench-net/chiave (chmod 600)"; }
# dalla 0.3 niente timer ogni minuto: il client e' un demone, il timer fa solo da guardiano
if [ -e /etc/systemd/system/minibench-net.timer ]; then systemctl disable --now minibench-net.timer 2>/dev/null; rm -f /etc/systemd/system/minibench-net.timer; fi
install -m 644 "$QUI/minibench-net.service" "$QUI/minibench-net-vivo.service" "$QUI/minibench-net-vivo.timer" /etc/systemd/system/
systemctl daemon-reload
echo "fatto. Poi: modifica /etc/minibench-net/config.env, metti la chiave, prova con"
echo "  python3 -I /opt/minibench-net/minibench-net.py --verifica      (deve dire PRONTO)"
echo "  e poi: systemctl enable --now minibench-net.service minibench-net-vivo.timer"
