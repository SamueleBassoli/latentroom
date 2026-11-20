"""Engine install, model import/delete, disk use, LoRA folder.

sd-cli.exe is never run here: it loads ggml-cuda.dll at start and nothing proves `--help`
stays off the GPU. Integrity = sha256-checked zips (downloads) + the zip's own CRC on unzip
+ sd-cli.exe present; the first real job is the live test."""

import contextlib
import errno
import json
import logging
import os
import shutil
import zipfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

from latentroom.engine import catalogue, downloads, hardware, paths
from latentroom.engine.catalogue import FileItem
from latentroom.engine.downloads import _sha256, _size  # one copy of each
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)

_RELEASE_URL = "https://github.com/leejet/stable-diffusion.cpp/releases/download/"

ENGINE_RELEASE = "master-994-e779d4f"  # unzips into paths.engine_dir() / ENGINE_RELEASE
ENGINE_FILES = (
    FileItem(
        id="engine",
        name="sd-master-e779d4f-bin-win-cuda12-x64.zip",
        url=_RELEASE_URL + ENGINE_RELEASE + "/sd-master-e779d4f-bin-win-cuda12-x64.zip",
        size=343773105,
        sha256="62c9c26963c3b1009a6a43b17eb543948777c2802553c2d232e51fb0665b655a",
        licence="MIT",
        kind="engine",
    ),
    FileItem(
        id="engine-cudart",
        name="cudart-sd-bin-win-cu12-x64.zip",
        url=_RELEASE_URL + ENGINE_RELEASE + "/cudart-sd-bin-win-cu12-x64.zip",
        size=563452046,
        sha256="fe20366827d357c00797eebb58244dddab7fd9a348d70090c3871004c320f38d",
        licence="NVIDIA CUDA Toolkit EULA (redistributable runtime)",
        kind="engine",
    ),
)
MANIFEST = "installed.json"  # release folders this installer created in engine/
EXE = "sd-cli.exe"  # both zips are flat: sd-cli.exe and the DLLs sit at the zip root
FILE_LIST = "latentroom-files.txt"  # in an unzipped folder: every file unzipped, one per line
DISK_MARGIN = 200 * 1024**2  # keep this much free after an unzip or an import copy
IMPORT_DEPTH = 4  # folder levels searched below the import folder (a whole drive is no hang)

# llama.cpp runs Smart search and Enhance prompt: ONE pin, the Windows
# Vulkan build. Enhance prompt runs it on the GPU (no CUDA runtime needed); Smart search passes
# --device none and stays on the CPU (this zip holds the same CPU DLLs as the CPU build). The
# zip is a Smart search file like its two GGUFs (models/, Models screen) and Enhance prompt
# fetches it on first use; it is unzipped into engine/llama-<release>-vulkan/ and kept, so a
# damaged folder is rebuilt without a download.
LLAMA_RELEASE = "b11850"
LLAMA_ZIP = FileItem(
    id="llama",
    name=f"llama-{LLAMA_RELEASE}-bin-win-vulkan-x64.zip",
    url="https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_RELEASE}/llama-{LLAMA_RELEASE}-bin-win-vulkan-x64.zip",
    size=36969567,
    sha256="55104cbb0415cdab6a26456e0a629fde3c4c696a7f054a28510ca9d91363bef3",
    licence="MIT",
    kind="llama",
)
LLAMA_EXE = "llama-server.exe"  # flat zip too: the exe and its DLLs at the root

ENGINE_MISSING = "The Latentroom engine is not installed. Run the setup again from Settings."
IN_USE = "Latentroom cannot change that file: it is in use. Wait for the picture to finish."
SEARCH_MISSING = "Smart search is not downloaded yet. Get it in the Models tab."
SEARCH_IN_USE = "Smart search is busy. Try again in a minute."


def _folder(item: FileItem) -> Path:
    """Same rule as downloads: engine zips in engine/, everything else in models/."""
    return paths.engine_dir() if item.kind == "engine" else paths.models_dir()


def _no_space(need: int) -> LatentroomError:
    return LatentroomError(
        "no_space", f"Not enough disk space: need {hardware.gb(need, up=True)}.", 507
    )


