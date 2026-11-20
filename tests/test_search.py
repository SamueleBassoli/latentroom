import os
import subprocess
import sys
import time
from array import array
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from latentroom.engine import catalogue, history, install, paths, search
from latentroom.engine.catalogue import FileItem
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError

FAKE = Path(__file__).parent / "fake_llama_server.py"


def _item(id_: str, kind: str) -> FileItem:
    name = f"{id_}.gguf"
    return FileItem(id_, name, f"https://example.invalid/{name}", 1, "0" * 64, "test", kind)


@pytest.fixture
def smart(monkeypatch) -> set[str]:
    """Smart search 'downloaded' (the returned set of file ids); the fake server runs it."""
    files = {f.id: f for f in (_item("emb", "embedding"), _item("vis", "embedding_vision"))}
    files[install.LLAMA_ZIP.id] = install.LLAMA_ZIP
    have = set(files)
    monkeypatch.setattr(catalogue, "files", lambda: files)
    monkeypatch.setattr(install, "installed", lambda: have)
    monkeypatch.setenv("LATENTROOM_LLAMA", f'"{sys.executable}" "{FAKE}"')
    search._skipped.clear()
    yield have
    search.stop()
    finish_indexing()


def picture(tmp_path: Path, name: str, colour) -> history.Entry:
    p = tmp_path / f"{name}.png"
    Image.new("RGB", (32, 32), colour).save(p)
    return history.add(JobRequest(prompt="a lighthouse at dusk"), p, 1)


def finish_indexing() -> None:
    # loop: another caller (a Library poll) may start a fresh, short indexer while we wait
    deadline = time.monotonic() + 30
    while (t := search._indexer) and t.is_alive():
        assert time.monotonic() < deadline, "indexing did not finish"
        t.join(1)


def ids(entries) -> list[int]:
    return [e.id for e in entries]


def test_search_finds_pictures_by_meaning(smart, tmp_path):
    red, green, blue = (picture(tmp_path, c, c) for c in ("red", "green", "blue"))
    assert search.available() and search.search("  ") == []
    assert search.status() == {"indexed": 0, "total": 3, "indexing": False}
    search.start_indexing()
    finish_indexing()
    assert search.status() == {"indexed": 3, "total": 3, "indexing": False}
    assert ids(search.search("a RED fox"))[0] == red.id
    found = search.search("green grass", k=2)
    assert len(found) == 2 and found[0] == history.get(green.id)
    sea = picture(tmp_path, "sea", (0, 0, 200))
    search.search("blue")  # a search also indexes pictures made since
    finish_indexing()
    assert search.status()["indexed"] == 4 and set(ids(search.search("blue", 2))) == {
        blue.id,
        sea.id,
    }
    history.delete(red.id)  # the trigger drops its vector
    finish_indexing()  # each search() above starts a (short) indexer too
    assert search.status() == {"indexed": 3, "total": 3, "indexing": False}
    assert red.id not in ids(search.search("red"))


def test_hits_come_best_first_from_one_connection(tmp_path, monkeypatch):
    """Every hit is read on one db connection, best score first; a vector whose
    picture was deleted between scoring and reading is left out."""
    monkeypatch.setattr(search, "_tag", lambda: "t")
    monkeypatch.setattr(search, "start_indexing", lambda: None)
    monkeypatch.setattr(search._llama, "embed", lambda texts: [[1.0, 0.0]])
    pics = [picture(tmp_path, f"p{i}", "red") for i in range(4)]
    scores = [(p.id, s) for p, s in zip(pics, (0.2, 0.9, 0.5, 0.7), strict=True)]
    with closing(history._db()) as con, con:  # 999: no entry, the best score
        con.executemany(
            "INSERT INTO embeddings VALUES (?, 't', ?)",
            [(i, array("f", [s, 0.0]).tobytes()) for i, s in [*scores, (999, 1.0)]],
        )
    opened, db = [], history._db
    monkeypatch.setattr(history, "_db", lambda: opened.append(1) or db())
    assert ids(search.search("x", k=5)) == [pics[1].id, pics[3].id, pics[2].id, pics[0].id]
    assert len(opened) == 1


def test_a_starred_picture_is_found_however_bad_its_match(monkeypatch):
    """Repro: 600 indexed pictures, the one starred is the worst match. Starred +
    search used to look only inside the best 500 and found nothing."""
    monkeypatch.setattr(search, "_tag", lambda: "t")
    monkeypatch.setattr(search, "start_indexing", lambda: None)
    monkeypatch.setattr(search._llama, "embed", lambda texts: [[1.0, 0.0]])
    n = 600
    with closing(history._db()) as con, con:
        con.executemany(
            "INSERT INTO entries VALUES (?, 1, 'txt2img', 'p', '{}', 1, 'std-q4', ?, 64, 64, ?)",
            [(i, f"{i}.png", i == n) for i in range(1, n + 1)],
        )
        con.executemany(  # score 1 - i/600: entry 600 is the worst match
            "INSERT INTO embeddings VALUES (?, 't', ?)",
            [(i, array("f", [1 - i / n, 0.0]).tobytes()) for i in range(1, n + 1)],
        )
    assert n not in ids(search.search("x", k=500))  # past the best 500
    assert ids(search.search("x", k=60, starred=True)) == [n]
    unstarred = ids(search.search("x", k=500, starred=False))
    assert len(unstarred) == 500 and n not in unstarred and unstarred[0] == 1


