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

const MANUAL_SEND_MS = 20;
const MANUAL_KEY_CODES = {
  Digit1: "1", Digit2: "2", Digit3: "3", Digit4: "4", Digit5: "5",
  KeyQ: "q", KeyW: "w", KeyE: "e", KeyR: "r", KeyT: "t", KeyU: "u",
  KeyI: "i", KeyO: "o", KeyP: "p", KeyA: "a", KeyS: "s", KeyD: "d",
  KeyF: "f", KeyG: "g", KeyH: "h", KeyJ: "j", KeyK: "k", KeyL: "l",
  KeyZ: "z", KeyX: "x", KeyC: "c", KeyV: "v", KeyB: "b", KeyN: "n",
  KeyM: "m", Space: "space", Tab: "tab", Enter: "enter",
  ShiftLeft: "shift", ShiftRight: "shift",
  ControlLeft: "ctrl", ControlRight: "ctrl",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
};

function manualClientId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `browser-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

const manualClient = manualClientId();
let manualLocked = false;
let manualTimer = null;
let manualInFlight = false;
let manualPendingDisarm = false;
let manualSeq = 0;
let manualLastError = "";
const manualKeys = new Set();
const manualButtons = new Set();
const manualMouse = { dx: 0, dy: 0, wheel: 0 };

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
  $("preview").src = `/preview.png?t=${Date.now()}`;
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

function clamp(value, limit) {
  return Math.max(-limit, Math.min(limit, value));
}

function clearManualMouse() {
  manualMouse.dx = 0;
  manualMouse.dy = 0;
  manualMouse.wheel = 0;
}

function clearManualState() {
  manualKeys.clear();
  manualButtons.clear();
  clearManualMouse();
}

function stopManualTimer() {
  if (manualTimer !== null) {
    clearInterval(manualTimer);
    manualTimer = null;
  }
}

function sendManualDisarm() {
  fetch("/api/manual/input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client: manualClient, seq: ++manualSeq, armed: false }),
  }).catch(() => {});
}

function releaseManual() {
  const wasActive = manualLocked || manualInFlight || manualPendingDisarm;
  manualLocked = false;
  stopManualTimer();
  clearManualState();
  if (wasActive) {
    if (manualInFlight) manualPendingDisarm = true;
    else sendManualDisarm();
  }
  if (document.pointerLockElement) document.exitPointerLock();
  updateManualUi(lastState);
}

async function manualTick() {
  if (!manualLocked || manualInFlight) return;
  manualInFlight = true;
  const payload = {
    client: manualClient,
    seq: ++manualSeq,
    armed: true,
    keys: [...manualKeys].sort(),
    buttons: [...manualButtons].sort(),
    dx: manualMouse.dx,
    dy: manualMouse.dy,
    wheel: manualMouse.wheel,
  };
  clearManualMouse();
  try {
    const response = await fetch("/api/manual/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      let message = `input rejected (${response.status})`;
      try {
        message = (await response.json()).error || message;
      } catch (_) {
        // keep the status message
      }
      manualLastError = message;
      releaseManual();
    } else {
      manualLastError = "";
    }
  } catch (_) {
    manualLastError = "input unavailable";
  } finally {
    manualInFlight = false;
    if (!manualLocked && manualPendingDisarm) {
      manualPendingDisarm = false;
      sendManualDisarm();
    }
  }
  updateManualUi(lastState);
}

function startManualTimer() {
  stopManualTimer();
  manualTick();
  manualTimer = setInterval(manualTick, MANUAL_SEND_MS);
}

function requestManualControl() {
  const state = lastState;
  if (!state || state.state !== "running" || state.mode !== "manual") return;
  if (!state.manual || state.manual.available === false) {
    manualLastError = state.manual?.last_error || "manual input is not ready yet";
    updateManualUi(state);
    return;
  }
  const preview = $("preview-box");
  const fallback = () => {
    try {
      const result = preview.requestPointerLock();
      if (result && typeof result.catch === "function") {
        result.catch(() => {
          manualLastError = "pointer lock was blocked";
          updateManualUi(lastState);
        });
      }
    } catch (error) {
      manualLastError = error.message;
      updateManualUi(lastState);
    }
  };
  try {
    const result = preview.requestPointerLock({ unadjustedMovement: true });
    if (result && typeof result.catch === "function") result.catch(fallback);
  } catch (_) {
    fallback();
  }
}

function updateManualUi(state) {
  const overlay = $("manual-overlay");
  const manualRunning = Boolean(state && state.state === "running" && state.mode === "manual");
  overlay.classList.toggle("visible", manualRunning);
  document.body.classList.toggle("manual-mode", manualRunning);
  document.body.classList.toggle("manual-active", manualLocked);
  if (!manualRunning) return;

  const manual = state.manual || {};
  const available = manual.available !== false;
  const foreign = Boolean(manual.armed && manual.client && manual.client !== manualClient);
  let title = "Click preview to control";
  let detail = "Mouse look · WASD move · LMB fire · RMB grip · Tab hand · Esc releases";
  if (manualLocked) {
    if (manual.armed && manual.client === manualClient) {
      title = "Manual control active";
      detail = "Esc releases the mouse · click the preview to re-arm";
    } else {
      title = "Capturing input…";
      detail = "waiting for the manual client";
    }
  } else if (foreign) {
    title = "Another browser has control";
    detail = "Click the preview to take over";
  } else if (!available) {
    title = manual.last_error ? "Manual input unavailable" : "Waiting for the manual client…";
    detail = manual.last_error || "The session is still starting";
  } else if (manualLastError) {
    title = "Input problem";
    detail = manualLastError;
  }
  $("manual-title").textContent = title;
  $("manual-detail").textContent = detail;
}

function manualKey(code) {
  return MANUAL_KEY_CODES[code] || null;
}

function render(state) {
  const previous = currentState;
  currentState = state.state;
  lastState = state;

  $("state").textContent = state.state;
  $("state").className = `badge ${state.state}`;
  $("preview-clip").style.aspectRatio = `${state.preview.width} / ${state.preview.height}`;

  const showSession = state.state === "running" || state.state === "stopping";
  $("session-id").textContent = showSession ? state.session_id || "" : "";
  $("session-id").style.display = showSession ? "" : "none";
  $("uptime").textContent = showSession ? uptime(state.started_at) : "";
  $("uptime").style.display = showSession ? "" : "none";

  if (
    manualLocked
    && (state.state !== "running" || state.mode !== "manual")
  ) {
    releaseManual();
  }
  if (
    manualLocked
    && state.manual?.armed
    && state.manual.client
    && state.manual.client !== manualClient
  ) {
    releaseManual();
  }

  applyControls(state);
  syncPreview(previous, state);
  updatePreviewWait(state);
  updateManualUi(state);
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
  bodySync();
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

$("preview-box").addEventListener("click", () => {
  if (!manualLocked) requestManualControl();
});

document.addEventListener("pointerlockchange", () => {
  const locked = document.pointerLockElement === $("preview-box");
  if (locked) {
    manualLocked = true;
    manualPendingDisarm = false;
    manualLastError = "";
    clearManualState();
    startManualTimer();
  } else if (manualLocked) {
    releaseManual();
  }
  updateManualUi(lastState);
});

document.addEventListener("pointerlockerror", () => {
  manualLastError = "pointer lock was blocked";
  updateManualUi(lastState);
});

document.addEventListener("keydown", (event) => {
  if (!manualLocked) return;
  const key = manualKey(event.code);
  if (!key) return;
  event.preventDefault();
  manualKeys.add(key);
}, true);

document.addEventListener("keyup", (event) => {
  if (!manualLocked) return;
  const key = manualKey(event.code);
  if (!key) return;
  event.preventDefault();
  manualKeys.delete(key);
}, true);

document.addEventListener("mousemove", (event) => {
  if (!manualLocked) return;
  manualMouse.dx = clamp(manualMouse.dx + clamp(event.movementX, 500), 10000);
  manualMouse.dy = clamp(manualMouse.dy + clamp(event.movementY, 500), 10000);
});

document.addEventListener("mousedown", (event) => {
  if (!manualLocked || ![0, 1, 2].includes(event.button)) return;
  event.preventDefault();
  manualButtons.add(event.button);
});

document.addEventListener("mouseup", (event) => {
  if (!manualLocked) return;
  manualButtons.delete(event.button);
});

document.addEventListener("wheel", (event) => {
  if (!manualLocked) return;
  event.preventDefault();
  manualMouse.wheel = clamp(manualMouse.wheel - Math.sign(event.deltaY), 10);
}, { passive: false });

document.addEventListener("contextmenu", (event) => {
  if (manualLocked) event.preventDefault();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") releaseManual();
});

window.addEventListener("blur", () => releaseManual());

refresh();
setInterval(refresh, 1000);

// Third-person body view. Separate from the session: it is its own MuJoCo
// process, so it can be live while the game session is stopped.
const BODY_REFRESH_MS = 120;
let bodyTimer = null;
let bodyFailures = 0;

function bodyRefresh() {
  const info = (lastState && lastState.body) || null;
  const img = $("body");
  const wait = $("body-wait");
  if (!info || !info.running || !info.frame) {
    wait.hidden = false;
    wait.textContent = info && info.error ? info.error : "body view off";
    return;
  }
  const probe = new Image();
  probe.onload = () => {
    bodyFailures = 0;
    img.src = probe.src;
    wait.hidden = true;
  };
  probe.onerror = () => {
    bodyFailures += 1;
    if (bodyFailures > 8) {
      wait.hidden = false;
      wait.textContent = "waiting for body view";
    }
  };
  probe.src = `/api/body.jpg?t=${Date.now()}`;
}

function bodySync() {
  const info = (lastState && lastState.body) || null;
  const wanted = Boolean(info && info.running);
  if (wanted && bodyTimer === null) {
    bodyTimer = setInterval(bodyRefresh, BODY_REFRESH_MS);
    bodyRefresh();
  } else if (!wanted && bodyTimer !== null) {
    clearInterval(bodyTimer);
    bodyTimer = null;
  }
}

// Manual verbs: the human is the BodyCommand writer. The dashboard only forwards
// a verb name; the viewer owns the mapping to a pose, so the two cannot drift.
async function bodyVerb(verb, button) {
  const status = $("verb-status");
  if (button) button.disabled = true;
  try {
    const reply = await post("/api/body/command", { verb });
    status.textContent = reply.ok
      ? `${verb} → seq ${reply.seq ?? "?"}, authority ${reply.authority ?? 0}`
      : `${verb} rejected: ${reply.error || "unknown error"}`;
  } catch (error) {
    status.textContent = `${verb} failed: ${error.message}`;
  } finally {
    if (button) button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-verb]");
  if (button) bodyVerb(button.dataset.verb, button);
});
