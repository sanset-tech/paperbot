import copy
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import data as dmod
from bot.broker import Order, PaperBroker, Position
from bot.config import ConfigError, load_config, validate
from bot.engine import Engine
from bot.risk import RiskManager
from run_backtest import make_broker, run

CFG = load_config(Path(__file__).resolve().parents[1] / "config.json")


def broker(cash=10000.0):
    return PaperBroker(cash=cash, commission_pct=0.001, commission_min=1.0, slippage_pct=0.0, peak_equity=cash)


def bar(o, h, l, c):
    return {"Open": o, "High": h, "Low": l, "Close": c}


def test_ordine_eseguito_all_apertura_successiva():
    b = broker()
    b.submit(Order("X", "BUY", 10, "2024-01-01", "t", stop=90))
    assert "X" not in b.positions
    b.fill_pending("2024-01-02", {"X": bar(100, 105, 99, 104)})
    p = b.positions["X"]
    assert p.entry_price == 100 and p.qty == 10
    assert b.cash == pytest.approx(10000 - 1000 - 1.0)


def test_stop_in_gap_esce_all_apertura_non_allo_stop():
    b = broker()
    b.positions["X"] = Position("X", 10, 100, "2024-01-01", 95)
    b.check_stops("2024-01-02", {"X": bar(90, 92, 88, 91)})
    assert b.trades[0]["exit"] == 90  # non 95: il gap si paga
    assert "X" not in b.positions


def test_stop_intraday():
    b = broker()
    b.positions["X"] = Position("X", 10, 100, "2024-01-01", 95)
    b.check_stops("2024-01-02", {"X": bar(99, 100, 94, 97)})
    assert b.trades[0]["exit"] == 95


def test_gap_up_ridimensiona_per_rischio():
    b = broker(100000)
    b.submit(Order("X", "BUY", 100, "d", "t", stop=90, risk_amt=1000))  # pensato a 100: rischio 10/az
    b.fill_pending("d2", {"X": bar(110, 112, 109, 111)})                 # apre a 110: rischio 20/az
    assert b.positions["X"].qty == 50


def test_sizing_rispetta_rischio_e_tetti():
    r = RiskManager(CFG["risk"])
    q, risk = r.size(equity=100000, peak=100000, price=100, stop=98, gross_exposure=0, cash_available=100000)
    assert risk == pytest.approx(1000)
    assert q == 200  # 1000/2 = 500 azioni, ma tetto 20% = 200
    q2, _ = r.size(100000, 100000, 100, 90, gross_exposure=95000, cash_available=5000)
    assert q2 == 50  # resta spazio solo per 5000


def test_rischio_dimezzato_in_drawdown():
    r = RiskManager(CFG["risk"])
    assert r.risk_fraction(80000, 100000) == pytest.approx(CFG["risk"]["risk_per_trade"] / 2)
    assert r.should_kill(74000, 100000)


def test_stato_salvato_e_ricaricato(tmp_path):
    b = broker()
    b.positions["X"] = Position("X", 5, 100, "2024-01-01", 90)
    b.submit(Order("Y", "BUY", 3, "2024-01-01", "t", stop=10))
    b.save(tmp_path / "s.json")
    b2 = PaperBroker.load(tmp_path / "s.json")
    assert b2.to_dict() == b.to_dict()


def test_config_rifiuta_leva():
    cfg = copy.deepcopy(CFG)
    cfg["risk"]["max_gross_exposure"] = 2.0
    with pytest.raises(ConfigError):
        validate(cfg)


def _cfg_small(kind="breakout"):
    cfg = copy.deepcopy(CFG)
    cfg["universe"] = ["A", "B", "C", "D", "E", "F"]
    cfg["strategy"]["type"] = kind
    return cfg


