"""Job description and the sd-cli argv builder.
JobRequest is also the JSON body of POST /api/jobs and what history "reuse settings" returns.

Every flag below exists in the pinned sd-cli (master-994-e779d4f, examples/common/common.cpp)
and in the fake engine's table (tests/fake_sd_cli.py), which rejects anything else."""

import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PureWindowsPath

from latentroom.engine import catalogue, images, install, paths
from latentroom.errors import LatentroomError

KINDS = ("txt2img", "edit", "img2img", "inpaint", "upscale", "enhance", "facefix")
_BAD = "The request was not understood. Reload Latentroom and try again."
OUTSIDE = "Latentroom can only use pictures you uploaded or made with it. Add it again."

# sd.cpp src/stable-diffusion.cpp sample_method_to_str / scheduler_to_str at e779d4f
SAMPLERS = (
    "euler euler_a heun dpm2 dpm++2s_a dpm++2m dpm++2mv2 ipndm ipndm_v lcm ddim_trailing tcd "
    "res_multistep res_2s er_sde euler_cfg_pp euler_a_cfg_pp euler_ge dpm++2m_sde "
    "dpm++2m_sde_bt lms"
).split()
SCHEDULERS = (
    "discrete karras exponential ays gits sgm_uniform simple smoothstep kl_optimal lcm "
    "bong_tangent ltx2 logit_normal flux2 flux beta llada_image"
).split()
TURBO_SIGMAS = "qwen-image-2.1-turbo"  # preset: 9 sigmas = 8 steps, cfg 1, euler
TURBO_STEPS = 8
SIGMA_STEPS = {TURBO_SIGMAS: TURBO_STEPS}  # preset -> steps (the sigma count minus one)
MIN_SIDE, MAX_SIDE = 256, 2048
REF_SIDE = 1024  # references are shrunk to this long side before the engine sees them
MAX_REFS, MAX_BATCH, MAX_LORAS, MAX_TEXT = 6, 8, 8, 8000
ENHANCE_STRENGTH = 0.35  # detail enhance: upscale, then img2img at most this strength
FACEFIX_STRENGTH = 0.4  # sd.cpp ADetailer default denoising strength
FACEFIX_PROMPT = "a detailed natural face, sharp focus"
LORA_TAG = re.compile(r"<lora:[^>]*>")  # any tag the user typed; the engine's is narrower
_NEEDS_PROMPT = ("txt2img", "edit", "img2img", "inpaint")
_NEEDS_INIT = ("img2img", "inpaint", "upscale", "enhance", "facefix")


@dataclass
class LoraRef:
    name: str  # file stem in paths.loras_dir(), e.g. "test-style-a"
    weight: float = 1.0


@dataclass
class JobRequest:
    kind: str = "txt2img"  # one of KINDS
    model_id: str = ""  # catalogue Model.id; "" = settings.model_id; ignored by "upscale"
    prompt: str = ""
    negative: str = ""  # only passed to the engine when cfg > 1
    width: int = 1024
    height: int = 1024
    seed: int = -1  # -1 = random; the runner picks the real seed and history stores it
    steps: int | None = None  # None = model default (catalogue Model.steps)
    cfg: float | None = None  # None = model default (catalogue Model.cfg)
    sampler: str = "euler"
    scheduler: str = ""  # "" = engine default
    batch: int = 1
    turbo: bool = False  # Turbo LoRA + turbo sigmas on a standard model
    loras: list[LoraRef] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # edit: absolute paths, uploads/ or output dir
    init_image: str = ""  # img2img / inpaint / upscale / enhance / facefix source image
    mask: str = ""  # inpaint: white = repaint, black = keep
    strength: float = 0.75  # img2img / inpaint / enhance
    max_vram_gb: float = 0  # 0 = engine decides (--auto-fit); copied from settings by the server

    @classmethod
    def from_dict(cls, data: object) -> "JobRequest":
        """Build from untrusted JSON. Structure only; ranges are checked by validate()."""
        if not isinstance(data, dict):
            raise LatentroomError("bad_request", _BAD)
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise LatentroomError("bad_request", _BAD)
        data = dict(data)
        try:
            data["loras"] = [LoraRef(**x) for x in data.get("loras", [])]
            return cls(**data)
        except TypeError as e:
            raise LatentroomError("bad_request", _BAD) from e

    def to_dict(self) -> dict:
        return asdict(self)


def _bad(message: str) -> LatentroomError:
    return LatentroomError("bad_request", message)


def _number(value, name: str, lo: float, hi: float, integer: bool = False) -> None:
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or not lo <= value <= hi:
        whole = "a whole number" if integer else "a number"
        raise _bad(f"{name} must be {whole} between {lo:g} and {hi:g}.")


