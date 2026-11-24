# ClickStorm

A fast, dependency-free Windows auto clicker. It drives the mouse through
arrayed Win32 `SendInput` calls, so it delivers whole bursts of clicks per
system call instead of paying per-click overhead. The UI is pure Tkinter, the
engine is isolated in its own high-priority thread, and settings persist in
`%APPDATA%\ClickStorm\settings.json`.

Supported on **Windows 10 and Windows 11** (x64, x86 and ARM64). The packaged
app is a single windowed `ClickStorm.exe`; it does **not** open a console or
PowerShell window.

## Install

Download the installer for your PC from the repository's **Releases** page:

| Installer | Use it on |
| --- | --- |
| `ClickStorm-Setup-<version>-x64.exe` | 64-bit Windows 10/11 (most PCs) |
| `ClickStorm-Setup-<version>-x86.exe` | 32-bit Windows 10 |
| `ClickStorm-Setup-<version>-arm64.exe` | Windows 11 on ARM |

Run it and approve the UAC prompt. ClickStorm installs to
`C:\Program Files\ClickStorm` (`Program Files (x86)` for the 32-bit build),
with Start-menu shortcuts and an entry in Settings → Apps.

- **Create a desktop shortcut** is checked by default; clear it if you do not
  want one.
- **Launch ClickStorm** on the final page is checked by default, so the app
  opens as soon as installation finishes. It is launched as your normal user,
  not with the installer's administrator token.
- Uninstall from Settings → Apps → Installed apps like any other program.
  Your saved settings in `%APPDATA%\ClickStorm` are left alone.

## Features

- Click rates from 1 to **25,000 CPS**, with a logarithmic slider, presets and
  a numeric entry.
- Per-run click target from 1 to **25,000 clicks**, a duration limit, or
  unlimited running.
- All five mouse buttons: left, right, middle, X1 and X2.
- Global hotkeys: **F5 starts**, **F9 stops** (remappable in Settings).
- Follow-cursor clicking or a fixed coordinate returned to on every burst.
- Optional timing jitter, 0-5 s start delay, always-on-top and
  minimize-on-start.
- Per-millisecond batching: at 25,000 CPS the engine sends 25 clicks in a
  single `SendInput` call every 1 ms slot, and any missed slot is paid back as
  catch-up clicks instead of being lost.
- Live stats: actual CPS, peak CPS, rate history, progress, ETA, batches,
  input events and failed events.
- **UPDATES** button: checks the latest GitHub release, lists the installers it
  offers and installs the one you pick.

## Run from source

Requires Windows 10/11 and Python 3.10+ (Tkinter ships with it).

```bat
python clickstorm.py
```

Use `pythonw clickstorm.py` if you want to launch without a terminal. The
source build has no dependencies.

## Build the executable

```bat
pyinstaller --noconfirm AutoClicker.spec
```

The result is `dist\ClickStorm.exe`. The spec builds `clickstorm.py` in
windowed mode (`console=False`) with `autoclicker.ico` as the application icon,
so launching it shows only the app window.

## Build the installer

Requires [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php) and the app built
for the same architecture (PyInstaller cannot cross-compile):

```bat
winget install JRSoftware.InnoSetup
python -m PyInstaller --noconfirm AutoClicker.spec
iscc /DMyAppArch=x64 installer\ClickStorm.iss
```

