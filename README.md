# ClickStorm

A fast Windows auto clicker with a native Rust click core and a pure Tkinter
interface. The engine keeps one pre-allocated `INPUT` batch, paces clicks
against a monotonic clock and delivers them through Win32 `SendInput` or
posted window messages. The Rust core (`native/clickstorm-engine`) runs that
hot loop outside the Python interpreter; when its DLL is absent the app falls
back to the identical pure Python engine, so a source run needs no toolchain.
Settings persist in `%APPDATA%\ClickStorm\settings.json`.

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

- Click rates from 1 to **100,000 CPS**, with a logarithmic slider, presets
  and a numeric entry. The gauge always reports what the target actually
  accepted, so an unreachable setting settles at the real ceiling.
- **Native Rust engine, with a real fallback.** Pacing, adaptive batching,
  `SendInput` and message delivery all run in `clickstorm_engine.dll` (Rust,
  no external crates). Python only configures a session and reads counters, so
  there is no interpreter or garbage collector in the click path. If the DLL
  cannot start a run, or errors before it has delivered any clicks, that run
  restarts automatically on the Python engine under the same session. A
  setting can force the Python engine permanently; the ENGINE strip always
  says which core is clicking.
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
- Four delivery modes: **SendInput**, which every app and game accepts and
  which Windows caps at roughly 6,000-7,000 CPS here; **direct window
  messages**, which skip the system input queue but are still asynchronous
  and capped by the 10,000-message posted queue; **SYNC**, which uses
  `SendMessageTimeout` so the window handles each click before the call
  returns and nothing waits in that queue; and **AUTO**, which uses SendInput
  up to 6,000 CPS and SYNC above it. SYNC is the path that can genuinely
  register rates above 25,000 in a Win32 window that handles button messages
  quickly.
- Optional **pinned window** (background) clicking: with Direct, SYNC or
  AUTO, the window under the cursor is resolved once when the run starts and
  clicked by handle from then on, so the cursor can move and the window can
  be covered.
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
source build has no Python dependencies. If `clickstorm_engine.dll` is not
next to the package the app runs the Python engine and the ENGINE strip says
`Python engine`; build the DLL with the command in the next section to get
the native core.

## Gauntlet: the console harness

`gauntlet.py` is the console harness for the same click engine. Run it from
the project (or beside an installed ClickStorm) and it drives
`clickstorm.engine` - the native Rust core when `clickstorm_engine.dll` is
present, otherwise the Python engine - so there is one engine rather than two
separate implementations. The single-file release asset keeps a compact
embedded fallback so it still starts on a machine without the package, but
that fallback is never used when ClickStorm is available.

`--batch` only applies to the embedded fallback; the shared engine chooses the
batch size adaptively because wide `SendInput` calls are slower once a target
throttles input.

