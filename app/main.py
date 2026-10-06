"""Web app: live view for everyone, dashboard + timelapse library for the admin.

Guests see the printers whose live view is public (switchable per printer in the dashboard).
Timelapses are admin-only, except the ones shared explicitly by an unguessable link.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Literal

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import timelapse
from .access import PublicStore
from .config import load_config
from .printer import PrinterMonitor

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

CONFIG = load_config(os.environ.get("CONFIG", "/config/config.yaml"))
STATIC = Path(__file__).parent / "static"
MEDIA = timelapse.timelapse_root(CONFIG.data_dir)
MEDIA.mkdir(parents=True, exist_ok=True)

SNAPSHOT_MAX_AGE = 2.0
SESSION_COOKIE = "session"
SESSION_SECONDS = 30 * 24 * 3600
# Guests never get past these. Everything else decides per request (live views are public
# unless switched off for that printer).
ADMIN_PREFIXES = ("/media/", "/api/timelapses", "/api/admin/")

public = PublicStore(CONFIG.data_dir / "public.json", {p.id: p.public for p in CONFIG.printers})
monitors: dict[str, PrinterMonitor] = {}


# -- sessions ---------------------------------------------------------------

def _session_key() -> bytes:
    path = CONFIG.data_dir / "session.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    # Mixing in the password logs every session out when the password changes.
    return hashlib.sha256(path.read_bytes() + CONFIG.auth.password.encode()).digest()


SESSION_KEY = _session_key() if CONFIG.auth else b""


def _sign(expires: str) -> str:
    return hmac.new(SESSION_KEY, expires.encode(), "sha256").hexdigest()


def _valid_session(value: str) -> bool:
    expires, _, signature = value.partition(".")
    return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(signature, _sign(expires))


class AdminSession:
    """Marks every request as admin or guest and keeps guests out of ADMIN_PREFIXES.

    Plain ASGI middleware: BaseHTTPMiddleware doesn't play well with endless MJPEG streams.
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _has_session(scope) -> bool:
        header = dict(scope["headers"]).get(b"cookie", b"").decode("latin-1")
        try:
            morsel = SimpleCookie(header).get(SESSION_COOKIE)
        except CookieError:
            return False
        return morsel is not None and _valid_session(morsel.value)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        admin = CONFIG.auth is None or self._has_session(scope)
        scope.setdefault("state", {})["admin"] = admin
        if not admin and scope["path"].startswith(ADMIN_PREFIXES):
            return await JSONResponse({"detail": "login required"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


def is_admin(request: Request) -> bool:
    return request.state.admin


# -- app --------------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    client = httpx.AsyncClient(timeout=10)
    _app.state.stream_client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=30))
    for printer in CONFIG.printers:
        monitors[printer.id] = PrinterMonitor(printer, CONFIG, client, public[printer.id])
    tasks = [asyncio.create_task(m.run()) for m in monitors.values()]
    timelapse.resume_pending_renders(CONFIG.data_dir, CONFIG.timelapse)
    if CONFIG.auth is None:
        log.warning("no auth configured: everyone is admin (dashboard, timelapses, delete)")
    yield
    for task in tasks:
        task.cancel()
    await client.aclose()
    await _app.state.stream_client.aclose()


