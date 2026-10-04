# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OWON HDS Scope — portable one-folder build."""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # UI assets (button sprite, icons, etc.)
        (str(ROOT / 'assets'), 'assets'),
        # Instrument and analysis data files
        (str(ROOT / 'modernlab'), 'modernlab'),
    ],
    hiddenimports=[
        # pyusb backend used at runtime
        'usb.backend.libusb1',
        'usb.backend.libusb0',
        'usb.backend.openusb',
        # libusb-package bundles the DLL
        'libusb_package',
        # matplotlib backends
        'matplotlib.backends.backend_tkagg',
        'matplotlib.backends._backend_tk',
        # tkinter is a stdlib module PyInstaller may miss on some builds
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'tkinter.simpledialog',
        # pypdf used for PDF export
        'pypdf',
        # pyserial COM-port path (SDS legacy, but ships in the package)
        'serial',
        'serial.tools.list_ports',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not used at runtime
        'pytest', 'unittest', 'IPython', 'jupyter',
        'scipy', 'pandas', 'cv2',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # one-folder (not one-file) for USB driver DLLs
    name='OWONScope',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                  # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                      # add icon path here when available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OWONScope',
)
