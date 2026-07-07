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

/* Simple SVG bar chart: per-fold returns (valid vs test). */
function foldReturnsChart(rows, { width = 720, height = 200 } = {}) {
  const values = rows.flatMap((row) => [row.valid_return, row.test_return]).filter((v) => v !== null && v !== undefined);
  if (!rows.length || !values.length) return el("div", { class: "hint" }, "暂无收益数据");
  const maxAbs = Math.max(0.01, ...values.map(Math.abs));
  const padL = 46, padB = 34, padT = 12;
  const plotW = width - padL - 10, plotH = height - padT - padB;
  const zeroY = padT + plotH * (maxAbs / (2 * maxAbs));
  const groupW = plotW / rows.length;
  const barW = Math.min(22, groupW / 2.6);
  const svg = [];
  const yOf = (v) => zeroY - (v / maxAbs) * (plotH / 2);
  svg.push(`<line x1="${padL}" y1="${zeroY}" x2="${width - 8}" y2="${zeroY}" stroke="#c9cfda" stroke-width="1"/>`);
  for (const frac of [-1, -0.5, 0.5, 1]) {
    const y = yOf(frac * maxAbs);
    svg.push(`<line x1="${padL}" y1="${y}" x2="${width - 8}" y2="${y}" stroke="#eef1f5"/>`);
    svg.push(`<text x="${padL - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="#68717f">${(frac * maxAbs * 100).toFixed(1)}%</text>`);
  }
  rows.forEach((row, index) => {
    const cx = padL + groupW * index + groupW / 2;
    const bars = [
      { v: row.valid_return, color: "#7f9be0", dx: -barW - 2 },
      { v: row.test_return, color: "#2456c4", dx: 2 },
    ];
    for (const bar of bars) {
      if (bar.v === null || bar.v === undefined) continue;
      const y = yOf(Math.max(bar.v, 0)), h = Math.abs(yOf(bar.v) - zeroY);
      svg.push(`<rect x="${cx + bar.dx}" y="${bar.v >= 0 ? y : zeroY}" width="${barW}" height="${Math.max(h, 1)}" rx="2" fill="${bar.color}"><title>${escapeHtml(row.fold_id)} ${bar.color === "#2456c4" ? "test" : "valid"}: ${(bar.v * 100).toFixed(2)}%</title></rect>`);
    }
    const label = String(row.fold_id || "").replace(/^fold_/, "");
    svg.push(`<text x="${cx}" y="${height - 16}" text-anchor="middle" font-size="10" fill="#68717f">${escapeHtml(label)}</text>`);
    if (row.epoch_label) svg.push(`<text x="${cx}" y="${height - 4}" text-anchor="middle" font-size="9" fill="#a5abb8">${escapeHtml(row.epoch_label)}</text>`);
  });
  svg.push(`<rect x="${padL}" y="0" width="10" height="10" fill="#7f9be0"/><text x="${padL + 14}" y="9" font-size="10" fill="#68717f">验证</text>`);
  svg.push(`<rect x="${padL + 52}" y="0" width="10" height="10" fill="#2456c4"/><text x="${padL + 66}" y="9" font-size="10" fill="#68717f">测试</text>`);
  const wrap = el("div", { class: "svg-chart" });
  wrap.innerHTML = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">${svg.join("")}</svg>`;
  return wrap;
}

/* ---------------- router ---------------- */

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);

