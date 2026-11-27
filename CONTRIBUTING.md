# Contributing to ClickStorm

Thanks for helping improve ClickStorm. It is a small Windows auto clicker, so
contributions are easiest to review when they stay focused. Python code is
standard-library only; the optional acceleration core is dependency-free Rust
(no crates) in `native/clickstorm-engine`.

By contributing you agree that your work is licensed under the project's
[MIT License](LICENSE).

## Ways to help

- Report bugs with your Windows version, ClickStorm version, target app and
  exact steps (include the live CPS reading and any `error.log` lines).
- Improve the docs, README or installer text.
- Send focused bug fixes and small features as pull requests.
- Test a build on Windows 10/11, x64, x86 or ARM64 hardware.

Please open an issue before starting a large feature so the approach can be
agreed on first.

## Development setup

Requirements: Windows 10 or 11 and Python 3.10+ (Tkinter is included).

```bat
git clone https://github.com/CoderProandTester/ClickStorm.git
cd ClickStorm
python clickstorm.py
```

There are no runtime dependencies and no package install step. Use
`pythonw clickstorm.py` to run without a console. Settings are written to
`%APPDATA%\ClickStorm\settings.json`.

Optional build tools:

```bat
winget install JRSoftware.InnoSetup
python -m pip install "pyinstaller>=6.10,<7"
```

## Project layout

| Path | Purpose |
| --- | --- |
| `clickstorm.py` | Entry point (also used by the PyInstaller spec) |
| `clickstorm/app.py` | Main window, session control, global hotkeys |
| `clickstorm/engine.py` | Python click engine: pacing, batching, exact targets |
| `clickstorm/native_engine.py` | ctypes binding to the Rust core + fallback selection |
| `native/clickstorm-engine/` | Rust click core (cdylib, no crates) |
| `tools/click_counter.py` | Delivered-CPS verification harness (separate process) |
| `clickstorm/winapi.py` | Win32 declarations and helpers (SendInput, cursor, keys) |
| `clickstorm/settings.py` | Settings persistence and migrations |
| `clickstorm/settings_dialog.py` | Advanced settings window |
| `clickstorm/theme.py` | Palette, fonts and custom Tk widgets |
| `AutoClicker.spec` | PyInstaller recipe (outputs `dist\ClickStorm.exe`) |
| `installer/ClickStorm.iss` | Inno Setup installer recipe |
| `.github/workflows/release.yml` | CI builds x64/x86/ARM64 installers |

## Engine guidelines

The engine is deliberately simple to reason about. Please preserve these
properties when changing it:

- **Never click on the UI thread.** Tk is only touched from the main thread;
  the worker communicates through `poll_events()` and `get_stats()`.
- **A `SendInput` call carries a time budget, not one click.** SENDINPUT
  opens at half of `round(cps * 0.006)` clicks per call and doubles to that
  budget (36 clicks at 6,000 CPS, about 167 calls a second) while calls stay
  clean; SYNC starts at one click per call and ramps to `round(cps * 0.003)`
  clicks (capped at 16). Never shrink the batch because a call was slow: that
  collapse to one click per call is what used to cap SENDINPUT near 2,000
  delivered CPS. Never grow a batch past the 20 ms credit bank either, or the
  wait for a whole batch can never be satisfied and the run freezes.
- **A short call is backpressure, not failure.** When the OS or the target
  accepts only part of a batch, set the next batch to exactly what was
  accepted, keep the remainder owed and retry it immediately. Halving on every
  short call sawtoothed SENDINPUT down to a few hundred CPS. Only a call that
  gets nothing at all backs off (0.1-1 ms), and only `STALL_SECONDS` with no
  accepted click ends the run. Never end a session because a queue is full.
- **SYNC and SENDINPUT must always use real input.** They call `SendInput`, so
  every app and game registers the clicks. Never route them through window
  messages: raw-input apps silently ignore `WM_*BUTTON*`, which is why the old
  message-based SYNC was replaced in 4.6.1.
- **Message delivery is DIRECT only, and it must never hang the clicker.**
  DIRECT posts `WM_*BUTTON*` with `PostMessage`; it is asynchronous and a
  window's posted-message queue is capped at 10,000 messages. A full queue is
  normal backpressure: pace to what `PostMessage` accepts and keep going. A
  down that posts without its up must retry the release so the button is never
  latched.
- **The native fallback is a safety net, not a retry loop.** A native session
  that refuses to start, or errors before delivering any click, restarts once
  on the Python core under the same session id. An error after real work is
  reported to the UI rather than silently retried. `UltimateEngine` is the
  only engine object the UI sees: keep the engine unified and do not add a
  backend picker or a "force Python" setting.
- **Exact click targets.** Click credit is only spent on clicks Windows
  accepted. Rejected clicks must stay due and be retried rather than silently
  dropped.
- **Responsive stop.** Stop, duration and target are checked between sends, so
  a turn must stay short even at high rates. Global hotkeys are polled on their
  own daemon thread and write to the engine directly; the UI only drains a
  queue, so a stop never waits for Tk.
- **Never target ClickStorm itself.** `make_direct_target` refuses windows that
  belong to this process, so a run started from the START button cannot post
  clicks into the app's own controls.
- **No giant catch-up bursts.** If the target is slow, the run should slow
  down; do not fire a backlog at it.

`clickstorm` Python code must remain standard-library only. The native core is
Rust with no crates: keep it that way so CI needs no package download and the
build stays reproducible. The native core is optional at runtime by design -
the Python engine must stay functional, and `make_engine()` must keep falling
back when the DLL is missing, 32-bit or ABI-mismatched.

## UI guidelines

