import subprocess
import sys
from pathlib import Path

import fake_catalogue
import pytest
from PIL import Image

from latentroom.engine import history, jobs, paths, runner
from latentroom.engine.jobs import JobRequest, LoraRef
from latentroom.errors import LatentroomError


@pytest.fixture
def stub(monkeypatch):
    return fake_catalogue.install_stub(monkeypatch)


def upload(name="in.png", size=(1000, 700)) -> str:
    p = paths.uploads_dir() / name
    Image.new("RGB", size, (90, 120, 150)).save(p)
    return str(p)


def opts(argv: list[str]) -> dict:
    return dict(zip(argv, argv[1:], strict=False))


def check(req: JobRequest, code="bad_request", text=""):
    with pytest.raises(LatentroomError) as e:
        jobs.validate(req)
    assert e.value.code == code and text in e.value.message, e.value.message
    return e.value


def fake_accepts(argv: list[str], tmp_path: Path) -> None:
    """The fake engine rejects unknown flags exactly like the real sd-cli."""
    r = subprocess.run(
        [sys.executable, str(fake_catalogue.FAKE_SD_CLI), *argv],
        capture_output=True, text=True, env={"FAKE_STEP_S": "0"}, cwd=tmp_path,
    )  # fmt: skip
    assert "unknown argument" not in r.stderr and r.returncode == 0, r.stderr


def test_txt2img_argv(stub, tmp_path):
    (paths.loras_dir() / "style-a.safetensors").write_bytes(b"x")
    req = JobRequest(
        model_id="std-q4", prompt="a fox <lora:C:/evil:1> at dawn", negative="blur",
        width=768, height=512, seed=7, steps=12, cfg=5, loras=[LoraRef("style-a", 0.6)],
    )  # fmt: skip
    jobs.validate(req)
    [argv] = jobs.build(req, tmp_path / "job")
    o = opts(argv)
    assert o["--diffusion-model"] == str(paths.models_dir() / "std-Q4_K.gguf")
    assert o["--vae"].endswith("vae.safetensors") and o["--llm"].endswith("llm.gguf")
    assert "--llm_vision" not in o  # only edit needs the vision encoder
    assert o["--prompt"] == "a fox  at dawn <lora:style-a:0.6>"  # typed tag removed
    assert o["--lora-model-dir"] == str(paths.loras_dir())
    assert (o["--steps"], o["--cfg-scale"], o["--negative-prompt"]) == ("12", "5", "blur")
    assert (o["--width"], o["--height"], o["--seed"], o["--auto-fit"]) == ("768", "512", "7", "on")
    assert "--disable-image-metadata" in argv and "--batch-count" not in o
    assert o["--output"] == str(tmp_path / "job" / "out.png")
    fake_accepts(argv, tmp_path)


def test_video_memory_limit_goes_to_the_engine_in_gib_as_is(stub, tmp_path):
    """sd-cli --max-vram takes GiB (its --help at the pinned e779d4f: "per-device budget
    in GiB"), the GB the user picks in Settings (SPEC "Sizes"): no conversion; 0 = no flag."""
    req = JobRequest(model_id="std-q4", prompt="a lighthouse at dusk", max_vram_gb=7.5)
    jobs.validate(req)
    [argv] = jobs.build(req, tmp_path / "a")
    assert opts(argv)["--max-vram"] == "7.5"
    fake_accepts(argv, tmp_path)
    req.max_vram_gb = 0
    assert "--max-vram" not in jobs.build(req, tmp_path / "b")[0]


def test_turbo_on_standard_and_turbo_model(stub, tmp_path):
    req = JobRequest(model_id="std-q4", prompt="cat", turbo=True, negative="dog", batch=3)
    jobs.validate(req)
    o = opts(jobs.build(req, tmp_path / "a")[0])
    assert (o["--sigmas"], o["--cfg-scale"], o["--sampling-method"]) == (
        "qwen-image-2.1-turbo", "1", "euler",
    )  # fmt: skip
    assert "--steps" not in o and "--negative-prompt" not in o  # cfg 1: negative is ignored
    assert o["--prompt"] == "cat <lora:../models/turbo-lora:1>"
    assert o["--batch-count"] == "3"
    req = JobRequest(model_id="turbo-q4", prompt="cat", turbo=True)
    o = opts(jobs.build(req, tmp_path / "b")[0])
    assert o["--sigmas"] == "qwen-image-2.1-turbo" and o["--prompt"] == "cat"
    assert "--lora-model-dir" not in o
    stub.installed.discard("turbo-lora")
    check(JobRequest(model_id="std-q4", prompt="x", turbo=True), "model_missing", "Turbo")


def test_counter_total_is_steps_times_frames(stub, tmp_path):
    for i, (steps, turbo, frames, total) in enumerate(((15, False, 2, 30), (8, True, 3, 24))):
        req = JobRequest(model_id="std-q4", prompt="cat", steps=steps, turbo=turbo, batch=frames)
        jobs.validate(req)
        [argv] = jobs.build(req, tmp_path / str(i))
        assert runner._units(argv) == (total, frames)  # the tray shows "k / total ÷ frames"


