# Сборка одного .exe: python -m PyInstaller --noconfirm Medizdeliya.spec
# Готовый файл появится в dist\Medizdeliya.exe

from PyInstaller.utils.hooks import collect_submodules

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("selectolax")
    + ["anyio", "h11", "httpx", "httpcore", "lxml.etree", "lxml._elementpath"]
)

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("app/web", "app/web")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "doctest", "pdb",
              "numpy", "pandas", "matplotlib", "PIL", "setuptools", "pip"],
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
    name="Medizdeliya",
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
)
