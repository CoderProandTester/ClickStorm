"""High-throughput Windows click engine.

The engine deliberately separates pacing from input delivery:

* A high-resolution pacing loop tracks how many clicks are *due*; overdue
  clicks stay in an accumulator instead of being dropped.
* Clicks are delivered as whole bursts through one ``SendInput`` array call
  per burst, which removes the per-call overhead that caps naive clickers.
* Click credit is only spent on clicks Windows actually accepted, so a run
  stops on the exact target instead of counting rejected input.
* An optional direct-message engine posts mouse messages to the window under
  the cursor for classic Win32 applications.

The result is exact click targets, low timing overhead and much higher usable
click rates than a naive ``click(); sleep(interval)`` loop.
"""

from __future__ import annotations

import math
import queue
import random
import threading
import time
from dataclasses import dataclass

from . import winapi


MIN_CPS = 1.0
# SendInput injects through one system input thread. Benchmarks of native
# clickers put the delivered ceiling at roughly 2,000-4,000 CPS, so 25,000 is
# the highest target worth offering: it lets a fast machine run flat out
# without pretending ten thousand clicks per second is physically possible.
MAX_CPS = 25_000.0
MAX_CLICKS = 25_000
MAX_DURATION = 86_400.0


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

    ARRAY_CACHE_LIMIT = 64
    # Small arrays keep Windows' input queue from backing up. A giant burst
    # makes SendInput block until the target drains the queue, which is what
    # makes a "10,000 CPS" run feel like a few hundred CPS and delays STOP.
    # 25 clicks (50 events) per call is fast; it shrinks to 5 if the queue
    # pushes back and grows back once every event is accepted again.
    SENDINPUT_BATCH_CLICKS = 25
    MIN_SENDINPUT_BATCH_CLICKS = 5
    MAX_SENDINPUT_BATCH_CLICKS = 25
    # A burst holding about this much click credit is delivered per call, so
    # high rates use small arrays instead of one tiny call per click.
    PACING_BURST_SECONDS = 0.004
    # Never chase a stall with a giant catch-up burst. If the target is slow,
    # the run should slow down with it instead of freezing the UI and making
    # stop/duration checks late.
    MAX_PENDING_CLICKS = 40
    # Clicks handled per worker turn before stop/duration/target are re-checked.
    # Delivery still checks stop between individual SendInput calls.
    MAX_CLICKS_PER_TURN = 100
    # Keep retrying through a temporarily swamped input queue (about 0.8 s)
    # before abandoning a run, so a brief stall cannot cut a target short.
    MAX_CONSECUTIVE_FAILURES = 200
    PAUSE_BETWEEN_FAILURES = 0.004

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
        self._last_error = ""

        self._events: queue.Queue[EngineEvent] = queue.Queue()
        self._array_cache: dict[tuple[str, int], object] = {}
        self._timer = winapi.TimerResolution()
        self._direct_target = None
        self._direct_point: tuple[int, int] | None = None
        self._batch_clicks = self.SENDINPUT_BATCH_CLICKS
        self._clean_bursts = 0

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
        self._timer.end()

    def poll_events(self) -> list[EngineEvent]:
        events: list[EngineEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
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

    def _run_session(self) -> None:
        with self._lock:
            config = self._config
            session_id = self._session_id
            self._started_at = time.perf_counter()
            self._finished_at = 0.0

        self._timer.begin()
        if config.performance_mode:
            winapi.set_thread_priority(True)

        reason = "stopped"
        message = ""

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

        self._direct_target = None
        self._direct_point = None
        if config.delivery == "direct":
            point = self._point(config, fixed_x, fixed_y)
            if point is not None:
                self._direct_target = winapi.make_direct_target(
                    point[0], point[1], config.button
                )
                if self._direct_target is not None:
                    winapi.focus_target(self._direct_target)
                self._direct_point = point

        start = time.perf_counter()
        last_tick = start
        accumulator = 0.0
        consecutive_failures = 0
        self._batch_clicks = self.SENDINPUT_BATCH_CLICKS
        self._clean_bursts = 0

        try:
            while True:
                if self._should_stop():
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

                rate = config.cps
                if config.jitter_percent > 0:
                    rate *= 1.0 + random.uniform(
                        -config.jitter_percent, config.jitter_percent
                    )

                # Click credit accrues at the configured rate. Whole clicks
                # stay due until they are actually delivered, so a busy input
                # queue or a late wake-up never silently loses clicks.
                accumulator += rate * max(0.0, now - last_tick)
                last_tick = now
                if accumulator > self.MAX_PENDING_CLICKS:
                    # Drop stale credit instead of firing a huge catch-up burst
                    # at a target that is still draining the previous one.
                    accumulator = float(self.MAX_PENDING_CLICKS)

                # Pace in bursts rather than per click: wait until a small
                # batch of credit exists, then pay one SendInput syscall for
                # the whole batch. This keeps large arrays flowing at high
                # rates instead of one tiny call every millisecond.
                quantum = max(
                    1,
                    min(
                        self._batch_clicks,
                        math.ceil(rate * self.PACING_BURST_SECONDS),
                    ),
                )
                if int(accumulator) < quantum:
                    wait = (quantum - accumulator) / max(rate, 0.001)
                    if not self._sleep_until(now + wait):
                        reason = "stopped"
                        break
                    continue
                due = int(accumulator)

                budget = min(due, self.MAX_CLICKS_PER_TURN)
                if target_clicks is not None:
                    budget = min(budget, target_clicks - self._clicks_sent)
                    if budget <= 0:
                        reason = "target"
                        break

                if config.position_mode == "fixed":
                    winapi.keep_cursor_locked(fixed_x, fixed_y)

                accepted, _error, detail = self._deliver(
                    config, budget, fixed_x, fixed_y
                )
                # Only spend credit on confirmed clicks; anything rejected is
                # still due and is retried on the next turn.
                accumulator -= accepted

                if accepted > 0:
                    consecutive_failures = 0
                    if accepted < budget and detail:
                        with self._lock:
                            self._last_error = detail
                else:
                    consecutive_failures += 1
                    with self._lock:
                        self._last_error = detail
                    if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        reason = "error"
                        message = detail
                        break
                    time.sleep(self.PAUSE_BETWEEN_FAILURES)

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
            if config.performance_mode:
                winapi.reset_thread_priority()
            self._timer.end()

            with self._lock:
                self._running = False
                self._finished_at = time.perf_counter()
                self._state = "error" if reason == "error" else "stopped"
                self._reason = reason
                clicks_sent = self._clicks_sent
                error = self._last_error

            if reason == "error":
                self._events.put(
                    EngineEvent(
                        kind="error",
                        session_id=session_id,
                        reason=reason,
                        message=message or error,
                        clicks_sent=clicks_sent,
                    )
                )
            else:
                self._events.put(
                    EngineEvent(
                        kind="finished",
                        session_id=session_id,
                        reason=reason,
                        message="",
                        clicks_sent=clicks_sent,
                    )
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _deliver(
        self,
        config: EngineConfig,
        click_count: int,
        fixed_x: int,
        fixed_y: int,
    ) -> tuple[int, int, str]:
        """Deliver up to ``click_count`` clicks.

        Returns ``(accepted_clicks, win32_error, detail_message)``. Only
        accepted clicks are reported, so the pacing loop never counts a
        rejected burst as delivered work.
        """
        if config.delivery == "direct":
            return self._deliver_direct(config, click_count, fixed_x, fixed_y)
        return self._deliver_sendinput(config, click_count)

    def _deliver_sendinput(
        self,
        config: EngineConfig,
        click_count: int,
    ) -> tuple[int, int, str]:
        """Deliver a burst with as few SendInput calls as possible."""
        remaining = click_count
        accepted_total = 0
        error = 0
        detail = ""

        while remaining > 0:
            if self._should_stop():
                break
            batch_clicks = min(remaining, self._batch_clicks)
            inputs = self._get_batch(config.button, batch_clicks)
            sent_events, error = winapi.send_inputs(
                inputs, batch_clicks * 2
            )
            accepted = max(0, min(batch_clicks, sent_events // 2))
            failed_events = max(0, batch_clicks * 2 - sent_events)

            # A partially accepted batch can leave a button logically held
            # down. Always release it before continuing.
            if sent_events % 2 == 1:
                winapi.send_button_release(config.button)

            self._record_delivery(
                accepted=accepted,
                requested=batch_clicks,
                input_events=sent_events,
                failed=failed_events,
            )
            accepted_total += accepted
            remaining -= accepted

            # Grow the array size while everything lands; shrink as soon as
            # Windows starts rejecting events so the queue can recover.
            if accepted == batch_clicks:
                self._clean_bursts += 1
                if (
                    self._clean_bursts >= 8
                    and self._batch_clicks < self.MAX_SENDINPUT_BATCH_CLICKS
                ):
                    self._batch_clicks += 5
                    self._clean_bursts = 0
            else:
                self._batch_clicks = max(
                    self.MIN_SENDINPUT_BATCH_CLICKS,
                    self._batch_clicks // 2,
                )
                self._clean_bursts = 0

            if accepted < batch_clicks:
                # Retry the shortfall on the next pacing turn instead of
                # dropping those clicks.
                detail = (
                    "Windows rejected injected input"
                    + (f" (error {error})" if error else "")
                    + ". If the target is running elevated, run AutoClicker "
                    "as administrator."
                )
                break

        return accepted_total, error, detail

    def _deliver_direct(
        self,
        config: EngineConfig,
        click_count: int,
        fixed_x: int,
        fixed_y: int,
    ) -> tuple[int, int, str]:
        """Post click messages to the window under the cursor."""
        point = self._point(config, fixed_x, fixed_y)
        if point is None:
            with self._lock:
                self._failed_events += click_count * 2
                self._last_error = "Could not read the cursor position."
            return 0, 0, self._last_error

        if point != self._direct_point:
            self._direct_target = winapi.make_direct_target(
                point[0], point[1], config.button
            )
            self._direct_point = point

        target = self._direct_target
        if target is None:
            with self._lock:
                self._failed_events += click_count * 2
                self._last_error = "No clickable window was found under the cursor."
            return 0, 0, self._last_error

        accepted = 0
        for _ in range(click_count):
            if self._should_stop():
                break
            if winapi.post_click(target):
                accepted += 1

        self._record_delivery(
            accepted=accepted,
            requested=click_count,
            input_events=accepted * 2,
            failed=(click_count - accepted) * 2,
        )
        detail = ""
        if accepted < click_count:
            detail = (
                "The target window rejected some posted clicks. Try SendInput "
                "delivery instead."
            )
            with self._lock:
                self._last_error = detail
        return accepted, 0, detail

    def _record_delivery(
        self,
        accepted: int,
        requested: int,
        input_events: int,
        failed: int,
    ) -> None:
        with self._lock:
            self._clicks_sent += accepted
            self._clicks_requested += requested
            self._input_events += input_events
            self._send_calls += 1
            self._batches += 1
            self._failed_events += failed

    def _should_stop(self) -> bool:
        # Plain attribute reads are atomic in CPython.  Avoiding a lock here
        # matters because this is called thousands of times per second while
        # the worker is spinning up to a deadline.
        return self._shutdown or self._stop_requested

    def _sleep_until(self, deadline: float) -> bool:
        """Sleep/spin until deadline.  Returns False when a stop is requested."""
        while True:
            if self._should_stop():
                return False
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            # Coarse sleep first, then a very short spin for sub-millisecond
            # accuracy.  timeBeginPeriod(1) is active during the session.
            if remaining > 0.002:
                time.sleep(min(remaining - 0.0015, 0.008))
            else:
                end = time.perf_counter() + remaining
                while time.perf_counter() < end:
                    if self._should_stop():
                        return False

    def _get_batch(self, button: str, click_count: int):
        key = (button, click_count)
        cached = self._array_cache.get(key)
        if cached is not None:
            return cached

        inputs = winapi.build_click_inputs(button, click_count)
        if len(self._array_cache) >= self.ARRAY_CACHE_LIMIT:
            self._array_cache.clear()
        self._array_cache[key] = inputs
        return inputs

    @staticmethod
    def _point(
        config: EngineConfig,
        fixed_x: int,
        fixed_y: int,
    ) -> tuple[int, int] | None:
        if config.position_mode == "fixed":
            return fixed_x, fixed_y
        return winapi.get_cursor_pos()
