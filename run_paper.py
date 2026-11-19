"""Paper trading autonomo. Va lanciato UNA volta al giorno dopo la chiusura USA
(es. alle 23:15 ora italiana) da cron / Utilità di pianificazione di Windows.

Ogni esecuzione:
  1. scarica i dati aggiornati
  2. carica il portafoglio virtuale dal file di stato
  3. processa le nuove barre chiuse (esegue gli ordini, controlla gli stop, genera i nuovi ordini)
  4. salva lo stato e scrive il report del giorno

Uso:
  python run_paper.py              # esecuzione normale
  python run_paper.py --status     # mostra il portafoglio senza fare nulla
  python run_paper.py --reset      # azzera il portafoglio virtuale (chiede conferma)
  python run_paper.py --unhalt     # riattiva dopo il kill switch (solo dopo aver capito cosa è successo)

Conto ombra Alpaca (solo paper, facoltativo): con "alpaca.enabled" in config.json e le variabili
d'ambiente ALPACA_API_KEY / ALPACA_SECRET_KEY, gli ordini in coda vengono replicati su Alpaca
DOPO il salvataggio dello stato (vedi bot/alpaca_bridge.py). Senza chiavi: solo simulazione interna.
"""
import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from bot import data as dmod
from bot.alpaca_bridge import sync_account
from bot.broker import PaperBroker
from bot.config import load_config, resolve, tactical_cfg
from bot.engine import Engine, load_blackout
from run_backtest import make_broker

log = logging.getLogger("paper")


