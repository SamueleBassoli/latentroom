"""Enhance prompt: a short idea, any language -> one long English prompt, written by
the text-encoder LLM Latentroom already downloads, with Qwen's official t2i rewrite system prompt
(latentroom/data/prompt_rewrite/, unmodified; Qwen research licence + NOTICE next to it).
Named prompt_rewrite, not enhance: job kind "enhance" is already "Add detail" (jobs.KINDS).
The LLM is a callable chat(system, user, **SAMPLING) -> answer text. Tests pass a fake; the
real one is `llama_chat`: llama-server (the shared llama.cpp Vulkan build, on the GPU) started
for one answer and stopped before it returns, so nothing stays loaded.
Run it through runner.Queue.run_exclusive: never next to a picture job."""

import atexit
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from latentroom.engine import catalogue, downloads, install, jobs, paths, procs, search
from latentroom.errors import LatentroomError

log = logging.getLogger(__name__)
SYSTEM_PROMPT = (
    Path(__file__).resolve().parent.parent / "data" / "prompt_rewrite" / "system_prompt_t2i.txt"
)
MAX_CHARS = 2000
# Official t2i profile (QwenLM/Qwen-Image-2.1 prompt_rewrite/pe_core.py PROFILES["t2i"]);
# max_tokens 1500 (tested). No fixed seed: a second click gives a new prompt.
SAMPLING = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "max_tokens": 1500,
}
KEYS = ("rewritten_prompt", "rewrited_prompt")  # pe_core accepts the misspelt key too
FAILED = "Could not improve this prompt. Try again."
CTX = 4096  # the 2.4k-token system prompt + the idea + a 1500-token answer
START_S = 120.0  # loaded in ~3 s from the file cache; a cold 5 GB read is slower
CALL_S = 600.0  # one answer: ~5 s on the GPU, ~2 min if llama.cpp falls back to the CPU
FETCH_S = 600.0  # the 37 MB llama.cpp zip on a first click, download retries included
LOG = "enhance.log"  # in paths.logs_dir(): llama-server warnings and errors (-lv 2), no idea
LIST_S = 30.0  # llama-server --list-devices: it starts every graphics driver, then exits
# One line of llama-server --list-devices (llama.cpp b11850 common/arg.cpp:1173, device names
# "Vulkan0".. from ggml-vulkan.cpp:16251): "  Vulkan1: NVIDIA GeForce RTX 4060 Laptop GPU
# (8188 MiB, 7724 MiB free)"
_DEVICE = re.compile(r"^\s+(\S+): (.+) \(\d+ MiB, \d+ MiB free\)\r?$", re.M)

Chat = Callable[..., str]
_lock = threading.Lock()
_proc: subprocess.Popen | None = None  # the llama-server of the call in flight, if any


def llama_chat(system: str, user: str, **sampling) -> str:
    """The text encoder's answer, run as a chat model by llama-server: 127.0.0.1, a random port
    and API key per call, the GPU when there is one (llama.cpp fits the layers to free VRAM).
    Stopped before returning: its ~6 GB of VRAM go back to pictures. Raises LatentroomError."""
    global _proc
    model = paths.models_dir() / jobs._need("llm", "The text encoder").name
    port, key = search._free_port(), secrets.token_urlsafe(24)
    cmd = _runtime()
    argv = [
        *cmd, *_nvidia(cmd), "-m", str(model), "-c", str(CTX), "-np", "1",
        "--host", "127.0.0.1", "--port", str(port), "--no-webui", "--offline", "-lv", "2",
    ]  # fmt: skip
    try:
        with _lock, open(paths.logs_dir() / LOG, "wb") as logf:
            _proc = proc = procs.start(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=os.environ | {"LLAMA_API_KEY": key},  # a command line is readable by all
                creationflags=search._NO_WINDOW,
                start_new_session=os.name != "nt",
            )
        _wait_ready(proc, port)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        body = json.dumps({"messages": messages, **sampling}).encode()
        status, raw = search._request(port, "POST", "/v1/chat/completions", key, body, CALL_S)
        if status != 200:  # the reason is in LOG; the reply may quote the idea
            log.warning("enhance prompt: llama-server answered HTTP %s", status)
            raise LatentroomError("enhance_failed", FAILED, 502)
        return json.loads(raw)["choices"][0]["message"]["content"]
    finally:
        stop()


def _runtime() -> list[str]:
    """llama-server's command (env LATENTROOM_LLAMA in tests, like Smart search). A first click
    downloads the shared llama.cpp zip if it is missing; the Models screen shows it too."""
    try:
        return search._command()
    except LatentroomError as e:
        if e.code != "model_missing":
            raise
    _fetch(install.LLAMA_ZIP)
    return search._command()


