/* MacroQuant HITL console SPA — hash routing, no build step, no dependencies. */

const $main = document.getElementById("main");
const $topbarRight = document.getElementById("topbar-right");
const $modalRoot = document.getElementById("modal-root");
const $toastRoot = document.getElementById("toast-root");

const STATE_LABELS = {
  launching: "启动中", starting: "初始化", running_session: "运行中", waiting_user: "等待批准",
  waiting_step_user: "等待 Step 批准", waiting_user_reply: "等待答复提问", paused: "已暂停",
  completed: "已完成", stopped: "已停止", failed: "失败", interrupted: "已中断", terminated: "已强制终止",
  created: "未启动", legacy: "历史实验", unreadable: "不可解析", unknown: "未知",
};
const KIND_LABELS = { fold: "Fold", meta_learning: "元学习", heldout: "Held-out" };

let pollTimer = null;
let liveTimers = [];

/* ---------------- theme ---------------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("ch_theme", theme); } catch { /* private mode */ }
  const button = document.getElementById("theme-toggle");
  if (button) button.textContent = theme === "dark" ? "☀️" : "🌙";
}

/* Theme switches must not rebuild the page (that would restart the live trace
   stream); only the SVG charts need repainting — they re-render in place. */
function refreshCharts() {
  document.querySelectorAll(".svg-chart").forEach((node) => {
    if (typeof node.__rerender === "function") node.replaceWith(node.__rerender());
  });
}

(function initTheme() {
  let stored = null;
  try { stored = localStorage.getItem("ch_theme"); } catch { /* private mode */ }
  const preferred = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  applyTheme(stored === "dark" || stored === "light" ? stored : preferred);
  const button = document.getElementById("theme-toggle");
  if (button) button.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
    refreshCharts();
  });
})();

/* Per-device UI scale: port-forwarded browsers and embedded webviews disagree
   wildly about effective size; the choice persists per browser profile. */
(function initZoom() {
  const select = document.getElementById("ui-zoom");
  if (!select) return;
  let stored = null;
  try { stored = localStorage.getItem("ch_zoom"); } catch { /* private mode */ }
  const apply = (value) => {
    document.body.style.zoom = value;
    try { localStorage.setItem("ch_zoom", value); } catch { /* private mode */ }
  };
  if (stored && [...select.options].some((option) => option.value === stored)) {
    select.value = stored;
    apply(stored);
  }
  select.addEventListener("change", () => apply(select.value));
})();

/* Session keys contain "/" (epoch_001/fold_2022Q1); in the hash they travel as
   "~" so URLs stay readable (no %2F). Old encoded links still parse. */
function sessionKeyToUrl(key) {
  return encodeURIComponent(String(key).replaceAll("/", "~"));
}

function sessionKeyFromUrl(segment) {
  return decodeURIComponent(segment).replaceAll("~", "/");
}

/* Ledger period ranges are serialized as "YYYYMMDD..YYYYMMDD"; render them
   as human dates without touching the stored format. */
function fmtPeriodRange(value) {
  const match = /^(\d{4})(\d{2})(\d{2})\.\.(\d{4})(\d{2})(\d{2})$/.exec(String(value || ""));
  if (!match) return value || "—";
  return `${match[1]}-${match[2]}-${match[3]} ～ ${match[4]}-${match[5]}-${match[6]}`;
}

function fmtDate(value) {
  const match = /^(\d{4})(\d{2})(\d{2})$/.exec(String(value || ""));
  return match ? `${match[1]}-${match[2]}-${match[3]}` : String(value || "—");
}

/* All backend timestamps are ISO-UTC; the console displays UTC+8 (Asia/Shanghai)
   regardless of the browser's locale. */
const TS_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour12: false,
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit",
});
const TS_TIME_FMT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour12: false,
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
});

function fmtTs(iso) {
  const ms = Date.parse(iso || "");
  if (Number.isNaN(ms)) return "—";
  return TS_FMT.format(ms).replaceAll("/", "-");
}

function fmtTsTime(iso) {
  const ms = Date.parse(iso || "");
  if (Number.isNaN(ms)) return "";
  return TS_TIME_FMT.format(ms).replaceAll("/", "-");
}

function fmtDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function foldDurationNode(detail, session, prefix = "", className = "") {
  const node = el("span", { class: className });
  const fixedValue = (session.record || {}).run_wall_seconds;
  const fixed = Number(fixedValue);
  const isFixed = fixedValue !== null && fixedValue !== undefined && Number.isFinite(fixed) && fixed >= 0;
  const status = detail.status || {};
  const startedAt = status.session_key === session.key ? Date.parse(status.session_started_at || "") : NaN;
  const isLive = !isFixed && detail.worker_alive && Number.isFinite(startedAt);
  const update = () => {
    const seconds = isFixed ? fixed : isLive ? (Date.now() - startedAt) / 1000 : null;
    node.textContent = [prefix, seconds === null ? "" : fmtDuration(seconds)].filter(Boolean).join(" · ");
  };
  update();
  if (isLive) liveTimers.push(setInterval(() => { if (node.isConnected) update(); }, 1000));
  return node;
}

/* ---------------- utilities ---------------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return response.json();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message, isError = false) {
  const node = el("div", { class: `toast${isError ? " error" : ""}` }, message);
  $toastRoot.append(node);
  setTimeout(() => node.remove(), isError ? 7000 : 3500);
}

function fmtPct(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function numClass(value) {
  if (value === null || value === undefined) return "num";
  return value >= 0 ? "num pos" : "num neg";
}

function signCls(value) {
  if (value === null || value === undefined) return "";
  return value >= 0 ? "pos" : "neg";
}

function stateBadge(state) {
  return el("span", { class: `badge state-${state}` }, STATE_LABELS[state] || state);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

/* Minimal markdown renderer for the analysis panel (headings, lists, code,
   bold, inline code). Input is escaped first, so no raw HTML passes through. */
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let inCode = false, inList = false;
  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(inCode ? "</pre>" : "<pre>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(line); continue; }
    let html = line
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const heading = html.match(/^(#{1,4})\s+(.*)$/);
    const listItem = html.match(/^\s*[-*]\s+(.*)$/);
    if (listItem && !heading) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${listItem[1]}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }
    if (heading) out.push(`<h${heading[1].length + 1}>${heading[2]}</h${heading[1].length + 1}>`);
    else if (html.trim() === "") out.push("");
    else out.push(`<p>${html}</p>`);
  }
  if (inList) out.push("</ul>");
  if (inCode) out.push("</pre>");
  const div = el("div", { class: "markdown" });
  div.innerHTML = out.join("\n");
  return div;
}

/* ---------------- charts ----------------
   Specs: thin marks (bars ≤24px, 2px surface gap, 4px rounded data-end,
   square baseline; 2px lines; ≥8px markers with 2px surface ring), hairline
   solid gridlines, muted-ink labels, legend for 2 series, hover tooltips.
   Palette: categorical slots 1-2 (blue/aqua), CVD+contrast validated on white;
   aqua's sub-3:1 relief is carried by the result tables and tooltips. */

/* Both palettes validated (dataviz validator): light pair on white, dark pair
   (#3987e5/#199e70, the palette's dark steps) on the dark panel — all checks pass. */
function themeInk() {
  if (currentTheme() === "dark") {
    return {
      // Categorical slots 1-3 (dark steps; validated with the dataviz checker
      // on the dark panel #1b1f28 — all pass incl. contrast).
      validColor: "#3987e5", testColor: "#199e70", heldoutColor: "#c98500",
      validLight: "#7fb2ef", testLight: "#5ec49a",
      grid: "#2b303c", baseline: "#4a5163", muted: "#98a0af", faint: "#6f7787", ring: "#1b1f28",
    };
  }
  return {
    // Light slots 1-3 validated on white; aqua/yellow sit in the sub-3:1
    // relief band — carried by the result tables and rich tooltips.
    validColor: "#2a78d6", testColor: "#1baf7a", heldoutColor: "#eda100",
    validLight: "#86b6ef", testLight: "#66cfa4",
    grid: "#e9ebf1", baseline: "#c2c7d2", muted: "#68717f", faint: "#a5abb8", ring: "#ffffff",
  };
}

function seriesSpec() {
  const ink = themeInk();
  return { valid: { color: ink.validColor, label: "验证" }, test: { color: ink.testColor, label: "测试" } };
}

let $chartTip = null;
function chartTipNode() {
  if (!$chartTip) {
    $chartTip = el("div", { class: "chart-tip", style: "display:none" });
    document.body.append($chartTip);
  }
  return $chartTip;
}

function bindChartTips(wrap) {
  const tip = chartTipNode();
  wrap.addEventListener("mousemove", (event) => {
    const target = event.target.closest("[data-tip]");
    if (!target) { tip.style.display = "none"; return; }
    tip.textContent = target.getAttribute("data-tip");
    tip.style.display = "block";
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let x = event.clientX + pad, y = event.clientY + pad;
    if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
    if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  });
  wrap.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  return wrap;
}

function chartLegend(seriesList) {
  return el("div", { class: "chart-legend" },
    ...seriesList.map((series) => el("span", { class: "legend-item" },
      el("span", { class: "legend-swatch", style: `background:${series.color}` }), series.label)));
}

/* ---- daily equity lines + drawdown subplot (vs 沪深300 benchmark) ----
   Series = daily simple returns [[YYYYMMDD, r], ...]; the client compounds
   into cumulative curves and running drawdowns. Benchmark is drawn dashed in
   neutral ink (a reference, not a categorical slot). */
const EQUITY_CACHE = new Map(); // experiment_id -> { fp, payload }

async function fetchExperimentEquity(expId, fp) {
  const hit = EQUITY_CACHE.get(expId);
  if (hit && hit.fp === fp) return hit.payload;
  const payload = await api(`/api/experiments/${encodeURIComponent(expId)}/equity`);
  EQUITY_CACHE.set(expId, { fp, payload });
  return payload;
}

/* Async host: renders the chart when the series payload arrives. */
function equityHost(expId, fp, opts) {
  const host = el("div", {}, el("div", { class: "hint" }, "收益曲线加载中…"));
  fetchExperimentEquity(expId, fp)
    .then((payload) => { host.innerHTML = ""; host.append(equityChart(payload, opts)); })
    .catch((error) => { host.innerHTML = ""; host.append(el("div", { class: "hint" }, `收益曲线加载失败：${error.message}`)); });
  return host;
}

function fmtDateTick(date, withYear) {
  return withYear ? `${date.slice(2, 4)}/${date.slice(4, 6)}-${date.slice(6, 8)}` : `${date.slice(4, 6)}-${date.slice(6, 8)}`;
}

/* Pure renderer: the server delivers per-series {dates, cum, drawdown, final}
   already computed from frozen run artifacts — no return math happens here. */
function equityChart(payload, { width = 680, height = 240, ddH = 90, mini = false, keys = null } = {}) {
  const INK = themeInk();
  const colorOf = { valid: INK.validColor, test: INK.testColor, heldout: INK.heldoutColor, benchmark: INK.muted };
  const wanted = (payload.series || []).filter((s) => (s.dates || []).length && (!keys || keys.includes(s.key)));
  if (!wanted.length) return el("div", { class: "hint" }, "暂无日度收益数据");
  const wantedDates = new Set(wanted.flatMap((s) => s.dates));
  const shown = [...wanted];
  const bench = payload.benchmark;
  if (bench && (bench.dates || []).length && bench.dates.some((d) => wantedDates.has(d))) {
    shown.push(bench);
  }
  const seriesList = shown.map((s) => ({
    key: s.key,
    label: s.label,
    final: s.final,
    dates: s.dates,
    cum: new Map(s.dates.map((d, i) => [d, s.cum[i]])),
    dd: new Map(s.dates.map((d, i) => [d, s.drawdown[i]])),
    color: colorOf[s.key] || INK.validColor,
    dash: s.key === "benchmark" ? "6 4" : null,
  }));
  const dates = [...new Set(seriesList.flatMap((s) => s.dates))].sort();
  if (mini) ddH = 0;
  const showDD = ddH > 0;
  const padL = mini ? 44 : 52, padR = 12, padT = 8, gap = showDD ? 16 : 0;
  // With a drawdown subplot the shared date labels sit BELOW it, so the main
  // plot needs only a slim bottom pad; standalone charts keep the label band.
  const padB = showDD ? 12 : (mini ? 26 : 32);
  const labelBand = showDD ? 24 : 0;
  const totalH = height + (showDD ? ddH + gap : 0) + labelBand;
  const plotW = width - padL - padR, mainH = height - padT - padB;
  const xOf = (i) => padL + (dates.length === 1 ? plotW / 2 : (i / (dates.length - 1)) * plotW);
  const cums = seriesList.flatMap((s) => [...s.cum.values()]);
  let lo = Math.min(0, ...cums), hi = Math.max(0, ...cums);
  const pad = Math.max((hi - lo) * 0.08, 0.002);
  lo -= pad; hi += pad;
  const yOf = (v) => padT + ((hi - v) / (hi - lo)) * mainH;
  const svg = [];
  // main gridlines: 4 evenly spaced levels + emphasized zero line
  for (let t = 0; t <= 4; t += 1) {
    const v = lo + ((hi - lo) * t) / 4;
    const y = yOf(v);
    svg.push(`<line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="${INK.grid}" stroke-width="1"/>`);
    svg.push(`<text x="${padL - 6}" y="${y + 3.5}" text-anchor="end" font-size="${mini ? 10 : 11}" fill="${INK.muted}">${(v * 100).toFixed(1)}%</text>`);
  }
  if (lo < 0 && hi > 0) {
    svg.push(`<line x1="${padL}" y1="${yOf(0)}" x2="${width - padR}" y2="${yOf(0)}" stroke="${INK.baseline}" stroke-width="1"/>`);
  }
  // x ticks (≤7), year shown on the first tick and on year changes
  const tickEvery = Math.max(1, Math.ceil(dates.length / (mini ? 4 : 7)));
  let prevYear = null;
  // Date labels: below the drawdown subplot when present (shared axis at the
  // figure bottom), otherwise a clear step below the main axis line.
  const tickY = showDD ? height + gap + ddH + 10 : padT + mainH + (mini ? 17 : 20);
  dates.forEach((d, i) => {
    if (i % tickEvery !== 0 && i !== dates.length - 1) return;
    const withYear = prevYear !== d.slice(0, 4);
    prevYear = d.slice(0, 4);
    svg.push(`<text x="${xOf(i)}" y="${tickY}" text-anchor="middle" font-size="${mini ? 10 : 11}" fill="${INK.muted}">${fmtDateTick(d, withYear)}</text>`);
  });
  // drawdown subplot
  let ddY = null;
  if (showDD) {
    const ddTop = height + gap;
    const ddLo = Math.min(-0.001, ...seriesList.flatMap((s) => [...s.dd.values()]));
    ddY = (v) => ddTop + (v / ddLo) * (ddH - 14);
    svg.push(`<line x1="${padL}" y1="${ddY(0)}" x2="${width - padR}" y2="${ddY(0)}" stroke="${INK.baseline}" stroke-width="1"/>`);
    svg.push(`<line x1="${padL}" y1="${ddY(ddLo)}" x2="${width - padR}" y2="${ddY(ddLo)}" stroke="${INK.grid}" stroke-width="1"/>`);
    svg.push(`<text x="${padL - 6}" y="${ddY(ddLo) + 3.5}" text-anchor="end" font-size="10" fill="${INK.muted}">${(ddLo * 100).toFixed(1)}%</text>`);
    svg.push(`<text x="${padL - 6}" y="${ddY(0) + 3.5}" text-anchor="end" font-size="10" fill="${INK.muted}">回撤</text>`);
    for (const s of seriesList) {
      const pts = dates.filter((d) => s.dd.has(d));
      if (!pts.length) continue;
      const line = pts.map((d, j) => `${j ? "L" : "M"}${xOf(dates.indexOf(d)).toFixed(1)},${ddY(s.dd.get(d)).toFixed(1)}`).join(" ");
      if (s.key !== "benchmark") {
        const first = xOf(dates.indexOf(pts[0])).toFixed(1);
        const last = xOf(dates.indexOf(pts[pts.length - 1])).toFixed(1);
        svg.push(`<path d="M${first},${ddY(0).toFixed(1)} ${line.slice(1)} L${last},${ddY(0).toFixed(1)} Z" fill="${s.color}" fill-opacity="0.16" stroke="none"/>`);
      }
      svg.push(`<path d="${line}" fill="none" stroke="${s.color}" stroke-width="1.5"${s.dash ? ` stroke-dasharray="${s.dash}"` : ""}/>`);
    }
  }
  // main lines (benchmark first so strategy lines sit on top) + endpoint dots
  for (const s of [...seriesList].sort((a, b) => (a.key === "benchmark" ? -1 : 0) - (b.key === "benchmark" ? -1 : 0))) {
    const pts = dates.filter((d) => s.cum.has(d));
    if (!pts.length) continue;
    const line = pts.map((d, j) => `${j ? "L" : "M"}${xOf(dates.indexOf(d)).toFixed(1)},${yOf(s.cum.get(d)).toFixed(1)}`).join(" ");
    svg.push(`<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"${s.dash ? ` stroke-dasharray="${s.dash}"` : ""}/>`);
    if (s.key !== "benchmark") {
      const lastDate = pts[pts.length - 1];
      svg.push(`<circle cx="${xOf(dates.indexOf(lastDate)).toFixed(1)}" cy="${yOf(s.cum.get(lastDate)).toFixed(1)}" r="3.5" fill="${s.color}" stroke="${INK.ring}" stroke-width="2"/>`);
    }
  }
  // hover columns: one hit target per date spanning both plots, rich tooltip
  const step = dates.length > 1 ? plotW / (dates.length - 1) : plotW;
  dates.forEach((d, i) => {
    const lines = [fmtDate(d)];
    for (const s of seriesList) {
      if (!s.cum.has(d)) continue;
      lines.push(`${s.label} 累计 ${(s.cum.get(d) * 100).toFixed(2)}% ｜ 回撤 ${(s.dd.get(d) * 100).toFixed(2)}%`);
    }
    const x = i === 0 ? padL : xOf(i) - step / 2;
    const w = i === 0 || i === dates.length - 1 ? step / 2 : step;
    svg.push(`<rect class="xcol" x="${x.toFixed(1)}" y="${padT}" width="${Math.max(w, 1).toFixed(1)}" height="${totalH - padT - 4}" data-tip="${escapeHtml(lines.join("\n"))}"/>`);
  });
  const wrap = el("div", { class: "svg-chart" },
    chartLegend(seriesList.map((s) => ({ color: s.color, label: `${s.label}: ${fmtPct(s.final)}` }))));
  const svgHost = el("div", {});
  svgHost.innerHTML = `<svg viewBox="0 0 ${width} ${totalH}" xmlns="http://www.w3.org/2000/svg">${svg.join("")}</svg>`;
  wrap.append(svgHost);
  wrap.__rerender = () => equityChart(payload, { width, height, ddH, mini, keys });
  return bindChartTips(wrap);
}

