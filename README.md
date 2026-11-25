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
- Global hotkeys: **F5 starts**, **F9 stops** (remappable in Settings), and
  **ESC always halts**, even with hotkeys switched off or the window busy.
- Follow-cursor clicking or a fixed coordinate the cursor is returned to
  while clicking.
- Optional timing jitter, 0-5 s start delay, always-on-top and
  minimize-on-start.
- Clock-based pacing: credit accrues against `perf_counter()`, so 25,000 CPS
  owes 25 clicks per millisecond of real time and 500 CPS owes one click every
  2 ms. Fractional rates such as 1,234 CPS stay exact, and a late wake-up is
  repaid instead of losing clicks.
- Small adaptive `SendInput` calls: one click per call at first, widening only
  while calls stay far cheaper than a slot, because wide arrays are measurably
  slower once an app throttles input.
- Three delivery modes: **SendInput**, which every app and game accepts and
  which Windows caps at roughly 6,000-7,000 CPS here; **direct window
  messages**, which skip the system input queue and measured ~25,000 CPS for
  apps that act on posted clicks; and **AUTO**, which uses SendInput up to
  6,000 CPS and direct posting above it, so a 25,000 CPS setting really does
  deliver 25,000 CPS wherever the target accepts posted clicks.
- Optional **pinned window** (background) clicking: with Direct or AUTO, the
  window under the cursor is resolved once when the run starts and clicked by
  handle from then on, so the cursor can move and the window can be covered.
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
the action is working. The publish job runs **only** for a version tag, a
`release/v<version>` branch, or a manual run with **publish** enabled, so a
plain push to `main` never creates a release. Three ways to publish:

```bat
git tag v4.0.0
git push origin v4.0.0
```

or push a branch named after the version, which is the branch-only route for
someone who can push commits but not tags:

