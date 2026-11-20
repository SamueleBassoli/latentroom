"""Model catalogue (latentroom/data/catalogue.json, generated offline) and fit prediction.
The app never calls Hugging Face for metadata at runtime."""

import json
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from latentroom.engine.hardware import Hardware, gb
from latentroom.errors import LatentroomError

CATALOGUE = Path(__file__).resolve().parent.parent / "data" / "catalogue.json"

# --- Fit model: measured 2026-10-23 on 4070 SUPER 12 GB (sd-cli master-994-e779d4f) ---
# sd-cli e779d4f runs with --auto-fit on (its default; docs/backend.md:130-216). The diffusion
# model (DiT) either stays on the GPU as a whole, or lives in RAM and is streamed to the GPU
# segment by segment, reusing what it can cache (compute stays on the GPU either way). Text
# encoder and VAE are placed after it; a failed VAE decode retries tiled, so the VAE never
# decides the fit. Facts, sd.cpp e779d4f src/core/backend_fit.cpp:
SD_MARGIN = 512 * 2**20  # GPU budget = free VRAM - 512 MiB (:157), also kept free at runtime
DIT_RESERVE = 2 * 2**30  # DiT stays on the GPU only with 2 GiB left for its compute (:119)
RAM_RESERVE = 2 * 2**30  # RAM budget = free RAM - max(2 GiB, 10%) (:439)
# "Free VRAM" above is never read live (usable()): card total - free on an idle Windows
# desktop (measured: 12282 - 11416 MiB) = the ~0.6 GB nvidia-smi shows used + what the
# driver keeps. A real 8 / 6 GB card then leaves 7.15 / 5.15 GiB, the tiers the picks are for.
DESKTOP_RESERVE = 865 * 2**20
# Measured 2026-10-23 on 4070 SUPER 12 GB: steady VRAM while sampling, above idle, minus the DiT
# (Q4_K 4003 MiB, Q8_0 7331 MiB) = 0.48 GiB at 512x512, 1.24-1.29 GiB at 1024x1024 and 1312x800
# (both quants), 3.0 GiB at 1024x1024 + refs of 1 + 0.25 MP, 5.0 GiB at 2048x2048. Encoders and
# VAE decode then fill whatever budget is left (peak 9.6 GiB uncapped, 5.7 at --max-vram 6, same
# speed; a VAE that runs out retries tiled): never what decides.
CUDA_CTX = 0.4e9  # CUDA context + fixed DiT compute (created after Latentroom reads free VRAM)
TE_STAGING = 0.55e9  # largest text-encoder tensor, kept free next to the DiT (:239-254)
WORK_PER_MP = 1.35e9  # DiT compute per megapixel of output and of each reference
REF_MP = 1.0  # sd.cpp resizes every ref to the output area, so a ref costs that area
# (a 2048x2048 edit's refs are 4 MP each; not measured), never less than 1 MP:
# exact for a 1 MP output, generous below it
SEGMENT = 0.5e9  # VRAM for the weights of the segment being streamed (not measured alone)
# Measured: --max-vram 4 and 6 both put the whole Q4_K DiT in RAM; per step +29% at 4 GiB (1.8 GB
# not cached by this model), +8% at 6 GiB (all of it cached): 0.11 per uncached GB on PCIe 3.0
# x16 on top of STREAM_COST. Streaming time grows with bytes, not with the share of the model.
STREAM_COST = 0.1  # a DiT placed in RAM, even fully cached on the GPU
SPILL_PER_GB = 0.25  # measured 0.11, x2+ for slower buses (predicts 1.5x and 1.1x for the runs)
# An edit keeps the refs' attention keys/values from step 1 for the later steps (sd.cpp e779d4f
# qwen_image_2_1.hpp:193-215, 441-452): 32 layers x K+V x 4096 x F16 = 0.5 MiB per token; a 1 MP
# ref = 4096 image tokens + up to ~1300 vision-encoder tokens. Live (12 GB card): 1 ref kept
# it, 2 refs (1 + 0.25 MP) ran out in step 1 and the engine retried without it (:572-578).
PREFIX_CACHE_PER_REF = 2.9e9
NO_CACHE_SLOWDOWN = 2.5  # live: 10.5 s per step without it (2 refs) vs 4.1 s with it (1 ref)
SIZES = (1024, 896, 768, 640, 512)  # square sizes recommend_size tries, largest first
# "Runs well" (setup step 3 tiles, Recommended): Full GPU, or partial GPU offload with a
# slowdown of at most WELL. Fit.slowdown is rounded to 0.1, so 1.2 compares exactly.
WELL = 1.2


