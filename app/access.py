"""Whether guests may watch a printer's live view: on, off, on for a few hours, or until the print ends.

Switches are changed from the admin dashboard and persisted in <data>/public.json, so they survive
restarts. The config's `public:` per printer is only the starting value.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MODES = ("on", "off", "until", "print")


class PublicSwitch:
    def __init__(self, store: PublicStore, mode: str, until: float | None = None, armed: bool = False):
        self.store = store
        self.mode = mode
        self.until = until
        self.armed = armed  # mode "print": a print has been seen since it was switched on

    @property
    def live(self) -> bool:
        if self.mode == "until":
            return time.time() < (self.until or 0)
        return self.mode in ("on", "print")

    def set(self, mode: str, hours: float | None = None) -> None:
        self.mode = mode
        self.until = time.time() + hours * 3600 if mode == "until" else None
        self.armed = False
        self.store.save()

    def observe(self, printing: bool) -> None:
        """Called on every printer poll, to switch off expired timers and finished prints.

        "Until the print ends" switched on while idle covers the next print.
        """
        if self.mode == "until" and not self.live:
            self.set("off")
        elif self.mode == "print":
            if printing and not self.armed:
                self.armed = True
                self.store.save()
            elif self.armed and not printing:
                self.set("off")

    def describe(self) -> dict:
        return {"mode": self.mode, "until": self.until, "live": self.live}

    def to_json(self) -> dict:
        return {"mode": self.mode, "until": self.until, "armed": self.armed}


class PublicStore:
    def __init__(self, path: Path, defaults: dict[str, bool]):
        self.path = path
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        self.switches: dict[str, PublicSwitch] = {}
        for printer_id, default in defaults.items():
            s = saved.get(printer_id)
            if not isinstance(s, dict) or s.get("mode") not in MODES:
                s = {"mode": "on" if default else "off"}
            self.switches[printer_id] = PublicSwitch(self, s["mode"], s.get("until"), bool(s.get("armed")))

    def __getitem__(self, printer_id: str) -> PublicSwitch:
        return self.switches[printer_id]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({pid: s.to_json() for pid, s in self.switches.items()}, indent=2))
        tmp.replace(self.path)
