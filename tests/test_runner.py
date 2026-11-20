import ctypes
import io
import json
import os
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

import fake_catalogue
import pytest
from PIL import Image

from latentroom.engine import awake, catalogue, history, images, jobs, paths, runner
from latentroom.engine.jobs import JobRequest, LoraRef
from latentroom.errors import LatentroomError

END = ("done", "failed", "cancelled")


@pytest.fixture
def stub(monkeypatch):
    return fake_catalogue.install_stub(monkeypatch)


@pytest.fixture
def queue(stub, tmp_path):
    q = runner.Queue(output_dir=lambda: tmp_path / "out")
    yield q
    q.shutdown()


def wait(q: runner.Queue, job_id: str, timeout=60) -> list[runner.JobEvent]:
    end = time.time() + timeout
    while time.time() < end:
        evs = [e for e in q.events() if e.job_id == job_id]
        if evs and evs[-1].type in END:
            return evs
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {q.events()}")


def upload(name="in.png", size=(512, 384)) -> str:
    p = paths.uploads_dir() / name
    Image.effect_noise(size, 50).convert("RGB").save(p)
    return str(p)


def tag(path: Path) -> str:
    """The Digital Source Type a saved picture carries. Fails on any other metadata."""
    with Image.open(path) as im:
        im.load()  # chunks after the pixels too
        assert set(im.info) == {"XML:com.adobe.xmp", "xmp", "Software"}, im.info
        assert im.info["Software"] == "Latentroom"
        return re.search(rb'DigitalSourceType="([^"]+)"', im.info["xmp"]).group(1).decode()


def test_txt2img_batch_events_files_history(queue, tmp_path):
    job = queue.submit(JobRequest(model_id="std-q4", prompt="a fox", steps=5, batch=2, seed=40))
    evs = wait(queue, job)
    types = [e.type for e in evs]
    assert types[:2] == ["queued", "preparing"] and types[-1] == "done", evs
    steps = [(e.step, e.total) for e in evs if e.type == "step"]
    assert steps == [(n, 10) for n in range(1, 11)]  # whole job: 2 images x 5 steps, forward
    assert [e.seq for e in queue.events()] == list(range(1, len(queue.events()) + 1))
    entries = [history.get(i) for i in evs[-1].entries]
    assert [e.seed for e in entries] == [40, 41]  # image b uses seed + b
    assert [e.request["batch"] for e in entries] == [1, 1]  # reuse makes exactly that picture
    for e in entries:
        path = Path(e.image)
        assert path.parent == tmp_path / "out" and path.name.startswith("latentroom-")
        assert tag(path) == images.MADE and b"a fox" not in path.read_bytes()
        assert (e.width, e.height) == (1024, 1024)
    assert not list(paths.jobs_dir().iterdir())  # job scratch removed
    assert queue.jobs() == []


@pytest.mark.parametrize("kind", ["edit", "img2img", "inpaint", "upscale", "enhance", "facefix"])
def test_every_kind_runs_on_the_fake_engine(queue, kind):
    init = upload()
    before = Path(init).read_bytes()
    mask = paths.uploads_dir() / "mask.png"
    Image.new("L", (512, 384), 0).save(mask)
    req = JobRequest(
        kind=kind, model_id="std-q4", prompt="a lighthouse at dusk",
        refs=[init] if kind == "edit" else [],
        init_image=init, mask=str(mask), width=512, height=512, steps=4,
    )  # fmt: skip
    evs = wait(queue, queue.submit(req))
    assert evs[-1].type == "done", evs[-1]
    image = Path(history.get(evs[-1].entries[0]).image)
    assert tag(image) == images.EDITED  # made from the user's photo
    assert b"lighthouse" not in image.read_bytes()
    assert Path(init).read_bytes() == before  # the upload itself is never touched
    size = {"upscale": (2048, 1536), "enhance": (1024, 768), "edit": (512, 512)}
    with Image.open(image) as im:
        assert im.size == size.get(kind, (512, 384))
    if kind == "inpaint":  # nothing painted: every pixel is the original
        with Image.open(image) as out, Image.open(init) as orig:
            assert out.tobytes() == orig.tobytes()
    if kind == "enhance":
        assert [e.total for e in evs if e.type == "step"][-1] == 1 + 4  # upscale + 4 steps


