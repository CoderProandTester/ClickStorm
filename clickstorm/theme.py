"""ClickStorm's visual language and custom Tk widgets.

Everything here is drawn with the standard library only.  The custom canvas
widgets keep the UI crisp and give ClickStorm a look that stock Tk widgets
cannot provide.
"""

from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#070b14",
    "bg2": "#091120",
    "panel": "#0d1626",
    "card": "#101b2d",
    "card2": "#142238",
    "card3": "#1a2b45",
    "border": "#223550",
    "border_soft": "#192941",
    "text": "#f3f7ff",
    "text_dim": "#c3d0e2",
    "muted": "#8398b5",
    "faint": "#5a6f8d",
    "accent": "#38bdf8",
    "accent_dark": "#0ea5e9",
    "cyan": "#22d3ee",
    "green": "#34d399",
    "green_dark": "#059669",
    "green_deep": "#052e24",
    "red": "#fb7185",
    "red_dark": "#e11d48",
    "red_deep": "#3a1420",
    "amber": "#fbbf24",
    "amber_deep": "#3a2c10",
    "purple": "#a78bfa",
    "white": "#ffffff",
    "black": "#04070d",
}

_FAMILIES = {
    "display": "Segoe UI",
    "body": "Segoe UI",
    "mono": "Consolas",
}


def init_fonts(root: tk.Misc) -> None:
    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()

    def pick(*names: str) -> str:
        for name in names:
            if name in available:
                return name
        return "Segoe UI"

    _FAMILIES["display"] = pick(
        "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"
    )
    _FAMILIES["body"] = pick("Segoe UI Variable Text", "Segoe UI")
    _FAMILIES["mono"] = pick("Bahnschrift", "Cascadia Mono", "Consolas")


def font(size: int, weight: str = "normal", family: str = "body"):
    return (_FAMILIES.get(family, "Segoe UI"), size, weight)


# ---------------------------------------------------------------------------
# Formatting / drawing helpers
# ---------------------------------------------------------------------------

def fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def fmt_rate(value: float) -> str:
    value = float(value)
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    if value > 0:
        return f"{value:.2f}"
    return "0"


def fmt_short(value: float) -> str:
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.0f}K"
    if value >= 1000:
        return f"{value / 1000:.1f}K"
    return f"{value:.0f}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(
        points, smooth=True, splinesteps=24, **kwargs
    )


def mix(color_a: str, color_b: str, t: float) -> str:
    """Blend two #rrggbb colors."""
    t = max(0.0, min(1.0, t))

    def parts(color: str):
        color = color.lstrip("#")
        return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))

    ar, ag, ab = parts(color_a)
    br, bg, bb = parts(color_b)
    return "#{:02x}{:02x}{:02x}".format(
        int(ar + (br - ar) * t),
        int(ag + (bg - ag) * t),
        int(ab + (bb - ab) * t),
    )


# ---------------------------------------------------------------------------
# Basic containers
# ---------------------------------------------------------------------------

class Card(tk.Frame):
    def __init__(self, parent, bg=None, border=None, **kwargs):
        super().__init__(
            parent,
            bg=bg or COLORS["card"],
            highlightbackground=border or COLORS["border"],
            highlightcolor=border or COLORS["border"],
            highlightthickness=1,
            bd=0,
            **kwargs,
        )


def make_label(parent, text, size=10, bold=False, color=None, bg=None, **kwargs):
    return tk.Label(
        parent,
        text=text,
        bg=bg or COLORS["card"],
        fg=color or COLORS["text"],
        font=font(size, "bold" if bold else "normal"),
        **kwargs,
    )


def make_separator(parent, bg=None, color=None, height=1):
    frame = tk.Frame(parent, bg=color or COLORS["border_soft"], height=height)
    frame.pack(fill="x")
    return frame


# ---------------------------------------------------------------------------
# Rounded canvas button
# ---------------------------------------------------------------------------

