"""Metriche di performance."""
from __future__ import annotations

import numpy as np
import pandas as pd


def equity_series(curve: list) -> pd.Series:
    s = pd.Series({pd.Timestamp(d): v for d, v in curve})
    return s.sort_index()


def summary(eq: pd.Series, trades: list | None = None) -> dict:
    eq = eq.dropna()
    if len(eq) < 2:
        return {}
    rets = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    vol = rets.std() * np.sqrt(252)
    downside = rets[rets < 0].std() * np.sqrt(252)
    dd = eq / eq.cummax() - 1
    out = {
        "inizio": eq.index[0].date().isoformat(),
        "fine": eq.index[-1].date().isoformat(),
        "capitale_finale": round(float(eq.iloc[-1]), 2),
        "rendimento_totale_%": round(100 * (eq.iloc[-1] / eq.iloc[0] - 1), 2),
        "CAGR_%": round(100 * cagr, 2),
        "volatilita_%": round(100 * vol, 2),
        "sharpe": round(rets.mean() / rets.std() * np.sqrt(252), 2) if rets.std() > 0 else 0.0,
        "sortino": round(rets.mean() * 252 / downside, 2) if downside > 0 else 0.0,
        "max_drawdown_%": round(100 * dd.min(), 2),
        "calmar": round(cagr / abs(dd.min()), 2) if dd.min() < 0 else 0.0,
    }
    if trades:
        pnl = np.array([t["pnl"] for t in trades])
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        out.update({
            "operazioni": len(pnl),
            "win_rate_%": round(100 * len(wins) / len(pnl), 1),
            "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if losses.sum() != 0 else float("inf"),
            "media_vincita": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "media_perdita": round(float(losses.mean()), 2) if len(losses) else 0.0,
        })
    return out


def buy_and_hold(data: dict[str, pd.DataFrame], start, end, capital: float) -> pd.Series:
    """Benchmark: stesso universo, pesi uguali, comprato e tenuto. Se il bot non batte questo, non serve."""
    closes = pd.DataFrame({t: df["Close"] for t, df in data.items()}).loc[start:end].ffill().dropna(axis=1, how="all")
    closes = closes.bfill()
    norm = closes / closes.iloc[0]
    return norm.mean(axis=1) * capital
