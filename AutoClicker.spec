# -*- mode: python ; coding: utf-8 -*-

# Build with: pyinstaller --noconfirm AutoClicker.spec
#
# The entry point is clickstorm.py. console=False is what stops Windows from
# opening a console / PowerShell window when the exe is launched.

a = Analysis(
    ['clickstorm.py'],
    pathex=[],
    binaries=[],
    datas=[('autoclicker.ico', '.')],
    hiddenimports=[],
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
