"""Resumable, verified downloads (HTTP Range into <name>.part, size + sha256 check, state in
paths.downloads_state_path() so it survives a restart).
Destination: kind "engine" -> paths.engine_dir(), everything else -> paths.models_dir().
The UI polls progress(); no callbacks cross the HTTP boundary.

One worker thread downloads the queue in order, so the first file asked for finishes first.
Only that worker touches a .part file while it runs; pause/cancel just flip the status and
the worker stops at the next chunk (and deletes the .part on cancel)."""

import errno
import hashlib
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from latentroom import REPO
from latentroom.engine import awake, hardware, paths
from latentroom.engine.catalogue import FileItem
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
TIMEOUT = 30  # seconds without data before the connection counts as lost
RETRY_DELAYS = (2, 5, 10, 30, 60, 60, 60)  # seconds; then "failed" and the user presses Retry
DISK_MARGIN = 200 * 1024**2  # keep this much free after the file is complete
SAVE_EVERY = 2.0  # seconds between state-file writes while bytes flow
ACTIVE = ("queued", "downloading", "verifying")

LOST = "Connection lost — Latentroom will retry."
GAVE_UP = "Connection lost. Check your internet connection, then press Retry."
DAMAGED = "The downloaded file was damaged. Press Retry to download it again."
NO_WRITE = "Latentroom cannot write to its data folder. Check the drive, then press Retry."
DEAD_LINK = (
    "The download link no longer works (error {}). "
    f"Please open an issue at {REPO}/issues so it gets fixed."
)


@dataclass
class DownloadState:
    id: str  # FileItem.id
    name: str  # FileItem.name
    # "queued" | "downloading" | "paused" | "verifying" | "done" | "failed" | "cancelled"
    status: str
    done: int  # bytes on disk so far
    total: int  # FileItem.size
    speed: float  # bytes per second, 0 when not downloading
    eta: float | None  # seconds left, None when unknown
    error: str | None  # plain sentence when failed, e.g. "Not enough disk space: need 4.2 GB."


@dataclass
class _Job:
    item: FileItem
    folder: Path  # fixed at start, so a data-folder change cannot split a file
    state: DownloadState

    @property
    def part(self) -> Path:
        return self.folder / (self.item.name + ".part")

    @property
    def final(self) -> Path:
        return self.folder / self.item.name


class _Stop(Exception):
    """Paused or cancelled by the user."""


class _Fatal(Exception):
    """Retrying will not help: the message is shown as is."""


_lock = threading.Condition()  # also wakes a retry wait on pause/cancel
_reg: dict = {"path": None, "jobs": {}}  # the state file in use and its jobs by id
_run_state: dict = {"worker": None, "active": None}


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _jobs() -> dict[str, _Job]:
    """Jobs of the current data folder, loaded once from its state file. Caller holds _lock."""
    path = paths.downloads_state_path()
    if _reg["path"] != path:
        _reg["path"], _reg["jobs"] = path, _read(path)
    return _reg["jobs"]


def _read(path: Path) -> dict[str, _Job]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    jobs = {}
    for entry in raw.get("downloads", []) if isinstance(raw, dict) else []:
        try:
            item, state = FileItem(**entry["item"]), DownloadState(**entry["state"])
            job = _Job(item, Path(entry["folder"]), state)
        except KeyError, TypeError:
            continue  # an entry from another version: forget it, start() re-checks the file
        st = job.state
        if st.status in ACTIVE:  # Latentroom closed mid-way: it waits for the user
            st.status = "paused"
        if st.status == "paused":
            st.done = _size(job.part)
        st.speed, st.eta = 0.0, None
        jobs[item.id] = job
    return jobs


def _save() -> None:
    """Write every job of the current folder to the state file. Caller holds _lock."""
    path = _reg["path"]
    data = {
        "downloads": [
            {"item": asdict(j.item), "folder": str(j.folder), "state": asdict(j.state)}
            for j in _reg["jobs"].values()
        ]
    }
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        paths.replace(tmp, path)
    except OSError:
        log.warning("could not save download state", exc_info=True)


