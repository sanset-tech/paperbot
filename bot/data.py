"""Dati di mercato: yfinance (reale), CSV (offline) o sintetico (solo test).

Ogni DataFrame ha indice DatetimeIndex (senza fuso) e colonne
Open, High, Low, Close, Volume, già rettificate per split e dividendi.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
COLS = ["Open", "High", "Low", "Close", "Volume"]


def clean(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{ticker}: colonne mancanti {missing}")
    df = df[COLS].astype(float)
    bad = df[["Open", "High", "Low", "Close"]].le(0).any(axis=1) | df[["Open", "High", "Low", "Close"]].isna().any(axis=1)
    if bad.any():
        log.warning("%s: scartate %d barre non valide", ticker, int(bad.sum()))
        df = df[~bad]
    # Coerenza High/Low (dati gratuiti a volte sono sporchi)
    df["High"] = df[["High", "Open", "Close"]].max(axis=1)
    df["Low"] = df[["Low", "Open", "Close"]].min(axis=1)
    df["Volume"] = df["Volume"].fillna(0)
    return df


def load_yfinance(tickers, start, end=None) -> dict[str, pd.DataFrame]:
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("yfinance non installato: pip install yfinance") from e
    out = {}
    for t in tickers:
        raw = yf.Ticker(t).history(start=start, end=end, auto_adjust=True, actions=False)
        if raw is None or raw.empty:
            log.error("%s: nessun dato da yfinance, escluso", t)
            continue
        out[t] = clean(raw, t)
    return out


def load_csv(tickers, csv_dir) -> dict[str, pd.DataFrame]:
    out = {}
    for t in tickers:
        p = Path(csv_dir) / f"{t}.csv"
        if not p.exists():
            log.error("%s: file %s mancante, escluso", t, p)
            continue
        out[t] = clean(pd.read_csv(p, index_col=0, parse_dates=True), t)
    return out


def save_csv(data: dict[str, pd.DataFrame], csv_dir) -> None:
    Path(csv_dir).mkdir(parents=True, exist_ok=True)
    for t, df in data.items():
        df.to_csv(Path(csv_dir) / f"{t}.csv")


def synthetic(tickers, start="2010-01-01", end="2024-12-31", seed=42) -> dict[str, pd.DataFrame]:
    """Dati FINTI con regimi di trend, solo per test e demo. Nessun valore predittivo."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    out = {}
    for t in tickers:
        n = len(idx)
        regime = np.repeat(rng.normal(0.0003, 0.0009, n // 150 + 1), 150)[:n]
        vol = np.repeat(rng.uniform(0.007, 0.02, n // 200 + 1), 200)[:n]
        ret = regime + vol * rng.standard_t(4, n) / np.sqrt(2)
        close = 100 * np.exp(np.cumsum(ret))
        gap = rng.normal(0, vol * 0.4)
        open_ = np.r_[close[0], close[:-1]] * np.exp(gap)
        hi = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, vol * 0.5)))
        lo = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, vol * 0.5)))
        df = pd.DataFrame({"Open": open_, "High": hi, "Low": lo, "Close": close,
                           "Volume": rng.integers(1e5, 1e6, n)}, index=idx)
        out[t] = clean(df, t)
    return out


def load(cfg: dict, start=None, end=None) -> dict[str, pd.DataFrame]:
    from .config import resolve
    d = cfg["data"]
    start = start or d["history_start"]
    src = d["source"]
    if src == "yfinance":
        data = load_yfinance(cfg["universe"], start, end)
    elif src == "csv":
        data = load_csv(cfg["universe"], resolve(cfg, d["csv_dir"]))
    elif src == "synthetic":
        data = synthetic(cfg["universe"])
    else:
        raise ValueError(f"sorgente dati sconosciuta: {src}")
    if not data:
        raise RuntimeError("nessun dato caricato")
    return data


def check_staleness(data: dict[str, pd.DataFrame], today: pd.Timestamp, max_days: int) -> list[str]:
    """Ritorna i ticker con dati troppo vecchi (feed rotto, delisting, ecc.)."""
    return [t for t, df in data.items() if (today - df.index[-1]).days > max_days]


def load_cash_rate(cfg: dict, start=None, end=None) -> pd.Series | None:
    """Tasso dei T-bill USA a 13 settimane (^IRX) come proxy del rendimento della liquidità.
    Se non disponibile si usa costs.cash_rate_fallback."""
    if cfg["data"]["source"] != "yfinance" or not cfg["costs"].get("cash_rate_ticker"):
        return None
    try:
        import yfinance as yf
        raw = yf.Ticker(cfg["costs"]["cash_rate_ticker"]).history(start=start or cfg["data"]["history_start"], end=end)
        if raw is None or raw.empty:
            return None
        s = raw["Close"] / 100.0
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s[s.between(0, 0.2)].sort_index()
    except Exception as e:  # il tasso è un dettaglio: non deve fermare il bot
        log.warning("tasso liquidità non disponibile (%s): uso il valore di riserva", e)
        return None
