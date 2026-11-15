"""Gestione del rischio: dimensionamento, limiti di esposizione, circuit breaker sul drawdown."""
from __future__ import annotations

import math


class RiskManager:
    def __init__(self, p: dict, fractional: bool = False):
        self.p = p
        self.fractional = fractional

    def drawdown(self, equity: float, peak: float) -> float:
        return 0.0 if peak <= 0 else max(0.0, 1 - equity / peak)

    def should_kill(self, equity: float, peak: float) -> bool:
        return self.drawdown(equity, peak) >= self.p["drawdown_kill_at"]

    def risk_fraction(self, equity: float, peak: float) -> float:
        r = self.p["risk_per_trade"]
        if self.drawdown(equity, peak) >= self.p["drawdown_reduce_at"]:
            r *= self.p["drawdown_reduce_factor"]
        return r

    def _round(self, q: float) -> float:
        return math.floor(q * 1e4) / 1e4 if self.fractional else float(math.floor(q))

    def size(self, equity: float, peak: float, price: float, stop: float,
             gross_exposure: float, cash_available: float) -> tuple[float, float]:
        """Ritorna (quantità, rischio in valuta). Quantità 0 = non entrare."""
        dist = price - stop
        if price <= 0 or dist <= 0:
            return 0.0, 0.0
        risk_amt = equity * self.risk_fraction(equity, peak)
        q_risk = risk_amt / dist
        q_cap = equity * self.p["max_position_pct"] / price
        room = max(0.0, equity * self.p["max_gross_exposure"] - gross_exposure)
        q_room = min(room, cash_available) / price
        q = self._round(max(0.0, min(q_risk, q_cap, q_room)))
        return q, risk_amt