def _text(value, name: str) -> None:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise _bad(f"{name} must be text of at most {MAX_TEXT} characters.")


def _file_of_kind(kind: str) -> catalogue.FileItem | None:
    """Other lines' files are found by KIND (contract rule), never by id."""
    return next((f for f in catalogue.files().values() if f.kind == kind), None)


def _need(kind: str, what: str) -> catalogue.FileItem:
    item = _file_of_kind(kind)
    if item is None or item.id not in install.installed():
        raise LatentroomError(
            "model_missing", f"{what} is not downloaded yet. Get it in the Models tab.", 404
        )
    return item


def is_network_path(path: str) -> bool:
    """A network or device path: \\\\host\\share, //host/share, \\\\?\\..., \\\\.\\... Text only, no
    disk access: Windows opens such a path by connecting to that host and handing it the user's
    login (an NTLM hash), so a path from a client is tested before anything touches it."""
    return path.replace("/", "\\").startswith("\\\\")


def _in_folder(path: str, folder: Path) -> bool:
    """`path` lies inside `folder`, by text alone (case ignored, no ..): no disk access."""
    p = PureWindowsPath(path)
    return ".." not in p.parts and p.is_relative_to(folder)


def _check_image(path: str, output_dir: Path | None) -> Path:
    """Only uploads, the output folder and history images are ever read."""
    from latentroom.engine import history  # history imports this module

    p = Path(path) if isinstance(path, str) and path else None
    if p is None or not p.is_absolute():
        raise _bad("Pick a picture first.")
    # a network path is never resolved: only one inside an output folder the user put on a NAS
    if is_network_path(path) and not (output_dir and _in_folder(path, output_dir)):
        raise _bad(OUTSIDE)
    p = p.resolve()
    roots = [paths.uploads_dir().resolve()] + ([output_dir.resolve()] if output_dir else [])
    if not any(p.is_relative_to(r) for r in roots) and not history.has_image(p):
        raise _bad(OUTSIDE)
    if not p.is_file():
        raise _bad("That picture no longer exists. Add it again.")
    try:
        images.image_size(p)
    except Exception as e:  # Pillow raises many types for a broken file
        msg = "That file is not a picture Latentroom can read. Use a PNG, JPEG or WebP."
        raise _bad(msg) from e
    return p


def _check_model(req: JobRequest) -> catalogue.Model:
    if not isinstance(req.model_id, str) or not req.model_id:
        raise LatentroomError("model_missing", "Pick a model first in the Models tab.", 404)
    model = catalogue.get(req.model_id)  # unknown id: model_missing
    have = install.installed()
    if any(f.id not in have for f in catalogue.required(req.model_id) if f.kind != "engine"):
        raise LatentroomError(
            "model_missing", "This model is not downloaded yet. Get it in the Models tab.", 404
        )
    return model


def validate(req: JobRequest, output_dir: Path | None = None) -> None:
    """Check kinds, sizes (multiple of 32), ranges and that input files exist.
    Raises LatentroomError("bad_request", <plain sentence naming the field>).
    `output_dir`: the current output folder, whose images may be inputs."""
    if req.kind not in KINDS:
        raise _bad(_BAD)
    for side, name in ((req.width, "Width"), (req.height, "Height")):
        _number(side, name, MIN_SIDE, MAX_SIDE, integer=True)  # message names the range
        if side % 32:  # in range, so both neighbours are too (MIN_SIDE, MAX_SIDE are /32)
            low = side // 32 * 32
            raise _bad(f"{name} must be a multiple of 32: the nearest are {low} and {low + 32}.")
    _number(req.seed, "Seed", -1, 2**32 - 1, integer=True)
    if req.steps is not None:
        _number(req.steps, "Steps", 1, 100, integer=True)
    if req.cfg is not None:
        _number(req.cfg, "Guidance", 1, 20)
    if req.sampler not in SAMPLERS:
        raise _bad("Pick a sampler from the list.")
    if req.scheduler and req.scheduler not in SCHEDULERS:
        raise _bad("Pick a scheduler from the list.")
    _number(req.batch, "Number of pictures", 1, MAX_BATCH, integer=True)
    _number(req.strength, "Strength", 0.05, 1)
    _number(req.max_vram_gb, "Video memory limit", 0, 256)
    _text(req.prompt, "The prompt")
    _text(req.negative, "The negative prompt")
    if not isinstance(req.turbo, bool):
        raise _bad(_BAD)
    if req.kind in _NEEDS_PROMPT and not LORA_TAG.sub("", req.prompt).strip():
        raise _bad("Describe the picture you want first.")

    if not isinstance(req.loras, list) or len(req.loras) > MAX_LORAS:
        raise _bad(f"Use at most {MAX_LORAS} LoRAs at once.")
    if req.loras:
        available = set(install.list_loras())
        for lora in req.loras:
            if not isinstance(lora.name, str) or lora.name not in available:
                raise _bad(f'The LoRA "{lora.name}" is not in your LoRA folder any more.')
            _number(lora.weight, "LoRA weight", -2, 2)

    if req.kind == "edit":
        if not isinstance(req.refs, list) or not 1 <= len(req.refs) <= MAX_REFS:
            raise _bad(f"Add between 1 and {MAX_REFS} reference pictures.")
        for ref in req.refs:
            _check_image(ref, output_dir)
    elif req.refs != []:  # the engine would silently drop them
        raise _bad('Reference pictures only work with "Edit with references". Remove them first.')
    if req.kind in _NEEDS_INIT:
        init = _check_image(req.init_image, output_dir)
        if req.kind in ("upscale", "enhance") and max(images.image_size(init)) > MAX_SIDE:
            raise _bad(f"This picture is too large to upscale: max {MAX_SIDE} pixels per side.")
    if req.kind == "inpaint":
        if not req.mask:
            raise _bad("Paint over the part you want to change first.")
        _check_image(req.mask, output_dir)

    if req.kind in ("upscale", "enhance"):
        _need("upscaler", "The upscaler")
    if req.kind == "facefix":
        if _file_of_kind("detector") is None:
            raise LatentroomError(
                "not_available",
                "Fix faces is not available in this version of Latentroom.",
                409,
            )
        _need("detector", "The face detector")
    if req.kind != "upscale":
        model = _check_model(req)
        if req.kind == "edit" and "llm_vision" not in _component_kinds(model):
            raise LatentroomError(
                "not_available",
                "This model cannot edit with reference pictures. Pick another model.",
                409,
            )
        if req.turbo and not model.sigmas:
            _need("lora", "The Turbo add-on")