function niceCeil(value) {
  const mag = Math.pow(10, Math.floor(Math.log10(value)));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (mult * mag >= value) return mult * mag;
  }
  return 10 * mag;
}

/* Rounded data-end bar: square at the baseline, 4px radius at the value end. */
function barPath(x, zeroY, valueY, w) {
  const up = valueY < zeroY;
  const h = Math.max(Math.abs(zeroY - valueY), 1);
  const r = Math.min(4, w / 2, h);
  if (up) {
    const y = zeroY - h;
    return `M${x},${zeroY} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${zeroY} Z`;
  }
  const y = zeroY + h;
  return `M${x},${zeroY} L${x + w},${zeroY} L${x + w},${y - r} Q${x + w},${y} ${x + w - r},${y} L${x + r},${y} Q${x},${y} ${x},${y - r} Z`;
}

function fmtAmount(value) {
  const n = Number(value) || 0;
  if (Math.abs(n) >= 1e8) return `¥${(n / 1e8).toFixed(2)}亿`;
  if (Math.abs(n) >= 1e4) return `¥${(n / 1e4).toFixed(1)}万`;
  return `¥${n.toFixed(0)}`;
}

/* Single-series bar chart (no legend needed for one series); direct value
   labels when the set is small, tooltips always. */
function singleSeriesBarChart(rows, { width = 640, height = 200, fmt = fmtPct } = {}) {
  const INK = themeInk();
  const color = INK.validColor; // categorical slot 1
  const values = rows.map((row) => row.value).filter((v) => v !== null && v !== undefined);
  if (!rows.length || !values.length) return el("div", { class: "hint" }, "暂无数据");
  const signed = values.some((v) => v < 0);
  const maxAbs = niceCeil(Math.max(1e-9, ...values.map(Math.abs)));
  const padL = 56, padR = 10, padB = 30, padT = signed ? 8 : 18;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const zeroY = signed ? padT + plotH / 2 : padT + plotH;
  const scale = signed ? plotH / 2 : plotH;
  const yOf = (v) => zeroY - (v / maxAbs) * scale;
  const svg = [];
  for (const frac of signed ? [-1, -0.5, 0.5, 1] : [0.5, 1]) {
    const y = yOf(frac * maxAbs);
    svg.push(`<line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="${INK.grid}" stroke-width="1"/>`);
    svg.push(`<text x="${padL - 6}" y="${y + 3.5}" text-anchor="end" font-size="11" fill="${INK.muted}">${escapeHtml(fmt(frac * maxAbs))}</text>`);
  }
  svg.push(`<line x1="${padL}" y1="${zeroY}" x2="${width - padR}" y2="${zeroY}" stroke="${INK.baseline}" stroke-width="1"/>`);
  const groupW = plotW / rows.length;
  const barW = Math.max(4, Math.min(24, groupW - 6));
  const showTipLabels = rows.length <= 8;
  const labelEvery = Math.max(1, Math.ceil(rows.length / 12));
  rows.forEach((row, index) => {
    const cx = padL + groupW * index + groupW / 2;
    const value = row.value;
    if (value === null || value === undefined) return;
    const tip = `${row.label} ${fmt(value)}`;
    svg.push(`<path d="${barPath(cx - barW / 2, zeroY, yOf(value), barW)}" fill="${color}" data-tip="${escapeHtml(tip)}"/>`);
    if (showTipLabels) {
      const labelY = value >= 0 ? yOf(value) - 5 : yOf(value) + 13;
      svg.push(`<text x="${cx}" y="${labelY}" text-anchor="middle" font-size="11" fill="${INK.muted}">${escapeHtml(fmt(value))}</text>`);
    }
    if (index % labelEvery === 0) {
      svg.push(`<text x="${cx}" y="${height - 8}" text-anchor="middle" font-size="11" fill="${INK.muted}">${escapeHtml(String(row.label))}</text>`);
    }
  });
  const wrap = el("div", { class: "svg-chart" });
  const svgHost = el("div", {});
  svgHost.innerHTML = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">${svg.join("")}</svg>`;
  wrap.append(svgHost);
  wrap.__rerender = () => singleSeriesBarChart(rows, { width, height, fmt });
  return bindChartTips(wrap);
}

/* Stat tiles: label + semibold value (proportional figures). */
function statTilesRow(tiles) {
  return el("div", { class: "tiles" },
    ...tiles.map((tile) => el("div", { class: "tile" },
      el("div", { class: "tile-label" }, tile.label),
      el("div", { class: `tile-value ${tile.cls || ""}` }, tile.value),
    )));
}

/* ---------------- router ---------------- */

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

function route(forceRefresh = false) {
  const force = forceRefresh === true; // hashchange passes an Event, not a force flag.
  const hash = location.hash || "#/";
  const expMatch = hash.match(/^#\/exp\/([^/]+)(?:\/(.*))?$/);
  const expId = expMatch ? decodeURIComponent(expMatch[1]) : null;
  const key = expMatch && expMatch[2] ? sessionKeyFromUrl(expMatch[2]) : null;
  // Session switch within an already-rendered experiment swaps only the right
  // panel: no page rebuild, no scroll jump, live stream and timers untouched.
  if (!force && expMatch && key && detailView && detailView.experimentId === expId
      && document.body.contains(detailView.listHost)) {
    selectSession(key);
    return;
  }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  for (const timer of liveTimers) clearInterval(timer);
  liveTimers = [];
  document.querySelectorAll(".modal-mask").forEach((node) => node.remove());
  // The live trace panel (SSE stream + accumulated events) survives navigation
  // within the same experiment; it is torn down only when leaving it.
  if (livePanel && (!expMatch || expId !== livePanel.expId)) destroyLivePanel();
  if (expMatch) renderDetailPage(expId, key);
  else { detailView = null; renderHomePage(); }
}

function selectSession(key) {
  // In-experiment switches bypass route(): stop the previous session's live
  // timers (GPU refresh, analysis re-polls) or they accumulate per visit.
  for (const timer of liveTimers) clearInterval(timer);
  liveTimers = [];
  detailView.selectedKey = key;
  const fresh = sessionDetailPanel(detailView.detail, key);
  detailView.rightHost.replaceWith(fresh);
  detailView.rightHost = fresh;
  detailView.listHost.querySelectorAll(".session-item").forEach((node) => {
    node.classList.toggle("selected", node.dataset.key === key);
  });
}

/* ---------------- home page ---------------- */

async function renderHomePage() {
  $main.innerHTML = '<div class="loading">加载中…</div>';
  $topbarRight.innerHTML = "";
  let payload;
  try {
    payload = await api("/api/experiments");
  } catch (error) {
    $main.innerHTML = `<div class="empty">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }
  $topbarRight.append(
    el("span", { class: "mode-note" }, `并行运行 ${payload.running.length}/${payload.max_running_experiments}`),
    el("button", { class: "btn primary", onclick: openCreateModal }, "＋ 新建实验"),
  );
  const container = el("div", {});
  const best = pickBestExperiment(payload.experiments);
  if (best) container.append(heroPanel(best), el("div", { class: "section-gap" }));
  container.append(el("div", { class: "page-head" },
    el("h2", {}, "实验列表"),
    el("span", { class: "sub" }, "点击实验卡片查看 Epoch/Fold 结果、运行状态与 Agent Trace"),
  ));
  if (!payload.experiments.length) {
    container.append(el("div", { class: "empty" }, "还没有实验 —— 点右上角「新建实验」开始。"));
  } else {
    const grid = el("div", { class: "grid" });
    for (const item of payload.experiments) grid.append(experimentCard(item));
    container.append(grid);
  }
  $main.innerHTML = "";
  $main.append(container);
  pollTimer = setInterval(async () => {
    if (location.hash && location.hash !== "#/" && location.hash !== "#") return;
    try { await renderHomePageSilent(); } catch { /* keep last view */ }
  }, 5000);
}

async function renderHomePageSilent() {
  const payload = await api("/api/experiments");
  const grid = document.querySelector(".grid");
  if (!grid) return;
  const fresh = el("div", { class: "grid" });
  for (const item of payload.experiments) fresh.append(experimentCard(item));
  grid.replaceWith(fresh);
  const hero = document.getElementById("hero-panel");
  const best = pickBestExperiment(payload.experiments);
  if (hero && best) hero.replaceWith(heroPanel(best));
}

function experimentCard(item) {
  const metrics = item.metrics || {};
  const total = item.total_sessions, done = item.completed_sessions || 0;
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const card = el("div", {
    class: "card clickable",
    onclick: () => { location.hash = `#/exp/${encodeURIComponent(item.experiment_id)}`; },
  });
  card.append(
    el("h3", {},
      el("a", { href: `#/exp/${encodeURIComponent(item.experiment_id)}` }, item.experiment_id),
      stateBadge(item.state),
      item.kind === "legacy" ? el("span", { class: "badge kind" }, "只读") : null,
    ),
    el("div", { class: "meta-line" },
      `创建 ${fmtTs(item.created_at)}`,
      item.current_session ? ` ｜ 当前 ${item.current_session}` : "",
      item.error ? ` ｜ ${item.error}` : "",
    ),
  );
  if (total) {
    card.append(
      el("div", { class: `progress${done >= total ? " done" : ""}` }, el("div", { style: `width:${pct}%` })),
      el("div", { class: "meta-line" }, `进度 ${done}/${total} 个会话`),
    );
  } else if (item.folds_recorded) {
    card.append(el("div", { class: "meta-line" }, `账本记录：${item.folds_recorded} 个 Fold ｜ ${item.heldout_recorded || 0} 个 held-out`));
  }
  // Same component + order as the hero and detail pages (Held-out first).
  card.append(statTilesRow([
    { label: "Held-out 收益", value: fmtPct(metrics.cum_heldout_return), cls: signCls(metrics.cum_heldout_return) },
    { label: "累计测试收益", value: fmtPct(metrics.cum_test_return), cls: signCls(metrics.cum_test_return) },
    { label: "累计验证收益", value: fmtPct(metrics.cum_valid_return), cls: signCls(metrics.cum_valid_return) },
  ]));
  if ((item.fold_returns || []).length) {
    card.append(equityHost(item.experiment_id, equityFingerprint(item), { width: 400, height: 130, mini: true }));
  }
  const actions = el("div", { class: "actions" });
  if (item.kind === "hitl" && ["interrupted", "stopped", "failed", "created"].includes(item.state)) {
    actions.append(el("button", {
      class: "btn small primary",
      onclick: async (event) => {
        event.stopPropagation();
        try {
          await api(`/api/experiments/${encodeURIComponent(item.experiment_id)}/control`, { method: "POST", body: JSON.stringify({ action: "resume" }) });
          toast("已请求恢复运行");
          renderHomePageSilent();
        } catch (error) { toast(`恢复失败：${error.message}`, true); }
      },
    }, "恢复运行"));
  }
  if (!item.worker_alive) {
    actions.append(el("button", {
      class: "btn small danger",
      onclick: (event) => { event.stopPropagation(); confirmDeleteExperiment(item.experiment_id); },
    }, "删除"));
  }
  if (actions.children.length) card.append(actions);
  return card;
}

/* Best-performing experiment hero: ranked by mean test-period Sharpe (falls
   back to cumulative test/validation return when Sharpe is unavailable). */
function pickBestExperiment(list) {
  const scored = list
    .filter((item) => (item.fold_returns || []).length)
    .map((item) => ({
      item,
      sharpe: item.metrics?.mean_test_sharpe ?? null,
      ret: item.metrics?.cum_test_return ?? item.metrics?.cum_valid_return ?? null,
    }))
    .filter((entry) => entry.sharpe !== null || entry.ret !== null);
  if (!scored.length) return null;
  scored.sort((a, b) => {
    if (a.sharpe !== null && b.sharpe !== null) return b.sharpe - a.sharpe;
    if (a.sharpe !== null) return -1;
    if (b.sharpe !== null) return 1;
    return b.ret - a.ret;
  });
  return scored[0].item;
}

/* Cache key: equity only changes when new records land (or a rerun replaces
   results — caught by the cumulative-return components). */
function equityFingerprint(item) {
  const metrics = item.metrics || {};
  return `${item.folds_recorded}|${item.heldout_recorded}|${metrics.cum_test_return}|${metrics.cum_valid_return}|${metrics.cum_heldout_return}`;
}

function heroPanel(item) {
  const metrics = item.metrics || {};
  const panel = el("div", { class: "panel hero", id: "hero-panel" });
  const charts = el("div", { class: "section-gap" },
    el("h4", {}, "日度累计收益 vs 沪深300（含回撤）"),
    equityHost(item.experiment_id, equityFingerprint(item), { width: 980, height: 240, ddH: 90 }),
  );
  panel.append(
    el("div", { class: "control-bar" },
      el("span", { class: "hero-crown" }, "🏆"),
      el("h3", { style: "margin:0" },
        el("a", { href: `#/exp/${encodeURIComponent(item.experiment_id)}` }, item.experiment_id)),
      stateBadge(item.state),
      el("span", { class: "mode-note" }, "当前最佳实验（按测试期平均 Sharpe）"),
    ),
    el("div", { class: "section-gap" }, statTilesRow([
      { label: "Held-out 收益（最终样本外）", value: fmtPct(metrics.cum_heldout_return), cls: `hero-key ${signCls(metrics.cum_heldout_return)}` },
      { label: "累计测试收益", value: fmtPct(metrics.cum_test_return), cls: signCls(metrics.cum_test_return) },
      { label: "累计验证收益", value: fmtPct(metrics.cum_valid_return), cls: signCls(metrics.cum_valid_return) },
      { label: "平均测试 Sharpe", value: metrics.mean_test_sharpe === null || metrics.mean_test_sharpe === undefined ? "—" : Number(metrics.mean_test_sharpe).toFixed(2) },
      { label: "已完成 Fold", value: String(item.folds_recorded ?? 0) },
    ])),
    charts,
  );
  return panel;
}

function confirmRevealTests(experimentId) {
  showModal("揭示测试结果", el("div", {},
    el("p", {}, "揭示后本实验封存：不能再批准会话、重跑、回滚、逐 Step 放行或注入任何指令（研究者一旦看到样本外结果，之后的每个决定都不再是样本外的）。"),
    el("p", {}, "查看/停止/删除仍然可用。此操作不可撤销。"),
  ), [
    el("button", { class: "btn", onclick: closeModal }, "取消"),
    el("button", {
      class: "btn danger",
      onclick: () => sendControlAction(
        experimentId, { action: "reveal_test_results" }, "测试结果已揭示，实验已封存", { modal: true }),
    }, "揭示并封存"),
  ]);
}

