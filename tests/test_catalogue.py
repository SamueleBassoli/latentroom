import bisect
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from latentroom.engine import catalogue as c
from latentroom.engine import install
from latentroom.engine.hardware import Gpu, Hardware, gb
from latentroom.errors import LatentroomError

GiB = 1024**3
KINDS = {"diffusion", "vae", "llm", "llm_vision", "upscaler", "lora", "detector", "engine"}
SEARCH = {"embedding", "embedding_vision", "llama"}  # Smart search


def card(vram_gb: float, ram_free_gb: float = 10, busy_gb: float = 0) -> Hardware:
    """A fake PC: a `vram_gb` card with the Windows desktop on it, and `busy_gb` more that other
    programs (or Latentroom's own engine) hold right now; RAM 16 GB with some free."""
    total = int(vram_gb * GiB)
    gpu = Gpu(f"RTX {vram_gb} GB", total, total - c.DESKTOP_RESERVE - int(busy_gb * GiB))
    return Hardware("ok", "", gpu, "581.42", 16 * GiB, int(ram_free_gb * GiB))


def busy(hw: Hardware, free_gb: float = 0.5) -> Hardware:
    """The same card while Latentroom runs a job or Enhance, or another app holds video memory."""
    return replace(hw, gpu=replace(hw.gpu, vram_free=int(free_gb * GiB)))


# The real 12 GB card (12282 MiB) and the 8 / 6 GB cards it stood in for
REAL_12, REAL_8, REAL_6 = card(12282 / 1024), card(8), card(6)


def test_catalogue_file_is_complete():
    files = c.files()
    assert {f.kind for f in files.values()} <= KINDS | SEARCH
    assert [f.kind for f in files.values()].count("upscaler") == 1
    assert [f.kind for f in files.values()].count("lora") == 1
    (face,) = [f for f in files.values() if f.kind == "detector"]  # optional: in no `needs`
    assert face.licence == "AGPL-3.0" and face.size == 6_033_980
    for f in files.values():
        assert f.url.startswith("https://") and f.size > 0 and f.name and f.licence
        # every download is checked, Real-ESRGAN too (its sha256 pinned by hand)
        assert re.fullmatch(r"[0-9a-f]{64}", f.sha256 or ""), f.id
        if "huggingface.co" in f.url:  # pinned to a commit, never to a moving branch
            assert re.search(r"/resolve/[0-9a-f]{40}/", f.url), f.id
        if f.id.startswith("uc-"):  # abenzerps' own repo is gone: the same files, mirrored
            assert "/KasugaiSakura/Qwen-Image-2.1-Uncensored-Abenzerps-GGUF/" in f.url, f.id
    for pair in c.load():
        for m in (pair.standard, pair.uncensored):
            if m:
                assert m.file.kind == "diffusion"
                assert sorted(files[i].kind for i in m.needs) == ["llm", "llm_vision", "vae"]
    assert all(p.uncensored is None or p.uncensored.adult for p in c.load())
    assert all(p.standard.experimental for p in c.load() if p.fast)


def test_smart_search_is_three_pinned_files_in_no_model():
    """Pins: llama.cpp b11850 Vulkan zip + EmbeddingGemma 2 Q8_0 + its mmproj."""
    files = c.files()
    by_kind = {f.kind: f for f in files.values() if f.kind in SEARCH}
    assert [f.kind for f in files.values()].count("embedding") == 1
    assert {k: (f.size, f.sha256[:8]) for k, f in by_kind.items()} == {
        "llama": (36969567, "55104cbb"),
        "embedding": (309855456, "2188ac1d"),
        "embedding_vision": (554821024, "c4a8a526"),
    }
    assert "/58faf9094088" in by_kind["embedding"].url and "/b11850/" in by_kind["llama"].url
    assert "win-vulkan-x64" in by_kind["llama"].name  # one runtime: GPU for Enhance prompt
    for pair in c.load():  # optional: no model needs it
        for m in (pair.standard, pair.uncensored):
            assert not m or not {files[i].kind for i in m.needs} & SEARCH
    assert not {f.kind for f in c.required("std-q4_k")} & SEARCH


