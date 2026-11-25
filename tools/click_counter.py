"""ClickStorm click counter - the verification harness for delivered CPS.

Run this on Windows, put it next to ClickStorm, and click it. It counts the
button messages that actually arrive, so the live CPS reading is the rate the
target registered - not the rate ClickStorm asked for.

    python tools\\click_counter.py

In ClickStorm, point at this window and start a run. SendInput delivery sends
real mouse input; Direct/AUTO delivery posts button messages straight to the
window. Both are counted here. The "unpaired" figure is the number of downs
without a matching up: a non-zero value that keeps growing means a receiver
dropped the release, which is what a latched mouse button looks like.

The counter is a separate process on purpose: ClickStorm refuses to post
clicks into its own windows, so the app's own window can never be the target.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections import deque

WINDOW_SECONDS = 2.0
REFRESH_MS = 100


class ClickCounter:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.downs = 0
        self.ups = 0
        self.recent: deque[float] = deque()
        self.started = time.perf_counter()

        root.title("ClickStorm - delivered CPS counter")
        root.configure(bg="#0b1418")
        root.geometry("560x360")
        root.attributes("-topmost", True)

        frame = tk.Frame(root, bg="#0b1418")
        frame.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(
            frame,
            text="DELIVERED CPS",
            bg="#0b1418",
            fg="#5d7d8a",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")

        self.cps_var = tk.StringVar(value="0")
        tk.Label(
            frame,
            textvariable=self.cps_var,
            bg="#0b1418",
            fg="#5ce1b9",
            font=("Segoe UI", 64, "bold"),
        ).pack(anchor="w")

        self.clicks_var = tk.StringVar(value="0 clicks  |  0 unpaired")
        tk.Label(
            frame,
            textvariable=self.clicks_var,
            bg="#0b1418",
            fg="#c7d6dd",
            font=("Consolas", 13),
        ).pack(anchor="w", pady=(0, 14))

        tk.Label(
            frame,
            text=(
                "Click this window with ClickStorm. SendInput and Direct/AUTO "
                "are both counted.\nThe count is what Windows delivered to "
                "this app, not what was requested."
            ),
            bg="#0b1418",
            fg="#5d7d8a",
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 12))

        tk.Button(
            frame,
            text="RESET",
            command=self.reset,
            bg="#16262c",
            fg="#c7d6dd",
            activebackground="#1f343c",
            activeforeground="#ffffff",
            relief="flat",
            padx=16,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")

        # bind_all (not bind) so a click that lands on a child widget still
        # counts; Tk reports one Button-1 event per delivered mouse down.
        root.bind_all("<Button-1>", self._on_down, add="+")
        root.bind_all("<ButtonRelease-1>", self._on_up, add="+")
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self._tick()

    def _on_down(self, _event=None) -> None:
        self.downs += 1
        now = time.perf_counter()
        self.recent.append(now)
        self._trim(now)

    def _on_up(self, _event=None) -> None:
        self.ups += 1

    def _trim(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()

    def reset(self) -> None:
        self.downs = 0
        self.ups = 0
        self.recent.clear()
        self.started = time.perf_counter()

    def _tick(self) -> None:
        now = time.perf_counter()
        self._trim(now)
        cps = len(self.recent) / WINDOW_SECONDS
        self.cps_var.set(f"{cps:,.0f}")
        self.clicks_var.set(
            f"{self.downs:,} clicks  |  {max(0, self.downs - self.ups):,} unpaired"
        )
        self.root.after(REFRESH_MS, self._tick)


def main() -> None:
    root = tk.Tk()
    ClickCounter(root)
    root.mainloop()


if __name__ == "__main__":
    main()