class PillButton(tk.Canvas):
    def __init__(
        self,
        parent,
        text,
        command=None,
        width=120,
        height=36,
        bg=None,
        fill=None,
        hover=None,
        active=None,
        fg=None,
        font_size=10,
        bold=True,
        radius=10,
        border=None,
        disabled_fill=None,
        disabled_fg=None,
    ):
        self._parent_bg = bg or COLORS["card"]
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=self._parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self._text = text
        self._command = command
        self._fill = fill or COLORS["card2"]
        self._hover = hover or COLORS["card3"]
        self._active = active or COLORS["accent"]
        self._fg = fg or COLORS["text"]
        self._font = font(font_size, "bold" if bold else "normal")
        self._radius = radius
        self._border = border
        self._disabled_fill = disabled_fill or COLORS["card2"]
        self._disabled_fg = disabled_fg or COLORS["faint"]
        self._enabled = True
        self._hovering = False
        self._pressed = False
        self._selected = False
        self._last_pointer_inside = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", lambda _e: self._redraw())
        self._redraw()

    # -- public ---------------------------------------------------------

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._redraw()

    def set_selected(self, selected: bool):
        self._selected = bool(selected)
        self._redraw()

    def set_text(self, text: str):
        self._text = text
        self._redraw()

    def set_colors(self, fill=None, hover=None, fg=None):
        if fill is not None:
            self._fill = fill
        if hover is not None:
            self._hover = hover
        if fg is not None:
            self._fg = fg
        self._redraw()

    # -- internal -------------------------------------------------------

    def _current_fill(self):
        if not self._enabled:
            return self._disabled_fill
        if self._selected:
            return self._active
        if self._pressed:
            return mix(self._hover, COLORS["black"], 0.12)
        if self._hovering:
            return self._hover
        return self._fill

    def _current_fg(self):
        if not self._enabled:
            return self._disabled_fg
        return self._fg

    def _redraw(self):
        self.delete("all")
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        if w <= 2:
            w = int(self.cget("width"))
            h = int(self.cget("height"))
        fill = self._current_fill()
        border = self._border if self._border else fill
        rounded_rect(
            self, 1, 1, w - 1, h - 1, self._radius, fill=fill, outline=border
        )
        self.create_text(
            w / 2,
            h / 2 + 1,
            text=self._text,
            fill=self._current_fg(),
            font=self._font,
            anchor="center",
        )

    def _on_enter(self, _event):
        self._hovering = True
        self._redraw()

    def _on_leave(self, _event):
        self._hovering = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event):
        if not self._enabled:
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, event):
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if was_pressed and inside and self._command:
            self._command()


# ---------------------------------------------------------------------------
# Segmented control
# ---------------------------------------------------------------------------

class SegmentedControl(tk.Frame):
    def __init__(
        self,
        parent,
        options,
        variable: tk.StringVar,
        command=None,
        bg=None,
        button_width=74,
        button_height=34,
        font_size=9,
        accent=None,
        gap=6,
    ):
        super().__init__(parent, bg=bg or COLORS["card"])
        self._variable = variable
        self._command = command
        self._buttons: dict[str, PillButton] = {}
        self._enabled = True

        for index, option in enumerate(options):
            if isinstance(option, tuple):
                value, label = option
            else:
                value, label = option, option
            button = PillButton(
                self,
                text=label,
                command=lambda v=value: self._select(v),
                width=button_width,
                height=button_height,
                bg=bg or COLORS["card"],
                fill=COLORS["card2"],
                hover=COLORS["card3"],
                active=accent or COLORS["accent"],
                fg=COLORS["text"],
                font_size=font_size,
                radius=8,
            )
            button.pack(side="left", padx=(0 if index == 0 else gap, 0))
            self._buttons[value] = button

        self._variable.trace_add("write", lambda *_a: self._refresh())
        self._refresh()

    def _select(self, value):
        if not self._enabled:
            return
        self._variable.set(value)
        self._refresh()
        if self._command:
            self._command(value)

    def _refresh(self):
        current = self._variable.get()
        for value, button in self._buttons.items():
            button.set_selected(value == current)

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        for button in self._buttons.values():
            button.set_enabled(self._enabled)

    def get(self) -> str:
        return self._variable.get()


# ---------------------------------------------------------------------------
# Logarithmic CPS slider
# ---------------------------------------------------------------------------