def test_recommend_is_a_level_with_both_files_that_run_well():
    """8 GB: Light runs on the GPU alone but has no Uncensored file; Good's Uncensored file is
    1.2 slower, Balanced is 1.1 on both: Balanced wins, for Standard and Uncensored alike."""
    fits = {m.id: c.fit(m, REAL_8) for p in c.load() for m in (p.standard, p.uncensored) if m}
    assert fits["std-q3_k"].label == "full_gpu"
    assert [fits[i].slowdown for i in ("std-q5_0", "uc-q5_k_m")] == [1.1, 1.2]
    assert [fits[i].slowdown for i in ("std-q4_k", "uc-q4_0")] == [1.1, 1.1]
    assert all(map(c.runs_well, (fits["uc-q5_k_m"], fits["std-q4_k"])))
    assert not c.runs_well(fits["std-q6_k"])  # 1.4
    assert c.recommend(c.load(), REAL_8).id == "q4"
    # a tie goes to the better quality: on 12 GB every level is full GPU
    assert c.recommend(c.load(), REAL_12).id == "q8"


@pytest.mark.parametrize(
    ("tier", "hw", "free_gib", "pick"),
    [  # video memory each tier leaves the engine (desktop on the card)
        ("12 real", REAL_12, 11.15, "q8"),
        ("8 real", REAL_8, 7.15, "q4"),  # was Fast light: setup and Models now agree
        ("8 limit 6.2", c.capped(REAL_12, 6.2), 7.07, "q4"),
        ("6 real", REAL_6, 5.15, "q3"),  # no level with both files runs well
        ("6 limit 4.2", c.capped(REAL_12, 4.2), 5.07, "q2"),
    ],
)
def test_recommend_per_card_tier(tier, hw, free_gib, pick):
    """The same picks from the card alone, however busy it is right now."""
    assert abs(c.usable(hw) / GiB - free_gib) < 0.01, tier
    assert c.recommend(c.load(), hw).id == c.recommend(c.load(), busy(hw)).id == pick, tier


def test_recommend_falls_back_to_setups_first_tile():
    """No level with both files runs well: the best non-Fast level whose files run well (6 GB:
    Light). Nothing runs well: the level that runs with the smallest slowdown (4 GB: Smallest),
    never the slowest one."""
    six = {p.id: [c.fit(m, REAL_6) for m in (p.standard, p.uncensored) if m] for p in c.load()}
    assert not [i for i, fs in six.items() if len(fs) == 2 and all(map(c.runs_well, fs))]
    assert c.recommend(c.load(), REAL_6).id == "q3"
    four = card(4)
    fits = [c.fit(m, four) for p in c.load() for m in (p.standard, p.uncensored) if m]
    assert not [f for f in fits if c.runs_well(f)]
    assert c.fit(c.get("std-q8_0"), four).label == "partial"
    assert c.recommend(c.load(), four).id == "q2"


def _speeds(hw: Hardware) -> dict[str, str]:
    return {m.id: c.speed(c.fit(m, hw)) for p in c.load() for m in (p.standard, p.uncensored) if m}


def test_wizard_speeds_never_show_a_time():
    """No time on any card: the fit is picked by video memory, not card speed, so a time from
    one 12 GB card is wrong for another. A file that does not run well says it is slower, never
    by how much; one that runs well (Full GPU, or partial at most 1.2 slower) says nothing."""
    s12, s8, s6, s4 = _speeds(REAL_12), _speeds(REAL_8), _speeds(REAL_6), _speeds(card(4))
    assert s12["std-q8_0"] == s12["turbo-q8_0"] == s12["uc-q8_0"] == s12["std-q4_k"] == ""
    assert s8["turbo-q3_k_xl"] == ""  # full GPU at 8 GB
    assert s8["std-q8_0"] == s6["std-q8_0"] == s6["turbo-q3_k_xl"] == "Works, but slower."
    assert s8["std-q6_k"] == s6["std-q4_k"] == s4["turbo-q3_k_xl"] == "Works, but slower."
    assert s8["std-q4_k"] == s8["uc-q5_k_m"] == s6["std-q2_k"] == ""  # partial, 1.1 or 1.2
    for s in (s12, s8, s6, s4):
        assert set(s.values()) <= {"", "Works, but slower."}  # no number, no unit


