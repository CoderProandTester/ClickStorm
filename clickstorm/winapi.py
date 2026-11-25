"""Small, dependency-free Win32 layer used by ClickStorm.

Only the pieces that the clicker actually needs are exposed here.  Keeping
the ctypes declarations in one module makes the input engine easier to test
and keeps the GUI completely free of Win32 details.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# DLLs
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")


# ---------------------------------------------------------------------------
# Types / constants
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

INPUT_MOUSE = 0

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
MK_XBUTTON1 = 0x0020
MK_XBUTTON2 = 0x0040

CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004
GA_ROOT = 2

THREAD_PRIORITY_NORMAL = 0
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15

# CreateWaitableTimerEx flags (Windows 10 1803+): a timer that can wake a
# thread on a sub-millisecond deadline, which keeps the click timeline
# accurate without spinning a whole core for every millisecond.
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
TIMER_ALL_ACCESS = 0x1F0003
INFINITE = 0xFFFFFFFF

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080
NORMAL_PRIORITY_CLASS = 0x00000020


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class LARGE_INTEGER(ctypes.Union):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("ControlMask", wintypes.ULONG),
        ("StateMask", wintypes.ULONG),
    ]


THREAD_POWER_THROTTLING_CURRENT_VERSION = 1
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1
THREAD_INFORMATION_CLASS_POWER_THROTTLING = 1


# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------

user32.SendInput.argtypes = (
    wintypes.UINT,
    ctypes.POINTER(INPUT),
    ctypes.c_int,
)
user32.SendInput.restype = wintypes.UINT

user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
user32.GetCursorPos.restype = wintypes.BOOL

user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL

user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short

user32.WindowFromPoint.argtypes = (POINT,)
user32.WindowFromPoint.restype = wintypes.HWND

user32.ChildWindowFromPointEx.argtypes = (
    wintypes.HWND,
    POINT,
    wintypes.UINT,
)
user32.ChildWindowFromPointEx.restype = wintypes.HWND

user32.ScreenToClient.argtypes = (
    wintypes.HWND,
    ctypes.POINTER(POINT),
)
user32.ScreenToClient.restype = wintypes.BOOL

user32.PostMessageW.argtypes = (
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
user32.PostMessageW.restype = wintypes.BOOL

user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND

user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL

user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int

user32.SystemParametersInfoW.argtypes = (
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(RECT),
    wintypes.UINT,
)
user32.SystemParametersInfoW.restype = wintypes.BOOL

user32.SetProcessDpiAwarenessContext.argtypes = (wintypes.HANDLE,)
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL

kernel32.GetCurrentThread.restype = wintypes.HANDLE
kernel32.SetThreadPriority.argtypes = (wintypes.HANDLE, ctypes.c_int)
kernel32.SetThreadPriority.restype = wintypes.BOOL

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.SetPriorityClass.restype = wintypes.BOOL

kernel32.SwitchToThread.argtypes = ()
kernel32.SwitchToThread.restype = wintypes.BOOL

kernel32.SetThreadInformation.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
)
kernel32.SetThreadInformation.restype = wintypes.BOOL

kernel32.CreateWaitableTimerExW.argtypes = (
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
)
kernel32.CreateWaitableTimerExW.restype = wintypes.HANDLE

kernel32.SetWaitableTimer.argtypes = (
    wintypes.HANDLE,
    ctypes.POINTER(LARGE_INTEGER),
    wintypes.LONG,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
)
kernel32.SetWaitableTimer.restype = wintypes.BOOL

kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

winmm.timeBeginPeriod.argtypes = (wintypes.UINT,)
winmm.timeBeginPeriod.restype = wintypes.UINT
winmm.timeEndPeriod.argtypes = (wintypes.UINT,)
winmm.timeEndPeriod.restype = wintypes.UINT


# ---------------------------------------------------------------------------
# Mouse input
# ---------------------------------------------------------------------------

BUTTONS = {
    "Left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 0),
    "Right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 0),
    "Middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 0),
    "X1": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    "X2": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}

BUTTON_NAMES = tuple(BUTTONS.keys())


def button_spec(button: str):
    """Return (down_flag, up_flag, mouse_data) for a mouse button name."""
    return BUTTONS.get(button, BUTTONS["Left"])


def build_click_inputs(button: str, click_count: int) -> ctypes.Array | None:
    """Build one INPUT array containing `click_count` complete clicks.

    A click is a DOWN followed by an UP, so an N-click array contains 2N
    INPUT structures.  These arrays can be reused for many SendInput calls.
    """
    if click_count <= 0:
        return None

    down_flag, up_flag, mouse_data = button_spec(button)
    inputs = (INPUT * (click_count * 2))()
    index = 0

    for _ in range(click_count):
        inp = inputs[index]
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(0, 0, mouse_data, down_flag, 0, 0)
        index += 1

        inp = inputs[index]
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(0, 0, mouse_data, up_flag, 0, 0)
        index += 1

    return inputs


def send_inputs(inputs, event_count: int) -> tuple[int, int]:
    """Send an INPUT array and return (events_accepted, last_error)."""
    sent = int(user32.SendInput(event_count, inputs, ctypes.sizeof(INPUT)))
    return sent, (ctypes.get_last_error() if sent < event_count else 0)


def send_button_release(button: str) -> int:
    """Send an extra button-up event (guards a partially accepted click)."""
    down_flag, up_flag, mouse_data = button_spec(button)
    inp = (INPUT * 1)()
    inp[0].type = INPUT_MOUSE
    inp[0].mi = MOUSEINPUT(0, 0, mouse_data, up_flag, 0, 0)
    return int(user32.SendInput(1, inp, ctypes.sizeof(INPUT)))


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def get_cursor_pos() -> tuple[int, int] | None:
    point = POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        return int(point.x), int(point.y)
    return None


def set_cursor_pos(x: int, y: int) -> bool:
    return bool(user32.SetCursorPos(int(x), int(y)))


def screen_size() -> tuple[int, int]:
    return (
        int(user32.GetSystemMetrics(0)),
        int(user32.GetSystemMetrics(1)),
    )


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) for the whole multi-monitor desktop."""
    x = int(user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
    y = int(user32.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
    width = int(user32.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
    height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
    if width <= 0 or height <= 0:
        width, height = screen_size()
        x = y = 0
    return x, y, width, height


def work_area() -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the primary monitor's work area.

    The work area excludes the taskbar, so windows sized and centred with it
    never end up hidden behind it.
    """
    rect = RECT()
    try:
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            if rect.right > rect.left and rect.bottom > rect.top:
                return (
                    int(rect.left),
                    int(rect.top),
                    int(rect.right),
                    int(rect.bottom),
                )
    except Exception:
        pass
    width, height = screen_size()
    return 0, 0, width, height


def clamp_to_screen(x: int, y: int) -> tuple[int, int]:
    left, top, width, height = virtual_screen_bounds()
    if width <= 0 or height <= 0:
        return int(x), int(y)
    return (
        max(left, min(left + width - 1, int(x))),
        max(top, min(top + height - 1, int(y))),
    )


def keep_cursor_locked(x: int, y: int, tolerance: int = 1) -> bool:
    """Return the cursor to (x, y) only when it has moved away."""
    point = POINT()
    if user32.GetCursorPos(ctypes.byref(point)):
        if abs(point.x - x) <= tolerance and abs(point.y - y) <= tolerance:
            return True
    return set_cursor_pos(x, y)


# ---------------------------------------------------------------------------
# Direct (posted message) clicking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectTarget:
    hwnd: int
    x: int
    y: int
    button: str


def _root_window(hwnd: int) -> int:
    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root or hwnd)


def resolve_window(x: int, y: int) -> int | None:
    """Find the deepest visible child window at a screen position."""
    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    if not hwnd:
        return None

    current = int(hwnd)
    for _ in range(10):
        point = POINT(int(x), int(y))
        if not user32.ScreenToClient(current, ctypes.byref(point)):
            break
        child = user32.ChildWindowFromPointEx(
            current,
            point,
            CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT,
        )
        if not child or int(child) == current:
            break
        current = int(child)
    return current


def make_direct_target(x: int, y: int, button: str) -> DirectTarget | None:
    hwnd = resolve_window(x, y)
    if not hwnd:
        return None
    point = POINT(int(x), int(y))
    if not user32.ScreenToClient(hwnd, ctypes.byref(point)):
        return None
    return DirectTarget(hwnd, int(point.x), int(point.y), button)


def focus_target(target: DirectTarget) -> bool:
    return bool(user32.SetForegroundWindow(_root_window(target.hwnd)))


def _lparam(x: int, y: int) -> int:
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def direct_messages(button: str, x: int, y: int):
    """Return (down_msg, up_msg, down_wparam, up_wparam, lparam)."""
    lparam = _lparam(x, y)
    if button == "Right":
        return WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON, 0, lparam
    if button == "Middle":
        return WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON, 0, lparam
    if button == "X1":
        return (
            WM_XBUTTONDOWN,
            WM_XBUTTONUP,
            MK_XBUTTON1 | (XBUTTON1 << 16),
            XBUTTON1 << 16,
            lparam,
        )
    if button == "X2":
        return (
            WM_XBUTTONDOWN,
            WM_XBUTTONUP,
            MK_XBUTTON2 | (XBUTTON2 << 16),
            XBUTTON2 << 16,
            lparam,
        )
    return WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON, 0, lparam


def post_click(target: DirectTarget) -> bool:
    """Post one complete click.  Returns True only if both messages queued."""
    down_msg, up_msg, down_wparam, up_wparam, lparam = direct_messages(
        target.button, target.x, target.y
    )
    down_ok = bool(
        user32.PostMessageW(target.hwnd, down_msg, down_wparam, lparam)
    )
    up_ok = bool(
        user32.PostMessageW(target.hwnd, up_msg, up_wparam, lparam)
    )
    return down_ok and up_ok


def post_clicks(target: DirectTarget, count: int) -> int:
    """Post up to ``count`` complete clicks and return how many queued.

    Posted messages do not go through the system input queue, so this path can
    carry a far wider batch than ``SendInput``. Everything is hoisted into
    locals because this is the hot loop of direct delivery.
    """
    if count <= 0:
        return 0
    down_msg, up_msg, down_wparam, up_wparam, lparam = direct_messages(
        target.button, target.x, target.y
    )
    post = user32.PostMessageW
    hwnd = target.hwnd
    accepted = 0
    for _ in range(count):
        if not post(hwnd, down_msg, down_wparam, lparam):
            break
        if not post(hwnd, up_msg, up_wparam, lparam):
            break
        accepted += 1
    return accepted


# ---------------------------------------------------------------------------
# System setup
# ---------------------------------------------------------------------------

def detach_console() -> bool:
    """Detach from any inherited console.

    If the process is ever started with a console attached (console-mode
    build, shortcut or interpreter), this releases it so no console or
    terminal window lingers next to the app window. Harmless when the
    process has no console.
    """
    try:
        return bool(kernel32.FreeConsole())
    except Exception:
        return False


def enable_dpi_awareness() -> bool:
    """Make Tk render crisply on high-DPI displays."""
    try:
        # PER_MONITOR_AWARE_V2 == -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:
        return False


def set_thread_priority(extreme: bool = True) -> bool:
    priority = (
        THREAD_PRIORITY_TIME_CRITICAL if extreme else THREAD_PRIORITY_HIGHEST
    )
    return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), priority))


def reset_thread_priority() -> bool:
    return bool(
        kernel32.SetThreadPriority(
            kernel32.GetCurrentThread(), THREAD_PRIORITY_NORMAL
        )
    )


def disable_power_throttling() -> bool:
    """Opt this thread out of ecoQoS execution-speed throttling.

    Windows 11 can schedule a CPU-hungry thread onto a low-power core and
    clamp its clock. The click thread is short-lived bursts of work that must
    land on a millisecond timeline, so it asks for full speed for as long as
    the session runs. Unavailable before Windows 10 1709; failure is ignored.
    """
    state = THREAD_POWER_THROTTLING_STATE(
        THREAD_POWER_THROTTLING_CURRENT_VERSION,
        THREAD_POWER_THROTTLING_EXECUTION_SPEED,
        0,
    )
    try:
        return bool(
            kernel32.SetThreadInformation(
                kernel32.GetCurrentThread(),
                THREAD_INFORMATION_CLASS_POWER_THROTTLING,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
    except Exception:
        return False


def switch_to_thread() -> bool:
    """Yield the rest of this thread's timeslice and return immediately."""
    try:
        return bool(kernel32.SwitchToThread())
    except Exception:
        time.sleep(0)
        return False


def set_process_priority(high: bool = True) -> bool:
    priority = HIGH_PRIORITY_CLASS if high else ABOVE_NORMAL_PRIORITY_CLASS
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), priority))


