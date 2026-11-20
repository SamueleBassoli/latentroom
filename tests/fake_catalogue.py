"""Stand-in for catalogue/install/hardware in the engine tests.
Usage in a test module:  stub = fake_catalogue.install(monkeypatch)"""

import sys
from pathlib import Path
from types import SimpleNamespace

from latentroom.engine import catalogue, hardware, install, paths
from latentroom.errors import LatentroomError

FAKE_SD_CLI = Path(__file__).parent / "fake_sd_cli.py"


def _file(id_: str, name: str, kind: str) -> catalogue.FileItem:
    return catalogue.FileItem(
        id=id_, name=name, url=f"https://example.invalid/{name}", size=1, sha256="0" * 64,
        licence="test", kind=kind,
    )  # fmt: skip


FILES = {
    f.id: f
    for f in (
        _file("std-q4", "std-Q4_K.gguf", "diffusion"),
        _file("turbo-q4", "turbo-Q4_K.gguf", "diffusion"),
        _file("plain-q4", "plain-Q4_K.gguf", "diffusion"),
        _file("unc-q4", "unc-Q4_K.gguf", "diffusion"),
        _file("vae", "vae.safetensors", "vae"),
        _file("llm", "llm.gguf", "llm"),
        _file("mmproj", "mmproj.gguf", "llm_vision"),
        _file("esrgan-x4", "RealESRGAN_x4plus.pth", "upscaler"),
        _file("turbo-lora", "turbo-lora.safetensors", "lora"),
        _file("face", "face_yolov8n.safetensors", "detector"),
        _file("engine", "sd.zip", "engine"),
    )
}


def _model(id_: str, steps=20, cfg=4.0, sigmas=None, needs=("vae", "llm", "mmproj"), adult=False):
    return catalogue.Model(
        id=id_, family="uncensored" if adult else "standard", quant="Q4_K", file=FILES[id_],
        needs=needs, adult=adult, experimental=False, steps=steps, cfg=cfg, sigmas=sigmas,
    )  # fmt: skip


MODELS = {
    "std-q4": _model("std-q4"),
    "turbo-q4": _model("turbo-q4", steps=8, cfg=1.0, sigmas="qwen-image-2.1-turbo"),
    "plain-q4": _model("plain-q4", needs=("vae", "llm")),  # no vision encoder: cannot edit
    "unc-q4": _model("unc-q4", adult=True),  # behind the 18+ gate
}
HW = hardware.Hardware(
    status="ok", message="test", gpu=hardware.Gpu("RTX test", 16 << 30, 15 << 30),
    driver="600.00", ram_total=32 << 30, ram_free=24 << 30,
)  # fmt: skip


def install_stub(monkeypatch, fit_label: str = "full_gpu") -> SimpleNamespace:
    """Patch the catalogue/install/hardware functions the engine calls; returns test knobs."""
    state = SimpleNamespace(
        files=dict(FILES), installed=set(FILES), fit_label=fit_label, fit_calls=[]
    )

    def get(model_id):
        if model_id not in MODELS:
            raise LatentroomError("model_missing", "Unknown model.", 404)
        return MODELS[model_id]

    def fit(model, hw, width=1024, height=1024, refs=0, lora_bytes=0):
        state.fit_calls.append((model.id, width, height, refs, lora_bytes))
        return catalogue.Fit(state.fit_label, 0, 0, 1.0, "test")

    monkeypatch.setattr(catalogue, "files", lambda: state.files)
    monkeypatch.setattr(catalogue, "get", get)
    monkeypatch.setattr(
        catalogue,
        "required",
        lambda i: [FILES["engine"], *(FILES[n] for n in get(i).needs), get(i).file],
    )
    monkeypatch.setattr(catalogue, "fit", fit)
    monkeypatch.setattr(install, "installed", lambda: state.installed)
    monkeypatch.setattr(
        install,
        "list_loras",
        lambda: sorted(p.stem for p in paths.loras_dir().glob("*.safetensors")),
    )
    monkeypatch.setattr(install, "engine_exe", lambda: None)
    monkeypatch.setattr(hardware, "detect", lambda: HW)
    monkeypatch.setenv("LATENTROOM_ENGINE", f'"{sys.executable}" "{FAKE_SD_CLI}"')
    monkeypatch.setenv("FAKE_STEP_S", "0.001")
    return state


def without(state: SimpleNamespace, kind: str) -> None:
    """Remove every file of `kind` from the stub catalogue."""
    state.files = {k: v for k, v in state.files.items() if v.kind != kind}
