# ClickStorm

A fast Windows auto clicker with two interchangeable engine cores and a pure
Tkinter interface. The engine keeps one pre-allocated `INPUT` batch, paces
clicks against a monotonic clock and injects them as real mouse input with
Win32 `SendInput` (or posts button messages in DIRECT mode, the default and
fastest delivery). The native Rust core (`native/clickstorm-engine`) is the
default engine and runs that hot loop outside the Python interpreter; the
identical Python core can be picked in Settings, or takes over automatically
when the DLL cannot. Settings persist in `%APPDATA%\ClickStorm\settings.json`.

Supported on **Windows 10 and Windows 11** (x64, x86 and ARM64). The native
Rust core is built for 64-bit x64 and ARM64; the 32-bit build uses the Python
engine. The packaged app is a single windowed `ClickStorm.exe`; it does
**not** open a console or PowerShell window.

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

- Click rates from 1 to **6,000 CPS**, with a logarithmic slider, presets
  and a numeric entry. The gauge always reports what the target actually
  accepted, so a throttled target settles at its real ceiling.
- **Two cores, one engine.** Pacing, adaptive batching, `SendInput` and
  message delivery run in `clickstorm_engine.dll` (Rust, no external crates);
  Python only configures a session and reads counters, so there is no
  interpreter or garbage collector in the click path. **Settings → ENGINE**
  picks **RUST** (the default and fastest) or **PYTHON**; Rust also falls back
  automatically when the DLL is missing, cannot start a run, or errors before
  delivering clicks - under the same session id. The ENGINE strip reports
  which core is clicking.
- Per-run click target from 1 to **25,000 clicks**, a duration limit, or
  unlimited running.
- All five mouse buttons: left, right, middle, X1 and X2.
- Global hotkeys: **F5 starts**, **F9 stops** (remappable in Settings), and
  **ESC always halts**, even with hotkeys switched off or the window busy.
- A scrollable, resizable settings window with SAVE always pinned on screen;
  it writes `%APPDATA%\ClickStorm\settings.json` before closing and reports a
  write failure in place.
- Follow-cursor clicking or a fixed coordinate the cursor is returned to
  while clicking.
- Optional timing jitter, 0-5 s start delay, always-on-top and
  minimize-on-start.
- Clock-based pacing: credit accrues against `perf_counter()`, so 5,000 CPS
  owes 5 clicks per millisecond of real time and 500 CPS owes one click every
  2 ms. Fractional rates such as 1,234 CPS stay exact, and a late wake-up is
  repaid instead of losing clicks.
- Batched `SendInput`: both real-input modes carry a time budget of clicks
  per call instead of one click, because a call costs far more than an event.
  SENDINPUT opens at half of a 6 ms budget (36 clicks at 6,000 CPS, about 167
  calls a second) and doubles to the full budget while calls stay clean; SYNC
  starts at one click per call then ramps to 3 ms worth (16 clicks). A partly
  accepted call is sized to what the target took instead of being halved, and
  the rest is retried immediately.
- **A busy target is paced, not stopped.** A short call (a full input queue, a
  full posted-message queue) keeps the click owed and backs off for at most a
  millisecond, so delivery settles at the rate the target really drains. Only
  a run where nothing at all is accepted for two seconds ends, with a message
  naming the queue and the mode to use instead.
- Three delivery modes, defaulting to **DIRECT**, the fastest mode. **DIRECT**
  posts `WM_*BUTTON*` messages to the window under the cursor: it skips the
  system input queue, can click a covered window, but games and raw-input apps
  ignore posted messages. **SYNC** and **SENDINPUT** inject real mouse input,
  which every app and game registers, and both batch several clicks per call
  so they can reach the 6,000 CPS ceiling.
- Optional **pinned window** (background) clicking in DIRECT mode: the window
  under the cursor is resolved once when the run starts and clicked by handle
  from then on, so the cursor can move and the window can be covered.
- Live stats: actual CPS, peak CPS, rate history, progress, ETA, batches,
  input events and failed events.
- **UPDATES** button: checks the latest GitHub release, lists the installers it
  offers and installs the one you pick.
- **Gauntlet**: a console script (`gauntlet.py`, also attached to every
  release) that drives the same engine with F6/ESC hotkeys and live CPS. It
  uses the native core whenever the ClickStorm package is present.

## Run from source

