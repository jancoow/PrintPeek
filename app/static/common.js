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

const H264_CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033"];

// MediaSource (or Safari's ManagedMediaSource) with H.264 support, or null
function h264Support() {
  const MS = window.ManagedMediaSource || window.MediaSource;
  if (!MS) return null;
  const codecs = H264_CODECS.filter((c) => MS.isTypeSupported(`video/mp4; codecs="${c}"`)).join();
  return codecs ? { MS, codecs } : null;
}

// Plays a printer's H.264 re-stream in a <video>: fragmented MP4 over a WebSocket, fed into
// MediaSource and kept close to live. onEnd(started) is called once when it fails or stops.
function h264Player(video, printerId, onEnd) {
  const { MS, codecs } = h264Support();
  const ms = new MS();
  let ws, sb, queue = [], started = false, ended = false;
  const startTimer = setTimeout(() => end(), 10000);

  function end() {
    if (ended) return;
    stop();
    onEnd(started);
  }
  function stop() {
    ended = true;
    clearTimeout(startTimer);
    if (ws) { ws.onclose = null; ws.close(); }
    video.pause();
    video.removeAttribute("src");
    video.srcObject = null;
    video.load();
  }
  function pump() {
    if (!sb || sb.updating || ended) return;
    if (queue.length) {
      const data = new Uint8Array(queue.reduce((n, b) => n + b.byteLength, 0));
      queue.reduce((offset, b) => { data.set(new Uint8Array(b), offset); return offset + b.byteLength; }, 0);
      queue = [];
      try { sb.appendBuffer(data); } catch { end(); }
      return;
    }
    if (!sb.buffered.length) return;
    const last = sb.buffered.end(sb.buffered.length - 1);
    const first = sb.buffered.start(0);
    if (last - first > 10) return sb.remove(first, last - 5); // keep memory flat; triggers updateend again
    const behind = last - video.currentTime;
    if (behind > 4) video.currentTime = last - 0.5; // fell behind (tab was busy): jump to live
    else video.playbackRate = behind > 1.5 ? 1.25 : behind < 0.3 ? 0.9 : 1;
  }

  ms.addEventListener("sourceopen", () => {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/printers/${printerId}/live`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send(JSON.stringify({ type: "mse", value: codecs }));
    ws.onclose = end;
    ws.onmessage = (e) => {
      if (typeof e.data !== "string") {
        queue.push(e.data);
        return pump();
      }
      const msg = JSON.parse(e.data);
      if (msg.type === "mse" && !sb) {
        sb = ms.addSourceBuffer(msg.value);
        sb.mode = "segments";
        sb.addEventListener("updateend", pump);
      } else if (msg.type === "error") {
        end();
      }
    };
  }, { once: true });
  video.addEventListener("playing", () => { started = true; clearTimeout(startTimer); }, { once: true });

  video.muted = true;
  video.playsInline = true;
  if (MS === window.ManagedMediaSource) {
    video.disableRemotePlayback = true;
    video.srcObject = ms;
  } else {
    video.src = URL.createObjectURL(ms);
  }
  video.play().catch(() => {});
  return { stop };
}

// Shows a printer's live camera: H.264 in the <video> when the browser and server support it
// (a fraction of the bandwidth), otherwise MJPEG in the <img>. Reconnects after errors and
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
    if (active && img.hidden === false) retry = setTimeout(load, 5000);
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