function confirmDeleteExperiment(experimentId) {
  const input = el("input", { type: "text", placeholder: experimentId });
  showModal("删除实验", el("div", {},
    el("p", {}, `此操作会永久删除 experiments/${experimentId}/ 目录（含账本、冻结策略与全部运行产物），不可恢复。`),
    el("p", {}, "输入实验名以确认："),
    el("div", { class: "field" }, input),
  ), [
    el("button", { class: "btn", onclick: closeModal }, "取消"),
    el("button", {
      class: "btn danger",
      onclick: async () => {
        if (input.value !== experimentId) { toast("实验名不匹配", true); return; }
        try {
          await api(`/api/experiments/${encodeURIComponent(experimentId)}?confirm=${encodeURIComponent(experimentId)}`, { method: "DELETE" });
          toast("已删除");
          closeModal();
          if (location.hash !== "#/") location.hash = "#/";
          else renderHomePage();
        } catch (error) { toast(`删除失败：${error.message}`, true); }
      },
    }, "确认删除"),
  ]);
}

/* ---------------- create modal ---------------- */

let createSchema = null;

async function openCreateModal() {
  let schema;
  try { schema = await api("/api/parameter-schema"); } catch (error) { toast(error.message, true); return; }
  createSchema = schema;
  const hasPeriodOptions = Object.keys(schema.period_options || {}).length > 0;
  const inputs = new Map();
  const body = el("div", {});
  // Validation errors surface at the TOP of the (scrollable) modal body.
  const errorBox = el("div", {});
  body.append(errorBox);
  body.append(el("p", { class: "hint" },
    hasPeriodOptions
      ? "所有参数均有默认值。周期从交易日历自动生成，仅列出数据完整、可回测的周期；切换 Fold 周期后选项与推荐值随之更新。"
      : "所有参数均有默认值；仅实验名与周期标签必填。周期标签格式随 Fold 周期而定：quarter → 2024Q1，month → 202401，week → 周一日期 20240108，year → 2024。"));
  for (const group of schema.groups) {
    const basic = group.fields.filter((field) => !field.advanced);
    const advanced = group.fields.filter((field) => field.advanced);
    if (!basic.length && !advanced.length) continue;
    const section = el("div", { class: "form-group" }, el("h4", {}, group.name));
    if (basic.length) section.append(fieldGrid(basic, inputs));
    if (advanced.length) {
      section.append(el("details", { class: "advanced" },
        el("summary", {}, `高级参数（${advanced.length}）`),
        fieldGrid(advanced, inputs),
      ));
    }
    body.append(section);
  }
  // Period selects depend on the fold cadence: repopulate options + suggested
  // defaults whenever fold_period changes.
  const cadenceEntry = inputs.get("fold_period");
  if (cadenceEntry && hasPeriodOptions) {
    repopulatePeriodSelects(inputs);
    cadenceEntry.input.addEventListener("change", () => repopulatePeriodSelects(inputs));
  }
  showModal("新建实验", body, [
    el("button", { class: "btn", onclick: closeModal }, "取消"),
    el("button", {
      class: "btn primary",
      onclick: async (event) => {
        const params = collectParams(inputs);
        event.target.disabled = true;
        try {
          const created = await api("/api/experiments", { method: "POST", body: JSON.stringify({ params }) });
          toast(`实验 ${created.experiment_id} 已创建并启动`);
          closeModal();
          location.hash = `#/exp/${encodeURIComponent(created.experiment_id)}`;
        } catch (error) {
          errorBox.innerHTML = "";
          errorBox.append(el("div", { class: "form-error" }, `创建失败：${error.message}`));
          const scroller = errorBox.closest(".body");
          if (scroller) scroller.scrollTop = 0;
        } finally { event.target.disabled = false; }
      },
    }, "创建并启动"),
  ]);
}

function fieldGrid(fields, inputs) {
  const grid = el("div", { class: "form-grid" });
  for (const field of fields) grid.append(fieldNode(field, inputs));
  return grid;
}

function fieldNode(field, inputs) {
  const wrap = el("div", { class: "field" });
  if (field.key === "gpu_count") wrap.classList.add("field-wide", "gpu-field");
  // multi defaults to a full row (long chip lists); "wide": false opts a short
  // chip group into a normal grid cell so it can share a row (e.g. 板块范围).
  if (field.type === "multi" && field.wide !== false) wrap.classList.add("field-wide");
  const labelText = field.required ? `${field.label} *` : field.label;
  if (field.type === "bool") {
    const input = el("input", { type: "checkbox" });
    input.checked = Boolean(field.default);
    inputs.set(field.key, { field, input });
    wrap.className = "field checkbox";
    wrap.append(input, el("div", {}, el("label", {}, labelText), el("div", { class: "help" }, field.help || "")));
    return wrap;
  }
  wrap.append(el("label", {}, labelText));
  let input;
  if (field.type === "choice") {
    input = el("select", {}, ...field.choices.map((choice) => {
      const option = el("option", { value: choice }, (field.choice_labels || {})[choice] || choice);
      if (choice === field.default) option.selected = true;
      return option;
    }));
  } else if (field.type === "period") {
    // Options are cadence-dependent; repopulatePeriodSelects fills them.
    input = el("select", { class: "period-select" });
  } else if (field.type === "multi") {
    // Checkbox group: multi-selects require ctrl-click and mis-toggle easily.
    const boxes = field.choices.map((choice) => {
      const box = el("input", { type: "checkbox", value: choice });
      box.checked = (field.default || []).includes(choice);
      return box;
    });
    // Chip text prefers the Chinese display label; the raw API name stays on
    // the tooltip for cross-referencing docs/data contracts.
    const groupNode = el("div", { class: "check-group" },
      ...boxes.map((box, index) => {
        const choice = field.choices[index];
        const label = (field.choice_labels || {})[choice];
        return el("label", { class: "check-item", title: label ? choice : "" }, box, label || choice);
      }));
    inputs.set(field.key, { field, getValue: () => boxes.filter((box) => box.checked).map((box) => box.value) });
    wrap.append(groupNode, el("div", { class: "help" }, field.help || ""));
    return wrap;
  } else if (field.type === "text") {
    input = el("textarea", { rows: "3" });
    input.value = field.default ?? "";
  } else {
    input = el("input", { type: field.type === "int" || field.type === "float" ? "number" : "text" });
    if (field.type === "float") input.setAttribute("step", "any");
    if (field.min !== undefined) input.setAttribute("min", String(field.min));
    if (field.max !== undefined) input.setAttribute("max", String(field.max));
    input.value = field.default ?? "";
    if (field.optional) input.placeholder = "留空使用默认";
    if (field.type === "int") {
      // Native WebKit spinners are hidden (unstylable); draw our own steppers.
      const step = (direction) => {
        if (direction > 0) input.stepUp(); else input.stepDown();
        input.dispatchEvent(new Event("change", { bubbles: true }));
      };
      const host = el("div", { class: "number-input" }, input,
        el("div", { class: "spin-col" },
          el("button", { type: "button", class: "spin", tabindex: "-1", onclick: () => step(1) }, "▲"),
          el("button", { type: "button", class: "spin", tabindex: "-1", onclick: () => step(-1) }, "▼"),
        ));
      inputs.set(field.key, { field, input });
      wrap.append(host, el("div", { class: "help" }, field.help || ""));
      if (field.key === "gpu_count") {
        const gpuStatus = el("div", { class: "gpu-status" }, el("span", { class: "help" }, "正在读取当前 GPU 状态…"));
        wrap.append(gpuStatus);
        api("/api/gpus").then((payload) => {
          const gpus = payload.gpus || [];
          if (!gpus.length) {
            gpuStatus.replaceChildren(el("span", { class: "help" }, `当前无可用 GPU 信息${payload.error ? `：${payload.error}` : ""}`));
            return;
          }
          gpuStatus.replaceChildren(...gpus.map((gpu) => el("div", { class: "gpu-status-item" },
            el("strong", {}, `GPU ${gpu.index}`),
            el("span", {}, `空闲 ${(gpu.memory_free_mib / 1024).toFixed(1)} / ${(gpu.memory_total_mib / 1024).toFixed(1)} GiB`),
          )));
          input.max = String(Math.min(Number(field.max || 4), gpus.length));
        }).catch((error) => { gpuStatus.replaceChildren(el("span", { class: "help" }, `GPU 状态读取失败：${error.message}`)); });
      }
      return wrap;
    }
  }
  inputs.set(field.key, { field, input });
  wrap.append(input, el("div", { class: "help" }, field.help || ""));
  return wrap;
}

const PERIOD_FIELD_KEYS = ["first_test_period", "last_test_period", "heldout_first_period", "heldout_last_period"];

function repopulatePeriodSelects(inputs) {
  const cadence = inputs.get("fold_period").input.value;
  const options = (createSchema.period_options || {})[cadence] || [];
  const defaults = (createSchema.period_defaults || {})[cadence] || {};
  for (const key of PERIOD_FIELD_KEYS) {
    const entry = inputs.get(key);
    if (!entry || entry.field.type !== "period" || !entry.input) continue;
    const previous = entry.input.value;
    entry.input.innerHTML = "";
    for (const label of options) {
      const option = el("option", { value: label }, label);
      entry.input.append(option);
    }
    const wanted = options.includes(previous) ? previous : defaults[key];
    if (wanted && options.includes(wanted)) entry.input.value = wanted;
  }
  updateValidationHint(inputs);
}

/* Folds are named by their TEST period; surface the derived validation period
   so the naming never reads as ambiguous. */
function updateValidationHint(inputs) {
  const entry = inputs.get("first_test_period");
  if (!entry || entry.field.type !== "period" || !entry.input) return;
  const cadence = inputs.get("fold_period").input.value;
  const options = (createSchema.period_options || {})[cadence] || [];
  const index = options.indexOf(entry.input.value);
  if (!entry.__hint) {
    entry.__hint = el("div", { class: "help derived-hint" });
    entry.input.parentElement.append(entry.__hint);
    entry.input.addEventListener("change", () => updateValidationHint(inputs));
  }
  entry.__hint.textContent = index > 0
    ? `↳ 首个 Fold：验证区间 ${options[index - 1]} → 测试区间 ${options[index]}（验证区间自动取测试周期的前一周期）`
    : "";
}

function collectParams(inputs) {
  const params = {};
  for (const [key, entry] of inputs.entries()) {
    const { field, input } = entry;
    let value;
    if (entry.getValue) value = entry.getValue();
    else if (field.type === "bool") value = input.checked;
    else value = input.value;
    if (field.type === "int" || field.type === "float") {
      if (value === "" || value === null) { if (field.optional) continue; value = field.default; }
      else value = field.type === "int" ? parseInt(value, 10) : parseFloat(value);
      if (Number.isNaN(value)) continue;
    }
    if (typeof value === "string") value = value.trim();
    if (value === "" && !field.required) {
      if (field.default === null || field.default === undefined) continue;
      value = field.default;
    }
    if (JSON.stringify(value) === JSON.stringify(field.default) && !field.required) continue;
    params[key] = value;
  }
  return params;
}

/* ---------------- modal helpers ---------------- */

function showModal(title, body, footerButtons, modalClass = "") {
  closeModal();
  const mask = el("div", { class: "modal-mask", onclick: (event) => { if (event.target === mask) closeModal(); } });
  mask.append(el("div", { class: `modal${modalClass ? ` ${modalClass}` : ""}` },
    el("header", {}, el("h3", {}, title), el("button", { class: "btn small", onclick: closeModal }, "✕")),
    el("div", { class: "body" }, body),
    el("footer", {}, ...footerButtons),
  ));
  $modalRoot.append(mask);
}

function closeModal() { $modalRoot.innerHTML = ""; }

/* ---------------- detail page ---------------- */

let detailView = null; // {experimentId, detail, listHost, rightHost, selectedKey}
let livePanel = null; // {expId, key, node, source, timers, refresh}

function destroyLivePanel() {
  if (!livePanel) return;
  livePanel.closed = true;
  try { livePanel.source?.close(); } catch { /* already closed */ }
  for (const timer of livePanel.timers) clearInterval(timer);
  livePanel = null;
}

