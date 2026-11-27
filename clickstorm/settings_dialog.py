"""Advanced settings window for ClickStorm.

The full settings set is taller than a 768p screen, so the window is
resizable and only the two setting columns scroll: the header and the save
footer stay pinned. That is what keeps SAVE always on screen instead of being
cut off at the bottom of a fixed-size window.

SAVE writes ``%APPDATA%\\ClickStorm\\settings.json`` before the window closes
and reports a write failure in place, so a change can never disappear just
because the dialog closed first.
"""

from __future__ import annotations

import os
import tkinter as tk
from dataclasses import replace
from tkinter import messagebox, ttk

from . import APP_NAME, APP_VERSION
from . import theme, winapi
from .settings import Settings, last_save_error, settings_path
from .theme import COLORS


class SettingsDialog(tk.Toplevel):
    WIDTH = 900
    HEIGHT = 740
    MIN_WIDTH = 720
    MIN_HEIGHT = 520
    PAD = 22

    def __init__(self, parent, settings: Settings, on_save):
        super().__init__(parent)
        self.withdraw()
        self.title(f"{APP_NAME} - Settings")
        self.configure(bg=COLORS["bg"])
        self.resizable(True, True)
        self.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.transient(parent)
        self._parent = parent
        self._settings = settings
        self._on_save = on_save
        self._capture_job = None
        self._capture_count = 0
        self._closed = False

        self.delivery_var = tk.StringVar(value=settings.delivery)
        self.engine_var = tk.StringVar(value=getattr(settings, "engine", "rust"))
        self.pin_window_var = tk.BooleanVar(value=settings.pin_window)
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
        # Keep the lifetime counter as a number. The previous dialog stored the
        # whole Settings object here, which made the next load discard the
        # counter (and wrote a nested object into settings.json).
        self._total_clicks = max(0, int(settings.total_clicks))

        self._build()
        self._position()
        self.bind("<Escape>", lambda _e: self.cancel())
        self.bind("<Control-s>", lambda _e: self.save())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.deiconify()
        self.grab_set()
        self.focus_force()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self):
        outer = tk.Frame(self, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True)

        self._build_header(outer)
        # Packed before the body so the save row can never be pushed off the
        # bottom of the window.
        self._build_footer(outer)
        self._build_body(outer)

    def _build_header(self, outer):
        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x", padx=self.PAD, pady=(16, 12))

        block = tk.Frame(header, bg=COLORS["bg"])
        block.pack(side="left")
        theme.make_label(
            block, "Settings", size=20, bold=True, bg=COLORS["bg"]
        ).pack(anchor="w")
        theme.make_label(
            block,
            "One engine, tuned your way. Changes are written to disk.",
            size=9, color=COLORS["muted"], bg=COLORS["bg"],
        ).pack(anchor="w", pady=(2, 0))

        badge = tk.Frame(
            header,
            bg=COLORS["card2"],
            highlightbackground=COLORS["border_soft"],
            highlightthickness=1,
        )
        badge.pack(side="right")
        theme.make_label(
            badge,
            f"v{APP_VERSION}",
            size=8, bold=True, color=COLORS["accent"], bg=COLORS["card2"],
        ).pack(padx=10, pady=5)

        tk.Frame(outer, bg=COLORS["border_soft"], height=1).pack(
            fill="x", padx=self.PAD
        )

    def _build_footer(self, outer):
        footer = tk.Frame(
            outer,
            bg=COLORS["panel"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        inner = tk.Frame(footer, bg=COLORS["panel"])
        inner.pack(fill="x", padx=self.PAD, pady=12)

        self.save_button = theme.PillButton(
            inner,
            text="SAVE SETTINGS",
            command=self.save,
            width=200,
            height=44,
            bg=COLORS["panel"],
            fill=COLORS["accent"],
            hover=COLORS["cyan"],
            active=COLORS["accent"],
            fg=COLORS["bg"],
            font_size=10,
        )
        self.save_button.pack(side="right")

        theme.PillButton(
            inner,
            text="CANCEL",
            command=self.cancel,
            width=130,
            height=44,
            bg=COLORS["panel"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=10,
        ).pack(side="right", padx=(0, 10))

        self._path_label = tk.Label(
            inner,
            text=f"Saved to {settings_path()}",
            bg=COLORS["panel"],
            fg=COLORS["faint"],
            font=theme.font(8),
            justify="left",
            anchor="w",
            wraplength=300,
        )
        self._path_label.pack(side="left", fill="x", expand=True)

    def _build_body(self, outer):
        body = tk.Frame(outer, bg=COLORS["bg"])
        body.pack(fill="both", expand=True, padx=(self.PAD, 12), pady=(0, 0))

        self.canvas = tk.Canvas(
            body,
            bg=COLORS["bg"],
            highlightthickness=0,
            bd=0,
            yscrollincrement=28,
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.scrollbar = theme.SlimScrollbar(
            body, command=self.canvas.yview, bg=COLORS["bg"]
        )
        self.scrollbar.pack(side="right", fill="y", padx=(8, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

        self.inner = tk.Frame(self.canvas, bg=COLORS["bg"])
        self._inner_window = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        content = tk.Frame(self.inner, bg=COLORS["bg"])
        content.pack(fill="both", expand=True, pady=(14, 6))
        content.grid_columnconfigure(0, weight=1, uniform="settingscol")
        content.grid_columnconfigure(1, weight=1, uniform="settingscol")

        left = tk.Frame(content, bg=COLORS["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right = tk.Frame(content, bg=COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))

        self._build_engine(left)
        self._build_delivery(left)
        self._build_timing(left)
        self._build_position(left)

        self._build_hotkeys(right)
        self._build_performance(right)
        self._build_data(right)

        self._bind_wheel(self.inner)

    # -- scrollable body ------------------------------------------------

    def _on_inner_configure(self, _event=None):
        try:
            bounds = self.canvas.bbox("all")
        except Exception:
            return
        if bounds:
            self.canvas.configure(
                scrollregion=(0, 0, bounds[2], bounds[3] + 4)
            )

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(
            self._inner_window, width=max(1, event.width)
        )

    def _bind_wheel(self, widget):
        for child in widget.winfo_children():
            self._bind_wheel(child)
        try:
            widget.bind("<MouseWheel>", self._on_wheel)
        except Exception:
            pass

    def _on_wheel(self, event):
        if self._closed:
            return None
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return None
        step = -3 if event.delta > 0 else 3
        self.canvas.yview_scroll(step, "units")
        return "break"

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
                justify="left", wraplength=360,
            ).pack(anchor="w", pady=(2, 0))
        return inner

    def _build_engine(self, parent):
        inner = self._section(
            parent,
            "Engine",
            "Rust is the default and the fastest core.",
        )
        self.engine_segment = theme.SegmentedControl(
            inner,
            options=(("rust", "RUST"), ("python", "PYTHON")),
            variable=self.engine_var,
            bg=COLORS["card"],
            button_width=84,
            button_height=32,
            font_size=8,
            gap=5,
            accent=COLORS["accent"],
        )
        self.engine_segment.pack(anchor="w", pady=(9, 6))
        theme.make_label(
            inner,
            "RUST runs the hot loop in clickstorm_engine.dll, with no "
            "interpreter or garbage collector in the click path. It is the "
            "fastest engine and falls back to Python automatically when the "
            "DLL is missing or cannot start. PYTHON runs the same pacing and "
            "delivery in the app process - useful for diagnosing the native "
            "core or on a machine where the DLL is unavailable.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=360,
        ).pack(anchor="w")

    def _build_delivery(self, parent):
        inner = self._section(
            parent,
            "Delivery",
            "DIRECT is the default and the fastest: posted window messages "
            "skip the system input queue.",
        )
        self.delivery_segment = theme.SegmentedControl(
            inner,
            options=(
                ("sync", "SYNC"),
                ("sendinput", "SENDINPUT"),
                ("direct", "DIRECT"),
            ),
            variable=self.delivery_var,
            bg=COLORS["card"],
            button_width=84,
            button_height=32,
            font_size=8,
            gap=5,
            accent=COLORS["accent"],
        )
        self.delivery_segment.pack(anchor="w", pady=(9, 6))
        theme.CheckPill(
            inner,
            "Pin the window under the cursor (DIRECT only)",
            self.pin_window_var,
            bg=COLORS["card"],
        ).pack(anchor="w", pady=(0, 6))
        theme.make_label(
            inner,
            "DIRECT posts button messages to the window under the cursor and "
            "is the fastest mode: it can click a covered window, but games "
            "and raw-input apps ignore it. SYNC and SENDINPUT inject real "
            "mouse input instead, in the order they deliver: SYNC starts at "
            "one click per call and widens to 3 ms worth while calls stay "
            "clean, SENDINPUT ramps to a 6 ms budget for the highest "
            "real-input rate. A target whose queue fills is paced, not "
            "stopped.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=360,
        ).pack(anchor="w")

    def _build_timing(self, parent):
        inner = self._section(
            parent,
            "Timing",
            "High-resolution pacing on a dedicated thread.",
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
            button_width=150,
            button_height=32,
            font_size=8,
        )
        self.position_segment.pack(anchor="w", pady=(9, 8))

        row = tk.Frame(inner, bg=COLORS["card"])
        row.pack(fill="x")
        theme.make_label(
            row, "X", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self._entry(row, self.x_var, width=6).pack(side="left", padx=(5, 10))
        theme.make_label(
            row, "Y", size=8, bold=True, color=COLORS["muted"]
        ).pack(side="left")
        self._entry(row, self.y_var, width=6).pack(side="left", padx=(5, 10))
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
            "Fixed point returns the cursor to these coordinates while "
            "clicking.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=360,
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
            "The stop hotkey is checked first, so it always wins. ESC always "
            "halts a run, even with hotkeys off or the window busy.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=360,
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
        ).pack(anchor="w", pady=(4, 6))
        theme.make_label(
            inner,
            "Both cores share the same pacing, batching, delivery and limits. "
            "The ENGINE section picks which one runs the click loop; Rust is "
            "the default.",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
            justify="left", wraplength=360,
        ).pack(anchor="w")

    def _build_data(self, parent):
        inner = self._section(parent, "Data & storage")
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
        self.lifetime_var.trace_add(
            "write", lambda *_a: self._lifetime_label.config(
                text=self.lifetime_var.get()
            )
        )

        actions = tk.Frame(inner, bg=COLORS["card"])
        actions.pack(fill="x", pady=(9, 4))
        theme.PillButton(
            actions,
            text="OPEN SETTINGS FOLDER",
            command=self._open_settings_folder,
            width=190,
            height=30,
            bg=COLORS["card"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=8,
        ).pack(side="left")

        reset = tk.Button(
            actions,
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
            pady=4,
        )
        reset.pack(side="right")

        theme.make_label(
            inner,
            f"{APP_NAME} v{APP_VERSION}  |  Rust + Python cores, Win32 input",
            size=8, color=COLORS["faint"], bg=COLORS["card"],
        ).pack(anchor="w", pady=(6, 0))
        theme.make_label(
            inner,
            f"Settings: {settings_path()}",
            size=7, color=COLORS["faint"], bg=COLORS["card"],
            wraplength=360, justify="left",
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
            try:
                self._capture_button.set_enabled(True)
                self._capture_button.set_text("CAPTURE IN 3s")
            except Exception:
                pass

    def _reset_lifetime(self):
        if messagebox.askyesno(
            "Reset lifetime clicks",
            "Reset the lifetime click counter to zero?",
            parent=self,
        ):
            self._total_clicks = 0
            self.lifetime_var.set("0 clicks")

    def _open_settings_folder(self):
        folder = settings_path().parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                messagebox.showinfo(
                    "Settings folder", str(folder), parent=self
                )
        except Exception as exc:
            messagebox.showerror(
                "Could not open the settings folder",
                f"{folder}\n\n{type(exc).__name__}: {exc}",
                parent=self,
            )

    def save(self):
        if self._closed:
            return
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
            engine=self.engine_var.get(),
            pin_window=bool(self.pin_window_var.get()),
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
            total_clicks=max(0, int(self._total_clicks)),
        )
        # Write the file before the window closes. Destroying the dialog first
        # and leaving persistence to the app callback is what let a failing
        # callback close the window and silently discard the changes.
        if not updated.save():
            messagebox.showerror(
                "Could not save settings",
                "ClickStorm could not write its settings file:\n\n"
                f"{settings_path()}\n\n"
                f"{last_save_error() or 'Unknown error.'}",
                parent=self,
            )
            return

        try:
            self._on_save(updated)
        except Exception as exc:
            messagebox.showerror(
                "Settings saved, but not applied",
                "The settings were written to disk, but this window could "
                "not apply them:\n\n"
                f"{type(exc).__name__}: {exc}",
                parent=self,
            )

        self._cancel_capture()
        try:
            self.grab_release()
        except Exception:
            pass
        self._closed = True
        self.destroy()

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
        vx, vy, vw, vh = winapi.virtual_screen_bounds()
        if vw <= 0 or vh <= 0:
            vx = vy = 0
            vw = self.winfo_screenwidth()
            vh = self.winfo_screenheight()

        width = max(self.MIN_WIDTH, min(self.WIDTH, vw - 80))
        height = max(self.MIN_HEIGHT, min(self.HEIGHT, vh - 80))

        parent = self._parent
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
        except Exception:
            px = py = 0
            pw = ph = width

        x = px + (pw - width) // 2
        y = py + (ph - height) // 2
        x = max(vx + 8, min(vx + vw - width - 8, x))
        y = max(vy + 8, min(vy + vh - height - 8, y))
        self.geometry(f"{width}x{height}+{x}+{y}")
