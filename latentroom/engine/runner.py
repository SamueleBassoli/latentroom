"""One-job-at-a-time queue that runs the engine.

Engine command: install.engine_exe(), unless env LATENTROOM_ENGINE is set (tests/QA only):
then LATENTROOM_ENGINE is the command, split with shlex (posix=False), e.g.
  LATENTROOM_ENGINE="C:/path/python.exe C:/path/latentroom/tests/fake_sd_cli.py"
The UI polls events(since); there are no callbacks across the HTTP boundary."""

import copy
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import BinaryIO

from latentroom.engine import (
    awake,
    catalogue,
    hardware,
    history,
    images,
    install,
    jobs,
    paths,
    procs,
)
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError

EVENT_TYPES = ("queued", "preparing", "step", "done", "failed", "cancelled")
OOM = "Not enough video memory — try a smaller size or a lighter model."
CRASH = (
    "The engine stopped with an error. Try again; if it keeps failing, "
    "send the engine log from Latentroom's logs folder."
)
NO_IMAGE = "The engine finished without making a picture. Try again."
NO_SAVE = "Latentroom could not save into the output folder. Pick another folder in Settings."
NO_KERNEL = "This engine build does not support your graphics card."
EMPTY = "The engine made an empty picture. Try a smaller size or a lighter model."
DAMAGED = "A model file is damaged or incomplete. Download it again in the Models tab."
DRIVER_OLD = (
    "Your NVIDIA driver is too old for the engine. "
    f"Update it from {hardware.DRIVER_URL}, then restart your PC."
)
NO_GPU = (
    "The engine could not find your NVIDIA graphics card. "
    f"Reinstall its driver from {hardware.DRIVER_URL}, then restart your PC."
)
# ggml-cuda.cu:272 logs "ggml_cuda_init: failed to initialize CUDA: <cudaGetErrorString>" and the
# engine goes on without the card; these two texts are in cudart64_12.dll 12.8 (the release's).
_CUDA_INIT = b"failed to initialize CUDA"
_DRIVER_OLD = b"CUDA driver version is insufficient for CUDA runtime version"
_NO_GPU = b"no CUDA-capable device is detected"
# A cut or wrong model file: model_loader_files.cpp:151, safetensors_io.cpp:232/:271,
# gguf_io.cpp:59, model_loader.cpp:232 ("unknown format <path>"), :1259/:1266.
_DAMAGED_TEXTS = (
    b"extends beyond its model file",
    b"data offsets out of bounds for tensor",
    b"with GGUFReader",
    b"unknown format",
    b"read tensor data failed: '",
)
# Memory failures in the pinned engine's output (sd.cpp master-994-e779d4f + its ggml):
# cudaMalloc's "out of memory" (ggml-cuda.cu:940, CUDA error), "failed to allocate CUDA0 buffer
# of size N" (ggml-alloc.c:941, ggml-backend.cpp:102), "model manager alloc [compute] params
# backend buffer failed" (model_manager.cpp:570, :899).
_OOM_TEXTS = (
    b"out of memory",
    b"cudaMalloc failed",
    b"failed to allocate CUDA",
    b"params backend buffer failed",
)
# LoRA load failures: model_manager.cpp:705/:713, diffusion_engine.cpp:1749/:1787, lora.hpp:80
_LORA_FAILED = re.compile(
    rb"load lora tensors from (.+?) failed"
    rb"|required lora (?:load failed|has no tensors): (.+?)(?: --- |$)"
    rb"|invalid LoRA alpha metadata '.*' in '(.+?)'",
    re.M,
)
_BAR = re.compile(rb"\|([^|]*)\|\s*(\d+)/(\d+)")  # "  |=====>     | 3/20 - 1.23it/s"
_APPLIED = re.compile(
    rb"\((\d+) / (\d+)\) LoRA tensors have been applied, lora_file_path = (.+?)(?: --- |$)"
)
_OUT = re.compile(r"out(?:_(\d+))?\.png")  # build(): out.png, or out_0.png ... for a batch
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REDACTED = "<redacted>"  # stands for a prompt in a kept log
_PROMPT_FLAGS = frozenset(("--prompt", "--negative-prompt", "--ad-prompt", "--ad-negative-prompt"))
PREREAD_CHUNK = 64 << 20
log = logging.getLogger(__name__)


