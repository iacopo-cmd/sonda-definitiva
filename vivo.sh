#!/bin/bash
# vivo.sh - il guardiano di MiniBench Net: l'UNICA cosa che fa il timer. Se il demone non gira o il suo battito
# (/var/lib/minibench-net/battito, aggiornato a ogni giro, almeno una volta al minuto) ha piu' di 20 minuti: lo riavvia.
B=/var/lib/minibench-net/battito
if ! systemctl is-active -q minibench-net.service; then
  logger -t minibench-net-vivo "demone fermo: lo riaccendo"; systemctl restart minibench-net.service; exit 0
fi
eta=$(( $(date +%s) - $(cut -d' ' -f1 "$B" 2>/dev/null || echo 0) ))
if [ "$eta" -gt 1200 ]; then
  logger -t minibench-net-vivo "battito vecchio di ${eta} s: demone bloccato, lo riavvio"; systemctl restart minibench-net.service
fi
