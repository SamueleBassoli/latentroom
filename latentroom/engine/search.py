"""Smart search: find history pictures by meaning, on the CPU.

EmbeddingGemma 2 (catalogue kinds "embedding" + "embedding_vision") runs in llama-server, the
shared llama.cpp build (install.install_llama()) with --device none: it never touches the GPU.
The server starts on demand on 127.0.0.1 with a random port and API key, at below-normal
priority, and stops after IDLE_S without a request, on stop(), and when Latentroom exits.
One vector per history picture lives in the history db (table embeddings, history migration
v2); a delete trigger drops it with its entry. Search = dot product over every vector
(the server returns unit vectors), stdlib only: 10k pictures ~0.24 s.

Server command: install.install_llama(), unless env LATENTROOM_LLAMA is set (tests/QA only):
then LATENTROOM_LLAMA is the command, split like runner's LATENTROOM_ENGINE, e.g.
  LATENTROOM_LLAMA="C:/path/python.exe C:/path/latentroom/tests/fake_llama_server.py"
"""

import atexit
import base64
import heapq
import http.client
import json
import logging
import math
import os
import secrets
import shlex
import socket
import subprocess
import threading
import time
from array import array
from contextlib import closing
from pathlib import Path

from latentroom.engine import catalogue, history, install, paths, procs
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)

IDLE_S = 300.0  # stop llama-server (~1 GB of RAM) after 5 minutes without a request
START_S = 120.0  # it is ready in 1.5 s here; a cold or slow disk takes longer
CALL_S = 120.0  # one request: a picture takes ~2 s, a search ~30 ms
IMAGE_TOKENS = 140  # 2 s per picture, same top-1 as 280; changes every vector
QUERY = "task: search result | query: {}"  # HF model card prompt; pictures get no prefix
FAILED = (
    "Smart search stopped with an error. Try again; if it keeps failing, "
    "send the search log from Latentroom's logs folder."
)
LOG = "search.log"  # in paths.logs_dir(): warnings and errors only, no search text
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOW = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)  # pictures keep their speed


class _Stopped(Exception):
    """stop() ran while the indexer waited: it gives up quietly."""


class _Refused(LatentroomError):
    """The server runs but answered this input with an error: skip it, do not restart."""


def _files() -> list[catalogue.FileItem]:
    """The text model, its picture reader and the llama.cpp zip: Smart search needs all 3."""
    by_kind = {f.kind: f for f in catalogue.files().values()}
    return [by_kind["embedding"], by_kind["embedding_vision"], install.LLAMA_ZIP]


def _tag() -> str:
    """Names what made a vector; vectors with another tag are made again."""
    model, vision, _ = _files()
    return f"{model.id}+{vision.id}@{IMAGE_TOKENS}"


def available() -> bool:
    """True when the 3 Smart search files are downloaded: search() can run."""
    try:
        files = _files()
    except KeyError:  # a catalogue without Smart search (test stubs): never a crash
        return False
    return {f.id for f in files} <= install.installed()


def _command() -> list[str]:
    env = os.environ.get("LATENTROOM_LLAMA", "").strip()
    if env:  # posix=False keeps the quotes around "C:/Program Files/..."; drop them
        tokens = shlex.split(env, posix=False)
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in tokens]
    return [str(install.install_llama())]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(
    port: int, method: str, path: str, key: str = "", body: bytes = b"", timeout: float = CALL_S
) -> tuple[int, bytes]:
    """(status, body). http.client, not urllib: a system proxy never sees localhost."""
    con = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        con.request(method, path, body or None, headers)
        r = con.getresponse()
        return r.status, r.read()
    finally:
        con.close()


