"""app.py: `--server-only` as a real process driven over urllib, plus the window-side logic
(WebView2 check, single instance, folder picker) with fakes. No window is opened here."""

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest

from latentroom import __version__, app

ROOT = Path(__file__).parent.parent


def _read_line(proc, timeout=30) -> str:
    out = []
    t = threading.Thread(target=lambda: out.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    assert out, "no address printed"
    return out[0].strip()


def test_server_only_process(latentroom_home):
    env = {**os.environ, "LATENTROOM_HOME": str(latentroom_home), "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-m", "latentroom", "--server-only"]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    try:
        url = _read_line(proc)
        assert url.startswith("http://127.0.0.1:") and "/?token=" in url
        base = url.split("?")[0]
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "api/version", timeout=10)
        assert e.value.code == 403

        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        with opener.open(url, timeout=10) as r:  # 303 -> / with the cookie
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
        with opener.open(base + "api/version", timeout=10) as r:
            assert json.load(r)["version"] == __version__
        req = urllib.request.Request(
            base + "api/settings",
            data=json.dumps({"adult_ok": True}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=10) as r:
            assert json.load(r)["adult_ok"] is True
        pid = int((latentroom_home / "instance.pid").read_text())  # the real interpreter's

        second = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert second.returncode == 1 and app.ALREADY in second.stdout
    finally:
        proc.terminate()
        proc.wait(10)
        proc.stdout.close()
    assert _gone(pid), "killing the launcher left Latentroom running"


def _gone(pid: int) -> bool:
    import ctypes

    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not h:
        return True
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 10_000) == 0
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        app.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"Latentroom {__version__}"


def test_webview2_check(monkeypatch):
    import winreg

    values = {}

    class Key:
        def __init__(self, root, sub):
            if sub not in values:
                raise FileNotFoundError(sub)
            self.sub = sub

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(winreg, "OpenKey", Key)
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (values[key.sub], 1))
    assert app.webview2_installed() is False  # no key at all
    machine = "SOFTWARE\\WOW6432Node\\" + app._RUNTIME
    for pv, ok in [("0.0.0.0", False), ("", False), ("130.0.2849.80", True)]:
        values[machine] = pv
        assert app.webview2_installed() is ok, pv
    values.clear()
    values["Software\\" + app._RUNTIME] = "130.0.2849.80"  # per-user install
    assert app.webview2_installed() is True


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(app, "_log_to_file", lambda: None)  # keep pytest's own logging


def test_no_webview2_shows_message_and_starts_nothing(monkeypatch, quiet, latentroom_home):
    shown = []
    monkeypatch.setattr(app, "webview2_installed", lambda: False)
    monkeypatch.setattr(app, "message_box", shown.append)
    monkeypatch.setattr(app, "claim_instance", lambda: pytest.fail("must not start"))
    assert app.main([]) == 1
    assert len(shown) == 1 and app.WEBVIEW2_URL in shown[0]


def test_second_launch_focuses_the_first(monkeypatch, quiet):
    focused = []
    monkeypatch.setattr(app, "webview2_installed", lambda: True)
    monkeypatch.setattr(app, "claim_instance", lambda: False)
    monkeypatch.setattr(app, "focus_first", lambda: focused.append(1) or True)
    monkeypatch.setattr(app, "_window", lambda srv: pytest.fail("must not open a window"))
    assert app.main([]) == 0 and focused == [1]


def test_window_closed_stops_server(monkeypatch, quiet, latentroom_home):
    seen = []
    monkeypatch.setattr(app, "webview2_installed", lambda: True)
    monkeypatch.setattr(app, "claim_instance", lambda: True)

    def window(srv):
        seen.append(srv)
        assert (latentroom_home / "instance.pid").is_file()

    monkeypatch.setattr(app, "_window", window)
    assert app.main([]) == 0
    assert seen[0]._stopped and not (latentroom_home / "instance.pid").exists()


