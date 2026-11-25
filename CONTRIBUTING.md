# Contributing to ClickStorm

Thanks for helping improve ClickStorm. It is a small, dependency-free Windows
auto clicker, so contributions are easiest to review when they stay focused and
keep the standard-library-only design.

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
| `clickstorm/engine.py` | Click engine: pacing, batching, exact targets |
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
- **Small `SendInput` arrays.** Large arrays make `SendInput` block while the
  target drains its input queue, which shows up as a low click rate and a
  sluggish STOP. Keep bursts small and adaptive.
- **`auto` delivery must never click into nothing.** It picks direct posting
  only above `SENDINPUT_CEILING_CPS` and only when a target window resolves;
  otherwise it falls back to SendInput for that session.
- **Exact click targets.** Click credit is only spent on clicks Windows
  accepted. Rejected clicks must stay due and be retried rather than silently
  dropped.
- **Responsive stop.** Stop, duration and target are checked between sends, so
  a turn must stay short even at high rates.
- **No giant catch-up bursts.** If the target is slow, the run should slow
  down; do not fire a backlog at it.

`clickstorm` must remain standard-library only. If a feature needs a dependency,
discuss it in an issue first.

## UI guidelines

- Use the palette, fonts and widgets in `clickstorm/theme.py`; do not hardcode
  colours or fonts in `app.py`.
- Keep the dashboard usable at 1050x700 and on high-DPI displays.
- New settings must be added to `Settings`, clamped on load, and given a
  migration when the default changes.

## Testing checklist

Run before opening a pull request:

```bat
python -m compileall -q clickstorm clickstorm.py
```

Then check the behaviour you touched manually:

- Start/stop with the buttons and with **F5**/**F9** (including quick taps).
- Click-target mode ends exactly on the target; duration mode ends on time.
- Test at low (10 CPS), normal (500-1,000 CPS) and high (10,000+ CPS) rates.
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
python -m PyInstaller --noconfirm --clean AutoClicker.spec
```

Output: `dist\ClickStorm.exe`. PyInstaller is not a cross-compiler: the exe
architecture matches the Python interpreter that built it. `console=False` in
the spec is what keeps Windows from opening a console window.

## Building the installer

The Inno Setup script takes the app version and target architecture as defines:

```bat
iscc /DMyAppArch=x64 /DMyAppVersion=4.1.0 installer\ClickStorm.iss
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
matching installer, and uploads it as an artifact. Pushing a `v*` tag publishes
the three installers to a GitHub Release, and so does a push to a
`release/v<version>` branch or a manual run with **publish** enabled (those two
create the tag). The workflow also fails if the tag or release branch does not
match `clickstorm/__init__.py`. The ARM64 leg is allowed to fail so a runner
outage cannot block the x64/x86 release.

Release procedure:

1. Bump `__version__` in `clickstorm/__init__.py`.
2. Update `README.md` if behaviour changed.
3. Commit and push `main`.
4. Tag and push the tag:

   ```bat
   git tag v4.0.0
   git push origin v4.0.0
   ```

   Without local git credentials, push a release branch instead — it publishes
   the same way and also creates the tag:

   ```bat
   git push origin main:release/v4.0.0
   ```

   Or run the workflow from the Actions UI with **publish** enabled.

5. Confirm all three installers are attached to the release.

Manual workflow runs produce the same installers as downloadable artifacts
without creating a release.

## Pull request guidelines

- Keep the change focused; separate refactors from behaviour changes.
- Describe what changed, why, and how you tested it (Windows version and
  architecture included).
- Include before/after CPS numbers for engine changes.
- Do not commit `dist/`, `build/`, `release/`, `.venv/` or `__pycache__`;
  `.gitignore` already excludes them.
- Code style is PEP 8, 4-space indents, type hints on public functions and
  short docstrings on non-obvious logic.

## Responsible use

Auto clickers can violate the terms of service of games and online services.
Do not use issues or pull requests to request bypassing anti-cheat or
automation limits; those changes will not be accepted.
