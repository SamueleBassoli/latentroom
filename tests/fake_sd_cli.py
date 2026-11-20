"""Fake sd-cli for tests and QA: same argv and the same stdout/stderr format as the pinned
stable-diffusion.cpp CLI (release master-994-e779d4f), no GPU, no model files.
Every message below is copied from the real source.

Run: python tests/fake_sd_cli.py <sd-cli arguments>
Env: FAKE_STEP_S    seconds actually slept per sampling step (default 0.01)
     FAKE_REPORT_S  seconds per step the fake CLAIMS in its speed/ETA text (default FAKE_STEP_S);
                    >= 15 s per run makes the real " - ETA mm:ss" suffix appear, without waiting
     FAKE_FAIL      non-empty -> exit 1 after the first step; "oom", "alloc", "kernel", "lora",
                    "damaged" -> that real failure text; "driver", "nogpu" -> the CUDA init
                    failure, then more output; "blocked" -> exit 0xC0000135 (a DLL quarantined)
     FAKE_EMPTY     non-empty -> the pictures saved are one colour (a run that went black)
     FAKE_VERBOSE   non-empty -> act as if -v was given: print the parameters, prompts included
     FAKE_CHILD     a file path -> start a child process (sleeps 30 s), write its PID there;
                    lets a test prove that cancel kills the whole process tree
     FAKE_HOLD      a file path -> after sampling step 2, wait until that file exists (60 s max);
                    holds a job in "developing" for as long as a test needs, on any machine speed
                    (step 2: a reader gets the step 1 line only when the next '\r' arrives)

Like the real CLI it rejects unknown flags ("[E] error: unknown argument: X", exit 1), so a
typo in jobs.build() fails here too. Adding a flag: check it exists in the real CLI first,
then add it below.
"""

import os
import re
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

# Subset of the real option table that Latentroom may use, by value kind.
KINDS = {
    "str": "--mode --output --prompt --negative-prompt --sampling-method --scheduler --sigmas "
    "--model --diffusion-model --vae --llm --llm_vision --lora-model-dir --lora-apply-mode "
    "--init-img --mask --upscale-model --max-vram --log-level --flash-attn --rng --taesd "
    "--model-args --ad-model --ad-prompt --ad-negative-prompt --preview --preview-path",
    "int": "--seed --width --height --batch-count --steps --upscale-repeats --threads "
    "--preview-interval",
    "float": "--cfg-scale --strength",
    "list": "--ref-image --image-preprocess",
    "bool": "--increase-ref-index --offload-to-cpu --disable-image-metadata --verbose "
    "--vae-tiling --color --taesd-preview-only --mmap",
    "onoff": "--auto-fit",
}
FLAGS = {flag: kind for kind, flags in KINDS.items() for flag in flags.split()}
SHORT = dict(
    zip(
        "-M -o -p -n -s -W -H -b -m -r -i -v -t".split(),
        "--mode --output --prompt --negative-prompt --seed --width --height --batch-count "
        "--model --ref-image --init-img --verbose --threads".split(),
        strict=True,
    )
)
PREVIEWS = ("none", "proj", "tae", "vae")
SIGMA_PRESETS = {"qwen-image-2.1-turbo": 9}  # preset name -> number of sigmas (steps + 1)
LORA_RE = re.compile(r"<lora:([^:>]+):([^>]+)>")  # the real CLI's regex
LORA_EXT = (".gguf", ".safetensors", ".pt", ".ckpt")


def log(level: str, msg: str) -> None:
    # real log_print(): "[I] message --- file.cpp:line"; only [E] goes to stderr
    stream = sys.stderr if level == "E" else sys.stdout
    print(f"[{level}] {msg} --- fake_sd_cli.py:1", file=stream, flush=True)


def die(msg: str) -> None:
    log("E", msg)
    sys.exit(1)


def parse(argv: list[str]) -> dict:
    opts: dict = {"--ref-image": [], "--image-preprocess": []}
    i = 0
    while i < len(argv):
        name = SHORT.get(argv[i], argv[i])
        kind = FLAGS.get(name)
        if kind is None:
            die(f"error: unknown argument: {argv[i]}")
        if kind == "bool":
            opts[name] = True
            i += 1
            continue
        if i + 1 >= len(argv):
            die(f"error: missing argument for {argv[i]}")
        value = argv[i + 1]
        try:
            if kind == "int":
                value = int(value)
            elif kind == "float":
                value = float(value)
        except ValueError:
            die(f"error: invalid parameter for argument: {argv[i]}")
        if kind == "onoff" and value not in ("on", "off"):
            die(f"invalid {name} value '{value}'; expected 'on' or 'off'")
        if name == "--preview" and value not in PREVIEWS:
            die(f"error: invalid preview method {value}")
        if kind == "list":
            opts[name].append(value)
        else:
            opts[name] = value
        i += 2
    return opts


