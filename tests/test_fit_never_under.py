"""The never-under check as a test: on the live 12 GB card,
fit never predicted less video memory than a run really used.

tests/data/vram_runs/ holds the live runs: results.jsonl (one record per run; it
holds no prompts) and <run>.vram.csv (seconds, MiB in use on the card, sampled every 0.5 s)."""

import json
from pathlib import Path

import pytest

from latentroom.engine import catalogue as c
from latentroom.engine import jobs
from latentroom.engine.hardware import Gpu, Hardware
from latentroom.engine.jobs import JobRequest

DATA = Path(__file__).parent / "data" / "vram_runs"
MiB = 1024**2
CARD, RAM_FREE = 12282 * MiB, 8584 * MiB  # RTX 4070 SUPER of the live runs
# What results.jsonl does not record, from the run script and the engine logs:
REFS = {"edit2p": 2, "edit2s": 2}
TURBO = {"turbo_lora"}
STEP1_S = {"edit2p": 35.36, "edit2s": 30.79}  # `1/25 - x s/it`: step 1 holds the OOM-retry peak
SKIP = {
    "lora": "not comparable: auto-fit kept the 5408 MiB text encoder on the GPU (lora.log)",
    "upscale": "one step: no sample between its start and end",
    "t2i_1024": "no vram.csv",
    "t2i_512": "no vram.csv",
}


def _runs() -> dict[str, dict]:
    runs = {}
    for line in (DATA / "results.jsonl").read_text(encoding="utf-8").splitlines():
        run = json.loads(line)
        runs[run["name"]] = run  # a re-run replaces the earlier record
    return {name: run for name, run in runs.items() if run["status"] == "done"}


def measured(run: dict) -> float:
    """MiB above idle (base_mib): peak from the end of step 1 to the last step; for a multi-ref
    edit from the start of step 1."""
    start = run["steps"]["first"][0][0] - STEP1_S.get(run["name"], 0)
    end = run["steps"]["last"][-1][0]
    rows = (DATA / f"{run['name']}.vram.csv").read_text(encoding="utf-8").split()
    samples = (map(float, row.split(",")) for row in rows)
    return max(used for t, used in samples if start <= t <= end) - run["base_mib"]


def predicted(run: dict) -> c.Fit:
    """fit as the runner calls it, on the card as it was when the run started."""
    gpu = Gpu("NVIDIA GeForce RTX 4070 SUPER", CARD, CARD - run["base_mib"] * MiB)
    hw = c.capped(Hardware("ok", "", gpu, "581.42", 0, RAM_FREE), run["max_vram"])
    turbo = run["name"] in TURBO
    req = JobRequest(kind=run["kind"], model_id=run["model"], prompt="x", turbo=turbo)
    w, h = run["out_size"]  # what the model made: jobs.job_size
    return c.fit(c.get(run["model"]), hw, w, h, REFS.get(run["name"], 0), jobs.lora_bytes(req))


@pytest.mark.parametrize("name", sorted(_runs()))
def test_fit_is_never_under_a_live_run(name):
    if name in SKIP:
        pytest.skip(SKIP[name])
    run = _runs()[name]
    fit, used = predicted(run), measured(run)
    assert fit.vram / MiB >= used, f"{name}: fit {fit.vram / MiB:.0f} MiB, used {used:.0f}"


def test_every_live_run_is_checked_or_skipped_for_a_reason():
    assert len(set(_runs()) - set(SKIP)) == 19 and set(SKIP) <= set(_runs())
