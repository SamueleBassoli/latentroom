"""GPU, driver, RAM and disk check for the wizard.
All sizes are bytes (int). Never raises: problems become Hardware.status + message.
Every size the USER reads uses 1 GB = 1024^3 bytes, labelled "GB": what Windows Explorer and Task
Manager show (SPEC "Sizes"). Only gb() here and gb()/sizeGB() in web/static/ui.js format them.
Reads NVML / nvidia-smi only: no CUDA context is created, so it is safe while the GPU is busy."""

import ctypes
import logging
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The pinned engine (install.ENGINE_RELEASE) is built with CUDA toolkit 12.8.1
# (sd.cpp .github/workflows/build.yml @ e779d4f); NVIDIA's release notes table
# "CUDA 12.8 Update 1" lists Windows driver >= 572.61. Re-check when the pin changes.
MIN_DRIVER = "572.61"
DRIVER_URL = "https://www.nvidia.com/drivers"
GB = 1024**3  # the one size unit the user sees, as Windows shows it; maths stays bytes
# README: RTX 2000 to 5000. Older NVIDIA cards (GTX 10/16, old Quadro) pass, with this line.
NOT_RTX = "Latentroom is made for GeForce RTX 2000 to 5000 cards. It may not work on this one."


@dataclass(frozen=True)
class Gpu:
    name: str  # "NVIDIA GeForce RTX 4060"
    vram_total: int  # bytes
    vram_free: int  # bytes, at detection time


@dataclass(frozen=True)
class Hardware:
    status: str  # "ok" | "no_nvidia" | "driver_too_old" | "error"
    message: str  # one plain sentence for the UI, e.g. "Your PC: RTX 4060 with 8.6 GB of ..."
    gpu: Gpu | None  # first NVIDIA GPU, None when status == "no_nvidia"
    driver: str | None  # "581.42"
    ram_total: int  # bytes
    ram_free: int  # bytes
    gpus: int = 0  # NVIDIA cards found; Latentroom uses the first
    warning: str = ""  # one plain line under setup's check (NOT_RTX), "" when none


class _NoNvidia(Exception):
    """No NVIDIA driver/GPU on this PC (as opposed to a driver that is there but fails)."""


def _nvml() -> tuple[Gpu, str, int]:
    import pynvml  # nvidia-ml-py; loads nvml.dll that ships with the driver

    try:
        pynvml.nvmlInit()
    except (pynvml.NVMLError_LibraryNotFound, pynvml.NVMLError_DriverNotLoaded) as e:
        raise _NoNvidia from e
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count < 1:
            raise _NoNvidia
        # shortcut: device 0 only; multi-GPU PCs may want the biggest card (sd-cli uses 0 too)
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        gpu = Gpu(str(pynvml.nvmlDeviceGetName(handle)), int(mem.total), int(mem.free))
        return gpu, str(pynvml.nvmlSystemGetDriverVersion()), int(count)
    finally:
        pynvml.nvmlShutdown()


def _smi_exe() -> Path | None:
    """nvidia-smi.exe from the driver's own folders only. Never PATH or the current folder:
    Windows searches the current folder first, so a planted nvidia-smi.bat would run."""
    for env, sub in (("SystemRoot", "System32"), ("ProgramFiles", "NVIDIA Corporation/NVSMI")):
        base = os.environ.get(env)
        if base and (Path(base) / sub / "nvidia-smi.exe").is_file():
            return Path(base) / sub / "nvidia-smi.exe"
    return None