function route() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  document.querySelectorAll(".modal-mask").forEach((node) => node.remove());
  const hash = location.hash || "#/";
  const expMatch = hash.match(/^#\/exp\/([^/]+)(?:\/(.*))?$/);
  if (expMatch) renderDetailPage(decodeURIComponent(expMatch[1]), expMatch[2] ? decodeURIComponent(expMatch[2]) : null);
  else renderHomePage();
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
  container.append(el("div", { class: "page-head" },
    el("h2", {}, "实验列表"),
    el("span", { class: "sub" }, "点击实验查看 Epoch/Fold 结果、运行状态与 Agent Trace"),
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
}

function experimentCard(item) {
  const metrics = item.metrics || {};
  const total = item.total_sessions, done = item.completed_sessions || 0;
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const card = el("div", { class: "card" });
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
  card.append(el("div", { class: "metrics" },
    metricNode("累计验证", metrics.cum_valid_return),
    metricNode("累计测试", metrics.cum_test_return),
    metricNode("Held-out", metrics.cum_heldout_return),
  ));
  if ((item.fold_returns || []).length) {
    card.append(foldReturnsChart(item.fold_returns.map((row) => ({ ...row, epoch_label: row.epoch_id })), { width: 380, height: 120 }));
  }
  const actions = el("div", { class: "actions" },
    el("a", { class: "btn small", href: `#/exp/${encodeURIComponent(item.experiment_id)}` }, "打开"),
  );
  if (item.kind === "hitl" && ["interrupted", "stopped", "failed", "created"].includes(item.state)) {
    actions.append(el("button", {
      class: "btn small primary",
      onclick: async () => {
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
      onclick: () => confirmDeleteExperiment(item.experiment_id),
    }, "删除"));
  }
  card.append(actions);
  return card;
}

function metricNode(label, value) {
  return el("div", { class: "metric" },
    el("span", { class: `v ${value === null || value === undefined ? "" : value >= 0 ? "pos" : "neg"}` }, fmtPct(value)),
    el("span", { class: "k" }, label),
  );
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

async function openCreateModal() {
  let schema;
  try { schema = await api("/api/parameter-schema"); } catch (error) { toast(error.message, true); return; }
  const inputs = new Map();
  const body = el("div", {});
  body.append(el("p", { class: "hint" },
    "所有参数均有默认值；仅实验名与周期标签必填。周期标签格式随 Fold 周期而定：quarter → 2024Q1，month → 202401，week → 周一日期 20240108，year → 2024。"));
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
  const errorBox = el("div", {});
  body.append(errorBox);
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
  } else if (field.type === "multi") {
    input = el("select", { multiple: "multiple", size: String(Math.min(3, field.choices.length)) },
      ...field.choices.map((choice) => {
        const option = el("option", { value: choice }, choice);
        if ((field.default || []).includes(choice)) option.selected = true;
        return option;
      }));
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

function collectParams(inputs) {
  const params = {};
  for (const [key, { field, input }] of inputs.entries()) {
    let value;
    if (field.type === "bool") value = input.checked;
    else if (field.type === "multi") value = [...input.selectedOptions].map((option) => option.value);
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

let traceSource = null;

async function renderDetailPage(experimentId, selectedKey) {
  if (traceSource) { traceSource.close(); traceSource = null; }
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
      status.analysis_error ? ` ｜ 分析：${status.analysis_error}` : "",
    ),
  );
  const container = el("div", {});
  container.append(head);
  if (detail.kind === "hitl") container.append(controlBar(detail));
  const chartRows = (detail.fold_returns || []).map((row) => ({ ...row, epoch_label: row.epoch_id }));
  if (chartRows.length) {
    container.append(el("div", { class: "panel section-gap" },
      el("h4", {}, "逐 Fold 收益（验证 vs 测试）"),
      foldReturnsChart(chartRows, { width: 1180, height: 190 }),
    ));
  }
  const layout = el("div", { class: "detail section-gap" });
  layout.append(sessionListPanel(detail, selectedKey), sessionDetailPanel(detail, selectedKey));
  container.append(layout);
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
      onclick: () => { location.hash = `#/exp/${encodeURIComponent(detail.experiment_id)}/${encodeURIComponent(session.key)}`; },
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

  // Directive editor for sessions that have not run yet (HITL only).
  if (detail.kind === "hitl" && !done && !running) {
    panel.append(directivePanel(detail, session, waiting));
  }
  if (running) panel.append(liveTracePanel(detail, session));
  if (session.kind === "fold" && done) panel.append(foldResultPanel(detail, session));
  if (session.kind === "meta_learning" && done) panel.append(metaResultPanel(session));
  if (session.kind === "heldout" && done) panel.append(heldoutPanel(session));
  if (done && session.record && session.record.run_id) {
    panel.append(el("div", { class: "panel section-gap" },
      el("h4", {}, "Agent Trace（回放）"),
      traceReplayNode(detail.experiment_id, session.record.run_id),
    ));
  }
  if (!done && !running && !waiting) {
    panel.append(el("div", { class: "panel section-gap" }, el("div", { class: "empty" }, "该会话尚未开始。")));
  }
  return panel;
}

function directivePanel(detail, session, waiting) {
  const control = detail.control || { directives: {}, approved_sessions: [] };
  const existing = (control.directives || {})[session.key] || "";
  const approved = (control.approved_sessions || []).includes(session.key);
  const textarea = el("textarea", { class: "directive", placeholder: "可选：为该会话注入研究方向 / 优化假设……" });
  textarea.value = existing;
  const isMeta = session.kind === "meta_learning";
  const panel = el("div", { class: "panel" },
    el("h4", {}, isMeta ? "元学习指令（本 Epoch）" : session.kind === "heldout" ? "Held-out 启动" : "本 Fold 研究者指令"),
  );
  if (session.kind !== "heldout") {
    panel.append(textarea, el("div", { class: "hint warn" },
      "指令会注入系统提示词并记入账本。请勿写入测试期/Held-out 结果或具体日历日期——那会破坏 walk-forward 的样本外有效性。"));
  }
  const buttons = el("div", { class: "control-bar section-gap" });
  const send = async (payload, note) => {
    try {
      await api(`/api/experiments/${encodeURIComponent(detail.experiment_id)}/control`, { method: "POST", body: JSON.stringify(payload) });
      toast(note);
      route();
    } catch (error) { toast(error.message, true); }
  };
  if (session.kind !== "heldout") {
    buttons.append(el("button", {
      class: "btn",
      onclick: () => send({ action: "set_directive", session_key: session.key, directive: textarea.value }, "指令已保存"),
    }, "保存指令"));
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
  if (waiting) panel.append(el("div", { class: "hint" }, "worker 正在等待此会话的批准。"));
  return panel;
}

function liveTracePanel(detail, session) {
  const panel = el("div", { class: "panel" }, el("h4", {}, `实时 Agent Trace — ${session.key}`));
  const tools = el("div", { class: "trace-tools" });
  const box = el("div", { class: "trace-box" });
  let autoScroll = true;
  const scrollToggle = el("label", {}, el("input", {
    type: "checkbox", checked: "checked",
    onchange: (event) => { autoScroll = event.target.checked; },
  }), " 自动滚动");
  tools.append(el("span", { class: "badge state-running_session" }, "streaming"), scrollToggle);
  panel.append(tools, box);
  const url = `/api/experiments/${encodeURIComponent(detail.experiment_id)}/trace/stream`;
  traceSource = new EventSource(url);
  traceSource.onmessage = (event) => {
    try {
      box.append(traceEventNode(JSON.parse(event.data)));
      while (box.children.length > 400) box.firstChild.remove();
      if (autoScroll) box.scrollTop = box.scrollHeight;
    } catch { /* skip malformed */ }
  };
  traceSource.addEventListener("eof", () => {
    box.append(el("div", { class: "hint" }, "—— trace 结束 ——"));
    traceSource.close();
  });
  traceSource.onerror = () => { /* EventSource auto-reconnects */ };
  return panel;
}

function traceReplayNode(experimentId, runId) {
  const box = el("div", { class: "trace-box" });
  const button = el("button", {
    class: "btn small",
    onclick: async () => {
      button.disabled = true;
      let offset = 0;
      try {
        for (let page = 0; page < 40; page += 1) {
          const data = await api(`/api/experiments/${encodeURIComponent(experimentId)}/trace?run_id=${encodeURIComponent(runId)}&offset=${offset}`);
          for (const event of data.events) box.append(traceEventNode(event));
          offset = data.next_offset;
          if (data.eof) break;
        }
        box.append(el("div", { class: "hint" }, "—— trace 结束 ——"));
      } catch (error) {
        box.append(el("div", { class: "hint" }, `加载失败：${error.message}`));
      }
    },
  }, "加载完整 trace");
  return el("div", {}, button, box);
}

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
  let brief = "";
  if (type === "llm_call") {
    brief = String(event.content || "").slice(0, 600);
    const toolCalls = (event.tool_calls || []).map((call) => call.function && call.function.name).filter(Boolean);
    if (toolCalls.length) head.append(el("span", {}, `→ ${toolCalls.join(", ")}`));
  } else if (event.raw) {
    brief = String(event.raw).slice(0, 400);
  } else {
    const payload = { ...event };
    for (const key of ["event_type", "ts", "call_id", "parent_call_id", "experiment_id", "epoch_id", "fold_id", "run_id", "conversation_id", "step_id", "phase"]) delete payload[key];
    brief = JSON.stringify(payload, null, 1).slice(0, 400);
  }
  if (brief) node.append(el("pre", {}, brief));
  node.append(el("details", {}, el("summary", {}, "完整事件"), el("pre", {}, JSON.stringify(event, null, 2))));
  return node;
}

function foldResultPanel(detail, session) {
  const record = session.record || {};
  const validation = record.validation_result || {};
  const panel = el("div", { class: "panel" }, el("h4", {}, `Fold 结果 — ${session.fold_id || record.fold_id}`));
  panel.append(el("table", { class: "kv" },
    kvRow("状态", `${record.fold_status || "—"}${record.finish_reason ? `（${record.finish_reason}）` : ""}`),
    kvRow("验证区间", record.validation_period || session.validation_period || "—"),
    kvRow("验证收益", el("span", { class: numClass(validation.total_return) }, fmtPct(validation.total_return))),
    kvRow("验证 Sharpe", validation.sharpe !== undefined && validation.sharpe !== null ? Number(validation.sharpe).toFixed(2) : "—"),
    kvRow("验证回撤", fmtPct(validation.max_drawdown)),
    kvRow("冻结产物", record.frozen_strategy_artifact_id || "—"),
    record.fold_directive ? kvRow("研究者指令", record.fold_directive) : null,
    (record.accept_reasons || []).length ? kvRow("拒绝原因", (record.accept_reasons || []).join("；")) : null,
  ));
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
        el("h4", { style: "margin:0" }, "冻结策略代码"),
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
    // Analysis.
    wrap.append(analysisNode(experimentId, epochId, foldId, fold.analysis || {}));
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
      ));
    }
  })();
  return wrap;
}

