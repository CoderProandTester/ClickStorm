"""In-app update window for ClickStorm.

The window checks the repository's latest GitHub release, lists the installers
that release offers so the user can pick one, downloads the choice on a worker
thread and then hands over to the installer.  Tk is only ever touched from the
main thread; worker threads post results through a queue.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox

from . import APP_NAME, APP_VERSION
from . import theme, updater
from .theme import COLORS


def _progress_text(done: int, total: int) -> str:
    if total > 0:
        percent = min(100, int(done * 100 / total))
        return (
            f"Downloading  {done / (1024 * 1024):.1f} MB of "
            f"{total / (1024 * 1024):.1f} MB  ({percent}%)"
        )
    return f"Downloading  {done / (1024 * 1024):.1f} MB"


class UpdateDialog(tk.Toplevel):
    WIDTH = 660
    HEIGHT = 452
    POLL_MS = 80

    def __init__(self, parent, on_install):
        super().__init__(parent)
        self.withdraw()
        self.title(f"{APP_NAME} - Updates")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.transient(parent)

        self._parent = parent
        self._on_install = on_install
        self._queue: queue.Queue = queue.Queue()
        self._poll_job = None
        self._closed = False
        self._busy = False
        self._assets: tuple = ()

        self.status_var = tk.StringVar(value=f"Installed version: v{APP_VERSION}")
        self.detail_var = tk.StringVar(
            value="Check the repository for a newer ClickStorm release."
        )
        self.progress_var = tk.StringVar(value="")

        self._build()
        self._position()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.deiconify()
        self.focus_force()

        self._check()
        self._poll_job = self.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build(self) -> None:
        outer = tk.Frame(self, bg=COLORS["bg"])
        outer.pack(fill="both", expand=True, padx=24, pady=20)

        theme.make_label(
            outer, "Check for updates", size=20, bold=True, bg=COLORS["bg"]
        ).pack(anchor="w")
        tk.Label(
            outer,
            textvariable=self.status_var,
            bg=COLORS["bg"],
            fg=COLORS["text_dim"],
            font=theme.font(10),
        ).pack(anchor="w", pady=(3, 0))

        card = theme.Card(outer)
        card.pack(fill="both", expand=True, pady=(14, 0))
        inner = tk.Frame(card, bg=COLORS["card"])
        inner.pack(fill="both", expand=True, padx=16, pady=13)

        theme.make_label(
            inner, "AVAILABLE INSTALLERS", size=8, bold=True,
            color=COLORS["muted"],
        ).pack(anchor="w")

        list_frame = tk.Frame(
            inner,
            bg=COLORS["card2"],
            highlightbackground=COLORS["border_soft"],
            highlightthickness=1,
        )
        list_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.listbox = tk.Listbox(
            list_frame,
            height=4,
            bg=COLORS["card2"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground=COLORS["bg"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            activestyle="none",
            font=theme.font(10, "mono"),
            exportselection=False,
        )
        self.listbox.pack(fill="both", expand=True, padx=8, pady=8)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        tk.Label(
            inner,
            textvariable=self.detail_var,
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=theme.font(8),
            justify="left",
            wraplength=self.WIDTH - 90,
        ).pack(anchor="w", pady=(9, 0))

        tk.Label(
            inner,
            textvariable=self.progress_var,
            bg=COLORS["card"],
            fg=COLORS["green"],
            font=theme.font(9, "bold", "mono"),
        ).pack(anchor="w", pady=(4, 0))

        footer = tk.Frame(outer, bg=COLORS["bg"])
        footer.pack(fill="x", pady=(16, 0))

        theme.PillButton(
            footer,
            text="CLOSE",
            command=self.close,
            width=110,
            height=42,
            bg=COLORS["bg"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=10,
        ).pack(side="left")

        self._install_button = theme.PillButton(
            footer,
            text="INSTALL UPDATE",
            command=self.install,
            width=200,
            height=42,
            bg=COLORS["bg"],
            fill=COLORS["accent"],
            hover=COLORS["cyan"],
            active=COLORS["accent"],
            fg=COLORS["bg"],
            font_size=10,
        )
        self._install_button.pack(side="right")
        self._install_button.set_enabled(False)

        self._check_button = theme.PillButton(
            footer,
            text="CHECK AGAIN",
            command=self._check,
            width=150,
            height=42,
            bg=COLORS["bg"],
            fill=COLORS["card2"],
            hover=COLORS["card3"],
            fg=COLORS["text_dim"],
            font_size=9,
        )
        self._check_button.pack(side="right", padx=(0, 10))

    def _position(self) -> None:
        self.update_idletasks()
        parent = self._parent
        try:
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
        except Exception:
            px = py = 0
            pw = ph = 800
        x = max(0, px + (pw - self.WIDTH) // 2)
        y = max(0, py + (ph - self.HEIGHT) // 2)
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    # ------------------------------------------------------------------
    # Background work
    # ------------------------------------------------------------------

    def _check(self) -> None:
        if self._closed or self._busy:
            return
        self._set_busy(True)
        self.progress_var.set("")
        self.status_var.set(f"Installed version: v{APP_VERSION}")
        self.detail_var.set("Checking GitHub for the latest release...")
        self.listbox.delete(0, "end")
        self._assets = ()
        threading.Thread(
            target=self._check_worker, name="ClickStormUpdateCheck", daemon=True
        ).start()

    def _check_worker(self) -> None:
        try:
            release = updater.fetch_latest_release()
        except Exception as exc:
            self._queue.put(("error", str(exc)))
            return
        self._queue.put(("release", release))

    def _download_worker(self, asset) -> None:
        try:
            path = updater.download_installer(
                asset,
                lambda done, total: self._queue.put(
                    ("progress", _progress_text(done, total))
                ),
            )
        except Exception as exc:
            self._queue.put(("download_error", str(exc)))
            return
        self._queue.put(("downloaded", path))

    def _poll(self) -> None:
        self._poll_job = None
        if self._closed:
            return
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "release":
                self._show_release(payload)
            elif kind == "error":
                self._show_error(payload)
            elif kind == "progress":
                self.progress_var.set(str(payload))
            elif kind == "download_error":
                self.progress_var.set("")
                messagebox.showerror(
                    "Update download failed", str(payload), parent=self
                )
                self._set_busy(False)
            elif kind == "downloaded":
                self._launch(payload)
        self._poll_job = self.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _show_release(self, release: updater.ReleaseInfo) -> None:
        self._set_busy(False)
        if not updater.is_newer(release.tag, APP_VERSION):
            self.status_var.set(
                f"You are up to date - v{APP_VERSION} is the latest release."
            )
            self.detail_var.set(
                "No newer release is published. Nothing to install."
            )
            self._assets = ()
            self._install_button.set_enabled(False)
            return

        self.status_var.set(
            f"Version {release.tag} is available  (installed v{APP_VERSION})."
        )
        self._assets = release.assets
        self.listbox.delete(0, "end")
        for asset in self._assets:
            arch = updater.asset_architecture(asset.name)
            label = updater.ARCH_NAMES.get(arch, "installer")
            self.listbox.insert("end", f"  {asset.name}    ({label}, {asset.size_text})")

        if not self._assets:
            self.detail_var.set(
                "This release does not include a downloadable installer. "
                f"Open {updater.RELEASES_PAGE} to see the release."
            )
            self._install_button.set_enabled(False)
            return

        index = updater.preferred_asset_index(self._assets)
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._install_button.set_enabled(True)
        self.detail_var.set(
            "Pick the installer for your Windows build, then choose INSTALL "
            "UPDATE. ClickStorm closes before the installer starts."
        )

    def _show_error(self, message: str) -> None:
        self._set_busy(False)
        self.status_var.set(f"Installed version: v{APP_VERSION}")
        self.detail_var.set(str(message))
        self._install_button.set_enabled(False)

    def _on_select(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        asset = self._assets[selection[0]]
        arch = updater.asset_architecture(asset.name)
        label = updater.ARCH_NAMES.get(arch, "installer")
        self.detail_var.set(
            f"{asset.name}  ({label}, {asset.size_text}). "
            "ClickStorm closes before the installer starts."
        )

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def install(self) -> None:
        if self._closed or self._busy or not self._assets:
            return
        selection = self.listbox.curselection()
        index = selection[0] if selection else updater.preferred_asset_index(
            self._assets
        )
        if index < 0 or index >= len(self._assets):
            return
        asset = self._assets[index]
        self._set_busy(True)
        self._install_button.set_enabled(False)
        self.detail_var.set(f"Preparing {asset.name}...")
        self.progress_var.set("Starting download...")
        threading.Thread(
            target=self._download_worker,
            args=(asset,),
            name="ClickStormUpdateDownload",
            daemon=True,
        ).start()

    def _launch(self, path) -> None:
        self.progress_var.set("Starting the installer...")
        try:
            updater.launch_installer(path)
        except Exception as exc:
            self._set_busy(False)
            self.progress_var.set("")
            messagebox.showerror(
                "Could not start the installer", str(exc), parent=self
            )
            return
        self.close()
        if self._on_install is not None:
            self._on_install()

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self._check_button.set_enabled(not busy)
        except Exception:
            pass
        if busy:
            self._install_button.set_enabled(False)
        elif self._assets:
            self._install_button.set_enabled(True)
        try:
            self.listbox.configure(state="disabled" if busy else "normal")
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        try:
            self.destroy()
        except Exception:
            pass