def png(width: int, height: int, text: str = "", flat: bool = False) -> bytes:
    """A small RGB PNG: one lighter first pixel (a real picture has more than one colour),
    or every pixel the same with `flat` (what the real CLI saves when a run goes black)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = bytes((40, 40, 48)) * width
    first = row if flat else bytes((200, 200, 200)) + row[3:]
    pixels = b"\x00" + first + (b"\x00" + row) * (height - 1)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if text:  # the real CLI embeds the prompt + settings unless --disable-image-metadata
        out += chunk(b"tEXt", b"parameters\x00" + text.encode("latin-1", "replace"))
    return out + chunk(b"IDAT", zlib.compress(pixels, 1)) + chunk(b"IEND", b"")


def png_size(path: str) -> tuple[int, int]:
    try:
        head = Path(path).read_bytes()[:24]
    except OSError:
        head = b""
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        die(f"load image from '{path}' failed")
    return struct.unpack(">II", head[16:24])


def progress(step: int, steps: int, speed: str, char: str = "=", head: bool = True, eta=""):
    """Same bytes as the real print_progress_line(): '\\r' redraws, '\\n' only at the end."""
    current = int(min(max(step, 0), steps) * 50 / steps) if steps > 0 else 0
    head_len = 0 if current == 50 else 1
    tip = (">" if head else char) * head_len
    bar = "  |" + char * current + tip + " " * (50 - current - head_len) + "|"
    count = f"{step:>{len(str(steps))}}/{steps}"
    eta = f" - ETA {eta}" if eta else ""
    end = "\n" if step == steps else ""
    sys.stdout.write(f"\r{bar} {count} - {speed:>10}{eta}\033[K{end}")
    sys.stdout.flush()


def sampling_line(step: int, steps: int, seconds: float) -> None:
    """Real pretty_progress(): it/s below 1 s per step, ETA only when the run takes >= 15 s."""
    speed = f"{1 / seconds:.2f}it/s" if 0 < seconds < 1 else f"{seconds:.2f}s/it"
    eta = ""
    if step < steps and seconds * steps >= 15:
        left = int(seconds * (steps - step))
        if left < 3600:
            eta = f"{left // 60:02d}:{left % 60:02d}"
        else:
            eta = f"{left // 3600}:{left // 60 % 60:02d}:{left % 60:02d}"
    progress(step, steps, speed, eta=eta)


def output_paths(output: str, count: int) -> list[str]:
    # real naming: printf pattern (out_%03d.png) -> formatted; else out.png, or out_0.png ... for >1
    if re.search(r"%\d*d", output):
        return [
            re.sub(r"%(\d*)d", lambda m, i=i: f"{i:{m.group(1)}d}", output) for i in range(count)
        ]
    base = Path(output)
    ext = base.suffix if base.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else ".png"
    stem = str(base.with_suffix("")) if base.suffix else str(base)
    if count == 1:
        return [stem + ext]
    return [f"{stem}_{i}{ext}" for i in range(count)]


def write_images(paths: list[str], width: int, height: int, text: str) -> None:
    flat = bool(os.environ.get("FAKE_EMPTY"))
    for idx, path in enumerate(paths):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(png(width, height, text, flat))
        log("I", f"save result image {idx} to '{path}' (success)")
    log("I", f"{len(paths)}/{len(paths)} images saved")


def fail(reason: str) -> None:
    """The pinned release's own failure texts (sd.cpp master-994-e779d4f and its ggml)."""
    if reason == "blocked":  # Windows: a DLL quarantined (STATUS_DLL_NOT_FOUND), no output
        sys.exit(0xC0000135)
    cuda = {  # ggml-cuda.cu:272, then the run goes on without the card: the start scrolls away
        "driver": "CUDA driver version is insufficient for CUDA runtime version",
        "nogpu": "no CUDA-capable device is detected",
    }
    if reason in cuda:
        log("E", f"ggml_cuda_init: failed to initialize CUDA: {cuda[reason]}")
        for i in range(200):
            log("I", f"loading tensor {i} on CPU")
    texts = {
        "oom": "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 9216.00 MiB on device 0: "
        "cudaMalloc failed: out of memory",  # ggml-cuda.cu:940
        "alloc": "ggml_gallocr_reserve_n_impl: failed to allocate CUDA0 buffer of size "
        "4831838208",  # ggml-alloc.c:941
        "kernel": "CUDA error: no kernel image is available for execution on the device",
        "lora": "required lora load failed: C:\\loras\\film grain.safetensors",  # :1787
        "damaged": "tensor 'model.diffusion_model.img_in.weight' extends beyond its model file",
    }
    die(texts.get(reason, f"fake failure: {reason}"))


