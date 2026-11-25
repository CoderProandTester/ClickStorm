# ClickStorm

A fast, dependency-free Windows auto clicker. It drives the mouse through
Win32 `SendInput`, one click per call on an absolute timeline, because
benchmarks show that is what the target actually receives — packed batches
look faster but arrive coalesced. The UI is pure Tkinter, the engine is
isolated in its own high-priority thread, and settings persist in
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
- Follow-cursor clicking or a fixed coordinate the cursor is returned to
  while clicking.
- Optional timing jitter, 0-5 s start delay, always-on-top and
  minimize-on-start.
- One click per `SendInput` call on an absolute timeline, with catch-up for
  clicks a late turn missed; see [How the speed works](#how-the-speed-works)
  for the measurements behind that choice.
- Live stats: actual CPS, peak CPS, rate history, progress, ETA, send calls,
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
the action is working. The publish job runs **only** for a version tag, a
`release/v<version>` branch, or a manual run with **publish** enabled, so a
plain push to `main` never creates a release. Three ways to publish:

```bat
git tag v3.1.0
git push origin v3.1.0
```

or push a branch named after the version, which is the branch-only route for
someone who can push commits but not tags:

```bat
git push origin main:release/v3.1.0
```

or, with no local git credentials at all, open **Actions → Windows installers →
Run workflow**, tick **publish** and run it. In every case the workflow creates
the `v<version>` tag itself and attaches the installers.

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

- **One click per `SendInput` call.** Each click is a single DOWN/UP pair in
  its own call, and the `INPUT` array is built once per session and reused,
  so the hot loop never allocates. This is the change that decides how many
  clicks the *target* receives: packing many clicks into one array raises the
  emitted rate but lowers the delivered rate, because batched clicks carry no
  spacing and the input stack coalesces them. An independent Windows
  benchmark measured emitted +36% against delivered -22% (fidelity 99% to
  57%), so ClickStorm sends one click at a time.
- **Absolute click timeline.** Click *n* is due at `start + n / cps` on
  `perf_counter()`, as a float, so a rate like 1,234 CPS averages exactly
  1,234 CPS instead of drifting and the first click fires immediately.
- **Catch-up without coalescing.** Every click whose deadline has passed is
  due on the next turn, and those clicks are sent as separate calls so they
  keep their own DOWN/UP pairs instead of collapsing into one. More than 50 ms
  of backlog is counted and discarded so a system freeze cannot fire an
  unbounded burst; the live footer shows how many clicks were recovered.
- **Paced credit, not dropped work.** Click credit is only spent on clicks
  Windows confirmed. A rejected shortfall goes back into the bank and is
  retried on the next slot, so a run ends on the exact target.
- **Three-tier wait.** Above 2 ms the thread sleeps on a high-resolution
  waitable timer (`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`), between 2 ms and
  50 us it yields with `SwitchToThread`, and it only spins the last 50 us — so
  the timeline holds without pinning a whole core. `timeBeginPeriod(1)` is
  active for the session, because the default 15.6 ms Windows tick would cap
  throughput near 64 CPS.
- **Full-speed scheduling.** While a session runs the click thread asks for
  time-critical priority and opts out of Windows 11 ecoQoS power throttling
  (`SetThreadInformation`), so it is not parked on a low-power core mid-run.
- **No cursor movement per click.** The mouse is not re-positioned for every
  click; SendInput clicks at the current position (fixed mode returns the
  cursor to its point every couple of milliseconds).

### Why high targets run flat out

Windows serialises injected input through a single system input thread, and the
receiving application consumes it on its own message loop, so the receiver —
not ClickStorm — is the bottleneck above roughly **2,000 CPS**. A benchmarked
native clicker measures **~4,000 delivered CPS** as the user-mode ceiling on a
fast desktop, with 100% fidelity up to 2,000 CPS and ~99% when unthrottled.
`25,000` is therefore offered as a "run as fast as the machine allows"
setting: the live CPS readout shows what actually reached Windows, and the
**TARGET**/**REMAINING** tiles still end the run on your exact click count.
For games and apps that throttle input, **500-2,000 CPS** usually registers
more reliably than an extreme setting.

If the target is the bottleneck, nothing on the sending side can add clicks
that the app refuses to process. What helps is where the clicks go: **Direct
message** delivery posts button messages straight to the window, which
bypasses the input stack and works while another window has focus, at the cost
of only working with classic Win32 windows.

## Prior art and credits

The pacing model here was chosen after reading how other Windows clickers are
built and what they measured. Credit to those projects:

| Project | Licence | What ClickStorm took from it |
| --- | --- | --- |
| [local764larp/auto-clicker](https://github.com/local764larp/auto-clicker) | none declared | The delivered-CPS benchmark that showed batched arrays lose 22% of delivered clicks, and the timer / yield / spin wait tiers. Measurements and design ideas only. |
| [chris101-dev/ColibriTap](https://github.com/chris101-dev/ColibriTap) | MIT | Drift-free scheduling against an absolute deadline instead of `sleep(interval)`. |
| [PriyanshuGeTRekT/WinClicker](https://github.com/PriyanshuGeTRekT/WinClicker) | MIT | Clicking a window in the background with posted button messages (our **Direct message** mode). |
| [Blur009/Blur-AutoClicker](https://github.com/Blur009/Blur-AutoClicker) | GPL-3.0 | Feature ideas only (click/time limits, position clicking). No code is reused: GPL-3.0 is not compatible with this project's MIT licence. |

No source code was copied from any of them. Where a project's licence would
not permit reuse, only published measurements, behaviour and design ideas were
used. ClickStorm remains MIT (see [LICENSE](LICENSE)).

Deliberately **not** used: kernel-mode input drivers such as
[Interception](https://github.com/oblitum/Interception). They would lift the
ceiling for some targets, but they need an administrator-installed, signed
driver and break the "single portable exe, no dependencies" promise.

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
clickstorm/engine.py     Click engine: timeline, per-click delivery, exact targets
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
- **Stopping feels delayed at extreme rates.** Stop is checked between every
  click and every `SendInput` call, but a backed-up input queue can still take
  a moment to drain.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