app = FastAPI(title="3D print", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(AdminSession)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/media", StaticFiles(directory=MEDIA), name="media")


# -- pages ------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if is_admin(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html")


@app.get("/watch/{printer_id}", include_in_schema=False)
async def watch(printer_id: str):
    if printer_id not in monitors:
        raise HTTPException(404, "unknown printer")
    return FileResponse(STATIC / "watch.html")  # the page itself says when it isn't live


# The app can be installed on a phone's home screen (PWA). The service worker has to be served
# from the root to cover the whole site.
@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/share/{token}", include_in_schema=False)
async def share_page(token: str):
    _shared(token)
    return FileResponse(STATIC / "share.html")


# -- login ------------------------------------------------------------------

class Credentials(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: Credentials, request: Request):
    if CONFIG.auth is None:
        return {"ok": True}
    ok = secrets.compare_digest(body.username.encode(), CONFIG.auth.username.encode()) & secrets.compare_digest(
        body.password.encode(), CONFIG.auth.password.encode())
    if not ok:
        await asyncio.sleep(1)  # a wrong password costs a second: slows down guessing
        raise HTTPException(401, "Wrong username or password")
    expires = str(int(time.time()) + SESSION_SECONDS)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE, f"{expires}.{_sign(expires)}", max_age=SESSION_SECONDS,
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


# -- printers ---------------------------------------------------------------

def _status(monitor: PrinterMonitor, admin: bool) -> dict:
    s = monitor.status()
    s["public"] = monitor.public.describe()
    if not admin:
        for key in ("error", "recording", "frames", "event"):
            s.pop(key, None)
    return s


@app.get("/api/status")
async def status(request: Request):
    admin = is_admin(request)
    return {
        "admin": admin,
        "login_enabled": CONFIG.auth is not None,
        "printers": [_status(m, admin) for m in monitors.values() if admin or m.public.live],
    }


def _viewable(printer_id: str, request: Request) -> PrinterMonitor:
    monitor = monitors.get(printer_id)
    if monitor is None or not (is_admin(request) or monitor.public.live):
        raise HTTPException(404, "this printer isn't live right now")
    return monitor


@app.get("/api/printers/{printer_id}/status")
async def printer_status(printer_id: str, request: Request):
    return _status(_viewable(printer_id, request), is_admin(request))


@app.get("/api/printers/{printer_id}/snapshot.jpg")
async def snapshot(printer_id: str, request: Request):
    frame = await _viewable(printer_id, request).snapshot(max_age=SNAPSHOT_MAX_AGE)
    if frame is None:
        raise HTTPException(502, "camera unavailable")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/printers/{printer_id}/thumbnail.png")
async def thumbnail(printer_id: str, request: Request):
    image = await _viewable(printer_id, request).thumbnail()
    if image is None:
        raise HTTPException(404, "no preview")
    return Response(image, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/printers/{printer_id}/stream.mjpeg")
async def stream(printer_id: str, request: Request):
    """MJPEG re-stream. go2rtc keeps a single connection to the printer however many people watch."""
    admin = is_admin(request)
    monitor = _viewable(printer_id, request)
    url = monitor.live_stream_url()
    if not url:
        raise HTTPException(404, "no camera")
    if not admin and monitor.viewers >= CONFIG.max_viewers:
        raise HTTPException(503, "too many viewers, try again later")
    client: httpx.AsyncClient = app.state.stream_client
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError:
        raise HTTPException(502, "camera unavailable")
    if upstream.status_code != 200:
        await upstream.aclose()
        raise HTTPException(502, "camera unavailable")

    async def body():
        monitor.viewers += 1
        try:
            async for chunk in upstream.aiter_raw():
                if not admin and not monitor.public.live:
                    break  # switched to private (or the timer ran out): guests are cut off
                yield chunk
        except httpx.HTTPError:
            pass
        finally:
            monitor.viewers -= 1
            await upstream.aclose()

    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/admin/printers/{printer_id}/event.jpg")
async def event_photo(printer_id: str):
    """The camera frame from when the print paused or failed."""
    monitor = monitors.get(printer_id)
    if monitor is None or monitor.event_photo is None:
        raise HTTPException(404, "no photo")
    return Response(monitor.event_photo, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/api/printers/{printer_id}/live")
async def live_h264(ws: WebSocket, printer_id: str):
    """H.264 live view: relays go2rtc's fragmented MP4 stream for the browser's MediaSource.

    go2rtc's own API stays unreachable: the only thing passed on to it is the browser's list of
    codecs, and nothing the browser sends after that.
    """
    admin = ws.scope["state"]["admin"]
    monitor = monitors.get(printer_id)
    url = monitor.h264_url() if monitor and (admin or monitor.public.live) else None
    if url is None or (not admin and monitor.viewers >= CONFIG.max_viewers):
        return await ws.close(code=4404 if url is None else 4503)
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 10)
    except Exception:
        return await ws.close(code=4400)
    codecs = hello.get("value") if isinstance(hello, dict) and hello.get("type") == "mse" else None
    if not isinstance(codecs, str) or len(codecs) > 300:
        return await ws.close(code=4400)

    async def relay(upstream):
        async for message in upstream:
            if not admin and not monitor.public.live:
                return  # switched to private (or the timer ran out): guests are cut off
            if isinstance(message, bytes):
                await ws.send_bytes(message)
            else:
                await ws.send_text(message)

    async def until_closed():
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass  # the browser has nothing more to say

    monitor.viewers += 1
    try:
        async with websockets.connect(url, max_size=None, open_timeout=10) as upstream:
            await upstream.send(json.dumps({"type": "mse", "value": codecs}))
            tasks = [asyncio.create_task(relay(upstream)), asyncio.create_task(until_closed())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
        pass
    finally:
        monitor.viewers -= 1
        with contextlib.suppress(Exception):
            await ws.close()


class PublicUpdate(BaseModel):
    mode: Literal["on", "off", "until", "print"]
    hours: float | None = Field(default=None, gt=0, le=24 * 7)


@app.post("/api/admin/printers/{printer_id}/public")
async def set_public(printer_id: str, body: PublicUpdate):
    if printer_id not in monitors:
        raise HTTPException(404, "unknown printer")
    if body.mode == "until" and not body.hours:
        raise HTTPException(422, "hours required")
    switch = monitors[printer_id].public
    switch.set(body.mode, body.hours)
    log.info("%s: live view %s", printer_id, switch.describe())
    return switch.describe()


# -- timelapses (admin) -----------------------------------------------------

@app.get("/api/timelapses")
async def timelapses():
    return timelapse.list_jobs(CONFIG.data_dir)


def _job_dir(printer: str, name: str) -> Path:
    path = timelapse.job_dir(CONFIG.data_dir, printer, name)
    if path is None:
        raise HTTPException(404, "unknown timelapse")
    return path


def _is_recording(path: Path) -> bool:
    return any(m.job and m.job.dir == path for m in monitors.values())


@app.post("/api/timelapses/{printer}/{name}/render")
async def rerender(printer: str, name: str):
    path = _job_dir(printer, name)
    if _is_recording(path):
        raise HTTPException(409, "still recording")
    if not (path / "frames").is_dir():
        raise HTTPException(409, "frames were not kept (timelapse.keep_frames)")
    timelapse.schedule_render(path, CONFIG.timelapse)
    return {"ok": True}


@app.post("/api/timelapses/{printer}/{name}/share")
async def share(printer: str, name: str):
    path = _job_dir(printer, name)
    if (timelapse.read_meta(path) or {}).get("status") != "done":
        raise HTTPException(409, "only finished timelapses can be shared")
    return {"token": timelapse.set_shared(path, True)}


@app.delete("/api/timelapses/{printer}/{name}/share")
async def unshare(printer: str, name: str):
    timelapse.set_shared(_job_dir(printer, name), False)
    return {"ok": True}


@app.delete("/api/timelapses/{printer}/{name}")
async def delete(printer: str, name: str):
    path = _job_dir(printer, name)
    if _is_recording(path):
        raise HTTPException(409, "still recording")
    shutil.rmtree(path)
    return {"ok": True}


# -- shared timelapses (public by token) --------------------------------------

def _shared(token: str) -> tuple[Path, dict]:
    found = timelapse.find_shared(CONFIG.data_dir, token)
    if found is None:
        raise HTTPException(404, "this link doesn't exist or is no longer shared")
    return found


@app.get("/api/shares/{token}")
async def share_info(token: str):
    _, meta = _shared(token)
    return {k: meta.get(k) for k in ("printer_name", "filename", "started", "result", "print_duration", "video_duration")}


@app.get("/api/shares/{token}/{file}")
async def share_file(token: str, file: str):
    if file not in ("video.mp4", "thumb.jpg"):
        raise HTTPException(404)
    path, _ = _shared(token)
    target = path / (timelapse.VIDEO if file == "video.mp4" else timelapse.THUMB)
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)