class _Llama:
    """At most one llama-server, started on demand. Every field is guarded by _lock."""

    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._ready = False
        self._port, self._key = 0, ""
        self._deadline = self._used = 0.0
        self._busy = 0  # requests in flight: never stopped for idleness under them
        self.gen = 0  # +1 on every stop(): an indexer started before gives up
        self.closed = False  # Latentroom is exiting: never start again

    def running(self) -> bool:
        with self._lock:
            return self._proc is not None

    def embed(self, inputs: list, gen: int | None = None) -> list[array]:
        """One unit vector per input. Raises LatentroomError (model_missing, not_available,
        engine_blocked)."""
        with self._lock:
            self._wait_ready(gen)
            port, key = self._port, self._key
            self._busy += 1
        try:
            body = json.dumps({"input": inputs, "encoding_format": "float"}).encode()
            status, raw = _request(port, "POST", "/v1/embeddings", key, body)
            if status != 200:  # the reply names the problem, never the input
                log.warning("Smart search refused an input: HTTP %s %r", status, raw[:300])
                raise _Refused("not_available", FAILED, 409)
            data = sorted(json.loads(raw)["data"], key=lambda d: d["index"])
            return [array("f", d["embedding"]) for d in data]
        except (OSError, ValueError, KeyError, TypeError) as e:
            with self._lock:
                if gen is not None and gen != self.gen:
                    raise _Stopped from e  # stop() killed the server under this request
            log.warning("Smart search request failed: %s", e)  # never the input itself
            raise LatentroomError("not_available", FAILED, 409) from e
        finally:
            with self._lock:
                self._busy -= 1
                self._used = time.monotonic()

    def _wait_ready(self, gen: int | None) -> None:
        """Under _lock: return once a ready server runs; start one if none does."""
        while True:
            if self.closed or (gen is not None and gen != self.gen):
                raise _Stopped
            if self._proc is None:
                self._spawn()
            if self._ready:
                if self._proc.poll() is None:
                    return
                ended = procs.note_exit(self._proc.returncode, paths.logs_dir() / LOG)
                log.warning("llama-server exited by itself (%s)", ended)
                self._kill()
                continue
            if _healthy(self._port):
                self._ready, self._used = True, time.monotonic()
                threading.Thread(
                    target=self._idle_stop,
                    args=(self._proc,),
                    name="latentroom-search",
                    daemon=True,
                ).start()
                continue
            if self._proc.poll() is not None or time.monotonic() > self._deadline:
                code = self._proc.poll()
                ended = procs.note_exit(code, paths.logs_dir() / LOG)
                log.error("llama-server did not start (%s), see %s", ended, LOG)
                self._kill()
                if procs.blocked(code):
                    raise LatentroomError("engine_blocked", procs.BLOCKED, 409)
                raise LatentroomError("not_available", FAILED, 409)
            self._lock.wait(0.1)  # lets stop() in; a 2nd caller waits for this same server

    def _spawn(self) -> None:
        if not available():
            raise LatentroomError("model_missing", install.SEARCH_MISSING, 404)
        model, vision, _ = (paths.models_dir() / f.name for f in _files())
        port, key = _free_port(), secrets.token_urlsafe(24)
        argv = [
            *_command(), "-m", str(model), "--mmproj", str(vision), "--embeddings",
            "--image-max-tokens", str(IMAGE_TOKENS), "-c", "4096", "-b", "4096", "-ub", "4096",
            "-t", str(max(1, (os.cpu_count() or 2) // 2)), "-np", "1",
            "--device", "none",  # the Vulkan build: CPU only, the GPU stays for pictures
            "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--offline", "-lv", "2",
        ]  # fmt: skip
        with open(paths.logs_dir() / LOG, "wb") as logf:
            self._proc = procs.start(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=os.environ | {"LLAMA_API_KEY": key},  # a command line is readable by all
                creationflags=_NO_WINDOW | _LOW,
                start_new_session=os.name != "nt",
            )
        self._ready, self._port, self._key = False, port, key
        self._deadline = time.monotonic() + START_S
        log.info("llama-server started (pid %s)", self._proc.pid)

    def _idle_stop(self, proc: subprocess.Popen) -> None:
        with self._lock:
            while self._proc is proc:
                left = self._used + IDLE_S - time.monotonic()
                if left <= 0 and not self._busy:
                    log.info("Smart search idle: llama-server stopped")
                    self._kill()
                    return
                self._lock.wait(max(left, 0.05))

    def stop(self) -> None:
        with self._lock:
            self.gen += 1
            self._kill()

    def _kill(self) -> None:
        """Under _lock. Waits for the exit: Windows keeps the exe and DLLs locked until then."""
        proc, self._proc, self._ready = self._proc, None, False
        self._lock.notify_all()
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                log.warning("llama-server (pid %s) did not exit", proc.pid)


def _healthy(port: int) -> bool:
    """200 once the model is loaded (503 before). 1 s: on Windows a connect to a closed
    localhost port only fails after ~2 s."""
    try:
        return _request(port, "GET", "/health", timeout=1)[0] == 200
    except OSError:
        return False


_llama = _Llama()
_index_lock = threading.Lock()
_indexer: threading.Thread | None = None
_skipped: set[int] = set()  # pictures that could not be read or embedded, this session


def _picture(path: str) -> dict:
    """A history PNG as a /v1/embeddings multimodal input (llama.cpp PR #30054)."""
    data = base64.b64encode(Path(path).read_bytes()).decode()
    url = f"data:image/png;base64,{data}"  # history pictures are always PNG
    return {"content": [{"type": "image_url", "image_url": {"url": url}}]}


def _missing(tag: str) -> list[tuple[int, str]]:
    """(entry id, image path) of pictures with no vector for `tag`, newest first."""
    with closing(history._db()) as con:
        return con.execute(
            """SELECT e.id, e.image FROM entries e LEFT JOIN embeddings v
            ON v.entry_id = e.id AND v.model = ? WHERE v.entry_id IS NULL ORDER BY e.id DESC""",
            (tag,),
        ).fetchall()


def _index(gen: int) -> None:
    tag = _tag()
    try:
        while todo := [(i, p) for i, p in _missing(tag) if i not in _skipped]:
            for entry_id, image in todo:
                try:
                    vec = _llama.embed([_picture(image)], gen)[0]
                except OSError, _Refused:  # moved/deleted on disk, or refused by the model
                    log.warning("Smart search skipped history entry %s", entry_id)
                    _skipped.add(entry_id)
                    continue
                with closing(history._db()) as con, con:  # deleted meanwhile: no orphan row
                    con.execute(
                        """INSERT OR REPLACE INTO embeddings (entry_id, model, vec)
                        SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM entries WHERE id = ?)""",
                        (entry_id, tag, vec.tobytes(), entry_id),
                    )
    except _Stopped:
        return
    except Exception:
        log.exception("Smart search indexing stopped")


def start_indexing() -> None:
    """Make every history picture searchable, in one background thread (~2 s a picture).
    Does nothing when Smart search is not downloaded or the thread already runs."""
    global _indexer
    with _index_lock:
        if (_indexer and _indexer.is_alive()) or not available():
            return
        _indexer = threading.Thread(
            target=_index, args=(_llama.gen,), name="latentroom-search-index", daemon=True
        )
        _indexer.start()


def status() -> dict:
    """{"indexed": n, "total": m, "indexing": bool}: n of the m history pictures are
    searchable now; indexing is True while the thread works ("Indexing 120 of 800")."""
    with closing(history._db()) as con:
        total = con.execute("SELECT count(*) FROM entries").fetchone()[0]
        done = con.execute("SELECT count(*) FROM embeddings WHERE model = ?", (_tag(),))
        done = done.fetchone()[0]
    with _index_lock:
        indexing = bool(_indexer and _indexer.is_alive())
    return {"indexed": done, "total": total, "indexing": indexing}


def search(text: str, k: int = 60, starred: bool | None = None) -> list[history.Entry]:
    """History pictures closest in meaning to `text`, best first, among the indexed ones;
    only the starred (True) or unstarred (False) ones when `starred` is set, so every starred
    picture can be found, not just those among the best 500.
    Also starts indexing new pictures. Raises LatentroomError (model_missing, not_available)."""
    text = text.strip()
    if not text:
        return []
    query = _llama.embed([QUERY.format(text)])[0]
    start_indexing()
    with closing(history._db()) as con:  # one connection for all hits (k=500, 1k: 396 -> 54 ms)
        rows = con.execute(  # the join costs the plain search 1-3 ms of ~290 (9k pictures)
            """SELECT v.entry_id, v.vec FROM embeddings v JOIN entries e ON e.id = v.entry_id
            WHERE v.model = ? AND (? IS NULL OR e.starred = ?)""",
            (_tag(), starred, starred),
        ).fetchall()
        k = max(1, min(int(k), 500))
        best = heapq.nlargest(k, rows, key=lambda r: math.sumprod(query, array("f", r[1])))
        found = []
        for entry_id, _ in best:
            try:
                found.append(history.get(entry_id, con))
            except LatentroomError:  # deleted in the meantime
                pass
    return found


def stop() -> None:
    """Stop llama-server and the indexer now (Latentroom exit; before deleting Smart search).
    The next search() starts them again."""
    _llama.stop()


@atexit.register
def _exit() -> None:
    _llama.closed = True
    _llama.stop()
