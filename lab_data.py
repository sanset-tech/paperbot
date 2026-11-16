"""Dati per il Laboratorio (docs/lab_data.json): ultime candele giornaliere CHIUSE dei 18 ETF
e pattern delle candele riconosciuti con definizioni fisse, uguali ogni giorno.

Gira insieme ai prezzi in diretta. Il briefing delle 14:47 legge questo file invece di
scaricare i prezzi da solo (dal cloud Yahoo e Stooq spesso lo bloccano).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "TLT", "GLD", "USO",
            "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "SMH"]

# Quadro di mercato prima dell'apertura USA: futures e mercati che trattano di notte o al mattino
MARKET = {"ES=F": "future S&P 500", "NQ=F": "future Nasdaq 100", "RTY=F": "future Russell 2000",
          "^VIX": "VIX (paura)", "^STOXX50E": "Euro Stoxx 50", "^N225": "Nikkei 225",
          "^TNX": "rendimento Treasury 10 anni", "CL=F": "petrolio WTI", "GC=F": "oro", "DX-Y.NYB": "dollaro (DXY)"}


def rsi(close: pd.Series, n: int) -> float:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    v = 100 - 100 / (1 + up.iloc[-1] / dn.iloc[-1]) if dn.iloc[-1] > 0 else 100.0
    return round(float(v), 1)


def indicators(df: pd.DataFrame) -> dict:
    """Contesto oggettivo dell'ultima candela chiusa. Stesse formule ogni giorno."""
    c, h, l, o = df["Close"], df["High"], df["Low"], df["Open"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    hi20, lo20 = h.tail(20).max(), l.tail(20).min()
    out = {
        "rsi14": rsi(c, 14),
        "rsi2": rsi(c, 2),
        "atr_pct": round(float(100 * atr14 / c.iloc[-1]), 2),
        "gap_pct": round(float(100 * (o.iloc[-1] / c.iloc[-2] - 1)), 2),
        "ret_5g_pct": round(float(100 * (c.iloc[-1] / c.iloc[-6] - 1)), 2),
        "posizione_range20": round(float((c.iloc[-1] - lo20) / (hi20 - lo20)), 2) if hi20 > lo20 else 0.5,
        "sopra_media50": bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else None,
        "sopra_media200": bool(c.iloc[-1] > c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None,
    }
    if "Volume" in df and df["Volume"].tail(21).gt(0).all():
        out["volume_relativo"] = round(float(df["Volume"].iloc[-1] / df["Volume"].iloc[-21:-1].mean()), 2)
    return out


def market_snapshot() -> dict:
    """Variazione di ogni mercato rispetto alla sua ultima chiusura, al momento dell'esecuzione."""
    import yfinance as yf
    out = {}
    for t, name in MARKET.items():
        try:
            d = yf.Ticker(t).history(period="10d", interval="1d", auto_adjust=False)
            m = yf.Ticker(t).history(period="2d", interval="15m", auto_adjust=False)
        except Exception as e:
            print("errore", t, e)
            continue
        d = d.dropna(subset=["Close"]) if d is not None else d
        m = m.dropna(subset=["Close"]) if m is not None else m
        if d is None or len(d) < 2:
            continue
        last = float(m["Close"].iloc[-1]) if m is not None and not m.empty else float(d["Close"].iloc[-1])
        day = d.index[-1]
        today = pd.Timestamp.now(tz=day.tz).date() if day.tz is not None else pd.Timestamp.now().date()
        # seduta di oggi già iniziata: confronto con la chiusura precedente; altrimenti con l'ultima chiusura
        traded_today = day.date() == today
        ref = float(d["Close"].iloc[-2]) if traded_today else float(d["Close"].iloc[-1])
        out[t] = {"nome": name, "ultimo": round(last, 3), "variazione_pct": round(100 * (last / ref - 1), 2),
                  "seduta_di_oggi": traded_today,
                  "ora": (m.index[-1] if m is not None and not m.empty else day).strftime("%Y-%m-%d %H:%M %Z")}
        if t == "^VIX":
            out[t]["variazione_5g_pct"] = round(100 * (float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-6]) - 1), 2) if len(d) >= 6 else None
    return out