def test_start_sweeps_old_previews(monkeypatch, quiet, latentroom_home):
    """A preview of a deleted picture is gone after the next start; the sweep is
    housekeeping, so a failure only goes to the log. An uploaded picture no history
    entry uses is gone too, a used one stays. The uninstaller's list is written."""
    from PIL import Image

    from latentroom.engine import history, paths, thumbs
    from latentroom.engine.jobs import JobRequest

    old = paths.thumbs_dir() / "7-1-1.jpg"  # no entry 7 in this history
    old.write_bytes(b"x")
    up = paths.uploads_dir()
    used, unused, other = up / f"{'a' * 32}.png", up / f"{'b' * 32}.jpg", up / "mine.png"
    for p in (used, unused, other):
        Image.new("RGB", (8, 8)).save(p)
    history.add(JobRequest(kind="img2img", init_image=str(used)), used, 1)
    opened = []
    monkeypatch.setattr(app, "webview2_installed", lambda: True)
    monkeypatch.setattr(app, "claim_instance", lambda: True)
    monkeypatch.setattr(app, "_window", lambda srv: opened.append(old.exists() or unused.exists()))
    assert app.main([]) == 0 and opened == [False]
    assert used.exists() and other.exists()  # in use; not a name Latentroom gives an upload
    assert (paths.data_dir() / paths.OWN_FILES).is_file()

    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(thumbs, "sweep", broken)
    assert app.main([]) == 0 and opened == [False, False]


def test_start_sweeps_job_folders_a_crash_left(monkeypatch, quiet):
    """A crash mid-job leaves jobs\\<id>\\ with the resized photos and masks: gone at the
    next start, before any job can run. Only Latentroom's own job-id names are touched."""
    from latentroom.engine import paths

    left = paths.jobs_dir() / "0123456789ab"
    (left / "refs").mkdir(parents=True)
    (left / "refs" / "ref-0.png").write_bytes(b"a photo")
    mine = paths.jobs_dir() / "not-a-job"
    mine.mkdir()
    opened = []
    monkeypatch.setattr(app, "webview2_installed", lambda: True)
    monkeypatch.setattr(app, "claim_instance", lambda: True)
    monkeypatch.setattr(app, "_window", lambda srv: opened.append(left.exists()))
    assert app.main([]) == 0 and opened == [False] and mine.is_dir()


def test_start_queues_again_the_jobs_a_crash_left(monkeypatch, quiet):
    """queue.json is there only after a crash: its jobs run again, and the sweep keeps the
    uploaded picture they use. A normal close deletes the file."""
    import fake_catalogue
    from PIL import Image

    from latentroom.engine import paths
    from latentroom.engine.jobs import JobRequest

    fake_catalogue.install_stub(monkeypatch)
    photo = paths.uploads_dir() / f"{'e' * 32}.png"
    Image.new("RGB", (512, 512), "red").save(photo)
    req = JobRequest(kind="img2img", model_id="std-q4", prompt="x", init_image=str(photo))
    job = {"request": req.to_dict(), "running": True, "retried": False}
    paths.queue_path().write_text(json.dumps([job]), encoding="utf-8")
    seen = []
    monkeypatch.setattr(app, "webview2_installed", lambda: True)
    monkeypatch.setattr(app, "claim_instance", lambda: True)
    monkeypatch.setattr(app, "_window", lambda srv: seen.append(srv.queue().events()[0].type))
    assert app.main([]) == 0 and seen == ["queued"] and photo.exists()
    assert not paths.queue_path().exists()


def test_mutex_is_per_data_folder():
    assert app.claim_instance() is True
    assert app.claim_instance() is False  # same folder, same process: already held


def test_pick_folder(tmp_path):
    calls = []

    class Window:
        def __init__(self, answer):
            self.answer = answer

        def create_file_dialog(self, kind, directory=""):
            calls.append((int(kind), directory))
            return self.answer

    api = app.Api()
    api._window = Window([str(tmp_path)])
    assert api.pick_folder(str(tmp_path)) == str(tmp_path)
    api._window = Window(None)  # cancelled
    assert api.pick_folder("C:/does/not/exist") == ""
    assert calls == [(20, str(tmp_path)), (20, "")]


def test_window_opens_maximized_and_restores_to_1280x800(monkeypatch):
    """Maximized, not fullscreen (that hides the title bar and the close button)."""
    seen = {}
    fake = SimpleNamespace(
        create_window=lambda *a, **kw: seen.update(kw),
        start=lambda **kw: None,
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    app._window(SimpleNamespace(url="http://127.0.0.1:1/"))
    assert seen["maximized"] is True and not seen.get("fullscreen")
    assert (seen["width"], seen["height"], seen["min_size"]) == (1280, 800, (1024, 700))
