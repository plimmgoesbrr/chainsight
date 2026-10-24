/* ChainSight dashboard — plain JavaScript, no build step, works offline. */
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const fmt = n => (n === null || n === undefined) ? "–" : Number(n).toLocaleString("en-IN");
const pct = (x, d = 0) => (x === null || x === undefined) ? "–" : (x * 100).toFixed(d) + "%";
const short = a => a && a.length > 16 ? a.slice(0, 8) + "…" + a.slice(-6) : a;
const C = { purple: "#a48bff", purpleD: "#7c5cff", cream: "#ece6b2", high: "#ff5c7a", med: "#f5c56b", low: "#5f5f6b",
            ip: "#5ad1e6", text: "#f3f3f6", muted: "#8d8d98", line: "rgba(255,255,255,0.07)", ok: "#4fd18b" };
const COMP_COL = { P: C.purple, A: C.cream, G: C.high, T: C.med };
let charts = [];
let cy = null;

Chart.defaults.color = C.muted;
Chart.defaults.font.family = '"Segoe UI", system-ui, sans-serif';
Chart.defaults.borderColor = C.line;

async function api(path, opts) {
  const r = await fetch("/api" + path, opts);
  if (!r.ok) {
    let m = r.statusText;
    try { m = (await r.json()).detail || m; } catch (e) {}
    throw new Error(m);
  }
  return r.json();
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 1800); }
function clearCharts() { charts.forEach(c => c.destroy()); charts = []; if (cy) { cy.destroy(); cy = null; } }
function badge(b) { return `<span class="badge b-${b}">${b}</span>`; }
function scoreCell(s, b) { return `<span class="score"><b style="font-size:15px;font-weight:600">${s.toFixed(0)}</b>${badge(b)}</span>`; }
function wIco(a) { const t = a.startsWith("bc1") ? "BC1" : a[0] === "3" ? "P2S" : "P2P"; return `<span class="w-ico">${t}</span>`; }
function noData(e) {
  app.innerHTML = `<div class="hello">Welcome to <span>ChainSight</span></div>
  <div class="card empty">${e.message}<br><br><a class="btn" href="#/data">Go to Data</a></div>`;
}

/* ------------------------------------------------------------------ router */
function route() {
  clearCharts();
  const h = location.hash.replace(/^#\/?/, "") || "overview";
  const [page, arg] = h.split("/");
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.r === page));
  if (page === "alerts") return alertsPage();
  if (page === "wallet") return walletPage(arg ? decodeURIComponent(arg) : null);
  if (page === "tx") return txPage(arg);
  if (page === "data") return dataPage();
  return overviewPage();
}
window.addEventListener("hashchange", route);
$("#search").addEventListener("keydown", async e => {
  if (e.key !== "Enter" || !e.target.value.trim()) return;
  try {
    const r = await api("/search?q=" + encodeURIComponent(e.target.value.trim()));
    location.hash = r.type === "wallet" ? "#/wallet/" + encodeURIComponent(r.id) : "#/tx/" + r.id;
    e.target.value = "";
  } catch (err) { toast(err.message); }
});

