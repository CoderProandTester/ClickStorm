"""ClickStorm click counter - the verification harness for delivered CPS.

Run this on Windows, put it next to ClickStorm, and click it. It counts the
button messages that actually arrive, so the live CPS reading is the rate the
target registered - not the rate ClickStorm asked for.

    python tools\\click_counter.py

In ClickStorm, point at this window and start a run. SYNC and SENDINPUT both
inject real mouse input; DIRECT posts button messages. All of them are counted
here. The "unpaired" figure is the number of downs without a matching up: a
non-zero value that keeps growing means a receiver dropped the release, which
is what a latched mouse button looks like.

For automated checks, ``--report PATH`` writes the live totals as JSON every
refresh so a harness can read the delivered count without scraping the window:

    python tools\\click_counter.py --report counter.json

The counter is a separate process on purpose: ClickStorm's DIRECT mode refuses
to post clicks into its own windows, so the app's own window can never be the
target.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import tkinter as tk
from collections import deque
from pathlib import Path

WINDOW_SECONDS = 2.0
REFRESH_MS = 100
WINDOW_TITLE = "ClickStorm - delivered CPS counter"


class ClickCounter:
    def __init__(
        self,
        root: tk.Tk,
        report_path: Path | None = None,
        title: str = WINDOW_TITLE,
    ) -> None:
        self.root = root
        self.report_path = Path(report_path) if report_path else None
        self.downs = 0
        self.ups = 0
        self.recent: deque[float] = deque()
        self.started = time.perf_counter()

        root.title(title)
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
                "Click this window with ClickStorm. SYNC, SENDINPUT and "
                "DIRECT are all counted.\\nThe count is what Windows "
                "delivered to this app, not what was requested."
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
        self._write_report(0.0)
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
        self._write_report(0.0)

    def _write_report(self, cps: float) -> None:
        """Publish the counters for the Windows CI delivery check."""
        if self.report_path is None:
            return
        payload = {
            "downs": self.downs,
            "ups": self.ups,
            "cps": round(float(cps), 3),
            "elapsed": round(time.perf_counter() - self.started, 3),
            "title": self.root.title(),
        }
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.report_path.with_name(self.report_path.name + ".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temp, self.report_path)
        except Exception:
            # The window must keep counting even if the report path is bad.
            pass

    def _tick(self) -> None:
        now = time.perf_counter()
        self._trim(now)
        cps = len(self.recent) / WINDOW_SECONDS
        self.cps_var.set(f"{cps:,.0f}")
        self.clicks_var.set(
            f"{self.downs:,} clicks  |  {max(0, self.downs - self.ups):,} unpaired"
        )
        self._write_report(cps)
        self.root.after(REFRESH_MS, self._tick)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write the live counters to this JSON file for automated checks",
    )
    parser.add_argument(
        "--title",
        default=WINDOW_TITLE,
        help="window title (the CI harness finds the window by it)",
    )
    args = parser.parse_args(argv)

    root = tk.Tk()
    ClickCounter(root, report_path=args.report, title=args.title)
    root.mainloop()


if __name__ == "__main__":
    main()
