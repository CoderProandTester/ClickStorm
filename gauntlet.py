"""ClickStorm Gauntlet - a standalone autoclicker (5,000 CPS ceiling).

A single-file console autoclicker for 64-bit Windows. It batches whole clicks
into one pre-allocated ``SendInput`` array and paces batches against
``perf_counter()``, so a 5,000 CPS target is 200 calls of 25 clicks per
second instead of 5,000 separate calls.

Architecture (the "Gauntlet Loop")
----------------------------------
1. A contiguous ``(INPUT * (batch * 2))`` array is built once, alternating
   DOWN and UP events, and every call reuses it. The hot loop allocates
   nothing, creates no lists or tuples and prints nothing.
2. The worker thread asks for 1 ms timer resolution (``timeBeginPeriod``) and
   ``THREAD_PRIORITY_ABOVE_NORMAL``; both are restored in ``finally``.
   ``REALTIME_PRIORITY_CLASS`` is deliberately never used because it can
   starve the HID input stack itself.
3. When armed, the loop dispatches one batch per kernel transition, counts the
   clicks Windows accepted, and holds the next batch on a deadline. Waking and
   yielding happen above 300 us, with a pure spin only for the last 300 us, so
   the deadline is hit without pegging a core (and without ``time.sleep``,
   which cannot resolve below the system tick).
4. The main thread is a watchdog only: it polls F6 (arm/disarm) and ESC (halt
   and exit) with ``GetAsyncKeyState`` and prints achieved CPS once a second.

Usage
-----
    python gauntlet.py                    # 5,000 CPS, left button
    python gauntlet.py --cps 1000         # slower target
    python gauntlet.py --cps 0            # uncapped: uses the 5,000 ceiling
    python gauntlet.py --batch 50         # 50 clicks (100 events) per call

F6 arms and disarms. ESC stops the worker and exits. Clicks go to whatever is
under the cursor, so point it somewhere harmless before arming.

Windows blocks injected input to elevated windows (UIPI) unless this process
is elevated too, so run the script from an Administrator terminal when the
target runs as administrator.

Run ``python gauntlet.py --help`` for the options.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from ctypes import wintypes

if sys.platform != "win32":
    raise SystemExit(
        "gauntlet.py is a Windows tool: SendInput, GetAsyncKeyState and "
        "timeBeginPeriod are Win32 APIs."
    )

# ---------------------------------------------------------------------------
# Win32 foundation (64-bit verified: sizeof(INPUT) must be 40 bytes)
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
winmm = ctypes.WinDLL("winmm", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

ULONG_PTR = ctypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUTunion),
    ]


assert ctypes.sizeof(INPUT) == 40, (
    f"64-bit INPUT must be 40 bytes, got {ctypes.sizeof(INPUT)}. "
    "Use 64-bit Python on Windows."
)
INPUT_SIZE = ctypes.sizeof(INPUT)

SendInput = user32.SendInput
SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
SendInput.restype = wintypes.UINT

user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
kernel32.SetThreadPriority.restype = wintypes.BOOL
kernel32.SwitchToThread.argtypes = ()
kernel32.SwitchToThread.restype = wintypes.BOOL

winmm.timeBeginPeriod.argtypes = (wintypes.UINT,)
winmm.timeBeginPeriod.restype = wintypes.UINT
winmm.timeEndPeriod.argtypes = (wintypes.UINT,)
winmm.timeEndPeriod.restype = wintypes.UINT

shell32.IsUserAnAdmin.argtypes = ()
shell32.IsUserAnAdmin.restype = wintypes.BOOL

THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_ABOVE_NORMAL = 1

# Virtual-key codes from winuser.h.
VK_F6 = 0x75
VK_ESCAPE = 0x1B

__version__ = "4.6.1"  # keep in sync with clickstorm/__init__.py

DEFAULT_CPS = 5_000.0
MAX_CPS = 5_000.0
DEFAULT_BATCH = 25
MAX_BATCH = 500
# Watchdog cadence (~66 Hz) and telemetry period.
WATCHDOG_SECONDS = 0.015
TELEMETRY_SECONDS = 1.0
# Above this much time remaining, hand the timeslice back instead of spinning;
# only the last stretch before the deadline is a pure spin. This keeps the
# 1 ms cadence exact without burning a whole core for the whole batch period.
SPIN_SECONDS = 0.0003
# Batches are normally a few milliseconds apart at the 5,000 CPS cap. A
# deliberately low --cps makes that gap much longer, so anything above this
# threshold waits on the arm event instead of spinning for seconds (still
# interruptible by F6/ESC).
LONG_WAIT_SECONDS = 0.004
# Give up on a target that refuses every event for this many batches in a row
# instead of spinning on a dead input queue.
MAX_CONSECUTIVE_FAILURES = 200

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def _format_rate(value: float) -> str:
    return f"{value:,.0f}"


class Gauntlet:
    """Pre-allocated SendInput batch engine on its own daemon thread."""

    def __init__(
        self,
        cps: float = DEFAULT_CPS,
        batch: int = DEFAULT_BATCH,
        button: str = "left",
    ) -> None:
        self.cps = float(cps)
        self.batch = max(1, min(MAX_BATCH, int(batch)))
        self.button = button if button in BUTTONS else "left"
        self._event_count = self.batch * 2
        self._interval = (
            self.batch / self.cps if self.cps > 0.0 else 0.0
        )

        self._batch_array = self._build_batch()
        self._release_array = self._build_release()

        self._armed_event = threading.Event()
        self._armed_flag = False
        self._stop_flag = False
        self._thread = threading.Thread(
            target=self._worker, name="GauntletWorker", daemon=True
        )

        # Read by the watchdog thread; ints are published under the GIL.
        self._clicks = 0
        self._failed = 0
        self._send_calls = 0
        self._error = ""

    # -- buffers --------------------------------------------------------

    def _build_batch(self):
        down_flag, up_flag = BUTTONS[self.button]
        array = (INPUT * (self.batch * 2))()
        for index in range(self.batch):
            item = array[index * 2]
            item.type = INPUT_MOUSE
            item.union.mi = MOUSEINPUT(0, 0, 0, down_flag, 0, 0)

            item = array[index * 2 + 1]
            item.type = INPUT_MOUSE
            item.union.mi = MOUSEINPUT(0, 0, 0, up_flag, 0, 0)
        return array

    def _build_release(self):
        _down_flag, up_flag = BUTTONS[self.button]
        array = (INPUT * 1)()
        array[0].type = INPUT_MOUSE
        array[0].union.mi = MOUSEINPUT(0, 0, 0, up_flag, 0, 0)
        return array

    # -- public state ---------------------------------------------------

    @property
    def clicks(self) -> int:
        return self._clicks

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def send_calls(self) -> int:
        return self._send_calls

    @property
    def armed(self) -> bool:
        return self._armed_flag

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> None:
        self._thread.start()

    def arm(self) -> None:
        self._armed_flag = True
        self._armed_event.set()

    def disarm(self) -> None:
        self._armed_flag = False
        self._armed_event.clear()

    def toggle(self) -> bool:
        if self._armed_flag:
            self.disarm()
        else:
            self.arm()
        return self._armed_flag

    def stop(self) -> None:
        self._stop_flag = True
        # Wake an idle worker and let it leave the loop promptly.
        self._armed_event.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    # -- worker ---------------------------------------------------------

    def _worker(self) -> None:
        kernel32.SetThreadPriority(
            kernel32.GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL
        )
        winmm.timeBeginPeriod(1)
        try:
            self._hot_loop()
        except Exception as exc:  # never let the worker die silently
            self._error = f"worker stopped: {exc}"
            self._armed_flag = False
            self._armed_event.clear()
        finally:
            winmm.timeEndPeriod(1)
            kernel32.SetThreadPriority(
                kernel32.GetCurrentThread(), THREAD_PRIORITY_NORMAL
            )

    def _hot_loop(self) -> None:
        """The Gauntlet Loop.

        No allocations, no list operations, no prints and no sleeps: one
        pre-built batch per kernel transition, a counter, and a deadline.
        """
        send = SendInput
        batch_array = self._batch_array
        release_array = self._release_array
        event_count = self._event_count
        input_size = INPUT_SIZE
        perf = time.perf_counter
        switch = kernel32.SwitchToThread
        interval = self._interval
        spin = SPIN_SECONDS
        batch_size = self.batch

        deadline = 0.0
        failures = 0
        clicks = 0

        while not self._stop_flag:
            if not self._armed_flag:
                # Idle wait: arms immediately when F6 sets the event, and
                # uses no CPU at all while disarmed.
                self._armed_event.wait(0.01)
                deadline = perf()
                continue

            sent = send(event_count, batch_array, input_size)
            accepted = sent >> 1
            if sent & 1:
                # An odd count means a DOWN whose UP was refused. Release the
                # button once so the target never sees it latched.
                send(1, release_array, input_size)

            clicks += accepted
            self._clicks = clicks
            self._send_calls = self._send_calls + 1

            if accepted < batch_size:
                self._failed = self._failed + (batch_size - accepted)
                if accepted == 0:
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self._error = (
                            "Windows rejected every click for "
                            f"{MAX_CONSECUTIVE_FAILURES} batches. If the "
                            "target runs elevated, start this script from an "
                            "Administrator terminal."
                        )
                        self._armed_flag = False
                        self._armed_event.clear()
                        failures = 0
                else:
                    failures = 0
            else:
                failures = 0

            if interval > 0.0:
                if deadline <= 0.0:
                    deadline = perf()
                deadline += interval
                while True:
                    if self._stop_flag or not self._armed_flag:
                        deadline = 0.0
                        break
                    remaining = deadline - perf()
                    if remaining <= 0.0:
                        break
                    if remaining > LONG_WAIT_SECONDS:
                        # Only reachable at low CPS: wait interruptibly rather
                        # than spin a core for a multi-millisecond gap.
                        self._armed_event.wait(remaining - LONG_WAIT_SECONDS)
                    elif remaining > spin:
                        switch()
                now = perf()
                if now - deadline > interval * 4.0:
                    # The machine or the target stalled; resync instead of
                    # firing a catch-up burst at it.
                    deadline = now


# ---------------------------------------------------------------------------
# Shared engine adapter
# ---------------------------------------------------------------------------

def _shared_engine_factory():
    """Return ClickStorm's engine factory when the package is importable."""
    try:
        from clickstorm.engine import make_engine
    except Exception:
        return None
    return make_engine