/* ------------------------------------------------------------------ overview */
async function overviewPage() {
  let s, al;
  try { [s, al] = await Promise.all([api("/summary"), api("/alerts?limit=6&no_seeds=true")]); } catch (e) { return noData(e); }
  const k = s.kpi, ev = s.evaluation;
  const d0 = new Date(s.time_min), d1 = new Date(s.time_max);
  const dOpt = { day: "2-digit", month: "short" };
  app.innerHTML = `
  <div class="hello">Investigation <span>overview</span></div>
  <div class="sub"><span>Dataset <b>${s.dataset}</b></span><span>·</span>
    <span>${d0.toLocaleDateString("en-GB", dOpt)} – ${d1.toLocaleDateString("en-GB", dOpt)} 2026</span><span>·</span>
    <span>Analysed in <b>${s.elapsed ?? "–"} s</b></span><span>·</span><span>${k.seeds} known flagged wallets as seeds</span></div>
  <div class="grid">
    <div class="card c3"><h3>Transactions analysed</h3><a class="arrow" href="#/data">›</a>
      <div class="big">${fmt(k.transactions)}</div><div class="kpi-sub">${fmt(k.observations)} network observations</div></div>
    <div class="card c3"><h3>High-risk alerts</h3><a class="arrow" href="#/alerts">›</a>
      <div class="big purple">${fmt(k.high)}<small>wallets</small></div><div class="kpi-sub">+ ${fmt(k.medium)} medium · of ${fmt(k.wallets)} wallets</div></div>
    <div class="card c6 r2"><h3>Daily activity <small><span style="color:${C.cream}">■</span> all transactions &nbsp; <span style="color:${C.purple}">■</span> touching flagged wallets</small></h3>
      <div class="chart-box" style="height:300px"><canvas id="daily"></canvas></div></div>
    <div class="card c3"><h3>Owner groups found</h3>
      <div class="big">${fmt(k.entities)}</div><div class="kpi-sub">multi-wallet owners · mixers filtered: ${k.coinjoin}</div></div>
    <div class="card c3"><h3>IP ↔ transaction links</h3>
      <div class="big">${fmt(k.ip_tx_links)}</div><div class="kpi-sub">${pct(k.tx_with_origin)} of transactions traced to an origin IP</div></div>

    <div class="card c8"><h3>Top new leads <small>not in any known-flagged list · <a href="#/alerts" style="color:#cfcfd6">view all ›</a></small></h3>
      <table><thead><tr><th>#</th><th>Wallet</th><th>Risk</th><th>Patterns</th><th>Main reason</th></tr></thead>
      <tbody>${al.rows.map((r, i) => `<tr class="click" data-a="${r.address}"><td>${i + 1}</td>
        <td>${wIco(r.address)}<span class="mono">${short(r.address)}</span>${r.is_seed ? ' <span class="badge b-SEED">SEED</span>' : ""}</td>
        <td>${scoreCell(r.score, r.band)}</td><td>${r.patterns.map(p => `<span class="chip">${p}</span>`).join("")}</td>
        <td class="reason-cell">${r.reason}</td></tr>`).join("")}</tbody></table></div>
    <div class="card c4"><h3>Detection quality <small>held-out test data</small></h3>
      ${ev ? `<div class="qgrid">
        <div class="q"><div class="v ok">${pct(ev.precision_at_50)}</div><div class="l">of the top 50 alerts are truly illicit</div></div>
        <div class="q"><div class="v p">${ev.cases_caught}/${ev.cases_total}</div><div class="l">laundering cases caught</div></div>
        <div class="q"><div class="v">${pct(ev.recall_flagged)}</div><div class="l">of illicit wallets flagged</div></div>
        <div class="q"><div class="v">${pct(ev.false_alarm_rate, 1)}</div><div class="l">false-alarm rate on normal wallets</div></div></div>
        <div class="note">Measured against planted ground truth the pipeline never sees.</div>`
        : `<div class="muted">No ground truth for this dataset — quality metrics are shown for the built-in case dataset.</div>`}</div>

    <div class="card c4"><h3>Patterns detected <small>flagged wallets</small></h3><div class="bars" id="pats"></div></div>
    <div class="card c4"><h3>Risk distribution <small>wallets per score band</small></h3><div class="chart-box sm"><canvas id="hist"></canvas></div></div>
    <div class="card c4"><h3>Where flagged activity comes from</h3><div class="bars" id="net"></div>
      <div class="note">From IPs linked to flagged transactions (offline geo / ASN lookup).</div></div>
  </div>`;
  app.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => location.hash = "#/wallet/" + encodeURIComponent(tr.dataset.a));
  // daily chart
  charts.push(new Chart($("#daily"), {
    type: "bar",
    data: { labels: s.daily.days, datasets: [
      { label: "All transactions", data: s.daily.all, backgroundColor: "rgba(236,230,178,0.85)", borderRadius: 8, barPercentage: .9, categoryPercentage: .75 },
      { label: "Touching flagged wallets", data: s.daily.flagged, backgroundColor: C.purple, borderRadius: 8, barPercentage: .9, categoryPercentage: .75 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 11 } },
                y: { grid: { color: C.line }, ticks: { maxTicksLimit: 5 } } } }
  }));
  // histogram
  const hc = s.histogram.counts;
  charts.push(new Chart($("#hist"), {
    type: "bar",
    data: { labels: s.histogram.bins, datasets: [{ data: hc, borderRadius: 6,
      backgroundColor: s.histogram.bins.map((b, i) => i * 10 >= s.bands.high ? C.high : i * 10 >= s.bands.medium ? C.med : "#3a3a44") }] },
    options: { maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { grid: { display: false }, ticks: { maxRotation: 0, font: { size: 10 } } },
                y: { type: "logarithmic", grid: { color: C.line }, ticks: { maxTicksLimit: 4 } } } }
  }));
  const pe = Object.entries(s.patterns), pm = Math.max(1, ...pe.map(x => x[1]));
  $("#pats").innerHTML = pe.map(([n, v]) => `<div class="row"><span>${n}</span><div class="track"><div class="fill" style="width:${v / pm * 100}%"></div></div><span class="n">${v}</span></div>`).join("");
  const at = s.network.asn_types, ct = s.network.countries;
  const tot = Object.values(at).reduce((a, b) => a + b, 0) || 1;
  const nm = { hosting: "Hosting / VPN", residential: "Home broadband", mobile: "Mobile", datacenter: "Data-centre" };
  $("#net").innerHTML = Object.entries(at).map(([n, v]) => `<div class="row"><span>${nm[n] || n}</span><div class="track"><div class="fill" style="width:${v / tot * 100}%;background:${n === "hosting" ? C.high : C.ok}"></div></div><span class="n">${pct(v / tot)}</span></div>`).join("")
    + `<div class="muted" style="margin-top:10px">Top countries: ${Object.entries(ct).map(([c, v]) => `<b style="color:#ddd">${c}</b> ${v}`).join(" · ")}</div>`;
}

