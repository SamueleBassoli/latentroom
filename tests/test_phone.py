"""v2 phone access: pairing, rate limit, stored tokens, QR, Tailscale detection, listener.
The phone listener is tested on 127.0.0.2 standing in for the Tailscale address."""

import http.client
import io
import json
import re
import subprocess

import pytest
from PIL import Image

from latentroom.engine import history, paths
from latentroom.engine.jobs import JobRequest
from latentroom.errors import LatentroomError
from latentroom.web import phone
from tests.test_server import Client, client, fake, srv  # noqa: F401 -- shared fixtures


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


PHONE, OTHER = "100.64.0.7", "100.64.0.9"


def test_code_is_six_digits_single_use_and_expires():
    clock = Clock()
    p = phone.Pairing(clock)
    code, expires = p.new_code()
    assert len(code) == 6 and code.isdigit() and expires == 1000 + phone.CODE_TTL
    token = p.pair(code, PHONE)
    assert len(token) >= 40 and phone.known(token)
    with pytest.raises(LatentroomError):
        p.pair(code, PHONE)  # single use
    code, _ = p.new_code()
    clock.now += phone.CODE_TTL
    with pytest.raises(LatentroomError):
        p.pair(code, PHONE)  # too old


def test_wrong_codes_are_rate_limited_per_address():
    clock = Clock()
    p = phone.Pairing(clock)
    code, _ = p.new_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(phone.MAX_WRONG):
        with pytest.raises(LatentroomError) as e:
            p.pair(wrong, OTHER)
        assert e.value.code == "bad_request"
    with pytest.raises(LatentroomError) as e:
        p.pair(code, OTHER)  # even the right code waits
    assert (e.value.code, e.value.status) == ("too_many_tries", 429)
    # another device failing codes on purpose does not lock the user's phone out
    assert p.pair(code, PHONE)
    code, _ = p.new_code()
    clock.now += 61
    assert p.pair(code, OTHER)


def test_tokens_stored_hashed_and_forgotten():
    phone.remember("tok-a")
    phone.remember("tok-b")
    stored = (paths.app_dir() / "phones.json").read_text()
    assert "tok-a" not in stored and phone.count() == 2
    assert phone.known("tok-a") and not phone.known("tok-c")
    phone.forget_all()
    assert not phone.known("tok-a") and phone.count() == 0
    (paths.app_dir() / "phones.json").write_text("{broken")
    assert not phone.known("x")


def test_qr_svg_is_local_svg():
    svg = phone.qr_svg("http://100.101.102.103:47615/#code=123456")
    assert svg.startswith("<svg") and "http" not in svg.split(">", 1)[1].replace(
        "http://www.w3.org", ""
    )
    head = svg.split(">", 1)[0]  # scales into the 168 px box instead of being cut off
    assert 'viewBox="0 0 ' in head and "width=" not in head and "height=" not in head


def test_tailscale_ip(monkeypatch):
    monkeypatch.setattr(phone, "_tailscale_exe", lambda: "tailscale")
    out = subprocess.CompletedProcess([], 0, stdout="100.101.102.103\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: out)
    assert phone.tailscale_ip() == ("100.101.102.103", "")

    out.stdout = "192.168.1.5\n"  # not a tailnet address: ignored
    monkeypatch.setattr(
        phone.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("100.64.0.9", 0))]
    )
    assert phone.tailscale_ip() == ("100.64.0.9", "")

    monkeypatch.setattr(phone, "_tailscale_exe", lambda: None)
    monkeypatch.setattr(
        phone.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.5", 0))]
    )
    assert phone.tailscale_ip() == (None, phone.NO_TAILSCALE)


def _pair_cookie(c: Client, code: str):
    conn = http.client.HTTPConnection(c.host, c.port, timeout=10)
    body = json.dumps({"code": code}).encode()
    conn.request(
        "POST",
        "/api/phone/pair",
        body,
        {"Host": f"{c.host}:{c.port}", "Content-Type": "application/json"},
    )
    r = conn.getresponse()
    out = r.status, r.getheader("Set-Cookie"), r.read()
    conn.close()
    return out


