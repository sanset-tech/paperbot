"""Test del conto ombra Alpaca. Con alpaca-py installato (GitHub) usano le classi vere della
libreria per costruire le richieste; senza, una copia minima delle stesse classi. Nessuna
chiamata di rete: il client Alpaca è sempre finto."""
import enum
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REAL_ALPACA = importlib.util.find_spec("alpaca") is not None


def _install_stub():
    class _Req:
        def __init__(self, **kw):
            self.__dict__.update({"order_class": None, "take_profit": None, "stop_loss": None}, **kw)

    en = types.ModuleType("alpaca.trading.enums")
    en.OrderSide = enum.Enum("OrderSide", {"BUY": "buy", "SELL": "sell"})
    en.TimeInForce = enum.Enum("TimeInForce", {"DAY": "day", "GTC": "gtc"})
    en.OrderClass = enum.Enum("OrderClass", {"SIMPLE": "simple", "BRACKET": "bracket", "OTO": "oto"})
    en.QueryOrderStatus = enum.Enum("QueryOrderStatus", {"OPEN": "open"})
    rq = types.ModuleType("alpaca.trading.requests")
    for n in ("MarketOrderRequest", "StopLossRequest", "TakeProfitRequest", "StopOrderRequest", "GetOrdersRequest"):
        setattr(rq, n, type(n, (_Req,), {}))
    cl = types.ModuleType("alpaca.trading.client")
    cl.TradingClient = lambda *a, **k: SimpleNamespace(args=a, kwargs=k)
    for name, mod in {"alpaca": types.ModuleType("alpaca"), "alpaca.trading": types.ModuleType("alpaca.trading"),
                      "alpaca.trading.enums": en, "alpaca.trading.requests": rq, "alpaca.trading.client": cl}.items():
        sys.modules[name] = mod


if not REAL_ALPACA:
    _install_stub()

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce  # noqa: E402

from bot import alpaca_bridge as ab  # noqa: E402
from bot.broker import Order, PaperBroker, Position  # noqa: E402
from bot.config import load_config  # noqa: E402

CFG = load_config(Path(__file__).resolve().parents[1] / "config.json")


class FakeClient:
    def __init__(self, open_=False, positions=None, fail=None, open_orders=None, orders=None):
        self.sent, self.cancelled, self.open_ = [], [], open_
        self.positions = positions or {}
        self.fail = fail  # eccezione da sollevare all'invio
        self.open_orders = open_orders or {}
        self.orders = orders or {}  # client_order_id -> ordine già presente su Alpaca (con status)
        self.lookups = []

    def get_clock(self):
        return SimpleNamespace(is_open=self.open_)

    def get_order_by_client_id(self, cid):
        self.lookups.append(cid)
        if cid in self.orders:
            return self.orders[cid]
        if any(r.client_order_id == cid for r in self.sent):
            return SimpleNamespace(id="x", status="accepted")
        raise RuntimeError("404 order not found")

    def submit_order(self, order_data):
        if self.fail:
            raise self.fail
        self.sent.append(order_data)
        return SimpleNamespace(id=f"id{len(self.sent)}", status="accepted")

    def get_open_position(self, t):
        if t not in self.positions:
            raise RuntimeError("404 position does not exist")
        return SimpleNamespace(qty=str(self.positions[t]))

    def get_orders(self, filter=None):
        return [SimpleNamespace(id=i) for i in self.open_orders.get(filter.symbols[0], [])]

    def cancel_order_by_id(self, i):
        self.cancelled.append(i)

    def get_all_positions(self):
        return [SimpleNamespace(symbol=t, qty=str(q)) for t, q in self.positions.items()]


def cfg(**alp):
    c = json.loads(json.dumps(CFG))
    c["alpaca"] = {**c.get("alpaca", {}), "enabled": True, **alp}
    return c


def broker_with(pending, positions=None):
    b = PaperBroker(cash=50000.0, commission_pct=0.001, commission_min=1.0, slippage_pct=0.0005, peak_equity=50000.0)
    b.pending = list(pending)
    b.positions = positions or {}
    b.last_price = {"SPY": 600.0, "QQQ": 500.0}
    b.last_processed = "2026-11-18"
    return b


def bridge(tmp_path, client, **alp):
    return ab.AlpacaBridge(cfg(**alp), tmp_path / "alpaca_orders.json", client)


