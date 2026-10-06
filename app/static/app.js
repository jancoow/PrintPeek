const cards = new Map();
let admin = false;
let timelapses = [];
let printerNames = {};
let filter = "all";
let current = null; // timelapse open in the player

// -- printers ----------------------------------------------------------------

function createCard(p) {
  const el = document.getElementById("printer-card").content.firstElementChild.cloneNode(true);
  const q = (s) => el.querySelector(s);
  q(".name").textContent = p.name;
  q(".cam").href = `/watch/${p.id}`;
  q(".share").addEventListener("click", () => copyText(`${location.origin}/watch/${p.id}`));
  q(".public-mode").addEventListener("change", (e) => setPublic(p.id, e.target));
  document.getElementById("printers").append(el);
  const card = { el, q, stream: liveStream(q(".cam img"), p.id) };
  cards.set(p.id, card);
  return card;
}

function removeCard(id) {
  const card = cards.get(id);
  card.stream.set(false);
  card.el.remove();
  cards.delete(id);
}

function publicText(pub) {
  if (!pub.live) return "Private";
  if (pub.mode === "until") return `Public until ${fmtClock(pub.until)}`;
  if (pub.mode === "print") return "Public until the print ends";
  return "Public";
}

function updateCard(p) {
  const card = cards.get(p.id) || createCard(p);
  const { q } = card;
  const active = p.state === "printing" || p.state === "paused";

  card.stream.set(p.camera.available);
  q(".cam img").style.transform = camTransform(p.camera);
  q(".nocam").hidden = p.camera.available;
  q(".badge:not(.visibility)").hidden = !p.recording;
  q(".frames").textContent = p.frames ? `· ${p.frames} frames` : "";

  const state = q(".state");
  state.className = `state ${p.state || ""}`;
  state.textContent = STATE_LABELS[p.state] || p.klippy || p.state || "?";
  state.title = p.error || p.message || "";

  q(".file").textContent = p.online ? (p.filename || "No file loaded") : (p.error || "Offline");
  q(".fill").style.width = `${((active || p.state === "complete") ? p.progress : 0) * 100}%`;
  q(".progress").textContent = active || p.state === "complete" ? `${(p.progress * 100).toFixed(1)}%` : "–";
  q(".layer").textContent = p.layer ? `${p.layer}${p.total_layers ? " / " + p.total_layers : ""}` : "–";
  q(".elapsed").textContent = active ? fmtDuration(p.print_duration) : "–";
  q(".eta").textContent = active ? fmtDuration(p.eta) : "–";
  q(".finish").hidden = !(p.state === "printing" && p.finish_at);
  q(".finish").textContent = `done at ${fmtFinish(p.finish_at)}`;
  setImage(q(".thumb"), active ? thumbUrl(p) : "");
  q(".nozzle").textContent = (p.extruder.tool ? `${p.extruder.tool} ` : "") + fmtTemp(p.extruder);
  q(".bed").textContent = fmtTemp(p.bed);
  updateEvent(q, p);
  updateTools(q(".tools"), p.tools);

  // Admin: who can see this printer
  const visibility = q(".visibility");
  visibility.hidden = !admin;
  visibility.textContent = publicText(p.public);
  visibility.classList.toggle("live", p.public.live);
  q(".public-ctl").hidden = !admin;
  const select = q(".public-mode");
  if (document.activeElement !== select) {
    const timed = select.querySelector('option[value="until"]');
    timed.hidden = p.public.mode !== "until";
    timed.textContent = p.public.mode === "until" ? publicText(p.public) : "";
    select.value = p.public.mode;
  }
}

