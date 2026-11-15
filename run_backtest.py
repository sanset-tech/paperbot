"""Backtest della strategia con lo stesso motore del paper trading.

Uso:
  python run_backtest.py                       # dati reali da yfinance
  python run_backtest.py --source synthetic    # dati finti, solo per provare che gira
  python run_backtest.py --start 2015-01-01 --end 2024-12-31
"""
import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from bot import data as dmod
from bot.broker import PaperBroker
from bot.config import load_config, resolve
from bot.engine import Engine, load_blackout
from bot.metrics import buy_and_hold, equity_series, summary


def make_broker(cfg) -> PaperBroker:
    c = cfg["costs"]
    cap = cfg["portfolio"]["initial_capital"]
    return PaperBroker(cash=cap, commission_pct=c["commission_pct"], commission_min=c["commission_min"],
                       slippage_pct=c["slippage_pct"], fractional=cfg["portfolio"]["fractional_shares"],
                       peak_equity=cap)


def run(cfg, data, start=None, end=None, cash_rate=None):
    eng = Engine(cfg, data, make_broker(cfg), load_blackout(resolve(cfg, cfg["blackout_file"])), cash_rate)
    cal = eng.calendar()
    first_ok = cal[min(eng.strategy.warmup, len(cal) - 1)]
    cal = cal[cal >= max(first_ok, pd.Timestamp(start) if start else first_ok)]
    if end:
        cal = cal[cal <= pd.Timestamp(end)]
    for d in cal:
        eng.step(d)
    return eng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--source", choices=["yfinance", "csv", "synthetic"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--strategy", choices=["rotation", "breakout"], help="sovrascrive strategy.type")
    ap.add_argument("--out", default="backtest_out")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    cfg = load_config(a.config)
    if a.source:
        cfg["data"]["source"] = a.source
    if a.strategy:
        cfg["strategy"]["type"] = a.strategy
    data = dmod.load(cfg)
    cash_rate = dmod.load_cash_rate(cfg)
    eng = run(cfg, data, a.start, a.end, cash_rate)
    b = eng.broker

    eq = equity_series(b.equity_curve)
    bh = buy_and_hold(data, eq.index[0], eq.index[-1], cfg["portfolio"]["initial_capital"])
    res = {"tipo_strategia": eng.kind, "strategia": summary(eq, b.trades), "benchmark_buy_and_hold": summary(bh)}
    res["strategia"]["interessi_liquidita"] = round(b.interest_earned, 2)
    res["strategia"]["tasso_liquidita"] = "storico ^IRX" if cash_rate is not None else f"fisso {cfg['costs']['cash_rate_fallback']:.1%}"
    yearly = eq.resample("YE").last().pct_change().dropna() * 100
    yearly.index = yearly.index.year
    res["rendimenti_annui_%"] = {int(k): round(v, 1) for k, v in yearly.items()}
    if b.halted:
        res["ATTENZIONE"] = "kill switch scattato durante il backtest"

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    pd.DataFrame(b.trades).to_csv(out / "trades.csv", index=False)
    pd.DataFrame({"strategia": eq, "buy_and_hold": bh}).to_csv(out / "equity.csv")
    (out / "report.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(2, 1, figsize=(10, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
        ax[0].plot(eq, label="Strategia")
        ax[0].plot(bh, label="Buy & hold (pesi uguali)", alpha=0.7)
        ax[0].legend(); ax[0].set_title("Equity"); ax[0].grid(alpha=0.3)
        ax[1].fill_between(eq.index, 100 * (eq / eq.cummax() - 1), 0, color="tab:red", alpha=0.4)
        ax[1].set_title("Drawdown %"); ax[1].grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(out / "equity.png", dpi=110)
    except ImportError:
        pass


if __name__ == "__main__":
    main()
