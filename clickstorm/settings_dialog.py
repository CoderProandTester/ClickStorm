"""Advanced settings window for ClickStorm."""

from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from tkinter import messagebox, ttk

from . import APP_NAME, APP_VERSION
from . import theme, winapi
from .settings import Settings, settings_path
from .theme import COLORS


class SettingsDialog(tk.Toplevel):
    WIDTH = 840
    HEIGHT = 664

    def __init__(self, parent, settings: Settings, on_save):
        super().__init__(parent)
        self.withdraw()
        self.title(f"{APP_NAME} - Settings")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self._parent = parent
        self._settings = settings
        self._on_save = on_save
        self._capture_job = None
        self._capture_count = 0
        self._closed = False

        self.delivery_var = tk.StringVar(value=settings.delivery)
        self.position_var = tk.StringVar(value=settings.position_mode)
        self.hotkeys_enabled_var = tk.BooleanVar(value=settings.hotkeys_enabled)
        self.performance_var = tk.BooleanVar(value=settings.performance_mode)
        self.topmost_var = tk.BooleanVar(value=settings.always_on_top)
        self.minimize_var = tk.BooleanVar(value=settings.minimize_on_start)

        self.delay_var = tk.StringVar(value=f"{settings.start_delay:.1f}")
        self.jitter_var = tk.StringVar(value=f"{settings.jitter_percent * 100:.0f}")
        self.x_var = tk.StringVar(value=str(settings.fixed_x))
        self.y_var = tk.StringVar(value=str(settings.fixed_y))
        self.start_key_var = tk.StringVar(value=settings.hotkey_start)
        self.stop_key_var = tk.StringVar(value=settings.hotkey_stop)
        self.lifetime_var = tk.StringVar(
            value=f"{settings.total_clicks:,} clicks"
        )
        self._total_clicks = settings.total_clicks

        self._build()
        self._position()
        self.bind("<Escape>", lambda _e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.deiconify()
        self.grab_set()
        self.focus_force()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        outer = tk.Frame(self, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x", pady=(0, 14))
        theme.make_label(
            header, "Advanced settings", size=20, bold=True,
            bg=COLORS["bg"],
        ).pack(anchor="w")
        theme.make_label(
            header,
            "Fine-tune the engine. Changes apply to your next run.",
            size=9, color=COLORS["muted"], bg=COLORS["bg"],
        ).pack(anchor="w", pady=(2, 0))

        body = tk.Frame(outer, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1, uniform="settings")
        body.grid_columnconfigure(1, weight=1, uniform="settings")

        left = tk.Frame(body, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(body, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._build_delivery(left)
        self._build_timing(left)
        self._build_position(left)

        self._build_hotkeys(right)
        self._build_performance(right)
        self._build_data(right)

        footer = tk.Frame(outer, bg=COLORS["bg"])
        footer.pack(fill="x", pady=(16, 0))

        theme.PillButton(
            footer,
            text="CANCEL",
            command=self.cancel,
            width=130,
            height=42,
            bg=COLORS["bg"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=10,
        ).pack(side="right")

        theme.PillButton(
            footer,
            text="SAVE CHANGES",
            command=self.save,
            width=190,
            height=42,
            bg=COLORS["bg"],
            fill=COLORS["accent"],
            hover=COLORS["cyan"],
            active=COLORS["accent"],
            fg=COLORS["bg"],
            font_size=10,
        ).pack(side="right", padx=(0, 10))

        theme.make_label(
            footer,
            "Hotkeys work globally, even while ClickStorm is minimized.",
            size=8, color=COLORS["faint"], bg=COLORS["bg"],
        ).pack(side="left", pady=6)

    def _section(self, parent, title, subtitle=None):
        card = theme.Card(parent)
        card.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="x", padx=16, pady=13)
        theme.make_label(
            inner, title.upper(), size=8, bold=True, color=COLORS["muted"]
        ).pack(anchor="w")
        if subtitle:
            theme.make_label(
                inner, subtitle, size=8, color=COLORS["faint"],
                justify="left", wraplength=350,
            ).pack(anchor="w", pady=(2, 0))
        return inner

    def _build_delivery(self, parent):
        inner = self._section(parent, "Delivery")
        self.delivery_segment = theme.SegmentedControl(
            inner,
            options=(
                ("sendinput", "SENDINPUT"),
                ("direct", "DIRECT"),
                ("auto", "AUTO"),
            ),
            variable=self.delivery_var,
            bg=COLORS["card"],
            button_width=104,
            button_height=32,
            font_size=8,
            accent=COLORS["accent"],
        )
        self.delivery_segment.pack(anchor="w", pady=(9, 5))
        theme.make_label(
            inner,
            "SendInput works with everything, but Windows caps real injected "
            "input near 6,000 CPS. Direct posts button messages straight to "
            "the window, which reaches the full 25,000 CPS but only works "
            "with classic Win32 windows. AUTO uses Direct above 6,000 CPS and "
            "SendInput below it.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=340,
        ).pack(anchor="w")

    def _build_timing(self, parent):
        inner = self._section(
            parent,
            "Timing",
            "Precision pacing uses a high-resolution timer and a dedicated thread.",
        )
        row = tk.Frame(inner, bg=COLORS["card"])
        row.pack(fill="x", pady=(8, 0))
        theme.make_label(
            row, "Start delay", size=9, color=COLORS["text_dim"]
        ).pack(side="left")
        self._spinbox(
            row, self.delay_var, minimum=0, maximum=10, increment=0.5,
            width=6, fmt="%.1f",
        ).pack(side="right")
        theme.make_label(
            row, "seconds", size=8, color=COLORS["faint"]
        ).pack(side="right", padx=(0, 6))

        row2 = tk.Frame(inner, bg=COLORS["card"])
        row2.pack(fill="x", pady=(8, 0))
        theme.make_label(
            row2, "Timing jitter", size=9, color=COLORS["text_dim"]
        ).pack(side="left")
        self._spinbox(
            row2, self.jitter_var, minimum=0, maximum=75, increment=5,
            width=6, fmt="%.0f",
        ).pack(side="right")
        theme.make_label(
            row2, "%", size=8, color=COLORS["faint"]
        ).pack(side="right", padx=(0, 6))

        theme.make_label(
            inner,
            "Jitter varies the rhythm while keeping the average CPS stable.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
        ).pack(anchor="w", pady=(7, 0))

    def _build_position(self, parent):
        inner = self._section(parent, "Click position")
        self.position_segment = theme.SegmentedControl(
            inner,
            options=(("cursor", "FOLLOW CURSOR"), ("fixed", "FIXED POINT")),
            variable=self.position_var,
            bg=COLORS["card"],
            button_width=138,
            button_height=32,
            font_size=8,
        )
        self.position_segment.pack(anchor="w", pady=(9, 8))

        row = tk.Frame(inner, bg=COLORS["card"])
        row.pack(fill="x")
        theme.make_label(
            row, "X", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self._entry(row, self.x_var, width=6).pack(side="left", padx=(5, 12))
        theme.make_label(
            row, "Y", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self._entry(row, self.y_var, width=6).pack(side="left", padx=(5, 12))
        self._capture_button = theme.PillButton(
            row,
            text="CAPTURE IN 3s",
            command=self._start_capture,
            width=132,
            height=30,
            bg=COLORS["card"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=8,
        )
        self._capture_button.pack(side="right")

        theme.make_label(
            inner,
            "Fixed point returns the cursor to these coordinates while clicking.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=340,
        ).pack(anchor="w", pady=(7, 0))

    def _build_hotkeys(self, parent):
        inner = self._section(parent, "Global hotkeys")
        theme.CheckPill(
            inner,
            "Enable global hotkeys",
            self.hotkeys_enabled_var,
            bg=COLORS["card"],
        ).pack(anchor="w", pady=(7, 8))

        for label, var in (
            ("Start", self.start_key_var),
            ("Stop", self.stop_key_var),
        ):
            row = tk.Frame(inner, bg=COLORS["card"])
            row.pack(fill="x", pady=2)
            theme.make_label(
                row, label, size=9, color=COLORS["text_dim"]
            ).pack(side="left")
            combo = ttk.Combobox(
                row,
                textvariable=var,
                values=list(winapi.HOTKEY_KEYS.keys()),
                state="readonly",
                width=12,
                style="Dark.TCombobox",
                font=theme.font(9),
            )
            combo.pack(side="right")
        theme.make_label(
            inner,
            "The stop hotkey is checked first, so it always wins.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
        ).pack(anchor="w", pady=(7, 0))

    def _build_performance(self, parent):
        inner = self._section(parent, "Performance & window")
        theme.CheckPill(
            inner,
            "Performance mode (thread priority boost)",
            self.performance_var,
            bg=COLORS["card"],
        ).pack(anchor="w", pady=(7, 4))
        theme.CheckPill(
            inner,
            "Always keep ClickStorm on top",
            self.topmost_var,
            bg=COLORS["card"],
        ).pack(anchor="w", pady=4)
        theme.CheckPill(
            inner,
            "Minimize when a run starts",
            self.minimize_var,
            bg=COLORS["card"],
        ).pack(anchor="w", pady=4)
        theme.make_label(
            inner,
            "ClickStorm asks Windows for a high-resolution timer only while clicking.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
        ).pack(anchor="w", pady=(6, 0))

    def _build_data(self, parent):
        inner = self._section(parent, "Data")
        row = tk.Frame(inner, bg=COLORS["card"])
        row.pack(fill="x", pady=(7, 0))
        theme.make_label(
            row, "Lifetime clicks", size=9, color=COLORS["text_dim"]
        ).pack(side="left")
        self._lifetime_label = theme.make_label(
            row,
            self.lifetime_var.get(),
            size=10, bold=True, color=COLORS["green"],
        )
        self._lifetime_label.pack(side="right")
        self._lifetime_var = self.lifetime_var

        reset = tk.Button(
            inner,
            text="Reset lifetime total",
            command=self._reset_lifetime,
            bg=COLORS["card2"],
            fg=COLORS["red"],
            activebackground=COLORS["red_deep"],
            activeforeground=COLORS["red"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=theme.font(8),
            cursor="hand2",
            padx=8,
            pady=3,
        )
        reset.pack(anchor="w", pady=(8, 4))

        theme.make_label(
            inner,
            f"{APP_NAME} v{APP_VERSION}  |  Win32 per-click input",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
        ).pack(anchor="w", pady=(2, 0))
        path_text = str(settings_path())
        theme.make_label(
            inner,
            f"Settings: {path_text}",
            size=7, color=COLORS["faint"], bg=COLORS["card"],
            wraplength=340, justify="left",
        ).pack(anchor="w", pady=(3, 0))

    # ------------------------------------------------------------------
    # Small widgets
    # ------------------------------------------------------------------

    def _spinbox(self, parent, variable, minimum, maximum, increment, width, fmt):
        return tk.Spinbox(
            parent,
            from_=minimum,
            to=maximum,
            increment=increment,
            textvariable=variable,
            width=width,
            format=fmt,
            justify="center",
            bg=COLORS["card2"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            buttonbackground=COLORS["card3"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=theme.font(9, "bold"),
        )

    def _entry(self, parent, variable, width=6):
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            justify="center",
            bg=COLORS["card2"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=theme.font(9, "bold"),
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _start_capture(self):
        self._cancel_capture()
        self._capture_count = 3
        self._capture_button.set_enabled(False)
        self._capture_button.set_text("CAPTURING IN 3...")
        self._capture_tick()

    def _capture_tick(self):
        if self._closed:
            return
        self._capture_count -= 1
        if self._capture_count > 0:
            self._capture_button.set_text(f"CAPTURING IN {self._capture_count}...")
            self._capture_job = self.after(1000, self._capture_tick)
            return
        point = winapi.get_cursor_pos()
        if point:
            self.x_var.set(str(point[0]))
            self.y_var.set(str(point[1]))
        self._capture_job = None
        self._capture_button.set_text("CAPTURE IN 3s")
        self._capture_button.set_enabled(True)

    def _cancel_capture(self):
        if self._capture_job is not None:
            try:
                self.after_cancel(self._capture_job)
            except Exception:
                pass
            self._capture_job = None
        if hasattr(self, "_capture_button"):
            self._capture_button.set_enabled(True)
            self._capture_button.set_text("CAPTURE IN 3s")

    def _reset_lifetime(self):
        if messagebox.askyesno(
            "Reset lifetime clicks",
            "Reset the lifetime click counter to zero?",
            parent=self,
        ):
            self._total_clicks = 0
            self.lifetime_var.set("0 clicks")

    def save(self):
        try:
            delay = float(self.delay_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid start delay",
                "Start delay must be a number between 0 and 10 seconds.",
                parent=self,
            )
            return

        try:
            jitter = float(self.jitter_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid jitter",
                "Jitter must be a number between 0 and 75 percent.",
                parent=self,
            )
            return

        try:
            fixed_x = int(self.x_var.get())
            fixed_y = int(self.y_var.get())
        except ValueError:
            messagebox.showerror(
                "Invalid point",
                "Fixed point X and Y must be whole numbers.",
                parent=self,
            )
            return

        if self.start_key_var.get() == self.stop_key_var.get():
            messagebox.showerror(
                "Hotkey conflict",
                "The start and stop hotkeys must be different.",
                parent=self,
            )
            return

        fixed_x, fixed_y = winapi.clamp_to_screen(fixed_x, fixed_y)
        updated = replace(
            self._settings,
            delivery=self.delivery_var.get(),
            position_mode=self.position_var.get(),
            fixed_x=fixed_x,
            fixed_y=fixed_y,
            start_delay=max(0.0, min(10.0, delay)),
            jitter_percent=max(0.0, min(0.75, jitter / 100.0)),
            hotkeys_enabled=bool(self.hotkeys_enabled_var.get()),
            hotkey_start=self.start_key_var.get(),
            hotkey_stop=self.stop_key_var.get(),
            performance_mode=bool(self.performance_var.get()),
            always_on_top=bool(self.topmost_var.get()),
            minimize_on_start=bool(self.minimize_var.get()),
            total_clicks=self._total_clicks,
        )
        self._cancel_capture()
        try:
            self.grab_release()
        except Exception:
            pass
        self._closed = True
        self.destroy()
        self._on_save(updated)

    def cancel(self):
        self._cancel_capture()
        self._closed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _position(self):
        self.update_idletasks()
        width, height = self.WIDTH, self.HEIGHT
        parent = self._parent
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
        except Exception:
            px = py = 0
            pw = ph = 800
        x = max(0, px + (pw - width) // 2)
        y = max(0, py + (ph - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