def test_the_prompt_never_reaches_the_file_even_when_the_engine_writes_it(queue, monkeypatch):
    real_build, real_save, raw = jobs.build, images.save_clean_png, []

    def build(req, job_dir):  # without --disable-image-metadata sd-cli puts the prompt in its PNG
        argvs = real_build(req, job_dir)
        return [[a for a in argv if a != "--disable-image-metadata"] for argv in argvs]

    def save(src, dst):
        raw.append(src.read_bytes())
        real_save(src, dst)

    monkeypatch.setattr(jobs, "build", build)
    monkeypatch.setattr(images, "save_clean_png", save)
    job = queue.submit(JobRequest(model_id="std-q4", prompt="a lighthouse at dusk", steps=2))
    image = Path(history.get(wait(queue, job)[-1].entries[0]).image)
    assert b"parameters\0a lighthouse at dusk" in raw[0]  # the engine did write it
    assert tag(image) == images.MADE and b"lighthouse" not in image.read_bytes()


def test_reworking_a_latentroom_picture_stays_created_by_ai(queue):
    job = queue.submit(JobRequest(model_id="std-q4", prompt="a lighthouse at dusk", steps=2))
    made = history.get(wait(queue, job)[-1].entries[0]).image
    req = JobRequest(kind="img2img", model_id="std-q4", prompt="at night", init_image=made)
    image = Path(history.get(wait(queue, queue.submit(req))[-1].entries[0]).image)
    assert tag(image) == images.MADE


def test_lora_report(queue):
    (paths.loras_dir() / "style-a.safetensors").write_bytes(b"x")
    ghost = paths.loras_dir() / "ghost.safetensors"
    ghost.write_bytes(b"x")
    req = JobRequest(
        model_id="std-q4", prompt="x", steps=2, loras=[LoraRef("style-a"), LoraRef("ghost", 0.5)]
    )
    job = queue.submit(req)
    ghost.unlink()  # gone before the engine looks: the engine warns and goes on without it
    done = wait(queue, job)[-1]
    assert done.type == "done"
    assert done.loras == [
        {"name": "style-a", "applied": 720, "total": 720},
        {"name": "ghost", "applied": 0, "total": 0},
    ]


LORA_FAILED = 'The LoRA "film grain" could not be loaded and is the likely cause. Try without it.'


@pytest.mark.parametrize(
    "fail, message, ended",
    [
        ("oom", runner.OOM, "0x00000001"),
        ("alloc", runner.OOM, "0x00000001"),
        ("kernel", runner.NO_KERNEL, "0x00000001"),
        ("lora", LORA_FAILED, "0x00000001"),
        ("damaged", runner.DAMAGED, "0x00000001"),
        ("driver", runner.DRIVER_OLD, "0x00000001"),
        ("nogpu", runner.NO_GPU, "0x00000001"),
        ("blocked", runner.procs.BLOCKED, "0xC0000135"),
        ("boom", runner.CRASH, "0x00000001"),
    ],
)
def test_failure_message_and_clean_log(queue, monkeypatch, fail, message, ended):
    monkeypatch.setenv("FAKE_FAIL", fail)
    job = queue.submit(JobRequest(model_id="std-q4", prompt="secret lighthouse", steps=3))
    failed = wait(queue, job)[-1]
    assert failed.type == "failed"
    assert failed.message == message
    assert "lighthouse" not in failed.message
    log = (paths.logs_dir() / f"engine-{job}.log").read_text(encoding="utf-8")
    assert "lighthouse" not in log and runner.REDACTED in log and "--steps 3" in log
    assert log.endswith(f"exit code {ended}\n")  # how it ended, in the report
    assert not list(paths.jobs_dir().iterdir()) and not history.list_entries()


def test_an_empty_picture_fails_and_stays_out_of_the_library(queue, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_EMPTY", "1")  # every pixel the same colour: a run that went black
    failed = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=2)))[-1]
    assert (failed.type, failed.message) == ("failed", runner.EMPTY)
    assert not history.list_entries() and not list((tmp_path / "out").glob("*.png"))
    monkeypatch.delenv("FAKE_EMPTY")  # a dark picture with any second colour is kept
    kept = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=2)))[-1]
    assert kept.type == "done" and len(history.list_entries()) == 1


