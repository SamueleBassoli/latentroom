"""Packaging guards. The real check is scripts/build.ps1 end to end; these
lock the rules a later edit could silently break."""

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

from latentroom import __version__

ROOT = Path(__file__).parent.parent


def test_version_is_plain_x_y_z():
    # The exe version resource and the installer file name are built from it.
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_version_is_the_same_in_pyproject_and_uv_lock():
    # Release step 1 (release.yml): CI runs with UV_LOCKED=1, so a bump without `uv lock` stops it.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == project["name"]]
    assert locked == [project["version"]] and project["version"] == __version__


def _script(rel: str):
    spec = importlib.util.spec_from_file_location(Path(rel).stem, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_notes_are_one_version_of_the_changelog():
    # release.yml stops the release when this returns "" and uses it as the release's notes.
    rn = _script("scripts/release_notes.py")
    log = "## [Unreleased]\n\n## [0.2.0] - 2026-11-02\n### Fixed\n- B\n\n## [0.1.0] - x\n- A\n"
    assert rn.notes(log, "0.2.0") == "### Fixed\n- B"
    assert rn.notes(log, "Unreleased") == "" and rn.notes(log, "0.2") == ""
    assert rn.notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), __version__)


def test_spec_bundles_the_web_page_and_no_console():
    spec = (ROOT / "packaging" / "latentroom.spec").read_text(encoding="utf-8")
    assert '"latentroom/web/static"' in spec and "console=False" in spec


def test_third_party_notices_hold_the_licence_of_every_bundled_part(tmp_path):
    # The spec writes them next to Latentroom.exe as THIRD-PARTY-NOTICES.txt; build.ps1 stops
    # when that file is missing or empty.
    n = _script("packaging/notices.py")
    tops = ["PIL/_imaging.pyd", "webview\\lib\\x.dll", "latentroom/web/i.html", "os.pyc"]
    assert n.bundled(tops) == {"pillow", "pywebview"}  # not latentroom: LICENSE, next to it
    text = n.notices({"pillow", "proxy_tools"})
    for part in ("Python Software Foundation", "OpenSSL", "libbzip2", "Zstandard", "SQLite"):
        assert part in text  # Python and the libraries inside it
    assert "The Python Imaging Library (PIL) is" in text  # pillow's own dist-info
    assert "Armin Ronacher" in text  # proxy_tools: from packaging/licenses/, the wheel has none
    assert "Microsoft.Web.WebView2" in text  # inside pywebview, not in its licence
    assert "made by madebyollin in 2026" in text  # the Texture-Fix decoder's NOTICE
    # A part with no licence text, or a Python version with none, stops the build.
    v = sys.version_info
    (tmp_path / f"python-{v.major}.{v.minor}").mkdir()
    with pytest.raises(SystemExit, match="proxy_tools"):
        n.notices({"proxy_tools"}, extra=tmp_path)
    (tmp_path / f"python-{v.major}.{v.minor}").rmdir()
    with pytest.raises(SystemExit, match=f"python-{v.major}.{v.minor}"):
        n.notices(set(), extra=tmp_path)


def test_build_script_checks_the_installers_own_app_id():
    # build.ps1 refuses to run its install test over a real install by looking up this AppId.
    iss = (ROOT / "packaging" / "latentroom.iss").read_text(encoding="utf-8")
    app_id = re.search(r"^AppId=\{(\{[0-9A-F-]{36}\})$", iss, re.M).group(1)
    assert f"'{app_id}_is1'" in (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
    # Never changes once released: an update with a new one would install a second Latentroom
    assert app_id == "{C0349225-2BE4-45F1-BA50-194AA43E02C6}"


def test_uninstaller_reads_the_list_the_app_writes():
    # the uninstaller deletes only what the app listed in this file, by this name.
    from latentroom.engine import paths

    iss = (ROOT / "packaging" / "latentroom.iss").read_text(encoding="utf-8")
    assert f"OwnFiles = '{paths.OWN_FILES}';" in iss
    assert "DelTree(AddBackslash(DataDir) + 'models'" not in iss  # never the whole shared folder


def test_uninstaller_names_the_pictures_folder_the_app_uses():
    # paths.pictures_dir() asks Windows for the known Pictures folder (OneDrive may have moved
    # it); the uninstaller's last message must name that one, not %USERPROFILE%\Pictures.
    iss = (ROOT / "packaging" / "latentroom.iss").read_text(encoding="utf-8")
    assert "CSIDL_MYPICTURES = $0027;" in iss  # the CSIDL of FOLDERID_Pictures
    assert "GetShellFolderByCSIDL(CSIDL_MYPICTURES, False)" in iss
    assert "OutDir := PicturesDir + '\\Latentroom';" in iss
