# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['direct_mailer.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['dns.resolver', 'dns.rdatatype', 'dns.rdataclass', 'socks', 'email.mime.multipart', 'email.mime.text', 'email.mime.base'],
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
    name='DirectMailer',
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
    icon=['sb.ico'],
)
