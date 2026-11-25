"""High-throughput Windows click engine.

Pacing model
------------
The configured rate is a per-second target.  One second is 1,000 ms, so the
engine works in clicks per millisecond (``cps / 1000``): 25,000 CPS is 25
clicks for every one of the 1,000 slots in a second, and 500 CPS is half a
click per slot.  Credit is accrued against the real clock (``perf_counter``)
as a float, so fractional rates such as 1,234 CPS (1.234 clicks/ms) keep their
fraction instead of drifting, and a late wake-up is repaid automatically
instead of losing clicks.

Pinned window
-------------
With ``pin_window`` the direct path resolves the window under the cursor once
when the run starts and then keeps the handle.  The cursor can move and the
target can sit behind another window: the clicks still land on that client
area, and the hot loop skips both ``GetCursorPos`` and the window hit test.
A run started from ClickStorm's own button finds ClickStorm under the cursor,
which is why ``make_direct_target`` refuses windows that belong to this
process.

Delivery
--------
Measured on Windows, one ``SendInput`` call carrying a single click (2 events)
costs roughly 20 us, while a call carrying 25 clicks (50 events) can take
milliseconds because the call waits for the receiving application to drain
the input queue.  Wide batches therefore *lower* delivered CPS, so the engine
sends a deliberately small batch per call (one click at first) and only widens
it while calls stay far cheaper than a slot; a slow or partially rejected call
immediately narrows it again.

Backlog
-------
Credit is capped at ``MAX_BACKLOG_MS`` milliseconds of the target rate.  When
the target is faster than the machine and the target app can deliver, the
excess is dropped and reported instead of piling up into a huge burst that
would stall the loop and make the click rate look frozen.  Credit is only
spent on clicks Windows actually accepted: rejected clicks stay owed and are
retried immediately.

The INPUT array is built once per session and reused by every call; the hot
loop allocates nothing, prints nothing and takes no locks.  The thread is
independent of Tk, so the UI and the global hotkeys stay responsive and STOP
takes effect between two calls.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from . import winapi


MIN_CPS = 1.0
# SendInput injects through one system input thread. Benchmarks of native
# clickers put delivered input at roughly 2,000-4,000 CPS, so 25,000 is the
# highest target worth offering: it lets a fast machine run flat out, and the
# 25 clicks/ms schedule is the example the pacing math is designed around.
MAX_CPS = 25_000.0
MAX_CLICKS = 25_000
MAX_DURATION = 86_400.0

# Real injected input cannot go faster than this: SendInput hands every event
# to one system input thread and then to the target's message loop, and
# measured ceilings on fast desktops land around 6,000-7,000 delivered CPS no
# matter how fast the sender is. Posted window messages skip that thread, so
# they do reach the requested rate. "auto" delivery therefore uses SendInput
# up to this rate and posted messages above it.
SENDINPUT_CEILING_CPS = 6_000.0

# The scheduling grid. One slot is one millisecond, so a CPS value divided by
# 1000 is exactly the number of clicks that belongs to one slot.
TICK_SECONDS = 0.001
# Clicks are never owed further than this many milliseconds of the target
# rate. A slow SendInput call must not build a backlog that later fires as one
# huge batch: that is the slowest possible way to deliver clicks and it makes
# a running session look frozen.
MAX_BACKLOG_MS = 20.0
# Reusable INPUT arrays are sized for this many clicks and never reallocated.
# Small batches are faster than wide ones once the receiving app throttles
# input, so the array covers the growth ceiling only. The batch starts at the
# number of clicks one 1 ms slot carries (25 at 25,000 CPS), which is one
# SendInput call per slot instead of 25 queue-blocking calls.
MAX_CHUNK_CLICKS = 32
MIN_CHUNK_CLICKS = 1
INITIAL_CHUNK_CLICKS = 1
# Posted window messages never wait for the system input queue, so direct
# delivery can carry a much wider batch per call.
DIRECT_CHUNK_CLICKS = 256
# A call slower than this is a sign the target is throttling input; widen the
# batch only while a call stays comfortably inside one millisecond slot.
CHUNK_SLOW_SECONDS = 0.0008
# Wait tiers, in seconds: above YIELD a timer sleeps, between SPIN and YIELD
# the thread yields its timeslice back to the CPU, below SPIN it spins.
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
    delivery: str = "sendinput"  # sendinput | direct | auto
    pin_window: bool = False
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
            self.delivery
            if self.delivery in ("sendinput", "direct", "auto")
            else "sendinput"
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
            pin_window=bool(self.pin_window),
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
            # A higher process class keeps the click thread from being pushed
            # behind the receiving app, which is what raises delivered CPS on
            # a busy machine.
            winapi.set_thread_priority(True)
            winapi.set_process_priority(True)
            # Windows 11 can move a busy thread onto an ecoQoS low-power core
            # and clamp its clock; the click thread asks for full speed for
            # the length of the session. Harmless where the API is missing.
            winapi.disable_power_throttling()

        target_clicks = (
            config.click_limit if config.limit_mode == "clicks" else None
        )
        duration = (
            config.duration if config.limit_mode == "duration" else None
        )

        if config.position_mode == "fixed":
            fixed_x, fixed_y = winapi.clamp_to_screen(
                config.fixed_x, config.fixed_y
            )
            winapi.set_cursor_pos(fixed_x, fixed_y)
        else:
            fixed_x = fixed_y = 0

        delivery = config.delivery
        if delivery == "auto":
            # Choose the path that can actually reach the requested rate:
            # SendInput is capped by Windows, posted messages are not.
            delivery = (
                "direct"
                if config.cps > SENDINPUT_CEILING_CPS
                else "sendinput"
            )

        direct_target = None
        direct_point = None
        pinned = False
        if delivery == "direct":
            point = self._point(config, fixed_x, fixed_y)
            if point is not None:
                # Requested pinning is remembered even when nothing was found
                # underneath, so the run reports the pinned hint instead of a
                # generic "no window" message.
                pinned = bool(config.pin_window)
                direct_target = winapi.make_direct_target(
                    point[0], point[1], config.button
                )
                if direct_target is not None:
                    # The window handle is kept for the whole run. Posting to
                    # it needs neither the cursor nor the foreground, so the
                    # target can even be covered by another window.
                    winapi.focus_target(direct_target)
                direct_point = point
            if config.delivery == "auto" and direct_target is None:
                # Nothing to post to under the cursor. An auto run must still
                # click, so fall back to the universally compatible path.
                delivery = "sendinput"

        # Built once per session. Every SendInput call reuses this array and
        # simply passes a smaller event count, so the loop never allocates.
        click_array = (
            winapi.build_click_inputs(config.button, MAX_CHUNK_CLICKS)
            if delivery == "sendinput"
            else None
        )

        cps = config.cps
        cps_per_ms = cps / 1000.0
        # Credit (clicks owed but not delivered yet) never exceeds a few
        # milliseconds of the target rate, so an over-fast target degrades into
        # a steady stream at the machine's real ceiling instead of stalling.
        backlog_cap = max(1.0, cps_per_ms * MAX_BACKLOG_MS)
        jitter = config.jitter_percent
        slot_clicks = int(cps_per_ms + 0.5)
        if click_array is not None:
            # A SendInput call waits for the target to drain its input queue,
            # so a wide call is slower than a narrow one: start at a single
            # click and let the adaptive growth widen it only when that is
            # actually cheap.
            chunk_limit = MAX_CHUNK_CLICKS
            chunk = INITIAL_CHUNK_CLICKS
        else:
            # Posted messages skip the input queue, so direct delivery can
            # start at the clicks one slot carries (25 at 25,000 CPS).
            chunk_limit = DIRECT_CHUNK_CLICKS
            chunk = max(
                INITIAL_CHUNK_CLICKS, min(chunk_limit, max(1, slot_clicks))
            )
        clean_bursts = 0
        failures = 0
        credit = 0.0
        start = time.perf_counter()
        last = start
        reason = "stopped"
        message = ""

        try:
            while True:
                # Plain attribute reads: no lock in the hot path.
                if self._shutdown or self._stop_requested:
                    reason = "stopped"
                    break

                now = time.perf_counter()
                delta = now - last
                last = now

                if duration is not None and now - start >= duration:
                    reason = "duration"
                    break
                if (
                    target_clicks is not None
                    and self._clicks_sent >= target_clicks
                ):
                    reason = "target"
                    break

                late = delta > 2.0 * TICK_SECONDS
                if late:
                    self._late_ticks += 1

                # Time-based credit: no slot grid to fall behind, so a late
                # wake-up is repaid on this turn and fractional rates keep
                # their fraction instead of drifting.
                gain = cps * delta
                if jitter > 0.0:
                    gain *= 1.0 + random.uniform(-jitter, jitter)
                credit += gain
                if credit > backlog_cap:
                    self._dropped_clicks += int(credit - backlog_cap)
                    credit = backlog_cap

                if credit < 1.0:
                    # Nothing is due yet. Sleep until the next whole click
                    # instead of waking on every empty 1 ms slot: below 1,000
                    # CPS that is several slots, above it a fraction of one.
                    wait = (1.0 - credit) / cps
                    resumed = 0.0
                    if wait > 0.0:
                        sleep_from = time.perf_counter()
                        if not self._wait_until(sleep_from + wait, timer):
                            reason = "stopped"
                            break
                        resumed = time.perf_counter()
                    # Account for the deliberate wait here, including any
                    # overshoot, so it is neither lost nor reported as a late
                    # turn.
                    if resumed:
                        credit += cps * (resumed - sleep_from)
                        if credit > backlog_cap:
                            credit = backlog_cap
                        last = resumed
                    continue

                due = int(credit)
                if due > chunk:
                    due = chunk
                if target_clicks is not None:
                    left = target_clicks - self._clicks_sent
                    if due > left:
                        # The run ends on this batch: the surplus never reaches
                        # the target and must not be reported as requested.
                        due = left
                    if due <= 0:
                        reason = "target"
                        break
                self._clicks_requested += due

                if config.position_mode == "fixed" and not pinned:
                    winapi.keep_cursor_locked(fixed_x, fixed_y)

                call_start = time.perf_counter()
                accepted = 0
                detail = ""

                if click_array is not None:
                    sent, error = winapi.send_inputs(click_array, due * 2)
                    accepted = sent >> 1
                    if accepted > due:
                        accepted = due
                    if sent & 1:
                        # A partial batch can leave the button held down.
                        winapi.send_button_release(config.button)
                    if accepted < due:
                        detail = (
                            "Windows rejected injected input"
                            + (f" (error {error})" if error else "")
                            + ". If the target is running elevated, run "
                            "ClickStorm as administrator."
                        )
                else:
                    if pinned:
                        # Resolved once at start: skipping the per-batch
                        # cursor lookup and window hit test is also the
                        # fastest path, because neither changes the answer.
                        point = direct_point
                    else:
                        point = self._point(config, fixed_x, fixed_y)
                    if pinned and direct_target is None:
                        detail = (
                            "Pinned window mode found no clickable window "
                            "under the cursor. Move the cursor over the "
                            "target window and start again."
                        )
                    elif point is None:
                        detail = "Could not read the cursor position."
                    else:
                        if not pinned and point != direct_point:
                            direct_target = winapi.make_direct_target(
                                point[0], point[1], config.button
                            )
                            direct_point = point
                            if direct_target is not None:
                                winapi.focus_target(direct_target)
                        if direct_target is None:
                            detail = (
                                "No clickable window was found under the cursor."
                            )
                        else:
                            accepted = winapi.post_clicks(direct_target, due)
                            if accepted < due:
                                detail = (
                                    "The target window rejected some posted "
                                    "clicks. Try SendInput delivery instead."
                                )

                call_seconds = time.perf_counter() - call_start

                # Only accepted clicks are paid for: anything Windows refused
                # stays owed and is retried on the next turn.
                credit -= accepted
                self._clicks_sent += accepted
                self._input_events += accepted * 2
                self._send_calls += 1
                self._batches += 1
                self._failed_events += (due - accepted) * 2
                if late:
                    self._catchup_clicks += accepted

                if accepted >= due:
                    failures = 0
                    if detail:
                        self._last_error = detail
                    else:
                        # A clean batch clears a message left by an earlier
                        # rejection so the UI never shows a stale error.
                        self._last_error = ""
                    if call_seconds < CHUNK_SLOW_SECONDS:
                        clean_bursts += 1
                        if clean_bursts >= 16 and chunk < chunk_limit:
                            chunk = min(chunk_limit, chunk * 2)
                            clean_bursts = 0
                    else:
                        # The call already takes longer than a slot: widening
                        # the array would only make delivery slower.
                        clean_bursts = 0
                        if chunk > MIN_CHUNK_CLICKS:
                            chunk = max(MIN_CHUNK_CLICKS, chunk // 2)
                else:
                    failures += 1
                    clean_bursts = 0
                    # Windows or the target started refusing events: use the
                    # narrowest call so the input queue can drain again.
                    if chunk > MIN_CHUNK_CLICKS:
                        chunk = max(MIN_CHUNK_CLICKS, chunk // 2)
                    if detail:
                        self._last_error = detail
                    if accepted == 0:
                        if failures >= MAX_CONSECUTIVE_FAILURES:
                            reason = "error"
                            message = (
                                detail
                                or "Windows rejected every injected click."
                            )
                            break
                        # Nothing got through. Yield to the queue first and
                        # only pause once a few calls in a row were refused:
                        # a flat 1 ms sleep threw away most of the next slot
                        # at high rates, while a 0.25 ms high-resolution wait
                        # still keeps a refusal storm from pinning a core.
                        winapi.switch_to_thread()
                        if failures > 4:
                            timer.sleep(0.00025)

        except Exception as exc:  # defensive: never silently kill the thread
            reason = "error"
            message = f"Click engine error: {exc}"
            with self._lock:
                self._last_error = message
        finally:
            timer.close()
            if config.performance_mode:
                winapi.reset_thread_priority()
                winapi.reset_process_priority()
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

        Three tiers, so the wait is cheap but still lands on the deadline: a
        high-resolution waitable timer above 2 ms, ``SwitchToThread`` between
        2 ms and 50 us, and a spin only for the last 50 us. Spinning on the
        last few hundred microseconds instead cost about half a core at
        1,000 CPS for no accuracy gain. Oversleeping is harmless: the credit
        maths repays it on the next turn.
        """
        while True:
            if self._shutdown or self._stop_requested:
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return True
            if remaining > YIELD_SECONDS:
                # Cap one sleep so a long wait (low rates) still reacts to the
                # stop hotkey within a few milliseconds.
                timer.sleep(min(remaining - YIELD_SECONDS + 0.0002, 0.01))
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
