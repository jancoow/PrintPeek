"""Loads config.yaml: the printers to watch and how timelapses are made."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass
class CameraConfig:
    # Everything here is optional: by default the camera is discovered through
    # Moonraker's webcam list (the same one Mainsail/Fluidd use).
    name: str | None = None  # pick a specific webcam by its Moonraker name
    stream: str | None = None
    snapshot: str | None = None
    rotation: int | None = None
    flip_horizontal: bool | None = None
    flip_vertical: bool | None = None
    h264: bool = True  # offer the H.264 live view (needs go2rtc)


@dataclass
class PrinterConfig:
    id: str
    name: str
    moonraker: str
    api_key: str | None = None
    public: bool = True  # guests may watch the live view (starting value; toggled in the dashboard)
    # Last frame of the timelapse. "after_end": after the end G-code (parked head, finished print).
    # "before_end": the last layer, for printers whose end G-code moves the bed out of view.
    # "auto": after_end, unless the end G-code moved Z far away from the print.
    final_frame: str = "auto"
    camera: CameraConfig = field(default_factory=CameraConfig)


@dataclass
class TimelapseConfig:
    mode: str = "layer"  # "layer": one frame per layer, "interval": one frame every `interval` seconds
    interval: float = 10.0
    min_frame_gap: float = 2.0  # never capture more often than this (tiny layers, vase mode)
    fps: int = 30
    hold_last: float = 2.0  # seconds the finished print stays on screen at the end
    crf: int = 23
    keep_frames: bool = False


@dataclass
class AuthConfig:
    username: str
    password: str


@dataclass
class Config:
    printers: list[PrinterConfig]
    timelapse: TimelapseConfig
    go2rtc: str | None
    data_dir: Path
    poll_interval: float
    max_viewers: int  # simultaneous live streams per printer
    auth: AuthConfig | None


def load_config(path: str | os.PathLike) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}

    printers = []
    for entry in raw.get("printers") or []:
        entry = dict(entry)
        camera = CameraConfig(**(entry.pop("camera", None) or {}))
        printer = PrinterConfig(camera=camera, **entry)
        if not ID_RE.match(printer.id):
            raise ValueError(f"printer id {printer.id!r} must be lowercase letters, digits, '-' or '_'")
        printer.moonraker = printer.moonraker.rstrip("/")
        if printer.final_frame not in ("auto", "after_end", "before_end"):
            raise ValueError(f"printer {printer.id}: final_frame must be auto, after_end or before_end")
        printers.append(printer)
    if not printers:
        raise ValueError("config: no printers configured")
    if len({p.id for p in printers}) != len(printers):
        raise ValueError("config: printer ids must be unique")

    timelapse = TimelapseConfig(**(raw.get("timelapse") or {}))
    if timelapse.mode not in ("layer", "interval"):
        raise ValueError("config: timelapse.mode must be 'layer' or 'interval'")

    auth = raw.get("auth")
    h264_options = str(raw.get("h264_options", "#video=h264live#width=1280")).strip()
    if any(c.isspace() for c in h264_options):
        raise ValueError("config: h264_options can't contain spaces (go2rtc refuses them); define an ffmpeg preset in go2rtc.yaml")
    return Config(
        printers=printers,
        timelapse=timelapse,
        go2rtc=(raw.get("go2rtc") or "").rstrip("/") or None,
        data_dir=Path(os.environ.get("DATA_DIR") or raw.get("data_dir") or "/data"),
        poll_interval=float(raw.get("poll_interval", 1.0)),
        max_viewers=int(raw.get("max_viewers", 10)),
        auth=AuthConfig(**auth) if auth else None,
    )
