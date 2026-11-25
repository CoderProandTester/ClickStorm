"""High-throughput Windows click engine.

Pacing model
------------
Clicks are scheduled on an absolute timeline: click *n* is due at
``start + n / cps``.  The timeline is a float, so a rate that is not a whole
number of clicks per millisecond keeps its fraction instead of drifting, and
the first click fires immediately when the run starts.

One click per call
------------------
Every click is submitted in its own ``SendInput`` call.  Packing many clicks
into one array raises the *emitted* rate (fewer syscalls) but lowers the
*delivered* rate: batched clicks carry no spacing, and the system input stack
coalesces them before the receiving application's message loop sees them.  An
independent Windows benchmark measured emitted +36% with delivered -22% and
fidelity falling from 99% to 57% for batched arrays (see "Prior art" in
README.md), so the engine sends one DOWN/UP pair per call and only ever puts
several clicks in flight when the requested rate genuinely needs it.

Catch-up
--------
When a turn wakes late, every click whose deadline has passed is due now.
They are sent as separate single-click calls, so a stalled millisecond is
repaid without being coalesced, and the schedule resumes on its original
timeline.  Backlog beyond ``MAX_CATCHUP_MS`` is dropped so a multi-second
stall cannot turn into an unbounded burst.

Waiting
-------
A three-tier wait keeps the timeline without pinning the CPU: a
high-resolution waitable timer covers gaps above 2 ms, ``SwitchToThread``
yields between 2 ms and 50 us, and the loop only spins the last 50 us.

Delivery
--------
Credit is only spent on clicks Windows actually accepted: a rejected click is
not counted and is retried on the next turn, so a run still ends on the exact
target.  The INPUT array is built once per session and reused; the hot loop
allocates nothing, prints nothing and takes no locks.

The thread is independent of Tk, so the UI and the global hotkeys stay
responsive and STOP takes effect between two clicks.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from . import winapi


MIN_CPS = 1.0
# Requested rates above ~4,000 CPS cannot be delivered by Windows: SendInput
# serialises through the system input thread and the receiving application
# consumes on its own message loop, so the receiver is the bottleneck.  The
# ceiling is offered as a "run flat out" setting rather than a promise.
MAX_CPS = 25_000.0
MAX_CLICKS = 25_000
MAX_DURATION = 86_400.0

# A stall longer than this many milliseconds is treated as a real freeze: the
# excess backlog is dropped instead of fired at the target as one huge burst.
MAX_CATCHUP_MS = 50
# Wait tiers, in seconds. Above YIELD the timer sleeps, between SPIN and YIELD
# the thread yields its timeslice, below SPIN it spins.
YIELD_SECONDS = 0.002
SPIN_SECONDS = 0.00005
# Keep retrying through a temporarily swamped input queue (about 0.4 s)
# before abandoning a run, so a brief stall cannot cut a target short.
MAX_CONSECUTIVE_FAILURES = 200


@dataclass(frozen=True)
class EngineConfig:
    cps: float = 500.0
    button: str = "Left"
    limit_mode: str = "unlimited"  # unlimited | clicks | duration
    click_limit: int = 5_000
    duration: float = 10.0
    delivery: str = "sendinput"  # sendinput | direct
    jitter_percent: float = 0.0
    position_mode: str = "cursor"  # cursor | fixed
    fixed_x: int = 0
    fixed_y: int = 0
    performance_mode: bool = True

    def normalised(self) -> "EngineConfig":
        cps = max(MIN_CPS, min(MAX_CPS, float(self.cps)))
        click_limit = max(1, min(MAX_CLICKS, int(self.click_limit)))
        duration = max(0.05, min(MAX_DURATION, float(self.duration)))
        jitter = max(0.0, min(0.75, float(self.jitter_percent)))
        button = self.button if self.button in winapi.BUTTONS else "Left"
        delivery = (
            self.delivery if self.delivery in ("sendinput", "direct") else "sendinput"
        )
        position = (
            self.position_mode if self.position_mode in ("cursor", "fixed") else "cursor"
        )
        limit = (
            self.limit_mode
            if self.limit_mode in ("unlimited", "clicks", "duration")
            else "unlimited"
        )
        return EngineConfig(
            cps=cps,
            button=button,
            limit_mode=limit,
            click_limit=click_limit,
            duration=duration,
            delivery=delivery,
            jitter_percent=jitter,
            position_mode=position,
            fixed_x=int(self.fixed_x),
            fixed_y=int(self.fixed_y),
            performance_mode=bool(self.performance_mode),
        )


@dataclass(frozen=True)
class EngineStats:
    session_id: int
    state: str
    running: bool
    clicks_sent: int
    clicks_requested: int
    input_events: int
    send_calls: int
    batches: int
    failed_events: int
    catchup_clicks: int
    late_ticks: int
    dropped_clicks: int
    elapsed: float
    started_at: float
    finished_at: float
    limit_mode: str
    target_clicks: int | None
    target_seconds: float | None
    progress: float | None
    average_cps: float
    last_error: str
    reason: str


@dataclass(frozen=True)
class EngineEvent:
    kind: str  # finished | error
    session_id: int
    reason: str
    message: str
    clicks_sent: int


class ClickEngine:
    """A reusable click engine with a long-lived worker thread.

    ``start(config)`` begins one session.  ``stop()`` requests a graceful
    stop.  ``poll_events()`` is polled by the Tk main loop; Tk is never
    touched from the worker thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()

        self._shutdown = False
        self._running = False
        self._stop_requested = False

        self._config = EngineConfig()
        self._session_id = 0

        self._started_at = 0.0
        self._finished_at = 0.0
        self._state = "idle"
        self._reason = ""

        self._clicks_sent = 0
        self._clicks_requested = 0
        self._input_events = 0
        self._send_calls = 0
        self._batches = 0
        self._failed_events = 0
        self._catchup_clicks = 0
        self._late_ticks = 0
        self._dropped_clicks = 0
        self._last_error = ""

        self._events = []
        self._events_lock = threading.Lock()
        self._timer_resolution = winapi.TimerResolution()

        self._thread = threading.Thread(
            target=self._worker,
            name="ClickStormEngine",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, config: EngineConfig) -> bool:
        config = config.normalised()
        with self._lock:
            if self._running or self._shutdown:
                return False
            self._config = config
            self._session_id += 1
            self._running = True
            self._stop_requested = False
            self._state = "running"
            self._reason = ""
            self._started_at = 0.0
            self._finished_at = 0.0
            self._clicks_sent = 0
            self._clicks_requested = 0
            self._input_events = 0
            self._send_calls = 0
            self._batches = 0
            self._failed_events = 0
            self._catchup_clicks = 0
            self._late_ticks = 0
            self._dropped_clicks = 0
            self._last_error = ""
        self._wake.set()
        return True

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
        self._wake.set()

    def close(self, timeout: float = 1.5) -> None:
        with self._lock:
            self._shutdown = True
            self._stop_requested = True
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout)
        self._timer_resolution.end()

    def poll_events(self) -> list[EngineEvent]:
        with self._events_lock:
            events = self._events
            self._events = []
        return events

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def get_stats(self) -> EngineStats:
        now = time.perf_counter()
        with self._lock:
            started = self._started_at
            if self._running:
                end = now
            else:
                end = self._finished_at or started
            elapsed = max(0.0, end - started) if started else 0.0

            target_clicks = (
                self._config.click_limit
                if self._config.limit_mode == "clicks"
                else None
            )
            target_seconds = (
                self._config.duration
                if self._config.limit_mode == "duration"
                else None
            )
            if target_clicks is not None:
                progress = min(1.0, self._clicks_sent / max(1, target_clicks))
            elif target_seconds is not None:
                progress = min(1.0, elapsed / max(0.001, target_seconds))
            else:
                progress = None

            average_cps = (
                self._clicks_sent / elapsed if elapsed > 0.0005 else 0.0
            )

            return EngineStats(
                session_id=self._session_id,
                state=self._state,
                running=self._running,
                clicks_sent=self._clicks_sent,
                clicks_requested=self._clicks_requested,
                input_events=self._input_events,
                send_calls=self._send_calls,
                batches=self._batches,
                failed_events=self._failed_events,
                catchup_clicks=self._catchup_clicks,
                late_ticks=self._late_ticks,
                dropped_clicks=self._dropped_clicks,
                elapsed=elapsed,
                started_at=started,
                finished_at=self._finished_at,
                limit_mode=self._config.limit_mode,
                target_clicks=target_clicks,
                target_seconds=target_seconds,
                progress=progress,
                average_cps=average_cps,
                last_error=self._last_error,
                reason=self._reason,
            )

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    return
                running = self._running

            if not running:
                self._wake.wait(0.25)
                self._wake.clear()
                continue

            self._run_session()

    # ------------------------------------------------------------------
    # Hot loop
    # ------------------------------------------------------------------

    def _run_session(self) -> None:
        with self._lock:
            config = self._config
            session_id = self._session_id
            self._started_at = time.perf_counter()
            self._finished_at = 0.0

        self._timer_resolution.begin()
        timer = winapi.HighResTimer()
        if config.performance_mode:
            winapi.set_thread_priority(True)
            # Windows 11 may move a busy thread onto an ecoQoS low-power core;
            # opting out of execution-speed throttling keeps the timeline on a
            # full-speed core. Harmless where the API is unavailable.
            winapi.disable_power_throttling()

        target_clicks = (
            config.click_limit if config.limit_mode == "clicks" else None
        )
        duration = (
            config.duration if config.limit_mode == "duration" else None
        )

        fixed_x = fixed_y = 0
        if config.position_mode == "fixed":
            fixed_x, fixed_y = winapi.clamp_to_screen(
                config.fixed_x, config.fixed_y
            )
            winapi.set_cursor_pos(fixed_x, fixed_y)

        direct_target = None
        direct_point = None
        if config.delivery == "direct":
            point = self._point(config, fixed_x, fixed_y)
            if point is not None:
                direct_target = winapi.make_direct_target(
                    point[0], point[1], config.button
                )
                if direct_target is not None:
                    winapi.focus_target(direct_target)
                direct_point = point

        # One click per call: a single DOWN/UP pair, built once and reused, so
        # the hot loop never allocates and no two clicks are ever coalesced.
        click_array = (
            winapi.build_click_inputs(config.button, 1)
            if config.delivery == "sendinput"
            else None
        )

        interval = 1.0 / config.cps
        # Clicks that may be repaid after a stall (e.g. 1,250 at 25,000 CPS).
        max_catchup = max(1, int(config.cps * (MAX_CATCHUP_MS / 1000.0)))
        jitter = config.jitter_percent
        failures = 0
        start = time.perf_counter()
        next_at = start
        reason = "stopped"
        message = ""
        lock_every = max(1, int(config.cps // 500) + 1)
        since_lock = 0

        try:
            while True:
                # Plain attribute reads: no lock in the hot path.
                if self._shutdown or self._stop_requested:
                    reason = "stopped"
                    break

                if not self._wait_until(next_at, timer):
                    reason = "stopped"
                    break

                now = time.perf_counter()

                if duration is not None and now - start >= duration:
                    reason = "duration"
                    break
                if (
                    target_clicks is not None
                    and self._clicks_sent >= target_clicks
                ):
                    reason = "target"
                    break

                # Every click whose deadline has passed is due now. A late
                # wake-up (timer overshoot, busy core, swamped target) makes
                # this larger; those clicks are owed to the run and are paid
                # out as separate calls so they keep their own DOWN/UP pairs.
                late = now - next_at
                due = 1 + int(late / interval) if late > 0.0 else 1
                lagging = due > max_catchup
                if lagging:
                    # A real freeze: drop the excess and restart the cadence
                    # from now instead of firing an unbounded burst.
                    self._dropped_clicks += due - max_catchup
                    due = max_catchup
                elif due > 1:
                    self._late_ticks += due - 1

                step = interval
                if jitter > 0.0:
                    step = interval * (1.0 + random.uniform(-jitter, jitter))
                if lagging:
                    next_at = now + step
                else:
                    next_at += due * step

                if target_clicks is not None:
                    left = target_clicks - self._clicks_sent
                    if due > left:
                        # The run ends on this turn: surplus clicks never
                        # reach the target and are not reported as requested.
                        due = left
                    if due <= 0:
                        reason = "target"
                        break
                self._clicks_requested += due

                since_lock += 1
                if config.position_mode == "fixed" and since_lock >= lock_every:
                    winapi.keep_cursor_locked(fixed_x, fixed_y)
                    since_lock = 0

                accepted_total = 0
                calls = 0
                detail = ""

                for _ in range(due):
                    if self._shutdown or self._stop_requested:
                        break

                    if click_array is not None:
                        events, error = winapi.send_inputs(click_array, 2)
                        calls += 1
                        if events == 2:
                            accepted_total += 1
                            continue
                        # A partial batch can leave the button held down.
                        if events & 1:
                            winapi.send_button_release(config.button)
                        detail = (
                            "Windows rejected injected input"
                            + (f" (error {error})" if error else "")
                            + ". If the target is running elevated, run "
                            "ClickStorm as administrator."
                        )
                    else:
                        point = self._point(config, fixed_x, fixed_y)
                        if point is None:
                            detail = "Could not read the cursor position."
                        else:
                            if point != direct_point:
                                direct_target = winapi.make_direct_target(
                                    point[0], point[1], config.button
                                )
                                direct_point = point
                                if direct_target is not None:
                                    winapi.focus_target(direct_target)
                            if direct_target is None:
                                detail = (
                                    "No clickable window was found under the "
                                    "cursor."
                                )
                            else:
                                calls += 1
                                if winapi.post_click(direct_target):
                                    accepted_total += 1
                                    continue
                                detail = (
                                    "The target window rejected posted clicks. "
                                    "Try SendInput delivery instead."
                                )

                    # The input queue is full: stop filling it and let it drain
                    # instead of burning the rest of this turn's budget.
                    break

                shortfall = due - accepted_total
                if shortfall > 0:
                    # Only accepted clicks are paid for. The shortfall keeps
                    # its deadline, so it is retried on the next turn.
                    next_at -= shortfall * step
                    self._failed_events += shortfall * 2
                    if accepted_total == 0:
                        # Nothing got through: give the queue a moment instead
                        # of spinning on a refused call.
                        winapi.switch_to_thread()
                        time.sleep(0.001)

                self._clicks_sent += accepted_total
                self._input_events += accepted_total * 2
                self._send_calls += calls
                self._batches += 1
                if due > 1 and accepted_total:
                    self._catchup_clicks += accepted_total

                if accepted_total:
                    failures = 0
                    if detail:
                        self._last_error = detail
                    elif shortfall == 0:
                        # A clean turn clears a message left by an earlier
                        # rejection so the UI never shows a stale error.
                        self._last_error = ""
                else:
                    failures += 1
                    if detail:
                        self._last_error = detail
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        reason = "error"
                        message = (
                            detail or "Windows rejected every injected click."
                        )
                        break

                if (
                    target_clicks is not None
                    and self._clicks_sent >= target_clicks
                ):
                    reason = "target"
                    break

        except Exception as exc:  # defensive: never silently kill the thread
            reason = "error"
            message = f"Click engine error: {exc}"
            with self._lock:
                self._last_error = message
        finally:
            timer.close()
            if config.performance_mode:
                winapi.reset_thread_priority()
            self._timer_resolution.end()

            with self._lock:
                self._running = False
                self._finished_at = time.perf_counter()
                self._state = "error" if reason == "error" else "stopped"
                self._reason = reason
                clicks_sent = self._clicks_sent
                error = self._last_error

            event = EngineEvent(
                kind="error" if reason == "error" else "finished",
                session_id=session_id,
                reason=reason,
                message=(message or error) if reason == "error" else "",
                clicks_sent=clicks_sent,
            )
            with self._events_lock:
                self._events.append(event)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _should_stop(self) -> bool:
        return self._shutdown or self._stop_requested

    def _wait_until(self, deadline: float, timer) -> bool:
        """Wait for ``deadline``. Returns False when a stop is requested.

        Three tiers, so the wait is cheap but still lands on the timeline: the
        high-resolution waitable timer sleeps through anything longer than
        2 ms, ``SwitchToThread`` yields between 2 ms and 50 us, and the last
        50 us are spun. Oversleeping is harmless: the due-click count repays
        it on the next turn.
        """
        while True:
            if self._shutdown or self._stop_requested:
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return True
            if remaining > YIELD_SECONDS:
                timer.sleep(remaining - YIELD_SECONDS + 0.0002)
            elif remaining > SPIN_SECONDS:
                winapi.switch_to_thread()
            else:
                while time.perf_counter() < deadline:
                    if self._shutdown or self._stop_requested:
                        return False

    @staticmethod
    def _point(
        config: EngineConfig,
        fixed_x: int,
        fixed_y: int,
    ) -> tuple[int, int] | None:
        if config.position_mode == "fixed":
            return fixed_x, fixed_y
        return winapi.get_cursor_pos()