If `iscc` is not on PATH, use the compiler's full path: typically
`C:\Program Files (x86)\Inno Setup 6\ISCC.exe` for a machine-wide install or
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` for a per-user install.

Output: `release\ClickStorm-Setup-<version>-x64.exe`. Use `x86` or `arm64` for
`MyAppArch` when building with that architecture's Python. The installer sets
the minimum OS to Windows 10 and restricts itself to the matching architecture.

## Releases (GitHub Actions)

`.github/workflows/release.yml` builds all three installers and publishes them
to a GitHub Release. Every push to `main` also builds the three installers as
workflow artifacts (no release is created), which is the easiest way to confirm
the action is working. The publish job runs **only** for a version tag or for a
manual run with **publish** enabled, so a plain push to `main` never creates a
release. Two ways to publish:

```bat
git tag v3.0.0
git push origin v3.0.0
```

or, with no local git credentials, open **Actions → Windows installers → Run
workflow**, tick **publish** and run it. The workflow then creates the
`v<version>` tag itself and attaches the installers.

The workflow reads the version from `clickstorm/__init__.py`, so bump it before
publishing. x64 and x86 build on `windows-latest` (x86 uses 32-bit Python);
ARM64 builds natively on the `windows-11-arm` runner and is allowed to fail
without holding back the x64/x86 release. A manual run without **publish**
produces the same installers as downloadable build artifacts only.

## Updates

Click **UPDATES** in the title bar to check the latest GitHub release. The
window compares it with the running version and, when a newer release exists,
lists every installer the release publishes (x64, x86, arm64) with its size.
The installer that matches this PC is preselected, but you can install any of
them. **INSTALL UPDATE** downloads your choice over HTTPS, starts it, and
closes ClickStorm so the installer can replace the running files; your
settings in `%APPDATA%\ClickStorm` are kept.

Update checks use the public GitHub API and need no account or token. Nothing
is downloaded until you press **INSTALL UPDATE**, and the app never installs
anything silently.

## Controls

| Action | Default |
| --- | --- |
| Start clicking | `F5` |
| Stop clicking | `F9` |

Hotkeys work while the window is unfocused. They are checked on the UI thread
and the stop key always wins, so a run can always be halted. Both keys can be
changed under **Settings → Global hotkeys**.

Clicks stop automatically when the click target is reached, when the duration
limit expires, or when you press stop. In unlimited mode only stop ends a run.

## How the speed works

- **Arrayed `SendInput`.** One call carries a whole megabyte-free array of
  DOWN/UP `INPUT` structures, which removes the per-call cost that limits
  naive one-click-per-call loops. The array is built once per session and
  reused, so the hot loop never allocates. It starts at 16 clicks per call,
  grows to 64 while everything lands and shrinks to 2 when Windows starts
  rejecting events, which keeps a slow target from blocking `SendInput`.
- **Millisecond batching math.** The target is converted to clicks per
  millisecond (`cps / 1000`, so 25,000 CPS is 25 clicks/ms). The fractional
  part is carried in a float bank, so a rate like 1,234 CPS (1.234 clicks/ms)
  averages exactly 1,234 CPS instead of drifting.
- **Catch-up for missed slots.** Every slot has an absolute deadline on
  `perf_counter()`. If a turn wakes late, the engine counts the slots it
  missed and adds their clicks to the same bank: two missed milliseconds at
  25,000 CPS are paid out as 50 clicks on the next batch. More than 50 ms of
  backlog is discarded so a system freeze cannot fire an unbounded burst; the
  live footer shows how many clicks were recovered.
- **Paced credit, not dropped work.** Click credit is only spent on clicks
  Windows confirmed. A rejected shortfall goes back into the bank and is
  retried on the next slot, so a run ends on the exact target.
- **Precision timing.** A high-resolution waitable timer
  (`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`) covers most of each 1 ms slot and
  the loop spins only the last few hundred microseconds, with
  `timeBeginPeriod(1)` and time-critical thread priority while a session is
  active. This holds the 1 ms grid without pinning an entire core.
- **No cursor movement per click.** The mouse is not re-positioned for every
  click; SendInput clicks at the current position (fixed mode returns the
  cursor to its point once per slot).

### Why high targets run flat out

Windows serialises injected input through a single system input thread, and the
receiving application consumes it on its own message loop. Independent
benchmarks of native clickers put the delivered ceiling at roughly
**2,000-4,000 CPS**; above that, extra requested clicks are not delivered by
Windows itself. `25,000` is therefore offered as a "run as fast as the machine
allows" setting: the live CPS readout shows what actually reached Windows.
For games and apps that throttle input, **500-2,000 CPS** usually registers
more reliably than an extreme setting.

## Settings and files

| Path | Purpose |
| --- | --- |
| `%APPDATA%\ClickStorm\settings.json` | Saved preferences, lifetime clicks, window geometry |
| `%APPDATA%\ClickStorm\error.log` | Startup and UI error details |

Settings are written atomically. An older settings file is migrated on load
(version 2 moves the default start hotkey to `F5` and caps the click target at
25,000).

## Contributing

Contribution setup, engine rules, testing checklist and release steps are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

## Project layout

```
clickstorm.py            Entry point (used by AutoClicker.spec)
clickstorm/app.py        Main window, hotkeys, session control
clickstorm/engine.py     Click engine: pacing, batching, exact targets
clickstorm/winapi.py     Dependency-free Win32 layer (SendInput, cursor, keys)
clickstorm/settings.py   Persistent settings + migration
clickstorm/settings_dialog.py  Advanced settings window
clickstorm/updater.py     GitHub release lookup, download and installer launch
clickstorm/update_dialog.py    In-app update window
clickstorm/theme.py      Palette, fonts and custom Tk widgets
autoclicker.ico          Application icon (window + exe + installer)
AutoClicker.spec         PyInstaller build recipe
installer/ClickStorm.iss Inno Setup installer recipe
.github/workflows/release.yml  CI: x64/x86/ARM64 installers and releases
```

## Troubleshooting

- **Some clicks do not register in a game.** Lower the CPS toward 500-2,000;
  many games ignore input delivered faster than their loop can consume.
- **Clicks do nothing in an elevated app.** Rights are not inherited: run
  `ClickStorm.exe` as administrator as well.
- **Stopping feels delayed at extreme rates.** Stop is checked on every 1 ms
  slot and every `SendInput` call, but a backed-up input queue can still take
  a moment to drain.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
