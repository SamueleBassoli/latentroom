"""Data-folder layout: the one place that knows where Latentroom reads and writes.

Two roots:
  app_dir()  fixed, small: settings.json, phones.json, logs/. LATENTROOM_HOME overrides it
             (tests, QA; see home_override()).
  data_dir() big, user may put it on another drive: engine/, models/, loras/, jobs/,
             uploads/, thumbs/, db.sqlite, downloads.json, queue.json. Chosen in the wizard
             (settings "data_dir").
Directory getters create the folder; file getters create the parent folder.
"""

import ctypes
import json
import os
import time
import uuid
from pathlib import Path

# Windows refuses to open or replace a file for a moment while another reader, a save or a
# virus scan holds it (PermissionError). Wait that out, for up to 1 s, instead of failing.
BUSY_TRIES, BUSY_WAIT = 50, 0.02
# Renames get longer: an antivirus scans a file just written, or a
# folder just unzipped (sd-cli.exe and its DLLs), and Windows refuses to move it meanwhile.
FILE_WAIT, FOLDER_WAIT = 2.0, 10.0
APP_LOG = "latentroom.log"  # in logs_dir(); the diagnostic report shows its last lines
# The list of Latentroom's own files in a data folder: install.write_own_files() writes it, the
# uninstaller deletes only what it names. Its name is in packaging/latentroom.iss too.
# A folder with it is Latentroom's. Never db.sqlite or downloads.json for that: common names,
# another app's folder may have them.
OWN_FILES = "latentroom-files.txt"


def read_json(p: Path) -> object:
    """The JSON in `p`; None when it is missing or not JSON. A busy file is retried, not read
    as missing: that showed the wizard again and the default data folder."""
    for _ in range(BUSY_TRIES):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(BUSY_WAIT)
        except OSError, ValueError:
            return None
    return None


def replace(src: Path, dst: Path, folder: bool = False) -> None:
    """os.replace(src, dst), tried again with a growing pause while Windows refuses it with
    PermissionError (WinError 5, 32, 33), for up to FILE_WAIT s, or FOLDER_WAIT s for a folder.
    Then that PermissionError is raised, as without the wait."""
    deadline = time.monotonic() + (FOLDER_WAIT if folder else FILE_WAIT)
    pause = 0.05
    while True:
        try:
            return os.replace(src, dst)
        except PermissionError:
            if time.monotonic() + pause > deadline:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 1.0)


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def home_override() -> str:
    """The test/QA data folder (LATENTROOM_HOME), "" for real use."""
    return os.environ.get("LATENTROOM_HOME") or ""


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def app_dir() -> Path:
    env = home_override()
    if env:
        return _mk(Path(env))
    return _mk(local_appdata() / "Latentroom")


def settings_path() -> Path:
    return app_dir() / "settings.json"


def logs_dir() -> Path:
    return _mk(app_dir() / "logs")


def data_dir() -> Path:
    saved = read_json(settings_path())
    chosen = saved.get("data_dir") if isinstance(saved, dict) else None
    return _mk(Path(chosen)) if isinstance(chosen, str) and chosen else app_dir()


def data_folder_for(picked: Path) -> Path:
    """The data folder for a folder the user picked: the folder itself when it is new, empty or
    Latentroom's already (its file list, or the folder its settings use), else a Latentroom
    folder inside it. Latentroom never adds its models\\ and engine\\ to another app's folder
    (picking D:\\AI must leave D:\\AI\\models be)."""
    try:
        if not picked.is_dir() or not any(picked.iterdir()) or picked in (app_dir(), data_dir()):
            return picked
        if (picked / OWN_FILES).exists():
            return picked
    except OSError:
        return picked  # unreadable: the caller's mkdir and every later use report it
    return picked / "Latentroom"


def engine_dir() -> Path:
    """Engine zips are downloaded here; each pinned release unzips into engine/<release>/."""
    return _mk(data_dir() / "engine")


def models_dir() -> Path:
    """Diffusion models AND shared components (VAE, text encoder, mmproj, upscaler, Turbo LoRA)."""
    return _mk(data_dir() / "models")


def loras_dir() -> Path:
    """The user's own LoRA files (*.safetensors). Shown by "Open LoRA folder"."""
    return _mk(data_dir() / "loras")


def jobs_dir() -> Path:
    """Per-job scratch: jobs/<job_id>/ holds resized refs, mask, raw engine output, engine log."""
    return _mk(data_dir() / "jobs")


def uploads_dir() -> Path:
    """Images the UI uploaded (drag-and-drop, file picker, phone) for use as refs/init/mask."""
    return _mk(data_dir() / "uploads")


def thumbs_dir() -> Path:
    """Saved Library previews (engine/thumbs.py). Safe to delete: remade."""
    return _mk(data_dir() / "thumbs")


def db_path() -> Path:
    return data_dir() / "db.sqlite"


def downloads_state_path() -> Path:
    return data_dir() / "downloads.json"


def queue_path() -> Path:
    """The waiting and running picture jobs; only there after a crash or power cut."""
    return data_dir() / "queue.json"


_FOLDERID_PICTURES = uuid.UUID("33E28130-4E1E-4676-835A-98395C3BC3BB")


def pictures_dir() -> Path:
    """The user's Pictures folder where Windows keeps it, also when it was moved (OneDrive,
    another drive); ~/Pictures when Windows cannot say."""
    try:
        get = ctypes.WinDLL("shell32").SHGetKnownFolderPath
        get.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        out = ctypes.c_void_p()
        hr = get(_FOLDERID_PICTURES.bytes_le, 0, None, ctypes.byref(out))  # bytes_le = a GUID
        try:
            if hr == 0 and out.value:
                return Path(ctypes.wstring_at(out.value))
        finally:
            ctypes.WinDLL("ole32").CoTaskMemFree(out)  # the caller frees it, even on failure
    except Exception:  # not Windows, or the call failed: the usual place
        pass
    return Path.home() / "Pictures"


def default_output_dir() -> Path:
    """Where finished PNGs go unless the user picked another folder in Settings."""
    if home_override():
        return _mk(app_dir() / "output")  # tests/QA never write into the real Pictures folder
    return _mk(pictures_dir() / "Latentroom")
