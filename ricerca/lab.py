"""Ricerca strategie alternative (solo backtest, separato dal bot).

Non modifica nulla del bot: legge solo i dati giornalieri pubblici (Yahoo) con le stesse funzioni di pulizia
del bot (bot.data.clean) e il risultato del backtest del bot (prodotto da run_backtest.py, invariato).
Nessuna chiave, nessun ordine.

Convenzioni (niente sguardo al futuro):
  - i pesi decisi sulla chiusura del giorno t vengono eseguiti all'APERTURA di t+1;
  - giorno t+1: rendimento apertura->chiusura con i pesi nuovi; il gap notturno (chiusura t -> apertura t+1)
    appartiene ancora ai pesi vecchi;
  - costi su ogni variazione di peso (commissione + slippage, come config.json del bot: 0,10% + 0,05% per lato);
  - la liquidità non investita matura il tasso dei T-bill (^IRX), come nel bot.
Semplificazione dichiarata: pesi costanti tra un ribilanciamento e l'altro (ribilanciamento implicito gratuito
fra un'operazione e l'altra). Pesa poco sulle strategie a bassa rotazione; è segnalata nel report.

Periodi:  sviluppo 2006-2015 | validazione 2016-2020 | test finale 2021-oggi.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.data import clean, synthetic  # noqa: E402  (sola lettura del codice del bot)

UNIVERSO = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
AZIONARI = ["SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ"]
COSTO = 0.001 + 0.0005
PERIODI = {"sviluppo_2006_2015": ("2006-01-01", "2015-12-31"),
           "validazione_2016_2020": ("2016-01-01", "2020-12-31"),
           "test_finale_2021_oggi": ("2021-01-01", "2100-01-01")}


# ============================================================ dati
def carica(sintetico: bool):
    if sintetico:
        d = synthetic(UNIVERSO, start="2005-01-01", end="2026-11-20")
        rf = pd.Series(0.02, index=next(iter(d.values())).index)
    else:
        import yfinance as yf
        d = {}
        for t in UNIVERSO:
            raw = yf.Ticker(t).history(start="2005-01-01", auto_adjust=True, actions=False)
            d[t] = clean(raw, t)
        irx = yf.Ticker("^IRX").history(start="2005-01-01")["Close"] / 100
        irx.index = pd.to_datetime(irx.index).tz_localize(None).normalize()
        rf = irx[irx.between(0, 0.2)]
    idx = sorted(set().union(*[df.index for df in d.values()]))
    f = lambda c: pd.DataFrame({t: df[c] for t, df in d.items()}).reindex(idx).ffill()
    P = {c: f(c) for c in ("Open", "High", "Low", "Close", "Volume")}
    rf = rf.reindex(P["Close"].index).ffill().fillna(0.015)
    return P, rf


# ============================================================ motore comune
def esegui(W: pd.DataFrame, P, rf, scala: pd.Series | None = None) -> dict:
    """W: pesi target decisi alla chiusura di t. Ritorna componenti giornaliere."""
    W = W.reindex(P["Close"].index).fillna(0.0)
    if scala is not None:
        W = W.mul(scala.reindex(W.index).fillna(0.0), axis=0)
    O, C = P["Open"][W.columns], P["Close"][W.columns]
    w1, w2 = W.shift(1).fillna(0.0), W.shift(2).fillna(0.0)
    intraday = (C / O - 1).fillna(0.0)
    gap = (O / C.shift(1) - 1).fillna(0.0)
    contrib = w1 * intraday + w2 * gap
    turnover = (w1 - w2).abs()
    costi = turnover * COSTO
    gross = w1.abs().sum(axis=1)
    cash = (1 - gross).clip(lower=0) * rf / 252
    ret = contrib.sum(axis=1) - costi.sum(axis=1) + cash
    return {"ret": ret, "contrib": contrib - costi, "w": w1, "costi": costi.sum(axis=1), "gross": gross}


def operazioni(res) -> pd.DataFrame:
    """Un'operazione = tratto continuo con peso dello stesso segno su un ticker."""
    righe = []
    w, c = res["w"], res["contrib"]
    for t in w.columns:
        s = np.sign(w[t].to_numpy())
        v = c[t].to_numpy()
        i, n = 0, len(s)
        while i < n:
            if s[i] == 0:
                i += 1
                continue
            j = i
            while j + 1 < n and s[j + 1] == s[i]:
                j += 1
            pnl = v[i:j + 2].sum() if j + 1 < n else v[i:j + 1].sum()  # include il costo d'uscita
            righe.append((t, w.index[i], w.index[j], j - i + 1, pnl))
            i = j + 1
    return pd.DataFrame(righe, columns=["ticker", "inizio", "fine", "giorni", "pnl_pct_capitale"])


