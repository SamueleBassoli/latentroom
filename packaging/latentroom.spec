# PyInstaller spec for Latentroom.exe.
# Build from the repo root:  uv run pyinstaller packaging/latentroom.spec --distpath dist --workpath build
# One folder (onedir), not one file: starts faster (no unpack to %TEMP% on every launch) and
# trips fewer antivirus false positives. No console window. Inno Setup packs dist/Latentroom/.
import sys
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).parent  # noqa: F821 (SPECPATH is set by PyInstaller)
sys.path.insert(0, str(ROOT))
from latentroom import __version__  # noqa: E402

nums = tuple(int(n) for n in __version__.split("."))  # "0.1.0" -> (0, 1, 0); fails loudly otherwise
assert len(nums) == 3, f"__version__ must be X.Y.Z, got {__version__!r}"
ver4 = nums + (0,)

# Code finds these next to its own module (Path(__file__).parent...), so keep the package layout.
datas = [(str(ROOT / "latentroom" / "web" / "static"), "latentroom/web/static")]
if (ROOT / "latentroom" / "data").is_dir():  # the model catalogue (latentroom/data/)
    datas.append((str(ROOT / "latentroom" / "data"), "latentroom/data"))

a = Analysis(  # noqa: F821
    [str(ROOT / "latentroom" / "__main__.py")],
    pathex=[str(ROOT)],
    datas=datas,
    # pywebview imports every GUI backend; only EdgeChromium (WinForms + pythonnet) is used here.
    excludes=["tkinter", "qtpy", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"],
)

# THIRD-PARTY-NOTICES.txt from what Analysis bundled (packaging/notices.py). Made here, so a
# missing licence text stops the build now; written next to the exe after COLLECT.
sys.path.insert(0, SPECPATH)  # noqa: F821
from notices import bundled, notices  # noqa: E402

third_party = notices(bundled(dest for dest, *_ in a.pure + a.binaries + a.datas))
pyz = PYZ(a.pure)  # noqa: F821

version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=ver4, prodvers=ver4),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # US English, Unicode
                    [
                        StringStruct("FileDescription", "Latentroom"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", "Latentroom"),
                        StringStruct("OriginalFilename", "Latentroom.exe"),
                        StringStruct("ProductName", "Latentroom"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Latentroom",
    console=False,
    icon=str(ROOT / "latentroom" / "web" / "static" / "icon.ico"),
    version=version,
    upx=False,  # UPX-packed files are a classic antivirus false-positive trigger
)
coll = COLLECT(exe, a.binaries, a.datas, name="Latentroom", upx=False)  # noqa: F821
# Next to the exe, so Inno Setup's dist\Latentroom\* puts it next to LICENSE in the app folder.
Path(DISTPATH, "Latentroom", "THIRD-PARTY-NOTICES.txt").write_text(third_party, "utf-8")  # noqa: F821
