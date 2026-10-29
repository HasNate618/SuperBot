const $ = (id) => document.getElementById(id);

const LIVE_STATES = new Set(["starting", "running", "stopping"]);

let lastState = null;
let previewRfb = null;
let previewConnected = false;
let previewConnecting = false;
let previewCreds = null;
let previewConfig = null;
let previewConfigPromise = null;
let previewGeneration = 0;
let previewError = "";
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

function previewAuthVisible() {
  return $("vnc-auth").classList.contains("visible");
}

function setPreviewError(message) {
  previewError = message || "";
  $("vnc-error").textContent = previewError;
}

function setPreviewConnecting(connecting) {
  previewConnecting = connecting;
  const button = $("vnc-auth").querySelector("button[type=submit]");
  if (!button) return;
  button.disabled = connecting;
  button.textContent = connecting ? "Connecting…" : "Connect preview";
}

function showPreviewAuth() {
  const wasVisible = previewAuthVisible();
  $("vnc-auth").classList.add("visible");
  $("vnc-error").textContent = previewError || "";
  if (!wasVisible) $("vnc-pass").focus();
}

function hidePreviewAuth() {
  $("vnc-auth").classList.remove("visible");
  $("vnc-pass").value = "";
}

async function loadPreviewConfig() {
  if (previewConfig) return previewConfig;
  const generation = previewGeneration;
  if (!previewConfigPromise) {
    const promise = api("/api/preview/config");
    previewConfigPromise = promise;
    const clear = () => {
      if (previewConfigPromise === promise) previewConfigPromise = null;
    };
    promise.then(clear, clear);
  }
  const config = await previewConfigPromise;
  if (generation === previewGeneration) {
    previewConfig = config;
    $("vnc-user").value = config.username || "";
    if (config.assets_available === false) {
      setPreviewError("noVNC assets are unavailable");
    }
  }
  return config;
}

function closePreview() {
  previewGeneration += 1;
  const rfb = previewRfb;
  previewRfb = null;
  previewConnected = false;
  setPreviewConnecting(false);
  if (rfb) {
    try {
      rfb.disconnect();
    } catch (_) {
      // already gone
    }
  }
  previewConfig = null;
  previewConfigPromise = null;
  hidePreviewAuth();
  $("vnc-pass").value = "";
  previewCreds = null;
  setPreviewError("");
}

async function connectPreview() {
  if (previewRfb || previewConnecting || !previewCreds) return;
  const generation = previewGeneration;
  setPreviewConnecting(true);
  try {
    const config = await loadPreviewConfig();
    if (generation !== previewGeneration || !previewCreds) return;
    if (!config || config.assets_available === false) {
      setPreviewError("noVNC assets are unavailable");
      previewCreds = null;
      showPreviewAuth();
      return;
    }
    const { default: RFB } = await import("/novnc/core/rfb.js");
    if (generation !== previewGeneration || !previewCreds) return;
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const wsUrl = config.ws_url || `${protocol}://${location.hostname}:${config.proxy_port}/`;
    const rfb = new RFB($("preview"), wsUrl, {
      credentials: previewCreds,
      wsProtocols: ["binary"],
    });
    if (generation !== previewGeneration || !previewCreds) {
      try {
        rfb.disconnect();
      } catch (_) {
        // already gone
      }
      return;
    }
    previewRfb = rfb;
    previewConnected = false;
    rfb.viewOnly = true;
    rfb.scaleViewport = false;
    rfb.resizeSession = false;
    rfb.focusOnClick = false;

    rfb.addEventListener("connect", () => {
      if (previewRfb !== rfb) return;
      previewConnected = true;
      setPreviewConnecting(false);
      previewCreds = null;
      previewError = "";
      $("vnc-error").textContent = "";
      hidePreviewAuth();
      $("preview-wait").style.display = "none";
    });

    rfb.addEventListener("securityfailure", (event) => {
      if (previewRfb !== rfb) return;
      previewConnected = false;
      setPreviewConnecting(false);
      setPreviewError(event.detail?.reason || "authentication failed");
      showPreviewAuth();
    });

    rfb.addEventListener("credentialsrequired", () => {
      if (previewRfb !== rfb) return;
      previewConnected = false;
      setPreviewConnecting(false);
      setPreviewError("VNC credentials are required");
      showPreviewAuth();
      rfb.disconnect();
    });

    rfb.addEventListener("disconnect", (event) => {
      if (previewRfb !== rfb) return;
      previewGeneration += 1;
      previewRfb = null;
      previewConnected = false;
      setPreviewConnecting(false);
      previewCreds = null;
      if (!event.detail.clean) {
        if (!previewError) setPreviewError("VNC connection closed; check the password and that the stream is running");
        showPreviewAuth();
      } else if (previewError) {
        showPreviewAuth();
      } else {
        hidePreviewAuth();
      }
    });
  } catch (error) {
    if (generation !== previewGeneration) return;
    setPreviewConnecting(false);
    previewRfb = null;
    previewConnected = false;
    previewCreds = null;
    setPreviewError(error.message || "preview connection failed");
    showPreviewAuth();
  } finally {
    if (generation === previewGeneration && !previewRfb) setPreviewConnecting(false);
  }
}