async function renderDetailPage(experimentId, selectedKey) {
  $main.innerHTML = '<div class="loading">加载中…</div>';
  $topbarRight.innerHTML = "";
  let detail;
  try {
    detail = await api(`/api/experiments/${encodeURIComponent(experimentId)}`);
  } catch (error) {
    $main.innerHTML = `<div class="empty">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }
  const status = detail.status || {};
  const sessions = detail.sessions || [];
  if (livePanel && livePanel.expId === experimentId) {
    const liveState = detail.worker_alive
      && ["running_session", "waiting_step_user", "waiting_user_reply"].includes(detail.state);
    const currentRunId = String(status.run_id || "");
    if (!liveState || livePanel.key !== status.session_key || livePanel.runId !== currentRunId) {
      destroyLivePanel();
    }
  }
  if (!selectedKey) {
    selectedKey = status.session_key
      || (sessions.find((session) => !session.record && !session.records) || sessions[sessions.length - 1] || {}).key;
  }
  const head = el("div", { class: "page-head" },
    el("h2", {},
      el("a", { href: "#/" }, "← 实验"),
      detail.experiment_id,
      stateBadge(detail.state),
      detail.kind === "legacy" ? el("span", { class: "badge kind" }, "只读") : null,
      detail.worker_alive && status.code_version && detail.repo_code_version
        && status.code_version !== detail.repo_code_version
        ? el("span", { class: "badge state-failed",
            title: `worker 启动于 ${status.code_version}，仓库已是 ${detail.repo_code_version}：长驻进程仍在运行旧代码，重启 worker 生效` },
            "代码过期")
        : null,
      detail.kind === "hitl" && (detail.control || {}).test_revealed
        ? el("span", { class: "badge state-waiting_user", title: "测试/Held-out 结果已揭示：实验已封存，不能再批准、重跑、回滚或注入指令" }, "已揭示测试（封存）")
        : null,
    ),
    el("div", { class: "sub" },
      `进度 ${detail.completed_sessions ?? 0}/${detail.total_sessions ?? "?"}`,
      status.error ? ` ｜ 错误：${status.error}` : "",
      // A worker-recorded analysis error is only current while that worker
      // lives; stale failures are visible per fold in the analysis section.
      detail.worker_alive && status.analysis_error ? ` ｜ 分析：${status.analysis_error}` : "",
    ),
  );
  const container = el("div", {});
  let barHost = null;
  if (detail.params && Object.keys(detail.params).length) {
    head.querySelector("h2").append(el("button", {
      class: "btn small", style: "margin-left:0.4rem",
      onclick: () => openParamsModal(detail),
    }, "创建参数"));
  }
  if (detail.kind === "hitl" && detail.control && !detail.control.test_revealed) {
    head.querySelector("h2").append(el("button", {
      class: "btn small", style: "margin-left:0.4rem",
      onclick: () => confirmRevealTests(detail.experiment_id),
    }, "揭示测试结果"));
  }
  container.append(head);
  if (detail.kind === "hitl") {
    barHost = controlBar(detail);
    container.append(barHost);
  }
  if ((detail.fold_returns || []).length) {
    const metrics = detail.metrics || {};
    const sharpe = metrics.mean_test_sharpe;
    const charts = el("div", { class: "section-gap" },
      el("h4", {}, "日度累计收益 vs 沪深300（含回撤）"),
      equityHost(detail.experiment_id, equityFingerprint(detail), { width: 980, height: 240, ddH: 90 }),
    );
    // Tile order standardized with the homepage hero: Held-out → test → valid.
    container.append(el("div", { class: "panel section-gap" },
      statTilesRow([
        { label: "Held-out 收益（最终样本外）", value: fmtPct(metrics.cum_heldout_return), cls: signCls(metrics.cum_heldout_return) },
        { label: "累计测试收益", value: fmtPct(metrics.cum_test_return), cls: signCls(metrics.cum_test_return) },
        { label: "累计验证收益", value: fmtPct(metrics.cum_valid_return), cls: signCls(metrics.cum_valid_return) },
        { label: "平均测试 Sharpe", value: sharpe === null || sharpe === undefined ? "—" : Number(sharpe).toFixed(2), cls: signCls(sharpe) },
        { label: "会话进度", value: `${detail.completed_sessions ?? 0} / ${detail.total_sessions ?? "?"}` },
      ]),
      charts,
    ));
  }
  container.append(stepTreePanel(detail));
  const layout = el("div", { class: "detail section-gap" });
  // Panels read the global detailView (held-out equity/style card): set it
  // BEFORE building them, or the first render of a detail page silently
  // skips those blocks (and could chart the previous experiment's data).
  detailView = { experimentId, detail, listHost: null, rightHost: null, barHost: null, selectedKey };
  const listHost = sessionListPanel(detail, selectedKey);
  const rightHost = sessionDetailPanel(detail, selectedKey);
  detailView.listHost = listHost;
  detailView.rightHost = rightHost;
  detailView.barHost = barHost;
  layout.append(listHost, rightHost);
  container.append(layout);
  $main.innerHTML = "";
  $main.append(container);
  pollTimer = setInterval(async () => {
    try {
      const fresh = await api(`/api/experiments/${encodeURIComponent(experimentId)}/status`);
      const freshState = fresh.state;
      const raw = fresh.raw_status || {};
      const badge = document.querySelector(".page-head .badge");
      if (badge && !badge.className.includes(`state-${freshState}`)) route(true); // fetch fresh detail on state change
      else if (raw.session_key && raw.session_key !== (status.session_key || null)) route(true);
      else if (String(raw.run_id || "") !== String(status.run_id || "")) route(true);
    } catch { /* transient */ }
  }, 4000);
}

/* Full creation-parameter record (params.json), grouped: explicit settings
   first, metadata last; values rendered verbatim so the researcher sees the
   exact configuration this experiment was built from. */
async function openParamsModal(detail) {
  const params = detail.params || {};
  // The create form only persists values that differ from the defaults, so the
  // full effective configuration = schema defaults overlaid with params.json.
  let schemaFields = [];
  try {
    const schema = await api("/api/parameter-schema");
    schemaFields = (schema.groups || []).flatMap((group) => group.fields || []);
  } catch { /* fall back to explicit params only */ }
  const render = (value) => el("code", {}, typeof value === "object" ? JSON.stringify(value) : String(value));
  const explicitRows = [];
  const defaultRows = [];
  const covered = new Set();
  for (const field of schemaFields) {
    covered.add(field.key);
    if (Object.prototype.hasOwnProperty.call(params, field.key)) {
      explicitRows.push(kvRow(el("span", {}, field.label, el("div", { class: "hint", style: "margin:0" }, field.key)),
        render(params[field.key])));
    } else {
      defaultRows.push(kvRow(el("span", {}, field.label, el("div", { class: "hint", style: "margin:0" }, field.key)),
        el("span", { class: "hint" }, render(field.default ?? "—").textContent)));
    }
  }
  // Anything persisted outside the schema (metadata, inherited artifact, …).
  const extraRows = Object.keys(params).filter((key) => !covered.has(key)).sort()
    .map((key) => kvRow(key, render(params[key])));
  const body = el("div", { class: "params-modal-body" },
    el("p", { class: "hint" },
      "创建时显式设置的参数在前；其余按创建表单默认生效（灰色）。运行期实际生效值以 run manifest / snapshot manifest 为准。"),
    explicitRows.length ? el("h4", {}, `显式设置（${explicitRows.length}）`) : null,
    explicitRows.length ? el("table", { class: "kv" }, ...explicitRows) : null,
    el("h4", {}, `默认值（${defaultRows.length}）`),
    el("table", { class: "kv" }, ...defaultRows),
    extraRows.length ? el("h4", {}, "元数据 / 其他") : null,
    extraRows.length ? el("table", { class: "kv" }, ...extraRows) : null,
  );
  showModal(`创建参数 · ${detail.experiment_id}`, body,
    [el("button", { class: "btn", onclick: closeModal }, "关闭")]);
}

function controlBar(detail) {
  const id = detail.experiment_id;
  const control = detail.control || { mode: "manual", request: null };
  const state = detail.state;
  const alive = detail.worker_alive;
  const send = (payload, note) => sendControlAction(id, payload, note);
  const bar = el("div", { class: "panel control-bar section-gap" });
  bar.append(el("span", { class: "mode-note" }, "运行模式："));
  const modeSelect = el("select", {
    onchange: () => send({ action: "set_mode", mode: modeSelect.value }, `模式已切换为 ${modeSelect.value}`),
  },
    el("option", { value: "manual" }, "逐会话批准"),
    el("option", { value: "step" }, "逐 Step 批准（最细）"),
    el("option", { value: "auto" }, "自动运行（连续执行）"),
  );
  modeSelect.value = control.mode;
  bar.append(modeSelect);
  if (control.request === "pause") bar.append(el("span", { class: "badge state-paused" }, "已请求暂停"));
  if (control.request === "stop") bar.append(el("span", { class: "badge state-stopped" }, "已请求停止"));
  if (control.skip_to_heldout) bar.append(el("span", { class: "badge state-waiting_user" }, "已请求提前收官"));
  bar.append(el("span", { class: "spacer" }));
  // Early finish: skip the remaining folds and jump straight to Held-out with
  // the latest frozen artifact (needs at least one recorded fold).
  if (detail.state !== "completed") {
    if (!control.skip_to_heldout && (detail.folds_recorded || 0) > 0) {
      bar.append(el("button", {
        class: "btn",
        onclick: () => {
          showModal("提前进入 Held-out", el("p", {},
            "跳过剩余全部 Fold（及后续元学习），直接以最新冻结策略进入 Held-out 冻结测试。已完成的 Fold 不受影响；人工控制模式下 Held-out 会话仍需批准。确定？"), [
            el("button", { class: "btn", onclick: closeModal }, "取消"),
            el("button", { class: "btn primary", onclick: () => { closeModal(); send({ action: "skip_to_heldout" }, "已请求提前进入 Held-out"); } }, "确认提前收官"),
          ]);
        },
      }, "提前收官 → Held-out"));
    } else if (control.skip_to_heldout) {
      bar.append(el("button", { class: "btn", onclick: () => send({ action: "cancel_skip_to_heldout" }, "已取消提前收官") }, "取消提前收官"));
    }
  }
  if (alive) {
    if (control.request !== "pause") {
      bar.append(el("button", { class: "btn", onclick: () => send({ action: "pause" }, "将在当前 Fold 结束后暂停") }, "暂停"));
    } else {
      bar.append(el("button", { class: "btn primary", onclick: () => send({ action: "resume" }, "已继续") }, "继续"));
    }
    bar.append(el("button", { class: "btn", onclick: () => send({ action: "stop" }, "将在当前会话结束后停止") }, "停止"));
    bar.append(el("button", {
      class: "btn danger",
      onclick: () => {
        showModal("强制终止", el("p", {}, "立即向 worker 发送 SIGTERM。正在运行的 Fold 会被中断且不写入账本（恢复时将重跑该 Fold）。确定？"), [
          el("button", { class: "btn", onclick: closeModal }, "取消"),
          el("button", {
            class: "btn danger",
            onclick: () => {
              closeModal();
              // The request blocks through the 10s SIGTERM grace; say so up
              // front, then report the actual outcome from the response.
              toast("正在终止 worker（优雅退出宽限最长约 10 秒）…");
              send({ action: "terminate" }, (result) => result.escalated
                ? `已强制终止（SIGKILL，pid ${result.terminated_pid}）`
                : `worker 已优雅退出（pid ${result.terminated_pid}）`);
            },
          }, "强制终止"),
        ]);
      },
    }, "强制终止"));
    bar.append(el("button", {
      class: "btn",
      onclick: () => {
        showModal("确认重启？", el("div", {},
          el("p", {}, "终止当前 worker 并立即按账本恢复运行：已完成会话保留，被中断的会话整体重跑。"),
        ), [
          el("button", { class: "btn", onclick: closeModal }, "取消"),
          el("button", { class: "btn primary", onclick: () => { closeModal(); send({ action: "restart" }, "已重启 worker"); } }, "确认重启"),
        ]);
      },
    }, "重启"));
  } else if (["interrupted", "stopped", "failed", "created"].includes(state)) {
    bar.append(el("button", { class: "btn primary", onclick: () => send({ action: "resume" }, "已请求恢复运行") }, "恢复运行"));
  }
  return bar;
}

function sessionListPanel(detail, selectedKey) {
  const panel = el("div", { class: "panel" }, el("h4", {}, "会话（元学习 / Fold / Held-out）"));
  const list = el("div", { class: "session-list" });
  const status = detail.status || {};
  let currentEpoch = null;
  for (const session of detail.sessions || []) {
    if (session.epoch_id !== currentEpoch && session.kind !== "heldout") {
      currentEpoch = session.epoch_id;
      list.append(el("div", { class: "epoch-head" }, `Epoch ${String(currentEpoch).replace("epoch_", "")}`));
    }
    const isDone = Boolean(session.record || (session.records || []).length);
    const isCurrent = status.session_key === session.key && detail.worker_alive;
    const isWaiting = isCurrent && detail.state === "waiting_user";
    const dotClass = isDone ? "done" : isWaiting ? "waiting" : isCurrent ? "running" : "pending";
    const validReturn = session.record && session.record.validation_result ? session.record.validation_result.total_return : null;
    const stateText = isCurrent && status.state === "waiting_step_user"
      ? `Step ${status.awaiting_step ?? "?"} 待批准`
      : isCurrent && status.state === "waiting_user_reply"
      ? "提问待答复"
      : isWaiting ? "待批准" : isCurrent ? "运行中" : "";
    const ret = session.kind === "fold" || session.kind === "meta_learning"
      ? foldDurationNode(
          detail,
          session,
          session.kind === "fold" && validReturn !== null && validReturn !== undefined
            ? fmtPct(validReturn) : stateText,
          session.kind === "fold" && validReturn !== null && validReturn !== undefined ? numClass(validReturn) : "",
        )
      : validReturn !== null && validReturn !== undefined
      ? el("span", { class: numClass(validReturn) }, fmtPct(validReturn))
      : el("span", {}, stateText);
    ret.classList.add("ret");
    const item = el("div", {
      class: `session-item${session.kind === "heldout" ? " phase-head" : ""}${session.key === selectedKey ? " selected" : ""}`,
      "data-key": session.key,
      onclick: () => { location.hash = `#/exp/${encodeURIComponent(detail.experiment_id)}/${sessionKeyToUrl(session.key)}`; },
    },
      el("span", { class: `dot ${dotClass}` }),
      el("span", { class: "label" },
        session.kind === "fold" ? String(session.fold_id || "").replace("fold_", "Fold ") : KIND_LABELS[session.kind] || session.kind),
      ret,
    );
    list.append(item);
  }
  panel.append(list);
  return panel;
}

