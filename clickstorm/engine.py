"""High-throughput Windows click engine.

Pacing model
------------
Clicks are scheduled on a 1 ms grid.  The configured rate is converted once to
``clicks_per_ms = cps / 1000`` and every grid slot adds that much credit to a
float bank, so a rate that is not a whole number of clicks per millisecond
(e.g. 1,234 CPS -> 1.234 clicks/ms) keeps its fraction instead of drifting.
Whole clicks are paid out as one ``SendInput`` call; the fraction stays in the
bank for the next slot.

Catch-up
--------
Every slot has an absolute deadline on ``perf_counter()``.  When a turn wakes
late (timer overshoot, a busy core, a swamped target window) the number of
slots that were missed is computed from the clock and their clicks are added
to the same bank.  One missed millisecond at 25,000 CPS therefore becomes a
50-click batch on the next turn (two missed milliseconds -> 50 extra clicks)
instead of being lost.  Credit beyond ``MAX_CATCHUP_MS`` is dropped so a
multi-second stall cannot turn into an unbounded burst.

Delivery
--------
Credit is only spent on clicks Windows actually accepted: a rejected shortfall
goes back into the bank and is retried on the next slot.  The INPUT array is
built once per session and reused by every call; the hot loop allocates
nothing, prints nothing and takes no locks, and it only uses a smaller event
count when it needs a smaller batch.

The thread is independent of Tk, so the UI and the global hotkeys stay
responsive and STOP takes effect within one millisecond slot.
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

# The scheduling grid. One slot is one millisecond.
TICK_SECONDS = 0.001
# A stall longer than this many slots is treated as a real freeze: the excess
# credit is dropped instead of fired at the target as one huge burst.
MAX_CATCHUP_MS = 50
# Reusable INPUT arrays are sized for this many clicks and never reallocated.
MAX_CHUNK_CLICKS = 64
MIN_CHUNK_CLICKS = 2
INITIAL_CHUNK_CLICKS = 16
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

        # Built once per session. Every SendInput call reuses this array and
        # simply passes a smaller event count, so the loop never allocates.
        click_array = (
            winapi.build_click_inputs(config.button, MAX_CHUNK_CLICKS)
            if config.delivery == "sendinput"
            else None
        )

        cps_per_ms = config.cps / 1000.0
        max_bank = max(1.0, cps_per_ms * MAX_CATCHUP_MS)
        jitter = config.jitter_percent
        chunk = INITIAL_CHUNK_CLICKS
        clean_bursts = 0
        failures = 0
        bank = 0.0
        start = time.perf_counter()
        deadline = start + TICK_SECONDS
        reason = "stopped"
        message = ""

        try:
            while True:
                # Plain attribute reads: no lock in the hot path.
                if self._shutdown or self._stop_requested:
                    reason = "stopped"
                    break

                if time.perf_counter() < deadline and not self._wait_until(
                    deadline, timer
                ):
                    reason = "stopped"
                    break

                # How many whole 1 ms slots are due right now? A late wake-up
                # makes this larger; those extra slots are the empty
                # milliseconds we owe the run, and their clicks are paid out
                # together with this turn's batch (2 ms missed at 25,000 CPS
                # -> 50 clicks in one SendInput call).
                now = time.perf_counter()
                behind = now - deadline
                slots = 1 + int(behind / TICK_SECONDS) if behind > 0.0 else 1
                if slots > 1:
                    self._late_ticks += slots - 1
                    if slots > MAX_CATCHUP_MS:
                        self._dropped_clicks += int(
                            cps_per_ms * (slots - MAX_CATCHUP_MS)
                        )
                        slots = MAX_CATCHUP_MS
                        deadline = now + TICK_SECONDS
                    else:
                        deadline += slots * TICK_SECONDS
                else:
                    deadline += TICK_SECONDS

                if duration is not None and now - start >= duration:
                    reason = "duration"
                    break
                if (
                    target_clicks is not None
                    and self._clicks_sent >= target_clicks
                ):
                    reason = "target"
                    break

                if jitter > 0.0:
                    bank += (
                        cps_per_ms
                        * slots
                        * (1.0 + random.uniform(-jitter, jitter))
                    )
                else:
                    bank += cps_per_ms * slots
                if bank > max_bank:
                    bank = max_bank

                due = int(bank)
                if due <= 0:
                    # Below one click per millisecond: skip the slots that
                    # cannot produce a click instead of waking 1,000 times a
                    # second for nothing. Their credit is added up front.
                    if cps_per_ms < 1.0:
                        extra = int((1.0 - bank) / cps_per_ms)
                        if extra > 0:
                            bank += cps_per_ms * extra
                            deadline += extra * TICK_SECONDS
                    continue

                bank -= due
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

                if config.position_mode == "fixed":
                    winapi.keep_cursor_locked(fixed_x, fixed_y)

                accepted_total = 0
                calls = 0
                detail = ""
                remaining = due

                if click_array is not None:
                    while remaining > 0:
                        if self._shutdown or self._stop_requested:
                            break
                        size = chunk if chunk < remaining else remaining
                        sent, error = winapi.send_inputs(click_array, size * 2)
                        accepted = sent >> 1
                        if accepted > size:
                            accepted = size
                        if sent & 1:
                            # A partial batch can leave the button held down.
                            winapi.send_button_release(config.button)
                        calls += 1
                        accepted_total += accepted
                        remaining -= accepted
                        if accepted == size:
                            clean_bursts += 1
                            if (
                                clean_bursts >= 16
                                and chunk < MAX_CHUNK_CLICKS
                            ):
                                chunk += 4
                                if chunk > MAX_CHUNK_CLICKS:
                                    chunk = MAX_CHUNK_CLICKS
                                clean_bursts = 0
                        else:
                            # Windows started rejecting events: use smaller
                            # arrays so the input queue can drain again.
                            if chunk > MIN_CHUNK_CLICKS:
                                chunk >>= 1
                                if chunk < MIN_CHUNK_CLICKS:
                                    chunk = MIN_CHUNK_CLICKS
                            clean_bursts = 0
                            detail = (
                                "Windows rejected injected input"
                                + (f" (error {error})" if error else "")
                                + ". If the target is running elevated, run "
                                "ClickStorm as administrator."
                            )
                            break
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
                                "No clickable window was found under the cursor."
                            )
                        else:
                            accepted = 0
                            for _ in range(remaining):
                                if self._shutdown or self._stop_requested:
                                    break
                                if winapi.post_click(direct_target):
                                    accepted += 1
                            accepted_total = accepted
                            calls = accepted
                            remaining -= accepted
                            if remaining:
                                detail = (
                                    "The target window rejected some posted "
                                    "clicks. Try SendInput delivery instead."
                                )

                if remaining > 0:
                    # Only accepted clicks are paid for. The shortfall stays
                    # in the bank and is retried on the next 1 ms slot.
                    bank += remaining

                self._clicks_sent += accepted_total
                self._input_events += accepted_total * 2
                self._send_calls += calls
                self._batches += 1
                self._failed_events += remaining * 2
                if slots > 1:
                    self._catchup_clicks += accepted_total

                if accepted_total:
                    failures = 0
                    if detail:
                        self._last_error = detail
                    elif remaining == 0:
                        # A clean batch clears a message left by an earlier
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
                    time.sleep(0.002)

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

        A high-resolution waitable timer covers most of the gap and the loop
        spins only the last few hundred microseconds, which is the cheapest
        way to hold a 1 ms grid without burning an entire core. Oversleeping
        is harmless: the catch-up slot count repays it on the next turn.
        """
        while True:
            if self._shutdown or self._stop_requested:
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                return True
            if remaining > 0.0005:
                timer.sleep(remaining - 0.0004)
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
