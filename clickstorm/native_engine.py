"""ctypes binding for ClickStorm's native Rust click engine.

The Rust core (``native/clickstorm-engine``) owns the click thread: pacing,
batching, SendInput and posted-message delivery. Python only configures a
session and reads counters, so the hot path has no interpreter, no allocator
churn and no garbage collection involved.

If the DLL is missing, built for 32-bit Windows, or speaks a different ABI,
``create_native_engine()`` returns ``None`` and the app falls back to the pure
Python engine. The UI never breaks because of the native core.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from pathlib import Path

from .engine import (
    EngineConfig,
    EngineEvent,
    EngineStats,
    MAX_CLICKS,
    MAX_CPS,
    MAX_DURATION,
    MIN_CPS,
)

ABI_VERSION = 1
CONFIG_SIZE = 80
STATS_SIZE = 112
_STATS_STATE = ("idle", "running", "stopped", "error")
_REASON = ("", "stopped", "target", "duration", "error")

BUTTON_CODES = {"Left": 0, "Right": 1, "Middle": 2, "X1": 3, "X2": 4}
DELIVERY_CODES = {"sendinput": 0, "direct": 1, "auto": 2, "sync": 3}
LIMIT_CODES = {"unlimited": 0, "clicks": 1, "duration": 2}


class _CsConfig(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("cps", ctypes.c_double),
        ("button", ctypes.c_uint32),
        ("delivery", ctypes.c_uint32),
        ("limit_mode", ctypes.c_uint32),
        ("click_limit", ctypes.c_int64),
        ("duration", ctypes.c_double),
        ("jitter_percent", ctypes.c_double),
        ("position_mode", ctypes.c_uint32),
        ("fixed_x", ctypes.c_int32),
        ("fixed_y", ctypes.c_int32),
        ("pin_window", ctypes.c_uint32),
        ("performance_mode", ctypes.c_uint32),
    ]


class _CsStats(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("running", ctypes.c_uint32),
        ("state", ctypes.c_uint32),
        ("reason", ctypes.c_uint32),
        ("clicks_sent", ctypes.c_int64),
        ("clicks_requested", ctypes.c_int64),
        ("input_events", ctypes.c_int64),
        ("send_calls", ctypes.c_int64),
        ("batches", ctypes.c_int64),
        ("failed_events", ctypes.c_int64),
        ("catchup_clicks", ctypes.c_int64),
        ("late_ticks", ctypes.c_int64),
        ("dropped_clicks", ctypes.c_int64),
        ("session_id", ctypes.c_int64),
        ("elapsed", ctypes.c_double),
        ("average_cps", ctypes.c_double),
    ]


_LIB = None
_LOAD_ERROR = ""
_LOAD_TRIED = False
_LOAD_LOCK = threading.Lock()


def _library_name() -> str:
    return "clickstorm_engine.dll"


def _library_candidates():
    name = _library_name()
    seen = []
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parent)
    roots.append(Path(__file__).resolve().parent.parent)
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    for root in roots:
        try:
            candidate = root / name
        except Exception:
            continue
        key = str(candidate)
        if key not in seen:
            seen.append(key)
    return seen


def _load():
    """Load and validate the native library once. Returns the CDLL or None."""
    global _LIB, _LOAD_ERROR, _LOAD_TRIED
    with _LOAD_LOCK:
        if _LOAD_TRIED:
            return _LIB
        _LOAD_TRIED = True

        if sys.platform != "win32":
            _LOAD_ERROR = "native engine is Windows-only"
            return None
        if struct.calcsize("P") != 8:
            # The ABI layout of the two structs is only guaranteed on 64-bit
            # Windows; the 32-bit build uses the Python engine instead.
            _LOAD_ERROR = "native engine requires 64-bit Windows"
            return None
        if ctypes.sizeof(_CsConfig) != CONFIG_SIZE:
            _LOAD_ERROR = (
                f"config layout mismatch: {ctypes.sizeof(_CsConfig)} != {CONFIG_SIZE}"
            )
            return None
        if ctypes.sizeof(_CsStats) != STATS_SIZE:
            _LOAD_ERROR = (
                f"stats layout mismatch: {ctypes.sizeof(_CsStats)} != {STATS_SIZE}"
            )
            return None

        last_error = ""
        for candidate in _library_candidates():
            if not os.path.exists(candidate):
                continue
            try:
                lib = ctypes.WinDLL(candidate)
                lib.cs_engine_abi_version.restype = ctypes.c_uint32
                lib.cs_engine_new.restype = ctypes.c_void_p
                lib.cs_engine_new.argtypes = []
                lib.cs_engine_free.restype = None
                lib.cs_engine_free.argtypes = [ctypes.c_void_p]
                lib.cs_engine_start.restype = ctypes.c_int
                lib.cs_engine_start.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_CsConfig),
                ]
                lib.cs_engine_stop.restype = None
                lib.cs_engine_stop.argtypes = [ctypes.c_void_p]
                lib.cs_engine_stats.restype = ctypes.c_int
                lib.cs_engine_stats.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_CsStats),
                ]
                lib.cs_engine_last_error.restype = ctypes.c_size_t
                lib.cs_engine_last_error.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_char),
                    ctypes.c_size_t,
                ]

                version = int(lib.cs_engine_abi_version())
                if version != ABI_VERSION:
                    last_error = (
                        f"engine ABI {version} does not match app ABI {ABI_VERSION}"
                    )
                    continue
                _LIB = lib
                _LOAD_ERROR = ""
                return lib
            except Exception as exc:  # a bad DLL must never break the app
                last_error = f"{candidate}: {type(exc).__name__}: {exc}"

        _LOAD_ERROR = last_error or "clickstorm_engine.dll was not found"
        return None


def available() -> bool:
    return _load() is not None


def load_error() -> str:
    _load()
    return _LOAD_ERROR


def _read_error(lib, handle) -> str:
    try:
        capacity = 512
        buf = ctypes.create_string_buffer(capacity)
        needed = int(lib.cs_engine_last_error(handle, buf, capacity))
        if needed == 0:
            return ""
        if needed > capacity:
            buf = ctypes.create_string_buffer(needed)
            lib.cs_engine_last_error(handle, buf, needed)
        return buf.raw[:needed].decode("utf-8", "replace")
    except Exception:
        return ""


class NativeClickEngine:
    """Drop-in replacement for :class:`clickstorm.engine.ClickEngine`.

    Same public methods as the Python engine, backed by the Rust core.
    """

    backend_name = "Rust"

    def __init__(self, lib) -> None:
        self._lib = lib
        self._handle = lib.cs_engine_new()
        if not self._handle:
            raise RuntimeError("cs_engine_new returned NULL")
        self._lock = threading.Lock()
        self._session_id = 0
        self._started = False
        self._event_sent = True
        self._events: list[EngineEvent] = []
        self._config = EngineConfig()
        self._cancelled = False

    # -- lifecycle ------------------------------------------------------

    @classmethod
    def create_if_available(cls):
        lib = _load()
        if lib is None:
            return None
        try:
            return cls(lib)
        except Exception as exc:
            global _LOAD_ERROR
            _LOAD_ERROR = f"could not create the native engine: {exc}"
            return None

    def start(self, config: EngineConfig) -> bool:
        config = config.normalised()
        with self._lock:
            if self._started and self._read_stats().running:
                return False
            self._flush_pending_event()
            raw = _CsConfig(
                abi_version=ABI_VERSION,
                cps=float(config.cps),
                button=BUTTON_CODES.get(config.button, 0),
                delivery=DELIVERY_CODES.get(config.delivery, 0),
                limit_mode=LIMIT_CODES.get(config.limit_mode, 0),
                click_limit=int(config.click_limit),
                duration=float(config.duration),
                jitter_percent=float(config.jitter_percent),
                position_mode=1 if config.position_mode == "fixed" else 0,
                fixed_x=int(config.fixed_x),
                fixed_y=int(config.fixed_y),
                pin_window=1 if config.pin_window else 0,
                performance_mode=1 if config.performance_mode else 0,
            )
            result = int(self._lib.cs_engine_start(self._handle, ctypes.byref(raw)))
            if result != 0:
                return False
            self._session_id += 1
            self._started = True
            self._event_sent = False
            self._config = config
            self._events.clear()
            return True

    def stop(self) -> None:
        with self._lock:
            if self._handle:
                self._lib.cs_engine_stop(self._handle)

    def close(self, timeout: float = 1.5) -> None:
        with self._lock:
            handle = self._handle
            if not handle:
                return
            self._lib.cs_engine_stop(handle)
        deadline = time.perf_counter() + max(0.0, timeout)
        while time.perf_counter() < deadline:
            if not self._read_stats().running:
                break
            time.sleep(0.005)
        with self._lock:
            handle = self._handle
            self._handle = None
            if handle:
                # The native thread holds its own Arc, so freeing is safe even
                # if a blocked SendInput outlives this call.
                self._lib.cs_engine_free(handle)

    # -- state ----------------------------------------------------------

    def is_running(self) -> bool:
        with self._lock:
            if not self._started or not self._handle:
                return False
            return bool(self._read_stats().running)

    def get_stats(self) -> EngineStats:
        with self._lock:
            native = self._read_stats()
            config = self._config
            error = _read_error(self._lib, self._handle) if self._handle else ""
            reason = _REASON[native.reason] if native.reason < len(_REASON) else ""
            running = bool(native.running)
            if native.state < len(_STATS_STATE):
                state = _STATS_STATE[native.state]
            else:
                state = "idle"
            if running:
                state = "running"

            target_clicks = (
                config.click_limit if config.limit_mode == "clicks" else None
            )
            target_seconds = (
                config.duration if config.limit_mode == "duration" else None
            )
            elapsed = float(native.elapsed)
            if target_clicks is not None:
                progress = min(1.0, native.clicks_sent / max(1, target_clicks))
            elif target_seconds is not None:
                progress = min(1.0, elapsed / max(0.001, target_seconds))
            else:
                progress = None

            return EngineStats(
                session_id=self._session_id,
                state=state,
                running=running,
                clicks_sent=int(native.clicks_sent),
                clicks_requested=int(native.clicks_requested),
                input_events=int(native.input_events),
                send_calls=int(native.send_calls),
                batches=int(native.batches),
                failed_events=int(native.failed_events),
                catchup_clicks=int(native.catchup_clicks),
                late_ticks=int(native.late_ticks),
                dropped_clicks=int(native.dropped_clicks),
                elapsed=elapsed,
                started_at=0.0,
                finished_at=0.0,
                limit_mode=config.limit_mode,
                target_clicks=target_clicks,
                target_seconds=target_seconds,
                progress=progress,
                average_cps=float(native.average_cps),
                last_error=error,
                reason=reason,
            )

    def poll_events(self) -> list[EngineEvent]:
        with self._lock:
            if not self._started or not self._handle:
                return []
            native = self._read_stats()
            if native.running:
                return []
            if self._event_sent:
                return []
            self._event_sent = True
            reason = _REASON[native.reason] if native.reason < len(_REASON) else ""
            if not reason:
                reason = "stopped"
            message = _read_error(self._lib, self._handle) if self._handle else ""
            if reason != "error":
                message = ""
            event = EngineEvent(
                kind="error" if reason == "error" else "finished",
                session_id=self._session_id,
                reason=reason,
                message=message,
                clicks_sent=int(native.clicks_sent),
            )
            self._events.append(event)
            events = self._events
            self._events = []
            return events

    # -- helpers --------------------------------------------------------

    def _read_stats(self):
        stats = _CsStats()
        if not self._handle:
            return stats
        try:
            self._lib.cs_engine_stats(self._handle, ctypes.byref(stats))
        except Exception:
            pass
        return stats

    def _flush_pending_event(self) -> None:
        """Keep the previous session's completion event for the UI queue."""
        if not self._started or self._event_sent:
            return
        native = self._read_stats()
        if native.running:
            return
        self._event_sent = True
        reason = _REASON[native.reason] if native.reason < len(_REASON) else ""
        if not reason:
            reason = "stopped"
        self._events.append(
            EngineEvent(
                kind="error" if reason == "error" else "finished",
                session_id=self._session_id,
                reason=reason,
                message=_read_error(self._lib, self._handle)
                if reason == "error"
                else "",
                clicks_sent=int(native.clicks_sent),
            )
        )


def create_native_engine():
    """Return a ready native engine, or None when the DLL is unavailable."""
    return NativeClickEngine.create_if_available()
