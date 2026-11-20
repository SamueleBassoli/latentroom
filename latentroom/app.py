"""Entry point: `python -m latentroom`. window, single instance, pickers.

Contract: main(argv) -> exit code. Flags below are fixed. Windows only (SPEC v1 platform)."""

import argparse
import ctypes
import hashlib
import logging
import os
import time
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from pathlib import Path

from latentroom import __version__
from latentroom.engine import paths

log = logging.getLogger(__name__)

TITLE = "Latentroom"
ICON = Path(__file__).parent / "web" / "static" / "icon.ico"  # also the exe and installer icon
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
WEBVIEW2_MISSING = (
    "Latentroom needs Microsoft Edge WebView2, which is missing on this PC.\n\n"
    f"Download the Evergreen Runtime from {WEBVIEW2_URL}\n"
    "install it, then open Latentroom again."
)
ALREADY = "Latentroom is already running."
# Microsoft's documented check (webview2/concepts/distribution): pv > 0.0.0.0 under one of these
_RUNTIME = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_ALREADY_EXISTS = 183  # ERROR_ALREADY_EXISTS
_mutex = None  # held for the life of the process: the single-instance lock


def webview2_installed() -> bool:
    import winreg

    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + _RUNTIME),  # 64-bit Windows
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + _RUNTIME),  # 32-bit Windows
        (winreg.HKEY_CURRENT_USER, "Software\\" + _RUNTIME),  # per-user install
    ):
        try:
            with winreg.OpenKey(root, sub) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(pv, str) and pv not in ("", "0.0.0.0"):
            return True
    return False


def message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 | 0x10000)  # error icon, foreground


def claim_instance() -> bool:
    """True for the first Latentroom on this data folder (a named mutex, freed by Windows when
    the process ends, even after a crash). A test data folder gets its own lock."""
    global _mutex
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    k32.CreateMutexW.restype = wintypes.HANDLE
    key = hashlib.sha256(str(paths.app_dir()).lower().encode()).hexdigest()[:16]
    _mutex = k32.CreateMutexW(None, False, f"Local\\Latentroom-{key}")
    return ctypes.get_last_error() != _ALREADY_EXISTS


def _pid_file() -> Path:
    return paths.app_dir() / "instance.pid"


def focus_first() -> bool:
    """Bring the running Latentroom window to the front (found by its process id)."""
    try:
        pid = int(_pid_file().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if (
            owner.value == pid
            and user32.IsWindowVisible(hwnd)
            and user32.GetWindowTextLengthW(hwnd)
        ):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    for hwnd in found:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE: un-minimise, keep maximised as it was
        user32.SetForegroundWindow(hwnd)
    return bool(found)


class Api:
    """`window.pywebview.api.*` for the page. pywebview hides names starting with "_"."""

    def __init__(self) -> None:
        self._window = None

    def pick_folder(self, start: str = "") -> str:
        """Folder the user picked, or "" when cancelled (data, output, import folders)."""
        import webview

        start = start if isinstance(start, str) and os.path.isdir(start) else ""
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return chosen[0] if chosen else ""


def _log_to_file() -> None:
    handler = RotatingFileHandler(
        paths.logs_dir() / paths.APP_LOG, maxBytes=1 << 20, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def _window(srv) -> None:
    import webview

    api = Api()
    api._window = webview.create_window(
        TITLE, srv.url, js_api=api, width=1280, height=800, min_size=(1024, 700), maximized=True
    )  # opens maximized; 1280 x 800 is the size Restore gives back
    webview.start(gui="edgechromium", icon=str(ICON))  # returns when the window closes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latentroom",
        description="Latentroom: make and edit pictures with Qwen-Image 2.1 on your own NVIDIA "
        "GPU.",
    )
    parser.add_argument("--version", action="version", version=f"Latentroom {__version__}")
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="start the local server without a window and print its address (for tests and QA)",
    )
    args = parser.parse_args(argv)
    _log_to_file()
    if not args.server_only and not webview2_installed():
        log.warning("WebView2 runtime not found")
        message_box(WEBVIEW2_MISSING)
        return 1
    if not claim_instance():
        if args.server_only:
            print(ALREADY, flush=True)
            return 1
        focus_first()
        return 0
    _pid_file().write_text(str(os.getpid()), encoding="utf-8")

    from latentroom.engine import install, runner, thumbs
    from latentroom.web import server

    for name, sweep in (  # before the server: no preview or job exists (privacy)
        ("old Library previews", thumbs.sweep),
        ("unused uploaded pictures", server.sweep_uploads),
        ("job folders a crash left", runner.sweep_jobs),
    ):
        try:  # housekeeping only: a broken history must not stop the window opening
            log.info("%d %s removed", sweep(), name)
        except Exception:
            log.exception("sweep of %s failed", name)
    try:  # the uninstaller's list of Latentroom's own files matches this version
        install.write_own_files()
    except OSError:
        log.exception("cannot write the list of Latentroom's own files")
    srv = server.start()
    # after the sweeps (they keep the uploads these jobs use), before the window opens
    if paths.queue_path().exists():  # a crash or power cut left jobs waiting
        try:
            log.info("%d jobs a crash left queued again", srv.queue().restore())
        except Exception:
            log.exception("the jobs a crash left could not be queued again")
    log.info("Latentroom %s started", __version__)
    try:
        if args.server_only:
            print(srv.url, flush=True)
            while True:
                time.sleep(1)  # Ctrl+C ends it (sleep stays interruptible on Windows)
        else:
            _window(srv)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()  # also cancels a running job: the engine process tree is killed
        _pid_file().unlink(missing_ok=True)
        log.info("Latentroom stopped")
    return 0