def metriche(res, da=None, a=None) -> dict:
    r = res["ret"].loc[da:a]
    r = r[r.index >= r.ne(0).idxmax()] if r.ne(0).any() else r
    if len(r) < 60:
        return {}
    eq = (1 + r).cumprod()
    anni = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / anni) - 1
    dd = (eq / eq.cummax() - 1).min()
    down = r[r < 0].std() * np.sqrt(252)
    tr = operazioni({k: (v.loc[r.index] if hasattr(v, "loc") else v) for k, v in res.items()})
    vinc, perd = tr.pnl_pct_capitale[tr.pnl_pct_capitale > 0], tr.pnl_pct_capitale[tr.pnl_pct_capitale <= 0]
    g = res["gross"].loc[r.index]
    return {
        "rendimento_netto_%": round(100 * (eq.iloc[-1] - 1), 1),
        "CAGR_%": round(100 * cagr, 2),
        "volatilita_%": round(100 * r.std() * np.sqrt(252), 1),
        "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2) if r.std() > 0 else 0,
        "sortino": round(r.mean() * 252 / down, 2) if down > 0 else 0,
        "max_drawdown_%": round(100 * dd, 1),
        "calmar": round(cagr / abs(dd), 2) if dd < 0 else 0,
        "operazioni": int(len(tr)),
        "win_rate_%": round(100 * len(vinc) / len(tr), 1) if len(tr) else 0,
        "profit_factor": round(vinc.sum() / -perd.sum(), 2) if len(perd) and perd.sum() < 0 else None,
        "EV_per_operazione_%capitale": round(100 * tr.pnl_pct_capitale.mean(), 3) if len(tr) else 0,
        "esposizione_media_%": round(100 * g.mean(), 1),
        "esposizione_max_%": round(100 * g.max(), 1),
        "costi_totali_%capitale": round(100 * res["costi"].loc[r.index].sum(), 2),
        "giorni_medi_in_posizione": round(float(tr.giorni.mean()), 1) if len(tr) else 0,
    }


def metriche_serie(r: pd.Series, da=None, a=None) -> dict:
    r = r.loc[da:a].dropna()
    if len(r) < 60:
        return {}
    eq = (1 + r).cumprod()
    anni = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / anni) - 1
    dd = (eq / eq.cummax() - 1).min()
    down = r[r < 0].std() * np.sqrt(252)
    return {"rendimento_netto_%": round(100 * (eq.iloc[-1] - 1), 1), "CAGR_%": round(100 * cagr, 2),
            "volatilita_%": round(100 * r.std() * np.sqrt(252), 1),
            "sharpe": round(r.mean() / r.std() * np.sqrt(252), 2), "sortino": round(r.mean() * 252 / down, 2),
            "max_drawdown_%": round(100 * dd, 1), "calmar": round(cagr / abs(dd), 2) if dd < 0 else 0}


# ============================================================ indicatori
def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(P, n=20):
    C = P["Close"]
    pc = C.shift(1)
    tr = np.maximum(P["High"] - P["Low"], np.maximum((P["High"] - pc).abs(), (P["Low"] - pc).abs()))
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def vol(C, n):
    return C.pct_change().rolling(n).std() * np.sqrt(252)


def primo_del_mese(idx):
    s = pd.Series(idx, index=idx)
    return s.groupby([idx.year, idx.month]).transform("min") == s  # primo giorno di borsa del mese (come il bot)