/* ------------------------------------------------------------------ alerts */
async function alertsPage(band = "") {
  let d;
  try { d = await api("/alerts?limit=150" + (band ? "&band=" + band : "")); } catch (e) { return noData(e); }
  app.innerHTML = `<div class="hello">Ranked <span>alerts</span></div>
  <div class="sub">Every flagged wallet, riskiest first. Click a row to investigate.</div>
  <div class="card c12">
    <div class="filters">
      <button class="fbtn ${band === "" ? "on" : ""}" data-b="">High + Medium</button>
      <button class="fbtn ${band === "HIGH" ? "on" : ""}" data-b="HIGH">High</button>
      <button class="fbtn ${band === "MEDIUM" ? "on" : ""}" data-b="MEDIUM">Medium</button>
      <span class="muted" style="margin-left:auto">${fmt(d.total)} wallets</span></div>
    <table><thead><tr><th>#</th><th>Wallet</th><th>Risk</th><th>Owner group</th><th>Patterns</th><th>Main reason</th></tr></thead>
    <tbody>${d.rows.map((r, i) => `<tr class="click" data-a="${r.address}"><td>${i + 1}</td>
      <td>${wIco(r.address)}<span class="mono">${short(r.address)}</span>${r.is_seed ? ' <span class="badge b-SEED">SEED</span>' : ""}</td>
      <td>${scoreCell(r.score, r.band)}</td><td class="muted">${r.entity_id} · ${r.cluster_size} wallet${r.cluster_size > 1 ? "s" : ""}</td>
      <td>${r.patterns.map(p => `<span class="chip">${p}</span>`).join("")}</td><td class="reason-cell">${r.reason}</td></tr>`).join("")}</tbody></table>
  </div>`;
  app.querySelectorAll(".fbtn").forEach(b => b.onclick = () => alertsPage(b.dataset.b));
  app.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => location.hash = "#/wallet/" + encodeURIComponent(tr.dataset.a));
}

/* ------------------------------------------------------------------ wallet */
function gauge(score, band, bands) {
  const r = 62, c = 2 * Math.PI * r, f = Math.max(0, Math.min(100, score)) / 100;
  const col = band === "HIGH" ? C.high : band === "MEDIUM" ? C.med : C.low;
  return `<svg class="gauge" viewBox="0 0 150 150">
    <circle cx="75" cy="75" r="${r}" stroke="rgba(255,255,255,.07)" stroke-width="12" fill="none"/>
    <circle cx="75" cy="75" r="${r}" stroke="${col}" stroke-width="12" fill="none" stroke-linecap="round"
      stroke-dasharray="${c * f} ${c}" transform="rotate(-90 75 75)"/>
    <text x="75" y="78" text-anchor="middle" font-size="40" font-weight="300">${score.toFixed(0)}</text>
    <text x="75" y="100" text-anchor="middle" font-size="11" fill="${C.muted}">/ 100 risk</text></svg>`;
}