class SharedGauntlet:
    """The same hotkey harness, driving ClickStorm's real click engine.

    Run from the project (or beside an installed ClickStorm) and this is what
    clicks: the native Rust core when its DLL is present, otherwise the
    Python engine. The embedded :class:`Gauntlet` loop is only used by the
    single-file download, so wherever the package is available there is
    exactly one engine rather than two separate implementations.
    """

    def __init__(self, engine, cps: float, button: str) -> None:
        from clickstorm.engine import EngineConfig

        self._engine = engine
        self.cps = float(cps)
        self.batch = 0
        self._config = EngineConfig(
            cps=self.cps,
            button=button,
            limit_mode="unlimited",
            delivery="sendinput",
        )
        self._armed_flag = False
        self._error = ""

    @classmethod
    def create(cls, cps: float, button: str):
        factory = _shared_engine_factory()
        if factory is None:
            return None
        try:
            engine = factory()
        except Exception:
            return None
        return cls(engine, cps, button)

    # -- compatibility surface used by main() ---------------------------

    @property
    def clicks(self) -> int:
        return int(self._engine.get_stats().clicks_sent)

    @property
    def failed(self) -> int:
        return int(self._engine.get_stats().failed_events)

    @property
    def send_calls(self) -> int:
        return int(self._engine.get_stats().send_calls)

    @property
    def armed(self) -> bool:
        return self._armed_flag and self._engine.is_running()

    @property
    def error(self) -> str:
        if self._error:
            return self._error
        if self._armed_flag:
            return ""
        stats = self._engine.get_stats()
        if stats.state == "error" and stats.last_error:
            return stats.last_error
        return ""

    def start(self) -> None:
        # The engine owns its own thread from construction.
        pass

    def arm(self) -> None:
        if self.armed:
            return
        self._error = ""
        if not self._engine.start(self._config):
            self._error = "the click engine is still shutting down"
            return
        self._armed_flag = True

    def disarm(self) -> None:
        self._armed_flag = False
        self._engine.stop()

    def toggle(self) -> bool:
        if self.armed:
            self.disarm()
        else:
            self.arm()
        return self.armed

    def stop(self) -> None:
        self._armed_flag = False
        self._engine.stop()

    def join(self, timeout: float = 2.0) -> None:
        self._engine.close(timeout)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="gauntlet.py",
        description=(
            "Standalone autoclicker (SendInput batch engine, 5,000 CPS "
            "ceiling). "
            "F6 arms/disarms, ESC stops and exits."
        ),
        epilog=(
            "Clicks go to whatever is under the cursor. Windows UIPI blocks "
            "input to elevated windows unless this script is elevated too."
        ),
    )
    parser.add_argument(
        "--cps",
        type=float,
        default=DEFAULT_CPS,
        help="target clicks per second; 0 = the 5,000 ceiling (default 5000)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"clicks per SendInput call, 1-{MAX_BATCH} (default {DEFAULT_BATCH})",
    )
    parser.add_argument(
        "--button",
        choices=sorted(BUTTONS),
        default="left",
        help="mouse button to click (default left)",
    )
    args = parser.parse_args(argv)
    if args.cps < 0.0:
        parser.error("--cps must be 0 (uncapped) or a positive number")
    if not (1 <= args.batch <= MAX_BATCH):
        parser.error(f"--batch must be between 1 and {MAX_BATCH}")
    return args