def test_a_windows_block_fails_the_job_with_its_cause(queue, monkeypatch):
    def blocked(argv, **kw):
        raise OSError(None, "blocked", argv[0], 4551)  # Smart App Control

    monkeypatch.setattr(runner.procs.subprocess, "Popen", blocked)
    monkeypatch.setattr(runner.procs, "BLOCKS", frozenset((None, 4551)))  # POSIX: no winerror
    failed = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x")))[-1]
    assert (failed.type, failed.message) == ("failed", runner.procs.BLOCKED)


def once(fn, error: Exception):
    """fn that raises `error` on its first call only."""
    calls = []

    def first_fails(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return fn(*a, **kw)

    return first_fails


@pytest.mark.parametrize("where", ["log", "folder"])
def test_the_queue_survives_an_error_in_its_own_housekeeping(queue, monkeypatch, caplog, where):
    """Keeping the engine log (an antivirus holds it, the disk is full) or making the job
    folder (the data drive is gone) fails: that job fails with CRASH, the next one still runs.
    Before, the worker thread died and every later job stayed "queued"."""
    full = OSError(28, "No space left on device")
    if where == "log":
        monkeypatch.setenv("FAKE_FAIL", "boom")
        monkeypatch.setattr(runner.shutil, "move", once(runner.shutil.move, full))
    else:
        monkeypatch.setattr(paths, "jobs_dir", once(paths.jobs_dir, full))
    first = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1))
    assert wait(queue, first, timeout=20)[-1].message == runner.CRASH
    assert "No space left" in caplog.text
    monkeypatch.delenv("FAKE_FAIL", raising=False)
    second = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1))
    assert wait(queue, second, timeout=20)[-1].type == "done"
    # "done" is sent just before the queue drops the job: give it a moment to clear.
    end = time.monotonic() + 5
    while queue.jobs() and time.monotonic() < end:
        time.sleep(0.05)
    assert queue._thread.is_alive() and queue.jobs() == []


def kept_log(queue, monkeypatch, req: JobRequest) -> str:
    """Run a failing job with an engine that echoes its prompts; return the kept log."""
    monkeypatch.setenv("FAKE_FAIL", "boom")
    monkeypatch.setenv("FAKE_VERBOSE", "1")
    job = queue.submit(req)
    assert wait(queue, job)[-1].type == "failed"
    return (paths.logs_dir() / f"engine-{job}.log").read_text(encoding="utf-8")


def test_only_the_newest_20_engine_logs_are_kept(queue, monkeypatch):
    """Each failed job keeps an engine-<job>.log; before, they piled up without end. The
    newest 20 stay (latentroom.log too keeps a fixed count), older ones go."""
    logs = paths.logs_dir()
    for n in range(25):
        old = logs / f"engine-old{n:02}.log"
        old.write_text("x")
        os.utime(old, (1_000_000 + n, 1_000_000 + n))
    assert "boom" in kept_log(queue, monkeypatch, JobRequest(model_id="std-q4", prompt="x"))
    kept = {p.name for p in logs.glob("engine-*.log")}
    assert len(kept) == 20 and {f"engine-old{n:02}.log" for n in range(6, 25)} < kept


def test_kept_log_never_holds_a_quoted_prompt(queue, monkeypatch):
    req = JobRequest(
        model_id="std-q4", prompt='portrait of "Anna Rossi" at home', cfg=5, steps=2,
        negative='no "blur", C:\\negs\\x\nsecond line',
    )  # fmt: skip
    log = kept_log(queue, monkeypatch, req)
    for secret in ("Anna", "Rossi", "portrait", "blur", "negs", "second line"):
        assert secret not in log, secret
    assert "--prompt <redacted>" in log and "--negative-prompt <redacted>" in log
    assert "--steps 2" in log and "fake failure: boom" in log  # the rest stays readable


def test_kept_log_hides_a_prompt_with_lora_tags(queue, monkeypatch):
    (paths.loras_dir() / "test-style-a.safetensors").write_bytes(b"x")
    req = JobRequest(
        model_id="std-q4", prompt="a <lora:typed:1> secret lighthouse", steps=2,
        loras=[LoraRef("test-style-a", 0.6)],
    )  # fmt: skip
    # argv: "a  secret lighthouse <lora:test-style-a:0.6>"; the engine echoes it tag-free
    log = kept_log(queue, monkeypatch, req)
    assert "lighthouse" not in log and "--prompt <redacted>" in log
    assert "LoRA tensors have been applied" in log  # other lines are kept