function analysisNode(experimentId, epochId, foldId, analysisInfo) {
  const wrap = el("div", { class: "section-gap" });
  const head = el("div", { class: "control-bar" },
    el("h4", { style: "margin:0" }, "策略分析（LLM，仅验证期证据）"),
    el("span", { class: "spacer" }),
  );
  const body = el("div", {});
  const regenButton = el("button", {
    class: "btn small",
    onclick: async () => {
      try {
        await api(`/api/experiments/${encodeURIComponent(experimentId)}/analysis/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}`, { method: "POST" });
        toast("分析已开始生成，稍后刷新查看");
        regenButton.disabled = true;
      } catch (error) { toast(error.message, true); }
    },
  }, analysisInfo.available ? "重新生成" : "生成分析");
  head.append(regenButton);
  wrap.append(head, body);
  if (analysisInfo.pending) body.append(el("div", { class: "hint" }, "分析生成中…"));
  else if (analysisInfo.available) {
    (async () => {
      try {
        const payload = await api(`/api/experiments/${encodeURIComponent(experimentId)}/analysis/${encodeURIComponent(epochId)}/${encodeURIComponent(foldId)}`);
        if (payload.content) body.append(renderMarkdown(payload.content));
      } catch (error) { body.append(el("div", { class: "hint" }, `分析加载失败：${error.message}`)); }
    })();
  } else body.append(el("div", { class: "hint" }, "尚未生成分析。"));
  return wrap;
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
  const panel = el("div", { class: "panel section-gap" }, el("h4", {}, "Held-out 冻结测试"));
  panel.append(el("table", { class: "data" },
    el("tr", {}, el("th", {}, "区间"), el("th", {}, "收益"), el("th", {}, "Sharpe"), el("th", {}, "回撤")),
    ...records.map((record) => {
      const result = record.test_result || {};
      return el("tr", {},
        el("td", {}, String(record.fold_id || "").replace("heldout_", "")),
        el("td", { class: numClass(result.total_return) }, fmtPct(result.total_return)),
        el("td", {}, result.sharpe !== undefined && result.sharpe !== null ? Number(result.sharpe).toFixed(2) : "—"),
        el("td", {}, fmtPct(result.max_drawdown)),
      );
    }),
  ));
  return panel;
}