class LogSlider(tk.Canvas):
    def __init__(
        self,
        parent,
        minimum=1.0,
        maximum=25_000.0,
        value=500.0,
        command=None,
        bg=None,
    ):
        super().__init__(
            parent,
            height=48,
            bg=bg or COLORS["card"],
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=1,
        )
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self._value = max(self.minimum, min(self.maximum, float(value)))
        self._command = command
        self._drag = False
        self._hover = False
        self._enabled = True

        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<MouseWheel>", self._on_wheel)
        self._redraw()

    # -- public ---------------------------------------------------------

    def get(self) -> float:
        return self._value

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._redraw()

    def set(self, value, notify=False):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        value = max(self.minimum, min(self.maximum, value))
        if abs(value - self._value) < 1e-9:
            return
        self._value = value
        self._redraw()
        if notify and self._command:
            self._command(self._value)

    # -- geometry -------------------------------------------------------

    def _pad(self):
        return 12

    def _track_width(self):
        return max(1, self.winfo_width() - self._pad() * 2)

    def _position_for(self, value):
        span = math.log(self.maximum / self.minimum)
        t = math.log(max(self.minimum, value) / self.minimum) / span
        return self._pad() + max(0.0, min(1.0, t)) * self._track_width()

    def _value_for(self, x):
        t = (x - self._pad()) / self._track_width()
        t = max(0.0, min(1.0, t))
        raw = self.minimum * math.pow(self.maximum / self.minimum, t)
        if t >= 0.9995:
            return self.maximum
        if t <= 0.0005:
            return self.minimum
        if raw >= 50_000:
            return float(round(raw / 1000) * 1000)
        if raw >= 10_000:
            return float(round(raw / 500) * 500)
        if raw >= 1000:
            return float(round(raw / 100) * 100)
        if raw >= 100:
            return float(round(raw / 10) * 10)
        if raw >= 20:
            return float(round(raw / 5) * 5)
        return float(max(1, round(raw)))

    # -- drawing --------------------------------------------------------

    def _redraw(self):
        self.delete("all")
        w = max(60, self.winfo_width())
        h = max(40, self.winfo_height())
        track_y = 20
        x1 = self._pad()
        x2 = w - self._pad()

        rounded_rect(
            self, x1, track_y - 3, x2, track_y + 3, 3,
            fill=COLORS["card3"], outline=COLORS["card3"],
        )

        # Decade ticks and labels
        for value, label in (
            (1, "1"),
            (10, "10"),
            (100, "100"),
            (1000, "1K"),
            (10_000, "10K"),
            (25_000, "MAX"),
        ):
            if value > self.maximum:
                continue
            x = self._position_for(value)
            self.create_line(
                x, track_y + 7, x, track_y + 11,
                fill=COLORS["border"], width=1,
            )
            anchor = "center"
            if value == self.minimum:
                anchor = "w"
            elif value >= self.maximum:
                anchor = "e"
            text_x = x
            if anchor == "w":
                text_x = x1
            elif anchor == "e":
                text_x = x2
            self.create_text(
                text_x, 40, text=label, fill=COLORS["faint"],
                font=font(7), anchor=anchor,
            )

        knob_x = self._position_for(self._value)
        if knob_x > x1 + 1:
            rounded_rect(
                self, x1, track_y - 3, knob_x, track_y + 3, 3,
                fill=COLORS["accent"] if self._enabled else COLORS["faint"],
                outline=COLORS["accent"] if self._enabled else COLORS["faint"],
            )
        glow = COLORS["cyan"] if self._drag or self._hover else COLORS["accent"]
        if not self._enabled:
            glow = COLORS["faint"]
        self.create_oval(
            knob_x - 9, track_y - 9, knob_x + 9, track_y + 9,
            fill=COLORS["card2"], outline=glow, width=2,
        )
        self.create_oval(
            knob_x - 4, track_y - 4, knob_x + 4, track_y + 4,
            fill=glow, outline=glow,
        )

    # -- events ---------------------------------------------------------

    def _on_press(self, event):
        if not self._enabled:
            return
        self.focus_set()
        self._drag = True
        self.set(self._value_for(event.x), notify=True)

    def _on_drag(self, event):
        if self._enabled and self._drag:
            self.set(self._value_for(event.x), notify=True)

    def _on_release(self, _event):
        self._drag = False
        self._redraw()

    def _on_enter(self, _event):
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _event):
        self._hover = False
        self._drag = False
        self._redraw()

    def _on_wheel(self, event):
        if not self._enabled:
            return "break"
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self.set(self._value * factor, notify=True)
        return "break"


# ---------------------------------------------------------------------------
# Circular rate gauge
# ---------------------------------------------------------------------------

