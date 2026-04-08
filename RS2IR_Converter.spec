# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for RS2IR Converter
#
# Build with:
#   pip install pyinstaller
#   pyinstaller RS2IR_Converter.spec
#
# Output: dist/RS2IR Converter/RS2IR Converter.exe
# The entire dist/RS2IR Converter/ folder is the distributable — zip it up.

block_cipher = None

# vgmstream binaries to bundle alongside the exe
import glob, os
vgmstream_binaries = [
    ('vgmstream-cli.exe', '.'),
] + [(dll, '.') for dll in glob.glob('*.dll')]

a = Analysis(
    ['rs_to_immerrock.py'],
    pathex=[],
    binaries=vgmstream_binaries,
    datas=[('THIRD_PARTY_LICENSES.txt', '.'), ('icon.ico', '.')],
    hiddenimports=['mido', 'mido.backends.midi', 'soundfile'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RS2IR Converter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,       # no terminal window — GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RS2IR Converter',
)
