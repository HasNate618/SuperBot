const $ = (id) => document.getElementById(id);

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

function uptime(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function render(state) {
  $("state").textContent = state.state;
  $("state").className = `badge ${state.state}`;
  $("session-id").textContent = state.session_id || "";
  $("uptime").textContent = uptime(state.started_at);
  $("start").disabled = !["stopped", "failed"].includes(state.state);
  $("stop").disabled = state.state === "stopped";
  $("preview-wait").style.display = state.preview.last_frame_at ? "none" : "block";
  showError(state.last_error);

  document.querySelectorAll("#modes button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
    button.disabled = state.state !== "running";
  });

  $("kill-game").disabled = state.state !== "running";
  $("kill-steam").disabled = state.state !== "running";

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
  try {
    render(await api("/api/state"));
  } catch (error) {
    showError(error.message);
  }
}

$("start").onclick = async () => {
  try {
    await post("/api/session/start");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
};

$("stop").onclick = async () => {
  try {
    await post("/api/session/stop");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
};

document.querySelectorAll("#modes button").forEach((button) => {
  button.onclick = async () => {
    try {
      await post("/api/session/mode", { mode: button.dataset.mode });
      await refresh();
    } catch (error) {
      showError(error.message);
    }
  };
});

$("kill-game").onclick = async () => {
  if (!confirm("Kill the game process?")) return;
  try {
    await post("/api/session/kill", { target: "game" });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
};

$("kill-steam").onclick = async () => {
  if (!confirm("Shut down the hidden Steam client?")) return;
  try {
    await post("/api/session/kill", { target: "steam" });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
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