def reset_process_priority() -> bool:
    return bool(
        kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(), NORMAL_PRIORITY_CLASS
        )
    )


class HighResTimer:
    """One-shot high-resolution waitable timer.

    ``sleep`` blocks for roughly the requested time and is used by the click
    engine for the long tier of its wait; the engine yields and then spins
    the last 50 us itself. Falls back to ``time.sleep`` when the OS cannot
    create a high-resolution timer (before Windows 10 1803), where the 1 ms
    ``timeBeginPeriod`` request still applies.
    """

    def __init__(self) -> None:
        self._handle = None
        self._due = LARGE_INTEGER()
        try:
            handle = kernel32.CreateWaitableTimerExW(
                None,
                None,
                CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                TIMER_ALL_ACCESS,
            )
            if handle:
                self._handle = handle
        except Exception:
            self._handle = None

    @property
    def high_resolution(self) -> bool:
        return self._handle is not None

    def sleep(self, seconds: float) -> None:
        if seconds <= 0.0:
            return
        if self._handle is None:
            time.sleep(seconds)
            return
        # SetWaitableTimer takes relative time in 100 ns units (negative).
        due = -int(seconds * 10_000_000)
        if due > -1:
            due = -1
        self._due.QuadPart = due
        try:
            if not kernel32.SetWaitableTimer(
                self._handle, ctypes.byref(self._due), 0, None, None, False
            ):
                time.sleep(seconds)
                return
            kernel32.WaitForSingleObject(
                self._handle, int(seconds * 1000.0) + 2
            )
        except Exception:
            time.sleep(seconds)

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass


class TimerResolution:
    """RAII wrapper around timeBeginPeriod(1)."""

    def __init__(self) -> None:
        self.active = False

    def begin(self) -> bool:
        if self.active:
            return True
        try:
            self.active = winmm.timeBeginPeriod(1) == 0
        except Exception:
            self.active = False
        return self.active

    def end(self) -> None:
        if not self.active:
            return
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass
        self.active = False


def is_key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def key_pressed(vk: int) -> tuple[bool, bool]:
    """Return ``(held_now, pressed_since_last_check)`` for a virtual key.

    The low bit is set when the key was pressed after the previous call, even
    if it was already released again. Polling that bit means a quick tap of a
    hotkey is not missed between two polls of the UI thread.
    """
    state = int(user32.GetAsyncKeyState(vk)) & 0xFFFF
    return bool(state & 0x8000), bool(state & 0x0001)


# Virtual key codes offered by the hotkey selector.
HOTKEY_KEYS = {
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
    "Pause": 0x13,
    "Scroll Lock": 0x91,
    "Insert": 0x2D,
    "Home": 0x24,
    "End": 0x23,
    "Page Up": 0x21,
    "Page Down": 0x22,
}