@dataclass(frozen=True)
class FileItem:
    """One downloadable file: a diffusion model, a shared component, or an engine zip."""

    id: str  # stable key, e.g. "std-q4_k", "vae", "llm-q4_k_m", "mmproj", "esrgan-x4", "engine"
    name: str  # file name on disk
    url: str  # fixed https download URL
    size: int  # bytes, checked after download
    sha256: str | None  # hex; None when the source publishes none (then size-only check)
    licence: str
    kind: str  # "diffusion"|"vae"|"llm"|"llm_vision"|"upscaler"|"lora"|"detector"|"engine"
    # Smart search: "embedding" + "embedding_vision" GGUFs and the "llama" runtime zip


@dataclass(frozen=True)
class Model:
    """A diffusion model the user can pick."""

    id: str  # == file.id
    family: str  # "standard" | "uncensored" | "turbo" (Turbo has no uncensored side)
    quant: str  # "Q4_K", "Q8_0", ...
    file: FileItem
    needs: tuple[str, ...]  # FileItem ids of shared components this model needs (vae, llm, ...)
    adult: bool  # uncensored: show the one-time 18+ notice
    experimental: bool  # Turbo candidates until the live GPU test proves they load
    steps: int  # default steps
    cfg: float  # default cfg scale
    sigmas: str | None  # sd-cli --sigmas preset, e.g. "qwen-image-2.1-turbo"; None = default


@dataclass(frozen=True)
class ModelPair:
    """One quality level, offered as standard + uncensored side by side."""

    id: str  # "q4", "turbo-q4", ...
    label: str  # plain words, e.g. "Balanced"
    standard: Model
    uncensored: Model | None  # None when that level has no uncensored file (and every Turbo)
    fast: bool = False  # Turbo pair


@dataclass(frozen=True)
class Fit:
    label: str  # "full_gpu" | "partial" | "too_big"
    vram: int  # predicted bytes on the GPU
    ram: int  # predicted bytes in system RAM
    slowdown: float  # 1.0 for full_gpu; 2.0 = twice the time. Ranking only, never shown
    reason: str  # one plain sentence, shown with the label


@cache
def _catalogue() -> tuple[dict[str, FileItem], dict[str, Model], tuple[ModelPair, ...]]:
    data = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    from latentroom.engine.install import ENGINE_FILES, LLAMA_ZIP  # install imports FileItem

    items = {f["id"]: FileItem(**f) for f in data["files"]}
    items.update({f.id: f for f in (*ENGINE_FILES, LLAMA_ZIP)})
    models = {}
    for m in data["models"]:
        models[m["id"]] = Model(**{**m, "file": items[m["file"]], "needs": tuple(m["needs"])})
    pairs = tuple(
        ModelPair(
            p["id"],
            p["label"],
            models[p["standard"]],
            models[p["uncensored"]] if p["uncensored"] else None,
            p["fast"],
        )
        for p in data["pairs"]
    )
    return items, models, pairs


def load() -> list[ModelPair]:
    """All pairs from catalogue.json, best quality first."""
    return list(_catalogue()[2])


def files() -> dict[str, FileItem]:
    """Every downloadable file by id: diffusion models, shared components, engine zips."""
    return dict(_catalogue()[0])


def get(model_id: str) -> Model:
    """Raises LatentroomError("model_missing", ..., 404) for an unknown id."""
    model = _catalogue()[1].get(model_id)
    if model is None:
        raise LatentroomError(
            "model_missing",
            "Latentroom does not know that model. Pick one in the Models tab.",
            404,
        )
    return model