// Why the print paused or failed (admins only; guests don't get p.event)
function updateEvent(q, p) {
  const box = q(".event");
  box.hidden = !p.event;
  if (!p.event) return;
  const e = p.event;
  const what = e.kind === "error" ? "Stopped" : "Paused";
  const when = e.at ? ` at ${fmtClock(e.at)}` : "";
  box.className = `event ${e.kind}`;
  q(".event-text").textContent = `${what}${when}: ${e.reason ? e.reason.text : "checking why…"}`;
  box.title = e.reason?.detail || "";
  const photo = q(".event-photo");
  photo.hidden = !e.photo;
  photo.href = `/api/admin/printers/${p.id}/event.jpg?at=${e.at || ""}`;
}

// One chip per toolhead: colour, temperature and filament. Faded when this print doesn't use it.
function updateTools(el, tools) {
  el.hidden = !tools;
  if (!tools) return;
  el.replaceChildren(...tools.map((t) => {
    const chip = document.createElement("span");
    chip.className = "tool" + (t.active ? " active" : "") + (t.used === false ? " unused" : "") + (t.mismatch ? " mismatch" : "");
    const swatch = document.createElement("i");
    swatch.className = "swatch" + (t.color ? "" : " none");
    if (t.color) swatch.style.background = t.color;
    const label = document.createElement("b");
    label.textContent = t.name;
    chip.append(swatch, label, ` ${fmtTemp(t)}`);
    if (t.material) chip.append(` · ${t.material}`);
    if (t.mismatch) chip.append(" ⚠");
    chip.title = [t.empty ? "No filament loaded" : t.material, t.mismatch, t.used === false ? "Not used in this print" : ""]
      .filter(Boolean).join(" · ");
    return chip;
  }));
}

async function setPublic(id, select) {
  const [mode, hours] = select.value.split(":");
  const r = await fetch(`/api/admin/printers/${id}/public`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, hours: hours ? Number(hours) : null }),
  });
  select.blur();
  if (!r.ok) toast("Couldn't change the live view");
  else toast(publicText(await r.json()));
  refreshStatus();
}

async function refreshStatus() {
  try {
    const data = await (await fetch("/api/status")).json();
    const wasAdmin = admin;
    admin = data.admin;
    document.getElementById("login").hidden = admin || !data.login_enabled;
    document.getElementById("logout").hidden = !admin || !data.login_enabled;
    document.getElementById("timelapse-section").hidden = !admin;
    document.getElementById("nothing-live").hidden = data.printers.length > 0;

    const ids = new Set(data.printers.map((p) => p.id));
    [...cards.keys()].filter((id) => !ids.has(id)).forEach(removeCard);
    printerNames = Object.fromEntries(data.printers.map((p) => [p.id, p.name]));
    data.printers.forEach(updateCard);
    if (admin && !wasAdmin) refreshTimelapses();
  } catch (e) {
    console.warn("status", e);
  }
}

document.getElementById("logout").onclick = async () => {
  await fetch("/api/logout", { method: "POST" });
  location.reload();
};

// -- timelapses (admin) --------------------------------------------------------

function renderFilters() {
  const ids = Object.keys(printerNames);
  const el = document.getElementById("filters");
  el.replaceChildren(
    ...[["all", "All"], ...ids.map((id) => [id, printerNames[id]])].map(([id, label]) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.className = filter === id ? "on" : "";
      b.onclick = () => { filter = id; renderFilters(); renderTimelapses(); };
      return b;
    }),
  );
}

function statusText(t) {
  switch (t.status) {
    case "recording": return `Recording · ${t.frames} frames`;
    case "queued": return "Waiting to render";
    case "rendering": return "Rendering…";
    case "failed": return "Render failed";
    case "empty": return "No frames captured";
    default: return fmtDuration(t.print_duration);
  }
}

