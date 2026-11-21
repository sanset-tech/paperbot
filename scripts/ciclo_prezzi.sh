#!/usr/bin/env bash
# Prezzi in diretta: ripete live_quotes.py + lab_data.py ogni INTERVALLO minuti fino a FINE (HH:MM UTC)
# e pubblica a ogni giro (pull --rebase, 3 tentativi). Serve perché GitHub crea solo ~1 esecuzione
# programmata l'ora (diagnosi 21/11: 10 su 38): un job lungo con sleep non dipende dal cron.
#
#   scripts/ciclo_prezzi.sh 17:50        ciclo fino alle 17:50 UTC
#   scripts/ciclo_prezzi.sh singolo      un giro solo (cron di riserva): salta se i prezzi hanno < SALTA_SE_MIN minuti
#
# Variabili per i test: PREZZI_CMD (comando di un giro), INTERVALLO_MIN (default 15) o INTERVALLO_S, SALTA_SE_MIN (default 10).
set -u
FINE="${1:-singolo}"
INTERVALLO_MIN="${INTERVALLO_MIN:-15}"
INTERVALLO_S="${INTERVALLO_S:-$((INTERVALLO_MIN * 60))}"
SALTA_SE_MIN="${SALTA_SE_MIN:-10}"
PREZZI_CMD="${PREZZI_CMD:-}"

git config user.name "paperbot"
git config user.email "paperbot@users.noreply.github.com"

aggiorna_repo() {  # porta il clone all'ultima versione di main (stato scritto dal bot compreso)
  git pull -q --rebase -X theirs origin main || { git rebase --abort 2>/dev/null; echo "::warning::pull non riuscito"; }
}

pubblica() {
  git add docs/live.json
  git add docs/lab_data.json 2>/dev/null || true
  if git diff --cached --quiet; then
    echo "Prezzi invariati"; return 0
  fi
  git commit -q -m "Prezzi e candele $(date -u +'%H:%M') UTC"
  for i in 1 2 3; do
    git pull -q --rebase -X theirs origin main && git push -q origin HEAD:main && return 0
    git rebase --abort 2>/dev/null
    echo "push rifiutato, nuovo tentativo ($i)"; sleep 5
  done
  echo "::warning::salvataggio non riuscito"; return 1
}

giro() {
  echo "== giro $(date -u +'%H:%M:%S') UTC"
  aggiorna_repo
  if [ -n "$PREZZI_CMD" ]; then
    bash -c "$PREZZI_CMD" || echo "::warning::download prezzi non riuscito"
  else
    python live_quotes.py || echo "::warning::download prezzi non riuscito"
    python lab_data.py || echo "candele non aggiornate"
  fi
  pubblica
}

minuti_da_ultimo_aggiornamento() {
  python3 - <<'PY'
import json, sys
from datetime import datetime, timezone
try:
    t = json.load(open("docs/live.json"))["aggiornato_utc"]
    age = (datetime.now(timezone.utc) - datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
    print(int(age.total_seconds() // 60))
except Exception:
    print(99999)
PY
}

if [ "$FINE" = "singolo" ]; then
  aggiorna_repo
  eta=$(minuti_da_ultimo_aggiornamento)
  if [ "$eta" -lt "$SALTA_SE_MIN" ]; then
    echo "Prezzi aggiornati $eta minuti fa (ciclo attivo): salto"; exit 0
  fi
  giro || exit 1
  exit 0
fi

[[ "$FINE" =~ ^[0-2][0-9]:[0-5][0-9](:[0-5][0-9])?$ ]] || { echo "::error::ora di fine non valida: $FINE (serve HH:MM UTC)"; exit 1; }
fine_s=$(date -u -d "$(date -u +%F) $FINE" +%s)
inizio=$(date -u +%s)
n=0
while :; do
  giro || true
  n=$((n + 1))
  prossimo=$((inizio + n * INTERVALLO_S))
  adesso=$(date -u +%s)
  while [ "$prossimo" -le "$adesso" ]; do n=$((n + 1)); prossimo=$((inizio + n * INTERVALLO_S)); done  # giro lento: niente recuperi a raffica
  if [ "$prossimo" -gt "$fine_s" ]; then
    echo "Fine ciclo ($FINE UTC), giri eseguiti fino a $(date -u +'%H:%M')"; break
  fi
  sleep $((prossimo - adesso))
done
exit 0