@dataclass
class JobEvent:
    seq: int  # 1, 2, 3 ... across the whole queue; poll with events(since=last seq seen)
    job_id: str
    type: str  # one of EVENT_TYPES
    time: float  # unix seconds
    step: int = 0  # "step": steps done in this JOB so far (1-based, never goes back)
    total: int = 0  # "step": steps of the whole job (every run, every image of a batch)
    message: str = ""  # "failed": one plain sentence + what to do; never the prompt
    entries: list[int] = field(default_factory=list)  # "done": history entry ids created
    loras: list[dict] = field(default_factory=list)  # "done": [{"name", "applied", "total"}]


class _Cancelled(Exception):
    pass


class _Failed(Exception):
    """An engine run failed; the message is what the user reads."""


def engine_command() -> list[str]:
    """The engine executable as an argv prefix. Raises LatentroomError("engine_missing")."""
    env = os.environ.get("LATENTROOM_ENGINE", "").strip()
    if env:  # posix=False keeps the quotes around "C:/Program Files/..."; drop them
        tokens = shlex.split(env, posix=False)
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t for t in tokens]
    exe = install.engine_exe()
    if exe is None:
        raise LatentroomError(
            "engine_missing",
            "The Latentroom engine is not installed. Run the setup again from Settings.",
            409,
        )
    return [str(exe)]


def _units(argv: list[str]) -> tuple[int, int]:
    """(steps this run adds to the job bar, images it makes). An upscale run counts 1."""
    opt = dict(zip(argv, argv[1:], strict=False))
    if opt.get("--mode") == "upscale":
        return 1, 1
    if "--sigmas" in opt:
        steps = jobs.SIGMA_STEPS.get(opt["--sigmas"], opt["--sigmas"].count(","))
    else:
        steps = int(opt.get("--steps", 20))
    count = int(opt.get("--batch-count", 1))
    return max(1, steps) * count, count


class _Bar:
    """Engine sampling bars -> forward-only fraction of one run.
    The first '=' bar fixes the per-image step count M: img2img samples fewer steps than
    --steps (only the sigmas below --strength), so M is read from the engine, not predicted."""

    def __init__(self, count: int) -> None:
        self.count, self.m, self.done = count, 0, 0

    def fraction(self, k: int, m: int) -> float:
        self.m = self.m or m
        if m != self.m:  # a tiling bar (VAE/ESRGAN tiles), not a sampling step
            return 0.0
        if k == m:
            self.done += 1
            return min(1.0, self.done / self.count)
        return min(1.0, (self.done + k / m) / self.count)


KEEP_ENGINE_LOGS = 20  # engine-<job>.log of failed jobs: a fixed count, like latentroom.log


def _prune_engine_logs() -> None:
    """Delete all but the newest KEEP_ENGINE_LOGS engine logs. Never fails a job."""
    try:
        logs = sorted(paths.logs_dir().glob("engine-*.log"), key=lambda p: p.stat().st_mtime)
        for p in logs[:-KEEP_ENGINE_LOGS]:
            p.unlink()
    except OSError:
        log.warning("old engine logs not pruned", exc_info=True)


def sweep_jobs() -> int:
    """Delete the job folders a crash left in jobs\\ (resized photos, masks, raw output).
    Call at app start, before the server: no job of this Latentroom runs yet, and the instance
    lock keeps a second one away. Only Queue.submit's own job-id names are touched. Returns
    the count; a folder still held stays until the next start."""
    gone = 0
    for p in paths.jobs_dir().iterdir():
        if re.fullmatch(r"[0-9a-f]{12}", p.name) and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            gone += not p.exists()
    return gone


def saved_jobs() -> list[dict]:
    """The jobs a crash left in queue.json, in queue order: [{"request": JobRequest as a dict,
    "running": bool, "retried": bool}]. [] when there is none; a broken file is deleted."""
    p = paths.queue_path()
    if not p.exists():
        return []
    saved = paths.read_json(p)
    if isinstance(saved, list) and all(
        isinstance(j, dict) and isinstance(j.get("request"), dict) for j in saved
    ):
        return saved
    log.warning("the saved queue could not be read: it is ignored")
    try:
        p.unlink()
    except OSError:
        log.warning("the saved queue could not be deleted", exc_info=True)
    return []


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":  # /T: the engine's child processes go too
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=_NO_WINDOW,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