function syncPreview(state) {
  if (!LIVE_STATES.has(state.state)) {
    closePreview();
    return;
  }
  const preview = state.preview || {};
  const transportAlive = Boolean(preview.vnc_alive && preview.proxy_alive);
  if (transportAlive) {
    if (previewRfb && previewConnected) {
      hidePreviewAuth();
    } else if (previewCreds) {
      connectPreview();
    } else {
      if (!previewConfig) {
        loadPreviewConfig().catch((error) => {
          setPreviewError(error.message || "preview configuration unavailable");
        });
      }
      showPreviewAuth();
    }
  } else {
    hidePreviewAuth();
  }
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
    return;
  }
  const preview = state.preview || {};
  const transportAlive = Boolean(preview.vnc_alive && preview.proxy_alive);
  if (previewRfb && previewConnected) {
    wait.style.display = "none";
  } else if (!transportAlive) {
    wait.textContent = "waiting for VNC transport…";
    wait.style.display = "block";
  } else if (previewAuthVisible()) {
    wait.style.display = "none";
  } else {
    wait.textContent = "authenticate to view the preview";
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
  const audio = state.audio || {};
  const audioButton = $("mute-audio");
  audioButton.textContent = audio.muted ? "Unmute game audio" : "Mute game audio";
  audioButton.disabled = actionInFlight || !running || !audio.available;
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
  let detail = "Mouse look · WASD move · arrows hand · F punch · LMB fire · RMB grip · Tab hand · Esc releases";
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
  syncPreview(state);
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
  const preview = state.preview || {};
  const transportAlive = Boolean(preview.vnc_alive && preview.proxy_alive);
  cards.push(`
    <div class="card">
      <strong>Preview</strong>
      <span class="${transportAlive ? "ok" : "bad"}">${transportAlive ? "live" : "waiting"}</span>
      <small>${escapeHtml(preview.mode)} · ${escapeHtml(preview.fps_target)} fps</small>
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

$("vnc-auth").addEventListener("submit", (event) => {
  event.preventDefault();
  setPreviewError("");
  const username = $("vnc-user").value;
  const password = $("vnc-pass").value;
  $("vnc-pass").value = "";
  previewCreds = { username, password };
  connectPreview();
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

$("mute-audio").onclick = () => runAction(() => post("/api/audio/mute", { muted: !(lastState?.audio?.muted) }));

$("log-refresh").onclick = async () => {
  try {
    const name = $("log-name").value;
    const response = await fetch(`/api/logs/${name}`);
    $("log-tail").textContent = response.ok ? await response.text() : `failed: ${response.status}`;
  } catch (error) {
    $("log-tail").textContent = error.message;
  }
};

$("preview-box").addEventListener("click", (event) => {
  if (event.target.closest("#vnc-auth")) return;
  if (!manualLocked) requestManualControl();
}, true);

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
