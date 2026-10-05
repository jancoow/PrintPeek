"""Watches one printer through Moonraker's HTTP API: status for the dashboard, and timelapse capture.

Nothing runs on the printer. Layer changes come from print_stats.info.current_layer when the
slicer sets it (SET_PRINT_STATS_INFO), otherwise from the Z height and the gcode file's layer
height, the same way Mainsail estimates layers.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import reasons, timelapse
from .access import PublicSwitch
from .config import Config, PrinterConfig

log = logging.getLogger(__name__)

OBJECTS = ("print_stats", "virtual_sdcard", "display_status", "heater_bed", "gcode_move", "webhooks", "toolhead=extruder")
# Queried when the printer has them. exception_manager: Snapmaker's error reports (spaghetti
# detection...), print_task_config: the filament loaded in each of a Snapmaker U1's toolheads.
OPTIONAL = {
    "exception_manager": "exception_manager",
    "print_task_config": "print_task_config=filament_type,filament_color_rgba,filament_exist",
}
ACTIVE = {"printing", "paused"}
FINISHED = {"complete", "cancelled", "error"}
RETRY_DISCOVERY = 60.0
CHECK_GO2RTC = 30.0
PRINT_HEIGHT_MARGIN = 2.0  # mm above the print's top that still counts as "at print height" (z-hops, lifts)
BED_AWAY = 20.0  # mm above the print's top after the end G-code: the bed (or head) moved out of view
END_FRAME_INTERVAL = 2.0  # seconds between the frames kept during the last layer
REASON_DELAY = 3.0  # seconds after a pause before looking for its reason, so the printer has logged it
REASON_WINDOW = 120.0  # seconds before the pause that a reported error or console message counts
GCODE_TAIL = 4096  # bytes of the gcode file before the pause point to look for a pause command
LOG_TAIL = 65536  # bytes of moonraker.log to look for a Snapmaker error report


class PrinterMonitor:
    def __init__(self, cfg: PrinterConfig, app: Config, client: httpx.AsyncClient, public: PublicSwitch):
        self.cfg = cfg
        self.app = app
        self.client = client
        self.public = public
        self.headers = {"X-Api-Key": cfg.api_key} if cfg.api_key else {}
        self.state_file = app.data_dir / "state" / f"{cfg.id}.json"

        self.online = False
        self.error: str | None = None
        self.raw: dict = {}
        self.metadata: dict = {}
        self._metadata_for: str | None = None

        cam = cfg.camera
        self.stream_url = cam.stream
        self.snapshot_url = cam.snapshot
        self.transform = {
            "rotation": cam.rotation or 0,
            "flip_horizontal": bool(cam.flip_horizontal),
            "flip_vertical": bool(cam.flip_vertical),
        }
        self._discovered = False
        self._discovery_at = 0.0
        self.go2rtc_ok = False
        self._go2rtc_at = 0.0
        self.viewers = 0
        self._snapshot: tuple[float, bytes] | None = None
        self._snapshot_lock = asyncio.Lock()
        self.objects: list[str] | None = None  # what to query, listed from Klipper on every (re)connect
        self.tools = ["extruder"]
        self.sensors: list[str] = []

        self.job = timelapse.Job.resume(self.state_file)
        self._last_capture = 0.0
        self._last_layer = 0
        self._pending_layer: int | None = None
        self._print_top = 0.0  # highest Z a layer frame was taken at
        self._end_frame: bytes | None = None  # last-layer frame, confirmed taken at print height
        self._end_candidate: bytes | None = None  # newest last-layer frame, confirmed on the next poll
        self._end_frame_at = 0.0

        self.event: dict | None = None  # the current pause or error and why, for the dashboard
        self.event_photo: bytes | None = None
        self._event_due: float | None = None
        self._prev_state: str | None = None
        self._last_exception: tuple[float, dict] | None = None
        self._sensors_ok: set[str] = set()  # filament sensors that saw filament during this print

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        if self.job:
            log.info("%s: resuming timelapse %s", self.cfg.id, self.job.dir.name)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # printer off, network hiccup, klippy restarting...
                message = str(e) or type(e).__name__
                if self.online or self.error != message:
                    log.warning("%s: %s", self.cfg.id, message)
                self.online = False
                self.error = message
                self.objects = None
            await asyncio.sleep(self.app.poll_interval)

    async def _tick(self) -> None:
        await self._discover_camera()
        await self._ensure_go2rtc_stream()
        if self.objects is None:
            await self._list_objects()
        self.raw = await self._query()
        self.online, self.error = True, None
        if self.raw.get("webhooks", {}).get("state") != "ready":
            self.objects = None  # klippy is restarting: list again when it's back, the config may have changed
        await self._track_job()
        await self._watch_events()
        self.public.observe(self.raw.get("print_stats", {}).get("state") in ACTIVE)

    async def _api(self, path: str, **params) -> dict:
        # An empty params dict makes httpx drop a query string that's already in `path`
        r = await self.client.get(f"{self.cfg.moonraker}{path}", params=params or None, headers=self.headers)
        if r.status_code != 200:
            try:
                message = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = f"HTTP {r.status_code}"
            raise RuntimeError(f"Moonraker {path}: {message}")
        return r.json()["result"]

    async def _list_objects(self) -> None:
        available = set((await self._api("/printer/objects/list"))["objects"])
        self.tools = sorted((o for o in available if re.fullmatch(r"extruder\d*", o)), key=reasons.tool_index) or ["extruder"]
        self.sensors = sorted(o for o in available if o.startswith(("filament_switch_sensor ", "filament_motion_sensor ")))
        optional = [query for name, query in OPTIONAL.items() if name in available]
        self.objects = [*OBJECTS, *self.tools, *self.sensors, *optional]

    async def _query(self) -> dict:
        query = "&".join(quote(o, safe="=,") for o in self.objects or OBJECTS)
        return (await self._api("/printer/objects/query?" + query))["status"]

    # -- camera ------------------------------------------------------------

    def _absolute(self, url: str | None) -> str | None:
        """Moonraker usually stores webcam URLs relative to the printer's web server (port 80)."""
        if not url:
            return None
        if "://" in url:
            return url
        parts = urlsplit(self.cfg.moonraker)
        return urljoin(f"{parts.scheme}://{parts.hostname}/", url)

    async def _discover_camera(self) -> None:
        cam = self.cfg.camera
        if self._discovered or (cam.stream and cam.snapshot):
            return
        if time.monotonic() - self._discovery_at < RETRY_DISCOVERY:
            return
        self._discovery_at = time.monotonic()

        webcams = [w for w in (await self._api("/server/webcams/list"))["webcams"] if w.get("enabled", True)]
        if cam.name:
            webcams = [w for w in webcams if w.get("name") == cam.name]
        if not webcams:
            log.warning("%s: no webcam found in Moonraker; set camera.stream/snapshot in the config", self.cfg.id)
            return
        webcam = webcams[0]
        self.stream_url = self.stream_url or self._absolute(webcam.get("stream_url"))
        self.snapshot_url = self.snapshot_url or self._absolute(webcam.get("snapshot_url"))
        for key in ("rotation", "flip_horizontal", "flip_vertical"):
            if getattr(cam, key) is None and webcam.get(key) is not None:
                self.transform[key] = webcam[key]
        self._discovered = True
        self._go2rtc_at = 0.0
        log.info("%s: camera %r stream=%s snapshot=%s", self.cfg.id, webcam.get("name"), self.stream_url, self.snapshot_url)

    async def _ensure_go2rtc_stream(self) -> None:
        """Register the camera with go2rtc (again, if go2rtc restarted) so it can fan it out."""
        if not (self.app.go2rtc and self.stream_url):
            return
        if time.monotonic() - self._go2rtc_at < CHECK_GO2RTC:
            return
        self._go2rtc_at = time.monotonic()
        try:
            r = await self.client.get(f"{self.app.go2rtc}/api/streams")
            r.raise_for_status()
            if self.cfg.id not in (r.json() or {}):
                r = await self.client.put(f"{self.app.go2rtc}/api/streams", params={"name": self.cfg.id, "src": self.stream_url})
                r.raise_for_status()
                log.info("%s: registered stream with go2rtc", self.cfg.id)
            self.go2rtc_ok = True
        except httpx.HTTPError as e:
            if self.go2rtc_ok:
                log.warning("%s: go2rtc unavailable (%s), falling back to the direct camera stream", self.cfg.id, e)
            self.go2rtc_ok = False

    def live_stream_url(self) -> str | None:
        if self.app.go2rtc and self.go2rtc_ok:
            return f"{self.app.go2rtc}/api/stream.mjpeg?src={self.cfg.id}"
        return self.stream_url

    async def snapshot(self, max_age: float = 0.0) -> bytes | None:
        """A camera frame. With max_age, a recent frame is reused so a burst of public
        requests turns into at most one request to the printer every max_age seconds."""
        async with self._snapshot_lock:
            if max_age and self._snapshot and time.monotonic() - self._snapshot[0] < max_age:
                return self._snapshot[1]
            frame = await self._fetch_snapshot()
            if frame:
                self._snapshot = (time.monotonic(), frame)
            return frame

    async def _fetch_snapshot(self) -> bytes | None:
        urls = [self.snapshot_url] if self.snapshot_url else []
        if self.app.go2rtc and self.go2rtc_ok:
            urls.append(f"{self.app.go2rtc}/api/frame.jpeg?src={self.cfg.id}")
        for url in urls:
            try:
                r = await self.client.get(url, timeout=10)
                if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                    return r.content
            except httpx.HTTPError:
                pass
        return None

    # -- print job tracking ------------------------------------------------

    async def _load_metadata(self, filename: str) -> None:
        self._metadata_for = filename
        try:
            self.metadata = await self._api("/server/files/metadata", filename=filename)
        except Exception as e:
            log.info("%s: no metadata for %s (%s)", self.cfg.id, filename, e)
            self.metadata = {}

    async def _track_job(self) -> None:
        ps = self.raw.get("print_stats", {})
        state, filename = ps.get("state"), ps.get("filename") or ""
        if filename and filename != self._metadata_for:
            await self._load_metadata(filename)

        if state in ACTIVE:
            if self.job and self.job.filename != filename:
                await self._finish("interrupted")
            if not self.job:
                self._start(filename)
            if state == "printing" and (ps.get("print_duration") or 0) > 0:
                await self._keep_end_frame(ps)
                await self._maybe_capture(ps)
        elif self.job:
            await self._finish(state if state in FINISHED else await self._result_from_history())

    def _start(self, filename: str) -> None:
        self.job = timelapse.Job.create(self.app.data_dir, self.cfg, filename, dict(self.transform))
        self.job.remember(self.state_file)
        self._last_capture, self._last_layer, self._pending_layer = 0.0, 0, None
        self._print_top, self._end_frame, self._end_candidate, self._end_frame_at = 0.0, None, None, 0.0
        log.info("%s: print started (%s), recording timelapse", self.cfg.id, filename)

    async def _result_from_history(self) -> str:
        """The print ended while we weren't watching; ask Moonraker how it went."""
        try:
            jobs = (await self._api("/server/history/list", limit=1, order="desc"))["jobs"]
            if jobs and jobs[0].get("filename") == self.job.filename:
                return jobs[0].get("status") or "interrupted"
        except Exception:
            pass
        return "interrupted"

    async def _finish(self, result: str) -> None:
        job, self.job = self.job, None
        if result == "complete":
            # The end G-code has run by now: usually this shows the parked head and the finished print.
            frame = await self.snapshot()
            if self._use_end_frame():
                log.info("%s: end G-code moved the print out of view, ending on the last layer instead", self.cfg.id)
                frame = self._end_frame
            if frame:
                job.add_frame(frame)
        job.finish(result, self.raw.get("print_stats", {}))
        self.state_file.unlink(missing_ok=True)
        log.info("%s: print %s (%s), %d frames", self.cfg.id, result, job.filename, job.frames)
        timelapse.schedule_render(job.dir, self.app.timelapse)

    def _z(self) -> float | None:
        pos = self.raw.get("gcode_move", {}).get("gcode_position")
        return pos[2] if pos else None

    def _print_height(self) -> float | None:
        """Top of the print: from the gcode file's metadata, or the highest layer frame so far."""
        heights = [h for h in (self.metadata.get("object_height"), self._print_top) if h]
        return max(heights) if heights else None

    def _on_last_layer(self, ps: dict) -> bool:
        info = ps.get("info") or {}
        if info.get("current_layer") and info.get("total_layer"):
            return info["current_layer"] >= info["total_layer"]
        return (self.raw.get("virtual_sdcard", {}).get("progress") or 0) >= 0.98

    async def _keep_end_frame(self, ps: dict) -> None:
        """During the last layer, keep a recent frame taken while the bed is still at print height.

        Some printers (like the Snapmaker U1) drop the bed in their end G-code, before the print
        counts as complete, so the usual "finished print" frame shows an empty chamber.
        """
        if self.cfg.final_frame == "after_end" or not self._on_last_layer(ps):
            return
        z, top = self._z(), self._print_height()
        if z is None or top is None:
            return
        if z > top + PRINT_HEIGHT_MARGIN:
            self._end_candidate = None  # may have been taken just as the bed started moving
            return
        if self._end_candidate:
            self._end_frame, self._end_candidate = self._end_candidate, None  # still at print height a poll later
        if time.monotonic() - self._end_frame_at >= END_FRAME_INTERVAL:
            self._end_frame_at = time.monotonic()
            self._end_candidate = await self.snapshot()

    def _use_end_frame(self) -> bool:
        if not self._end_frame or self.cfg.final_frame == "after_end":
            return False
        if self.cfg.final_frame == "before_end":
            return True
        z, top = self._z(), self._print_height()
        return z is not None and top is not None and z > top + BED_AWAY

    # -- pauses and errors --------------------------------------------------

    async def _watch_events(self) -> None:
        """On a pause or error: keep a camera frame of that moment and find out why."""
        ps = self.raw.get("print_stats", {})
        state, now = ps.get("state"), time.time()
        if exceptions := (self.raw.get("exception_manager") or {}).get("exceptions"):
            self._last_exception = (now, exceptions[-1])
        if state == "printing":
            if self._prev_state not in ACTIVE:
                self._sensors_ok = set()  # a new print
            self._sensors_ok.update(s for s in self.sensors if self.raw.get(s, {}).get("filament_detected"))

        if state != self._prev_state:
            first = self._prev_state is None  # already paused when the app started: the time is unknown
            self._prev_state = state
            self.event = self.event_photo = self._event_due = None
            if state in ("paused", "error"):
                self.event_photo = await self.snapshot()
                self.event = {"kind": state, "at": None if first else now, "reason": None, "photo": self.event_photo is not None}
                self._event_due = now if first else now + REASON_DELAY

        if self._event_due is not None and now >= self._event_due:
            self._event_due = None
            self.event["reason"] = await self._find_reason(state)
            log.info("%s: print %s: %s", self.cfg.id, state, self.event["reason"]["text"])

    async def _find_reason(self, state: str) -> dict:
        ps = self.raw.get("print_stats", {})
        if state == "error":
            return reasons.reason(ps.get("message") or "Klipper reported an error", "printer")
        at = self.event.get("at") if self.event else None
        since = at - REASON_WINDOW if at else time.time() - (ps.get("total_duration") or 0)
        if self._last_exception and self._last_exception[0] >= since:
            return reasons.from_exception(self._last_exception[1])
        if found := self._runout():
            return found
        for find in (self._reason_from_gcode, self._reason_from_console, self._reason_from_log):
            try:
                if found := await find(since):
                    return found
            except (httpx.HTTPError, RuntimeError, KeyError, TypeError, ValueError) as e:
                log.debug("%s: %s: %s", self.cfg.id, find.__name__, e)
        return reasons.UNKNOWN

    def _runout(self) -> dict | None:
        """A sensor that saw filament earlier in this print and doesn't anymore."""
        gone = [s for s in self.sensors if s in self._sensors_ok and self.raw.get(s, {}).get("filament_detected") is False]
        if not gone:
            return None
        active = reasons.tool_index(self.raw.get("toolhead", {}).get("extruder") or "extruder")
        sensor = min(gone, key=lambda s: reasons.sensor_tool(s) != active)  # the active tool's sensor first
        return reasons.runout(sensor, reasons.sensor_tool(sensor), len(self.tools) > 1)

    async def _tail(self, path: str, byte_range: str) -> bytes | None:
        r = await self.client.get(f"{self.cfg.moonraker}{path}", headers={**self.headers, "Range": f"bytes={byte_range}"})
        return r.content if r.status_code == 206 else None  # never download a whole gcode file or log

    async def _reason_from_gcode(self, since: float) -> dict | None:
        filename = self.raw.get("print_stats", {}).get("filename")
        position = self.raw.get("virtual_sdcard", {}).get("file_position") or 0
        if not filename or position <= 0:
            return None
        chunk = await self._tail(f"/server/files/gcodes/{quote(filename)}", f"{max(0, position - GCODE_TAIL)}-{position - 1}")
        return reasons.pause_in_gcode(chunk) if chunk else None

    async def _reason_from_console(self, since: float) -> dict | None:
        entries = (await self._api("/server/gcode_store", count=100)).get("gcode_store") or []
        return reasons.from_console(entries, since)

    async def _reason_from_log(self, since: float) -> dict | None:
        """Snapmaker's error reports also end up in moonraker.log, in case the poll missed one."""
        if "exception_manager" not in (self.objects or []):
            return None
        tail = await self._tail("/server/files/logs/moonraker.log", f"-{LOG_TAIL}")
        found = [e for e in reasons.exceptions_in_log((tail or b"").decode("utf-8", "replace")) if (e.get("timestamp") or 0) >= since]
        return reasons.from_exception(found[-1]) if found else None

    def _layer(self, ps: dict) -> tuple[int, bool] | None:
        """(layer, exact). Exact when the slicer reports layers, estimated from Z otherwise."""
        current = (ps.get("info") or {}).get("current_layer")
        if current:
            return int(current), True
        lh, flh = self.metadata.get("layer_height"), self.metadata.get("first_layer_height")
        pos = self.raw.get("gcode_move", {}).get("gcode_position")
        if lh and flh and pos:
            return max(1, round((pos[2] - flh) / lh) + 1), False
        return None

    async def _maybe_capture(self, ps: dict) -> None:
        # Only called once print_duration > 0: Klipper keeps it at 0 until the first extrusion,
        # which skips heating, homing and bed probing (where Z is all over the place).
        cfg = self.app.timelapse
        now = time.monotonic()
        if now - self._last_capture < cfg.min_frame_gap:
            return

        layer = self._layer(ps) if cfg.mode == "layer" else None
        if layer is None:
            if now - self._last_capture >= cfg.interval:
                await self._capture()
            return

        layer, exact = layer
        if not exact:
            total = self._total_layers(ps)
            if total and layer > total + 1:
                return  # Z far above the print: the end G-code moving the bed away, not a new layer
            if layer != self._pending_layer:
                self._pending_layer = layer  # must hold for a second poll, so z-hops don't count as layers
                return
        if layer > self._last_layer:
            self._last_layer = layer
            await self._capture()
            if (z := self._z()) is not None:
                self._print_top = max(self._print_top, z)
        elif layer < self._last_layer:
            self._last_layer = layer

    async def _capture(self) -> None:
        self._last_capture = time.monotonic()
        if frame := await self.snapshot():
            self.job.add_frame(frame)
        else:
            log.warning("%s: could not grab a camera frame", self.cfg.id)

    # -- dashboard ---------------------------------------------------------

    def _total_layers(self, ps: dict) -> int | None:
        total = (ps.get("info") or {}).get("total_layer") or self.metadata.get("layer_count")
        if total:
            return int(total)
        m = self.metadata
        if m.get("object_height") and m.get("layer_height") and m.get("first_layer_height"):
            return math.ceil((m["object_height"] - m["first_layer_height"]) / m["layer_height"]) + 1
        return None

    def status(self) -> dict:
        ps = self.raw.get("print_stats", {})
        state = ps.get("state") if self.online else "offline"
        active = state in ACTIVE
        progress = self.raw.get("virtual_sdcard", {}).get("progress") or 0
        duration = ps.get("print_duration") or 0

        eta = None
        if active:
            if progress > 0.05 and duration > 0:
                eta = duration / progress - duration
            elif self.metadata.get("estimated_time"):
                eta = max(0, self.metadata["estimated_time"] - duration)

        layer = self._layer(ps) if active and duration > 0 else None
        active_tool = self.raw.get("toolhead", {}).get("extruder") or "extruder"
        extruder, bed = self.raw.get(active_tool, {}), self.raw.get("heater_bed", {})
        multi_tool = len(self.tools) > 1
        return {
            "id": self.cfg.id,
            "name": self.cfg.name,
            "online": self.online,
            "error": self.error,
            "klippy": self.raw.get("webhooks", {}).get("state"),
            "state": state,
            "message": self.raw.get("display_status", {}).get("message"),
            "filename": ps.get("filename") or None,
            "progress": progress,
            "print_duration": duration,
            "eta": eta,
            "layer": layer[0] if layer else None,
            "total_layers": self._total_layers(ps) if active else None,
            "extruder": {
                "temp": extruder.get("temperature"),
                "target": extruder.get("target"),
                "tool": f"T{reasons.tool_index(active_tool)}" if multi_tool else None,
            },
            "tools": self._tools(active_tool, active) if multi_tool else None,
            "event": self.event,
            "bed": {"temp": bed.get("temperature"), "target": bed.get("target")} if bed else None,
            "camera": {"available": bool(self.stream_url or self.snapshot_url), **self.transform},
            "recording": self.job is not None,
            "frames": self.job.frames if self.job else 0,
        }

    def _tools(self, active_tool: str, printing: bool) -> list[dict]:
        """Each toolhead with its temperature and filament: what's loaded (when the printer says,
        like the U1) or else what the print was sliced for."""
        loaded = self.raw.get("print_task_config") or {}
        meta = self.metadata if printing else {}
        sliced_types, sliced_colors = _split(meta.get("filament_type")), _split(meta.get("filament_colour"))
        used = meta.get("filament_used_mm") if isinstance(meta.get("filament_used_mm"), list) else None
        tools = []
        for name in self.tools:
            i = reasons.tool_index(name)
            heater = self.raw.get(name, {})
            loaded_type, sliced_type = _material(_at(loaded.get("filament_type"), i)), _material(_at(sliced_types, i))
            empty = _at(loaded.get("filament_exist"), i) is False
            in_use = bool(_at(used, i)) if used else None
            color = _color(_at(loaded.get("filament_color_rgba"), i)) if loaded_type else _color(_at(sliced_colors, i))
            mismatch = in_use and loaded_type and sliced_type and loaded_type.upper() != sliced_type.upper()
            tools.append({
                "name": f"T{i}",
                "temp": heater.get("temperature"),
                "target": heater.get("target"),
                "active": name == active_tool,
                "material": loaded_type or (None if empty else sliced_type),
                "color": None if empty else color,
                "empty": empty,
                "used": in_use,
                "mismatch": f"{loaded_type} loaded, the print expects {sliced_type}" if mismatch else None,
            })
        return tools


HEX_COLOR = re.compile(r"#?([0-9A-Fa-f]{6})")


def _at(values, i: int):
    return values[i] if isinstance(values, list) and 0 <= i < len(values) else None


def _split(value) -> list:
    """Slicer metadata lists per tool: "PLA;PETG" or ["PLA", "PETG"]."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(";")]
    return value if isinstance(value, list) else []


def _material(value) -> str | None:
    value = str(value or "").strip()
    return value if value and value.upper() not in ("NONE", "-") else None


def _color(value) -> str | None:
    match = HEX_COLOR.match(str(value or "").strip())
    return f"#{match.group(1).upper()}" if match else None
