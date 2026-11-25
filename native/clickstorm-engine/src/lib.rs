//! ClickStorm native click engine.
//!
//! One engine, two delivery paths:
//!
//! * `SendInput` - real OS input that every application and game accepts.
//!   Windows serialises these events through one system input thread, so the
//!   delivered rate is capped by Windows and the target's message loop.
//! * Posted window messages (`WM_*BUTTON*`) - bypasses the system input queue,
//!   so a classic Win32 window can consume a much higher rate. Games that read
//!   raw input or DirectInput ignore posted messages.
//!
//! The pacing maths (credit against `Instant`/QPC, capped backlog, exact
//! click targets) matches the Python engine that this replaces, but the hot
//! loop is native: no interpreter, no allocator, no garbage collector.
//!
//! The C ABI is versioned; `clickstorm/native_engine.py` is the only caller.

#![allow(non_snake_case)]
// The concise, dependency-free implementation used for the non-Windows
// target is intentionally not the full engine; keep `cargo check` quiet there.
#![cfg_attr(not(windows), allow(dead_code))]

use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const ABI_VERSION: u32 = 1;

const MIN_CPS: f64 = 1.0;
// SendInput cannot reach this, but posted/synchronous window messages skip
// the system input thread, so the target is free to ask for more and let the
// receiver's real ceiling show up in the delivered counter.
const MAX_CPS: f64 = 100_000.0;
const MAX_CLICKS: i64 = 25_000;
const MAX_DURATION: f64 = 86_400.0;

// SendInput hands every event to one system input thread, so wide batches do
// not raise delivered throughput; they only make the call block while the
// target drains its queue. The batch therefore starts at one click and only
// widens while calls stay far cheaper than a pacing slot.
const MAX_CHUNK_CLICKS: usize = 32;
const MIN_CHUNK_CLICKS: usize = 1;
const CHUNK_SLOW: Duration = Duration::from_micros(800);
// Posted messages never touch the input queue, so the direct path can use a
// wide batch per syscall.
const DIRECT_CHUNK_CLICKS: usize = 256;
const MAX_BACKLOG_MS: f64 = 20.0;
const MAX_CONSECUTIVE_FAILURES: u32 = 200;

const STARTUP_UNSET: u64 = u64::MAX;

