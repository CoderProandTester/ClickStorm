"""Windows CI check: delivered SENDINPUT rate and real SYNC registration.

The check starts ``tools/click_counter.py`` in its own process, puts the
cursor over that window, and drives the real engine at a 5,000 CPS target.
The counter only reports clicks that Windows actually delivered to it, so it
measures the target side of the click path rather than the engine's own
bookkeeping.

Requirements checked:

* the native Rust core is the one clicking (the fastest engine),
* SENDINPUT delivers at least 4,000 clicks per second at a 5,000 target,
* SYNC registers essentially every click it asks for,
* neither mode leaves the button unpaired.

Run on Windows only, from the repository root:

    python tools\\ci_delivery_check.py
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_CPS = 5_000.0
SENDINPUT_CLICKS = 2_500
SYNC_CLICKS = 1_200
MIN_DELIVERED_CPS = 4_000.0
MIN_DELIVERED_RATIO = 0.90
SESSION_TIMEOUT = 25.0


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = (
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
)
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL

HWND_TOPMOST = wintypes.HWND(-1)
SWP_SHOWWINDOW = 0x0040
WINDOW_X, WINDOW_Y = 80, 80
WINDOW_W, WINDOW_H = 560, 360


def read_report(path: Path, timeout: float = 5.0) -> dict:
    deadline = time.perf_counter() + timeout
    last_error = None
    while time.perf_counter() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # file may not exist yet, or be mid-write
            last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"counter report was not readable: {last_error}")


def settle_report(path: Path, timeout: float = 3.0) -> dict:
    """Wait for Tk to drain its queue: the count must stop moving."""
    deadline = time.perf_counter() + timeout
    last = -1
    stable_since = time.perf_counter()
    data = read_report(path)
    while time.perf_counter() < deadline:
        if int(data["downs"]) != last:
            last = int(data["downs"])
            stable_since = time.perf_counter()
        elif time.perf_counter() - stable_since >= 0.25:
            return data
        time.sleep(0.05)
        data = read_report(path)
    return data


def write_summary(lines: list[str]) -> None:
    """Publish diagnostics where the CI job summary exposes them."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    except Exception:
        pass


def wait_for_window(title: str, timeout: float = 15.0) -> int:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            return int(hwnd)
        time.sleep(0.1)
    raise RuntimeError(f"counter window {title!r} never appeared")


def aim_at_counter(hwnd: int) -> None:
    if not user32.SetWindowPos(
        hwnd, HWND_TOPMOST, WINDOW_X, WINDOW_Y, WINDOW_W, WINDOW_H,
        SWP_SHOWWINDOW,
    ):
        raise RuntimeError("could not position the counter window")
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    rect = _Rect()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("could not read the counter window rectangle")
    center_x = (rect.left + rect.right) // 2
    center_y = (rect.top + rect.bottom) // 2
    if not user32.SetCursorPos(center_x, center_y):
        raise RuntimeError("could not move the cursor over the counter")
    time.sleep(0.35)


def run_session(
    engine,
    delivery: str,
    clicks: int,
    report: Path,
    require_rate: bool,
) -> tuple[int, float]:
    from clickstorm.engine import EngineConfig

    before = read_report(report)["downs"]
    started = time.perf_counter()
    config = EngineConfig(
        cps=TARGET_CPS,
        limit_mode="clicks",
        click_limit=clicks,
        delivery=delivery,
    )
    if not engine.start(config):
        raise RuntimeError(f"{delivery}: engine refused to start")

    while engine.is_running() and time.perf_counter() - started < SESSION_TIMEOUT:
        time.sleep(0.005)
    elapsed = time.perf_counter() - started
    if engine.is_running():
        engine.stop()
        raise RuntimeError(f"{delivery}: session did not finish within {SESSION_TIMEOUT}s")

    # Let the counter's Tk loop drain the last events before reading it.
    after = settle_report(report)
    stats = engine.get_stats()
    delivered = int(after["downs"]) - int(before)
    print(
        f"{delivery}: engine sent {stats.clicks_sent:,}/{clicks:,} "
        f"in {elapsed:.3f}s, counter received {delivered:,} "
        f"({delivered / elapsed:,.0f} delivered CPS), "
        f"failed events {stats.failed_events:,}, "
        f"unpaired {max(0, after['downs'] - after['ups']):,}"
    )
    if stats.clicks_sent != clicks:
        raise RuntimeError(
            f"{delivery}: engine finished with {stats.clicks_sent} of {clicks} clicks"
        )
    if stats.failed_events:
        raise RuntimeError(f"{delivery}: {stats.failed_events} input events were rejected")
    if delivered < clicks * MIN_DELIVERED_RATIO:
        raise RuntimeError(
            f"{delivery}: only {delivered} of {clicks} clicks reached the target"
        )
    if require_rate and delivered / elapsed < MIN_DELIVERED_CPS:
        raise RuntimeError(
            f"{delivery}: delivered {delivered / elapsed:,.0f} CPS, "
            f"below the {MIN_DELIVERED_CPS:,.0f} CPS requirement"
        )
    if after["downs"] - after["ups"] > 5:
        raise RuntimeError(
            f"{delivery}: {after['downs'] - after['ups']} unpaired button events"
        )
    return delivered, elapsed


def main() -> int:
    if sys.platform != "win32":
        print("This check needs Windows.", file=sys.stderr)
        return 2

    # Physical pixels, so the cursor lands on the window on high-DPI runners.
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    from clickstorm.engine import make_engine

    work = Path(tempfile.mkdtemp(prefix="clickstorm-delivery-"))
    report = work / "counter.json"
    log = work / "counter.log"
    title = f"ClickStorm CI counter {os.getpid()}"

    with log.open("wb") as log_handle:
        counter = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / "click_counter.py"),
                "--report",
                str(report),
                "--title",
                title,
            ],
            cwd=str(ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

    engine = None
    try:
        hwnd = wait_for_window(title)
        aim_at_counter(hwnd)
        read_report(report)

        engine = make_engine()
        core = getattr(engine, "core_name", "Python")
        if core != "Rust":
            reason = getattr(engine, "last_fallback_reason", "") or "DLL missing"
            raise RuntimeError(
                f"the fastest engine is not in use: core is {core} ({reason})"
            )
        print(f"core: {core} ({getattr(engine, 'backend_name', 'Ultimate')})")

        # Re-aim before each run: the previous run must not leave the cursor
        # over something else if the counter moved.
        aim_at_counter(hwnd)
        run_session(engine, "sendinput", SENDINPUT_CLICKS, report, require_rate=True)
        aim_at_counter(hwnd)
        run_session(engine, "sync", SYNC_CLICKS, report, require_rate=False)
        print("PASS: SENDINPUT delivered at rate and SYNC registered its clicks.")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        summary = ["### Delivery test failed", "", "```", f"FAIL: {exc}"]
        try:
            report_data = read_report(report, timeout=1.0)
            print("counter report:", report_data)
            summary.append(f"counter report: {report_data}")
        except Exception:
            pass
        try:
            counter_log = log.read_text(encoding="utf-8", errors="replace")
            print("counter log:", counter_log)
            if counter_log.strip():
                summary.append("counter log:")
                summary.extend(counter_log.strip().splitlines()[-30:])
        except Exception:
            pass
        summary.append("```")
        write_summary(summary)
        return 1
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass
        counter.terminate()
        try:
            counter.wait(timeout=5)
        except subprocess.TimeoutExpired:
            counter.kill()


if __name__ == "__main__":
    raise SystemExit(main())