def engine_exe() -> Path | None:
    """sd-cli.exe of the pinned release if installed and complete, else None."""
    return _complete(paths.engine_dir() / ENGINE_RELEASE, EXE)


def _complete(folder: Path, exe: str) -> Path | None:
    """folder/exe when it and every file unzipped with it are there, else None: an antivirus
    may delete a DLL later. A folder unzipped before FILE_LIST existed counts as complete."""
    path = folder / exe
    if not path.is_file():
        return None
    try:
        names = (folder / FILE_LIST).read_text(encoding="utf-8").splitlines()
    except OSError, ValueError:
        return path
    missing = [n for n in names if n and not (folder / n).is_file()]
    if missing:
        if missing[0] not in _reported:  # once: the UI asks for this every few seconds
            _reported.add(missing[0])
            log.warning("%s is incomplete: %s missing", folder.name, ", ".join(missing[:5]))
        return None
    return path


_reported: set[str] = set()


def install_engine() -> Path:
    """Unzip both downloaded ENGINE_FILES into engine/<ENGINE_RELEASE>/; return sd-cli.exe path.
    Raises LatentroomError("engine_missing", ...) when the zips are not downloaded/verified."""
    root = paths.engine_dir()
    zips = [root / f.name for f in ENGINE_FILES]
    if not all(_size(z) == f.size for z, f in zip(zips, ENGINE_FILES, strict=True)):
        exe = engine_exe()
        if exe:
            return exe  # installed earlier; the zips were removed after unzipping
        raise LatentroomError("engine_missing", ENGINE_MISSING, 409)

    final = root / ENGINE_RELEASE
    _unzip(zips, final, EXE, partial(LatentroomError, "engine_missing", ENGINE_MISSING, 409))

    # done: drop the two zips (0.9 GB) and older releases THIS installer put here. The data
    # folder can be any folder the user picked: nothing else in engine/ is ever touched.
    old = _installed_releases() - {ENGINE_RELEASE}
    _write_manifest(root, old | {ENGINE_RELEASE})  # recorded before anything is deleted
    for z in zips:
        z.unlink(missing_ok=True)
    for name in old:
        shutil.rmtree(root / name, ignore_errors=True)
    _write_manifest(root, {ENGINE_RELEASE} | {n for n in old if (root / n).exists()})
    return final / EXE