class Queue:
    def __init__(self, output_dir: Callable[[], Path] = paths.default_output_dir) -> None:
        """`output_dir` is called when a job finishes (the setting may change meanwhile)."""
        self._output_dir = output_dir
        self._lock = threading.Condition()
        self._pending: list[tuple[str, JobRequest]] = []
        self._current: tuple[str, JobRequest] | None = None  # the running job
        self._retried: set[str] = set()  # jobs running at a crash once already (restore)
        self._cancel_current = False
        self._proc: subprocess.Popen | None = None
        self._events: deque[JobEvent] = deque(maxlen=1000)
        self._seq = 0
        self._stopped = False
        self._exclusive = False  # run_exclusive() is running (Enhance prompt)
        self._thread = threading.Thread(target=self._work, name="latentroom-queue", daemon=True)
        self._thread.start()

    # ---- public API ---------------------------------------------------------------------
    def submit(self, req: JobRequest, retried: bool = False) -> str:
        """Validate + fit-check, queue, return job_id at once. `retried`: see restore().
        Raises LatentroomError (bad_request, too_big, engine_missing, model_missing)."""
        req = copy.deepcopy(req)  # the caller may reuse its object
        jobs.validate(req, self._output_dir())
        if req.kind != "upscale":
            self._fit_check(req)
        engine_command()
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            if self._stopped:
                msg = "Latentroom is closing. Start it again."
                raise LatentroomError("not_available", msg, 409)
            if self._exclusive:
                raise LatentroomError("busy", "Wait for the prompt to finish.", 409)
            self._pending.append((job_id, req))
            if retried:
                self._retried.add(job_id)
            self._save()
            self._emit(job_id, "queued")
            self._lock.notify_all()
        return job_id

    def restore(self) -> int:
        """Queue again, in order, the jobs a crash or power cut left in queue.json (app start).
        The job that was running goes first, once: if it was running at the crash before too,
        it may be what crashes Latentroom, so it is dropped. Returns the count queued."""
        queued = 0
        for job in saved_jobs():
            if job.get("running") and job.get("retried"):
                log.warning("a job running at two crashes in a row was dropped")
                continue
            try:
                req = JobRequest.from_dict(job["request"])
                self.submit(req, retried=bool(job.get("running") or job.get("retried")))
                queued += 1
            except Exception as e:  # its pictures or model may be gone; the log gets no prompt
                why = getattr(e, "code", type(e).__name__)
                log.warning("a saved job was not queued again: %s", why)
        with self._lock:
            self._save()  # nothing queued again: the file goes
        return queued

    def run_exclusive(self, fn: Callable[[], object]) -> object:
        """fn() with the GPU to itself: refused (busy) while a job is queued or running or
        another fn runs; submit() is refused until it returns. For Enhance prompt."""
        with self._lock:
            if self._stopped:
                msg = "Latentroom is closing. Start it again."
                raise LatentroomError("not_available", msg, 409)
            if self._pending or self._current:
                raise LatentroomError("busy", "Wait for the picture to finish.", 409)
            if self._exclusive:
                raise LatentroomError("busy", "Wait for the prompt to finish.", 409)
            self._exclusive = True
        try:
            return fn()
        finally:
            with self._lock:
                self._exclusive = False

    def cancel(self, job_id: str) -> bool:
        """Remove a queued job or kill the running engine process tree. False if unknown/done."""
        with self._lock:
            for i, (pending_id, _req) in enumerate(self._pending):
                if pending_id == job_id:
                    del self._pending[i]
                    self._save()
                    self._emit(job_id, "cancelled")
                    return True
            if not self._current or self._current[0] != job_id or self._cancel_current:
                return False
            self._cancel_current = True
            proc = self._proc
        if proc is not None:
            _kill_tree(proc)
        return True

    def jobs(self) -> list[dict]:
        """Queued + running jobs: [{"id", "kind", "status": "queued"|"running", "position",
        "request"}]. "request" lets the page show a job it did not submit (queued again after
        a crash, or before a reload)."""
        with self._lock:
            out = []
            if self._current:
                job_id, req = self._current
                out.append({"id": job_id, "kind": req.kind, "status": "running", "position": 0})
                out[-1]["request"] = req.to_dict()
            for n, (job_id, req) in enumerate(self._pending, 1):
                out.append({"id": job_id, "kind": req.kind, "status": "queued", "position": n})
                out[-1]["request"] = req.to_dict()
            return out

    def events(self, since: int = 0) -> list[JobEvent]:
        """Events with seq > since, oldest first (the last ~1000 are kept)."""
        with self._lock:
            return [e for e in self._events if e.seq > since]

    def shutdown(self) -> None:
        """Cancel everything, kill the engine, stop the worker thread. Safe to call twice."""
        with self._lock:
            self._stopped = True
            for job_id, _req in self._pending:
                self._emit(job_id, "cancelled")
            self._pending.clear()
            self._save()  # a normal close: nothing is queued again at the next start
            self._cancel_current = self._current is not None
            proc = self._proc
            self._lock.notify_all()
        if proc is not None:
            _kill_tree(proc)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=15)

    # ---- worker -------------------------------------------------------------------------
    def _emit(self, job_id: str, type_: str, **kw) -> None:
        """Caller holds the lock."""
        self._seq += 1
        self._events.append(JobEvent(self._seq, job_id, type_, time.time(), **kw))

    def _save(self) -> None:
        """Write the running and waiting jobs to queue.json (atomic), so a crash or power cut
        loses none; delete it when there is none or Latentroom is closing. Never fails a job.
        Caller holds the lock."""
        queued = [(*self._current, True)] if self._current else []
        queued += [(job_id, req, False) for job_id, req in self._pending]
        try:
            p = paths.queue_path()
            if self._stopped or not queued:
                p.unlink(missing_ok=True)
                return
            tmp = p.with_name(p.name + ".tmp")
            saved = [
                {"request": req.to_dict(), "running": running, "retried": job_id in self._retried}
                for job_id, req, running in queued
            ]
            tmp.write_text(json.dumps(saved), encoding="utf-8")
            paths.replace(tmp, p)
        except OSError:
            log.warning("the queue could not be saved", exc_info=True)

    def _fit_check(self, req: JobRequest) -> None:
        width, height = jobs.job_size(req)
        refs = len(req.refs) if req.kind == "edit" else 0
        model = catalogue.get(req.model_id)
        hw = catalogue.capped(hardware.detect(), req.max_vram_gb)  # the card, not its free VRAM
        fit = catalogue.fit(model, hw, width, height, refs, jobs.lora_bytes(req))
        if fit.label == "too_big":
            raise LatentroomError(
                "too_big",
                "Too big for this PC at this size. Pick a smaller size or a lighter model.",
                409,
            )

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._pending and not self._stopped:
                    self._lock.wait()
                if self._stopped:
                    return
                job_id, req = self._pending.pop(0)
                self._current, self._cancel_current = (job_id, req), False
                self._save()
            try:
                with awake.keep_awake():  # this worker thread's ask, cleared when the job ends
                    self._run(job_id, req)
            except Exception:  # its own housekeeping failed (log move, job folder): not fatal
                log.exception("job %s failed", job_id)
                with self._lock:
                    self._emit(job_id, "failed", message=CRASH)
            finally:
                with self._lock:
                    self._current, self._proc = None, None
                    self._save()

    def _run(self, job_id: str, req: JobRequest) -> None:
        job_dir = paths.jobs_dir() / job_id
        engine_log = job_dir / "engine.log"
        with self._lock:
            self._emit(job_id, "preparing")
        req = replace(req, seed=req.seed if req.seed >= 0 else secrets.randbelow(2**31))
        try:
            # sd.cpp #2131: a Library picture edited with the seed it was made with gets the
            # same noise again and comes out oversaturated. The next seed is stored and shown.
            if req.seed in {history.seed_of(Path(p)) for p in [*req.refs, req.init_image] if p}:
                req = replace(req, seed=(req.seed + 1) % 2**32)
            argvs = jobs.build(req, job_dir)
            loras = self._engine_runs(job_id, req, argvs, engine_log)
            entries = self._store(req, job_dir)
        except _Cancelled:
            with self._lock:
                self._emit(job_id, "cancelled")
        except Exception as e:
            if isinstance(e, _Failed):
                message = str(e)
            elif isinstance(e, LatentroomError):
                message = e.message
            else:
                log.exception("job %s failed", job_id)
                message = CRASH
            if engine_log.exists():  # written without prompts (_engine_runs): keep as is
                shutil.move(engine_log, paths.logs_dir() / f"engine-{job_id}.log")
                _prune_engine_logs()
            with self._lock:
                self._emit(job_id, "failed", message=message)
        else:
            with self._lock:
                self._emit(job_id, "done", entries=entries, loras=loras)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def _engine_runs(
        self, job_id: str, req: JobRequest, argvs: list[list[str]], engine_log: Path
    ) -> list[dict]:
        """Every engine run of the job, in order; emits "step"; returns the LoRA report.
        The log never receives a prompt: not in the command line, not echoed by the engine."""
        cmd = engine_command()
        plan = [_units(argv) for argv in argvs]
        total, base, last = sum(units for units, _ in plan), 0, 0
        applied: dict[str, tuple[int, int]] = {}  # LoRA file stem -> (applied, total)
        echoes = _echoes(argvs)

        def step(n: int) -> None:
            nonlocal last
            if n > last:
                last = n
                with self._lock:
                    self._emit(job_id, "step", step=n, total=total)

        # unbuffered: each line is on disk at once, so a stuck job's log can be read meanwhile
        with open(engine_log, "wb", buffering=0) as logf:
            for argv, (units, count) in zip(argvs, plan, strict=True):
                logf.write(("$ " + subprocess.list2cmdline(cmd + _shown(argv)) + "\n").encode())
                bar = _Bar(count)
                self._preread(argv)
                for k, m, line in self._stream(cmd + argv, engine_log.parent, logf, echoes):
                    if m:
                        step(base + int(bar.fraction(k, m) * units))
                    elif hit := _APPLIED.search(line):  # may print twice per LoRA: keyed
                        stem = Path(hit.group(3).decode(errors="replace").strip()).stem
                        applied[stem] = (int(hit.group(1)), int(hit.group(2)))
                base += units
                step(base)
        report = []
        for lora in req.loras:
            done, of = applied.get(lora.name, (0, 0))
            report.append({"name": lora.name, "applied": done, "total": of})
        return report

    def _preread(self, argv: list[str]) -> None:
        """Read the diffusion model and VAE files once, in order, before the engine starts.
        Stops at a cancel; a read error only logs: the engine then reads the file itself."""
        opt = dict(zip(argv, argv[1:], strict=False))
        for path in filter(None, (opt.get("--diffusion-model"), opt.get("--vae"))):
            # Windows mmap has no read-ahead in this engine build: load 25-78 s -> about 2 s
            try:
                with open(path, "rb", buffering=0) as f:
                    while not self._cancel_current and f.read(PREREAD_CHUNK):
                        pass
            except OSError as e:
                log.warning("pre-read of %s failed: %s", Path(path).name, e.strerror)
            if self._cancel_current:
                raise _Cancelled

    def _stream(
        self, argv: list[str], cwd: Path, logf: BinaryIO, echoes: list[bytes]
    ) -> Iterator[tuple]:
        """Run one engine process. Yields (k, M, b"") per sampling bar, (0, 0, line) per line.
        Reads BYTES: text mode would turn the bar's '\\r' into '\\n'. Every line goes to
        `logf`, except that a line holding any of `echoes` (a prompt) is written as REDACTED."""
        with self._lock:
            if self._cancel_current:
                raise _Cancelled
            self._proc = proc = procs.start(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # [E] lines; one stream keeps the order
                creationflags=_NO_WINDOW,
                start_new_session=os.name != "nt",
            )
        tail, buf, cuda = b"", b"", b""
        while chunk := proc.stdout.read1(65536):
            *parts, buf = re.split(rb"([\r\n])", buf + chunk)
            for seg, sep in zip(parts[::2], parts[1::2], strict=True):
                logf.write(_hide(seg, echoes) + sep)
                tail = (tail + seg + b"\n")[-4000:]
                if _CUDA_INIT in seg:  # printed at the start: kept, the tail scrolls past it
                    cuda = seg + b"\n"
                bar = _BAR.search(seg)
                if bar is None:
                    if seg.strip():
                        yield 0, 0, seg
                elif b"#" not in bar.group(1) and b"MB/s" not in seg and int(bar.group(3)):
                    yield int(bar.group(2)), int(bar.group(3)), b""  # a sampling bar
        logf.write(_hide(buf, echoes))  # the last line, when it has no line end
        code = proc.wait()
        proc.stdout.close()
        with self._lock:
            self._proc = None
            if self._cancel_current:
                raise _Cancelled
        if code != 0:  # the code ends the kept log and latentroom.log: a crash is 0xC0000005
            text = procs.exit_text(code)
            logf.write(b"\n" * bool(buf) + f"{text}\n".encode())
            log.warning("job %s: the engine stopped, %s", cwd.name, text)
            raise _Failed(_failure(code, cuda + tail + buf))

    def _store(self, req: JobRequest, job_dir: Path) -> list[int]:
        """Clean final images into the output folder, one history entry each. A one-colour
        picture (black, from a run that went wrong) is never kept."""
        found = sorted(
            (int(m.group(1) or 0), p) for p in job_dir.iterdir() if (m := _OUT.fullmatch(p.name))
        )
        if not found:
            raise _Failed(NO_IMAGE)
        kept = [(i, raw) for i, raw in found if not images.one_colour(raw)]
        if len(kept) < len(found):
            log.warning("job %s: %d empty picture(s) dropped", job_dir.name, len(found) - len(kept))
        if not kept:
            raise _Failed(EMPTY)
        try:
            out_dir = self._output_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise _Failed(NO_SAVE) from e
        ids = []
        for i, raw in kept:
            clean = job_dir / f"clean_{i}.png"
            if req.kind == "inpaint":  # only the painted area may change
                images.paste_back(Path(req.init_image), raw, Path(req.mask), clean)
            else:
                images.save_clean_png(raw, clean)
            images.mark_ai(clean, [*req.refs, req.init_image])  # the invisible "made with AI" tag
            seed = req.seed + i  # sd.cpp: image b of a batch uses seed + b (image.cpp:873)
            final = _save_new(clean, out_dir, seed)
            ids.append(history.add(replace(req, seed=seed, batch=1), final, seed).id)
        return ids


