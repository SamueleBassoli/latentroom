"""Fake llama-server for tests and QA: the flags, routes and JSON shapes engine/search.py uses
from the pinned llama.cpp release (b11850, tools/server/README.md), no model files.

Run: python tests/fake_llama_server.py <llama-server arguments>
With --embeddings (Smart search, must also get --device none: CPU only) vectors (unit length,
4 numbers: red, green, blue, rest) stand in for EmbeddingGemma 2:
- a picture: its mean colour, so a red PNG lands near the word "red";
- a text: the words red/green/blue after the query prefix the model card asks for.
Without it (Enhance prompt, never --device none: GPU) /v1/chat/completions answers the t2i
rewriter's JSON with the user's idea echoed in it.
Like the real server: /health is public (503 while "loading", then 200); every other route
needs "Authorization: Bearer <LLAMA_API_KEY env>" (401 otherwise).
Env: FAKE_LLAMA_FAIL   "start" -> exit 1 at once; "blocked" -> exit 0xC0000135 at once (a DLL
                       quarantined); "black" -> HTTP 500 for an all-black picture;
                       "chat" -> HTTP 500 for a chat request
     FAKE_LLAMA_DEVICES what --list-devices prints (default: no graphics card)
     FAKE_LLAMA_LOAD_S seconds /health answers 503 (default 0.2)
     FAKE_LLAMA_EMBED_S seconds each /v1/embeddings request takes (default 0)
Unknown flags are rejected (exit 1), so a typo in search._Llama._spawn() or
prompt_rewrite.llama_chat() fails here too.
Adding a flag: check it exists in the real server first, then add it below.
"""

import base64
import io
import json
import math
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageStat

QUERY = "task: search result | query: "
WORDS = ("red", "green", "blue")
VALUED = {
    "-m", "--mmproj", "--image-max-tokens", "-c", "-b", "-ub", "-t", "-np", "--host",
    "--port", "-lv", "--device",
}  # fmt: skip
SWITCHES = {"--embeddings", "--no-webui", "--offline", "--list-devices"}


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def text_vector(text: str) -> list[float] | None:
    if not text.startswith(QUERY):
        return None  # the fake insists on the model card's query prompt
    words = text[len(QUERY) :].lower().split()
    return unit([float(words.count(w)) for w in WORDS] + [0.1])


def picture_vector(url: str) -> list[float] | None:
    head, _, data = url.partition(",")
    if head != "data:image/png;base64":
        raise ValueError("not a PNG data URI")
    img = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
    rgb = [c / 255 for c in ImageStat.Stat(img).mean]
    if os.environ.get("FAKE_LLAMA_FAIL") == "black" and not any(rgb):
        return None
    return unit([*rgb, 0.1])


def vector(item) -> list[float] | None:
    if isinstance(item, str):
        return text_vector(item)
    parts = item["content"]
    if len(parts) != 1 or parts[0]["type"] != "image_url":
        raise ValueError("one image part expected")
    return picture_vector(parts[0]["image_url"]["url"])


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._send(404, {"error": {"code": 404, "message": "File Not Found"}})
        if time.monotonic() < READY_AT:
            return self._send(503, {"error": {"code": 503, "message": "Loading model"}})
        self._send(200, {"status": "ok"})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._send(401, {"error": {"code": 401, "message": "Invalid API Key"}})
        if self.path == "/v1/chat/completions" and "--embeddings" not in ARGS:
            return self._chat(json.loads(body))
        if self.path != "/v1/embeddings":
            return self._send(404, {"error": {"code": 404, "message": "File Not Found"}})
        time.sleep(float(os.environ.get("FAKE_LLAMA_EMBED_S", "0")))
        try:
            vectors = [vector(i) for i in json.loads(body)["input"]]
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, {"error": {"code": 400, "message": str(e)}})
        if None in vectors:
            return self._send(500, {"error": {"code": 500, "message": "failed to embed"}})
        data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
        self._send(200, {"object": "list", "data": data})

    def _chat(self, body: dict) -> None:
        """Enhance prompt: the official answer shape, the idea echoed inside it."""
        if os.environ.get("FAKE_LLAMA_FAIL") == "chat":
            return self._send(500, {"error": {"code": 500, "message": "failed to decode"}})
        system, user = (m["content"] for m in body["messages"])
        if "rewritten_prompt" not in system or body.get("max_tokens") != 1500:
            return self._send(400, {"error": {"code": 400, "message": "not the t2i rewriter"}})
        answer = json.dumps({"rewritten_prompt": f"An English prompt about: {user}"})
        message = {"role": "assistant", "content": f"Here it is:\n{answer}"}
        self._send(200, {"choices": [{"index": 0, "message": message}]})

    def log_message(self, *args) -> None:  # -lv 2: no request lines
        pass


def parse(argv: list[str]) -> dict[str, str]:
    args, i = {}, 0
    while i < len(argv):
        flag = argv[i]
        if flag in SWITCHES:
            args[flag], i = "", i + 1
        elif flag in VALUED and i + 1 < len(argv):
            args[flag], i = argv[i + 1], i + 2
        else:
            print(f"error: invalid argument: {flag}", file=sys.stderr)
            sys.exit(1)
    return args


if __name__ == "__main__":
    ARGS = parse(sys.argv[1:])
    if "--list-devices" in ARGS:  # the real format: common/arg.cpp common_print_available_devices
        print(os.environ.get("FAKE_LLAMA_DEVICES", "Available devices:\n  (none)"))
        sys.exit(0)
    if os.environ.get("FAKE_LLAMA_FAIL") == "start":
        print("error: failed to load model", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("FAKE_LLAMA_FAIL") == "blocked":  # an antivirus took one of its DLLs
        sys.exit(0xC0000135)
    embeddings = "--embeddings" in ARGS  # else a chat model (Enhance prompt)
    for need in ("-m", "--mmproj", "--embeddings", "--port") if embeddings else ("-m", "--port"):
        if need not in ARGS:
            print(f"error: the fake needs {need}", file=sys.stderr)
            sys.exit(1)
    if embeddings != (ARGS.get("--device") == "none"):
        print("error: Smart search runs on the CPU, Enhance prompt on the GPU", file=sys.stderr)
        sys.exit(1)
    KEY = os.environ.get("LLAMA_API_KEY", "")
    READY_AT = time.monotonic() + float(os.environ.get("FAKE_LLAMA_LOAD_S", "0.2"))
    address = (ARGS.get("--host", "127.0.0.1"), int(ARGS["--port"]))
    ThreadingHTTPServer(address, Handler).serve_forever()
