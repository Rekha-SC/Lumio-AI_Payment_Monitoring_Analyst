/* Lumio live demo — plain JavaScript, no build step. Polls the API once a second. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const C = { ink: "#14161A", teal: "#0F766E", amb: "#D97706", muted: "#5A6068", line: "#D6D8DA", grey: "#8A9098", red: "#B42318" };
const S = { state: null, tab: "live", scope: {}, scopeAuto: true, lastTime: null, lastEventId: 0, seenInc: new Set(), charts: {},
            drawerId: null, busy: false, outSel: null, daySel: null, settings: null };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  if (!r.ok) { let m = await r.text(); try { m = JSON.parse(m).detail || m; } catch (e) {} throw new Error(m); }
  return r.json();
}
const post = (p, b) => api(p, { method: "POST", body: JSON.stringify(b || {}) });
const put = (p, b) => api(p, { method: "PUT", body: JSON.stringify(b || {}) });
function toast(msg, ms = 3400) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), ms); }
function shellify(root = document) { $$(".card", root).forEach(c => { if (!c.parentElement.classList.contains("shell")) { const s = document.createElement("div"); s.className = "shell"; c.replaceWith(s); s.appendChild(c); } }); }
function card(inner, cls = "") { return `<div class="shell"><div class="card ${cls}">${inner}</div></div>`; }
function kpi(k, v, s, cls = "") { return card(`<div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div>`, "kpi " + cls); }
const money = v => v >= 1e6 ? "$" + (v / 1e6).toFixed(1) + "M" : v >= 1e4 ? "$" + Math.round(v / 1e3) + "K" : "$" + Math.round(v).toLocaleString();

/* ------------------------------------------------------------------ chime for new alerts */
let actx = null;
function chime() { try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); const t = actx.currentTime;
  [660, 880].forEach((f, i) => { const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = f; o.type = "sine";
    g.gain.setValueAtTime(0, t + i * .14); g.gain.linearRampToValueAtTime(.07, t + i * .14 + .02); g.gain.exponentialRampToValueAtTime(.0001, t + i * .14 + .5);
    o.connect(g).connect(actx.destination); o.start(t + i * .14); o.stop(t + i * .14 + .55); }); } catch (e) {} }

/* ------------------------------------------------------------------ top bar */
function renderTop(st) {
  const d = st.day;
  $("#clockDate").innerHTML = `${esc(st.date)} 2025 · day ${d.index} of ${d.of} · ` +
    (d.business ? `<b class="bd">business day ${d.business_day} of 6</b>` : `<b class="we">weekend</b>`) + (d.payday ? ` · <b class="pd">payday</b>` : "");
  $("#clockTime").textContent = st.time;
  const lb = $("#liveBadge"); lb.classList.toggle("on", st.running);
  $("#liveBadge span").textContent = st.ended ? "DATASET COMPLETE" : st.turbo_until ? "FAST-FORWARD" : st.running ? "LIVE" : "PAUSED";
  $("#turbo").textContent = st.turbo_until ? `→ ${st.turbo_until}` : "";
  $("#playIcon").textContent = st.running ? "❚❚" : "▶"; $("#playLabel").textContent = st.running ? "Pause" : "Play";
  if (!$("#speedSeg").children.length) $("#speedSeg").innerHTML = st.speeds.map(s => `<button data-speed="${s}">${s}×</button>`).join("");
  $$("#speedSeg button").forEach(b => b.classList.toggle("on", +b.dataset.speed === +st.speed));
  $("#agentMode").textContent = st.agent_mode === "claude" ? "AI agent: Claude" : "AI agent: offline planner";
  $("#scanInfo").textContent = st.next_scan ? `scan ${st.scans} · next ${st.next_scan}` : "—";
  const e = st.email, ep = $("#emailPill");
  ep.className = "pill clickable " + (e.enabled && e.configured ? "ok" : "warn");
  ep.textContent = e.enabled && e.configured ? `✉ email on · ${e.sent} sent${e.failed ? ` · ${e.failed} failed` : ""}` : `✉ email off — set up`;
  const open = st.incidents.filter(i => i.status === "open" && i.kind === "incident").length;
  $("#incCount").textContent = open || "";
  $("#mailCount").textContent = (e.sent + e.preview + e.queued + e.failed) || "";
  // 10-day timeline
  const tl = $("#timeline"); let h = `<div class="tl-fill" style="width:${(st.progress * 100).toFixed(2)}%"></div>`;
  st.days.forEach((dd, i) => { const left = i * 10; h += `<span class="tl-day ${dd.business ? "" : "we"}" style="left:${left}%;width:10%">${dd.label.slice(0, 3)}</span>`; });
  const t0 = new Date("2025-02-07T00:00:00").getTime(), span = 10 * 864e5;
  st.incidents.filter(i => i.kind === "incident").forEach(i => { const dt = new Date(`2025-02-${i.opened_day.slice(-2)}T${i.opened}:00`).getTime();
    h += `<span class="tl-mark alert" style="left:${((dt - t0) / span * 100).toFixed(2)}%" title="#${i.id} ${esc(i.headline)}"></span>`; });
  tl.innerHTML = h;
}
$("#speedSeg").addEventListener("click", async e => { const b = e.target.closest("button"); if (!b) return; await post("/api/control", { action: "speed", value: +b.dataset.speed }); tick(true); });
$("#playBtn").onclick = async () => { if (!S.state) return;
  if (S.state.ended) { toast("All 10 days are done — press Reset to replay them."); return; }
  await post("/api/control", { action: S.state.running ? "pause" : "play" }); tick(true); };
$("#resetBtn").onclick = async () => {
  if (!confirm("Start the 10 days again from Friday 7 February 00:00? Incidents, emails and learning from this run are cleared.")) return;
  await post("/api/control", { action: "reset" }); S.lastEventId = 0; S.seenInc.clear(); S.scope = {}; S.scopeAuto = true; closeDrawer(); toast("Reset to Friday 7 February 2025, 00:00"); tick(true); };
$("#jumpBtn").onclick = e => { e.stopPropagation(); const m = $("#jumpMenu"); if (!S.state) return;
  m.innerHTML = `<button data-next="1"><b>Next anomaly</b><small>skip to 15 min before it</small></button>` +
    S.state.days.map(d => `<button data-to="${d.date}T08:00"><b>${d.label} · 08:00</b><small>${d.business ? "business day " + d.business_day : "weekend"}${d.payday ? " · payday" : ""}</small></button>`).join("");
  m.classList.toggle("on"); };
document.addEventListener("click", () => $("#jumpMenu").classList.remove("on"));
$("#jumpMenu").addEventListener("click", async e => { const b = e.target.closest("button"); if (!b) return;
  try { if (b.dataset.next) { await post("/api/control", { action: "next" }); toast("Skipping ahead to the next anomaly…"); }
        else { await post("/api/control", { action: "jump", to: b.dataset.to }); toast("Fast-forwarding — Lumio processes every scan on the way"); } }
  catch (err) { toast(err.message); } tick(true); });
document.addEventListener("keydown", e => { if (e.code === "Space" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) { e.preventDefault(); $("#playBtn").click(); }
  if (e.key === "Escape") { closeDrawer(); closeInject(); } });
$("#emailPill").onclick = () => showTab("settings");

