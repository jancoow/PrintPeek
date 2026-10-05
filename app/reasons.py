"""Why a print paused or stopped, pieced together from what Moonraker exposes.

Klipper doesn't record why a print paused, so PrinterMonitor looks in a few places, most specific
first: the printer's own error report (Snapmaker's exception_manager, e.g. its spaghetti
detection), a filament sensor that ran out during the print, a pause command in the gcode file
right before the pause point, and the console. This module only interprets what was found.
"""
from __future__ import annotations

import ast
import re

PAUSE_COMMANDS = {
    "PAUSE": "a PAUSE command",
    "M600": "a filament change (M600)",
    "M601": "a pause command (M601)",
    "M0": "a stop command (M0)",
    "M1": "a stop command (M1)",
    "M25": "a pause command (M25)",
}

# exception_manager messages, in plain words. Others are shown as the printer reports them.
EXCEPTIONS = {
    "detected noodle": "Possible spaghetti, detected by the printer's camera",
    "detected dirty bed": "Something on the bed, detected by the printer's camera",
}

LOG_EXCEPTION = re.compile(r"klippy raise exception: (\{[^\n]{0,2000}\})")
TOOL_NAME = re.compile(r"^(?:e|extruder|t|tool)(\d+)(?:_|$)", re.I)
CONSOLE_HINT = re.compile(r"runout|run out|clog|tangl|jam", re.I)


def reason(text: str, source: str, detail: str | None = None) -> dict:
    return {"text": text, "source": source, "detail": detail}


UNKNOWN = reason("No reason reported. Probably paused from the screen, Mainsail or an app.", "unknown")


def tool_index(name: str) -> int:
    """'extruder' -> 0, 'extruder2' -> 2."""
    digits = name.removeprefix("extruder")
    return int(digits) if digits.isdigit() else 0


def sensor_tool(sensor: str) -> int | None:
    """The tool a filament sensor belongs to, when its name says so ('... e1_filament' -> 1)."""
    match = TOOL_NAME.match(sensor.split(" ", 1)[-1])
    return int(match.group(1)) if match else None


def from_exception(exc: dict) -> dict:
    message = str(exc.get("message") or "").strip()
    code = "-".join(str(exc.get(k, "?")) for k in ("id", "index", "code"))
    text = EXCEPTIONS.get(message.lower()) or f"Printer error: {message or 'no message'}"
    return reason(text, "printer", f"error {code}: {message}")


def exceptions_in_log(text: str) -> list[dict]:
    """exception_manager entries from moonraker.log: "klippy raise exception: {'id': 532, ...}"."""
    found = []
    for match in LOG_EXCEPTION.finditer(text):
        try:
            entry = ast.literal_eval(match.group(1))
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def runout(sensor: str, tool: int | None, multi_tool: bool) -> dict:
    where = f" on T{tool}" if multi_tool and tool is not None else ""
    return reason(f"Filament ran out or stopped moving{where}", "sensor", sensor.split(" ", 1)[-1])


def _command(line: str) -> str:
    return line.split(";", 1)[0].strip().split(" ", 1)[0].upper()


def pause_in_gcode(chunk: bytes) -> dict | None:
    """chunk: the gcode file up to the pause point. A pause command among the last few commands
    means the file itself paused (a filament change, or a pause added in the slicer)."""
    lines = chunk.decode("utf-8", "replace").splitlines()[1:]  # the first line is cut off
    commands = [c for c in map(_command, lines) if c][-3:]
    for command in reversed(commands):
        if command in PAUSE_COMMANDS:
            return reason(f"The G-code paused the print with {PAUSE_COMMANDS[command]}", "gcode")
    return None


def from_console(entries: list[dict], since: float) -> dict | None:
    """Moonraker's gcode_store: commands typed in a console, and Klipper's responses."""
    for entry in reversed(entries):
        if (entry.get("time") or 0) < since:
            break
        message = str(entry.get("message") or "").strip()
        if entry.get("type") == "command":
            command = _command(message)
            if command in PAUSE_COMMANDS:
                return reason(f"Paused from the console ({command})", "console")
        elif message.startswith("!!"):
            return reason(message.lstrip("! ").strip(), "console")
        elif CONSOLE_HINT.search(message):
            return reason(message.lstrip("/ ").strip(), "console")
    return None