async function walletPage(addr) {
  if (!addr) {
    try { const a = await api("/alerts?limit=1&band=HIGH&no_seeds=true"); if (a.rows.length) { location.hash = "#/wallet/" + encodeURIComponent(a.rows[0].address); return; } }
    catch (e) { return noData(e); }
  }
  let w, g;
  try { [w, g] = await Promise.all([api("/wallet/" + encodeURIComponent(addr)), api("/graph/" + encodeURIComponent(addr))]); }
  catch (e) { app.innerHTML = `<div class="card empty">${e.message}</div>`; return; }
  const pats = Object.keys(w.patterns);
  app.innerHTML = `
  <div class="hello">Wallet <span>${short(w.address)}</span></div>
  <div class="sub"><span class="addr-full" id="copy" title="Copy">${w.address} ⧉</span>${badge(w.band)}
    ${w.is_seed ? '<span class="badge b-SEED">KNOWN FLAGGED (SEED)</span>' : ""}
    ${w.is_service_hub ? '<span class="badge b-LOW">SERVICE HUB</span>' : ""}
    <span>Owner group <b>${w.entity_id}</b> · ${w.cluster_size} wallet${w.cluster_size > 1 ? "s" : ""} (confidence ${pct(w.cluster_confidence)})</span>
    <span>· ${w.n_transactions} transactions</span>
    ${pats.map(p => `<span class="chip">${p}</span>`).join("")}</div>
  <div class="grid">
    <div class="card c4"><h3>Risk score</h3>
      <div class="gauge-wrap">${gauge(w.score, w.band)}
        <div><div style="font-size:22px;font-weight:500;color:${w.band === "HIGH" ? C.high : w.band === "MEDIUM" ? C.med : C.muted}">${w.band}</div>
        <div class="muted" style="margin-top:6px;line-height:1.5">High ≥ ${w.bands.high}<br>Medium ≥ ${w.bands.medium}</div></div></div>
      <div class="comp">${w.components.map(c => `<div class="row"><div class="t">${c.label}<small>${c.key === "P" ? "XGBoost" : c.key === "A" ? "Isolation Forest" : c.key === "G" ? "Personalised PageRank" : "pattern detectors"} · weight ${c.weight}</small></div>
        <div class="track"><div class="fill" style="width:${c.value * 100}%;background:${COMP_COL[c.key]}"></div></div><div class="pts">+${c.points.toFixed(1)}</div></div>`).join("")}</div>
    </div>
    <div class="card c8"><h3>Money trail <small>click a wallet to open it · drag to explore</small></h3><div id="cy"></div>
      <div class="legend"><span><i style="background:${C.high};box-shadow:0 0 0 2px #fff"></i>Focus wallet</span><span><i style="background:${C.high}"></i>High risk</span>
      <span><i style="background:${C.med}"></i>Medium</span><span><i style="background:#4b4b56"></i>Low</span><span><i style="background:transparent;border:2px solid ${C.high}"></i>Known flagged (seed)</span>
      <span><i style="background:${C.ip};border-radius:3px"></i>IP address (origin)</span><span><i style="background:${C.cream}"></i>Service hub (exchange)</span></div></div>

    <div class="card c6"><h3>Why it was flagged</h3>
      ${w.reasons.length ? `<ol class="reasons">${w.reasons.map(r => `<li>${r}</li>`).join("")}</ol>` : `<div class="muted">This wallet is low risk — no strong reasons to flag it.</div>`}
      <div class="note">✓ Deterministic: every sentence is filled from stored values — no LLM, same input gives the same text.</div></div>
    <div class="card c6"><h3>Score breakdown <small>points each factor added</small></h3><div class="chart-box" style="height:${Math.max(160, Math.min(9, w.factors.length) * 30 + 30)}px"><canvas id="fac"></canvas></div></div>

    <div class="card c8"><h3>Transactions <small>${w.n_transactions} total${w.n_transactions > 60 ? " · first 60 shown" : ""}</small></h3>
      <table><thead><tr><th>Time (UTC)</th><th>TXID</th><th>Direction</th><th>Amount (BTC)</th><th>In → Out</th><th>Origin IP</th></tr></thead>
      <tbody>${w.transactions.map(t => `<tr class="click" data-t="${t.txid}"><td class="muted">${t.time.slice(0, 16).replace("T", " ")}</td>
        <td class="mono">${t.txid.slice(0, 12)}…</td><td class="${t.direction}">${t.direction === "in" ? "↓ received" : "↑ sent"}</td>
        <td>${(t.amount ?? 0).toFixed(5)}</td><td class="muted">${t.n_in} → ${t.n_out}${t.coinjoin ? ' <span class="chip">mixer</span>' : ""}</td>
        <td class="muted">${t.origin_ip ? `${t.origin_ip} <span style="color:#ccc">${t.country}</span>` : "–"}</td></tr>`).join("")}</tbody></table></div>
    <div class="card c4"><h3>Network evidence</h3>
      <div class="iplist">${w.origin_ips.length ? w.origin_ips.map(o => `<div class="ip"><div><div class="mono" style="color:#eee">${o.ip}</div>
        <div class="muted">${o.country} · ${o.asn} · <span class="${o.asn_type === "hosting" ? "tag-host" : "tag-res"}">${o.asn_type}</span></div></div>
        <div style="text-align:right"><div>${o.tx} tx</div><div class="muted">${o.linked_wallet ? "linked (K ≥ 1.5)" : "seen once"}</div></div></div>`).join("")
        : '<div class="muted">No outgoing transactions traced to an origin IP.</div>'}</div>
      <div class="note">IP links are supporting evidence with a confidence level — never proof of who owns the wallet.</div>
      ${w.related.length ? `<h3 style="margin-top:18px">Related suspicious wallets</h3>${w.related.slice(0, 5).map(r => `<div class="ip" style="cursor:pointer" data-a="${r.address}"><span class="mono">${short(r.address)}</span><span style="margin-left:auto">${scoreCell(r.score, r.band)}</span></div>`).join("")}` : ""}
    </div>
  </div>`;
  $("#copy").onclick = () => { navigator.clipboard && navigator.clipboard.writeText(w.address); toast("Address copied"); };
  app.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => location.hash = "#/tx/" + tr.dataset.t);
  app.querySelectorAll(".ip[data-a]").forEach(el => el.onclick = () => location.hash = "#/wallet/" + encodeURIComponent(el.dataset.a));
  // factors chart
  const fac = w.factors.slice(0, 9);
  charts.push(new Chart($("#fac"), {
    type: "bar",
    data: { labels: fac.map(f => f.factor), datasets: [{ data: fac.map(f => f.points), backgroundColor: fac.map(f => COMP_COL[f.component] || C.purple), borderRadius: 6, barThickness: 16 }] },
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false },
      tooltip: { callbacks: { label: c => " +" + c.raw.toFixed(1) + " points" } } },
      scales: { x: { grid: { color: C.line }, title: { display: true, text: "risk points" } }, y: { grid: { display: false }, ticks: { color: "#d7d7de" } } } }
  }));
  drawGraph(g, w.address);
}

