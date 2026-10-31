const list = document.querySelector("#monitor-list");
const notice = document.querySelector("#notice");
const connection = document.querySelector("#connection-state");
const connectionLabel = document.querySelector("#connection-label");
const form = document.querySelector("#add-form");
const refreshButton = document.querySelector("#refresh-button");
let refreshing = false;

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

function setConnection(online) {
  connection.classList.toggle("is-online", online);
  connection.classList.toggle("is-offline", !online);
  connectionLabel.textContent = online ? "Monitor online" : "Connection lost";
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatTime(value) {
  if (!value) return "Not checked yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function updateSummary(monitors) {
  document.querySelector("#total-count").textContent = monitors.length;
  document.querySelector("#up-count").textContent = monitors.filter((monitor) => monitor.status === "up").length;
  document.querySelector("#down-count").textContent = monitors.filter((monitor) => monitor.status === "down").length;
  document.querySelector("#unknown-count").textContent = monitors.filter((monitor) => monitor.status === "unknown").length;
}

function renderMonitor(monitor) {
  const card = makeElement("article", "monitor-card");
  const top = makeElement("div", "monitor-top");
  const title = makeElement("div", "monitor-title");
  const name = makeElement("h3", "monitor-name", monitor.name);
  const url = makeElement("a", "monitor-url", monitor.url);
  url.href = monitor.url;
  url.target = "_blank";
  url.rel = "noopener noreferrer";
  url.title = monitor.url;
  const badge = makeElement("span", `status-badge status-${monitor.status}`, monitor.status);
  title.append(name, url);
  top.append(title, badge);

  const metrics = makeElement("div", "monitor-metrics");
  const latest = makeElement("span", "", "Last checked");
  latest.append(makeElement("strong", "", formatTime(monitor.last_checked)));
  const latency = makeElement("span", "", "Latency");
  latency.append(makeElement("strong", "", monitor.latency_ms === null ? "—" : `${monitor.latency_ms} ms`));
  const response = makeElement("span", "", "Response");
  response.append(makeElement("strong", "", monitor.status_code === null ? "—" : String(monitor.status_code)));
  metrics.append(latest, latency, response);

  const history = makeElement("div", "history");
  history.setAttribute("aria-label", "Recent check history");
  const checks = monitor.history || [];
  for (const check of checks.slice(-18)) {
    const bar = makeElement("span", `history-bar status-${check.status}`);
    bar.title = `${check.status.toUpperCase()} · ${formatTime(check.checked_at)}${check.status_code ? ` · HTTP ${check.status_code}` : ""}`;
    history.append(bar);
  }
  if (checks.length === 0) history.append(makeElement("span", "history-caption", "No history yet"));
  else history.append(makeElement("span", "history-caption", `${checks.length} checks`));

  const actions = makeElement("div", "monitor-actions");
  const checkButton = makeElement("button", "text-button", "Check now");
  checkButton.type = "button";
  checkButton.dataset.action = "check";
  checkButton.dataset.id = monitor.id;
  const deleteButton = makeElement("button", "text-button delete-button", "Remove");
  deleteButton.type = "button";
  deleteButton.dataset.action = "delete";
  deleteButton.dataset.id = monitor.id;
  actions.append(checkButton, deleteButton);

  card.append(top, metrics, history, actions);
  if (monitor.error) card.title = monitor.error;
  return card;
}

function render(monitors) {
  updateSummary(monitors);
  list.replaceChildren();
  if (monitors.length === 0) {
    const empty = makeElement("div", "empty-state");
    empty.append(makeElement("strong", "", "Nothing to monitor yet"));
    empty.append(document.createTextNode("Add a health URL to start collecting checks."));
    list.append(empty);
  } else {
    for (const monitor of monitors) list.append(renderMonitor(monitor));
  }
  list.setAttribute("aria-busy", "false");
}

async function loadMonitors() {
  if (refreshing) return;
  refreshing = true;
  refreshButton.disabled = true;
  try {
    const payload = await request("/api/monitors");
    render(payload.monitors);
    setConnection(true);
    showNotice("");
  } catch (error) {
    setConnection(false);
    showNotice(error.message);
  } finally {
    refreshing = false;
    refreshButton.disabled = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const submitButton = form.querySelector("button[type=submit]");
  submitButton.disabled = true;
  const formData = new FormData(form);
  try {
    await request("/api/monitors", {
      method: "POST",
      body: JSON.stringify({
        name: formData.get("name"),
        url: formData.get("url"),
        interval_seconds: Number(formData.get("interval_seconds")),
      }),
    });
    form.reset();
    document.querySelector("#monitor-interval").value = "60";
    showNotice("");
    await loadMonitors();
  } catch (error) {
    showNotice(error.message);
  } finally {
    submitButton.disabled = false;
  }
});

list.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const { action, id } = button.dataset;
  if (action === "delete" && !window.confirm("Remove this monitor and its check history?")) return;
  button.disabled = true;
  try {
    if (action === "check") await request(`/api/monitors/${id}/check`, { method: "POST" });
    else await request(`/api/monitors/${id}`, { method: "DELETE" });
    showNotice("");
    await loadMonitors();
  } catch (error) {
    showNotice(error.message);
    button.disabled = false;
  }
});

refreshButton.addEventListener("click", loadMonitors);
loadMonitors();
window.setInterval(loadMonitors, 5000);