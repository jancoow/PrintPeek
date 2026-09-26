"""Timelapse jobs on disk: one directory per print holding frames, meta.json and the rendered video.

    <data>/timelapses/<printer>/<YYYYmmdd-HHMMSS>_<gcode name>/
        frames/000001.jpg ...   (removed after rendering unless keep_frames)
        meta.json
        timelapse.mp4
        thumb.jpg
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import PrinterConfig, TimelapseConfig

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
VIDEO = "timelapse.mp4"
THUMB = "thumb.jpg"

_render_lock = asyncio.Lock()  # one ffmpeg at a time; the server has other things to do
_tasks: set[asyncio.Task] = set()


def timelapse_root(data_dir: Path) -> Path:
    return data_dir / "timelapses"


def read_meta(job_dir: Path) -> dict | None:
    try:
        return json.loads((job_dir / "meta.json").read_text())
    except (OSError, ValueError):
        return None


def write_meta(job_dir: Path, meta: dict) -> None:
    tmp = job_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(job_dir / "meta.json")


def update_meta(job_dir: Path, **fields) -> dict | None:
    """Re-read before writing, so fields set elsewhere in the meantime (like a share token) survive."""
    meta = read_meta(job_dir)
    if meta is None:
        return None
    meta.update(fields)
    write_meta(job_dir, meta)
    return meta


class Job:
    """A print that is currently being recorded."""

    def __init__(self, job_dir: Path, meta: dict):
        self.dir = job_dir
        self.meta = meta
        self.frames_dir = job_dir / "frames"
        self.frames = len(list(self.frames_dir.glob("*.jpg"))) if self.frames_dir.is_dir() else 0

    @property
    def filename(self) -> str:
        return self.meta["filename"]

    @classmethod
    def create(cls, data_dir: Path, printer: PrinterConfig, filename: str, transform: dict) -> Job:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("._")[:60] or "print"
        job_dir = timelapse_root(data_dir) / printer.id / f"{datetime.now():%Y%m%d-%H%M%S}_{stem}"
        (job_dir / "frames").mkdir(parents=True, exist_ok=True)
        meta = {
            "printer": printer.id,
            "printer_name": printer.name,
            "filename": filename,
            "started": time.time(),
            "ended": None,
            "result": None,
            "status": "recording",
            "frames": 0,
            "transform": transform,
        }
        write_meta(job_dir, meta)
        return cls(job_dir, meta)

    @classmethod
    def resume(cls, state_file: Path) -> Job | None:
        """Pick up the job that was recording when the service last stopped."""
        try:
            job_dir = Path(json.loads(state_file.read_text())["dir"])
        except (OSError, ValueError, KeyError):
            return None
        meta = read_meta(job_dir)
        if not meta or meta.get("status") != "recording":
            state_file.unlink(missing_ok=True)
            return None
        return cls(job_dir, meta)

    def remember(self, state_file: Path) -> None:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"dir": str(self.dir)}))

    def add_frame(self, jpeg: bytes) -> None:
        self.frames += 1
        (self.frames_dir / f"{self.frames:06d}.jpg").write_bytes(jpeg)
        self.meta["frames"] = self.frames
        write_meta(self.dir, self.meta)

    def finish(self, result: str, print_stats: dict) -> None:
        self.meta.update(ended=time.time(), result=result, status="queued", frames=self.frames)
        for key in ("print_duration", "filament_used"):
            if print_stats.get(key):
                self.meta[key] = print_stats[key]
        write_meta(self.dir, self.meta)


def transform_filters(transform: dict) -> list[str]:
    """ffmpeg filters matching the webcam orientation configured in Mainsail/Fluidd."""
    filters = []
    if transform.get("flip_horizontal"):
        filters.append("hflip")
    if transform.get("flip_vertical"):
        filters.append("vflip")
    rotation = int(transform.get("rotation") or 0) % 360
    if rotation == 90:
        filters.append("transpose=clock")
    elif rotation == 180:
        filters += ["hflip", "vflip"]
    elif rotation == 270:
        filters.append("transpose=cclock")
    return filters


async def _ffmpeg(*args: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return proc.returncode == 0, err.decode(errors="replace")[-1000:]


async def render(job_dir: Path, cfg: TimelapseConfig) -> None:
    async with _render_lock:
        frames_dir = job_dir / "frames"
        frames = sorted(frames_dir.glob("*.jpg")) if frames_dir.is_dir() else []
        if len(frames) < 2:
            update_meta(job_dir, status="empty")
            return

        meta = update_meta(job_dir, status="rendering")
        if meta is None:
            return
        log.info("rendering %s (%d frames)", job_dir.name, len(frames))

        transform = transform_filters(meta.get("transform") or {})
        video_filters = transform + [
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            f"tpad=stop_mode=clone:stop_duration={cfg.hold_last}",
            "format=yuv420p",
        ]
        tmp = job_dir / "timelapse.tmp.mp4"
        ok, err = await _ffmpeg(
            "-framerate", str(cfg.fps), "-pattern_type", "glob", "-i", str(frames_dir / "*.jpg"),
            "-vf", ",".join(video_filters),
            "-c:v", "libx264", "-preset", "medium", "-crf", str(cfg.crf),
            "-movflags", "+faststart", "-f", "mp4", str(tmp),
        )
        if not ok:
            log.error("render of %s failed: %s", job_dir.name, err)
            tmp.unlink(missing_ok=True)
            update_meta(job_dir, status="failed", error=err)
            return
        tmp.replace(job_dir / VIDEO)

        await _ffmpeg("-i", str(frames[-1]), "-vf", ",".join(transform + ["scale=480:-2"]), str(job_dir / THUMB))

        update_meta(job_dir, status="done", error=None, fps=cfg.fps, video_duration=len(frames) / cfg.fps + cfg.hold_last)
        if not cfg.keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
        log.info("rendered %s", job_dir.name)


def schedule_render(job_dir: Path, cfg: TimelapseConfig) -> None:
    task = asyncio.create_task(render(job_dir, cfg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def resume_pending_renders(data_dir: Path, cfg: TimelapseConfig) -> None:
    """Renders that were queued or running when the service stopped."""
    for meta_file in timelapse_root(data_dir).glob("*/*/meta.json"):
        meta = read_meta(meta_file.parent)
        if meta and meta.get("status") in ("queued", "rendering"):
            schedule_render(meta_file.parent, cfg)


def job_dir(data_dir: Path, printer: str, name: str) -> Path | None:
    if not (NAME_RE.match(printer) and NAME_RE.match(name)):
        return None
    path = timelapse_root(data_dir) / printer / name
    return path if (path / "meta.json").is_file() else None


def set_shared(job_dir: Path, shared: bool) -> str | None:
    """Create (or keep) the random share token for a timelapse, or revoke it."""
    meta = read_meta(job_dir) or {}
    token = (meta.get("share_token") or secrets.token_urlsafe(18)) if shared else None
    update_meta(job_dir, share_token=token)
    return token


def find_shared(data_dir: Path, token: str) -> tuple[Path, dict] | None:
    if not TOKEN_RE.match(token):
        return None
    for meta_file in timelapse_root(data_dir).glob("*/*/meta.json"):
        meta = read_meta(meta_file.parent)
        if meta and meta.get("share_token") and secrets.compare_digest(meta["share_token"], token):
            return meta_file.parent, meta
    return None


def list_jobs(data_dir: Path) -> list[dict]:
    jobs = []
    for meta_file in timelapse_root(data_dir).glob("*/*/meta.json"):
        d = meta_file.parent
        meta = read_meta(d)
        if meta is None:
            continue
        rel = f"{d.parent.name}/{d.name}"
        video = d / VIDEO
        meta.pop("transform", None)
        meta.update(
            id=rel,
            video_url=f"/media/{rel}/{VIDEO}" if video.exists() else None,
            thumb_url=f"/media/{rel}/{THUMB}" if (d / THUMB).exists() else None,
            size=video.stat().st_size if video.exists() else None,
            has_frames=(d / "frames").is_dir() and any((d / "frames").iterdir()),
        )
        jobs.append(meta)
    jobs.sort(key=lambda m: m.get("started") or 0, reverse=True)
    return jobs
