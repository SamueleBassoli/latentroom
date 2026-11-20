"""Hardening: locked renames, Windows blocks, exit codes."""

import os
import subprocess
import sys
import time

import pytest

from latentroom.engine import paths, procs
from latentroom.errors import LatentroomError


def refusing(times: int, error=lambda: PermissionError(13, "scanned by an antivirus")):
    """os.replace that raises `error()` the first `times` calls, then works. .calls counts."""
    real = os.replace

    def replace(src, dst):
        replace.calls += 1
        if replace.calls <= times:
            raise error()
        return real(src, dst)

    replace.calls = 0
    return replace


@pytest.mark.skipif(os.name != "nt", reason="WinError mapping")
@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_a_locked_file_is_a_permission_error(winerror):  # what paths.replace waits out
    assert isinstance(OSError(None, "locked", None, winerror), PermissionError)


def test_replace_waits_out_a_lock(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.part", tmp_path / "a.gguf"
    src.write_text("new")
    fake = refusing(3)
    monkeypatch.setattr(paths.os, "replace", fake)
    paths.replace(src, dst)
    assert fake.calls == 4 and dst.read_text() == "new" and not src.exists()


def test_replace_gives_up_after_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FILE_WAIT", 0.3)
    fake = refusing(10**6)
    monkeypatch.setattr(paths.os, "replace", fake)
    t0 = time.monotonic()
    with pytest.raises(PermissionError):
        paths.replace(tmp_path / "a", tmp_path / "b")
    assert 1 < fake.calls < 10 and time.monotonic() - t0 < 1


def test_a_folder_waits_longer_than_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FILE_WAIT", 0)
    monkeypatch.setattr(paths, "FOLDER_WAIT", 0.3)
    file, folder = refusing(10**6), refusing(10**6)
    for fake, is_folder in (file, False), (folder, True):
        monkeypatch.setattr(paths.os, "replace", fake)
        with pytest.raises(PermissionError):
            paths.replace(tmp_path / "a", tmp_path / "b", folder=is_folder)
    assert file.calls == 1 and folder.calls > 1


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    fake = refusing(1, lambda: FileNotFoundError(2, "gone"))
    monkeypatch.setattr(paths.os, "replace", fake)
    with pytest.raises(FileNotFoundError):
        paths.replace(tmp_path / "a", tmp_path / "b")
    assert fake.calls == 1


def blocked_popen(winerror: int):
    """subprocess.Popen as Windows answers when it refuses to run the exe."""

    def popen(argv, **kw):
        raise OSError(None, "blocked", argv[0], winerror)

    return popen


@pytest.mark.skipif(os.name != "nt", reason="WinError")
@pytest.mark.parametrize("winerror", [4551, 1260, 225, 226])
def test_a_windows_block_is_named(monkeypatch, caplog, winerror):
    monkeypatch.setattr(procs.subprocess, "Popen", blocked_popen(winerror))
    with pytest.raises(LatentroomError) as e:
        procs.start(["C:/data/engine/sd-cli.exe", "--help"])
    assert (e.value.code, e.value.status, e.value.message) == ("engine_blocked", 409, procs.BLOCKED)
    assert "Smart App Control" in procs.BLOCKED and "INSTALL.md" in procs.BLOCKED
    assert f"sd-cli.exe (WinError {winerror})" in caplog.text and "C:/data" not in caplog.text


def test_any_other_start_error_is_raised_as_before(tmp_path):
    with pytest.raises(FileNotFoundError):
        procs.start([str(tmp_path / "no-such.exe")])


def test_a_process_starts_as_with_popen():
    proc = procs.start([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL)
    assert proc.wait() == 0


@pytest.mark.parametrize(
    "code, text",
    [(1, "exit code 0x00000001"), (-1073741819, "exit code 0xC0000005"),
     (3221225477, "exit code 0xC0000005")],
)  # fmt: skip
def test_exit_text_reads_windows_crash_codes_in_hex(code, text):
    assert procs.exit_text(code) == text


def test_note_exit_ends_the_process_log_with_a_non_zero_code(tmp_path):
    log = tmp_path / "search.log"
    log.write_text("loading model\n")
    assert procs.note_exit(None, log) == "still running"
    assert procs.note_exit(0, log) == "exit code 0x00000000"
    assert log.read_text() == "loading model\n"  # a clean end adds nothing
    assert procs.note_exit(-1073741819, log) == "exit code 0xC0000005"
    assert log.read_text() == "loading model\n\nexit code 0xC0000005\n"
    assert procs.note_exit(1, tmp_path / "no" / "such.log") == "exit code 0x00000001"


@pytest.mark.parametrize("env", [None, {**os.environ, "LLAMA_API_KEY": "k"}])
def test_every_engine_numbers_the_cards_like_nvml(env):
    """CUDA counts the fastest card as 0, NVML (free VRAM, what fits) the first on the PCI
    bus: on a two-GPU PC the engine could run on another card than the one measured. An env
    the caller passes (the llama key) is kept."""
    code = "import os; print(os.environ.get('CUDA_DEVICE_ORDER'), os.environ.get('LLAMA_API_KEY'))"
    proc = procs.start([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
    out, _ = proc.communicate(timeout=30)
    assert out.split() == ["PCI_BUS_ID", "k" if env else str(os.environ.get("LLAMA_API_KEY"))]


PARENT = """import os, subprocess, sys, time
from latentroom.engine import procs
exe = getattr(sys, "_base_executable", sys.executable)  # one process, not a venv launcher
child = procs.start([exe, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL)
print(os.getpid(), child.pid, flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_the_engine_dies_with_latentroom():
    """Latentroom killed from Task Manager or crashed: the engine it started ends too
    (before, sd-cli or llama-server kept running and holding the video memory)."""
    from test_app import ROOT, _gone

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cmd = [sys.executable, "-c", PARENT]
    parent = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
    child = None
    try:
        app_pid, child = map(int, parent.stdout.readline().split())
        os.kill(app_pid, 9)  # TerminateProcess, like Task Manager's End task
        assert _gone(child), "the engine outlived Latentroom"
    finally:
        parent.kill()
        parent.wait(10)
        parent.stdout.close()
        if child:
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_a_failed_job_object_never_stops_the_engine(monkeypatch, caplog):
    def broken():
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(procs, "_job", None)
    monkeypatch.setattr(procs, "_new_job", broken)
    proc = procs.start([sys.executable, "-c", "pass"])
    assert proc.wait(30) == 0 and "not tied to Latentroom" in caplog.text
