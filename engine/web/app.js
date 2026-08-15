// All rendering builds DOM nodes and assigns textContent — never innerHTML. Request paths,
// override ids and match patterns all originate off-machine or from the control API, so treating
// them as markup would be a script-injection sink in a page that holds full API access.

const api = (path, opts = {}) => {
  const options = { ...opts };
  if (options.body !== undefined) {
    options.headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  }
  return fetch(`/__mock__/${path}`, options).then((r) => r.json());
};

const el = (id) => document.getElementById(id);

const openOverrides = new Set(); // preserve expand/collapse across auto-refresh

function span(className, text) {
  const node = document.createElement("span");
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function refresh() {
  let health = null;
  let sessions = null;
  let overrides = [];
  let recent = [];
  try {
    [health, sessions, overrides, recent] = await Promise.all([
      api("health"),
      api("sessions"),
      api("overrides"),
      api("recent"),
    ]);
  } catch {
    renderBanner(null); // engine down or unreachable — say so instead of freezing on stale data
    return;
  }
  renderBanner(health);
  renderSessions(sessions);
  renderOverrides(overrides);
  renderRecent(recent);
}

function renderBanner(health) {
  const dot = el("banner-dot");
  const text = el("banner-text");
  if (!health) {
    dot.className = "dot off";
    text.textContent = "⚪ not connected";
    return;
  }
  if (health.intercepting) {
    dot.className = "dot on";
    text.textContent = `🔴 INTERCEPTING — "${health.activeSession}" · ${health.overrideCount} override(s) · proxy :${health.proxyPort}`;
  } else {
    dot.className = "dot warn";
    text.textContent = `🟠 proxy up but PAC disabled — run \`lyrebird up\` (session "${health.activeSession}")`;
  }
  el("override-count").textContent = health.overrideCount || 0;
}

function renderSessions(data) {
  const select = el("session-select");
  select.replaceChildren();
  for (const session of data.sessions) {
    const option = document.createElement("option");
    option.value = session.name;
    option.textContent = `${session.name}${session.verified ? " ✓" : ""} (${session.overrideCount})`;
    option.selected = session.name === data.active;
    select.appendChild(option);
  }
}

function renderOverrides(overrides) {
  const list = el("overrides");
  list.replaceChildren();
  if (!overrides.length) {
    const empty = document.createElement("div");
    empty.className = "row muted";
    empty.textContent = "No overrides in the active session.";
    list.appendChild(empty);
    return;
  }
  for (const override of overrides) {
    const match = override.match || {};
    const details = document.createElement("details");
    details.className = "override matched";
    details.open = openOverrides.has(override.id);
    details.ontoggle = () => {
      details.open ? openOverrides.add(override.id) : openOverrides.delete(override.id);
    };

    const summary = document.createElement("summary");
    summary.append(
      span("caret", "▸"),
      span("method", match.method || "*"),
      span("path", match.path || "*"),
      span("grow"),
    );
    if (override.delayMs) summary.append(span("returns", `⏱ ${override.delayMs} ms`));
    summary.append(span("returns", summarizeReturn(override)));

    const remove = document.createElement("button");
    remove.textContent = "✕";
    remove.title = "Remove override";
    remove.onclick = async (event) => {
      event.preventDefault();
      event.stopPropagation();
      await api(`overrides/${encodeURIComponent(override.id)}`, { method: "DELETE" });
      refresh();
    };
    summary.appendChild(remove);

    const body = document.createElement("pre");
    body.className = "body";
    body.textContent = bodyPretty(override);

    details.append(summary, body);
    list.appendChild(details);
  }
}

function summarizeReturn(override) {
  if (override.mode === "replace") return `replace → ${override.status || 200}`;
  return `patch${override.patchStrategy ? ` (${override.patchStrategy})` : ""}`;
}

function bodyPretty(override) {
  if (override.mode === "replace") {
    if (override.body === undefined || override.body === null) return "(empty body)";
    return typeof override.body === "string" ? override.body : JSON.stringify(override.body, null, 2);
  }
  return JSON.stringify(override.patch || {}, null, 2);
}

function renderRecent(recent) {
  const list = el("recent");
  list.replaceChildren();
  for (const entry of recent.slice(0, 40)) {
    const row = document.createElement("div");
    row.className = `row${entry.matched ? " matched" : ""}`;
    row.append(
      span("method", entry.method),
      span(`status-${String(entry.status)[0]}`, entry.status),
      span("path", entry.path),
    );
    if (entry.matched) row.append(span("grow"), span("muted", `→ ${entry.matched}`));
    else if (entry.patchSkipped) row.append(span("grow"), span("muted", `patch skipped: ${entry.patchSkipped}`));
    list.appendChild(row);
  }
}

el("session-select").onchange = async (event) => {
  await api("sessions/active", { method: "PUT", body: JSON.stringify({ name: event.target.value }) });
  refresh();
};
// Names the session and the count: this deletes rules from a file someone may be keeping.
el("clear-overrides").onclick = async () => {
  const session = el("session-select").value;
  const count = el("override-count").textContent;
  if (!confirm(`Delete all ${count} override(s) from "${session}"?\n\nThis rewrites the session file.`)) return;
  await api("overrides", { method: "DELETE" });
  refresh();
};

refresh();
setInterval(refresh, 3000);