def required(model_id: str) -> list[FileItem]:
    """Everything needed to run this model: engine zips + shared components + the model file."""
    from latentroom.engine.install import ENGINE_FILES

    model = get(model_id)
    items = _catalogue()[0]
    return [*ENGINE_FILES, *(items[i] for i in model.needs), model.file]


def usable(hw: Hardware) -> int:
    """Video memory fit counts on: the card minus what an idle Windows desktop keeps.
    Never the live free VRAM: Latentroom's own engine or Enhance, or another app,
    lowers it for a while, and a model that fits the card must not turn "Too big" then."""
    return max(hw.gpu.vram_total - DESKTOP_RESERVE, 0) if hw.gpu else 0


def capped(hw: Hardware, max_vram_gb: float) -> Hardware:
    """The hardware fit should see under the user's video memory limit (settings.max_vram_gb):
    a smaller card. sd-cli --max-vram N makes its budget min(N GiB, free) with no 512 MiB margin
    (backend_fit.cpp ~150); fit subtracts CUDA_CTX and SD_MARGIN from usable(), so add both back."""
    if max_vram_gb <= 0 or hw.gpu is None:
        return hw
    limit = int(max_vram_gb * 2**30 + CUDA_CTX + SD_MARGIN + DESKTOP_RESERVE)
    return replace(hw, gpu=replace(hw.gpu, vram_total=min(hw.gpu.vram_total, limit)))


def fit(
    model: Model,
    hw: Hardware,
    width: int = 1024,
    height: int = 1024,
    refs: int = 0,
    lora_bytes: int = 0,
) -> Fit:
    """Predict where the weights land for this image size, number of reference images and
    total size of the LoRA files (Turbo add-on included)."""
    if hw.gpu is None:
        return Fit("too_big", 0, 0, 0.0, "Latentroom needs an NVIDIA graphics card.")
    dit = model.file.size
    items = _catalogue()[0]
    encoders = ("llm", "llm_vision") if refs else ("llm",)
    # the text (and vision) encoder runs first; counted in RAM, where auto-fit puts it when
    # the GPU is full (on a big card it may stay on the GPU: then the RAM figure is generous)
    staged = sum(items[i].size for i in model.needs if items[i].kind in encoders)
    mp = width * height / 1024**2
    work = WORK_PER_MP * (mp + refs * max(REF_MP, mp))
    # LoRA weights sit on the GPU next to the context: live turbo_lora used +776 MiB for the
    # 871 MiB file, so the file size is the measure
    fixed = CUDA_CTX + lora_bytes
    budget = usable(hw) - fixed - SD_MARGIN
    resident = dit + max(work, DIT_RESERVE + TE_STAGING) <= budget  # auto-fit's DiT rule
    if not resident and work + SEGMENT > budget:
        with_refs = " with these references" if refs else ""
        return Fit(
            "too_big",
            int(fixed + work + SEGMENT),
            0,
            0.0,
            f"Not enough video memory for {width}x{height}{with_refs}: pick a smaller size.",
        )
    # a DiT in RAM keeps copies of what fits next to the work on the GPU, streams the rest
    cached = dit if resident else min(dit, budget - work - SEGMENT)
    spill = dit - cached
    ram = int(staged + (0 if resident else dit))
    # Only a DiT in RAM must fit there: auto-fit streams it every step, from disk that would
    # crawl. Encoders that find no RAM go to disk (docs/backend.md placement step 4) and cost
    # seconds once per image.
    if not resident and hw.ram_free and dit + RAM_RESERVE > hw.ram_free:  # 0 = unknown
        return Fit(
            "too_big",
            int(fixed + work + cached),
            ram,
            0.0,
            f"Needs {gb(dit + RAM_RESERVE, up=True)} of free RAM besides the graphics card; "
            f"this PC has {gb(hw.ram_free)} free.",
        )
    # an edit whose refs' keys/values do not fit: step 1 fills the engine's whole budget trying,
    # then every step runs without them. shortcut: a DiT in RAM is assumed to never keep them
    # (not measured); measure a capped edit to relax it.
    prefix = refs * PREFIX_CACHE_PER_REF
    retry = refs > 0 and not (resident and dit + work + prefix <= budget)
    peak = int(usable(hw) - SD_MARGIN)  # the engine's budget, its context included
    if resident and not retry:
        return Fit(
            "full_gpu",
            int(fixed + dit + work + prefix),
            ram,
            1.0,
            "Fits in your graphics card: fastest.",
        )
    if resident:
        return Fit(
            "partial",
            peak,
            ram,
            NO_CACHE_SLOWDOWN,
            "The reference pictures leave no room in your graphics card to reuse their work "
            "between steps, so it is slower.",
        )
    # placed in RAM costs time even when every segment stays cached; then per GB streamed
    slowdown = 1 + STREAM_COST + SPILL_PER_GB * spill / 1e9
    slowdown = round(slowdown * (NO_CACHE_SLOWDOWN if retry else 1), 1)
    where = (
        f"{gb(spill)} of it runs from RAM instead of the graphics card"
        if spill > 0
        else "It waits in RAM and is copied to the graphics card"
    )
    return Fit(
        "partial",
        peak if retry else int(fixed + work + SEGMENT + cached),
        ram,
        slowdown,
        f"{where}, so it is slower.",
    )