def test_senza_chiavi_nessun_ordine_e_avviso_senza_valori(monkeypatch, caplog):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "segreto-da-non-mostrare")
    caplog.set_level("WARNING")
    assert ab.make_client() is None
    assert "ALPACA_API_KEY" in caplog.text and "segreto-da-non-mostrare" not in caplog.text
    out = ab.sync_account(cfg(), broker_with([Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580.123)]),
                          "main", lambda c, p: Path("/nonesiste") / p)
    assert out == ["Alpaca: non attivo (chiavi mancanti)"]


def test_client_sempre_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    if REAL_ALPACA:
        c = ab.make_client()
        assert "paper" in str(c._base_url).lower()
    else:
        assert ab.make_client().kwargs == {"paper": True}


def test_buy_con_stop_diventa_oto_gtc_arrotondato(tmp_path):
    c = FakeClient()
    ev = bridge(tmp_path, c).sync(broker_with([Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580.1234)]))
    r = c.sent[0]
    assert r.order_class == OrderClass.OTO and r.take_profit is None
    assert r.stop_loss.stop_price == 580.12 and r.time_in_force == TimeInForce.GTC
    assert r.side == OrderSide.BUY and r.qty == 5 and r.client_order_id == "pb-main-2026-11-18-SPY-BUY"
    assert any("inviato BUY SPY" in e for e in ev)


def test_take_profit_solo_se_configurato_diventa_bracket(tmp_path):
    c = FakeClient()
    bridge(tmp_path, c, take_profit_r=2).sync(broker_with([Order("SPY", "BUY", 5, "2026-11-18", "t", stop=590)]))
    r = c.sent[0]
    assert r.order_class == OrderClass.BRACKET
    assert r.take_profit.limit_price == 620.0  # 600 + 2 * (600 - 590)


def test_buy_senza_stop_resta_ordine_semplice(tmp_path):
    c = FakeClient()
    bridge(tmp_path, c).sync(broker_with([Order("SPY", "BUY", 5, "2026-11-18", "rotazione")]))
    assert c.sent[0].order_class in (None, OrderClass.SIMPLE) and c.sent[0].stop_loss is None


def test_nessun_doppione_tra_esecuzioni(tmp_path):
    o = Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580)
    c = FakeClient()
    b1 = bridge(tmp_path, c)
    b1.sync(broker_with([o]))
    b1.ledger.save()
    bridge(tmp_path, c).sync(broker_with([o]))       # 2ª esecuzione: registro letto da file
    (tmp_path / "alpaca_orders.json").unlink()
    bridge(tmp_path, c).sync(broker_with([o]))       # registro perso: lo blocca il client_order_id
    assert len(c.sent) == 1


def test_errore_api_registrato_senza_eccezioni_e_tentativi_limitati(tmp_path):
    c = FakeClient(fail=RuntimeError("403 insufficient buying power"))
    o = Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580)
    for _ in range(5):
        br = bridge(tmp_path, c, max_tentativi=3)
        ev = br.sync(broker_with([o]))
        br.ledger.save()
    rec = json.loads((tmp_path / "alpaca_orders.json").read_text())["ordini"]["pb-main-2026-11-18-SPY-BUY"]
    assert rec["stato"] == "errore" and rec["tentativi"] == 3 and "insufficient" in rec["errore"]
    assert any("abbandonato" in e for e in ev)


def test_mercato_aperto_non_invia(tmp_path):
    c = FakeClient(open_=True)
    ev = bridge(tmp_path, c).sync(broker_with([Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580)]))
    assert c.sent == [] and "mercato aperto" in ev[0]


def test_sell_annulla_gli_stop_e_vende(tmp_path):
    c = FakeClient(positions={"QQQ": 10}, open_orders={"QQQ": ["stop1"]})
    b = broker_with([Order("QQQ", "SELL", 10, "2026-11-18", "rotazione: esce")],
                    {"QQQ": Position("QQQ", 10, 480, "2026-11-01", 0.0)})
    bridge(tmp_path, c).sync(b)
    assert c.cancelled == ["stop1"]
    assert len(c.sent) == 1 and c.sent[0].side == OrderSide.SELL and c.sent[0].qty == 10