def test_cancel_running_and_queued(queue, monkeypatch):
    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    running = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=40))
    queued = queue.submit(JobRequest(model_id="std-q4", prompt="y", steps=40))
    end = time.time() + 30
    while not any(e.type == "step" and e.job_id == running for e in queue.events()):
        assert time.time() < end
        time.sleep(0.05)
    assert [j["status"] for j in queue.jobs()] == ["running", "queued"]
    assert queue.cancel(queued) and not queue.cancel(queued)
    started = time.time()
    assert queue.cancel(running)
    assert wait(queue, running)[-1].type == "cancelled"
    assert time.time() - started < 10  # killed, not waited for (40 x 0.5 s)
    assert [e.type for e in queue.events() if e.job_id == queued] == ["queued", "cancelled"]
    assert not queue.cancel("nope") and not history.list_entries()


def alive(pid: int) -> bool:
    """True while process `pid` runs (Windows: its handle is not signalled yet)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        k.CloseHandle(handle)


def test_cancel_kills_the_engine_child_processes(queue, monkeypatch, tmp_path):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    monkeypatch.setenv("FAKE_CHILD", str(pid_file))
    job = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=40))
    end = time.time() + 30
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.time() < end
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    assert alive(pid)
    started = time.time()
    assert queue.cancel(job) and wait(queue, job)[-1].type == "cancelled"
    assert time.time() - started < 10  # killed: a surviving child keeps the pipe open (30 s)
    while alive(pid):
        assert time.time() - started < 10, "the engine's child process survived cancel"
        time.sleep(0.05)


def spy_start(monkeypatch, calls: list) -> None:
    real = runner.procs.start

    def start(*a, **kw):
        calls.append("start")
        return real(*a, **kw)

    monkeypatch.setattr(runner.procs, "start", start)


@pytest.mark.parametrize(
    ("kind", "order"),
    [
        ("txt2img", ["std-Q4_K.gguf", "vae.safetensors", "start"]),
        ("upscale", ["start"]),  # the upscaler file is small: no pre-read
        ("enhance", ["start", "std-Q4_K.gguf", "vae.safetensors", "start"]),
    ],
)
def test_model_files_are_read_before_the_engine_starts(queue, monkeypatch, caplog, kind, order):
    for name in ("std-Q4_K.gguf", "vae.safetensors", "llm.gguf"):
        (paths.models_dir() / name).write_bytes(b"w" * 10)
    monkeypatch.setattr(runner, "PREREAD_CHUNK", 3)
    calls, real_open = [], open

    def spy_open(file, mode="r", *a, **kw):
        if mode == "rb":
            calls.append(Path(file).name)
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(runner, "open", spy_open, raising=False)
    spy_start(monkeypatch, calls)
    req = JobRequest(kind=kind, model_id="std-q4", prompt="x", init_image=upload(), steps=2)
    assert wait(queue, queue.submit(req))[-1].type == "done"
    assert calls == order and "pre-read" not in caplog.text


@pytest.mark.parametrize("how", ["cancel", "shutdown"])
def test_cancel_during_the_pre_read_stops_the_job(stub, tmp_path, monkeypatch, how):
    reading, calls = threading.Event(), []

    class Endless(io.BytesIO):  # a model file that never ends
        def read(self, n=-1):
            reading.set()
            time.sleep(0.01)
            return b"w"

    monkeypatch.setattr(runner, "open", lambda f, mode="r", *a, **kw: Endless(), raising=False)
    spy_start(monkeypatch, calls)
    q = runner.Queue(output_dir=lambda: tmp_path / "out")
    job = q.submit(JobRequest(model_id="std-q4", prompt="x"))
    assert reading.wait(10)
    assert [e.type for e in q.events()] == ["queued", "preparing"]
    started = time.time()
    if how == "cancel":
        assert q.cancel(job) and wait(q, job)[-1].type == "cancelled"
    q.shutdown()
    assert time.time() - started < 5 and calls == []  # the engine never started
    assert [e.type for e in q.events()][-1] == "cancelled"


def test_an_unreadable_model_file_does_not_fail_the_job(queue, caplog):
    evs = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=2)))
    assert evs[-1].type == "done"  # the fake engine needs no file; sd-cli would read it itself
    assert "pre-read of std-Q4_K.gguf failed" in caplog.text
    assert str(paths.models_dir()) not in caplog.text  # the name only


@pytest.mark.parametrize("field", ["init_image", "mask", "refs"])
def test_picture_deleted_while_queued(queue, monkeypatch, field):
    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    blocker = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=40))
    gone, keep = upload("gone.png"), upload("keep.png")
    mask = paths.uploads_dir() / "mask.png"
    Image.new("L", (512, 384), 0).save(mask)
    kind = {"init_image": "img2img", "mask": "inpaint", "refs": "edit"}[field]
    req = JobRequest(
        kind=kind, model_id="std-q4", prompt="x", init_image=keep, mask=str(mask),
        refs=[keep] if kind == "edit" else [],  # refs only go with edit
    )  # fmt: skip
    job = queue.submit(replace(req, **{field: [gone] if field == "refs" else gone}))
    Path(gone).unlink()
    queue.cancel(blocker)
    failed = wait(queue, job)[-1]
    assert failed.type == "failed"
    assert failed.message == "The picture gone.png is no longer there. Add it again."
    assert not list(paths.logs_dir().glob("engine-*.log"))  # the engine never ran


def test_engine_log_is_on_disk_while_the_job_runs(queue, monkeypatch, tmp_path):
    """A stuck job can be diagnosed: each engine line reaches engine.log as it arrives
    (the log of a held job was 0 bytes until the job ended: an 8 KB write buffer)."""
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_HOLD", str(gate))
    job = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=40))
    end = time.time() + 30
    while not any(e.type == "step" and e.job_id == job for e in queue.events()):
        assert time.time() < end
        time.sleep(0.05)
    log = (paths.jobs_dir() / job / "engine.log").read_bytes()
    gate.touch()
    assert b"--steps 40" in log and b"MB/s" in log and b" 1/40 " in log, log
    assert wait(queue, job)[-1].type == "done"


def test_submit_errors(queue, stub, monkeypatch):
    stub.fit_label = "too_big"
    with pytest.raises(LatentroomError) as e:
        queue.submit(JobRequest(model_id="std-q4", prompt="x", width=2048, height=2048))
    assert e.value.code == "too_big" and stub.fit_calls[-1] == ("std-q4", 2048, 2048, 0, 0)
    stub.fit_label = "partial"
    monkeypatch.delenv("LATENTROOM_ENGINE")
    with pytest.raises(LatentroomError) as e:
        queue.submit(JobRequest(model_id="std-q4", prompt="x"))
    assert e.value.code == "engine_missing"
    assert queue.events() == []


def test_fit_check_counts_the_loras(queue, stub):  # user LoRAs + the Turbo add-on
    (paths.loras_dir() / "test-style-a.safetensors").write_bytes(b"x" * 5)
    req = JobRequest(model_id="std-q4", prompt="x", loras=[LoraRef("test-style-a")], turbo=True)
    queue.cancel(queue.submit(req))
    assert stub.fit_calls[-1] == ("std-q4", 1024, 1024, 0, 5 + 1)  # fake turbo-lora is 1 byte


def test_fit_check_sees_the_video_memory_limit(queue, stub, monkeypatch):
    seen = []
    fit = catalogue.fit
    monkeypatch.setattr(catalogue, "fit", lambda m, hw, *a: seen.append(hw) or fit(m, hw, *a))
    queue.cancel(queue.submit(JobRequest(model_id="std-q4", prompt="x", max_vram_gb=4)))
    assert seen[-1] == catalogue.capped(fake_catalogue.HW, 4) != fake_catalogue.HW


def test_random_seed_is_stored(queue):
    done = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1)))[-1]
    entry = history.get(done.entries[0])
    assert entry.seed >= 0 and entry.request["seed"] == entry.seed
    assert Path(entry.image).name.endswith(f"-{entry.seed}.png")


def test_editing_a_picture_with_its_own_seed_runs_the_next_seed(queue, monkeypatch):
    """sd.cpp #2131: a Library picture edited (or reworked) with the seed it was made with gets
    the same noise again and comes out oversaturated. The engine gets seed + 1, history stores
    it (Develop and Library show it). A plain re-develop keeps its seed."""
    first = JobRequest(model_id="std-q4", prompt="x", steps=1, seed=7)
    made = history.get(wait(queue, queue.submit(first))[-1].entries[0]).image
    real_build, sent = jobs.build, []

    def build(req, job_dir):
        argvs = real_build(req, job_dir)
        sent.append(int(argvs[-1][argvs[-1].index("--seed") + 1]))
        return argvs

    monkeypatch.setattr(jobs, "build", build)
    rework = replace(first, kind="img2img", prompt="at night", init_image=made)
    reqs = [
        replace(first, kind="edit", prompt="at night", refs=[made]),
        rework,
        replace(rework, seed=9),
        first,  # re-develop
    ]
    stored = [history.get(wait(queue, queue.submit(r))[-1].entries[0]).seed for r in reqs]
    assert sent == stored == [8, 8, 9, 7]


def test_never_overwrites(tmp_path):
    src = tmp_path / "s.png"
    src.write_bytes(b"new")
    first = runner._save_new(src, tmp_path, 1)
    second = runner._save_new(src, tmp_path, 1)
    assert first != second and second.stem.endswith("-1")


def test_bar_reads_img2img_step_count():
    units, count = runner._units(["--steps", "20", "--batch-count", "2"])
    bar = runner._Bar(count)
    assert (units, count) == (40, 2)
    # img2img at strength 0.3: the engine samples 6 of 20 steps per image
    got = [int(bar.fraction(k, 6) * units) for k in (1, 3, 6)]
    assert got == [3, 10, 20] and bar.fraction(2, 9) == 0  # a tiling bar is not a step
    assert runner._units(["--sigmas", "qwen-image-2.1-turbo"]) == (8, 1)
    assert runner._units(["--mode", "upscale"]) == (1, 1)


def test_shutdown_twice(stub, tmp_path):
    q = runner.Queue(output_dir=lambda: tmp_path)
    q.shutdown()
    q.shutdown()
    with pytest.raises(LatentroomError):
        q.submit(JobRequest(model_id="std-q4", prompt="x"))
    with pytest.raises(LatentroomError):
        q.run_exclusive(lambda: 1)


def test_run_exclusive_never_next_to_a_picture_job(queue, monkeypatch):
    inside, release = threading.Event(), threading.Event()

    def enhance():
        inside.set()
        assert release.wait(10)
        return "a prompt"

    out = []
    t = threading.Thread(target=lambda: out.append(queue.run_exclusive(enhance)))
    t.start()
    assert inside.wait(10)
    for fn, message in [
        (lambda: queue.submit(JobRequest(model_id="std-q4", prompt="x")), "prompt"),
        (lambda: queue.run_exclusive(lambda: 1), "prompt"),  # two clicks, one GPU
    ]:
        with pytest.raises(LatentroomError) as e:
            fn()
        assert (e.value.code, e.value.status, e.value.message) == (
            "busy", 409, f"Wait for the {message} to finish."
        )  # fmt: skip
    assert queue.jobs() == [] and queue.events() == []
    release.set()
    t.join(10)
    assert out == ["a prompt"]

    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    job = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=40))  # free again
    with pytest.raises(LatentroomError) as e:
        queue.run_exclusive(lambda: pytest.fail("ran during a picture job"))
    assert (e.value.code, e.value.message) == ("busy", "Wait for the picture to finish.")
    assert queue.cancel(job) and wait(queue, job)[-1].type == "cancelled"
    end = time.time() + 10
    while queue.jobs():  # "cancelled" is sent just before the worker lets go of the job
        assert time.time() < end
        time.sleep(0.02)
    with pytest.raises(ZeroDivisionError):  # a failing fn frees the queue too
        queue.run_exclusive(lambda: 1 / 0)
    assert queue.run_exclusive(lambda: "again") == "again"


def test_the_pc_stays_awake_while_a_job_runs_and_not_after(queue, monkeypatch):
    asked = []  # (flags, thread) of every keep-awake call, the Windows call itself left out
    monkeypatch.setattr(awake, "_set", lambda flags: asked.append((flags, threading.get_ident())))
    wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1)))
    monkeypatch.setenv("FAKE_FAIL", "boom")
    failed = wait(queue, queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1)))[-1]
    assert failed.type == "failed"
    end = time.time() + 10
    while len(asked) < 4 and time.time() < end:  # the release comes just after the last event
        time.sleep(0.02)
    on, off = awake.ES_CONTINUOUS | awake.ES_SYSTEM_REQUIRED, awake.ES_CONTINUOUS
    assert [f for f, _ in asked] == [on, off, on, off]  # let go after the failed job too
    assert asked[0][1] == asked[1][1] != threading.get_ident()  # the job's own thread asks


def saved() -> list[tuple[str, bool, bool]]:
    """queue.json as (prompt, running, retried) per job."""
    jobs_ = json.loads(paths.queue_path().read_text(encoding="utf-8"))
    return [(j["request"]["prompt"], j["running"], j["retried"]) for j in jobs_]


def prompts(q: runner.Queue) -> list[str]:
    return [j["request"]["prompt"] for j in q.jobs()]


def running(q: runner.Queue, job_id: str) -> None:
    end = time.time() + 30
    while not any(e.type == "step" and e.job_id == job_id for e in q.events()):
        assert time.time() < end
        time.sleep(0.05)


def test_the_queue_is_on_disk_until_it_empties_or_latentroom_closes(queue, monkeypatch):
    """A crash or power cut loses no waiting job; a normal close leaves no prompt behind."""
    job = queue.submit(JobRequest(model_id="std-q4", prompt="x", steps=1))
    wait(queue, job)
    end = time.time() + 10
    while paths.queue_path().exists():  # deleted as the queue empties, just after "done"
        assert time.time() < end
        time.sleep(0.02)
    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    first = queue.submit(JobRequest(model_id="std-q4", prompt="a fox", steps=40))
    second = queue.submit(JobRequest(model_id="std-q4", prompt="a cat", steps=40))
    running(queue, first)
    assert saved() == [("a fox", True, False), ("a cat", False, False)]
    assert queue.cancel(second) and saved() == [("a fox", True, False)]
    queue.shutdown()
    assert not paths.queue_path().exists()


def test_restore_after_a_crash(stub, tmp_path, monkeypatch, caplog):
    """Queued again in order, the running job first. A job that no longer passes the checks
    is skipped; the running job is tried once more, then dropped. No prompt in the log."""
    monkeypatch.setenv("FAKE_STEP_S", "0.5")
    req = JobRequest(model_id="std-q4", steps=40)
    crash = [
        {"request": replace(req, prompt="secret one").to_dict(), "running": True, "retried": False},
        {"request": replace(req, prompt="secret bad", width=5).to_dict(), "running": False},
        {"request": replace(req, prompt="secret two").to_dict(), "running": False},
        {"request": replace(req, prompt="secret three").to_dict(), "running": False},
    ]
    paths.queue_path().write_text(json.dumps(crash), encoding="utf-8")
    q = runner.Queue(output_dir=lambda: tmp_path)
    assert q.restore() == 3
    assert prompts(q) == ["secret one", "secret two", "secret three"]
    running(q, q.jobs()[0]["id"])
    assert saved() == [("secret one", True, True), ("secret two", False, False),
                       ("secret three", False, False)]  # fmt: skip
    again = paths.queue_path().read_text(encoding="utf-8")  # what a second crash leaves
    q.shutdown()
    paths.queue_path().write_text(again, encoding="utf-8")
    q = runner.Queue(output_dir=lambda: tmp_path)
    try:
        assert q.restore() == 2  # "secret one" was running at both crashes
        assert prompts(q) == ["secret two", "secret three"]
    finally:
        q.shutdown()
    assert "bad_request" in caplog.text and "dropped" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("text", ["not json{", '[{"no": "request"}]', '{"request": {}}'])
def test_a_broken_saved_queue_is_ignored(stub, tmp_path, caplog, text):
    paths.queue_path().write_text(text, encoding="utf-8")
    q = runner.Queue(output_dir=lambda: tmp_path)
    try:
        assert q.restore() == 0 and q.jobs() == []
    finally:
        q.shutdown()
    assert not paths.queue_path().exists() and "could not be read" in caplog.text


def test_a_queued_upload_survives_the_start_sweep(stub):
    from latentroom.web import server

    used, unused = upload(f"{'c' * 32}.png"), upload(f"{'d' * 32}.png")
    req = JobRequest(kind="img2img", model_id="std-q4", prompt="x", init_image=used)
    job = {"request": req.to_dict(), "running": False, "retried": False}
    paths.queue_path().write_text(json.dumps([job]), encoding="utf-8")
    assert server.sweep_uploads() == 1
    assert Path(used).exists() and not Path(unused).exists()
