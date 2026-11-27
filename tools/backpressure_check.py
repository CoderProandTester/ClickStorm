"""Headless check: what the engine does when the target cannot keep up.

Runs ``clickstorm.engine`` against a stub ``winapi`` whose ``SendInput`` and
``PostMessage`` behave like the real queues: a call only inserts as many events
as the queue has room for, the queue drains at a fixed rate, and every call
costs a little time. Nothing here needs Windows, so it runs on the Linux CI
image and on any developer machine.

What it proves:

* a 2,000 CPS DIRECT run keeps clicking while the target's 10,000-message
  queue is saturated - before 4.6.4 the run aborted after a few seconds;
* SENDINPUT settles at the rate the target drains instead of sawtoothing down
  to tiny batches, which is what made a busy target read a few hundred CPS;
* a target that refuses everything is still reported as an error, after the
  two-second stall window instead of after fifty milliseconds;
* a healthy target still gets the exact click count, with no failed events.

Run from the repository root:

    python tools/backpressure_check.py
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import clickstorm  # noqa: E402  (the package must exist before the stub)


class QueueModel:
    """A queue that only accepts what has drained, like the real ones."""

    def __init__(self, capacity: float, drain_per_second: float) -> None:
        self.capacity = float(capacity)
        self.drain_per_second = float(drain_per_second)
        self.depth = 0.0
        self.last = time.perf_counter()

    def push(self, count: int) -> int:
        count = int(count)
        if count <= 0:
            return 0
        now = time.perf_counter()
        self.depth = max(
            0.0, self.depth - (now - self.last) * self.drain_per_second
        )
        self.last = now
        room = int(self.capacity - self.depth)
        taken = max(0, min(count, room))
        self.depth += taken
        return taken


class Stub:
    """One model of the two Windows queues the engine can deliver through."""

    def __init__(
        self,
        events_capacity: float = 1e12,
        events_drain: float = 1e12,
        posts_capacity: float = 1e12,
        posts_drain: float = 1e12,
        blocked: bool = False,
        call_seconds: float = 0.00015,
    ) -> None:
        self.events = QueueModel(events_capacity, events_drain)
        self.posts = QueueModel(posts_capacity, posts_drain)
        self.blocked = blocked
        self.call_seconds = call_seconds

    def send(self, event_count: int) -> int:
        """Model SendInput: insert what fits, whole clicks only."""
        if self.blocked:
            return 0
        if self.call_seconds:
            time.sleep(self.call_seconds)
        events = self.events.push(event_count)
        if events & 1:
            # The engine answers a dangling DOWN with an UP, which needs a
            # slot of its own; keep the model at whole clicks.
            events -= 1
            self.events.depth -= 1
        return events

    def post(self, clicks: int) -> int:
        """Model PostMessage: one queued message per DOWN and per UP."""
        if self.blocked:
            return 0
        if self.call_seconds:
            time.sleep(self.call_seconds)
        messages = self.posts.push(clicks * 2)
        whole = messages // 2
        self.posts.depth -= messages - whole * 2
        return whole


_HOLDER: dict[str, Stub | None] = {"stub": None}


def _build_stub_module() -> types.ModuleType:
    module = types.ModuleType("clickstorm.winapi")

    def stub() -> Stub:
        current = _HOLDER["stub"]
        if current is None:
            raise RuntimeError("no stub installed")
        return current

    def send_inputs(_inputs, event_count: int):
        return stub().send(int(event_count)), 0

    module.BUTTONS = {
        "Left": (0x0002, 0x0004, 0),
        "Right": (0x0008, 0x0010, 0),
        "Middle": (0x0020, 0x0040, 0),
        "X1": (0x0080, 0x0100, 1),
        "X2": (0x0080, 0x0100, 2),
    }
    module.build_click_inputs = lambda button, count: int(count)
    module.send_inputs = send_inputs
    module.send_button_release = lambda button: 1
    module.post_clicks = lambda target, count: stub().post(int(count))
    module.get_cursor_pos = lambda: (100, 100)
    module.set_cursor_pos = lambda x, y: True
    module.clamp_to_screen = lambda x, y: (int(x), int(y))
    module.make_direct_target = lambda x, y, button: (x, y, button)
    module.focus_target = lambda target: True
    module.switch_to_thread = lambda: True
    module.set_thread_priority = lambda extreme=True: True
    module.reset_thread_priority = lambda: True
    module.set_process_priority = lambda high=True: True
    module.reset_process_priority = lambda: True
    module.disable_power_throttling = lambda: True

    class HighResTimer:
        high_resolution = False

        def sleep(self, seconds: float) -> None:
            time.sleep(seconds)

        def close(self) -> None:
            pass

    class TimerResolution:
        active = False

        def begin(self) -> bool:
            return True

        def end(self) -> None:
            pass

    module.HighResTimer = HighResTimer
    module.TimerResolution = TimerResolution
    return module


sys.modules["clickstorm.winapi"] = _build_stub_module()
setattr(clickstorm, "winapi", sys.modules["clickstorm.winapi"])

from clickstorm.engine import ClickEngine, EngineConfig  # noqa: E402


def run(config: EngineConfig, seconds: float, sample_at: float | None = None):
    """Run for ``seconds`` and return (stats, still running, events, rate)."""
    _HOLDER["stub"] = STUB
    engine = ClickEngine()
    started = time.perf_counter()
    if not engine.start(config):
        engine.close()
        raise RuntimeError("the engine refused to start")
    try:
        midpoint = None
        if sample_at is not None:
            while engine.is_running() and time.perf_counter() - started < sample_at:
                time.sleep(0.01)
            midpoint = engine.get_stats()
        while engine.is_running() and time.perf_counter() - started < seconds:
            time.sleep(0.01)
        still_running = engine.is_running()
        if still_running:
            engine.stop()
            deadline = time.perf_counter() + 2.0
            while engine.is_running() and time.perf_counter() < deadline:
                time.sleep(0.01)
        stats = engine.get_stats()
        events = engine.poll_events()
    finally:
        engine.close()
    elapsed = max(1e-6, stats.elapsed)
    steady = None
    if midpoint is not None:
        span = time.perf_counter() - started - sample_at
        steady = (stats.clicks_sent - midpoint.clicks_sent) / max(1e-6, span)
    return stats, still_running, events, stats.clicks_sent / elapsed, steady


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}: {detail}")
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def case_low_rate_direct() -> None:
    """Low rates used to freeze once the batch doubled past the credit bank."""
    global STUB
    print("DIRECT, 500 CPS, unlimited drain (low-rate stall regression)")
    STUB = Stub()
    stats, running, events, average, _ = run(
        EngineConfig(cps=500, delivery="direct", limit_mode="unlimited"),
        seconds=2.0,
    )
    errors = [event.message for event in events if event.kind == "error"]
    check("no error event", not errors, errors or "none")
    check(
        "kept clicking for the whole run",
        stats.clicks_sent >= 900,
        f"{stats.clicks_sent:,} clicks in {stats.elapsed:.2f}s "
        f"({average:,.0f} CPS)",
    )


def case_mid_rate_direct() -> None:
    """2,000 CPS DIRECT against a target that always keeps up."""
    global STUB
    print("DIRECT, 2,000 CPS, unlimited drain (batch-vs-bank regression)")
    STUB = Stub()
    stats, running, events, average, _ = run(
        EngineConfig(cps=2_000, delivery="direct", limit_mode="unlimited"),
        seconds=2.0,
    )
    errors = [event.message for event in events if event.kind == "error"]
    check("no error event", not errors, errors or "none")
    check(
        "delivered the target rate",
        stats.clicks_sent >= 3_700,
        f"{stats.clicks_sent:,} clicks in {stats.elapsed:.2f}s "
        f"({average:,.0f} CPS)",
    )


def case_direct_saturated() -> None:
    """2,000 CPS DIRECT against a full 10,000-message queue (the stop bug)."""
    global STUB
    print("DIRECT, 2,000 CPS, 10,000-message queue draining 500 clicks/s")
    # 4,000 posted messages a second against a 1,000 a second drain fills the
    # real 10,000-message queue in about three seconds, which is the run the
    # old engine ended with a burst of refused PostMessage calls.
    STUB = Stub(posts_capacity=10_000, posts_drain=500 * 2)
    stats, running, events, average, _ = run(
        EngineConfig(cps=2_000, delivery="direct", limit_mode="unlimited"),
        seconds=6.0,
    )
    errors = [event.message for event in events if event.kind == "error"]
    check("run still clicking at 6 s", running, f"running={running}")
    check("no error event", not errors, errors or "none")
    check(
        "kept delivering through the full queue",
        stats.clicks_sent >= 6_500,
        f"{stats.clicks_sent:,} clicks ({average:,.0f} CPS average)",
    )


def case_sendinput_saturated() -> None:
    """SENDINPUT against a congested input queue: settle at its drain rate."""
    global STUB
    drain_cps = 2_500.0
    print(
        f"SENDINPUT, 6,000 CPS, 10,000-event input queue draining "
        f"{drain_cps:,.0f} clicks/s"
    )
    STUB = Stub(events_capacity=10_000, events_drain=drain_cps * 2)
    stats, running, events, average, steady = run(
        EngineConfig(cps=6_000, delivery="sendinput", limit_mode="unlimited"),
        seconds=4.0,
        sample_at=2.0,
    )
    errors = [event.message for event in events if event.kind == "error"]
    check("run still clicking at 4 s", running, f"running={running}")
    check("no error event", not errors, errors or "none")
    check(
        "steady delivery tracks the drain rate",
        steady is not None and steady >= drain_cps * 0.85,
        f"{steady or 0:,.0f} CPS in the second half "
        f"({average:,.0f} CPS average, drain {drain_cps:,.0f})",
    )


def case_blocked() -> None:
    """Nothing accepted at all: report after the stall window, not instantly."""
    global STUB
    print("SENDINPUT, 500 CPS, every event refused (blocked input)")
    STUB = Stub(blocked=True)
    stats, running, events, average, _ = run(
        EngineConfig(cps=500, delivery="sendinput", limit_mode="unlimited"),
        seconds=4.0,
    )
    errors = [event.message for event in events if event.kind == "error"]
    check("run reported as an error", bool(errors), errors or "no error event")
    check("run is over", not running, f"running={running}")
    check(
        "waited the two-second stall window",
        stats.elapsed >= 1.9,
        f"stopped after {stats.elapsed:.2f}s",
    )


def case_healthy() -> None:
    """A healthy target still gets exactly the clicks it was promised."""
    global STUB
    print("SENDINPUT, 6,000 CPS, 1,000 click target, unlimited drain")
    STUB = Stub()
    stats, running, events, average, _ = run(
        EngineConfig(
            cps=6_000,
            delivery="sendinput",
            limit_mode="clicks",
            click_limit=1_000,
        ),
        seconds=5.0,
    )
    check("finished on its target", not running, f"running={running}")
    check(
        "exact click count",
        stats.clicks_sent == 1_000,
        f"{stats.clicks_sent:,} clicks",
    )
    check("no failed events", stats.failed_events == 0, f"{stats.failed_events}")
    check(
        "no error event",
        not [event for event in events if event.kind == "error"],
        "clean",
    )
    check(
        "reached the target rate",
        average >= 5_400,
        f"{average:,.0f} CPS average",
    )


STUB = Stub()


def main() -> int:
    print("ClickStorm backpressure check (stubbed Win32 queues)\n")
    case_healthy()
    print()
    case_low_rate_direct()
    print()
    case_mid_rate_direct()
    print()
    case_direct_saturated()
    print()
    case_sendinput_saturated()
    print()
    case_blocked()
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s) failed")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("PASS: the engine paces a saturated target and never aborts a live one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
