// Helpers shared by the dashboard, the live view and the share page.

const STATE_LABELS = { standby: "Idle", printing: "Printing", paused: "Paused", complete: "Done", cancelled: "Cancelled", error: "Error", offline: "Offline" };

function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "–";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

function fmtTemp(heater) {
  if (!heater || heater.temp == null) return "–";
  return `${heater.temp.toFixed(0)}°` + (heater.target ? ` / ${heater.target.toFixed(0)}°` : "");
}

function fmtClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// "14:35", or "tomorrow 08:10" / "Thu 08:10" when it's not today
function fmtFinish(epochSeconds) {
  if (epochSeconds == null) return "";
  const d = new Date(epochSeconds * 1000);
  const days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  const time = fmtClock(epochSeconds);
  if (days === 0) return time;
  if (days === 1) return `tomorrow ${time}`;
  return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
}

function thumbUrl(p) {
  return p.thumbnail ? `/api/printers/${p.id}/thumbnail.png?f=${encodeURIComponent(p.filename || "")}` : "";
}

// Sets an <img>'s src only when it changes, and hides it without one
function setImage(img, url) {
  img.hidden = !url;
  if (url && img.getAttribute("src") !== url) img.src = url;
  if (!url) img.removeAttribute("src");
}

function camTransform(cam) {
  const t = [];
  if (cam.rotation) t.push(`rotate(${cam.rotation}deg)`);
  if (cam.flip_horizontal) t.push("scaleX(-1)");
  if (cam.flip_vertical) t.push("scaleY(-1)");
  return t.join(" ");
}

// Drives an <img> showing a printer's MJPEG re-stream: reconnects after errors and
// disconnects while the tab is hidden. Call .set(false) to stop it.
function liveStream(img, video, printerId) {
  let active = false, h264 = false, player = null, failures = 0, retry;
  const load = () => {
    clearTimeout(retry);
    player?.stop();
    player = null;
    const show = active && !document.hidden;
    const useH264 = show && h264 && failures < 2 && h264Support();
    video.hidden = !useH264;
    img.hidden = !show || useH264;
    if (useH264) {
      img.removeAttribute("src");
      player = h264Player(video, printerId, (started) => {
        player = null;
        failures = started ? 0 : failures + 1; // never started twice: stay on MJPEG
        retry = setTimeout(load, started ? 2000 : 0);
      });
    } else if (show) {
      img.src = `/api/printers/${printerId}/stream.mjpeg?t=${Date.now()}`;
    } else {
      img.removeAttribute("src");
    }
  };
  img.addEventListener("error", () => {
    clearTimeout(retry);
    if (active) retry = setTimeout(load, 5000);
  });
  document.addEventListener("visibilitychange", load);
  return {
    set(on, canH264 = false) {
      if (on !== active || canH264 !== h264) {
        active = on;
        h264 = canH264;
        load();
      }
    },
  };
}

function toast(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 1800);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Link copied");
  } catch {
    prompt("Copy this link:", text);
  }
}

// Installable as an app on phones (see /sw.js)
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("service worker", e));
}
