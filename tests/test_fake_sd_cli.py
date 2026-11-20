import os
import re
import struct
import subprocess
from pathlib import Path

STEP_RE = re.compile(r"\|[=> ]*\|\s+(\d+)/(\d+) - ")  # sampling bar ('=' + '>'), not '#' loading


def run(cmd: list[str], *args: str, **env: str) -> subprocess.CompletedProcess:
    # bytes, then decode: text=True would turn every progress '\r' into '\n'
    r = subprocess.run(
        [*cmd, *args], capture_output=True, env={**os.environ, **env}, timeout=60, check=False
    )
    r.stdout, r.stderr = r.stdout.decode(), r.stderr.decode()
    return r


def png_info(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data


def steps_seen(stdout: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in STEP_RE.findall(stdout)]


def test_txt2img_progress_and_png(fake_engine, tmp_path):
    out = tmp_path / "out.png"
    r = run(
        fake_engine,
        "-p",
        "a lighthouse at dusk",
        "--steps",
        "5",
        "-W",
        "64",
        "-H",
        "32",
        "-o",
        str(out),
    )
    assert r.returncode == 0, r.stderr
    assert steps_seen(r.stdout) == [(i, 5) for i in range(1, 6)]
    assert "\r" in r.stdout and "#" in r.stdout  # carriage-return bars + loading bar
    width, height, data = png_info(out)
    assert (width, height) == (64, 32)
    assert b"a lighthouse at dusk" in data  # like the real CLI: prompt embedded by default


def test_disable_metadata_and_batch_naming(fake_engine, tmp_path):
    r = run(
        fake_engine,
        "-p",
        "a lighthouse at dusk",
        "--steps",
        "2",
        "-W",
        "32",
        "-H",
        "32",
        "-b",
        "2",
        "--disable-image-metadata",
        "-o",
        str(tmp_path / "out.png"),
    )
    assert r.returncode == 0, r.stderr
    assert len(steps_seen(r.stdout)) == 4
    for name in ("out_0.png", "out_1.png"):
        assert b"lighthouse" not in png_info(tmp_path / name)[2]
    r = run(
        fake_engine,
        "--steps",
        "1",
        "-W",
        "32",
        "-H",
        "32",
        "-b",
        "2",
        "-o",
        str(tmp_path / "img_%03d.png"),
    )
    assert r.returncode == 0 and (tmp_path / "img_001.png").is_file()


def test_turbo_sigmas_preset_sets_8_steps(fake_engine, tmp_path):
    r = run(
        fake_engine,
        "--sigmas",
        "qwen-image-2.1-turbo",
        "--steps",
        "25",
        "-W",
        "32",
        "-H",
        "32",
        "-o",
        str(tmp_path / "t.png"),
    )
    assert r.returncode == 0, r.stderr
    assert steps_seen(r.stdout)[-1] == (8, 8)


def test_lora_applied_line(fake_engine, tmp_path):
    (tmp_path / "test-style-a.safetensors").write_bytes(b"x")
    r = run(
        fake_engine,
        "-p",
        "a lighthouse at dusk<lora:test-style-a:0.8>",
        "--steps",
        "1",
        "--lora-model-dir",
        str(tmp_path),
        "-W",
        "32",
        "-H",
        "32",
        "-o",
        str(tmp_path / "o.png"),
    )
    assert r.returncode == 0, r.stderr
    applied = re.findall(r"\((\d+) / (\d+)\) LoRA tensors have been applied", r.stdout)
    assert len(applied) == 2  # real CLI may print it twice per LoRA: parsers must de-duplicate


def test_missing_lora_warns_and_still_makes_image(fake_engine, tmp_path):
    r = run(
        fake_engine,
        "-p",
        "a lighthouse at dusk<lora:test-style-b:1>",
        "--steps",
        "1",
        "--lora-model-dir",
        str(tmp_path),
        "-W",
        "32",
        "-H",
        "32",
        "-o",
        str(tmp_path / "o.png"),
    )
    assert r.returncode == 0 and (tmp_path / "o.png").is_file()
    assert "[W] can not found lora " in r.stdout and "test-style-b" in r.stdout


def test_eta_suffix_when_run_is_long(fake_engine, tmp_path):
    r = run(
        fake_engine,
        "--steps",
        "4",
        "-W",
        "32",
        "-H",
        "32",
        "-o",
        str(tmp_path / "o.png"),
        FAKE_REPORT_S="5",
    )
    assert r.returncode == 0, r.stderr
    assert "5.00s/it - ETA 00:15" in r.stdout and steps_seen(r.stdout)[-1] == (4, 4)


def test_preview_file_written(fake_engine, tmp_path):
    prev = tmp_path / "preview.png"
    r = run(
        fake_engine,
        "--steps",
        "2",
        "-W",
        "64",
        "-H",
        "64",
        "--preview",
        "proj",
        "--preview-path",
        str(prev),
        "-o",
        str(tmp_path / "o.png"),
    )
    assert r.returncode == 0, r.stderr
    assert png_info(prev)[:2] == (8, 8)


def test_adetailer_mode_keeps_size(fake_engine, tmp_path):
    src = tmp_path / "src.png"
    assert run(fake_engine, "--steps", "1", "-W", "32", "-H", "16", "-o", str(src)).returncode == 0
    r = run(
        fake_engine,
        "-M",
        "adetailer",
        "-i",
        str(src),
        "--ad-model",
        "face.pt",
        "-o",
        str(tmp_path / "fixed.png"),
    )
    assert r.returncode == 0, r.stderr
    assert png_info(tmp_path / "fixed.png")[:2] == (32, 16)


def test_upscale_mode_is_4x(fake_engine, tmp_path):
    src = tmp_path / "src.png"
    assert run(fake_engine, "--steps", "1", "-W", "32", "-H", "16", "-o", str(src)).returncode == 0
    r = run(
        fake_engine,
        "-M",
        "upscale",
        "-i",
        str(src),
        "--upscale-model",
        "x4.pth",
        "-o",
        str(tmp_path / "big.png"),
    )
    assert r.returncode == 0, r.stderr
    assert png_info(tmp_path / "big.png")[:2] == (128, 64)


def test_fail_env_and_oom_text(fake_engine, tmp_path):
    out = tmp_path / "o.png"
    r = run(fake_engine, "--steps", "3", "-o", str(out), FAKE_FAIL="oom")
    assert r.returncode != 0
    assert "out of memory" in r.stderr and r.stderr.startswith("[E] ")
    assert not out.exists()
    r = run(fake_engine, "--steps", "3", "-o", str(out), FAKE_FAIL="1")
    assert r.returncode != 0 and not out.exists()


def test_unknown_flag_rejected_like_real_cli(fake_engine):
    r = run(fake_engine, "--stepz", "3")
    assert r.returncode == 1
    assert "[E] error: unknown argument: --stepz" in r.stderr


def test_missing_input_image_fails(fake_engine, tmp_path):
    r = run(fake_engine, "-r", str(tmp_path / "nope.png"), "-o", str(tmp_path / "o.png"))
    assert r.returncode == 1