def _unzip(zips: list[Path], final: Path, exe: str, missing: Callable[[], Exception]) -> None:
    """Unzip `zips` into `final` through a staging folder, so `final` is all or nothing. A
    damaged zip is deleted (it downloads again) and raises missing(), as does a set without
    `exe`. Any `final` already there is replaced."""
    staging = final.with_name(final.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        for z in zips:
            try:
                with zipfile.ZipFile(z) as archive:
                    need = sum(i.file_size for i in archive.infolist()) + DISK_MARGIN
                    if hardware.free_disk(final.parent) < need:
                        raise _no_space(need)
                    archive.extractall(staging)  # stdlib strips "..", drive letters, abs paths
            except zipfile.BadZipFile, EOFError:  # incl. CRC mismatch: download it again
                log.warning("zip %s is damaged; deleting it", z.name, exc_info=True)
                z.unlink(missing_ok=True)
                raise missing() from None
        if not (staging / exe).is_file():
            log.error("%s not found in %s", exe, [z.name for z in zips])
            raise missing()
        files = sorted(p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file())
        (staging / FILE_LIST).write_text("\n".join(files) + "\n", encoding="utf-8")
        shutil.rmtree(final, ignore_errors=True)  # a reinstall over a broken folder
        paths.replace(staging, final, folder=True)  # an antivirus scans the new exes first
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise _no_space(DISK_MARGIN) from e
        log.warning("unzip into %s failed", final.name, exc_info=True)
        raise LatentroomError("not_available", IN_USE, 409) from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _llama_dir() -> Path:
    # "-vulkan": a folder unzipped from the old CPU zip (same release) is never reused
    return paths.engine_dir() / f"llama-{LLAMA_RELEASE}-vulkan"  # never in the engine manifest


def llama_exe() -> Path | None:
    """llama-server.exe of LLAMA_RELEASE if unzipped and complete, else None."""
    return _complete(_llama_dir(), LLAMA_EXE)


def install_llama() -> Path:
    """llama-server.exe of LLAMA_RELEASE, unzipped from the downloaded LLAMA_ZIP if needed.
    Raises LatentroomError("model_missing", ...) when the zip is not downloaded or is damaged."""
    exe = llama_exe()
    if exe:
        return exe
    z = _folder(LLAMA_ZIP) / LLAMA_ZIP.name
    missing = partial(LatentroomError, "model_missing", SEARCH_MISSING, 404)
    if _size(z) != LLAMA_ZIP.size:
        raise missing()
    _unzip([z], _llama_dir(), LLAMA_EXE, missing)
    return _llama_dir() / LLAMA_EXE


def _installed_releases() -> set[str]:
    """Release folders Latentroom unzipped into engine/ (its manifest); bad entries are ignored."""
    try:
        names = json.loads((paths.engine_dir() / MANIFEST).read_text(encoding="utf-8"))["releases"]
    except OSError, ValueError, KeyError, TypeError:
        return set()
    # a plain "master-*" folder name only: an edited manifest cannot point outside engine/
    ok = [n for n in names if isinstance(n, str) and n.startswith("master-") and Path(n).name == n]
    return set(ok)


def _write_manifest(root: Path, releases: set[str]) -> None:
    (root / MANIFEST).write_text(json.dumps({"releases": sorted(releases)}), encoding="utf-8")


def write_own_files() -> None:
    """Write paths.OWN_FILES in the data folder: each path Latentroom may put there, relative,
    one per line, a folder with a trailing backslash. Uninstall "Yes" deletes only these, never
    a whole models\\ or engine\\ folder another app may share. Call at app start
    and when the data folder changes, so it always matches this version's catalogue."""
    lines = ["# Latentroom's own files in this folder. Uninstall deletes only these, if asked."]
    items = catalogue.files() | {f.id: f for f in (*ENGINE_FILES, LLAMA_ZIP)}
    for i in items.values():
        lines += [f"{_folder(i).name}\\{i.name}{end}" for end in ("", ".part", ".import")]
    lines += [f"engine\\{n}\\" for n in sorted(_installed_releases() | {ENGINE_RELEASE})]
    lines += [f"engine\\{_llama_dir().name}\\", f"engine\\{MANIFEST}", "downloads.json"]
    text = "\n".join(lines) + "\n"
    (paths.data_dir() / paths.OWN_FILES).write_text(text, encoding="ascii")


def installed() -> set[str]:
    """FileItem ids present in the data folder with the right size."""
    have = {i.id for i in catalogue.files().values() if _size(_folder(i) / i.name) == i.size}
    if engine_exe():
        have |= {f.id for f in ENGINE_FILES}
    return have


def _candidates(folder: Path, sizes: set[int]) -> list[Path]:
    """Files under `folder` (at most IMPORT_DEPTH levels) whose size is in `sizes`."""
    found, base = [], len(folder.parts)
    for top, dirs, names in os.walk(folder):  # unreadable folders are skipped silently
        if len(Path(top).parts) - base >= IMPORT_DEPTH:
            dirs.clear()
        found += [Path(top) / n for n in names if _size(Path(top) / n) in sizes]
    return found


def _place(src: Path, item: FileItem) -> None:
    """Hardlink src into the data folder (same drive), else copy it. src is only read."""
    dest = _folder(item) / item.name
    tmp = dest.with_name(dest.name + ".import")
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
    except OSError:  # other drive, FAT/exFAT, network share: copy instead
        need = item.size + DISK_MARGIN
        if hardware.free_disk(dest.parent) < need:
            raise _no_space(need) from None
        try:
            shutil.copyfile(src, tmp)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno == errno.ENOSPC:
                raise _no_space(need) from e
            raise
        if _size(tmp) != item.size:  # the source changed or the disk lied while we copied
            tmp.unlink(missing_ok=True)
            msg = f"The copy of {src.name} is incomplete. Check the disk and try again."
            raise LatentroomError("not_available", msg, 409) from None
    paths.replace(tmp, dest)


def import_models(folder: Path) -> list[str]:
    """Copy (or hardlink on the same drive) catalogue files found in `folder`, verified by
    size/sha256. Never modifies the source folder. Returns the imported FileItem ids."""
    if not folder.is_dir():
        raise LatentroomError("bad_request", "That folder does not exist.", 400)
    have = installed()
    missing = [i for i in catalogue.files().values() if i.id not in have]
    sizes = {i.size for i in missing}
    imported = []
    for src in _candidates(folder, sizes):
        for item in [i for i in missing if i.size == _size(src) and i.id not in imported]:
            if item.sha256 is None:  # nothing to hash against: the name must match too
                ok = src.name.lower() == item.name.lower()
            else:
                try:
                    ok = _sha256(src) == item.sha256  # renamed files are found too
                except OSError:
                    ok = False
            if ok:
                try:
                    _place(src, item)
                except OSError:
                    log.warning("import of %s failed", src, exc_info=True)
                    msg = f"Latentroom could not copy {src.name}. Check both drives, then try "
                    msg += "again."
                    raise LatentroomError("not_available", msg, 409) from None
                imported.append(item.id)
                break
    return imported


def delete_model(file_id: str) -> None:
    """Delete a model file; also cancel its download and free its .part (paused or running)."""
    item = catalogue.files().get(file_id)
    if item is None:
        raise LatentroomError("not_found", "That model no longer exists.", 404)
    if item.kind == "engine":
        raise LatentroomError("bad_request", "The engine cannot be deleted from here.", 400)
    if item.kind == "llama":  # its unzipped folder first: llama-server.exe may still run
        _remove_folder(_llama_dir())
    try:
        downloads.cancel(file_id)  # deletes the .part, or the worker does once it lets go
    except LatentroomError:
        pass  # never downloaded in this data folder
    try:
        (paths.models_dir() / item.name).unlink(missing_ok=True)
    except OSError as e:  # Windows: sd-cli.exe has the file open
        raise LatentroomError("not_available", IN_USE, 409) from e
    with contextlib.suppress(OSError):  # a .part the download list forgot; never fatal
        (paths.models_dir() / (item.name + ".part")).unlink(missing_ok=True)


def _remove_folder(p: Path) -> None:
    """Delete folder p all or nothing: it is renamed aside first, which Windows refuses while
    a program inside it runs (then nothing is deleted and nothing breaks)."""
    gone = p.with_name(p.name + ".del")
    shutil.rmtree(gone, ignore_errors=True)  # left by an earlier delete
    try:
        paths.replace(p, gone, folder=True)
    except FileNotFoundError:
        return
    except OSError as e:
        raise LatentroomError("not_available", SEARCH_IN_USE, 409) from e
    shutil.rmtree(gone, ignore_errors=True)


def _tree_size(p: Path) -> int:
    return sum(_size(Path(top) / n) for top, _, names in os.walk(p) for n in names)


def disk_use() -> dict[str, int]:
    """Bytes on disk per FileItem id (engine counted under "engine")."""
    use = {}
    for item in catalogue.files().values():
        if item.kind != "engine":
            folder = _folder(item)
            n = _size(folder / item.name) + _size(folder / (item.name + ".part"))
            if n:
                use[item.id] = n
    llama = _tree_size(_llama_dir())  # unzipped from LLAMA_ZIP: counted with it, not the engine
    if llama:
        use[LLAMA_ZIP.id] = use.get(LLAMA_ZIP.id, 0) + llama
    engine = _tree_size(paths.engine_dir()) - llama  # release + any zips not yet unzipped
    if engine:
        use["engine"] = engine
    return use


def list_loras() -> list[str]:
    """LoRA names (file stem, no extension) of *.safetensors in paths.loras_dir(), sorted."""
    names = [
        p.stem
        for p in paths.loras_dir().iterdir()
        if p.is_file() and p.suffix.lower() == ".safetensors"
    ]
    return sorted(names, key=str.lower)