def main(argv=None) -> int:
    args = _parse_args(argv)
    target = min(MAX_CPS, args.cps) if args.cps > 0.0 else DEFAULT_CPS
    button = args.button.capitalize()
    shared = SharedGauntlet.create(cps=target, button=button)
    if shared is not None:
        gauntlet = shared
        backend = "shared ClickStorm engine (native core when present)"
    else:
        gauntlet = Gauntlet(cps=target, batch=args.batch, button=args.button)
        backend = "embedded fallback (ClickStorm package not found)"

    print(f"ClickStorm Gauntlet {__version__} - one click engine")
    print(f"  engine      {backend}")
    if shared is not None:
        print(
            "  batch      adaptive - the shared engine narrows or widens "
            "its SendInput call to what the target accepts"
        )
        if args.batch != DEFAULT_BATCH:
            print(
                "             --batch is only used by the embedded fallback; "
                "ignored here"
            )
    else:
        print(
            f"  batch      {gauntlet.batch} clicks "
            f"({gauntlet._event_count} input events) per SendInput call"
        )
    if shared is not None:
        note = "" if args.cps > 0.0 else "  (uncapped maps to the 5,000 ceiling)"
        print(f"  target      {_format_rate(target)} CPS, paced by the engine{note}")
    elif target > 0.0:
        per_second = gauntlet.cps / gauntlet.batch
        print(
            f"  target      {_format_rate(gauntlet.cps)} CPS  "
            f"({_format_rate(per_second)} calls/s, "
            f"{gauntlet._interval * 1000.0:.3f} ms per batch)"
        )
    else:
        print("  target      uncapped (send as fast as Windows accepts)")
    print("  button      " + args.button)
    if _is_admin():
        print("  elevation   administrator (can click elevated windows)")
    else:
        print(
            "  elevation   standard user - Windows UIPI blocks injected input "
            "to elevated windows"
        )
    print()
    print("F6 arms/disarms. ESC stops and exits. Point the cursor at the target.")
    print()

    started = time.perf_counter()
    prev_clicks = 0
    prev_time = started

    gauntlet.start()
    try:
        while True:
            time.sleep(WATCHDOG_SECONDS)

            if user32.GetAsyncKeyState(VK_F6) & 0x0001:
                if gauntlet.toggle():
                    print("[armed] gauntlet running", flush=True)
                else:
                    print("[idle] gauntlet paused", flush=True)

            if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
                print("\n[ESC] stopping", flush=True)
                break

            now = time.perf_counter()
            elapsed = now - prev_time
            if elapsed >= TELEMETRY_SECONDS:
                clicks = gauntlet.clicks
                achieved = (clicks - prev_clicks) / elapsed
                state = "ARMED" if gauntlet.armed else "IDLE"
                batch_text = (
                    "auto" if getattr(gauntlet, "batch", 1) == 0
                    else f"{gauntlet.batch:>3}"
                )
                print(
                    f"[{now - started:7.1f}s] {state:<5} "
                    f"{achieved:9,.0f} CPS  batch {batch_text}  "
                    f"clicks {clicks:>12,}  dropped {gauntlet.failed:,}",
                    flush=True,
                )
                prev_clicks = clicks
                prev_time = now

            message = gauntlet.error
            if message:
                print(f"\nERROR: {message}", file=sys.stderr, flush=True)
                return 2
    except KeyboardInterrupt:
        print("\n[Ctrl+C] stopping", flush=True)
    finally:
        gauntlet.stop()
        gauntlet.join()

    clicks = gauntlet.clicks
    total_seconds = time.perf_counter() - started
    average = clicks / total_seconds if total_seconds > 0.0 else 0.0
    print(
        f"stopped: {clicks:,} clicks in {total_seconds:.1f}s "
        f"({_format_rate(average)} CPS average), "
        f"{gauntlet.failed:,} dropped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
