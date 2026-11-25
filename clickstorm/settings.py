"""Persistent settings for ClickStorm.

Settings live in ``%APPDATA%\\ClickStorm\\settings.json`` so the packaged
executable can remember preferences without writing next to the .exe.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import APP_DATA_DIR
from .engine import MAX_CLICKS, MAX_CPS, MAX_DURATION, MIN_CPS
from .winapi import BUTTONS, HOTKEY_KEYS

SETTINGS_VERSION = 2
_GEOMETRY_RE = re.compile(r"^\d+x\d+([+-]\d+[+-]\d+)?$")

#: Why the last :meth:`Settings.save` call failed, for the UI to show. Empty
#: after a successful write.
_LAST_SAVE_ERROR = ""


def last_save_error() -> str:
    return _LAST_SAVE_ERROR


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home() / "AppData" / "Roaming"
    return root / APP_DATA_DIR / "settings.json"


@dataclass
class Settings:
    cps: float = 500.0
    mouse_button: str = "Left"
    limit_mode: str = "unlimited"
    click_limit: int = 5_000
    duration: float = 10.0

    start_delay: float = 2.0
    jitter_percent: float = 0.0
    delivery: str = "sendinput"
    pin_window: bool = False
    position_mode: str = "cursor"
    fixed_x: int = 0
    fixed_y: int = 0

    hotkey_start: str = "F5"
    hotkey_stop: str = "F9"
    hotkeys_enabled: bool = True

    always_on_top: bool = False
    minimize_on_start: bool = False
    performance_mode: bool = True

    total_clicks: int = 0
    window_geometry: str = "1180x800"
    settings_version: int = SETTINGS_VERSION

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "Settings":
        path = settings_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return cls()

        if not isinstance(raw, dict):
            return cls()

        defaults = cls()
        values = asdict(defaults)
        out = cls()

        def number(key: str, minimum: float, maximum: float) -> float:
            try:
                value = float(raw.get(key, values[key]))
            except (TypeError, ValueError):
                value = float(values[key])
            return max(minimum, min(maximum, value))

        def integer(key: str, minimum: int, maximum: int) -> int:
            try:
                value = int(float(raw.get(key, values[key])))
            except (TypeError, ValueError):
                value = int(values[key])
            return max(minimum, min(maximum, value))

        def boolean(key: str) -> bool:
            value = raw.get(key, values[key])
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        def choice(key: str, allowed, fallback):
            value = raw.get(key, values[key])
            return value if value in allowed else fallback

        out.cps = number("cps", MIN_CPS, MAX_CPS)
        out.mouse_button = choice("mouse_button", BUTTONS.keys(), "Left")
        out.limit_mode = choice(
            "limit_mode", ("unlimited", "clicks", "duration"), "unlimited"
        )
        out.click_limit = integer("click_limit", 1, MAX_CLICKS)
        out.duration = number("duration", 0.05, MAX_DURATION)

        out.start_delay = number("start_delay", 0.0, 10.0)
        out.jitter_percent = number("jitter_percent", 0.0, 0.75)
        out.delivery = choice(
            "delivery", ("sendinput", "direct", "auto"), "sendinput"
        )
        out.pin_window = boolean("pin_window")
        out.position_mode = choice(
            "position_mode", ("cursor", "fixed"), "cursor"
        )
        out.fixed_x = integer("fixed_x", -100_000, 100_000)
        out.fixed_y = integer("fixed_y", -100_000, 100_000)

        out.hotkey_start = choice(
            "hotkey_start", HOTKEY_KEYS.keys(), defaults.hotkey_start
        )
        out.hotkey_stop = choice(
            "hotkey_stop", HOTKEY_KEYS.keys(), defaults.hotkey_stop
        )
        out.hotkeys_enabled = boolean("hotkeys_enabled")

        out.always_on_top = boolean("always_on_top")
        out.minimize_on_start = boolean("minimize_on_start")
        out.performance_mode = boolean("performance_mode")

        out.total_clicks = integer("total_clicks", 0, 9_000_000_000_000)

        geometry = raw.get("window_geometry", values["window_geometry"])
        out.window_geometry = (
            geometry
            if isinstance(geometry, str) and _GEOMETRY_RE.match(geometry)
            else defaults.window_geometry
        )

        try:
            raw_version = int(raw.get("settings_version", 1))
        except (TypeError, ValueError):
            raw_version = 1
        if raw_version < 2:
            # Version 2 defaults: F5 starts, F9 stops.
            out.hotkey_start = "F5"
            out.hotkey_stop = "F9"

        if out.hotkey_start == out.hotkey_stop:
            out.hotkey_stop = defaults.hotkey_stop
            if out.hotkey_stop == out.hotkey_start:
                out.hotkey_stop = "F9" if out.hotkey_start != "F9" else "F10"

        out.settings_version = SETTINGS_VERSION
        return out

    def save(self) -> bool:
        """Write the settings atomically. Returns False and records why.

        The write goes to a temporary file in the same directory, is flushed
        to disk and then renamed over the real file, so a crash mid-write can
        never leave a truncated settings.json behind. Callers that care about
        failures (the settings dialog) use :func:`last_save_error`.
        """
        global _LAST_SAVE_ERROR
        _LAST_SAVE_ERROR = ""
        path = settings_path()
        temp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                asdict(self), indent=2, ensure_ascii=False
            )
            fd, temp_name = tempfile.mkstemp(
                prefix="settings-", suffix=".json", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
                temp_name = None
            finally:
                if temp_name is not None:
                    try:
                        os.unlink(temp_name)
                    except OSError:
                        pass
            return True
        except Exception as exc:
            _LAST_SAVE_ERROR = f"{type(exc).__name__}: {exc}"
            return False
