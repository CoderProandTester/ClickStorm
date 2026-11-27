"""High-throughput Windows click engine.

Pacing model
------------
The configured rate is a per-second target.  One second is 1,000 ms, so the
engine works in clicks per millisecond (``cps / 1000``): 5,000 CPS is 5
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
SYNC and SENDINPUT both inject real mouse input with ``SendInput``, so every
application and game registers them.  Both send a batch of clicks in one
call, because SendInput charges far more for a call than for an event.  SYNC
starts at one complete click per call and widens to three milliseconds of
clicks once calls stay clean; SENDINPUT fills a six millisecond budget (36
clicks at 6,000 CPS, about 167 calls a second instead of 6,000).  A slow call
is never a reason to send fewer clicks -- that is what used to cap delivery
near 2,000 CPS.

Backpressure
------------
A busy target is not a failure.  ``SendInput`` inserts only the events that
fit in the system input queue and ``PostMessage`` fails once a window's 10,000
message queue is full, so both paths see shortfalls while the target catches
up.  The shortfall stays owed, the batch is narrowed to exactly what the
target accepted, and the loop backs off for at most a millisecond: delivery
continues at whatever rate the target really drains.  Only a run where
*nothing* is accepted for ``STALL_SECONDS`` ends, with a message that says
which queue refused and what to use instead.

DIRECT is the default and the fastest path: it posts ``WM_*BUTTON*`` to the
window under the cursor.  Posted messages skip the system input queue, but
applications that read raw input (most games) ignore them and Windows caps
the posted queue at 10,000 messages, so DIRECT suits classic Win32 windows
that answer button messages.

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
from dataclasses import dataclass, replace

from . import winapi


MIN_CPS = 1.0
# Windows injects SendInput events through one system input thread and lands
# around 6,000-7,000 delivered CPS before the receiving app becomes the limit,
# so 6,000 is the highest target the app offers and 5,000 is comfortably
# inside what the input thread can deliver.
MAX_CPS = 6_000.0
MAX_CLICKS = 25_000
MAX_DURATION = 86_400.0

# The scheduling grid. One slot is one millisecond, so a CPS value divided by
# 1000 is exactly the number of clicks that belongs to one slot.
TICK_SECONDS = 0.001
# Clicks are never owed further than this many milliseconds of the target
# rate. A slow SendInput call must not build a backlog that later fires as one
# huge batch: that is the slowest possible way to deliver clicks and it makes
# a running session look frozen.
MAX_BACKLOG_MS = 20.0
# Every SendInput call has a fixed cost that dwarfs the per-event cost, so a
# call carries a time budget instead of a single millisecond slot. At 6,000
# CPS the 6 ms SENDINPUT budget is 36 clicks in one call (about 167 calls a
# second); SYNC starts at one click a call and widens to a 3 ms budget (16
# clicks) as the target keeps up. Shrinking the batch when a call is slow is
# what used to collapse delivery to one click per call and cap the peak near
# 2,000 CPS.
MAX_CHUNK_CLICKS = 64
MIN_CHUNK_CLICKS = 1
SENDINPUT_BATCH_SECONDS = 0.006
SYNC_BATCH_SECONDS = 0.003
SYNC_MAX_CLICKS = 16
# Posted window messages never wait for the system input queue, so direct
# delivery can carry a wider batch per call. 64 clicks is about 10 ms at the
# 6,000 CPS ceiling, which keeps STOP responsive while still cutting the
# number of PostMessage loops per second by an order of magnitude.
DIRECT_CHUNK_CLICKS = 64
# Grow the batch only after this many clean calls, and only while the call
# fitted inside the batch's own time budget. Four, not eight: a batch that a
# busy target narrowed is back at full size within a few milliseconds instead
# of spending a hundred calls climbing back up.
CHUNK_GROW_BURSTS = 4
# Wait tiers, in seconds: above YIELD a timer sleeps, between SPIN and YIELD
# the thread yields its timeslice back to the CPU, below SPIN it spins.
# Sleeping down to 0.5 ms keeps the hot thread from burning a core between
# four-millisecond batches; oversleep is harmless because credit repays it.
YIELD_SECONDS = 0.0005
SPIN_SECONDS = 0.00005
# A target that is merely busy is not a failure. SendInput inserts only the
# events that fit in the system input queue and PostMessage fails once a
# window's 10,000-message queue is full, so both paths see shortfalls while
# the target catches up: the click stays owed, the loop backs off and delivery
# continues at whatever rate the target accepts. A run only ends when nothing
# at all has been accepted for this long. Two seconds is long enough for a
# target to load a level or answer a dialog, and short enough to report a
# genuinely blocked target instead of clicking into the void.
STALL_SECONDS = 2.0
# Retry back-off while nothing is accepted: 0.1 ms doubling to 1 ms, so a
# saturated queue is polled rather than spun and a freed slot is picked up
# within a millisecond.
BACKOFF_MIN_SECONDS = 0.0001
BACKOFF_MAX_SECONDS = 0.001


@dataclass(frozen=True)
class EngineConfig:
    cps: float = 500.0
    button: str = "Left"
    limit_mode: str = "unlimited"  # unlimited | clicks | duration
    click_limit: int = 5_000
    duration: float = 10.0
    delivery: str = "direct"  # direct | sync | sendinput
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
        # "auto" was retired when DIRECT became the default: every old AUTO
        # configuration now means DIRECT, the fastest delivered path.
        delivery = (
            "direct"
            if self.delivery == "auto"
            else (
                self.delivery
                if self.delivery in ("sendinput", "direct", "sync")
                else "direct"
            )
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

    backend_name = "Python"

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

        # Built once per session. Every SendInput call reuses this array and
        # simply passes a smaller event count, so the loop never allocates.
        click_array = (
            winapi.build_click_inputs(config.button, MAX_CHUNK_CLICKS)
            if delivery != "direct"
            else None
        )

        cps = config.cps
        cps_per_ms = cps / 1000.0
        # Credit (clicks owed but not delivered yet) never exceeds a few
        # milliseconds of the target rate, so an over-fast target degrades into
        # a steady stream at the machine's real ceiling instead of stalling.
        backlog_cap = max(1.0, cps_per_ms * MAX_BACKLOG_MS)
        jitter = config.jitter_percent
        # A call carries a time budget of clicks, not a single 1 ms slot.
        # SendInput's fixed per-call cost is what capped high rates before:
        # five clicks a call needed 1,000 calls a second, and a call that
        # overran its slot shrank the batch until delivery collapsed to one
        # click a call. A 4 ms budget makes the same rate need ~250 calls.
        sendinput_batch = max(
            MIN_CHUNK_CLICKS,
            min(MAX_CHUNK_CLICKS, int(round(cps * SENDINPUT_BATCH_SECONDS))),
        )
        if click_array is not None:
            if delivery == "sync":
                # SYNC is the compatibility path: it begins with one complete
                # click per call so even a target that dislikes batched
                # DOWN/UP pairs sees every click, then widens only after the
                # target has accepted a run of clean calls.
                chunk_limit = max(
                    MIN_CHUNK_CLICKS,
                    min(SYNC_MAX_CLICKS, int(round(cps * SYNC_BATCH_SECONDS))),
                )
                chunk = MIN_CHUNK_CLICKS
            else:
                # SENDINPUT starts at its full time budget: at 6,000 CPS that
                # is 24 clicks (48 events) per call, about 250 calls a second
                # instead of the 6,000 two-event calls the input thread
                # cannot drain fast enough to reach the target rate.
                chunk_limit = sendinput_batch
                chunk = sendinput_batch
        else:
            # Posted messages skip the input queue, so direct delivery starts
            # at the full time budget as well.
            chunk_limit = DIRECT_CHUNK_CLICKS
            chunk = sendinput_batch
        # A batch can never be bigger than the credit bank. Credit is capped
        # at backlog_cap, so waiting for a batch larger than that waits for
        # clicks the cap refuses to accumulate: the loop sleeps forever and
        # the run stops clicking while it still reports RUNNING. That is what
        # froze low and medium rate runs a few seconds in, just after the
        # batch had doubled its way past the cap (64 clicks against a 40-click
        # bank at 2,000 CPS). Clamping the batch to the bank keeps every wait
        # satisfiable - a slow target is paced, never deadlocked.
        chunk_limit = max(
            MIN_CHUNK_CLICKS, min(chunk_limit, int(backlog_cap))
        )
        chunk = min(chunk, chunk_limit)
        clean_bursts = 0
        zero_streak = 0
        stall_started = 0.0
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

                if credit < chunk:
                    # Nothing is due yet. Wait for a whole batch instead of
                    # waking for every single click: one call per millisecond
                    # slot keeps SendInput's per-call overhead off the hot
                    # path, and at low rates the wait is the next click.
                    wait = (chunk - credit) / cps
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
                    if sent & 1:
                        # A partial batch left the button held down. The
                        # release completes that click, so it counts: rounding
                        # it down sent the same click again and paid for a
                        # second call just to undo it.
                        winapi.send_button_release(config.button)
                        accepted = (sent + 1) >> 1
                    else:
                        accepted = sent >> 1
                    if accepted > due:
                        accepted = due
                    if accepted < due:
                        detail = (
                            "Windows is not accepting injected input"
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
                                    "The target window's message queue is "
                                    "full - it is not processing posted "
                                    "clicks fast enough. Use SENDINPUT or "
                                    "SYNC delivery for this target."
                                )

                call_seconds = time.perf_counter() - call_start

                # Only accepted clicks are paid for: anything the OS or the
                # target left out stays owed and is retried immediately.
                credit -= accepted
                self._clicks_sent += accepted
                self._input_events += accepted * 2
                self._send_calls += 1
                self._batches += 1
                self._failed_events += (due - accepted) * 2
                if late:
                    self._catchup_clicks += accepted

                if accepted >= due:
                    clean_bursts += 1
                    zero_streak = 0
                    stall_started = 0.0
                    # A clean batch clears a message left by an earlier
                    # shortfall so the UI never shows a stale error.
                    self._last_error = ""
                    # Grow only while the call fitted inside the batch's own
                    # time budget: a call that took longer than the clicks it
                    # carried are worth must not grow, but it is never a
                    # reason to shrink (shrink-on-slow was the 2,000 CPS cap).
                    if call_seconds < max(0.001, chunk / cps):
                        if (
                            clean_bursts >= CHUNK_GROW_BURSTS
                            and chunk < chunk_limit
                        ):
                            chunk = min(chunk_limit, chunk * 2)
                            clean_bursts = 0
                    else:
                        clean_bursts = 0
                    continue

                clean_bursts = 0
                if accepted > 0:
                    # Partial call: the input queue (or the window's posted
                    # message queue) only had room for this many clicks. Carry
                    # exactly that many next time. Halving on every partial
                    # call is what sawtoothed SENDINPUT down to a few hundred
                    # CPS: it fell to one click per call and needed a run of
                    # clean calls to climb back to a full batch. The remainder
                    # is already owed, so the next turn sends it at once.
                    chunk = max(MIN_CHUNK_CLICKS, min(chunk_limit, accepted))
                    zero_streak = 0
                    stall_started = 0.0
                    self._last_error = ""
                    continue

                # Nothing got through at all: the queue is full, input is
                # blocked, or the target stopped pumping messages.
                zero_streak += 1
                if chunk > MIN_CHUNK_CLICKS:
                    chunk = max(MIN_CHUNK_CLICKS, chunk // 2)
                message = detail or "Windows rejected every injected click."
                self._last_error = message
                now_call = time.perf_counter()
                if not stall_started:
                    stall_started = now_call
                elif now_call - stall_started >= STALL_SECONDS:
                    reason = "error"
                    message = (
                        f"{message} Nothing has been accepted for "
                        f"{STALL_SECONDS:.0f} s, so the run was stopped."
                    )
                    break
                # Back off instead of spinning: 0.1 ms doubling to 1 ms, so a
                # freed queue slot is picked up within a millisecond while a
                # saturated target is not polled thousands of times a second.
                timer.sleep(
                    min(
                        BACKOFF_MAX_SECONDS,
                        BACKOFF_MIN_SECONDS * (1 << min(zero_streak, 5)),
                    )
                )

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
        high-resolution waitable timer above 0.5 ms, ``SwitchToThread``
        between 0.5 ms and 50 us, and a spin only for the last 50 us.
        Spinning on the last few hundred microseconds instead cost about half
        a core at 1,000 CPS for no accuracy gain. Oversleeping is harmless:
        the credit maths repays it on the next turn.
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


class UltimateEngine:
    """ClickStorm's one engine: the native core plus the Python core.

    Both cores implement the same pacing, batching, limiting and delivery
    contract, so the rest of the app only ever sees a single engine. The
    native Rust core is used whenever its DLL loads; if it refuses to start,
    or errors before it has delivered a meaningful run, the same configuration
    restarts on the Python core under one stable session id, so the UI never
    sees a failed run. An error after real work is still reported normally - a
    fallback must not hide a session that was already clicking.

    The ENGINE setting picks the core: ``Rust`` (the default) wraps the native
    core in this class, and ``Python`` returns :class:`ClickEngine` directly.
    The ENGINE strip reports which core is delivering right now, including an
    automatic fallback from the native core.
    """

    _COUNTERS = (
        "clicks_sent",
        "clicks_requested",
        "input_events",
        "send_calls",
        "batches",
        "failed_events",
        "catchup_clicks",
        "late_ticks",
        "dropped_clicks",
    )

    def __init__(
        self,
        primary,
        fallback_factory=ClickEngine,
        immediate_window: float = 0.75,
        primary_name: str = "Rust",
    ) -> None:
        self._primary = primary
        self._primary_name = primary_name
        self._fallback_factory = fallback_factory
        self._fallback = None
        self._active = primary
        self._using_fallback = False
        self._immediate_window = immediate_window
        self._lock = threading.Lock()
        self._session_id = 0
        self._started = False
        self._config = EngineConfig()
        self._offsets = {name: 0 for name in self._COUNTERS}
        self._elapsed_offset = 0.0
        self._fallback_reason = ""

    # -- public API -----------------------------------------------------

    @property
    def backend_name(self) -> str:
        return "Ultimate"

    @property
    def core_name(self) -> str:
        """The core delivering right now: ``Rust`` or ``Python``."""
        if self._using_fallback or self._primary_name == "Python":
            return "Python"
        return self._primary_name

    @property
    def last_fallback_reason(self) -> str:
        return self._fallback_reason

    def start(self, config: EngineConfig) -> bool:
        with self._lock:
            config = config.normalised()
            self._stop_quietly(self._primary)
            if self._fallback is not None:
                self._stop_quietly(self._fallback)
            self._offsets = {name: 0 for name in self._COUNTERS}
            self._elapsed_offset = 0.0
            self._fallback_reason = ""
            self._using_fallback = False
            self._active = self._primary

            started = False
            try:
                started = bool(self._primary.start(config))
            except Exception as exc:  # a broken DLL must not kill the app
                self._fallback_reason = f"native core failed to start: {exc}"

            if not started:
                if not self._fallback_reason:
                    self._fallback_reason = (
                        f"{self._primary_name} core refused to start"
                    )
                # The primary core never began this session, so its stats
                # still describe an older run and must not be carried over.
                if not self._switch_to_fallback(collect=False):
                    return False
                try:
                    started = bool(self._fallback.start(config))
                except Exception as exc:
                    self._fallback_reason = (
                        f"Python core failed to start: {exc}"
                    )
                    started = False
                if not started:
                    return False

            self._session_id += 1
            self._started = True
            self._config = config
            return True

    def stop(self) -> None:
        try:
            self._active.stop()
        except Exception:
            pass

    def close(self, timeout: float = 1.5) -> None:
        for engine in (self._primary, self._fallback):
            if engine is None:
                continue
            try:
                engine.close(timeout)
            except Exception:
                pass

    def is_running(self) -> bool:
        try:
            return bool(self._active.is_running())
        except Exception:
            return False

    def poll_events(self) -> list[EngineEvent]:
        with self._lock:
            if not self._started:
                return []
            if self._using_fallback:
                return self._rewrite(self._fallback.poll_events())

            try:
                events = self._primary.poll_events()
            except Exception:
                events = []
            for event in events:
                if event.kind == "error" and self._failed_immediately():
                    reason = event.message or "native core errored immediately"
                    if self._switch_to_fallback() and self._fallback.start(
                        self._config
                    ):
                        # The run continues on the Python core under the
                        # same session id; the UI only sees the restored run.
                        self._fallback_reason = reason
                        return []
                    # The fallback could not take over: report the original
                    # native failure rather than pretending the run is alive.
                    return self._rewrite([event])
                return self._rewrite([event])
            return []

    def get_stats(self) -> EngineStats:
        with self._lock:
            stats = self._active.get_stats()
            if not self._using_fallback:
                return replace(stats, session_id=self._session_id)
            elapsed = stats.elapsed + self._elapsed_offset
            total_clicks = stats.clicks_sent + self._offsets["clicks_sent"]
            extra = {
                "clicks_sent": self._offsets["clicks_sent"],
                "clicks_requested": self._offsets["clicks_requested"],
                "input_events": self._offsets["input_events"],
                "send_calls": self._offsets["send_calls"],
                "batches": self._offsets["batches"],
                "failed_events": self._offsets["failed_events"],
                "catchup_clicks": self._offsets["catchup_clicks"],
                "late_ticks": self._offsets["late_ticks"],
                "dropped_clicks": self._offsets["dropped_clicks"],
            }
            return replace(
                stats,
                session_id=self._session_id,
                elapsed=elapsed,
                average_cps=(total_clicks / elapsed if elapsed > 0.0005 else 0.0),
                **{
                    name: getattr(stats, name) + extra[name]
                    for name in self._COUNTERS
                },
            )

    # -- internals ------------------------------------------------------

    def _switch_to_fallback(self, collect: bool = True) -> bool:
        if self._fallback_factory is None:
            return False
        if self._fallback is None:
            try:
                self._fallback = self._fallback_factory()
            except Exception as exc:
                self._fallback_reason = (
                    f"Python fallback could not start: {exc}"
                )
                return False

        stats = None
        if collect:
            try:
                stats = self._primary.get_stats()
            except Exception:
                pass
        if stats is not None:
            for name in self._COUNTERS:
                self._offsets[name] += int(getattr(stats, name))
            self._elapsed_offset += float(stats.elapsed)
        self._stop_quietly(self._primary)
        if self._primary.is_running():
            # Never run two clickers side by side just to recover a run.
            self._fallback_reason = (
                "native core did not stop; refusing to start a second clicker"
            )
            return False
        self._using_fallback = True
        self._active = self._fallback
        return True

    def _failed_immediately(self) -> bool:
        try:
            stats = self._primary.get_stats()
        except Exception:
            return True
        return (
            stats.elapsed < self._immediate_window
            or stats.clicks_sent == 0
        )

    @staticmethod
    def _stop_quietly(engine) -> None:
        try:
            if not engine.is_running():
                return
            engine.stop()
            deadline = time.perf_counter() + 0.35
            while engine.is_running() and time.perf_counter() < deadline:
                time.sleep(0.005)
        except Exception:
            pass

    def _rewrite(self, events: list[EngineEvent]) -> list[EngineEvent]:
        return [
            replace(event, session_id=self._session_id) for event in events
        ]


def make_engine(preference: str = "rust"):
    """Return ClickStorm's click engine for the ENGINE setting.

    ``preference`` is ``"rust"`` (the default) or ``"python"``. Rust wraps
    the native core in :class:`UltimateEngine`, which falls back to the Python
    core automatically when the DLL is missing, cannot start, or a native
    session fails before delivering clicks. Python returns
    :class:`ClickEngine` directly, so the native DLL is never loaded.
    """
    if preference == "python":
        return ClickEngine()

    native = None
    try:
        from .native_engine import create_native_engine

        native = create_native_engine()
    except Exception:
        native = None
    if native is not None:
        return UltimateEngine(
            native,
            fallback_factory=ClickEngine,
            primary_name="Rust",
        )
    return UltimateEngine(
        ClickEngine(),
        fallback_factory=None,
        primary_name="Python",
    )
