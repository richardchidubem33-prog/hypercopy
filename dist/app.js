/* HyperCopy dashboard */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  data: null,
  tier: "5-10k",
  window: "30D",
  sigFilter: "consensus",
  live: null,
  copy: null,
  copying: new Set(JSON.parse(localStorage.getItem("hc_copying") || "[]")),
};

const fmtUSD = (n) => {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a >= 1e9) return "$" + (n / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return "$" + (n / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
  return "$" + n.toFixed(a < 10 ? 2 : 0);
};
const fmtPct = (n, d = 1) => {
  n = Number(n) || 0;
  if (Math.abs(n) >= 10) return (n >= 0 ? "+" : "") + n.toFixed(1) + "×";
  return (n >= 0 ? "+" : "") + (n * 100).toFixed(d) + "%";
};
const fmtPctRaw = (n, d = 1) => (Number(n) * 100).toFixed(d) + "%";
const short = (a) => a ? a.slice(0, 6) + "…" + a.slice(-4) : "";
const initials = (a) => a ? a.slice(2, 4).toUpperCase() : "?";
const roiClass = (n) => (n >= 0 ? "up" : "down");
const wrClass = (w) => (w >= 0.7 ? "up" : w >= 0.6 ? "" : "down");
const wrColor = (w) => (w >= 0.7 ? "var(--green)" : w >= 0.6 ? "var(--amber)" : "var(--red)");
const levColor = (l) => (l > 5 ? "var(--red)" : "var(--text)");

function nameOf(t) {
  return t.displayName || "Anon Trader";
}

/* ---------------- init ---------------- */
let STATIC_MODE = false;
async function load() {
  let res;
  try {
    res = await fetch("/api/snapshot", { cache: "no-store" });
    if (!res.ok) throw new Error("no api");
  } catch (e) {
    STATIC_MODE = true;
    res = await fetch("snapshot.json", { cache: "no-store" });
  }
  if (!res.ok) throw new Error("snapshot " + res.status);
  state.data = await res.json();
  if (STATIC_MODE) {
    const b = $("#refresh-btn");
    b.textContent = "Snapshot mode";
    b.title = "Static snapshot — run server.py for live refresh";
    b.disabled = true;
    // Best-effort: load the last-known bot state captured at build time.
    try {
      const lr = await fetch("live.json", { cache: "no-store" });
      if (lr.ok) { state.live = await lr.json(); renderFeed(); }
    } catch (e) { /* ignore */ }
    try {
      const cr = await fetch("copy.json", { cache: "no-store" });
      if (cr.ok) { state.copy = await cr.json(); renderCopy(); }
    } catch (e) { /* ignore */ }
  }
  renderAll();
}

function renderAll() {
  const d = state.data;
  if (!d) return;
  renderStats();
  renderFunnel();
  renderTierTabs();
  renderWindowTabs();
  renderSignals();
  renderLeaderboard();
  renderTracked();
  $("#live-time").textContent = "Live · " + new Date(d.generatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderStats() {
  const m = state.data.meta;
  $("#stat-scanned").textContent = m.leaderboardSize.toLocaleString();
  $("#stat-enriched").textContent = m.enriched.toLocaleString();
  $("#stat-qualified").textContent = m.qualifiedCount.toLocaleString();
  $("#stat-cohort").textContent = m.signalCohort.toLocaleString();
}

function renderFunnel() {
  const m = state.data.meta;
  const rows = [
    ["Scanned", m.leaderboardSize, "var(--blue)"],
    ["Enriched", m.enriched, "var(--blue)"],
    ["Qualified ≥70%", m.qualifiedCount, "var(--green)"],
    ["Signal cohort", m.signalCohort, "var(--green)"],
  ];
  const max = m.leaderboardSize || 1;
  $("#funnel").innerHTML = rows.map(([lab, n, c]) => {
    const w = Math.max(4, (n / max) * 100);
    return `<div class="funnel"><span class="lab">${lab}</span>
      <span class="bar"><i style="width:${w}%;background:${c}"></i></span>
      <span class="num">${n.toLocaleString()}</span></div>`;
  }).join("");
}

function renderTierTabs() {
  const tiers = state.data.meta.tiers.map(t => t.name);
  $("#tier-tabs").innerHTML = tiers.map(t =>
    `<button class="tab ${t === state.tier ? "active" : ""}" data-tier="${t}">${t}</button>`
  ).join("");
  $$("#tier-tabs .tab").forEach(b => b.onclick = () => {
    state.tier = b.dataset.tier; renderTierTabs(); renderLeaderboard(); renderTracked();
  });
}

function renderWindowTabs() {
  const ws = state.data.meta.windows;
  $("#window-tabs").innerHTML = ws.map(w =>
    `<button class="tab ${w === state.window ? "active green" : ""}" data-w="${w}">${w}</button>`
  ).join("");
  $$("#window-tabs .tab").forEach(b => b.onclick = () => {
    state.window = b.dataset.w; renderWindowTabs(); renderLeaderboard(); renderTracked();
  });
}

/* ---------------- signals ---------------- */
function renderSignals() {
  const all = state.data.signals || [];
  const cons = all.filter(s => s.consensus);
  $("#cnt-consensus").textContent = cons.length;
  $("#cnt-all").textContent = all.length;
  $$("#sig-filter .tab").forEach(b => b.classList.toggle("active", b.dataset.f === state.sigFilter));
  const list = state.sigFilter === "consensus" ? cons : all;

  if (!list.length) { $("#signals").innerHTML = `<div class="empty">No signals in this view.</div>`; return; }

  $("#signals").innerHTML = list.map((s, i) => {
    const contested = s.alignment < 0.6;
    const cls = contested ? "contested" : s.bias.toLowerCase();
    const lp = (s.longCount / s.holders) * 100;
    const sp = (s.shortCount / s.holders) * 100;
    const detail = [
      ...s.longs.map(r => traderRow(r, "l")),
      ...s.shorts.map(r => traderRow(r, "s")),
    ].join("");
    return `<div class="sig ${cls}" data-i="${i}">
      <div class="sig-top">
        <span class="coin">${s.coin}</span>
        <span class="pill ${cls}">${contested ? "SPLIT" : s.bias}</span>
        <div class="conv"><div class="n" style="color:${contested ? "var(--amber)" : s.bias === "LONG" ? "var(--green)" : "var(--red)"}">${s.conviction}</div><div class="l">conviction</div></div>
      </div>
      <div class="alignbar">
        <span class="l" style="width:${lp}%"></span>
        <span class="s" style="width:${sp}%"></span>
      </div>
      <div class="alignmeta"><span class="l">${s.longCount} long</span><span class="s">${s.shortCount} short</span></div>
      <div class="sig-foot">
        <span><b>${s.holders}</b> / ${s.activeTracked} active traders aligned</span>
        <span><b>${fmtUSD(s.grossNotional)}</b> deployed</span>
      </div>
      <div class="sig-detail">${detail}</div>
    </div>`;
  }).join("");

  $$("#signals .sig").forEach(el => el.onclick = () => el.classList.toggle("open"));
}

function traderRow(r, side) {
  return `<div class="trow">
    <span class="sd ${side}"></span>
    <span class="ad">${short(r.address)}</span>
    ${r.qualified ? '<span class="tag">70%+</span>' : ""}
    <span class="nt">${fmtUSD(r.notional)}</span>
    <span class="wr" style="color:${wrColor(r.winRate)}">${fmtPctRaw(r.winRate, 0)}</span>
  </div>`;
}

/* ---------------- leaderboard ---------------- */
function renderLeaderboard() {
  const tw = state.data.tiers[state.tier]?.[state.window];
  const rows = tw?.table || [];
  if (!rows.length) { $("#lb-body").innerHTML = `<tr><td colspan="9"><div class="empty">No traders in this tier.</div></td></tr>`; return; }
  $("#lb-body").innerHTML = rows.map((t, i) => {
    const status = t.passes
      ? '<span class="badge pass">Qualified</span>'
      : t.winRate >= 0.6
        ? '<span class="badge watch">Watch</span>'
        : '<span class="badge fail">Fail</span>';
    const act = t.active ? '<span class="badge act">Active</span>' : '<span class="badge flat">Flat</span>';
    const lev = t.maxTradeRatio > 0 ? t.maxTradeRatio.toFixed(1) + "×" : "—";
    return `<tr data-addr="${t.address}">
      <td class="rank">${i + 1}</td>
      <td class="l"><div class="who"><div class="av">${initials(t.address)}</div>
        <div><div class="nm">${nameOf(t)}</div><div class="ad">${short(t.address)}</div></div></div></td>
      <td>${fmtUSD(t.accountValue)}</td>
      <td class="${roiClass(t.roi)}">${fmtPct(t.roi)}</td>
      <td class="${roiClass(t.pnl)}">${fmtUSD(t.pnl)}</td>
      <td class="muted">${fmtUSD(t.volume)}</td>
      <td style="color:${wrColor(t.winRate)}">${fmtPctRaw(t.winRate)}</td>
      <td style="color:${levColor(t.maxTradeRatio)}">${lev}</td>
      <td>${status} ${act}</td>
    </tr>`;
  }).join("");
  $$("#lb-body tr").forEach(tr => tr.onclick = () => openModal(tr.dataset.addr));
}

/* ---------------- tracked ---------------- */
function renderTracked() {
  const tw = state.data.tiers[state.tier]?.[state.window];
  const list = tw?.tracked || [];
  if (!list.length) { $("#tracked").innerHTML = `<div class="empty">No qualifying wallets in ${state.tier} · ${state.window} right now.</div>`; return; }
  $("#tracked").innerHTML = list.map(t => {
    const q = t.qualified ? '<span class="badge pass">Qualified</span>' : '<span class="badge watch">Watch</span>';
    const a = t.active ? '<span class="badge act">Active</span>' : '<span class="badge flat">Flat</span>';
    const pos = (t.positions || []).slice(0, 7).map(p =>
      `<span class="pos-chip ${p.side === "long" ? "l" : "s"}">${p.coin} ${p.side === "long" ? "L" : "S"}</span>`
    ).join("") || '<span class="muted" style="font-size:11px">No open positions</span>';
    const more = (t.positions || []).length > 7 ? `<span class="pos-chip">+${t.positions.length - 7}</span>` : "";
    const on = state.copying.has(t.address);
    return `<div class="tcard">
      <div class="th"><div class="av">${initials(t.address)}</div>
        <div><div class="nm">${nameOf(t)}</div><div class="ad">${short(t.address)}</div></div>
        <div class="st">${q}${a}</div></div>
      <div class="tstats">
        <div class="tstat2"><div class="k">ROI ${state.window}</div><div class="v ${roiClass(t.roi)}">${fmtPct(t.roi)}</div></div>
        <div class="tstat2"><div class="k">Win rate</div><div class="v" style="color:${wrColor(t.winRate)}">${fmtPctRaw(t.winRate)}</div></div>
        <div class="tstat2"><div class="k">Max trade</div><div class="v" style="color:${levColor(t.maxTradeRatio)}">${t.maxTradeRatio ? t.maxTradeRatio.toFixed(1) + "×" : "—"}</div></div>
      </div>
      <div class="tpos">${pos}${more}</div>
      <button class="btn copybtn ${on ? "on" : ""}" data-addr="${t.address}">${on ? "✓ Copying" : "＋ Copy trader"}</button>
    </div>`;
  }).join("");
  $$("#tracked .copybtn").forEach(b => b.onclick = async (e) => {
    e.stopPropagation();
    const a = b.dataset.addr;
    const t = (state.data.traders || {})[a] || {};
    const was = state.copying.has(a);
    // optimistic UI
    if (was) state.copying.delete(a); else state.copying.add(a);
    localStorage.setItem("hc_copying", JSON.stringify([...state.copying]));
    renderTracked();
    try {
      const r = await fetch("/api/watchlist", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          address: a, action: was ? "remove" : "add",
          tier: state.tier, name: t.displayName || null,
          winRate: t.winRate, qualified: t.qualified,
        }),
      });
      if (!r.ok) throw new Error("watchlist " + r.status);
      const j = await r.json();
      // reconcile with server truth
      if (j.watching) state.copying.add(a); else state.copying.delete(a);
      localStorage.setItem("hc_copying", JSON.stringify([...state.copying]));
      renderTracked();
    } catch (err) {
      console.error(err);
      // roll back on failure
      if (was) state.copying.add(a); else state.copying.delete(a);
      localStorage.setItem("hc_copying", JSON.stringify([...state.copying]));
      renderTracked();
    }
  });
}

