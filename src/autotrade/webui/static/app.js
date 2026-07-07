/* MacroQuant HITL console SPA — hash routing, no build step, no dependencies. */

const $main = document.getElementById("main");
const $topbarRight = document.getElementById("topbar-right");
const $modalRoot = document.getElementById("modal-root");
const $toastRoot = document.getElementById("toast-root");

const STATE_LABELS = {
  starting: "启动中", running_session: "运行中", waiting_user: "等待批准", paused: "已暂停",
  completed: "已完成", stopped: "已停止", failed: "失败", interrupted: "已中断",
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

function fmtDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
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
      validColor: "#3987e5", testColor: "#199e70",
      // Lighter steps of the same hues: the short-side stack segment.
      validLight: "#7fb2ef", testLight: "#5ec49a",
      grid: "#2b303c", baseline: "#4a5163", muted: "#98a0af", faint: "#6f7787", ring: "#1b1f28",
    };
  }
  return {
    validColor: "#2a78d6", testColor: "#1baf7a",
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

function niceCeil(value) {
  const mag = Math.pow(10, Math.floor(Math.log10(value)));
  for (const mult of [1, 2, 2.5, 5, 10]) {
    if (mult * mag >= value) return mult * mag;
  }
  return 10 * mag;
}

/* Stack segment between two y levels; the far (data-end) edge may be rounded. */
function segmentPath(x, yNear, yFar, w, roundFar) {
  const top = Math.min(yNear, yFar), bottom = Math.max(yNear, yFar);
  const h = Math.max(bottom - top, 1);
  if (!roundFar) return `M${x},${top} H${x + w} V${bottom} H${x} Z`;
  const r = Math.min(4, w / 2, h);
  if (yFar < yNear) { // grows upward: round the top corners
    return `M${x},${bottom} L${x},${top + r} Q${x},${top} ${x + r},${top} L${x + w - r},${top} Q${x + w},${top} ${x + w},${top + r} L${x + w},${bottom} Z`;
  }
  return `M${x},${top} L${x + w},${top} L${x + w},${bottom - r} Q${x + w},${bottom} ${x + w - r},${bottom} L${x + r},${bottom} Q${x},${bottom} ${x},${bottom - r} Z`;
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

/* Grouped bars: per-fold two-series returns (default valid vs test). */
function foldReturnsChart(rows, { width = 640, height = 220, mini = false, series = null } = {}) {
  const INK = themeInk();
  const SPEC = seriesSpec();
  const SERIES = {
    valid: series ? { ...series[0], color: series[0].color || SPEC.valid.color } : SPEC.valid,
    test: series ? { ...series[1], color: series[1].color || SPEC.test.color } : SPEC.test,
  };
  const validKey = series ? series[0].key : "valid_return";
  const testKey = series ? series[1].key : "test_return";
  // Long/short ride INSIDE the default bars as stacked shades of each series'
  // hue (lighter step = short side); minis and custom series stay solid totals.
  const splitKeys = { [validKey]: ["valid_long", "valid_short"], [testKey]: ["test_long", "test_short"] };
  const lightFor = { [validKey]: INK.validLight, [testKey]: INK.testLight };
  const hasSplit = !series && !mini
    && rows.some((row) => row.valid_long !== null && row.valid_long !== undefined);
  const barExtents = (row, key) => {
    if (hasSplit && row[splitKeys[key][0]] !== null && row[splitKeys[key][0]] !== undefined) {
      const parts = [row[splitKeys[key][0]] || 0, row[splitKeys[key][1]] || 0];
      return [
        parts.filter((v) => v > 0).reduce((a, b) => a + b, 0),
        parts.filter((v) => v < 0).reduce((a, b) => a + b, 0),
      ];
    }
    return [row[key], row[key]];
  };
  const values = rows
    .flatMap((row) => [...barExtents(row, validKey), ...barExtents(row, testKey)])
    .filter((v) => v !== null && v !== undefined);
  if (!rows.length || !values.length) return el("div", { class: "hint" }, "暂无收益数据");
  const maxAbs = niceCeil(Math.max(0.005, ...values.map(Math.abs)));
  const padL = mini ? 42 : 50, padR = 10, padB = mini ? 26 : 36, padT = 8;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const zeroY = padT + plotH / 2;
  const yOf = (v) => zeroY - (v / maxAbs) * (plotH / 2);
  const groupW = plotW / rows.length;
  const barW = Math.max(3, Math.min(24, (groupW - 8) / 2 - 1));
  const svg = [];
  for (const frac of [-1, -0.5, 0.5, 1]) {
    const y = yOf(frac * maxAbs);
    svg.push(`<line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="${INK.grid}" stroke-width="1"/>`);
    svg.push(`<text x="${padL - 6}" y="${y + 3.5}" text-anchor="end" font-size="${mini ? 10 : 11}" fill="${INK.muted}">${(frac * maxAbs * 100).toFixed(1)}%</text>`);
  }
  svg.push(`<line x1="${padL}" y1="${zeroY}" x2="${width - padR}" y2="${zeroY}" stroke="${INK.baseline}" stroke-width="1"/>`);
  const labelEvery = Math.max(1, Math.ceil(rows.length / (mini ? 5 : 12)));
  rows.forEach((row, index) => {
    const cx = padL + groupW * index + groupW / 2;
    const bars = [
      { v: row[validKey], key: validKey, series: SERIES.valid, x: cx - barW - 1 }, // 2px surface gap between the pair
      { v: row[testKey], key: testKey, series: SERIES.test, x: cx + 1 },
    ];
    for (const bar of bars) {
      if (bar.v === null || bar.v === undefined) continue;
      // Rich hover: headline value plus the row's full breakdown.
      const lines = [`${String(row.fold_id || "")} ｜ ${bar.series.label} ${(bar.v * 100).toFixed(2)}%`];
      if (!series) {
        lines.push(`验证 ${fmtPct(row.valid_return)} ｜ 测试 ${fmtPct(row.test_return)}`);
        if (row.valid_long !== null && row.valid_long !== undefined) {
          lines.push(`多头 验证 ${fmtPct(row.valid_long)} / 测试 ${fmtPct(row.test_long)}`);
          lines.push(`空头 验证 ${fmtPct(row.valid_short)} / 测试 ${fmtPct(row.test_short)}`);
        }
        if (row.fold_status) lines.push(`状态 ${row.fold_status}`);
      }
      const tip = escapeHtml(lines.join("\n"));
      const [longVal, shortVal] = hasSplit
        ? [row[splitKeys[bar.key][0]], row[splitKeys[bar.key][1]]]
        : [null, null];
      if (longVal === null || longVal === undefined) {
        svg.push(`<path d="${barPath(bar.x, zeroY, yOf(bar.v), barW)}" fill="${bar.series.color}" data-tip="${tip}"/>`);
        continue;
      }
      // Stacked long (solid) + short (lighter shade of the same hue), positives
      // up / negatives down, 2px surface gap between stacked segments; the
      // outermost segment in each direction carries the rounded data-end.
      const segments = [
        { v: longVal || 0, color: bar.series.color },
        { v: shortVal || 0, color: lightFor[bar.key] },
      ].filter((seg) => seg.v !== 0);
      let up = 0, down = 0;
      const drawn = [];
      for (const seg of segments) {
        if (seg.v > 0) { drawn.push({ ...seg, from: up, to: up + seg.v, dir: 1 }); up += seg.v; }
        else { drawn.push({ ...seg, from: down, to: down + seg.v, dir: -1 }); down += seg.v; }
      }
      for (const seg of drawn) {
        const isOuter = seg.dir > 0 ? seg.to === up : seg.to === down;
        const gap = seg.from === 0 ? 0 : 2; // surface gap toward the baseline side
        const yNear = yOf(seg.from) - seg.dir * gap;
        svg.push(`<path d="${segmentPath(bar.x, yNear, yOf(seg.to), barW, isOuter)}" fill="${seg.color}" data-tip="${tip}"/>`);
      }
      if (!drawn.length) {
        svg.push(`<path d="${barPath(bar.x, zeroY, yOf(bar.v), barW)}" fill="${bar.series.color}" data-tip="${tip}"/>`);
      }
    }
    if (index % labelEvery === 0) {
      const label = String(row.fold_id || "").replace(/^fold_/, "");
      svg.push(`<text x="${cx}" y="${height - (mini ? 8 : 18)}" text-anchor="middle" font-size="${mini ? 10 : 11}" fill="${INK.muted}">${escapeHtml(label)}</text>`);
      if (!mini && row.epoch_label) {
        svg.push(`<text x="${cx}" y="${height - 5}" text-anchor="middle" font-size="10" fill="${INK.faint}">${escapeHtml(String(row.epoch_label).replace("epoch_", "E"))}</text>`);
      }
    }
  });
  const legendItems = hasSplit
    ? [
        { color: SERIES.valid.color, label: "验证·多头" },
        { color: INK.validLight, label: "验证·空头" },
        { color: SERIES.test.color, label: "测试·多头" },
        { color: INK.testLight, label: "测试·空头" },
      ]
    : [SERIES.valid, SERIES.test];
  const wrap = el("div", { class: "svg-chart" }, chartLegend(legendItems));
  const svgHost = el("div", {});
  svgHost.innerHTML = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">${svg.join("")}</svg>`;
  wrap.append(svgHost);
  wrap.__rerender = () => foldReturnsChart(rows, { width, height, mini, series });
  return bindChartTips(wrap);
}

/* Cumulative equity lines: ∏(1+r)−1 across folds, valid vs test. */
function cumulativeReturnChart(rows, { width = 640, height = 220, labels = null } = {}) {
  const INK = themeInk();
  const SPEC = seriesSpec();
  const SERIES = {
    valid: { ...SPEC.valid, label: labels?.valid || SPEC.valid.label },
    test: { ...SPEC.test, label: labels?.test || SPEC.test.label },
  };
  const build = (key) => {
    let equity = 1;
    const points = [];
    rows.forEach((row, index) => {
      const value = row[key];
      if (value === null || value === undefined) return;
      equity *= 1 + value;
      points.push({ index, cum: equity - 1, fold: row.fold_id });
    });
    return points;
  };
  const seriesData = [
    { ...SERIES.valid, points: build("valid_return") },
    { ...SERIES.test, points: build("test_return") },
  ].filter((series) => series.points.length);
  const all = seriesData.flatMap((series) => series.points.map((p) => p.cum));
  if (rows.length < 2 || !all.length) return el("div", { class: "hint" }, "累计曲线需要至少两个已完成 Fold");
  const maxAbs = niceCeil(Math.max(0.005, ...all.map(Math.abs)));
  const padL = 50, padR = 14, padB = 30, padT = 8;
  const plotW = width - padL - padR, plotH = height - padT - padB;
  const zeroY = padT + plotH / 2;
  const yOf = (v) => zeroY - (v / maxAbs) * (plotH / 2);
  const xOf = (index) => padL + (rows.length === 1 ? plotW / 2 : (index / (rows.length - 1)) * plotW);
  const svg = [];
  for (const frac of [-1, -0.5, 0.5, 1]) {
    const y = yOf(frac * maxAbs);
    svg.push(`<line x1="${padL}" y1="${y}" x2="${width - padR}" y2="${y}" stroke="${INK.grid}" stroke-width="1"/>`);
    svg.push(`<text x="${padL - 6}" y="${y + 3.5}" text-anchor="end" font-size="11" fill="${INK.muted}">${(frac * maxAbs * 100).toFixed(1)}%</text>`);
  }
  svg.push(`<line x1="${padL}" y1="${zeroY}" x2="${width - padR}" y2="${zeroY}" stroke="${INK.baseline}" stroke-width="1"/>`);
  const labelEvery = Math.max(1, Math.ceil(rows.length / 12));
  rows.forEach((row, index) => {
    if (index % labelEvery !== 0) return;
    const label = String(row.fold_id || "").replace(/^fold_/, "");
    svg.push(`<text x="${xOf(index)}" y="${height - 10}" text-anchor="middle" font-size="11" fill="${INK.muted}">${escapeHtml(label)}</text>`);
  });
  for (const series of seriesData) {
    const path = series.points.map((p, i) => `${i ? "L" : "M"}${xOf(p.index).toFixed(1)},${yOf(p.cum).toFixed(1)}`).join(" ");
    svg.push(`<path d="${path}" fill="none" stroke="${series.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`);
    for (const p of series.points) {
      const tip = `${String(p.fold || "")} 累计${series.label} ${(p.cum * 100).toFixed(2)}%`;
      // ≥8px marker with a 2px surface ring so overlapping lines stay legible.
      svg.push(`<circle cx="${xOf(p.index).toFixed(1)}" cy="${yOf(p.cum).toFixed(1)}" r="4.5" fill="${series.color}" stroke="${INK.ring}" stroke-width="2" data-tip="${escapeHtml(tip)}"/>`);
    }
  }
  const wrap = el("div", { class: "svg-chart" }, chartLegend(seriesData));
  const svgHost = el("div", {});
  svgHost.innerHTML = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">${svg.join("")}</svg>`;
  wrap.append(svgHost);
  wrap.__rerender = () => cumulativeReturnChart(rows, { width, height, labels });
  return bindChartTips(wrap);
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

function route() {
  const hash = location.hash || "#/";
  const expMatch = hash.match(/^#\/exp\/([^/]+)(?:\/(.*))?$/);
  const expId = expMatch ? decodeURIComponent(expMatch[1]) : null;
  const key = expMatch && expMatch[2] ? sessionKeyFromUrl(expMatch[2]) : null;
  // Session switch within an already-rendered experiment swaps only the right
  // panel: no page rebuild, no scroll jump, live stream and timers untouched.
  if (expMatch && key && detailView && detailView.experimentId === expId
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
      `创建 ${item.created_at ? item.created_at.slice(0, 16).replace("T", " ") : "—"}`,
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
    card.append(foldReturnsChart(item.fold_returns.map((row) => ({ ...row, epoch_label: row.epoch_id })), { width: 400, height: 130, mini: true }));
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

function heroPanel(item) {
  const metrics = item.metrics || {};
  const rows = (item.fold_returns || []).map((row) => ({ ...row, epoch_label: row.epoch_id }));
  const heldoutRows = (item.heldout_returns || [])
    .filter((row) => row.return !== null && row.return !== undefined)
    .map((row) => ({ label: row.label, value: row.return }));
  const panel = el("div", { class: "panel hero", id: "hero-panel" });
  const charts = el("div", { class: "charts-row section-gap" },
    el("div", { class: "chart-cell" }, el("h4", {}, "逐 Fold 收益"), foldReturnsChart(rows)),
    el("div", { class: "chart-cell" }, el("h4", {}, "累计收益曲线"), cumulativeReturnChart(rows)),
  );
  if (heldoutRows.length) {
    charts.append(el("div", { class: "chart-cell" },
      el("h4", {}, "Held-out 各期收益（最终样本外）"),
      singleSeriesBarChart(heldoutRows, { fmt: fmtPct })));
  }
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
      const option = el("option", { value: choice }, choice);
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
    const groupNode = el("div", { class: "check-group" },
      ...boxes.map((box, index) => el("label", { class: "check-item" }, box, field.choices[index])));
    inputs.set(field.key, { field, getValue: () => boxes.filter((box) => box.checked).map((box) => box.value) });
    wrap.append(groupNode, el("div", { class: "help" }, field.help || ""));
    return wrap;
  } else if (field.type === "text") {
    input = el("textarea", { rows: "3" });
    input.value = field.default ?? "";
  } else {
    input = el("input", { type: field.type === "int" || field.type === "float" ? "number" : "text" });
    if (field.type === "float") input.setAttribute("step", "any");
    input.value = field.default ?? "";
    if (field.optional) input.placeholder = "留空使用默认";
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

function showModal(title, body, footerButtons) {
  closeModal();
  const mask = el("div", { class: "modal-mask", onclick: (event) => { if (event.target === mask) closeModal(); } });
  mask.append(el("div", { class: "modal" },
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
  try { livePanel.source.close(); } catch { /* already closed */ }
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
  container.append(head);
  if (detail.kind === "hitl") container.append(controlBar(detail));
  const chartRows = (detail.fold_returns || []).map((row) => ({ ...row, epoch_label: row.epoch_id }));
  if (chartRows.length) {
    const metrics = detail.metrics || {};
    const sharpe = metrics.mean_test_sharpe;
    const charts = el("div", { class: "charts-row section-gap" },
      el("div", { class: "chart-cell" }, el("h4", {}, "逐 Fold 收益（验证 vs 测试）"), foldReturnsChart(chartRows)),
      el("div", { class: "chart-cell" }, el("h4", {}, "累计收益曲线"), cumulativeReturnChart(chartRows)),
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
  const layout = el("div", { class: "detail section-gap" });
  const listHost = sessionListPanel(detail, selectedKey);
  const rightHost = sessionDetailPanel(detail, selectedKey);
  layout.append(listHost, rightHost);
  container.append(layout);
  detailView = { experimentId, detail, listHost, rightHost, selectedKey };
  $main.innerHTML = "";
  $main.append(container);
  pollTimer = setInterval(async () => {
    try {
      const fresh = await api(`/api/experiments/${encodeURIComponent(experimentId)}/status`);
      const freshState = fresh.state;
      const badge = document.querySelector(".page-head .badge");
      if (badge && !badge.className.includes(`state-${freshState}`)) route(); // full refresh on state change
      else if (fresh.raw_status && fresh.raw_status.session_key && fresh.raw_status.session_key !== (status.session_key || null)) route();
    } catch { /* transient */ }
  }, 4000);
}

function controlBar(detail) {
  const id = detail.experiment_id;
  const control = detail.control || { mode: "step", request: null };
  const state = detail.state;
  const alive = detail.worker_alive;
  const send = async (payload, note) => {
    try {
      await api(`/api/experiments/${encodeURIComponent(id)}/control`, { method: "POST", body: JSON.stringify(payload) });
      if (note) toast(note);
      route();
    } catch (error) { toast(error.message, true); }
  };
  const bar = el("div", { class: "panel control-bar section-gap" });
  bar.append(el("span", { class: "mode-note" }, "运行模式："));
  const modeSelect = el("select", {
    onchange: () => send({ action: "set_mode", mode: modeSelect.value }, `模式已切换为 ${modeSelect.value}`),
  },
    el("option", { value: "step" }, "step（逐会话批准）"),
    el("option", { value: "auto" }, "auto（连续执行）"),
  );
  modeSelect.value = control.mode;
  bar.append(modeSelect);
  if (control.request === "pause") bar.append(el("span", { class: "badge state-paused" }, "已请求暂停"));
  if (control.request === "stop") bar.append(el("span", { class: "badge state-stopped" }, "已请求停止"));
  bar.append(el("span", { class: "spacer" }));
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
          el("button", { class: "btn danger", onclick: () => { closeModal(); send({ action: "terminate" }, "已发送终止信号"); } }, "强制终止"),
        ]);
      },
    }, "强制终止"));
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
    const item = el("div", {
      class: `session-item${session.key === selectedKey ? " selected" : ""}`,
      "data-key": session.key,
      onclick: () => { location.hash = `#/exp/${encodeURIComponent(detail.experiment_id)}/${sessionKeyToUrl(session.key)}`; },
    },
      el("span", { class: `dot ${dotClass}` }),
      el("span", { class: "label" },
        session.kind === "fold" ? String(session.fold_id || "").replace("fold_", "Fold ") : KIND_LABELS[session.kind] || session.kind),
      validReturn !== null && validReturn !== undefined
        ? el("span", { class: `ret ${numClass(validReturn)}` }, fmtPct(validReturn))
        : el("span", { class: "ret" }, isWaiting ? "待批准" : isCurrent ? "运行中" : ""),
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
  const running = isCurrent && detail.worker_alive && detail.state === "running_session";
  const waiting = isCurrent && detail.state === "waiting_user";
  const done = Boolean(session.record || (session.records || []).length);

  // Directive editor for sessions that have not run yet — and for a recorded
  // fold whose re-run is waiting for approval (prompt edits land here).
  if (detail.kind === "hitl" && (!done || waiting) && !running) {
    panel.append(directivePanel(detail, session, waiting));
  }
  if (running) panel.append(liveTracePanel(detail, session));
  if (session.kind === "fold" && done) {
    panel.append(foldResultPanel(detail, session));
    // The LLM strategy review gets its own card, peer to the fold result.
    panel.append(analysisPanel(detail.experiment_id, session.epoch_id, session.fold_id || (session.record || {}).fold_id));
    const recordedFolds = (detail.sessions || []).filter((s) => s.kind === "fold" && s.record);
    if (detail.kind === "hitl" && recordedFolds.length && recordedFolds[recordedFolds.length - 1].key === session.key) {
      panel.append(rerunPanel(detail, session));
    }
  }
  if (session.kind === "meta_learning" && done) panel.append(metaResultPanel(session));
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
  const send = async (payload, note) => {
    try {
      await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, { method: "POST", body: JSON.stringify(payload) });
      toast(note);
      refreshDetail();
    } catch (error) { toast(error.message, true); }
  };
  if (session.kind !== "heldout") {
    if (session.kind === "fold") {
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
  if ((detail.control || {}).mode === "step" && !approved) {
    buttons.append(el("button", {
      class: "btn primary",
      onclick: () => send({ action: "approve", session_key: session.key, directive: textarea.value }, "已批准，会话即将启动"),
    }, waiting ? "批准并启动" : "预先批准"));
  } else if (approved) {
    buttons.append(el("span", { class: "badge state-completed" }, "已批准"));
  }
  panel.append(buttons);
  if (waiting) panel.append(el("div", { class: "hint" }, "worker 正在等待此会话的批准。建议先预览完整系统提示词，确认注入内容无误后再批准。"));
  return panel;
}

/* Re-fetch the experiment payload and swap both detail panels in place —
   control-state changes update without a page rebuild or scroll jump. */
async function refreshDetail() {
  if (!detailView) { route(); return; }
  try {
    const detail = await api(`/api/experiments/${encodeURIComponent(detailView.experimentId)}`);
    detailView.detail = detail;
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
  const send = async (payload, note) => {
    try {
      await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, { method: "POST", body: JSON.stringify(payload) });
      toast(note);
      closeModal();
      refreshDetail();
    } catch (error) { toast(error.message, true); }
  };
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
    ), footer);
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
  if ((detail.control || {}).mode === "step" && !approved) {
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
    let text = `${label} ${counts[key]}`;
    if (key === "backtest" && stats.backtest_wall_seconds) text += `（Σ ${fmtDuration(stats.backtest_wall_seconds)}）`;
    row.append(el("span", { class: "stat-chip" }, text));
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

function liveTracePanel(detail, session) {
  // Reuse the streaming panel across page rebuilds and session navigation so
  // the SSE stream, scroll position, and accumulated events survive.
  if (livePanel && livePanel.expId === detail.experiment_id && livePanel.key === session.key
      && livePanel.source.readyState !== EventSource.CLOSED) {
    livePanel.refresh(detail);
    return livePanel.node;
  }
  destroyLivePanel();
  const panel = el("div", { class: "panel" }, el("h4", {}, `实时 Agent Trace — ${session.key}`));
  const status = detail.status || {};
  const tools = el("div", { class: "trace-tools" });
  const box = el("div", { class: "trace-box" });
  let autoScroll = true;
  const scrollToggle = el("label", {}, el("input", {
    type: "checkbox", checked: "checked",
    onchange: (event) => { autoScroll = event.target.checked; },
  }), " 自动滚动");
  const countdown = el("span", {
    class: "badge state-running_session", style: "display:none",
    title: "推理截止倒计时 = 名义 deadline + 已回补的回测墙钟；回测执行中独立计时",
  });
  tools.append(el("span", { class: "badge state-running_session" }, "streaming"), countdown, scrollToggle);
  const statsHost = el("div", {});
  const prepText = el("span", {}, "");
  const prep = el("div", { class: "prep-indicator" }, el("span", { class: "spinner" }), prepText);
  panel.append(tools, statsHost, prep, box);
  let sawEvent = false;
  const source = new EventSource(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stream`);
  source.onmessage = (event) => {
    try {
      sawEvent = true;
      prep.style.display = "none";
      box.append(traceEventNode(JSON.parse(event.data)));
      while (box.children.length > 400) box.firstChild.remove();
      if (autoScroll) box.scrollTop = box.scrollHeight;
    } catch { /* skip malformed */ }
  };
  source.addEventListener("eof", () => {
    box.append(el("div", { class: "hint" }, "—— trace 结束 ——"));
    source.close();
  });
  source.onerror = () => { /* EventSource auto-reconnects */ };
  let deadlineMs = status.fold_deadline_at ? Date.parse(status.fold_deadline_at) : null;
  let startedMs = status.session_started_at ? Date.parse(status.session_started_at) : Date.now();
  let creditSeconds = 0;
  let inBacktest = false;
  const tick = () => {
    if (!sawEvent) {
      const elapsed = (Date.now() - startedMs) / 1000;
      prepText.textContent = `沙箱与数据快照准备中（已 ${fmtDuration(elapsed)}）… 首个 Agent 事件到达后开始流式显示`;
    }
    if (deadlineMs) {
      countdown.style.display = "";
      if (inBacktest) {
        countdown.textContent = `回测执行中（独立计时，已回补 ${fmtDuration(creditSeconds)}）`;
      } else {
        const remain = (deadlineMs + creditSeconds * 1000 - Date.now()) / 1000;
        countdown.textContent = remain >= 0
          ? `推理剩余 ${fmtDuration(remain)}（含回补 ${fmtDuration(creditSeconds)}）`
          : `收尾中 +${fmtDuration(-remain)}`;
      }
    }
  };
  const pollStats = async () => {
    try {
      const stats = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stats`);
      creditSeconds = Number(stats.backtest_wall_seconds) || 0;
      inBacktest = Boolean(stats.in_backtest);
      statsHost.innerHTML = "";
      statsHost.append(statsChipsRow(stats));
    } catch { /* trace not started yet */ }
    try {
      const fresh = await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/status`);
      const raw = fresh.raw_status || {};
      if (raw.fold_deadline_at) deadlineMs = Date.parse(raw.fold_deadline_at);
      if (raw.session_started_at) startedMs = Date.parse(raw.session_started_at);
    } catch { /* transient */ }
  };
  tick();
  pollStats();
  const timers = [setInterval(tick, 1000), setInterval(pollStats, 5000)];
  livePanel = {
    expId: detail.experiment_id,
    key: session.key,
    node: panel,
    source,
    timers,
    refresh: (freshDetail) => {
      const rawStatus = freshDetail.status || {};
      if (rawStatus.fold_deadline_at) deadlineMs = Date.parse(rawStatus.fold_deadline_at);
    },
  };
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
  "reason", "error_type", "name",
];

function traceEventNode(event) {
  const type = event.event_type || "event";
  const node = el("div", { class: "trace-event" });
  const time = (event.ts || "").replace("T", " ").slice(5, 19);
  const head = el("div", { class: "head" },
    el("span", { class: `type ${type}` }, type),
    el("span", {}, time),
    event.step_id ? el("span", {}, `step ${event.step_id}`) : null,
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
                route();
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

function foldResultPanel(detail, session) {
  const record = session.record || {};
  const validation = record.validation_result || {};
  const statusLabels = { frozen: "已冻结新产物", no_update: "沿用父产物（有验证未获接受）", no_valid_backtest: "沿用父产物（无完整验证）" };
  const panel = el("div", { class: "panel" },
    el("div", { class: "control-bar" },
      el("h4", { style: "margin:0" }, `Fold 结果 — ${session.fold_id || record.fold_id}`),
      el("span", { class: `badge state-${record.fold_status === "frozen" ? "completed" : "stopped"}` },
        statusLabels[record.fold_status] || record.fold_status || "—"),
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
    kvRow("验证区间", record.validation_period || session.validation_period || "—"),
    kvRow("冻结产物", record.frozen_strategy_artifact_id || "—"),
    (record.accept_reasons || []).length ? kvRow("未接受原因", (record.accept_reasons || []).join("；")) : null,
  );
  panel.append(meta);
  if (record.fold_directive) {
    panel.append(el("details", { class: "section-gap" },
      el("summary", { class: "mode-note", style: "cursor:pointer" }, "研究者本 Fold 指令"),
      el("div", { class: "markdown", style: "white-space:pre-wrap" }, record.fold_directive),
    ));
  }
  if ((record.steps || []).length) {
    const table = el("table", { class: "data section-gap" },
      el("tr", {}, el("th", {}, "Step"), el("th", {}, "状态"), el("th", {}, "收益"), el("th", {}, "Sharpe"), el("th", {}, "回撤")),
      ...record.steps.map((step) => {
        const summary = step.summary || {};
        return el("tr", {},
          el("td", {}, step.step_id || "—"),
          el("td", {}, step.status || "—"),
          el("td", { class: numClass(summary.total_return) }, fmtPct(summary.total_return)),
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
    // Strategy files + download.
    const filesPanel = el("div", {},
      el("div", { class: "control-bar" },
        el("h4", { class: "subsection-title" }, "冻结策略代码"),
        el("span", { class: "spacer" }),
        el("a", {
          class: "btn small",
          href: `/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/strategy.zip`,
        }, "⬇ 下载 zip（output + models）"),
      ),
    );
    const chips = el("div", { class: "file-list section-gap" });
    const viewer = el("pre", { class: "code-view section-gap", style: "display:none" });
    for (const file of fold.strategy_files || []) {
      chips.append(el("span", {
        class: "file-chip",
        onclick: async (event) => {
          document.querySelectorAll(".file-chip.active").forEach((chip) => chip.classList.remove("active"));
          event.target.classList.add("active");
          viewer.style.display = "block";
          viewer.textContent = "加载中…";
          try {
            const response = await fetch(`/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/strategy-file?path=${encodeURIComponent(file.path)}`);
            viewer.textContent = response.ok ? await response.text() : `加载失败（${response.status}）`;
          } catch (error) { viewer.textContent = String(error); }
        },
      }, `${file.path} (${(file.bytes / 1024).toFixed(1)}K)`));
    }
    filesPanel.append(chips, viewer);
    wrap.append(filesPanel);
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
        el("div", { class: "section-gap" }, el("a", {
          class: "btn small",
          href: `/api/experiments/${encodeURIComponent(experimentId)}/folds/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}/orders.csv?result=test_000`,
        }, "⬇ 测试期交易明细 CSV")),
      ));
    }
  })();
  return wrap;
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
          let value = row[key];
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
  const panel = el("div", { class: "panel section-gap" });
  const regenButton = el("button", { class: "btn small" }, "生成分析");
  const head = el("div", { class: "control-bar" },
    el("h4", { class: "subsection-title" }, "策略分析（LLM，仅验证期证据）"),
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
      setTimeout(load, 20_000);
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
      setTimeout(load, 8000);
    } else if (payload.content) {
      const meta = payload.meta || {};
      if (meta.model) {
        body.append(el("div", { class: "hint", style: "margin-top:0" },
          `模型 ${meta.model} ｜ 生成于 ${(meta.created_at || "").slice(0, 16).replace("T", " ")}${meta.retried_after_length_stop ? " ｜ 曾因长度截断重试" : ""}`));
      }
      body.append(renderMarkdown(payload.content));
    } else {
      body.append(el("div", { class: "hint" }, "尚未生成分析——点击右上角「生成分析」。"));
    }
  }
  load();
  return panel;
}

function metaResultPanel(session) {
  const record = session.record || {};
  const panel = el("div", { class: "panel section-gap" }, el("h4", {}, `元学习结果 — ${session.epoch_id}`));
  panel.append(el("table", { class: "kv" },
    kvRow("状态", record.status || "—"),
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
  const barRows = records
    .map((record) => ({
      label: String(record.fold_id || "").replace("heldout_", ""),
      value: (record.test_result || {}).total_return,
    }))
    .filter((row) => row.value !== null && row.value !== undefined);
  const cumRows = records.map((record) => ({
    fold_id: String(record.fold_id || "").replace("heldout_", ""),
    test_return: (record.test_result || {}).total_return,
    valid_return: null,
  }));
  if (barRows.length) {
    const charts = el("div", { class: "charts-row section-gap" },
      el("div", { class: "chart-cell" }, el("h4", {}, "各期收益"),
        singleSeriesBarChart(barRows, { fmt: fmtPct, height: 190 })));
    if (barRows.length >= 2) {
      charts.append(el("div", { class: "chart-cell" }, el("h4", {}, "累计收益曲线"),
        cumulativeReturnChart(cumRows, { height: 190, labels: { test: "Held-out" } })));
    }
    panel.append(charts);
  }
  panel.append(el("table", { class: "data section-gap" },
    el("tr", {}, el("th", {}, "区间"), el("th", {}, "起止"), el("th", {}, "收益"),
      el("th", {}, "多头"), el("th", {}, "空头"), el("th", {}, "Sharpe"), el("th", {}, "回撤"), el("th", {}, "订单")),
    ...records.map((record) => {
      const result = record.test_result || {};
      const period = record.period || {};
      return el("tr", {},
        el("td", {}, String(record.fold_id || "").replace("heldout_", "")),
        el("td", {}, period.start && period.end ? `${period.start}–${period.end}` : "—"),
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