def setup_logging(journal_dir: Path):
    journal_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler(journal_dir / "bot.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])


def last_complete_day(now_rome: datetime, after_hour: int) -> pd.Timestamp:
    """La barra di oggi è considerata chiusa solo dopo l'ora indicata (ora di Roma)."""
    today = pd.Timestamp(now_rome.date())
    return today if now_rome.hour >= after_hour else today - pd.Timedelta(days=1)


def status(b: PaperBroker) -> str:
    eq = b.equity()
    lines = [f"Ultimo giorno processato: {b.last_processed}",
             f"Equity: {eq:,.2f} | Cassa: {b.cash:,.2f} | Picco: {b.peak_equity:,.2f} | "
             f"Drawdown: {100 * (1 - eq / b.peak_equity) if b.peak_equity else 0:.1f}%",
             f"Interessi maturati sulla liquidità: {b.interest_earned:,.2f}",
             f"Ultimo ribilanciamento: {b.last_rebalance or '-'}",
             f"Bloccato (kill switch): {'SI' if b.halted else 'no'}", "Posizioni:"]
    for t, p in b.positions.items():
        px = b.last_price.get(t, p.entry_price)
        stop = f"stop {p.stop:>9.2f} " if p.stop > 0 else ""
        lines.append(f"  {t:6} qty {p.qty:>8} carico {p.entry_price:>9.2f} ultimo {px:>9.2f} "
                     f"{stop}peso {100 * p.qty * px / eq:4.1f}% P&L {100 * (px / p.entry_price - 1):+.1f}%")
    if not b.positions:
        lines.append("  (nessuna)")
    lines.append("Ordini per la prossima apertura:")
    for o in b.pending:
        lines.append(f"  {o.side} {o.ticker} x{o.qty} ({o.reason})")
    if not b.pending:
        lines.append("  (nessuno)")
    return "\n".join(lines)


def alpaca_sync(cfg, broker, account) -> str:
    """Replica gli ordini in coda sul conto paper di Alpaca. Mai bloccante: lo stato è già salvato."""
    events = sync_account(cfg, broker, account, resolve)
    for ev in events:
        (log.error if "ERRORE" in ev else log.warning if "ATTENZIONE" in ev else log.info)(ev)
    return ("\n" + "\n".join(events)) if events else ""


def append_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--unhalt", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.config)
    state_path = resolve(cfg, cfg["state_file"])
    journal = resolve(cfg, cfg["journal_dir"])
    setup_logging(journal)

    if a.reset:
        if input("Azzero il portafoglio virtuale? Scrivi SI: ").strip() == "SI":
            state_path.unlink(missing_ok=True)
            print("Portafoglio azzerato.")
        return

    broker = PaperBroker.load(state_path) if state_path.exists() else None
    if a.status:
        print(status(broker) if broker else "Nessun portafoglio ancora: lancia senza opzioni.")
        return
    if a.unhalt:
        if broker:
            broker.halted = False
            broker.peak_equity = broker.equity()  # il drawdown riparte da qui
            broker.save(state_path)
            log.warning("Kill switch disattivato manualmente. Picco reimpostato a %.2f", broker.peak_equity)
        return

    now = datetime.now(ZoneInfo("Europe/Rome"))
    cutoff = last_complete_day(now, cfg["process_today_after_hour_rome"])

    # scarica abbastanza storia per gli indicatori (circa 2 anni bastano per SMA 200)
    start = (cutoff - pd.Timedelta(days=800)).strftime("%Y-%m-%d")
    data = dmod.load(cfg, start=start, end=(cutoff + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    data = {t: df[df.index <= cutoff] for t, df in data.items()}

    stale = dmod.check_staleness(data, cutoff, cfg["data"]["max_staleness_days"])
    if stale:
        log.warning("Dati vecchi per %s: esclusi da questa esecuzione", stale)
        data = {t: df for t, df in data.items() if t not in stale}
    missing = set(cfg["universe"]) - set(data)
    if broker:
        held_missing = set(broker.positions) & missing
        if held_missing:
            # mai decidere alla cieca su una posizione aperta
            log.error("Mancano i dati di posizioni aperte %s: esecuzione interrotta, nessuna modifica.", held_missing)
            sys.exit(2)
    if not data:
        log.error("Nessun dato utilizzabile. Nessuna modifica.")
        sys.exit(2)

    first_run = broker is None
    if first_run:
        broker = make_broker(cfg)
        log.info("Nuovo portafoglio virtuale: %.2f %s", broker.cash, cfg["portfolio"]["currency"])

    cash_rate = dmod.load_cash_rate(cfg, start=start)
    eng = Engine(cfg, data, broker, load_blackout(resolve(cfg, cfg["blackout_file"])), cash_rate)
    cal = eng.calendar()
    if first_run:
        todo = cal[-1:]  # si parte da oggi: niente replay del passato
    else:
        todo = cal[cal > pd.Timestamp(broker.last_processed)]
    if len(todo) == 0:
        log.info("Nessuna nuova barra chiusa da processare (ultima: %s).", broker.last_processed)
        alpaca_sync(cfg, broker, "main")  # ritenta gli invii falliti; i doppioni sono bloccati
        print(status(broker))
        return

    n_fills, n_trades = len(broker.fills), len(broker.trades)
    for d in todo:
        for ev in eng.step(d):
            log.info(ev)

    broker.save(state_path)
    append_csv(journal / "fills.csv", broker.fills[n_fills:])
    append_csv(journal / "trades_closed.csv", broker.trades[n_trades:])
    append_csv(journal / "equity.csv", [{"date": d, "equity": e} for d, e in broker.equity_curve[-len(todo):]])
    report = status(broker) + alpaca_sync(cfg, broker, "main")

    # conto tattico (operazioni brevi), separato dal portafoglio principale
    tcfg = tactical_cfg(cfg)
    if tcfg:
        report += "\n\n=== CONTO TATTICO ===\n" + run_account(tcfg, data, cash_rate, journal, "tattica_")
    (journal / "ultimo_report.txt").write_text(report, encoding="utf-8")
    print(report)


def run_account(cfg, data, cash_rate, journal, prefix) -> str:
    """Esegue un conto aggiuntivo con il suo file di stato. Ritorna il suo stato in testo."""
    sp = resolve(cfg, cfg["state_file"])
    b = PaperBroker.load(sp) if sp.exists() else None
    first = b is None
    if first:
        b = make_broker(cfg)
        log.info("Nuovo conto tattico virtuale: %.2f", b.cash)
    sub = {t: data[t] for t in cfg["universe"] if t in data}
    if set(b.positions) - set(sub):
        log.error("Conto tattico: mancano dati di posizioni aperte, nessuna modifica.")
        return status(b)
    eng = Engine(cfg, sub, b, load_blackout(resolve(cfg, cfg["blackout_file"])), cash_rate)
    cal = eng.calendar()
    todo = cal[-1:] if first else cal[cal > pd.Timestamp(b.last_processed)]
    nf, nt = len(b.fills), len(b.trades)
    for d in todo:
        for ev in eng.step(d):
            log.info("tattica: %s", ev)
    if len(todo):
        b.save(sp)
        append_csv(journal / f"{prefix}fills.csv", b.fills[nf:])
        append_csv(journal / f"{prefix}trades_closed.csv", b.trades[nt:])
    return status(b) + alpaca_sync(cfg, b, "tattica")


if __name__ == "__main__":
    main()
