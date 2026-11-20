import hashlib
import json
import threading
import time
import types
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from latentroom.engine import awake, downloads, paths
from latentroom.engine.catalogue import FileItem
from latentroom.errors import LatentroomError

BLOB = bytes(range(256)) * 256  # 64 KiB of known bytes


class Server:
    """A local file host with Range support and knobs to misbehave."""

    def __init__(self):
        self.blob = BLOB
        self.ranges = []  # Range header of every request
        self.drop_after = None  # send only this many bytes once, then hang up
        self.ignore_range = False
        self.status = 200
        self.delay = 0.0  # seconds per 1 KiB

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.ranges.append(self.headers.get("Range"))
                if outer.status != 200:
                    self.send_error(outer.status)
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and not outer.ignore_range:
                    start = int(rng.removeprefix("bytes=").split("-")[0])
                if start >= len(outer.blob):
                    self.send_error(416)
                    return
                body = outer.blob[start:]
                self.send_response(206 if start or (rng and not outer.ignore_range) else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if outer.drop_after is not None:
                    body, outer.drop_after = body[: outer.drop_after], None
                for i in range(0, len(body), 1024):
                    self.wfile.write(body[i : i + 1024])
                    if outer.delay:
                        time.sleep(outer.delay)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda *a: None  # the client hanging up mid-file is the test
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/file.gguf"

    def item(self, kind="diffusion", sha256=None, size=None):
        return FileItem(
            id="test-model",
            name="test-model.gguf",
            url=self.url,
            size=len(BLOB) if size is None else size,
            sha256=sha256 or hashlib.sha256(BLOB).hexdigest(),
            licence="test",
            kind=kind,
        )


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(downloads, "CHUNK", 1024)
    monkeypatch.setattr(downloads, "RETRY_DELAYS", (0, 0, 0))
    downloads._reg.update(path=None, jobs={})  # forget the previous test's data folder
    s = Server()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def wait(status, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = {s.id: s for s in downloads.progress()}.get("test-model")
        if st and st.status in status:
            return st
        time.sleep(0.01)
    raise AssertionError(f"still {st}")


def test_download_verifies_and_lands_in_models(server):
    downloads.start(server.item())
    st = wait({"done", "failed"})
    assert st.status == "done" and st.done == st.total == len(BLOB) and st.error is None
    final = paths.models_dir() / "test-model.gguf"
    assert final.read_bytes() == BLOB and not final.with_name("test-model.gguf.part").exists()
    saved = json.loads(paths.downloads_state_path().read_text(encoding="utf-8"))
    assert saved["downloads"][0]["state"]["status"] == "done"
    assert downloads.start(server.item()).status == "done"  # second start: no new download
    assert len(server.ranges) == 1


def test_engine_zips_go_to_the_engine_folder(server):
    downloads.start(server.item(kind="engine"))
    assert wait({"done", "failed"}).status == "done"
    assert (paths.engine_dir() / "test-model.gguf").exists()


def test_interrupted_transfer_resumes_with_range(server):
    server.drop_after = 20000
    downloads.start(server.item())
    assert wait({"done", "failed"}).status == "done"
    assert server.ranges[0] == "bytes=0-" and server.ranges[1] == "bytes=20000-"
    assert (paths.models_dir() / "test-model.gguf").read_bytes() == BLOB


def test_server_ignoring_range_starts_over(server):
    server.drop_after, server.ignore_range = 20000, True
    downloads.start(server.item())
    assert wait({"done", "failed"}).status == "done"
    assert (paths.models_dir() / "test-model.gguf").read_bytes() == BLOB


def test_corrupted_file_fails_and_is_deleted(server):
    downloads.start(server.item(sha256="0" * 64))
    st = wait({"done", "failed"})
    assert st.status == "failed" and st.error == downloads.DAMAGED
    assert list(paths.models_dir().iterdir()) == []


def test_wrong_size_fails(server):
    downloads.start(server.item(size=len(BLOB) - 10))
    assert wait({"done", "failed"}).error == downloads.DAMAGED


def test_dead_link_fails_without_retrying(server):
    server.status = 404
    st = downloads.start(server.item())
    st = wait({"failed"})
    assert "no longer works" in st.error and len(server.ranges) == 1


def test_connection_gone_gives_up_with_a_plain_message(server, monkeypatch):
    def offline(*a, **k):
        raise ConnectionResetError("network down")

    monkeypatch.setattr(downloads.urllib.request, "urlopen", offline)
    downloads.start(server.item())
    st = wait({"failed"})
    assert st.error == downloads.GAVE_UP


def test_disk_space_is_checked_before_starting(server, monkeypatch):
    monkeypatch.setattr(downloads.hardware, "free_disk", lambda p: 1000)
    st = downloads.start(server.item())
    assert st.status == "failed" and st.error.startswith("Not enough disk space: need 0.2 GB")
    assert server.ranges == []


def test_disk_check_counts_downloads_already_queued(server, monkeypatch):
    """5 GB free and two 3 GB files: the second one is refused before it starts."""
    busy = types.SimpleNamespace(is_alive=lambda: True)  # no worker runs: both stay queued
    monkeypatch.setitem(downloads._run_state, "worker", busy)
    monkeypatch.setattr(downloads.hardware, "free_disk", lambda p: 5 << 30)
    a = replace(server.item(size=3 << 30), id="a", name="a.gguf")
    assert downloads.start(a).status == "queued"
    st = downloads.start(replace(a, id="b", name="b.gguf"))
    assert st.status == "failed"
    assert st.error == "Not enough disk space: need 6.2 GB for this and the downloads before it."
    assert [s.status for s in downloads.progress()] == ["queued", "failed"]


def test_pause_survives_a_restart_then_resumes(server):
    server.delay = 0.005
    downloads.start(server.item())
    while wait({"downloading"}).done < 8000:
        time.sleep(0.01)
    downloads.pause("test-model")
    st = wait({"paused"})
    time.sleep(0.2)  # let the worker let go of the file
    part = paths.models_dir() / "test-model.gguf.part"
    assert 0 < part.stat().st_size < len(BLOB)

    downloads._reg.update(path=None, jobs={})  # Latentroom restarts: state comes from disk
    st = {s.id: s for s in downloads.progress()}["test-model"]
    assert st.status == "paused" and st.done == part.stat().st_size

    server.delay = 0
    downloads.resume("test-model")
    assert wait({"done", "failed"}).status == "done"
    assert server.ranges[-1] == f"bytes={st.done}-"
    assert (paths.models_dir() / "test-model.gguf").read_bytes() == BLOB


def test_a_crash_mid_download_comes_back_paused(server):
    folder = paths.models_dir()
    (folder / "test-model.gguf.part").write_bytes(BLOB[:5000])
    state = downloads.DownloadState(
        "test-model", "test-model.gguf", "downloading", 3000, 1, 9, 9, None
    )
    entry = {"item": vars(server.item()), "folder": str(folder), "state": vars(state)}
    paths.downloads_state_path().write_text(json.dumps({"downloads": [entry]}), encoding="utf-8")
    st = downloads.progress()[0]
    assert (st.status, st.done, st.speed, st.eta) == ("paused", 5000, 0.0, None)


def test_cancel_deletes_the_part_file(server):
    server.delay = 0.005
    downloads.start(server.item())
    while wait({"downloading"}).done < 4000:
        time.sleep(0.01)
    assert downloads.cancel("test-model").status == "cancelled"
    deadline = time.monotonic() + 5
    part = paths.models_dir() / "test-model.gguf.part"
    while part.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not part.exists()
    assert wait({"cancelled"}).done == 0


def test_unknown_id_is_not_found(server):
    for action in (downloads.pause, downloads.resume, downloads.cancel):
        with pytest.raises(LatentroomError) as e:
            action("nope")
        assert (e.value.code, e.value.status) == ("not_found", 404)


def test_unreadable_state_file_means_no_downloads(server):
    paths.downloads_state_path().write_text("{not json", encoding="utf-8")
    assert downloads.progress() == []


def test_the_pc_stays_awake_while_a_file_downloads_and_not_after(server, monkeypatch):
    asked = []
    monkeypatch.setattr(awake, "_set", asked.append)  # the Windows call itself left out
    server.status = 404  # a failed download lets go too
    downloads.start(server.item())
    wait({"failed"})
    end = time.monotonic() + 10
    while len(asked) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert asked == [awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED, awake.ES_CONTINUOUS]