class Gauge(tk.Canvas):
    def __init__(self, parent, bg=None, width=300, height=198):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=bg or COLORS["card"],
            highlightthickness=0,
            bd=0,
        )
        self._value = 0.0
        self._maximum = 500.0
        self._target = 500.0
        self._running = False
        self._subtitle = "TARGET 500 CPS"
        self.bind("<Configure>", lambda _e: self._redraw())
        self._redraw()

    def set(self, value, maximum, target, running, subtitle=None):
        self._value = max(0.0, float(value))
        self._maximum = max(1.0, float(maximum))
        self._target = max(0.0, float(target))
        self._running = bool(running)
        if subtitle is not None:
            self._subtitle = subtitle
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = max(200, self.winfo_width())
        h = max(150, self.winfo_height())
        cx = w / 2
        cy = h * 0.60
        radius = min(w * 0.32, h * 0.42)
        x1, y1 = cx - radius, cy - radius
        x2, y2 = cx + radius, cy + radius

        ratio = min(1.0, self._value / self._maximum) if self._maximum else 0.0
        active_color = COLORS["cyan"] if self._running else COLORS["faint"]

        # Outer tick marks
        for index in range(13):
            angle = math.radians(225 - index * 22.5)
            inner = radius - 15
            outer = radius - 11
            color = COLORS["border"] if index % 2 else COLORS["faint"]
            self.create_line(
                cx + inner * math.cos(angle),
                cy - inner * math.sin(angle),
                cx + outer * math.cos(angle),
                cy - outer * math.sin(angle),
                fill=color,
                width=1,
            )

        self.create_arc(
            x1, y1, x2, y2,
            start=225, extent=-270,
            style="arc", outline=COLORS["card3"], width=13,
        )
        if ratio > 0:
            self.create_arc(
                x1, y1, x2, y2,
                start=225, extent=-270 * ratio,
                style="arc", outline=active_color, width=13,
            )

        # Needle dot at the end of the active arc
        if ratio > 0:
            angle = math.radians(225 - 270 * ratio)
            nx = cx + radius * math.cos(angle)
            ny = cy - radius * math.sin(angle)
            self.create_oval(
                nx - 5, ny - 5, nx + 5, ny + 5,
                fill=COLORS["bg"], outline=active_color, width=2,
            )

        number = fmt_rate(self._value)
        size = 31 if len(number) <= 4 else 27 if len(number) <= 6 else 23
        self.create_text(
            cx, cy - 8,
            text=number,
            fill=COLORS["text"] if self._running else COLORS["text_dim"],
            font=font(size, "bold", "mono"),
            anchor="center",
        )
        self.create_text(
            cx, cy + 24,
            text="CURRENT CPS",
            fill=COLORS["faint"],
            font=font(8, "bold"),
            anchor="center",
        )
        self.create_text(
            cx, cy + 45,
            text=self._subtitle,
            fill=COLORS["muted"],
            font=font(8),
            anchor="center",
        )


# ---------------------------------------------------------------------------
# Rate history sparkline
# ---------------------------------------------------------------------------

class Sparkline(tk.Canvas):
    def __init__(self, parent, bg=None, height=98, max_points=120):
        super().__init__(
            parent,
            height=height,
            bg=bg or COLORS["card"],
            highlightthickness=0,
            bd=0,
        )
        self._values: list[float] = []
        self._target = 0.0
        self._max_points = max_points
        self.bind("<Configure>", lambda _e: self._redraw())
        self._redraw()

    def set_data(self, values, target=0.0):
        self._values = [max(0.0, float(v)) for v in values][-self._max_points :]
        self._target = max(0.0, float(target))
        self._redraw()

    def clear(self):
        self._values = []
        self._target = 0.0
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = max(100, self.winfo_width())
        h = max(50, self.winfo_height())
        left, right, top, bottom = 12, w - 12, 12, h - 22

        for fraction in (0.0, 0.5, 1.0):
            y = bottom - (bottom - top) * fraction
            self.create_line(
                left, y, right, y,
                fill=COLORS["border_soft"], width=1,
                dash=(2, 5) if fraction == 0 else None,
            )

        peak = max(
            [10.0, self._target, *(self._values or [0.0])]
        )
        peak *= 1.12

        if self._target > 0:
            y = bottom - (bottom - top) * min(1.0, self._target / peak)
            self.create_line(
                left, y, right, y,
                fill=COLORS["amber"], width=1, dash=(4, 6),
            )
            self.create_text(
                left + 3, max(top - 1, y - 9),
                text=f"TARGET {fmt_short(self._target)}",
                fill=COLORS["amber"],
                font=font(7),
                anchor="w",
            )

        values = self._values
        if len(values) >= 2:
            self.create_text(
                right, 2,
                text=f"SCALE {fmt_short(peak)} CPS",
                fill=COLORS["faint"],
                font=font(7, "bold"),
                anchor="ne",
            )
        else:
            self.create_text(
                w / 2, (top + bottom) / 2,
                text="Waiting for click data...",
                fill=COLORS["faint"],
                font=font(8),
            )
            return

        step = (right - left) / max(1, self._max_points - 1)
        offset = self._max_points - len(values)
        points = []
        for index, value in enumerate(values):
            x = left + (offset + index) * step
            y = bottom - (bottom - top) * min(1.0, value / peak)
            points.extend((x, y))

        if len(points) >= 4:
            area = [left, bottom, *points, points[-2], bottom]
            self.create_polygon(
                area,
                fill="#0b2c3a",
                outline="",
                smooth=True,
                splinesteps=18,
            )
        self.create_line(
            *points,
            fill=COLORS["accent"],
            width=2,
            smooth=True,
            splinesteps=18,
        )

        last_value = values[-1]
        lx = left + (offset + len(values) - 1) * step
        ly = bottom - (bottom - top) * min(1.0, last_value / peak)
        self.create_oval(
            lx - 3.5, ly - 3.5, lx + 3.5, ly + 3.5,
            fill=COLORS["cyan"], outline=COLORS["card"], width=2,
        )


