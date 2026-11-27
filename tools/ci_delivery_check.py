"""Windows CI check: delivered SENDINPUT rate and real SYNC registration.

The check starts ``tools/click_counter.py`` in its own process, puts the
cursor over that window, and drives the real engine at a 5,000 CPS target.
The counter only reports clicks that Windows actually delivered to it, so it
measures the target side of the click path rather than the engine's own
bookkeeping.

Requirements checked:

* both cores are exercised, Rust then Python,
* SENDINPUT delivers at least 4,000 clicks per second at a 5,000 target,
* SYNC registers essentially every click it asks for,
* DIRECT completes a 4,000 click (two second) run at 2,000 CPS,
* no mode leaves the button unpaired.

This job is the gate in front of the installer builds: no exe is built unless
both cores pass here.

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
# DIRECT at 2,000 CPS for two seconds: long enough to outlast the batches the
# old engine finished before its credit bank deadlocked or its target's
# message queue filled, whichever came first.
DIRECT_CPS = 2_000.0
DIRECT_CLICKS = 4_000
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


def annotate_failure(message: str) -> None:
    """Also expose the reason as a check-run annotation."""
    escaped = (
        message.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )
    print(f"::error title=Delivery test failed::{escaped[:6000]}")


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
    cps: float = TARGET_CPS,
    label: str = "engine",
) -> tuple[int, float]:
    from clickstorm.engine import EngineConfig

    tag = f"{label}/{delivery}"
    before = read_report(report)["downs"]
    started = time.perf_counter()
    config = EngineConfig(
        cps=cps,
        limit_mode="clicks",
        click_limit=clicks,
        delivery=delivery,
    )
    if not engine.start(config):
        raise RuntimeError(f"{tag}: engine refused to start")

    while engine.is_running() and time.perf_counter() - started < SESSION_TIMEOUT:
        time.sleep(0.005)
    elapsed = time.perf_counter() - started
    if engine.is_running():
        engine.stop()
        raise RuntimeError(f"{tag}: session did not finish within {SESSION_TIMEOUT}s")

    # Let the counter's Tk loop drain the last events before reading it.
    after = settle_report(report)
    stats = engine.get_stats()
    delivered = int(after["downs"]) - int(before)
    line = (
        f"{tag}: engine sent {stats.clicks_sent:,}/{clicks:,} "
        f"in {elapsed:.3f}s, counter received {delivered:,} "
        f"({delivered / elapsed:,.0f} delivered CPS), "
        f"retried shortfall events {stats.failed_events:,}, "
        f"unpaired {max(0, after['downs'] - after['ups']):,}"
    )
    print(line)
    write_summary([line])
    detail = (
        f"engine sent {stats.clicks_sent}, retried {stats.failed_events}, "
        f"counter {delivered:,} delivered ({after['downs']:,} down / "
        f"{after['ups']:,} up), elapsed {elapsed:.3f}s, "
        f"last error {stats.last_error or 'none'}"
    )
    if stats.clicks_sent != clicks:
        raise RuntimeError(
            f"{tag}: engine finished with {stats.clicks_sent} of {clicks} clicks "
            f"({detail})"
        )
    # A short call is retried, not lost: the engine keeps a click owed until
    # Windows accepts it. Treating every retried shortfall as a failure made a
    # merely busy input queue fail the job while every click still arrived, so
    # the failure condition is a click that never landed at all.
    owed = stats.clicks_requested - stats.clicks_sent
    if owed > 0:
        raise RuntimeError(f"{tag}: {owed} clicks were never accepted ({detail})")
    if delivered < clicks * MIN_DELIVERED_RATIO:
        raise RuntimeError(
            f"{tag}: only {delivered} of {clicks} clicks reached the target "
            f"({detail})"
        )
    if require_rate and delivered / elapsed < MIN_DELIVERED_CPS:
        raise RuntimeError(
            f"{tag}: delivered {delivered / elapsed:,.0f} CPS, "
            f"below the {MIN_DELIVERED_CPS:,.0f} CPS requirement ({detail})"
        )
    if after["downs"] - after["ups"] > 5:
        raise RuntimeError(
            f"{tag}: {after['downs'] - after['ups']} unpaired button events "
            f"({detail})"
        )
    return delivered, elapsed


def open_engine(preference: str):
    """Return the engine for ``preference``, proving that core is running."""
    from clickstorm.engine import make_engine

    engine = make_engine(preference)
    expected = "Rust" if preference == "rust" else "Python"
    core = getattr(engine, "core_name", "Python")
    if core != expected:
        from clickstorm import native_engine

        reason = (
            getattr(engine, "last_fallback_reason", "")
            or native_engine.load_error()
            or "DLL missing"
        )
        candidates = {
            str(path): os.path.exists(path)
            for path in native_engine._library_candidates()
        }
        engine.close()
        raise RuntimeError(
            f"the {expected} core is not in use: core is {core} ({reason}); "
            f"candidates {candidates}"
        )
    print(f"{core} core: {getattr(engine, 'backend_name', 'Ultimate')}")
    return engine


def check_engine(preference: str, hwnd: int, report: Path) -> None:
    """Run every delivery mode on one core; any failure aborts the check."""
    engine = open_engine(preference)
    label = getattr(engine, "core_name", "Python")
    try:
        # Re-aim before each run: the previous run must not leave the cursor
        # over something else if the counter moved.
        aim_at_counter(hwnd)
        run_session(
            engine,
            "sendinput",
            SENDINPUT_CLICKS,
            report,
            require_rate=True,
            label=label,
        )
        aim_at_counter(hwnd)
        run_session(
            engine, "sync", SYNC_CLICKS, report, require_rate=False, label=label
        )
        aim_at_counter(hwnd)
        # A full posted-message queue used to abort the session; a live target
        # that drains is now paced instead, so the run must end on its count.
        run_session(
            engine,
            "direct",
            DIRECT_CLICKS,
            report,
            require_rate=False,
            cps=DIRECT_CPS,
            label=label,
        )
    finally:
        engine.close()


def main() -> int:
    if sys.platform != "win32":
        print("This check needs Windows.", file=sys.stderr)
        return 2

    # Physical pixels, so the cursor lands on the window on high-DPI runners.
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

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

    try:
        hwnd = wait_for_window(title)
        aim_at_counter(hwnd)
        read_report(report)

        for preference in ("rust", "python"):
            check_engine(preference, hwnd, report)
        print(
            "PASS: both cores delivered SENDINPUT at rate, registered SYNC "
            "clicks and finished a DIRECT run."
        )
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
        annotate_failure(f"FAIL: {exc} (session {os.environ.get('SESSIONNAME', '?')}, python {sys.version.split()[0]})")
        return 1
    finally:
        counter.terminate()
        try:
            counter.wait(timeout=5)
        except subprocess.TimeoutExpired:
            counter.kill()


if __name__ == "__main__":
    raise SystemExit(main())
