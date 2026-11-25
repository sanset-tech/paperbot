"""Laboratorio intraday: il metodo manuale (trend + medie esponenziali + candele d'inversione) messo alla prova.

Solo backtest. Non tocca lo stato del bot, non manda ordini.

Regole (versione 1, tradotta dal metodo descritto dall'utente):
  trend     EMA veloce sopra/sotto EMA lenta, EMA lenta in salita/discesa
            e retta di trend sugli ultimi 3 minimi (rialzo) o massimi (ribasso) con la stessa pendenza
  ingresso  ritracciamento sulla EMA veloce o sulla retta di trend
            + candela d'inversione (engulfing o martello / stella cadente)
            + RSI 14 NON in ipercomprato/ipervenduto (30-70)
  uscita    stop oltre il minimo/massimo delle ultime 2 candele, obiettivo 2 volte il rischio,
            altrimenti chiusura a fine seduta: niente posizioni la notte, quindi niente gap d'apertura
  orari     niente ingressi nella prima mezz'ora e dopo le 15:00 (New York)
  esecuzione segnale sulla chiusura della candela, ingresso all'apertura della successiva;
            se nella stessa candela si toccano stop e obiettivo si conta lo stop (ipotesi prudente)

Uso:  python intraday_lab.py --out risultati_intraday
      python intraday_lab.py --synthetic        # dati finti, solo per provare che gira
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

UNIVERSO = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
TZ = "America/New_York"


# ---------------------------------------------------------------- dati
def scarica_alpaca(tickers, start, end, cache: Path) -> dict[str, pd.DataFrame]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    cli = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for t in tickers:
        f = cache / f"{t}_30m.csv"
        if f.exists():
            out[t] = pd.read_csv(f, index_col=0, parse_dates=True)
            continue
        df = None
        for feed in (DataFeed.SIP, DataFeed.IEX):
            try:
                req = StockBarsRequest(symbol_or_symbols=t, timeframe=TimeFrame(30, TimeFrameUnit.Minute),
                                       start=pd.Timestamp(start, tz="UTC"), end=pd.Timestamp(end, tz="UTC"),
                                       adjustment=Adjustment.ALL, feed=feed)
                df = cli.get_stock_bars(req).df
                if df is not None and not df.empty:
                    print(f"{t}: {len(df)} candele ({feed.value})")
                    break
            except Exception as e:  # piano gratuito: se SIP non è permesso si passa a IEX
                print(f"{t}: feed {feed.value} non disponibile ({e})")
        if df is None or df.empty:
            continue
        df = df.reset_index(level=0, drop=True) if isinstance(df.index, pd.MultiIndex) else df
        df = df.rename(columns=str.capitalize)[["Open", "High", "Low", "Close", "Volume"]]
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(TZ).tz_localize(None)
        df.to_csv(f)
        out[t] = df
    return out


def sintetici(tickers, seed=1) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2019-01-01", "2024-12-31")
    out = {}
    for t in tickers:
        rows, px = [], 100.0
        drift = 0.0
        for d in days:
            if rng.random() < 0.05:
                drift = rng.normal(0, 0.0008)
            px *= np.exp(rng.normal(0, 0.006))  # gap notturno
            for k in range(13):
                ts = d + pd.Timedelta(hours=9, minutes=30 + 30 * k)
                r = drift + rng.normal(0, 0.003)
                o, c = px, px * np.exp(r)
                h, lo = max(o, c) * (1 + abs(rng.normal(0, 0.0015))), min(o, c) * (1 - abs(rng.normal(0, 0.0015)))
                rows.append((ts, o, h, lo, c, 1e5))
                px = c
        out[t] = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"]).set_index("ts")
    return out


def sessione(df: pd.DataFrame, minuti: int) -> pd.DataFrame:
    """Solo seduta regolare 9:30-16:00 New York; 30 min oppure aggregato a 60 min (ancorato alle 9:30)."""
    df = df.sort_index()
    t = df.index.hour * 60 + df.index.minute
    df = df[(t >= 570) & (t < 960)].copy()
    if minuti == 30:
        return df
    t = df.index.hour * 60 + df.index.minute
    key = df.index.normalize() + pd.to_timedelta(570 + ((t - 570) // minuti) * minuti, unit="min")
    g = df.groupby(key)
    return pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                         "Close": g["Close"].last(), "Volume": g["Volume"].sum()})


# ---------------------------------------------------------------- indicatori
def rsi(c: pd.Series, n=14) -> pd.Series:
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def retta_pivot(serie: pd.Series, k: int, tipo: str, n_pivot=3) -> tuple[np.ndarray, np.ndarray]:
    """Retta di trend sugli ultimi n_pivot minimi (o massimi) confermati.
    Un pivot alla candela i è noto solo alla candela i+k: niente sguardo al futuro.
    Ritorna (valore della retta proiettata sulla candela corrente, pendenza relativa per candela)."""
    v = serie.to_numpy()
    n = len(v)
    win = pd.Series(v).rolling(2 * k + 1, center=True)
    ext = win.min() if tipo == "min" else win.max()
    is_piv = (pd.Series(v) == ext).to_numpy()
    livello = np.full(n, np.nan)
    pend = np.full(n, np.nan)
    piv_x, piv_y = [], []
    for i in range(n):
        j = i - k  # pivot che diventa noto adesso
        if j >= k and is_piv[j]:
            piv_x.append(j)
            piv_y.append(v[j])
            piv_x, piv_y = piv_x[-n_pivot:], piv_y[-n_pivot:]
        if len(piv_x) == n_pivot:
            a, b = np.polyfit(piv_x, piv_y, 1)
            livello[i] = a * i + b
            pend[i] = a / v[i]
    return livello, pend


def prepara(df: pd.DataFrame, ema_f: int, ema_s: int) -> pd.DataFrame:
    o = df.copy()
    o["ef"] = o["Close"].ewm(span=ema_f, adjust=False).mean()
    o["es"] = o["Close"].ewm(span=ema_s, adjust=False).mean()
    o["es_slope"] = o["es"] - o["es"].shift(5)
    o["rsi"] = rsi(o["Close"])
    prev = o["Close"].shift(1)
    tr = pd.concat([o["High"] - o["Low"], (o["High"] - prev).abs(), (o["Low"] - prev).abs()], axis=1).max(axis=1)
    o["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    o["sup"], o["sup_slope"] = retta_pivot(o["Low"], 2, "min")
    o["res"], o["res_slope"] = retta_pivot(o["High"], 2, "max")
    body = (o["Close"] - o["Open"]).abs()
    rng = (o["High"] - o["Low"]).replace(0, np.nan)
    lw = o[["Open", "Close"]].min(axis=1) - o["Low"]
    uw = o["High"] - o[["Open", "Close"]].max(axis=1)
    po, pc = o["Open"].shift(1), o["Close"].shift(1)
    o["bull"] = ((o["Close"] > o["Open"]) & (pc < po) & (o["Close"] >= po) & (o["Open"] <= pc)) | \
                ((lw >= 2 * body) & (uw <= body) & ((o["Close"] - o["Low"]) / rng >= 0.66))
    o["bear"] = ((o["Close"] < o["Open"]) & (pc > po) & (o["Close"] <= po) & (o["Open"] >= pc)) | \
                ((uw >= 2 * body) & (lw <= body) & ((o["High"] - o["Close"]) / rng >= 0.66))
    return o


# ---------------------------------------------------------------- segnali e operazioni
def operazioni(t: str, o: pd.DataFrame, minuti: int, target_r=2.0) -> list[dict]:
    idx = o.index
    day = idx.normalize().to_numpy()
    mins = (idx.hour * 60 + idx.minute).to_numpy()
    O, H, L, C = (o[c].to_numpy() for c in ("Open", "High", "Low", "Close"))
    ef, es, sl, r, atr = (o[c].to_numpy() for c in ("ef", "es", "es_slope", "rsi", "atr"))
    sup, sups, res, ress = (o[c].to_numpy() for c in ("sup", "sup_slope", "res", "res_slope"))
    bull, bear = o["bull"].to_numpy(), o["bear"].to_numpy()
    out, i, n = [], 60, len(o)
    while i < n - 1:
        ok_ora = mins[i] >= 600 and mins[i] + minuti <= 900 and day[i + 1] == day[i]
        lato = 0
        if ok_ora and np.isfinite(atr[i]) and atr[i] > 0 and 30 < r[i] < 70:
            if (ef[i] > es[i] and sl[i] > 0 and sups[i] > 0 and bull[i]
                    and (L[i] <= ef[i] * 1.001 or L[i] <= sup[i] * 1.002)):
                lato = 1
            elif (ef[i] < es[i] and sl[i] < 0 and ress[i] < 0 and bear[i]
                    and (H[i] >= ef[i] * 0.999 or H[i] >= res[i] * 0.998)):
                lato = -1
        if not lato:
            i += 1
            continue
        e = O[i + 1]
        stop = (min(L[i], L[i - 1]) - 0.1 * atr[i]) if lato == 1 else (max(H[i], H[i - 1]) + 0.1 * atr[i])
        rischio = (e - stop) * lato
        if rischio < 0.25 * atr[i]:
            stop = e - lato * 0.25 * atr[i]
            rischio = 0.25 * atr[i]
        obiettivo = e + lato * target_r * rischio
        j, uscita, motivo = i + 1, None, None
        while True:
            hit_stop = L[j] <= stop if lato == 1 else H[j] >= stop
            hit_tgt = H[j] >= obiettivo if lato == 1 else L[j] <= obiettivo
            if hit_stop:
                uscita, motivo = stop, "stop"
            elif hit_tgt:
                uscita, motivo = obiettivo, "obiettivo"
            elif j + 1 >= n or day[j + 1] != day[j]:
                uscita, motivo = C[j], "fine seduta"
            if uscita is not None:
                break
            j += 1
        out.append({"ticker": t, "lato": "long" if lato == 1 else "short", "entrata_ts": idx[i + 1],
                    "uscita_ts": idx[j], "entrata": e, "uscita": uscita, "stop": stop, "rischio": rischio,
                    "motivo": motivo})
        i = j + 1  # una posizione per ticker alla volta
    return out


def simula(trades: pd.DataFrame, capitale=50000.0, rischio_pct=0.005, max_pos_pct=0.25,
           max_aperte=5, costo_bps=3.0) -> tuple[pd.DataFrame, pd.Series]:
    trades = trades.sort_values("entrata_ts").reset_index(drop=True)
    eq, aperte, righe = capitale, [], []
    curva = {}
    for _, tr in trades.iterrows():
        chiuse = [a for a in aperte if a["uscita_ts"] <= tr["entrata_ts"]]
        for a in chiuse:
            eq += a["pnl"]
            curva[a["uscita_ts"]] = eq
        aperte = [a for a in aperte if a["uscita_ts"] > tr["entrata_ts"]]
        if len(aperte) >= max_aperte:
            continue
        q = min(eq * rischio_pct / tr["rischio"], eq * max_pos_pct / tr["entrata"])
        q = np.floor(q)
        if q <= 0:
            continue
        lato = 1 if tr["lato"] == "long" else -1
        lordo = (tr["uscita"] - tr["entrata"]) * lato * q
        costi = (tr["entrata"] + tr["uscita"]) * q * costo_bps / 1e4
        pnl = lordo - costi
        d = dict(tr)
        d.update(qty=q, pnl=pnl, R=pnl / (tr["rischio"] * q))
        aperte.append(d)
        righe.append(d)
    for a in sorted(aperte, key=lambda a: a["uscita_ts"]):
        eq += a["pnl"]
        curva[a["uscita_ts"]] = eq
    return pd.DataFrame(righe), pd.Series(curva).sort_index()


def metriche(fatti: pd.DataFrame, curva: pd.Series, capitale=50000.0) -> dict:
    if fatti.empty:
        return {"operazioni": 0}
    gd = curva.groupby(curva.index.normalize()).last()
    gd = pd.concat([pd.Series({gd.index[0] - pd.Timedelta(days=1): capitale}), gd])
    anni = max((gd.index[-1] - gd.index[0]).days / 365.25, 0.1)
    ret = gd.pct_change().dropna()
    bdays = pd.bdate_range(gd.index[0], gd.index[-1])
    ret_full = ret.reindex(bdays, fill_value=0.0)
    vinc, perd = fatti.loc[fatti.pnl > 0, "pnl"], fatti.loc[fatti.pnl <= 0, "pnl"]
    return {
        "operazioni": int(len(fatti)),
        "operazioni_al_mese": round(len(fatti) / (anni * 12), 1),
        "win_rate_%": round(100 * len(vinc) / len(fatti), 1),
        "R_medio": round(float(fatti.R.mean()), 3),
        "profit_factor": round(float(vinc.sum() / -perd.sum()), 2) if len(perd) and perd.sum() < 0 else None,
        "rendimento_totale_%": round(100 * (gd.iloc[-1] / capitale - 1), 1),
        "CAGR_%": round(100 * ((gd.iloc[-1] / capitale) ** (1 / anni) - 1), 2),
        "max_drawdown_%": round(100 * float((gd / gd.cummax() - 1).min()), 1),
        "sharpe": round(float(ret_full.mean() / ret_full.std() * np.sqrt(252)), 2) if ret_full.std() > 0 else None,
        "long_R_medio": round(float(fatti.loc[fatti.lato == "long", "R"].mean()), 3) if (fatti.lato == "long").any() else None,
        "short_R_medio": round(float(fatti.loc[fatti.lato == "short", "R"].mean()), 3) if (fatti.lato == "short").any() else None,
        "uscite": fatti.motivo.value_counts().to_dict(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="risultati_intraday")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=str((pd.Timestamp.now("UTC") - pd.Timedelta(days=1)).date()))
    ap.add_argument("--split", default="2023-01-01", help="da qui in poi: verifica fuori campione")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = sintetici(UNIVERSO[:3]) if a.synthetic else scarica_alpaca(UNIVERSO, a.start, a.end, Path("data_intraday"))
    split = pd.Timestamp(a.split)
    report = {"regole": __doc__.split("Uso:")[0].strip(), "dati": {t: [str(d.index[0]), str(d.index[-1]), len(d)] for t, d in raw.items()},
              "varianti": {}}
    for minuti in (30, 60):
        barre = {t: sessione(d, minuti) for t, d in raw.items()}
        for ef, es in ((9, 21), (20, 50)):
            nome = f"{minuti}min_EMA{ef}-{es}"
            tr = [x for t, d in barre.items() for x in operazioni(t, prepara(d, ef, es), minuti)]
            trades = pd.DataFrame(tr)
            v = {}
            for bps in (0.0, 3.0, 6.0):
                fatti, curva = simula(trades, costo_bps=bps) if len(trades) else (pd.DataFrame(), pd.Series(dtype=float))
                chiave = f"costi_{bps:g}bps"
                v[chiave] = {"intero_periodo": metriche(fatti, curva)}
                if bps == 3.0 and len(fatti):
                    dentro = fatti[fatti.entrata_ts < split]
                    fuori = fatti[fatti.entrata_ts >= split]
                    v[chiave]["fino_al_2022"] = metriche(*simula(trades[trades.entrata_ts < split], costo_bps=bps))
                    v[chiave]["dal_2023_fuori_campione"] = metriche(*simula(trades[trades.entrata_ts >= split], costo_bps=bps))
                    ann = curva.groupby(curva.index.year).last()
                    base = pd.concat([pd.Series({ann.index[0] - 1: 50000.0}), ann])
                    v[chiave]["rendimenti_annui_%"] = {int(k): round(100 * x, 1) for k, x in base.pct_change().dropna().items()}
                    v[chiave]["per_ticker_R_medio"] = fatti.groupby("ticker").R.mean().round(3).to_dict()
                    fatti.to_csv(out / f"trades_{nome}.csv", index=False)
            report["varianti"][nome] = v
            print(nome, json.dumps(v["costi_3bps"]["intero_periodo"]))
    json.dump(report, open(out / "report.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
