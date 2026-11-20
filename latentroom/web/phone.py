"""v2 "Use from my phone": Tailscale address, pairing codes, phone tokens, QR code.
 The HTTP side lives in server.py; this module holds no sockets.

Phone tokens are stored only as sha256 hashes in paths.app_dir()/phones.json, so the file
never holds a usable credential. "Forget all phones" deletes that file."""

import hashlib
import hmac
import io
import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import segno

from latentroom.engine import paths
from latentroom.errors import LatentroomError
from latentroom.settings import write_json_atomic

PORT = 47615  # fixed so a phone bookmark keeps working; a random port when it is taken
COOKIE = "lr_phone"
CODE_TTL = 300  # seconds a pairing code stays valid
MAX_WRONG = 5  # wrong codes per minute from one address (one phone); then 429 for that one only
TAILNET = ipaddress.ip_network("100.64.0.0/10")
NO_TAILSCALE = "No Tailscale address found on this PC."  # Settings lists the steps under it
WRONG_CODE = "That code is wrong or too old. Make a new code on the PC and type it again."
TOO_MANY = "Too many wrong codes. Wait a minute and try again."

_store_lock = threading.Lock()


def _is_tailnet(text: str) -> bool:
    try:
        return ipaddress.ip_address(text) in TAILNET
    except ValueError:
        return False


def _tailscale_exe() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale/tailscale.exe"
    return str(default) if default.is_file() else None


def tailscale_ip() -> tuple[str | None, str]:
    """(IPv4 address, "") or (None, plain reason). `tailscale ip -4` first, then any local
    IPv4 address inside 100.64.0.0/10. Local only: no network call leaves the PC."""
    exe = _tailscale_exe()
    if exe:
        try:
            out = subprocess.run(
                [exe, "ip", "-4"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            for word in out.split():
                if _is_tailnet(word):
                    return word, ""
        except OSError, subprocess.SubprocessError:
            pass
    try:
        found = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        found = []
    for info in found:
        if _is_tailnet(info[4][0]):
            return info[4][0], ""
    return None, NO_TAILSCALE


class Pairing:
    """One 6-digit code at a time, single use, CODE_TTL seconds, wrong tries rate-limited per
    client address: a device on the tailnet failing codes on purpose never locks the user's
    phone out. One address gets MAX_WRONG * CODE_TTL / 60 = 25 guesses per code."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._expires = 0.0
        self._wrong: dict[str, deque[float]] = {}

    def new_code(self) -> tuple[str, float]:
        with self._lock:
            self._code = f"{secrets.randbelow(10**6):06d}"
            self._expires = self._clock() + CODE_TTL
            return self._code, self._expires

    def pair(self, code: str, client: str) -> str:
        """Right code -> a new long phone token (remembered). Else LatentroomError."""
        with self._lock:
            now = self._clock()
            for who, tries in list(self._wrong.items()):
                while tries and tries[0] <= now - 60:
                    tries.popleft()
                if not tries:
                    del self._wrong[who]
            tries = self._wrong.setdefault(client, deque())
            if len(tries) >= MAX_WRONG:
                raise LatentroomError("too_many_tries", TOO_MANY, 429)
            ok = (
                self._code is not None
                and now < self._expires
                and hmac.compare_digest(code.encode(), self._code.encode())
            )
            if not ok:
                tries.append(now)
                raise LatentroomError("bad_request", WRONG_CODE)
            self._code = None
        token = secrets.token_urlsafe(32)
        remember(token)
        return token


def _store() -> Path:
    return paths.app_dir() / "phones.json"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hashes() -> list[str]:
    data = paths.read_json(_store())  # busy for a moment != empty: that forgot every phone
    found = data.get("tokens", []) if isinstance(data, dict) else []
    return [h for h in found if isinstance(h, str)] if isinstance(found, list) else []


def remember(token: str) -> None:
    with _store_lock:
        write_json_atomic(_store(), {"tokens": [*_hashes(), _hash(token)]})


def known(token: str) -> bool:
    h = _hash(token)
    return any(hmac.compare_digest(h, x) for x in _hashes())


def count() -> int:
    return len(_hashes())


def forget_all() -> None:
    with _store_lock:
        _store().unlink(missing_ok=True)


def qr_svg(text: str) -> str:
    """QR code as inline SVG, made locally (segno, pure Python). Never a Micro QR: phone
    cameras do not read those. A viewBox, no fixed size: it scales into its box.
    Border 4 = the quiet zone the QR standard asks for."""
    buf = io.BytesIO()
    segno.make_qr(text, error="m").save(buf, kind="svg", border=4, omitsize=True, xmldecl=False)
    return buf.getvalue().decode("utf-8")