def test_fast_help_says_faster_without_a_number():
    no_gpu = Hardware("no_nvidia", "", None, None, 0, 0)
    for hw in (REAL_12, REAL_8, REAL_6, card(4), no_gpu):
        assert c.fast_help(hw) == "Faster, a bit less detail. 8 steps."


def test_balanced_pair_runs_fully_from_7_3_gib():
    """With the 4.15 GB uncensored Q4_0 both Balanced sides fit on the GPU (the 4.60 GB
    Q4_K_M did not) from ~7.3 GiB for the engine, e.g. a 12 GB card held to 6.5 GB. A real 8 GB
    card leaves 7.15 GiB (budget: CUDA context + sd.cpp's 512 MiB margin): Q4 misses there
    by a little, runs well, and is still the pick (test_recommend_per_card_tier)."""
    hw = c.capped(card(12), 6.5)
    pair = c.recommend(c.load(), hw)
    assert pair.id == "q4" and pair.uncensored.quant == "Q4_0"
    assert c.fit(pair.standard, hw).label == c.fit(pair.uncensored, hw).label == "full_gpu"
    assert c.fit(pair.standard, REAL_8).label == "partial"


def test_turbo_is_a_fast_standard_option_only():
    """No "Turbo uncensored" item and none of its AtomicChat files."""
    turbo = [p for p in c.load() if p.fast]
    assert len(turbo) == 4
    assert all(p.uncensored is None and p.standard.family == "turbo" for p in turbo)
    assert all(not p.standard.adult for p in turbo)
    assert not [f for f in c.files().values() if "AtomicChat" in f.url]
    assert {m.family for m in c._catalogue()[1].values()} == {"standard", "uncensored", "turbo"}


def test_pairs_best_quality_first_turbo_last():
    pairs = c.load()
    assert pairs[0].id == "q8" and pairs[0].uncensored.family == "uncensored"
    fast = [p.fast for p in pairs]
    assert fast == sorted(fast)  # every Turbo pair after every standard one
    turbo = pairs[-1].standard
    assert (turbo.steps, turbo.cfg, turbo.sigmas) == (8, 1.0, "qwen-image-2.1-turbo")


def test_defaults_40_steps_guidance_2_5_and_turbo_unchanged():
    models = c._catalogue()[1].values()
    std = {(m.steps, m.cfg) for m in models if m.family != "turbo"}
    turbo = {(m.steps, m.cfg) for m in models if m.family == "turbo"}
    assert (std, turbo) == ({(40, 2.5)}, {(8, 1.0)})


def test_get_and_required():
    with pytest.raises(LatentroomError) as e:
        c.get("nope")
    assert (e.value.code, e.value.status) == ("model_missing", 404)
    kinds = [f.kind for f in c.required("std-q4_k")]
    assert kinds == ["engine", "engine", "vae", "llm", "llm_vision", "diffusion"]


def test_fit_matches_the_measured_anchors():
    """Measured 2026-10-23 on 4070 SUPER 12 GB (sd-cli e779d4f): VRAM above idle while
    sampling, max of the run. Never predict less than measured, nor 1.2 GiB more."""
    MiB = 1024**2
    hw = card(12)
    q4, q8 = c.get("std-q4_k"), c.get("std-q8_0")
    for model, w, h, refs, measured in (
        (q4, 1024, 1024, 0, 5377),
        (q4, 1312, 800, 0, 5372),
        (q4, 512, 512, 0, 4495),
        (q4, 2048, 2048, 0, 9337),  # detail enhance
        (q8, 1024, 1024, 0, 8797),
    ):
        f = c.fit(model, hw, w, h, refs)
        assert f.label == "full_gpu" and f.slowdown == 1.0
        assert measured * MiB <= f.vram < measured * MiB + 1.2 * GiB, (model.id, w, h, refs)