def _component_kinds(model: catalogue.Model) -> dict[str, catalogue.FileItem]:
    every = catalogue.files()
    return {every[i].kind: every[i] for i in model.needs if i in every}


def _snap(v: float) -> int:
    return int(max(MIN_SIDE, min(MAX_SIDE, v // 32 * 32)))


def _scaled(path: Path, factor: float) -> tuple[int, int]:
    """Image size times `factor`, long side <= MAX_SIDE, short side >= MIN_SIDE, /32."""
    w, h = images.image_size(path)
    scale = max(min(factor, MAX_SIDE / max(w, h)), MIN_SIDE / min(w, h))
    return _snap(round(w * scale)), _snap(round(h * scale))


def job_size(req: JobRequest) -> tuple[int, int]:
    """Size of the biggest image the diffusion model makes for this job (for catalogue.fit).
    Call after validate()."""
    if req.kind in ("txt2img", "edit"):
        return req.width, req.height
    init = Path(req.init_image)
    if req.kind == "upscale":
        w, h = images.image_size(init)
        return w * 4, h * 4
    if req.kind == "facefix":
        return images.image_size(init)
    return _scaled(init, 2 if req.kind == "enhance" else 1)


def lora_bytes(req: JobRequest) -> int:
    """Total size of the LoRA files this job loads, Turbo add-on included (for catalogue.fit).
    Call after validate()."""
    files = (paths.loras_dir() / f"{lora.name}.safetensors" for lora in req.loras)
    total = sum(p.stat().st_size for p in files if p.is_file())  # gone: validate() said so
    if req.kind != "upscale" and req.turbo and not catalogue.get(req.model_id).sigmas:
        total += _file_of_kind("lora").size  # same rule as _gen()
    return total


def _out(job_dir: Path, name: str) -> list[str]:
    """Plain name (no %d pattern: a user folder may contain '%'). batch > 1 -> out_0.png..."""
    return ["--output", str(job_dir / name)]


def _gen(req: JobRequest, model: catalogue.Model, prompt: str, strength: float | None) -> list:
    """Diffusion flags shared by every kind that runs the model."""
    parts = _component_kinds(model)
    # --mmap: model load 313 s -> 86 s at 1024²; every fit anchor used it
    argv = ["--mmap", "--diffusion-model", str(paths.models_dir() / model.file.name)]
    for kind, flag in (("vae", "--vae"), ("llm", "--llm")):
        argv += [flag, str(paths.models_dir() / parts[kind].name)]
    if req.kind == "edit":
        argv += ["--llm_vision", str(paths.models_dir() / parts["llm_vision"].name)]

    sigmas, cfg = model.sigmas, req.cfg if req.cfg is not None else model.cfg
    tags = [(lora.name, lora.weight) for lora in req.loras]
    if req.turbo and not sigmas:
        sigmas, cfg = TURBO_SIGMAS, 1.0
        turbo = _file_of_kind("lora")  # contract: Turbo LoRA relative to --lora-model-dir
        tags.append((f"../{paths.models_dir().name}/{Path(turbo.name).stem}", 1))
    prompt = " ".join([LORA_TAG.sub("", prompt).strip()] + [f"<lora:{n}:{w:g}>" for n, w in tags])
    argv += ["--prompt", prompt.strip(), "--cfg-scale", f"{cfg:g}"]
    if cfg > 1 and req.negative.strip():
        argv += ["--negative-prompt", req.negative]
    if sigmas:  # the sigma count decides the steps; the presets are tuned for euler
        argv += ["--sigmas", sigmas, "--sampling-method", "euler"]
    else:
        argv += ["--steps", str(req.steps or model.steps), "--sampling-method", req.sampler]
        if req.scheduler:
            argv += ["--scheduler", req.scheduler]
    if tags:
        argv += ["--lora-model-dir", str(paths.loras_dir())]
    if strength is not None:
        argv += ["--strength", f"{strength:g}"]
    return argv + ["--seed", str(req.seed)]


def build(req: JobRequest, job_dir: Path) -> list[list[str]]:
    """sd-cli arguments for this job, WITHOUT the executable (the runner prepends it).
    One list per engine run, run in order (most kinds: one; "enhance": upscale then img2img).
    Outputs go to job_dir (final images: out.png, or out_0.png... for a batch).
    No process is started here; input images are prepared into job_dir (RGB, safe size).
    Raises LatentroomError("not_found") when an input picture was deleted since validate()."""
    inputs = [
        *(req.refs if req.kind == "edit" else []),
        *([req.init_image] if req.kind in _NEEDS_INIT else []),
        *([req.mask] if req.kind == "inpaint" else []),
    ]
    for p in inputs:
        if not Path(p).is_file():  # a queued job waits: the user may have deleted it meanwhile
            raise LatentroomError(
                "not_found", f"The picture {Path(p).name} is no longer there. Add it again.", 404
            )
    job_dir.mkdir(parents=True, exist_ok=True)
    tail = ["--auto-fit", "on", "--disable-image-metadata"]
    if req.max_vram_gb > 0:
        tail += ["--max-vram", f"{req.max_vram_gb:g}"]
    batch = ["--batch-count", str(req.batch)] if req.batch > 1 else []
    init = job_dir / "init.png"

    if req.kind in ("upscale", "enhance"):
        images.prepare_ref(Path(req.init_image), init, max_side=MAX_SIDE)
        upscaler = str(paths.models_dir() / _file_of_kind("upscaler").name)
        name = "out.png" if req.kind == "upscale" else "up.png"
        upscale = ["--mode", "upscale", "--init-img", str(init), "--upscale-model", upscaler]
        runs = [upscale + _out(job_dir, name) + tail]
        if req.kind == "upscale":
            return runs
    model = catalogue.get(req.model_id)
    size = job_size(req)
    w_h = ["--width", str(size[0]), "--height", str(size[1])]

    if req.kind in ("txt2img", "edit"):
        argv = _gen(req, model, req.prompt, None) + w_h + batch
        for i, ref in enumerate(req.refs if req.kind == "edit" else [], 1):
            images.prepare_ref(Path(ref), job_dir / f"ref_{i}.png", max_side=REF_SIDE)
            argv += ["--ref-image", str(job_dir / f"ref_{i}.png")]
        return [argv + _out(job_dir, "out.png") + tail]
    if req.kind in ("img2img", "inpaint"):
        images.prepare_ref(Path(req.init_image), init, size=size)
        argv = _gen(req, model, req.prompt, req.strength) + w_h + batch
        argv += ["--init-img", str(init)]
        if req.kind == "inpaint":
            images.prepare_mask(Path(req.mask), job_dir / "mask.png", size)
            argv += ["--mask", str(job_dir / "mask.png")]
        return [argv + _out(job_dir, "out.png") + tail]
    if req.kind == "enhance":
        argv = _gen(req, model, req.prompt, min(req.strength, ENHANCE_STRENGTH)) + w_h
        return runs + [
            argv + ["--init-img", str(job_dir / "up.png")] + _out(job_dir, "out.png") + tail
        ]
    # facefix: sd-cli's own ADetailer (YOLOv8 face detector -> crop -> img2img -> paste back)
    images.prepare_ref(Path(req.init_image), init, max_side=MAX_SIDE)
    detector = str(paths.models_dir() / _file_of_kind("detector").name)
    argv = ["--mode", "adetailer", "--init-img", str(init), "--ad-model", detector]
    strength = min(req.strength, FACEFIX_STRENGTH)
    argv += _gen(req, model, req.prompt.strip() or FACEFIX_PROMPT, strength)
    return [argv + _out(job_dir, "out.png") + tail]