/* ------------------------------------------------------------------ tabs */
function showTab(t) { S.tab = t; $$("#tabs button").forEach(x => x.classList.toggle("on", x.dataset.tab === t)); $$(".tab").forEach(x => x.classList.toggle("on", x.id === "tab-" + t)); refreshTab(true); }
$$("#tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));

/* ------------------------------------------------------------------ KPIs */
function renderKpis(st) {
  const k = st.kpi || {}, open = st.incidents.filter(i => i.status === "open" && i.kind === "incident").length;
  const slow = k.p95_s && k.exp_p95_s && k.p95_s > k.exp_p95_s * 1.15, low = k.success && k.exp_success && k.success < k.exp_success - 0.8;
  const vr = k.normal_per_min ? (k.payments_per_min / k.normal_per_min - 1) * 100 : 0;
  const teams = new Set(st.incidents.flatMap(i => i.teams));
  $("#kpis").innerHTML =
    kpi("Payments / min", k.payments_per_min ? k.payments_per_min.toLocaleString() : "—", k.normal_per_min ? `normal ${k.normal_per_min.toLocaleString()} (${vr >= 0 ? "+" : ""}${vr.toFixed(0)}%)` : "", Math.abs(vr) > 30 ? "warn" : "") +
    kpi("Success rate", k.success ? k.success.toFixed(2) + "%" : "—", k.exp_success ? `normal ${k.exp_success.toFixed(2)}%` : "", low ? "warn" : "") +
    kpi("Payment time (p95)", k.p95_s ? k.p95_s.toFixed(2) + " s" : "—", k.exp_p95_s ? `Lumio expects ${k.exp_p95_s.toFixed(2)} s` : "", slow ? "warn" : "") +
    kpi("Open incidents", open, `${st.incidents.filter(i => i.kind === "incident").length} in ${st.day.index} day${st.day.index > 1 ? "s" : ""}`, open ? "warn" : "good") +
    kpi("Emails routed", (st.email.sent + st.email.preview + st.email.queued).toLocaleString(), `to ${teams.size} teams · ${st.email.enabled ? st.email.sent + " delivered" : "preview mode"}`, "good") +
    kpi("Pages avoided", st.naive_pages.toLocaleString(), "a fixed-threshold rule would have fired", "good");
}

/* ------------------------------------------------------------------ charts */
const markPlugin = { id: "marks", afterDatasetsDraw(chart, _a, opts) {
  const marks = opts.marks || [], labels = chart.data.labels, { ctx, chartArea: a } = chart, x = chart.scales.x; ctx.save();
  marks.forEach((m, i) => { const idx = labels.indexOf(m.t); if (idx < 0) return; const px = x.getPixelForValue(idx);
    const col = m.kind === "alert" ? C.amb : m.kind === "release" ? C.ink : C.grey;
    ctx.strokeStyle = col; ctx.lineWidth = m.kind === "alert" ? 2 : 1.2; ctx.setLineDash(m.kind === "start" ? [3, 3] : []);
    ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.setLineDash([]);
    if (opts.labels) { ctx.fillStyle = col; ctx.font = "600 11px Inter"; const w = ctx.measureText(m.label).width; ctx.fillText(m.label, Math.min(px + 4, a.right - w - 2), a.top + 12 + (i % 3) * 13); } });
  ctx.restore(); } };
function lineChart(canvas, unit, labels = true) {
  return new Chart(canvas, { type: "line", data: { labels: [], datasets: [] }, plugins: [markPlugin],
    options: { animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, marks: { marks: [], labels },
        tooltip: { backgroundColor: C.ink, filter: it => !["lo", "hi"].includes(it.dataset.key),
          callbacks: { label: it => ` ${it.dataset.label}: ${unit === "s" ? it.parsed.y?.toFixed(2) + " s" : unit === "%" ? it.parsed.y?.toFixed(2) + "%" : Math.round(it.parsed.y).toLocaleString()}` } } },
      scales: { x: { grid: { display: false }, ticks: { font: { family: "Inter", size: 11 }, color: C.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
                y: { grid: { color: "#EEF0F1" }, border: { display: false }, ticks: { font: { family: "Inter", size: 11 }, color: C.muted, maxTicksLimit: 6,
                     callback: v => unit === "s" ? v + " s" : unit === "%" ? v + "%" : v >= 1000 ? (v / 1000) + "k" : v } } } } });
}
function setSeries(chart, pts, kind, marks) {
  const L = pts.map(p => p.t), ds = [];
  if (kind === "lat") {
    ds.push({ key: "lo", label: "lo", data: pts.map(p => p.lo), borderWidth: 0, pointRadius: 0, fill: false });
    ds.push({ key: "hi", label: "hi", data: pts.map(p => p.hi), borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: "rgba(15,118,110,.13)" });
    ds.push({ key: "alert", label: "Alert threshold", data: pts.map(p => p.alert), borderColor: C.amb, borderDash: [2, 3], borderWidth: 1.4, pointRadius: 0 });
    ds.push({ key: "exp", label: "Expected", data: pts.map(p => p.exp), borderColor: C.grey, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0 });
    ds.push({ key: "act", label: "Actual", data: pts.map(p => p.p95), borderColor: C.ink, borderWidth: 2.4, pointRadius: 0, tension: .25,
      segment: { borderColor: c => (pts[c.p1DataIndex] && pts[c.p1DataIndex].p95 > pts[c.p1DataIndex].alert) ? C.amb : C.ink } });
  } else if (kind === "suc") {
    ds.push({ key: "exp", label: "Expected", data: pts.map(p => p.exp_succ), borderColor: C.grey, borderDash: [5, 4], borderWidth: 1.3, pointRadius: 0 });
    ds.push({ key: "act", label: "Actual", data: pts.map(p => p.succ), borderColor: C.teal, borderWidth: 2, pointRadius: 0, tension: .25,
      segment: { borderColor: c => (pts[c.p1DataIndex] && pts[c.p1DataIndex].succ < pts[c.p1DataIndex].exp_succ - 1.5) ? C.amb : C.teal } });
  } else {
    ds.push({ key: "exp", label: "Expected", data: pts.map(p => p.exp_vol), borderColor: C.grey, borderDash: [5, 4], borderWidth: 1.3, pointRadius: 0 });
    ds.push({ key: "act", label: "Actual", data: pts.map(p => p.vol), borderColor: C.ink, borderWidth: 1.8, pointRadius: 0, tension: .25, fill: false,
      segment: { borderColor: c => { const p = pts[c.p1DataIndex]; return p && p.exp_vol && Math.abs(p.vol / p.exp_vol - 1) > .35 ? C.amb : C.ink; } } });
  }
  chart.data.labels = L; chart.data.datasets = ds; chart.options.plugins.marks.marks = marks || []; chart.update("none");
}
async function loadSeries() {
  const d = await api("/api/series?" + new URLSearchParams(S.scope).toString());
  if (!S.charts.lat) { S.charts.lat = lineChart($("#latChart"), "s"); S.charts.suc = lineChart($("#sucChart"), "%", false); S.charts.vol = lineChart($("#volChart"), "n", false); }
  setSeries(S.charts.lat, d.points, "lat", d.marks); setSeries(S.charts.suc, d.points, "suc", d.marks); setSeries(S.charts.vol, d.points, "vol", d.marks);
  $("#chartTitle").innerHTML = "Payment time · " + esc(d.label) + ` <small>${d.segments} segment${d.segments > 1 ? "s" : ""}</small>`;
  $("#scopeAll").style.visibility = Object.keys(S.scope).length ? "visible" : "hidden";
}
$("#scopeAll").onclick = () => { S.scope = {}; S.scopeAuto = true; loadSeries(); renderHeat(S.state); };
function setScope(scope) { S.scope = { ...scope }; S.scopeAuto = false; loadSeries(); if (S.state) renderHeat(S.state); }

/* ------------------------------------------------------------------ alerts */
function tilesHtml(t) { return `<div class="tiles">${(t || []).map(x => `<div class="tile"><div class="l">${esc(x.label)}</div><div class="v">${esc(x.value)}</div><div class="u">${esc(x.sub)}</div></div>`).join("")}</div>`; }
function whoBadge(i) { return i.explained_by === "agent" ? `<span class="badge agent">AI agent</span>` : i.explained_by === "script" ? `<span class="badge script">scripted checks</span>` : ""; }
function headLabel(i) {
  if (i.kind === "advisory") return i.status === "resolved" ? `Advisory · closed ${i.resolved || ""}` : "Advisory · business information";
  if (i.kind === "data_quality") return i.status === "resolved" ? `Data ticket · closed ${i.resolved || ""}` : "Data-quality ticket";
  if (i.severity === "pending") return "Investigating…";
  return i.status === "resolved" ? `Resolved ${i.resolved || ""}` : `${i.severity} severity`;
}
function ctaButtons(i, big = false) {
  if (i.status !== "open" || i.severity === "pending") return "";
  const cls = big ? "btn" : "btn small";
  let h = (i.ctas || []).filter(c => c.action !== "ack").map(c => `<button class="${cls} ${c.style === "danger" ? "amber" : "primary"}" data-act="${c.action}" data-id="${i.id}">${esc(c.label)}</button>`).join("");
  if (!i.acked) h += `<button class="${cls} ${h ? "" : "primary"}" data-act="ack" data-id="${i.id}">Acknowledge</button>`;
  return h;
}
function alertCard(i) {
  const cls = i.kind === "advisory" ? "info" : i.kind === "data_quality" ? "dq" : i.severity === "pending" ? "pending" : i.status === "resolved" ? "resolved" : i.severity;
  const status = i.acked ? `<span class="badge ok">✓ ${esc(i.acked_by)} · ${i.acked}</span>` : i.escalated ? `<span class="badge esc">escalated</span>` : "";
  const acts = (i.actions || []).slice(-1).map(a => `<div class="did">▶ ${esc(a.label)} — ${esc(a.by)}</div>`).join("");
  return `<div class="ac ${cls}" data-open="${i.id}"><div class="ac-h"><span>Lumio · ${esc(headLabel(i))}</span><span>${i.opened_day || ""} ${i.opened || ""} · INC-${String(i.id).padStart(4, "0")}</span></div>
    <div class="ac-b"><div class="ac-t">${esc(i.headline)}</div>${i.summary ? `<div class="ac-s">${esc(i.summary)}</div>` : ""}${tilesHtml(i.tiles)}
    ${i.teams.length ? `<div class="routed">✉ ${i.teams.map(t => `<span class="chip">${esc(t)}</span>`).join("")}${i.cc.length ? `<span class="cc">cc ${esc(i.cc.join(", "))}</span>` : ""}</div>` : ""}
    ${acts}<div class="ac-f">${ctaButtons(i)}<button class="btn small ghost" data-open="${i.id}">Evidence →</button><span class="who">${status}${whoBadge(i)}</span></div></div></div>`;
}
function renderAlerts(st) {
  const list = [...st.incidents].sort((a, b) => (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1) || b.id - a.id).slice(0, 25);
  $("#alertFeed").innerHTML = list.length ? list.map(alertCard).join("") :
    `<div class="empty">No alerts yet. Lumio scores 48 segments every 15 minutes.<br><br>Press <b>Play</b>, pick a speed, or <b>Jump to</b> a business day.</div>`;
}
document.addEventListener("click", async e => {
  const a = e.target.closest("[data-act]");
  if (a) { e.stopPropagation(); try { const r = await post(`/api/incidents/${a.dataset.id}/action`, { action: a.dataset.act, by: "Lumio dashboard" }); toast(r.message, 4500); } catch (err) { toast(err.message); }
    tick(true); if (S.drawerId === +a.dataset.id) openDrawer(+a.dataset.id); return; }
  const o = e.target.closest("[data-open]"); if (o && !e.target.closest("#drawer")) openDrawer(+o.dataset.open);
});

/* ------------------------------------------------------------------ banner for new alerts */
function banner(i) {
  const b = $("#banner"), sev = i.kind === "advisory" ? "info" : i.kind === "data_quality" ? "dq" : i.severity;
  b.className = "banner on " + sev;
  b.innerHTML = `<div class="b-icon">${i.kind === "data_quality" ? "≋" : i.kind === "advisory" ? "i" : "!"}</div><div class="b-txt"><div class="b-k">${esc(headLabel(i))} · INC-${String(i.id).padStart(4, "0")}</div>
    <div class="b-h">${esc(i.headline)}</div><div class="b-r">✉ Emailed ${esc(i.teams.join(" + "))}${i.cc.length ? " · cc " + esc(i.cc.join(", ")) : ""}</div></div><button class="btn small" data-open="${i.id}">Open</button>`;
  clearTimeout(b._h); b._h = setTimeout(() => b.classList.remove("on"), 7000);
  if (i.kind === "incident") chime();
}

/* ------------------------------------------------------------------ heatmap */
const DIMS = { banks: ["Bank X", "Bank Y", "Bank Z", "Bank W"], channels: ["UPI", "Card"], platforms: ["Android", "iOS"], regions: ["North", "South", "West"] };
function heatSkeleton(el, cellFn) {
  let h = `<div></div>` + DIMS.channels.map(c => `<div class="hd grp">${c}</div>`).join("");
  h += `<div></div>` + DIMS.channels.flatMap(c => DIMS.platforms.map(p => `<div class="hd sub">${p}</div>`)).join("");
  h += `<div></div>` + DIMS.channels.flatMap(c => DIMS.platforms.flatMap(p => DIMS.regions.map(r => `<div class="hd">${r[0]}</div>`))).join("");
  DIMS.banks.forEach(b => { h += `<div class="rl">${b}</div>`; DIMS.channels.forEach(c => DIMS.platforms.forEach(p => DIMS.regions.forEach(r => { h += cellFn(`${b}|${c}|${p}|${r}`); }))); });
  el.innerHTML = h;
}
function zColor(z, thr) { if (z >= thr) return ["#D97706", "#fff"]; if (z >= thr * 0.6) return ["#F3C783", "#5B3A00"]; if (z >= 2.5) return ["#FBE6C4", "#7A5410"]; return ["#E3F1EE", "#3D6B65"]; }
function renderHeat(st) {
  if (!st) return;
  const g = Object.fromEntries((st.grid || []).map(x => [x.seg, x]));
  const inInc = new Set(st.incidents.filter(i => i.status === "open" && i.kind === "incident").flatMap(i => i.segments));
  const sel = Object.keys(S.scope).length === 4 ? `${S.scope.bank}|${S.scope.channel}|${S.scope.platform}|${S.scope.region}` : null;
  heatSkeleton($("#heat"), seg => { const x = g[seg];
    if (!x) return `<div class="cell" data-seg="${seg}">·</div>`;
    if (x.status === "hold") return `<div class="cell hold ${sel === seg ? "sel" : ""}" data-seg="${seg}">held</div>`;
    const [bg, fg] = zColor(x.z, x.threshold); const r = x.exp ? x.p95 / x.exp : 1, vr = x.exp_vol ? x.vol / x.exp_vol - 1 : 0;
    let lab = ""; if (x.z >= 2.5) lab = x.flags.includes("V") || Math.abs(vr) > .4 ? (vr > 0 ? "+" : "−") + Math.abs(vr * 100).toFixed(0) + "%" : r >= 1.15 ? r.toFixed(1) + "×" : x.succ.toFixed(0) + "%";
    return `<div class="cell ${inInc.has(seg) ? "inc" : ""} ${sel === seg ? "sel" : ""}" data-seg="${seg}" style="background:${bg};color:${fg}">${lab}</div>`; });
  $("#gridTs").textContent = st.grid_ts ? `· window ${st.grid_ts}` : "";
}
$("#heat").addEventListener("click", e => { const c = e.target.closest(".cell"); if (!c || !c.dataset.seg) return; const [bank, channel, platform, region] = c.dataset.seg.split("|"); setScope({ bank, channel, platform, region }); });
$("#heat").addEventListener("mousemove", e => { const c = e.target.closest(".cell"), tip = $("#tip"); if (!c || !S.state || !c.dataset.seg) { tip.classList.remove("on"); return; }
  const x = (S.state.grid || []).find(g => g.seg === c.dataset.seg), name = c.dataset.seg.split("|").join(" · ");
  tip.innerHTML = !x ? esc(name) : x.status === "hold" ? `<b>${esc(name)}</b><br>Data held by the quality gate — not scored.` :
    `<b>${esc(name)}</b><br>Payment time ${x.p95.toFixed(2)} s (expected ${x.exp.toFixed(2)} s)<br>Success ${x.succ.toFixed(1)}%<br>Payments ${x.vol.toLocaleString()} (expected ${x.exp_vol.toLocaleString()})<br>Deviation ${x.z.toFixed(1)} · alert at ${x.threshold.toFixed(1)}${x.naive ? "<br><span style='color:#F3C783'>A fixed-threshold rule would page here</span>" : ""}`;
  tip.style.left = Math.min(e.clientX + 14, innerWidth - 310) + "px"; tip.style.top = (e.clientY + 14) + "px"; tip.classList.add("on"); });
$("#heat").addEventListener("mouseleave", () => $("#tip").classList.remove("on"));

/* ------------------------------------------------------------------ events */
const ICON = { alert: "!", detected: "!", investigation: "?", data_quality: "≋", suppressed: "✓", resolved: "✓", learning: "↺", deploy: "⇪", calendar: "◷",
  change: "⚙", weather: "❄", action: "▶", ack: "✓", watching: "·", email: "✉", escalation: "⚠", advisory: "i", system: "i" };
function eventsHtml(evs, fresh = 0) {
  return evs.map(e => `<div class="ev ${e.kind} ${e.id > fresh && fresh ? "fresh" : ""}"><span class="t">${e.day ? e.day + " " : ""}${e.ts}</span><span class="i">${ICON[e.kind] || "i"}</span><span class="m">${esc(e.message)}</span></div>`).join("") || `<div class="empty">Nothing yet.</div>`;
}
function renderEvents(st) { const show = $("#showWatch").checked;
  $("#events").innerHTML = eventsHtml(st.events.filter(e => show || e.kind !== "watching"), S.lastEventId);
  S.lastEventId = Math.max(S.lastEventId, ...st.events.map(e => e.id), 0); }
$("#showWatch").onchange = () => S.state && renderEvents(S.state);

/* ------------------------------------------------------------------ incident drawer */
let miniChart = null;
function barsHtml(a) { const max = Math.max(...a.bars.map(b => b.value), 1e-4);
  return a.bars.map(b => `<div class="bar ${b.hot ? "warn" : ""}"><span>${esc(b.label)}</span><div class="track"><div class="fill" style="width:${(b.value / max * 100).toFixed(1)}%"></div></div><span class="val">${a.unit === "" ? b.value.toLocaleString() : b.value + (a.unit === "%" ? "%" : a.unit === "s" ? " s" : "×")}</span></div>`).join(""); }
async function openDrawer(id) {
  S.drawerId = id; history.replaceState(null, "", "#incident=" + id);
  const d = await api(`/api/incidents/${id}`), a = d.alert || {}, r = d.routing || {};
  const head = d.kind === "advisory" ? "info" : d.kind === "data_quality" ? "dq" : d.status === "resolved" ? "resolved" : d.severity;
  const acts = (r.actions || []).reduce((m, x) => { (m[x.team] = m[x.team] || []).push(x.text); return m; }, {});
  const cost = (d.cost || []).map(c => `<tr><td>${esc(c.actor)}</td><td>${esc(c.purpose)}</td><td>${c.llm_calls}</td><td>${(c.tokens_in + c.tokens_out).toLocaleString()}</td><td>$${(c.cost_usd || 0).toFixed(4)}</td><td>${Math.round(c.duration_ms)} ms</td></tr>`).join("");
  $("#drawerBody").innerHTML = `
    <div class="big-alert ${head}"><div class="ah"><span>Lumio · ${esc(headLabel(d))}</span><span>${d.opened_day || ""} ${d.opened} · INC-${String(d.id).padStart(4, "0")}</span></div>
      <div class="ab"><h2>${esc(d.headline)}</h2><div class="sum">${esc(d.summary || "")}</div>${tilesHtml(d.tiles)}
        <div class="cause"><div><div class="lab">Likely cause</div>${esc(d.likely_cause || "—")}</div><div><div class="lab">Suggested next step</div>${esc(d.next_step || "—")}</div></div>
        <div class="acts">${ctaButtons(d, true)}${d.kind === "incident" && d.status === "open" && !d.feedback ? `<button class="btn ghost" data-act="dismiss" data-id="${d.id}">Not useful</button>` : ""}
          <span class="meta">${d.acked ? `<span class="badge ok">✓ acknowledged by ${esc(d.acked_by)} at ${d.acked}</span>` : d.escalated ? `<span class="badge esc">escalated to Duty Manager</span>` : ""} ${whoBadge(d)} ${d.confidence ? "confidence " + Math.round(d.confidence * 100) + "%" : ""}</span></div>
        ${(d.actions || []).length ? `<div class="done-acts">${d.actions.map(x => `<div>▶ ${esc(x.label)} — ${esc(x.by)} · ${esc(x.at)}</div>`).join("")}</div>` : ""}</div></div>
    ${r.to ? `<div class="sec"><h4>Routing · who was told and why</h4><div class="route"><div class="r-to"><b>To</b> ${(d.teams || []).map(t => `<span class="chip">${esc(t)}</span>`).join("")}${d.cc.length ? `<b class="ccl">Copied</b> ${d.cc.map(t => `<span class="chip ghost">${esc(t)}</span>`).join("")}` : ""}</div>
      <p class="why">${esc(r.why)}</p>${Object.entries(acts).map(([t, xs]) => `<div class="act-item"><b>${esc(t)}</b>${xs.map(x => `<div>${esc(x)}</div>`).join("")}</div>`).join("")}</div></div>` : ""}
    ${(d.emails || []).length ? `<div class="sec"><h4>Emails sent for this incident</h4>${d.emails.map(m => `<div class="mail-row" data-mail="${m.id}"><span class="st ${m.status}">${m.status}</span><span>${m.ts}</span><b>${esc(m.subject)}</b></div>`).join("")}</div>` : ""}
    ${d.kind !== "data_quality" ? `<div class="sec"><h4>${d.metric === "volume" ? "Payments per minute" : "Payment time"} in this scope</h4><div class="shell"><div class="card"><div class="mini-chart"><canvas id="miniChart"></canvas></div></div></div></div>` : ""}
    ${a.attribution ? `<div class="sec"><h4>${esc(a.attribution.title)}</h4><div class="bars">${barsHtml(a.attribution)}</div></div>` : ""}
    ${d.steps.length ? `<div class="sec"><h4>Investigation trace · ${d.steps.length} steps · every number came from a database query</h4><div class="trace">${d.steps.map(s => `
      <div class="step ${s.actor}" data-n="${s.step}"><div class="sh"><span class="badge ${s.actor === "agent" ? "agent" : "script"}">${s.actor === "agent" ? "AI agent" : "script"}</span><code>${esc(s.tool)}</code><span class="ms">${s.ms ? s.ms.toFixed(0) + " ms" : ""}</span></div><p>${esc(s.finding)}</p></div>`).join("")}</div></div>` : ""}
    ${cost ? `<div class="sec"><h4>Cost of this investigation</h4><table class="tbl"><tr><th>Who</th><th>Purpose</th><th>LLM calls</th><th>Tokens</th><th>Cost</th><th>Time</th></tr>${cost}</table></div>` : ""}`;
  $("#drawer").classList.add("on"); $("#scrim").classList.add("on");
  $$("#drawerBody [data-mail]").forEach(x => x.onclick = () => { closeDrawer(); S.outSel = +x.dataset.mail; showTab("outbox"); });
  if (d.kind !== "data_quality") { const s = await api("/api/series?" + new URLSearchParams(d.scope).toString() + "&hours=8");
    if (miniChart) miniChart.destroy(); miniChart = lineChart($("#miniChart"), d.metric === "volume" ? "n" : "s"); setSeries(miniChart, s.points, d.metric === "volume" ? "vol" : "lat", s.marks); }
}
function closeDrawer() { S.drawerId = null; $("#drawer").classList.remove("on"); $("#scrim").classList.remove("on"); if (location.hash.startsWith("#incident")) history.replaceState(null, "", "#"); }
$("#drawerClose").onclick = closeDrawer; $("#scrim").onclick = closeDrawer;

/* ------------------------------------------------------------------ inject */
function fillSelect(el, items, any) { el.innerHTML = (any ? `<option value="">Any</option>` : "") + items.map(i => `<option>${i}</option>`).join(""); }
$("#injectBtn").onclick = () => { fillSelect($("#injBank"), DIMS.banks); fillSelect($("#injChannel"), DIMS.channels, true); fillSelect($("#injPlatform"), DIMS.platforms, true); $("#injectModal").classList.add("on"); };
function closeInject() { $("#injectModal").classList.remove("on"); }
$("#injectClose").onclick = closeInject; $("#injectCancel").onclick = closeInject;
$("#injectGo").onclick = async () => { const r = await post("/api/inject", { bank: $("#injBank").value, channel: $("#injChannel").value, platform: $("#injPlatform").value, effect: $("#injEffect").value });
  closeInject(); toast(`Injected: ${r.fault.name}. Lumio has not been told.`); tick(true); };

/* ------------------------------------------------------------------ incidents tab */
function renderIncidents() { const st = S.state; if (!st) return;
  const rows = st.incidents.map(i => `<tr class="click" data-open="${i.id}"><td><b>#${i.id}</b></td><td><span class="st ${i.kind === "incident" ? i.status : i.kind}">${i.kind === "incident" ? i.status : i.kind.replace("_", " ")}</span></td>
    <td>${esc(i.headline)}<div class="hint" style="margin:2px 0 0">${esc(i.likely_cause || "")}</div></td><td>${i.opened_day} ${i.started || ""}</td><td>${i.opened || "—"}</td><td>${i.resolved || "—"}</td>
    <td>${i.teams.map(t => `<span class="chip small">${esc(t)}</span>`).join(" ")}</td><td>${i.acked ? "✓ " + esc(i.acked_by) : i.escalated ? '<span class="badge esc">escalated</span>' : "—"}</td></tr>`).join("");
  $("#incTable").innerHTML = `<tr><th>ID</th><th>Type</th><th>Alert</th><th>Began</th><th>Alerted</th><th>Resolved</th><th>Routed to</th><th>Acknowledged</th></tr>` + (rows || `<tr><td colspan="8" class="hint">No incidents yet.</td></tr>`); }

/* ------------------------------------------------------------------ reports tab */
async function renderReports() {
  const s = await api("/api/reports");
  $("#repKpis").innerHTML = kpi("Payments processed", s.payments.toLocaleString(), "across the days played so far") + kpi("Incidents", s.incidents, `${s.tickets} data ticket${s.tickets === 1 ? "" : "s"} · ${s.advisories} advisor${s.advisories === 1 ? "y" : "ies"}`, s.incidents ? "warn" : "good") +
    kpi("Median time to detect", s.mttd != null ? s.mttd + " min" : "—", "incl. the confirmation scan") + kpi("Value at risk flagged", money(s.value_at_risk), "computed per incident") +
    kpi("Emails routed", s.emails, "alerts, updates and reports") + kpi("Pages avoided", s.naive_pages.toLocaleString(), "vs a fixed-threshold rule", "good");
  const sel = S.daySel || (s.days.filter(d => d.played > 0).slice(-1)[0] || s.days[0]).date; S.daySel = sel;
  $("#dayCards").innerHTML = s.days.map((d, i) => `<button class="day ${d.date === sel ? "on" : ""} ${d.business ? "" : "we"} ${d.played ? "" : "future"}" data-day="${d.date}">
    <div class="d-top"><span>${d.short}</span><em>${d.business ? "Business day" : "Weekend"}${d.payday ? " · payday" : ""}</em></div>
    <div class="d-n">${d.played ? d.n_incidents : "—"}<small>${d.played ? " incident" + (d.n_incidents === 1 ? "" : "s") : " not played yet"}</small></div>
    <div class="d-s">${d.played ? `${d.payments.toLocaleString()} payments · ${d.success ?? "—"}%` : "&nbsp;"}</div><div class="d-bar"><i style="width:${Math.round(d.played * 100)}%"></i></div></button>`).join("");
  $$("#dayCards .day").forEach(b => b.onclick = () => { S.daySel = b.dataset.day; renderReports(); });
  const r = await api("/api/reports/" + sel);
  $("#dayDetail").innerHTML = card(`<div class="card-head"><div><div class="kicker">Daily report</div><h3>${esc(r.label)} ${r.business ? "" : "<small>weekend</small>"}${r.payday ? " <small>payday</small>" : ""}</h3></div>
      <div class="row-btns"><a class="btn small" href="/api/reports/${r.date}/csv">Download CSV</a><button class="btn small primary" id="emailRep">✉ Email this report</button></div></div>
    <div class="rep-grid"><div class="chart-wrap mid"><canvas id="repVol"></canvas></div><div class="chart-wrap mid"><canvas id="repSuc"></canvas></div></div>
    <table class="tbl">${`<tr><th>ID</th><th>Type</th><th>What</th><th>Began</th><th>Alerted</th><th>Detect</th><th>Duration</th><th>Value at risk</th><th>Teams</th><th>Ack</th></tr>` +
      (r.incidents.map(i => `<tr class="click" data-open="${i.id}"><td>#${i.id}</td><td><span class="st ${i.kind === "incident" ? i.severity : i.kind}">${i.kind === "incident" ? i.severity : i.kind.replace("_", " ")}</span></td><td>${esc(i.headline)}</td><td>${i.started || "—"}</td><td>${i.opened}</td><td>${i.detect_min ?? "—"} min</td><td>${i.duration_min} min</td><td>${i.value_at_risk ? money(i.value_at_risk) : "—"}</td><td>${i.teams.length}</td><td>${i.acked_by ? "✓" : "—"}</td></tr>`).join("") || `<tr><td colspan="10" class="hint">No incidents on this day${r.played < 1 ? " so far" : ""}.</td></tr>`)}</table>`);
  $("#emailRep").onclick = async () => { await post(`/api/reports/${r.date}/email`); toast("Report queued — see the Email outbox"); };
  const H = r.hourly || [];
  const mk = (id, cfg) => { if (S.charts[id]) S.charts[id].destroy(); S.charts[id] = new Chart($("#" + id), cfg); };
  const base = { animation: false, maintainAspectRatio: false, plugins: { legend: { labels: { font: { family: "Inter", size: 11 }, boxWidth: 12 } } },
    scales: { x: { grid: { display: false }, ticks: { font: { size: 10 }, color: C.muted } }, y: { grid: { color: "#EEF0F1" }, ticks: { font: { size: 10 }, color: C.muted } } } };
  mk("repVol", { type: "bar", data: { labels: H.map(h => String(h.h).padStart(2, "0") + ":00"), datasets: [{ label: "Payments", data: H.map(h => h.payments), backgroundColor: "rgba(15,118,110,.8)" },
    { type: "line", label: "Expected", data: H.map(h => h.expected), borderColor: C.grey, borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 }] }, options: base });
  mk("repSuc", { type: "line", data: { labels: H.map(h => String(h.h).padStart(2, "0") + ":00"), datasets: [{ label: "Success rate %", data: H.map(h => h.success), borderColor: C.teal, pointRadius: 0, borderWidth: 2 },
    { label: "Payment time (s)", data: H.map(h => h.p95), borderColor: C.amb, pointRadius: 0, borderWidth: 2, yAxisID: "y2" }] },
    options: { ...base, scales: { ...base.scales, y: { ...base.scales.y, suggestedMin: 90, max: 100 }, y2: { position: "right", grid: { display: false }, ticks: { font: { size: 10 }, color: C.amb } } } } });
}

/* ------------------------------------------------------------------ outbox tab */
async function renderOutbox() {
  const rows = await api("/api/outbox");
  $("#outList").innerHTML = rows.map(r => `<div class="out ${S.outSel === r.id ? "on" : ""}" data-oid="${r.id}"><div class="o-top"><span class="st ${r.status}">${r.status}</span><span class="o-kind">${r.kind}</span><span class="o-ts">${r.ts}</span></div>
    <div class="o-sub">${esc(r.subject)}</div><div class="o-to">→ ${esc(r.teams.join(", ") || "report")}${r.error ? ` · <span class="err">${esc(r.error)}</span>` : ""}</div></div>`).join("") || `<div class="empty">No emails yet. They appear here the moment an alert fires.</div>`;
  $$("#outList .out").forEach(o => o.onclick = () => { S.outSel = +o.dataset.oid; renderOutbox(); });
  if (!S.outSel && rows.length) S.outSel = rows[0].id;
  const sel = rows.find(r => r.id === S.outSel);
  if (sel) { $("#prevTitle").textContent = sel.subject; $("#prevStatus").innerHTML = `<span class="st ${sel.status}">${sel.status}</span> ${sel.status === "preview" ? `<button class="btn small" id="sendOne">Send now</button>` : ""}`;
    const f = $("#prevFrame"); if (f.dataset.oid !== String(sel.id)) { f.src = `/api/outbox/${sel.id}/html`; f.dataset.oid = sel.id; }
    const so = $("#sendOne"); if (so) so.onclick = async () => { await post(`/api/outbox/${sel.id}/send`); toast("Sending…"); setTimeout(renderOutbox, 1500); }; }
}
$("#sendAll").onclick = async () => { const r = await post("/api/outbox/send-all-previews"); toast(`${r.queued} emails queued for sending`); setTimeout(renderOutbox, 1500); };

/* ------------------------------------------------------------------ quality, learning, proof */
async function renderQuality() {
  const d = await api("/api/quality"), t = d.totals || {};
  $("#dqKpis").innerHTML = kpi("Records received", (d.records || 0).toLocaleString(), "live metric rows") + kpi("Windows checked", ((t.ok || 0) + (t.hold || 0) + (t.dedup || 0)).toLocaleString(), "segment × 15 minutes") +
    kpi("Held, not scored", t.hold || 0, "missing data — no false alarm", t.hold ? "warn" : "good") + kpi("Cleaned", t.dedup || 0, "duplicates removed first", t.dedup ? "warn" : "good") +
    kpi("Passed", (t.ok || 0).toLocaleString(), "complete and on time", "good") + kpi("Latest window", d.window || "—", "checked every scan");
  $("#dqWin").textContent = d.window ? "· " + d.window : "";
  const g = Object.fromEntries(d.segments.map(x => [x.seg, x]));
  heatSkeleton($("#dqHeat"), seg => { const x = g[seg]; if (!x) return `<div class="cell">·</div>`;
    if (x.status === "hold") return `<div class="cell hold" title="${esc(x.note)}">held</div>`;
    if (x.status === "dedup") return `<div class="cell" style="background:#F3C783;color:#5B3A00">${x.duplicates} dup</div>`;
    return `<div class="cell" style="background:#E3F1EE;color:#3D6B65">${Math.round(x.completeness * 100)}%</div>`; });
  $("#dqEvents").innerHTML = eventsHtml(d.events.map((e, i) => ({ ...e, kind: "data_quality", id: i })));
}
async function renderLearning() {
  const d = await api("/api/learning"), m = Object.fromEntries(d.sensitivity.map(x => [x.seg, x.delta]));
  heatSkeleton($("#sensGrid"), seg => { const t = m[seg]; return t ? `<div class="cell" style="background:#F3C783;color:#5B3A00">${(d.default + t).toFixed(1)}</div>` : `<div class="cell" style="background:#F1F3F2">${d.default.toFixed(1)}</div>`; });
  $("#learnEvents").innerHTML = eventsHtml(d.events.map((e, i) => ({ ...e, id: i })));
}
async function renderProof() {
  const d = await api("/api/proof");
  const det = d.rows.filter(r => r.detect_min != null && r.kind === "incident").map(r => r.detect_min).sort((a, b) => a - b);
  $("#proofKpis").innerHTML = kpi("Handled correctly", `${d.passed} / ${d.scored}`, `${d.total} planted · ${d.total - d.scored} not reached yet`, d.passed === d.scored ? "good" : "") +
    kpi("False alarms", d.false_alarms.length, "alerts with no planted cause", d.false_alarms.length ? "bad" : "good") + kpi("Lumio incidents", d.lumio_alerts, "paged teams") +
    kpi("Simple-rule pages", d.naive_pages.toLocaleString(), "fixed threshold, same data", "warn") + kpi("Median time to detect", det.length ? det[Math.floor(det.length / 2)] + " min" : "—", "incl. confirmation scan") +
    kpi("AI cost", "$" + d.cost.usd.toFixed(d.cost.usd ? 4 : 2), `${d.cost.llm_calls} LLM calls`);
  const lab = { incident: "Incident", data_quality: "Data quality", not_incident: "Expected", advisory: "Advisory", transient: "Blip" };
  $("#proofTable").innerHTML = `<tr><th>Result</th><th>Day</th><th>Planted event</th><th>Type</th><th>Expected</th><th>What Lumio did</th><th>Routed to</th></tr>` +
    d.rows.map(r => `<tr ${r.incident_id ? `class="click" data-open="${r.incident_id}"` : ""}><td><span class="st ${r.status}">${r.status}</span></td><td>${r.date}<br><small>${r.start}–${r.end || ""}</small></td>
      <td><b>${esc(r.name)}</b>${r.source ? ` <span class="badge nab">real data · ${esc(r.source)}</span>` : ""}${r.custom ? ` <span class="badge agent">injected live</span>` : ""}</td><td>${lab[r.kind]}</td>
      <td>${esc(r.expected)}</td><td>${esc(r.outcome)}</td><td>${r.teams.map(t => `<span class="chip small">${esc(t)}</span>`).join(" ")}</td></tr>`).join("") +
    d.false_alarms.map(f => `<tr><td><span class="st fail">false alarm</span></td><td>${f.opened}</td><td colspan="5">#${f.id} ${esc(f.headline)}</td></tr>`).join("");
  $("#stressBars").innerHTML = d.stress_test.rows.map((r, i) => `<div class="bar ${i === 0 ? "hot" : r.caught < 50 ? "warn" : ""}"><span>${esc(r.name)}</span><div class="track"><div class="fill" style="width:${r.caught}%"></div></div><span class="val">${r.caught}%</span></div>`).join("");
  $("#costTable").innerHTML = `<tr><th>Incident</th><th>Explained by</th><th>Steps</th><th>LLM calls</th><th>Cost</th><th>Time</th></tr>` +
    (d.per_incident.map(p => `<tr class="click" data-open="${p.id}"><td><b>#${p.id}</b> ${esc((p.headline || "").slice(0, 54))}${(p.headline || "").length > 54 ? "…" : ""}</td><td>${whoBadge(p)}</td><td>${p.steps}</td><td>${p.llm_calls}</td><td>$${p.usd.toFixed(4)}</td><td>${p.ms} ms</td></tr>`).join("") || `<tr><td colspan="6" class="hint">No investigations yet.</td></tr>`);
}

/* ------------------------------------------------------------------ dataset tab */
async function renderDataset() {
  const d = await api("/api/dataset"); const pr = await api("/api/proof").catch(() => ({ rows: [] }));
  $("#nabCards").innerHTML = d.nab.map(n => card(`<div class="kicker">Real data · Numenta Anomaly Benchmark</div><h3>${esc(n.used_for)}</h3><p class="nab-f"><code>${esc(n.file)}</code></p>
    <p><b>Real anomaly:</b> ${esc(n.real_anomaly)}</p><p><b>In this demo:</b> ${esc(n.demo_time)}</p>`, "nabcard")).join("");
  const reveal = $("#revealKey").checked;
  $("#calendar").innerHTML = d.days.map(day => { const feeds = d.feed.filter(f => f.ts.startsWith(day.label)); const truth = reveal ? pr.rows.filter(r => r.date === day.label) : [];
    return `<div class="cal-day ${day.business ? "" : "we"}"><div class="cal-h"><b>${day.label}</b><em>${day.business ? "Business day " + day.business_day : "Weekend"}${day.payday ? " · payday" : ""}</em></div>
      ${feeds.map(f => `<div class="cal-f">${ICON[f.kind] || "i"} ${esc(f.ts.slice(-5))} ${esc(f.message.replace(/^[^:]+: /, ""))}</div>`).join("")}
      ${truth.map(t => `<div class="cal-t ${t.kind}">● ${esc(t.start)} ${esc(t.name)}</div>`).join("")}</div>`; }).join("");
  $("#dsFiles").innerHTML = d.files.map(f => `<a class="file" href="/api/dataset/${encodeURIComponent(f.name)}"><b>${esc(f.name)}</b><span>${f.size_kb.toLocaleString()} KB</span></a>`).join("") || `<div class="empty">Dataset files not found.</div>`;
}
$("#revealKey").onchange = renderDataset;

/* ------------------------------------------------------------------ settings tab */
async function renderSettings() {
  const s = await api("/api/settings"); S.settings = s; const e = s.email, r = s.rules;
  const presetOpts = Object.keys(s.presets).map(k => `<option value="${k}" ${e.preset === k ? "selected" : ""}>${{ gmail: "Gmail", outlook: "Outlook.com", office365: "Microsoft 365", custom: "Other SMTP" }[k]}</option>`).join("");
  $("#setEmail").innerHTML = `<div class="card-head"><div><div class="kicker">Email delivery</div><h3>Where alerts are sent from</h3></div><label class="switch"><input type="checkbox" id="eOn" ${e.enabled ? "checked" : ""}><span></span></label></div>
    <div class="form one">
      <label>Provider<select id="ePreset">${presetOpts}</select></label><p class="hint" id="eHelp">${esc(s.presets[e.preset]?.help || "")}</p>
      <div class="two"><label>SMTP server<input id="eHost" value="${esc(e.host)}"></label><label>Port<input id="ePort" type="number" value="${e.port}"></label></div>
      <label>Security<select id="eSec"><option value="starttls" ${e.security === "starttls" ? "selected" : ""}>STARTTLS (587)</option><option value="ssl" ${e.security === "ssl" ? "selected" : ""}>SSL (465)</option><option value="none" ${e.security === "none" ? "selected" : ""}>None (local test server)</option></select></label>
      <label>Sign-in email<input id="eUser" value="${esc(e.username)}" placeholder="you@gmail.com" autocomplete="off"></label>
      <label>App password<input id="ePass" type="password" placeholder="${e.password_set ? "•••••••• (saved — type to change)" : "16-character app password"}" autocomplete="new-password"></label>
      <div class="two"><label>From name<input id="eFromN" value="${esc(e.from_name)}"></label><label>From address<input id="eFrom" value="${esc(e.from_addr)}" placeholder="same as sign-in"></label></div>
      <div class="demo-box"><label class="chk"><input type="checkbox" id="eDemo" ${e.demo_mode ? "checked" : ""}><span><b>Demo mode</b> — send every department's email to one inbox</span></label>
        <label>Demo inbox<input id="eInbox" value="${esc(e.demo_inbox)}" placeholder="you@gmail.com"></label>
        <label class="chk"><input type="checkbox" id="eTags" ${e.plus_tags ? "checked" : ""}><span>Tag by department (you<b>+fraud</b>@gmail.com) so routing is visible in one inbox</span></label></div>
      <label>Dashboard address used by email buttons<input id="eUrl" value="${esc(e.dashboard_url)}"></label>
      <div class="row-btns"><button class="btn primary" id="eSave">Save email settings</button></div>
      <div class="test-row"><input id="eTestTo" placeholder="send a test to…" value="${esc(e.demo_inbox || e.username)}"><button class="btn" id="eTest">Send test email</button></div><div id="eTestMsg" class="hint"></div>
    </div>`;
  $("#ePreset").onchange = () => { const p = s.presets[$("#ePreset").value]; if (p.host) $("#eHost").value = p.host; $("#ePort").value = p.port; $("#eSec").value = p.security; $("#eHelp").textContent = p.help; };
  const emailBody = () => ({ enabled: $("#eOn").checked, preset: $("#ePreset").value, host: $("#eHost").value.trim(), port: +$("#ePort").value, security: $("#eSec").value,
    username: $("#eUser").value.trim(), password: $("#ePass").value, from_name: $("#eFromN").value, from_addr: $("#eFrom").value.trim(),
    demo_mode: $("#eDemo").checked, demo_inbox: $("#eInbox").value.trim(), plus_tags: $("#eTags").checked, dashboard_url: $("#eUrl").value.trim() });
  $("#eSave").onclick = async () => { await put("/api/settings/email", emailBody()); toast("Email settings saved"); renderSettings(); tick(true); };
  $("#eOn").onchange = $("#eSave").onclick;
  $("#eTest").onclick = async () => { await put("/api/settings/email", emailBody()); $("#eTestMsg").textContent = "Sending…"; const res = await post("/api/settings/test-email", { to: $("#eTestTo").value.trim() }); $("#eTestMsg").textContent = res.message; $("#eTestMsg").className = "hint " + (res.ok ? "ok" : "err"); };
  const num = (id, label, v, min, max, step, help) => `<label class="rule"><span>${label}<small>${help}</small></span><input id="${id}" type="number" min="${min}" max="${max}" step="${step}" value="${v}"></label>`;
  const chk = (id, label, v) => `<label class="chk"><input type="checkbox" id="${id}" ${v ? "checked" : ""}><span>${label}</span></label>`;
  $("#setRules").innerHTML = `<div class="card-head"><div><div class="kicker">Alert rules · applied at the next scan</div><h3>When does Lumio trigger?</h3></div></div>
    <div class="slider"><div class="sl-top"><b>Sensitivity</b><span id="sensVal">${r.sensitivity.toFixed(1)}</span></div><input type="range" id="rSens" min="3" max="8" step="0.5" value="${r.sensitivity}">
      <div class="sl-lab"><span>more alerts</span><span>fewer alerts</span></div><small>How many "normal spreads" a segment must move from what Lumio expects. Shown as the amber alert line on the chart.</small></div>
    <div class="form one">
      ${num("rVol", "Volume sensitivity", r.volume_sensitivity, 3, 12, 0.5, "for payments vanishing or surging")}
      ${num("rConf", "Confirmation scans", r.confirm_scans, 1, 3, 1, "abnormal this many 15-min scans in a row")}
      ${num("rFloor", "Success-rate floor (%)", r.success_floor, 80, 99.9, 0.1, "always alert below this")}
      ${num("rCeil", "Payment-time ceiling (s)", r.latency_ceiling, 2, 30, 0.5, "always alert above this")}
      ${num("rHigh", "High severity from ($ at risk)", r.high_value_at_risk, 1000, 1e7, 1000, "otherwise medium")}
      <label class="rule"><span>Email incidents from<small>severity</small></span><select id="rMin"><option value="medium" ${r.email_min_severity === "medium" ? "selected" : ""}>Medium and high</option><option value="high" ${r.email_min_severity === "high" ? "selected" : ""}>High only</option></select></label>
      ${chk("rRes", "Send ‘resolved’ emails", r.email_resolved)}
      <div class="two">${chk("rEsc", "Escalate to the Duty Manager if not acknowledged within", r.email_escalation)}<input id="rEscMin" type="number" min="5" max="240" step="5" value="${r.escalate_after_min}"></div>
      ${chk("rRep", "End-of-day report at 18:00 on business days", r.daily_report)}${chk("rTix", "Data-quality tickets to Monitoring & Data", r.email_tickets)}${chk("rAdv", "System-wide advisories to Business Operations", r.email_advisories)}
      <div class="row-btns"><button class="btn primary" id="rSave">Save alert rules</button><button class="btn ghost" id="rReset">Restore defaults</button></div></div>`;
  $("#rSens").oninput = () => $("#sensVal").textContent = (+$("#rSens").value).toFixed(1);
  $("#rSave").onclick = async () => { await put("/api/settings/rules", { sensitivity: +$("#rSens").value, volume_sensitivity: +$("#rVol").value, confirm_scans: +$("#rConf").value,
    success_floor: +$("#rFloor").value, latency_ceiling: +$("#rCeil").value, high_value_at_risk: +$("#rHigh").value, email_min_severity: $("#rMin").value,
    email_resolved: $("#rRes").checked, email_escalation: $("#rEsc").checked, escalate_after_min: +$("#rEscMin").value, daily_report: $("#rRep").checked,
    email_tickets: $("#rTix").checked, email_advisories: $("#rAdv").checked }); toast("Alert rules saved — applied at the next scan"); renderSettings(); };
  $("#rReset").onclick = async () => { await put("/api/settings/rules", s.defaults); toast("Defaults restored"); renderSettings(); };
  const routes = { duty: "high-severity incidents, escalations, daily report", support: "customer-impacting incidents (copied)", mobile: "app release problems", release: "app release problems",
    sre: "upstream services, drift, unknown causes", fraud: "card-testing attacks, fraud-check service", cards: "card network, card fraud, card rules", corebank: "core-banking failures",
    network: "regional outages", product: "unusual causes found by the AI agent", data: "data-quality tickets", bizops: "system-wide demand advisories, daily report" };
  $("#setDepts").innerHTML = `<div class="card-head"><div><div class="kicker">Departments · routing</div><h3>Who gets which alert</h3></div></div>
    <div class="depts">${Object.values(s.departments).map(d => `<div class="dept"><div class="d-name"><b>${esc(d.name)}</b><small>${esc(d.lead)} · gets ${esc(routes[d.key])}</small></div>
      <input data-dept="${d.key}" value="${esc(d.email)}" placeholder="${esc(d.address || "team@company.com")}"></div>`).join("")}</div>
    <p class="hint">${e.demo_mode ? "Demo mode is on: every department's mail goes to the demo inbox (placeholder shows the exact address). Fill these in for real team addresses." : "Each department receives its own email."}</p>
    <div class="row-btns"><button class="btn primary" id="dSave">Save departments</button></div>`;
  $("#dSave").onclick = async () => { const body = {}; $$("#setDepts [data-dept]").forEach(i => body[i.dataset.dept] = { email: i.value.trim() }); await put("/api/settings/departments", body); toast("Departments saved"); renderSettings(); };
  shellify($("#tab-settings"));
}

/* ------------------------------------------------------------------ how it works */
async function renderHow() {
  const d = await api("/api/architecture"), c = d.counts, n = x => (x || 0).toLocaleString();
  $("#dbBadge").textContent = `${d.database} · ${d.mode === "all" ? "all-in-one" : "separate services"}`;
  const box = (x, y, w, t, s, stat, col = C.teal, dark = false) => `<g transform="translate(${x},${y})"><rect width="${w}" height="96" rx="12" fill="${dark ? col : "#fff"}" stroke="${dark ? col : C.line}" stroke-width="1.5"/><rect width="5" height="96" rx="2" fill="${col}"/>
    <text x="16" y="28" font-family="Inter" font-weight="700" font-size="14.5" fill="${dark ? "#fff" : C.ink}">${t}</text><text x="16" y="49" font-family="Inter" font-size="11.5" fill="${dark ? "#D7EFEB" : C.muted}">${s}</text>
    <text x="16" y="78" font-family="Inter" font-weight="700" font-size="12.5" fill="${dark ? "#fff" : col}">${stat}</text></g>`;
  const ar = (x1, y1, x2, y2) => `<path d="M${x1},${y1} L${x2},${y2}" stroke="${C.ink}" stroke-width="1.5" marker-end="url(#ah)" fill="none"/>`, W = 196;
  $("#arch").innerHTML = `<svg viewBox="0 0 1260 400" role="img" aria-label="Lumio architecture"><defs><marker id="ah" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 z" fill="${C.ink}"/></marker></defs>
    ${box(8, 30, W, "Data feeds", "NAB-based · 48 segments", n(c.live_rows) + " live rows", C.ink)}${box(220, 30, W, "Ingest API", "POST /api/ingest", n(c.samples) + " txn samples")}
    ${box(432, 30, W, "Database", d.database, n(c.history_rows) + " history rows")}${box(644, 30, W, "Data-quality gate", "missing · duplicates", n(c.dq_checks) + " checks")}
    ${box(856, 30, W, "Detector", "ML: time · success · volume", n(c.scans) + " scans", C.amb)}${box(1056, 30, W - 4, "Rules", "thresholds · guardrails", "your settings", C.amb)}
    ${box(1056, 240, W - 4, "Investigator", "scripted root cause", n(c.steps) + " steps")}${box(856, 240, W, "AI agent", "only when scripts fail", d.agent.startsWith("Claude") ? "Claude" : "offline planner", "#5B3FC4")}
    ${box(644, 240, W, "Narrator + routing", "12 departments", n(c.incidents) + " alerts", C.teal, true)}${box(432, 240, W, "Email notifier", "SMTP · outbox", n(c.emails) + " emails")}
    ${box(220, 240, W, "Dashboard", "this page · reports", n(c.events) + " events")}${box(8, 240, W, "Learning loop", "ack · act · dismiss", n(c.feedback) + " decisions")}
    ${ar(204, 78, 218, 78)}${ar(416, 78, 430, 78)}${ar(628, 78, 642, 78)}${ar(840, 78, 854, 78)}${ar(1052, 78, 1054, 78)}${ar(1152, 126, 1152, 238)}
    ${ar(1056, 288, 1053, 288)}${ar(852, 288, 842, 288)}${ar(640, 288, 630, 288)}${ar(428, 288, 418, 288)}${ar(216, 288, 206, 288)}
    <path d="M106,238 C106,180 900,180 950,128" stroke="${C.teal}" stroke-width="1.6" stroke-dasharray="5 4" fill="none" marker-end="url(#ah)"/>
    <text x="520" y="172" font-family="Inter" font-size="11.5" font-weight="600" fill="${C.teal}" text-anchor="middle">feedback and actions change thresholds and the live data</text>
    <text x="8" y="385" font-family="Inter" font-size="12" fill="${C.muted}">Model: gradient-boosted forecasts (payment time, success, volume) trained on ${n(d.model && d.model.windows)} windows in ${d.model ? d.model.train_s : "—"} s · ${d.bucket_min}-min buckets · ${d.scan_min}-min scans · ${d.segments} segments</text></svg>`;
  $("#principles").innerHTML = [["Numbers come from code", "Every figure in an alert or email comes from a database query or a model. The AI may choose the next query and the words — never the numbers."],
    ["The right team, not everyone", "Twelve departments, clear routing rules: releases to Release Management, card network to Card Ops, bad data to Monitoring — never a blanket page."],
    ["Calm on purpose", "Paydays, storms and planned maintenance are expected, not incidents. Two confirming scans, statistical guardrails and a data-quality gate keep false alarms at zero."]]
    .map(([t, p]) => card(`<div class="kicker">Principle</div><h3>${t}</h3><p>${p}</p>`, "principle")).join("");
}

function refreshTab(force) {
  const t = S.tab;
  if (t === "incidents") renderIncidents(); if (t === "reports") renderReports(); if (t === "outbox") renderOutbox(); if (t === "quality") renderQuality();
  if (t === "learning") renderLearning(); if (t === "proof") renderProof(); if (t === "how") renderHow();
  if (force && t === "dataset") renderDataset(); if (force && t === "settings") renderSettings();
}

/* ------------------------------------------------------------------ main loop */
async function tick(force = false) {
  if (S.busy && !force) return; S.busy = true;
  try {
    const st = await api("/api/state"); S.state = st;
    const lo = $("#loading");
    if (!st.history_ready || !st.model_ready) { if (lo) lo.querySelector("span").textContent = !st.history_ready ? "Loading three weeks of payment history…" : "Training Lumio's models of normal…"; S.busy = false; return; }
    if (lo) { lo.remove(); route(); }
    renderTop(st); renderKpis(st); renderAlerts(st); renderHeat(st); renderEvents(st);
    const fresh = st.incidents.filter(i => !S.seenInc.has(i.id) && i.severity !== "pending" && i.headline && !i.headline.endsWith("investigating…"));
    if (S.lastTime && fresh.length) { const n = fresh[0]; banner(n); if (S.scopeAuto && n.kind === "incident") { S.scope = { ...n.scope }; S.lastHeavy = 0; force = true; } }
    st.incidents.forEach(i => { if (i.severity !== "pending" && i.headline && !i.headline.endsWith("investigating…")) S.seenInc.add(i.id); });
    const now = Date.now(), slow = st.turbo_until || st.speed >= 6;   // while fast-forwarding, refresh heavy panels less often
    if ((st.time !== S.lastTime && (!slow || now - (S.lastHeavy || 0) > 2500)) || force) {
      S.lastTime = st.time; S.lastHeavy = now; if (S.tab === "live") await loadSeries(); if (!["live", "settings", "dataset"].includes(S.tab)) refreshTab(); }
  } catch (e) { console.warn(e); }
  S.busy = false;
}
function route() { const h = location.hash; if (h.startsWith("#incident=")) openDrawer(+h.split("=")[1]); if (h.startsWith("#reports=")) { S.daySel = h.split("=")[1]; showTab("reports"); } }
window.addEventListener("hashchange", route);
document.body.insertAdjacentHTML("beforeend", `<div class="loading" id="loading"><div class="spin"></div><span>Connecting…</span></div>`);
shellify(); tick(true); setInterval(tick, 1000);