def test_fit_counts_the_lora_files_on_the_gpu():
    """Live turbo_lora (std-q4_k 1024x1024 + Turbo add-on, uncapped) used 6153 MiB,
    482 MiB over the LoRA-less prediction."""
    MiB = 1024**2
    hw, q4 = card(12), c.get("std-q4_k")
    (turbo,) = [f for f in c.files().values() if f.kind == "lora"]
    f = c.fit(q4, hw, 1024, 1024, 0, turbo.size)
    assert f.label == "full_gpu" and 6153 * MiB <= f.vram < 6153 * MiB + 1.2 * GiB
    assert c.fit(q4, hw, lora_bytes=3 * turbo.size).vram == c.fit(q4, hw).vram + 3 * turbo.size
    # LoRAs can tip a card that just fits over the edge
    tight = card(8.5)
    assert c.fit(q4, tight).label == "full_gpu"
    assert c.fit(q4, tight, lora_bytes=turbo.size).label == "partial"


def test_fit_covers_step_1_of_a_two_ref_edit():
    """Live edit2p / edit2s (std-q4_k 1024x1024, refs 1 + 0.25 MP, uncapped) sampled at
    7100 MiB, but in step 1 the engine tried to keep the refs' work for later steps, climbed to
    10317 / 10635 MiB, ran out and retried without it: 10.5 s per step, vs 4.1 s for the 1-ref
    edit (lora run) that kept it. A run that needs the retry is partial, never full_gpu."""
    MiB = 1024**2
    hw, q4 = card(12), c.get("std-q4_k")
    f = c.fit(q4, hw, 1024, 1024, 2)
    assert f.label == "partial" and f.slowdown >= 2
    assert 10635 * MiB <= f.vram < 10317 * MiB + 1.2 * GiB
    assert f.reason.endswith(", so it is slower."), f.reason  # the number is never shown
    assert c.fit(q4, hw, 1024, 1024, 1).label == "full_gpu"  # the 1-ref edit kept it
    big = c.fit(q4, card(24), 1024, 1024, 2)
    assert big.label == "full_gpu" and big.vram > f.vram  # room for it, and it is counted
    streamed = c.fit(c.get("std-q8_0"), hw, 1024, 1024, 2)  # DiT in RAM: retried as well
    assert streamed.label == "partial" and streamed.vram == f.vram and streamed.slowdown > 2.5


def test_two_refs_of_a_1024_edit_cost_a_3_mp_picture():
    """sd.cpp resizes every ref to the output area (image.cpp:292, log
    "resize vae ref image 1 from 512x512 to 768x768"), so the DiT works on 1 + 2 MP. With room
    for the refs' cache (24 GB): the 3 MP picture's figure plus that cache."""
    hw, q4 = card(24), c.get("std-q4_k")
    edit, three_mp = c.fit(q4, hw, 1024, 1024, 2), c.fit(q4, hw, 1024, 3 * 1024)
    assert edit.label == three_mp.label == "full_gpu"
    assert abs(edit.vram - three_mp.vram - 2 * c.PREFIX_CACHE_PER_REF) <= 1


def test_refs_of_an_edit_above_1_mp_cost_the_output_area():
    """sd.cpp resizes every ref to the output area, so the refs of a 2048x2048
    edit are 4 MP each, not 1. The repro (12282 MiB card, 11071 free): fit said partial x4.1
    for 7725 MiB of work; at 3 x 4 MP it is 15450 MiB, more than the whole card."""
    MiB, q4 = 1024**2, c.get("std-q4_k")
    qa_card = Hardware("ok", "", Gpu("RTX 4070 SUPER", 12282 * MiB, 11071 * MiB), "", 0, 0)
    f = c.fit(q4, qa_card, 2048, 2048, 2)
    assert f.label == "too_big" and f.vram >= 15450 * MiB
    assert "2048x2048 with these references" in f.reason
    hw = card(48)  # room for everything: the 12 MP picture's figure plus the refs' cache
    edit, twelve_mp = c.fit(q4, hw, 2048, 2048, 2), c.fit(q4, hw, 2048, 3 * 2048)
    assert edit.label == twelve_mp.label == "full_gpu"
    assert abs(edit.vram - twelve_mp.vram - 2 * c.PREFIX_CACHE_PER_REF) <= 1