def _job(item_id: str) -> _Job:
    job = _jobs().get(item_id)
    if job is None:
        raise LatentroomError("not_found", "That download no longer exists.", 404)
    return job


def _queue(job: _Job) -> DownloadState:
    """Check disk space, then put the job in the queue. Caller holds _lock."""
    st = job.state
    st.done, st.speed, st.eta = _size(job.part), 0.0, None
    need = job.item.size - st.done + DISK_MARGIN
    ahead = sum(  # bytes the downloads already in the queue will still write
        j.item.size - _size(j.part)
        for j in _jobs().values()
        if j is not job and j.state.status in ACTIVE
    )
    if hardware.free_disk(job.folder) < need + ahead:
        after = " for this and the downloads before it" if ahead else ""
        need_gb = hardware.gb(need + ahead, up=True)
        st.status, st.error = "failed", f"Not enough disk space: need {need_gb}{after}."
    else:
        st.status, st.error = "queued", None
        worker = _run_state["worker"]
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_run, name="latentroom-downloads", daemon=True)
            _run_state["worker"] = worker
            worker.start()
    _save()
    return replace(st)


def start(item: FileItem) -> DownloadState:
    """Queue a download (no-op if already done or running). Checks free disk BEFORE starting."""
    with _lock:
        jobs = _jobs()
        job = jobs.get(item.id)
        if job is not None and job.item == item and job.state.status in ACTIVE:
            return replace(job.state)
        folder = paths.engine_dir() if item.kind == "engine" else paths.models_dir()
        new = DownloadState(item.id, item.name, "queued", 0, item.size, 0.0, None, None)
        job = jobs[item.id] = _Job(item, folder, new)
        if _size(job.final) == item.size:  # already there (an earlier download or an import)
            new.status, new.done = "done", item.size
            _save()
            return replace(new)
        return _queue(job)


def pause(item_id: str) -> DownloadState:
    with _lock:
        st = _job(item_id).state
        if st.status in ("queued", "downloading"):
            st.status, st.speed, st.eta = "paused", 0.0, None
            _lock.notify_all()
            _save()
        return replace(st)


def resume(item_id: str) -> DownloadState:
    with _lock:
        job = _job(item_id)
        if job.state.status in ("paused", "failed", "cancelled"):
            return _queue(job)
        return replace(job.state)


def cancel(item_id: str) -> DownloadState:
    """Stop and delete the .part file."""
    with _lock:
        job = _job(item_id)
        st = job.state
        if st.status != "done":
            st.status, st.done, st.speed, st.eta, st.error = "cancelled", 0, 0.0, None, None
            if _run_state["active"] is not job:  # else the worker deletes it once it lets go
                job.part.unlink(missing_ok=True)
            _lock.notify_all()
            _save()
        return replace(st)


def progress() -> list[DownloadState]:
    """Every download this data folder knows about (also finished/failed ones)."""
    with _lock:
        return [replace(j.state) for j in _jobs().values()]


# --- worker ---------------------------------------------------------------------------------


def _run() -> None:
    while True:
        with _lock:
            job = next((j for j in _jobs().values() if j.state.status == "queued"), None)
            if job is None:
                _run_state["worker"] = None
                return
            job.state.status = "downloading"
            _run_state["active"] = job
            _save()
        try:
            with awake.keep_awake():  # this worker thread's ask, cleared when the file ends
                _download(job)
        except Exception:  # a bug must not kill the queue silently
            log.exception("download %s crashed", job.item.id)
            _fail(job, GAVE_UP)
        finally:
            with _lock:
                _run_state["active"] = None
                if job.state.status == "cancelled":
                    job.part.unlink(missing_ok=True)


def _fail(job: _Job, message: str) -> None:
    with _lock:
        if job.state.status not in ("paused", "cancelled"):
            job.state.status, job.state.error = "failed", message
        job.state.speed, job.state.eta = 0.0, None
        _save()


