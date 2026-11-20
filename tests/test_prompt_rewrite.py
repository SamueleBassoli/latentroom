"""Enhance prompt engine: guards, model gate, official sampling, answer parsing,
the llama-server start/answer/stop. The LLM is a fake chat callable or tests/fake_llama_server.py;
nothing runs on the GPU."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from latentroom.engine import downloads, install, paths, prompt_rewrite, search
from latentroom.errors import LatentroomError

FAKE = Path(__file__).parent / "fake_llama_server.py"
FAILED = prompt_rewrite.FAILED

GOOD = json.dumps({"rewritten_prompt": "A lighthouse at dusk, warm light.", "wh_ratio": "3:2"})


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(install, "installed", lambda: {"llm-q4_k_m", "vae"})


def fake_chat(answer, calls=None):
    def chat(system, user, **sampling):
        if calls is not None:
            calls.append((system, user, sampling))
        return answer

    return chat


def test_sends_the_official_system_prompt_and_sampling(model):
    calls = []
    out = prompt_rewrite.enhance("  un faro al tramonto \n", fake_chat(GOOD, calls))
    assert out == "A lighthouse at dusk, warm light."
    ((system, user, sampling),) = calls
    digest = hashlib.sha256(prompt_rewrite.SYSTEM_PROMPT.read_bytes()).hexdigest()
    assert digest == "a77c9a06c59b120741141d9514b95682bb8761d02bec49ca61def7b2b3d9fb99"
    assert system == prompt_rewrite.SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert user == "un faro al tramonto"  # the idea only, no added length line
    assert sampling == {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
        "presence_penalty": 1.5, "max_tokens": 1500,
    }  # fmt: skip


@pytest.mark.parametrize(
    "idea, message",
    [("", "Write an idea first."), ("  \n ", "Write an idea first."),
     ("x" * 2001, "Keep the idea under 2000 characters.")],
)  # fmt: skip
def test_guards_never_call_the_model(model, idea, message):
    calls = []
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance(idea, fake_chat(GOOD, calls))
    assert (e.value.code, e.value.message, e.value.status) == ("bad_request", message, 400)
    assert calls == []
    assert prompt_rewrite.enhance("x" * 2000, fake_chat(GOOD))  # the limit itself is fine


def test_text_model_must_be_downloaded(monkeypatch):
    monkeypatch.setattr(install, "installed", lambda: {"vae"})
    calls = []
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance("a lighthouse at dusk", fake_chat(GOOD, calls))
    assert (e.value.code, e.value.status) == ("model_missing", 404) and calls == []
    assert e.value.message == "The text encoder is not downloaded yet. Get it in the Models tab."


@pytest.mark.parametrize(
    "answer",
    [
        '<think>a draft {"rewritten_prompt": "no"}</think>' + GOOD,
        "Here it is:\n```json\n" + GOOD + "\n```\nHope this helps {smile}",
        json.dumps({"rewritten_prompt": "old"}) + " then " + GOOD,  # the last object wins
        json.dumps({"rewrited_prompt": "A lighthouse at dusk, warm light."}),  # pe_core key
        "draft</think>" + GOOD,  # template ate the opening <think>
    ],
)
def test_parse_answer(answer):
    assert prompt_rewrite.parse_answer(answer) == "A lighthouse at dusk, warm light."


@pytest.mark.parametrize(
    "answer",
    ["", "A lighthouse at dusk.", '{"rewritten_prompt": "  "}', '{"rewritten_prompt": 3}',
     '{"rewritten_prompt": "cut off', "<think>" + GOOD, '["rewritten_prompt"]'],
)  # fmt: skip
def test_unusable_answer_is_a_plain_error_never_raw_text(model, answer):
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance("a lighthouse at dusk", fake_chat(answer))
    assert (e.value.code, e.value.message, e.value.status) == (
        "enhance_failed", "Could not improve this prompt. Try again.", 502
    )  # fmt: skip


def test_a_crashing_model_is_a_plain_error(model, caplog):
    def chat(system, user, **sampling):
        raise ConnectionError("refused: a lighthouse at dusk")

    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance("a lighthouse at dusk", chat)
    assert e.value.code == "enhance_failed"
    assert "ConnectionError" in caplog.text and "lighthouse" not in caplog.text  # no prompt


@pytest.fixture
def fake_llama(model, monkeypatch):
    """The real llama_chat path with tests/fake_llama_server.py as llama-server; yields the
    servers it started."""
    monkeypatch.setenv("LATENTROOM_LLAMA", f'"{sys.executable}" "{FAKE}"')
    started, real = [], prompt_rewrite.subprocess.Popen

    def popen(argv, **kw):
        started.append(real(argv, **kw))
        return started[-1]

    monkeypatch.setattr(prompt_rewrite.subprocess, "Popen", popen)
    yield started
    prompt_rewrite.stop()


def test_llama_chat_starts_the_server_answers_and_stops_it(fake_llama):
    out = prompt_rewrite.enhance("a lighthouse at dusk")  # the fake rejects --device none: GPU
    assert out == "An English prompt about: a lighthouse at dusk"
    listing, proc = fake_llama  # --list-devices (the fake: no card), then the server
    assert "--list-devices" in listing.args and "--device" not in proc.args  # llama.cpp picks
    assert proc.poll() is not None and prompt_rewrite._proc is None  # nothing stays loaded
    assert "lighthouse" not in (paths.logs_dir() / prompt_rewrite.LOG).read_text()  # no idea


# llama-server --list-devices as llama.cpp b11850 prints it (common/arg.cpp:1164-1173)
HYBRID = """Available devices:
  Vulkan0: AMD Radeon(TM) 780M Graphics (16384 MiB, 15950 MiB free)
  Vulkan1: NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB, 7724 MiB free)