function sessionDetailPanel(detail, selectedKey) {
  const session = (detail.sessions || []).find((entry) => entry.key === selectedKey);
  const panel = el("div", {});
  if (!session) {
    panel.append(el("div", { class: "panel" }, el("div", { class: "empty" }, "请选择左侧的会话")));
    return panel;
  }
  const status = detail.status || {};
  const isCurrent = status.session_key === session.key;
  const running = isCurrent && detail.worker_alive
    && ["running_session", "waiting_step_user", "waiting_user_reply"].includes(detail.state);
  const waiting = isCurrent && detail.state === "waiting_user";
  const done = Boolean(session.record || (session.records || []).length);

  // Directive editor for sessions that have not run yet — and for a recorded
  // fold whose re-run is waiting for approval (prompt edits land here).
  if (detail.kind === "hitl" && (!done || waiting) && !running) {
    panel.append(directivePanel(detail, session, waiting));
  }
  if (running) panel.append(askUserPanel(detail, session), stepGatePanel(detail, session), liveTracePanel(detail, session));
  if (session.kind === "fold" && done) {
    panel.append(foldResultPanel(detail, session));
    // The LLM strategy review gets its own card, peer to the fold result.
    panel.append(analysisPanel(detail.experiment_id, session.epoch_id, session.fold_id || (session.record || {}).fold_id));
    const recordedFolds = (detail.sessions || []).filter((s) => s.kind === "fold" && s.record);
    if (detail.kind === "hitl" && recordedFolds.length && recordedFolds[recordedFolds.length - 1].key === session.key) {
      panel.append(rerunPanel(detail, session));
    } else if (detail.kind === "hitl" && recordedFolds.some((s) => s.key === session.key)) {
      // Any earlier recorded fold can become the frontier again via rollback.
      panel.append(rollbackPanel(detail, session));
    }
  }
  if (session.kind === "meta_learning" && done) panel.append(metaResultPanel(detail, session));
  if (session.kind === "heldout" && done) panel.append(heldoutPanel(session));
  if (done && session.record && session.record.run_id) {
    const statsHost = el("div", {});
    panel.append(el("div", { class: "panel section-gap" },
      el("h4", {}, "Agent Trace（回放）"),
      statsHost,
      traceReplayNode(detail.experiment_id, session.record.run_id),
    ));
    (async () => {
      try {
        const stats = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stats?run_id=${encodeURIComponent(session.record.run_id)}`);
        statsHost.append(statsChipsRow(stats));
      } catch { /* trace may be absent for legacy runs */ }
    })();
  }
  if (!done && !running && !waiting) {
    panel.append(el("div", { class: "panel section-gap" }, el("div", { class: "empty" }, "该会话尚未开始。")));
  }
  return panel;
}

function directivePanel(detail, session, waiting) {
  const control = detail.control || { directives: {}, approved_sessions: [] };
  const isMeta = session.kind === "meta_learning";
  // A meta session with no per-session override inherits the experiment-level
  // directive from creation; prefill it so it never needs retyping.
  const inherited = isMeta ? String((detail.params || {}).meta_learning_directive || "") : "";
  const existing = (control.directives || {})[session.key] ?? "";
  const approved = (control.approved_sessions || []).includes(session.key);
  const textarea = el("textarea", { class: "directive", placeholder: "可选：为该会话注入研究方向 / 优化假设……" });
  textarea.value = existing || inherited;
  const panel = el("div", { class: "panel" },
    el("h4", {}, isMeta ? "元学习指令（本 Epoch）" : session.kind === "heldout" ? "Held-out 启动" : "本 Fold 研究者指令"),
  );
  if (session.kind !== "heldout") {
    if (isMeta && inherited && !existing) {
      panel.append(el("div", { class: "hint" }, "已预填创建实验时的元学习探索方向；不修改则按原方向执行，可直接编辑覆盖本 Epoch。"));
    }
    panel.append(textarea, el("div", { class: "hint warn" },
      "指令会注入系统提示词并记入账本。请勿写入测试期/Held-out 结果或具体日历日期——那会破坏 walk-forward 的样本外有效性。"));
  }
  const buttons = el("div", { class: "control-bar section-gap" });
  const send = (payload, note) => sendControlAction(detail.experiment_id, payload, note);
  if (session.kind !== "heldout") {
    if (session.kind === "fold" || session.kind === "meta_learning") {
      buttons.append(el("button", {
        class: "btn",
        onclick: () => openPromptEditor(detail, session, textarea.value),
      }, "编辑完整系统提示词"));
    }
    buttons.append(el("button", {
      class: "btn",
      onclick: () => openPromptPreview(detail, session, textarea.value, { approved, waiting, send }),
    }, "预览完整系统提示词"));
    if ((detail.control?.prompt_overrides || {})[session.key]) {
      buttons.append(el("span", { class: "badge state-waiting_user" }, "已覆盖系统提示词"));
    }
  }
  if ((detail.control || {}).mode !== "auto" && !approved) {
    buttons.append(el("button", {
      class: "btn primary",
      onclick: () => send({ action: "approve", session_key: session.key, directive: textarea.value }, "已批准，会话即将启动"),
    }, waiting ? "批准并启动" : "预先批准"));
  } else if (approved) {
    buttons.append(el("span", { class: "badge state-completed" }, "已批准"));
  }
  panel.append(buttons);
  // Pre-fold GPU allocation: live nvidia-smi inventory + per-session count.
  if (session.kind === "fold" && !session.record) panel.append(gpuAllocationRow(detail, session, send));
  if (waiting) panel.append(el("div", { class: "hint" }, "worker 正在等待此会话的批准。建议先预览完整系统提示词，确认注入内容无误后再批准。"));
  return panel;
}

/* GPU status + per-fold allocation picker, shown at the fold approval gate.
   The chosen count rides in control.gpu_counts[session_key]; the sandbox's
   "auto" selector then picks that many GPUs by free memory at start. */
function gpuAllocationRow(detail, session, send) {
  const current = ((detail.control || {}).gpu_counts || {})[session.key];
  const experimentDefault = Number((detail.params || {}).gpu_count || 1);
  const wrap = el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, "本 Fold GPU 分配"),
    el("div", { class: "hint" }, "批准前可查看实时资源并为本 Fold 沙箱设定 GPU 数；具体设备仍按空闲显存自动挑选。"));
  const statusHost = el("div", {}, el("div", { class: "hint" }, "GPU 状态加载中…"));
  const stamp = el("span", { class: "hint", style: "margin-left:auto" });
  const select = el("select", { class: "input" });
  select.append(el("option", { value: "" }, `实验默认（${experimentDefault} 块）`));
  for (let n = 1; n <= 4; n += 1) select.append(el("option", { value: String(n) }, `${n} 块`));
  if (current) select.value = String(current);
  const row = el("div", { class: "control-bar section-gap" },
    el("span", { class: "mode-note" }, "分配数量："), select,
    el("button", {
      class: "btn small",
      onclick: () => send(
        { action: "set_gpu_count", session_key: session.key, directive: select.value },
        select.value ? `本 Fold 将分配 ${select.value} 块 GPU` : "已恢复默认 GPU 分配",
      ),
    }, "保存"),
    current ? el("span", { class: "badge state-waiting_user" }, `已设 ${current} 块`) : null,
    stamp,
  );
  wrap.append(statusHost, row);
  const refresh = async () => {
    let payload;
    try { payload = await api("/api/gpus"); } catch (error) {
      statusHost.innerHTML = "";
      statusHost.append(el("div", { class: "hint" }, `GPU 状态加载失败：${error.message}`));
      return;
    }
    const gpus = payload.gpus || [];
    statusHost.innerHTML = "";
    if (!gpus.length) {
      statusHost.append(el("div", { class: "hint" },
        `无可用 GPU 信息${payload.error ? `（${payload.error}）` : ""}；将按默认配置运行`));
      return;
    }
    const grid = el("div", { class: "gpu-grid" });
    for (const gpu of gpus) {
      const usedPct = gpu.memory_total_mib
        ? Math.round(100 * (gpu.memory_total_mib - gpu.memory_free_mib) / gpu.memory_total_mib) : 0;
      const util = gpu.utilization_pct === null || gpu.utilization_pct === undefined ? "—" : `${gpu.utilization_pct}%`;
      const temp = gpu.temperature_c === null || gpu.temperature_c === undefined ? "—" : `${gpu.temperature_c}°C`;
      grid.append(el("div", { class: "gpu-row" },
        el("span", { class: "gpu-name" }, `GPU ${gpu.index} · ${gpu.name.replace(/^NVIDIA\s+/, "")}`),
        el("span", { class: "gpu-bar", title: `显存占用 ${usedPct}%` }, el("span", { style: `width:${usedPct}%` })),
        el("span", { class: "gpu-meta" },
          `空闲 ${(gpu.memory_free_mib / 1024).toFixed(1)}G / ${(gpu.memory_total_mib / 1024).toFixed(1)}G ｜ 算力 ${util} ｜ ${temp}`),
      ));
    }
    statusHost.append(grid);
    stamp.textContent = `实时检测 · ${new Date().toLocaleTimeString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" })}`;
  };
  refresh();
  // Live re-detection while the gate is open; dies with navigation (liveTimers).
  liveTimers.push(setInterval(() => { if (wrap.isConnected) refresh(); }, 60_000));
  return wrap;
}

/* POST one control action, then refresh the detail page in place (a full
   route() rebuild flashes the page). Shared by every control-sending panel. */
async function sendControlAction(experimentId, payload, note, { modal = false } = {}) {
  try {
    const result = await api(`/api/experiments/${encodeURIComponent(experimentId)}/control`, {
      method: "POST", body: JSON.stringify(payload),
    });
    if (note) toast(typeof note === "function" ? note(result) : note);
    if (modal) closeModal();
    refreshDetail();
  } catch (error) { toast(error.message, true); }
}

/* Re-fetch the experiment payload and swap both detail panels in place —
   control-state changes update without a page rebuild or scroll jump. */
async function refreshDetail() {
  if (!detailView) { route(); return; }
  try {
    const detail = await api(`/api/experiments/${encodeURIComponent(detailView.experimentId)}`);
    detailView.detail = detail;
    if (detailView.barHost) {
      const bar = controlBar(detail);
      detailView.barHost.replaceWith(bar);
      detailView.barHost = bar;
    }
    const list = sessionListPanel(detail, detailView.selectedKey);
    detailView.listHost.replaceWith(list);
    detailView.listHost = list;
    if (detailView.selectedKey) selectSession(detailView.selectedKey);
  } catch { route(); }
}

/* Full system-prompt editor: saves a verbatim per-session override. */
async function openPromptEditor(detail, session, directive) {
  const existing = (detail.control?.prompt_overrides || {})[session.key];
  let text = existing || "";
  if (!text) {
    try {
      const data = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/prompt-preview`, {
        method: "POST",
        body: JSON.stringify({ session_key: session.key, directive }),
      });
      text = data.prompt;
    } catch (error) { toast(`加载失败：${error.message}`, true); return; }
  }
  const editor = el("textarea", { class: "directive prompt-editor", spellcheck: "false" });
  editor.value = text;
  const send = (payload, note) => sendControlAction(detail.experiment_id, payload, note, { modal: true });
  const footer = [el("button", { class: "btn", onclick: closeModal }, "取消")];
  if (existing) {
    footer.push(el("button", {
      class: "btn danger",
      onclick: () => send({ action: "set_prompt_override", session_key: session.key, directive: "" }, "已清除覆盖，恢复自动装配"),
    }, "清除覆盖"));
  }
  footer.push(el("button", {
    class: "btn primary",
    onclick: () => send({ action: "set_prompt_override", session_key: session.key, directive: editor.value }, "已保存系统提示词覆盖"),
  }, "保存为本会话系统提示词"));
  showModal(`编辑系统提示词 — ${session.key}`,
    el("div", {},
      el("div", { class: "hint warn" },
        "保存后本会话将【原样】使用此文本作为系统提示词：运行时不再注入自动生成的「当前实验事实」JSON 与其它自动段落，请保留必要的协议/合同/禁止行为段落。清除覆盖即恢复自动装配。覆盖内容会记录进 run manifest 供审计。"),
      editor,
    ), footer, "prompt-modal");
}

/* Review-then-approve: assemble the session's system prompt (with the draft
   directive embedded) for inspection before the session is allowed to start. */
async function openPromptPreview(detail, session, directive, { approved, waiting, send }) {
  const override = (detail.control?.prompt_overrides || {})[session.key];
  let data;
  if (override) {
    data = { prompt: override, note: "当前已设置系统提示词覆盖，运行时将原样使用以下文本（不再自动装配）。" };
  } else {
    try {
      data = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/prompt-preview`, {
        method: "POST",
        body: JSON.stringify({ session_key: session.key, directive }),
      });
    } catch (error) { toast(`预览失败：${error.message}`, true); return; }
  }
  const footer = [el("button", { class: "btn", onclick: closeModal }, "关闭")];
  if ((detail.control || {}).mode !== "auto" && !approved) {
    footer.push(el("button", {
      class: "btn primary",
      onclick: () => {
        closeModal();
        send({ action: "approve", session_key: session.key, directive }, "已批准，会话即将启动");
      },
    }, waiting ? "确认无误，批准并启动" : "确认无误，预先批准"));
  }
  showModal(`系统提示词预览 — ${session.key}`,
    el("div", {},
      el("div", { class: "hint" }, data.note),
      el("pre", { class: "code-view section-gap", style: "white-space:pre-wrap; max-height:58vh" }, data.prompt),
      el("div", { class: "hint" }, `共 ${data.prompt.length} 字符。修改指令请关闭后在指令框编辑，再重新预览。`),
    ),
    footer);
}

const STAT_CHIPS = [
  ["llm_call", "🤖 LLM"],
  ["web_search", "🔍 搜索"],
  ["web_fetch", "🌐 抓取"],
  ["backtest", "📊 回测"],
  ["shell", "🖥 Shell"],
  ["explore", "🧭 Explore"],
  ["read", "📄 读取"],
  ["context_compaction", "🗜 压缩"],
];

function fmtTokens(count) {
  const n = Number(count) || 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)} M tokens`;
  if (n >= 1000) return `${Math.round(n / 1000)} k tokens`;
  return `${n} tokens`;
}

function statsChipsRow(stats) {
  const counts = stats.counts || {};
  const row = el("div", { class: "stats-chips" });
  for (const [key, label] of STAT_CHIPS) {
    if (!counts[key]) continue;
    row.append(el("span", { class: "stat-chip" }, `${label} ${counts[key]}`));
  }
  if (stats.backtest_wall_seconds) {
    row.append(el("span", { class: "stat-chip" }, `⏸ 预算暂停/回补 Σ ${fmtDuration(stats.backtest_wall_seconds)}`));
  }
  if (stats.in_backtest) {
    const progress = stats.backtest_progress || {};
    const done = Number(progress.day_index) || 0;
    const total = Number(progress.total_days) || 0;
    row.append(el("span", { class: "stat-chip" }, total && done
      ? `⏳ 回测已完成 ${done}/${total} 日（${Number(progress.percent || 0).toFixed(1)}%）`
      : "⏳ 回测初始化 / 首个交易日处理中"));
    if (progress.activity === "nl" && progress.activity_status === "running") {
      row.append(el("span", { class: "stat-chip" }, `🧠 NL 分析中（第 ${Number(progress.nl_call_index) || 1} 次）`));
    }
  }
  if (stats.llm_prompt_tokens || stats.llm_completion_tokens) {
    row.append(
      el("span", { class: "stat-chip" }, `输入 ${fmtTokens(stats.llm_prompt_tokens)}`),
      el("span", { class: "stat-chip" }, `输出 ${fmtTokens(stats.llm_completion_tokens)}`),
    );
  } else if (stats.llm_total_tokens) {
    row.append(el("span", { class: "stat-chip" }, `Σ ${fmtTokens(stats.llm_total_tokens)}`));
  }
  return row;
}

/* ask_user tool: when the Agent pauses on a question (state=waiting_user_reply),
   show it and send the researcher's reply (empty reply = proceed, Agent decides).
   The wait is excluded from the Agent's reasoning budget. */
function currentStrategyDownloadNode(detail) {
  const host = el("span", {}, el("button", {
    class: "btn", disabled: "", title: "正在确认是否已有正式验证 Step 快照",
  }, "策略快照检查中…"));
  api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/current-step`)
    .then((payload) => {
      host.innerHTML = "";
      if (payload.available) {
        host.append(el("a", {
          class: "btn",
          href: `/api/experiments/${encodeURIComponent(detail.experiment_id)}/current-step/source.zip`,
          title: "下载最近一次正式验证 Step 保存的只读策略快照",
        }, "下载当前策略"));
      } else {
        host.append(el("button", {
          class: "btn", disabled: "", title: payload.reason || "尚无正式验证 Step 快照",
        }, "暂无策略快照"));
      }
    })
    .catch(() => {
      host.innerHTML = "";
      host.append(el("button", { class: "btn", disabled: "" }, "策略快照不可用"));
    });
  return host;
}

function askUserPanel(detail, session) {
  const status = detail.status || {};
  const question = status.awaiting_question || null;
  if (detail.kind !== "hitl" || status.state !== "waiting_user_reply"
      || status.session_key !== session.key || !question) return el("span", {});
  const textarea = el("textarea", { class: "directive",
    placeholder: "方向性指引（作为研究者答复注入对话；留空=让 Agent 自行决策）……" });
  const send = (reply, message) => sendControlAction(
    detail.experiment_id, { action: "reply_question", session_key: session.key, directive: reply }, message);
  return el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, `Agent 提问 #${question.index ?? "?"}（等待不消耗推理预算）`),
    el("div", { class: "ask-user-question" }, String(question.question || "")),
    textarea,
    el("div", { class: "control-bar" },
      currentStrategyDownloadNode(detail),
      el("button", { class: "btn primary", onclick: () => send(textarea.value, "已答复，Agent 继续") }, "答复并继续"),
      el("button", { class: "btn", onclick: () => send("", "已放行（无指引）") }, "不给指引，继续"),
    ),
  );
}

/* Step-level HITL: toggle per-session gating and, when the worker is holding
   at a step (state=waiting_step_user), show the step result and release it
   with an optional per-step directive (injected into the tool observation). */
function stepGatePanel(detail, session) {
  if (session.kind !== "fold" || detail.kind !== "hitl") return el("span", {});
  const control = detail.control || {};
  const status = (detail.status || {});
  const override = (control.step_gate || {})[session.key];
  const enabled = override === undefined ? control.mode === "step" : Boolean(override);
  const send = (payload, message) => sendControlAction(detail.experiment_id, payload, message);
  const panel = el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, "逐 Step 门控"),
    el("div", { class: "hint" },
      (detail.control || {}).mode === "step"
        ? "运行模式为「逐 Step 批准」：所有 Fold 默认开启门控，此处仅用于为本 Fold 单独例外（关闭/恢复默认）。"
        : "为本 Fold 单独开启：每次正式验证回测完成即暂停等待批准，可在放行时注入 Step 级指令（等待不消耗推理预算）。全局默认请用运行模式「逐 Step 批准」。"),
    el("div", { class: "control-bar" },
      el("button", {
        class: enabled ? "btn small" : "btn small primary",
        onclick: () => send(
          { action: "set_step_gate", session_key: session.key, directive: enabled ? "0" : "1" },
          enabled ? "已关闭本 Fold 逐 Step 门控" : "已开启本 Fold 逐 Step 门控",
        ),
      }, enabled ? "关闭门控" : "开启门控"),
      override !== undefined && control.mode === "step" ? el("button", {
        class: "btn small",
        onclick: () => send({ action: "set_step_gate", session_key: session.key, directive: "" }, "已恢复模式默认"),
      }, "恢复模式默认") : null,
      enabled ? el("span", { class: "badge state-waiting_user" },
        override === undefined ? "门控开启（逐 Step 模式）" : "门控已开启") : null,
    ),
  );
  if (status.state === "waiting_step_user" && status.session_key === session.key) {
    const summary = status.step_summary || {};
    const diagnostics = (summary.diagnostic_warnings || []).map((raw) => {
      const message = String(raw);
      const legacyMemory = message.match(/^Agent process peak RSS was ([0-9.]+ GiB)\./);
      return legacyMemory
        ? { message: `性能参考：本次回测策略进程峰值内存约 ${legacyMemory[1]}，不影响验证结果。`, warning: false }
        : { message, warning: !message.startsWith("性能参考：") };
    });
    const textarea = el("textarea", { class: "directive section-gap",
      placeholder: "可选：本 Step 结果的针对性指令（作为待检验假设注入下一轮对话）……" });
    panel.append(
      el("div", { class: "section-gap" }, statTilesRow([
        { label: `Step ${status.awaiting_step ?? "?"} 验证收益`, value: fmtPct(summary.total_return), cls: signCls(summary.total_return) },
        { label: "Sharpe", value: summary.sharpe === null || summary.sharpe === undefined ? "—" : Number(summary.sharpe).toFixed(2) },
        { label: "最大回撤", value: fmtPct(summary.max_drawdown) },
        { label: "完整验证", value: summary.complete_validation ? "是" : "否" },
        { label: "本 Fold 已耗时", value: foldDurationNode(detail, session) },
      ])),
      ...diagnostics.map((item) => el("div", { class: item.warning ? "hint warn" : "hint" }, item.message)),
      analysisNode(
        `/api/experiments/${encodeURIComponent(detail.experiment_id)}/current-step/analysis`,
        "当前 Step DeepSeek 分析（可选，仅验证期证据）",
        { standalone: false },
      ),
      textarea,
      el("div", { class: "control-bar" },
        currentStrategyDownloadNode(detail),
        el("button", {
          class: "btn primary",
          onclick: () => send(
            { action: "approve_step", session_key: session.key, directive: textarea.value },
            "已放行该 Step",
          ),
        }, "批准并继续"),
      ),
    );
  }
  return panel;
}