/* ---------------- modal ---------------- */
function openModal(addr) {
  const t = state.data.traders[addr];
  const ov = $("#overlay");
  if (!t) { return; }
  const status = t.passes ? '<span class="badge pass">Qualified</span>' : t.winRate >= 0.6 ? '<span class="badge watch">Watch</span>' : '<span class="badge fail">Fail</span>';
  $("#modal-h").innerHTML = `<div class="av">${initials(addr)}</div>
    <div><h3>${t.displayName || short(addr)}</h3><div class="ad">${addr}</div></div>
    <div style="margin-left:14px">${status}</div>
    <div class="x" id="modal-x">✕</div>`;
  const pos = (t.positions || []).map(p => `<div class="mpos-row">
      <span class="coin">${p.coin}</span>
      <span class="sd ${p.side === "long" ? "l" : "s"}">${p.side.toUpperCase()}</span>
      <span class="muted" style="font-size:11px">${p.leverage ? p.leverage + "×" : ""}</span>
      <span class="nt">${fmtUSD(p.positionValue)}</span>
      <span class="up ${p.unrealizedPnl >= 0 ? "up" : "down"}">${p.unrealizedPnl >= 0 ? "+" : ""}${fmtUSD(p.unrealizedPnl)}</span>
    </div>`).join("") || '<div class="empty">No open positions.</div>';
  $("#modal-b").innerHTML = `
    <div class="mstats">
      <div class="mstat"><div class="k">Account</div><div class="v">${fmtUSD(t.accountValue)}</div></div>
      <div class="mstat"><div class="k">Win rate</div><div class="v" style="color:${wrColor(t.winRate)}">${fmtPctRaw(t.winRate)}</div></div>
      <div class="mstat"><div class="k">Max trade</div><div class="v" style="color:${levColor(t.maxTradeRatio)}">${t.maxTradeRatio ? t.maxTradeRatio.toFixed(1) + "×" : "—"}</div></div>
      <div class="mstat"><div class="k">Closed trades</div><div class="v">${t.closes}</div></div>
    </div>
    <div class="mstats" style="grid-template-columns:repeat(3,1fr)">
      <div class="mstat"><div class="k">ROI 30D</div><div class="v ${roiClass(t.roi30)}">${fmtPct(t.roi30)}</div></div>
      <div class="mstat"><div class="k">ROI 90D</div><div class="v ${roiClass(t.roi90)}">${fmtPct(t.roi90)}</div></div>
      <div class="mstat"><div class="k">ROI 1Y</div><div class="v ${roiClass(t.roi1y)}">${fmtPct(t.roi1y)}</div></div>
    </div>
    <div class="mpos"><h4>Open positions (${(t.positions || []).length})</h4>${pos}</div>`;
  ov.classList.add("on");
  $("#modal-x").onclick = closeModal;
}
function closeModal() { $("#overlay").classList.remove("on"); }
$("#overlay").addEventListener("click", (e) => { if (e.target.id === "overlay") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ---------------- signal filter ---------------- */
$$("#sig-filter .tab").forEach(b => b.onclick = () => { state.sigFilter = b.dataset.f; renderSignals(); });

/* ---------------- refresh ---------------- */
$("#refresh-btn").onclick = async () => {
  const btn = $("#refresh-btn");
  btn.disabled = true; const old = btn.innerHTML; btn.innerHTML = "↻ Rebuilding…";
  try {
    await fetch("/api/refresh", { method: "POST" });
    const start = Date.now();
    const prev = state.data?.generatedAt;
    while (Date.now() - start < 240000) {
      await new Promise(r => setTimeout(r, 4000));
      const res = await fetch("/api/snapshot", { cache: "no-store" });
      const d = await res.json();
      if (d.generatedAt !== prev) { state.data = d; renderAll(); break; }
    }
  } catch (e) { console.error(e); }
  btn.disabled = false; btn.innerHTML = old;
};

/* ================= live feed ================= */
const ago = (ts) => {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};

function renderFeed() {
  const d = state.live;
  if (!d) return;
  const st = d.status || {};
  const dot = $("#feed-dot");
  dot.className = "feeddot " + (st.running ? "on" : "off");
  dot.title = st.running ? "Feed running" : "Feed stopped";

  $("#feed-meta").innerHTML = `
    <span class="fm"><b>${d.wallets || 0}</b> wallets</span>
    <span class="fm"><b>${d.activeWallets || 0}</b> active</span>
    <span class="fm"><b>${fmtUSD(d.equityTracked)}</b> tracked</span>
    <span class="fm">poll <b>${d.pollSeconds || 15}s</b></span>
    <span class="fm">cycle <b>${st.cycles || 0}</b>${st.errors ? ` · <b style="color:var(--red)">${st.errors} err</b>` : ""}</span>`;

  const evs = (d.events || []).slice(0, 40);
  if (!evs.length) {
    $("#feedlist").innerHTML = `<div class="empty">Feed is live and watching ${d.wallets || 0} wallets. Waiting for the next trade event\u2026</div>`;
    return;
  }
  $("#feedlist").innerHTML = evs.map((e, i) => {
    const kind = (e.kind || "OPEN").toUpperCase();
    const side = (e.side || "").toLowerCase();
    const who = e.name || (e.address ? short(e.address) : "");
    return `<div class="fe ${i === 0 ? "new" : ""}">
      <span class="t">${ago(e.ts)}</span>
      <span class="k ${kind}">${kind}</span>
      <span class="coin">${e.coin || ""}</span>
      <span class="sd ${side}">${side === "long" ? "LONG" : side === "short" ? "SHORT" : "\u2014"}</span>
      <span class="nt">${fmtUSD(e.notional)}</span>
      <span class="who">${who}</span>
    </div>`;
  }).join("");
}

async function loadLive() {
  try {
    const r = await fetch("/api/live", { cache: "no-store" });
    if (!r.ok) throw new Error("live " + r.status);
    state.live = await r.json();
    renderFeed();
  } catch (e) { /* keep last known */ }
}

/* ================= copy engine ================= */
function sparkline(curve) {
  if (!curve || curve.length < 2) {
    return `<div class="empty" style="padding:24px 0;text-align:center">Collecting equity ticks\u2026</div>`;
  }
  const vals = curve.map(p => p.equity);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || Math.max(1, max * 0.0005);
  const W = 600, H = 120, pad = 4;
  const x = (i) => pad + (i / (vals.length - 1)) * (W - pad * 2);
  const y = (v) => H - pad - ((v - min) / span) * (H - pad * 2);
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const up = vals[vals.length - 1] >= vals[0];
  const col = up ? "var(--green)" : "var(--red)";
  const base = y(vals[0]).toFixed(1);
  const area = `${pad},${H - pad} ${pts} ${W - pad},${H - pad}`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${up ? "#16e0a3" : "#ff4d6d"}" stop-opacity=".22"/>
      <stop offset="100%" stop-color="${up ? "#16e0a3" : "#ff4d6d"}" stop-opacity="0"/>
    </linearGradient></defs>
    <line x1="${pad}" y1="${base}" x2="${W - pad}" y2="${base}" stroke="var(--border)" stroke-dasharray="3 4"/>
    <polygon points="${area}" fill="url(#sg)"/>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

function renderCopy() {
  const d = state.copy;
  if (!d) return;
  const mode = (d.mode || "paper").toLowerCase();
  const badge = $("#mode-badge");
  badge.textContent = mode.toUpperCase();
  badge.className = "mode-badge " + (mode === "live" ? "live" : "paper");

  const pos = Object.values(d.positions || {});
  const upnl = Number(d.unrealizedPnl || 0);
  const openNotional = Number(d.openNotional || 0);
  const realized = Number(d.realizedPnl || 0);
  const equity = Number(d.equity || 0);
  const startEq = (d.equityCurve && d.equityCurve[0]?.equity) || equity;
  const ret = startEq ? (equity - startEq) / startEq : 0;

  $("#engine-stats").innerHTML = `
    <div class="es"><div class="k">Equity</div><div class="v">${fmtUSD(equity)}</div></div>
    <div class="es"><div class="k">Unrealized</div><div class="v ${upnl >= 0 ? "up" : "down"}">${upnl >= 0 ? "+" : ""}${fmtUSD(upnl)}</div></div>
    <div class="es"><div class="k">Realized</div><div class="v ${realized >= 0 ? "up" : "down"}">${realized >= 0 ? "+" : ""}${fmtUSD(realized)}</div></div>
    <div class="es"><div class="k">Open notional</div><div class="v">${fmtUSD(openNotional)}</div></div>
    <div class="es"><div class="k">Positions</div><div class="v">${pos.length}</div></div>
    <div class="es"><div class="k">Orders</div><div class="v">${(d.orders || []).length}</div></div>`;

  $("#eng-equity").innerHTML = `${fmtUSD(equity)} <span class="${ret >= 0 ? "up" : "down"}" style="font-size:12px;font-weight:700">${ret >= 0 ? "+" : ""}${(ret * 100).toFixed(2)}%</span>`;
  $("#spark").innerHTML = sparkline(d.equityCurve || []);
  $("#eng-sub").innerHTML = `
    <span>Cash <b style="color:var(--text)">${fmtUSD(d.cash)}</b></span>
    <span>Exposure <b style="color:var(--text)">${equity ? ((openNotional / equity) * 100).toFixed(0) : 0}%</b></span>
    <span>Updated <b style="color:var(--text)">${d.updatedAt ? ago(d.updatedAt) + " ago" : "\u2014"}</b></span>`;

  $("#eng-poscount").textContent = pos.length;
  if (!pos.length) {
    $("#eng-positions").innerHTML = `<div class="empty">No open positions. The engine opens trades when a consensus signal clears its conviction threshold.</div>`;
  } else {
    $("#eng-positions").innerHTML = pos.map(p => {
      const pl = Number(p.upnl || 0);
      return `<div class="epos">
        <span class="coin">${p.coin}</span>
        <span class="sd ${p.side}">${p.side === "long" ? "LONG" : "SHORT"}</span>
        <span class="nt">${fmtUSD(p.notional)}</span>
        <span class="pl ${pl >= 0 ? "up" : "down"}">${pl >= 0 ? "+" : ""}${fmtUSD(pl)}</span>
      </div>`;
    }).join("");
  }

  const orders = (d.orders || []).slice(0, 40);
  if (!orders.length) {
    $("#orderbody").innerHTML = `<tr><td colspan="8" class="empty">No orders yet.</td></tr>`;
  } else {
    $("#orderbody").innerHTML = orders.map(o => {
      const act = (o.action || "").toUpperCase();
      const pnl = Number(o.pnl || 0);
      const hasPnl = o.pnl !== undefined && o.pnl !== null;
      return `<tr>
        <td class="l" style="font-family:var(--mono);font-size:10.5px;color:var(--muted2)">${new Date(o.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
        <td class="l"><span class="act ${act}">${act}</span></td>
        <td class="l" style="font-weight:700">${o.coin || ""} <span class="muted" style="font-size:10px">${o.side || ""}</span></td>
        <td style="font-family:var(--mono)">${o.qty != null ? Number(o.qty).toPrecision(5) : "\u2014"}</td>
        <td style="font-family:var(--mono)">${o.px != null ? "$" + Number(o.px).toPrecision(5) : "\u2014"}</td>
        <td style="font-family:var(--mono)">${o.notional != null ? fmtUSD(o.notional) : "\u2014"}</td>
        <td class="${hasPnl ? (pnl >= 0 ? "up" : "down") : ""}" style="font-family:var(--mono)">${hasPnl ? (pnl >= 0 ? "+" : "") + fmtUSD(pnl) : "\u2014"}</td>
        <td class="l muted" style="font-size:10.5px">${o.why || ""}</td>
      </tr>`;
    }).join("");
  }
}

async function loadCopy() {
  try {
    const r = await fetch("/api/copy", { cache: "no-store" });
    if (!r.ok) throw new Error("copy " + r.status);
    state.copy = await r.json();
    renderCopy();
  } catch (e) { /* keep last known */ }
}

/* ================= polling ================= */
async function loadWatchlist() {
  try {
    const r = await fetch("/api/watchlist", { cache: "no-store" });
    if (!r.ok) return;
    const j = await r.json();
    const addrs = new Set((j.wallets || []).map(w => (w.address || "").toLowerCase()));
    state.copying = addrs;
    localStorage.setItem("hc_copying", JSON.stringify([...addrs]));
    if (state.data) renderTracked();
  } catch (e) { /* ignore */ }
}

function startPolling() {
  loadWatchlist();
  loadLive();
  loadCopy();
  setInterval(loadLive, 5000);
  setInterval(loadCopy, 4000);
  setInterval(loadWatchlist, 30000);
}

/* ---------------- boot ---------------- */
load().then(() => {
  if (STATIC_MODE) {
    // No backend: panels show the last-known state captured at build time.
    // If neither file existed, hide the shells rather than show empty boxes.
    if (!state.live) { const fs = $("#feedlist")?.closest("section"); if (fs) fs.style.display = "none"; }
    if (!state.copy) { const cs = $("#orderbody")?.closest("section"); if (cs) cs.style.display = "none"; }
    return;
  }
  startPolling();
}).catch(err => {
  console.error(err);
  $("#signals").innerHTML = `<div class="empty">Could not load snapshot: ${err.message}</div>`;
});

/* reset paper account */
const resetBtn = $("#btn-reset");
if (resetBtn) resetBtn.onclick = async () => {
  if (!confirm("Reset the paper account to starting equity and clear all positions?")) return;
  resetBtn.disabled = true;
  try {
    await fetch("/api/copy/reset", { method: "POST" });
    await loadCopy();
  } catch (e) { console.error(e); }
  resetBtn.disabled = false;
};