```bat
git push origin main:release/v4.0.0
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
| Panic stop | `Esc` |

Hotkeys work while the window is unfocused. They are polled on a dedicated
daemon thread (`GetAsyncKeyState`) rather than the Tk event loop, so a stop
lands even while the dashboard is redrawing at 25,000 CPS or a settings dialog
is open. A stop is written straight to the thread-safe engine, so it does not
wait for the UI either. The stop key always wins, and both keys can be changed
under **Settings → Global hotkeys**; **ESC** is fixed and is never disabled.

Clicks stop automatically when the click target is reached, when the duration
limit expires, or when you press stop. In unlimited mode only stop ends a run.

## How the speed works

- **Small adaptive `SendInput` calls.** The `INPUT` array is built once per
  session and reused, so the hot loop never allocates, and a call carries a
  single click (DOWN + UP) at first. It widens to 2, 4 ... 16 clicks only
  while a call stays far cheaper than a 1 ms slot, and narrows again the
  moment a call gets slow or Windows rejects part of it. A 2-event call costs
  about 20 us, while a 50-event call can cost milliseconds because `SendInput`
  waits for the receiving app to drain its input queue - so wide batches lower
  the delivered rate instead of raising it.
- **A higher-priority process.** While a session runs, both the click thread
  and the process class are raised so the click stream is not preempted by
  background work, then both are restored when the run ends.
- **Millisecond batching math.** The target is converted to clicks per
  millisecond (`cps / 1000`, so 25,000 CPS is 25 clicks/ms). The fractional
  part is carried in a float bank, so a rate like 1,234 CPS (1.234 clicks/ms)
  averages exactly 1,234 CPS instead of drifting.
- **Time-based catch-up.** Credit is accrued from `perf_counter()` deltas
  rather than from a fixed tick, so a late wake-up is repaid on the next turn
  (a 2 ms hiccup at 25,000 CPS owes 50 clicks) and fractional rates never
  drift. Backlog is capped at 20 ms of the target rate, so a target faster
  than the machine can deliver degrades into a steady stream at the real
  ceiling instead of a giant burst that makes the run look frozen.
- **Paced credit, not dropped work.** Click credit is only spent on clicks
  Windows confirmed. A rejected shortfall goes back into the bank and is
  retried on the next slot, so a run ends on the exact target.
- **Three-tier wait.** A high-resolution waitable timer
  (`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`) sleeps through anything above
  2 ms, `SwitchToThread` yields the timeslice between 2 ms and 50 us, and the
  loop only spins the last 50 us — so the timeline holds without pinning a
  core. `timeBeginPeriod(1)` (the default 15.6 ms Windows tick would cap
  throughput near 64 CPS), time-critical thread priority, a higher process
  class and an opt-out from Windows 11 ecoQoS power throttling
  (`SetThreadInformation`) are all held for the session.
- **No cursor movement per click.** The mouse is not re-positioned for every
  click; SendInput clicks at the current position (fixed mode returns the
  cursor to its point once per slot).
- **Pinned target for direct delivery.** With **Pin the window under the
  cursor** enabled, the direct path resolves the window handle once at the
  start of the run and posts to it for the whole session. That removes the
  per-batch `GetCursorPos` + `WindowFromPoint` + `ScreenToClient` work (the
  fastest direct path) and lets the target be moved or covered. Windows that
  belong to ClickStorm itself are never used as a target, so a run started
  from the START button cannot click the app's own controls.

### Measured speed

Windows serialises injected `SendInput` through a single system input thread,
and the receiving application consumes it on its own message loop, so the real
ceiling belongs to the target rather than to ClickStorm. Measured with
ClickStorm 4.0.1 clicking into a window that counted every click it received:

| Target | SendInput delivered | Direct delivered |
| --- | --- | --- |
| 1,000 CPS | 999 CPS | - |
| 5,000 CPS | 4,981 CPS | - |
| 10,000 CPS | 6,194 CPS | 9,988 CPS |
| 20,000 CPS | 5,820 CPS | 19,962 CPS |
| 25,000 CPS | 6,052 CPS | 24,945 CPS |

The sending side is not the limit: measured against a null sink that accepts
every event instantly, this engine emits **~24,900 CPS** at a 25,000 target.
`SendInput` - what games and normal apps expect - tops out at roughly
**6,000-7,000 CPS** on this machine. Above that Windows still accepts the
events, but the target's message loop cannot consume them, which is why a
25,000 CPS SendInput target reads around 6,000 CPS on the live gauge. Direct
delivery skips the system input queue by posting
`WM_LBUTTONDOWN`/`WM_LBUTTONUP` straight to the window and reaches
**~25,000 CPS**, but only apps that act on posted messages respond to it (many
games ignore them and only move the cursor, so SendInput stays the default).

Version 4.0.0 replaced the wide catch-up bursts of earlier builds (16 clicks
per call, growing to 64) with small adaptive calls: on the same machine, a
5,000 CPS target delivered about **166 CPS** before that change and about
**4,980 CPS** after it.

### Why more threads do not help

A single click thread is not a limitation that extra threads can lift.
Microsoft documents `SendInput` as inserting "the events in the `INPUT`
structures **serially** into the keyboard or mouse input stream", and it
returns zero when "the input was already blocked by another thread". Every
sender therefore queues behind the same system input path and the same
target message loop, so N threads contend rather than add throughput, and
they can also interleave DOWN/UP pairs, leaving a button latched. What does
work is not pushing the input through that path at all: posted window
messages skip it, which is why the Direct and AUTO delivery modes reach the
full rate where a window accepts them.

Above the ceiling, `25,000` behaves as a "run as fast as this app allows"
setting: the live CPS readout shows what actually reached the target. Games
and apps that throttle input usually register **500-2,000 CPS** more reliably
than an extreme setting.

## Prior art and credits

ClickStorm's pacing and delivery choices were made after reading how other
Windows clickers are built and what they measured. Credit to those projects:

| Project | Licence | What was taken from it |
| --- | --- | --- |
| [local764larp/auto-clicker](https://github.com/local764larp/auto-clicker) | none declared | The delivered-versus-emitted CPS benchmark that shows wide `SendInput` batches lose delivered clicks, and the timer / yield / spin wait tiers. Measurements and design ideas only. |
| [chris101-dev/ColibriTap](https://github.com/chris101-dev/ColibriTap) | MIT | Drift-free pacing against an absolute deadline on a monotonic clock instead of `sleep(interval)`. |
| [PriyanshuGeTRekT/WinClicker](https://github.com/PriyanshuGeTRekT/WinClicker) | MIT | Clicking a window in the background with posted button messages (the **Direct message** delivery mode). |
| [Blur009/Blur-AutoClicker](https://github.com/Blur009/Blur-AutoClicker) | GPL-3.0 | Feature ideas only (click/time limits, position clicking). No code is reused, because GPL-3.0 is not compatible with this project's MIT licence. |

No source code was copied from any of these projects. Where a licence would
not have allowed reuse, only published measurements, behaviour and design
ideas were used. ClickStorm remains MIT (see [LICENSE](LICENSE)).

Deliberately **not** used: kernel-mode input drivers such as
[Interception](https://github.com/oblitum/Interception). They can lift the
ceiling for some targets, but they need an administrator-installed, signed
driver and would end the "single portable exe, no dependencies" promise.

## Settings and files

| Path | Purpose |
| --- | --- |
| `%APPDATA%\ClickStorm\settings.json` | Saved preferences, lifetime clicks, window geometry |
| `%APPDATA%\ClickStorm\error.log` | Startup, UI and update error details |

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
- **25,000 CPS reads around 6,000 CPS actual.** That is the `SendInput`
  ceiling: Windows delivers roughly 6,000-7,000 injected clicks per second
  here no matter how high the target is, and no user-mode clicker can change
  that. Set **Settings → Delivery** to **AUTO** (or **Direct window
  messages**): direct posting measured ~25,000 CPS, and AUTO picks it
  automatically above 6,000 CPS while staying on SendInput for slower rates.
  If the window under the cursor does not accept posted clicks, AUTO falls
  back to SendInput rather than clicking into nothing.
- **The update reported a missing module.** 4.0.1 starts the downloaded
  installer without inheriting the windowed app's absent std handles (that
  call was the failure), and every update failure is now written to
  `%APPDATA%\ClickStorm\error.log` with its exact message.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