def loras(prompt: str, lora_dir: str) -> None:
    """Real CLI: a missing LoRA is a warning and the run goes on WITHOUT it."""
    for name, _weight in LORA_RE.findall(prompt):
        base = os.path.normpath(os.path.join(lora_dir, name))
        found = next((base + e for e in LORA_EXT if os.path.isfile(base + e)), None)
        if found is None:
            log("W", f"can not found lora {base}")
            continue
        for _ in range(2):  # real: "may show up twice in the logs per LoRA"
            log("I", f"(720 / 720) LoRA tensors have been applied, lora_file_path = {found}")


def dump(opts: dict) -> None:
    """Real -v: LOG_DEBUG of SDGenerationParams::to_string() (main.cpp:700), one message over
    many lines; the prompt is shown with its <lora:...> tags already removed (common.cpp:2584)."""
    prompt = opts.get("--prompt", "")
    if "--lora-model-dir" in opts:
        prompt = LORA_RE.sub("", prompt)
    flags = ("--negative-prompt", "--ad-prompt", "--ad-negative-prompt")
    values = (prompt, *(opts.get(f, "") for f in flags))
    keys = ("prompt", "negative_prompt", "ad_prompt", "ad_negative_prompt")
    lines = "".join(f'  {k}: "{v}",\n' for k, v in zip(keys, values, strict=True))
    log("D", "SDGenerationParams {\n" + lines + "}")


def hold(gate: Path, limit: float = 60) -> None:
    end = time.monotonic() + limit
    while not gate.exists() and time.monotonic() < end:
        time.sleep(0.05)


def main(argv: list[str]) -> int:
    opts = parse(argv)
    if os.environ.get("FAKE_CHILD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        Path(os.environ["FAKE_CHILD"]).write_text(str(child.pid))
    if opts.get("--verbose") or os.environ.get("FAKE_VERBOSE"):
        dump(opts)
    step_s = float(os.environ.get("FAKE_STEP_S", "0.01"))
    report_s = float(os.environ.get("FAKE_REPORT_S", step_s))
    failure = os.environ.get("FAKE_FAIL", "")
    output = opts.get("--output", "output.png")
    meta = "" if opts.get("--disable-image-metadata") else opts.get("--prompt", "")
    mode = opts.get("--mode", "img_gen")

    if mode in ("upscale", "adetailer"):  # one input image in, one image out
        src = opts.get("--init-img") or die(f"error: {mode} mode needs --init-img")
        model = "--upscale-model" if mode == "upscale" else "--ad-model"
        opts.get(model) or die(f"error: {mode} mode needs {model}")
        w, h = png_size(src)
        factor = 4 ** opts.get("--upscale-repeats", 1) if mode == "upscale" else 1
        time.sleep(step_s)
        if failure:
            fail(failure)
        write_images(output_paths(output, 1), w * factor, h * factor, meta)
        return 0
    if mode != "img_gen":
        die(f"fake sd-cli: mode '{mode}' not simulated")

    for ref in opts["--ref-image"] + [opts[k] for k in ("--init-img", "--mask") if k in opts]:
        png_size(ref)  # the real CLI fails on a missing/broken input image
    if "--lora-model-dir" in opts:  # real: <lora:...> tags are only parsed with this flag
        loras(opts.get("--prompt", ""), opts["--lora-model-dir"])

    for i in range(1, 21):  # model loading bar: '#', no head, MB/s
        progress(i, 20, "812.40MB/s", "#", False)

    sigmas = opts.get("--sigmas")
    if sigmas:
        steps = (SIGMA_PRESETS.get(sigmas) or len(sigmas.split(","))) - 1
    else:
        steps = opts.get("--steps", 20)
    width, height = opts.get("--width", 512), opts.get("--height", 512)
    batch = opts.get("--batch-count", 1)
    preview = opts.get("--preview", "none") != "none"
    every = max(opts.get("--preview-interval", 1), 1)
    preview_path = Path(opts.get("--preview-path", "preview.png"))
    for _image in range(batch):
        for step in range(1, steps + 1):
            time.sleep(step_s)
            if preview and step % every == 0:  # real CLI: overwrites one file, no log line
                preview_path.write_bytes(png(max(width // 8, 1), max(height // 8, 1)))
            sampling_line(step, steps, report_s)
            if step == 2 and os.environ.get("FAKE_HOLD"):
                hold(Path(os.environ["FAKE_HOLD"]))
            if failure:
                sys.stdout.write("\n")
                fail(failure)
    write_images(output_paths(output, batch), width, height, meta)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