def _nvidia(cmd: list[str]) -> list[str]:
    """["--device", <the NVIDIA card>] when llama-server sees several graphics cards, so the
    text encoder lands on the card pictures use; [] (llama.cpp picks, as before) otherwise or
    when the list fails."""
    try:
        listing = subprocess.run(
            [*cmd, "--list-devices"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LIST_S,
            creationflags=search._NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("enhance prompt: llama-server --list-devices failed: %s", e)
        return []
    name = pick_nvidia(listing)
    if name:
        log.info("enhance prompt: llama-server on %s", name)
    return ["--device", name] if name else []


def pick_nvidia(listing: str) -> str | None:
    """The device name of the one NVIDIA card in `llama-server --list-devices` output that lists
    two or more cards; None for one card, no or several NVIDIA cards, or any other text."""
    devices = _DEVICE.findall(listing)
    nvidia = [name for name, description in devices if "NVIDIA" in description.upper()]
    return nvidia[0] if len(devices) > 1 and len(nvidia) == 1 else None


def _fetch(item: catalogue.FileItem) -> None:
    st = downloads.start(item)
    deadline = time.monotonic() + FETCH_S
    while st.status in downloads.ACTIVE and time.monotonic() < deadline:
        time.sleep(0.25)
        st = next((s for s in downloads.progress() if s.id == item.id), st)
    if st.status != "done":  # still running on: the next click finds it done
        log.warning("enhance prompt: the llama.cpp download is %s", st.status)
        raise LatentroomError("enhance_failed", st.error or FAILED, 502)


def _wait_ready(proc: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + START_S
    while not search._healthy(port):
        if proc.poll() is not None or time.monotonic() > deadline:
            code = proc.poll()
            ended = procs.note_exit(code, paths.logs_dir() / LOG)
            log.error("llama-server did not start (%s), see %s", ended, LOG)
            if procs.blocked(code):
                raise LatentroomError("engine_blocked", procs.BLOCKED, 409)
            raise LatentroomError("enhance_failed", FAILED, 502)
        time.sleep(0.1)


@atexit.register
def stop() -> None:
    """Kill the running llama-server, if any, and wait for its exit (Windows keeps the exe and
    the model locked until then). After every call and on Latentroom exit; safe any time."""
    global _proc
    with _lock:
        proc, _proc = _proc, None
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            log.warning("llama-server (pid %s) did not exit", proc.pid)


def enhance(idea: str, chat: Chat | None = None) -> str:
    """The idea rewritten as one English prompt. Raises LatentroomError: bad_request (empty or
    over MAX_CHARS), model_missing 404 (text encoder not downloaded), enhance_failed 502 (no
    usable answer), plus whatever the chat raises as LatentroomError. Never returns raw model
    text."""
    idea = idea.strip()
    if not idea:
        raise LatentroomError("bad_request", "Write an idea first.", 400)
    if len(idea) > MAX_CHARS:
        raise LatentroomError("bad_request", f"Keep the idea under {MAX_CHARS} characters.", 400)
    jobs._need("llm", "The text encoder")  # model_missing 404 unless downloaded (by kind)
    system = SYSTEM_PROMPT.read_text(encoding="utf-8")
    try:
        answer = (chat or llama_chat)(system, idea, **SAMPLING)
    except LatentroomError:
        raise
    except Exception as e:  # a crashed or unreachable LLM: plain message, the type to the log
        log.error("enhance prompt: the text model failed (%s)", type(e).__name__)  # no prompt
        raise LatentroomError("enhance_failed", FAILED, 502) from None
    prompt = parse_answer(answer)
    if not prompt:
        log.warning("enhance prompt: no usable answer (%d chars)", len(answer or ""))  # no text
        raise LatentroomError("enhance_failed", FAILED, 502)
    return prompt


def parse_answer(text: str) -> str:
    """The rewritten prompt from the model's answer, or "" if there is none. Like pe_core:
    keep what follows the thinking, then the last JSON object holding a non-empty prompt wins."""
    text = (text or "").rsplit("</think>", 1)[-1]  # Qwen chat templates may drop the <think>
    if "<think>" in text:
        return ""  # cut off while still thinking
    decoder = json.JSONDecoder()
    for start in reversed([m.start() for m in re.finditer(r"\{", text)]):
        try:
            obj, _end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(obj, dict):
            for key in KEYS:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""
