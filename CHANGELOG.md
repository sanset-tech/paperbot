# Registro modifiche

Ogni modifica automatica o manuale va annotata qui: data, area (tecnica / laboratorio / strategia),
cosa è cambiato, perché (con i numeri) e come annullarla. Regole: vedi `regole/governance` nel Laboratorio.

## 2026-11-21 — tecnica: prezzi in diretta con un ciclo invece che col solo cron
- **Cosa**: `quotes.yml` ora avvia due job lunghi che ripetono `live_quotes.py` + `lab_data.py` ogni 15 minuti
  con sleep e pubblicano a ogni giro (pull --rebase, 3 tentativi): cron `1 12` (fino alle 17:50 UTC) e
  `49 17` (fino alle 21:15 UTC), entrambi sotto il limite di 6 h per job. Logica in `scripts/ciclo_prezzi.sh`.
  I cron di prima (`7,22,37,52 13-21`, `3,33 12`) restano come riserva: un giro singolo, che salta da solo
  se i prezzi hanno meno di 10 minuti. Gruppo di concorrenza proprio (`prezzi-ciclo` per i cicli, unico per
  i giri di riserva) invece di `paperbot`, condiviso col bot: un ciclo di ore avrebbe bloccato il bot delle 22:07.
  Avvio manuale: un giro, oppure un ciclo con l'input `ciclo_fino_a` (HH:MM UTC).
- **Perché**: API Actions, 18-20/11: 10 esecuzioni programmate create al giorno su 38 previste (11 il 18/11
  con un avvio manuale), **tutte riuscite, nessuna cancellata o fallita**: GitHub semplicemente non crea le altre
  (throttling dei cron frequenti, ~1 all'ora con 30-45 min di ritardo). I giri delle 12:03/12:33 non sono mai
  partiti; primo aggiornamento 13:05-13:11 UTC. Il gruppo condiviso non era la causa (zero cancellazioni), ma
  con un job lungo lo sarebbe diventato.
- Provato in locale contro un repository finto con push concorrenti: 5 giri pubblicati su 5, nessun conflitto.
- **Annullare**: `git revert` di questo commit (tornano i soli cron, con il gruppo `paperbot`).

## 2026-11-21 — tecnica: stato degli ordini Alpaca aggiornato
- **Cosa**: `bot/alpaca_bridge.py` a ogni esecuzione rilegge da Alpaca (`get_order_by_client_id`, solo
  lettura, anche a mercato aperto) gli ordini del registro ancora "inviato" e non in uno stato finale, e
  aggiorna `stato_alpaca` (ora scritto come `filled`, `accepted`… invece di `OrderStatus.X`), `qty_eseguita`,
  `prezzo_medio`. Stati finali: `filled` → stato "eseguito"; `canceled`/`expired`/`rejected`/`replaced` →
  "annullato". Gli ordini "eseguito"/"annullato" contano come già gestiti: mai reinviati.
- **Perché**: diagnosi 21/11: i 4 ordini di allineamento del 19/11 (DBC 272, EFA 95, SPY 13, VNQ 103)
  erano ancora "inviato"/`PENDING_NEW` dopo 5 esecuzioni, anche se la riconciliazione dava posizioni
  allineate. Lo stato si scriveva solo all'invio. Si sistemano da soli alla prossima esecuzione del bot.
- 3 test nuovi in `tests/test_alpaca.py` (registro reale del 19/11 col client finto): falliscono senza la
  correzione, 47/47 passano con.
- **Annullare**: `git revert` di questo commit (i campi in più nel registro vengono ignorati).

## 2026-11-19 — tecnica (richiesta dell'utente)
- **Conto ombra Alpaca, solo paper** (`bot/alpaca_bridge.py`, agganciato in `run_paper.py` dopo il salvataggio
  dello stato): gli ordini in coda del simulatore vengono replicati su Alpaca con `paper=True` fisso.
  BUY con stop → OTO con stop lato server (BRACKET solo se `alpaca.take_profit_r` è impostato: Alpaca vuole
  il take profit per il bracket), GTC perché con DAY lo stop scadrebbe a fine seduta. SELL → annulla gli
  stop aperti sul ticker, vende, e se la vendita è parziale rimette lo stop sul resto. Niente doppioni
  (client_order_id deterministico + registro `state/alpaca_orders.json`), errori registrati con al massimo
  3 tentativi, nessun invio a mercato aperto, riconciliazione posizioni simulatore/Alpaca a ogni esecuzione.
- La strategia di rotazione non usa stop: i suoi BUY restano ordini a mercato semplici, come nel backtest.
  `alpaca.stop_emergenza_pct` (spento) aggiungerebbe uno stop solo su Alpaca, fuori dal backtest.
- Senza `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` nulla cambia: avviso nel log e solo simulazione interna.
- 15 test nuovi (`tests/test_alpaca.py`); su GitHub girano con la libreria alpaca-py vera.
- Annullare: `"alpaca": {"enabled": false}` in config.json, oppure `git revert` del commit.
- **Allineamento una tantum** (`alpaca.allineamento_iniziale: true`): compra su Alpaca le 4 posizioni che il
  simulatore aveva prima del collegamento (DBC 272, EFA 95, SPY 13, VNQ 103), solo la differenza rispetto a ciò
  che Alpaca ha già, senza stop (`stop_emergenza_pct` resta spento). Non si ripete: segno "completato" nel
  registro e client_order_id fisso `pb-main-allinea-<TICKER>`. Se un ticker ha un ordine in coda, viene rinviato.

## 2026-11-18 — tecnica
- **Prezzi in diretta**: cron spostati a minuti non tondi (`7,22,37,52 13-21`) e aggiunti due giri di
  pre-apertura (`3,33 12`). Motivo: diagnosi 17/11, solo 8 esecuzioni su 36 e prima esecuzione alle
  14:12 UTC, dopo il briefing delle 13:47 → futures vecchi nel briefing.
- **Paperbot**: cron `7 22`, `41 23`, `7 6` (prima `15 22`, `45 23`, `15 6`). Motivo: ritardo medio 53 min
  agli orari tondi.
- **Test nel repository** (`tests/test_core.py`, 24 test) e workflow `Test` a ogni modifica del codice,
  così ogni correzione automatica viene verificata anche su GitHub.
- **Workflow `Esperimento`**: confronta main con un branch di prova sugli stessi dati (dal 2005, 2015, 2021
  e ultimi 3 anni come verifica fuori campione) e dice se la modifica rispetta la governance. Non tocca lo stato.
- Annullare: `git revert` dei commit del 18/11.
