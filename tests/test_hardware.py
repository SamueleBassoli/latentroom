import os
import subprocess
import sys
import types

import pytest

from latentroom.engine import hardware
from latentroom.engine.hardware import Gpu

GiB = 1024**3
RTX4060 = Gpu("NVIDIA GeForce RTX 4060", 8 * GiB, 7 * GiB)


class _NoLib(Exception):
    pass


def _fail(exc):
    def probe():
        raise exc

    return probe


@pytest.fixture
def ram(monkeypatch):
    monkeypatch.setattr(hardware, "_ram", lambda: (32 * GiB, 20 * GiB))


def test_ok_card(monkeypatch, ram):
    monkeypatch.setattr(hardware, "_nvml", lambda: (RTX4060, "581.42", 1))
    hw = hardware.detect()
    assert hw.status == "ok" and hw.gpu == RTX4060 and hw.driver == "581.42"
    assert hw.message == "Your PC: RTX 4060 with 8 GB of video memory and 32 GB of RAM."
    assert (hw.ram_total, hw.ram_free) == (32 * GiB, 20 * GiB)
    assert hw.gpus == 1 and hw.warning == ""


@pytest.mark.parametrize(
    ("name", "warned"),
    [
        ("NVIDIA GeForce GTX 1660 SUPER", True),
        ("Quadro P4000", True),
        ("NVIDIA RTX A4000", False),  # workstation RTX counts
        ("NVIDIA GeForce RTX 5090", False),
    ],
)
def test_a_card_without_rtx_is_warned_not_stopped(monkeypatch, ram, name, warned):
    monkeypatch.setattr(hardware, "_nvml", lambda: (Gpu(name, 6 * GiB, 5 * GiB), "581.42", 1))
    hw = hardware.detect()
    assert hw.status == "ok"  # setup goes on
    assert hw.warning == (hardware.NOT_RTX if warned else "")


def test_one_size_unit_for_the_user():
    """1 GB = 1024^3 bytes in every message, as Windows Task Manager and Explorer show
    (was 1e9: 32 GB of RAM read "34.4 GB"); amounts Latentroom needs round up."""
    assert hardware.gb(34359738368) == "32 GB"  # 32 GB of RAM
    assert hardware.gb(12282 * 1024**2) == "12 GB"  # a 12 GB RTX 4070 SUPER, as NVML reports it
    assert hardware.gb(6 * GiB) == "6 GB"  # same figure in the hardware line and in fit
    four2 = int(4.2 * GiB)
    assert hardware.gb(four2) == hardware.gb(four2, up=True) == "4.2 GB"
    assert hardware.gb(four2 + GiB // 100, up=True) == "4.3 GB"
    assert hardware.gb(-1, up=True) == "0 GB"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_ram_total_is_the_installed_ram_task_manager_shows():
    """Task Manager shows the installed RAM (32 GB), not the part Windows can use
    (31.9 GB here, after hardware reserved): installed memory comes in whole MiB."""
    total, free = hardware._ram()
    assert total % 1024**2 == 0 and total > free > 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_drive_reads_the_file_system_of_a_folder_not_made_yet(tmp_path):
    fs, network = hardware.drive(tmp_path / "not" / "made")
    assert fs in ("NTFS", "ReFS") and network is False  # the test PC's local drive


def test_old_driver_says_update(monkeypatch, ram):
    monkeypatch.setattr(hardware, "_nvml", lambda: (RTX4060, "560.94", 1))
    hw = hardware.detect()
    assert hw.status == "driver_too_old" and hw.gpu == RTX4060
    assert "560.94" in hw.message and hardware.DRIVER_URL in hw.message


def test_no_nvidia(monkeypatch, ram):
    monkeypatch.setattr(hardware, "_nvml", _fail(hardware._NoNvidia()))
    monkeypatch.setattr(hardware, "_smi_exe", lambda: None)
    hw = hardware.detect()
    assert hw.status == "no_nvidia" and hw.gpu is None and hw.driver is None


def test_broken_nvml_falls_back_to_nvidia_smi(monkeypatch, ram):
    monkeypatch.setattr(hardware, "_nvml", _fail(RuntimeError("nvml exploded")))
    monkeypatch.setattr(hardware, "_smi_exe", lambda: "C:/fake/nvidia-smi.exe")
    out = (
        "NVIDIA GeForce RTX 3060, 12288, 11000, 566.36\n"
        "NVIDIA GeForce GTX 1050, 2048, 2000, 566.36\n"
    )
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out, stderr=""),
    )
    hw = hardware.detect()
    assert hw.status == "driver_too_old"  # 566.36 < 572.61
    assert hw.gpu == Gpu("NVIDIA GeForce RTX 3060", 12288 * 1024**2, 11000 * 1024**2)
    assert hw.gpus == 2  # the first card is the one used; the report counts both