function drawGraph(g, focus) {
  const els = [];
  g.nodes.forEach(n => {
    const col = n.type === "ip" ? C.ip : n.hub ? C.cream : n.band === "HIGH" ? C.high : n.band === "MEDIUM" ? C.med : "#4b4b56";
    els.push({ data: { id: n.id, label: n.type === "ip" ? `${n.label}\n${n.country} · ${n.asn_type}` : n.label, col, type: n.type,
      size: n.focus ? 46 : n.type === "ip" ? 26 : 18 + (n.score || 0) / 6, seed: n.seed ? 1 : 0, focus: n.focus ? 1 : 0 } });
  });
  g.edges.forEach((e, i) => els.push({ data: { id: "e" + i, source: e.source, target: e.target, ip: e.ip ? 1 : 0,
    w: e.ip ? 1.5 : Math.min(6, 1 + Math.log10(1 + e.amount * 100)) } }));
  cy = cytoscape({
    container: $("#cy"), elements: els, wheelSensitivity: 0.2,
    style: [
      { selector: "node", style: { "background-color": "data(col)", width: "data(size)", height: "data(size)", label: "data(label)",
        color: "#c9c9d1", "font-size": 9, "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "wrap", "border-width": 0 } },
      { selector: "node[type='ip']", style: { shape: "round-rectangle", color: C.ip } },
      { selector: "node[seed=1]", style: { "border-width": 3, "border-color": C.high, "background-color": "#2a1018" } },
      { selector: "node[focus=1]", style: { "border-width": 4, "border-color": "#ffffff", "font-size": 11, color: "#fff", "font-weight": 600 } },
      { selector: "edge", style: { width: "data(w)", "line-color": "rgba(164,139,255,.45)", "target-arrow-color": "rgba(164,139,255,.7)",
        "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.8 } },
      { selector: "edge[ip=1]", style: { "line-color": "rgba(90,209,230,.6)", "line-style": "dashed", "target-arrow-color": C.ip } },
    ],
    layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 70, padding: 20, randomize: false },
  });
  cy.on("tap", "node", ev => {
    const d = ev.target.data();
    if (d.type !== "ip" && d.id !== focus) location.hash = "#/wallet/" + encodeURIComponent(d.id);
  });
}

/* ------------------------------------------------------------------ transaction */
async function txPage(txid) {
  let t;
  try { t = await api("/transactions?txid=" + txid); } catch (e) { app.innerHTML = `<div class="card empty">${e.message}</div>`; return; }
  const side = (arr) => arr.map(x => `<tr class="click" data-a="${x.address}"><td>${wIco(x.address)}<span class="mono">${short(x.address)}</span></td>
    <td>${x.amount.toFixed(6)}</td><td>${scoreCell(x.score, x.band)}</td></tr>`).join("");
  app.innerHTML = `<div class="hello">Transaction <span>${t.txid.slice(0, 10)}…</span></div>
  <div class="sub"><span class="addr-full">${t.txid}</span><span>${t.time.slice(0, 19).replace("T", " ")} UTC</span>
    <span>· fee ${t.fee.toFixed(6)} BTC · ${t.script_type}</span>${t.coinjoin ? '<span class="chip">Mixing-like (CoinJoin)</span>' : ""}</div>
  <div class="grid">
    <div class="card c6"><h3>Inputs (${t.n_in})</h3><table><thead><tr><th>Wallet</th><th>BTC</th><th>Risk</th></tr></thead><tbody>${side(t.inputs)}</tbody></table></div>
    <div class="card c6"><h3>Outputs (${t.n_out})</h3><table><thead><tr><th>Wallet</th><th>BTC</th><th>Risk</th></tr></thead><tbody>${side(t.outputs)}</tbody></table></div>
    <div class="card c12"><h3>Network evidence</h3>${t.origin_ip ? `<div class="iplist"><div class="ip"><div><span class="mono">${t.origin_ip}</span>
      <div class="muted">${t.country} · ${t.asn}</div></div><div>matching degree P = ${t.origin_P.toFixed(2)} <span class="muted">(link threshold 1.5)</span></div></div></div>`
      : '<div class="muted">The originating node was not observed by the sensors for this transaction.</div>'}</div>
  </div>`;
  app.querySelectorAll("tr.click").forEach(tr => tr.onclick = () => location.hash = "#/wallet/" + encodeURIComponent(tr.dataset.a));
}

/* ------------------------------------------------------------------ data */
let selDs = "case_dataset.csv";
async function dataPage() {
  let ds;
  try { ds = await api("/datasets"); } catch (e) { app.innerHTML = `<div class="card empty">${e.message}</div>`; return; }
  let st = await api("/status");
  let sum = null;
  try { sum = await api("/summary"); } catch (e) {}
  app.innerHTML = `
  <div class="hello">Load &amp; <span>analyse</span></div>
  <div class="sub">Bulk metadata in CSV, JSON or XML — timestamp, src/dst IP &amp; port, TXID, input/output wallets &amp; amounts, fee, script type.</div>
  <div class="grid">
    <div class="card c5"><h3>1 · Choose a dataset</h3><div id="dslist"></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap">
        <label class="btn ghost" style="display:inline-block">Upload file…<input type="file" id="up" accept=".csv,.json,.xml" hidden></label>
        <span class="muted">Known flagged wallets (seeds):</span>
        <select id="seeds">${ds.seeds.map(s => `<option ${s === "case_dataset_seeds.csv" ? "selected" : ""}>${s}</option>`).join("")}<option value="">none</option></select>
      </div>
      <div style="margin-top:18px"><button class="btn purple" id="run">Run analysis</button></div>
      <div class="err" id="err"></div></div>
    <div class="card c4"><h3>2 · Pipeline <small id="elapsed"></small></h3><div id="stages"></div>
      <div style="margin-top:12px;display:none" id="donebox"><a class="btn" href="#/overview">Open results ›</a></div></div>
    <div class="card c3"><h3>Data quality</h3><div id="dq"></div></div>
  </div>`;
  const renderDs = () => {
    $("#dslist").innerHTML = ds.datasets.map(d => `<div class="ds ${d.path === selDs ? "on" : ""}" data-p="${d.path}">
      <span><span class="fmt">${d.name.split(".").pop().toUpperCase()}</span>${d.name}${d.uploaded ? ' <span class="chip">uploaded</span>' : ""}</span>
      <span class="muted">${d.size_mb} MB</span></div>`).join("");
    app.querySelectorAll(".ds").forEach(el => el.onclick = () => { selDs = el.dataset.p; renderDs(); });
  };
  renderDs();
  const renderDq = (s) => {
    if (!s) { $("#dq").innerHTML = '<div class="muted">Run an analysis to see data quality.</div>'; return; }
    const q = s.quality, c = s.clustering;
    $("#dq").innerHTML = `<div class="bars">
      ${[["Rows read", q.rows_in], ["Duplicates removed", q.duplicates_removed], ["Missing fees filled", q.fees_filled], ["Invalid tx dropped", q.invalid_tx_dropped],
         ["H1 merges (multi-input)", c.h1_merges], ["H2 merges (change)", c.h2_merges], ["H3 rejections (reuse)", c.h3_rejections],
         ["Mixer tx filtered", c.coinjoin_tx], ["Service hubs found", c.service_hubs]]
        .map(([l, v]) => `<div class="row" style="grid-template-columns:1fr auto"><span>${l}</span><b style="font-weight:500">${fmt(v)}</b></div>`).join("")}</div>
      <div class="note">Dataset: ${s.dataset}</div>`;
  };
  renderDq(sum);
  const renderStages = (st) => {
    $("#stages").innerHTML = st.stages.map((n, i) => {
      let cls = "", ic = i + 1, tm = "";
      if (st.done || (st.stage > i)) { cls = "ok"; ic = "✓"; }
      else if (st.running && st.stage === i) { cls = "run"; ic = ""; }
      if (st.timings && st.timings[n] !== undefined && st.timings[n] !== null) tm = st.timings[n].toFixed(2) + " s";
      return `<div class="stage ${cls}"><span class="ic">${ic}</span>${n}<span class="tm">${tm}</span></div>`;
    }).join("");
    if (st.done) { $("#donebox").style.display = "block"; $("#elapsed").textContent = `done in ${st.elapsed} s`; }
    if (st.error) $("#err").textContent = st.error;
  };
  renderStages(st);
  $("#up").onchange = async (e) => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    try { const r = await api("/upload", { method: "POST", body: fd }); selDs = r.path; ds = await api("/datasets"); renderDs(); toast("Uploaded " + f.name); }
    catch (err) { $("#err").textContent = err.message; }
  };
  $("#run").onclick = async () => {
    $("#err").textContent = ""; $("#donebox").style.display = "none"; $("#elapsed").textContent = "";
    try {
      await api("/analyze", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dataset: selDs, seeds: $("#seeds").value }) });
    } catch (err) { $("#err").textContent = err.message; return; }
    $("#run").disabled = true;
    const t0 = Date.now();
    const poll = setInterval(async () => {
      const s = await api("/status");
      if (!s.done && !s.error) $("#elapsed").textContent = ((Date.now() - t0) / 1000).toFixed(1) + " s";
      renderStages(s);
      if (!s.running) {
        clearInterval(poll); $("#run").disabled = false;
        if (s.done) { try { renderDq(await api("/summary")); } catch (e) {} toast("Analysis complete"); }
      }
    }, 300);
  };
}

route();
