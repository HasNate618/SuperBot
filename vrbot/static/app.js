const $ = (id) => document.getElementById(id);

const LIVE_STATES = ["starting", "running", "stopping"];

let currentState = null;
let lastState = null;
let previewOpen = false;
let previewRetry = null;
let pollInFlight = false;
let actionInFlight = false;
let clientError = "";
let lastServerError = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      message = (await response.json()).error || message;
    } catch (_) {
      // keep the status text
    }
    throw new Error(message);
  }
  return response.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function showError(message) {
  $("error").textContent = message || "";
  $("error").style.display = message ? "block" : "none";
}

function renderError() {
  showError(clientError || lastServerError || "");
}

function setClientError(message) {
  clientError = message || "";
  renderError();
}

function applyServerError(serverError) {
  const next = serverError || null;
  if (next !== lastServerError) {
    lastServerError = next;
    if (next) clientError = "";
  }
  renderError();
}

function uptime(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function isLive(state) {
  return LIVE_STATES.includes(state);
}

function clearPreviewRetry() {
  if (previewRetry !== null) {
    clearTimeout(previewRetry);
    previewRetry = null;
  }
}

function openPreview() {
  clearPreviewRetry();
  if (previewOpen) return;
  previewOpen = true;
  $("preview").src = `/preview.mjpg?t=${Date.now()}`;
}

function closePreview() {
  clearPreviewRetry();
  previewOpen = false;
  $("preview").removeAttribute("src");
}

function schedulePreviewRetry() {
  clearPreviewRetry();
  if (!isLive(currentState)) return;
  previewRetry = setTimeout(() => {
    previewRetry = null;
    if (!isLive(currentState) || previewOpen) return;
    openPreview();
  }, 1000);
}

function syncPreview(previous, state) {
  const changed = state.state !== previous;
  if (changed) clearPreviewRetry();
  if (!isLive(state.state)) {
    closePreview();
    return;
  }
  if (changed && !previewOpen) openPreview();
}

function updatePreviewWait(state) {
  const wait = $("preview-wait");
  const stateText = {
    stopped: "session stopped",
    failed: "session failed",
    starting: "starting…",
  }[state.state];
  if (stateText) {
    wait.textContent = stateText;
    wait.style.display = "block";
  } else if (state.preview.last_frame_at) {
    wait.style.display = "none";
  } else {
    wait.textContent = "waiting for the first frame…";
    wait.style.display = "block";
  }
}

function applyControls(state) {
  const running = state.state === "running";
  $("start").disabled = actionInFlight || !["stopped", "failed"].includes(state.state);
  $("stop").disabled = actionInFlight || state.state === "stopped";
  document.querySelectorAll("#modes button").forEach((button) => {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
    button.disabled = actionInFlight || !running;
  });
  $("kill-game").disabled = actionInFlight || !running;
  $("kill-steam").disabled = actionInFlight || !running;
}

function render(state) {
  const previous = currentState;
  currentState = state.state;
  lastState = state;

  $("state").textContent = state.state;
  $("state").className = `badge ${state.state}`;

  const showSession = state.state === "running" || state.state === "stopping";
  $("session-id").textContent = showSession ? state.session_id || "" : "";
  $("session-id").style.display = showSession ? "" : "none";
  $("uptime").textContent = showSession ? uptime(state.started_at) : "";
  $("uptime").style.display = showSession ? "" : "none";

  applyControls(state);
  syncPreview(previous, state);
  updatePreviewWait(state);
  applyServerError(state.last_error);

  const cards = Object.entries(state.processes).map(([name, process]) => `
    <div class="card">
      <strong>${escapeHtml(name)}</strong>
      <span class="${process.alive ? "ok" : "bad"}">${process.alive ? "alive" : "down"}</span>
      <small>${escapeHtml(process.pid ?? "")}</small>
    </div>
  `);
  cards.push(`
    <div class="card">
      <strong>OpenXR</strong>
      <span>${escapeHtml(state.openxr_client || "none")}</span>
    </div>
  `);
  cards.push(`
    <div class="card">
      <strong>Preview</strong>
      <span>${escapeHtml(state.preview.fps_actual)} fps</span>
    </div>
  `);
  $("health").innerHTML = cards.join("");
}

async function refresh() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    render(await api("/api/state"));
  } catch (error) {
    setClientError(error.message);
  } finally {
    pollInFlight = false;
  }
}

async function runAction(action) {
  if (actionInFlight) return;
  actionInFlight = true;
  if (lastState) applyControls(lastState);
  try {
    await action();
    setClientError("");
    await refresh();
  } catch (error) {
    setClientError(error.message);
  } finally {
    actionInFlight = false;
    if (lastState) applyControls(lastState);
  }
}

$("preview").addEventListener("error", () => {
  previewOpen = false;
  if (!isLive(currentState)) return;
  schedulePreviewRetry();
});

$("start").onclick = () => runAction(() => post("/api/session/start"));

$("stop").onclick = () => runAction(() => post("/api/session/stop"));

document.querySelectorAll("#modes button").forEach((button) => {
  button.onclick = () => runAction(() => post("/api/session/mode", { mode: button.dataset.mode }));
});

$("kill-game").onclick = () => {
  if (!confirm("Kill the game process?")) return;
  runAction(() => post("/api/session/kill", { target: "game" }));
};

$("kill-steam").onclick = () => {
  if (!confirm("Shut down the hidden Steam client?")) return;
  runAction(() => post("/api/session/kill", { target: "steam" }));
};

$("log-refresh").onclick = async () => {
  try {
    const name = $("log-name").value;
    const response = await fetch(`/api/logs/${name}`);
    $("log-tail").textContent = response.ok ? await response.text() : `failed: ${response.status}`;
  } catch (error) {
    $("log-tail").textContent = error.message;
  }
};

refresh();
setInterval(refresh, 1000);