# ---------------------------------------------------------------------------
# Status pill, progress, switch
# ---------------------------------------------------------------------------

class StatusPill(tk.Canvas):
    def __init__(self, parent, bg=None, width=162, height=30):
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=bg or COLORS["bg"],
            highlightthickness=0,
            bd=0,
        )
        self._text = "READY"
        self._fg = COLORS["muted"]
        self._fill = COLORS["card2"]
        self._dot = COLORS["faint"]
        self.bind("<Configure>", lambda _e: self._redraw())
        self._redraw()

    def set(self, text, fg, fill, dot):
        self._text = text
        self._fg = fg
        self._fill = fill
        self._dot = dot
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = max(60, self.winfo_width())
        h = max(20, self.winfo_height())
        rounded_rect(
            self, 1, 1, w - 1, h - 1, (h - 2) / 2,
            fill=self._fill, outline=self._fill,
        )
        self.create_oval(
            13, h / 2 - 3.5, 20, h / 2 + 3.5,
            fill=self._dot, outline=self._dot,
        )
        self.create_text(
            27, h / 2 + 1,
            text=self._text,
            fill=self._fg,
            font=font(8, "bold"),
            anchor="w",
        )


class ThinProgress(tk.Canvas):
    def __init__(self, parent, bg=None, height=8):
        super().__init__(
            parent,
            height=height,
            bg=bg or COLORS["card"],
            highlightthickness=0,
            bd=0,
        )
        self._fraction = 0.0
        self._color = COLORS["accent"]
        self.bind("<Configure>", lambda _e: self._redraw())

    def set(self, fraction, color=None):
        self._fraction = max(0.0, min(1.0, float(fraction)))
        if color:
            self._color = color
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = max(10, self.winfo_width())
        h = max(4, self.winfo_height())
        rounded_rect(
            self, 0, 0, w, h, h / 2,
            fill=COLORS["card3"], outline=COLORS["card3"],
        )
        if self._fraction > 0:
            rounded_rect(
                self, 0, 0, max(h, w * self._fraction), h, h / 2,
                fill=self._color, outline=self._color,
            )