@pytest.mark.parametrize("kind", ["breakout", "rotation"])
def test_nessun_lookahead(kind):
    """Cambiare il futuro non deve cambiare nessuna decisione presa nel passato."""
    cfg = _cfg_small(kind)
    data = dmod.synthetic(cfg["universe"], "2012-01-01", "2018-12-31", seed=7)
    cut = pd.Timestamp("2016-06-30")
    eng1 = run(cfg, data, end=cut)
    tampered = {}
    for t, df in data.items():
        df = df.copy()
        df.loc[df.index > cut, ["Open", "High", "Low", "Close"]] *= 3.0
        tampered[t] = df
    eng2 = run(cfg, tampered, end=cut)
    assert eng1.broker.fills == eng2.broker.fills
    assert eng1.broker.equity_curve == eng2.broker.equity_curve


@pytest.mark.parametrize("kind", ["breakout", "rotation"])
def test_backtest_coerente(kind):
    cfg = _cfg_small(kind)
    data = dmod.synthetic(cfg["universe"], "2012-01-01", "2020-12-31", seed=3)
    b = run(cfg, data).broker
    assert len(b.trades) > 5
    assert b.cash >= -1e-6                       # mai in negativo: niente leva nascosta
    assert len(b.positions) <= cfg["risk"]["max_positions"]
    # equity finale = capitale + somma P&L chiusi + P&L aperto
    open_pnl = sum((b.last_price[t] - p.entry_price) * p.qty - p.entry_cost for t, p in b.positions.items())
    expected = cfg["portfolio"]["initial_capital"] + sum(t["pnl"] for t in b.trades) + open_pnl + b.interest_earned
    assert b.equity() == pytest.approx(expected, abs=1.0)


@pytest.mark.parametrize("kind", ["breakout", "rotation"])
def test_kill_switch_liquida(kind):
    cfg = _cfg_small(kind)
    data = dmod.synthetic(cfg["universe"], "2012-01-01", "2016-12-31", seed=1)
    eng = Engine(cfg, data, make_broker(cfg))
    cal = eng.calendar()
    for d in cal[:400]:
        eng.step(d)
    eng.broker.peak_equity = eng.broker.equity() * 2  # simula un crollo del 50%
    eng.step(cal[400])
    assert eng.broker.halted
    eng.step(cal[401])
    eng.step(cal[402])
    assert not eng.broker.positions
    assert not any(o.side == "BUY" for o in eng.broker.pending)


def test_vendita_parziale_e_incremento():
    b = broker(100000)
    b.submit(Order("X", "BUY", 100, "d0", "t"))
    b.fill_pending("d1", {"X": bar(100, 101, 99, 100)})
    b.submit(Order("X", "BUY", 100, "d1", "t", add=True))
    b.fill_pending("d2", {"X": bar(110, 111, 109, 110)})
    assert b.positions["X"].qty == 200 and b.positions["X"].entry_price == pytest.approx(105)
    b.submit(Order("X", "SELL", 50, "d2", "riduce"))
    b.fill_pending("d3", {"X": bar(120, 121, 119, 120)})
    assert b.positions["X"].qty == 150
    assert b.trades[-1]["qty"] == 50


def test_interessi_liquidita():
    b = broker(10000)
    b.accrue_interest(0.0365, 10)
    assert b.cash == pytest.approx(10010)


def test_rotazione_mensile_pesi():
    cfg = _cfg_small("rotation")
    data = dmod.synthetic(cfg["universe"], "2012-01-01", "2016-12-31", seed=11)
    eng = run(cfg, data)
    b = eng.broker
    months = {f["date"][:7] for f in b.fills if f["side"] in ("BUY", "SELL") and "rotazione" in f["reason"]}
    days = {f["date"] for f in b.fills if "rotazione" in f["reason"]}
    assert len(days) <= len(months) + 1  # al massimo un giorno di esecuzione per mese
    assert len(b.positions) <= cfg["strategy"]["top_n"]
    for t, p in b.positions.items():
        assert p.qty * b.last_price[t] <= b.equity() * (cfg["risk"]["max_position_pct"] + 0.05)