def test_nvidia_smi_only_from_the_driver_folders(monkeypatch, tmp_path):
    """A nvidia-smi.bat planted in the current folder or on PATH never runs."""
    planted = tmp_path / "Downloads"
    planted.mkdir()
    (planted / "nvidia-smi.bat").write_text("@echo PLANTED GPU, 4096, 4096, 999.99")
    (planted / "nvidia-smi.exe").write_bytes(b"MZ")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", str(planted) + os.pathsep + os.environ.get("PATH", ""))
    for env in ("SystemRoot", "ProgramFiles"):
        (tmp_path / env).mkdir()
        monkeypatch.setenv(env, str(tmp_path / env))
    ran = []
    out = "NVIDIA GeForce RTX 3060, 12288, 11000, 581.42\n"
    monkeypatch.setattr(
        hardware.subprocess,
        "run",
        lambda cmd, **k: ran.append(cmd[0]) or subprocess.CompletedProcess(cmd, 0, out, ""),
    )
    with pytest.raises(hardware._NoNvidia):
        hardware._smi()
    assert ran == []

    nvsmi = tmp_path / "ProgramFiles" / "NVIDIA Corporation" / "NVSMI"
    nvsmi.mkdir(parents=True)
    (nvsmi / "nvidia-smi.exe").write_bytes(b"MZ")
    assert hardware._smi()[1] == "581.42" and ran == [nvsmi / "nvidia-smi.exe"]
    system32 = tmp_path / "SystemRoot" / "System32"
    system32.mkdir()
    (system32 / "nvidia-smi.exe").write_bytes(b"MZ")
    hardware._smi()
    assert ran[-1] == system32 / "nvidia-smi.exe"


def test_broken_everything_is_error_not_crash(monkeypatch, ram):
    monkeypatch.setattr(hardware, "_nvml", _fail(RuntimeError("nvml exploded")))
    monkeypatch.setattr(hardware, "_smi", _fail(subprocess.TimeoutExpired("nvidia-smi", 15)))
    hw = hardware.detect()
    assert hw.status == "error" and hw.gpu is None and "driver" in hw.message


def test_nvml_without_library_means_no_nvidia(monkeypatch):
    fake = types.SimpleNamespace(
        NVMLError_LibraryNotFound=_NoLib, NVMLError_DriverNotLoaded=_NoLib, nvmlInit=_fail(_NoLib())
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    with pytest.raises(hardware._NoNvidia):
        hardware._nvml()


def test_nvml_reads_first_gpu(monkeypatch):
    calls = []
    fake = types.SimpleNamespace(
        NVMLError_LibraryNotFound=_NoLib,
        NVMLError_DriverNotLoaded=_NoLib,
        nvmlInit=lambda: calls.append("init"),
        nvmlShutdown=lambda: calls.append("shutdown"),
        nvmlDeviceGetCount=lambda: 2,
        nvmlDeviceGetHandleByIndex=lambda i: f"gpu{i}",
        nvmlDeviceGetName=lambda h: "NVIDIA GeForce RTX 4060",
        nvmlDeviceGetMemoryInfo=lambda h: types.SimpleNamespace(total=8 * GiB, free=7 * GiB),
        nvmlSystemGetDriverVersion=lambda: "581.42",
    )
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    assert hardware._nvml() == (RTX4060, "581.42", 2)  # card 0, and how many there are
    assert calls == ["init", "shutdown"]


def test_real_detect_never_raises():
    hw = hardware.detect()  # NVML / nvidia-smi only, no CUDA: safe on any PC
    assert hw.status in ("ok", "no_nvidia", "driver_too_old", "error") and hw.message


def test_free_disk_of_a_folder_that_does_not_exist_yet(tmp_path):
    assert hardware.free_disk(tmp_path / "not" / "yet") > 0