- Use the palette, fonts and widgets in `clickstorm/theme.py`; do not hardcode
  colours or fonts in `app.py`.
- Keep the dashboard usable at 1050x700 and on high-DPI displays.
- Keep the settings window scrollable with its save row pinned; never
  reintroduce a fixed size that can crop the buttons or the save path.
- New settings must be added to `Settings`, clamped on load, and given a
  migration when the default changes.

## Testing checklist

Run before opening a pull request:

```bat
python -m compileall -q clickstorm clickstorm.py gauntlet.py tools\click_counter.py
python tools\backpressure_check.py
cargo test --manifest-path native\clickstorm-engine\Cargo.toml
cargo check --manifest-path native\clickstorm-engine\Cargo.toml --target x86_64-pc-windows-msvc
```

`tools\backpressure_check.py` needs no Windows and no display: it stubs the two
Win32 queues, models a target that drains slowly and proves that a saturated
queue is paced instead of aborting the run.

The second cargo command type-checks the real Windows implementation (the host
build only compiles the non-Windows stub). CI builds
`native\clickstorm_engine.dll` for x64 and ARM64; the x86 build intentionally
packages the Python engine because the ABI structs are only guaranteed on
64-bit Windows.

Then check the behaviour you touched manually:

- Start/stop with the buttons and with **F5**/**F9** (including quick taps).
- Click-target mode ends exactly on the target; duration mode ends on time.
- Test at low (10 CPS), normal (500-1,000 CPS) and high (4,000-6,000 CPS)
  rates; 6,000 is the cap.
- For delivery or batching changes, run
  `python tools/ci_delivery_check.py` on Windows. It loads the native core,
  starts `tools/click_counter.py`, and verifies the delivered SENDINPUT rate,
  SYNC click registration and a three-second DIRECT run at 2,000 CPS.
- Verify follow-cursor and fixed-point modes.
- Confirm the app keeps its icon, DPI scaling and saved window geometry.

For engine changes, a short scripted check is also useful:

```python
import time
from clickstorm.engine import ClickEngine, EngineConfig

engine = ClickEngine()
engine.start(EngineConfig(cps=1000, limit_mode="clicks", click_limit=1000))
while engine.is_running():
    time.sleep(0.01)
stats = engine.get_stats()
print(stats.clicks_sent, stats.elapsed, stats.reason)
engine.close()
```

Clicks are real input: point the cursor somewhere harmless before running it.

## Building the executable

```bat
cargo build --release --manifest-path native\clickstorm-engine\Cargo.toml
copy native\clickstorm-engine\target\release\clickstorm_engine.dll native\
python -m PyInstaller --noconfirm --clean AutoClicker.spec
```

Output: `dist\ClickStorm.exe`. PyInstaller is not a cross-compiler: the exe
architecture matches the Python interpreter that built it. `console=False` in
the spec is what keeps Windows from opening a console window. Delete
`native\clickstorm_engine.dll` before packaging to verify the Python fallback
build still works.

## Building the installer

The Inno Setup script takes the app version and target architecture as defines:

```bat
iscc /DMyAppArch=x64 /DMyAppVersion=4.3.0 installer\ClickStorm.iss
```

`MyAppArch` is `x64`, `x86` (32-bit) or `arm64`. Output:

```
release\ClickStorm-Setup-<version>-<arch>.exe
```

The installer requires admin, installs to Program Files, creates Start-menu
shortcuts, offers a desktop shortcut and a "Launch ClickStorm" option, and
restricts itself to Windows 10+ and the matching architecture.

## CI and releases

`.github/workflows/release.yml` builds all three installers on GitHub Actions:

| Matrix entry | Runner | Python |
| --- | --- | --- |
| x64 | `windows-latest` | x64 |
| x86 (32-bit) | `windows-latest` | x86 |
| arm64 | `windows-11-arm` | arm64 |

Each job verifies the built exe is the requested architecture, compiles the
matching installer, and uploads it as an artifact. **A push to `main`
publishes the release**: the workflow creates the `v<version>` tag itself,
attaches the three installers plus `Gauntlet-<version>.py`, and leaves the
version alone on later main pushes once it is already released. There are no
release branches. A `v*` tag push and a manual run with **publish** enabled
still work, and a tag that does not match `clickstorm/__init__.py` fails. The
ARM64 leg is allowed to fail so a runner outage cannot block the x64/x86
release.

Release procedure:

1. Bump `__version__` in `clickstorm/__init__.py` **and** in `gauntlet.py`.
2. Update `README.md` if behaviour changed.
3. Commit and push `main`:

   ```bat
   git push origin main
   ```

   The push to `main` is what publishes the release. No tag push and no
   release branch are needed.

   Alternatively tag it yourself (`git tag v4.0.0` then
   `git push origin v4.0.0`) or run the workflow from the Actions UI with
   **publish** enabled.

4. Confirm all three installers and `Gauntlet-<version>.py` are attached to
   the release.

Manual workflow runs produce the same installers as downloadable artifacts
without creating a release.

## Pull request guidelines

- Keep the change focused; separate refactors from behaviour changes.
- Describe what changed, why, and how you tested it (Windows version and
  architecture included).
- Include before/after CPS numbers for engine changes, and say which
  delivery mode (SYNC, SENDINPUT, DIRECT) and target you measured.
- Do not commit `dist/`, `build/`, `release/`, `.venv/` or `__pycache__`;
  `.gitignore` already excludes them.
- Code style is PEP 8, 4-space indents, type hints on public functions and
  short docstrings on non-obvious logic.

## Responsible use

Auto clickers can violate the terms of service of games and online services.
Do not use issues or pull requests to request bypassing anti-cheat or
automation limits; those changes will not be accepted.