def test_edit_prepares_refs(stub, tmp_path):
    refs = [upload("a.png", (3000, 2000)), upload("b.png", (300, 200))]
    req = JobRequest(kind="edit", model_id="std-q4", prompt="swap", refs=refs)
    jobs.validate(req)
    argv = jobs.build(req, tmp_path / "job")[0]
    # sd.cpp's qwen preset already numbers refs <image1>..<imageN>; the flag is deprecated
    assert argv.count("--ref-image") == 2 and "--increase-ref-index" not in argv
    assert opts(argv)["--llm_vision"].endswith("mmproj.gguf")
    with Image.open(tmp_path / "job" / "ref_1.png") as im:
        assert im.size == (1024, 683)
    fake_accepts(argv, tmp_path)
    check(JobRequest(kind="edit", model_id="std-q4", prompt="x"), text="between 1 and 6")
    check(JobRequest(kind="edit", model_id="plain-q4", prompt="x", refs=refs[:1]), "not_available")


def test_img2img_inpaint_sizes(stub, tmp_path):
    init = upload("in.png", (1000, 700))
    req = JobRequest(kind="img2img", model_id="std-q4", prompt="oil", init_image=init, strength=0.5)
    jobs.validate(req)
    assert jobs.job_size(req) == (992, 672)  # the picture's own size, snapped to 32
    o = opts(jobs.build(req, tmp_path / "a")[0])
    assert (o["--width"], o["--height"], o["--strength"]) == ("992", "672", "0.5")
    with Image.open(o["--init-img"]) as im:
        assert im.size == (992, 672)
    mask = paths.uploads_dir() / "m.png"
    Image.new("L", (1000, 700), 0).save(mask)
    req = JobRequest(kind="inpaint", model_id="std-q4", prompt="x", init_image=init, mask=str(mask))
    jobs.validate(req)
    argv = jobs.build(req, tmp_path / "b")[0]
    with Image.open(opts(argv)["--mask"]) as im:
        assert im.size == (992, 672)
    fake_accepts(argv, tmp_path)
    check(JobRequest(kind="inpaint", model_id="std-q4", prompt="x", init_image=init), text="Paint")


def test_upscale_and_enhance(stub, tmp_path):
    init = upload("small.png", (512, 384))
    req = JobRequest(kind="upscale", init_image=init)
    jobs.validate(req)  # no model needed
    [argv] = jobs.build(req, tmp_path / "u")
    assert opts(argv)["--mode"] == "upscale" and "--diffusion-model" not in argv
    fake_accepts(argv, tmp_path)
    req = JobRequest(kind="enhance", model_id="std-q4", init_image=init, strength=0.9, seed=3)
    jobs.validate(req)
    up, gen = jobs.build(req, tmp_path / "e")
    assert opts(up)["--output"].endswith("up.png")
    o = opts(gen)
    assert o["--init-img"] == opts(up)["--output"] and o["--strength"] == "0.35"
    assert (o["--width"], o["--height"]) == ("1024", "768")  # 2x the original
    stub.installed.discard("esrgan-x4")
    check(req, "model_missing", "upscaler")
    big = upload("big.png", (2100, 300))
    check(JobRequest(kind="upscale", init_image=big), text="too large")


def test_facefix(stub, tmp_path):
    init = upload("face.png", (640, 640))
    req = JobRequest(kind="facefix", model_id="std-q4", init_image=init, strength=0.75)
    jobs.validate(req)
    [argv] = jobs.build(req, tmp_path / "f")
    o = opts(argv)
    assert o["--mode"] == "adetailer" and o["--ad-model"].endswith("face_yolov8n.safetensors")
    assert o["--strength"] == "0.4" and o["--prompt"] == jobs.FACEFIX_PROMPT
    assert "--ad-prompt" not in o  # optional override: empty = inherit --prompt (docs/adetailer.md)
    fake_accepts(argv, tmp_path)
    stub.installed.discard("face")
    check(req, "model_missing", "face detector")
    fake_catalogue.without(stub, "detector")
    check(req, "not_available", "Fix faces")  # Develop's name for it


def test_every_diffusion_run_memory_maps_the_model(stub, tmp_path):
    """--mmap loads the model far faster (live: 1024² 313 s -> 86 s). The
    upscale-only run gets none: sd-cli --mode upscale never makes the sd context that reads it
    (main.cpp:888 at e779d4f) and new_upscaler_ctx() has no mmap parameter."""
    init, mask = upload("in.png", (640, 512)), paths.uploads_dir() / "m.png"
    Image.new("L", (640, 512), 0).save(mask)
    refs, base = [upload("r.png", (300, 200))], {"model_id": "std-q4", "prompt": "a lighthouse"}
    runs = []
    for kind in jobs.KINDS:
        req = JobRequest(kind=kind, init_image=init, mask=str(mask), **base)
        req.refs = refs if kind == "edit" else []  # refs on other kinds are rejected
        jobs.validate(req)
        runs += jobs.build(req, tmp_path / kind)
    assert len(runs) == len(jobs.KINDS) + 1  # enhance: upscale, then img2img
    for argv in runs:
        assert argv.count("--mmap") == ("--diffusion-model" in argv), argv
    assert sum("--mmap" in argv for argv in runs) == len(jobs.KINDS) - 1
    fake_accepts(runs[0], tmp_path)


