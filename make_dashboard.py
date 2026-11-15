"""Genera la dashboard per il telefono (docs/index.html) dallo stato del portafoglio virtuale.

Pagina statica, nessuna libreria esterna: funziona su GitHub Pages e si installa
su Android con "Aggiungi a schermata Home".
"""
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import load_config, resolve, tactical_cfg

NAMES = {"SPY": "Azioni USA", "QQQ": "Nasdaq 100", "IWM": "Small cap USA", "EFA": "Azioni Europa/Giappone",
         "EEM": "Emergenti", "TLT": "Bond USA 20+ anni", "IEF": "Bond USA 7-10 anni", "GLD": "Oro",
         "DBC": "Materie prime", "VNQ": "Immobiliare USA"}


def fmt(x, d=2):
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def spark(curve, w=340, h=120):
    vals = [v for _, v in curve][-260:]
    if len(vals) < 2:
        return '<p class="muted">Il grafico compare dopo qualche giorno di dati.</p>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 6 - (v - lo) / rng * (h - 12):.1f}" for i, v in enumerate(vals))
    color = "#2e7d32" if vals[-1] >= vals[0] else "#c62828"
    return (f'<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{pts}"/></svg>'
            f'<div class="axis"><span>{fmt(lo, 0)}</span><span>{fmt(hi, 0)}</span></div>')


def health(state, now_rome):
    if state.get("halted"):
        return "ALLARME", "Kill switch scattato: il bot ha liquidato e si è fermato.", "bad"
    lp = state.get("last_processed")
    if not lp:
        return "ATTENZIONE", "Nessun giorno ancora processato.", "warn"
    days = (now_rome.date() - datetime.strptime(lp, "%Y-%m-%d").date()).days
    if days > 5:
        return "ALLARME", f"Ultimo giorno processato {days} giorni fa: il bot non sta girando.", "bad"
    eq, peak = equity(state), state.get("peak_equity") or 1
    dd = 1 - eq / peak
    if dd >= 0.12:
        return "ATTENZIONE", f"Drawdown {fmt(100 * dd, 1)}%: vicino alle soglie di protezione.", "warn"
    return "OK", "Il bot gira regolarmente.", "ok"


def equity(s):
    return s["cash"] + sum(p["qty"] * s["last_price"].get(t, p["entry_price"]) for t, p in s["positions"].items())