def patterns(df: pd.DataFrame) -> list[dict]:
    """Pattern sull'ultima candela di df (colonne Open, High, Low, Close; almeno 25 righe)."""
    if len(df) < 25:
        return []
    o, h, l, c = (df[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    sma20 = df["Close"].rolling(20).mean().iloc[-1]
    up_trend = c[-1] > sma20
    body = abs(c - o)
    rng = h - l
    upper = h - pd.Series(o).combine(pd.Series(c), max).to_numpy()
    lower = pd.Series(o).combine(pd.Series(c), min).to_numpy() - l
    avg_body = body[-11:-1].mean()
    bull = c > o
    bear = c < o
    out = []

    def add(name, direction):
        out.append({"pattern": name, "direzione_attesa": direction})

    if rng[-1] > 0 and body[-1] <= 0.1 * rng[-1]:
        add("doji", "rialzo" if up_trend else "ribasso")
    if body[-1] > 0 and lower[-1] >= 2 * body[-1] and upper[-1] <= body[-1] and not up_trend:
        add("hammer", "rialzo")
    if body[-1] > 0 and upper[-1] >= 2 * body[-1] and lower[-1] <= body[-1] and up_trend:
        add("shooting star", "ribasso")
    if bear[-2] and bull[-1] and o[-1] <= c[-2] and c[-1] >= o[-2] and body[-1] > body[-2]:
        add("engulfing rialzista", "rialzo")
    if bull[-2] and bear[-1] and o[-1] >= c[-2] and c[-1] <= o[-2] and body[-1] > body[-2]:
        add("engulfing ribassista", "ribasso")
    if bear[-3] and body[-3] > avg_body and body[-2] < 0.3 * body[-3] and bull[-1] and c[-1] > (o[-3] + c[-3]) / 2:
        add("morning star", "rialzo")
    if bull[-3] and body[-3] > avg_body and body[-2] < 0.3 * body[-3] and bear[-1] and c[-1] < (o[-3] + c[-3]) / 2:
        add("evening star", "ribasso")
    if all(bull[-3:]) and c[-1] > c[-2] > c[-3] and o[-3] < o[-2] <= c[-3] and o[-2] < o[-1] <= c[-2]:
        add("three white soldiers", "rialzo")
    if all(bear[-3:]) and c[-1] < c[-2] < c[-3] and c[-3] <= o[-2] < o[-3] and c[-2] <= o[-1] < o[-2]:
        add("three black crows", "ribasso")
    if h[-1] < h[-2] and l[-1] > l[-2]:
        add("inside bar", "rialzo" if up_trend else "ribasso")
    return out


def clean_nan(x):
    """Il JSON non ammette NaN: i valori mancanti diventano null."""
    if isinstance(x, dict):
        return {k: clean_nan(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean_nan(v) for v in x]
    if isinstance(x, float) and x != x:
        return None
    return x


def closed_bars(df: pd.DataFrame, now_ny: pd.Timestamp) -> pd.DataFrame:
    """Toglie la candela di oggi se la seduta non è ancora chiusa (16:00 New York + margine)."""
    if len(df) and df.index[-1].date() == now_ny.date() and (now_ny.hour, now_ny.minute) < (16, 15):
        return df.iloc[:-1]
    return df


def main():
    import yfinance as yf
    now_ny = pd.Timestamp.now(tz="America/New_York")
    out = {"aggiornato_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "nota": "candele giornaliere chiuse (prezzi non rettificati), pattern calcolati sull'ultima candela chiusa",
           "tickers": {}}
    last_days = set()
    for t in UNIVERSE:
        try:
            df = yf.Ticker(t).history(period="14mo", interval="1d", auto_adjust=False)
        except Exception as e:
            print("errore", t, e)
            continue
        if df is None or df.empty:
            continue
        df.index = pd.to_datetime(df.index).tz_convert("America/New_York")
        df = closed_bars(df[["Open", "High", "Low", "Close", "Volume"]].dropna(), now_ny)
        if len(df) < 25:
            continue
        sma20 = float(df["Close"].rolling(20).mean().iloc[-1])
        bars = [[ix.strftime("%Y-%m-%d")] + [round(float(v), 4) for v in row]
                for ix, row in df[["Open", "High", "Low", "Close"]].tail(10).iterrows()]
        last_days.add(bars[-1][0])
        out["tickers"][t] = {"candele": bars, "media20": round(sma20, 4),
                             "sopra_media20": bool(df["Close"].iloc[-1] > sma20), "pattern": patterns(df),
                             "indicatori": indicators(df)}
    out["ultima_candela_chiusa"] = max(last_days) if last_days else None
    tk = out["tickers"].values()
    out["ampiezza_mercato"] = {
        "pct_sopra_media50": round(100 * sum(1 for v in tk if v["indicatori"]["sopra_media50"]) / max(1, len(tk))),
        "pct_sopra_media20": round(100 * sum(1 for v in tk if v["sopra_media20"]) / max(1, len(tk)))}
    try:
        out["mercato_pre_apertura"] = market_snapshot()
    except Exception as e:  # il quadro di mercato è un extra: non deve bloccare le candele
        print("quadro di mercato non disponibile:", e)
    docs = Path(__file__).resolve().parent / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "lab_data.json").write_text(json.dumps(clean_nan(out), ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print("ticker:", len(out["tickers"]), "ultima candela:", out["ultima_candela_chiusa"])


if __name__ == "__main__":
    main()
