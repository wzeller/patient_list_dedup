# PyInstaller build for the macOS app (PyInstaller 6.x).
# Build locally:  pyinstaller --noconfirm PatientListDedup.spec
# Produces:       dist/PatientListDedup.app

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['patient_list_dedup'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PatientListDedup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PatientListDedup',
)

app = BUNDLE(
    coll,
    name='PatientListDedup.app',
    icon=None,
    bundle_identifier='com.wzeller.patientlistdedup',
    info_plist={
        'CFBundleName': 'Patient List Dedup',
        'CFBundleDisplayName': 'Patient List Dedup',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
    },
)