function liveTracePanel(detail, session) {
  // Reuse the streaming panel across page rebuilds and session navigation so
  // the SSE stream, scroll position, and accumulated events survive.
  const status = detail.status || {};
  const runId = String(status.run_id || "");
  if (livePanel && livePanel.expId === detail.experiment_id && livePanel.key === session.key
      && livePanel.runId === runId
      && !livePanel.closed && (!livePanel.source || livePanel.source.readyState !== EventSource.CLOSED)) {
    const reused = livePanel;
    reused.refresh(detail);
    // The reused node is attached to its new parent only after this function
    // returns. Wait through the following layout frame before restoring the
    // bottom position; one frame is too early on control-mode panel rebuilds.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (livePanel === reused) reused.scrollToBottom();
    }));
    return reused.node;
  }
  destroyLivePanel();
  const panel = el("div", { class: "panel section-gap" }, el("h4", {}, `实时 Agent Trace — ${session.key}`));
  const tools = el("div", { class: "trace-tools" });
  const box = el("div", { class: "trace-box" });
  let autoScroll = true;
  const scrollToggle = el("label", {}, el("input", {
    type: "checkbox", checked: "checked",
    onchange: (event) => { autoScroll = event.target.checked; },
  }), " 自动滚动");
  const countdown = el("span", {
    class: "badge state-running_session", style: "display:none",
    title: "Agent 会话预算 = 名义 deadline + 已回补的回测墙钟；等待研究者时暂停",
  });
  tools.append(el("span", { class: "badge state-running_session" }, "streaming"), countdown, scrollToggle);
  const statsHost = el("div", {});
  const prepText = el("span", {}, "");
  const prep = el("div", { class: "prep-indicator" }, el("span", { class: "spinner" }), prepText);
  panel.append(tools, statsHost, prep, box);
  let sawEvent = false;
  let traceKnown = Boolean(status.trace_path && status.fold_deadline_at);
  let source = null;
  const runQuery = runId ? `run_id=${encodeURIComponent(runId)}&` : "";
  const appendEvents = (events) => {
    if (!events.length) return;
    sawEvent = true;
    prep.style.display = "none";
    const fragment = document.createDocumentFragment();
    for (const event of events) fragment.append(traceEventNode(event));
    box.append(fragment);
    while (box.children.length > 400) box.firstChild.remove();
    if (autoScroll) box.scrollTop = box.scrollHeight;
  };
  const openStream = (offset) => {
    if (!livePanel || livePanel.node !== panel || livePanel.closed) return;
    source = new EventSource(
      `/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stream?${runQuery}offset=${offset}`,
    );
    livePanel.source = source;
    source.onmessage = (event) => {
      try { appendEvents([JSON.parse(event.data)]); } catch { /* skip malformed */ }
    };
    source.addEventListener("eof", () => {
      box.append(el("div", { class: "hint" }, "—— trace 结束 ——"));
      source.close();
    });
    source.onerror = () => { /* EventSource auto-reconnects */ };
  };
  let deadlineMs = status.fold_deadline_at ? Date.parse(status.fold_deadline_at) : null;
  let startedMs = status.session_started_at ? Date.parse(status.session_started_at) : Date.now();
  let sessionState = detail.state;
  let creditSeconds = 0;
  let inBacktest = false;
  let activeBacktestStartedMs = null;
  let backtestProgress = null;
  let agentSessionEnded = false;
  let statsSignature = "";
  const tick = () => {
    if (!sawEvent) {
      if (traceKnown) {
        prepText.textContent = "正在加载 Agent Trace…";
      } else {
        const elapsed = (Date.now() - startedMs) / 1000;
        prepText.textContent = `沙箱与数据快照准备中（已 ${fmtDuration(elapsed)}）… 首个 Agent 事件到达后开始流式显示`;
      }
    }
    if (sessionState === "waiting_step_user") {
      countdown.style.display = "";
      countdown.textContent = "等待 Step 批准（Agent 会话计时暂停）";
    } else if (sessionState === "waiting_user_reply") {
      countdown.style.display = "";
      countdown.textContent = "等待研究者答复（Agent 会话计时暂停）";
    } else if (agentSessionEnded && sessionState === "running_session") {
      countdown.style.display = "";
      countdown.textContent = "Agent 推理已结束；Environment 正在验收、评估或落盘（不消耗 Agent 预算）";
    } else if (deadlineMs && sessionState === "running_session") {
      countdown.style.display = "";
      if (inBacktest) {
        const activeSeconds = activeBacktestStartedMs ? Math.max(0, (Date.now() - activeBacktestStartedMs) / 1000) : 0;
        const done = Number((backtestProgress || {}).day_index) || 0;
        const total = Number((backtestProgress || {}).total_days) || 0;
        const progressText = total && done
          ? `已完成 ${done}/${total} 日，${Number((backtestProgress || {}).percent || 0).toFixed(1)}%`
          : "初始化 / 首个交易日处理中";
        const activity = backtestProgress || {};
        const activityStartedMs = activity.activity_started_at ? Date.parse(activity.activity_started_at) : null;
        const nlText = activity.activity === "nl" && activity.activity_status === "running"
          ? `；NL 第 ${Number(activity.nl_call_index) || 1} 次分析中 ${fmtDuration(activityStartedMs ? Math.max(0, (Date.now() - activityStartedMs) / 1000) : 0)}`
          : "";
        countdown.textContent = `回测执行中（${progressText}${nlText}；本次 ${fmtDuration(activeSeconds)}，结束后回补）`;
      } else {
        const remain = (deadlineMs + creditSeconds * 1000 - Date.now()) / 1000;
        countdown.textContent = remain >= 0
          ? `Agent 活跃会话预算剩余 ${fmtDuration(remain)}（含暂停回补 ${fmtDuration(creditSeconds)}）`
          : `收尾中 +${fmtDuration(-remain)}`;
      }
    } else {
      countdown.style.display = "none";
    }
  };
  const pollStats = async () => {
    try {
      const stats = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stats?${runQuery}`);
      creditSeconds = Number(stats.backtest_wall_seconds) || 0;
      inBacktest = Boolean(stats.in_backtest);
      activeBacktestStartedMs = stats.active_backtest_started_at ? Date.parse(stats.active_backtest_started_at) : null;
      backtestProgress = stats.backtest_progress || null;
      agentSessionEnded = Number((stats.counts || {}).session_end || 0) > 0;
      const signature = JSON.stringify([stats.counts, stats.backtest_wall_seconds,
        stats.llm_prompt_tokens, stats.llm_completion_tokens, stats.llm_total_tokens,
        stats.in_backtest, stats.backtest_progress]);
      if (signature !== statsSignature) {
        statsSignature = signature;
        statsHost.replaceChildren(statsChipsRow(stats));
      }
    } catch { /* trace not started yet */ }
    try {
      const fresh = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/status`);
      const raw = fresh.raw_status || {};
      // This panel is pinned to one run.  A newer run is rebuilt by the outer
      // detail poll; never borrow its state/deadline during that short window.
      if (String(raw.run_id || "") === runId) {
        sessionState = fresh.state;
        deadlineMs = raw.fold_deadline_at ? Date.parse(raw.fold_deadline_at) : null;
        if (raw.session_started_at) startedMs = Date.parse(raw.session_started_at);
        traceKnown = Boolean(raw.trace_path && raw.fold_deadline_at);
      }
    } catch { /* transient */ }
  };
  tick();
  const timers = [setInterval(tick, 1000), setInterval(pollStats, 5000)];
  livePanel = {
    expId: detail.experiment_id,
    key: session.key,
    runId,
    node: panel,
    source: null,
    closed: false,
    timers,
    scrollToBottom: () => { if (autoScroll) box.scrollTop = box.scrollHeight; },
    refresh: (freshDetail) => {
      const rawStatus = freshDetail.status || {};
      sessionState = freshDetail.state;
      deadlineMs = rawStatus.fold_deadline_at ? Date.parse(rawStatus.fold_deadline_at) : null;
      traceKnown = Boolean(rawStatus.trace_path && rawStatus.fold_deadline_at);
    },
  };
  (async () => {
    let offset = 0;
    try {
      const tail = await api(
        `/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace?${runQuery}tail_events=200`,
      );
      if (tail.history_truncated) {
        box.append(el("div", { class: "hint" }, "仅显示最近 200 条事件；完整记录可在运行结束后回放或下载。"));
      }
      appendEvents(tail.events || []);
      offset = Number(tail.next_offset) || 0;
    } catch { /* trace may not exist while the sandbox is being prepared */ }
    openStream(offset);
    pollStats();
  })();
  return panel;
}

/* Replay loader: batched pages, collapsible, with a raw .jsonl download so the
   browser never has to hold a 20MB trace as DOM just to archive it. */
function traceReplayNode(experimentId, runId) {
  const box = el("div", { class: "trace-box", style: "display:none" });
  const info = el("span", { class: "hint", style: "margin:0" }, "");
  let offset = 0, loadedEvents = 0, eof = false, expanded = false, loading = false;
  const moreButton = el("button", { class: "btn small", style: "display:none" }, "继续加载");
  const toggleButton = el("button", { class: "btn small" }, "加载 trace");
  async function loadBatch() {
    if (loading || eof) return;
    loading = true;
    moreButton.disabled = true;
    try {
      // ~2MB / batch keeps even huge traces incremental.
      for (let page = 0; page < 4 && !eof; page += 1) {
        const data = await api(`/api/experiments/${encodeURIComponent(experimentId)}/trace?run_id=${encodeURIComponent(runId)}&offset=${offset}`);
        for (const event of data.events) box.append(traceEventNode(event));
        loadedEvents += data.events.length;
        offset = data.next_offset;
        eof = Boolean(data.eof);
      }
      info.textContent = `已加载 ${loadedEvents} 个事件${eof ? "（全部）" : "，可继续加载"}`;
      if (eof) box.append(el("div", { class: "hint" }, "—— trace 结束 ——"));
    } catch (error) {
      info.textContent = `加载失败：${error.message}`;
    } finally {
      loading = false;
      moreButton.disabled = false;
      moreButton.style.display = eof || !expanded ? "none" : "";
    }
  }
  toggleButton.addEventListener("click", async () => {
    expanded = !expanded;
    box.style.display = expanded ? "" : "none";
    toggleButton.textContent = expanded ? "收起 trace" : loadedEvents ? "展开 trace" : "加载 trace";
    moreButton.style.display = expanded && !eof && loadedEvents ? "" : "none";
    if (expanded && !loadedEvents) await loadBatch();
  });
  moreButton.addEventListener("click", loadBatch);
  const download = el("a", {
    class: "btn small",
    href: `/api/experiments/${encodeURIComponent(experimentId)}/trace/download?run_id=${encodeURIComponent(runId)}`,
  }, "⬇ 下载完整 .jsonl");
  return el("div", {}, el("div", { class: "control-bar" }, toggleButton, moreButton, download, info), box);
}

/* Event rendering (Claude-Code-like): LLM natural-language output rides in
   full; reasoning and tool payloads collapse and materialize only on expand,
   so the DOM stays light even over long traces. */
function lazyDetails(summaryText, build) {
  const details = el("details", {}, el("summary", {}, summaryText));
  details.addEventListener("toggle", () => {
    if (details.open && !details.__filled) {
      details.__filled = true;
      details.append(build());
    }
  });
  return details;
}

const TOOL_BRIEF_KEYS = [
  "action", "tool", "cmd", "command", "pattern", "path", "query", "engine", "perspective",
  "mode", "result_name", "replay_window", "exit_code", "duration_seconds", "replay_wall_seconds",
  "reason", "error_type", "name", "trade_date", "day_index", "total_days", "percent",
  "elapsed_seconds", "orders_so_far", "activity", "activity_status", "nl_call_index",
  "activity_elapsed_seconds",
];

function traceEventNode(event) {
  const type = event.event_type || "event";
  const node = el("div", { class: "trace-event" });
  const time = fmtTsTime(event.ts);
  const head = el("div", { class: "head" },
    el("span", { class: `type ${type}` }, type),
    el("span", {}, time),
    event.step_id ? el("span", {}, event.step_id) : null,
    event.phase ? el("span", {}, event.phase) : null,
    event.status ? el("span", {}, String(event.status)) : null,
  );
  node.append(head);
  if (type === "llm_call" || type === "explore_llm_call") {
    const toolCalls = (event.tool_calls || []).map((call) => call.function && call.function.name).filter(Boolean);
    if (toolCalls.length) head.append(el("span", {}, `→ ${toolCalls.join(", ")}`));
    const reasoning = String(event.reasoning_content || "");
    if (reasoning) {
      node.append(lazyDetails(`推理过程（${(reasoning.length / 1000).toFixed(1)}k 字符）`, () => el("pre", {}, reasoning)));
    }
    const content = String(event.content || "").trim();
    if (content) node.append(el("div", { class: "llm-content" }, content));
  } else if (event.raw) {
    node.append(el("pre", {}, String(event.raw).slice(0, 400)));
  } else {
    const brief = TOOL_BRIEF_KEYS
      .filter((key) => event[key] !== undefined && event[key] !== null && event[key] !== "")
      .map((key) => `${key}=${String(typeof event[key] === "object" ? JSON.stringify(event[key]) : event[key]).slice(0, 120)}`)
      .join("  ");
    if (brief) head.append(el("span", { class: "tool-brief" }, brief));
  }
  node.append(lazyDetails("完整事件 JSON", () => el("pre", {}, JSON.stringify(event, null, 2))));
  return node;
}

/* Re-run the latest recorded fold with a revised directive / system prompt. */
function rerunPanel(detail, session) {
  const alive = detail.worker_alive;
  const panel = el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, "重跑本 Fold（最新完成）"),
    el("div", { class: "hint" },
      "追加一次全新的 Fold 会话：账本新增记录（旧记录保留供审计），冻结产物以重跑标签另存，已有 Held-out 结果将在重跑后自动重放。启动后在本会话的指令面板修改指令或系统提示词，再批准运行。"),
  );
  const bar = el("div", { class: "control-bar section-gap" });
  if (alive) {
    bar.append(el("span", { class: "hint warn", style: "margin:0" }, "worker 运行中——先「停止」或「强制终止」后方可重跑。"));
  } else {
    bar.append(el("button", {
      class: "btn primary",
      onclick: () => {
        showModal("确认重跑该 Fold？", el("div", {},
          el("p", {}, `将重跑 ${session.key}，并使现有 Held-out 结果过期（重跑完成后自动重放 Held-out）。`),
          el("p", { class: "hint" }, "重跑会话默认等待批准：批准前可修改本 Fold 指令或编辑完整系统提示词。"),
        ), [
          el("button", { class: "btn", onclick: closeModal }, "取消"),
          el("button", {
            class: "btn primary",
            onclick: async () => {
              closeModal();
              try {
                await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, {
                  method: "POST",
                  body: JSON.stringify({ action: "rerun_fold", session_key: session.key }),
                });
                toast("重跑已启动，等待批准");
                route(true);
              } catch (error) { toast(error.message, true); }
            },
          }, "确认重跑"),
        ]);
      },
    }, "修改提示词并重跑"));
  }
  panel.append(bar);
  return panel;
}

/* Roll the experiment back so this (earlier) fold becomes the frontier:
   every later ledger record is dropped (frozen dirs archived, ledger backed
   up server-side) and the run resumes from the next fold. */
function rollbackPanel(detail, session) {
  const alive = detail.worker_alive;
  const panel = el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, "回滚到此 Fold"),
    el("div", { class: "hint" },
      "把实验进度回退到本 Fold 刚完成时：其后所有 Fold、后续 Epoch 元学习与全部 Held-out 账本记录将被移除（原账本自动备份、冻结产物归档到 _archive，可人工找回），随后从下一个 Fold 继续（人工控制模式下等待批准，可先修改指令/提示词）。"),
  );
  const bar = el("div", { class: "control-bar section-gap" });
  if (alive) {
    bar.append(el("span", { class: "hint warn", style: "margin:0" }, "worker 运行中——先「停止」或「强制终止」后方可回滚。"));
  } else {
    bar.append(el("button", {
      class: "btn danger",
      onclick: () => {
        showModal("确认回滚？", el("div", {},
          el("p", {}, `将把实验回退到 ${session.key} 完成时点，丢弃其后全部账本记录（含 Held-out）。`),
          el("p", { class: "hint" }, "账本会先备份（experiment_ledger.rollback_*.jsonl），被丢弃的冻结产物移入 strategy_artifacts/_archive/。此操作不可从界面撤销。"),
        ), [
          el("button", { class: "btn", onclick: closeModal }, "取消"),
          el("button", {
            class: "btn danger",
            onclick: async () => {
              closeModal();
              try {
                await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, {
                  method: "POST",
                  body: JSON.stringify({ action: "rollback_fold", session_key: session.key }),
                });
                toast("已回滚并重启 worker");
                route(true);
              } catch (error) { toast(error.message, true); }
            },
          }, "确认回滚"),
        ]);
      },
    }, "回滚到此 Fold"));
  }
  panel.append(bar);
  return panel;
}

/* ---------------- Step 产物树 ---------------- */

/* Cross-fold lineage of validated step artifacts. Branches appear when the
   Agent used step_rollback, or a fold session restarted from a user-set
   parent override. Built for large trees: collapsible subtrees, text filter,
   one shared viewport-clamped tooltip (never clipped by the scroll box),
   inline download on every node with a snapshot. */