# ============================================================ strategie
def s0_rotazione(P, top_n=5):
    """Replica della strategia del bot (per controllo e come base dell'overlay di volatilità)."""
    C = P["Close"]
    sma = C.rolling(200).mean()
    score = sum(C / C.shift(n) - 1 for n in (21, 63, 126, 252)) / 4
    v = vol(C, 63)
    W = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for d in C.index[primo_del_mese(C.index)]:
        ok = (C.loc[d] > sma.loc[d]) & (score.loc[d] > 0) & (v.loc[d] > 0)
        cand = score.loc[d][ok].sort_values(ascending=False).head(top_n)
        row = pd.Series(0.0, index=C.columns)
        if len(cand):
            inv = 1 / v.loc[d, cand.index]
            row[cand.index] = np.minimum((len(cand) / top_n) * inv / inv.sum(), 0.2)
        W.loc[d] = row
    return W.ffill().fillna(0.0)


def a_trend(P, lb=252):
    """A. Trend following assoluto (time-series momentum) con conferma su due orizzonti.
    Ipotesi: un asset in trend su 12 mesi E su 3 mesi tende a proseguire (effetto momentum, documentato
    su molte classi di attivo). Ogni ETF è valutato da solo (non in classifica come la rotazione).
    Peso = obiettivo 20% di volatilità per asset / N (massimo 1/N), ribilanciamento settimanale.
    Indicatori scartati: MACD e ADX (ridondanti con i due momentum, aggiungono solo parametri)."""
    C = P["Close"]
    sig = ((C / C.shift(lb) - 1 > 0) & (C / C.shift(63) - 1 > 0) & (C > C.rolling(200).mean())).astype(float)
    w = (0.20 / vol(C, 63)).clip(upper=1.0) / len(C.columns)
    W = (sig * w).fillna(0.0)
    idx = C.index.to_series()
    sett = idx.dt.isocalendar().week.astype(int) + 100 * idx.dt.isocalendar().year.astype(int)
    venerdi = sett != sett.shift(-1)  # ultimo giorno di borsa della settimana
    W[~venerdi.to_numpy()] = np.nan
    return W.ffill().fillna(0.0)


def b_mean_reversion(P, soglia=10, max_giorni=10):
    """B. Mean reversion di breve sugli ETF azionari, solo in trend rialzista.
    Ipotesi: forti ipervenduti a 2 giorni (RSI2) dentro un trend rialzista rientrano in pochi giorni.
    Uscita: chiusura sopra la media a 5 giorni o dopo max_giorni (stop temporale: uno stop di prezzo
    stretto su questa logica taglia proprio i rimbalzi). Peso fisso 1/6 per posizione."""
    C = P["Close"][AZIONARI]
    sma200, sma5, r2 = C.rolling(200).mean(), C.rolling(5).mean(), rsi(C, 2)
    W = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for t in C.columns:
        dentro, giorni = False, 0
        c, s2, s5, rr = C[t].to_numpy(), sma200[t].to_numpy(), sma5[t].to_numpy(), r2[t].to_numpy()
        col = np.zeros(len(c))
        for i in range(len(c)):
            if not dentro:
                if np.isfinite(s2[i]) and c[i] > s2[i] and rr[i] < soglia:
                    dentro, giorni = True, 0
            else:
                giorni += 1
                if c[i] > s5[i] or giorni >= max_giorni:
                    dentro = False
            col[i] = 1 / len(AZIONARI) if dentro else 0.0
        W[t] = col
    return W