function renderTimelapses() {
  const list = timelapses.filter((t) => filter === "all" || t.printer === filter);
  const grid = document.getElementById("timelapses");
  if (!list.length) {
    grid.innerHTML = `<div class="empty">No timelapses yet. They appear here as soon as a print starts.</div>`;
    return;
  }
  grid.replaceChildren(...list.map((t) => {
    const b = document.createElement("button");
    b.className = "tl";
    const thumb = t.thumb_url
      ? `<img src="${t.thumb_url}" alt="" loading="lazy">`
      : `<span>${t.status === "recording" ? "● Recording" : statusText(t)}</span>`;
    const date = new Date(t.started * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    b.innerHTML = `
      <div class="thumb">${thumb}</div>
      <div class="meta">
        <div class="title"></div>
        <div class="sub"><span class="printer"></span><span>${date}</span></div>
        <div class="sub"><span class="result-${t.result || ""}">${t.result || ""}${t.share_token ? " · 🔗 shared" : ""}</span><span>${statusText(t)}</span></div>
      </div>`;
    b.querySelector(".title").textContent = t.filename.split("/").pop().replace(/\.gcode$/i, "");
    b.querySelector(".printer").textContent = t.printer_name;
    b.onclick = () => openPlayer(t);
    return b;
  }));
}

async function refreshTimelapses() {
  if (!admin) return;
  try {
    const r = await fetch("/api/timelapses");
    if (!r.ok) return;
    timelapses = await r.json();
    renderFilters();
    renderTimelapses();
  } catch (e) {
    console.warn("timelapses", e);
  }
}

// -- player ----------------------------------------------------------------

const player = document.getElementById("player");
const video = document.getElementById("player-video");
const shareUrl = (token) => `${location.origin}/share/${token}`;

function showShare(token) {
  document.getElementById("player-shared").hidden = !token;
  document.getElementById("player-share-url").value = token ? shareUrl(token) : "";
  document.getElementById("player-share").hidden = !!token || current.status !== "done";
}

function openPlayer(t) {
  current = t;
  document.getElementById("player-title").textContent = `${t.printer_name} · ${t.filename}`;
  video.hidden = !t.video_url;
  video.src = t.video_url || "";
  document.getElementById("player-download").hidden = !t.video_url;
  document.getElementById("player-download").href = t.video_url || "";
  document.getElementById("player-render").hidden = !t.has_frames || t.status === "recording";
  document.getElementById("player-delete").hidden = t.status === "recording";
  showShare(t.share_token);
  player.showModal();
  if (t.video_url) video.play().catch(() => {});
}

player.addEventListener("close", () => { video.pause(); video.removeAttribute("src"); video.load(); });
document.getElementById("player-close").onclick = () => player.close();
player.addEventListener("click", (e) => { if (e.target === player) player.close(); });

document.getElementById("player-share").onclick = async () => {
  const r = await fetch(`/api/timelapses/${current.id}/share`, { method: "POST" });
  const body = await r.json();
  if (!r.ok) return toast(body.detail);
  current.share_token = body.token;
  showShare(body.token);
  copyText(shareUrl(body.token));
  refreshTimelapses();
};

document.getElementById("player-unshare").onclick = async () => {
  const r = await fetch(`/api/timelapses/${current.id}/share`, { method: "DELETE" });
  if (!r.ok) return toast("Couldn't stop sharing");
  current.share_token = null;
  showShare(null);
  toast("Link no longer works");
  refreshTimelapses();
};

document.getElementById("player-copy").onclick = () => copyText(document.getElementById("player-share-url").value);

document.getElementById("player-render").onclick = async () => {
  const r = await fetch(`/api/timelapses/${current.id}/render`, { method: "POST" });
  toast(r.ok ? "Re-rendering…" : (await r.json()).detail);
  player.close();
  refreshTimelapses();
};

document.getElementById("player-delete").onclick = async () => {
  if (!confirm(`Delete the timelapse of ${current.filename}?`)) return;
  const r = await fetch(`/api/timelapses/${current.id}`, { method: "DELETE" });
  toast(r.ok ? "Deleted" : (await r.json()).detail);
  player.close();
  refreshTimelapses();
};

refreshStatus();
setInterval(refreshStatus, 2000);
setInterval(refreshTimelapses, 10000);
