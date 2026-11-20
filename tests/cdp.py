"""Minimal Chrome DevTools Protocol client for the browser tests: stdlib only (socket + WebSocket
frames). Starts headless Chrome on a throwaway profile, drives one page, collects console errors."""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import time
import warnings
from pathlib import Path

CHROME_PATHS = [
    Path(os.environ.get(var, "C:/"), "Google/Chrome/Application/chrome.exe")
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
]
# a page load that hits this is loaded once more: Windows now and then fails one request on
# 127.0.0.1 with WSAENOBUFS, 1 full run in ~10; not port exhaustion, cause not found
RETRY = "net::ERR_NO_BUFFER_SPACE"


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if p.is_file():
            return str(p)
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")


class Chrome:
    def __init__(self, exe: str, profile: Path) -> None:
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        port_file, end = profile / "DevToolsActivePort", time.monotonic() + 20
        while True:
            try:  # Chrome may still be writing it (half a file, or locked on Windows)
                port, path = port_file.read_text().split()
                break
            except OSError, ValueError:
                if time.monotonic() > end or self.proc.poll() is not None:
                    self.proc.kill()
                    raise RuntimeError("Chrome did not open its DevTools port") from None
                time.sleep(0.05)
        self.sock = socket.create_connection(("127.0.0.1", int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        assert head.startswith(b"HTTP/1.1 101"), head
        self.next_id, self.events, self.session = 0, [], None
        target = self.call("Target.createTarget", url="about:blank")["targetId"]
        attached = self.call("Target.attachToTarget", targetId=target, flatten=True)
        self.session = attached["sessionId"]
        for domain in ("Page", "Runtime", "Log"):
            self.call(domain + ".enable")
        # Windows Server (CI) turns animations off, so Chrome reports reduced motion and the app
        # rightly stops its animations: pin the normal setting; a test may still ask for reduce.
        motion = {"name": "prefers-reduced-motion", "value": "no-preference"}
        self.call("Emulation.setEmulatedMedia", features=[motion])

    # ---- WebSocket frames (RFC 6455): client frames are masked, server frames are not ----
    def _send(self, text: str) -> None:
        data, mask = text.encode(), os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = int.from_bytes(data, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")
        self.sock.sendall(head + mask + masked.to_bytes(n, "big"))

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Chrome closed the DevTools connection")
            buf += chunk
        return bytes(buf)

    def _recv(self) -> dict:
        msg = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            n = struct.unpack("!H", self._read(2))[0] if n == 126 else (
                struct.unpack("!Q", self._read(8))[0] if n == 127 else n)  # fmt: skip
            if b0 & 0x0F == 0x8:
                raise ConnectionError("Chrome closed the DevTools connection")
            msg += self._read(n)
            if b0 & 0x80:  # FIN: last fragment
                return json.loads(msg)

    def call(self, method: str, **params):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params}
        if self.session:
            msg["sessionId"] = self.session
        self._send(json.dumps(msg))
        while True:
            m = self._recv()
            if m.get("id") == self.next_id:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m["result"]
            if "method" in m:
                self.events.append(m)

    # ---- page helpers ----
    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"{expr}: {r['exceptionDetails']}")
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 15, show: str = "") -> None:
        """Poll until the JS expression is true. `show`: JS whose value goes into the error."""
        end = time.monotonic() + timeout
        while not self.eval(expr):
            if time.monotonic() > end:
                seen = f" (page: {self.eval(show)!r})" if show else ""
                raise TimeoutError(f"still false after {timeout} s: {expr}{seen}")
            time.sleep(0.05)

    def open(self, url: str, width: int, height: int) -> None:
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)  # fmt: skip
        for again in (False, True):
            first = len(self.events)
            self.call("Page.navigate", url=url)
            self.wait("document.readyState === 'complete' && location.href !== 'about:blank'")
            self.eval("document.fonts.ready.then(() => true)")
            if again or not any(
                e["method"] == "Log.entryAdded" and RETRY in e["params"]["entry"]["text"]
                for e in self.events[first:]
            ):
                return
            warnings.warn(f"{RETRY} while loading {url}: loaded it once more", stacklevel=2)
            del self.events[first:]  # that load's errors; the new load's own still count
            self.call("Page.navigate", url="about:blank")  # same URL + #hash would not reload

    def errors(self) -> list[str]:
        """Console errors, uncaught exceptions and failed loads seen so far."""
        self.eval("1")  # pull in events that arrived since the last call
        out = []
        for e in self.events:
            m, p = e["method"], e["params"]
            if m == "Runtime.exceptionThrown":
                d = p["exceptionDetails"]
                out.append("exception: " + d.get("exception", {}).get("description", d["text"]))
            elif m == "Runtime.consoleAPICalled" and p["type"] in ("error", "assert"):
                args = (str(a.get("value", a.get("description"))) for a in p["args"])
                out.append("console: " + " ".join(args))
            elif m == "Log.entryAdded" and p["entry"]["level"] == "error":
                out.append(f"log: {p['entry']['text']} {p['entry'].get('url', '')}")
        return out

    def screenshot(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.session = None
            self.call("Browser.close")
        except OSError, RuntimeError:
            pass
        self.sock.close()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
