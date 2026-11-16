
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