Requires Windows 10/11 and Python 3.10+ (Tkinter ships with it).

```bat
python clickstorm.py
```

Use `pythonw clickstorm.py` if you want to launch without a terminal. The
source build has no Python dependencies. The engine looks for
`clickstorm_engine.dll` in the package folder, the project root and `native\`
(the build below puts it there); if it is missing the unified engine uses its
Python core and the ENGINE strip says so. Build the DLL with the command in
the next section to get the native core.

## Gauntlet: the console harness

`gauntlet.py` is the console harness for the same click engine. Run it from
the project (or beside an installed ClickStorm) and it drives
`clickstorm.engine` - the native Rust core when `clickstorm_engine.dll` is
present, otherwise the Python engine - so there is one engine rather than two
separate implementations. The single-file release asset keeps a compact
embedded fallback so it still starts on a machine without the package, but
that fallback is never used when ClickStorm is available.

`--batch` only applies to the embedded fallback; the shared engine picks the
batch size from the rate. The console tool uses the same 6,000 CPS ceiling as
the app.

```bat
python gauntlet.py                 :: 5,000 CPS, left button
python gauntlet.py --cps 1000
python gauntlet.py --cps 0         :: uncapped: uses the 6,000 ceiling
python gauntlet.py --batch 25 --button right
```

**F6** arms and disarms, **ESC** stops the worker and exits. The console prints
achieved CPS, batch size, accepted clicks and dropped events once a second.
Clicks go to whatever is under the cursor.

The script requests 1 ms timer resolution (`timeBeginPeriod(1)`),
`THREAD_PRIORITY_ABOVE_NORMAL` for the worker (never `REALTIME`, which can
starve the HID input stack), and restores both on exit. Above 300 us of a
deadline it yields the timeslice, then spins only the last 300 us, so the
cadence is exact without pegging a core. Batches are sent through one
`SendInput` call each; the 40-byte 64-bit `INPUT` layout is asserted at start
to catch the classic 32-byte union mistake that makes `SendInput` return 0.

Like every `SendInput` tool, what the target *receives* is capped by Windows
(roughly 6,000-7,000 CPS into most apps), even though the sender may target
5,000 or 6,000. Run it from an Administrator terminal when the target window is
elevated; Windows UIPI blocks injected input across integrity levels.

## Build the executable

Build the native core first (64-bit builds only; requires Rust):

```bat
cargo build --release --manifest-path native\clickstorm-engine\Cargo.toml
copy native\clickstorm-engine\target\release\clickstorm_engine.dll native\
```

The source app loads the DLL from `native\`; the packaged app bundles it.

Then package the app:

```bat
pyinstaller --noconfirm AutoClicker.spec
```

The result is `dist\ClickStorm.exe`. The spec builds `clickstorm.py` in
windowed mode (`console=False`) with `autoclicker.ico` as the application icon,
so launching it shows only the app window. It bundles
`native\clickstorm_engine.dll` when that file exists and silently packages the
Python engine when it does not, so CI and source builds both work.

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
to a GitHub Release **on every push to `main`**. That single branch is the
whole release process - there are no release branches to create, push or clean
up. The workflow reads the version, creates the `v<version>` tag itself,
attaches the three installers and `Gauntlet-<version>.py`, and skips publishing
when that version is already on the Releases page, so ordinary main pushes do
not republish it.

```bat
git push origin main
```

A `v*` tag push or a manual run with **publish** enabled still publishes, if
you prefer one of those. The workflow reads the version from
`clickstorm/__init__.py` (and `gauntlet.py` for the script asset), so bump both
before pushing. x64 and x86 build on `windows-latest` (x86 uses 32-bit Python);
ARM64 builds natively on the `windows-11-arm` runner and is allowed to fail
without holding back the x64/x86 release. A manual run without **publish**
produces the same installers as downloadable build artifacts only.

A **Delivered-CPS test** job runs on `windows-latest` before the release job.
It builds the native core and starts `tools/click_counter.py` in its own
window, then drives the engine at a 5,000 CPS target. The release is blocked
unless the Rust core is the one clicking, SENDINPUT delivers at least 4,000
clicks per second, and SYNC registers essentially every click it asks for.

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
lands even while the dashboard is redrawing at 5,000 CPS or a settings dialog
is open. A stop is written straight to the thread-safe engine, so it does not
wait for the UI either. The stop key always wins, and both keys can be changed
under **Settings → Global hotkeys**; **ESC** is fixed and is never disabled.

Clicks stop automatically when the click target is reached, when the duration
limit expires, or when you press stop. In unlimited mode only stop ends a run.

## How the speed works

- **Batched `SendInput` calls.** The `INPUT` array is built once per session
  and reused, so the hot loop never allocates. Both real-input modes carry a
  time budget of clicks per call, because a call has a fixed cost that dwarfs
  the per-event cost: SENDINPUT fills 6 ms (36 clicks, 72 events, at 6,000 CPS
  - about 167 calls a second instead of 6,000), and SYNC starts at one click
  per call then ramps to 3 ms worth (16 clicks) while the target keeps up. A
  slow call never narrows the batch, because shrinking on a slow call drove
  the batch to one click per call and capped delivery near 2,000 CPS.
- **Backpressure, not an abort.** Windows caps a window's posted-message
  queue at 10,000 messages and inserts only the `SendInput` events that fit in
  the input queue, so a target that cannot keep up answers a batch with fewer
  clicks than it carried. Those clicks stay owed: the next call carries
  exactly what the target accepted, the rest follows immediately, and a call
  that gets nothing at all backs off for 0.1-1 ms. Delivery therefore settles
  at the target's real drain rate and the run keeps going. Before 4.6.4 two
  hundred refused calls in a row (about 50 ms) ended the session, which is why
  a 2,000 CPS DIRECT run against a full message queue stopped after a few
  seconds instead of continuing.
- **A higher-priority process.** While a session runs, both the click thread
  and the process class are raised so the click stream is not preempted by
  background work, then both are restored when the run ends.
- **Millisecond batching math.** The target is converted to clicks per
  millisecond (`cps / 1000`, so 5,000 CPS is 5 clicks/ms). The fractional
  part is carried in a float bank, so a rate like 1,234 CPS (1.234 clicks/ms)
  averages exactly 1,234 CPS instead of drifting.
- **Time-based catch-up.** Credit is accrued from `perf_counter()` deltas
  rather than from a fixed tick, so a late wake-up is   repaid on the next turn (a 2 ms hiccup at 5,000 CPS owes 10 clicks) and fractional rates never
  drift. Backlog is capped at 20 ms of the target rate, so a target faster
  than the machine can deliver degrades into a steady stream at the real
  ceiling instead of a giant burst that makes the run look frozen.
- **Paced credit, not dropped work.** Click credit is only spent on clicks
  Windows confirmed. A rejected shortfall goes back into the bank and is
  retried on the next slot, so a run ends on the exact target.
- **Three-tier wait.** A high-resolution waitable timer
  (`CREATE_WAITABLE_TIMER_HIGH_RESOLUTION`) sleeps through anything above
  0.5 ms, `SwitchToThread` yields the timeslice between 0.5 ms and 50 us, and
  the loop only spins the last 50 us — so the timeline holds without pinning
  a core between four-millisecond batches. `timeBeginPeriod(1)` (the default 15.6 ms Windows tick would cap
  throughput near 64 CPS), time-critical thread priority, a higher process
  class and an opt-out from Windows 11 ecoQoS power throttling
  (`SetThreadInformation`) are all held for the session.
- **No cursor movement per click.** The mouse is not re-positioned for every
  click; SendInput clicks at the current position (fixed mode returns the
  cursor to its point once per slot).
- **Pinned target for message delivery.** With **Pin the window under the
  cursor** enabled, the DIRECT path resolves the window handle once at the
  start of the run and sends to it for the whole session. That removes
  the per-batch `GetCursorPos` + `WindowFromPoint` + `ScreenToClient` work and
  lets the target be moved or covered. Windows that belong to ClickStorm
  itself are never used as a target, so a run started from the START button
  cannot click the app's own controls.

### Measured speed

Windows serialises injected `SendInput` through a single system input thread,
and the receiving application consumes it on its own message loop, so the real
ceiling belongs to the target rather than to ClickStorm. Measured with
ClickStorm 4.0.1 clicking into a window that counted every click it received:

| Target | SendInput delivered | Direct delivered |
| --- | --- | --- |
| 1,000 CPS | 999 CPS | - |
| 5,000 CPS | 4,981 CPS | - |

The sending side is not the limit: measured against a null sink that accepts
every event instantly, this engine emits well above the 6,000 cap. Windows
injects `SendInput` at roughly **6,000-7,000 CPS** into most apps on this
machine, so a 5,000 target is reachable, but it sits close to the wall. A
target that throttles input settles at whatever it can consume, and the live
gauge reports that number.

Version 4.6.3 replaced the one-millisecond-slot batching with a time budget
per call and stopped shrinking the batch when a call was slow. The old
shrink-on-slow rule drove the batch down to a single click per `SendInput`
call, which is what capped SENDINPUT near 2,000 delivered CPS on a fast
target. SENDINPUT now ramps from half of a 6 ms budget to the full 6 ms and
SYNC ramps from one click to a 3 ms budget, so both modes reach the rate the
input thread can carry. The
engine ceiling is 6,000 CPS; DIRECT remains the fastest mode because posted
messages never enter the input queue.

Version 4.6.4 fixed the two ways batching lost clicks on a busy target. A
partly accepted call used to halve the batch, so a congested input queue
sawtoothed SENDINPUT down to one click per call and it read only a few
hundred CPS; the batch now becomes exactly what the target accepted and the
remainder is retried in the same turn. A refused call also used to count
towards a fifty-millisecond abort, which is why a 2,000 CPS DIRECT run
stopped after a couple of seconds with the target's message queue full; a
queue that is merely full is now backpressure, and only two seconds with no
accepted click at all ends a run. The delivered order is **DIRECT, then
SYNC, then SENDINPUT**: posted messages skip the input queue, while both
`SendInput` modes wait behind the same system input thread.

### What "5,000 CPS" can and cannot mean

A clicker can *emit* 5,000 clicks a second; the numbers above are what the
receiving window actually **received**. Only the second number matters.

- **`SendInput` has a practical user-mode ceiling.** Microsoft documents that
  the events are inserted into the input stream *serially*, and Windows hands
  them to one system input thread before the target's message loop sees them.
  On the machine measured here that tops out around **6,000-7,000 delivered
  CPS**, which is why the app offers up to 6,000 and 5,000 is the comfortable
  setting.
- **Batching helps; it does not bypass the ceiling.** Sending several
  milliseconds of clicks per call removes per-call overhead, but once the
  input queue is saturated the receiving app consumes at its own rate and the
  rest is coalesced or dropped.
- **Posted messages do skip the input queue**, which is why DIRECT can make a
  normal Win32 window count more than `SendInput` can. Games read raw input or
  DirectInput, ignore posted messages, and still act once per frame; posted
  clicks are not a way to make a game see real mouse clicks.
- **Kernel drivers are not a shortcut.** Interception or a virtual HID device
  needs an administrator-installed signed driver, and the target still has to
  process the input. A game at 240 fps cannot act on more than 240 clicks a
  second, and a physical 1 kHz mouse cannot report more than 1,000 clicks a
  second in the first place.
- **Nothing here fabricates the number.** The gauge counts only events Windows
  accepted (`SendInput`'s return value) or messages successfully queued
  (`PostMessage`'s return value). A 5,000 target that reads 3,000 is the
  target refusing the rest, not a rounding error.

`tools/click_counter.py` is a separate window that counts the button messages
it receives and shows live delivered CPS. Point ClickStorm at it to measure
your own machine with any delivery mode.

### Delivery modes

**DIRECT is the default and the fastest mode.** It posts
`WM_LBUTTONDOWN`/`WM_LBUTTONUP` straight to the window under the cursor and
can pin that window, so the cursor can move or the window can be covered.
Posted messages are asynchronous and wait in a per-window queue that Windows
caps near 10,000 messages, and games or raw-input apps ignore them. Use SYNC
or SENDINPUT for those; DIRECT is for classic Win32 windows that act on button
messages.

**SYNC** and **SENDINPUT** both use `SendInput`, so the target receives real
mouse input exactly like a physical mouse. SYNC starts with one complete
DOWN/UP pair in each call, the most compatible form, then ramps the batch to
3 ms worth (16 clicks) while the target keeps up; use it when a game or app
misses clicks. SENDINPUT ramps from half of a 6 ms budget to the full budget
for the highest rate these modes can carry. Neither mode needs a window under the cursor: they
click wherever the pointer is. Delivered order is **DIRECT, then SYNC, then
SENDINPUT**: SYNC starts from the smallest, most compatible batch and widens
quickly, while SENDINPUT opens with its largest batch and has to narrow it
when a target's input queue is congested.

### Why more threads do not help

A single click thread is not a limitation that extra threads can lift.
Microsoft documents `SendInput` as inserting "the events in the `INPUT`
structures **serially** into the keyboard or mouse input stream", and it
returns zero when "the input was already blocked by another thread". Every
sender therefore queues behind the same system input path and the same
target message loop, so N threads contend rather than add throughput, and
they can also interleave DOWN/UP pairs, leaving a button latched. What does
work is not pushing the input through that path at all: posted window messages
do, which is why DIRECT can deliver more than `SendInput` to a window that
acts on them.

Above the ceiling, any setting behaves as a "run as fast as this app allows"
value: the live CPS readout shows what actually reached the target. Games and
apps that throttle input usually register **500-2,000 CPS** more reliably than
an extreme setting.

## Prior art and credits

ClickStorm's pacing and delivery choices were made after reading how other
Windows clickers are built and what they measured. Credit to those projects:

| Project | Licence | What was taken from it |
| --- | --- | --- |
| [local764larp/auto-clicker](https://github.com/local764larp/auto-clicker) | none declared | The delivered-versus-emitted CPS benchmark that shows wide `SendInput` batches lose delivered clicks, and the timer / yield / spin wait tiers. Measurements and design ideas only. |
| [chris101-dev/ColibriTap](https://github.com/chris101-dev/ColibriTap) | MIT | Drift-free pacing against an absolute deadline on a monotonic clock instead of `sleep(interval)`. |
| [PriyanshuGeTRekT/WinClicker](https://github.com/PriyanshuGeTRekT/WinClicker) | MIT | Clicking a window in the background with posted button messages (the **Direct message** delivery mode). |
| [Assassin654/AutoClicker](https://github.com/Assassin654/AutoClicker) | GPL-2.0 | Behaviour and measurements only. Its background "ghost click" mode sends button messages to a chosen window; ClickStorm evaluated that idea, then made SYNC real input in 4.6.1 because raw-input apps ignore messages. No code was copied, translated or adapted. |
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
25,000; version 4 drops the old Force Python flag; version 5 retires AUTO and
clamps a saved rate to the app maximum; version 6 makes DIRECT the default
delivery, adds the ENGINE setting and moves an old default SYNC to DIRECT).
Missing keys take safe defaults, so upgrading never resets an existing file.
The settings window writes this file when SAVE is pressed and shows the exact
path in its footer.

## Contributing

Contribution setup, engine rules, testing checklist and release steps are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

## Project layout

```
clickstorm.py            Entry point (used by AutoClicker.spec)
gauntlet.py             Standalone console Gauntlet autoclicker (up to 6,000 CPS)
native/clickstorm-engine/ Rust click core: pacing, batching, SendInput, direct
clickstorm/native_engine.py  ctypes binding to the Rust core
clickstorm/app.py        Main window, hotkeys, session control
clickstorm/engine.py     Unified engine: native core + Python core, pacing, limits
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
- **A high CPS reads lower in a busy app.** Windows injects `SendInput`
  through one system input thread at roughly 6,000-7,000 clicks per second,
  and the target's own loop has to consume them. Try DIRECT for the fastest
  delivery, or SENDINPUT for the fastest real input; lower the rate if the
  target throttles. `tools/click_counter.py` shows what it really receives.
- **DIRECT clicks do nothing in a game or a custom-drawn window.** Those apps
  do not act on `WM_*BUTTON*` messages; they read raw input. Use SYNC or
  SENDINPUT there.
- **"Failed to load Python DLL" after an in-app update.** The relaunched app
  inherited the old onefile's extraction folder, which is deleted when the old
  process exits. 4.6.2 clears PyInstaller's private `_PYI_*` variables, sets
  `PYINSTALLER_RESET_ENVIRONMENT=1` and pins TEMP/TMP before starting the
  installer, and the installer launches ClickStorm through Explorer so it
  gets the user's clean environment. That also fixes the relaunch when the
  installer was started by an older ClickStorm build.
- **The update reported a missing module.** 4.0.1 starts the downloaded
  installer without inheriting the windowed app's absent std handles (that
  call was the failure), and every update failure is now written to
  `%APPDATA%\ClickStorm\error.log` with its exact message.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