@pytest.mark.parametrize(("cap", "measured"), [(4, 1.29), (6, 1.08)])
def test_slowdown_matches_measured_max_vram_runs(cap, measured):
    """--max-vram 4 / 6 at 1024x1024 put the Q4_K DiT in RAM (sd-cli printed `params cpu`).
    Even fully cached (6 GiB) it was 8% slower, so a DiT in RAM is never full_gpu."""
    f = c.fit(c.get("std-q4_k"), c.capped(card(12), cap))
    assert f.label == "partial" and measured <= f.slowdown <= 2 * measured - 0.9
    assert f.reason.endswith(", so it is slower."), f.reason  # the number is never shown


def test_video_memory_limit_is_the_engine_budget():
    """settings.max_vram_gb is sd-cli --max-vram (GiB), its whole budget, no 512 MiB
    margin. Live m1024_4 (limit 4) used 4448 MiB above idle: 4096 + the CUDA context."""
    hw, q4 = card(12), c.get("std-q4_k")
    assert c.capped(hw, 0) is hw and c.capped(hw, 99) == hw  # no limit / one above the card
    four = c.capped(hw, 4)
    assert c.usable(four) == int(4 * GiB + c.CUDA_CTX + c.SD_MARGIN)
    assert c.fit(q4, hw).label == "full_gpu"
    assert c.fit(q4, four).label == "partial"  # the old bug: "fastest" at any limit
    assert 4448 * 1024**2 <= c.fit(q4, four).vram
    no_gpu = Hardware("no_nvidia", "", None, None, 16 * GiB, 10 * GiB)
    assert c.capped(no_gpu, 4) is no_gpu


BUSY = (0, 0.5, 1.5, 2.0)  # GiB of video memory other programs (or Latentroom) hold right now
# card GB -> recommend_size(), "pair@size label of its standard side" (10 GiB RAM free), the same
# for every BUSY (judged on the card). Every card from 2.5 GB up gets a pair that runs, at
# a smaller size if it must. Where nothing runs well, the level with the smallest slowdown.
EXPECTED = {
    2.5: "q2@512 partial",
    4: "q2@1024 partial",
    6: "q3@1024 partial",
    8: "q4@1024 partial",
    12: "q8@1024 full_gpu",
    16: "q8@1024 full_gpu",
    24: "q8@1024 full_gpu",
}


@pytest.mark.parametrize(("vram", "busy_gb"), [(v, b) for v in EXPECTED for b in BUSY])
def test_recommend_size_per_card_and_busy_vram(vram, busy_gb):
    hw = card(vram, busy_gb=busy_gb)
    pair, w, h = c.recommend_size(c.load(), hw)
    sides = [c.fit(m, hw, w, h) for m in (pair.standard, pair.uncensored) if m]
    assert all(f.label != "too_big" for f in sides) and w == h
    assert f"{pair.id}@{w} {sides[0].label}" == EXPECTED[vram]
    assert pair is c.recommend(c.load(), hw, w, h)


def test_fit_while_latentroom_is_busy_is_fit_idle():
    """Fit is judged on the card, never on the live free video memory. A job or
    Enhance running, or another app, changes nothing (it made fit say "Too big"); the user's
    limit still does."""
    for hw in (REAL_6, REAL_8, REAL_12, c.capped(REAL_12, 4)):
        for m in (m for p in c.load() for m in (p.standard, p.uncensored) if m):
            for size, refs in ((1024, 0), (512, 0), (1024, 1)):
                idle = c.fit(m, hw, size, size, refs)
                assert c.fit(m, busy(hw, 0.2), size, size, refs) == idle, (m.id, size, refs)
        assert c.fast_help(busy(hw, 0.2)) == c.fast_help(hw)
    q8 = c.get("std-q8_0")
    assert c.fit(q8, c.capped(REAL_12, 4)) != c.fit(q8, REAL_12)


