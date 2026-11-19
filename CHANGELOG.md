# Registro modifiche

Ogni modifica automatica o manuale va annotata qui: data, area (tecnica / laboratorio / strategia),
cosa è cambiato, perché (con i numeri) e come annullarla. Regole: vedi `regole/governance` nel Laboratorio.

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