```bat
python gauntlet.py                 :: 25,000 CPS, left button, 25 clicks per call
python gauntlet.py --cps 5000
python gauntlet.py --cps 0         :: uncapped: send as fast as Windows accepts
python gauntlet.py --batch 50 --button right
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
(roughly 6,000-7,000 CPS into most apps), even though the sender can emit
25,000. Run it from an Administrator terminal when the target window is
elevated; Windows UIPI blocks injected input across integrity levels.

## Build the executable

Build the native core first (64-bit builds only; requires Rust):

```bat
cargo build --release --manifest-path native\clickstorm-engine\Cargo.toml
copy native\clickstorm-engine\target\release\clickstorm_engine.dll native\
```

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
- **Pinned target for message delivery.** With **Pin the window under the
  cursor** enabled, the Direct and SYNC paths resolve the window handle once
  at the start of the run and send to it for the whole session. That removes
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

### What "25,000 CPS" can and cannot mean

A clicker can *emit* 25,000 clicks a second; the numbers above are what the
receiving window actually **received**. Only the second number matters.

- **`SendInput` has a hard user-mode ceiling.** Microsoft documents that the
events are inserted into the input stream *serially*, and Windows hands them
to one system input thread before the target's message loop sees them. On the
machine measured here that tops out around **6,000-7,000 delivered CPS** no
matter how many events are batched into a call.
- **Wider batches do not bypass it.** A GitHub autoclicker claiming 100,000
CPS from arrayed `SendInput` is counting events handed to the OS queue, not
clicks the target processed. Once that queue is saturated the receiving app
consumes at its own rate and the rest is coalesced or dropped.
- **Posted messages do skip the input queue**, which is why the Direct/AUTO
path can make a normal Win32 window count about **25,000 CPS**. Games read
raw input or DirectInput, ignore posted messages, and still act once per
frame; posted clicks are not a way to make a game see 25,000 real mouse
clicks.
- **Kernel drivers are not a shortcut.** Interception or a virtual HID device
needs an administrator-installed signed driver, and the target still has to
process the input. A game at 240 fps cannot act on more than 240 clicks a
second, and a physical 1 kHz mouse cannot report more than 1,000 clicks a
second in the first place.
- **Nothing here fabricates the number.** The gauge counts only events Windows
accepted (`SendInput`'s return value) or messages successfully queued
(`PostMessage`'s return value). A 25,000 target that reads 6,000 is Windows
refusing the rest, not a rounding error.

`tools/click_counter.py` is a separate window that counts the button messages
it receives and shows live delivered CPS. Point ClickStorm at it to measure
your own machine with any delivery mode.

### Synchronous delivery: registering more than 25,000

Posted messages are asynchronous. They wait in a per-window queue that Windows
caps at 10,000 messages, so a target that falls behind loses the surplus even
though `PostMessage` reported success. **SYNC** does not post: it calls
`SendMessageTimeoutW`, which runs the target's window procedure before the
call returns. A click is handled, not queued, so the delivered count matches
the sent count up to the speed of the target's own handler, and a 25,000+
setting can be registered by a Win32 window that processes button messages
quickly.

Each send has a 20 ms timeout, so a hung window cannot freeze the clicker, and
a timed-out release is posted so the button never latches. This is the same class of idea as the background "ghost click" mode
in [Assassin654/AutoClicker](https://github.com/Assassin654/AutoClicker):
synchronous button messages to a chosen window. ClickStorm's implementation is
independent, written in Rust, and adds timeouts/hang protection; no code was
copied or translated. Games that read raw input or DirectInput still ignore
these messages, so SYNC raises registered clicks in classic Win32 apps, not in
games.

### Why more threads do not help

A single click thread is not a limitation that extra threads can lift.
Microsoft documents `SendInput` as inserting "the events in the `INPUT`
structures **serially** into the keyboard or mouse input stream", and it
returns zero when "the input was already blocked by another thread". Every
sender therefore queues behind the same system input path and the same
target message loop, so N threads contend rather than add throughput, and
they can also interleave DOWN/UP pairs, leaving a button latched. What does
work is not pushing the input through that path at all: window messages do,
which is why the Direct, SYNC and AUTO delivery modes reach the full rate
where a window accepts them. SYNC additionally makes the target handle each
click before the call returns, so the posted queue's 10,000-message cap no
longer limits what is registered.

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
| [Assassin654/AutoClicker](https://github.com/Assassin654/AutoClicker) | GPL-2.0 | Behaviour and measurements only. Its background "ghost click" mode sends button messages synchronously to a chosen window, which is why **SYNC** exists. ClickStorm implements the idea independently with `SendMessageTimeoutW`, hang protection and a Rust core; no code was copied, translated or adapted. |
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
25,000). Version 3 adds the Force Python engine option; missing keys take
safe defaults, so upgrading never resets an existing file.

## Contributing

Contribution setup, engine rules, testing checklist and release steps are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).

## Project layout

```
clickstorm.py            Entry point (used by AutoClicker.spec)
gauntlet.py             Standalone console Gauntlet autoclicker (25,000+ CPS)
native/clickstorm-engine/ Rust click core: pacing, batching, SendInput, direct
clickstorm/native_engine.py  ctypes binding and Python-fallback selection
clickstorm/app.py        Main window, hotkeys, session control
clickstorm/engine.py     Python click engine: pacing, batching, exact targets
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
  ceiling: Windows serialises injected input through one system input thread,
  so it delivers roughly 6,000-7,000 clicks per second here no matter how high
  the target is. No user-mode clicker can change that, and wider batches only
  fill the queue. Set **Settings → Delivery** to **AUTO** (or **SYNC**):
  synchronous messages bypass both the input thread and the posted-message
  queue, so a Win32 window that handles button messages quickly registers
  every click. Games and raw-input apps ignore them. Use
  `tools/click_counter.py` to see what a target really receives.
- **SYNC clicks do nothing in a game or a custom-drawn window.** Those apps do
  not act on `WM_*BUTTON*` messages; they read raw input. Use SendInput there
  and expect the ~6,000 CPS Windows ceiling.
- **SYNC reports timeouts.** The target's window procedure is busy or not
  pumping messages. Each send gives up after 20 ms; lower the rate or use
  Direct/SendInput for that window.
- **The update reported a missing module.** 4.0.1 starts the downloaded
  installer without inheriting the windowed app's absent std handles (that
  call was the failure), and every update failure is now written to
  `%APPDATA%\ClickStorm\error.log` with its exact message.
- **Windows SmartScreen warns about the installer.** The build is not code
  signed, so choose "More info" → "Run anyway".