def test_qa01_small_card_gets_a_smaller_size_instead_of_nothing():
    hw = card(3)
    assert c.recommend(c.load(), hw) is None  # nothing runs at 1024x1024...
    pair, w, h = c.recommend_size(c.load(), hw)  # ...so the wizard offers 768x768
    assert (w, h) == (768, 768) and c.fit(pair.standard, hw, w, h).label == "partial"
    assert c.recommend_size(c.load(), card(4, ram_free_gb=6))


def test_encoder_without_room_in_ram_does_not_block_a_full_gpu_model():
    """auto-fit puts an encoder that finds no RAM on disk; only a DiT in RAM must fit there."""
    f = c.fit(c.get("std-q4_k"), card(12, ram_free_gb=2))
    assert f.label == "full_gpu"


def test_partial_says_it_is_slower():
    f = c.fit(c.get("std-q6_k"), card(8))
    assert f.label == "partial" and f.slowdown > 1
    assert f.reason.endswith(", so it is slower."), f.reason  # the number is never shown


def test_bigger_images_and_references_need_more():
    hw, m = card(12), c.get("std-q4_k")
    small, big, refs = c.fit(m, hw), c.fit(m, hw, 1536, 1536), c.fit(m, hw, refs=2)
    assert small.vram < big.vram and small.vram < refs.vram
    assert refs.ram > small.ram  # the vision encoder waits in RAM too
    huge = c.fit(m, card(6), 2048, 2048)
    assert huge.label == "too_big" and "smaller size" in huge.reason


def test_too_big_when_ram_runs_out_but_not_when_ram_is_unknown():
    m = c.get("std-q8_0")  # 7.69 GB: on a 6 GB card it must live in RAM
    low = c.fit(m, card(6, ram_free_gb=4))
    assert low.label == "too_big" and "free RAM" in low.reason
    assert c.fit(m, card(6, ram_free_gb=0)).label == "partial"  # 0 = could not read RAM


def test_no_gpu_runs_nothing():
    hw = Hardware("no_nvidia", "", None, None, 16 * GiB, 10 * GiB)
    assert c.fit(c.get("std-q2_k"), hw).label == "too_big"
    assert c.recommend(c.load(), hw) is None
    assert c.recommend_size(c.load(), hw) is None


DOCS = Path(c.__file__).parents[2]


def flat(doc: str) -> str:
    return " ".join((DOCS / doc).read_text(encoding="utf-8").split())


def test_readme_names_the_smallest_card_the_fit_check_allows():
    """README's floor for small cards is fit's own, rounded up, for the smallest model file."""
    small = min((f for f in c.files().values() if f.kind == "diffusion"), key=lambda f: f.size)
    model = c.get(small.id)

    def floor(side: int) -> str:  # the smallest card fit does not call too big, in MiB steps
        def runs(mib: int) -> bool:
            return c.fit(model, card(mib / 1024), side, side).label != "too_big"

        return gb(bisect.bisect_left(range(32 * 1024), True, key=runs) * 2**20, up=True)

    assert (
        f"the smallest model (a {gb(small.size)} file) run with {floor(512)} of video memory at"
        f" 512 × 512, and {floor(1024)} at 1024 × 1024."
    ) in flat("README.md")


def test_the_docs_name_every_site_setup_downloads_from():
    """No mirror: README and INSTALL name the sites (and the servers they hand files over from)."""
    items = [*c.files().values(), *install.ENGINE_FILES, install.LLAMA_ZIP]
    assert {urlsplit(f.url).hostname for f in items} == {"github.com", "huggingface.co"}
    for doc in ("README.md", "docs/INSTALL.md"):
        text = flat(doc)
        assert "github.com (the engines) and huggingface.co (the models)" in text, doc
        assert "release-assets.githubusercontent.com and addresses ending in hf.co" in text, doc
