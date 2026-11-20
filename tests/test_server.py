"""Server tests: real HTTP on 127.0.0.1 against fake engine modules (contract shapes only)."""

import http.client
import io
import json
import re
import socket
import struct
import time
import zlib
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest
from PIL import Image

from latentroom import REPO, __version__, settings
from latentroom.engine import (
    catalogue,
    downloads,
    hardware,
    history,
    images,
    install,
    jobs,
    paths,
    prompt_rewrite,
    runner,
    search,
)
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError
from latentroom.web import phone, server, update

GPU = hardware.Hardware(
    "ok", "", hardware.Gpu("Test GPU", 12 << 30, 10 << 30), "580.1", 32 << 30, 16 << 30, 1
)
F_MODEL = catalogue.FileItem(
    "std-q4", "model-q4.gguf", "https://example.invalid/m", 10, None, "apache-2.0", "diffusion"
)
F_VAE = catalogue.FileItem(
    "vae", "vae.safetensors", "https://example.invalid/v", 5, None, "apache-2.0", "vae"
)
F_UP = catalogue.FileItem(
    "upscaler", "esrgan.pth", "https://example.invalid/u", 3, None, "bsd", "upscaler"
)
F_FACE = catalogue.FileItem(
    "face", "face_yolov8n.pt", "https://example.invalid/f", 6, None, "agpl-3.0", "detector"
)
F_ENGINE = catalogue.FileItem(
    "engine", "sd.zip", "https://example.invalid/e", 4, None, "mit", "engine"
)
MODEL = catalogue.Model("std-q4", "qwen", "Q4_K", F_MODEL, ("vae",), False, False, 25, 4.0, None)
PAIR = catalogue.ModelPair("q4", "Standard", MODEL, None)
FIT = catalogue.Fit("full_gpu", 7 << 30, 1 << 30, 1.0, "")


def _state(file_id, status="downloading"):
    return downloads.DownloadState(file_id, file_id + ".bin", status, 1, 10, 2.0, 4.5, None)


class FakeQueue:
    def __init__(self, output_dir=paths.default_output_dir):
        self.output_dir, self.submitted, self.stopped = output_dir, [], False
        self.exclusive = 0  # run_exclusive calls

    def submit(self, req):
        self.submitted.append(req)
        return "job1"

    def cancel(self, job_id):
        return job_id == "job1"

    def run_exclusive(self, fn):
        self.exclusive += 1
        return fn()

    def jobs(self):
        return [{"job_id": "job1", "status": "queued"}]

    def events(self, since=0):
        all_ = [
            runner.JobEvent(1, "job1", "queued", 1.0),
            runner.JobEvent(2, "job1", "step", 2.0, 1, 25),
        ]
        return [e for e in all_ if e.seq > since]

    def shutdown(self):
        self.stopped = True


@pytest.fixture
def fake(monkeypatch, latentroom_home):
    """Every engine call the server makes, replaced by a fake. Returns a call log + entry image."""
    calls = []
    img = latentroom_home / "out.png"
    Image.new("RGB", (64, 64), "gray").save(img)
    entry = history.Entry(
        7, 1.0, "txt2img", "a lighthouse at dusk", {}, 42, "std-q4", str(img), 64, 64
    )

    def rec(name, value=None):
        def f(*a, **k):
            calls.append((name, a, k))
            return value(*a, **k) if callable(value) else value

        return f

    def get_entry(entry_id):
        if entry_id != 7:
            raise LatentroomError("not_found", "That picture no longer exists.", 404)
        return entry

    for mod, name, value in [
        (hardware, "detect", GPU),
        (hardware, "free_disk", 123),
        (catalogue, "load", [PAIR]),
        (catalogue, "files", {f.id: f for f in (F_MODEL, F_VAE, F_UP, F_FACE, F_ENGINE)}),
        (catalogue, "get", MODEL),
        (catalogue, "required", [F_VAE, F_MODEL]),
        (catalogue, "fit", FIT),
        (catalogue, "recommend", PAIR),
        (downloads, "start", lambda item: _state(item.id, "queued")),
        (downloads, "pause", lambda i: _state(i, "paused")),
        (downloads, "resume", lambda i: _state(i)),
        (downloads, "cancel", lambda i: _state(i, "cancelled")),
        (downloads, "progress", [_state("vae")]),
        (install, "engine_exe", None),
        (install, "install_engine", Path("C:/x/sd-cli.exe")),
        (install, "installed", {"vae"}),
        (install, "import_models", ["std-q4"]),
        (install, "delete_model", None),
        (install, "disk_use", {"vae": 5}),
        (install, "list_loras", ["test-style-a"]),
        (history, "list_entries", [entry]),
        (history, "get", get_entry),
        (history, "star", lambda i, s: entry),
        (history, "delete", None),
        (history, "reuse", JobRequest(prompt="a lighthouse at dusk")),
        (images, "thumbnail", (b"thumbbytes", "image/webp")),
        (server, "_open_folder", None),
        (server, "_show_file", None),
        (phone, "tailscale_ip", (None, phone.NO_TAILSCALE)),
    ]:
        monkeypatch.setattr(mod, name, rec(name, value))
    monkeypatch.setattr(runner, "Queue", FakeQueue)
    return calls