def runs_well(f: Fit) -> bool:
    """Full GPU, or partial GPU offload at most WELL slower."""
    return f.label == "full_gpu" or (f.label == "partial" and f.slowdown <= WELL)


def recommend(
    pairs: list[ModelPair], hw: Hardware, width: int = 1024, height: int = 1024
) -> ModelPair | None:
    """The one Recommended level, the same for Standard and Uncensored (setup and Models):
    1. a level with both files, both running well: the smallest slowdown on its slower file,
       a tie to the better quality (pairs are best quality first);
    2. else the best non-Fast level whose files all run well;
    3. else the non-Fast level that runs with the smallest slowdown (a small card should not
       start on the slowest level), else any that runs; None if nothing runs.
    Light (Q3_K) drew a double lighthouse and Smallest (Q2_K) a stray frame in testing; they
    have no Uncensored file, so they can win only from step 2."""
    sides = {p.id: [m for m in (p.standard, p.uncensored) if m] for p in pairs}
    fits = {i: [fit(m, hw, width, height) for m in ms] for i, ms in sides.items()}
    both = [p for p in pairs if p.uncensored and all(map(runs_well, fits[p.id]))]
    if both:
        return min(both, key=lambda p: max(f.slowdown for f in fits[p.id]))
    runs = [p for p in pairs if all(f.label != "too_big" for f in fits[p.id])]
    slow = [p for p in runs if not p.fast]  # setup's tiles are never Fast
    well = [p for p in slow if all(map(runs_well, fits[p.id]))]
    if well:
        return well[0]
    return min(slow or runs, key=lambda p: max(f.slowdown for f in fits[p.id]), default=None)


def recommend_size(pairs: list[ModelPair], hw: Hardware) -> tuple[ModelPair, int, int] | None:
    """recommend() at the largest square size in SIZES where a pair runs: (pair, width, height).
    A small or busy card gets a smaller picture instead of nothing."""
    for size in SIZES:
        if pair := recommend(pairs, hw, size, size):
            return pair, size, size
    return None


# --- Speed in plain words for the wizard (step 3) and the Models Fast help ---
# Never a time, nor a speed-up or slowdown factor: the fit is picked by video memory, not by
# how fast the card is, so a time from one card is wrong for another with the same memory.


def speed(f: Fit) -> str:
    """Wizard line under a model that does not run well: "Works, but slower.", else "".
    Setup groups by it: "" (and not too big) = runs well, a tile."""
    return "" if runs_well(f) or f.label == "too_big" else "Works, but slower."


def fast_help(hw: Hardware) -> str:
    """Help line of the Fast (Turbo) rows, the same on every card."""
    return "Faster, a bit less detail. 8 steps."