fn now_ns() -> u64 {
    static BASE: OnceLock<Instant> = OnceLock::new();
    BASE.get_or_init(Instant::now).elapsed().as_nanos() as u64
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

// ---------------------------------------------------------------------------
// C ABI
// ---------------------------------------------------------------------------

/// Engine configuration. Field order and types are mirrored by
/// `_CsConfig` in `clickstorm/native_engine.py`; never reorder without
/// bumping `ABI_VERSION`.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct CsConfig {
    pub abi_version: u32,
    pub cps: f64,
    pub button: u32,
    pub delivery: u32,
    pub limit_mode: u32,
    pub click_limit: i64,
    pub duration: f64,
    pub jitter_percent: f64,
    pub position_mode: u32,
    pub fixed_x: i32,
    pub fixed_y: i32,
    pub pin_window: u32,
    pub performance_mode: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct CsStats {
    pub abi_version: u32,
    pub running: u32,
    pub state: u32,
    pub reason: u32,
    pub clicks_sent: i64,
    pub clicks_requested: i64,
    pub input_events: i64,
    pub send_calls: i64,
    pub batches: i64,
    pub failed_events: i64,
    pub catchup_clicks: i64,
    pub late_ticks: i64,
    pub dropped_clicks: i64,
    pub session_id: i64,
    pub elapsed: f64,
    pub average_cps: f64,
}

pub const STATE_IDLE: u32 = 0;
pub const STATE_RUNNING: u32 = 1;
pub const STATE_STOPPED: u32 = 2;
pub const STATE_ERROR: u32 = 3;

pub const REASON_NONE: u32 = 0;
pub const REASON_STOPPED: u32 = 1;
pub const REASON_TARGET: u32 = 2;
pub const REASON_DURATION: u32 = 3;
pub const REASON_ERROR: u32 = 4;

#[derive(Clone, Copy, Debug, PartialEq)]
enum Button {
    Left,
    Right,
    Middle,
    X1,
    X2,
}

impl Button {
    fn from_code(code: u32) -> Button {
        match code {
            1 => Button::Right,
            2 => Button::Middle,
            3 => Button::X1,
            4 => Button::X2,
            _ => Button::Left,
        }
    }

    fn flags(self) -> (u32, u32, u32) {
        match self {
            Button::Left => (0x0002, 0x0004, 0),
            Button::Right => (0x0008, 0x0010, 0),
            Button::Middle => (0x0020, 0x0040, 0),
            Button::X1 => (0x0080, 0x0100, 1),
            Button::X2 => (0x0080, 0x0100, 2),
        }
    }

    fn messages(self) -> (u32, u32, usize, usize) {
        match self {
            Button::Left => (0x0201, 0x0202, 0x0001, 0),
            Button::Right => (0x0204, 0x0205, 0x0002, 0),
            Button::Middle => (0x0207, 0x0208, 0x0010, 0),
            Button::X1 => (0x020B, 0x020C, 0x0020 | (1 << 16), 1 << 16),
            Button::X2 => (0x020B, 0x020C, 0x0040 | (2 << 16), 2 << 16),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Delivery {
    SendInput,
    Direct,
    /// `SendMessageTimeout`: the target handles the click before the call
    /// returns, so it never waits in the (10,000-message) posted queue.
    Sync,
    Auto,
}

#[derive(Clone, Copy)]
struct Config {
    cps: f64,
    button: Button,
    delivery: Delivery,
    limit_mode: u32,
    click_limit: i64,
    duration: f64,
    jitter: f64,
    fixed: bool,
    fixed_x: i32,
    fixed_y: i32,
    pin_window: bool,
    performance: bool,
}

impl Config {
    fn from_ffi(raw: &CsConfig) -> Config {
        let button = raw.button;
        let delivery = raw.delivery;
        let position = raw.position_mode;
        let limit = raw.limit_mode;
        Config {
            cps: raw.cps.clamp(MIN_CPS, MAX_CPS),
            button: Button::from_code(button),
            delivery: match delivery {
                1 => Delivery::Direct,
                2 => Delivery::Auto,
                3 => Delivery::Sync,
                _ => Delivery::SendInput,
            },
            limit_mode: if limit <= 2 { limit } else { 0 },
            click_limit: raw.click_limit.clamp(1, MAX_CLICKS),
            duration: raw.duration.clamp(0.05, MAX_DURATION),
            jitter: raw.jitter_percent.clamp(0.0, 0.75),
            fixed: position == 1,
            fixed_x: raw.fixed_x,
            fixed_y: raw.fixed_y,
            pin_window: raw.pin_window != 0,
            performance: raw.performance_mode != 0,
        }
    }
}

// ---------------------------------------------------------------------------
// Engine state
// ---------------------------------------------------------------------------

struct Counters {
    clicks_sent: AtomicI64,
    clicks_requested: AtomicI64,
    input_events: AtomicI64,
    send_calls: AtomicI64,
    batches: AtomicI64,
    failed_events: AtomicI64,
    catchup_clicks: AtomicI64,
    late_ticks: AtomicI64,
    dropped_clicks: AtomicI64,
}

impl Counters {
    fn new() -> Counters {
        Counters {
            clicks_sent: AtomicI64::new(0),
            clicks_requested: AtomicI64::new(0),
            input_events: AtomicI64::new(0),
            send_calls: AtomicI64::new(0),
            batches: AtomicI64::new(0),
            failed_events: AtomicI64::new(0),
            catchup_clicks: AtomicI64::new(0),
            late_ticks: AtomicI64::new(0),
            dropped_clicks: AtomicI64::new(0),
        }
    }

    fn reset(&self) {
        for counter in [
            &self.clicks_sent,
            &self.clicks_requested,
            &self.input_events,
            &self.send_calls,
            &self.batches,
            &self.failed_events,
            &self.catchup_clicks,
            &self.late_ticks,
            &self.dropped_clicks,
        ] {
            counter.store(0, Ordering::Release);
        }
    }
}

/// Shared with the click thread through an `Arc`; the thread never touches the
/// `Engine` box itself, so freeing the handle is safe even while it runs.
struct Inner {
    running: AtomicBool,
    stop: AtomicBool,
    state: AtomicU32,
    reason: AtomicU32,
    session_id: AtomicI64,
    started_ns: AtomicU64,
    finished_ns: AtomicU64,
    counters: Counters,
    error: Mutex<String>,
}

impl Inner {
    fn new() -> Inner {
        Inner {
            running: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            state: AtomicU32::new(STATE_IDLE),
            reason: AtomicU32::new(REASON_NONE),
            session_id: AtomicI64::new(0),
            started_ns: AtomicU64::new(STARTUP_UNSET),
            finished_ns: AtomicU64::new(STARTUP_UNSET),
            counters: Counters::new(),
            error: Mutex::new(String::new()),
        }
    }

    fn fail(&self, message: impl Into<String>) {
        *lock(&self.error) = message.into();
        self.reason.store(REASON_ERROR, Ordering::Release);
        self.state.store(STATE_ERROR, Ordering::Release);
    }
}

pub struct Engine {
    inner: Arc<Inner>,
    thread: Mutex<Option<JoinHandle<()>>>,
    start_lock: Mutex<()>,
}

impl Engine {
    fn new() -> Engine {
        Engine {
            inner: Arc::new(Inner::new()),
            thread: Mutex::new(None),
            start_lock: Mutex::new(()),
        }
    }

    fn start(&self, cfg: Config) -> i32 {
        let _guard = lock(&self.start_lock);
        if self.inner.running.load(Ordering::Acquire) {
            return 1;
        }
        {
            let mut slot = lock(&self.thread);
            if let Some(handle) = slot.take() {
                if !handle.is_finished() {
                    // The previous run is still unwinding; do not start a
                    // second clicker next to it.
                    *slot = Some(handle);
                    return 1;
                }
                let _ = handle.join();
            }
        }

        self.inner.counters.reset();
        *lock(&self.inner.error) = String::new();
        self.inner.stop.store(false, Ordering::Release);
        self.inner.running.store(true, Ordering::Release);
        self.inner.reason.store(REASON_NONE, Ordering::Release);
        self.inner.state.store(STATE_RUNNING, Ordering::Release);
        self.inner.started_ns.store(now_ns(), Ordering::Release);
        self.inner.finished_ns.store(STARTUP_UNSET, Ordering::Release);
        self.inner.session_id.fetch_add(1, Ordering::AcqRel);

        let inner = Arc::clone(&self.inner);
        let handle = thread::Builder::new()
            .name("ClickStormNative".to_string())
            .spawn(move || {
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    run_session(Arc::clone(&inner), cfg);
                }));
                if result.is_err() {
                    inner.fail("Native click engine panicked");
                }
                inner.running.store(false, Ordering::Release);
                inner.finished_ns.store(now_ns(), Ordering::Release);
                if inner.reason.load(Ordering::Acquire) != REASON_ERROR {
                    inner.state.store(STATE_STOPPED, Ordering::Release);
                }
            });
        match handle {
            Ok(handle) => {
                *lock(&self.thread) = Some(handle);
                0
            }
            Err(exc) => {
                self.inner.running.store(false, Ordering::Release);
                self.inner.fail(format!("Could not start the click thread: {exc}"));
                2
            }
        }
    }

    fn stop(&self) {
        self.inner.stop.store(true, Ordering::Release);
    }

    fn stats(&self) -> CsStats {
        let now = now_ns();
        let started = self.inner.started_ns.load(Ordering::Acquire);
        let finished = self.inner.finished_ns.load(Ordering::Acquire);
        let running = self.inner.running.load(Ordering::Acquire);
        let elapsed = if started == STARTUP_UNSET {
            0.0
        } else {
            let end = if running || finished == STARTUP_UNSET { now } else { finished };
            end.saturating_sub(started) as f64 / 1_000_000_000.0
        };
        let clicks = self.inner.counters.clicks_sent.load(Ordering::Acquire);
        CsStats {
            abi_version: ABI_VERSION,
            running: u32::from(running),
            state: self.inner.state.load(Ordering::Acquire),
            reason: self.inner.reason.load(Ordering::Acquire),
            clicks_sent: clicks,
            clicks_requested: self.inner.counters.clicks_requested.load(Ordering::Acquire),
            input_events: self.inner.counters.input_events.load(Ordering::Acquire),
            send_calls: self.inner.counters.send_calls.load(Ordering::Acquire),
            batches: self.inner.counters.batches.load(Ordering::Acquire),
            failed_events: self.inner.counters.failed_events.load(Ordering::Acquire),
            catchup_clicks: self.inner.counters.catchup_clicks.load(Ordering::Acquire),
            late_ticks: self.inner.counters.late_ticks.load(Ordering::Acquire),
            dropped_clicks: self.inner.counters.dropped_clicks.load(Ordering::Acquire),
            session_id: self.inner.session_id.load(Ordering::Acquire),
            elapsed,
            average_cps: if elapsed > 0.0005 { clicks as f64 / elapsed } else { 0.0 },
        }
    }
}

// ---------------------------------------------------------------------------
// FFI entry points
// ---------------------------------------------------------------------------

#[no_mangle]
pub extern "C" fn cs_engine_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub extern "C" fn cs_engine_new() -> *mut Engine {
    Box::into_raw(Box::new(Engine::new()))
}

#[no_mangle]
pub unsafe extern "C" fn cs_engine_free(engine: *mut Engine) {
    if engine.is_null() {
        return;
    }
    // The click thread holds an Arc<Inner> and never dereferences the Engine
    // box, so dropping it here is safe: `stop` ends the thread shortly after.
    let boxed = Box::from_raw(engine);
    boxed.stop();
    drop(boxed);
}

#[no_mangle]
pub unsafe extern "C" fn cs_engine_start(engine: *mut Engine, raw: *const CsConfig) -> i32 {
    if engine.is_null() || raw.is_null() {
        return 2;
    }
    let engine = &*engine;
    let raw = &*raw;
    if raw.abi_version != ABI_VERSION {
        engine.inner.fail(format!(
            "Engine ABI mismatch: app sent {}, engine speaks {}",
            raw.abi_version, ABI_VERSION
        ));
        return 2;
    }
    engine.start(Config::from_ffi(raw))
}

#[no_mangle]
pub unsafe extern "C" fn cs_engine_stop(engine: *mut Engine) {
    if let Some(engine) = engine.as_ref() {
        engine.stop();
    }
}

#[no_mangle]
pub unsafe extern "C" fn cs_engine_stats(engine: *mut Engine, out: *mut CsStats) -> i32 {
    if engine.is_null() || out.is_null() {
        return 2;
    }
    *out = (&*engine).stats();
    0
}

/// Copy the last error as UTF-8 into `buf` (no terminator). Returns the byte
/// length, so the caller can grow its buffer when the return exceeds `cap`.
#[no_mangle]
pub unsafe extern "C" fn cs_engine_last_error(
    engine: *mut Engine,
    buf: *mut u8,
    cap: usize,
) -> usize {
    let Some(engine) = engine.as_ref() else {
        return 0;
    };
    let message = lock(&engine.inner.error);
    let bytes = message.as_bytes();
    let copy = bytes.len().min(cap);
    if !buf.is_null() && copy > 0 {
        std::ptr::copy_nonoverlapping(bytes.as_ptr(), buf, copy);
    }
    bytes.len()
}

// ---------------------------------------------------------------------------
// Session (platform independent shell)
// ---------------------------------------------------------------------------

fn run_session(inner: Arc<Inner>, cfg: Config) {
    let outcome = platform::run(&inner, cfg);
    match outcome {
        Ok(reason) => {
            inner.reason.store(reason, Ordering::Release);
            if reason == REASON_ERROR {
                inner.state.store(STATE_ERROR, Ordering::Release);
            }
        }
        Err(message) => inner.fail(message),
    }
}

// ---------------------------------------------------------------------------
// Windows implementation
// ---------------------------------------------------------------------------

#[cfg(windows)]
mod platform {
    use super::*;
    use std::ffi::c_void;

    type Handle = *mut c_void;
    type Bool = i32;
    type Uint = u32;
    type Dword = u32;
    type Long = i32;
    type UlongPtr = usize;
    type Wparam = usize;
    type Lparam = isize;

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct MouseInput {
        dx: Long,
        dy: Long,
        mouse_data: Dword,
        flags: Dword,
        time: Dword,
        extra_info: UlongPtr,
    }

    /// `INPUT` for a mouse event. On x64 this is exactly 40 bytes: 4 bytes of
    /// `type`, 4 bytes of padding, then the 32-byte MOUSEINPUT. The Python
    /// layer asserts the same size at runtime.
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Input {
        kind: Dword,
        mi: MouseInput,
    }

    #[repr(C)]
    struct Point {
        x: Long,
        y: Long,
    }

    #[repr(C)]
    struct PowerThrottlingState {
        version: Uint,
        control_mask: Uint,
        state_mask: Uint,
    }

    #[link(name = "user32")]
    extern "system" {
        fn SendInput(count: Uint, inputs: *const Input, size: i32) -> Uint;
        fn GetCursorPos(point: *mut Point) -> Bool;
        fn SetCursorPos(x: i32, y: i32) -> Bool;
        fn WindowFromPoint(point: Point) -> isize;
        fn ChildWindowFromPointEx(hwnd: isize, point: Point, flags: Uint) -> isize;
        fn ScreenToClient(hwnd: isize, point: *mut Point) -> Bool;
        fn GetAncestor(hwnd: isize, flags: Uint) -> isize;
        fn PostMessageW(hwnd: isize, msg: Uint, wparam: Wparam, lparam: Lparam) -> Bool;
        fn SetForegroundWindow(hwnd: isize) -> Bool;
        fn GetWindowThreadProcessId(hwnd: isize, pid: *mut Dword) -> Dword;
        fn SendMessageTimeoutW(
            hwnd: isize,
            msg: Uint,
            wparam: Wparam,
            lparam: Lparam,
            flags: Uint,
            timeout: Uint,
            result: *mut usize,
        ) -> isize;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentThread() -> Handle;
        fn GetCurrentProcess() -> Handle;
        fn GetCurrentProcessId() -> Dword;
        fn SetThreadPriority(thread: Handle, priority: i32) -> Bool;
        fn SetPriorityClass(process: Handle, class: Dword) -> Bool;
        fn SwitchToThread() -> Bool;
        fn SetThreadInformation(
            thread: Handle,
            class: i32,
            info: *mut c_void,
            size: Dword,
        ) -> Bool;
        fn CreateWaitableTimerExW(
            attributes: *mut c_void,
            name: *const u16,
            flags: Dword,
            access: Dword,
        ) -> Handle;
        fn SetWaitableTimer(
            timer: Handle,
            due: *const i64,
            period: i32,
            routine: *mut c_void,
            arg: *mut c_void,
            resume: Bool,
        ) -> Bool;
        fn WaitForSingleObject(handle: Handle, millis: Dword) -> Dword;
        fn CloseHandle(handle: Handle) -> Bool;
        fn SetLastError(code: Dword);
        fn GetLastError() -> Dword;
    }

    #[link(name = "winmm")]
    extern "system" {
        fn timeBeginPeriod(period: Uint) -> Uint;
        fn timeEndPeriod(period: Uint) -> Uint;
    }

    const INPUT_MOUSE: Dword = 0;
    const CWP_SKIPINVISIBLE: Uint = 0x0001;
    const CWP_SKIPDISABLED: Uint = 0x0002;
    const CWP_SKIPTRANSPARENT: Uint = 0x0004;
    const GA_ROOT: Uint = 2;
    const THREAD_PRIORITY_TIME_CRITICAL: i32 = 15;
    const THREAD_PRIORITY_NORMAL: i32 = 0;
    const HIGH_PRIORITY_CLASS: Dword = 0x0000_0080;
    const NORMAL_PRIORITY_CLASS: Dword = 0x0000_0020;
    const CREATE_WAITABLE_TIMER_HIGH_RESOLUTION: Dword = 0x0000_0002;
    const TIMER_ALL_ACCESS: Dword = 0x001F_0003;
    const INFINITE: Dword = 0xFFFF_FFFF;
    const THREAD_INFORMATION_CLASS_POWER_THROTTLING: i32 = 1;
    const THREAD_POWER_THROTTLING_CURRENT_VERSION: Uint = 1;
    const THREAD_POWER_THROTTLING_EXECUTION_SPEED: Uint = 0x1;
    // Block while waiting, with the timeout as the only escape. SMTO_ABORTIFHUNG
    // is deliberately not used: it can return zero without setting
    // ERROR_TIMEOUT, which would make an unprocessed click look successful.
    const SMTO_BLOCK: Uint = 0x0001;
    const ERROR_TIMEOUT: Dword = 1460;

    const YIELD_SECONDS: f64 = 0.002;
    const SPIN_SECONDS: f64 = 0.000_050;

    pub fn run(inner: &Arc<Inner>, cfg: Config) -> Result<u32, String> {
        unsafe {
            let timer = Timer::new();

            timeBeginPeriod(1);
            let thread = GetCurrentThread();
            SetThreadPriority(thread, THREAD_PRIORITY_TIME_CRITICAL);
            if cfg.performance {
                SetPriorityClass(GetCurrentProcess(), HIGH_PRIORITY_CLASS);
                let state = PowerThrottlingState {
                    version: THREAD_POWER_THROTTLING_CURRENT_VERSION,
                    control_mask: THREAD_POWER_THROTTLING_EXECUTION_SPEED,
                    state_mask: 0,
                };
                SetThreadInformation(
                    thread,
                    THREAD_INFORMATION_CLASS_POWER_THROTTLING,
                    &state as *const PowerThrottlingState as *mut c_void,
                    std::mem::size_of::<PowerThrottlingState>() as Dword,
                );
            }

            let result = run_hot(inner, cfg, &timer);

            if cfg.performance {
                SetThreadPriority(thread, THREAD_PRIORITY_NORMAL);
                SetPriorityClass(GetCurrentProcess(), NORMAL_PRIORITY_CLASS);
            }
            timeEndPeriod(1);
            result
        }
    }

    unsafe fn run_hot(inner: &Arc<Inner>, cfg: Config, timer: &Timer) -> Result<u32, String> {
        let (down_flag, up_flag, data) = cfg.button.flags();
        let mut input_array = vec![
            Input {
                kind: INPUT_MOUSE,
                mi: MouseInput {
                    dx: 0,
                    dy: 0,
                    mouse_data: data,
                    flags: down_flag,
                    time: 0,
                    extra_info: 0,
                },
            };
            MAX_CHUNK_CLICKS * 2
        ];
        for index in 0..MAX_CHUNK_CLICKS {
            input_array[index * 2].mi = MouseInput {
                dx: 0,
                dy: 0,
                mouse_data: data,
                flags: down_flag,
                time: 0,
                extra_info: 0,
            };
            input_array[index * 2 + 1].mi = MouseInput {
                dx: 0,
                dy: 0,
                mouse_data: data,
                flags: up_flag,
                time: 0,
                extra_info: 0,
            };
        }
        let release = Input {
            kind: INPUT_MOUSE,
            mi: MouseInput {
                dx: 0,
                dy: 0,
                mouse_data: data,
                flags: up_flag,
                time: 0,
                extra_info: 0,
            },
        };

        if cfg.fixed {
            let (x, y) = clamp_to_screen(cfg.fixed_x, cfg.fixed_y);
            SetCursorPos(x, y);
        }

        let mut delivery = cfg.delivery;
        let mut target = None;
        let mut point = None;

        if delivery == Delivery::Auto {
            // Prefer synchronous delivery above the SendInput ceiling: it
            // registers each click instead of letting a posted queue drop
            // the surplus.
            delivery = if cfg.cps > 6_000.0 { Delivery::Sync } else { Delivery::SendInput };
        }

        if matches!(delivery, Delivery::Direct | Delivery::Sync) {
            if let Some(at) = current_point(cfg) {
                point = Some(at);
                target = resolve_target(at.0, at.1, cfg.button);
                if target.is_none() && cfg.delivery == Delivery::Auto {
                    // Nothing to post to under the cursor. AUTO must still
                    // click, so fall back to the universally accepted path.
                    delivery = Delivery::SendInput;
                } else if let Some(target) = target {
                    focus_target(target.hwnd);
                }
            }
            if matches!(delivery, Delivery::Direct | Delivery::Sync) && target.is_none() {
                return Err(
                    "Message delivery found no clickable window under the cursor. Move \
                     the cursor over the target window (not ClickStorm) and start again."
                        .to_string(),
                );
            }
        }

        let use_send_input = delivery == Delivery::SendInput;
        let mut chunk = if use_send_input {
            MIN_CHUNK_CLICKS
        } else {
            let per_slot = (cfg.cps / 1000.0).round() as usize;
            per_slot.clamp(1, DIRECT_CHUNK_CLICKS)
        };
        let chunk_limit = if use_send_input {
            MAX_CHUNK_CLICKS
        } else {
            DIRECT_CHUNK_CLICKS
        };

        let cps = cfg.cps;
        let backlog_cap = (cps / 1000.0 * MAX_BACKLOG_MS).max(1.0);
        let mut credit = 0.0_f64;
        let mut rng: u64 = 0x9E37_79B9_7F4A_7C15 ^ now_ns();
        let mut failures = 0_u32;
        let mut clean_bursts = 0_u32;
        let mut last = Instant::now();
        let start = last;
        let duration = if cfg.limit_mode == 2 { Some(cfg.duration) } else { None };
        let target_clicks = if cfg.limit_mode == 1 { Some(cfg.click_limit) } else { None };

        loop {
            if inner.stop.load(Ordering::Acquire) {
                return Ok(REASON_STOPPED);
            }

            let now = Instant::now();
            let delta = now.duration_since(last).as_secs_f64();
            last = now;

            if let Some(limit) = duration {
                if now.duration_since(start).as_secs_f64() >= limit {
                    return Ok(REASON_DURATION);
                }
            }
            let sent_so_far = inner.counters.clicks_sent.load(Ordering::Relaxed);
            if let Some(limit) = target_clicks {
                if sent_so_far >= limit {
                    return Ok(REASON_TARGET);
                }
            }

            let late = delta > 0.002;
            if late {
                inner.counters.late_ticks.fetch_add(1, Ordering::Relaxed);
            }

            let mut gain = cps * delta;
            if cfg.jitter > 0.0 {
                // xorshift64*; deterministic, allocation-free and good enough
                // for timing jitter.
                rng ^= rng >> 12;
                rng ^= rng << 25;
                rng ^= rng >> 27;
                let unit = (rng.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 11) as f64
                    / (1u64 << 53) as f64;
                gain *= 1.0 + (unit * 2.0 - 1.0) * cfg.jitter;
            }
            credit += gain;
            if credit > backlog_cap {
                let overflow = (credit - backlog_cap) as i64;
                if overflow > 0 {
                    inner.counters.dropped_clicks.fetch_add(overflow, Ordering::Relaxed);
                }
                credit = backlog_cap;
            }

            if credit < 1.0 {
                let wait = (1.0 - credit) / cps;
                let sleep_from = Instant::now();
                if !wait_until(inner, sleep_from + Duration::from_secs_f64(wait), timer) {
                    return Ok(REASON_STOPPED);
                }
                let resumed = Instant::now();
                credit += cps * resumed.duration_since(sleep_from).as_secs_f64();
                if credit > backlog_cap {
                    credit = backlog_cap;
                }
                last = resumed;
                continue;
            }

            let mut due = credit as i64;
            if due > chunk as i64 {
                due = chunk as i64;
            }
            if let Some(limit) = target_clicks {
                let left = limit - sent_so_far;
                if due > left {
                    due = left;
                }
                if due <= 0 {
                    return Ok(REASON_TARGET);
                }
            }
            inner.counters.clicks_requested.fetch_add(due, Ordering::Relaxed);

            if cfg.fixed && !cfg.pin_window {
                let (x, y) = clamp_to_screen(cfg.fixed_x, cfg.fixed_y);
                keep_cursor_locked(x, y);
            }

            let call_start = Instant::now();
            let mut accepted = 0_i64;
            let mut detail = String::new();

            if use_send_input {
                let events = (due as usize) * 2;
                let sent = SendInput(events as Uint, input_array.as_ptr(), std::mem::size_of::<Input>() as i32);
                accepted = (sent / 2) as i64;
                if accepted > due {
                    accepted = due;
                }
                if sent % 2 != 0 {
                    SendInput(1, &release, std::mem::size_of::<Input>() as i32);
                }
                if accepted < due {
                    detail = "Windows rejected injected input. If the target is running \
                              elevated, run ClickStorm as administrator."
                        .to_string();
                }
            } else {
                let at = if cfg.pin_window {
                    point
                } else {
                    current_point(cfg)
                };
                match at {
                    None => detail = "Could not read the cursor position.".to_string(),
                    Some((x, y)) => {
                        if !cfg.pin_window && point != Some((x, y)) {
                            target = resolve_target(x, y, cfg.button);
                            point = Some((x, y));
                            if let Some(t) = target {
                                focus_target(t.hwnd);
                            }
                        }
                        match target {
                            None => {
                                detail = "No clickable window was found under the cursor."
                                    .to_string()
                            }
                            Some(t) => {
                                accepted = if delivery == Delivery::Sync {
                                    sync_clicks(t, due, 20)
                                } else {
                                    post_clicks(t, due)
                                };
                                if accepted < due {
                                    detail = if delivery == Delivery::Sync {
                                        "The target window timed out or rejected some \
                                         synchronous clicks."
                                            .to_string()
                                    } else {
                                        "The target window rejected some posted clicks. \
                                         Try SendInput delivery instead."
                                            .to_string()
                                    };
                                }
                            }
                        }
                    }
                }
            }

            let call_seconds = call_start.elapsed();
            credit -= accepted as f64;
            inner.counters.clicks_sent.fetch_add(accepted, Ordering::Relaxed);
            inner.counters.input_events.fetch_add(accepted * 2, Ordering::Relaxed);
            inner.counters.send_calls.fetch_add(1, Ordering::Relaxed);
            inner.counters.batches.fetch_add(1, Ordering::Relaxed);
            inner
                .counters
                .failed_events
                .fetch_add((due - accepted) * 2, Ordering::Relaxed);
            if late {
                inner.counters.catchup_clicks.fetch_add(accepted, Ordering::Relaxed);
            }

            if accepted >= due {
                failures = 0;
                if detail.is_empty() {
                    *lock(&inner.error) = String::new();
                } else {
                    *lock(&inner.error) = detail;
                }
                if call_seconds < CHUNK_SLOW {
                    clean_bursts += 1;
                    if clean_bursts >= 16 && chunk < chunk_limit {
                        chunk = (chunk * 2).min(chunk_limit);
                        clean_bursts = 0;
                    }
                } else {
                    clean_bursts = 0;
                    if chunk > MIN_CHUNK_CLICKS {
                        chunk = (chunk / 2).max(MIN_CHUNK_CLICKS);
                    }
                }
            } else {
                failures += 1;
                clean_bursts = 0;
                if chunk > MIN_CHUNK_CLICKS {
                    chunk = (chunk / 2).max(MIN_CHUNK_CLICKS);
                }
                if !detail.is_empty() {
                    *lock(&inner.error) = detail.clone();
                }
                if accepted == 0 {
                    if failures >= MAX_CONSECUTIVE_FAILURES {
                        return Err(if detail.is_empty() {
                            "Windows rejected every injected click.".to_string()
                        } else {
                            detail
                        });
                    }
                    SwitchToThread();
                    if failures > 4 {
                        thread::sleep(Duration::from_micros(250));
                    }
                }
            }
        }
    }

    unsafe fn wait_until(inner: &Arc<Inner>, deadline: Instant, timer: &Timer) -> bool {
        loop {
            if inner.stop.load(Ordering::Acquire) {
                return false;
            }
            let now = Instant::now();
            if now >= deadline {
                return true;
            }
            let remaining = deadline.duration_since(now).as_secs_f64();
            if remaining > YIELD_SECONDS {
                // Cap the block so STOP is still honoured within ~10 ms.
                timer.sleep(Duration::from_secs_f64((remaining - YIELD_SECONDS + 0.0002).min(0.010)));
            } else if remaining > SPIN_SECONDS {
                SwitchToThread();
            } else {
                std::hint::spin_loop();
            }
        }
    }

    fn current_point(cfg: Config) -> Option<(i32, i32)> {
        if cfg.fixed {
            return Some(clamp_to_screen(cfg.fixed_x, cfg.fixed_y));
        }
        unsafe {
            let mut point = Point { x: 0, y: 0 };
            if GetCursorPos(&mut point) != 0 {
                Some((point.x, point.y))
            } else {
                None
            }
        }
    }

    fn clamp_to_screen(x: i32, y: i32) -> (i32, i32) {
        // Multi-monitor desktops can start at negative coordinates; without
        // GetSystemMetrics here the clamp would be wrong for those layouts,
        // so only clamp the obvious extremes.
        (x.clamp(-100_000, 100_000), y.clamp(-100_000, 100_000))
    }

    unsafe fn keep_cursor_locked(x: i32, y: i32) {
        let mut point = Point { x: 0, y: 0 };
        if GetCursorPos(&mut point) != 0
            && (point.x - x).abs() <= 1
            && (point.y - y).abs() <= 1
        {
            return;
        }
        SetCursorPos(x, y);
    }

    #[derive(Clone, Copy)]
    struct Target {
        hwnd: isize,
        x: i32,
        y: i32,
        button: Button,
    }

    unsafe fn resolve_target(x: i32, y: i32, button: Button) -> Option<Target> {
        let hwnd = WindowFromPoint(Point { x, y });
        if hwnd == 0 {
            return None;
        }
        let mut current = hwnd;
        for _ in 0..10 {
            let mut point = Point { x, y };
            if ScreenToClient(current, &mut point) == 0 {
                break;
            }
            let child = ChildWindowFromPointEx(
                current,
                point,
                CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT,
            );
            if child == 0 || child == current {
                break;
            }
            current = child;
        }

        let mut pid: Dword = 0;
        GetWindowThreadProcessId(current, &mut pid);
        if pid == GetCurrentProcessId() {
            // Never post clicks into ClickStorm's own controls.
            return None;
        }

        let mut point = Point { x, y };
        if ScreenToClient(current, &mut point) == 0 {
            return None;
        }
        Some(Target {
            hwnd: current,
            x: point.x,
            y: point.y,
            button,
        })
    }

    unsafe fn focus_target(hwnd: isize) {
        let root = GetAncestor(hwnd, GA_ROOT);
        SetForegroundWindow(if root != 0 { root } else { hwnd });
    }

    /// Deliver clicks synchronously: the target's window procedure handles
    /// each message before `SendMessageTimeoutW` returns, so nothing sits in
    /// the posted-message queue and no click is lost to queue pressure.
    /// A timeout (rather than a zero result) means the window did not process
    /// it; in that case the release is posted so the button cannot latch.
    unsafe fn sync_clicks(target: Target, count: i64, timeout_ms: Uint) -> i64 {
        if count <= 0 {
            return 0;
        }
        let (down_msg, up_msg, down_wparam, up_wparam) = target.button.messages();
        let lparam =
            ((((target.y as u32) & 0xFFFF) << 16) | ((target.x as u32) & 0xFFFF)) as Lparam;
        let flags = SMTO_BLOCK;
        let mut result: usize = 0;
        let mut accepted = 0;
        for _ in 0..count {
            SetLastError(0);
            let down = SendMessageTimeoutW(
                target.hwnd,
                down_msg,
                down_wparam,
                lparam,
                flags,
                timeout_ms,
                &mut result,
            );
            if down == 0 && GetLastError() == ERROR_TIMEOUT {
                break;
            }
            SetLastError(0);
            let up = SendMessageTimeoutW(
                target.hwnd,
                up_msg,
                up_wparam,
                lparam,
                flags,
                timeout_ms,
                &mut result,
            );
            if up == 0 && GetLastError() == ERROR_TIMEOUT {
                PostMessageW(target.hwnd, up_msg, up_wparam, lparam);
                break;
            }
            accepted += 1;
        }
        accepted
    }

    unsafe fn post_clicks(target: Target, count: i64) -> i64 {
        if count <= 0 {
            return 0;
        }
        let (down_msg, up_msg, down_wparam, up_wparam) = target.button.messages();
        let lparam = ((((target.y as u32) & 0xFFFF) << 16) | ((target.x as u32) & 0xFFFF)) as Lparam;
        let hwnd = target.hwnd;
        let mut accepted = 0;
        for _ in 0..count {
            if PostMessageW(hwnd, down_msg, down_wparam, lparam) == 0 {
                break;
            }
            if PostMessageW(hwnd, up_msg, up_wparam, lparam) == 0 {
                // Never leave the button latched down.
                PostMessageW(hwnd, up_msg, up_wparam, lparam);
                break;
            }
            accepted += 1;
        }
        accepted
    }

    struct Timer {
        handle: Handle,
    }

    impl Timer {
        fn new() -> Timer {
            let handle = unsafe {
                CreateWaitableTimerExW(
                    std::ptr::null_mut(),
                    std::ptr::null(),
                    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                    TIMER_ALL_ACCESS,
                )
            };
            Timer { handle }
        }

        fn sleep(&self, duration: Duration) {
            if duration.is_zero() {
                return;
            }
            if self.handle.is_null() {
                thread::sleep(duration);
                return;
            }
            let due = -((duration.as_nanos() / 100) as i64).max(1);
            unsafe {
                if SetWaitableTimer(
                    self.handle,
                    &due,
                    0,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    0,
                ) == 0
                {
                    thread::sleep(duration);
                    return;
                }
                WaitForSingleObject(self.handle, INFINITE);
            }
        }
    }

    impl Drop for Timer {
        fn drop(&mut self) {
            if !self.handle.is_null() {
                unsafe {
                    CloseHandle(self.handle);
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Non-Windows stub
// ---------------------------------------------------------------------------

#[cfg(not(windows))]
mod platform {
    use super::*;

    pub fn run(_inner: &Arc<Inner>, _cfg: Config) -> Result<u32, String> {
        Err("The native click engine is Windows-only; the Python engine is used here.".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_is_clamped() {
        let raw = CsConfig {
            abi_version: ABI_VERSION,
            cps: 1_000_000.0,
            button: 9,
            delivery: 7,
            limit_mode: 9,
            click_limit: 0,
            duration: -4.0,
            jitter_percent: 4.0,
            position_mode: 3,
            fixed_x: 0,
            fixed_y: 0,
            pin_window: 1,
            performance_mode: 1,
        };
        let cfg = Config::from_ffi(&raw);
        assert_eq!(cfg.cps, MAX_CPS);
        assert_eq!(cfg.button, Button::Left);
        assert_eq!(cfg.delivery, Delivery::SendInput);
        assert_eq!(cfg.limit_mode, 0);
        assert_eq!(cfg.click_limit, 1);
        assert_eq!(cfg.duration, 0.05);
        assert_eq!(cfg.jitter, 0.75);
        assert!(!cfg.fixed);
    }

    #[test]
    fn struct_layout_matches_abi() {
        // The Python ctypes mirror asserts the same numbers.
        assert_eq!(std::mem::size_of::<CsConfig>(), 80);
        assert_eq!(std::mem::size_of::<CsStats>(), 112);
    }

    #[test]
    fn a_new_engine_is_idle() {
        let engine = Engine::new();
        let stats = engine.stats();
        assert_eq!(stats.running, 0);
        assert_eq!(stats.state, STATE_IDLE);
        assert_eq!(stats.clicks_sent, 0);
    }
}