def test_sell_parziale_rimette_lo_stop_sul_resto(tmp_path):
    c = FakeClient(positions={"QQQ": 10}, open_orders={"QQQ": ["stop1"]})
    b = broker_with([Order("QQQ", "SELL", 4, "2026-11-18", "ribilanciamento")],
                    {"QQQ": Position("QQQ", 10, 480, "2026-11-01", 470.555)})
    bridge(tmp_path, c).sync(b)
    sell, stop = c.sent
    assert sell.qty == 4 and stop.qty == 6 and stop.stop_price == 470.56
    assert stop.client_order_id.endswith("-stop")


def test_sell_senza_posizione_su_alpaca_saltato(tmp_path):
    c = FakeClient()
    ev = bridge(tmp_path, c).sync(broker_with([Order("QQQ", "SELL", 4, "2026-11-18", "kill switch")]))
    assert c.sent == [] and any("nessuna posizione" in e for e in ev)


def test_riconciliazione_segnala_differenze(tmp_path):
    c = FakeClient(positions={"SPY": 3})
    br = bridge(tmp_path, c)
    ev = br.sync(broker_with([], {"SPY": Position("SPY", 5, 600, "2026-11-01", 0.0)}))
    assert "ATTENZIONE" in ev[-1]
    assert br.ledger.data["riconciliazione"]["differenze"] == {"SPY": {"simulatore": 5, "alpaca": 3}}


def test_registro_corrotto_non_blocca(tmp_path):
    (tmp_path / "alpaca_orders.json").write_text("{non json")
    c = FakeClient()
    bridge(tmp_path, c).sync(broker_with([Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580)]))
    assert len(c.sent) == 1 and (tmp_path / "alpaca_orders.corrotto.json").exists()


def test_sync_account_non_solleva_mai(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "make_client", lambda a="main": (_ for _ in ()).throw(RuntimeError("boom")))
    out = ab.sync_account(cfg(), broker_with([]), "main", lambda c, p: tmp_path / p)
    assert out and "ERRORE" in out[0]


def test_conto_non_elencato_ignorato(tmp_path):
    assert ab.sync_account(cfg(), broker_with([]), "tattica", lambda c, p: tmp_path / p) == []
    assert ab.sync_account(cfg(enabled=False), broker_with([]), "main", lambda c, p: tmp_path / p) == []


def _pos():
    return {"DBC": Position("DBC", 272, 23.0, "2026-11-16", 0.0), "EFA": Position("EFA", 95, 90.0, "2026-11-16", 0.0),
            "SPY": Position("SPY", 13, 750.0, "2026-11-16", 0.0), "VNQ": Position("VNQ", 103, 90.0, "2026-11-16", 0.0)}


def test_allineamento_una_sola_volta(tmp_path):
    c = FakeClient()
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    ev = br.sync(broker_with([], _pos()))
    br.ledger.save()
    assert sorted((r.symbol, r.qty) for r in c.sent) == [("DBC", 272), ("EFA", 95), ("SPY", 13), ("VNQ", 103)]
    assert all(r.order_class in (None, OrderClass.SIMPLE) and r.stop_loss is None for r in c.sent)  # niente stop
    assert any("completato" in e for e in ev)
    bridge(tmp_path, c, allineamento_iniziale=True).sync(broker_with([], _pos()))  # 2ª esecuzione
    (tmp_path / "alpaca_orders.json").unlink()                                      # registro perso
    bridge(tmp_path, c, allineamento_iniziale=True).sync(broker_with([], _pos()))
    assert len(c.sent) == 4


def test_allineamento_compra_solo_la_differenza(tmp_path):
    c = FakeClient(positions={"SPY": 13, "DBC": 200})
    bridge(tmp_path, c, allineamento_iniziale=True).sync(broker_with([], _pos()))
    assert sorted((r.symbol, r.qty) for r in c.sent) == [("DBC", 72), ("EFA", 95), ("VNQ", 103)]


def test_allineamento_con_errore_riprova_solo_i_mancanti(tmp_path):
    c = FakeClient(fail=RuntimeError("500"))
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    br.sync(broker_with([], _pos()))
    br.ledger.save()
    assert br.ledger.data["allineamento"]["stato"] == "in corso"
    c.fail = None
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    br.sync(broker_with([], _pos()))
    assert len(c.sent) == 4 and br.ledger.data["allineamento"]["stato"] == "completato"


def test_allineamento_spento_di_default(tmp_path):
    c = FakeClient()
    bridge(tmp_path, c, allineamento_iniziale=False).sync(broker_with([], _pos()))
    assert c.sent == []


