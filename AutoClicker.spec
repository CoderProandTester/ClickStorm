# -*- mode: python ; coding: utf-8 -*-

# Build with: pyinstaller --noconfirm AutoClicker.spec
#
# The entry point is clickstorm.py. console=False is what stops Windows from
# opening a console / PowerShell window when the exe is launched.
#
# The native Rust engine is optional. CI builds clickstorm_engine.dll and
# copies it to native/ before this spec runs; without it the app packages the
# pure Python engine, so a source build still works with no Rust toolchain.

import os

_engine_dll = os.path.join('native', 'clickstorm_engine.dll')
_binaries = [(_engine_dll, '.')] if os.path.exists(_engine_dll) else []

a = Analysis(
    ['clickstorm.py'],
    pathex=[],
    binaries=_binaries,
    datas=[('autoclicker.ico', '.')],
    hiddenimports=['clickstorm.native_engine'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClickStorm',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['autoclicker.ico'],
)
