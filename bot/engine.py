"""Motore giornaliero. Lo STESSO codice gira in backtest e in paper trading live,
così quello che vedi nel backtest è quello che il bot farà davvero."""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .broker import Order, PaperBroker
from .risk import RiskManager
from .strategy import RotationStrategy, TrendStrategy

log = logging.getLogger(__name__)


def load_blackout(path: Path) -> set[str]:
    """Date (YYYY-MM-DD) in cui NON aprire nuove posizioni: FOMC, BCE, NFP, CPI...
    Un file compilato a mano è più affidabile di uno scraping che si rompe."""
    if not path.exists():
        return set()
    df = pd.read_csv(path, comment="#")
    return set(pd.to_datetime(df.iloc[:, 0]).dt.strftime("%Y-%m-%d"))


class Engine:
    def __init__(self, cfg: dict, data: dict[str, pd.DataFrame], broker: PaperBroker,
                 blackout: set[str] | None = None, cash_rate: pd.Series | None = None):
        self.cfg = cfg
        self.kind = cfg["strategy"].get("type", "breakout")
        self.strategy = RotationStrategy(cfg["strategy"]) if self.kind == "rotation" else TrendStrategy(cfg["strategy"])
        self.cash_rate = cash_rate
        self.cash_rate_fallback = cfg["costs"].get("cash_rate_fallback", 0.0)
        self.risk = RiskManager(cfg["risk"], cfg["portfolio"]["fractional_shares"])
        self.data = {t: self.strategy.prepare(df) for t, df in data.items()}
        self.broker = broker
        self.blackout = blackout or set()

    def calendar(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(sorted(set().union(*[df.index for df in self.data.values()])))

    def bars_on(self, date: pd.Timestamp) -> dict:
        return {t: df.loc[date] for t, df in self.data.items() if date in df.index}

    def step(self, date: pd.Timestamp) -> list[str]:
        """Un giorno di mercato. Ritorna un elenco di eventi leggibili."""
        b, ds = self.broker, date.strftime("%Y-%m-%d")
        bars = self.bars_on(date)
        n_fills = len(b.fills)

        b.fill_pending(ds, bars)       # 1. ordini di ieri all'apertura
        b.check_stops(ds, bars)        # 2. stop intraday
        if b.last_processed:           # interessi sulla liquidità per i giorni trascorsi
            b.accrue_interest(self.rate_on(date), (date - pd.Timestamp(b.last_processed)).days)
        eq = b.mark(ds, bars)          # 3. valorizzazione in chiusura
        b.last_processed = ds

        # 4. circuit breaker: drawdown oltre soglia => liquida tutto e fermati
        if not b.halted and self.risk.should_kill(eq, b.peak_equity):
            b.halted = True
            log.critical("KILL SWITCH: drawdown %.1f%%. Liquidazione e stop operatività.",
                         100 * self.risk.drawdown(eq, b.peak_equity))
        if b.halted:
            b.pending = [o for o in b.pending if o.side == "SELL"]
            for t, p in b.positions.items():
                b.submit(Order(t, "SELL", p.qty, ds, "kill switch"))
            return self._events(n_fills)

        if self.kind == "rotation":
            self._rotation(date, ds, bars, eq)
            return self._events(n_fills)

        # 5. uscite e trailing stop
        for t, p in list(b.positions.items()):
            row = bars.get(t)
            if row is None:
                continue
            if self.strategy.wants_exit(row):
                b.submit(Order(t, "SELL", p.qty, ds, "uscita: rottura minimi/trend"))
            else:
                p.stop = self.strategy.trail_stop(p.stop, row)

        # 6. nuovi ingressi
        if ds in self.blackout:
            log.info("%s: data in blackout macro, nessun nuovo ingresso", ds)
            return self._events(n_fills)

        selling = {o.ticker for o in b.pending if o.side == "SELL"}
        held = set(b.positions) - selling
        pending_buys = {o.ticker for o in b.pending if o.side == "BUY"}
        slots = self.cfg["risk"]["max_positions"] - len(held) - len(pending_buys)
        if slots <= 0:
            return self._events(n_fills)

        cands = [(t, r) for t, r in bars.items()
                 if t not in b.positions and t not in pending_buys and self.strategy.wants_entry(r)]
        cands.sort(key=lambda x: self.strategy.rank_key(x[1]), reverse=True)

        gross = b.gross_exposure() + b.pending_buy_notional(b.last_price)
        cash = b.cash - b.pending_buy_notional(b.last_price)
        for t, row in cands[:slots]:
            px = float(row["Close"])
            stop = self.strategy.initial_stop(row)
            qty, risk_amt = self.risk.size(eq, b.peak_equity, px, stop, gross, cash)
            if qty <= 0:
                continue
            b.submit(Order(t, "BUY", qty, ds, "breakout sopra trend", stop=stop, risk_amt=risk_amt))
            gross += qty * px
            cash -= qty * px * (1 + b.slippage_pct) + b.commission(qty * px)
        return self._events(n_fills)

    def rate_on(self, date: pd.Timestamp) -> float:
        if self.cash_rate is not None and len(self.cash_rate):
            v = self.cash_rate.asof(date)
            if pd.notna(v):
                return max(0.0, float(v))
        return self.cash_rate_fallback

    def _rotation(self, date: pd.Timestamp, ds: str, bars: dict, eq: float) -> None:
        """Ribilancia il primo giorno di borsa di ogni mese (ordini eseguiti all'apertura successiva)."""
        b, r = self.broker, self.cfg["risk"]
        if b.last_rebalance and pd.Timestamp(b.last_rebalance).to_period("M") == date.to_period("M"):
            return
        # serve il dato di oggi per tutto l'universo, altrimenti si rimanda a domani
        if len(bars) < len(self.data) * 0.8:
            log.warning("%s: dati incompleti, ribilanciamento rimandato", ds)
            return
        weights = self.strategy.target_weights(bars)
        if ds in self.blackout:
            weights = {t: w for t, w in weights.items() if t in b.positions}  # niente nuovi ingressi
        if self.risk.drawdown(eq, b.peak_equity) >= r["drawdown_reduce_at"]:
            weights = {t: w * r["drawdown_reduce_factor"] for t, w in weights.items()}
        weights = {t: min(w, r["max_position_pct"]) for t, w in weights.items()}
        tot = sum(weights.values())
        if tot > r["max_gross_exposure"]:
            weights = {t: w * r["max_gross_exposure"] / tot for t, w in weights.items()}

        investable = eq * (1 - 2 * (b.slippage_pct + b.commission_pct))  # margine per i costi
        band = self.cfg["strategy"].get("rebalance_band", 0.02) * eq
        rnd = self.risk._round
        b.pending = [o for o in b.pending if o.side == "SELL"]
        for t in set(b.positions) | set(weights):
            px = b.last_price.get(t)
            if not px:
                continue
            cur = b.positions[t].qty if t in b.positions else 0.0
            tgt = rnd(investable * weights.get(t, 0.0) / px)
            delta = tgt - cur
            if delta == 0:
                continue
            full_change = cur == 0 or tgt == 0
            if not full_change and abs(delta) * px < band:
                continue  # differenza piccola: non vale le commissioni
            if delta < 0:
                why = "rotazione: esce" if tgt == 0 else "rotazione: riduce"
                b.submit(Order(t, "SELL", -delta, ds, why))
            else:
                why = "rotazione: entra" if cur == 0 else "rotazione: aumenta"
                b.submit(Order(t, "BUY", delta, ds, why, add=cur > 0))
        b.last_rebalance = ds
        log.info("%s ribilanciamento: %s", ds, {t: round(w, 3) for t, w in sorted(weights.items())} or "tutto liquidità")

    def _events(self, since: int) -> list[str]:
        return [f"{f['date']} {f['side']} {f['ticker']} x{f['qty']} @ {f['price']} ({f['reason']})"
                for f in self.broker.fills[since:]]