def test_allineamento_rinviato_se_ordine_in_coda_sul_ticker(tmp_path):
    c = FakeClient()
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    br.sync(broker_with([Order("SPY", "SELL", 13, "2026-11-18", "rotazione: esce")], _pos()))
    assert "SPY" not in [r.symbol for r in c.sent if r.side == OrderSide.BUY]
    assert br.ledger.data["allineamento"]["stato"] == "in corso"


def _registro_allineamento_19_11(path):
    """Il registro reale del 19/11: 4 ordini di allineamento fermi a 'inviato' / PENDING_NEW."""
    ordini = {f"pb-main-allinea-{t}": {"stato": "inviato", "id": f"id-{t}", "stato_alpaca": "OrderStatus.PENDING_NEW",
                                        "ticker": t, "lato": "BUY", "qty": q, "creato": "2026-11-18",
                                        "motivo": "allineamento iniziale", "stop": 0.0}
              for t, q in (("DBC", 272), ("EFA", 95), ("SPY", 13), ("VNQ", 103))}
    path.write_text(json.dumps({"ordini": ordini, "riconciliazione": None,
                                "allineamento": {"stato": "completato"}}))


def test_stato_ordini_inviati_aggiornato_dopo_l_esecuzione(tmp_path):
    _registro_allineamento_19_11(tmp_path / "alpaca_orders.json")
    filled = {f"pb-main-allinea-{t}": SimpleNamespace(id=f"id-{t}", status="filled", filled_qty=str(q),
                                                      filled_avg_price="25.5")
              for t, q in (("DBC", 272), ("EFA", 95), ("SPY", 13))}
    filled["pb-main-allinea-VNQ"] = SimpleNamespace(id="id-VNQ", status="accepted")  # non ancora eseguito
    c = FakeClient(orders=filled, positions={"DBC": 272, "EFA": 95, "SPY": 13})
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    ev = br.sync(broker_with([], _pos()))
    br.ledger.save()
    reg = json.loads((tmp_path / "alpaca_orders.json").read_text())["ordini"]
    dbc = reg["pb-main-allinea-DBC"]
    assert dbc["stato"] == "eseguito" and dbc["stato_alpaca"] == "filled"
    assert dbc["qty_eseguita"] == 272 and dbc["prezzo_medio"] == 25.5
    assert reg["pb-main-allinea-VNQ"]["stato"] == "inviato" and reg["pb-main-allinea-VNQ"]["stato_alpaca"] == "accepted"
    assert any("ora filled" in e for e in ev)
    assert c.sent == []  # nessun reinvio, l'allineamento resta completato

    # esecuzione successiva: gli ordini finali non vengono più riletti, quello in attesa sì
    c.lookups.clear()
    c.orders["pb-main-allinea-VNQ"] = SimpleNamespace(id="id-VNQ", status="filled", filled_qty="103")
    br = bridge(tmp_path, c, allineamento_iniziale=True)
    br.sync(broker_with([], _pos()))
    assert c.lookups == ["pb-main-allinea-VNQ"]
    assert br.ledger.get("pb-main-allinea-VNQ")["stato"] == "eseguito"


def test_stato_aggiornato_anche_a_mercato_aperto_e_ordine_eseguito_mai_reinviato(tmp_path):
    o = Order("SPY", "BUY", 5, "2026-11-18", "t", stop=580)
    c = FakeClient()
    br = bridge(tmp_path, c)
    br.sync(broker_with([o]))
    br.ledger.save()
    cid = "pb-main-2026-11-18-SPY-BUY"
    assert br.ledger.get(cid)["stato_alpaca"] == "accepted"
    c.orders[cid] = SimpleNamespace(id="id1", status="canceled")
    c.open_ = True
    br = bridge(tmp_path, c)
    ev = br.sync(broker_with([o]))
    assert br.ledger.get(cid)["stato"] == "annullato" and any("mercato aperto" in e for e in ev)
    br.ledger.save()
    c.open_ = False
    bridge(tmp_path, c).sync(broker_with([o]))  # ordine chiuso: mai reinviato con lo stesso id
    assert len(c.sent) == 1


def test_stato_non_leggibile_lascia_il_record(tmp_path):
    _registro_allineamento_19_11(tmp_path / "alpaca_orders.json")
    br = bridge(tmp_path, FakeClient())
    ev = br.refresh()
    assert br.ledger.get("pb-main-allinea-SPY")["stato"] == "inviato"
    assert sum("non leggibile" in e for e in ev) == 4