"""


@pytest.mark.parametrize(
    "listing, device",
    [
        (HYBRID, "Vulkan1"),
        (HYBRID.replace("\n", "\r\n"), "Vulkan1"),
        ("Available devices:\n  Vulkan0: NVIDIA RTX 3060 (12288 MiB, 11000 MiB free)\n", None),
        ("Available devices:\n  (none)\n", None),
        (HYBRID.replace("AMD Radeon(TM) 780M Graphics", "NVIDIA GeForce RTX 3090"), None),
        (HYBRID.replace("NVIDIA GeForce RTX 4060 Laptop GPU", "Intel(R) Arc(TM) A770"), None),
        ("error: invalid argument: --list-devices\n", None),
        ("", None),
    ],
)  # fmt: skip
def test_pick_nvidia_only_when_several_cards(listing, device):
    assert prompt_rewrite.pick_nvidia(listing) == device


def test_llama_chat_passes_the_nvidia_card(fake_llama, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_DEVICES", HYBRID)
    prompt_rewrite.enhance("a lighthouse at dusk")
    assert fake_llama[-1].args[2:4] == ["--device", "Vulkan1"]  # right after the executable


def test_llama_chat_blocked_by_windows(fake_llama, monkeypatch):
    monkeypatch.setenv("FAKE_LLAMA_FAIL", "blocked")  # exits 0xC0000135: a DLL quarantined
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance("a lighthouse at dusk")
    assert (e.value.code, e.value.message) == ("engine_blocked", prompt_rewrite.procs.BLOCKED)


@pytest.mark.parametrize("fail", ["start", "chat"])
def test_llama_chat_failures_say_so_and_stop_the_server(fake_llama, monkeypatch, fail):
    monkeypatch.setenv("FAKE_LLAMA_FAIL", fail)  # the server dies at once / answers HTTP 500
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite.enhance("a lighthouse at dusk")
    assert (e.value.code, e.value.status, e.value.message) == ("enhance_failed", 502, FAILED)
    assert all(p.poll() is not None for p in fake_llama) and prompt_rewrite._proc is None


def test_first_click_downloads_the_shared_llama_runtime(monkeypatch):
    calls = []

    def command():
        if not calls:
            calls.append("missing")
            raise LatentroomError("model_missing", install.SEARCH_MISSING, 404)
        return ["llama-server.exe"]

    def start(item):
        calls.append(item)
        return downloads.DownloadState(item.id, item.name, "done", item.size, item.size, 0, 0, None)

    monkeypatch.setattr(search, "_command", command)
    monkeypatch.setattr(downloads, "start", start)
    assert prompt_rewrite._runtime() == ["llama-server.exe"]
    assert calls == ["missing", install.LLAMA_ZIP]


def test_a_failed_runtime_download_says_why(monkeypatch):
    def command():
        raise LatentroomError("model_missing", install.SEARCH_MISSING, 404)

    def start(item):
        gave_up = downloads.GAVE_UP
        return downloads.DownloadState(item.id, item.name, "failed", 0, item.size, 0, None, gave_up)

    monkeypatch.setattr(search, "_command", command)
    monkeypatch.setattr(downloads, "start", start)
    with pytest.raises(LatentroomError) as e:
        prompt_rewrite._runtime()
    assert (e.value.code, e.value.message) == ("enhance_failed", downloads.GAVE_UP)
