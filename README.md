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
- Live stats: actual CPS, peak CPS, rate history, progress, ETA, batches,
  input events and failed events.

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
the action is working. Push a version tag:

```bat
git tag v2.0.0
git push origin v2.0.0
```

The workflow reads the version from `clickstorm/__init__.py`, so bump it before
tagging. x64 and x86 build on `windows-latest` (x86 uses 32-bit Python); ARM64
builds natively on the `windows-11-arm` runner. Running the workflow manually
produces the same installers as downloadable build artifacts without creating a
release.

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

- **Arrayed `SendInput`.** One call carries a small burst of clicks, which
  removes the per-call cost that limits naive one-click-per-call loops.
  Bursts stay at 25 clicks (50 input events) and shrink to 5 if the input
  queue pushes back. Deliberately small arrays keep `SendInput` from blocking
  on a slow target, which is what makes extreme settings feel slow.
- **Steady burst pacing.** At higher rates the engine waits for a small batch
  of click credit (about 4 ms worth) and sends it in one call instead of
  dribbling one click per syscall. Stale credit is dropped rather than fired
  as a huge catch-up burst, so STOP stays responsive and the reported CPS is
  the rate actually reaching Windows.
- **Paced credit, not dropped work.** The engine banks click credit at the
  configured rate and only spends it on clicks Windows confirmed. Anything the
  system rejects stays due and is retried, so a run ends on the exact target.
- **Precision timing.** A 1 ms timer resolution plus a short sleep/spin
  deadline keeps bursts on schedule, and the click thread runs at
  time-critical priority while a session is active.
- **No cursor movement per click.** The mouse is not re-positioned for every
  click; SendInput clicks at the current position (fixed mode returns the
  cursor to its point once per burst).

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
- **Stopping feels delayed at extreme rates.** A turn is capped at 100
  clicks and bursts at 25 so stop is checked constantly, but a backed-up input
  queue can still take a moment to drain.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