def c_breakout(P, n=55, conferma_volume=True):
    """C. Breakout Donchian con conferma dei volumi e stop ATR.
    Ipotesi: la rottura del massimo a n giorni con volume sopra la media segnala l'inizio di un trend
    (i falsi breakout tendono ad avere volumi deboli). Uscita: minimo a n/2 giorni o trailing stop 3 ATR.
    Dimensionamento: rischio 0,5% del capitale per operazione sulla distanza dello stop, massimo 20%."""
    C, H, L, V = P["Close"], P["High"], P["Low"], P["Volume"]
    hh = H.rolling(n).max().shift(1)
    ll = L.rolling(max(n // 2, 10)).min().shift(1)
    a = atr(P)
    vm = V.rolling(20).mean()
    W = pd.DataFrame(0.0, index=C.index, columns=C.columns)
    for t in C.columns:
        c, h_, l_, at, v, m = (x[t].to_numpy() for x in (C, hh, ll, a, V, vm))
        col, stop, w = np.zeros(len(c)), np.nan, 0.0
        for i in range(len(c)):
            if w == 0:
                vol_ok = (not conferma_volume) or (m[i] > 0 and v[i] > m[i])
                if np.isfinite(h_[i]) and c[i] > h_[i] and vol_ok and at[i] > 0:
                    stop = c[i] - 3 * at[i]
                    w = min(0.005 / (3 * at[i] / c[i]), 0.2)
            else:
                stop = max(stop, c[i] - 3 * at[i])
                if c[i] < stop or c[i] < l_[i]:
                    w = 0.0
            col[i] = w
        W[t] = col
    return W


def d_volatilita(P, rf, vol_target=0.10):
    """D. Volatility adaptive applicato alla rotazione del bot.
    Ipotesi: la volatilità è persistente; ridurre l'esposizione quando la volatilità del portafoglio sale
    taglia i drawdown più di quanto tagli il rendimento. Scala = vol_target / volatilità realizzata (20 g),
    massimo 1 (niente leva); dimezzata se la volatilità di SPY è nel 20% più alto degli ultimi 3 anni.
    Più un freno sul drawdown: -10% => esposizione al 50%, -15% => a zero per un mese."""
    W = s0_rotazione(P)
    base = esegui(W, P, rf)
    rv = base["contrib"].sum(axis=1).rolling(20).std() * np.sqrt(252)
    scala = (vol_target / rv).clip(upper=1.0).fillna(1.0)
    spy_v = vol(P["Close"]["SPY"], 20)
    estremo = spy_v > spy_v.rolling(756, min_periods=252).quantile(0.8)
    scala = scala.where(~estremo, scala * 0.5)
    scala = freno_drawdown(W, P, rf, scala)
    return W, scala


def freno_drawdown(W, P, rf, scala, ridurre=0.10, fermo=0.15, pausa=21):
    """Arresto di emergenza: calcolato giorno per giorno sull'equity fino al giorno PRIMA."""
    s = scala.copy()
    eq, picco, stop_fino = 1.0, 1.0, -1
    comp = esegui(W, P, rf)
    risky = comp["contrib"].sum(axis=1)
    gross = comp["gross"]
    rfd = rf / 252
    out = np.zeros(len(s))
    for i, d in enumerate(s.index):
        dd = 1 - eq / picco
        m = s.iloc[i]
        if i <= stop_fino:
            m = 0.0
        elif dd >= fermo:
            m, stop_fino, picco = 0.0, i + pausa, eq  # dopo la pausa si riparte dal livello attuale
        elif dd >= ridurre:
            m *= 0.5
        out[i] = m
        m_eff = out[i - 1] if i else 1.0
        r = m_eff * risky.iloc[i] + (1 - min(m_eff * gross.iloc[i], 1)) * rfd.iloc[i]
        eq *= 1 + r
        picco = max(picco, eq)
    return pd.Series(out, index=s.index)


def e_multi(rets: dict[str, pd.Series], finestra=252):
    """E. Multi-strategia: A, B, C a pari rischio; ogni strategia si spegne se il suo Sharpe degli ultimi
    12 mesi è negativo (filtro di regime a livello di strategia). Pesi = inverso della volatilità a 63 g,
    calcolati con dati fino al giorno prima."""
    R = pd.DataFrame(rets)
    vol63 = R.rolling(63).std().shift(1)
    sh = (R.rolling(finestra).mean() / R.rolling(finestra).std()).shift(1)
    attivo = (sh > 0).astype(float)
    inv = (1 / vol63).replace([np.inf], np.nan) * attivo
    pesi = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
    tot = pesi.sum(axis=1)
    return (pesi * R).sum(axis=1)  # le strategie spente restano in liquidità già dentro le loro serie


# ============================================================ validazione
def walk_forward(serie_per_param: dict, primo_anno=2011, anni_stima=5):
    """Ogni anno sceglie il parametro con Sharpe migliore nei 5 anni precedenti e lo usa nell'anno seguente."""
    R = pd.DataFrame(serie_per_param)
    pezzi, scelte = [], {}
    for y in range(primo_anno, R.index[-1].year + 1):
        stima = R.loc[f"{y - anni_stima}-01-01":f"{y - 1}-12-31"]
        sh = stima.mean() / stima.std()
        best = sh.idxmax()
        scelte[y] = str(best)
        pezzi.append(R.loc[f"{y}-01-01":f"{y}-12-31", best])
    return pd.concat(pezzi), scelte


def monte_carlo(r: pd.Series, n=2000, blocco=20, seed=7):
    """Bootstrap a blocchi dei rendimenti giornalieri: distribuzione di CAGR e drawdown massimo."""
    rng = np.random.default_rng(seed)
    v = r.dropna().to_numpy()
    L = len(v)
    cagr, dd = [], []
    for _ in range(n):
        starts = rng.integers(0, L - blocco, L // blocco + 1)
        x = np.concatenate([v[s:s + blocco] for s in starts])[:L]
        eq = np.cumprod(1 + x)
        cagr.append(eq[-1] ** (252 / L) - 1)
        dd.append((eq / np.maximum.accumulate(eq) - 1).min())
    q = lambda a, p: round(100 * float(np.percentile(a, p)), 1)
    return {"CAGR_%_p5": q(cagr, 5), "CAGR_%_mediana": q(cagr, 50), "CAGR_%_p95": q(cagr, 95),
            "maxDD_%_p5_peggiore": q(dd, 5), "maxDD_%_mediana": q(dd, 50),
            "prob_CAGR_negativo_%": round(100 * float(np.mean(np.array(cagr) < 0)), 1)}


# ============================================================ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="risultati_ricerca")
    ap.add_argument("--bot-equity", help="equity.csv prodotto da run_backtest.py (bot invariato)")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    P, rf = carica(a.synthetic)
    inizio = "2006-01-01"

    griglie = {
        "A_trend": {lb: lambda lb=lb: esegui(a_trend(P, lb), P, rf) for lb in (189, 252, 315)},
        "B_mean_reversion": {s: lambda s=s: esegui(b_mean_reversion(P, s), P, rf) for s in (5, 10, 15)},
        "C_breakout": {n: lambda n=n: esegui(c_breakout(P, n), P, rf) for n in (40, 55, 70)},
        "D_volatilita": {vt: lambda vt=vt: (lambda W, s: esegui(W, P, rf, s))(*d_volatilita(P, rf, vt)) for vt in (0.08, 0.10, 0.12)},
    }
    default = {"A_trend": 252, "B_mean_reversion": 10, "C_breakout": 55, "D_volatilita": 0.10}
    calc = {k: {p: f() for p, f in g.items()} for k, g in griglie.items()}

    rep = {"note": __doc__.strip(), "strategie": {}}
    serie = {}
    for k, per_param in calc.items():
        res = per_param[default[k]]
        serie[k] = res["ret"].loc[inizio:]
        voce = {"parametro_base": default[k],
                "periodi": {n: metriche(res, *p) for n, p in PERIODI.items()},
                "intero_2006_oggi": metriche(res, inizio, None),
                "robustezza_sharpe_per_parametro": {str(p): {n: metriche(r, *pp).get("sharpe") for n, pp in PERIODI.items()}
                                                    for p, r in per_param.items()}}
        wf, scelte = walk_forward({p: r["ret"] for p, r in per_param.items()})
        voce["walk_forward_2011_oggi"] = metriche_serie(wf)
        voce["walk_forward_parametri_scelti"] = scelte
        voce["monte_carlo"] = monte_carlo(serie[k])
        operazioni(res).to_csv(out / f"operazioni_{k}.csv", index=False)
        rep["strategie"][k] = voce

    # E: multi-strategia
    multi = e_multi({k: serie[k] for k in ("A_trend", "B_mean_reversion", "C_breakout")}).loc[inizio:]
    serie["E_multi"] = multi
    rep["strategie"]["E_multi"] = {
        "periodi": {n: metriche_serie(multi, *p) for n, p in PERIODI.items()},
        "intero_2006_oggi": metriche_serie(multi),
        "monte_carlo": monte_carlo(multi),
        "nota": "metriche per operazione non calcolate: combina le serie di A, B, C (già al netto dei costi)"}

    # riferimenti: replica rotazione, bot reale, passivi
    r0 = esegui(s0_rotazione(P), P, rf)
    serie["replica_rotazione"] = r0["ret"].loc[inizio:]
    rif = {"replica_rotazione_bot": {n: metriche(r0, *p) for n, p in PERIODI.items()}}
    ew = P["Close"].pct_change().mean(axis=1).loc[inizio:]
    spy = P["Close"]["SPY"].pct_change().loc[inizio:]
    for nome, s in (("passivo_pesi_uguali_10_ETF", ew), ("passivo_SPY", spy)):
        rif[nome] = {n: metriche_serie(s, *p) for n, p in PERIODI.items()}
        rif[nome]["monte_carlo"] = monte_carlo(s)
    if a.bot_equity and Path(a.bot_equity).exists():
        be = pd.read_csv(a.bot_equity, index_col=0, parse_dates=True)["strategia"].pct_change().dropna()
        serie["BOT_reale"] = be
        rif["BOT_reale"] = {n: metriche_serie(be, *p) for n, p in PERIODI.items()}
        rif["BOT_reale"]["monte_carlo"] = monte_carlo(be)
        rif["correlazione_replica_vs_bot"] = round(float(serie["replica_rotazione"].corr(be)), 3)
    rep["riferimenti"] = rif
    S = pd.DataFrame(serie).dropna()
    rep["correlazioni"] = S.corr().round(2).to_dict()

    # combinazione col bot: 70% bot + 30% strategia (ribilanciata ogni giorno, semplificazione)
    base = "BOT_reale" if "BOT_reale" in S else "replica_rotazione"
    rep["combinazione_70_bot_30_strategia"] = {
        k: {n: metriche_serie(0.7 * S[base] + 0.3 * S[k], *p).get("sharpe") for n, p in PERIODI.items()}
        for k in ("A_trend", "B_mean_reversion", "C_breakout", "D_volatilita", "E_multi")}
    rep["combinazione_70_bot_30_strategia"]["solo_bot"] = {n: metriche_serie(S[base], *p).get("sharpe") for n, p in PERIODI.items()}

    # criteri minimi (dichiarati prima di vedere i risultati)
    crit = {}
    bot_sh_test = metriche_serie(S[base], *PERIODI["test_finale_2021_oggi"]).get("sharpe", 0)
    for k, v in rep["strategie"].items():
        per = v["periodi"]
        sh = [per[n].get("sharpe", 0) or 0 for n in PERIODI]
        pf_ok = all((per[n].get("profit_factor") or 0) >= 1.2 for n in PERIODI) if k != "E_multi" else True
        dd_ok = (v["intero_2006_oggi"].get("max_drawdown_%") or -100) >= -20
        wf_ok = (v.get("walk_forward_2011_oggi", {}).get("sharpe", 0) or 0) >= 0.5 if k != "E_multi" else min(sh) >= 0.5
        rob = v.get("robustezza_sharpe_per_parametro")
        rob_ok = all((x or 0) >= 0.3 for d in rob.values() for x in d.values()) if rob else True
        comb = rep["combinazione_70_bot_30_strategia"][k]
        aggiunge = (sh[2] > bot_sh_test) or (comb["test_finale_2021_oggi"] or 0) > (bot_sh_test or 0)
        crit[k] = {"sharpe_>=0.5_in_tutti_i_periodi": min(sh) >= 0.5, "profit_factor_>=1.2_in_tutti_i_periodi": pf_ok,
                   "drawdown_entro_-20%": dd_ok, "walk_forward_sharpe_>=0.5": wf_ok,
                   "robusta_ai_parametri": rob_ok, "aggiunge_valore_al_bot_nel_test_finale": aggiunge}
        crit[k] = {kk: bool(vv) for kk, vv in crit[k].items()}
        crit[k]["SUPERA"] = all(crit[k].values())
    rep["criteri"] = crit

    # sensibilità ai costi: costi realistici Alpaca (zero commissioni, ~0,03% tra spread e slippage per lato)
    global COSTO
    COSTO, sens = 0.0003, {}
    for k, p in default.items():
        r = griglie[k][p]()
        sens[k] = {n: {m: metriche(r, *pp).get(m) for m in ("sharpe", "CAGR_%", "profit_factor")} for n, pp in PERIODI.items()}
    rep["sensibilita_costi_0.03%_per_lato"] = sens
    COSTO = 0.001 + 0.0005
    json.dump(rep, open(out / "report.json", "w"), indent=1, default=str)
    print(json.dumps(crit, indent=1))


if __name__ == "__main__":
    main()
