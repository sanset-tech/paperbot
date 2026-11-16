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
<div class="card" id="main">
  <div class="big">{fmt(eq)} <small>{cur}</small></div>
  <div class="grid">
    <div><span class="muted">Da inizio</span><br><b class="{"pos" if tot >= 0 else "neg"}">{tot:+.2f}%</b></div>
    <div><span class="muted">Drawdown</span><br><b>{fmt(dd, 1)}%</b></div>
    <div><span class="muted">Interessi</span><br><b>{fmt(state.get("interest_earned", 0))}</b></div>
  </div>
  {spark(state["equity_curve"])}
</div>
<div class="card"><h2>Portafoglio</h2><table id="ptable">{rows}</table></div>
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
.badge{{font-size:11px;font-weight:600;padding:2px 8px;border-radius:9px;margin-left:6px;vertical-align:middle}}
.badge.on{{background:#2e7d32;color:#fff}}.badge.off{{background:#8884;color:var(--fg)}}
.status.ok{{border-left:6px solid #2e7d32}}.status.warn{{border-left:6px solid #f9a825}}.status.bad{{border-left:6px solid #c62828}}
</style></head>
<body><header>📈 Paperbot <span id="livebadge" class="badge"></span></header><main>{body}
<p class="muted small" style="text-align:center">Pagina generata {now.strftime("%d/%m/%Y %H:%M")} · <span id="liveinfo">prezzi in diretta in caricamento…</span></p></main>
<script>window.PB_CAP = {cap}; window.PB_CUR = "{cur}";</script>
<script src="live.js"></script></body></html>'''

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
    # stato per l'aggiornamento in diretta della pagina
    if state is not None:
        (out_dir / "state.json").write_text(json.dumps({k: state[k] for k in (
            "cash", "positions", "pending", "equity_curve", "peak_equity", "last_processed", "slippage_pct")}), encoding="utf-8")
    (out_dir / "live.js").write_text(LIVE_JS, encoding="utf-8")


LIVE_JS = r"""
(function () {
  var NAMES = {SPY:"Azioni USA",QQQ:"Nasdaq 100",IWM:"Small cap USA",EFA:"Azioni Europa/Giappone",EEM:"Emergenti",
    TLT:"Bond USA 20+ anni",IEF:"Bond USA 7-10 anni",GLD:"Oro",DBC:"Materie prime",VNQ:"Immobiliare USA"};
  function fmt(x, d) { if (d === undefined) d = 2; return x.toLocaleString("it-IT", {minimumFractionDigits: d, maximumFractionDigits: d}); }
  function pct(x) { return (x >= 0 ? "+" : "") + fmt(x, 2) + "%"; }
  function cls(x) { return x >= 0 ? "pos" : "neg"; }
  function get(u) { return fetch(u + "?t=" + Date.now(), {cache: "no-store"}).then(function (r) { if (!r.ok) throw r.status; return r.json(); }); }
  function spark(pts) {
    if (!pts || pts.length < 2) return "";
    var v = pts.map(function (p) { return p[1]; }), lo = Math.min.apply(null, v), hi = Math.max.apply(null, v), r = (hi - lo) || 1, w = 340, h = 90;
    var d = v.map(function (y, i) { return (i * w / (v.length - 1)).toFixed(1) + "," + (h - 5 - (y - lo) / r * (h - 10)).toFixed(1); }).join(" ");
    var c = v[v.length - 1] >= v[0] ? "#2e7d32" : "#c62828";
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" class="chart" style="height:90px" preserveAspectRatio="none"><polyline fill="none" stroke="' + c + '" stroke-width="2.5" points="' + d + '"/></svg>' +
      '<div class="axis"><span>' + pts[0][0] + '</span><span>andamento di oggi (ora di New York)</span><span>' + pts[pts.length - 1][0] + '</span></div>';
  }
  function render(st, lv) {
    var q = lv.quotes || {}, slip = st.slippage_pct || 0;
    var pos = {}, est = false, cash = st.cash;
    Object.keys(st.positions).forEach(function (t) { var p = st.positions[t]; pos[t] = {qty: p.qty, entry: p.entry_price}; });
    // ordini in attesa: se la seduta del giorno dopo è già iniziata, stimali eseguiti all'apertura
    st.pending.forEach(function (o) {
      var x = q[o.ticker]; if (!x || x.day <= o.created) return;
      est = true;
      if (o.side === "BUY") { var px = x.open * (1 + slip); var p = pos[o.ticker] || {qty: 0, entry: px};
        p.entry = (p.entry * p.qty + px * o.qty) / (p.qty + o.qty); p.qty += o.qty; p.today = true; pos[o.ticker] = p; cash -= o.qty * px; }
      else if (pos[o.ticker]) { var sp = x.open * (1 - slip); cash += Math.min(o.qty, pos[o.ticker].qty) * sp; pos[o.ticker].qty -= o.qty; if (pos[o.ticker].qty <= 0) delete pos[o.ticker]; }
    });
    var eq = cash, rows = "", dayBase = 0, dayNow = 0;
    var names = Object.keys(pos).sort(function (a, b) { return pos[b].qty * ((q[b] || {}).last || 0) - pos[a].qty * ((q[a] || {}).last || 0); });
    names.forEach(function (t) { var x = q[t], p = pos[t]; var last = x ? x.last : p.entry; eq += p.qty * last; });
    names.forEach(function (t) {
      var x = q[t], p = pos[t], last = x ? x.last : p.entry, val = p.qty * last;
      var ref = x ? (p.today ? p.entry : (x.day > st.last_processed ? x.prev_close : last)) : last;
      var dayP = x ? (last / ref - 1) * 100 : 0, tot = (last / p.entry - 1) * 100;
      rows += '<tr><td><b>' + t + '</b><br><span class="muted">' + (NAMES[t] || "") + '</span></td><td class="r">' + fmt(last) +
        '<br><span class="muted">' + fmt(100 * val / eq, 1) + '%</span></td><td class="r ' + cls(dayP) + '">oggi ' + pct(dayP) +
        '<br><span class="' + cls(tot) + '">tot ' + pct(tot) + '</span></td></tr>';
    });
    rows += '<tr><td><b>Liquidità</b></td><td class="r">' + fmt(cash, 0) + '<br><span class="muted">' + fmt(100 * cash / eq, 1) + '%</span></td><td></td></tr>';
    var curve = st.equity_curve || [], prev = curve.length ? curve[curve.length - 1][1] : window.PB_CAP;
    var anyNew = names.some(function (t) { return q[t] && q[t].day > st.last_processed; }) || est;
    var day = anyNew ? eq - prev : 0, dayPct = prev ? 100 * day / prev : 0, tot = 100 * (eq / window.PB_CAP - 1);
    var peak = Math.max(st.peak_equity || eq, eq), dd = 100 * (1 - eq / peak);
    var main = document.getElementById("main");
    if (main) main.innerHTML =
      '<div class="muted small">Valore adesso</div><div class="big">' + fmt(eq) + ' <small>' + window.PB_CUR + '</small></div>' +
      '<div class="grid"><div><span class="muted">Oggi</span><br><b class="' + cls(day) + '">' + (day >= 0 ? "+" : "") + fmt(day, 0) + ' (' + pct(dayPct) + ')</b></div>' +
      '<div><span class="muted">Da inizio</span><br><b class="' + cls(tot) + '">' + pct(tot) + '</b></div>' +
      '<div><span class="muted">Drawdown</span><br><b>' + fmt(dd, 1) + '%</b></div></div>' +
      (lv.equity_intraday && lv.equity_intraday.length > 1 ? spark(lv.equity_intraday) : "") +
      (est ? '<div class="small muted" style="margin-top:6px">Include gli ordini di oggi stimati al prezzo di apertura: diventano definitivi dopo la chiusura.</div>' : "");
    var tb = document.getElementById("ptable"); if (tb) tb.innerHTML = rows;
    var b = document.getElementById("livebadge");
    if (b) { b.textContent = lv.mercato_aperto ? "● IN DIRETTA" : "mercato chiuso"; b.className = "badge " + (lv.mercato_aperto ? "on" : "off"); }
    var spy = q.SPY ? q.SPY.time : "", info = document.getElementById("liveinfo");
    if (info) info.textContent = "Prezzi aggiornati alle " + spy.slice(11) + " di New York (" + new Date(lv.aggiornato_utc).toLocaleTimeString("it-IT", {hour: "2-digit", minute: "2-digit"}) + " in Italia), aggiornamento automatico";
  }
  function tick() { Promise.all([get("state.json"), get("live.json")]).then(function (r) { render(r[0], r[1]); })
    .catch(function () { var i = document.getElementById("liveinfo"); if (i) i.textContent = "prezzi in diretta non disponibili, valori della chiusura"; }); }
  tick(); setInterval(tick, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
})();
"""


if __name__ == "__main__":
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.json")
    build(cfg, resolve(cfg, "docs"))
    print("Dashboard scritta in docs/index.html")
