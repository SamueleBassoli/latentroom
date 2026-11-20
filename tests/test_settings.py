import ctypes
import json
import threading
from ctypes import wintypes

from latentroom import settings
from latentroom.engine import paths


def _hold(p, seconds: float) -> None:
    """Open `p` so nobody else may read or replace it (as a virus scan or another reader can),
    and let go after `seconds`."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.CreateFileW(str(p), 0x80000000, 0, None, 3, 0, None)  # GENERIC_READ, no sharing
    assert h != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    threading.Timer(seconds, k32.CloseHandle, (h,)).start()


def test_a_busy_file_is_waited_for_not_read_as_missing():
    """While something held settings.json, a read saw "no file" (first_run_done
    False: the wizard came back) and a save failed. Both now wait for the file."""
    settings.save(settings.Settings(first_run_done=True))
    _hold(paths.settings_path(), 0.2)
    assert settings.load().first_run_done
    _hold(paths.settings_path(), 0.2)
    settings.save(settings.Settings(first_run_done=True, model_id="std-q4"))
    assert settings.load().model_id == "std-q4"


def test_defaults_when_missing():
    assert settings.load() == settings.Settings()


def test_round_trip_and_atomic(latentroom_home):
    s = settings.Settings(output_dir="D:/Pics", max_vram_gb=6.5, adult_ok=True, model_id="std-q4_k")
    settings.save(s)
    assert settings.load() == s
    assert [p.name for p in latentroom_home.iterdir() if p.suffix == ".tmp"] == []


def test_broken_file_never_raises():
    paths.settings_path().write_text("{not json", encoding="utf-8")
    assert settings.load() == settings.Settings()
    paths.settings_path().write_text("[1, 2]", encoding="utf-8")
    assert settings.load() == settings.Settings()


def test_wrong_types_and_unknown_keys_fall_back():
    bad = {"adult_ok": "yes", "max_vram_gb": True, "data_dir": 5, "first_run_done": True, "x": 1}
    paths.settings_path().write_text(json.dumps(bad), encoding="utf-8")
    assert settings.load() == settings.Settings(first_run_done=True)


def test_data_dir_is_what_paths_reads(tmp_path):
    big = tmp_path / "big"
    settings.save(settings.Settings(data_dir=str(big)))
    assert paths.data_dir() == big