class SlimScrollbar(tk.Canvas):
    """A minimal scrollbar that only draws a thumb when scrolling matters."""

    def __init__(self, parent, command, bg=None, width=8):
        super().__init__(
            parent,
            width=width,
            bg=bg or COLORS["bg"],
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._drag_start_y = None
        self._drag_start_first = 0.0
        self._hover = False
        self._enabled = True
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    def set(self, first, last) -> None:
        try:
            first = max(0.0, min(1.0, float(first)))
            last = max(first, min(1.0, float(last)))
        except (TypeError, ValueError):
            return
        if abs(first - self._first) < 1e-6 and abs(last - self._last) < 1e-6:
            return
        self._first = first
        self._last = last
        self._redraw()

    # -- drawing -------------------------------------------------------

    def _visible(self) -> bool:
        return self._enabled and (self._last - self._first) < 0.999

    def _thumb_geometry(self):
        height = max(1, self.winfo_height())
        visible = max(0.0, self._last - self._first)
        thumb = max(28.0, visible * height)
        top = self._first * height
        top = max(0.0, min(height - thumb, top))
        return top, top + thumb, height

    def _redraw(self) -> None:
        self.delete("all")
        if not self._visible():
            return
        top, bottom, _height = self._thumb_geometry()
        width = max(3, self.winfo_width())
        pad = 2
        color = COLORS["accent_dark"] if self._hover else COLORS["card3"]
        rounded_rect(
            self,
            pad,
            top + pad,
            width - pad,
            bottom - pad,
            (width - pad * 2) / 2,
            fill=color,
            outline=color,
        )

    # -- interaction ---------------------------------------------------

    def _on_enter(self, _event) -> None:
        self._hover = True
        if self._visible():
            self.configure(cursor="hand2")
            self._redraw()

    def _on_leave(self, _event) -> None:
        self._hover = False
        self._drag_start_y = None
        self.configure(cursor="arrow")
        self._redraw()

    def _on_press(self, event) -> str:
        if not self._visible():
            return "break"
        top, bottom, _height = self._thumb_geometry()
        if top <= event.y <= bottom:
            self._drag_start_y = event.y
            self._drag_start_first = self._first
        else:
            self._command(
                "scroll", -1 if event.y < top else 1, "pages"
            )
        return "break"

    def _on_drag(self, event) -> str:
        if self._drag_start_y is None or not self._visible():
            return "break"
        _top, _bottom, height = self._thumb_geometry()
        visible = max(0.0, self._last - self._first)
        thumb = max(28.0, visible * height)
        track = max(1.0, height - thumb)
        delta = event.y - self._drag_start_y
        target = self._drag_start_first + (delta / track) * (1.0 - visible)
        self._command("moveto", max(0.0, min(1.0 - visible, target)))
        return "break"

    def _on_release(self, _event) -> str:
        self._drag_start_y = None
        return "break"


class CheckPill(tk.Checkbutton):
    def __init__(self, parent, text, variable, bg=None, command=None):
        self._bg = bg or COLORS["card"]
        super().__init__(
            parent,
            text=text,
            variable=variable,
            command=command,
            indicatoron=False,
            bg=COLORS["card2"],
            fg=COLORS["text_dim"],
            selectcolor=COLORS["accent"],
            activebackground=COLORS["card3"],
            activeforeground=COLORS["white"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=font(9, "bold"),
            padx=12,
            pady=7,
            cursor="hand2",
            anchor="w",
        )


# ---------------------------------------------------------------------------
# Brand mark
# ---------------------------------------------------------------------------

class LogoMark(tk.Canvas):
    def __init__(self, parent, size=50, bg=None):
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=bg or COLORS["bg"],
            highlightthickness=0,
            bd=0,
        )
        self._size = size
        self.bind("<Configure>", lambda _e: self._redraw())
        self._redraw()

    def _redraw(self):
        self.delete("all")
        size = max(24, min(self.winfo_width(), self.winfo_height()))
        if size <= 2:
            size = self._size
        pad = 1
        rounded_rect(
            self, pad, pad, size - pad, size - pad, size * 0.26,
            fill="#0a2530", outline=COLORS["accent"],
        )
        rounded_rect(
            self, size * 0.14, size * 0.14, size * 0.86, size * 0.86,
            size * 0.18, fill="#0d3341", outline="",
        )
        bolt = [
            (0.58, 0.12),
            (0.29, 0.54),
            (0.47, 0.54),
            (0.40, 0.88),
            (0.72, 0.43),
            (0.53, 0.43),
        ]
        points = []
        for x, y in bolt:
            points.extend((x * size, y * size))
        self.create_polygon(
            points, fill=COLORS["cyan"], outline=COLORS["white"], width=1,
        )
        self.create_oval(
            size * 0.69, size * 0.66, size * 0.79, size * 0.76,
            fill=COLORS["green"], outline="",
        )


# ---------------------------------------------------------------------------
# ttk styling (comboboxes in the settings dialog)
# ---------------------------------------------------------------------------

def init_ttk(root: tk.Misc) -> None:
    from tkinter import ttk

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Dark.TCombobox",
        fieldbackground=COLORS["card2"],
        background=COLORS["card2"],
        foreground=COLORS["text"],
        arrowcolor=COLORS["text_dim"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        insertcolor=COLORS["text"],
        padding=5,
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", COLORS["card2"])],
        foreground=[("readonly", COLORS["text"])],
        background=[("active", COLORS["card3"])],
        selectbackground=[("readonly", COLORS["card2"])],
        selectforeground=[("readonly", COLORS["text"])],
    )
    try:
        root.option_add("*TCombobox*Listbox.background", COLORS["card2"])
        root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
        root.option_add("*TCombobox*Listbox.selectForeground", COLORS["bg"])
        root.option_add("*TCombobox*Listbox.font", "Segoe UI 9")
    except Exception:
        pass

