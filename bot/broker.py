"""Broker virtuale (paper) con costi, slippage, gap sugli stop e stato persistente su file."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Position:
    ticker: str
    qty: float
    entry_price: float
    entry_date: str
    stop: float
    entry_cost: float = 0.0  # commissioni pagate in entrata


@dataclass
class Order:
    ticker: str
    side: str            # "BUY" | "SELL"
    qty: float
    created: str
    reason: str
    stop: float = 0.0    # solo BUY: stop iniziale (0 = nessuno stop)
    risk_amt: float = 0.0
    add: bool = False    # BUY che incrementa una posizione esistente (ribilanciamento)


@dataclass
class PaperBroker:
    cash: float
    commission_pct: float
    commission_min: float
    slippage_pct: float
    fractional: bool = False
    positions: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    trades: list = field(default_factory=list)       # operazioni chiuse
    fills: list = field(default_factory=list)        # tutti gli eseguiti
    equity_curve: list = field(default_factory=list) # [data, equity]
    last_price: dict = field(default_factory=dict)
    peak_equity: float = 0.0
    halted: bool = False
    last_processed: str | None = None
    last_rebalance: str | None = None
    interest_earned: float = 0.0

    def accrue_interest(self, annual_rate: float, days: float) -> None:
        """Interessi sulla liquidità (come un conto deposito / BOT a breve)."""
        if self.cash > 0 and annual_rate > 0 and days > 0:
            i = self.cash * annual_rate * days / 365.0
            self.cash += i
            self.interest_earned += i

    # ---------- costi ----------
    def commission(self, notional: float) -> float:
        return max(self.commission_min, abs(notional) * self.commission_pct)

    def _round(self, q):
        return math.floor(q * 1e4) / 1e4 if self.fractional else float(math.floor(q))

    # ---------- ordini ----------
    def submit(self, order: Order) -> None:
        if order.qty <= 0:
            return
        if any(o.ticker == order.ticker and o.side == order.side for o in self.pending):
            return
        self.pending.append(order)

    def pending_buy_notional(self, prices: dict) -> float:
        return sum(o.qty * prices.get(o.ticker, 0.0) for o in self.pending if o.side == "BUY")

    def fill_pending(self, date: str, bars: dict) -> None:
        """Esegue gli ordini all'APERTURA della barra di oggi (generati ieri in chiusura)."""
        keep = []
        # prima le vendite: liberano liquidità per gli acquisti
        for o in sorted(self.pending, key=lambda o: o.side != "SELL"):
            bar = bars.get(o.ticker)
            if bar is None:
                keep.append(o)  # mercato chiuso per questo strumento: riprova domani
                continue
            if o.side == "SELL":
                if o.ticker in self.positions:
                    self._close(o.ticker, bar["Open"] * (1 - self.slippage_pct), date, o.reason, o.qty)
            else:
                self._open(o, bar, date)
        self.pending = keep

    def _open(self, o: Order, bar, date: str) -> None:
        if o.ticker in self.positions and not o.add:
            return
        px = bar["Open"] * (1 + self.slippage_pct)
        if px <= o.stop:
            self._log_fill(date, o.ticker, "SKIP", 0, px, 0, "apertura sotto lo stop: ingresso annullato")
            return
        qty = o.qty
        if o.risk_amt > 0:  # se c'è gap in su il rischio per azione cresce: ridimensiona
            qty = min(qty, self._round(o.risk_amt / (px - o.stop)))
        while qty > 0 and qty * px + self.commission(qty * px) > self.cash:
            qty = self._round(qty * 0.98) if self.fractional else qty - 1
        if qty <= 0:
            self._log_fill(date, o.ticker, "SKIP", 0, px, 0, "liquidità insufficiente")
            return
        fee = self.commission(qty * px)
        self.cash -= qty * px + fee
        if o.ticker in self.positions:  # incremento: prezzo medio di carico
            p = self.positions[o.ticker]
            p.entry_price = (p.entry_price * p.qty + px * qty) / (p.qty + qty)
            p.qty += qty
            p.entry_cost += fee
        else:
            self.positions[o.ticker] = Position(o.ticker, qty, px, date, o.stop, fee)
        self._log_fill(date, o.ticker, "BUY", qty, px, fee, o.reason)

    def _close(self, ticker: str, px: float, date: str, reason: str, qty: float | None = None) -> None:
        p: Position = self.positions[ticker]
        q = p.qty if qty is None or qty >= p.qty else qty
        fee = self.commission(q * px)
        share = q / p.qty
        entry_cost = p.entry_cost * share
        self.cash += q * px - fee
        pnl = (px - p.entry_price) * q - fee - entry_cost
        if q >= p.qty:
            del self.positions[ticker]
        else:
            p.qty -= q
            p.entry_cost -= entry_cost
        self.trades.append({
            "ticker": ticker, "entry_date": p.entry_date, "exit_date": date,
            "qty": q, "entry": round(p.entry_price, 4), "exit": round(px, 4),
            "pnl": round(pnl, 2), "ret_pct": round(100 * pnl / (p.entry_price * q), 2), "reason": reason,
        })
        self._log_fill(date, ticker, "SELL", q, px, fee, reason)

    def check_stops(self, date: str, bars: dict, skip: set | None = None) -> None:
        """Stop eseguiti intraday. Se l'apertura è già sotto lo stop (gap), esce all'apertura: perdita reale, non teorica."""
        for t in list(self.positions):
            bar = bars.get(t)
            if bar is None:
                continue
            stop = self.positions[t].stop
            if stop <= 0:
                continue
            if bar["Open"] <= stop:
                self._close(t, bar["Open"] * (1 - self.slippage_pct), date, "stop (gap in apertura)")
            elif bar["Low"] <= stop:
                self._close(t, stop * (1 - self.slippage_pct), date, "stop")
        # un SELL in coda per una posizione già chiusa dallo stop non serve più
        self.pending = [o for o in self.pending if not (o.side == "SELL" and o.ticker not in self.positions)]

    def mark(self, date: str, bars: dict) -> float:
        for t, bar in bars.items():
            self.last_price[t] = float(bar["Close"])
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        self.equity_curve.append([date, round(eq, 2)])
        return eq

    def equity(self) -> float:
        return self.cash + sum(p.qty * self.last_price.get(t, p.entry_price) for t, p in self.positions.items())

    def gross_exposure(self) -> float:
        return sum(p.qty * self.last_price.get(t, p.entry_price) for t, p in self.positions.items())

    def _log_fill(self, date, ticker, side, qty, px, fee, reason):
        self.fills.append({"date": date, "ticker": ticker, "side": side, "qty": qty,
                           "price": round(px, 4), "fee": round(fee, 2), "reason": reason})

    # ---------- persistenza ----------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = {t: asdict(p) for t, p in self.positions.items()}
        d["pending"] = [asdict(o) for o in self.pending]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PaperBroker":
        d = dict(d)
        pos = {t: Position(**p) for t, p in d.pop("positions", {}).items()}
        pend = [Order(**o) for o in d.pop("pending", [])]
        b = cls(**d)
        b.positions, b.pending = pos, pend
        return b

    def save(self, path: str | Path) -> None:
        """Scrittura atomica: se il processo muore a metà, il vecchio stato resta intatto."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path) -> "PaperBroker":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