def build(cfg, out_dir: Path):
    sp = resolve(cfg, cfg["state_file"])
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else None
    now = datetime.now(ZoneInfo("Europe/Rome"))
    cap = cfg["portfolio"]["initial_capital"]
    cur = cfg["portfolio"]["currency"]

    if state is None:
        body = '<div class="card"><h2>Nessun portafoglio ancora</h2><p>Si crea alla prima esecuzione.</p></div>'
    else:
        eq = equity(state)
        peak = state.get("peak_equity") or eq
        tot = 100 * (eq / cap - 1)
        dd = 100 * (1 - eq / peak) if peak else 0
        lvl, msg, cls = health(state, now)
        rows = ""
        for t, p in sorted(state["positions"].items(), key=lambda x: -x[1]["qty"] * state["last_price"].get(x[0], 0)):
            px = state["last_price"].get(t, p["entry_price"])
            val = p["qty"] * px
            pnl = 100 * (px / p["entry_price"] - 1)
            rows += (f'<tr><td><b>{t}</b><br><span class="muted">{NAMES.get(t, "")}</span></td>'
                     f'<td class="r">{fmt(100 * val / eq, 1)}%<br><span class="muted">{fmt(val, 0)}</span></td>'
                     f'<td class="r {"pos" if pnl >= 0 else "neg"}">{pnl:+.1f}%</td></tr>')
        cash_w = 100 * state["cash"] / eq if eq else 0
        rows += f'<tr><td><b>Liquidità</b></td><td class="r">{fmt(cash_w, 1)}%<br><span class="muted">{fmt(state["cash"], 0)}</span></td><td></td></tr>'
        pend = "".join(f'<li>{o["side"]} <b>{o["ticker"]}</b> x{o["qty"]:g} <span class="muted">({html.escape(o["reason"])})</span></li>'
                       for o in state["pending"]) or '<li class="muted">nessuno</li>'
        fills = [f for f in state.get("fills", []) if f["side"] in ("BUY", "SELL")][-12:][::-1]
        hist = "".join(f'<li><span class="muted">{f["date"]}</span> {f["side"]} <b>{f["ticker"]}</b> x{f["qty"]:g} @ {fmt(f["price"])}</li>'
                       for f in fills) or '<li class="muted">ancora nessuna</li>'
        body = f'''
<div class="card status {cls}"><b>{lvl}</b> · {html.escape(msg)}</div>
<div class="card">
  <div class="big">{fmt(eq)} <small>{cur}</small></div>
  <div class="grid">
    <div><span class="muted">Da inizio</span><br><b class="{"pos" if tot >= 0 else "neg"}">{tot:+.2f}%</b></div>
    <div><span class="muted">Drawdown</span><br><b>{fmt(dd, 1)}%</b></div>
    <div><span class="muted">Interessi</span><br><b>{fmt(state.get("interest_earned", 0))}</b></div>
  </div>
  {spark(state["equity_curve"])}
</div>
<div class="card"><h2>Portafoglio</h2><table>{rows}</table></div>
<div class="card"><h2>Ordini alla prossima apertura</h2><ul>{pend}</ul></div>
<div class="card"><h2>Ultime operazioni</h2><ul>{hist}</ul></div>
<div class="card muted small">Ultimo giorno di borsa: {state.get("last_processed")} · Ultimo ribilanciamento: {state.get("last_rebalance") or "-"}<br>
Capitale iniziale {fmt(cap, 0)} {cur} · Portafoglio VIRTUALE, nessun soldo vero.</div>'''

    tcfg = tactical_cfg(cfg)
    if state is not None and tcfg:
        tp = resolve(tcfg, tcfg["state_file"])
        if tp.exists():
            ts = json.loads(tp.read_text(encoding="utf-8"))
            teq = equity(ts)
            tcap = tcfg["portfolio"]["initial_capital"]
            tpos = "".join(f'<li><b>{t}</b> x{p["qty"]:g} <span class="muted">carico {fmt(p["entry_price"])}</span></li>'
                           for t, p in ts["positions"].items()) or '<li class="muted">nessuna posizione aperta</li>'
            tpend = "".join(f'<li>{o["side"]} <b>{o["ticker"]}</b> x{o["qty"]:g} <span class="muted">({html.escape(o["reason"])})</span></li>'
                            for o in ts["pending"])
            closed = ts.get("trades", [])
            wins = sum(1 for t in closed if t["pnl"] > 0)
            body += f'''
<div class="card"><h2>Conto tattico (operazioni brevi)</h2>
  <div class="big" style="font-size:22px">{fmt(teq)} <small>{cur}</small></div>
  <div class="muted small">Da inizio {100 * (teq / tcap - 1):+.2f}% · operazioni chiuse {len(closed)}{f" · vinte {round(100 * wins / len(closed))}%" if closed else ""}</div>
  <ul style="margin-top:8px">{tpos}{tpend}</ul>
</div>'''

    page = f'''<!DOCTYPE html>
<html lang="it"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paperbot</title>
<link rel="manifest" href="manifest.json"><meta name="theme-color" content="#1b2a3a">
<link rel="icon" href="icon.svg">
<style>
:root{{--bg:#f4f5f7;--card:#fff;--fg:#1b2a3a;--muted:#6b7785}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1720;--card:#18222e;--fg:#e8edf2;--muted:#8a96a3}}}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
header{{padding:16px;font-weight:700;font-size:18px}}main{{padding:0 12px 24px;max-width:520px;margin:auto}}
.card{{background:var(--card);border-radius:14px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px #0002}}
h2{{font-size:15px;margin:0 0 8px}}.big{{font-size:30px;font-weight:700}}.big small{{font-size:14px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}}
.muted{{color:var(--muted)}}.small{{font-size:12px}}.pos{{color:#2e7d32}}.neg{{color:#c62828}}
table{{width:100%;border-collapse:collapse}}td{{padding:7px 2px;border-top:1px solid #8882;vertical-align:top}}.r{{text-align:right}}
ul{{margin:0;padding-left:18px}}li{{margin:3px 0}}.chart{{width:100%;height:120px}}.axis{{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}}
.status.ok{{border-left:6px solid #2e7d32}}.status.warn{{border-left:6px solid #f9a825}}.status.bad{{border-left:6px solid #c62828}}
</style></head>
<body><header>📈 Paperbot</header><main>{body}
<p class="muted small" style="text-align:center">Aggiornato {now.strftime("%d/%m/%Y %H:%M")}</p></main></body></html>'''

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps({
        "name": "Paperbot", "short_name": "Paperbot", "start_url": ".", "display": "standalone",
        "background_color": "#1b2a3a", "theme_color": "#1b2a3a",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}, indent=1), encoding="utf-8")
    (out_dir / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#1b2a3a"/>'
        '<polyline points="96,360 200,260 280,310 416,160" fill="none" stroke="#4caf50" stroke-width="40" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.json")
    build(cfg, resolve(cfg, "docs"))
    print("Dashboard scritta in docs/index.html")
