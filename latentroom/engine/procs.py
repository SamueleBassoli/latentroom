"""Engine processes (sd-cli, llama-server): start them, note how they ended.

Windows may refuse to run Latentroom's unsigned exes (Smart App Control,
an App Control or AppLocker policy, an antivirus). start() names that cause in one plain
sentence instead of the generic crash text. A non-zero exit code goes into the process's
own log and latentroom.log as "exit code 0xC0000005", so the diagnostic report carries it.
"""

import ctypes
import functools
import logging
import os
import subprocess
import threading
from pathlib import Path

from latentroom import REPO
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)
# CreateProcess errors meaning "Windows will not run this file" (Microsoft's System Error
# Codes): 4551 ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION (Smart App Control, App Control),
# 1260 ERROR_ACCESS_DISABLED_BY_POLICY (AppLocker, group policy), 225 ERROR_VIRUS_INFECTED and
# 226 ERROR_VIRUS_DELETED (an antivirus).
BLOCKS = frozenset((4551, 1260, 225, 226))
BLOCKED = (
    "Windows blocked the Latentroom engine (Smart App Control or your antivirus). "
    f'See "Blocked by Windows" in docs/INSTALL.md at {REPO}.'
)
# Exit codes of a process Windows started but could not load (ntstatus.h): 0xC0000135
# STATUS_DLL_NOT_FOUND (an antivirus quarantined one of its DLLs) and 0xC0E90002
# STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION (Smart App Control or App Control blocked a DLL).
BLOCKED_EXITS = frozenset((0xC0000135, 0xC0E90002))
# CUDA's default order puts the fastest card first; NVML (hardware.py: free VRAM, what fits)
# counts in PCI bus order. This makes CUDA device 0 the card Latentroom measured (two-GPU PCs).
ENGINE_ENV = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID"}


def start(argv: list[str], **popen) -> subprocess.Popen:
    """subprocess.Popen(argv, **popen), tied to Latentroom: Windows ends the process when
    Latentroom ends, even killed from Task Manager or crashed. Its CUDA counts the cards
    like NVML, on top of the env passed, or Latentroom's. Raises
    LatentroomError("engine_blocked", BLOCKED, 409) when Windows refuses to run the file; any
    other OSError as before."""
    env = popen.get("env")
    popen["env"] = {**(os.environ if env is None else env), **ENGINE_ENV}
    try:
        proc = subprocess.Popen(argv, **popen)
    except OSError as e:
        if getattr(e, "winerror", None) not in BLOCKS:
            raise
        log.error("Windows refused to start %s (WinError %s)", Path(argv[0]).name, e.winerror)
        raise LatentroomError("engine_blocked", BLOCKED, 409) from e
    if os.name == "nt":
        _tie(proc)
    return proc


# One Windows job object per app, "kill on job close": its only handle closes when Latentroom
# ends, however it ends, and Windows then ends every process in the job.
_job: int | None = None
_job_lock = threading.Lock()
_EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation
_KILL_ON_JOB_CLOSE = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


class _Limits(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
        ("IoInfo", ctypes.c_uint64 * 6),  # IO_COUNTERS
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@functools.cache
def _k32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
    ]  # fmt: skip
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    return k32


def _new_job() -> int:
    """A job object that kills its processes when its (never inherited) handle closes."""
    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    limits = _Limits(LimitFlags=_KILL_ON_JOB_CLOSE)
    ref, size = ctypes.byref(limits), ctypes.sizeof(limits)
    if not job or not k32.SetInformationJobObject(job, _EXTENDED_LIMITS, ref, size):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def _tie(proc: subprocess.Popen) -> None:
    """Put `proc` in the app's job. On failure the process runs untied: logged, never fatal."""
    global _job
    try:
        with _job_lock:
            if _job is None:
                _job = _new_job()
        if not _k32().AssignProcessToJobObject(_job, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        log.warning("engine process %s is not tied to Latentroom", proc.pid, exc_info=True)


def exit_text(code: int) -> str:
    """'exit code 0xC0000005': Windows crash codes read in hex (a negative POSIX code fits)."""
    return f"exit code 0x{code & 0xFFFFFFFF:08X}"


def blocked(code: int | None) -> bool:
    """True when exit code `code` means Windows security or an antivirus stopped one of the
    process's files: say BLOCKED. Python on Windows gives it unsigned (3221225781), a
    negative form reads the same."""
    return code is not None and (code & 0xFFFFFFFF) in BLOCKED_EXITS


def note_exit(code: int | None, log_file: Path) -> str:
    """How a process ended, for a latentroom.log line ("still running" when `code` is None).
    A non-zero exit code is also added to the end of the process's own log, `log_file`."""
    if code is None:
        return "still running"
    text = exit_text(code)
    if code:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{text}\n")
        except OSError:
            log.warning("cannot add the exit code to %s", log_file.name)
    return text
