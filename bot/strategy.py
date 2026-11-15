"""Strategia trend-following multi-asset (stile Donchian/Turtle con filtro di regime).

Tutti gli indicatori al giorno t usano SOLO dati fino a t compreso.
I segnali calcolati sulla chiusura di t vengono eseguiti all'apertura di t+1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()  # media di Wilder


class TrendStrategy:
    def __init__(self, p: dict):
        self.p = p

    @property
    def warmup(self) -> int:
        return max(self.p["trend_sma"], self.p["entry_breakout"], self.p["momentum_lookback"]) + 5

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.p
        out = df.copy()
        out["sma_trend"] = out["Close"].rolling(p["trend_sma"]).mean()
        # shift(1): il massimo dei N giorni PRECEDENTI, così il breakout è reale e non usa la barra di oggi
        out["hh_entry"] = out["High"].rolling(p["entry_breakout"]).max().shift(1)
        out["ll_exit"] = out["Low"].rolling(p["exit_breakout"]).min().shift(1)
        out["atr"] = atr(out, p["atr_period"])
        out["momentum"] = out["Close"] / out["Close"].shift(p["momentum_lookback"]) - 1
        return out

    @staticmethod
    def _ok(row, cols) -> bool:
        return all(np.isfinite(row[c]) for c in cols)

    def wants_entry(self, row) -> bool:
        if not self._ok(row, ["sma_trend", "hh_entry", "atr", "momentum"]):
            return False
        return row["Close"] > row["sma_trend"] and row["Close"] > row["hh_entry"] and row["momentum"] > 0 and row["atr"] > 0

    def wants_exit(self, row) -> bool:
        if not self._ok(row, ["ll_exit", "sma_trend"]):
            return False
        return row["Close"] < row["ll_exit"] or row["Close"] < row["sma_trend"]

    def initial_stop(self, row) -> float:
        return float(row["Close"] - self.p["initial_stop_atr"] * row["atr"])

    def trail_stop(self, current_stop: float, row) -> float:
        if not np.isfinite(row["atr"]):
            return current_stop
        return max(current_stop, float(row["Close"] - self.p["trailing_stop_atr"] * row["atr"]))  # lo stop sale e basta

    @staticmethod
    def rank_key(row) -> float:
        return float(row["momentum"])


class RotationStrategy:
    """Rotazione mensile (stile Faber / momentum multi-asset).

    Una volta al mese: tra gli ETF sopra la media a 200 giorni e con momentum positivo
    tiene i migliori "top_n". Ogni slot vale 1/top_n del capitale: gli slot vuoti restano
    in liquidità, che matura interessi. Nei mercati ribassisti il portafoglio si svuota da solo.
    """

    def __init__(self, p: dict):
        self.p = p

    @property
    def warmup(self) -> int:
        return max(self.p["trend_sma"], max(self.p["score_lookbacks"]), self.p["vol_lookback"]) + 5

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.p
        out = df.copy()
        out["sma_trend"] = out["Close"].rolling(p["trend_sma"]).mean()
        out["score"] = sum(out["Close"] / out["Close"].shift(n) - 1 for n in p["score_lookbacks"]) / len(p["score_lookbacks"])
        out["vol"] = out["Close"].pct_change().rolling(p["vol_lookback"]).std() * np.sqrt(252)
        return out

    def eligible(self, row) -> bool:
        vals = [row["sma_trend"], row["score"], row["vol"]]
        if not all(np.isfinite(v) for v in vals):
            return False
        return row["Close"] > row["sma_trend"] and row["score"] > 0 and row["vol"] > 0

    def target_weights(self, rows: dict) -> dict[str, float]:
        p = self.p
        cands = sorted(((t, r) for t, r in rows.items() if self.eligible(r)),
                       key=lambda x: float(x[1]["score"]), reverse=True)[: p["top_n"]]
        if not cands:
            return {}
        slot = 1.0 / p["top_n"]
        if p.get("weighting") == "inverse_vol":
            inv = {t: 1 / float(r["vol"]) for t, r in cands}
            tot = sum(inv.values())
            budget = slot * len(cands)  # gli slot vuoti restano in liquidità
            return {t: budget * v / tot for t, v in inv.items()}
        return {t: slot for t, _ in cands}
