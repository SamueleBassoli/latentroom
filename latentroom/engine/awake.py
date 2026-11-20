"""Keep the PC awake while a model download or a picture job runs: Windows does not go to
sleep, the display may still turn off. No setting and no UI: the ask ends with the work.

SetThreadExecutionState asks for the calling THREAD: "The system tracks each thread that calls
SetThreadExecutionState" (learn.microsoft.com, System Sleep Criteria), and a clear from another
thread does not undo it. So each worker wraps its own work in keep_awake() on its own thread; no
counter is shared between threads. When Latentroom closes, Windows drops its asks."""

import contextlib
import ctypes
import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set(flags: int) -> None:
    if sys.platform != "win32":
        return
    if not ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)):
        log.warning("Windows refused the keep-awake request %#x", flags)


@contextlib.contextmanager
def keep_awake():
    """The PC stays awake inside the block; the ask is cleared on the way out, also on errors.
    Never nest it on one thread: the inner exit would clear the outer ask."""
    _set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        _set(ES_CONTINUOUS)