class Client:
    def __init__(self, url: str, cookie: str = "") -> None:
        u = urlsplit(url)
        self.host, self.port, self.cookie = u.hostname, u.port, cookie

    def call(self, method, path, body=None, headers=None, ctype="application/json"):
        h = {"Host": f"{self.host}:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = ctype
        h.update(headers or {})
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def json(self, method, path, body=None, **kw):
        status, headers, data = self.call(method, path, body, **kw)
        return status, json.loads(data)


@pytest.fixture
def srv(fake):
    s = server.start()
    yield s
    s.stop()


@pytest.fixture
def client(srv):
    status, headers, _ = Client(srv.url).call(
        "GET", urlsplit(srv.url).path + "?" + urlsplit(srv.url).query
    )
    assert status == 303 and headers["Location"] == "/"
    return Client(srv.url, headers["Set-Cookie"].split(";")[0])


# ---- security ----


def test_token_becomes_strict_httponly_cookie(srv):
    status, headers, _ = Client(srv.url).call("GET", "/?" + urlsplit(srv.url).query)
    assert status == 303
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("lr_token=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert Client(srv.url).call("GET", "/?token=wrong")[0] == 403


def test_api_needs_the_cookie(srv, client):
    status, body = Client(srv.url).json("GET", "/api/version")
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert body["error"]["message"] == server.FORBIDDEN
    assert Client(srv.url, "lr_token=nope").json("GET", "/api/version")[0] == 403
    assert client.json("GET", "/api/version") == (
        200,
        {"version": __version__, "engine_release": install.ENGINE_RELEASE},
    )


def test_api_update_is_the_update_check(client, monkeypatch):
    """GET /api/update is update.check(); offline is a 502 the Settings row shows."""
    monkeypatch.setattr(
        update, "check", lambda: {"current": "0.1.0", "latest": "0.2.0", "newer": True}
    )
    assert client.json("GET", "/api/update") == (200, update.check())

    def offline():
        raise LatentroomError("offline", update.OFFLINE, 502)

    monkeypatch.setattr(update, "check", offline)
    status, body = client.json("GET", "/api/update")
    assert (status, body["error"]) == (502, {"code": "offline", "message": update.OFFLINE})


@pytest.mark.parametrize("host", ["localhost:{port}", "evil.example:{port}", "127.0.0.1", ""])
def test_foreign_host_rejected(client, host):
    status, _ = client.json("GET", "/api/version", headers={"Host": host.format(port=client.port)})
    assert status == 403


def test_origin_and_fetch_site(client):
    ok = f"http://127.0.0.1:{client.port}"
    assert client.json("GET", "/api/version", headers={"Origin": ok})[0] == 200
    assert client.json("GET", "/api/version", headers={"Origin": "http://evil.example"})[0] == 403
    assert client.json("GET", "/api/version", headers={"Origin": "null"})[0] == 403
    assert client.json("GET", "/api/version", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert client.json("GET", "/api/version", headers={"Sec-Fetch-Site": "same-site"})[0] == 403
    assert client.json("GET", "/api/version", headers={"Sec-Fetch-Site": "none"})[0] == 200


def test_post_needs_json(client):
    for body, ctype in [(b"starred=1", "application/x-www-form-urlencoded"), (b"{}", "text/plain")]:
        status, out = client.json("POST", "/api/history/7/star", body, ctype=ctype)
        assert status == 400 and out["error"]["code"] == "bad_request"
    assert client.json("POST", "/api/history/7/star", b"{nope")[0] == 400
    assert client.json("POST", "/api/history/7/star", b"[true]")[0] == 400
    assert client.json("POST", "/api/history/7/star", {"starred": "yes"})[0] == 400
    assert client.json("POST", "/api/history/7/star", {"starred": True, "x": 1})[0] == 400
    form = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "0"}
    assert client.json("POST", "/api/jobs/job1/cancel", headers=form)[0] == 400


@pytest.mark.parametrize(
    "path, ctype, cookie, status, claim",
    [
        ("/api/history/7/star", "text/plain", True, 400, 0),  # refused before the body is read
        ("/api/history/7/star", "application/json", False, 403, 0),  # not signed in
        ("/api/nope", "application/json", True, 404, 0),
        ("/api/settings", "application/json", True, 400, server.MAX_JSON + 1),  # too big: unread
    ],
)
def test_a_refused_post_still_gets_its_answer(client, srv, path, ctype, cookie, status, claim):
    # a body left unread makes the server's close send RST, and an RST that lands before the
    # client reads throws the answer away (Windows: ConnectionAbortedError). The client here reads
    # late on purpose, the way a busy browser does.
    body = b'{"starred": true}' + b" " * 20000  # more than the server's 8 KB read buffer
    head = f"POST {path} HTTP/1.1\r\nHost: {client.host}:{client.port}\r\n"
    head += f"Content-Type: {ctype}\r\nContent-Length: {claim or len(body)}\r\n"
    head += f"Cookie: {client.cookie}\r\n" if cookie else ""
    with socket.create_connection((client.host, client.port), timeout=10) as s:
        s.sendall(head.encode() + b"\r\n")  # http.client too sends the head, then the body
        time.sleep(0.1)
        s.sendall(body)
        time.sleep(0.3)  # the server has answered and closed by now
        answer = s.makefile("rb").read()
    assert answer.startswith(f"HTTP/1.1 {status} ".encode())
    assert b"\r\nConnection: close\r\n" in answer  # the unread body ends the connection


def test_one_connection_carries_many_requests(client):
    # on HTTP/1.0 every request took a new socket, ~20 per page load, each parked in
    # TIME_WAIT for minutes; parallel runs ran Windows out of them (ERR_NO_BUFFER_SPACE)
    conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
    h = {"Host": f"{client.host}:{client.port}", "Cookie": client.cookie}
    socks = []
    for method, path, body in [
        ("GET", "/api/version", None),
        ("POST", "/api/settings", b"{}"),  # a body the server reads
        ("GET", "/app.js", None),
        ("GET", "/api/nope", None),  # an error answer keeps the connection too
    ]:
        conn.request(method, path, body, {**h, "Content-Type": "application/json"} if body else h)
        conn.getresponse().read()
        socks.append(conn.sock)
    assert socks[0] is not None and all(s is socks[0] for s in socks), socks
    # a body the server never reads must not be taken for the next request: the server closes
    conn.request("DELETE", "/api/history/1", b"GET /api/version HTTP/1.1\r\n\r\n", h)
    r = conn.getresponse()
    r.read()
    assert r.getheader("Connection") == "close" and conn.sock is None
    conn.close()


def test_an_idle_connection_is_closed_after_the_timeout(client, monkeypatch):
    # keep-alive must not let a closed tab or a sleeping phone hold a server thread forever
    monkeypatch.setattr(server._Handler, "timeout", 0.3)
    conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
    conn.request("GET", "/api/version", headers={"Host": f"{client.host}:{client.port}"})
    assert conn.getresponse().read()
    conn.sock.settimeout(5)
    t = time.monotonic()
    assert conn.sock.recv(1) == b""  # the server closed it, nothing was sent
    assert time.monotonic() - t < 3
    conn.close()


def test_an_upload_that_stalls_gets_a_plain_answer(client, srv, monkeypatch):
    # a phone that loses signal mid-upload; before, this was a 500 asking for a bug report
    monkeypatch.setattr(server._Handler, "timeout", 0.3)
    notes = []
    monkeypatch.setattr(srv, "note", lambda e, where="": notes.append(e))
    conn = http.client.HTTPConnection(client.host, client.port, timeout=10)
    conn.putrequest("POST", "/api/uploads", skip_host=True)
    for k, v in (("Host", f"{client.host}:{client.port}"), ("Cookie", client.cookie)):
        conn.putheader(k, v)
    conn.putheader("Content-Type", "image/png")
    conn.putheader("Content-Length", "100000")
    conn.endheaders(b"\x89PNG\r\n\x1a\n")  # 8 of 100000 bytes, then the phone goes quiet
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    assert (r.status, body["error"]["message"]) == (408, server.STALLED)
    assert [e.code for e in notes] == ["timeout"]  # not an internal error
    assert not any(paths.uploads_dir().iterdir())


def test_body_size_caps(client):
    big = {"Content-Length": str(server.MAX_JSON + 1)}
    status, _, _ = client.call("POST", "/api/settings", b"{}", headers=big)
    assert status == 400


def test_static_files_and_traversal(client):
    status, headers, data = client.call("GET", "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert (
        headers["X-Content-Type-Options"] == "nosniff"
        and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    )
    assert b'<script src="/app.js"' in data
    status, headers, _ = client.call("GET", "/app.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    for bad in [
        "/../pyproject.toml",
        "/%2e%2e/%2e%2e/pyproject.toml",
        "/..%5c..%5cpyproject.toml",
        "/nope.html",
    ]:
        assert client.call("GET", bad)[0] == 404, bad
    assert client.json("GET", "/api/nope")[0] == 404
    assert client.call("PUT", "/api/version")[0] == 501


_SERVER_PY = Path(server.__file__).resolve()
_REPO = _SERVER_PY.parents[2]


@pytest.mark.parametrize(
    "path",
    [
        "/../server.py",
        "/%2e%2e/server.py",
        "/%2E%2E/%2E%2E/__init__.py",
        "/..%5cserver.py",
        "/..%5c..%5c..%5cpyproject.toml",
        "/static/../../server.py",
        "/" + quote(_SERVER_PY.as_posix()),
        "/" + quote(str(_SERVER_PY), safe=""),
        "/" + quote(str(_REPO / "pyproject.toml"), safe=""),
    ],
)
def test_static_guard_blocks_real_files_outside(client, path):
    # every path lands on a file that exists, so only the folder guard can answer 404
    target = (server.STATIC.resolve() / unquote(path).lstrip("/")).resolve()
    assert target.is_file() and not target.is_relative_to(server.STATIC.resolve()), target
    assert client.call("GET", path)[0] == 404


def test_test_page_calls_every_route():
    # shortcut: checks the literal parts of each route path only, not the method
    page = "".join(
        f.read_text(encoding="utf-8")
        for f in sorted(server.STATIC.rglob("*"))
        if f.suffix in (".js", ".html")
    )
    for method, rx, _fn, _flags in server._ROUTES:
        for part in re.split(r"\(.*?\)+", rx.pattern):
            assert part.replace("\\", "") in page, (method, rx.pattern)


def test_github_address_everywhere():  # no "whoever gave you", one address
    texts = {
        f: f.read_text(encoding="utf-8")
        for f in sorted(Path(server.__file__).parents[1].rglob("*"))
        if f.suffix in (".py", ".js", ".html")
    }
    assert [f for f, t in texts.items() if "whoever gave you" in t] == []
    ui = (server.STATIC / "ui.js").read_text(encoding="utf-8")
    assert f"export const REPO = '{REPO}';" in ui  # the JS copy equals latentroom.REPO
    assert f"{REPO}/issues " in server.INTERNAL and f"{REPO}/issues " in downloads.DEAD_LINK


def test_every_external_address_in_the_page_is_one_link_helper():
    """An address in static/ is always ui.js extLink(...), a link the desktop window
    opens in the system browser. Left plain: REPO's definition, extLink's own https://, and the
    diagnostic toast (a toast cannot be clicked)."""
    addr = re.compile(r"\$\{REPO\}|https?://(?!www\.w3\.org)|\b[a-z0-9-]+\.(?:com|org|net|io)\b")
    linked = re.compile(r"extLink\((`[^`]*`|'[^']*')\)")
    plain = [
        f"{f.name}:{n}"
        for f in sorted(server.STATIC.rglob("*"))
        if f.suffix in (".js", ".html")
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
        if addr.search(linked.sub("", line))
    ]
    ui = (server.STATIC / "ui.js").read_text(encoding="utf-8").splitlines()
    allowed = {
        f"ui.js:{n}"
        for n, line in enumerate(ui, 1)
        if line.startswith(("export const REPO =", "export const extLink =", "// Only constant"))
        or "toast(`Diagnostic report copied." in line
    }
    assert set(plain) == allowed, sorted(set(plain) ^ allowed)
    import webview  # the window opens a target=_blank link with the system browser (pywebview)

    assert webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True


def test_the_rule_and_the_qwen_licence_read_the_same_in_the_app_and_readme():
    """The rule at the 18+ question and the Qwen licence line are
    one copy each in ui.js (USE_RULE, QWEN_LICENCE). README says every part of the rule (as one
    sentence) and the licence word for word, never says "adult" and never shortens the licence
    to "non-commercial"."""
    rule = (
        "You are responsible for what you make. No real people without their consent. "
        "Nobody under 18. Nothing illegal where you live."
    )
    qwen = "Qwen-Image models: research or evaluation use only, no commercial use."
    ui = (server.STATIC / "ui.js").read_text(encoding="utf-8")
    readme = (Path(server.__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    flat = " ".join(readme.split()).lower()
    assert f"export const USE_RULE = '{rule}';" in ui and f"QWEN_LICENCE = `{qwen} " in ui
    assert all(part.strip().lower() in flat for part in rule.split(".") if part.strip())
    assert readme.count(qwen) == 2  # top lines + Licences
    assert "adult" not in flat and "non-commercial" not in readme


def test_the_qwen_notice_does_not_put_latentroom_under_qwen_terms():
    """Pre-release review: NOTICE ships next to the Qwen prompt; it said "Latentroom is free and
    for non-commercial use only", which contradicts the app's GPL-3.0. Qwen's terms cover that
    one file; the NOTICE's required first line stays word for word."""
    data = Path(server.__file__).parents[1] / "data" / "prompt_rewrite"
    notice = (data / "NOTICE").read_text(encoding="utf-8")
    first = "Qwen is licensed under the Qwen RESEARCH LICENSE AGREEMENT, Copyright (c) 2026"
    assert notice.startswith(first) and "GPL-3.0" in notice
    assert "non-commercial" not in notice.lower()


def test_errors_plain_and_internal(client, srv, monkeypatch):
    monkeypatch.setattr(install, "list_loras", lambda: 1 / 0)
    status, body = client.json("GET", "/api/loras")
    assert status == 500 and body == {"error": {"code": "internal", "message": server.INTERNAL}}
    assert "ZeroDivisionError" in srv.diagnostics()

    def boom():
        raise LatentroomError("engine_missing", "The Latentroom engine is not installed.", 409)

    monkeypatch.setattr(install, "install_engine", boom)
    status, body = client.json("POST", "/api/engine/install")
    assert status == 409 and body["error"]["code"] == "engine_missing"


# ---- endpoints ----


def test_hardware_catalogue_fit(client, fake):
    assert client.json("GET", "/api/hardware")[1]["gpu"]["name"] == "Test GPU"
    status, body = client.json("GET", "/api/catalogue?width=1024&height=768&refs=2")
    assert status == 200 and body["recommended"] == "q4" and body["size"] == 1024
    std = body["pairs"][0]["standard"]
    assert std["fit"]["label"] == "full_gpu" and std["installed"] is False  # model file missing
    assert std["speed"] == "" and body["fast_help"].endswith("8 steps.")
    assert body["pairs"][0]["uncensored"] is None
    assert [f["id"] for f in body["files"]] == ["vae", "upscaler", "face"]  # parts, not the model
    assert ("fit", (MODEL, GPU, 1024, 768, 2, 0), {}) in fake
    assert client.json("GET", "/api/fit?model_id=std-q4&width=512")[1]["label"] == "full_gpu"
    assert client.json("GET", "/api/fit")[0] == 400
    status, body = client.json("GET", "/api/catalogue?width=1000")
    assert status == 400 and "multiple of 32" in body["error"]["message"]
    assert client.json("GET", "/api/catalogue?height=4096")[0] == 400
    assert client.json("GET", "/api/catalogue?refs=x")[0] == 400


REAL = {name: getattr(catalogue, name) for name in ("load", "fit", "recommend")}  # before `fake`


def test_catalogue_offers_a_smaller_size_on_a_small_card(client, monkeypatch):
    """A 3 GB card runs no pair at 1024 x 1024. The wizard still gets a
    pick, with the largest square size it runs at, instead of nothing. The same answers
    while the card is busy (a job, Enhance or another app): fit sees the card, not its free VRAM."""
    for name, value in REAL.items():
        monkeypatch.setattr(catalogue, name, value)
    idle = hardware.Gpu("RTX 3 GB", 3 * 2**30, 3 * 2**30 - catalogue.DESKTOP_RESERVE)
    small = hardware.Hardware("ok", "", idle, "581.42", 16 * 2**30, 10 * 2**30)
    monkeypatch.setattr(hardware, "detect", lambda: small)
    assert REAL["recommend"](REAL["load"](), small) is None  # what the wizard got before
    body = client.json("GET", "/api/catalogue")[1]
    # nothing runs well at 768: the level that runs with the smallest slowdown (recommend step 3)
    assert (body["recommended"], body["size"]) == ("q2", 768)
    size = "width=768&height=768"
    asks = [f"/api/catalogue?{size}", f"/api/fit?model_id=turbo-q3_k_xl&{size}"]
    answers = [client.json("GET", a)[1] for a in asks]  # the wizard's 2nd call, Develop's check
    (turbo,) = [p["standard"] for p in answers[0]["pairs"] if p["id"] == "turbo-q3"]
    assert turbo["fit"]["label"] == answers[1]["label"] == "partial"
    full = replace(small, gpu=replace(idle, vram_free=2**28))  # 256 MiB left
    monkeypatch.setattr(hardware, "detect", lambda: full)
    assert [client.json("GET", a)[1] for a in asks] == answers


def test_catalogue_lists_the_smart_search_files(client, monkeypatch):
    items = [
        catalogue.FileItem(i, f"{i}.bin", "https://example.invalid/x", 1, None, "mit", kind)
        for i, kind in [("e", "embedding"), ("m", "embedding_vision"), ("z", "llama")]
    ]  # Models shows them as one "Smart search" row
    monkeypatch.setattr(catalogue, "files", lambda: {f.id: f for f in (F_MODEL, *items)})
    assert [f["id"] for f in client.json("GET", "/api/catalogue")[1]["files"]] == ["e", "m", "z"]


def test_fit_and_recommend_see_the_video_memory_limit(client, fake):
    settings.save(settings.Settings(max_vram_gb=4))
    client.json("GET", "/api/catalogue")
    client.json("GET", "/api/fit?model_id=std-q4")
    seen = [a[1] for name, a, _ in fake if name in ("fit", "recommend")]
    assert len(seen) == 3 and set(seen) == {catalogue.capped(GPU, 4)} != {GPU}


def test_fit_counts_the_loras_and_turbo_like_the_queue(client, fake, monkeypatch):
    """Develop and Models send the LoRAs and the Turbo add-on Develop has on, and
    fit gets the bytes the queue counts (jobs.lora_bytes). A name not in the LoRA folder counts
    nothing and builds no path."""
    turbo = catalogue.FileItem(
        "turbo-lora", "turbo.safetensors", "https://example.invalid/t", 871, None, "mit", "lora"
    )
    monkeypatch.setattr(catalogue, "files", lambda: {turbo.id: turbo})
    (paths.loras_dir() / "test-style-a.safetensors").write_bytes(b"x" * 300)
    on = "loras=" + quote("test-style-a/../uploads/x/nope", safe="") + "&turbo=1"
    client.json("GET", f"/api/fit?model_id=std-q4&{on}")
    client.json("GET", f"/api/catalogue?{on}")
    client.json("GET", "/api/fit?model_id=std-q4&loras=test-style-a")
    client.json("GET", "/api/fit?model_id=std-q4")
    assert [a[5] for name, a, _ in fake if name == "fit"] == [300 + 871, 300 + 871, 300, 0]


def test_downloads(client, fake):
    assert client.json("GET", "/api/downloads")[1]["downloads"][0]["id"] == "vae"
    status, body = client.json("POST", "/api/downloads", {"model_id": "std-q4"})
    assert status == 200 and [d["id"] for d in body["downloads"]] == ["std-q4"]  # vae installed
    body = client.json("POST", "/api/downloads", {"file_id": "upscaler"})[1]
    assert body["downloads"][0]["status"] == "queued"
    assert client.json("POST", "/api/downloads", {"file_id": "nope"})[0] == 404
    assert client.json("POST", "/api/downloads", {"model_id": "a", "file_id": "b"})[0] == 400
    for action, status in [("pause", "paused"), ("resume", "downloading"), ("cancel", "cancelled")]:
        assert client.json("POST", f"/api/downloads/vae/{action}")[1]["status"] == status
    assert client.json("POST", "/api/downloads/vae/explode")[0] == 404


def test_engine_models_loras(client, fake, tmp_path):
    assert client.json("GET", "/api/engine")[1] == {
        "release": install.ENGINE_RELEASE,
        "installed": False,
        "files": [asdict(F_ENGINE)],  # the wizard's Picture engine row reads it
    }
    assert client.json("POST", "/api/engine/install")[1]["installed"] is True
    assert client.json("GET", "/api/models")[1] == {
        "installed": ["vae"],
        "disk_use": {"vae": 5},
        "free": 123,
        "data_dir": str(paths.data_dir()),
        "data_warnings": [],  # the test's data folder is on a plain local drive
        "output_dir": str(paths.default_output_dir()),
    }
    assert client.json("POST", "/api/models/import", {"folder": str(tmp_path)})[1] == {
        "imported": ["std-q4"]
    }
    assert client.json("POST", "/api/models/import", {"folder": "relative"})[0] == 400
    assert client.json("POST", "/api/models/import", {"folder": str(tmp_path / "no")})[0] == 400
    assert client.json("DELETE", "/api/models/std-q4")[1] == {"ok": True}
    assert ("delete_model", ("std-q4",), {}) in fake
    assert client.json("GET", "/api/loras")[1] == {"loras": ["test-style-a"]}


def test_uploads(client):
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "PNG")
    status, body = client.json("POST", "/api/uploads", buf.getvalue(), ctype="image/png")
    assert status == 200 and (body["width"], body["height"]) == (40, 30)
    assert Path(body["path"]).parent == paths.uploads_dir() and body["name"].endswith(".png")
    status, headers, data = client.call("GET", "/api/uploads/" + body["name"])
    assert status == 200 and headers["Content-Type"] == "image/png" and data == buf.getvalue()
    assert client.json("POST", "/api/uploads", b"not an image", ctype="image/png")[0] == 400
    assert client.json("POST", "/api/uploads", buf.getvalue(), ctype="image/gif")[0] == 400
    assert client.json("POST", "/api/uploads", buf.getvalue(), ctype="application/json")[0] == 400
    assert client.call("GET", "/api/uploads/..%5csettings.json")[0] == 404
    assert client.call("GET", "/api/uploads/" + "0" * 32 + ".png")[0] == 404


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_upload_damaged_picture_rejected(client, fmt):
    buf = io.BytesIO()
    Image.effect_noise((64, 48), 64).convert("RGB").save(buf, fmt)
    good = buf.getvalue()
    for cut in (12, 20, len(good) // 2):  # 12 = exactly the PNG end chunk (IEND)
        status, body = client.json(
            "POST", "/api/uploads", good[:-cut], ctype="image/" + fmt.lower()
        )
        assert status == 400, (fmt, cut)
        assert body["error"]["message"] == "This picture is damaged. Try another file."
    assert not any(paths.uploads_dir().iterdir())
    status, body = client.json("POST", "/api/uploads", b"not an image", ctype="image/png")
    assert status == 400 and "not a picture" in body["error"]["message"]


def _png_header(width: int, height: int) -> bytes:
    """A PNG that claims width x height: only the header, so nothing big is ever allocated."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")  # Pillow: > 89 MP
def test_upload_pixel_limit_and_phone_camera_jpeg(client):
    # 100 MP is refused from the header, before any decode (Pillow alone waits to 179 MP)
    too_big = "That picture is far too large. Use one under 100 megapixels."
    for w, h in ((10_001, 10_000), (20_000, 20_000)):
        status, body = client.json("POST", "/api/uploads", _png_header(w, h), ctype="image/png")
        assert (status, body["error"]["message"]) == (400, too_big), (w, h)
    assert not any(paths.uploads_dir().iterdir())
    # MPO = the multi-picture JPEG phones save (Samsung, iPhone portrait): kept as .jpg
    buf = io.BytesIO()
    frames = [Image.new("RGB", (40, 30), c) for c in ("red", "blue")]
    frames[0].save(buf, "MPO", save_all=True, append_images=frames[1:])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    status, body = client.json("POST", "/api/uploads", buf.getvalue(), ctype="image/jpeg")
    assert status == 200 and body["name"].endswith(".jpg"), body
    assert (body["width"], body["height"]) == (40, 30)


def test_jobs_and_events(client, srv):
    settings.save(settings.Settings(max_vram_gb=6, model_id="std-q4"))
    status, body = client.json(
        "POST", "/api/jobs", {"kind": "upscale", "init_image": "x.png", "max_vram_gb": 99}
    )
    assert (status, body) == (200, {"job_id": "job1"})
    req = srv.queue().submitted[0]
    assert (req.kind, req.max_vram_gb, req.model_id) == ("upscale", 6, "std-q4")
    assert client.json("POST", "/api/jobs", {"nope": 1})[0] == 400
    assert client.json("GET", "/api/jobs")[1] == {"jobs": [{"job_id": "job1", "status": "queued"}]}
    assert client.json("POST", "/api/jobs/job1/cancel")[1] == {"cancelled": True}
    body = client.json("GET", "/api/events?since=1")[1]
    assert [e["type"] for e in body["events"]] == ["step"] and body["last_seq"] == 2
    assert client.json("GET", "/api/events?since=5")[1] == {"events": [], "last_seq": 2}
    assert client.json("GET", "/api/events?since=-1")[0] == 400


def test_jobs_refuses_refs_outside_edit(client, srv, monkeypatch):
    monkeypatch.setattr(srv.queue(), "submit", lambda req: jobs.validate(req) or "job1")
    body = {"kind": "txt2img", "model_id": "std-q4", "prompt": "x", "refs": ["C:/a.png"]}
    status, answer = client.json("POST", "/api/jobs", body)
    assert status == 400 and answer["error"]["code"] == "bad_request"
    assert answer["error"]["message"] == (
        'Reference pictures only work with "Edit with references". Remove them first.'
    )


def test_prompt_enhance(client, srv, monkeypatch):  # mock LLM, real engine module
    path = "/api/prompt/enhance"
    status, body = client.json("POST", path, {"prompt": "un faro al tramonto"})
    assert (status, body["error"]["code"]) == (404, "model_missing")  # text encoder missing
    llm = catalogue.FileItem("llm", "llm.gguf", "https://example.invalid/l", 7, None, "x", "llm")
    monkeypatch.setattr(catalogue, "files", lambda: {"llm": llm})
    monkeypatch.setattr(install, "installed", lambda: {"llm"})
    asks, answer = [], ['{"rewritten_prompt": "A lighthouse at dusk."}']

    def chat(system, user, **sampling):
        asks.append(user)
        return answer[0]

    monkeypatch.setattr(prompt_rewrite, "llama_chat", chat)
    assert client.json("POST", path, {"prompt": "un faro al tramonto"}) == (
        200, {"prompt": "A lighthouse at dusk."}
    )  # fmt: skip
    assert asks == ["un faro al tramonto"] and srv.queue().exclusive == 2  # both via the lock
    for bad in [{"prompt": "  "}, {"prompt": "x" * 2001}, {"prompt": 3}, {"prompt": "x", "y": 1}]:
        assert client.json("POST", path, bad)[1]["error"]["code"] == "bad_request", bad
    answer[0] = "Sure! A lighthouse at dusk."  # no JSON: never shown raw
    status, body = client.json("POST", path, {"prompt": "a lighthouse"})
    assert (status, body["error"]["message"]) == (502, prompt_rewrite.FAILED)

    def busy(fn):
        raise LatentroomError("busy", "Wait for the picture to finish.", 409)

    monkeypatch.setattr(srv.queue(), "run_exclusive", busy)
    status, body = client.json("POST", path, {"prompt": "a lighthouse"})
    assert (status, body["error"]["code"]) == (409, "busy")
    assert len(asks) == 2  # neither the bad bodies nor the busy call reached the model


def test_history(client, fake):
    body = client.json(
        "GET", "/api/history?page=1&per_page=10&starred=1&search=lighthouse&day=2026-10-22"
    )[1]
    assert body["entries"][0]["id"] == 7
    assert (
        "list_entries",
        (),
        {"page": 1, "per_page": 10, "starred": True, "search": "lighthouse", "day": "2026-10-22"},
    ) in fake
    assert client.json("GET", "/api/history?day=yesterday")[0] == 400
    assert client.json("GET", "/api/history?starred=maybe")[0] == 400
    assert client.json("GET", "/api/history?per_page=1000")[0] == 400
    assert client.json("GET", "/api/history/7")[1]["seed"] == 42
    assert client.json("GET", "/api/history/8") == (
        404,
        {"error": {"code": "not_found", "message": "That picture no longer exists."}},
    )
    status, headers, data = client.call("GET", "/api/history/7/image")
    assert status == 200 and headers["Content-Type"] == "image/png" and data[:4] == b"\x89PNG"
    assert client.call("GET", "/api/history/7/thumb")[1]["Content-Type"] == "image/jpeg"
    assert client.json("POST", "/api/history/7/star", {"starred": False})[0] == 200
    assert ("star", (7, False), {}) in fake
    assert client.json("DELETE", "/api/history/7")[1] == {"ok": True}
    assert client.json("GET", "/api/history/7/reuse")[1]["prompt"] == "a lighthouse at dusk"
    assert client.json("GET", "/api/search")[1] == {"available": False}  # not downloaded


def test_a_thumb_is_saved_kept_out_of_caches_and_goes_with_its_picture(
    client, fake, latentroom_home
):
    status, headers, data = client.call("GET", "/api/history/7/thumb")
    assert (status, headers["Content-Type"], data) == (200, "image/jpeg", b"thumbbytes")
    assert headers["Cache-Control"] == "no-store"  # the phone's browser keeps no copy
    assert client.call("GET", "/api/history/7/thumb")[2] == b"thumbbytes"
    assert [c[0] for c in fake].count("thumbnail") == 1  # the 2nd one came from the disk
    (latentroom_home / "out.png").unlink()  # entry 7's picture, deleted in Explorer
    assert client.call("GET", "/api/history/7/thumb")[0] == 404
    assert not any(paths.thumbs_dir().iterdir())  # no copy of it stays


def test_history_finds_by_meaning_once_smart_search_is_downloaded(client, fake, monkeypatch):
    pics = [
        history.Entry(i, 1.0, "txt2img", "p", {}, 1, "std-q4", "x.png", 64, 64, i % 2 == 0)
        for i in range(1, 7)
    ]  # best match first; even ids are starred
    asked = []
    monkeypatch.setattr(search, "available", lambda: True)

    def found(text, k, starred):  # starred is filtered inside search.search
        asked.append((text, k, starred))
        return [e for e in pics if starred is None or e.starred == starred][:k]

    monkeypatch.setattr(search, "search", found)
    body = client.json("GET", "/api/history?search=a%20red%20fox&page=1&per_page=2")[1]
    assert body["smart"] and [e["id"] for e in body["entries"]] == [3, 4]  # the 2nd page
    assert asked == [("a red fox", 4, None)]
    body = client.json("GET", "/api/history?search=fox&starred=1&per_page=2")[1]
    assert [e["id"] for e in body["entries"]] == [2, 4] and asked[-1] == ("fox", 2, True)
    for q in ("", "?search=%20", "?search=fox&day=2026-10-22"):  # the prompt search
        assert client.json("GET", "/api/history" + q)[1]["smart"] is False
    assert len(asked) == 2


def test_search_state_starts_indexing(client, monkeypatch):
    started = []
    monkeypatch.setattr(search, "available", lambda: True)
    monkeypatch.setattr(search, "start_indexing", lambda: started.append(1))
    monkeypatch.setattr(search, "status", lambda: {"indexed": 120, "total": 800, "indexing": True})
    body = client.json("GET", "/api/search")[1]
    assert body == {"available": True, "indexed": 120, "total": 800, "indexing": True}
    assert started == [1]


def test_deleting_smart_search_stops_its_server_first(client, monkeypatch):
    z = catalogue.FileItem("z", "z.zip", "https://example.invalid/z", 1, None, "mit", "llama")
    monkeypatch.setattr(catalogue, "files", lambda: {f.id: f for f in (F_VAE, z)})
    done = []
    monkeypatch.setattr(search, "stop", lambda: done.append("stop"))
    monkeypatch.setattr(install, "delete_model", done.append)
    assert client.json("DELETE", "/api/models/z")[1] == {"ok": True}
    client.json("DELETE", "/api/models/vae")  # another part: Smart search keeps running
    assert done == ["stop", "z", "vae"]


def test_settings(client, tmp_path):
    assert client.json("GET", "/api/settings")[1] == settings.Settings().__dict__
    out = tmp_path / "pics"
    status, body = client.json(
        "POST", "/api/settings", {"output_dir": str(out), "adult_ok": True, "max_vram_gb": 7.5}
    )
    assert status == 200 and body["adult_ok"] is True and out.is_dir()
    assert settings.load().max_vram_gb == 7.5
    for bad in [
        {"nope": 1},
        {"adult_ok": "yes"},
        {"max_vram_gb": 999},
        {"output_dir": "relative/dir"},
        {"max_vram_gb": True},
    ]:
        assert client.json("POST", "/api/settings", bad)[0] == 400, bad
    status, body = client.json("POST", "/api/settings", {"phone_enabled": True})
    assert status == 409 and body["error"]["code"] == "not_available"


def test_another_apps_folder_gets_a_latentroom_folder_inside(client, tmp_path):
    """Picking D:\\AI (a ComfyUI models\\ inside) never mixes Latentroom's models\\
    into it: the data folder becomes D:\\AI\\Latentroom. An empty or a Latentroom folder is kept,
    and the new folder lists Latentroom's own files at once, for the uninstaller."""
    ai, empty, ours = tmp_path / "AI", tmp_path / "empty", tmp_path / "old"
    (ai / "models").mkdir(parents=True)
    (ai / "models" / "their.safetensors").write_bytes(b"x")
    empty.mkdir()
    ours.mkdir()
    (ours / paths.OWN_FILES).write_text("downloads.json\n")  # not a bare db.sqlite
    for picked, want in [(ai, ai / "Latentroom"), (empty, empty), (ours, ours)]:
        status, body = client.json("POST", "/api/settings", {"data_dir": str(picked)})
        assert status == 200 and body["data_dir"] == str(want), body
        assert paths.data_dir() == want and (want / paths.OWN_FILES).is_file()
    assert sorted(p.name for p in ai.iterdir()) == ["Latentroom", "models"]


def test_diagnostics_never_leaks(client, srv, monkeypatch):
    monkeypatch.setattr(hardware, "drive", lambda p: ("exFAT", False))
    settings.save(settings.Settings(output_dir="C:/Users/someone/Pictures/secret"))
    paths.logs_dir().joinpath("engine-job1.log").write_text(
        "step 1/25\nloading C:\\Users\\someone\\m.gguf\n"
    )
    paths.logs_dir().joinpath(paths.APP_LOG).write_text(
        "WARNING job job1: the engine stopped, exit code 0xC0000005\n"
        "ERROR cannot read C:\\Users\\someone\\AppData\\Local\\Latentroom\\x.json\n"
    )
    text = client.json("GET", "/api/diagnostics")[1]["text"]
    assert "Latentroom " + __version__ in text and "Test GPU" in text and "step 1/25" in text
    assert "--- latentroom.log" in text and "exit code 0xC0000005" in text
    assert "someone" not in text and srv.token not in text and "lighthouse" not in text
    assert '"output_dir": "custom"' in text
    assert '"gpus": 1' in text and "Data folder file system: exFAT" in text
    assert "Data folder name has non-ASCII: no" in text


def test_diagnostics_says_non_ascii_without_the_name(client, srv, tmp_path):
    srv.update_settings({"data_dir": str(tmp_path / "Bilder-Jürgen")})
    text = client.json("GET", "/api/diagnostics")[1]["text"]
    assert "Data folder name has non-ASCII: yes" in text and "Jürgen" not in text


def test_data_folder_warnings_never_block(monkeypatch):
    drives = {"Z:": ("NTFS", True), "F:": ("FAT32", False), "E:": ("exFAT", False)}
    seen = []  # folders whose drive Windows was asked about

    def drive(p):
        seen.append(p)
        return drives.get(str(p)[:2], ("NTFS", False))

    monkeypatch.setattr(hardware, "drive", drive)
    monkeypatch.setenv("OneDrive", "C:\\Users\\sam\\OneDrive")
    monkeypatch.delenv("OneDriveConsumer", raising=False)
    monkeypatch.delenv("OneDriveCommercial", raising=False)
    w = server.data_warnings
    assert w(Path("C:/Users/sam/onedrive/AI")) == [server.ONEDRIVE]  # case ignored, as Windows
    assert w(Path("C:/Users/sam/OneDriveStuff")) == []
    assert w(Path("Z:/AI")) == [server.NETWORK]  # a mapped network drive
    assert w(Path("F:/AI")) == [server.FAT32]
    assert w(Path("E:/AI")) == []  # exFAT holds big files
    seen.clear()
    assert w(Path("//nas/share/AI")) == [server.NETWORK] and seen == []  # the share is not asked


def test_open(client, fake):
    assert client.json("POST", "/api/open", {"what": "output"})[1] == {"ok": True}
    assert client.json("POST", "/api/open", {"what": "loras"})[0] == 200
    assert client.json("POST", "/api/open", {"what": "file", "entry_id": 7})[0] == 200
    assert [c[0] for c in fake if c[0] in ("_open_folder", "_show_file")] == [
        "_open_folder",
        "_open_folder",
        "_show_file",
    ]
    assert client.json("POST", "/api/open", {"what": "C:/Windows"})[0] == 400
    assert client.json("POST", "/api/open", {"what": "file", "entry_id": "7"})[0] == 400
    assert client.json("POST", "/api/open", {"what": "file", "entry_id": 8})[0] == 404


def test_stop_cancels_running_job(fake):
    s = server.start()
    q = s.queue()
    s.stop()
    s.stop()
    assert q.stopped
