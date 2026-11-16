"""Prezzi quasi in tempo reale per la dashboard (docs/live.json).

Gira ogni 15 minuti circa durante la seduta USA (GitHub Actions). Non tocca il portafoglio:
legge lo stato e scrive solo i prezzi e l'andamento della giornata.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from bot.config import load_config, resolve, tactical_cfg


def accounts(cfg):
    out = []
    for name, c in (("base", cfg), ("tattica", tactical_cfg(cfg))):
        if c:
            p = resolve(c, c["state_file"])
            if p.exists():
                out.append((name, json.loads(p.read_text(encoding="utf-8"))))
    return out


def main():
    import yfinance as yf
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.json")
    accs = accounts(cfg)
    tickers = {"SPY"}
    for _, s in accs:
        tickers |= set(s["positions"]) | {o["ticker"] for o in s["pending"]}
    quotes, intraday = {}, {}
    for t in sorted(tickers):
        try:
            d = yf.Ticker(t).history(period="5d", interval="1d", auto_adjust=False)
            m = yf.Ticker(t).history(period="1d", interval="5m", auto_adjust=False)
        except Exception as e:  # una quotazione mancante non deve fermare il resto
            print("errore", t, e)
            continue
        if d is None or d.empty:
            continue
        d.index = pd.to_datetime(d.index).tz_convert("America/New_York")
        last_day = d.index[-1].date()
        prev_close = float(d["Close"].iloc[-2]) if len(d) > 1 else float(d["Open"].iloc[-1])
        last = float(d["Close"].iloc[-1])
        when = d.index[-1]
        if m is not None and not m.empty:
            m.index = pd.to_datetime(m.index).tz_convert("America/New_York")
            last = float(m["Close"].iloc[-1])
            when = m.index[-1]
            intraday[t] = [[ts.strftime("%H:%M"), round(float(v), 4)] for ts, v in m["Close"].items()]
        quotes[t] = {"last": round(last, 4), "prev_close": round(prev_close, 4),
                     "open": round(float(d["Open"].iloc[-1]), 4), "day": str(last_day),
                     "time": when.strftime("%Y-%m-%d %H:%M")}

    # andamento intraday del portafoglio base: posizioni attuali valorizzate barra per barra
    curve = []
    base = next((s for n, s in accs if n == "base"), None)
    if base and base["positions"] and all(t in intraday for t in base["positions"]):
        times = sorted({x[0] for t in base["positions"] for x in intraday[t]})
        last_px = {t: quotes[t]["prev_close"] for t in base["positions"]}
        series = {t: dict(intraday[t]) for t in base["positions"]}
        for tm in times:
            for t in base["positions"]:
                if tm in series[t]:
                    last_px[t] = series[t][tm]
            eq = base["cash"] + sum(p["qty"] * last_px[t] for t, p in base["positions"].items())
            curve.append([tm, round(eq, 2)])

    now_ny = datetime.now(timezone.utc).astimezone(pd.Timestamp.now(tz="America/New_York").tz)
    spy_t = quotes.get("SPY", {}).get("time", "")
    fresh = bool(spy_t) and (pd.Timestamp(now_ny).tz_localize(None) - pd.Timestamp(spy_t)).total_seconds() < 1800
    out = {"aggiornato_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "mercato_aperto": fresh and now_ny.weekday() < 5 and (9, 30) <= (now_ny.hour, now_ny.minute) <= (16, 5),
           "quotes": quotes, "equity_intraday": curve}
    docs = resolve(cfg, "docs")
    docs.mkdir(exist_ok=True)
    (docs / "live.json").write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print("prezzi aggiornati:", len(quotes), "mercato aperto:", out["mercato_aperto"])


if __name__ == "__main__":
    main()
