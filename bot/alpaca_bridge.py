"""Conto ombra su Alpaca (SOLO paper trading).

Il simulatore interno (PaperBroker) resta la fonte di verità: stato, dashboard e CSV
non dipendono mai da Alpaca. Questo modulo replica sul conto paper di Alpaca gli ordini
che il simulatore ha messo in coda per la prossima apertura:

- BUY  -> ordine a mercato con stop loss lato server: OTO (solo stop) oppure BRACKET
          (stop + take profit, solo se `alpaca.take_profit_r` è impostato in config).
          Alpaca accetta il BRACKET solo con entrambe le gambe, per questo senza take
          profit si usa l'OTO. Time in force GTC: con DAY la gamba di stop scadrebbe a
          fine seduta e le posizioni multi-giorno resterebbero senza protezione.
          Un BUY senza stop (la rotazione non usa stop) diventa un ordine a mercato
          semplice, salvo `alpaca.stop_emergenza_pct`.
- SELL -> annulla gli ordini aperti sul ticker (gambe di stop), vende a mercato e,
          se resta una parte della posizione con uno stop, rimette lo stop su quella parte.

Niente doppioni: ogni ordine ha un client_order_id deterministico, registrato in un
registro JSON separato (scrittura atomica); prima di inviare si chiede anche ad Alpaca
se quel client_order_id esiste già. Ogni errore viene registrato e loggato, mai rilanciato.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("alpaca")

ENV_KEYS = {"main": ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
            "tattica": ("ALPACA_TATTICA_API_KEY", "ALPACA_TATTICA_SECRET_KEY")}
DEFAULTS = {"enabled": False, "accounts": ["main"], "time_in_force": "gtc", "take_profit_r": None,
            "stop_emergenza_pct": None, "ledger_file": "state/alpaca_orders.json", "max_tentativi": 3,
            "allineamento_iniziale": False}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("alpaca") or {})}


def client_order_id(account: str, o) -> str:
    raw = f"pb-{account}-{o.created}-{o.ticker}-{o.side}{'-add' if getattr(o, 'add', False) else ''}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:128]


def make_client(account: str = "main"):
    """TradingClient paper, oppure None (con avviso, senza mostrare valori) se mancano le chiavi."""
    k_name, s_name = ENV_KEYS.get(account, ENV_KEYS["main"])
    key, secret = os.environ.get(k_name, "").strip(), os.environ.get(s_name, "").strip()
    if not key or not secret:
        log.warning("Alpaca: %s/%s non impostate: nessun ordine inviato ad Alpaca (solo simulazione interna).",
                    k_name, s_name)
        return None
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        log.error("Alpaca: libreria alpaca-py non installata (pip install alpaca-py): nessun ordine inviato.")
        return None
    return TradingClient(key, secret, paper=True)  # paper=True fisso: questo modulo non tocca mai conti reali


class Ledger:
    """Registro degli ordini inoltrati, separato dallo stato del simulatore."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = {"ordini": {}, "riconciliazione": None, "allineamento": None}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                # registro illeggibile: lo si mette da parte, i doppioni li blocca comunque il client_order_id
                log.error("Alpaca: registro %s illeggibile (%s): ne creo uno nuovo.", self.path, e)
                self.path.replace(self.path.with_suffix(".corrotto.json"))

    def get(self, cid):
        return self.data["ordini"].get(cid)

    def put(self, cid, **rec):
        rec["aggiornato_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.data["ordini"][cid] = {**self.data["ordini"].get(cid, {}), **rec}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.path)


def _err(e) -> str:
    return f"{type(e).__name__}: {str(e)[:300]}"


# Stati finali di Alpaca e stato corrispondente nel registro. Gli stati del registro che
# indicano un ordine già gestito (mai da reinviare) sono in GESTITI.
FINALI = {"filled": "eseguito", "canceled": "annullato", "expired": "annullato",
          "rejected": "annullato", "replaced": "annullato", "done_for_day": "annullato"}
GESTITI = ("inviato", "eseguito", "annullato")


def _status(s) -> str:
    """'OrderStatus.FILLED', OrderStatus.FILLED o 'filled' -> 'filled'."""
    s = getattr(s, "value", s)
    return str(s or "").split(".")[-1].lower()


class AlpacaBridge:
    def __init__(self, cfg: dict, ledger_path: Path, client, account: str = "main"):
        self.s = settings(cfg)
        self.ledger = Ledger(ledger_path)
        self.client = client
        self.account = account

    # ---------- costruzione delle richieste (separata dall'invio, così è testabile) ----------
    def _tif(self):
        from alpaca.trading.enums import TimeInForce
        return TimeInForce.DAY if str(self.s["time_in_force"]).lower() == "day" else TimeInForce.GTC

    def buy_request(self, o, ref_price: float | None, cid: str):
        from alpaca.trading.enums import OrderClass, OrderSide
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        stop = float(o.stop or 0)
        if stop <= 0 and self.s["stop_emergenza_pct"] and ref_price:
            stop = ref_price * (1 - float(self.s["stop_emergenza_pct"]))
        kw = dict(symbol=o.ticker, qty=int(o.qty), side=OrderSide.BUY, time_in_force=self._tif(),
                  client_order_id=cid)
        if stop > 0:
            kw["stop_loss"] = StopLossRequest(stop_price=round(stop, 2))
            tp_r = self.s["take_profit_r"]
            if tp_r and ref_price and ref_price > stop:
                kw["order_class"] = OrderClass.BRACKET
                kw["take_profit"] = TakeProfitRequest(limit_price=round(ref_price + tp_r * (ref_price - stop), 2))
            else:
                kw["order_class"] = OrderClass.OTO
        return MarketOrderRequest(**kw)

    def sell_request(self, ticker: str, qty: int, cid: str):
        from alpaca.trading.enums import OrderSide
        from alpaca.trading.requests import MarketOrderRequest
        return MarketOrderRequest(symbol=ticker, qty=int(qty), side=OrderSide.SELL,
                                  time_in_force=self._tif(), client_order_id=cid)

    def stop_request(self, ticker: str, qty: int, stop: float, cid: str):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        return StopOrderRequest(symbol=ticker, qty=int(qty), side=OrderSide.SELL, stop_price=round(stop, 2),
                                time_in_force=TimeInForce.GTC, client_order_id=cid)

    # ---------- accesso ad Alpaca, ogni chiamata protetta ----------
    def _exists(self, cid) -> bool:
        try:
            self.client.get_order_by_client_id(cid)
            return True
        except Exception:  # 404 = non esiste (o errore: lo dirà l'invio)
            return False

    def _alpaca_qty(self, ticker) -> int:
        try:
            return int(float(self.client.get_open_position(ticker).qty))
        except Exception:
            return 0

    def _cancel_open(self, ticker) -> int:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        n = 0
        for od in self.client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker],
                                                                 nested=False)):
            self.client.cancel_order_by_id(od.id)
            n += 1
        return n

    def _submit(self, cid, req, info: dict) -> str:
        rec = self.ledger.get(cid) or {}
        if rec.get("stato") in GESTITI:
            return f"Alpaca: {cid} già inviato, salto"
        if rec.get("tentativi", 0) >= self.s["max_tentativi"]:
            return f"Alpaca: {cid} abbandonato dopo {rec['tentativi']} tentativi falliti"
        if self._exists(cid):
            self.ledger.put(cid, stato="inviato", nota="già presente su Alpaca", **info)
            return f"Alpaca: {cid} già presente su Alpaca, registrato"
        try:
            od = self.client.submit_order(order_data=req)
            self.ledger.put(cid, stato="inviato", id=str(od.id), stato_alpaca=_status(getattr(od, "status", "")),
                            **info)
            return f"Alpaca: inviato {info['lato']} {info['ticker']} x{info['qty']} ({cid})"
        except Exception as e:
            self.ledger.put(cid, stato="errore", tentativi=rec.get("tentativi", 0) + 1, errore=_err(e), **info)
            log.error("Alpaca: invio %s fallito: %s", cid, _err(e))
            return f"Alpaca: ERRORE su {cid}"

    def refresh(self) -> list[str]:
        """Rilegge da Alpaca gli ordini inviati che non sono ancora in uno stato finale e aggiorna
        stato/stato_alpaca nel registro (prima restava per sempre lo stato del momento dell'invio).
        Solo lettura su Alpaca; un errore lascia il record com'è e si riprova alla prossima esecuzione."""
        out = []
        for cid, rec in sorted(self.ledger.data["ordini"].items()):
            if rec.get("stato") != "inviato" or _status(rec.get("stato_alpaca")) in FINALI:
                continue
            try:
                od = self.client.get_order_by_client_id(cid)
            except Exception as e:
                out.append(f"Alpaca: stato di {cid} non leggibile ({_err(e)})")
                continue
            st = _status(getattr(od, "status", ""))
            if not st or st == _status(rec.get("stato_alpaca")):
                continue
            upd = {"stato_alpaca": st}
            if st in FINALI:
                upd["stato"] = FINALI[st]
            for campo, attr in (("qty_eseguita", "filled_qty"), ("prezzo_medio", "filled_avg_price")):
                v = getattr(od, attr, None)
                if v not in (None, ""):
                    upd[campo] = float(v)
            self.ledger.put(cid, **upd)
            if st == "rejected":
                log.warning("Alpaca: ordine %s rifiutato da Alpaca", cid)
            out.append(f"Alpaca: {cid} ora {st}")
        return out

    # ---------- sincronizzazione ----------
    def sync(self, broker) -> list[str]:
        ev = self.refresh()  # anche a mercato aperto: è solo lettura
        try:
            if self.client.get_clock().is_open:
                ev.append("Alpaca: mercato aperto, ordini non inviati (eseguirebbero a prezzi diversi "
                          "dall'apertura simulata); riprovo alla prossima esecuzione")
                return ev
        except Exception as e:
            ev.append(f"Alpaca: impossibile leggere l'orario di mercato ({_err(e)}), nessun ordine inviato")
            return ev

        if self.s["allineamento_iniziale"]:
            ev.extend(self.align(broker))

        for o in sorted(broker.pending, key=lambda x: x.side != "SELL"):  # prima le vendite
            cid = client_order_id(self.account, o)
            info = dict(ticker=o.ticker, lato=o.side, qty=int(o.qty), creato=o.created, motivo=o.reason)
            try:
                if o.side == "BUY":
                    if int(o.qty) < 1:
                        continue
                    ref = broker.last_price.get(o.ticker)
                    ev.append(self._submit(cid, self.buy_request(o, ref, cid), dict(info, stop=round(o.stop, 2))))
                else:
                    ev.extend(self._sell(broker, o, cid, info))
            except Exception as e:  # errore nel preparare la richiesta: mai bloccare il resto
                self.ledger.put(cid, stato="errore", errore=_err(e), **info)
                log.error("Alpaca: %s non preparato: %s", cid, _err(e))
                ev.append(f"Alpaca: ERRORE su {cid}")
        ev.append(self.reconcile(broker))
        return ev

    def _sell(self, broker, o, cid, info) -> list[str]:
        rec = self.ledger.get(cid) or {}
        if rec.get("stato") in GESTITI + ("saltato",):
            return [f"Alpaca: {cid} già gestito, salto"]
        if self._exists(cid):
            self.ledger.put(cid, stato="inviato", nota="già presente su Alpaca", **info)
            return [f"Alpaca: {cid} già presente su Alpaca, registrato"]
        held = self._alpaca_qty(o.ticker)
        if held <= 0:
            self.ledger.put(cid, stato="saltato", nota="nessuna posizione su Alpaca", **info)
            return [f"Alpaca: nessuna posizione {o.ticker} su Alpaca, vendita non necessaria"]
        try:
            n = self._cancel_open(o.ticker)  # libera le azioni bloccate dalle gambe di stop
        except Exception as e:
            self.ledger.put(cid, stato="errore", tentativi=rec.get("tentativi", 0) + 1, errore=_err(e), **info)
            return [f"Alpaca: ERRORE annullando gli ordini aperti su {o.ticker}: {_err(e)}"]
        qty = min(int(o.qty), held)
        out = [f"Alpaca: annullati {n} ordini aperti su {o.ticker}"] if n else []
        out.append(self._submit(cid, self.sell_request(o.ticker, qty, cid), dict(info, qty=qty)))
        rest = held - qty
        pos = broker.positions.get(o.ticker)
        if rest > 0 and pos is not None and pos.stop > 0:  # vendita parziale: lo stop resta sul resto
            scid = (cid + "-stop")[:128]
            out.append(self._submit(scid, self.stop_request(o.ticker, rest, pos.stop, scid),
                                    dict(info, lato="STOP", qty=rest, stop=round(pos.stop, 2))))
        return out

    def align(self, broker) -> list[str]:
        """Allineamento UNA TANTUM: compra su Alpaca le posizioni che il simulatore aveva già prima del
        collegamento. Doppia protezione contro le ripetizioni: segno "completato" nel registro (dopo il
        quale non fa più nulla, anche se l'opzione resta accesa) e client_order_id fisso per ticker,
        che Alpaca rifiuterebbe se arrivasse una seconda volta."""
        stato = self.ledger.data.get("allineamento") or {}
        if stato.get("stato") == "completato":
            return []
        busy = {o.ticker for o in broker.pending}
        out, tutti_ok = [], True
        for t, p in sorted(broker.positions.items()):
            cid = f"pb-{self.account}-allinea-{t}"
            if t in busy:  # un ordine in coda sullo stesso ticker: si allinea alla prossima esecuzione
                out.append(f"Alpaca: allineamento {t} rinviato (ordine in coda sullo stesso ticker)")
                tutti_ok = False
                continue
            rec = self.ledger.get(cid) or {}
            if rec.get("stato") in GESTITI:
                continue
            delta = int(p.qty) - self._alpaca_qty(t)
            if delta <= 0:
                self.ledger.put(cid, stato="saltato", nota="già allineato su Alpaca", ticker=t, lato="BUY", qty=0)
                continue
            o = SimpleOrder(t, "BUY", delta, broker.last_processed, "allineamento iniziale", p.stop)
            info = dict(ticker=t, lato="BUY", qty=delta, creato=broker.last_processed, motivo="allineamento iniziale",
                        stop=round(p.stop, 2))
            out.append(self._submit(cid, self.buy_request(o, broker.last_price.get(t), cid), info))
            if (self.ledger.get(cid) or {}).get("stato") not in GESTITI:
                tutti_ok = False
        if tutti_ok:
            self.ledger.data["allineamento"] = {
                "stato": "completato", "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "posizioni": {t: int(p.qty) for t, p in broker.positions.items()}}
            out.append("Alpaca: allineamento iniziale completato (non verrà più ripetuto)")
        else:
            self.ledger.data["allineamento"] = {"stato": "in corso"}
        return out

    def reconcile(self, broker) -> str:
        """Confronta le posizioni del simulatore con quelle di Alpaca (solo log, nessuna correzione)."""
        try:
            alp = {p.symbol: int(float(p.qty)) for p in self.client.get_all_positions()}
        except Exception as e:
            return f"Alpaca: riconciliazione non riuscita ({_err(e)})"
        sim = {t: int(p.qty) for t, p in broker.positions.items()}
        diff = {t: {"simulatore": sim.get(t, 0), "alpaca": alp.get(t, 0)}
                for t in sorted(set(sim) | set(alp)) if sim.get(t, 0) != alp.get(t, 0)}
        self.ledger.data["riconciliazione"] = {
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "ultimo_giorno_simulato": broker.last_processed, "differenze": diff}
        if diff:
            log.warning("Alpaca: posizioni diverse dal simulatore: %s", diff)
            return f"Alpaca: ATTENZIONE posizioni diverse dal simulatore {diff}"
        return "Alpaca: posizioni allineate al simulatore"


class SimpleOrder:
    """Ordine minimo con gli stessi campi di bot.broker.Order usati qui."""

    def __init__(self, ticker, side, qty, created, reason, stop=0.0):
        self.ticker, self.side, self.qty, self.created, self.reason, self.stop = ticker, side, qty, created, reason, stop
        self.add = False


def sync_account(cfg: dict, broker, account: str, base_dir_resolver) -> list[str]:
    """Punto d'ingresso usato da run_paper.py. Non solleva mai eccezioni."""
    s = settings(cfg)
    if not s["enabled"] or account not in s["accounts"]:
        return []
    try:
        client = make_client(account)
        if client is None:
            return ["Alpaca: non attivo (chiavi mancanti)"]
        ledger_path = base_dir_resolver(cfg, s["ledger_file"] if account == "main"
                                        else s["ledger_file"].replace(".json", f"_{account}.json"))
        bridge = AlpacaBridge(cfg, ledger_path, client, account)
        try:
            return bridge.sync(broker)
        finally:
            try:
                bridge.ledger.save()
            except OSError as e:
                log.error("Alpaca: registro non salvato: %s", e)
    except Exception as e:
        log.error("Alpaca: sincronizzazione interrotta: %s", _err(e))
        return [f"Alpaca: ERRORE {_err(e)}"]