def test_inizio_mese():
    from bot.strategy import in_turn_of_month
    assert in_turn_of_month(pd.Timestamp("2026-11-30"))      # ultimo giorno di borsa di novembre
    assert in_turn_of_month(pd.Timestamp("2026-12-03"))      # terzo giorno di borsa
    assert not in_turn_of_month(pd.Timestamp("2026-12-04"))
    assert not in_turn_of_month(pd.Timestamp("2026-11-27"))


@pytest.mark.parametrize("seed", [2, 5])
def test_tattica_coerente_e_senza_lookahead(seed):
    from bot.config import tactical_cfg
    cfg = copy.deepcopy(CFG)
    cfg["tactical"]["enabled"] = True
    tc = tactical_cfg(cfg)
    data = dmod.synthetic(tc["universe"], "2012-01-01", "2018-12-31", seed=seed)
    b = run(tc, data).broker
    assert len(b.trades) > 10
    assert b.cash >= -1e-6
    assert all(f["side"] != "BUY" or f["qty"] * f["price"] <= tc["portfolio"]["initial_capital"] * 0.4 for f in b.fills)
    cut = pd.Timestamp("2016-06-30")
    e1 = run(tc, data, end=cut).broker
    tamp = {t: df.assign(**{c: df[c].where(df.index <= cut, df[c] * 0.5) for c in ["Open", "High", "Low", "Close"]}) for t, df in data.items()}
    e2 = run(tc, tamp, end=cut).broker
    assert e1.fills == e2.fills


def _bars(rows, n_pad=25, base=100.0):
    pad = [(base, base + 0.5, base - 0.5, base + 0.1)] * n_pad
    df = pd.DataFrame(pad + rows, columns=["Open", "High", "Low", "Close"])
    return df


def test_pattern_candele():
    from lab_data import patterns
    names = lambda df: {p["pattern"] for p in patterns(df)}
    assert "engulfing rialzista" in names(_bars([(101, 101.2, 99.4, 99.6), (99.4, 101.8, 99.2, 101.6)]))
    assert "engulfing ribassista" in names(_bars([(99.6, 101.2, 99.4, 101.0), (101.2, 101.4, 99.0, 99.3)]))
    assert "hammer" in names(_bars([(96, 96.2, 95, 95.5), (95.4, 95.6, 93.0, 95.6)], base=100))
    assert "inside bar" in names(_bars([(100, 102, 98, 101), (100.5, 101.5, 99, 100.8)]))
    assert "doji" in names(_bars([(100, 101, 99, 100.05)]))
    assert names(_bars([(100, 100.6, 99.6, 100.3)])) <= {"inside bar", "doji"}


def test_candela_di_oggi_esclusa_se_seduta_aperta():
    from lab_data import closed_bars
    idx = pd.date_range("2026-11-12", periods=3, freq="D", tz="America/New_York")
    df = pd.DataFrame({"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3], "Close": [1, 2, 3]}, index=idx)
    assert len(closed_bars(df, pd.Timestamp("2026-11-14 11:00", tz="America/New_York"))) == 2
    assert len(closed_bars(df, pd.Timestamp("2026-11-14 16:30", tz="America/New_York"))) == 3


def test_indicatori_candele():
    import numpy as np
    from lab_data import indicators
    idx = pd.bdate_range("2025-01-01", periods=260)
    c = pd.Series(np.linspace(100, 130, 260), index=idx)
    df = pd.DataFrame({"Open": c * 0.999, "High": c * 1.005, "Low": c * 0.995, "Close": c, "Volume": 1000.0}, index=idx)
    df.iloc[-1, df.columns.get_loc("Volume")] = 2000.0
    ind = indicators(df)
    assert ind["sopra_media50"] and ind["sopra_media200"]
    assert ind["rsi14"] > 70 and ind["volume_relativo"] == 2.0
    assert 0.7 <= ind["posizione_range20"] <= 1.0
    assert ind["atr_pct"] > 0


def test_json_senza_nan():
    import json, math
    from lab_data import clean_nan
    x = clean_nan({"a": [1.0, float("nan")], "b": {"c": math.nan}})
    assert json.dumps(x, allow_nan=False) == '{"a": [1.0, null], "b": {"c": null}}'