def test_phone_listener_end_to_end(client, srv, monkeypatch):  # noqa: F811
    monkeypatch.setattr(phone, "tailscale_ip", lambda: ("127.0.0.2", ""))
    # a fresh port, not the fixed 47615: another test run (or the dev server) may take that one
    # the moment this test closes it, and then the "closed, so refused" check at the end fails
    monkeypatch.setattr(phone, "PORT", 0)
    assert client.json("GET", "/api/phone")[1]["enabled"] is False
    assert client.json("POST", "/api/phone/code")[0] == 409  # not on yet
    assert client.json("POST", "/api/settings", {"phone_enabled": True})[0] == 200
    info = client.json("GET", "/api/phone")[1]
    assert info["enabled"] and info["available"] and info["address"].startswith("http://127.0.0.2:")
    assert srv._phone.server_address[0] == "127.0.0.2"  # the Tailscale address only

    ph = Client(info["address"])
    status, body = ph.json("GET", "/api/version")
    assert status == 403 and body["error"]["code"] == "not_paired"  # the page shows pairing
    assert (
        Client(info["address"], client.cookie).json("GET", "/api/version")[0] == 403
    )  # desktop cookie
    assert ph.call("GET", "/?" + srv.url.split("?")[1])[0] == 403  # desktop token is not for phones
    assert ph.call("GET", "/")[0] == 200  # the page itself loads, to show the pairing box
    assert ph.json("GET", "/api/version", headers={"Host": f"127.0.0.1:{ph.port}"})[0] == 403
    assert client.json("POST", "/api/phone/pair", {"code": "123456"})[0] == 403  # phones only

    code = client.json("POST", "/api/phone/code")[1]
    assert code["qr_svg"].startswith("<svg") and len(code["code"]) == 6
    assert _pair_cookie(ph, "999999" if code["code"] != "999999" else "888888")[0] == 400
    status, cookie, _ = _pair_cookie(ph, code["code"])
    assert status == 200 and cookie.startswith("lr_phone=") and "HttpOnly" in cookie
    ph.cookie = cookie.split(";")[0]
    assert ph.json("GET", "/api/version")[0] == 200
    assert ph.json("GET", "/api/history")[0] == 200
    assert (
        ph.json("POST", "/api/jobs", {"prompt": "a lighthouse at dusk"})[0] == 200
    )  # shared queue
    for method, path, body in [
        ("POST", "/api/open", {"what": "output"}),
        ("POST", "/api/settings", {"adult_ok": True}),
        ("POST", "/api/phone/code", None),
        ("POST", "/api/models/import", {"folder": "C:/"}),
        ("GET", "/api/diagnostics", None),
        ("GET", "/api/update", None),  # a phone never makes the PC go online
    ]:
        status, out = ph.json(method, path, body)
        assert (status, out["error"]["message"]) == (
            403,
            "This works only on the PC, in the Latentroom window.",
        ), path

    assert client.json("POST", "/api/phone/forget")[1] == {"ok": True}
    assert ph.json("GET", "/api/version")[0] == 403  # revoked at once

    assert client.json("POST", "/api/settings", {"phone_enabled": False})[0] == 200
    assert srv.phone_host() is None
    with pytest.raises(OSError):
        ph.call("GET", "/")


def test_a_phone_never_sees_a_pc_path(client, srv, monkeypatch, latentroom_home):  # noqa: F811
    # PC paths hold the Windows user name; the phone gets "<key>/<file name>" and sends it back
    monkeypatch.setattr(phone, "tailscale_ip", lambda: ("127.0.0.2", ""))
    monkeypatch.setattr(phone, "PORT", 0)
    assert client.json("POST", "/api/settings", {"phone_enabled": True})[0] == 200
    ph = Client(client.json("GET", "/api/phone")[1]["address"])
    code = client.json("POST", "/api/phone/code")[1]["code"]
    ph.cookie = _pair_cookie(ph, code)[1].split(";")[0]
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "gray").save(buf, "PNG")

    desk = client.json("POST", "/api/uploads", buf.getvalue(), ctype="image/png")[1]
    assert desk["path"] == str(paths.uploads_dir() / desk["name"])  # the PC window: unchanged
    up = ph.json("POST", "/api/uploads", buf.getvalue(), ctype="image/png")[1]
    real = str(paths.uploads_dir() / up["name"])
    monkeypatch.setattr(history, "reuse", lambda i: JobRequest(kind="upscale", init_image=real))
    pages = ["/api/models", "/api/settings", "/api/history", "/api/history/7"]
    for path in [*pages, "/api/history/7/reuse"]:
        status, _, data = ph.call("GET", path)
        text = data.decode()
        assert status == 200 and not re.search(r"[A-Za-z]:[\\/]", text), path
        assert latentroom_home.parent.name not in text, path
    assert not re.search(r"[A-Za-z]:[\\/]", json.dumps(up)) and up["path"].endswith(up["name"])
    entry = ph.json("GET", "/api/history/7")[1]
    assert entry["image"].split("/")[-1] == "out.png"  # the name stays: Save to phone uses it
    assert ph.json("GET", "/api/history")[1]["entries"][0]["image"] == entry["image"]  # one key

    edit = {"kind": "edit", "prompt": "a lighthouse at dusk", "refs": [up["path"], entry["image"]]}
    assert ph.json("POST", "/api/jobs", edit)[0] == 200
    assert srv.queue().submitted[-1].refs == [real, str(latentroom_home / "out.png")]
    again = ph.json("GET", "/api/history/7/reuse")[1]["init_image"]
    assert ph.json("POST", "/api/jobs", {"kind": "upscale", "init_image": again})[0] == 200
    assert srv.queue().submitted[-1].init_image == real
    for bad in ["C:/a.png", real, "0123456789abcdef/out.png"]:  # made up, or a stale key
        status, body = ph.json("POST", "/api/jobs", {"kind": "upscale", "init_image": bad})
        assert (status, body["error"]["code"]) == (404, "not_found"), bad
    assert len(srv.queue().submitted) == 2