def _save_new(src: Path, out_dir: Path, seed: int) -> Path:
    """Copy into the output folder under a new name; never overwrites an existing file."""
    data, stamp = src.read_bytes(), time.strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        dst = out_dir / f"latentroom-{stamp}-{seed}{f'-{n}' if n else ''}.png"
        try:
            with open(dst, "xb") as f:
                f.write(data)
            return dst
        except FileExistsError:
            continue
        except OSError as e:
            raise _Failed(NO_SAVE) from e
    raise _Failed(NO_SAVE)


def _failure(code: int, tail: bytes) -> str:
    """The sentence for an engine run that ended with non-zero `code`; `tail` = its last output."""
    if procs.blocked(code):
        return procs.BLOCKED
    if _DRIVER_OLD in tail:
        return DRIVER_OLD
    if _NO_GPU in tail:
        return NO_GPU
    if b"no kernel image is available" in tail:  # the CUDA runtime's text, after "CUDA error: "
        return NO_KERNEL
    if any(t in tail for t in _OOM_TEXTS):
        return OOM
    if hit := _LORA_FAILED.search(tail):
        name = Path(next(g for g in hit.groups() if g).decode(errors="replace").strip()).stem
        return f'The LoRA "{name}" could not be loaded and is the likely cause. Try without it.'
    if any(t in tail for t in _DAMAGED_TEXTS):
        return DAMAGED
    return CRASH


def _shown(argv: list[str]) -> list[str]:
    """argv as the kept log may show it: the value after each prompt flag is REDACTED."""
    return [REDACTED if i and argv[i - 1] in _PROMPT_FLAGS else a for i, a in enumerate(argv)]


def _echoes(argvs: list[list[str]]) -> list[bytes]:
    """Every piece of a prompt the engine may print back, as UTF-8 (how sd-cli writes).
    -v prints a prompt with its <lora:...> tags removed, and a prompt may span lines."""
    out = set()
    for argv in argvs:
        for flag, value in zip(argv, argv[1:], strict=False):
            if flag in _PROMPT_FLAGS:
                for form in (value, jobs.LORA_TAG.sub("", value)):
                    out |= {p.strip().encode() for p in re.split(r"[\r\n]+", form)}
    out.discard(b"")
    return sorted(out)


def _hide(line: bytes, echoes: list[bytes]) -> bytes:
    return REDACTED.encode() if any(e in line for e in echoes) else line
