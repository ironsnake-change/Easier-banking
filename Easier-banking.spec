import os
import ssl
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


datas = [("static", "static"), (".env.example", ".")]
patched_ssl_ca = Path(ssl.__file__).resolve().parent / "codex-rejected-cas.pem"
if patched_ssl_ca.is_file():
    datas.append((str(patched_ssl_ca), "."))

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("anyio")
    + ["fastapi", "starlette", "pydantic", "httpx", "openai"]
)

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Easier-banking",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=os.getenv("EASIER_BANKING_DESKTOP_CONSOLE", "").strip() == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