function stepTreePanel(detail) {
  const host = el("div", {});
  api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/steps`)
    .then((payload) => {
      if ((payload.nodes || []).length) host.append(stepTreeSection(detail, payload));
    })
    .catch(() => { /* no tree for this experiment */ });
  return host;
}

function stepTreeSection(detail, payload) {
  const nodes = payload.nodes;
  const ids = new Set(nodes.map((node) => node.node_id));
  const byParent = new Map();
  for (const node of nodes) {
    const key = node.parent_node_id && ids.has(node.parent_node_id) ? node.parent_node_id : "";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(node);
  }
  const state = { collapsed: new Set(), filter: "" };
  const rows = el("div", { class: "step-tree", onscroll: hideStepTip });
  const summary = el("span", { class: "hint", style: "margin-left:auto" });
  const validated = nodes.filter((node) => node.complete_validation).length;

  const haystack = (node) => [
    node.node_id, node.fold_id, node.fold_ref, node.result_name, node.epoch_id,
    ...(node.frozen_for || []),
  ].filter(Boolean).join(" ").toLowerCase();

  const render = () => {
    hideStepTip();
    rows.innerHTML = "";
    const query = state.filter.trim().toLowerCase();
    let visible = new Set(nodes.map((node) => node.node_id));
    if (query) {
      // Matches plus their ancestors, so hits keep their lineage context.
      const parentOf = new Map(nodes.map((node) => [node.node_id, node.parent_node_id]));
      visible = new Set();
      for (const node of nodes) {
        if (!haystack(node).includes(query)) continue;
        let cursor = node.node_id;
        while (cursor && ids.has(cursor) && !visible.has(cursor)) {
          visible.add(cursor);
          cursor = parentOf.get(cursor);
        }
      }
    }
    const walk = (parentKey, depth) => {
      for (const node of byParent.get(parentKey) || []) {
        if (!visible.has(node.node_id)) continue;
        const children = (byParent.get(node.node_id) || []).filter((child) => visible.has(child.node_id));
        // A filter overrides manual collapse: hits must never be hidden.
        const collapsed = !query && state.collapsed.has(node.node_id);
        rows.append(stepTreeRow(detail, payload, node, depth, {
          childCount: children.length,
          collapsed,
          toggle: () => {
            if (state.collapsed.has(node.node_id)) state.collapsed.delete(node.node_id);
            else state.collapsed.add(node.node_id);
            render();
          },
        }));
        if (!collapsed) walk(node.node_id, depth + 1);
      }
    };
    walk("", 0);
    if (!rows.children.length) rows.append(el("div", { class: "empty" }, "没有命中的节点"));
    const failed = nodes.length - validated;
    summary.textContent = query
      ? `命中 ${visible.size} / ${nodes.length} 节点`
      : `${validated} 已验证${failed ? ` · ${failed} 失败` : ""} · 共 ${nodes.length} 节点`;
  };

  const filterInput = el("input", {
    class: "input step-filter", type: "search", placeholder: "筛选 Fold / 节点 / 结果名…",
    oninput: (event) => { state.filter = event.target.value; render(); },
  });
  const toolbar = el("div", { class: "step-toolbar" },
    filterInput,
    el("button", { class: "btn small", onclick: () => { state.collapsed = new Set(); render(); } }, "全部展开"),
    el("button", {
      class: "btn small",
      onclick: () => {
        state.collapsed = new Set(nodes.filter((node) => (byParent.get(node.node_id) || []).length).map((node) => node.node_id));
        render();
      },
    }, "全部折叠"),
    summary,
  );
  render();
  return el("div", { class: "panel section-gap" },
    el("h4", {}, "Step 产物树"),
    el("div", { class: "hint" },
      "跨 Fold 的已验证策略谱系：每个节点保存该版本完整源代码与验证明细。悬停看指标详情，点行看完整信息，行内直接下载；HITL 实验可从节点回滚。"),
    toolbar,
    rows,
  );
}

function stepTreeRow(detail, payload, node, depth, { childCount, collapsed, toggle }) {
  const metrics = node.metrics || {};
  const failed = node.status === "failed";
  const zipUrl = `/api/experiments/${encodeURIComponent(detail.experiment_id)}/steps/${encodeURIComponent(node.node_id)}/source.zip`;
  const badges = [];
  if (node.is_current) badges.push(el("span", { class: "badge state-running_session" }, "当前位置"));
  for (const key of node.frozen_for || []) badges.push(el("span", { class: "badge state-completed" }, `冻结 ${key}`));
  if (failed) badges.push(el("span", { class: "badge state-failed" }, "失败"));
  if (node.has_snapshot && !node.restorable) badges.push(el("span", { class: "badge kind" }, "旧格式"));
  const actions = el("span", { class: "step-actions" });
  if (node.has_snapshot) {
    actions.append(el("a", {
      class: "btn small", href: zipUrl, title: "下载该版本完整源代码与验证明细",
      onclick: (event) => event.stopPropagation(),
    }, "下载"));
  }
  if (node.restorable && detail.kind === "hitl") {
    actions.append(el("button", {
      class: "btn small",
      onclick: (event) => { event.stopPropagation(); hideStepTip(); openStepParentOverrideModal(detail, payload, node); },
    }, "回滚…"));
  }
  const row = el("div", {
    class: `step-node${failed ? " failed" : ""}`,
    style: `padding-left:${8 + depth * 20}px`,
    onclick: () => { hideStepTip(); openStepNodeModal(detail, payload, node); },
    onmouseenter: (event) => showStepTip(node, event.currentTarget),
    onmouseleave: hideStepTip,
  },
    childCount
      ? el("button", {
          class: "step-toggle", title: collapsed ? `展开 ${childCount} 个子节点` : "折叠子树",
          onclick: (event) => { event.stopPropagation(); hideStepTip(); toggle(); },
        }, collapsed ? "▸" : "▾")
      : el("span", { class: "step-toggle leaf" }, "·"),
    el("span", { class: "step-label" }, `${node.fold_id || node.fold_ref || "?"} · ${node.result_name || node.node_id}`),
    collapsed ? el("span", { class: "step-chip" }, `+${childCount}`) : null,
    Number.isFinite(metrics.total_return)
      ? el("span", { class: `step-chip ${numClass(metrics.total_return)}` }, fmtPct(metrics.total_return)) : null,
    Number.isFinite(metrics.sharpe)
      ? el("span", { class: "step-chip" }, `S ${Number(metrics.sharpe).toFixed(2)}`) : null,
    ...badges,
    el("span", { class: "step-time" }, fmtTs(node.created_at)),
    actions,
  );
  return row;
}

/* One shared fixed-position tooltip: immune to the tree's overflow clipping
   and cheaper than a hidden card per row on large trees. pointer-events:none
   so it never steals hover from the rows underneath. */
function showStepTip(node, row) {
  let tip = document.getElementById("step-tip");
  if (!tip) {
    tip = el("div", { id: "step-tip" });
    document.body.append(tip);
  }
  const m = node.metrics || {};
  const line = (k, v) => el("div", { class: "step-tip-line" }, el("span", { class: "k" }, `${k}：`), String(v));
  tip.innerHTML = "";
  tip.append(
    el("div", { class: "step-tip-title" }, node.node_id),
    line("Fold", `${node.epoch_id || "—"} / ${node.fold_id || node.fold_ref || "—"}`),
    line("验证收益", fmtPct(m.total_return)),
    line("多 / 空拆解", `${fmtPct(m.long_return)} / ${fmtPct(m.short_return)}`),
    line("Sharpe", m.sharpe === undefined || m.sharpe === null ? "—" : Number(m.sharpe).toFixed(2)),
    line("最大回撤", fmtPct(m.max_drawdown)),
    line("记录于", fmtTs(node.created_at)),
    (node.frozen_for || []).length ? line("冻结用于", node.frozen_for.join("、")) : null,
    node.status === "failed" ? line("失败原因", node.error || "—") : null,
    node.has_snapshot ? null : line("快照", "无（失败尝试不留产物）"),
  );
  tip.style.display = "block";
  const rect = row.getBoundingClientRect();
  const margin = 8;
  tip.style.left = `${Math.max(margin, Math.min(rect.left + 24, window.innerWidth - tip.offsetWidth - margin))}px`;
  let top = rect.bottom + 4;
  if (top + tip.offsetHeight > window.innerHeight - margin) top = rect.top - tip.offsetHeight - 4;
  tip.style.top = `${Math.max(margin, top)}px`;
}

function hideStepTip() {
  const tip = document.getElementById("step-tip");
  if (tip) tip.style.display = "none";
}

function openStepNodeModal(detail, payload, node) {
  const m = node.metrics || {};
  const zipUrl = `/api/experiments/${encodeURIComponent(detail.experiment_id)}/steps/${encodeURIComponent(node.node_id)}/source.zip`;
  const body = el("div", {},
    el("table", { class: "kv" },
      kvRow("节点", node.node_id),
      kvRow("Fold", `${node.epoch_id || "—"} / ${node.fold_id || node.fold_ref || "—"}`),
      kvRow("验证收益", el("span", { class: numClass(m.total_return) }, fmtPct(m.total_return))),
      kvRow("多 / 空拆解", `${fmtPct(m.long_return)} / ${fmtPct(m.short_return)}`),
      kvRow("Sharpe", m.sharpe === undefined || m.sharpe === null ? "—" : Number(m.sharpe).toFixed(2)),
      kvRow("最大回撤", fmtPct(m.max_drawdown)),
      kvRow("记录时间", fmtTs(node.created_at)),
      (node.frozen_for || []).length ? kvRow("冻结用于", node.frozen_for.join("、")) : null,
      node.status === "failed" ? kvRow("失败原因", node.error || "—") : null,
      kvRow("附件", (node.attachments || []).join("、") || "—"),
      kvRow("产物 hash", el("code", {}, String(node.artifact_hash || "—"))),
      kvRow("模型 hash", el("code", {}, String(node.model_artifact_hash || "—"))),
    ),
    node.has_snapshot
      ? el("p", { class: "hint" },
          node.restorable
            ? "「下载源码 + 结果」包含该版本完整 output/ 源代码、models/ 参数与该次验证的明细结果文件。"
            : "旧格式节点：可下载完整源代码与验证明细（打包为节点目录原样），不支持设为回滚起点。")
      : el("p", { class: "hint" }, "失败尝试不保存产物快照，仅记录失败原因供避坑。"),
  );
  const buttons = [el("button", { class: "btn", onclick: closeModal }, "关闭")];
  if (node.has_snapshot) buttons.push(el("a", { class: "btn", href: zipUrl }, "下载源码 + 结果"));
  if (node.restorable && detail.kind === "hitl") {
    buttons.push(el("button", {
      class: "btn primary",
      onclick: () => openStepParentOverrideModal(detail, payload, node),
    }, "从此节点回滚…"));
  }
  showModal("Step 节点详情", body, buttons);
}

/* User-side step rollback: make this node the parent of a fold session
   (pending fold: takes effect at its next start; completed fold: combine with
   rerun, which stays restricted to the latest completed fold). */
function openStepParentOverrideModal(detail, payload, node) {
  const sessions = payload.fold_sessions || [];
  if (!sessions.length) { toast("该实验没有 Fold 会话", true); return; }
  const select = el("select", { class: "input" },
    ...sessions.map((session) => el("option", { value: session.key }, session.key)));
  const own = sessions.find((session) => session.epoch_id === node.epoch_id && session.fold_id === node.fold_id);
  if (own) select.value = own.key;
  const send = (action, sessionKey, directive) =>
    api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, {
      method: "POST",
      body: JSON.stringify({ action, session_key: sessionKey, directive }),
    });
  const body = el("div", {},
    el("p", {}, `把 ${node.node_id} 设为所选 Fold 会话的父产物起点（替代默认的冻结继承链）。`),
    el("p", { class: "hint" },
      "只能选不晚于该节点所属会话的目标（更晚节点携带未来验证信息会被拒绝）。尚未运行的 Fold：下次启动该会话时生效（人工控制模式下批准后）。已完成的 Fold：用「设置并重跑」"
      + "（仅允许重跑最新完成的 Fold，且需先停止 worker）。设置持续有效：重新设置即覆盖，「清除」即恢复默认继承链。"),
    el("label", { class: "hint" }, "目标 Fold 会话"),
    select,
  );
  showModal("从此节点回滚 / 设为起点", body, [
    el("button", { class: "btn", onclick: closeModal }, "取消"),
    el("button", {
      class: "btn",
      onclick: async () => {
        try {
          await send("set_parent_override", select.value, "");
          toast(`已清除 ${select.value} 的起点覆盖`);
          closeModal();
        } catch (error) { toast(error.message, true); }
      },
    }, "清除该会话覆盖"),
    el("button", {
      class: "btn primary",
      onclick: async () => {
        try {
          await send("set_parent_override", select.value, node.node_id);
          toast(`已把 ${select.value} 的起点设为该节点`);
          closeModal();
        } catch (error) { toast(error.message, true); }
      },
    }, "仅设置起点"),
    el("button", {
      class: "btn danger",
      onclick: async () => {
        try {
          await send("set_parent_override", select.value, node.node_id);
          await send("rerun_fold", select.value, null);
          toast("已设置起点并启动重跑（等待批准）");
          closeModal();
          route(true);
        } catch (error) { toast(error.message, true); }
      },
    }, "设置并重跑"),
  ]);
}

function foldResultPanel(detail, session) {
  const record = session.record || {};
  const validation = record.validation_result || {};
  const statusLabels = { frozen: "已冻结新产物", no_update: "沿用父产物（有验证未获接受）", no_valid_backtest: "沿用父产物（无完整验证）" };
  const panel = el("div", { class: "panel" },
    el("div", { class: "control-bar" },
      el("h4", { style: "margin:0" }, `Fold 结果 — ${session.fold_id || record.fold_id}`),
      el("span", {
        class: `badge state-${record.fold_status !== "frozen" ? "stopped"
          : (record.accept_warnings || []).length ? "waiting_user" : "completed"}`,
      },
        record.fold_status === "frozen" && (record.accept_warnings || []).length
          ? "已冻结（有验收警告）"
          : statusLabels[record.fold_status] || record.fold_status || "—"),
      record.finish_reason ? el("span", { class: "mode-note" }, `结束原因 ${record.finish_reason}`) : null,
    ),
  );
  // Headline validation metrics as tiles, metadata as a compact kv block.
  panel.append(el("div", { class: "section-gap" }, statTilesRow([
    { label: "验证收益", value: fmtPct(validation.total_return), cls: signCls(validation.total_return) },
    { label: "验证 Sharpe", value: validation.sharpe === undefined || validation.sharpe === null ? "—" : Number(validation.sharpe).toFixed(2), cls: signCls(validation.sharpe) },
    { label: "验证回撤", value: fmtPct(validation.max_drawdown) },
    { label: "多 / 空拆解", value: `${fmtPct(validation.long_return)} / ${fmtPct(validation.short_return)}` },
  ])));
  const meta = el("table", { class: "kv section-gap" },
    kvRow("验证区间", fmtPeriodRange(record.validation_period || session.validation_period)),
    record.run_wall_seconds ? kvRow("总耗时", fmtDuration(record.run_wall_seconds)) : null,
    kvRow("冻结产物", record.frozen_strategy_artifact_id || "—"),
    (record.accept_reasons || []).length ? kvRow("未接受原因", (record.accept_reasons || []).join("；")) : null,
    (record.accept_warnings || []).length
      ? kvRow("验收警告", el("span", { class: "num neg" }, record.accept_warnings.join("；")))
      : null,
  );
  panel.append(meta);
  if (record.run_id) {
    panel.append(el("div", { class: "section-gap" },
      el("h4", { class: "subsection-title" }, "验证期日度累计收益 vs 沪深300（含回撤）"),
      foldEquityHost(detail.experiment_id, session.epoch_id, session.fold_id || record.fold_id, record.run_id, "valid", { width: 860, height: 210, ddH: 76 }),
    ));
    panel.append(styleCard(detail.experiment_id, record.run_id, "valid"));
  }
  if (record.fold_directive) {
    panel.append(el("details", { class: "section-gap" },
      el("summary", { class: "mode-note", style: "cursor:pointer" }, "研究者本 Fold 指令"),
      el("div", { class: "markdown", style: "white-space:pre-wrap" }, record.fold_directive),
    ));
  }
  if ((record.steps || []).length) {
    const table = el("table", { class: "data section-gap" },
      el("tr", {}, el("th", {}, "Step"), el("th", {}, "状态"), el("th", {}, "收益"),
        el("th", {}, "超额(vs 300)"), el("th", {}, "β"), el("th", {}, "Sharpe"), el("th", {}, "回撤")),
      ...record.steps.map((step) => {
        const summary = step.summary || {};
        const bench = summary.benchmark || {};
        return el("tr", {},
          el("td", {}, step.step_id || "—"),
          el("td", {}, step.status || "—"),
          el("td", { class: numClass(summary.total_return) }, fmtPct(summary.total_return)),
          el("td", { class: numClass(bench.excess_return) }, fmtPct(bench.excess_return)),
          el("td", {}, bench.beta !== undefined && bench.beta !== null ? Number(bench.beta).toFixed(2) : "—"),
          el("td", {}, summary.sharpe !== undefined && summary.sharpe !== null ? Number(summary.sharpe).toFixed(2) : "—"),
          el("td", {}, fmtPct(summary.max_drawdown)),
        );
      }),
    );
    panel.append(el("h4", { class: "section-gap" }, "Step 历史"), table);
  }
  // Guarded test audit block (collapsed, clearly labelled).
  panel.append(loadFoldExtras(detail.experiment_id, session.epoch_id, session.fold_id || record.fold_id));
  return panel;
}

function kvRow(key, value) {
  return el("tr", {}, el("td", {}, key), el("td", {}, value));
}

/* Barra-lite style validation card: CSI300 alpha/beta regression + holdings
   style tilts (signed percentile deviation, [-1,1]) + SW industry weights. */
function styleCard(expId, runId, prefix) {
  const host = el("div", { class: "section-gap" },
    el("h4", { class: "subsection-title" }, "风格暴露与基准归因（Barra-lite）"),
    el("div", { class: "hint" }, "加载中…"));
  api(`/api/experiments/${encodeURIComponent(expId)}/style?run_id=${encodeURIComponent(runId)}&prefix=${encodeURIComponent(prefix)}`)
    .then((payload) => {
      host.querySelector(".hint").remove();
      const reg = payload.benchmark_regression || {};
      const style = payload.style || {};
      const shortWindow = (reg.n_days || 0) < 15;
      host.append(statTilesRow([
        { label: "β（vs 沪深300）", value: reg.beta === null || reg.beta === undefined ? "—" : Number(reg.beta).toFixed(2) },
        { label: "年化 α", value: fmtPct(reg.alpha_annualized), cls: signCls(reg.alpha_annualized) },
        { label: "R²", value: reg.r2 === null || reg.r2 === undefined ? "—" : Number(reg.r2).toFixed(2) },
        { label: "样本天数", value: String(reg.n_days ?? "—") },
      ]));
      if (shortWindow) host.append(el("div", { class: "hint" }, "窗口较短，回归结果仅供参考。"));
      const tilts = style.tilts;
      if (tilts) {
        const rows = [
          { label: "市值（+大盘 / −小盘）", value: tilts.size },
          { label: "PB（+高估值 / −低估值）", value: tilts.pb },
          { label: "换手（+高换手 / −低换手）", value: tilts.turnover },
        ];
        const list = el("div", { class: "tilts section-gap" });
        for (const row of rows) {
          const pct = Math.min(Math.abs(row.value), 1) * 50;
          const side = row.value >= 0 ? "left:50%" : `left:${50 - pct}%`;
          list.append(el("div", { class: "tilt-row" },
            el("span", { class: "tilt-label" }, row.label),
            el("span", { class: "tilt-bar" }, el("span", { class: "tilt-fill", style: `${side};width:${pct}%` })),
            el("span", { class: "tilt-value" }, (row.value >= 0 ? "+" : "") + Number(row.value).toFixed(2)),
          ));
        }
        host.append(list);
        host.append(el("div", { class: "hint" },
          `持仓覆盖 ${style.days} 个交易日 ｜ 日均 ${style.avg_names} 只 ｜ 日均多头 ${fmtAmount(style.avg_long_gross)} / 空头 ${fmtAmount(style.avg_short_gross)}`));
        if ((style.industries || []).length) {
          host.append(el("div", { class: "hint" },
            "行业净权重（申万一级）：" + style.industries.map((i) => `${i.name} ${(i.weight * 100).toFixed(0)}%`).join(" ｜ ")));
        }
      } else {
        host.append(el("div", { class: "hint" }, "该窗口没有可估值的持仓，无风格暴露可计算。"));
      }
    })
    .catch((error) => {
      const missing = /没有已落盘|404/.test(error.message);
      host.append(el("div", { class: "hint" },
        missing ? "该运行未落盘风格归因数据，无风格分析可展示。" : `风格分析加载失败：${error.message}`));
    });
  return host;
}

/* Per-fold daily equity (validation and guarded test parts share one fetch). */
const FOLD_EQUITY_CACHE = new Map(); // `${exp}/${epoch}/${fold}/${run}` -> promise
function foldEquityHost(expId, epochId, foldId, runId, part, opts) {
  const key = `${expId}/${epochId}/${foldId}/${runId || ""}`;
  if (!FOLD_EQUITY_CACHE.has(key)) {
    FOLD_EQUITY_CACHE.set(key, api(`/api/experiments/${encodeURIComponent(expId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/equity`));
  }
  const host = el("div", {}, el("div", { class: "hint" }, "收益曲线加载中…"));
  FOLD_EQUITY_CACHE.get(key).then((payload) => {
    host.innerHTML = "";
    host.append(equityChart(payload[part] || {}, opts));
  }).catch((error) => {
    FOLD_EQUITY_CACHE.delete(key);
    host.innerHTML = "";
    host.append(el("div", { class: "hint" }, `收益曲线加载失败：${error.message}`));
  });
  return host;
}

function loadFoldExtras(experimentId, epochId, foldId) {
  const wrap = el("div", { class: "section-gap" }, el("div", { class: "loading" }, "加载策略与分析…"));
  (async () => {
    let fold;
    try {
      fold = await api(`/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}`);
    } catch (error) {
      wrap.innerHTML = "";
      wrap.append(el("div", { class: "hint" }, `无法加载 Fold 附加信息：${error.message}`));
      return;
    }
    wrap.innerHTML = "";
    // Frozen strategy: one ZIP package (output + models), no per-file listing.
    wrap.append(el("div", { class: "control-bar" },
      el("h4", { class: "subsection-title" }, "冻结策略产物"),
      el("span", { class: "mode-note" }, "打包 output 与 models 全部文件"),
      el("span", { class: "spacer" }),
      el("a", {
        class: "btn small",
        href: `/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/strategy.zip`,
      }, "⬇ 下载 ZIP 包"),
    ));
    // Validation-backtest order stream: stats, charts, table, CSV export.
    wrap.append(ordersNode(experimentId, epochId, foldId));
    // Test audit, collapsed with warning.
    const audit = fold.test_audit || {};
    if (audit.test_result) {
      const test = audit.test_result;
      wrap.append(el("details", { class: "test-audit section-gap" },
        el("summary", {}, "测试期结果（事后审计 — 谨慎查看）"),
        el("div", { class: "hint warn" },
          "以下是本 Fold 冻结后在测试区间的样本外结果，仅供事后审计。请勿把测试期表现写入后续 Fold 指令，否则该实验的样本外结论将失效。"),
        el("table", { class: "kv" },
          kvRow("测试收益", el("span", { class: numClass(test.total_return) }, fmtPct(test.total_return))),
          kvRow("测试 Sharpe", test.sharpe !== undefined && test.sharpe !== null ? Number(test.sharpe).toFixed(2) : "—"),
          kvRow("测试回撤", fmtPct(test.max_drawdown)),
        ),
        el("div", { class: "section-gap" },
          el("h4", { class: "subsection-title" }, "测试期日度累计收益 vs 沪深300（含回撤）"),
          foldEquityHost(experimentId, epochId, foldId, (fold.record || {}).run_id, "test", { width: 760, height: 190, ddH: 64 }),
        ),
        (fold.record || {}).run_id ? styleCard(experimentId, fold.record.run_id, "test") : null,
        el("div", { class: "section-gap" }, el("a", {
          class: "btn small",
          href: `/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/orders.csv?result=test_000`,
        }, "⬇ 测试期交易明细 CSV")),
      ));
    }
  })();
  return wrap;
}

/* Most orders carry HH:MM decision times; transfers record a full ISO
   timestamp — render those as Shanghai HH:MM:SS instead of the raw string. */
function fmtOrderCell(key, value) {
  if (key === "decision_time" && typeof value === "string" && value.includes("T")) {
    const ms = Date.parse(value);
    if (!Number.isNaN(ms)) {
      return new Intl.DateTimeFormat("zh-CN", {
        hour12: false, timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit",
      }).format(ms);
    }
  }
  return value;
}

const ORDER_TABLE_COLUMNS = [
  ["trade_date", "日期"], ["decision_time", "决策时点"], ["ts_code", "代码"], ["action", "动作"],
  ["account", "账户"], ["requested_amount", "委托量"], ["filled_quantity", "成交量"],
  ["price", "价格"], ["status", "状态"], ["reject_reason", "拒单原因"],
];

/* Validation-backtest transaction details: stats tiles, per-day amount chart,
   order table, CSV export. Result switcher covers the fold's valid_* runs. */
function ordersNode(experimentId, epochId, foldId) {
  const base = `/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}`;
  const body = el("div", {});
  const wrap = el("div", { class: "section-gap" },
    el("h4", { class: "subsection-title", style: "margin-bottom:0.4rem" }, "交易明细（验证回测）"), body);
  let loading = false;
  async function load(result) {
    // No flash: keep the current content (dimmed) until the new data arrives.
    if (loading) return;
    loading = true;
    body.style.opacity = body.children.length ? "0.55" : "";
    if (!body.children.length) body.append(el("div", { class: "loading" }, "加载交易明细…"));
    let data;
    try {
      data = await api(`${base}/orders${result ? `?result=${encodeURIComponent(result)}` : ""}`);
    } catch (error) {
      body.innerHTML = "";
      body.style.opacity = "";
      loading = false;
      body.append(el("div", { class: "hint" }, `无交易明细：${error.message}`));
      return;
    }
    body.innerHTML = "";
    body.style.opacity = "";
    loading = false;
    const bar = el("div", { class: "control-bar" });
    if ((data.available || []).length > 1) {
      for (const name of data.available) {
        bar.append(el("span", {
          class: `file-chip${name === data.result ? " active" : ""}`,
          onclick: () => load(name),
        }, name));
      }
    } else {
      bar.append(el("span", { class: "mode-note" }, data.result));
    }
    bar.append(el("span", { class: "spacer" }), el("a", {
      class: "btn small",
      href: `${base}/orders.csv?result=${encodeURIComponent(data.result)}`,
    }, "⬇ 导出 CSV"));
    const stats = data.stats || {};
    const byAction = stats.by_action || {};
    body.append(bar, el("div", { class: "section-gap" }, statTilesRow([
      { label: "订单 / 成交 / 拒单", value: `${stats.orders} / ${stats.filled} / ${stats.rejected}` },
      { label: "成交额", value: fmtAmount(stats.turnover) },
      { label: "买 / 卖", value: `${byAction.buy || 0} / ${byAction.sell || 0}` },
      { label: "信用/做空动作", value: String((byAction.credit_buy || 0) + (byAction.credit_sell || 0) + (byAction.fin_buy || 0) + (byAction.short || 0) + (byAction.cover || 0) + (byAction.sell_repay || 0)) },
    ])));
    const daily = (stats.daily || []).map((d) => ({ label: String(d.trade_date).slice(4), value: d.amount }));
    if (daily.length) {
      body.append(el("h4", { class: "section-gap" }, "逐日成交金额"),
        singleSeriesBarChart(daily, { fmt: fmtAmount, height: 180 }));
    }
    if (Object.keys(stats.reject_reasons || {}).length) {
      body.append(el("div", { class: "stats-chips section-gap" },
        ...Object.entries(stats.reject_reasons).map(([reason, count]) =>
          el("span", { class: "stat-chip" }, `拒单 ${reason} ×${count}`))));
    }
    const rows = data.rows || [];
    if (rows.length) {
      const table = el("table", { class: "data section-gap" },
        el("tr", {}, ...ORDER_TABLE_COLUMNS.map(([, label]) => el("th", {}, label))),
        ...rows.slice(0, 80).map((row) => el("tr", {}, ...ORDER_TABLE_COLUMNS.map(([key]) => {
          let value = fmtOrderCell(key, row[key]);
          if (key === "price" && value !== null && value !== undefined) value = Number(value).toFixed(3);
          return el("td", {}, value === null || value === undefined ? "—" : String(value));
        }))),
      );
      const box = el("div", { class: "orders-table-box" }, table);
      body.append(box);
      if (data.row_count > Math.min(rows.length, 80)) {
        body.append(el("div", { class: "hint" }, `表格显示前 ${Math.min(rows.length, 80)} 条，共 ${data.row_count} 条 —— 完整明细请导出 CSV。`));
      }
    }
  }
  load(null);
  return wrap;
}

/* Standalone LLM strategy-review card (peer of the fold-result panel). */
function analysisPanel(experimentId, epochId, foldId) {
  const base = `/api/experiments/${encodeURIComponent(experimentId)}/analysis/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}`;
  return analysisNode(base, "策略分析（LLM，仅验证期证据）");
}

function analysisNode(base, title, { standalone = true } = {}) {
  const panel = el("div", { class: standalone ? "panel section-gap" : "section-gap" });
  const regenButton = el("button", { class: "btn small" }, "生成分析");
  const head = el("div", { class: "control-bar" },
    el("h4", { class: "subsection-title" }, title),
    el("span", { class: "spacer" }),
    regenButton,
  );
  const body = el("div", { class: "section-gap" }, el("div", { class: "loading" }, "加载分析…"));
  panel.append(head, body);
  regenButton.addEventListener("click", async () => {
    try {
      await api(base, { method: "POST" });
      toast("分析已开始生成，稍后自动刷新");
      regenButton.disabled = true;
      liveTimers.push(setTimeout(load, 20_000));
    } catch (error) { toast(error.message, true); }
  });
  async function load() {
    let payload;
    try { payload = await api(base); } catch (error) {
      body.innerHTML = "";
      body.append(el("div", { class: "hint" }, `分析加载失败：${error.message}`));
      return;
    }
    body.innerHTML = "";
    regenButton.disabled = Boolean(payload.pending);
    regenButton.textContent = payload.available ? "重新生成" : "生成分析";
    if (payload.pending) {
      body.append(el("div", { class: "prep-indicator" }, el("span", { class: "spinner" }), el("span", {}, "分析生成中…")));
      liveTimers.push(setTimeout(load, 8000));
    } else if (payload.content) {
      const meta = payload.meta || {};
      if (meta.model) {
        body.append(el("div", { class: "hint", style: "margin-top:0" },
          `模型 ${meta.model} ｜ 生成于 ${fmtTs(meta.created_at)}${meta.retried_after_length_stop ? " ｜ 曾因长度截断重试" : ""}`));
      }
      body.append(renderMarkdown(payload.content));
    } else {
      body.append(el("div", { class: "hint" }, "尚未生成分析——点击右上角「生成分析」。"));
    }
  }
  load();
  return panel;
}

function metaResultPanel(detail, session) {
  const record = session.record || {};
  const panel = el("div", { class: "panel section-gap" }, el("h4", {}, `元学习结果 — ${session.epoch_id}`));
  panel.append(el("table", { class: "kv" },
    kvRow("状态", record.status || "—"),
    kvRow("总耗时", foldDurationNode(detail, session)),
    record.meta_learning_directive ? kvRow("注入指令", record.meta_learning_directive) : null,
    record.sandbox_image_update ? kvRow("沙箱镜像", JSON.stringify(record.sandbox_image_update)) : null,
  ));
  if (record.taste) {
    panel.append(el("h4", { class: "section-gap" }, "Taste（注入本 Epoch 全部 Fold）"), renderMarkdown(record.taste));
  }
  return panel;
}

function heldoutPanel(session) {
  const records = session.records || [];
  const results = records.map((record) => record.test_result || {});
  const returns = results.map((r) => r.total_return).filter((v) => v !== null && v !== undefined);
  const sharpes = results.map((r) => r.sharpe).filter((v) => v !== null && v !== undefined);
  const drawdowns = results.map((r) => r.max_drawdown).filter((v) => v !== null && v !== undefined);
  const longs = results.map((r) => r.long_return).filter((v) => v !== null && v !== undefined);
  const shorts = results.map((r) => r.short_return).filter((v) => v !== null && v !== undefined);
  const cum = returns.length ? returns.reduce((acc, r) => acc * (1 + r), 1) - 1 : null;
  const wins = returns.filter((r) => r > 0).length;
  const panel = el("div", { class: "panel section-gap" },
    el("h4", { class: "subsection-title" }, "Held-out 冻结测试（最终样本外）"));
  panel.append(el("div", { class: "section-gap" }, statTilesRow([
    { label: "累计收益", value: fmtPct(cum), cls: signCls(cum) },
    { label: "平均 Sharpe", value: sharpes.length ? (sharpes.reduce((a, b) => a + b, 0) / sharpes.length).toFixed(2) : "—" },
    { label: "最差单期回撤", value: drawdowns.length ? fmtPct(Math.max(...drawdowns)) : "—" },
    { label: "正收益期数", value: returns.length ? `${wins} / ${returns.length}` : "—" },
    { label: "多 / 空贡献（累计）", value: longs.length
        ? `${fmtPct(longs.reduce((a, b) => a + b, 0))} / ${fmtPct(shorts.reduce((a, b) => a + b, 0))}` : "—" },
  ])));
  if (returns.length && detailView) {
    panel.append(el("div", { class: "section-gap" },
      el("h4", {}, "日度累计收益 vs 沪深300（含回撤）"),
      equityHost(detailView.experimentId, equityFingerprint(detailView.detail), { width: 860, height: 220, ddH: 80, keys: ["heldout"] }),
    ));
    const lastRun = records[records.length - 1] && records[records.length - 1].run_id;
    if (lastRun) panel.append(styleCard(detailView.experimentId, String(lastRun), "heldout"));
  }
  panel.append(el("table", { class: "data section-gap" },
    el("tr", {}, el("th", {}, "区间"), el("th", {}, "起止"), el("th", {}, "收益"),
      el("th", {}, "多头"), el("th", {}, "空头"), el("th", {}, "Sharpe"), el("th", {}, "回撤"), el("th", {}, "订单")),
    ...records.map((record) => {
      const result = record.test_result || {};
      const period = record.period || {};
      return el("tr", {},
        el("td", {}, String(record.fold_id || "").replace("heldout_", "")),
        el("td", {}, period.start && period.end ? `${fmtDate(period.start)} ～ ${fmtDate(period.end)}` : "—"),
        el("td", { class: numClass(result.total_return) }, fmtPct(result.total_return)),
        el("td", { class: numClass(result.long_return) }, fmtPct(result.long_return)),
        el("td", { class: numClass(result.short_return) }, fmtPct(result.short_return)),
        el("td", {}, result.sharpe !== undefined && result.sharpe !== null ? Number(result.sharpe).toFixed(2) : "—"),
        el("td", {}, fmtPct(result.max_drawdown)),
        el("td", {}, result.order_count ?? "—"),
      );
    }),
  ));
  return panel;
}
