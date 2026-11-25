"""ClickStorm's main window and application logic."""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
import tkinter as tk
import traceback
from collections import deque
from tkinter import messagebox

from . import APP_NAME, APP_TAGLINE
from . import theme, winapi
from .engine import (
    ClickEngine,
    EngineConfig,
    MAX_CLICKS,
    MAX_CPS,
    MIN_CPS,
    SENDINPUT_CEILING_CPS,
)
from .settings import Settings, settings_path
from .settings_dialog import SettingsDialog
from .update_dialog import UpdateDialog
from .theme import COLORS, fmt_clock, fmt_int, fmt_rate


# ---------------------------------------------------------------------------
# Paths / window helpers
# ---------------------------------------------------------------------------

def resource_path(*parts: str) -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


ICON_PATH = resource_path("autoclicker.ico")


_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")


def _restore_window(root: tk.Tk, geometry: str) -> None:
    width, height = 1180, 800
    pos_x = pos_y = None
    match = _GEOMETRY_RE.match((geometry or "").strip())
    if match:
        width = int(match.group(1))
        height = int(match.group(2))
        if match.group(3) is not None and match.group(4) is not None:
            pos_x = int(match.group(3))
            pos_y = int(match.group(4))

    vx, vy, vw, vh = winapi.virtual_screen_bounds()
    max_width = max(1050, vw - 30) if vw > 0 else 1600
    max_height = max(700, vh - 60) if vh > 0 else 1000
    width = max(1050, min(max_width, width))
    height = max(700, min(max_height, height))

    if pos_x is not None and pos_y is not None:
        on_screen_x = pos_x + width > vx + 80 and pos_x < vx + vw - 80
        on_screen_y = pos_y + 60 > vy and pos_y < vy + vh - 60
        if not (on_screen_x and on_screen_y):
            pos_x = pos_y = None

    if pos_x is None or pos_y is None:
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        pos_x = max(0, (screen_w - width) // 2)
        pos_y = max(0, (screen_h - height) // 2 - 12)

    root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")


def _format_rate_input(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_seconds_short(seconds: float) -> str:
    if seconds < 0.1:
        return "<0.1s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return fmt_clock(seconds)


def _log_exception(context: str, exc: BaseException | None = None) -> None:
    try:
        path = settings_path().parent
        path.mkdir(parents=True, exist_ok=True)
        with (path / "error.log").open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            if exc is None:
                handle.write(traceback.format_exc())
            else:
                handle.write(f"{type(exc).__name__}: {exc}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class ClickStormApp:
    UI_INTERVAL_MS = 50
    # Hotkeys are polled much faster than the dashboard is redrawn so short
    # taps are not missed.
    HOTKEY_INTERVAL_MS = 15

    def __init__(self, root: tk.Tk, settings: Settings):
        self.root = root
        self.settings = settings
        self.engine = ClickEngine()

        self.shutdown = False
        self._settings_open = False
        self._settings_dialog = None
        self._update_open = False
        self._update_dialog = None

        self._countdown_active = False
        self._countdown_end = 0.0
        self._countdown_job = None

        self._run_active = False
        self._active_session_id = 0
        self._session_committed = True
        self._auto_minimized = False

        self._rate = float(settings.cps)
        self._limit_mode = settings.limit_mode
        self._click_limit = int(settings.click_limit)
        self._duration = float(settings.duration)
        self._start_delay = float(settings.start_delay)

        self._hotkey_keys_down: set[int] = set()

        self._sample_time = time.perf_counter()
        self._sample_clicks = 0
        self._current_cps = 0.0
        self._peak_cps = 0.0
        self._rate_history: deque[float] = deque(maxlen=120)
        self._sparkline_dirty = True
        self._last_ui_stats_time = 0.0

        self._quick_buttons = []
        self._ui_job = None
        self._hotkey_job = None

        self._build_gui()
        self._sync_from_settings()
        self._set_status("READY", "idle")
        self._refresh_hotkey_labels()
        self._refresh_engine_strip()
        self._update_footer_idle()
        self._update_limit_ui()
        self._set_rate(settings.cps, update_entry=True)
        self._set_controls_enabled(True)
        self._apply_topmost()

        try:
            self.root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._ui_job = self.root.after(self.UI_INTERVAL_MS, self._ui_tick)
        self._hotkey_job = self.root.after(
            self.HOTKEY_INTERVAL_MS, self._hotkey_tick
        )

    # ------------------------------------------------------------------
    # GUI construction
    # ------------------------------------------------------------------

    def _build_gui(self) -> None:
        self.root.configure(bg=COLORS["bg"])

        main = tk.Frame(self.root, bg=COLORS["bg"])
        main.pack(fill="both", expand=True, padx=22, pady=18)

        self._build_header(main)
        self._build_footer(main)

        # The dashboard scrolls if the window is shorter than its natural
        # height. Header and footer stay fixed so START / STOP are always
        # reachable, even on 768p displays.
        body = tk.Frame(main, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        self.body_canvas = tk.Canvas(
            body,
            bg=COLORS["bg"],
            highlightthickness=0,
            bd=0,
            yscrollincrement=26,
        )
        self.body_canvas.pack(side="left", fill="both", expand=True)

        self.scrollbar = theme.SlimScrollbar(
            body,
            command=self.body_canvas.yview,
            bg=COLORS["bg"],
        )
        self.scrollbar.pack(side="right", fill="y", padx=(9, 0))
        self.body_canvas.configure(yscrollcommand=self.scrollbar.set)

        self.body_inner = tk.Frame(self.body_canvas, bg=COLORS["bg"])
        self._body_window = self.body_canvas.create_window(
            (0, 0), window=self.body_inner, anchor="nw"
        )
        self.body_inner.bind("<Configure>", self._on_body_configure)
        self.body_canvas.bind("<Configure>", self._on_body_canvas_configure)
        self.root.bind_all("<MouseWheel>", self._on_body_wheel, add="+")

        content = self.body_inner
        content.grid_columnconfigure(0, weight=3, uniform="maincol")
        content.grid_columnconfigure(1, weight=2, uniform="maincol")
        content.grid_rowconfigure(0, weight=1)

        left = tk.Frame(content, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        right = tk.Frame(content, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._left_column = left
        self._right_column = right

        self._build_rate_card(left)
        self._build_limit_card(left)
        self._build_bottom_row(left)
        self._build_engine_card(left)
        self._build_performance_card(right)
        self._build_history_card(right)
        self._build_summary_card(right)

    # -- scrollable body ------------------------------------------------

    def _on_body_configure(self, _event=None) -> None:
        try:
            bounds = self.body_canvas.bbox("all")
        except Exception:
            return
        if bounds:
            self.body_canvas.configure(
                scrollregion=(0, 0, bounds[2], bounds[3] + 6)
            )

    def _on_body_canvas_configure(self, event) -> None:
        self.body_canvas.itemconfigure(
            self._body_window, width=max(1, event.width)
        )

    def _is_inside_body(self, widget) -> bool:
        node = widget
        while node is not None:
            if node is self.body_canvas or node is self.body_inner:
                return True
            node = getattr(node, "master", None)
        return False

    def _on_body_wheel(self, event):
        if self.shutdown:
            return None
        widget = event.widget
        if not hasattr(widget, "master") or not self._is_inside_body(widget):
            return None
        first, last = self.body_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return None
        step = -2 if event.delta > 0 else 2
        self.body_canvas.yview_scroll(step, "units")
        return "break"

    def fit_initial_window(self) -> None:
        """Grow the window so the whole dashboard is visible when possible."""
        try:
            self.root.update_idletasks()
            current_w = max(1, self.root.winfo_width())
            current_h = max(1, self.root.winfo_height())
            body_h = max(1, self.body_canvas.winfo_height())
            chrome = max(0, current_h - body_h)
            needed = self.body_inner.winfo_reqheight() + chrome + 8
            if needed <= current_h + 2:
                return

            _, work_top, _, work_bottom = winapi.work_area()
            work_h = max(600, work_bottom - work_top)
            max_client = max(700, work_h - 52)
            target_h = min(max(current_h, needed), max_client)
            if target_h <= current_h + 2:
                return

            match = _GEOMETRY_RE.match(self.root.geometry())
            if match:
                x = int(match.group(3) or 0)
                y = int(match.group(4) or 0)
            else:
                x, y = self.root.winfo_x(), self.root.winfo_y()

            title_bar = 44
            if y + target_h + title_bar > work_bottom:
                y = max(work_top, work_bottom - target_h - title_bar)
            self.root.geometry(f"{current_w}x{target_h}+{x}+{y}")
            self.root.update_idletasks()
        except Exception as exc:
            _log_exception("Fitting window", exc)

    def _build_header(self, parent) -> None:
        header = tk.Frame(parent, bg=COLORS["bg"])
        header.pack(fill="x", pady=(0, 14))

        brand = tk.Frame(header, bg=COLORS["bg"])
        brand.pack(side="left")

        theme.LogoMark(brand, size=50, bg=COLORS["bg"]).pack(side="left")
        name_block = tk.Frame(brand, bg=COLORS["bg"])
        name_block.pack(side="left", padx=(12, 0))
        tk.Label(
            name_block,
            text=APP_NAME,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=theme.font(24, "bold", "display"),
        ).pack(anchor="w")
        theme.make_label(
            name_block,
            "ULTRA-FAST AUTO CLICKER  /  WINDOWS",
            size=7, bold=True, color=COLORS["accent"], bg=COLORS["bg"],
        ).pack(anchor="w", pady=(0, 1))

        right = tk.Frame(header, bg=COLORS["bg"])
        right.pack(side="right")

        self.status_pill = theme.StatusPill(right, bg=COLORS["bg"])
        self.status_pill.pack(side="right", padx=(12, 0))

        self.settings_button = theme.PillButton(
            right,
            text="SETTINGS",
            command=self.open_settings,
            width=108,
            height=32,
            bg=COLORS["bg"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=8,
        )
        self.settings_button.pack(side="right", padx=(10, 0))

        self.update_button = theme.PillButton(
            right,
            text="UPDATES",
            command=self.open_updates,
            width=100,
            height=32,
            bg=COLORS["bg"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["accent"],
            font_size=8,
        )
        self.update_button.pack(side="right", padx=(10, 0))

        self.hotkey_header_label = theme.make_label(
            right,
            "",
            size=8,
            bold=True,
            color=COLORS["faint"],
            bg=COLORS["bg"],
        )
        self.hotkey_header_label.pack(side="right", pady=8)

        separator = tk.Frame(parent, bg=COLORS["border_soft"], height=1)
        separator.pack(fill="x", pady=(0, 0))

    def _build_rate_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=18, pady=12)

        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x")
        theme.make_label(
            header, "CLICK RATE", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        theme.make_label(
            header, "PRECISION CONTROL", size=7, bold=True,
            color=COLORS["faint"],
        ).pack(side="right")

        hero = tk.Frame(inner, bg=COLORS["card"])
        hero.pack(fill="x", pady=(2, 0))

        self.rate_big_var = tk.StringVar(value="500")
        tk.Label(
            hero,
            textvariable=self.rate_big_var,
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=theme.font(33, "bold", "mono"),
        ).pack(side="left", anchor="s")
        tk.Label(
            hero,
            text="CPS",
            bg=COLORS["card"],
            fg=COLORS["accent"],
            font=theme.font(11, "bold"),
        ).pack(side="left", anchor="s", padx=(5, 0), pady=(0, 6))

        interval_block = tk.Frame(hero, bg=COLORS["card"])
        interval_block.pack(side="right", pady=(7, 0))
        self.interval_var = tk.StringVar(value="2.000 ms / click")
        tk.Label(
            interval_block,
            textvariable=self.interval_var,
            bg=COLORS["card"],
            fg=COLORS["text_dim"],
            font=theme.font(10, "bold", "mono"),
        ).pack(anchor="e")
        tk.Label(
            interval_block,
            text="CLICK INTERVAL",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(7, "bold"),
        ).pack(anchor="e")

        self.rate_slider = theme.LogSlider(
            inner,
            minimum=MIN_CPS,
            maximum=MAX_CPS,
            value=self._rate,
            command=self._on_slider,
            bg=COLORS["card"],
        )
        self.rate_slider.pack(fill="x", pady=(3, 0))

        controls = tk.Frame(inner, bg=COLORS["card"])
        controls.pack(fill="x", pady=(2, 0))

        self.rate_chips = []
        for label, value in (
            ("10", 10),
            ("100", 100),
            ("500", 500),
            ("1K", 1000),
            ("5K", 5000),
            ("10K", 10000),
            ("MAX", MAX_CPS),
        ):
            button = theme.PillButton(
                controls,
                text=label,
                command=lambda v=value: self._set_rate(v, update_entry=True),
                width=48,
                height=27,
                bg=COLORS["card"],
                fill=COLORS["card2"],
                hover=COLORS["card3"],
                fg=COLORS["text_dim"],
                font_size=8,
            )
            button.pack(side="left", padx=(0, 4))
            self.rate_chips.append(button)

        entry_block = tk.Frame(controls, bg=COLORS["card"])
        entry_block.pack(side="right")
        tk.Label(
            entry_block,
            text="CPS",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(7, "bold"),
        ).pack(side="right", padx=(5, 0))
        self.cps_entry = self._entry(
            entry_block, None, width=8, justify="center"
        )
        self.cps_entry.pack(side="right")
        self.cps_entry.bind("<Return>", self._on_cps_entry)
        self.cps_entry.bind("<FocusOut>", self._on_cps_entry)

        self.rate_hint = tk.Label(
            inner,
            text="",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
        )
        self.rate_hint.pack(anchor="w", pady=(3, 0))

    def _build_limit_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="x", pady=(8, 0))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=18, pady=11)

        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x", pady=(0, 8))
        theme.make_label(
            header, "SESSION LIMIT", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self.limit_summary_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.limit_summary_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
        ).pack(side="right")

        self.limit_mode_var = tk.StringVar(value=self._limit_mode)
        self.limit_segment = theme.SegmentedControl(
            inner,
            options=(
                ("unlimited", "UNLIMITED"),
                ("clicks", "CLICKS"),
                ("duration", "DURATION"),
            ),
            variable=self.limit_mode_var,
            command=self._on_limit_mode,
            bg=COLORS["card"],
            button_width=104,
            button_height=31,
            font_size=8,
            accent=COLORS["cyan"],
        )
        self.limit_segment.pack(anchor="w", pady=(0, 7))

        entry_row = tk.Frame(inner, bg=COLORS["card"])
        entry_row.pack(fill="x")
        self.quick_frame = tk.Frame(entry_row, bg=COLORS["card"])
        self.quick_frame.pack(side="left")

        suffix_block = tk.Frame(entry_row, bg=COLORS["card"])
        suffix_block.pack(side="right")
        self.limit_suffix_var = tk.StringVar(value="clicks")
        tk.Label(
            suffix_block,
            textvariable=self.limit_suffix_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
        ).pack(side="right", padx=(6, 0))
        self.limit_entry = self._entry(
            suffix_block, None, width=12, justify="center"
        )
        self.limit_entry.pack(side="right")
        self.limit_entry.bind("<Return>", self._on_limit_entry)
        self.limit_entry.bind("<FocusOut>", self._on_limit_entry)

        progress_header = tk.Frame(inner, bg=COLORS["card"])
        progress_header.pack(fill="x", pady=(9, 3))
        tk.Label(
            progress_header,
            text="PROGRESS",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(7, "bold"),
        ).pack(side="left")
        self.progress_text_var = tk.StringVar(value="Unlimited")
        tk.Label(
            progress_header,
            textvariable=self.progress_text_var,
            bg=COLORS["card"],
            fg=COLORS["text_dim"],
            font=theme.font(8, "bold"),
        ).pack(side="right")

        self.progress_bar = theme.ThinProgress(inner, bg=COLORS["card"], height=8)
        self.progress_bar.pack(fill="x")

        progress_footer = tk.Frame(inner, bg=COLORS["card"])
        progress_footer.pack(fill="x", pady=(4, 0))
        self.progress_detail_var = tk.StringVar(value="No limit - STOP ends the run.")
        tk.Label(
            progress_footer,
            textvariable=self.progress_detail_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
        ).pack(side="left")
        self.eta_var = tk.StringVar(value="ETA --:--")
        tk.Label(
            progress_footer,
            textvariable=self.eta_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
        ).pack(side="right")

    def _build_bottom_row(self, parent) -> None:
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure(0, weight=1, uniform="bottom")
        row.grid_columnconfigure(1, weight=1, uniform="bottom")

        mouse_card = theme.Card(row)
        mouse_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        mouse_inner = tk.Frame(mouse_card, bg=COLORS["card"])
        mouse_inner.pack(fill="x", padx=14, pady=10)
        theme.make_label(
            mouse_inner, "MOUSE BUTTON", size=8, bold=True, color=COLORS["muted"]
        ).pack(anchor="w", pady=(0, 7))
        self.mouse_button_var = tk.StringVar(value=self.settings.mouse_button)
        self.mouse_segment = theme.SegmentedControl(
            mouse_inner,
            options=("Left", "Right", "Middle", "X1", "X2"),
            variable=self.mouse_button_var,
            command=self._on_mouse_button,
            bg=COLORS["card"],
            button_width=48,
            button_height=29,
            font_size=8,
            gap=4,
            accent=COLORS["accent"],
        )
        self.mouse_segment.pack(anchor="w")
        theme.make_label(
            mouse_inner,
            "All five Windows mouse buttons are supported.",
            size=7, color=COLORS["faint"],
        ).pack(anchor="w", pady=(5, 0))

        delay_card = theme.Card(row)
        delay_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        delay_inner = tk.Frame(delay_card, bg=COLORS["card"])
        delay_inner.pack(fill="x", padx=14, pady=10)
        theme.make_label(
            delay_inner, "START DELAY", size=8, bold=True, color=COLORS["muted"]
        ).pack(anchor="w", pady=(0, 7))
        self.delay_var = tk.StringVar(value=f"{self._start_delay:.1f}")
        self.delay_segment = theme.SegmentedControl(
            delay_inner,
            options=(
                ("0.0", "0s"),
                ("1.0", "1s"),
                ("2.0", "2s"),
                ("3.0", "3s"),
                ("5.0", "5s"),
            ),
            variable=self.delay_var,
            command=self._on_delay,
            bg=COLORS["card"],
            button_width=44,
            button_height=29,
            font_size=8,
            gap=4,
            accent=COLORS["purple"],
        )
        self.delay_segment.pack(anchor="w")
        theme.make_label(
            delay_inner,
            "Move your cursor into place before clicks begin.",
            size=7, color=COLORS["faint"],
        ).pack(anchor="w", pady=(5, 0))

    def _build_engine_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="both", expand=True, pady=(8, 0))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=16, pady=10)

        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x")
        theme.make_label(
            header, "ENGINE", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        theme.PillButton(
            header,
            text="CUSTOMIZE",
            command=self.open_settings,
            width=106,
            height=27,
            bg=COLORS["card"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=8,
        ).pack(side="right")

        self.engine_info_var = tk.StringVar(value="")
        tk.Label(
            inner,
            textvariable=self.engine_info_var,
            bg=COLORS["card"],
            fg=COLORS["text_dim"],
            font=theme.font(9),
            justify="left",
        ).pack(anchor="w", pady=(5, 8))

        badge_row = tk.Frame(inner, bg=COLORS["card"])
        badge_row.pack(fill="x")
        for text, color in (
            ("SMALL BATCHES", COLORS["accent"]),
            ("HIGH-RES TIMER", COLORS["green"]),
            ("EXACT TARGET", COLORS["purple"]),
        ):
            badge = tk.Frame(
                badge_row,
                bg=COLORS["card2"],
                highlightbackground=COLORS["border_soft"],
                highlightthickness=1,
            )
            badge.pack(side="left", padx=(0, 6))
            tk.Label(
                badge,
                text="\u25cf",
                bg=COLORS["card2"],
                fg=color,
                font=theme.font(7),
            ).pack(side="left", padx=(7, 3), pady=4)
            tk.Label(
                badge,
                text=text,
                bg=COLORS["card2"],
                fg=COLORS["muted"],
                font=theme.font(7, "bold"),
            ).pack(side="left", padx=(0, 7), pady=4)

    def _build_performance_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="x")
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=14, pady=11)

        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x")
        theme.make_label(
            header, "LIVE PERFORMANCE", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self.perf_state_var = tk.StringVar(value="IDLE")
        tk.Label(
            header,
            textvariable=self.perf_state_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8, "bold"),
        ).pack(side="right")

        self.gauge = theme.Gauge(inner, bg=COLORS["card"], height=172)
        self.gauge.pack(fill="x", pady=(0, 4))
        self._gauge_target = self._rate
        self._gauge_running = False
        self.gauge.set(0, max(1, self._rate), self._rate, False,
                       subtitle=f"TARGET {fmt_rate(self._rate)} CPS")

        session_strip = tk.Frame(
            inner,
            bg=COLORS["card2"],
            highlightbackground=COLORS["border_soft"],
            highlightthickness=1,
        )
        session_strip.pack(fill="x", pady=(0, 1))

        left = tk.Frame(session_strip, bg=COLORS["card2"])
        left.pack(side="left", padx=12, pady=7)
        tk.Label(
            left,
            text="THIS SESSION",
            bg=COLORS["card2"],
            fg=COLORS["faint"],
            font=theme.font(7, "bold"),
        ).pack(anchor="w")
        self.session_big_var = tk.StringVar(value="0")
        tk.Label(
            left,
            textvariable=self.session_big_var,
            bg=COLORS["card2"],
            fg=COLORS["green"],
            font=theme.font(17, "bold", "mono"),
        ).pack(anchor="w")

        right = tk.Frame(session_strip, bg=COLORS["card2"])
        right.pack(side="right", padx=12, pady=7)
        tk.Label(
            right,
            text="ELAPSED",
            bg=COLORS["card2"],
            fg=COLORS["faint"],
            font=theme.font(7, "bold"),
        ).pack(anchor="e")
        self.elapsed_big_var = tk.StringVar(value="00:00")
        tk.Label(
            right,
            textvariable=self.elapsed_big_var,
            bg=COLORS["card2"],
            fg=COLORS["text_dim"],
            font=theme.font(12, "bold", "mono"),
        ).pack(anchor="e")

    def _build_history_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="x", pady=(8, 0))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=14, pady=9)
        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x")
        theme.make_label(
            header, "RATE HISTORY", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        theme.make_label(
            header, "LAST 10 SECONDS", size=7, color=COLORS["faint"]
        ).pack(side="right")
        self.sparkline = theme.Sparkline(inner, bg=COLORS["card"], height=80)
        self.sparkline.pack(fill="x", pady=(2, 0))

    def _build_summary_card(self, parent) -> None:
        card = theme.Card(parent)
        card.pack(fill="both", expand=True, pady=(8, 0))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="both", expand=True, padx=12, pady=9)

        header = tk.Frame(inner, bg=COLORS["card"])
        header.pack(fill="x", pady=(0, 6))
        theme.make_label(
            header, "CLICK SUMMARY", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        theme.make_label(
            header, "SESSION TOTALS", size=7, color=COLORS["faint"]
        ).pack(side="right")

        grid = tk.Frame(inner, bg=COLORS["card"])
        grid.pack(fill="both", expand=True)

        self.stat_vars = {
            key: tk.StringVar(value="0")
            for key in (
                "lifetime",
                "target",
                "remaining",
                "average",
                "peak",
                "calls",
                "inputs",
                "dropped",
            )
        }
        tiles = (
            ("lifetime", "LIFETIME", COLORS["green"]),
            ("target", "TARGET", COLORS["accent"]),
            ("remaining", "REMAINING", COLORS["amber"]),
            ("average", "AVERAGE", COLORS["text"]),
            ("peak", "PEAK", COLORS["purple"]),
            ("calls", "SEND CALLS", COLORS["text_dim"]),
            ("inputs", "INPUT EVENTS", COLORS["text_dim"]),
            ("dropped", "FAILED EVENTS", COLORS["red"]),
        )
        for index, (key, title, color) in enumerate(tiles):
            row, column = divmod(index, 4)
            grid.grid_columnconfigure(column, weight=1, uniform="summary")
            grid.grid_rowconfigure(row, weight=1, uniform="summaryrow")
            tile = tk.Frame(
                grid,
                bg=COLORS["card2"],
                highlightbackground=COLORS["border_soft"],
                highlightthickness=1,
            )
            tile.grid(
                row=row, column=column, sticky="nsew",
                padx=(0 if column == 0 else 3, 0), pady=(0, 3),
            )
            tk.Label(
                tile,
                text=title,
                bg=COLORS["card2"],
                fg=COLORS["faint"],
                font=theme.font(7, "bold"),
            ).pack(anchor="w", padx=8, pady=(5, 0))
            tk.Label(
                tile,
                textvariable=self.stat_vars[key],
                bg=COLORS["card2"],
                fg=color,
                font=theme.font(9, "bold", "mono"),
            ).pack(anchor="w", padx=8, pady=(0, 5))

    def _build_footer(self, parent) -> None:
        footer = tk.Frame(
            parent,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        inner = tk.Frame(footer, bg=COLORS["panel"])
        inner.pack(fill="x", padx=16, pady=11)

        status_block = tk.Frame(inner, bg=COLORS["panel"])
        status_block.pack(side="left", fill="y")
        self.footer_title_var = tk.StringVar(value="Ready to click")
        self.footer_sub_var = tk.StringVar(value="")
        tk.Label(
            status_block,
            textvariable=self.footer_title_var,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=theme.font(10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            status_block,
            textvariable=self.footer_sub_var,
            bg=COLORS["panel"],
            fg=COLORS["faint"],
            font=theme.font(8),
        ).pack(anchor="w", pady=(2, 0))

        self.stop_button = theme.PillButton(
            inner,
            text="STOP  \u00b7  F9",
            command=self.stop_clicking,
            width=146,
            height=46,
            bg=COLORS["panel"],
            fill=COLORS["red"],
            hover="#fda4af",
            active=COLORS["red"],
            fg="#2a0710",
            font_size=10,
        )
        self.stop_button.pack(side="right", padx=(10, 0))

        self.start_button = theme.PillButton(
            inner,
            text="START CLICKING  \u00b7  F5",
            command=self.start_clicking,
            width=252,
            height=46,
            bg=COLORS["panel"],
            fill=COLORS["green"],
            hover="#6ee7b7",
            active=COLORS["green"],
            fg="#04150f",
            font_size=11,
        )
        self.start_button.pack(side="right")

        self.stop_button.set_enabled(False)

    # ------------------------------------------------------------------
    # Small widget helpers
    # ------------------------------------------------------------------

    def _entry(self, parent, variable, width=8, justify="center"):
        entry = tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            justify=justify,
            bg=COLORS["card2"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            disabledbackground=COLORS["card2"],
            disabledforeground=COLORS["faint"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=theme.font(10, "bold", "mono"),
        )
        return entry

    @staticmethod
    def _write_entry(entry: tk.Entry, text: str) -> None:
        state = str(entry.cget("state"))
        if state == "disabled":
            entry.config(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        entry.config(state=state)

    # ------------------------------------------------------------------
    # Synchronisation / visual refresh
    # ------------------------------------------------------------------

    def _sync_from_settings(self) -> None:
        self.mouse_button_var.set(self.settings.mouse_button)
        self.limit_mode_var.set(self.settings.limit_mode)
        self._limit_mode = self.settings.limit_mode
        self._click_limit = max(1, min(MAX_CLICKS, int(self.settings.click_limit)))
        self._duration = float(self.settings.duration)
        self._start_delay = float(self.settings.start_delay)
        self.delay_var.set(f"{self._start_delay:.1f}")
        self.delay_segment._refresh()
        if self.settings.hotkey_start == self.settings.hotkey_stop:
            self.settings.hotkey_start = "F5"
            self.settings.hotkey_stop = "F9"
        self._update_limit_ui()
        self._refresh_hotkey_labels()
        self._refresh_engine_strip()
        self._apply_topmost()

    def _apply_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.settings.always_on_top))
        except Exception:
            pass

    def _set_status(self, text: str, state: str) -> None:
        styles = {
            "idle": (COLORS["muted"], COLORS["card2"], COLORS["faint"]),
            "countdown": (COLORS["amber"], COLORS["amber_deep"], COLORS["amber"]),
            "running": (COLORS["green"], "#0b3328", COLORS["green"]),
            "stopping": (COLORS["amber"], COLORS["amber_deep"], COLORS["amber"]),
            "done": (COLORS["green"], "#0b3328", COLORS["green"]),
            "error": (COLORS["red"], COLORS["red_deep"], COLORS["red"]),
        }
        fg, fill, dot = styles.get(state, styles["idle"])
        self.status_pill.set(text.upper(), fg, fill, dot)
        states = {
            "idle": "IDLE",
            "countdown": "ARMED",
            "running": "LIVE",
            "stopping": "STOPPING",
            "done": "DONE",
            "error": "ERROR",
        }
        self.perf_state_var.set(states.get(state, "IDLE"))

    def _refresh_hotkey_labels(self) -> None:
        start = self.settings.hotkey_start
        stop = self.settings.hotkey_stop
        self.hotkey_header_label.config(
            text=f"{start} START   \u00b7   {stop} STOP"
        )
        self.start_button.set_text(f"START CLICKING  \u00b7  {start}")
        self.stop_button.set_text(f"STOP  \u00b7  {stop}")

    def _refresh_engine_strip(self) -> None:
        delivery = {
            "sendinput": "SendInput, small adaptive batches",
            "direct": "Direct window messages (experimental)",
            "auto": (
                "AUTO - Direct above "
                f"{fmt_rate(SENDINPUT_CEILING_CPS)} CPS"
            ),
        }.get(self.settings.delivery, "SendInput, small adaptive batches")
        position = (
            "Follow cursor"
            if self.settings.position_mode == "cursor"
            else f"Fixed at {self.settings.fixed_x}, {self.settings.fixed_y}"
        )
        jitter = (
            f"  \u00b7  {self.settings.jitter_percent * 100:.0f}% jitter"
            if self.settings.jitter_percent > 0
            else ""
        )
        self.engine_info_var.set(
            f"{delivery}  \u00b7  {position}{jitter}\n"
            f"Three-tier wait  \u00b7  high-priority click thread"
        )

    def _update_footer_idle(self) -> None:
        delay = self._start_delay
        if delay > 0:
            delay_text = f"{delay:.1f}s start delay"
        else:
            delay_text = "instant start"
        mode = {
            "direct": "Direct",
            "auto": "AUTO delivery",
        }.get(self.settings.delivery, "SendInput")
        self.footer_title_var.set("Ready to click")
        self.footer_sub_var.set(
            f"{delay_text}  \u00b7  {mode}  \u00b7  "
            f"{self.settings.hotkey_start} starts, "
            f"{self.settings.hotkey_stop} stops"
        )

    def _set_rate(self, value: float, update_entry: bool = False) -> None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        self._rate = max(MIN_CPS, min(MAX_CPS, value))
        if update_entry:
            self._write_entry(self.cps_entry, _format_rate_input(self._rate))
        self.rate_slider.set(self._rate)
        self.rate_big_var.set(fmt_rate(self._rate))
        interval_ms = 1000.0 / max(self._rate, 0.001)
        if interval_ms >= 100:
            interval_text = f"{interval_ms:.1f} ms / click"
        elif interval_ms >= 1:
            interval_text = f"{interval_ms:.3f} ms / click"
        else:
            interval_text = f"{interval_ms:.4f} ms / click"
        self.interval_var.set(interval_text)
        self._update_rate_hint()
        self._update_limit_summary()
        if not self._run_active:
            self._gauge_target = self._rate
            self.gauge.set(
                0,
                max(self._rate, 1),
                self._rate,
                False,
                subtitle=f"TARGET {fmt_rate(self._rate)} CPS",
            )
            self.sparkline.set_data(list(self._rate_history), self._rate)

    def _update_rate_hint(self) -> None:
        rate = self._rate
        # 1,000 ms per second, so the target over 1,000 is exactly the batch
        # that belongs to one slot.
        per_ms = rate / 1000.0
        if per_ms >= 1:
            batch = f"{per_ms:,.2f}".rstrip("0").rstrip(".")
            slot_text = f"{batch} clicks every 1 ms slot"
        else:
            slot_text = f"1 click every {1000.0 / rate:,.2f} ms"
        if rate > SENDINPUT_CEILING_CPS:
            if self.settings.delivery == "sendinput":
                text = (
                    f"{slot_text}. SendInput tops out near "
                    f"{fmt_rate(SENDINPUT_CEILING_CPS)} CPS here - set "
                    "Delivery to AUTO for the full rate."
                )
                color = COLORS["red"]
            else:
                text = f"{slot_text}, delivered over the direct path."
                color = COLORS["green"]
        elif rate > 2_000:
            text = f"{slot_text}. Very fast; apps that throttle input register fewer."
            color = COLORS["amber"]
        elif rate >= 1000:
            text = f"{slot_text}. High speed, high precision."
            color = COLORS["text_dim"]
        elif rate >= 100:
            text = f"{slot_text}. Fast and app-friendly."
            color = COLORS["faint"]
        else:
            text = f"{slot_text}. Relaxed, human-speed clicking."
            color = COLORS["faint"]
        self.rate_hint.config(text=text, fg=color)

    def _on_slider(self, value: float) -> None:
        if self._countdown_active or self._run_active:
            return
        self._set_rate(value, update_entry=True)

    def _on_cps_entry(self, _event=None) -> None:
        if self._countdown_active or self._run_active:
            return
        text = self.cps_entry.get().replace(",", "").strip()
        try:
            value = float(text)
        except ValueError:
            value = self._rate
        value = max(MIN_CPS, min(MAX_CPS, value))
        self._set_rate(value, update_entry=True)

    def _on_mouse_button(self, _value: str) -> None:
        if self._countdown_active or self._run_active:
            return
        self.settings.mouse_button = self.mouse_button_var.get()

    def _on_delay(self, value: str) -> None:
        if self._countdown_active or self._run_active:
            return
        try:
            self._start_delay = float(value)
        except ValueError:
            self._start_delay = 2.0
        self.settings.start_delay = self._start_delay
        self._update_footer_idle()

    # ------------------------------------------------------------------
    # Session limit UI
    # ------------------------------------------------------------------

    def _on_limit_mode(self, value: str) -> None:
        if self._countdown_active or self._run_active:
            return
        self._limit_mode = value
        self.settings.limit_mode = value
        self._update_limit_ui()

    def _on_limit_entry(self, _event=None) -> None:
        if self._countdown_active or self._run_active:
            return
        self._commit_limit_entry(show_error=False)

    def _commit_limit_entry(self, show_error: bool = False) -> bool:
        if self._countdown_active or self._run_active:
            return True
        text = self.limit_entry.get().strip()
        if self._limit_mode == "clicks":
            cleaned = text.replace(",", "").replace(" ", "")
            try:
                value = int(float(cleaned))
                if value <= 0:
                    raise ValueError
            except ValueError:
                if show_error:
                    messagebox.showerror(
                        "Invalid click target",
                        "Enter a whole number of clicks greater than zero.",
                        parent=self.root,
                    )
                self._write_entry(self.limit_entry, f"{self._click_limit:,}")
                return False
            self._click_limit = max(1, min(MAX_CLICKS, value))
            self._write_entry(self.limit_entry, f"{self._click_limit:,}")
        elif self._limit_mode == "duration":
            value = self._parse_duration(text)
            if value is None or value <= 0:
                if show_error:
                    messagebox.showerror(
                        "Invalid duration",
                        "Enter a duration in seconds, or use s/m/h suffixes.",
                        parent=self.root,
                    )
                self._write_entry(
                    self.limit_entry, _format_rate_input(self._duration)
                )
                return False
            self._duration = max(0.05, min(86_400.0, value))
            self._write_entry(
                self.limit_entry, _format_rate_input(self._duration)
            )
        self._update_limit_summary()
        return True

    @staticmethod
    def _parse_duration(text: str) -> float | None:
        text = text.strip().lower().replace(" ", "")
        if not text:
            return None
        multiplier = 1.0
        if text.endswith("ms"):
            text = text[:-2]
            multiplier = 0.001
        elif text.endswith("h"):
            text = text[:-1]
            multiplier = 3600.0
        elif text.endswith("m"):
            text = text[:-1]
            multiplier = 60.0
        elif text.endswith("s"):
            text = text[:-1]
        try:
            return float(text) * multiplier
        except ValueError:
            return None

    def _update_limit_ui(self) -> None:
        for child in self.quick_frame.winfo_children():
            child.destroy()
        self._quick_buttons = []

        busy = self._countdown_active or self._run_active
        enabled = not busy and self._limit_mode != "unlimited"

        if self._limit_mode == "unlimited":
            self.limit_suffix_var.set("until stopped")
            self._write_entry(self.limit_entry, "\u221e")
            self.limit_entry.config(state="disabled")
            self.progress_text_var.set("Unlimited")
            self.progress_detail_var.set("No limit - press STOP when finished.")
            self.eta_var.set("ETA --:--")
            self.progress_bar.set(0, COLORS["accent"])
            self._gauge_target = self._rate
        elif self._limit_mode == "clicks":
            self.limit_suffix_var.set("clicks")
            self._write_entry(self.limit_entry, f"{self._click_limit:,}")
            self.limit_entry.config(state="normal" if enabled else "disabled")
            for text, value in (
                ("1K", 1000),
                ("5K", 5000),
                ("10K", 10000),
                ("25K", MAX_CLICKS),
            ):
                self._make_quick_button(text, lambda v=value: self._quick_clicks(v))
            self.progress_text_var.set(f"0 / {self._click_limit:,}")
            self.progress_detail_var.set("Stops automatically at the target.")
            self.eta_var.set("ETA --:--")
            self.progress_bar.set(0, COLORS["cyan"])
        else:
            self.limit_suffix_var.set("seconds")
            self._write_entry(self.limit_entry, _format_rate_input(self._duration))
            self.limit_entry.config(state="normal" if enabled else "disabled")
            for text, value in (
                ("5s", 5),
                ("10s", 10),
                ("30s", 30),
                ("1m", 60),
                ("5m", 300),
            ):
                self._make_quick_button(text, lambda v=value: self._quick_duration(v))
            self.progress_text_var.set(f"00:00 / {fmt_clock(self._duration)}")
            self.progress_detail_var.set("Stops automatically when time is up.")
            self.eta_var.set(f"ETA {fmt_clock(self._duration)}")
            self.progress_bar.set(0, COLORS["purple"])

        self._update_limit_summary()
        self._set_controls_enabled(not busy)

    def _make_quick_button(self, label: str, command) -> None:
        button = theme.PillButton(
            self.quick_frame,
            text=label,
            command=command,
            width=45,
            height=27,
            bg=COLORS["card"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=8,
        )
        button.pack(side="left", padx=(0, 4))
        if self._countdown_active or self._run_active:
            button.set_enabled(False)
        self._quick_buttons.append(button)

    def _quick_clicks(self, value: int) -> None:
        if self._countdown_active or self._run_active:
            return
        self._click_limit = max(1, min(MAX_CLICKS, int(value)))
        self._write_entry(self.limit_entry, f"{self._click_limit:,}")
        self._update_limit_summary()

    def _quick_duration(self, value: float) -> None:
        if self._countdown_active or self._run_active:
            return
        self._duration = float(value)
        self._write_entry(self.limit_entry, _format_rate_input(value))
        self._update_limit_summary()

    def _update_limit_summary(self) -> None:
        if self._limit_mode == "unlimited":
            self.limit_summary_var.set("Runs until stopped")
        elif self._limit_mode == "clicks":
            seconds = self._click_limit / max(self._rate, 0.001)
            self.limit_summary_var.set(
                f"approx. {_format_seconds_short(seconds)} at {fmt_rate(self._rate)} CPS"
            )
        else:
            self.limit_summary_var.set(
                f"{_format_seconds_short(self._duration)} duration"
            )

    # ------------------------------------------------------------------
    # Control state
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        busy = self._countdown_active or self._run_active
        enabled = bool(enabled) and not busy

        try:
            self.rate_slider.set_enabled(enabled)
            self.cps_entry.config(state="normal" if enabled else "disabled")
        except Exception:
            pass

        for button in getattr(self, "rate_chips", []):
            button.set_enabled(enabled)

        try:
            self.mouse_segment.set_enabled(enabled)
            self.limit_segment.set_enabled(enabled)
            self.delay_segment.set_enabled(enabled)
        except Exception:
            pass

        limit_enabled = enabled and self._limit_mode != "unlimited"
        try:
            self.limit_entry.config(state="normal" if limit_enabled else "disabled")
        except Exception:
            pass

        for button in getattr(self, "_quick_buttons", []):
            button.set_enabled(limit_enabled)

        try:
            self.start_button.set_enabled(not busy)
            self.stop_button.set_enabled(busy)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start_clicking(self) -> None:
        if self.shutdown or self._countdown_active or self._run_active:
            return
        if self.engine.is_running():
            return

        if not self._commit_limit_entry(show_error=True):
            return
        if not self._validate_rate():
            return

        self.settings.cps = self._rate
        self.settings.mouse_button = self.mouse_button_var.get()
        self.settings.limit_mode = self._limit_mode
        self.settings.click_limit = self._click_limit
        self.settings.duration = self._duration
        self.settings.start_delay = self._start_delay
        self.settings.save()

        if self._start_delay > 0:
            self._begin_countdown()
        else:
            self._engage()

    def _validate_rate(self) -> bool:
        try:
            value = float(self.cps_entry.get().replace(",", "").strip())
        except ValueError:
            messagebox.showerror(
                "Invalid click rate",
                f"Enter a click rate between {int(MIN_CPS)} and {int(MAX_CPS):,} CPS.",
                parent=self.root,
            )
            return False
        if not (MIN_CPS <= value <= MAX_CPS):
            messagebox.showerror(
                "Invalid click rate",
                f"Click rate must be between {int(MIN_CPS)} and {int(MAX_CPS):,} CPS.",
                parent=self.root,
            )
            return False
        self._set_rate(value, update_entry=True)
        return True

    def _begin_countdown(self) -> None:
        self._countdown_active = True
        self._countdown_end = time.perf_counter() + max(0.0, self._start_delay)
        try:
            if self._countdown_job is not None:
                self.root.after_cancel(self._countdown_job)
        except Exception:
            pass
        self._set_status(f"STARTING IN {self._start_delay:.1f}s", "countdown")
        self.footer_title_var.set("Get ready...")
        self.footer_sub_var.set(
            f"Move the cursor to your target. {self.settings.hotkey_stop} cancels."
        )
        self._set_controls_enabled(False)
        self._countdown_job = self.root.after(50, self._countdown_tick)

    def _countdown_tick(self) -> None:
        self._countdown_job = None
        if self.shutdown or not self._countdown_active:
            return
        remaining = self._countdown_end - time.perf_counter()
        if remaining <= 0:
            self._countdown_active = False
            self._engage()
            return
        text = f"STARTING IN {remaining:.1f}s"
        self._set_status(text, "countdown")
        self.footer_title_var.set(f"Starting in {remaining:.1f}s")
        self._countdown_job = self.root.after(50, self._countdown_tick)

    def _cancel_countdown(self) -> None:
        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None
        self._countdown_active = False
        self._set_status("READY", "idle")
        self.footer_title_var.set("Ready to click")
        self._update_footer_idle()
        self._set_controls_enabled(True)

    def _engage(self) -> None:
        self._countdown_active = False
        config = EngineConfig(
            cps=self._rate,
            button=self.mouse_button_var.get(),
            limit_mode=self._limit_mode,
            click_limit=self._click_limit,
            duration=self._duration,
            delivery=self.settings.delivery,
            jitter_percent=self.settings.jitter_percent,
            position_mode=self.settings.position_mode,
            fixed_x=self.settings.fixed_x,
            fixed_y=self.settings.fixed_y,
            performance_mode=self.settings.performance_mode,
        )

        if not self.engine.start(config):
            self._set_status("ERROR", "error")
            self.footer_title_var.set("Could not start")
            self.footer_sub_var.set("The previous run is still shutting down. Try again.")
            self._set_controls_enabled(True)
            return

        stats = self.engine.get_stats()
        self._active_session_id = stats.session_id
        self._run_active = True
        self._session_committed = False
        self._auto_minimized = False
        self._reset_sampling()
        self._rate_history.clear()
        self.sparkline.clear()
        self.sparkline.set_data([], self._rate)
        self._peak_cps = 0.0
        self._current_cps = 0.0
        self._set_status("RUNNING", "running")
        self.footer_title_var.set("Clicking")
        self.footer_sub_var.set(self._run_subtitle())
        self._set_controls_enabled(False)
        self._refresh_engine_strip()

        if self.settings.minimize_on_start:
            self._auto_minimized = True
            self.root.after(120, self._minimize_for_run)

    def _minimize_for_run(self) -> None:
        if self._run_active and not self.shutdown:
            try:
                self.root.iconify()
            except Exception:
                pass

    def _run_subtitle(self) -> str:
        if self._limit_mode == "clicks":
            goal = f"{self._click_limit:,} click target"
        elif self._limit_mode == "duration":
            goal = f"{fmt_clock(self._duration)} duration"
        else:
            goal = "unlimited run"
        return (
            f"{goal}  \u00b7  {fmt_rate(self._rate)} CPS  \u00b7  "
            f"{self.settings.delivery.title()} delivery"
        )

    def stop_clicking(self) -> None:
        if self._countdown_active:
            self._cancel_countdown()
            return
        if self.engine.is_running():
            self.engine.stop()
            self._set_status("STOPPING", "stopping")
            self.footer_title_var.set("Stopping...")
            self.footer_sub_var.set("Finishing the current batch.")
            self.stop_button.set_enabled(False)

    # ------------------------------------------------------------------
    # Engine events / session completion
    # ------------------------------------------------------------------

    def _poll_engine_events(self) -> None:
        for event in self.engine.poll_events():
            if event.session_id != self._active_session_id:
                continue
            if event.kind == "error":
                self._finish_session(
                    reason="error",
                    error_message=event.message,
                    clicks=event.clicks_sent,
                )
            else:
                self._finish_session(
                    reason=event.reason,
                    error_message="",
                    clicks=event.clicks_sent,
                )

    def _finish_session(self, reason: str, error_message: str, clicks: int) -> None:
        if not self._run_active and self._session_committed:
            return

        stats = self.engine.get_stats()
        elapsed = stats.elapsed
        self._run_active = False
        self._countdown_active = False

        if not self._session_committed:
            self.settings.total_clicks += int(stats.clicks_sent)
            self._session_committed = True
            self.settings.save()

        self._set_controls_enabled(True)
        self._refresh_engine_strip()

        if reason == "target":
            self._set_status("TARGET REACHED", "done")
            self.footer_title_var.set(
                f"Target reached - {self._click_limit:,} clicks"
            )
            self.footer_sub_var.set(
                f"{elapsed:.2f}s elapsed  \u00b7  peak {fmt_rate(self._peak_cps)} CPS"
            )
            if self._auto_minimized:
                self.root.bell()
        elif reason == "duration":
            self._set_status("TIME COMPLETE", "done")
            self.footer_title_var.set("Duration complete")
            self.footer_sub_var.set(
                f"{stats.clicks_sent:,} clicks in {elapsed:.2f}s  \u00b7  "
                f"peak {fmt_rate(self._peak_cps)} CPS"
            )
            if self._auto_minimized:
                self.root.bell()
        elif reason == "error":
            self._set_status("ERROR", "error")
            self.footer_title_var.set("Click engine error")
            self.footer_sub_var.set(error_message or "The run stopped unexpectedly.")
            if self._auto_minimized:
                try:
                    self.root.deiconify()
                except Exception:
                    pass
            self._auto_minimized = False
            try:
                messagebox.showerror(
                    "ClickStorm engine error",
                    error_message or "The click engine stopped unexpectedly.",
                    parent=self.root,
                )
            except Exception:
                pass
        else:
            self._set_status("STOPPED", "idle")
            self.footer_title_var.set("Run stopped")
            self.footer_sub_var.set(
                f"{stats.clicks_sent:,} clicks in {elapsed:.2f}s  \u00b7  "
                f"{fmt_rate(stats.average_cps)} avg CPS"
            )
            if self._auto_minimized:
                try:
                    self.root.deiconify()
                except Exception:
                    pass
            self._auto_minimized = False

        self._current_cps = 0.0
        self._update_gauge_idle()
        self._update_stats_display(stats, final=True)

    # ------------------------------------------------------------------
    # Live updates / sampling
    # ------------------------------------------------------------------

    def _reset_sampling(self) -> None:
        self._sample_time = time.perf_counter()
        self._sample_clicks = 0
        self._last_ui_stats_time = 0.0

    def _ui_tick(self) -> None:
        self._ui_job = None
        if self.shutdown:
            return
        try:
            self._poll_engine_events()

            now = time.perf_counter()
            if now - self._last_ui_stats_time >= 0.05:
                stats = self.engine.get_stats()
                if stats.session_id != self._active_session_id and not stats.running:
                    # The engine has no session yet (startup) or a new one is
                    # about to begin. Keep the dashboard showing idle values.
                    pass
                if stats.running:
                    self._sample_stats(stats, now)
                    self._update_stats_display(stats, final=False)
                elif self._run_active:
                    self._sample_stats(stats, now)
                    self._update_stats_display(stats, final=False)
                elif stats.clicks_sent and self._session_committed:
                    # Leave the completed run's results on screen until the
                    # next run begins or a setting changes.
                    pass
                else:
                    self._update_idle_display()
                self._last_ui_stats_time = now
        except Exception as exc:
            _log_exception("UI tick", exc)
        finally:
            if not self.shutdown:
                self._ui_job = self.root.after(
                    self.UI_INTERVAL_MS, self._ui_tick
                )

    def _sample_stats(self, stats, now: float) -> None:
        delta_time = now - self._sample_time
        if delta_time < 0.10:
            return
        delta_clicks = max(0, stats.clicks_sent - self._sample_clicks)
        raw_cps = delta_clicks / delta_time
        if self._current_cps <= 0:
            self._current_cps = raw_cps
        else:
            self._current_cps += 0.45 * (raw_cps - self._current_cps)
        self._peak_cps = max(self._peak_cps, raw_cps)
        self._rate_history.append(raw_cps)
        self._sparkline_dirty = True
        self._sample_time = now
        self._sample_clicks = stats.clicks_sent

    def _update_stats_display(self, stats, final: bool = False) -> None:
        target = self._rate
        running = bool(stats.running)
        display_cps = self._current_cps if running else 0.0
        self.session_big_var.set(fmt_int(stats.clicks_sent))
        self.elapsed_big_var.set(fmt_clock(stats.elapsed))

        active_clicks = (
            stats.clicks_sent if not self._session_committed else 0
        )
        self.stat_vars["lifetime"].set(
            fmt_int(self.settings.total_clicks + active_clicks)
        )
        if self._limit_mode == "clicks":
            self.stat_vars["target"].set(f"{self._click_limit:,}")
            remaining = max(0, self._click_limit - stats.clicks_sent)
            self.stat_vars["remaining"].set(f"{remaining:,}")
        elif self._limit_mode == "duration":
            self.stat_vars["target"].set(fmt_clock(self._duration))
            remaining = max(0.0, self._duration - stats.elapsed)
            self.stat_vars["remaining"].set(fmt_clock(remaining))
        else:
            self.stat_vars["target"].set("Unlimited")
            self.stat_vars["remaining"].set("\u221e")

        average = stats.average_cps
        self.stat_vars["average"].set(f"{fmt_rate(average)}")
        self.stat_vars["peak"].set(f"{fmt_rate(self._peak_cps)}")
        self.stat_vars["calls"].set(fmt_int(stats.send_calls))
        self.stat_vars["inputs"].set(fmt_int(stats.input_events))
        self.stat_vars["dropped"].set(fmt_int(stats.failed_events))

        if self._limit_mode == "clicks":
            fraction = min(1.0, stats.clicks_sent / max(1, self._click_limit))
            self.progress_text_var.set(
                f"{stats.clicks_sent:,} / {self._click_limit:,}"
            )
            self.progress_detail_var.set("Stops automatically at the target.")
            if average > 0 and remaining > 0:
                eta = remaining / average
                self.eta_var.set(f"ETA {fmt_clock(eta)}")
            elif remaining <= 0:
                self.eta_var.set("ETA 00:00")
            else:
                self.eta_var.set("ETA --:--")
            self.progress_bar.set(fraction, COLORS["cyan"])
        elif self._limit_mode == "duration":
            fraction = min(1.0, stats.elapsed / max(0.01, self._duration))
            self.progress_text_var.set(
                f"{fmt_clock(stats.elapsed)} / {fmt_clock(self._duration)}"
            )
            self.progress_detail_var.set("Stops automatically when time is up.")
            remaining_time = max(0.0, self._duration - stats.elapsed)
            self.eta_var.set(f"ETA {fmt_clock(remaining_time)}")
            self.progress_bar.set(fraction, COLORS["purple"])
        else:
            self.progress_text_var.set(f"{stats.clicks_sent:,} clicks")
            self.progress_detail_var.set("No limit - press STOP when finished.")
            self.eta_var.set("ETA --:--")
            self.progress_bar.set(0, COLORS["accent"])

        if self._sparkline_dirty or final:
            self.sparkline.set_data(list(self._rate_history), target)
            self._sparkline_dirty = False

        gauge_max = max(target, display_cps, 1.0)
        self.gauge.set(
            display_cps,
            gauge_max,
            target,
            running,
            subtitle=f"TARGET {fmt_rate(target)} CPS",
        )

        if running:
            self.footer_title_var.set(
                f"Clicking - {fmt_rate(display_cps)} CPS actual"
            )
            caught_up = (
                f"  \u00b7  {stats.catchup_clicks:,} caught up"
                if stats.catchup_clicks
                else ""
            )
            self.footer_sub_var.set(
                f"{self._run_subtitle()}  \u00b7  {fmt_int(stats.clicks_sent)} clicks"
                + caught_up
            )

    def _update_idle_display(self) -> None:
        stats = self.engine.get_stats()
        if stats.clicks_sent and not self._session_committed:
            self.session_big_var.set(fmt_int(stats.clicks_sent))
            self.elapsed_big_var.set(fmt_clock(stats.elapsed))
        else:
            self.session_big_var.set("0")
            self.elapsed_big_var.set("00:00")
        self.stat_vars["lifetime"].set(fmt_int(self.settings.total_clicks))
        if self._limit_mode == "clicks":
            self.stat_vars["target"].set(f"{self._click_limit:,}")
            self.stat_vars["remaining"].set(f"{self._click_limit:,}")
        elif self._limit_mode == "duration":
            self.stat_vars["target"].set(fmt_clock(self._duration))
            self.stat_vars["remaining"].set(fmt_clock(self._duration))
        else:
            self.stat_vars["target"].set("Unlimited")
            self.stat_vars["remaining"].set("\u221e")
        self.stat_vars["average"].set("0")
        self.stat_vars["peak"].set(fmt_rate(self._peak_cps))
        self.stat_vars["calls"].set("0")
        self.stat_vars["inputs"].set("0")
        self.stat_vars["dropped"].set("0")
        self.progress_text_var.set(
            f"0 / {self._click_limit:,}"
            if self._limit_mode == "clicks"
            else f"00:00 / {fmt_clock(self._duration)}"
            if self._limit_mode == "duration"
            else "Unlimited"
        )
        self.progress_detail_var.set(
            "Stops automatically at the target."
            if self._limit_mode == "clicks"
            else "Stops automatically when time is up."
            if self._limit_mode == "duration"
            else "No limit - press STOP when finished."
        )
        self.eta_var.set(
            f"ETA {fmt_clock(self._duration)}"
            if self._limit_mode == "duration"
            else "ETA --:--"
        )
        self.progress_bar.set(0, COLORS["accent"])
        if self._sparkline_dirty:
            self.sparkline.set_data(list(self._rate_history), self._rate)
            self._sparkline_dirty = False
        self._update_gauge_idle()

    def _update_gauge_idle(self) -> None:
        self.gauge.set(
            0,
            max(self._rate, 1),
            self._rate,
            False,
            subtitle=f"TARGET {fmt_rate(self._rate)} CPS",
        )

    # ------------------------------------------------------------------
    # Global hotkeys
    # ------------------------------------------------------------------

    def _hotkey_tick(self) -> None:
        self._hotkey_job = None
        if self.shutdown:
            return
        try:
            self._poll_hotkeys()
        except Exception as exc:
            _log_exception("Hotkey tick", exc)
        finally:
            if not self.shutdown:
                self._hotkey_job = self.root.after(
                    self.HOTKEY_INTERVAL_MS, self._hotkey_tick
                )

    def _poll_hotkeys(self) -> None:
        if not self.settings.hotkeys_enabled or self.shutdown:
            return
        start_vk = winapi.HOTKEY_KEYS.get(self.settings.hotkey_start)
        stop_vk = winapi.HOTKEY_KEYS.get(self.settings.hotkey_stop)
        start_pressed = False
        stop_pressed = False

        for vk, kind in ((start_vk, "start"), (stop_vk, "stop")):
            if vk is None:
                continue
            down, tapped = winapi.key_pressed(vk)
            # ``tapped`` catches a press+release that happened between polls;
            # the set catches a key that is still held from before.
            if tapped or (down and vk not in self._hotkey_keys_down):
                if kind == "start":
                    start_pressed = True
                else:
                    stop_pressed = True
            if down:
                self._hotkey_keys_down.add(vk)
            else:
                self._hotkey_keys_down.discard(vk)

        if stop_pressed and (self._run_active or self._countdown_active):
            self.stop_clicking()
        if start_pressed:
            if not self._settings_open:
                self.start_clicking()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def open_settings(self) -> None:
        if self._settings_open and self._settings_dialog is not None:
            try:
                self._settings_dialog.lift()
                self._settings_dialog.focus_force()
            except Exception:
                pass
            return

        self._settings_open = True
        dialog = SettingsDialog(
            self.root,
            self.settings,
            on_save=self._on_settings_saved,
        )
        self._settings_dialog = dialog
        dialog.bind(
            "<Destroy>",
            lambda event: self._settings_destroyed(event.widget, dialog),
            add="+",
        )

    def _settings_destroyed(self, widget, dialog) -> None:
        if widget is dialog:
            self._settings_open = False
            self._settings_dialog = None

    def _on_settings_saved(self, new_settings: Settings) -> None:
        old_cps = self._rate
        self.settings = new_settings
        self.settings.cps = old_cps
        self._start_delay = self.settings.start_delay
        self.delay_var.set(f"{self._start_delay:.1f}")
        try:
            self.delay_segment._refresh()
        except Exception:
            pass
        self._sync_from_settings()
        self._update_rate_hint()
        self._update_limit_summary()
        self._update_footer_idle()
        self.settings.save()
        if not self._run_active and not self._countdown_active:
            self._update_stats_display(self.engine.get_stats(), final=True)

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    def open_updates(self) -> None:
        if self._update_open and self._update_dialog is not None:
            try:
                self._update_dialog.lift()
                self._update_dialog.focus_force()
            except Exception:
                pass
            return

        self._update_open = True
        dialog = UpdateDialog(self.root, on_install=self.close_for_update)
        self._update_dialog = dialog
        dialog.bind(
            "<Destroy>",
            lambda event: self._update_destroyed(event.widget, dialog),
            add="+",
        )

    def _update_destroyed(self, widget, dialog) -> None:
        if widget is dialog:
            self._update_open = False
            self._update_dialog = None

    def close_for_update(self) -> None:
        """Close ClickStorm so a freshly downloaded installer can run."""
        try:
            self.close()
        except Exception:
            _log_exception("Closing for an update")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self.shutdown:
            return
        self.shutdown = True

        if self._countdown_job is not None:
            try:
                self.root.after_cancel(self._countdown_job)
            except Exception:
                pass
            self._countdown_job = None
        if self._ui_job is not None:
            try:
                self.root.after_cancel(self._ui_job)
            except Exception:
                pass
            self._ui_job = None
        if self._hotkey_job is not None:
            try:
                self.root.after_cancel(self._hotkey_job)
            except Exception:
                pass
            self._hotkey_job = None

        if self._run_active and not self._session_committed:
            try:
                stats = self.engine.get_stats()
                self.settings.total_clicks += int(stats.clicks_sent)
                self._session_committed = True
            except Exception:
                pass

        try:
            self.engine.stop()
        except Exception:
            pass

        try:
            self.settings.cps = self._rate
            self.settings.mouse_button = self.mouse_button_var.get()
            self.settings.limit_mode = self._limit_mode
            self.settings.click_limit = self._click_limit
            self.settings.duration = self._duration
            self.settings.start_delay = self._start_delay
            self.settings.window_geometry = self.root.geometry()
            self.settings.save()
        except Exception:
            _log_exception("Saving settings on close")

        try:
            self.engine.close()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # No console/PowerShell window should ever accompany the app window.
    winapi.detach_console()
    winapi.enable_dpi_awareness()

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ClickStorm.UltraAutoClicker.2"
        )
    except Exception:
        pass

    settings = Settings.load()

    root = tk.Tk()
    root.withdraw()
    root.title(f"{APP_NAME} - {APP_TAGLINE}")
    root.configure(bg=COLORS["bg"])
    root.minsize(1050, 700)
    theme.init_fonts(root)
    theme.init_ttk(root)
    _restore_window(root, settings.window_geometry)

    def report_callback_exception(exc_type, exc_value, exc_tb):
        _log_exception("Tk callback", exc_value)
        try:
            messagebox.showerror(
                "ClickStorm error",
                f"{exc_type.__name__}: {exc_value}\n\n"
                "Details were written to the ClickStorm error log.",
                parent=root,
            )
        except Exception:
            pass

    root.report_callback_exception = report_callback_exception

    try:
        app = ClickStormApp(root, settings)
    except Exception as exc:
        _log_exception("Startup", exc)
        try:
            messagebox.showerror(
                "ClickStorm could not start",
                str(exc),
                parent=root,
            )
        finally:
            root.destroy()
        return

    root.deiconify()
    root.update_idletasks()
    app.fit_initial_window()
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    root.mainloop()

    # If the user closed the window through some other path, make sure the
    # engine thread and timer resolution are cleaned up.
    if not app.shutdown:
        try:
            app.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