def _smi() -> tuple[Gpu, str, int]:
    exe = _smi_exe()
    if not exe:
        raise _NoNvidia
    query = "--query-gpu=name,memory.total,memory.free,driver_version"
    out = subprocess.run(
        [exe, query, "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    rows = out.strip().splitlines()  # one per card
    name, total, free, driver = (x.strip() for x in rows[0].split(","))  # IndexError -> "error"
    mib = 1024 * 1024
    return Gpu(name, int(float(total)) * mib, int(float(free)) * mib), driver, len(rows)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _ram() -> tuple[int, int]:
    """(total, available) system RAM in bytes; (0, 0) when unknown. On Windows total is the
    installed RAM, as Task Manager shows it (32 GB, not the 31.9 GB left after hardware reserve)."""
    try:
        if sys.platform == "win32":
            st = _MemoryStatus()
            st.dwLength = ctypes.sizeof(st)
            k32 = ctypes.windll.kernel32
            if not k32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return 0, 0
            kib = ctypes.c_ulonglong()  # fails on some VMs (no SMBIOS table): keep ullTotalPhys
            ok = k32.GetPhysicallyInstalledSystemMemory(ctypes.byref(kib))
            installed = kib.value * 1024 if ok else 0
            return max(installed, int(st.ullTotalPhys)), int(st.ullAvailPhys)
        page = os.sysconf("SC_PAGE_SIZE")
        return os.sysconf("SC_PHYS_PAGES") * page, os.sysconf("SC_AVPHYS_PAGES") * page
    except OSError, ValueError, AttributeError:
        return 0, 0


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _short(name: str) -> str:
    return name.removeprefix("NVIDIA ").removeprefix("GeForce ")


def gb(n: float, up: bool = False) -> str:
    """The ONE formatter for sizes the user reads: "4.2 GB", "32 GB"; 1 GB = 1024^3 bytes.
    up=True for amounts Latentroom needs: rounds up, so it never promises less than needed."""
    if up:
        n = math.ceil(max(n, 0) / (GB / 10)) * (GB / 10)
    return f"{n / GB:.1f}".removesuffix(".0") + " GB"


def detect() -> Hardware:
    """NVML (nvidia-ml-py), fallback `nvidia-smi --query-gpu`. Never touches CUDA."""
    ram_total, ram_free = _ram()
    gpu = driver = None
    gpus = 0
    for probe in (_nvml, _smi):
        try:
            gpu, driver, gpus = probe()
            break
        except _NoNvidia:
            continue
        except Exception:  # broken driver, odd output: try the next probe, then report
            log.warning("GPU probe %s failed", probe.__name__, exc_info=True)
            driver = ""  # remember that something NVIDIA-ish is there but misbehaves
    if gpu is None:
        if driver == "":
            return Hardware(
                "error",
                "Latentroom could not read your NVIDIA graphics card. "
                f"Reinstall the latest driver from {DRIVER_URL}, then restart Latentroom.",
                None,
                None,
                ram_total,
                ram_free,
            )
        return Hardware(
            "no_nvidia",
            "No NVIDIA graphics card found: Latentroom needs one, other cards are not supported "
            f"yet. If you do have one, install its driver from {DRIVER_URL}.",
            None,
            None,
            ram_total,
            ram_free,
        )
    if _version(driver) < _version(MIN_DRIVER):
        return Hardware(
            "driver_too_old",
            f"Your NVIDIA driver ({driver}) is too old: Latentroom needs {MIN_DRIVER} or newer. "
            f"Update it from {DRIVER_URL}, then restart Latentroom.",
            gpu,
            driver,
            ram_total,
            ram_free,
            gpus,
        )
    return Hardware(
        "ok",
        f"Your PC: {_short(gpu.name)} with {gb(gpu.vram_total)} of video memory "
        f"and {gb(ram_total)} of RAM.",
        gpu,
        driver,
        ram_total,
        ram_free,
        gpus,
        "" if "RTX" in gpu.name else NOT_RTX,  # "NVIDIA RTX A4000" counts as RTX
    )


def drive(path: Path) -> tuple[str, bool]:
    """(file system, is a network drive) of the drive holding `path`, which may not exist yet:
    ("NTFS", False), ("FAT32", False), ("NTFS", True) for a mapped network drive.
    ("", False) when unknown or not on Windows."""
    if sys.platform != "win32":
        return "", False
    k32 = ctypes.windll.kernel32
    root, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
    if not k32.GetVolumePathNameW(str(Path(path).absolute()), root, len(root)):
        return "", False
    k32.GetVolumeInformationW(root.value, None, 0, None, None, None, fs, len(fs))  # fs "" if fails
    return fs.value, k32.GetDriveTypeW(root.value) == 4  # DRIVE_REMOTE


def free_disk(path: Path) -> int:
    """Free bytes on the drive holding `path` (the folder may not exist yet). 0 if unknown."""
    p = Path(path).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0