def test_validate_messages(stub, tmp_path):
    check(JobRequest(model_id="std-q4", prompt="x", width=1000), text="Width")
    e = check(JobRequest(model_id="std-q4", prompt="x", height=1000), text="multiple of 32")
    assert e.message.startswith("Height") and "992 and 1024" in e.message
    check(JobRequest(model_id="std-q4", prompt="x", width=4000), text="between 256 and 2048")
    check(JobRequest(model_id="std-q4", prompt="x", height=4096), text="between 256 and 2048")
    check(JobRequest(model_id="std-q4", prompt="x", width=True), text="Width")
    check(JobRequest(model_id="std-q4", prompt="x", steps=0), text="Steps")
    check(JobRequest(model_id="std-q4", prompt="x", cfg=99), text="Guidance")
    check(JobRequest(model_id="std-q4", prompt="x", batch=9), text="Number of pictures")
    check(JobRequest(model_id="std-q4", prompt="x", sampler="nope"), text="sampler")
    check(JobRequest(model_id="std-q4", prompt="<lora:a:1>  "), text="Describe")
    check(JobRequest(kind="paint", model_id="std-q4", prompt="x"), text="Reload")
    check(JobRequest(model_id="std-q4", prompt="x", loras=[LoraRef("ghost")]), text="ghost")
    ref = upload("r.png", (300, 200))  # refs outside edit were silently dropped
    for kind in set(jobs.KINDS) - {"edit"}:
        req = JobRequest(kind=kind, model_id="std-q4", prompt="x", refs=[ref], init_image=ref)
        check(req, text='only work with "Edit with references"')
    check(JobRequest(model_id="std-q4", prompt="x", refs="r.png"), text="Edit with references")
    jobs.validate(JobRequest(model_id="std-q4", prompt="x", refs=[]))  # what the web form sends
    check(JobRequest(model_id="nope", prompt="x"), "model_missing")
    check(JobRequest(model_id="", prompt="x"), "model_missing")
    stub.installed.discard("vae")
    check(JobRequest(model_id="std-q4", prompt="x"), "model_missing", "not downloaded")


def test_input_images_only_from_allowed_places(stub, tmp_path):
    outside = tmp_path / "elsewhere.png"
    Image.new("RGB", (300, 300)).save(outside)
    req = JobRequest(kind="img2img", model_id="std-q4", prompt="x", init_image=str(outside))
    check(req, text="uploaded or made")
    sneaky = str(paths.uploads_dir() / ".." / ".." / outside.name)  # resolves outside uploads/
    check(JobRequest(kind="upscale", init_image=sneaky), text="uploaded or made")
    jobs.validate(req, output_dir=tmp_path)  # the output folder is allowed
    entry = history.add(req, outside, 1)  # and so is any history image
    assert history.has_image(Path(entry.image))
    jobs.validate(req)
    check(JobRequest(kind="upscale", init_image="relative.png"), text="Pick a picture")
    broken = paths.uploads_dir() / "broken.png"
    broken.write_bytes(b"not a png")
    check(JobRequest(kind="upscale", init_image=str(broken)), text="not a picture")


NETWORK = [r"\\evil\share\x.png", "//evil/share/x.png", r"\\?\UNC\evil\share\x.png", r"\\.\C:\x"]


def test_network_paths_refused_before_any_disk_access(stub, tmp_path, monkeypatch):
    # resolving \\host\share makes Windows connect to that host and send it the user's
    # login (NTLM hash); a phone or page could name one in init_image, refs or mask
    def net(p) -> bool:
        return str(p).replace("/", "\\").startswith("\\\\")

    touched, real = [], Path.resolve
    monkeypatch.setattr(  # the old code reached the network here; the test never does
        Path, "resolve", lambda p, strict=False: touched.append(p) or (p if net(p) else real(p))
    )
    ok = upload()
    for bad in NETWORK:
        check(JobRequest(kind="upscale", init_image=bad), text="uploaded or made")
        req = JobRequest(kind="edit", model_id="std-q4", prompt="x", refs=[ok, bad])
        check(req, text="uploaded or made")
        req = JobRequest(kind="inpaint", model_id="std-q4", prompt="x", init_image=ok, mask=bad)
        check(req, text="uploaded or made")
        jobs.validate(JobRequest(kind="upscale", init_image=ok), output_dir=tmp_path)
    assert touched and not [p for p in touched if net(p)]
    assert all(jobs.is_network_path(b) for b in NETWORK)
    assert not jobs.is_network_path(ok) and not jobs.is_network_path("C:/x/y.png")
    # an output folder the user put on a NAS stays usable: decided by text, nothing opened
    nas = Path(r"\\nas\pics")
    assert jobs._in_folder(r"\\NAS\Pics\latentroom-1.png", nas)
    assert not jobs._in_folder(r"\\nas\pics\..\..\evil\s\x.png", nas)
    assert not jobs._in_folder(r"\\nas\picsX\a.png", nas)
