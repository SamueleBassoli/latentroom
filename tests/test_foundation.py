import importlib
import json
import os
import subprocess
import sys

import pytest

from latentroom.engine import paths
from latentroom.engine.jobs import JobRequest, LoraRef
from latentroom.errors import LatentroomError

MODULES = [
    "latentroom",
    "latentroom.app",
    "latentroom.errors",
    "latentroom.settings",
    "latentroom.web.server",
    *(
        f"latentroom.engine.{m}"
        for m in (
            "paths",
            "hardware",
            "catalogue",
            "downloads",
            "install",
            "jobs",
            "runner",
            "history",
            "images",
            "procs",
        )
    ),
]


@pytest.mark.parametrize("name", MODULES)
def test_every_module_imports(name):
    importlib.import_module(name)


def test_layout_lives_under_latentroom_home(latentroom_home):
    assert paths.app_dir() == latentroom_home
    assert paths.data_dir() == latentroom_home
    for d in (
        paths.engine_dir(),
        paths.models_dir(),
        paths.loras_dir(),
        paths.jobs_dir(),
        paths.uploads_dir(),
        paths.logs_dir(),
        paths.default_output_dir(),
    ):
        assert d.is_dir() and d.is_relative_to(latentroom_home)
    assert paths.db_path() == latentroom_home / "db.sqlite"


def test_data_dir_follows_settings(latentroom_home, tmp_path):
    big = tmp_path / "other-drive" / "Latentroom"
    paths.settings_path().write_text(json.dumps({"data_dir": str(big)}), encoding="utf-8")
    assert paths.data_dir() == big and paths.models_dir() == big / "models"
    assert paths.logs_dir() == latentroom_home / "logs"  # logs + settings stay in the app dir
    paths.settings_path().write_text("not json", encoding="utf-8")
    assert paths.data_dir() == latentroom_home  # broken settings never crash, default wins


def test_only_our_own_list_makes_a_folder_ours(latentroom_home, tmp_path):
    """db.sqlite and downloads.json are common names: a folder with them is another app's
    and gets a Latentroom folder inside (before, Latentroom wrote its downloads.json over that
    app's own). Ours: the folder with Latentroom's file list, or the one its settings use."""
    other, ours, current = tmp_path / "other", tmp_path / "ours", tmp_path / "current"
    for d in (other, ours, current):
        d.mkdir()
    for name in ("db.sqlite", "downloads.json"):
        (other / name).write_text("{}")
    (ours / paths.OWN_FILES).write_text("downloads.json\n")
    (current / "db.sqlite").write_bytes(b"")  # in use, its list not written (yet)
    paths.settings_path().write_text(json.dumps({"data_dir": str(current)}), encoding="utf-8")
    assert paths.data_folder_for(other) == other / "Latentroom"
    for d in (ours, current, latentroom_home):
        assert paths.data_folder_for(d) == d


def test_pictures_go_where_windows_keeps_them(latentroom_home, tmp_path, monkeypatch):
    if sys.platform == "win32":  # the real call: the folder Explorer shows, wherever it was moved
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            mine = os.path.expandvars(winreg.QueryValueEx(k, "My Pictures")[0])
        assert os.path.normcase(paths.pictures_dir()) == os.path.normcase(mine)

    def broken(_name):
        raise OSError("no shell32")

    monkeypatch.setattr(paths.ctypes, "WinDLL", broken)
    assert paths.pictures_dir() == paths.Path.home() / "Pictures"  # any error: the usual place

    moved = tmp_path / "OneDrive" / "Pictures"
    monkeypatch.setattr(paths, "pictures_dir", lambda: moved)
    monkeypatch.setattr(paths, "home_override", lambda: "")  # as for a real user
    assert paths.default_output_dir() == moved / "Latentroom"
    assert (moved / "Latentroom").is_dir()


def test_job_request_round_trip():
    req = JobRequest(prompt="a lighthouse at dusk", loras=[LoraRef("test-style-a", 0.8)])
    again = JobRequest.from_dict(json.loads(json.dumps(req.to_dict())))
    assert again == req and isinstance(again.loras[0], LoraRef)


@pytest.mark.parametrize("bad", [[], {"prompt": "x", "evil": 1}, {"loras": [{"nam": "x"}]}])
def test_job_request_rejects_bad_json(bad):
    with pytest.raises(LatentroomError) as e:
        JobRequest.from_dict(bad)
    assert e.value.code == "bad_request" and e.value.status == 400


def test_cli_help_runs():
    r = subprocess.run(
        [sys.executable, "-m", "latentroom", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0 and "--server-only" in r.stdout