def _download(job: _Job) -> None:
    st, attempt = job.state, 0
    while True:
        before = _size(job.part)
        try:
            _fetch(job)
            break
        except _Stop:
            return
        except _Fatal as e:
            return _fail(job, str(e))
        except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, short reads
            if _size(job.part) > before:
                attempt = 0  # it was moving: a flaky line gets a fresh set of retries
            if attempt >= len(RETRY_DELAYS):
                return _fail(job, GAVE_UP)
            log.info("download %s: %s, retry in %ss", job.item.id, e, RETRY_DELAYS[attempt])
            with _lock:
                st.error, st.speed, st.eta = LOST, 0.0, None
                _save()
                if _lock.wait_for(lambda: st.status != "downloading", RETRY_DELAYS[attempt]):
                    return
            attempt += 1
    _verify(job)


def _open(job: _Job, have: int):
    headers = {"User-Agent": "Latentroom", "Range": f"bytes={have}-"}
    req = urllib.request.Request(job.item.url, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)  # noqa: S310 fixed catalogue URL
    except urllib.error.HTTPError as e:
        if e.code == 416:  # nothing left to send: the size/hash check decides
            return None
        if e.code in (408, 429) or e.code >= 500:
            raise  # the server is struggling: retry
        raise _Fatal(DEAD_LINK.format(e.code)) from e


def _write_error(e: OSError, job: _Job) -> _Fatal:
    if e.errno == errno.ENOSPC:
        need_gb = hardware.gb(job.item.size - _size(job.part), up=True)
        return _Fatal(f"Not enough disk space: need {need_gb}.")
    log.warning("cannot write %s", job.part, exc_info=True)
    return _Fatal(NO_WRITE)


def _fetch(job: _Job) -> None:
    """Append the missing bytes to the .part file. Network errors propagate (retry)."""
    st, total = job.state, job.item.size
    have = _size(job.part)
    if have >= total:
        return
    resp = _open(job, have)
    if resp is None:
        return
    with resp:
        if resp.status != 206:
            have = 0  # the server ignored Range and sends the whole file: start over
        try:
            f = open(job.part, "ab" if have else "wb")  # noqa: SIM115 closed by the with below
        except OSError as e:
            raise _write_error(e, job) from e
        with f:
            window_t = saved = time.monotonic()
            window_b = have
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                try:
                    f.write(chunk)
                except OSError as e:
                    raise _write_error(e, job) from e
                have += len(chunk)
                if have > total:
                    break  # more bytes than the catalogue says: the check in _verify fails it
                now = time.monotonic()
                with _lock:
                    if st.status != "downloading":
                        raise _Stop  # paused or cancelled: at most one chunk too many
                    st.done, st.error = have, None
                    if now - window_t >= 1.0:
                        st.speed = (have - window_b) / (now - window_t)
                        st.eta = (total - have) / st.speed if st.speed else None
                        window_t, window_b = now, have
                    if now - saved >= SAVE_EVERY:
                        _save()
                        saved = now
    if have < total:
        raise ConnectionError(f"connection closed at {have} of {total} bytes")


def _sha256(p: Path) -> str:
    with open(p, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _verify(job: _Job) -> None:
    st, item = job.state, job.item
    with _lock:
        if st.status != "downloading":
            return
        st.status, st.speed, st.eta, st.error = "verifying", 0.0, None, None
        _save()
    ok = _size(job.part) == item.size and (item.sha256 is None or _sha256(job.part) == item.sha256)
    with _lock:
        if st.status == "cancelled":
            return
        if not ok:
            log.warning("download %s failed its size/sha256 check", item.id)
            job.part.unlink(missing_ok=True)
            st.done = 0
            return _fail(job, DAMAGED)
        try:
            paths.replace(job.part, job.final)  # an antivirus may still scan the new file
        except OSError:
            log.warning("cannot move %s into place", job.part, exc_info=True)
            return _fail(job, NO_WRITE)
        st.status, st.done = "done", item.size
        _save()