def test_server_is_local_low_priority_and_needs_its_key(smart, monkeypatch):
    flags, popen = [], subprocess.Popen

    def spy(*args, **kwargs):  # the flag, not the process: a child inherits a low parent
        flags.append(kwargs["creationflags"])
        return popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    assert search.search("red") == [] and search._llama.running()
    if os.name == "nt":
        assert flags[0] & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    args = search._llama._proc.args
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--image-max-tokens") + 1] == "140"
    assert {"--embeddings", "--no-webui", "--offline"} <= set(args)
    assert search._llama._key not in " ".join(args)  # passed by env, not the command line
    port = int(args[args.index("--port") + 1])
    assert search._request(port, "POST", "/v1/embeddings", "wrong", b"{}")[0] == 401


def test_an_idle_server_stops_and_starts_again(smart, monkeypatch):
    monkeypatch.setattr(search, "IDLE_S", 0.3)
    started, real = [], search.procs.start  # not _llama._proc: on a loaded PC the idle stop
    # may already have run when search() returns, and _proc is None by then

    def start(*a, **kw):
        started.append(real(*a, **kw))
        return started[-1]

    monkeypatch.setattr(search.procs, "start", start)
    search.search("red")
    first = started[0]
    deadline = time.monotonic() + 10
    while search._llama.running():
        assert time.monotonic() < deadline, "not stopped after IDLE_S"
        time.sleep(0.05)
    assert first.poll() is not None
    search.search("red")
    assert len(started) == 2 and started[1].pid != first.pid


def test_without_the_download(smart, tmp_path):
    smart.discard("vis")
    picture(tmp_path, "red", "red")
    assert not search.available()
    search.start_indexing()
    assert search.status() == {"indexed": 0, "total": 1, "indexing": False}
    with pytest.raises(LatentroomError) as e:
        search.search("red")
    assert (e.value.code, e.value.status) == ("model_missing", 404)
    assert e.value.message == install.SEARCH_MISSING and not search._llama.running()


def test_a_server_that_cannot_start(smart, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_FAIL", "start")
    with pytest.raises(LatentroomError) as e:
        search.search("red")
    assert (e.value.code, e.value.status, e.value.message) == ("not_available", 409, search.FAILED)
    assert not search._llama.running()
    assert "failed to load model" in (paths.logs_dir() / search.LOG).read_text()
    for c in "red", "green", "blue":
        picture(tmp_path, c, c)
    search.start_indexing()
    finish_indexing()  # gave up at once: no restart per picture
    assert search.status()["indexed"] == 0 and not search._skipped


def test_a_server_blocked_by_windows(smart, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_FAIL", "blocked")  # exits 0xC0000135: a DLL quarantined
    with pytest.raises(LatentroomError) as e:
        search.search("red")
    assert (e.value.code, e.value.message) == ("engine_blocked", search.procs.BLOCKED)
    assert not search._llama.running()


def test_unreadable_or_refused_pictures_are_skipped(smart, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_FAIL", "black")  # the fake answers HTTP 500 for black
    gone = picture(tmp_path, "gone", "red")
    Path(gone.image).unlink()  # deleted outside Latentroom
    black = picture(tmp_path, "black", "black")
    ok = picture(tmp_path, "ok", "green")
    search.start_indexing()
    finish_indexing()
    assert search.status() == {"indexed": 1, "total": 3, "indexing": False}
    assert search._skipped == {gone.id, black.id}
    assert ids(search.search("green")) == [ok.id]


def test_stop_ends_indexing_and_the_server(smart, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_EMBED_S", "0.2")
    for i in range(10):
        picture(tmp_path, f"p{i}", "red")
    search.start_indexing()
    deadline = time.monotonic() + 20
    while search.status()["indexed"] < 1:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    search.stop()
    finish_indexing()
    assert not search._llama.running() and search.status()["indexed"] < 10


def test_latentroom_exit_stops_the_server_for_good(smart, monkeypatch):
    search.search("red")
    proc = search._llama._proc
    monkeypatch.setattr(search._llama, "closed", False)  # restored after the test
    search._exit()
    assert proc.poll() is not None
    with pytest.raises(search._Stopped):
        search.search("red")
    assert not search._llama.running()
